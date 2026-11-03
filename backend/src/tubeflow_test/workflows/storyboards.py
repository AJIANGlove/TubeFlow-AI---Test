from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from temporalio import activity, workflow
from temporalio.api.enums.v1 import WorkflowExecutionStatus
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from tubeflow_test.domain.storyboards import (
    GenerationStatus,
    storyboard_auto_retry_delay_seconds,
)

if TYPE_CHECKING:
    from tubeflow_test.application.director_generation import DirectorGenerationService
    from tubeflow_test.application.project_director import ProjectDirectorService
    from tubeflow_test.application.storyboard_outbox import StoryboardOutboxEvent
    from tubeflow_test.application.storyboards import StoryboardGenerationService

_TERMINAL_ACTIVITY_RESULTS = {"SUCCEEDED", "FAILED", "REMOTE_STATE_UNKNOWN"}
# 导演 provider 单次调用的活动超时：必须大于 TUBEFLOW_DIRECTOR_PROVIDER_TIMEOUT_SECONDS（目标 900s）
# 并覆盖退避余量；与 application/project_director.py 的 PROJECT_DIRECTOR_UNIT_LEASE 同改联动
# 不变式：HTTP 超时 < 租约 ≤ 活动超时。
# 记录见 runtime/operations/director_timeout_fix_plan_20261013.md。
DIRECTOR_CALL_ACTIVITY_TIMEOUT = timedelta(minutes=20)


def _recovery_status_delay(result: object) -> tuple[str, float]:
    if isinstance(result, dict):
        return str(result["status"]), float(result["retry_after_seconds"])
    return (
        str(getattr(result, "status", result)),
        float(getattr(result, "retry_after_seconds", 5.0)),
    )


@dataclass(frozen=True)
class StoryboardWorkflowInput:
    owner_id: str
    aggregate_id: str
    request_sha256: str
    parallelism: int = 4


@dataclass(frozen=True)
class StoryboardRecoveryResult:
    status: str
    retry_after_seconds: float


@dataclass(slots=True)
class StoryboardActivities:
    director: DirectorGenerationService
    storyboards: StoryboardGenerationService
    project_director: ProjectDirectorService
    # §15.9 踢点：导演 run 终态/物化完成、故事板终态后推进 AUTO 编排（失败仅记日志）。
    auto_kick: Callable[[UUID, UUID], None] | None = None

    def _kick(self, owner_id: UUID, project_id: UUID) -> None:
        if self.auto_kick is not None:
            self.auto_kick(owner_id, project_id)

    @activity.defn(name="advance_director_generation")
    def advance_director(self, value: StoryboardWorkflowInput) -> str:
        return self.director.advance(UUID(value.owner_id), UUID(value.aggregate_id)).status.value

    @activity.defn(name="advance_storyboard_generation")
    def advance_storyboard(self, value: StoryboardWorkflowInput) -> str:
        owner_id, generation_id = UUID(value.owner_id), UUID(value.aggregate_id)
        try:
            result = self.storyboards.resume(generation_id, owner_id)
            return result.status.value
        finally:
            generation = self.storyboards.status(generation_id, owner_id)
            self.storyboards.release(generation)
            if generation.status in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}:
                self._kick(owner_id, generation.project_id)

    @activity.defn(name="reconcile_director_generation")
    def reconcile_director(self, value: StoryboardWorkflowInput) -> str:
        return self.director.reconcile(UUID(value.owner_id), UUID(value.aggregate_id)).status.value

    @activity.defn(name="reconcile_storyboard_generation")
    def reconcile_storyboard(self, value: StoryboardWorkflowInput) -> StoryboardRecoveryResult:
        # 对账终态与正常推进共用同一 kick（advance_storyboard 的 finally 中触发）。
        status = self.advance_storyboard(value)
        generation = self.storyboards.status(UUID(value.aggregate_id), UUID(value.owner_id))
        delay = 5.0
        if (
            generation.status is GenerationStatus.REMOTE_STATE_UNKNOWN
            and generation.recovery_failure_count > 0
        ):
            delay = storyboard_auto_retry_delay_seconds(generation.recovery_failure_count)
        return StoryboardRecoveryResult(status, delay)

    @activity.defn(name="advance_project_director")
    def advance_project_director(self, value: StoryboardWorkflowInput) -> str:
        owner_id, run_id = UUID(value.owner_id), UUID(value.aggregate_id)
        result = self.project_director.advance(owner_id, run_id)
        if result.status.value == "SUCCEEDED":
            self.project_director.materialize_if_ready(owner_id, result.project_id)
        if result.status.value in _TERMINAL_ACTIVITY_RESULTS:
            self._kick(owner_id, result.project_id)
            return result.status.value
        return (
            "ACTIVE"
            if self.project_director.has_claimable_unit(owner_id, run_id, datetime.now(UTC))
            else "WAITING"
        )


@workflow.defn(name="DirectorGenerationWorkflow")
class DirectorGenerationWorkflow:
    @workflow.run
    async def run(self, value: StoryboardWorkflowInput) -> None:
        while True:
            result = await workflow.execute_activity(
                "advance_director_generation",
                value,
                start_to_close_timeout=DIRECTOR_CALL_ACTIVITY_TIMEOUT,
            )
            if result in {"SUCCEEDED", "FAILED", "REMOTE_STATE_UNKNOWN"}:
                return
            await workflow.sleep(timedelta(seconds=2))


@workflow.defn(name="GridSheetGenerationWorkflow")
class GridSheetGenerationWorkflow:
    @workflow.run
    async def run(self, value: StoryboardWorkflowInput) -> None:
        while True:
            result = await workflow.execute_activity(
                "advance_storyboard_generation",
                value,
                start_to_close_timeout=timedelta(minutes=20),
            )
            if result in {"SUCCEEDED", "FAILED", "REMOTE_STATE_UNKNOWN"}:
                return
            await workflow.sleep(timedelta(seconds=2))


@workflow.defn(name="ProjectDirectorRunWorkflow")
class ProjectDirectorRunWorkflow:
    @workflow.run
    async def run(self, value: StoryboardWorkflowInput) -> None:
        parallelism = max(1, value.parallelism)
        while True:
            results = await asyncio.gather(
                *(
                    workflow.execute_activity(
                        "advance_project_director",
                        value,
                        start_to_close_timeout=DIRECTOR_CALL_ACTIVITY_TIMEOUT,
                    )
                    for _ in range(parallelism)
                )
            )
            if any(result in _TERMINAL_ACTIVITY_RESULTS for result in results):
                return
            if all(result == "WAITING" for result in results):
                await workflow.sleep(timedelta(seconds=1))


@workflow.defn(name="DirectorRecoveryWorkflow")
class DirectorRecoveryWorkflow:
    @workflow.run
    async def run(self, value: StoryboardWorkflowInput) -> None:
        while True:
            result = await workflow.execute_activity(
                "reconcile_director_generation",
                value,
                start_to_close_timeout=timedelta(minutes=10),
            )
            if result in {"SUCCEEDED", "FAILED"}:
                return
            await workflow.sleep(timedelta(seconds=5))


RECOVERY_CONTINUE_AS_NEW_ITERATIONS = 100


@workflow.defn(name="GridSheetRecoveryWorkflow")
class GridSheetRecoveryWorkflow:
    @workflow.run
    async def run(self, value: StoryboardWorkflowInput) -> None:
        for _ in range(RECOVERY_CONTINUE_AS_NEW_ITERATIONS):
            result = await workflow.execute_activity(
                "reconcile_storyboard_generation",
                value,
                start_to_close_timeout=timedelta(minutes=20),
            )
            status, delay = _recovery_status_delay(result)
            if status in {"SUCCEEDED", "FAILED"}:
                return
            await workflow.sleep(timedelta(seconds=delay))
        workflow.continue_as_new(value)


class TemporalStoryboardRecoveryProbe:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def is_active(self, workflow_id: str) -> bool | None:
        try:
            description = await self._client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False
            return None
        if description.status is None:
            return None
        return int(description.status) == int(
            WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING
        )


class TemporalStoryboardWorkflowStarter:
    def __init__(self, client: Client, task_queue: str, parallelism: int = 4) -> None:
        self._client, self._task_queue, self._parallelism = client, task_queue, parallelism

    async def start(self, event: StoryboardOutboxEvent) -> None:
        if event.event_type == "DIRECTOR_GENERATION_REQUESTED":
            workflow_name = "DirectorGenerationWorkflow"
        elif event.event_type == "PROJECT_DIRECTOR_RUN_REQUESTED":
            workflow_name = "ProjectDirectorRunWorkflow"
        elif event.event_type == "STORYBOARD_GENERATION_REQUESTED":
            workflow_name = "GridSheetGenerationWorkflow"
        elif event.event_type == "DIRECTOR_RECOVERY_REQUESTED":
            workflow_name = "DirectorRecoveryWorkflow"
        elif event.event_type == "STORYBOARD_RECOVERY_REQUESTED":
            workflow_name = "GridSheetRecoveryWorkflow"
        else:
            raise ValueError(f"unsupported storyboard outbox event: {event.event_type}")
        try:
            await self._client.start_workflow(
                workflow_name,
                StoryboardWorkflowInput(
                    str(event.owner_id),
                    str(event.aggregate_id),
                    event.request_sha256,
                    parallelism=self._parallelism,
                ),
                id=event.workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return


def create_storyboard_worker(
    client: Client,
    task_queue: str,
    activities: StoryboardActivities,
    activity_executor: Executor,
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[
            DirectorGenerationWorkflow,
            ProjectDirectorRunWorkflow,
            GridSheetGenerationWorkflow,
            DirectorRecoveryWorkflow,
            GridSheetRecoveryWorkflow,
        ],
        activities=[
            activities.advance_director,
            activities.advance_project_director,
            activities.advance_storyboard,
            activities.reconcile_director,
            activities.reconcile_storyboard,
        ],
        activity_executor=activity_executor,
    )
