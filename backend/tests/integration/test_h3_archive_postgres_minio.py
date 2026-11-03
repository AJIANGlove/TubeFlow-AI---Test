from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from minio import Minio
from minio.error import S3Error
from sqlalchemy import select

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow, StagedObjectRow
from tubeflow_test.adapters.persistence.media_repository import reconcile_staged_objects
from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_generation.adapters import build_h3_archive_adapter
from tubeflow_test.video_generation.orchestration import PersistentAttempt
from tubeflow_test.video_generation.persistence import VideoAttemptRecord
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository


@pytest.mark.integration
def test_h3_archive_concurrency_and_purge_interlock(
    postgres_database, storyboard_parent_chain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    bucket = f"h3-archive-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    parents, generation_id = storyboard_parent_chain
    sessions = postgres_database.sessions
    repo = SqlAlchemyVideoRepository(
        sessions, lambda *_args: (_ for _ in ()).throw(AssertionError()), lambda value: value
    )
    payload = b"real-h3-output"
    digest = hashlib.sha256(payload).hexdigest()
    attempt = repo.create_attempt(
        PersistentAttempt(
            uuid4(),
            parents.owner_id,
            parents.project_id,
            parents.group_id,
            generation_id,
            "archive-concurrency",
            "a" * 64,
            "b" * 64,
            {},
        )
    )
    with sessions.begin() as session:
        row = session.get(VideoAttemptRecord, attempt.attempt_id)
        assert row is not None
        row.output_sha256 = digest
        row.output_id = "provider-output"
        row.output_size_bytes = len(payload)
        row.output_media_type = "video/mp4"
        row.stage = "SUCCEEDED"
        row.archive_stage = "DOWNLOADED"
    adapter = build_h3_archive_adapter(sessions, storage)

    def archive() -> object:
        return adapter.archive_immutable(
            attempt_id=attempt.attempt_id,
            generation_id=generation_id,
            output_id="provider-output",
            content=payload,
            media_type="video/mp4",
            expected_sha256=digest,
        )

    try:
        with pytest.raises(ValueError, match="not ready"):
            adapter.archive_immutable(
                attempt_id=attempt.attempt_id,
                generation_id=generation_id,
                output_id="caller-controlled-output",
                content=payload,
                media_type="video/mp4",
                expected_sha256=digest,
            )
        with sessions() as session:
            assert session.scalar(select(StagedObjectRow)) is None

        with ThreadPoolExecutor(max_workers=32) as pool:
            assets = tuple(pool.map(lambda _index: archive(), range(32)))
        assert len({asset.asset_id for asset in assets}) == 1  # type: ignore[attr-defined]
        with sessions() as session:
            assert len(tuple(session.scalars(select(MediaAssetRow)))) == 1
            assert len(tuple(session.scalars(select(ProjectAssetReferenceRow)))) == 1
            journal = session.scalar(select(StagedObjectRow))
            assert journal is not None and journal.state == "FINALIZED"

        class FailOnTransactionalGet:
            def __init__(self, wrapped: PrivateObjectStorage) -> None:
                self.wrapped = wrapped
                self.get_calls = 0

            def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
                self.get_calls += 1
                if self.get_calls > 1:
                    raise AssertionError(
                        "existing attach performed object I/O inside its transaction"
                    )
                return self.wrapped.get_verified(key, expected_sha256, expected_size)

            def __getattr__(self, name: str) -> object:
                return getattr(self.wrapped, name)

        fail_on_second_get = FailOnTransactionalGet(storage)
        replay_adapter = build_h3_archive_adapter(  # type: ignore[arg-type]
            sessions, fail_on_second_get
        )
        replayed = replay_adapter.archive_immutable(
            attempt_id=attempt.attempt_id,
            generation_id=generation_id,
            output_id="provider-output",
            content=payload,
            media_type="video/mp4",
            expected_sha256=digest,
        )
        assert replayed.asset_id == assets[0].asset_id  # type: ignore[attr-defined]
        assert fail_on_second_get.get_calls == 1

        retry_payload = b"generation-specific-h3-output"
        retry_digest = hashlib.sha256(retry_payload).hexdigest()
        retry_output_id = "generation-specific"
        retry_logical_key = hashlib.sha256(
            f"{attempt.attempt_id}:{retry_output_id}:{retry_digest}".encode()
        ).hexdigest()
        with sessions.begin() as session:
            row = session.get_one(VideoAttemptRecord, attempt.attempt_id)
            row.output_sha256 = retry_digest
            row.output_id = retry_output_id
            row.output_size_bytes = len(retry_payload)
            row.output_media_type = "video/mp4"
        original_retry_key, _, _ = adapter._claim_stage(  # noqa: SLF001
            parents.owner_id,
            parents.project_id,
            retry_logical_key,
            retry_digest,
            len(retry_payload),
            "video/mp4",
        )
        with sessions.begin() as session:
            row = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == original_retry_key)
            )
            assert row is not None
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        assert reconcile_staged_objects(sessions, storage, older_than_seconds=-1) == 1

        def retry_archive() -> object:
            return adapter.archive_immutable(
                attempt_id=attempt.attempt_id,
                generation_id=generation_id,
                output_id=retry_output_id,
                content=retry_payload,
                media_type="video/mp4",
                expected_sha256=retry_digest,
            )

        suffixed_winner = retry_archive()
        response_loss_retry = retry_archive()
        assert response_loss_retry == suffixed_winner
        with sessions() as session:
            winner_journal = session.scalar(
                select(StagedObjectRow)
                .where(
                    StagedObjectRow.logical_object_key == retry_logical_key,
                    StagedObjectRow.state == "FINALIZED",
                )
                .order_by(
                    StagedObjectRow.generation.desc(),
                    StagedObjectRow.created_at.desc(),
                    StagedObjectRow.id.desc(),
                )
            )
            assert winner_journal is not None
            assert winner_journal.object_key != retry_logical_key
            assert winner_journal.asset_id == suffixed_winner.asset_id  # type: ignore[attr-defined]
            assert len(tuple(session.scalars(select(MediaAssetRow)))) == 2
        with sessions.begin() as session:
            row = session.get_one(VideoAttemptRecord, attempt.attempt_id)
            row.output_sha256 = digest
            row.output_id = "provider-output"
            row.output_size_bytes = len(payload)
            row.output_media_type = "video/mp4"

        archive_key = hashlib.sha256(
            f"{attempt.attempt_id}:provider-output:{digest}".encode()
        ).hexdigest()
        with sessions.begin() as session:
            session.query(ProjectAssetReferenceRow).delete()
        storage.delete_exact(archive_key)
        with pytest.raises(S3Error):
            archive()
        with sessions() as session:
            assert session.scalar(select(ProjectAssetReferenceRow)) is None

        late_payload = b"late-provider-output"
        late_digest = hashlib.sha256(late_payload).hexdigest()
        late_key = hashlib.sha256(f"{attempt.attempt_id}:late:{late_digest}".encode()).hexdigest()
        actual_late_key, token, generation = adapter._claim_stage(  # noqa: SLF001
            parents.owner_id,
            parents.project_id,
            late_key,
            late_digest,
            len(late_payload),
            "video/mp4",
        )
        with sessions.begin() as session:
            project = session.get(ProjectRow, parents.project_id)
            assert project is not None
            project.production_state = "PURGING"
            project.production_revision += 1
            late = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == actual_late_key)
            )
            assert late is not None
            late.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        assert reconcile_staged_objects(sessions, storage, older_than_seconds=-1) == 1
        storage.put_at_key(actual_late_key, late_payload, "video/mp4", late_digest)
        assert not adapter._mark_staged(actual_late_key, token, generation)  # noqa: SLF001
        with pytest.raises(DomainError, match="permanently deleted"):
            archive()
        with sessions() as session:
            late = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == actual_late_key)
            )
            assert late is not None and late.state == "CLEANUP_PENDING"
            assert (
                session.scalar(
                    select(MediaAssetRow).where(MediaAssetRow.object_key == actual_late_key)
                )
                is None
            )
        assert (
            storage.get_verified(actual_late_key, late_digest, len(late_payload)) == late_payload
        )
        with sessions.begin() as session:
            late = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == actual_late_key)
            )
            assert late is not None
            late.cleanup_after = datetime.now(UTC) - timedelta(seconds=1)
        assert reconcile_staged_objects(sessions, storage, older_than_seconds=-1) == 0
        with sessions() as session:
            tombstone = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == actual_late_key)
            )
            assert tombstone is not None and tombstone.state == "CLEANUP_PENDING"
        assert not storage.exists(actual_late_key)
    finally:
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
