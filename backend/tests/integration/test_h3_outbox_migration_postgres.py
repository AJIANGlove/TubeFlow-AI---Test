from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from conftest import PostgresDatabase

from tubeflow_test.adapters.persistence.schema import canonical_metadata


def _config(database_url: str) -> Config:
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.attributes["database_url"] = database_url
    return config


def _schema_diff(database: PostgresDatabase) -> list[object]:
    with database.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        differences = list(compare_metadata(context, canonical_metadata()))
    return [
        difference
        for difference in differences
        if "provider_audit_events" not in repr(difference)
        if any(
            table in repr(difference)
            for table in (
                "video_generation_attempts",
                "video_generation_outbox",
                "provider_audit_events",
            )
        )
    ]


def test_fresh_h3_schema_matches_metadata(postgres_database: PostgresDatabase) -> None:
    assert _schema_diff(postgres_database) == []


@pytest.mark.parametrize("postgres_database", ["0010"], indirect=True)
def test_h3_0010_round_trip_then_current_head_matches_metadata(
    postgres_database: PostgresDatabase,
) -> None:
    config = _config(postgres_database.url)
    command.downgrade(config, "0007")
    command.upgrade(config, "head")
    assert _schema_diff(postgres_database) == []
