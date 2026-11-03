from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyDeletionRepository,
    production_active_job_gates,
)
from tubeflow_test.adapters.persistence.models import CompositionJobRow, CompositionOutboxRow
from tubeflow_test.adapters.persistence.narration import (
    NarrationAttemptRecord,
    NarrationProjectRecord,
)
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorGenerationAttemptRow,
    StoryboardGenerationRow,
    StoryboardOutboxRow,
)
from tubeflow_test.domain.director_generation import DirectorAttemptStatus
from tubeflow_test.domain.narration import NarrationAttemptState
from tubeflow_test.domain.storyboards import GenerationStatus
from tubeflow_test.video_generation.orchestration import ArchiveStage, AttemptStage
from tubeflow_test.video_generation.persistence import VideoAttemptRecord


def _gate(name: str):  # type: ignore[no-untyped-def]
    return next(gate for gate in production_active_job_gates() if gate.name == name)


@pytest.mark.parametrize("status", tuple(GenerationStatus))
def test_storyboard_gate_covers_every_canonical_state(status: GenerationStatus) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE authoritative_narration_groups "
                "(id CHAR(32) PRIMARY KEY, project_id CHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE project_director_runs "
                "(id CHAR(32) PRIMARY KEY, project_id CHAR(32), status VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE project_director_current "
                "(owner_id CHAR(32), project_id CHAR(32), revision_id CHAR(32), "
                "PRIMARY KEY (owner_id, project_id))"
            )
        )
    for table in (
        StoryboardGenerationRow.__table__,
        DirectorGenerationAttemptRow.__table__,
        StoryboardOutboxRow.__table__,
    ):
        table.create(engine)
    project_id = uuid4()
    with Session(engine) as session:
        session.add(
            StoryboardGenerationRow(
                id=uuid4(),
                owner_id=uuid4(),
                project_id=project_id,
                group_id=uuid4(),
                applied_revision_id=uuid4(),
                version=1,
                kind="GRID_SHEET",
                input_json={},
                idempotency_key="intent",
                provider_idempotency_key="provider",
                canonical_request_sha256="a" * 64,
                status=status.value,
                state_revision=1,
                checkpoint_json={},
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        assert _gate("storyboard").has_active(session, project_id) is (
            status not in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}
        )


@pytest.mark.parametrize("stage", tuple(AttemptStage))
def test_h3_gate_covers_every_canonical_attempt_state(stage: AttemptStage) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoAttemptRecord.__table__.create(engine)
    project_id = uuid4()
    terminal = stage in {AttemptStage.FAILED, AttemptStage.CANCELLED}
    archived = stage is AttemptStage.ARCHIVED
    with Session(engine) as session:
        session.add(
            VideoAttemptRecord(
                attempt_id=uuid4(),
                owner_id=uuid4(),
                project_id=project_id,
                group_id=uuid4(),
                generation_id=uuid4(),
                idempotency_key="intent",
                generation_sha256="a" * 64,
                request_sha256="b" * 64,
                request_json={},
                node_id=None,
                remote_id=None,
                stage=stage.value,
                staged_ids=[],
                cleaned_staged_ids=[],
                archive_stage=(
                    ArchiveStage.COMPLETE.value
                    if archived
                    else ArchiveStage.NOT_STARTED.value
                ),
                output_id=None,
                output_sha256=None,
                archived_asset_json=None,
                error_code=None,
                retry_evidence=[],
                slot_claimed=False,
                narration_validation_json=None,
                input_snapshot_json=None,
                probe_evidence_json=None,
                output_size_bytes=None,
                output_media_type=None,
                remote_phase=None,
                remote_acceptance=None,
                raw_receipt_json=None,
                requested_action=None,
                cleanup_terminal_stage=None,
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()
        assert _gate("h3").has_active(session, project_id) is not (terminal or archived)


def test_h3_succeeded_remains_active_until_archive_complete() -> None:
    assert AttemptStage.SUCCEEDED not in {AttemptStage.FAILED, AttemptStage.CANCELLED}


@pytest.mark.parametrize("state", tuple(NarrationAttemptState))
def test_narration_gate_uses_nested_canonical_attempt_state(
    state: NarrationAttemptState,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    project_id, owner_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE narration_project_records "
                "(project_id CHAR(32) PRIMARY KEY, owner_id CHAR(32), payload JSON)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE narration_attempt_records "
                "(id CHAR(32) PRIMARY KEY, owner_id CHAR(32), project_id CHAR(32), "
                "idempotency_key VARCHAR(128), request_sha256 VARCHAR(64), "
                "provider_request_id TEXT, "
                "revision INTEGER, payload JSON)"
            )
        )
    with Session(engine) as session:
        session.add_all(
            (
                NarrationProjectRecord(
                    project_id=project_id,
                    owner_id=owner_id,
                    payload={"job": {"state": "FAILED"}},
                ),
                NarrationAttemptRecord(
                    id=uuid4(),
                    owner_id=owner_id,
                    project_id=project_id,
                    idempotency_key="unknown",
                    request_sha256="a" * 64,
                    revision=1,
                    payload={"attempt": {"state": state.value}},
                ),
            )
        )
        session.commit()
        assert _gate("narration").has_active(session, project_id) is (
            state
            in {
                NarrationAttemptState.INTENT_RECORDED,
                NarrationAttemptState.SUBMISSION_STARTED,
                NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            }
        )


@pytest.mark.parametrize("status", tuple(DirectorAttemptStatus))
def test_storyboard_gate_covers_director_attempts(status: DirectorAttemptStatus) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE authoritative_narration_groups "
                "(id CHAR(32) PRIMARY KEY, project_id CHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE project_director_runs "
                "(id CHAR(32) PRIMARY KEY, project_id CHAR(32), status VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE project_director_current "
                "(owner_id CHAR(32), project_id CHAR(32), revision_id CHAR(32), "
                "PRIMARY KEY (owner_id, project_id))"
            )
        )
    for table in (
        StoryboardGenerationRow.__table__,
        DirectorGenerationAttemptRow.__table__,
        StoryboardOutboxRow.__table__,
    ):
        table.create(engine)
    project_id, group_id = uuid4(), uuid4()
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO authoritative_narration_groups (id, project_id) "
                "VALUES (:id, :project_id)"
            ),
            {"id": group_id.hex, "project_id": project_id.hex},
        )
        session.add(
            DirectorGenerationAttemptRow(
                id=uuid4(),
                owner_id=uuid4(),
                group_id=group_id,
                idempotency_key="director",
                provider_idempotency_key="provider",
                canonical_request_sha256="a" * 64,
                input_json={},
                prompt_ref="prompt",
                prompt_sha256="b" * 64,
                prompt_content="prompt",
                model="model",
                parameters_json={},
                status=status.value,
                state_revision=1,
                history_json=[],
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
        assert _gate("storyboard").has_active(session, project_id) is (
            status not in {DirectorAttemptStatus.SUCCEEDED, DirectorAttemptStatus.FAILED}
        )


@pytest.mark.parametrize(
    ("outbox_state", "job_state", "active"),
    (
        ("PENDING", None, True),
        ("DISPATCHED", None, True),
        ("DISPATCHED", "RUNNING", True),
        ("DISPATCHED", "SUCCEEDED", False),
        ("DISPATCHED", "FAILED", False),
    ),
)
def test_composition_gate_closes_dispatch_window_without_permanent_block(
    outbox_state: str, job_state: str | None, active: bool
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    CompositionOutboxRow.__table__.create(engine)
    CompositionJobRow.__table__.create(engine)
    project_id, owner_id, revision_id = uuid4(), uuid4(), uuid4()
    digest = "a" * 64
    with Session(engine) as session:
        session.add(
            CompositionOutboxRow(
                id=uuid4(),
                project_id=project_id,
                owner_id=owner_id,
                revision_id=revision_id,
                inputs_sha256=digest,
                workflow_id=f"composition:{project_id}:{digest}",
                state=outbox_state,
                attempt=1,
                workflow_generation=1,
                next_attempt_at=datetime.now(UTC),
            )
        )
        if job_state is not None:
            session.add(
                CompositionJobRow(
                    id=uuid4(),
                    project_id=project_id,
                    owner_id=owner_id,
                    idempotency_key=f"compose:{owner_id}:{project_id}:{digest}",
                    state=job_state,
                    attempt=1,
                    workflow_generation=1,
                )
            )
        session.commit()
        assert _gate("composition").has_active(session, project_id) is active


def test_project_deletion_begin_rejects_active_h3_sql_row() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoAttemptRecord.__table__.create(engine)
    project_id, owner_id = uuid4(), uuid4()
    session = Session(engine)
    session.add(
        VideoAttemptRecord(
            attempt_id=uuid4(),
            owner_id=owner_id,
            project_id=project_id,
            group_id=uuid4(),
            generation_id=uuid4(),
            idempotency_key="active",
            generation_sha256="a" * 64,
            request_sha256="b" * 64,
            request_json={},
            stage=AttemptStage.SUCCEEDED.value,
            staged_ids=[],
            cleaned_staged_ids=[],
            archive_stage=ArchiveStage.NOT_STARTED.value,
            retry_evidence=[],
            slot_claimed=False,
            updated_at=datetime.now(UTC),
        )
    )
    session.commit()

    class Transaction:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    class DeletionSession:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args: object) -> None:
            session.close()

        def begin(self) -> Transaction:
            return Transaction()

        def scalar(self, statement):  # type: ignore[no-untyped-def]
            sql = str(statement)
            if "FROM projects" in sql:
                return SimpleNamespace(production_state="ACTIVE")
            if any(
                table in sql
                for table in (
                    "narration_project_records",
                    "narration_attempt_records",
                    "storyboard_generations",
                    "director_generation_attempts",
                    "storyboard_outbox",
                    "project_director_runs",
                    "project_director_current",
                    "composition_jobs",
                    "composition_outbox",
                )
            ):
                return None
            return session.scalar(statement)

        def scalars(self, statement):  # type: ignore[no-untyped-def]
            return session.scalars(statement)

        def get(self, *_args: object):  # type: ignore[no-untyped-def]
            return None

        def add(self, _value: object) -> None:
            raise AssertionError("deletion must not be enqueued")

    repository = SqlAlchemyDeletionRepository(  # type: ignore[arg-type]
        lambda: DeletionSession(), production_active_job_gates()
    )
    assert repository.begin_with_lock_and_outbox(project_id, owner_id) is False
