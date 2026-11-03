from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _config(*, ini_url: str, database_url: str | None = None) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(BACKEND_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", ini_url.replace("%", "%%"))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def _schema_url(database_url: URL, schema: str) -> str:
    return database_url.update_query_dict(
        {"options": f"-csearch_path={schema}"}
    ).render_as_string(hide_password=False)


def _tables(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@contextmanager
def _isolated_databases(admin_url: str) -> Iterator[tuple[URL, URL]]:
    suffix = uuid4().hex
    names = (f"alembic_url_a_{suffix}", f"alembic_url_b_{suffix}")
    parsed_url = make_url(admin_url)
    root_url = parsed_url.set(database=parsed_url.database, query={})
    admin_engine = create_engine(root_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            for name in names:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield root_url.set(database=names[0]), root_url.set(database=names[1])
    finally:
        with admin_engine.connect() as connection:
            for name in names:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.mark.integration
def test_alembic_url_precedence_across_real_databases_and_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres_url = os.environ.get("TUBEFLOW_TEST_POSTGRES_URL")
    assert postgres_url, "TUBEFLOW_TEST_POSTGRES_URL is required for PostgreSQL integration"

    with _isolated_databases(postgres_url) as (database_a, database_b):
        schemas = {
            "attribute": f"attribute_{uuid4().hex}",
            "connection": f"connection_{uuid4().hex}",
            "environment": f"environment_{uuid4().hex}",
            "ini": f"ini_{uuid4().hex}",
        }
        for database, schema_names in (
            (database_a, (schemas["attribute"], schemas["connection"], schemas["ini"])),
            (database_b, (schemas["environment"],)),
        ):
            engine = create_engine(database)
            try:
                with engine.begin() as connection:
                    for schema in schema_names:
                        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            finally:
                engine.dispose()

        attribute_url = _schema_url(database_a, schemas["attribute"])
        connection_url = _schema_url(database_a, schemas["connection"])
        environment_url = _schema_url(database_b, schemas["environment"])
        ini_url = _schema_url(database_a, schemas["ini"])

        monkeypatch.setenv("TUBEFLOW_DATABASE_URL", environment_url)
        command.upgrade(_config(ini_url=ini_url, database_url=attribute_url), "head")
        command.upgrade(_config(ini_url=ini_url), "head")

        connection_engine = create_engine(connection_url)
        try:
            with connection_engine.begin() as connection:
                supplied = _config(ini_url=ini_url, database_url=environment_url)
                supplied.attributes["connection"] = connection
                command.upgrade(supplied, "head")
        finally:
            connection_engine.dispose()

        assert "alembic_version" in _tables(attribute_url)
        assert "alembic_version" in _tables(environment_url)
        assert "alembic_version" in _tables(connection_url)
        assert _tables(ini_url) == set()
