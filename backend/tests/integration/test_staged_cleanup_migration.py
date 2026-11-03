from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select, text

from tubeflow_test.adapters.persistence.identity_project.models import AccountRow, StagedObjectRow


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    config.attributes["database_url"] = database_url
    return config


def _head_revision(config: Config) -> str:
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


@pytest.mark.integration
@pytest.mark.parametrize("postgres_database", ["0006"], indirect=True)
def test_0006_round_trip_then_current_head_preserves_cleanup_metadata(postgres_database) -> None:
    engine = postgres_database.engine
    sessions = postgres_database.sessions
    anonymous_key = f"anonymous-delete-{uuid4()}"
    owner_key = f"owner-stage-{uuid4()}"
    owner_id = uuid4()
    with sessions.begin() as session:
        session.add(AccountRow(
            id=owner_id,
            username=f"migration-owner-{owner_id.hex}",
            password_hash="test",
            role="USER",
            active=True,
            session_generation=1,
        ))
        session.flush()
        session.add(
            StagedObjectRow(
                object_key=anonymous_key,
                ownership_scope="GLOBAL",
                purpose="PROJECT_DELETION",
                state="CLEANUP_PENDING",
                generation=1,
                execution_token=uuid4(),
                cleanup_after=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            StagedObjectRow(
                object_key=owner_key,
                logical_object_key=owner_key,
                owner_id=owner_id,
                ownership_scope="OWNER",
                state="PENDING_UPLOAD",
                generation=1,
                execution_token=uuid4(),
                lease_until=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
    inspector = inspect(engine)
    assert {column["name"] for column in inspector.get_columns("staged_objects")} >= {
        "logical_object_key",
        "lease_until",
        "cleanup_after",
        "cleanup_checked_at",
        "cleanup_attempt",
    }
    foreign_keys = inspector.get_foreign_keys("staged_objects")
    assert any(item["constrained_columns"] == ["owner_id"] for item in foreign_keys)
    assert not any(item["constrained_columns"] == ["project_id"] for item in foreign_keys)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0006"

    command.downgrade(_config(postgres_database.url), "0005")
    with engine.connect() as connection:
        keys = set(connection.scalars(text("SELECT object_key FROM staged_objects")))
        assert anonymous_key not in keys
        assert owner_key in keys
    downgraded = inspect(engine)
    assert "logical_object_key" not in {
        column["name"] for column in downgraded.get_columns("staged_objects")
    }
    assert any(
        item["constrained_columns"] == ["project_id"]
        for item in downgraded.get_foreign_keys("staged_objects")
    )

    command.upgrade(_config(postgres_database.url), "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _head_revision(
            _config(postgres_database.url)
        )
    with sessions() as session:
        owner_row = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == owner_key)
        )
        assert owner_row is not None and owner_row.logical_object_key == owner_key
    upgraded = inspect(engine)
    orm_columns = set(StagedObjectRow.__table__.columns.keys())
    database_columns = {column["name"] for column in upgraded.get_columns("staged_objects")}
    assert orm_columns == database_columns
