"""封面 outbox 迁移（0031）测试：沿用 test_cover_migration 的录制式 harness。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from tubeflow_test.adapters.persistence.cover import models as cover_models
from tubeflow_test.adapters.persistence.schema import canonical_metadata

MODEL_TABLE = cover_models.CoverOutboxRow.__table__


def _load_migration() -> ModuleType:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0031_cover_outbox.py"
    spec = importlib.util.spec_from_file_location("cover_outbox_0031", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    return module


class _RecordedUpgrade:
    def __init__(self) -> None:
        self.metadata = sa.MetaData()
        for stub in ("accounts", "projects"):
            sa.Table(
                stub,
                self.metadata,
                sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            )
        self.tables: dict[str, sa.Table] = {}
        self.indexes: dict[str, dict[str, dict[str, Any]]] = {}
        self.dropped: list[str] = []


def _run_upgrade(module: ModuleType) -> _RecordedUpgrade:
    recorded = _RecordedUpgrade()

    def create_table(name: str, *args: object, **kwargs: object) -> None:
        recorded.tables[name] = sa.Table(name, recorded.metadata, *args)

    def create_index(
        name: str, table: str, columns: list[str], *args: object, **kwargs: object
    ) -> None:
        recorded.indexes.setdefault(table, {})[name] = {"columns": tuple(columns), **kwargs}

    module.op.create_table = create_table
    module.op.create_index = create_index
    module.upgrade()
    return recorded


def _default_signature(column: sa.Column[Any]) -> tuple[bool, str | None]:
    if column.server_default is None:
        return (False, None)
    return (True, str(column.server_default.arg))


def _foreign_keys(column: sa.Column[Any]) -> set[tuple[str, str | None]]:
    return {(fk.target_fullname, fk.ondelete) for fk in column.foreign_keys}


def test_cover_outbox_migration_revision_chain_and_upgrade_shape() -> None:
    module = _load_migration()
    assert module.revision == "0031"
    assert module.down_revision == "0030"

    recorded = _run_upgrade(module)
    assert list(recorded.tables) == ["cover_outbox"]
    index_names = set(recorded.indexes["cover_outbox"])
    assert index_names == {
        "ix_cover_outbox_owner_id",
        "ix_cover_outbox_project_id",
        "ix_cover_outbox_aggregate_id",
        "uq_cover_outbox_pending_workflow_id",
    }


def test_cover_outbox_migration_table_matches_model_metadata() -> None:
    recorded = _run_upgrade(_load_migration())
    migration_table = recorded.tables["cover_outbox"]

    assert set(migration_table.c.keys()) == set(MODEL_TABLE.c.keys())
    for column_name, migration_column in migration_table.c.items():
        model_column = MODEL_TABLE.c[column_name]
        assert migration_column.nullable == model_column.nullable, column_name
        assert str(migration_column.type) == str(model_column.type), column_name
        assert _default_signature(migration_column) == _default_signature(model_column), (
            column_name
        )
        assert _foreign_keys(migration_column) == _foreign_keys(model_column), column_name
    assert {column.name for column in migration_table.primary_key.columns} == {
        column.name for column in MODEL_TABLE.primary_key.columns
    }
    assert {
        index["columns"]
        for name, index in recorded.indexes["cover_outbox"].items()
        if not name.startswith("uq_")
    } == {
        tuple(index.columns.keys()) for index in MODEL_TABLE.indexes if not index.unique
    }


def test_cover_outbox_pending_workflow_id_partial_unique_index() -> None:
    recorded = _run_upgrade(_load_migration())
    pending = recorded.indexes["cover_outbox"]["uq_cover_outbox_pending_workflow_id"]
    assert pending["unique"] is True
    assert "acknowledged_at IS NULL" in str(pending["postgresql_where"])

    model_index = next(index for index in MODEL_TABLE.indexes if index.unique)
    assert model_index.dialect_options["postgresql"]["where"] is not None
    assert model_index.dialect_options["sqlite"]["where"] is not None


def test_cover_outbox_migration_downgrade_drops_table() -> None:
    module = _load_migration()
    dropped: list[str] = []
    module.op.drop_table = lambda name, *args, **kwargs: dropped.append(name)
    module.downgrade()
    assert dropped == ["cover_outbox"]


def test_cover_outbox_model_registered_on_canonical_metadata() -> None:
    metadata = canonical_metadata()
    assert "cover_outbox" in metadata.tables
    assert "cover_outbox" in {table.name for table in metadata.sorted_tables}
