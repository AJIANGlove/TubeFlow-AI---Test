"""The PostgreSQL integration fixture drops its temporary schema even if migration fails.

2026-11-06: an empty `contract_*` schema was left on the shared test database because
`alembic upgrade` ran outside the fixture's try/finally.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "integration" / "conftest.py"


def _integration_conftest(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    pytest.importorskip("alembic")
    name = "integration_conftest_under_test"
    spec = importlib.util.spec_from_file_location(name, CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


def test_schema_is_dropped_when_the_migration_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _integration_conftest(monkeypatch)
    statements: list[str] = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def execute(self, statement: object) -> None:
            statements.append(str(statement))

    class Engine:
        def begin(self) -> Connection:
            return Connection()

        def dispose(self) -> None:
            statements.append("dispose")

    def failing_upgrade(_config: object, _revision: str) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setenv("TUBEFLOW_TEST_POSTGRES_URL", "postgresql+psycopg://user@localhost/db")
    monkeypatch.setattr(module, "create_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(module.command, "upgrade", failing_upgrade)
    fixture = module.postgres_database.__wrapped__

    with pytest.raises(RuntimeError, match="migration failed"):
        next(fixture(SimpleNamespace()))

    create, drop, dispose = statements
    assert create.startswith('CREATE SCHEMA "contract_')
    assert drop == create.replace("CREATE", "DROP", 1) + " CASCADE"
    assert dispose == "dispose"
