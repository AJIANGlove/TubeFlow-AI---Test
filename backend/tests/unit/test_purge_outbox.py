from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tubeflow_test.application.purge_outbox import PurgeEnvelope, PurgeOutboxRelay
from tubeflow_test.workflows.project_purge import TemporalPurgeDispatcher


class RestartedOutbox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, object]:
        return json.loads(self.path.read_text())

    def _write(self, state: dict[str, object]) -> None:
        self.path.write_text(json.dumps(state))

    def claim(self) -> PurgeEnvelope | None:
        state = self._read()
        if state["done"]:
            return None
        return PurgeEnvelope(
            UUID(str(state["id"])),
            UUID(str(state["project_id"])),
            UUID(str(state["owner_id"])),
            int(state["attempt"]),
            int(state["workflow_generation"]),
            UUID(str(state["lease_token"])),
        )

    def succeeded(self, envelope) -> None:
        state = self._read()
        state["done"] = True
        self._write(state)

    def failed(self, envelope, error_code: str) -> None:
        state = self._read()
        state["failures"] = int(state["failures"]) + 1
        self._write(state)


class Dispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def dispatch(self, project_id, owner_id, workflow_generation: int) -> None:
        self.calls += 1
        if self.fail:
            raise OSError


@pytest.mark.asyncio
async def test_outbox_relay_recovers_from_durable_state_with_new_instance(tmp_path: Path) -> None:
    state_path = tmp_path / "outbox.json"
    state_path.write_text(
        json.dumps(
            {
                "id": str(uuid4()),
                "project_id": str(uuid4()),
                "owner_id": str(uuid4()),
                "attempt": 1,
                "workflow_generation": 1,
                "lease_token": str(uuid4()),
                "done": False,
                "failures": 0,
            }
        )
    )
    first = Dispatcher(fail=True)
    assert await PurgeOutboxRelay(RestartedOutbox(state_path), first).relay_one()
    assert json.loads(state_path.read_text())["failures"] == 1
    second = Dispatcher()
    assert await PurgeOutboxRelay(RestartedOutbox(state_path), second).relay_one()
    assert json.loads(state_path.read_text())["done"] and second.calls == 1
    assert not await PurgeOutboxRelay(RestartedOutbox(state_path), second).relay_one()


class TemporalClient:
    def __init__(self) -> None:
        self.ids: list[str] = []

    async def start_workflow(
        self, workflow_name, argument, *, id: str, task_queue: str, id_reuse_policy
    ):
        assert id_reuse_policy.name == "REJECT_DUPLICATE"
        self.ids.append(id)
        return object()


@pytest.mark.asyncio
async def test_temporal_dispatch_uses_fixed_workflow_id_for_duplicate_absorption() -> None:
    client = TemporalClient()
    dispatcher = TemporalPurgeDispatcher(client)
    project, owner = uuid4(), uuid4()
    await dispatcher.dispatch(project, owner, 1)
    await dispatcher.dispatch(project, owner, 1)
    await dispatcher.dispatch(project, owner, 2)
    assert client.ids == [
        f"project-purge:{project}:1",
        f"project-purge:{project}:1",
        f"project-purge:{project}:2",
    ]
