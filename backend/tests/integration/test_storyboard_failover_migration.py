from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


@pytest.mark.integration
def test_storyboard_failover_migration_roundtrip(postgres_database) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    config.attributes["database_url"] = postgres_database.url

    command.downgrade(config, "0035")
    columns = {
        item["name"]
        for item in inspect(postgres_database.engine).get_columns("storyboard_generations")
    }
    assert "provider" not in columns
    assert "failover_chain_id" not in columns

    command.upgrade(config, "0036")
    inspector = inspect(postgres_database.engine)
    columns = {item["name"] for item in inspector.get_columns("storyboard_generations")}
    assert "provider" in columns
    assert "failover_chain_id" in columns
    checks = {item["name"] for item in inspector.get_check_constraints("storyboard_generations")}
    assert "ck_storyboard_generations_ck_sb_failover_fields_all_or_none" in checks
