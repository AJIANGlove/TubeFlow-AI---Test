from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorRevisionRow,
    StoryboardGenerationRow,
)
from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    VideoCapacityUnavailable,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import AttemptStage, PersistentAttempt
from tubeflow_test.video_generation.persistence import H3NodeRecord
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository


@pytest.mark.integration
def test_postgres_round_trip_revision_cas_and_atomic_capacity(
    postgres_database,
    storyboard_parent_chain,
) -> None:
    sessions: sessionmaker[Session] = postgres_database.sessions
    parents, generation_id = storyboard_parent_chain
    with sessions.begin() as session:
        session.add(
            H3NodeRecord(
                node_id="pg-node",
                base_url="http://pg-node",
                token_secret_ref="node-token",
                capacity=1,
                claimed_slots=0,
                enabled=True,
                health="READY",
                capability="video.reference_images_to_video",
                model_key="minimax-h3",
                model_version="1",
                workflow_id="h3-r2v-images",
                workflow_version="1",
                manifest_sha256="a" * 64,
            )
        )

    def unavailable_loader(session: Session, owner_id, group_id):  # type: ignore[no-untyped-def]
        raise AssertionError("loader is not used in repository persistence integration")

    first_repo = SqlAlchemyVideoRepository(sessions, unavailable_loader, lambda ref: ref)
    attempt = PersistentAttempt(
        uuid4(),
        parents.owner_id,
        parents.project_id,
        parents.group_id,
        generation_id,
        "pg-key",
        "b" * 64,
        "c" * 64,
        {},
    )
    saved = first_repo.create_attempt(attempt)
    del first_repo
    second_repo = SqlAlchemyVideoRepository(sessions, unavailable_loader, lambda ref: ref)
    recovered = second_repo.get_attempt(saved.attempt_id)
    assert recovered == saved
    stale = recovered
    assert stale is not None
    advanced = second_repo.save_attempt(replace(stale, stage=AttemptStage.STAGING))
    assert advanced.revision == stale.revision + 1
    with pytest.raises(VideoRuleViolation, match="revision_conflict"):
        second_repo.save_attempt(replace(stale, stage=AttemptStage.CANCELLED))

    workflow = ImmutableWorkflowSnapshot(
        "minimax-h3", "1", "h3-r2v-images", "1", "a" * 64, "preview", {}
    )
    second_repo.claim_compatible_node(advanced.attempt_id, workflow)
    other_group_id, other_revision_id, other_generation_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            AuthoritativeNarrationGroupRecord(
                id=other_group_id,
                project_id=parents.project_id,
                owner_id=parents.owner_id,
                snapshot_sha256="9" * 64,
                snapshot_revision=1,
                sequence=2,
                start_frame=48_000,
                end_frame=96_000,
                sample_rate_hz=48_000,
                start_ms=1_000,
                end_ms=2_000,
                video_blocked=False,
                block_reason=None,
            )
        )
        session.add(
            DirectorRevisionRow(
                id=other_revision_id,
                owner_id=parents.owner_id,
                project_id=parents.project_id,
                group_id=other_group_id,
                revision_number=1,
                kind="APPLIED",
                shots_json=[{"ordinal": 1, "description": "A second distinct event"}],
                links_json=[],
                group_sentence_ids_json=[],
                review_state="APPROVED",
                continuous_observation_approvals_json=[],
                created_at=datetime.now(UTC),
            )
        )
        session.flush()
        session.add(
            StoryboardGenerationRow(
                id=other_generation_id,
                owner_id=parents.owner_id,
                project_id=parents.project_id,
                group_id=other_group_id,
                applied_revision_id=other_revision_id,
                version=1,
                kind="GRID",
                input_json={},
                idempotency_key=f"storyboard-{other_generation_id.hex}",
                provider_idempotency_key=f"provider-{other_generation_id.hex}",
                canonical_request_sha256="a" * 64,
                status="SUCCEEDED",
                state_revision=1,
                created_at=datetime.now(UTC),
                checkpoint_json={},
            )
        )
    other = second_repo.create_attempt(
        PersistentAttempt(
            uuid4(),
            parents.owner_id,
            parents.project_id,
            other_group_id,
            other_generation_id,
            "pg-key-2",
            "d" * 64,
            "e" * 64,
            {},
        )
    )
    # 容量耗尽按设计抛 VideoCapacityUnavailable（orchestration 专门捕获），
    # 不是 VideoRuleViolation。
    with pytest.raises(VideoCapacityUnavailable, match="capacity"):
        second_repo.claim_compatible_node(other.attempt_id, workflow)
    claimed = second_repo.get_attempt(advanced.attempt_id)
    assert claimed is not None and claimed.slot_lease_token is not None
    second_repo.release_node(advanced.attempt_id, claimed.slot_lease_token)
    second_repo.release_node(advanced.attempt_id, claimed.slot_lease_token)
    with sessions() as session:
        node = session.get(H3NodeRecord, "pg-node")
        assert node is not None and node.claimed_slots == 0
