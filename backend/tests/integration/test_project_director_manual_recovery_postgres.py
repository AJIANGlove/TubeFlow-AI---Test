from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from test_project_director_recovery_postgres import (
    _Clock,
    _ProviderMustNotRun,
    _row_fingerprint,
    _run_id,
    _ScriptModel,
    _service,
    _units,
    _UuidFactory,
)

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.storyboards.models import StoryboardOutboxRow
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    ProjectDirectorRunRow,
    ProjectDirectorUnitRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
    _run_row,
    _unit_row,
)
from tubeflow_test.api.narration_runtime import SqlProjectSnapshotAdapter
from tubeflow_test.api.project_director import create_project_director_router
from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import ProjectDirectorRunStatus, ProjectDirectorTrigger
from tubeflow_test.identity_project.domain import Account, AccountRole

pytestmark = pytest.mark.integration


def _unknown(database, parents, *, multiple=False):
    owner, project = parents.owner_id, parents.project_id
    with database.sessions.begin() as session:
        row = session.get(ProjectRow, project)
        row.script = " ".join(f"Sentence {index}." for index in range(1, 19 if multiple else 13))
    SqlProjectSnapshotAdapter(
        database.sessions,
        director_model_name="gpt-5.6-sol",
        director_parameters={"temperature": 0.2},
        director_provider_configured=True,
    ).confirm_and_enqueue(owner, project)
    source_id = _run_id(database, owner, project)
    model = _ScriptModel(timeout_on=3)
    service = _service(database, model)
    service.advance(owner, source_id)
    service.advance(owner, source_id)
    assert service.advance(owner, source_id).status.value == "REMOTE_STATE_UNKNOWN"
    if multiple:
        # Reproduce a second concurrent request whose timeout was persisted after unit 3.
        with database.sessions.begin() as session:
            second_unknown = _units(session, source_id)[3]
            second_unknown.status = "REMOTE_STATE_UNKNOWN"
            second_unknown.failure_code = "second_transport_timeout"
            second_unknown.attempt_count = 1
    return service, model, source_id


def _client(service, owner_id):
    app = FastAPI()
    app.include_router(
        create_project_director_router(
            service,
            lambda: Account(owner_id, "test-owner", "hash", AccountRole.USER),
            provider_configured=True,
        )
    )
    return TestClient(app)


@pytest.mark.parametrize("multiple", [False, True])
def test_manual_unknown_recovery_api_is_audited_atomic_and_reuses_only_success(
    postgres_database, production_parent_chain, multiple
):
    database, parents = postgres_database, production_parent_chain
    service, model, source_id = _unknown(database, parents, multiple=multiple)
    with database.sessions() as session:
        original_units = _units(session, source_id)
        successes = {item.id: _row_fingerprint(item) for item in original_units[:2]}
        unknown_ids = [item.id for item in original_units if item.status == "REMOTE_STATE_UNKNOWN"]
        original_failure = session.get(ProjectDirectorRunRow, source_id).failure_code
    endpoint = f"/projects/{parents.project_id}/director-preplan/runs"
    client = _client(service, parents.owner_id)
    assert client.get(f"{endpoint}/latest").json()["allowed_actions"] == [
        "REGENERATE_DIRECTOR_PREPLAN", "REFRESH"
    ]
    headers = {"Idempotency-Key": "manual-waiver"}
    response = client.post(endpoint, headers=headers, json={})
    assert response.status_code == 202
    new_id = UUID(response.json()["run_id"])
    assert client.post(endpoint, headers=headers, json={}).json()["run_id"] == str(new_id)
    assert model.calls == [1, 2, 3]
    assert client.post(
        endpoint, headers={"Idempotency-Key": "second-click"}, json={}
    ).status_code == 409
    assert _client(service, uuid4()).post(endpoint, headers=headers, json={}).status_code == 404
    with database.sessions() as session:
        source = session.get(ProjectDirectorRunRow, source_id)
        replacement = session.get(ProjectDirectorRunRow, new_id)
        assert source.status == "FAILED"
        assert source.failure_code == "manual_evidence_waived"
        assert replacement.recovery_source_run_id == source_id
        audit = replacement.manual_recovery_audit_json
        assert audit["action"] == "manual_evidence_waived"
        assert audit["actor_id"] == str(parents.owner_id)
        assert audit["source_run_id"] == str(source_id)
        assert audit["new_run_id"] == str(new_id)
        assert audit["source_failure_code"] == original_failure
        assert set(audit["unknown_unit_ids"]) == set(map(str, unknown_ids))
        assert datetime.fromisoformat(audit["occurred_at"]).tzinfo is not None
        assert audit["confirm_new_input"] is False
        assert "confirmed_not_accepted" not in str(audit)
        for unit in _units(session, source_id):
            if unit.id in successes:
                assert _row_fingerprint(unit) == successes[unit.id]
            else:
                assert unit.status == "FAILED"
        recovered_units = _units(session, new_id)
        assert [item.reused_from_unit_id for item in recovered_units] == [
            *successes, *([None] * len(unknown_ids))
        ]
        assert session.scalar(select(func.count()).select_from(ProjectDirectorRunRow)) == 2
        assert session.scalar(select(func.count()).select_from(StoryboardOutboxRow)) == 2
    model.timeout_on = None
    for _ in range(6):
        finished = service.advance(parents.owner_id, new_id)
        if finished.status.value == "SUCCEEDED":
            break
    assert finished.status.value == "SUCCEEDED"
    assert model.calls == [1, 2, 3] + ([3, 4] if multiple else [3])
    with database.sessions() as session:
        assert session.get(ProjectDirectorRunRow, new_id).manual_recovery_audit_json == audit
        assert all(
            _row_fingerprint(session.get(ProjectDirectorUnitRow, unit_id)) == fingerprint
            for unit_id, fingerprint in successes.items()
        )


def test_manual_changed_input_requires_confirmation_and_restarts_every_unit(
    postgres_database, production_parent_chain
):
    database, parents = postgres_database, production_parent_chain
    service, model, source_id = _unknown(database, parents)
    snapshot = service.status(parents.owner_id, source_id).input_snapshot
    service._model_name = "new-model-deployment"
    endpoint = f"/projects/{parents.project_id}/director-preplan/runs"
    client = _client(service, parents.owner_id)
    headers = {"Idempotency-Key": "new-input-confirmation"}
    rejected = client.post(endpoint, headers=headers, json={})
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "director_input_changed"
    with database.sessions() as session:
        assert session.get(ProjectDirectorRunRow, source_id).status == "REMOTE_STATE_UNKNOWN"
        assert session.scalar(select(func.count()).select_from(ProjectDirectorRunRow)) == 1
    accepted = client.post(endpoint, headers=headers, json={"confirm_new_input": True})
    assert accepted.status_code == 202
    new_id = UUID(accepted.json()["run_id"])
    assert service.status(parents.owner_id, new_id).input_snapshot == snapshot
    with database.sessions() as session:
        replacement = session.get(ProjectDirectorRunRow, new_id)
        assert replacement.recovery_source_run_id is None
        assert replacement.manual_recovery_audit_json["confirm_new_input"] is True
        assert all(unit.reused_from_unit_id is None for unit in _units(session, new_id))
    model.timeout_on = None
    for _ in range(5):
        finished = service.advance(parents.owner_id, new_id)
        if finished.status.value == "SUCCEEDED":
            break
    assert finished.status.value == "SUCCEEDED"
    assert model.calls == [1, 2, 3, 1, 2, 3]


@pytest.mark.parametrize("fault", ["receipt", "global_request_hash", "unit_prompt", "inflight"])
def test_manual_waiver_rejects_inflight_or_incomplete_source_without_partial_writes(
    postgres_database, production_parent_chain, fault
):
    database, parents = postgres_database, production_parent_chain
    service, model, source_id = _unknown(database, parents, multiple=True)
    with database.sessions.begin() as session:
        units = _units(session, source_id)
        if fault == "receipt":
            units[1].provider_receipt = None
        elif fault == "global_request_hash":
            units[0].canonical_request_sha256 = "0" * 64
        elif fault == "unit_prompt":
            units[1].prompt_content += "tampered"
        else:
            units[3].status = "SUBMITTING"
            units[3].lease_token = uuid4()
            units[3].lease_until = datetime.now(UTC) + timedelta(minutes=10)
    with database.sessions() as session:
        before = _row_fingerprint(session.get(ProjectDirectorRunRow, source_id))
        units_before = [_row_fingerprint(item) for item in _units(session, source_id)]
    with pytest.raises(DomainError) as rejected:
        service.create_run(
            owner_id=parents.owner_id,
            project_id=parents.project_id,
            idempotency_key="invalid-source-waiver",
            trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
        )
    assert rejected.value.code == (
        "director_run_unresolved" if fault == "inflight" else "director_recovery_source_invalid"
    )
    with database.sessions() as session:
        assert _row_fingerprint(session.get(ProjectDirectorRunRow, source_id)) == before
        assert [_row_fingerprint(item) for item in _units(session, source_id)] == units_before
        assert session.scalar(select(func.count()).select_from(ProjectDirectorRunRow)) == 1
        assert session.scalar(select(func.count()).select_from(StoryboardOutboxRow)) == 1
    assert model.calls == [1, 2, 3]


def test_manual_waiver_outbox_insert_failure_rolls_back_source_and_audit(
    postgres_database, production_parent_chain
):
    database, parents = postgres_database, production_parent_chain
    service, _model, source_id = _unknown(database, parents)
    with database.sessions() as session:
        before = _row_fingerprint(session.get(ProjectDirectorRunRow, source_id))
        units_before = [_row_fingerprint(item) for item in _units(session, source_id)]

    def fail_outbox(_mapper, _connection, _target):
        raise RuntimeError("injected outbox failure")

    event.listen(StoryboardOutboxRow, "before_insert", fail_outbox)
    try:
        with pytest.raises(RuntimeError, match="injected outbox failure"):
            service.create_run(
                owner_id=parents.owner_id,
                project_id=parents.project_id,
                idempotency_key="outbox-fails",
                trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
            )
    finally:
        event.remove(StoryboardOutboxRow, "before_insert", fail_outbox)
    with database.sessions() as session:
        assert _row_fingerprint(session.get(ProjectDirectorRunRow, source_id)) == before
        assert [_row_fingerprint(item) for item in _units(session, source_id)] == units_before
        assert session.scalar(select(func.count()).select_from(ProjectDirectorRunRow)) == 1
        assert session.scalar(select(func.count()).select_from(StoryboardOutboxRow)) == 1


@pytest.mark.parametrize("same_key", [False, True])
def test_manual_waiver_project_lock_allows_only_one_replacement_for_concurrent_clicks(
    postgres_database, production_parent_chain, same_key
):
    database, parents = postgres_database, production_parent_chain
    _service_before, _model, source_id = _unknown(database, parents)
    barrier = Barrier(2)

    class ConcurrentRepository(SqlAlchemyProjectDirectorRepository):
        def latest_run(self, owner_id, project_id):
            result = super().latest_run(owner_id, project_id)
            barrier.wait(timeout=10)
            return result

    service = ProjectDirectorService(
        ConcurrentRepository(database.sessions.begin),
        _ProviderMustNotRun(),
        _UuidFactory(),
        _Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )

    def request(index):
        try:
            return service.create_run(
                owner_id=parents.owner_id,
                project_id=parents.project_id,
                idempotency_key="concurrent" if same_key else f"concurrent-{index}",
                trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
            ).run_id
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request, range(2)))
    ids = {item for item in results if isinstance(item, UUID)}
    assert len(ids) == 1
    assert results[0] == results[1] if same_key else (
        "director_recovery_source_invalid" in results
    )
    with database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ProjectDirectorRunRow)) == 2
        assert session.scalar(select(func.count()).select_from(StoryboardOutboxRow)) == 2
        replacement = session.get(ProjectDirectorRunRow, next(iter(ids)))
        assert replacement.manual_recovery_audit_json["source_run_id"] == str(source_id)


def test_manual_waiver_does_not_modify_historical_unknown_run_behind_newer_history(
    postgres_database, production_parent_chain
):
    database, parents = postgres_database, production_parent_chain
    service, _model, source_id = _unknown(database, parents)
    source = service.status(parents.owner_id, source_id)
    newer, units = service.prepare_run(
        context=source.input_snapshot,
        idempotency_key="historical-operations-replacement",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )
    newer = replace(
        newer,
        status=ProjectDirectorRunStatus.FAILED,
        failure_code="provider_http_400",
        created_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    with database.sessions.begin() as session:
        session.add(_run_row(newer))
        session.add_all(_unit_row(item) for item in units)
    with database.sessions() as session:
        original = _row_fingerprint(session.get(ProjectDirectorRunRow, source_id))
    response = _client(service, parents.owner_id).post(
        f"/projects/{parents.project_id}/director-preplan/runs",
        headers={"Idempotency-Key": "history-is-uncertain"},
        json={},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "director_recovery_history_unresolved"
    with database.sessions() as session:
        assert _row_fingerprint(session.get(ProjectDirectorRunRow, source_id)) == original
        assert session.scalar(select(func.count()).select_from(ProjectDirectorRunRow)) == 2
