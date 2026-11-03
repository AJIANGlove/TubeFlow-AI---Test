from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_video_orchestration import setup

from tubeflow_test.video_generation import temporal as video_temporal
from tubeflow_test.video_generation.orchestration import ArchiveStage, AttemptStage
from tubeflow_test.video_generation.temporal import RecoverVideoCommand
from tubeflow_test.video_generation.worker import create_video_worker

_long_io_activity_runs = 0


@activity.defn(name="test_h3_long_io_with_continuous_heartbeat")
async def _long_io_with_continuous_heartbeat(attempt_id: str) -> int:
    global _long_io_activity_runs
    _long_io_activity_runs += 1
    async with video_temporal._continuous_heartbeat(
        UUID(attempt_id),
        interval_seconds=0.03,
    ):
        await asyncio.to_thread(time.sleep, 1.0)
    return _long_io_activity_runs


@workflow.defn(name="TestH3HeartbeatWorkflow", sandboxed=False)
class _HeartbeatWorkflow:
    @workflow.run
    async def run(self, attempt_id: str) -> int:
        return await workflow.execute_activity(
            "test_h3_long_io_with_continuous_heartbeat",
            attempt_id,
            start_to_close_timeout=timedelta(seconds=2),
            heartbeat_timeout=timedelta(milliseconds=300),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )


@pytest.mark.asyncio
async def test_continuous_heartbeat_stays_in_activity_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    activity_marker: ContextVar[str] = ContextVar("activity_marker")
    activity_marker.set("attempt-context")
    observed: list[tuple[str, str]] = []

    def heartbeat(detail: str) -> None:
        observed.append((activity_marker.get(), detail))

    monkeypatch.setattr(video_temporal.activity, "heartbeat", heartbeat)
    attempt_id = uuid4()
    async with video_temporal._continuous_heartbeat(attempt_id, interval_seconds=0.01):
        await asyncio.sleep(0.055)

    assert len(observed) >= 3
    assert all(marker == "attempt-context" for marker, _detail in observed)
    assert all(detail == str(attempt_id) for _marker, detail in observed)


@pytest.mark.asyncio
async def test_continuous_heartbeat_propagates_background_context_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    def heartbeat(_detail: str) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("Not in activity context")

    monkeypatch.setattr(video_temporal.activity, "heartbeat", heartbeat)
    try:
        async with video_temporal._continuous_heartbeat(uuid4(), interval_seconds=0.01):
            await asyncio.sleep(0.035)
    except RuntimeError as exc:
        assert "continuous activity heartbeat failed" in str(exc)
    else:
        raise AssertionError("background heartbeat failure must not be swallowed")


@pytest.mark.asyncio
async def test_temporal_environment_long_io_keeps_heartbeating_without_replay() -> None:
    global _long_io_activity_runs
    _long_io_activity_runs = 0
    attempt_id = uuid4()
    task_queue = f"h3-heartbeat-{uuid4()}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[_HeartbeatWorkflow],
            activities=[_long_io_with_continuous_heartbeat],
        ),
    ):
        result = await environment.client.execute_workflow(
            _HeartbeatWorkflow.run,
            str(attempt_id),
            id=f"h3-heartbeat-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == 1
    assert _long_io_activity_runs == 1


@pytest.mark.asyncio
async def test_production_worker_starts_and_executes_async_activity() -> None:
    class Service:
        calls = 0

        def advance(self, _owner_id: UUID, _attempt_id: UUID) -> object:
            self.calls += 1
            return type(
                "Attempt",
                (),
                {
                    "archive_stage": ArchiveStage.NOT_STARTED,
                    "stage": AttemptStage.ARCHIVED,
                    "error_code": None,
                    "stalled_at": None,
                },
            )()

    service = Service()
    task_queue = f"h3-production-worker-{uuid4()}"
    command = RecoverVideoCommand(uuid4(), uuid4())
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        create_video_worker(  # type: ignore[arg-type]
            environment.client, service, task_queue=task_queue
        ),
    ):
        result = await environment.client.execute_workflow(
            video_temporal.GroupVideoRecoveryWorkflow.run,
            command,
            id=f"h3-production-worker-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == command
    assert service.calls == 1


@pytest.mark.asyncio
async def test_temporal_byte_gate_persists_failed_without_activity_replay() -> None:
    source, repository, engine, assets, service = setup()
    raw = source.raw_audio_assets[0]
    assets.values[raw.asset_id] = b"tampered"
    intent = service.trigger(source.owner_id, source.group_id, "temporal-byte-gate")
    task_queue = f"h3-byte-gate-{uuid4()}"
    command = RecoverVideoCommand(source.owner_id, intent.attempt_id)

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        create_video_worker(environment.client, service, task_queue=task_queue),
    ):
        result = await environment.client.execute_workflow(
            video_temporal.GroupVideoRecoveryWorkflow.run,
            command,
            id=f"h3-byte-gate-{uuid4()}",
            task_queue=task_queue,
        )

    persisted = repository.get_attempt(intent.attempt_id)
    assert result == command
    assert persisted is not None
    assert persisted.stage is AttemptStage.FAILED
    assert persisted.error_code == "narration_audio_bytes_changed"
    assert persisted.revision == intent.revision + 1
    assert engine.calls == []
