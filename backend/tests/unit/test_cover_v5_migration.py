"""迁移 0032（cover_character_cards / label / character_cards_used）形状与模型一致性。

照 `test_cover_migration.py` 的记录式 DDL 回放模式：0030+0032 叠加后的完整
形状在该文件比对；本文件只钉 0032 自身的修订链、升级形状与降级顺序。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa

from tubeflow_test.adapters.persistence.cover import models as cover_models


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0032_cover_character_cards.py"
    )
    spec = importlib.util.spec_from_file_location("cover_character_cards_0032", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    return module


class _RecordedUpgrade:
    def __init__(self) -> None:
        self.metadata = sa.MetaData()
        self.tables: dict[str, sa.Table] = {}
        self.indexes: dict[str, set[tuple[str, ...]]] = {}
        self.added: list[tuple[str, sa.Column[Any]]] = []


def _run_upgrade(module: ModuleType) -> _RecordedUpgrade:
    recorded = _RecordedUpgrade()

    def create_table(name: str, *args: object, **kwargs: object) -> None:
        recorded.tables[name] = sa.Table(name, recorded.metadata, *args)

    def create_index(
        name: str, table: str, columns: list[str], *args: object, **kwargs: object
    ) -> None:
        recorded.indexes.setdefault(table, set()).add(tuple(columns))

    def add_column(table: str, column: sa.Column[Any], *args: object, **kwargs: object) -> None:
        recorded.added.append((table, column))

    module.op.create_table = create_table
    module.op.create_index = create_index
    module.op.add_column = add_column
    module.upgrade()
    return recorded


def test_0032_revision_chain_and_upgrade_shape() -> None:
    module = _load_migration()
    assert module.revision == "0032"
    assert module.down_revision == "0031"

    recorded = _run_upgrade(module)
    assert list(recorded.tables) == ["cover_character_cards"]
    assert [(table, column.name) for table, column in recorded.added] == [
        ("cover_style_profiles", "label"),
        ("cover_prompt_generations", "character_cards_used"),
    ]


def test_0032_character_cards_table_shape() -> None:
    recorded = _run_upgrade(_load_migration())
    table = recorded.tables["cover_character_cards"]
    model_table = cover_models.CoverCharacterCardRow.__table__

    assert set(table.c.keys()) == set(model_table.c.keys())
    assert {column.name for column in table.primary_key.columns} == {"id"}
    unique_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert unique_sets == {("channel_id", "character_image_id")}
    checks = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert checks == {"source IN ('manual', 'analysis')"}
    assert recorded.indexes.get("cover_character_cards") == {
        ("owner_id",),
        ("channel_id",),
    }

    foreign_keys = {
        column.name: {(fk.target_fullname, fk.ondelete) for fk in column.foreign_keys}
        for column in table.c
        if column.foreign_keys
    }
    assert foreign_keys == {
        # owner/频道 FK 照 cover_ref_images 频道行模式；角色图 FK 照既有同类引用。
        "owner_id": {("accounts.id", None)},
        "channel_id": {("channels.id", "RESTRICT")},
        "character_image_id": {("character_images.id", None)},
    }
    for name in ("identity_text", "appearance_text"):
        column = table.c[name]
        assert column.nullable is False
        assert column.server_default is not None and str(column.server_default.arg) == ""


def test_0032_added_columns_defaults() -> None:
    recorded = _run_upgrade(_load_migration())
    captured = {(table, column.name): column for table, column in recorded.added}

    label = captured[("cover_style_profiles", "label")]
    assert label.nullable is False
    assert str(label.type) == "VARCHAR(60)"
    assert label.server_default is not None and str(label.server_default.arg) == ""

    lineage = captured[("cover_prompt_generations", "character_cards_used")]
    assert lineage.nullable is False
    assert str(lineage.type) == "JSON"
    assert lineage.server_default is not None and str(lineage.server_default.arg) == "[]"


def test_0032_downgrade_drops_columns_then_table() -> None:
    module = _load_migration()
    dropped_columns: list[tuple[str, str]] = []
    dropped_tables: list[str] = []
    module.op.drop_column = lambda table, column, *args, **kwargs: dropped_columns.append(
        (table, column)
    )
    module.op.drop_table = lambda name, *args, **kwargs: dropped_tables.append(name)
    module.downgrade()

    assert dropped_columns == [
        ("cover_prompt_generations", "character_cards_used"),
        ("cover_style_profiles", "label"),
    ]
    assert dropped_tables == ["cover_character_cards"]
