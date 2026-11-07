"""Safety of the PostgreSQL integration fixtures (2026-11-06).

- The temporary schema is dropped even when the migration fails: an empty `contract_*`
  schema was left behind because `alembic upgrade` ran outside the fixture's try/finally.
- Integration tests create schemas or databases only in a database whose name ends with
  "test": that empty schema was found in the application database `tubeflow`. (A first rule,
  "refuse when public.projects has rows", also refused the test database `tubeflowtest`,
  which holds a migration-probe project.)
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

INTEGRATION = Path(__file__).resolve().parents[1] / "integration"


def _load(monkeypatch: pytest.MonkeyPatch, file_name: str, module_name: str) -> ModuleType:
    monkeypatch.syspath_prepend(str(INTEGRATION))
    spec = importlib.util.spec_from_file_location(module_name, INTEGRATION / file_name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar(self) -> object:
        return self.value


class _Database:
    """Fake engine: answers the guard's current_database() query and records statements."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.statements: list[str] = []

    def connect(self) -> _Database:
        return self

    def begin(self) -> _Database:
        return self

    def __enter__(self) -> _Database:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, statement: object) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "current_database()" in sql:
            return _Result(self.name)
        return _Result(None)

    def dispose(self) -> None:
        self.statements.append("dispose")

    def writes(self) -> list[str]:
        return [sql for sql in self.statements if sql.startswith(("CREATE", "DROP"))]


@pytest.mark.parametrize(
    ("name", "refused"),
    [
        ("tubeflowtest", False),  # the shared test database (has a migration-probe project)
        ("TubeflowTest", False),
        ("tubeflow", True),  # the application database
        ("postgres", True),
        ("tubeflow_test_backup", True),
    ],
)
def test_guard_allows_only_databases_named_like_a_test_database(
    monkeypatch: pytest.MonkeyPatch, name: str, refused: bool
) -> None:
    guard = _load(monkeypatch, "database_guard.py", "database_guard_under_test")
    database = _Database(name)
    if refused:
        with pytest.raises(pytest.fail.Exception, match=f"connected to '{name}'"):
            guard.refuse_application_database(database)
    else:
        guard.refuse_application_database(database)
    assert database.writes() == []


def _fixture(monkeypatch: pytest.MonkeyPatch, database: _Database) -> ModuleType:
    pytest.importorskip("alembic")
    module = _load(monkeypatch, "conftest.py", "integration_conftest_under_test")
    monkeypatch.setenv("TUBEFLOW_TEST_POSTGRES_URL", "postgresql+psycopg://user@localhost/db")
    monkeypatch.setattr(module, "create_engine", lambda *_args, **_kwargs: database)
    return module


def test_schema_is_dropped_when_the_migration_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    database = _Database("tubeflowtest")
    module = _fixture(monkeypatch, database)

    def failing_upgrade(_config: object, _revision: str) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(module.command, "upgrade", failing_upgrade)

    with pytest.raises(RuntimeError, match="migration failed"):
        next(module.postgres_database.__wrapped__(SimpleNamespace()))

    create, drop = database.writes()
    assert create.startswith('CREATE SCHEMA "contract_')
    assert drop == create.replace("CREATE", "DROP", 1) + " CASCADE"
    assert database.statements[-1] == "dispose"


def test_fixture_creates_nothing_in_the_application_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _Database("tubeflow")
    module = _fixture(monkeypatch, database)
    upgrades: list[str] = []
    monkeypatch.setattr(module.command, "upgrade", lambda _config, rev: upgrades.append(rev))

    with pytest.raises(pytest.fail.Exception, match="connected to 'tubeflow'"):
        next(module.postgres_database.__wrapped__(SimpleNamespace()))

    assert database.writes() == [] and upgrades == []


def test_every_integration_test_that_creates_schemas_or_databases_calls_the_guard() -> None:
    creating = re.compile(r"CREATE (SCHEMA|DATABASE)")
    unguarded = [
        path.name
        for path in sorted(INTEGRATION.glob("*.py"))
        if path.name != "database_guard.py"
        and creating.search(path.read_text(encoding="utf-8"))
        and "refuse_application_database(" not in path.read_text(encoding="utf-8")
    ]
    assert unguarded == []


class _ObjectStore:
    """Fake MinIO client: answers bucket_exists and records every other call."""

    def __init__(self, buckets: set[str]) -> None:
        self.buckets = buckets
        self.calls: list[str] = []

    def bucket_exists(self, bucket_name: str) -> bool:
        self.calls.append(f"bucket_exists:{bucket_name}")
        return bucket_name in self.buckets


def test_object_store_guard_refuses_the_application_minio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load(monkeypatch, "object_store_guard.py", "object_store_guard_under_test")
    application = _ObjectStore({"tubeflow-private", "something-else"})
    with pytest.raises(pytest.fail.Exception, match="'tubeflow-private'"):
        guard.refuse_application_object_store(application)
    assert application.calls == ["bucket_exists:tubeflow-private"]

    empty_test_store = _ObjectStore(set())
    guard.refuse_application_object_store(empty_test_store)
    assert empty_test_store.calls == ["bucket_exists:tubeflow-private"]


def test_object_store_guard_names_the_configured_application_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tubeflow_test.config import Settings

    guard = _load(monkeypatch, "object_store_guard.py", "object_store_guard_under_test")
    assert Settings.model_fields["minio_bucket"].default == guard.APPLICATION_BUCKET


def test_every_integration_test_that_builds_a_minio_client_calls_the_guard() -> None:
    builds_client = re.compile(r"\b(Minio|_TrackingMinio)\(")
    unguarded = [
        path.name
        for path in sorted(INTEGRATION.glob("*.py"))
        if path.name != "object_store_guard.py"
        and builds_client.search(path.read_text(encoding="utf-8"))
        and "refuse_application_object_store(" not in path.read_text(encoding="utf-8")
    ]
    assert unguarded == []


def test_minio_tests_never_fall_back_to_root_or_default_credentials() -> None:
    offending = [
        path.name
        for path in sorted(INTEGRATION.glob("*.py"))
        if any(
            marker in path.read_text(encoding="utf-8")
            for marker in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "development-only-change-me")
        )
    ]
    assert offending == []


def test_no_integration_test_hardcodes_the_migration_head() -> None:
    # A literal revision is fine only when the same file pins the fixture to it
    # (@pytest.mark.parametrize("postgres_database", ["0006"], indirect=True)); otherwise the
    # assertion means "current head" and goes stale with every new migration (it did: 0039).
    compared = re.compile(r"alembic_version\"\)\)\s*==\s*\"(\d{4})\"")
    pinned = re.compile(r"parametrize\(\s*\"postgres_database\",\s*\[\s*\"(\d{4})\"")
    offending = []
    for path in sorted(INTEGRATION.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        stale = set(compared.findall(source)) - set(pinned.findall(source))
        if stale:
            offending.append((path.name, sorted(stale)))
    assert offending == []
