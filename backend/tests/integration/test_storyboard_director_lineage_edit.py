from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorPointerRow,
    DirectorRevisionRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    DirectorRevisionLineageRow,
    ProjectDirectorCurrentRow,
    ProjectDirectorGroupCandidateRow,
    ProjectDirectorMaterializationRow,
    ProjectDirectorRevisionRow,
    ProjectDirectorRunRow,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    ReviewState,
    ShotSentenceLink,
)

CREATED_AT = datetime(2026, 8, 31, tzinfo=UTC)


def _revision(
    *,
    owner_id: UUID,
    project_id: UUID,
    group_id: UUID,
    sentence_id: UUID,
    description: str,
) -> DirectorPlanRevision:
    shot = DirectorShot(
        uuid4(),
        1,
        "A materially distinct edited scene",
        "wide",
        "eye-level",
        "The subject completes the edited action",
        "establishing",
        description,
    )
    return DirectorPlanRevision(
        uuid4(),
        owner_id,
        project_id,
        group_id,
        1,
        DirectorRevisionKind.DRAFT,
        (shot,),
        (ShotSentenceLink(shot.shot_id, sentence_id),),
        (sentence_id,),
        ReviewState.NOT_REQUIRED,
        (),
        CREATED_AT,
    )


def _add_initial_draft(
    session: Session,
    *,
    owner_id: UUID,
    project_id: UUID,
    group_id: UUID,
    sentence_id: UUID,
) -> UUID:
    revision_id, shot_id = uuid4(), uuid4()
    session.add(
        DirectorRevisionRow(
            id=revision_id,
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            revision_number=1,
            kind="DRAFT",
            shots_json=[
                {
                    "shot_id": str(shot_id),
                    "order": 1,
                    "scene": "The initial scene",
                    "shot_size": "wide",
                    "camera_viewpoint": "eye-level",
                    "subject_action_outcome": "The initial action completes",
                    "narrative_function": "establishing",
                    "description": "The immutable initial draft",
                }
            ],
            links_json=[{"shot_id": str(shot_id), "sentence_id": str(sentence_id)}],
            group_sentence_ids_json=[str(sentence_id)],
            review_state="NOT_REQUIRED",
            continuous_observation_approvals_json=[],
            created_at=CREATED_AT,
        )
    )
    session.flush()
    session.add(
        DirectorPointerRow(
            owner_id=owner_id,
            group_id=group_id,
            draft_revision_id=revision_id,
            applied_revision_id=None,
        )
    )
    return revision_id


def _add_materialized_lineage(
    session: Session,
    *,
    owner_id: UUID,
    project_id: UUID,
    group_id: UUID,
    initial_revision_id: UUID | None,
    sentence_id: UUID,
) -> tuple[UUID, UUID, UUID]:
    run_id, preplan_revision_id = uuid4(), uuid4()
    materialization_id, candidate_id = uuid4(), uuid4()
    run = ProjectDirectorRunRow(
        id=run_id,
        owner_id=owner_id,
        project_id=project_id,
        trigger="CONFIRM_NARRATION",
        idempotency_key=f"lineage-run-{run_id.hex}",
        canonical_input_sha256="a" * 64,
        input_json={},
        snapshot_sha256="b" * 64,
        snapshot_revision=1,
        model="gpt-5.6-sol",
        parameters_json={},
        status="SUCCEEDED",
        total_units=1,
        state_revision=1,
        revision_id=None,
        failure_code=None,
        retry_blocked=False,
        created_at=CREATED_AT,
    )
    session.add(run)
    session.flush()
    session.add(
        ProjectDirectorRevisionRow(
            id=preplan_revision_id,
            owner_id=owner_id,
            project_id=project_id,
            run_id=run_id,
            revision_number=1,
            snapshot_sha256="b" * 64,
            snapshot_revision=1,
            sentence_ids_json=[str(sentence_id)],
            shots_json=[],
            links_json=[],
            quality_conflicts_json=[],
            created_at=CREATED_AT,
        )
    )
    session.flush()
    run.revision_id = preplan_revision_id
    session.add(
        ProjectDirectorMaterializationRow(
            id=materialization_id,
            owner_id=owner_id,
            project_id=project_id,
            preplan_revision_id=preplan_revision_id,
            snapshot_sha256="b" * 64,
            snapshot_revision=1,
            algorithm_version="sentence-id-source-lineage-remap-v2",
            canonical_input_sha256="c" * 64,
            status="READY",
            created_at=CREATED_AT,
        )
    )
    session.flush()
    session.add(
        ProjectDirectorGroupCandidateRow(
            id=candidate_id,
            materialization_id=materialization_id,
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            group_sequence=1,
            shots_json=[],
            links_json=[],
            source_shot_links_json=[],
            conflicts_json=[],
            review_state="NOT_REQUIRED",
            final_revision_id=initial_revision_id,
        )
    )
    session.flush()
    if initial_revision_id is not None:
        session.add(
            DirectorRevisionLineageRow(
                revision_id=initial_revision_id,
                project_director_revision_id=preplan_revision_id,
                materialization_id=materialization_id,
                candidate_id=candidate_id,
            )
        )
    return preplan_revision_id, materialization_id, candidate_id


@pytest.mark.integration
def test_page_edits_inherit_materialized_lineage_and_keep_old_revisions(
    postgres_database: Any,
    production_parent_chain: Any,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    group_id = production_parent_chain.group_id
    sentence_id = uuid4()
    with sessions.begin() as session:
        initial_revision_id = _add_initial_draft(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            sentence_id=sentence_id,
        )
        lineage_source = _add_materialized_lineage(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            initial_revision_id=initial_revision_id,
            sentence_id=sentence_id,
        )

    repository = SqlAlchemyStoryboardRepository(sessions.begin)
    first_edit = repository.create_revision(
        _revision(
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            sentence_id=sentence_id,
            description="The first page edit",
        ),
        initial_revision_id,
    )
    second_edit = repository.create_revision(
        _revision(
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            sentence_id=sentence_id,
            description="The latest page edit",
        ),
        first_edit.revision_id,
    )
    with pytest.raises(DomainError) as stale:
        repository.create_revision(
            _revision(
                owner_id=owner_id,
                project_id=project_id,
                group_id=group_id,
                sentence_id=sentence_id,
                description="A stale concurrent edit",
            ),
            initial_revision_id,
        )
    assert stale.value.code == "revision_conflict"

    current = repository.get_draft_revision(owner_id, group_id)
    assert current is not None
    assert current.revision_id == second_edit.revision_id
    assert (first_edit.revision_number, second_edit.revision_number) == (2, 3)

    with sessions() as session:
        lineage_rows = session.scalars(select(DirectorRevisionLineageRow)).all()
        revision_ids = set(session.scalars(select(DirectorRevisionRow.id)).all())
        pointer = session.get(DirectorPointerRow, (owner_id, group_id))
    assert {item.revision_id for item in lineage_rows} == {
        initial_revision_id,
        first_edit.revision_id,
        second_edit.revision_id,
    }
    assert {
        (
            item.project_director_revision_id,
            item.materialization_id,
            item.candidate_id,
        )
        for item in lineage_rows
    } == {lineage_source}
    assert revision_ids == {
        initial_revision_id,
        first_edit.revision_id,
        second_edit.revision_id,
    }
    assert pointer is not None and pointer.draft_revision_id == second_edit.revision_id


@pytest.mark.integration
def test_plain_draft_edit_does_not_invent_project_director_lineage(
    postgres_database: Any,
    production_parent_chain: Any,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    group_id = production_parent_chain.group_id
    sentence_id = uuid4()
    with sessions.begin() as session:
        initial_revision_id = _add_initial_draft(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            sentence_id=sentence_id,
        )

    repository = SqlAlchemyStoryboardRepository(sessions.begin)
    edited = repository.create_revision(
        _revision(
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            sentence_id=sentence_id,
            description="A plain draft edit",
        ),
        initial_revision_id,
    )

    with sessions() as session:
        assert session.get(DirectorRevisionLineageRow, initial_revision_id) is None
        assert session.get(DirectorRevisionLineageRow, edited.revision_id) is None
        assert session.scalar(select(DirectorRevisionLineageRow.revision_id)) is None
    current = repository.get_draft_revision(owner_id, group_id)
    assert current is not None and current.revision_id == edited.revision_id


@pytest.mark.integration
def test_first_conflicted_candidate_edit_creates_and_then_inherits_lineage(
    postgres_database: Any,
    production_parent_chain: Any,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    group_id = production_parent_chain.group_id
    sentence_id = uuid4()
    with sessions.begin() as session:
        lineage_source = _add_materialized_lineage(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            initial_revision_id=None,
            sentence_id=sentence_id,
        )
        session.add(
            ProjectDirectorCurrentRow(
                owner_id=owner_id,
                project_id=project_id,
                revision_id=lineage_source[0],
                pointer_version=1,
            )
        )

    repository = SqlAlchemyStoryboardRepository(sessions.begin)
    first = repository.create_revision(
        _revision(
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            sentence_id=sentence_id,
            description="The first conflict-resolving page edit",
        ),
        None,
        lineage_source[2],
    )
    second = repository.create_revision(
        _revision(
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            sentence_id=sentence_id,
            description="The later immutable page edit",
        ),
        first.revision_id,
    )

    with sessions() as session:
        first_lineage = session.get(DirectorRevisionLineageRow, first.revision_id)
        second_lineage = session.get(DirectorRevisionLineageRow, second.revision_id)
    assert first_lineage is not None
    assert second_lineage is not None
    assert (
        first_lineage.project_director_revision_id,
        first_lineage.materialization_id,
        first_lineage.candidate_id,
    ) == lineage_source
    assert (
        second_lineage.project_director_revision_id,
        second_lineage.materialization_id,
        second_lineage.candidate_id,
    ) == lineage_source


@pytest.mark.integration
def test_first_candidate_edit_rejects_a_candidate_from_another_group(
    postgres_database: Any,
    production_parent_chain: Any,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    target_group_id = production_parent_chain.group_id
    source_group_id, sentence_id = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            AuthoritativeNarrationGroupRecord(
                id=source_group_id,
                project_id=project_id,
                owner_id=owner_id,
                snapshot_sha256="b" * 64,
                snapshot_revision=1,
                sequence=999,
                start_frame=0,
                end_frame=48000,
                sample_rate_hz=48000,
                start_ms=0,
                end_ms=1000,
                video_blocked=False,
                block_reason=None,
            )
        )
        session.flush()
        lineage_source = _add_materialized_lineage(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=source_group_id,
            initial_revision_id=None,
            sentence_id=sentence_id,
        )
        session.add(
            ProjectDirectorCurrentRow(
                owner_id=owner_id,
                project_id=project_id,
                revision_id=lineage_source[0],
                pointer_version=1,
            )
        )

    repository = SqlAlchemyStoryboardRepository(sessions.begin)
    with pytest.raises(DomainError) as raised:
        repository.create_revision(
            _revision(
                owner_id=owner_id,
                project_id=project_id,
                group_id=target_group_id,
                sentence_id=sentence_id,
                description="Must not attach a foreign group candidate",
            ),
            None,
            lineage_source[2],
        )
    assert raised.value.code == "invalid_project_director_candidate"
    with sessions() as session:
        assert session.get(DirectorPointerRow, (owner_id, target_group_id)) is None
        assert session.scalar(select(DirectorRevisionRow.id)) is None
        assert session.scalar(select(DirectorRevisionLineageRow.revision_id)) is None


@pytest.mark.integration
def test_first_candidate_edit_rejects_a_stale_materialization_candidate(
    postgres_database: Any,
    production_parent_chain: Any,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    group_id = production_parent_chain.group_id
    sentence_id = uuid4()
    with sessions.begin() as session:
        stale_source = _add_materialized_lineage(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=group_id,
            initial_revision_id=None,
            sentence_id=sentence_id,
        )
        session.add(
            ProjectDirectorCurrentRow(
                owner_id=owner_id,
                project_id=project_id,
                revision_id=stale_source[0],
                pointer_version=1,
            )
        )
        current_materialization_id, current_candidate_id = uuid4(), uuid4()
        session.add(
            ProjectDirectorMaterializationRow(
                id=current_materialization_id,
                owner_id=owner_id,
                project_id=project_id,
                preplan_revision_id=stale_source[0],
                snapshot_sha256="b" * 64,
                snapshot_revision=1,
                algorithm_version="sentence-id-source-lineage-remap-v2",
                canonical_input_sha256="d" * 64,
                status="REVIEW_REQUIRED",
                created_at=CREATED_AT + timedelta(seconds=1),
            )
        )
        session.flush()
        session.add(
            ProjectDirectorGroupCandidateRow(
                id=current_candidate_id,
                materialization_id=current_materialization_id,
                owner_id=owner_id,
                project_id=project_id,
                group_id=group_id,
                group_sequence=1,
                shots_json=[],
                links_json=[],
                source_shot_links_json=[],
                conflicts_json=[],
                review_state="REQUIRED",
                final_revision_id=None,
            )
        )

    repository = SqlAlchemyStoryboardRepository(sessions.begin)
    with pytest.raises(DomainError) as raised:
        repository.create_revision(
            _revision(
                owner_id=owner_id,
                project_id=project_id,
                group_id=group_id,
                sentence_id=sentence_id,
                description="Must not attach a stale materialization candidate",
            ),
            None,
            stale_source[2],
        )
    assert raised.value.code == "revision_conflict"
    with sessions() as session:
        assert session.get(DirectorPointerRow, (owner_id, group_id)) is None
        assert session.scalar(select(DirectorRevisionRow.id)) is None
        assert session.scalar(select(DirectorRevisionLineageRow.revision_id)) is None
