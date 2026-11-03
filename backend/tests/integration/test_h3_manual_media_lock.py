from __future__ import annotations

from uuid import UUID

import pytest
from conftest import PostgresDatabase, ProductionParentChain
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_h3_storyboard_binding import _seed_current_media, _writer

from tubeflow_test.adapters.persistence.models import MediaAssetRow
from tubeflow_test.video_generation.orchestration import AuthoritativeGroupInput
from tubeflow_test.video_generation.persistence import VideoAttemptRecord


@pytest.mark.integration
def test_manual_intent_source_read_precedes_storyboard_row_lock(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    """The real asset reader verifies the source row again in its own session."""
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    writer = _writer(postgres_database)
    original_loader = writer._group_loader

    def load_with_real_media_lock(
        session: Session, owner_id: UUID, group_id: UUID
    ) -> AuthoritativeGroupInput:
        source = original_loader(session, owner_id, group_id)
        with postgres_database.sessions.begin() as reader_session:
            locked = reader_session.scalar(
                select(MediaAssetRow)
                .where(MediaAssetRow.id == source.storyboard_image.asset_id)
                .with_for_update()
            )
            assert locked is not None
            assert locked.sha256 == source.storyboard_image.sha256
        return source

    writer._group_loader = load_with_real_media_lock
    attempt_id = writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_id,
        idempotency_key="manual-real-media-lock",
    )

    with postgres_database.sessions() as session:
        attempt = session.get(VideoAttemptRecord, attempt_id)
        assert attempt is not None
        assert attempt.stage == "CREATED"
