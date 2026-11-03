from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

if TYPE_CHECKING:
    from tubeflow_test.application.narration import ProjectNarrationService

TASK_QUEUE = "tubeflow-narration"


@dataclass(frozen=True)
class NarrationWorkflowInput:
    job_id: str
    owner_id: str
    project_id: str
    sentence_sequences: tuple[int, ...]


@workflow.defn
class NarrationWorkflow:
    @workflow.run
    async def run(self, data: NarrationWorkflowInput) -> None:
        failures: list[int] = []
        for offset in range(0, len(data.sentence_sequences), 20):
            batch = data.sentence_sequences[offset : offset + 20]
            pending = batch
            for _provider_attempt in range(3):
                try:
                    pending = await workflow.execute_activity(
                        "synthesize_narration_batch",
                        args=[data.owner_id, data.project_id, pending],
                        start_to_close_timeout=timedelta(minutes=15),
                        schedule_to_close_timeout=timedelta(minutes=35),
                        heartbeat_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(
                            initial_interval=timedelta(seconds=10),
                            maximum_interval=timedelta(seconds=30),
                            maximum_attempts=100,
                            # 既定意图冲突（确定性错误）不进入活动级重试，避免热循环。
                            non_retryable_error_types=["NarrationIntentConflictError"],
                        ),
                    )
                except Exception:  # Remaining batches still receive an explicit result.
                    break
                if not pending:
                    break
            failures.extend(pending)
        await workflow.execute_activity(
            "finish_narration_job",
            args=[data.owner_id, data.project_id, data.job_id, tuple(sorted(set(failures)))],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


class NarrationActivities:
    def __init__(
        self,
        service: ProjectNarrationService,
        auto_kick: Callable[[UUID, UUID], None] | None = None,
    ) -> None:
        self._service = service
        # §15.9 踢点：finish_job 完成/阻断后推进 AUTO 编排；失败只记日志（kick 内吞）。
        self._auto_kick = auto_kick

    @activity.defn(name="synthesize_narration_batch")
    async def synthesize_batch(
        self, owner_id: str, project_id: str, sequences: tuple[int, ...]
    ) -> tuple[int, ...]:
        activity.heartbeat({"sequences": sequences, "phase": "before_submission"})
        heartbeat = asyncio.create_task(self._heartbeat_while_running(sequences[0]))
        try:
            return await self._service.synthesize_batch(UUID(owner_id), UUID(project_id), sequences)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    @activity.defn(name="finish_narration_job")
    async def finish_job(
        self,
        owner_id: str,
        project_id: str,
        job_id: str,
        failures: tuple[int, ...],
    ) -> None:
        await asyncio.to_thread(
            self._service.finish_job,
            UUID(owner_id),
            UUID(project_id),
            job_id,
            failures,
        )
        if self._auto_kick is not None:
            await asyncio.to_thread(self._auto_kick, UUID(owner_id), UUID(project_id))

    @staticmethod
    async def _heartbeat_while_running(sequence: int) -> None:
        while True:
            await asyncio.sleep(10)
            activity.heartbeat({"sequence": sequence, "phase": "provider_wait"})

    @activity.defn(name="finalize_narration_timeline")
    async def finalize_timeline(self, owner_id: str, project_id: str) -> None:
        self._service.finalize_timeline(UUID(owner_id), UUID(project_id))


class TemporalNarrationStarter:
    def __init__(
        self,
        client: Client | None = None,
        *,
        target: str | None = None,
        task_queue: str = TASK_QUEUE,
    ) -> None:
        if client is None and target is None:
            raise ValueError("Temporal client or target is required")
        self._client, self._target, self._task_queue = client, target, task_queue
        self._connect_lock = asyncio.Lock()

    async def _shared_client(self) -> Client:
        if self._client is None:
            async with self._connect_lock:
                if self._client is None:
                    self._client = await Client.connect(str(self._target))
        return self._client

    async def start(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        sentence_sequences: tuple[int, ...],
        job_id: str,
    ) -> str:
        client = await self._shared_client()
        with suppress(WorkflowAlreadyStartedError):
            await client.start_workflow(
                NarrationWorkflow.run,
                NarrationWorkflowInput(job_id, str(owner_id), str(project_id), sentence_sequences),
                id=job_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        return job_id
