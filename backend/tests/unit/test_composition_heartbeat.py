from __future__ import annotations

import time
from uuid import uuid4

import pytest
from temporalio.testing import ActivityEnvironment

from tubeflow_test.workflows import composition as composition_module


@pytest.mark.asyncio
async def test_compose_activity_heartbeats_while_compose_is_running(monkeypatch) -> None:
    beats: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        composition_module.activity, "heartbeat", lambda *details: beats.append(details)
    )
    monkeypatch.setattr(composition_module, "COMPOSE_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    env = ActivityEnvironment()

    class Service:
        def compose_revision(self, *_args: object) -> None:
            time.sleep(0.25)

    compose = composition_module.create_composition_activity(Service())  # type: ignore[arg-type]
    request = composition_module.CompositionWorkflowInput(
        project_id=str(uuid4()),
        owner_id=str(uuid4()),
        revision_id=str(uuid4()),
        workflow_generation=1,
    )
    await env.run(compose, request)
    states = [beat[0]["state"] for beat in beats]
    assert states[0] == "started"
    assert "composing" in states
    assert states[-1] == "finished"
