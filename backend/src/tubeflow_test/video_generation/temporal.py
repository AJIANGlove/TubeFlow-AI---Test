from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    DurableGroupVideoOrchestrator,
)


@dataclass(frozen=True, slots=True)
class RecoverVideoCommand:
    owner_id: UUID
    attempt_id: UUID
    stalled_poll_seconds: float = 300.0


class GroupVideoActivities:
    def __init__(
        self,
        service: DurableGroupVideoOrchestrator,
        auto_kick: Callable[[UUID, UUID], None] | None = None,
    ) -> None:
        self._service = service
        # §15.9 踢点：尝试终态（归档/失败/取消重提后）推进 AUTO 编排；失败仅记日志。
        self._auto_kick = auto_kick

    def _kick(self, owner_id: UUID, project_id: UUID) -> None:
        if self._auto_kick is not None:
            self._auto_kick(owner_id, project_id)

    @activity.defn(name="recover_group_video_attempt")
    async def recover(self, command: RecoverVideoCommand) -> str:
        async with _continuous_heartbeat(command.attempt_id):
            try:
                attempt = await asyncio.to_thread(
                    self._service.advance, command.owner_id, command.attempt_id
                )
                if (attempt.error_code or "").startswith("local_integrity_blocked:"):
                    result = "LOCAL_INTEGRITY_BLOCKED"
                elif attempt.archive_stage is ArchiveStage.CLEANUP_PENDING:
                    result = ArchiveStage.CLEANUP_PENDING.value
                elif attempt.archive_stage is ArchiveStage.QC_FAILED:
                    # QC 失败是等待用户决策的自动化终态，不再循环轮询。
                    result = "QC_FAILED"
                elif attempt.stage in {
                    AttemptStage.ARCHIVED,
                    AttemptStage.FAILED,
                    AttemptStage.CANCELLED,
                }:
                    # 终态优先于停滞标记：取消停滞尝试后 stalled_at 仍保留为历史事实，
                    # 但恢复工作流必须随终态结束，不得继续降频空转。
                    result = attempt.stage.value
                elif attempt.stalled_at is not None:
                    return "STALLED"
                else:
                    return attempt.stage.value
            except VideoRuleViolation as exc:
                if str(exc) in {
                    "no compatible H3 node has capacity",
                    "assigned H3 node capability or version no longer matches",
                }:
                    return "WAITING"
                raise
            terminal = {"ARCHIVED", "FAILED", "CANCELLED", "QC_FAILED", "LOCAL_INTEGRITY_BLOCKED"}
            if self._auto_kick is not None and result in terminal:
                await asyncio.to_thread(self._kick, command.owner_id, attempt.project_id)
            return result

    @activity.defn(name="archive_group_video_attempt")
    async def archive(self, command: RecoverVideoCommand) -> str:
        async with _continuous_heartbeat(command.attempt_id):
            attempt = await asyncio.to_thread(
                self._service.archive, command.owner_id, command.attempt_id
            )
            if self._auto_kick is not None:
                await asyncio.to_thread(self._kick, command.owner_id, attempt.project_id)
            return attempt.archive_stage.value


@asynccontextmanager
async def _continuous_heartbeat(
    attempt_id: UUID, *, interval_seconds: float = 10.0
) -> AsyncIterator[None]:
    async def beat() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            activity.heartbeat(str(attempt_id))

    activity.heartbeat(str(attempt_id))
    heartbeat_task = asyncio.create_task(beat(), name=f"h3-heartbeat-{attempt_id}")
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            if not body_failed:
                raise RuntimeError("continuous activity heartbeat failed") from exc


@workflow.defn(name="GroupVideoRecoveryWorkflow", sandboxed=False)
class GroupVideoRecoveryWorkflow:
    @workflow.run
    async def run(self, command: RecoverVideoCommand) -> RecoverVideoCommand:
        """Recover and archive only the persisted attempt selected by the caller."""
        backoff = 2
        while True:
            state = await workflow.execute_activity(
                "recover_group_video_attempt",
                command,
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2),
                    backoff_coefficient=2.0,
                    maximum_interval=timedelta(seconds=30),
                    maximum_attempts=5,
                    non_retryable_error_types=["VideoRuleViolation"],
                ),
            )
            if state in {"ARCHIVED", "FAILED", "CANCELLED", "QC_FAILED", "LOCAL_INTEGRITY_BLOCKED"}:
                return command
            if state == "STALLED":
                backoff = 2
                await workflow.sleep(timedelta(seconds=command.stalled_poll_seconds))
                continue
            await workflow.sleep(timedelta(seconds=backoff))
            backoff = min(backoff * 2, 30)
