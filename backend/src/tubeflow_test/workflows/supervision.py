from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from tubeflow_test.production_lifecycle import raise_lifecycle_errors


@dataclass(frozen=True, slots=True)
class WorkerComponent:
    name: str
    run: Callable[[], Awaitable[None]]
    ready: Callable[[], bool] = lambda: True


@dataclass(slots=True)
class WorkerReadiness:
    healthy: bool = False
    last_error: BaseException | None = None
    ready_components: set[str] = field(default_factory=set)


class SupervisedWorkerGroup:
    """Fail the process if any required worker or relay stops."""

    def __init__(
        self,
        components: Sequence[WorkerComponent],
        *,
        readiness_poll_seconds: float = 0.05,
    ) -> None:
        if not components:
            raise ValueError("at least one worker component is required")
        names = [component.name for component in components]
        if len(names) != len(set(names)):
            raise ValueError("worker component names must be unique")
        self._components = tuple(components)
        self._readiness_poll_seconds = readiness_poll_seconds
        self.state = WorkerReadiness()

    async def run(self) -> None:
        tasks: dict[asyncio.Task[None], WorkerComponent] = {
            asyncio.create_task(self._run_component(component), name=component.name): component
            for component in self._components
        }
        readiness = asyncio.create_task(self._monitor_readiness(), name="worker-readiness")
        primary: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        drain: asyncio.Future[list[None | BaseException]] | None = None
        results_processed = False
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                if not task.done():
                    task.cancel()
            drain = asyncio.gather(*tasks, return_exceptions=True)
            results, cancellations = await _shielded_results(drain)
            if cancellations:
                raise cancellations[0]
            failures: list[BaseException] = []
            for task, result in zip(tasks, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    failures.append(result)
                else:
                    failures.append(
                        RuntimeError(
                            f"worker component exited unexpectedly: {tasks[task].name}"
                        )
                    )
            results_processed = True
            if len(failures) == 1:
                raise failures[0]
            if failures:
                raise BaseExceptionGroup("worker components failed", failures)
        except BaseException as error:
            primary = error
        finally:
            self.state.healthy = False
            readiness.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if drain is None:
                drain = asyncio.gather(*tasks, return_exceptions=True)
            results, _cancellations = await _shielded_results(drain)
            if not results_processed:
                for task, result in zip(tasks, results, strict=True):
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        cleanup_errors.append(result)
                    elif not isinstance(result, BaseException):
                        cleanup_errors.append(
                            RuntimeError(
                                f"worker component exited unexpectedly: {tasks[task].name}"
                            )
                        )
            readiness_results, _readiness_cancellations = await _shielded_results(
                asyncio.gather(readiness, return_exceptions=True)
            )
            for result in readiness_results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    cleanup_errors.append(result)
        if primary is not None:
            self.state.last_error = primary
        raise_lifecycle_errors("worker supervision failed", primary, cleanup_errors)

    @staticmethod
    async def _run_component(component: WorkerComponent) -> None:
        await component.run()

    async def _monitor_readiness(self) -> None:
        while True:
            ready = {
                component.name for component in self._components if component.ready()
            }
            self.state.ready_components = ready
            self.state.healthy = len(ready) == len(self._components)
            await asyncio.sleep(self._readiness_poll_seconds)


async def _shielded_results[ResultT](
    drain: asyncio.Future[ResultT],
) -> tuple[ResultT, list[asyncio.CancelledError]]:
    cancellations: list[asyncio.CancelledError] = []
    while True:
        try:
            return await asyncio.shield(drain), cancellations
        except asyncio.CancelledError as error:
            cancellations.append(error)
