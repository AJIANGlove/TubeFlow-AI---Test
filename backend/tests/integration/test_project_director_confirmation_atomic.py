from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from tubeflow_test.adapters.persistence.identity_project.models import (
    ProjectRow,
    ProjectSnapshotCharacterRow,
)
from tubeflow_test.adapters.persistence.narration import NarrationProjectRecord
from tubeflow_test.adapters.persistence.storyboards.models import StoryboardOutboxRow
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    ProjectDirectorRunRow,
    ProjectDirectorUnitRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
)
from tubeflow_test.api.narration_runtime import SqlProjectSnapshotAdapter
from tubeflow_test.application.project_director import GLOBAL_PLAN_DOCUMENT_PLACEHOLDER
from tubeflow_test.domain.errors import DomainError

SCRIPT_312 = " ".join(f"Sentence {index}." for index in range(1, 313))


def _create_project(database: object, parent: object, script: str) -> UUID:
    project_id = uuid4()
    sessions = database.sessions
    with sessions.begin() as session:
        session.add(
            ProjectRow(
                id=project_id,
                owner_id=parent.owner_id,
                channel_id=parent.channel_id,
                title=f"Atomic confirmation {project_id.hex}",
                script=script,
                character_image_ids=[parent.character_id],
                visual_style="documentary realism",
                voice_version_id=parent.voice_id,
                locale="en-US",
                aspect_ratio="16:9",
                draft_revision=1,
                production_state="ACTIVE",
                production_revision=1,
            )
        )
    return project_id


def _adapter(database: object, *, configured: bool) -> SqlProjectSnapshotAdapter:
    return SqlProjectSnapshotAdapter(
        database.sessions,
        director_model_name="gpt-5.6-sol",
        director_parameters={"temperature": 0.2},
        director_provider_configured=configured,
    )


def _count(session: object, model: object, project_id: UUID) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(model).where(model.project_id == project_id)
        )
        or 0
    )


def _assert_no_confirmation_writes(database: object, project_id: UUID) -> None:
    sessions = database.sessions
    with sessions() as session:
        project = session.get(ProjectRow, project_id)
        assert project is not None and project.snapshot_confirmed_at is None
        assert session.get(NarrationProjectRecord, project_id) is None
        assert _count(session, ProjectSnapshotCharacterRow, project_id) == 0
        assert _count(session, ProjectDirectorRunRow, project_id) == 0
        assert _count(session, ProjectDirectorUnitRow, project_id) == 0
        assert _count(session, StoryboardOutboxRow, project_id) == 0


@pytest.mark.integration
def test_confirmation_atomically_persists_narration_312_units_and_one_director_outbox(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    project_id = _create_project(postgres_database, production_parent_chain, SCRIPT_312)
    adapter = _adapter(postgres_database, configured=True)
    owner_id = production_parent_chain.owner_id

    _script, _locale, _voice_id, first_job = adapter.confirm_and_enqueue(owner_id, project_id)
    _script, _locale, _voice_id, replay_job = adapter.confirm_and_enqueue(owner_id, project_id)

    assert first_job == replay_job
    assert len(first_job.sentence_sequences) == 312
    sessions = postgres_database.sessions
    with sessions() as session:
        project = session.get(ProjectRow, project_id)
        narration = session.get(NarrationProjectRecord, project_id)
        runs = session.scalars(
            select(ProjectDirectorRunRow).where(ProjectDirectorRunRow.project_id == project_id)
        ).all()
        units = session.scalars(
            select(ProjectDirectorUnitRow)
            .where(ProjectDirectorUnitRow.project_id == project_id)
            .order_by(ProjectDirectorUnitRow.ordinal)
        ).all()
        outbox = session.scalars(
            select(StoryboardOutboxRow).where(
                StoryboardOutboxRow.project_id == project_id,
                StoryboardOutboxRow.event_type == "PROJECT_DIRECTOR_RUN_REQUESTED",
            )
        ).all()

        assert project is not None and project.snapshot_confirmed_at is not None
        assert narration is not None
        assert len(narration.payload["script"]["sentences"]) == 312
        assert len(runs) == 1
        assert runs[0].total_units == 53
        assert runs[0].status == "INTENT_PERSISTED"
        assert [item.ordinal for item in units] == list(range(1, 54))
        plan = units[0]
        assert plan.kind == "GLOBAL_PLAN"
        assert plan.sentences_json == []
        assert len(plan.context_before_json) == 312
        shot_units = [item for item in units if item.kind == "SHOT_PLAN"]
        assert len(shot_units) == 52
        assert all(len(item.sentences_json) == 6 for item in shot_units)
        assert all(GLOBAL_PLAN_DOCUMENT_PLACEHOLDER in item.prompt_content for item in shot_units)
        assert all(item.plan_document_sha256 is None for item in shot_units)
        assert all(item.attempt_count == 0 for item in units)
        assert len(outbox) == 1
        assert outbox[0].aggregate_id == runs[0].id
        assert outbox[0].request_sha256 == runs[0].canonical_input_sha256
        assert _count(session, ProjectSnapshotCharacterRow, project_id) == 1


@pytest.mark.integration
def test_unconfigured_provider_rolls_back_snapshot_job_run_units_and_outbox(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    project_id = _create_project(postgres_database, production_parent_chain, "One sentence.")
    owner_id = production_parent_chain.owner_id

    with pytest.raises(DomainError) as raised:
        _adapter(postgres_database, configured=False).confirm_and_enqueue(owner_id, project_id)

    assert raised.value.code == "director_provider_not_configured"
    _assert_no_confirmation_writes(postgres_database, project_id)


@pytest.mark.integration
def test_director_intent_failure_rolls_back_entire_confirmation_transaction(
    postgres_database: object,
    production_parent_chain: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _create_project(postgres_database, production_parent_chain, "One sentence.")
    owner_id = production_parent_chain.owner_id

    def fail_create_run(
        _repository: SqlAlchemyProjectDirectorRepository,
        _session: object,
        _run: object,
        _units: object,
    ) -> object:
        raise RuntimeError("injected project director intent failure")

    monkeypatch.setattr(
        SqlAlchemyProjectDirectorRepository,
        "create_run_in_session",
        fail_create_run,
    )

    with pytest.raises(RuntimeError, match="injected project director intent failure"):
        _adapter(postgres_database, configured=True).confirm_and_enqueue(owner_id, project_id)

    _assert_no_confirmation_writes(postgres_database, project_id)
