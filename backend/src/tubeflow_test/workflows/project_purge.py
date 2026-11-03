from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.worker import Worker

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.workflows.composition import (
    ManualCompositionWorkflow,
    TemporalCompositionDispatcher,
    create_composition_activity,
)
from tubeflow_test.workflows.supervision import SupervisedWorkerGroup, WorkerComponent

if TYPE_CHECKING:
    from tubeflow_test.application.composition import ManualCompositionService
    from tubeflow_test.application.composition_outbox import (
        CompositionOutboxPort,
        CompositionOutboxRelay,
    )
    from tubeflow_test.application.deletion import ProjectDeletionService
    from tubeflow_test.application.purge_outbox import PurgeOutboxPort, PurgeOutboxRelay

PURGE_TASK_QUEUE = "tubeflow-project-purge"


@dataclass(frozen=True, slots=True)
class PurgeWorkflowInput:
    project_id: str
    owner_id: str
    workflow_generation: int


@workflow.defn(name="project-purge-v1")
class ProjectPurgeWorkflow:
    @workflow.run
    async def run(self, request: PurgeWorkflowInput) -> None:
        await workflow.execute_activity(
            "purge-project-objects-v1",
            request,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


def create_purge_activity(
    service: ProjectDeletionService,
) -> Callable[[PurgeWorkflowInput], None]:
    @activity.defn(name="purge-project-objects-v1")
    def purge(request: PurgeWorkflowInput) -> None:
        activity.heartbeat({"generation": request.workflow_generation, "state": "started"})
        try:
            if activity.is_cancelled():
                raise DomainError("project_purge_cancelled", "project purge was cancelled")
            service.clean(
                UUID(request.project_id),
                UUID(request.owner_id),
                workflow_generation=request.workflow_generation,
            )
        except DomainError as error:
            raise ApplicationError(error.message, type=error.code, non_retryable=True) from error
        activity.heartbeat({"generation": request.workflow_generation, "state": "finished"})

    return purge


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


class TemporalPurgeDispatcher:
    def __init__(self, client: TemporalStartPort) -> None:
        self._client = client

    async def dispatch(self, project_id: UUID, owner_id: UUID, workflow_generation: int) -> None:
        try:
            await self._client.start_workflow(
                "project-purge-v1",
                PurgeWorkflowInput(str(project_id), str(owner_id), workflow_generation),
                id=f"project-purge:{project_id}:{workflow_generation}",
                task_queue=PURGE_TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return


async def run_purge_worker(
    *,
    temporal_target: str,
    service: ProjectDeletionService,
    compositions: ManualCompositionService,
    outbox: PurgeOutboxPort,
    composition_outbox: CompositionOutboxPort,
) -> None:
    from tubeflow_test.application.composition_outbox import CompositionOutboxRelay
    from tubeflow_test.application.purge_outbox import PurgeOutboxRelay

    client = await Client.connect(temporal_target)
    purge_relay = PurgeOutboxRelay(outbox, TemporalPurgeDispatcher(client))
    composition_relay = CompositionOutboxRelay(
        composition_outbox, TemporalCompositionDispatcher(client)
    )
    activity_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="tubeflow-purge-activity"
    )
    try:
        worker = Worker(
            client,
            task_queue=PURGE_TASK_QUEUE,
            workflows=[ProjectPurgeWorkflow, ManualCompositionWorkflow],
            activities=[create_purge_activity(service), create_composition_activity(compositions)],
            activity_executor=activity_executor,
        )

        relay_ready = {"purge-outbox-relay": False, "composition-outbox-relay": False}

        async def relay_forever(
            name: str, relay: PurgeOutboxRelay | CompositionOutboxRelay
        ) -> None:
            while True:
                while await relay.relay_one():
                    pass
                relay_ready[name] = True
                await asyncio.sleep(1)

        group = SupervisedWorkerGroup(
            [
                WorkerComponent("purge-temporal-worker", worker.run),
                WorkerComponent(
                    "purge-outbox-relay",
                    lambda: relay_forever("purge-outbox-relay", purge_relay),
                    lambda: relay_ready["purge-outbox-relay"],
                ),
                WorkerComponent(
                    "composition-outbox-relay",
                    lambda: relay_forever("composition-outbox-relay", composition_relay),
                    lambda: relay_ready["composition-outbox-relay"],
                ),
            ]
        )
        await group.run()
    finally:
        activity_executor.shutdown()
