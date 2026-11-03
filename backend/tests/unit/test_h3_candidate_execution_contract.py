from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from test_video_auto_mode import setup, submit_calls

from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.engine import EngineNode, EngineUncertain
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    RequestedAction,
)

CANDIDATE_WORKFLOW = "h3.tubeflow.storyboard-to-video"


def candidate_setup():
    source, repository, engine, assets, service = setup()
    source = replace(
        source,
        workflow=replace(
            source.workflow, workflow_id=CANDIDATE_WORKFLOW, workflow_version="1.0.0"
        ),
    )
    repository.groups[source.group_id] = source
    repository.nodes = {
        key: replace(node, workflow_id=CANDIDATE_WORKFLOW, workflow_version="1.0.0")
        for key, node in repository.nodes.items()
    }
    return source, repository, engine, assets, service


@pytest.mark.parametrize("failure", ["unknown", "not_found", "transport"])
@pytest.mark.parametrize("initial_state", ["submitted", "running", "lost_submit"])
def test_candidate_uncertain_remote_keeps_attempt_and_slot(failure, initial_state):
    source, repository, engine, _assets, service = candidate_setup()
    engine.lose_submit_response = initial_state == "lost_submit"
    intent = service.trigger(source.owner_id, source.group_id, "candidate-uncertain")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.slot_claimed
    if initial_state == "running":
        submitted = service.advance(source.owner_id, intent.attempt_id)
        assert submitted.stage is AttemptStage.RUNNING
    for _ in range(8):
        engine.remote_acceptance = "UNKNOWN" if failure == "unknown" else None
        engine.inspect_not_found = failure == "not_found"
        engine.lose_inspect_response = failure == "transport"
        current = service.advance(source.owner_id, intent.attempt_id)
        assert current.stage is AttemptStage.REMOTE_STATE_UNKNOWN
        assert current.slot_claimed
        assert current.node_id == submitted.node_id
        assert current.request == submitted.request
        assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
        assert submit_calls(engine) == 1
        assert not any(call.startswith("delete:") for _, call in engine.calls)
    engine.remote_acceptance = None
    engine.lose_inspect_response = False
    engine.inspect_status = "SUCCEEDED"
    recovered = service.advance(source.owner_id, intent.attempt_id)
    assert recovered.attempt_id == intent.attempt_id
    assert recovered.stage is AttemptStage.SUCCEEDED
    assert not recovered.slot_claimed
    assert submit_calls(engine) == 1


@pytest.mark.parametrize("pending_status", ["RUNNING", "UNKNOWN", "ACCEPTED", "QUEUED"])
def test_candidate_async_cancel_keeps_intent_slot_and_operation_key(monkeypatch, pending_status):
    source, repository, engine, _assets, service = candidate_setup()
    intent = service.trigger(source.owner_id, source.group_id, "candidate-cancel")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    cancel_keys = []

    def cancel(node: EngineNode, attempt_id: UUID | str, *, idempotency_key: str):
        engine.calls.append((node.node_id, "cancel"))
        cancel_keys.append(idempotency_key)
        return {
            "attempt_id": str(attempt_id),
            "schema": "kinestack.execution.v1",
            "status": pending_status,
        }

    monkeypatch.setattr(engine, "cancel", cancel)
    service.request_action(source.owner_id, intent.attempt_id, RequestedAction.CANCEL)
    for _ in range(8):
        current = service.advance(source.owner_id, intent.attempt_id)
        assert current.requested_action is RequestedAction.CANCEL
        assert current.slot_claimed
        assert current.node_id == submitted.node_id
        assert current.cancel_idempotency_key == f"{intent.attempt_id}:cancel"
        assert current.cancel_dispatched
        assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
        assert submit_calls(engine) == 1
    assert len(cancel_keys) >= 2
    assert set(cancel_keys) == {f"{intent.attempt_id}:cancel"}
    engine.inspect_status = "CANCELLED"
    cancelled = service.advance(source.owner_id, intent.attempt_id)
    assert cancelled.stage is AttemptStage.CANCELLED
    assert cancelled.requested_action is None
    assert cancelled.archive_stage is ArchiveStage.CLEANUP_PENDING
    cleaned = service.advance(source.owner_id, intent.attempt_id)
    assert cleaned.stage is AttemptStage.CANCELLED
    assert not cleaned.slot_claimed
    assert submit_calls(engine) == 1


@pytest.mark.parametrize("candidate", [False, True])
def test_cancel_receipt_identity_validation_is_preserved(monkeypatch, candidate):
    source, _repository, engine, _assets, service = candidate_setup() if candidate else setup()
    intent = service.trigger(source.owner_id, source.group_id, "cancel-invalid-identity")
    service.advance(source.owner_id, intent.attempt_id)
    monkeypatch.setattr(
        engine,
        "cancel",
        lambda *args, **kwargs: {
            "attempt_id": str(uuid4()),
            "schema": "kinestack.execution.v1",
            "status": "RUNNING",
        },
    )
    with pytest.raises(VideoRuleViolation, match="task mismatch"):
        service.cancel(source.owner_id, intent.attempt_id)


def test_legacy_cancel_still_requires_terminal_receipt(monkeypatch):
    source, _repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "legacy-pending-cancel")
    service.advance(source.owner_id, intent.attempt_id)
    monkeypatch.setattr(
        engine,
        "cancel",
        lambda node, attempt_id, **kwargs: {
            "attempt_id": str(attempt_id),
            "schema": "kinestack.execution.v1",
            "status": "RUNNING",
        },
    )
    with pytest.raises(VideoRuleViolation, match="invalid cancel receipt status"):
        service.cancel(source.owner_id, intent.attempt_id)


def test_candidate_direct_cancel_timeout_keeps_durable_intent():
    source, repository, engine, _assets, service = candidate_setup()
    intent = service.trigger(source.owner_id, source.group_id, "candidate-cancel-timeout")
    service.advance(source.owner_id, intent.attempt_id)
    engine.lose_cancel_response_once = True
    engine.cancel_status_after_loss = "RUNNING"
    with pytest.raises(EngineUncertain):
        service.cancel(source.owner_id, intent.attempt_id)
    current = repository.get_attempt(intent.attempt_id)
    assert current is not None
    assert current.requested_action is RequestedAction.CANCEL
    assert current.slot_claimed
    assert current.cancel_dispatched
    assert current.cancel_idempotency_key == f"{intent.attempt_id}:cancel"


def test_candidate_resume_does_not_erase_pending_cancel_intent():
    source, repository, engine, _assets, service = candidate_setup()
    intent = service.trigger(source.owner_id, source.group_id, "candidate-cancel-resume")
    service.advance(source.owner_id, intent.attempt_id)
    service.request_action(source.owner_id, intent.attempt_id, RequestedAction.CANCEL)
    current = service.resume(source.owner_id, intent.attempt_id)
    assert current.stage is AttemptStage.RUNNING
    assert current.requested_action is RequestedAction.CANCEL
    assert current.slot_claimed
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
