from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from tubeflow_test.domain.errors import DomainError

if TYPE_CHECKING:
    from tubeflow_test.application.composition import (
        CompositionRepository,
        ManualCompositionService,
    )
    from tubeflow_test.application.composition_outbox import CompositionEnvelope

COMPOSITION_TASK_QUEUE = "tubeflow-project-purge"

# 合成是单次长编码：历史实测 18.5 分钟成片约 7 分钟，36 分钟成片预计 ~14 分钟，
# 30 分钟对更大项目余量不足，故放宽到 60 分钟（仍远小于运行器 3600s 上限）。
COMPOSITION_START_TO_CLOSE = timedelta(minutes=60)


@dataclass(frozen=True, slots=True)
class CompositionWorkflowInput:
    project_id: str
    owner_id: str
    revision_id: str
    workflow_generation: int


@workflow.defn(name="manual-composition-v1")
class ManualCompositionWorkflow:
    @workflow.run
    async def run(self, request: CompositionWorkflowInput) -> None:
        await workflow.execute_activity(
            "compose-project-media-v1",
            request,
            start_to_close_timeout=COMPOSITION_START_TO_CLOSE,
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


COMPOSE_HEARTBEAT_INTERVAL_SECONDS = 10.0


async def _heartbeat_while_composing(generation: int) -> None:
    while True:
        await asyncio.sleep(COMPOSE_HEARTBEAT_INTERVAL_SECONDS)
        activity.heartbeat({"generation": generation, "state": "composing"})


def create_composition_activity(
    service: ManualCompositionService,
    auto_kick: Callable[[UUID, UUID], None] | None = None,
) -> Callable[[CompositionWorkflowInput], Coroutine[object, object, None]]:
    @activity.defn(name="compose-project-media-v1")
    async def compose(request: CompositionWorkflowInput) -> None:
        activity.heartbeat({"generation": request.workflow_generation, "state": "started"})
        heartbeat = asyncio.create_task(
            _heartbeat_while_composing(request.workflow_generation)
        )
        try:
            if activity.is_cancelled():
                raise DomainError("composition_cancelled", "composition was cancelled")
            await asyncio.to_thread(
                service.compose_revision,
                UUID(request.revision_id),
                UUID(request.project_id),
                UUID(request.owner_id),
                request.workflow_generation,
            )
        except DomainError as error:
            raise ApplicationError(error.message, type=error.code, non_retryable=True) from error
        except OSError as error:
            # 媒体管线在 OS 层的失败（E2BIG/EMFILE/ENOENT 等）：首个 attempt 已把 job
            # 标成失败态，同代际重试只会转成误导性的 composition_retry_required，
            # 因此这里如实落码并停止重试（跨代际的自动重试仍由 outbox 负责）。
            raise ApplicationError(
                f"media command failed: [Errno {error.errno}] {error.strerror}",
                type="composition_exec_failed",
                non_retryable=True,
            ) from error
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            # §15.9 踢点：合成成功/既定失败后推进 AUTO 编排；失败仅记日志（kick 内吞）。
            if auto_kick is not None:
                await asyncio.to_thread(
                    auto_kick, UUID(request.owner_id), UUID(request.project_id)
                )
        activity.heartbeat({"generation": request.workflow_generation, "state": "finished"})

    return compose


class TemporalCompositionCommands:
    def __init__(self, repository: CompositionRepository) -> None:
        self._repository = repository

    async def start(self, project_id: UUID, owner_id: UUID) -> dict[str, object]:
        receipt = self._repository.enqueue_composition(project_id, owner_id)
        return {
            "status": receipt.state,
            "workflow_id": receipt.workflow_id,
            "workflow_generation": receipt.workflow_generation,
        }

    async def retry(self, project_id: UUID, owner_id: UUID) -> dict[str, object]:
        receipt = self._repository.retry_composition(project_id, owner_id)
        return {
            "status": receipt.state,
            "workflow_id": receipt.workflow_id,
            "workflow_generation": receipt.workflow_generation,
        }


class TemporalStartPort(Protocol):
    async def start_workflow(
        self,
        workflow_name: str,
        argument: object,
        *,
        id: str,
        task_queue: str,
        id_reuse_policy: WorkflowIDReusePolicy,
    ) -> object: ...


class TemporalCompositionDispatcher:
    def __init__(self, client: TemporalStartPort) -> None:
        self._client = client

    async def dispatch(self, envelope: CompositionEnvelope) -> None:
        try:
            await self._client.start_workflow(
                "manual-composition-v1",
                CompositionWorkflowInput(
                    str(envelope.project_id),
                    str(envelope.owner_id),
                    str(envelope.revision_id),
                    envelope.workflow_generation,
                ),
                id=envelope.workflow_id,
                task_queue=COMPOSITION_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return
