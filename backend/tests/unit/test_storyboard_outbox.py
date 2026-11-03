from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from temporalio.common import WorkflowIDReusePolicy

from tubeflow_test.application.storyboard_dispatcher import StoryboardOutboxDispatcher
from tubeflow_test.application.storyboard_outbox import StoryboardOutboxEvent
from tubeflow_test.workflows.storyboards import TemporalStoryboardWorkflowStarter


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 28, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Repo:
    def __init__(self) -> None:
        self.event = StoryboardOutboxEvent(
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
            "DIRECTOR_GENERATION_REQUESTED",
            "a" * 64,
            "stable-workflow",
            uuid4(),
            1,
        )
        self.acked = False
        self.retried = False

    def claim_outbox(
        self, now: datetime, lease_until: datetime, token: UUID
    ) -> StoryboardOutboxEvent | None:
        del now, lease_until
        return (
            None
            if self.acked
            else StoryboardOutboxEvent(
                self.event.event_id,
                self.event.owner_id,
                self.event.project_id,
                self.event.aggregate_id,
                self.event.event_type,
                self.event.request_sha256,
                self.event.workflow_id,
                token,
                self.event.attempt_count,
            )
        )

    def acknowledge_outbox(self, event_id: UUID, token: UUID, now: datetime) -> bool:
        del event_id, token, now
        self.acked = True
        return True

    def retry_outbox(self, event_id: UUID, token: UUID, next_at: datetime, error: str) -> bool:
        del event_id, token, next_at, error
        self.retried = True
        return True


class Starter:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.workflow_ids: list[str] = []

    async def start(self, event: StoryboardOutboxEvent) -> None:
        self.workflow_ids.append(event.workflow_id)
        if self.fail:
            raise OSError


@pytest.mark.asyncio
async def test_dispatcher_acks_stable_workflow_start() -> None:
    repo, starter = Repo(), Starter()
    dispatcher = StoryboardOutboxDispatcher(repo, starter, Clock(), Ids())
    assert await dispatcher.dispatch_one()
    assert repo.acked and starter.workflow_ids == ["stable-workflow"]


@pytest.mark.asyncio
async def test_dispatcher_releases_failed_start_for_retry() -> None:
    repo = Repo()
    dispatcher = StoryboardOutboxDispatcher(repo, Starter(fail=True), Clock(), Ids())
    assert not await dispatcher.dispatch_one()
    assert repo.retried and not repo.acked


@pytest.mark.asyncio
async def test_temporal_starter_routes_recovery_explicitly_and_rejects_unknown() -> None:
    class Client:
        def __init__(self) -> None:
            self.workflow: str | None = None
            self.options: dict[str, object] = {}

        async def start_workflow(self, workflow: str, *args: object, **kwargs: object) -> None:
            del args
            self.workflow = workflow
            self.options = kwargs

    client = Client()
    starter = TemporalStoryboardWorkflowStarter(client, "storyboards")  # type: ignore[arg-type]
    base = Repo().event
    recovery = StoryboardOutboxEvent(
        base.event_id,
        base.owner_id,
        base.project_id,
        base.aggregate_id,
        "DIRECTOR_RECOVERY_REQUESTED",
        base.request_sha256,
        base.workflow_id,
        base.lease_token,
        base.attempt_count,
    )
    await starter.start(recovery)
    assert client.workflow == "DirectorRecoveryWorkflow"
    assert client.options["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
    with pytest.raises(ValueError, match="unsupported"):
        await starter.start(
            StoryboardOutboxEvent(
                recovery.event_id,
                recovery.owner_id,
                recovery.project_id,
                recovery.aggregate_id,
                "UNKNOWN",
                recovery.request_sha256,
                recovery.workflow_id,
                recovery.lease_token,
                recovery.attempt_count,
            )
        )
