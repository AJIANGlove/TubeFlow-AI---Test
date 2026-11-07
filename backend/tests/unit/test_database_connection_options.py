"""`Database` keeps a PostgreSQL URL's own libpq options (2026-11-06).

Passing only the S6 timeouts as `connect_args` overrode an `options=-csearch_path=...` in
the URL, so writes made through `Database` in integration tests landed in `public` rather
than the temporary schema (four of the long-standing integration failures).
"""

from __future__ import annotations

import pytest

from tubeflow_test.adapters.persistence import database as database_module
from tubeflow_test.adapters.persistence.database import Database, postgres_connection_options

TIMEOUTS = "-c lock_timeout=15s -c statement_timeout=60s"


def test_url_without_options_gets_exactly_the_timeouts() -> None:
    url = "postgresql+psycopg://user:pw@db:5432/tubeflow"
    assert postgres_connection_options(url) == TIMEOUTS


def test_url_search_path_is_kept_before_the_timeouts() -> None:
    url = "postgresql+psycopg://user:pw@db/tubeflowtest?options=-csearch_path%3Dcontract_abc"
    assert postgres_connection_options(url) == f"-csearch_path=contract_abc {TIMEOUTS}"


def test_repeated_options_in_the_url_are_all_kept() -> None:
    url = (
        "postgresql+psycopg://user:pw@db/tubeflowtest"
        "?options=-csearch_path%3Ds1&options=-capplication_name%3Dx"
    )
    assert postgres_connection_options(url) == (f"-csearch_path=s1 -capplication_name=x {TIMEOUTS}")


def test_database_passes_the_merged_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(database_module, "create_engine", fake_create_engine)
    Database("postgresql+psycopg://user:pw@db/tubeflowtest?options=-csearch_path%3Dcontract_x")
    assert captured["connect_args"] == {"options": f"-csearch_path=contract_x {TIMEOUTS}"}


def test_sqlite_gets_no_connect_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(database_module, "create_engine", fake_create_engine)
    Database("sqlite+pysqlite:///:memory:")
    assert "connect_args" not in captured
