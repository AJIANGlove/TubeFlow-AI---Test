from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from temporalio.worker import Worker

from tubeflow_test.application.storyboard_dispatcher import StoryboardOutboxDispatcher

if TYPE_CHECKING:
    from tubeflow_test.application.storyboard_batch import StoryboardBatchService
    from tubeflow_test.application.storyboard_recovery import StoryboardRecoverySupervisor


@dataclass(slots=True)
class SupervisedStoryboardRunner:
    worker: Worker
    dispatcher: StoryboardOutboxDispatcher
    dispatch_interval_seconds: float = 0.5
    last_error: BaseException | None = None
    _healthy: bool = False
    closers: tuple[Callable[[], None], ...] = ()
    # §15：AUTO 编排器复用的批量编排服务（播种/生成；自身幂等）。
    batch: StoryboardBatchService | None = None
    recovery_sweeper: StoryboardRecoverySupervisor | None = None

    @property
    def healthy(self) -> bool:
        return self._healthy and self.last_error is None

    async def run(self) -> None:
        worker_task = asyncio.create_task(self.worker.run())
        dispatcher_task = asyncio.create_task(self._dispatch_forever())
        tasks = [worker_task, dispatcher_task]
        if self.recovery_sweeper is not None:
            tasks.append(asyncio.create_task(self._recovery_forever()))
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            completed = done.pop()
            error = completed.exception()
            if error is not None:
                raise error
            raise RuntimeError("storyboard worker component exited unexpectedly")
        except BaseException as exc:
            self._healthy = False
            self.last_error = exc
            raise

    async def _dispatch_forever(self) -> None:
        while True:
            dispatched = await self.dispatcher.dispatch_one()
            self._healthy = True
            if not dispatched:
                await asyncio.sleep(self.dispatch_interval_seconds)

    async def _recovery_forever(self) -> None:
        if self.recovery_sweeper is None:
            return
        while True:
            await self.recovery_sweeper.sweep_once()
            self._healthy = True
            await asyncio.sleep(20)

    def close(self) -> None:
        for close in reversed(self.closers):
            close()
