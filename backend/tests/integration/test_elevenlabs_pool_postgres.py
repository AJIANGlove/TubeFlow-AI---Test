from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import migration_head
from sqlalchemy import inspect, text

from tubeflow_test.adapters.persistence.elevenlabs import (
    ElevenlabsKeyEventRow,
    ElevenlabsKeyRow,
    SqlElevenlabsKeyPool,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.domain import QuotaSnapshot

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    config.attributes["database_url"] = database_url
    return config


@pytest.mark.integration
def test_elevenlabs_pool_schema_matches_models_and_downgrade_drops_tables(
    postgres_database,
) -> None:
    engine = postgres_database.engine
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("elevenlabs_keys")} == set(
        ElevenlabsKeyRow.__table__.columns.keys()
    )
    assert {c["name"] for c in inspector.get_columns("elevenlabs_key_events")} == set(
        ElevenlabsKeyEventRow.__table__.columns.keys()
    )
    foreign_keys = inspector.get_foreign_keys("elevenlabs_key_events")
    assert any(
        item["constrained_columns"] == ["key_id"]
        and item["referred_table"] == "elevenlabs_keys"
        and item["options"].get("ondelete") == "CASCADE"
        for item in foreign_keys
    )
    assert any(
        item["column_names"] == ["api_key"]
        for item in inspector.get_unique_constraints("elevenlabs_keys")
    )
    # 夹具从空库升级到 head；此处断言迁移链已到当前 head（从 migrations/ 读取，不写死版本号）。
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == migration_head()

    command.downgrade(_config(postgres_database.url), "0033")
    downgraded = inspect(engine)
    assert "elevenlabs_keys" not in downgraded.get_table_names()
    assert "elevenlabs_key_events" not in downgraded.get_table_names()

    command.upgrade(_config(postgres_database.url), "head")
    assert "elevenlabs_keys" in inspect(engine).get_table_names()


@pytest.mark.integration
def test_sql_pool_crud_unique_and_cascade(postgres_database) -> None:
    pool = SqlElevenlabsKeyPool(postgres_database.sessions)
    first = pool.add_key(api_key="sk-first", label="主号", now=NOW)
    second = pool.add_key(api_key="sk-second", label="", now=NOW + timedelta(minutes=1))
    assert first.status == "active"
    assert [entry.id for entry in pool.list_keys()] == [first.id, second.id]
    assert pool.get_key(first.id) == first
    assert pool.find_by_api_key("sk-second") == second
    assert pool.find_by_api_key("sk-missing") is None

    with pytest.raises(DomainError) as captured:
        pool.add_key(api_key="sk-first", label="", now=NOW)
    assert captured.value.code == "elevenlabs_key_duplicate"

    updated = pool.set_status(first.id, "exhausted", last_error="402", now=NOW)
    assert updated is not None and updated.status == "exhausted" and updated.last_error == "402"
    assert pool.set_status(first.id, "active", last_error=None, now=NOW) is not None
    cleared = pool.get_key(first.id)
    assert cleared is not None and cleared.last_error is None

    pool.save_quota(first.id, QuotaSnapshot("creator", 5, 100, 1, 30, "active", True, None), NOW)
    refreshed = pool.get_key(first.id)
    assert refreshed is not None
    assert refreshed.quota_tier == "creator"
    assert refreshed.character_count == 5
    assert refreshed.quota_refreshed_at == NOW

    pool.record_event(first.id, "added", "tier=creator", NOW)
    pool.record_event(second.id, "added", "tier=free", NOW)
    events = pool.list_events()
    assert [item.event for item in events] == ["added", "added"]
    assert events[0].id > events[1].id
    assert [item.key_id for item in pool.list_events(key_id=first.id)] == [first.id]

    for index in range(55):
        pool.record_event(first.id, "probe", f"i{index}", NOW + timedelta(seconds=index))
    pool.trim_events(first.id, 50)
    assert len(pool.list_events(key_id=first.id, limit=100)) == 50
    assert pool.list_events(key_id=second.id)

    assert pool.delete_key(first.id) is True
    assert pool.delete_key(first.id) is False
    assert pool.get_key(first.id) is None
    assert pool.list_events(key_id=first.id) == ()
    assert len(pool.list_events(key_id=second.id)) == 1
