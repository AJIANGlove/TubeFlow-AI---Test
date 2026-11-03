from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    ProjectDirectorCurrentRow,
    ProjectDirectorGroupCandidateRow,
    ProjectDirectorMaterializationRow,
    ProjectDirectorRevisionRow,
    ProjectDirectorRunRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
)
from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorConflict,
    ProjectDirectorGroupCandidate,
    ProjectDirectorMaterialization,
    ProjectDirectorSourceShotLink,
    ProjectDirectorUnit,
)
from tubeflow_test.domain.storyboards import DirectorShot, ReviewState, ShotSentenceLink


class _Ids:
    def new(self) -> UUID:
        return uuid4()


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 31, tzinfo=UTC)


class _UnusedModel:
    def generate(self, unit: ProjectDirectorUnit) -> object:
        raise AssertionError(f"provider must not be called for unit {unit.unit_id}")


def _add_revision(
    session: object,
    *,
    owner_id: UUID,
    project_id: UUID,
    revision_number: int,
) -> tuple[UUID, UUID]:
    run_id, revision_id = uuid4(), uuid4()
    created_at = datetime(2026, 8, 31, revision_number, tzinfo=UTC)
    run = ProjectDirectorRunRow(
        id=run_id,
        owner_id=owner_id,
        project_id=project_id,
        trigger="MANUAL_REGENERATE",
        idempotency_key=f"current-materialization-run-{run_id.hex}",
        canonical_input_sha256=f"{revision_number}" * 64,
        input_json={},
        snapshot_sha256="a" * 64,
        snapshot_revision=1,
        model="gpt-5.6-sol",
        parameters_json={},
        status="SUCCEEDED",
        total_units=1,
        state_revision=1,
        revision_id=None,
        failure_code=None,
        retry_blocked=False,
        created_at=created_at,
    )
    session.add(run)
    session.flush()
    session.add(
        ProjectDirectorRevisionRow(
            id=revision_id,
            owner_id=owner_id,
            project_id=project_id,
            run_id=run_id,
            revision_number=revision_number,
            snapshot_sha256="a" * 64,
            snapshot_revision=1,
            sentence_ids_json=[str(uuid4())],
            shots_json=[],
            links_json=[],
            quality_conflicts_json=[],
            created_at=created_at,
        )
    )
    session.flush()
    run.revision_id = revision_id
    return run_id, revision_id


def _add_materialization(
    session: object,
    *,
    owner_id: UUID,
    project_id: UUID,
    group_id: UUID,
    revision_id: UUID,
    canonical_hash: str,
) -> UUID:
    materialization_id, candidate_id = uuid4(), uuid4()
    source_shot_id, candidate_shot_id, sentence_id = uuid4(), uuid4(), uuid4()
    session.add(
        ProjectDirectorMaterializationRow(
            id=materialization_id,
            owner_id=owner_id,
            project_id=project_id,
            preplan_revision_id=revision_id,
            snapshot_sha256="a" * 64,
            snapshot_revision=1,
            algorithm_version="sentence-id-source-lineage-remap-v2",
            canonical_input_sha256=canonical_hash,
            status="REVIEW_REQUIRED",
            created_at=datetime(2026, 8, 31, tzinfo=UTC),
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
            shots_json=[
                {
                    "shot_id": str(candidate_shot_id),
                    "order": 1,
                    "scene": "An immutable review candidate",
                    "shot_size": "wide",
                    "camera_viewpoint": "eye-level",
                    "subject_action_outcome": "The current revision remains explicit",
                    "narrative_function": "establishing",
                    "description": "A candidate retained only for its matching preplan revision",
                }
            ],
            links_json=[{"shot_id": str(candidate_shot_id), "sentence_id": str(sentence_id)}],
            source_shot_links_json=[
                {
                    "source_shot_id": str(source_shot_id),
                    "candidate_shot_id": str(candidate_shot_id),
                }
            ],
            conflicts_json=[
                {
                    "code": "coverage_conflict",
                    "sentence_ids": [str(sentence_id)],
                    "shot_ids": [str(candidate_shot_id)],
                }
            ],
            review_state="REQUIRED",
            final_revision_id=None,
        )
    )
    return materialization_id


@pytest.mark.integration
def test_current_materialization_never_falls_back_to_an_older_preplan_revision(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    with sessions.begin() as session:
        _old_run_id, old_revision_id = _add_revision(
            session,
            owner_id=owner_id,
            project_id=project_id,
            revision_number=1,
        )
        _new_run_id, new_revision_id = _add_revision(
            session,
            owner_id=owner_id,
            project_id=project_id,
            revision_number=2,
        )
        old_materialization_id = _add_materialization(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=production_parent_chain.group_id,
            revision_id=old_revision_id,
            canonical_hash="b" * 64,
        )
        session.add(
            ProjectDirectorCurrentRow(
                owner_id=owner_id,
                project_id=project_id,
                revision_id=new_revision_id,
                pointer_version=2,
            )
        )

    repository = SqlAlchemyProjectDirectorRepository(sessions.begin)
    service = ProjectDirectorService(
        repository,
        _UnusedModel(),
        _Ids(),
        _Clock(),
        model_name="gpt-5.6-sol",
        parameters={},
    )

    assert repository.latest_materialization(owner_id, project_id) is None
    with pytest.raises(DomainError) as raised:
        service.current_materialization(owner_id, project_id)
    assert raised.value.code == "not_found"

    with sessions.begin() as session:
        new_materialization_id = _add_materialization(
            session,
            owner_id=owner_id,
            project_id=project_id,
            group_id=production_parent_chain.group_id,
            revision_id=new_revision_id,
            canonical_hash="c" * 64,
        )

    current = repository.latest_materialization(owner_id, project_id)
    assert current is not None
    assert current.materialization_id == new_materialization_id
    assert current.materialization_id != old_materialization_id
    assert current.preplan_revision_id == new_revision_id


@pytest.mark.integration
def test_persist_materialization_flushes_parent_before_candidates_without_final_revision(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    """Regression for the FK violation hit when every first candidate lacks a final
    revision: without an intermediate flush the candidate insert could be emitted
    before the parent materialization insert and the whole persist rolled back."""
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    with sessions.begin() as session:
        _run_id, revision_id = _add_revision(
            session,
            owner_id=owner_id,
            project_id=project_id,
            revision_number=1,
        )

    repository = SqlAlchemyProjectDirectorRepository(sessions.begin)
    shot_id, sentence_id, source_shot_id = uuid4(), uuid4(), uuid4()
    value = ProjectDirectorMaterialization(
        materialization_id=uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        preplan_revision_id=revision_id,
        snapshot_sha256="a" * 64,
        snapshot_revision=1,
        algorithm_version="controlled-semantic-lineage-remap-v3",
        canonical_input_sha256="d" * 64,
        status=MaterializationStatus.REVIEW_REQUIRED,
        candidates=(
            ProjectDirectorGroupCandidate(
                candidate_id=uuid4(),
                group_id=production_parent_chain.group_id,
                group_sequence=1,
                shots=(
                    DirectorShot(
                        shot_id,
                        1,
                        "A conflicted group candidate",
                        "wide",
                        "eye-level",
                        "The group requires manual review",
                        "establishing",
                        "The first candidate has no final revision by design",
                    ),
                ),
                sentence_links=(ShotSentenceLink(shot_id, sentence_id),),
                source_shot_links=(ProjectDirectorSourceShotLink(source_shot_id, shot_id),),
                conflicts=(ProjectDirectorConflict("sentence_coverage_missing", (sentence_id,)),),
                review_state=ReviewState.REQUIRED,
                final_revision=None,
            ),
        ),
        created_at=datetime(2026, 8, 31, tzinfo=UTC),
    )

    persisted = repository.persist_materialization(value)
    assert persisted.materialization_id == value.materialization_id
    assert persisted.candidates[0].final_revision is None

    replayed = repository.persist_materialization(value)
    assert replayed.materialization_id == value.materialization_id
