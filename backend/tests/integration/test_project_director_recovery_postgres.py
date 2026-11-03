from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.storyboards.models import StoryboardOutboxRow
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    ProjectDirectorRunRow,
    ProjectDirectorUnitRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
)
from tubeflow_test.api.narration_runtime import SqlProjectSnapshotAdapter
from tubeflow_test.application.director_generation import (
    DirectorResponseDiagnostics,
    DirectorStructuredResponseError,
)
from tubeflow_test.application.project_director import (
    ProjectDirectorModelResult,
    ProjectDirectorService,
)
from tubeflow_test.domain.project_director import (
    ProjectDirectorTrigger,
    ProjectDirectorUnit,
    ProjectDirectorUnitKind,
)

SCRIPT_12 = " ".join(f"Sentence {index}." for index in range(1, 13))


class _UuidFactory:
    def new(self) -> UUID:
        return uuid4()


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _Result:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.receipt = "provider-receipt"
        self.raw_response_sha256 = "a" * 64


class _ScriptModel:
    """Deterministic provider stub: valid v4 payloads plus scripted failures."""

    def __init__(self, *, timeout_on: int | None = None, structured_on: int | None = None) -> None:
        self.calls: list[int] = []
        self.timeout_on = timeout_on
        self.structured_on = structured_on

    def generate(self, unit: ProjectDirectorUnit) -> ProjectDirectorModelResult:
        self.calls.append(unit.ordinal)
        if unit.ordinal == self.timeout_on:
            raise TimeoutError("response lost")
        if unit.ordinal == self.structured_on:
            raise DirectorStructuredResponseError(
                "director_response_content_json_invalid",
                provider_receipt="provider-structured-receipt",
                diagnostics=DirectorResponseDiagnostics(
                    http_status=200,
                    content_type="application/json",
                    request_id_present=True,
                    request_id_source="header",
                    raw_size_bytes=41,
                    raw_response_sha256="d" * 64,
                    parse_stage="content_json",
                    exception_class="JSONDecodeError",
                ),
            )
        if unit.kind is ProjectDirectorUnitKind.GLOBAL_PLAN:
            return _Result(
                {
                    "output_locale": unit.required_locale,
                    "chapters": [
                        {
                            "order": 1,
                            "sentence_ids": [str(item.sentence_id) for item in unit.context_before],
                            "visual_beat": "The chapter beat plays out.",
                            "setting": "A supported setting.",
                        }
                    ],
                }
            )
        return _Result(
            {
                "output_locale": unit.required_locale,
                "shots": [
                    {
                        "order": 1,
                        "scene": f"scene-{unit.ordinal}",
                        "shot_size": "WIDE",
                        "camera_viewpoint": "EYE_LEVEL",
                        "action": f"event-{unit.ordinal}",
                        "outcome": f"result-{unit.ordinal}",
                        "narrative_function": f"function-{unit.ordinal}",
                        "description": f"description-{unit.ordinal}",
                        "semantic_event": (
                            "ESTABLISH_CONTEXT" if unit.ordinal % 2 else "PERFORM_ACTION"
                        ),
                        "subject_focus": ("ENVIRONMENT" if unit.ordinal % 2 else "PRIMARY_SUBJECT"),
                        "time_relation": "SAME_MOMENT",
                        "location_relation": "SAME_LOCATION",
                        "sentence_ids": [str(item.sentence_id) for item in unit.sentences],
                    }
                ],
            }
        )


class _ProviderMustNotRun:
    def generate(self, unit: ProjectDirectorUnit) -> ProjectDirectorModelResult:
        raise AssertionError(f"Provider must not be called while creating unit {unit.ordinal}")


def _service(database: object, model: object) -> ProjectDirectorService:
    return ProjectDirectorService(
        SqlAlchemyProjectDirectorRepository(database.sessions.begin),
        model,
        _UuidFactory(),
        _Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )


def _row_fingerprint(
    row: ProjectDirectorRunRow | ProjectDirectorUnitRow,
) -> tuple[tuple[str, object], ...]:
    return tuple(
        (
            column.name,
            json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value,
        )
        for column in row.__table__.columns
        for value in (getattr(row, column.name),)
    )


def _confirm_script(database: object, owner_id: UUID, project_id: UUID) -> None:
    with database.sessions.begin() as session:
        project = session.get(ProjectRow, project_id)
        assert project is not None
        project.script = SCRIPT_12
    SqlProjectSnapshotAdapter(
        database.sessions,
        director_model_name="gpt-5.6-sol",
        director_parameters={"temperature": 0.2},
        director_provider_configured=True,
    ).confirm_and_enqueue(owner_id, project_id)


def _run_id(database: object, owner_id: UUID, project_id: UUID) -> UUID:
    with database.sessions() as session:
        run = session.scalar(
            select(ProjectDirectorRunRow).where(
                ProjectDirectorRunRow.owner_id == owner_id,
                ProjectDirectorRunRow.project_id == project_id,
            )
        )
        assert run is not None
        return run.id


def _units(session: object, run_id: UUID) -> list[ProjectDirectorUnitRow]:
    return list(
        session.scalars(
            select(ProjectDirectorUnitRow)
            .where(ProjectDirectorUnitRow.run_id == run_id)
            .order_by(ProjectDirectorUnitRow.ordinal)
        ).all()
    )


@pytest.mark.integration
def test_postgres_recovery_reuses_plan_and_succeeded_units_and_regenerates_holes(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    _confirm_script(postgres_database, owner_id, project_id)
    source_id = _run_id(postgres_database, owner_id, project_id)

    model = _ScriptModel(timeout_on=3)
    service = _service(postgres_database, model)
    service.advance(owner_id, source_id)
    service.advance(owner_id, source_id)
    uncertain = service.advance(owner_id, source_id)
    assert uncertain.status.value == "REMOTE_STATE_UNKNOWN"
    assert model.calls == [1, 2, 3]

    resolved = service.resolve_unknown(
        owner_id,
        source_id,
        "CONFIRMED_NOT_ACCEPTED",
        "Provider console proves unit 3 was not accepted",
    )
    assert resolved.status.value == "FAILED"
    assert resolved.retry_blocked is False

    with sessions() as session:
        source = session.get(ProjectDirectorRunRow, source_id)
        assert source is not None
        source_units = _units(session, source_id)
        assert [item.status for item in source_units] == [
            "SUCCEEDED",
            "SUCCEEDED",
            "FAILED",
        ]
        source_run_before = _row_fingerprint(source)
        source_units_before = [_row_fingerprint(item) for item in source_units]
        source_unit_ids = [item.id for item in source_units]
        source_provider_keys = [item.provider_idempotency_key for item in source_units]
        plan_document_sha256 = source_units[0].plan_document_sha256
        assert plan_document_sha256 is not None

    recovery = _service(postgres_database, _ProviderMustNotRun()).create_run(
        owner_id=owner_id,
        project_id=project_id,
        idempotency_key=f"manual-recovery-{uuid4()}",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )

    with sessions() as session:
        stored_recovery = session.get(ProjectDirectorRunRow, recovery.run_id)
        recovery_units = _units(session, recovery.run_id)
        recovery_outbox = session.scalar(
            select(StoryboardOutboxRow).where(
                StoryboardOutboxRow.aggregate_id == recovery.run_id,
                StoryboardOutboxRow.event_type == "PROJECT_DIRECTOR_RUN_REQUESTED",
            )
        )
        source_after = session.get(ProjectDirectorRunRow, source_id)
        source_units_after = _units(session, source_id)

        assert stored_recovery is not None
        assert stored_recovery.status == "INTENT_PERSISTED"
        assert stored_recovery.total_units == 3
        assert stored_recovery.recovery_source_run_id == source_id
        assert [item.kind for item in recovery_units] == [
            "GLOBAL_PLAN",
            "SHOT_PLAN",
            "SHOT_PLAN",
        ]
        assert [item.status for item in recovery_units] == [
            "SUCCEEDED",
            "SUCCEEDED",
            "INTENT_PERSISTED",
        ]
        assert [item.reused_from_unit_id for item in recovery_units] == [
            source_unit_ids[0],
            source_unit_ids[1],
            None,
        ]
        plan = recovery_units[0]
        assert plan.plan_document is not None
        assert plan.plan_document_sha256 == plan_document_sha256
        assert plan.provider_idempotency_key != source_provider_keys[0]
        for recovered_unit, source_unit in zip(
            recovery_units[:2], source_units_after[:2], strict=True
        ):
            assert recovered_unit.provider_receipt == source_unit.provider_receipt
            assert recovered_unit.raw_response_sha256 == source_unit.raw_response_sha256
            assert recovered_unit.shots_json == source_unit.shots_json
            assert recovered_unit.links_json == source_unit.links_json
        fresh = recovery_units[2]
        assert fresh.plan_document_sha256 == plan_document_sha256
        assert fresh.prompt_sha256 == source_units_after[2].prompt_sha256
        assert fresh.canonical_request_sha256 == source_units_after[2].canonical_request_sha256
        assert fresh.attempt_count == 0
        assert fresh.shots_json == [] and fresh.links_json == []
        assert "__GLOBAL_PLAN_DOCUMENT__" not in fresh.prompt_content
        assert recovery_outbox is not None
        assert recovery_outbox.aggregate_id == recovery.run_id
        assert recovery_outbox.request_sha256 == stored_recovery.canonical_input_sha256
        assert source_after is not None
        assert _row_fingerprint(source_after) == source_run_before
        assert [_row_fingerprint(item) for item in source_units_after] == source_units_before


@pytest.mark.integration
def test_postgres_scheduled_retries_then_terminal_failure_and_prefix_recovery(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    _confirm_script(postgres_database, owner_id, project_id)
    source_id = _run_id(postgres_database, owner_id, project_id)

    model = _ScriptModel(structured_on=2)
    service = _service(postgres_database, model)
    service.advance(owner_id, source_id)

    first = service.advance(owner_id, source_id)
    assert first.status.value == "RUNNING"
    with sessions() as session:
        run = session.get(ProjectDirectorRunRow, source_id)
        unit = _units(session, source_id)[1]
        assert run is not None and run.auto_retries_used == 1
        assert unit.status == "INTENT_PERSISTED"
        assert unit.attempt_count == 1
        assert unit.last_failure_code == "director_response_content_json_invalid"
        assert unit.next_attempt_at is not None
        assert unit.provider_receipt == "provider-structured-receipt"
        assert unit.raw_response_sha256 == "d" * 64
        assert unit.response_diagnostics_json["parse_stage"] == "content_json"
        assert unit.response_diagnostics_json["request_id_source"] == "header"

    # Force the scheduled retries to be due instead of sleeping.
    def _make_retry_due() -> None:
        with sessions.begin() as session:
            session.execute(
                text(
                    "UPDATE project_director_units SET next_attempt_at = :past "
                    "WHERE run_id = :run_id AND ordinal = 2"
                ),
                {"past": datetime.now(UTC) - timedelta(seconds=1), "run_id": source_id},
            )

    _make_retry_due()
    service.advance(owner_id, source_id)
    with sessions() as session:
        run = session.get(ProjectDirectorRunRow, source_id)
        unit = _units(session, source_id)[1]
        assert run is not None and run.auto_retries_used == 2
        assert unit.attempt_count == 2

    failed = service.advance(owner_id, source_id)
    assert failed.status.value == "RUNNING"
    with sessions() as session:
        third = _units(session, source_id)[2]
        assert third.status == "SUCCEEDED"

    _make_retry_due()
    failed = service.advance(owner_id, source_id)
    assert failed.status.value == "FAILED"
    assert failed.failure_code == "director_response_content_json_invalid"
    assert failed.auto_retries_used == 2
    assert model.calls == [1, 2, 2, 3, 2]

    with sessions() as session:
        source = session.get(ProjectDirectorRunRow, source_id)
        assert source is not None
        source_units = _units(session, source_id)
        assert [item.status for item in source_units] == [
            "SUCCEEDED",
            "FAILED",
            "SUCCEEDED",
        ]
        source_run_before = _row_fingerprint(source)
        source_units_before = [_row_fingerprint(item) for item in source_units]
        source_unit_ids = [item.id for item in source_units]
        plan_document_sha256 = source_units[0].plan_document_sha256

    recovery = _service(postgres_database, _ProviderMustNotRun()).create_run(
        owner_id=owner_id,
        project_id=project_id,
        idempotency_key=f"local-rejection-recovery-{uuid4()}",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )

    with sessions() as session:
        stored_recovery = session.get(ProjectDirectorRunRow, recovery.run_id)
        recovery_units = _units(session, recovery.run_id)
        source_after = session.get(ProjectDirectorRunRow, source_id)
        source_units_after = _units(session, source_id)

        assert stored_recovery is not None
        assert stored_recovery.recovery_source_run_id == source_id
        assert [item.status for item in recovery_units] == [
            "SUCCEEDED",
            "INTENT_PERSISTED",
            "SUCCEEDED",
        ]
        assert [item.reused_from_unit_id for item in recovery_units] == [
            source_unit_ids[0],
            None,
            source_unit_ids[2],
        ]
        hole = recovery_units[1]
        assert hole.plan_document_sha256 == plan_document_sha256
        assert hole.response_diagnostics_json == {}
        assert hole.attempt_count == 0
        assert hole.last_failure_code is None
        assert source_units_after[1].response_diagnostics_json["parse_stage"] == "content_json"
        assert source_after is not None
        assert _row_fingerprint(source_after) == source_run_before
        assert [_row_fingerprint(item) for item in source_units_after] == source_units_before
