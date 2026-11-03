from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    CharacterImageRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.identity_project.staged_objects import RequestStagedStorage
from tubeflow_test.domain.errors import DomainError


@dataclass(frozen=True)
class Stored:
    key: str
    sha256: str = "a" * 64
    size_bytes: int = 1
    media_type: str = "image/png"
    created: bool = True


class Storage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(self, body: bytes, media_type: str) -> Stored:
        key = f"key-{len(self.values)}"
        self.values[key] = body
        return Stored(key, media_type=media_type)

    def put_private_at_key(self, key: str, body: bytes, media_type: str) -> Stored:
        self.values[key] = body
        return Stored(key, media_type=media_type)

    def get(self, key: str) -> bytes:
        return self.values[key]

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        del expected_sha256, expected_size
        return self.get(key)

    def delete_exact(self, key: str) -> None:
        self.values.pop(key)

    def exists(self, key: str) -> bool:
        return key in self.values


def test_unfinalized_object_is_durably_staged_for_the_canonical_cleaner() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    StagedObjectRow.__table__.create(database.engine)
    storage = Storage()
    staged = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
    stored = staged.put(b"x", "image/png")
    with database.unit_of_work() as session:
        assert session.scalar(select(StagedObjectRow.object_key)) == stored.key
        assert session.scalar(select(StagedObjectRow.state)) == "STAGED"
    assert storage.values == {stored.key: b"x"}


def test_finalized_object_remains_in_the_canonical_journal() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    StagedObjectRow.__table__.create(database.engine)
    storage = Storage()
    staged = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
    stored = staged.put(b"x", "image/png")
    with database.unit_of_work() as session:
        session.execute(
            update(StagedObjectRow)
            .where(StagedObjectRow.object_key == stored.key)
            .values(state="ATTACHED", reference_type="character_image")
        )
    staged.finalize()
    assert storage.values == {stored.key: b"x"}


def test_crash_after_business_commit_preserves_referenced_staged_object() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    AccountRow.__table__.create(database.engine)
    StagedObjectRow.__table__.create(database.engine)
    CharacterImageRow.__table__.create(database.engine)
    storage = Storage()
    staged = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
    stored = staged.put(b"x", "image/png")
    owner_id = uuid4()
    with database.unit_of_work() as session:
        session.add(
            AccountRow(
                id=owner_id,
                username="owner",
                password_hash="hash",
                role="user",
                active=True,
                session_generation=1,
            )
        )
        session.add(
            CharacterImageRow(
                id=uuid4(),
                owner_id=owner_id,
                channel_id=uuid4(),
                asset_id=uuid4(),
                object_key=stored.key,
                sha256="a" * 64,
                media_type="image/png",
                size_bytes=1,
                width=1,
                height=1,
            )
        )
    # The single canonical cleaner later checks this authoritative reference.
    assert storage.values == {stored.key: b"x"}


def test_crash_after_put_before_staged_cas_keeps_exact_key_in_journal() -> None:
    class CrashAfterPut(Storage):
        def put_private_at_key(self, key: str, body: bytes, media_type: str) -> Stored:
            super().put_private_at_key(key, body, media_type)
            raise RuntimeError("simulated process boundary after MinIO PUT")

    database = Database("sqlite+pysqlite:///:memory:")
    StagedObjectRow.__table__.create(database.engine)
    storage = CrashAfterPut()
    staged = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="process boundary"):
        staged.put(b"x", "image/png")
    with database.unit_of_work() as session:
        assert session.scalar(select(StagedObjectRow.state)) == "PENDING_UPLOAD"
    assert len(storage.values) == 1
    assert len(storage.values) == 1


def test_late_upload_cannot_complete_after_cleanup_claim() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    StagedObjectRow.__table__.create(database.engine)

    class ClaimDuringPut(Storage):
        def put_private_at_key(self, key: str, body: bytes, media_type: str) -> Stored:
            stored = super().put_private_at_key(key, body, media_type)
            with database.unit_of_work() as session:
                session.execute(
                    update(StagedObjectRow)
                    .where(StagedObjectRow.object_key == key)
                    .values(
                        state="CLEANUP_PENDING",
                        generation=StagedObjectRow.generation + 1,
                        execution_token=uuid4(),
                    )
                )
            return stored

    storage = ClaimDuringPut()
    staged = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="claimed for cleanup"):
        staged.put(b"x", "image/png")
    # The writer cannot prove that another request did not reuse and attach the
    # exact key. The canonical cleaner owns eventual deletion.
    assert len(storage.values) == 1
    with database.unit_of_work() as session:
        assert session.scalar(select(StagedObjectRow.state)) == "CLEANUP_PENDING"


def test_late_exact_replay_never_deletes_existing_winner_after_claim_loss() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    StagedObjectRow.__table__.create(database.engine)
    key = "voice-catalog/v1/winner.zip"

    class ExistingWinner(Storage):
        def put_at_key(
            self, object_key: str, body: bytes, media_type: str, expected_sha256: str
        ) -> Stored:
            del expected_sha256
            assert object_key == key
            assert self.values[key] == body
            with database.unit_of_work() as session:
                session.execute(
                    update(StagedObjectRow)
                    .where(StagedObjectRow.object_key == key)
                    .values(
                        state="CLEANUP_PENDING",
                        generation=StagedObjectRow.generation + 1,
                        execution_token=uuid4(),
                    )
                )
            return Stored(key, media_type=media_type, created=False)

    storage = ExistingWinner()
    storage.values[key] = b"winner"
    staged = RequestStagedStorage(database, storage, ownership_scope="GLOBAL")  # type: ignore[arg-type]
    with database.unit_of_work() as session:
        session.add(
            StagedObjectRow(
                object_key=key,
                logical_object_key=key,
                created_at=datetime.now(UTC),
                ownership_scope="GLOBAL",
                state="FINALIZED",
                sha256="a" * 64,
                size_bytes=len(b"winner"),
                media_type="application/zip",
                reference_type="voice_version",
            )
        )
    with pytest.raises(DomainError, match="already claimed"):
        staged.put_at_key(key, b"winner", "application/zip", "a" * 64)
    assert storage.values[key] == b"winner"
