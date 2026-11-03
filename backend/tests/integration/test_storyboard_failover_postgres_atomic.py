from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorPointerRow,
    DirectorRevisionRow,
    StoryboardGenerationRow,
    StoryboardOutboxRow,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
    _generation_row,
)
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    DirectorShot,
    GenerationStatus,
    ShotSentenceLink,
    StoryboardAssetKind,
    StoryboardGeneration,
)

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _root_generation(owner_id, project_id, group_id, revision_id) -> StoryboardGeneration:
    generation_id = uuid4()
    shot = DirectorShot(uuid4(), 1, "scene", "wide", "eye", "arrives", "establish", "desc")
    character = CharacterAssetSnapshot(1, uuid4(), uuid4(), "a" * 64, "image/png", 10)
    return StoryboardGeneration(
        generation_id=generation_id,
        owner_id=owner_id,
        project_id=project_id,
        group_id=group_id,
        applied_revision_id=revision_id,
        shots_snapshot=(shot,),
        sentence_links_snapshot=(ShotSentenceLink(shot.shot_id, uuid4()),),
        version=1,
        kind=StoryboardAssetKind.GRID_SHEET,
        prompt="grid",
        character_assets_snapshot=(character,),
        style_snapshot="ink",
        model="gpt-image-2.5",
        parameters={"aspect_ratio": "16:9", "quality": "high"},
        idempotency_key="root",
        provider_idempotency_key="b" * 64,
        canonical_request_sha256="c" * 64,
        status=GenerationStatus.SUBMITTING,
        created_at=NOW,
        state_revision=1,
        provider="grsai",
        failover_chain_id=generation_id,
        failover_policy_version="storyboard-failover-v1",
        failover_route_index=0,
        failover_model_attempt=1,
        failover_total_attempt=1,
    )


def _successor(failed: StoryboardGeneration) -> StoryboardGeneration:
    return replace(
        failed,
        generation_id=uuid4(),
        version=1,
        idempotency_key=f"auto-retry-{failed.generation_id}",
        provider_idempotency_key="d" * 64,
        status=GenerationStatus.INTENT_PERSISTED,
        state_revision=0,
        auto_attempt=2,
        supersedes_generation_id=failed.generation_id,
        failover_model_attempt=2,
        failover_total_attempt=2,
    )


@pytest.mark.integration
def test_postgres_failover_supersede_is_atomic_under_concurrency(
    postgres_database, production_parent_chain
) -> None:
    ids = production_parent_chain
    failed = _root_generation(ids.owner_id, ids.project_id, ids.group_id, uuid4())
    row = _generation_row(failed)
    revision = DirectorRevisionRow(
        id=failed.applied_revision_id,
        owner_id=ids.owner_id,
        project_id=ids.project_id,
        group_id=ids.group_id,
        revision_number=1,
        kind="APPLIED",
        shots_json=row.input_json["shots"],
        links_json=row.input_json["links"],
        group_sentence_ids_json=[item["sentence_id"] for item in row.input_json["links"]],
        review_state="NOT_REQUIRED",
        continuous_observation_approvals_json=[],
        created_at=NOW,
    )
    with postgres_database.sessions.begin() as session:
        session.add(revision)
        session.flush()
        session.add(row)
        session.add(
            DirectorPointerRow(
                owner_id=ids.owner_id,
                group_id=ids.group_id,
                draft_revision_id=revision.id,
                applied_revision_id=revision.id,
            )
        )

    @contextmanager
    def uow() -> Iterator[Session]:
        session = postgres_database.sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    first_repo = SqlAlchemyStoryboardRepository(uow)
    second_repo = SqlAlchemyStoryboardRepository(uow)

    def supersede(repo: SqlAlchemyStoryboardRepository) -> StoryboardGeneration:
        return repo.supersede_with_auto_retry(
            failed,
            failure_stage="provider",
            failure_code="error",
            successor=_successor(failed),
            next_attempt_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(supersede, (first_repo, second_repo)))
    assert all(item.status is GenerationStatus.FAILED for item in results)
    with postgres_database.sessions() as session:
        attempts = session.scalars(
            select(StoryboardGenerationRow).where(
                StoryboardGenerationRow.failover_chain_id == failed.generation_id
            )
        ).all()
        assert len(attempts) == 2
        assert max(int(item.failover_total_attempt or 0) for item in attempts) == 2
        assert session.scalar(select(func.count(StoryboardOutboxRow.id))) == 1


@pytest.mark.integration
def test_postgres_unknown_recovery_requeue_is_candidate_scoped_and_idempotent(
    postgres_database, production_parent_chain
) -> None:
    ids = production_parent_chain
    unknown = replace(
        _root_generation(ids.owner_id, ids.project_id, ids.group_id, uuid4()),
        status=GenerationStatus.REMOTE_STATE_UNKNOWN,
    )
    row = _generation_row(unknown)
    revision = DirectorRevisionRow(
        id=unknown.applied_revision_id,
        owner_id=ids.owner_id,
        project_id=ids.project_id,
        group_id=ids.group_id,
        revision_number=1,
        kind="APPLIED",
        shots_json=row.input_json["shots"],
        links_json=row.input_json["links"],
        group_sentence_ids_json=[item["sentence_id"] for item in row.input_json["links"]],
        review_state="NOT_REQUIRED",
        continuous_observation_approvals_json=[],
        created_at=NOW,
    )
    with postgres_database.sessions.begin() as session:
        session.execute(
            text("UPDATE projects SET production_mode='AUTO' WHERE id=CAST(:project_id AS UUID)"),
            {"project_id": ids.project_id},
        )
        session.add(revision)
        session.flush()
        session.add(row)

    @contextmanager
    def uow() -> Iterator[Session]:
        session = postgres_database.sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    repo = SqlAlchemyStoryboardRepository(uow)
    candidates = repo.list_unknown_recovery_candidates(NOW)
    assert len(candidates) == 1
    assert candidates[0].generation.generation_id == unknown.generation_id
    assert candidates[0].group_sequence == 1
    assert not candidates[0].has_pending_recovery
    event_id = uuid4()
    repo.authorize_generation_recovery(unknown.owner_id, unknown.generation_id, event_id, NOW)
    repo.authorize_generation_recovery(unknown.owner_id, unknown.generation_id, uuid4(), NOW)
    with postgres_database.sessions() as session:
        events = session.scalars(
            select(StoryboardOutboxRow).where(
                StoryboardOutboxRow.aggregate_id == unknown.generation_id
            )
        ).all()
        assert len(events) == 1
        assert events[0].workflow_id == f"storyboard-grid-recovery-{unknown.owner_id}-{event_id}"
    candidates = repo.list_unknown_recovery_candidates(NOW)
    assert len(candidates) == 1 and candidates[0].has_pending_recovery
    assert repo.list_unknown_recovery_candidates(NOW.replace(year=NOW.year + 1)) == ()
