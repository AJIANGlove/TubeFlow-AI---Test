from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorRevisionRow,
    StoryboardGenerationRow,
)
from tubeflow_test.video_generation.persistence import (
    ProviderAuditEventRecord,
    VideoAttemptRecord,
)
from tubeflow_test.video_generation.provider_audit import ProviderAuditEvent, canonical_hash
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository


@pytest.mark.integration
def test_0010_append_only_identity_concurrent_sequence_and_wide_fields(
    postgres_database, production_parent_chain
) -> None:
    ids = production_parent_chain
    revision_id, generation_id, attempt_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    with postgres_database.sessions.begin() as session:
        session.add(
            DirectorRevisionRow(
                id=revision_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                group_id=ids.group_id,
                revision_number=1,
                kind="GRID_SHEET",
                shots_json=[],
                links_json=[],
                group_sentence_ids_json=[],
                review_state="APPROVED",
                continuous_observation_approvals_json=[],
                created_at=now,
            )
        )
        session.add(
            StoryboardGenerationRow(
                id=generation_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                group_id=ids.group_id,
                applied_revision_id=revision_id,
                version=1,
                kind="GRID_SHEET",
                input_json={},
                idempotency_key="storyboard",
                provider_idempotency_key="storyboard-provider",
                canonical_request_sha256="a" * 64,
                status="SUCCEEDED",
                state_revision=1,
                created_at=now,
                checkpoint_json={},
            )
        )
        session.add(
            VideoAttemptRecord(
                attempt_id=attempt_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                group_id=ids.group_id,
                generation_id=generation_id,
                idempotency_key="video",
                generation_sha256="b" * 64,
                request_sha256="c" * 64,
                request_json={},
                stage="CREATED",
                staged_ids=[],
                cleaned_staged_ids=[],
                archive_stage="NOT_STARTED",
                retry_evidence=[],
                slot_claimed=False,
                intent_sequence=1,
                revision=1,
                updated_at=now,
            )
        )
    repository = SqlAlchemyVideoRepository(
        postgres_database.sessions,
        lambda *_args: None,
        lambda value: value,  # type: ignore[arg-type]
    )

    def append(index: int) -> None:
        receipt = {"id": f"safe-{index}", "status": "RECORDED"}
        repository.append_provider_event(
            ProviderAuditEvent(
                attempt_id,
                ids.owner_id,
                ids.project_id,
                ids.group_id,
                "kinestack",
                "node-1",
                "s" * 512,
                "workflow",
                "1",
                "minimax-h3",
                "1",
                "video",
                "remote-1",
                "ARCHIVE_CONFIRM_INSPECT_INVALID_RECEIPT",
                datetime.now(UTC),
                "c" * 64,
                receipt,
                canonical_hash(receipt),
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(append, range(32)))
    with postgres_database.sessions.begin() as session:
        rows = tuple(
            session.scalars(
                select(ProviderAuditEventRecord)
                .where(ProviderAuditEventRecord.attempt_id == attempt_id)
                .order_by(ProviderAuditEventRecord.event_sequence)
            )
        )
        assert [row.event_sequence for row in rows] == list(range(1, 33))
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text(
                    "UPDATE provider_audit_events SET event_type='MUTATED' "
                    "WHERE attempt_id=:attempt"
                ),
                {"attempt": attempt_id},
            )

    other_project_id, other_group_id = uuid4(), uuid4()
    with postgres_database.sessions.begin() as session:
        session.add(
            ProjectRow(
                id=other_project_id,
                owner_id=ids.owner_id,
                channel_id=ids.channel_id,
                title="Independent audit identity target",
                script="Independent audit identity target.",
                character_image_ids=[ids.character_id],
                visual_style="documentary",
                voice_version_id=ids.voice_id,
                locale="en-US",
                aspect_ratio="16:9",
                draft_revision=1,
                production_state="ACTIVE",
                production_revision=1,
            )
        )
        session.flush()
        session.add(
            AuthoritativeNarrationGroupRecord(
                id=other_group_id,
                project_id=other_project_id,
                owner_id=ids.owner_id,
                snapshot_sha256="9" * 64,
                snapshot_revision=1,
                sequence=2,
                start_frame=0,
                end_frame=48_000,
                sample_rate_hz=48_000,
                start_ms=0,
                end_ms=1_000,
                video_blocked=False,
                block_reason=None,
            )
        )
    with pytest.raises(IntegrityError), postgres_database.sessions.begin() as session:
        session.add(
            ProviderAuditEventRecord(
                event_id=uuid4(),
                attempt_id=attempt_id,
                owner_id=ids.owner_id,
                project_id=other_project_id,
                group_id=other_group_id,
                event_sequence=33,
                event_type="INSPECT_RECEIPT",
                occurred_at=now,
                provider="kinestack",
                node_id="node-1",
                schema_version="kinestack.h3.v1",
                workflow_id="workflow",
                workflow_version="1",
                model_key="minimax-h3",
                model_version="1",
                local_idempotency_key="video",
                provider_task_key="remote-1",
                request_sha256="c" * 64,
                receipt_json={},
                receipt_sha256=canonical_hash({}),
            )
        )
