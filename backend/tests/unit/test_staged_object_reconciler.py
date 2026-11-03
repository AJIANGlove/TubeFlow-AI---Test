from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tubeflow_test.adapters.persistence.media_repository import reconcile_staged_objects


class _Storage:
    def __init__(self, keys: set[str]) -> None:
        self.keys = keys
        self.deleted: list[str] = []

    def delete_exact(self, key: str) -> None:
        self.deleted.append(key)
        self.keys.discard(key)

    def exists(self, key: str) -> bool:
        return key in self.keys

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        raise AssertionError("test rows intentionally have no hash facts")


def test_orphan_reconciler_never_deletes_attached_or_finalized_objects(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'staging.sqlite'}")
    sessions = sessionmaker(engine, expire_on_commit=False)
    old = datetime.now(UTC) - timedelta(hours=2)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE staged_objects (id CHAR(32) PRIMARY KEY, object_key TEXT UNIQUE, "
                "logical_object_key TEXT, "
                "project_id CHAR(32), owner_id CHAR(32), purpose VARCHAR(32), kind VARCHAR(32), "
                "ownership_scope VARCHAR(16), state VARCHAR(16), sha256 VARCHAR(64), "
                "size_bytes BIGINT, media_type VARCHAR(64), generation INTEGER, "
                "execution_token CHAR(32), asset_id CHAR(32), created_at TIMESTAMP, "
                "reference_type VARCHAR(32), lease_until TIMESTAMP, cleanup_after TIMESTAMP, "
                "cleanup_checked_at TIMESTAMP, cleanup_attempt INTEGER NOT NULL DEFAULT 0)"
            )
        )
        connection.execute(text("CREATE TABLE media_assets (id CHAR(32), object_key TEXT)"))
        connection.execute(text("CREATE TABLE character_images (id CHAR(32), object_key TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE voice_versions (id CHAR(32), reference_zip_object_key TEXT, "
                "preview_object_key TEXT, avatar_object_key TEXT)"
            )
        )
        for state in ("PENDING_UPLOAD", "STAGED", "ATTACHED", "FINALIZED"):
            connection.execute(
                text(
                    "INSERT INTO staged_objects "
                    "(id,object_key,ownership_scope,state,generation,created_at) "
                    "VALUES (:id,:key,'PROJECT',:state,1,:created_at)"
                ),
                {"id": uuid4().hex, "key": state.lower(), "state": state, "created_at": old},
            )
    storage = _Storage({"pending_upload", "staged", "attached", "finalized"})

    removed = reconcile_staged_objects(sessions, storage, older_than_seconds=3600)  # type: ignore[arg-type]

    assert removed == 2
    assert set(storage.deleted) == {"pending_upload", "staged"}
    assert storage.keys == {"attached", "finalized"}
    with engine.connect() as connection:
        states = {
            row.object_key: row.state
            for row in connection.execute(text("SELECT object_key,state FROM staged_objects"))
        }
    assert states == {
        "pending_upload": "CLEANUP_PENDING",
        "staged": "CLEANUP_PENDING",
        "attached": "ATTACHED",
        "finalized": "FINALIZED",
    }
