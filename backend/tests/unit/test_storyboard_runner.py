from __future__ import annotations

import asyncio

import pytest

from tubeflow_test.workflows.storyboard_runner import SupervisedStoryboardRunner


class BlockingWorker:
    async def run(self) -> None:
        await asyncio.Event().wait()


class ReadyDispatcher:
    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def dispatch_one(self) -> bool:
        self.called.set()
        return False


@pytest.mark.asyncio
async def test_runner_is_unready_until_dispatcher_and_worker_are_running() -> None:
    dispatcher = ReadyDispatcher()
    runner = SupervisedStoryboardRunner(BlockingWorker(), dispatcher, 0.01)  # type: ignore[arg-type]
    assert not runner.healthy
    task = asyncio.create_task(runner.run())
    await dispatcher.called.wait()
    assert runner.healthy
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runner.healthy


@pytest.mark.asyncio
async def test_runner_treats_normal_worker_exit_as_unhealthy() -> None:
    class ExitingWorker:
        async def run(self) -> None:
            return

    runner = SupervisedStoryboardRunner(ExitingWorker(), ReadyDispatcher())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        await runner.run()
    assert not runner.healthy
