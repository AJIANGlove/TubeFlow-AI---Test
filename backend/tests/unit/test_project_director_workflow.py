from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from temporalio.common import WorkflowIDReusePolicy
from test_project_director import Ids, Model, context, create
from test_project_director_readiness import ReadRepository, _service

from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.application.storyboard_outbox import StoryboardOutboxEvent
from tubeflow_test.domain.project_director import ProjectDirectorGroupContext
from tubeflow_test.workflows import storyboards as storyboard_workflows
from tubeflow_test.workflows.storyboards import (
    ProjectDirectorRunWorkflow,
    StoryboardActivities,
    StoryboardWorkflowInput,
    TemporalStoryboardWorkflowStarter,
)


@pytest.mark.asyncio
async def test_starter_routes_project_director_event_to_deduplicated_workflow() -> None:
    class Client:
        def __init__(self) -> None:
            self.workflow: str | None = None
            self.argument: StoryboardWorkflowInput | None = None
            self.options: dict[str, object] = {}

        async def start_workflow(
            self,
            workflow_name: str,
            argument: StoryboardWorkflowInput,
            **options: object,
        ) -> None:
            self.workflow = workflow_name
            self.argument = argument
            self.options = options

    owner_id, project_id, run_id = uuid4(), uuid4(), uuid4()
    event = StoryboardOutboxEvent(
        event_id=uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        aggregate_id=run_id,
        event_type="PROJECT_DIRECTOR_RUN_REQUESTED",
        request_sha256="a" * 64,
        workflow_id=f"project-director-{owner_id}-{run_id}",
        lease_token=uuid4(),
        attempt_count=1,
    )
    client = Client()

    await TemporalStoryboardWorkflowStarter(  # type: ignore[arg-type]
        client, "storyboards", parallelism=3
    ).start(event)

    assert client.workflow == "ProjectDirectorRunWorkflow"
    assert client.argument == StoryboardWorkflowInput(
        str(owner_id), str(run_id), event.request_sha256, parallelism=3
    )
    assert client.options == {
        "id": event.workflow_id,
        "task_queue": "storyboards",
        "id_reuse_policy": WorkflowIDReusePolicy.REJECT_DUPLICATE,
    }


def _activities(service: object) -> StoryboardActivities:
    return StoryboardActivities(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, service),
    )


def test_activity_advances_one_unit_then_reports_active_and_succeeded() -> None:
    value = context(2)
    model = Model()
    repository = ReadRepository(value)
    service = _service(repository, model)
    repository.groups = (
        ProjectDirectorGroupContext(
            UUID(int=6001),
            1,
            tuple(item.sentence_id for item in value.sentences),
        ),
    )
    run = create(service, value, "workflow")
    activities = _activities(service)
    workflow_input = StoryboardWorkflowInput(
        str(value.owner_id), str(run.run_id), run.canonical_input_sha256
    )

    first = activities.advance_project_director(workflow_input)

    assert first == "ACTIVE"
    assert model.calls == [1]
    assert repository.materializations == {}

    second = activities.advance_project_director(workflow_input)

    assert second == "SUCCEEDED"
    assert model.calls == [1, 2]
    materialization = service.current_materialization(value.owner_id, value.project_id)
    assert (
        materialization.preplan_revision_id
        == service.current(value.owner_id, value.project_id).revision_id
    )


def test_activity_reports_waiting_when_only_backoff_retries_remain() -> None:
    value = context(1)
    model = Model(structured_on=2)
    repository = ReadRepository(value)

    class RealClock:
        def now(self) -> datetime:
            return datetime.now(UTC)

    service = ProjectDirectorService(
        repository,
        model,
        Ids(),
        RealClock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )
    run = create(service, value, "workflow-waiting")
    activities = _activities(service)
    workflow_input = StoryboardWorkflowInput(
        str(value.owner_id), str(run.run_id), run.canonical_input_sha256
    )

    assert activities.advance_project_director(workflow_input) == "ACTIVE"
    assert activities.advance_project_director(workflow_input) == "WAITING"
    assert model.calls == [1, 2]
    unit = repository.list_units(value.owner_id, run.run_id)[1]
    assert unit.attempt_count == 1
    assert unit.next_attempt_at is not None


def _patch_activities(
    monkeypatch: pytest.MonkeyPatch,
    responses: tuple[str, ...],
) -> tuple[list[str], list[timedelta]]:
    iterator = iter(responses)
    calls: list[str] = []
    sleeps: list[timedelta] = []

    async def execute_activity(
        name: str, _value: StoryboardWorkflowInput, **_options: object
    ) -> str:
        calls.append(name)
        return next(iterator)

    async def fake_sleep(duration: timedelta) -> None:
        sleeps.append(duration)

    monkeypatch.setattr(storyboard_workflows.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(storyboard_workflows.workflow, "sleep", fake_sleep)
    return calls, sleeps


@pytest.mark.asyncio
async def test_workflow_fans_out_parallel_activities_without_sleeping_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, sleeps = _patch_activities(
        monkeypatch,
        ("ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE", "SUCCEEDED", "ACTIVE", "ACTIVE"),
    )

    await ProjectDirectorRunWorkflow().run(
        StoryboardWorkflowInput(str(uuid4()), str(uuid4()), "b" * 64, parallelism=4)
    )

    assert calls == ["advance_project_director"] * 8
    assert sleeps == []


@pytest.mark.asyncio
async def test_workflow_sleeps_one_second_only_when_every_lane_is_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, sleeps = _patch_activities(
        monkeypatch,
        (
            "WAITING",
            "WAITING",
            "WAITING",
            "WAITING",
            "SUCCEEDED",
            "WAITING",
            "WAITING",
            "WAITING",
        ),
    )

    await ProjectDirectorRunWorkflow().run(
        StoryboardWorkflowInput(str(uuid4()), str(uuid4()), "b" * 64, parallelism=4)
    )

    assert len(calls) == 8
    assert sleeps == [timedelta(seconds=1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("parallelism", (1, 2, 3))
async def test_workflow_parallelism_bounds_the_activity_fanout(
    parallelism: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_flight = 0
    max_in_flight = 0

    async def execute_activity(
        name: str, _value: StoryboardWorkflowInput, **_options: object
    ) -> str:
        del name
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return "SUCCEEDED"

    monkeypatch.setattr(storyboard_workflows.workflow, "execute_activity", execute_activity)

    await ProjectDirectorRunWorkflow().run(
        StoryboardWorkflowInput(str(uuid4()), str(uuid4()), "b" * 64, parallelism=parallelism)
    )

    assert max_in_flight == parallelism


@pytest.mark.asyncio
async def test_workflow_parallelism_is_clamped_to_at_least_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _sleeps = _patch_activities(monkeypatch, ("SUCCEEDED",))

    await ProjectDirectorRunWorkflow().run(
        StoryboardWorkflowInput(str(uuid4()), str(uuid4()), "b" * 64, parallelism=0)
    )

    assert calls == ["advance_project_director"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["FAILED", "REMOTE_STATE_UNKNOWN"])
async def test_project_director_workflow_returns_after_a_terminal_lane(
    terminal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, sleeps = _patch_activities(
        monkeypatch,
        ("ACTIVE", terminal, "ACTIVE", "ACTIVE"),
    )

    await ProjectDirectorRunWorkflow().run(
        StoryboardWorkflowInput(str(uuid4()), str(uuid4()), "b" * 64, parallelism=4)
    )

    assert calls == ["advance_project_director"] * 4
    assert sleeps == []
