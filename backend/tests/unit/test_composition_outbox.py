from __future__ import annotations

from uuid import uuid4

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from tubeflow_test.application.composition_outbox import CompositionEnvelope
from tubeflow_test.workflows.composition import TemporalCompositionDispatcher


class TemporalClient:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.arguments: list[object] = []

    async def start_workflow(
        self, workflow_name, argument, *, id: str, task_queue: str, id_reuse_policy
    ):
        assert id_reuse_policy.name == "REJECT_DUPLICATE"
        self.ids.append(id)
        self.arguments.append(argument)
        if len(self.ids) > 1:
            raise WorkflowAlreadyStartedError(id, workflow_name)
        return object()


@pytest.mark.asyncio
async def test_duplicate_dispatch_uses_stable_workflow_id_and_absorbs_lost_response() -> None:
    client = TemporalClient()
    dispatcher = TemporalCompositionDispatcher(client)
    envelope = CompositionEnvelope(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "a" * 64,
        "composition:stable",
        1,
        1,
        uuid4(),
    )
    await dispatcher.dispatch(envelope)
    await dispatcher.dispatch(envelope)
    assert client.ids == ["composition:stable", "composition:stable"]
    assert [item.workflow_generation for item in client.arguments] == [1, 1]
