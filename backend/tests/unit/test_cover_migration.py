from __future__ import annotations

import importlib.util
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SAWarning

from tubeflow_test.adapters.persistence.cover import models as cover_models
from tubeflow_test.adapters.persistence.identity_project.models import (
    ChannelRow,
    ProjectRow,
)
from tubeflow_test.adapters.persistence.schema import canonical_metadata

COVER_TABLES = (
    "cover_ref_images",
    "cover_style_drafts",
    "cover_style_profiles",
    "cover_prompt_generations",
    "cover_image_generations",
)
MODEL_TABLES = {
    "cover_ref_images": cover_models.CoverRefImageRow.__table__,
    "cover_style_drafts": cover_models.CoverStyleDraftRow.__table__,
    "cover_style_profiles": cover_models.CoverStyleProfileRow.__table__,
    "cover_prompt_generations": cover_models.CoverPromptGenerationRow.__table__,
    "cover_image_generations": cover_models.CoverImageGenerationRow.__table__,
    "cover_character_cards": cover_models.CoverCharacterCardRow.__table__,
}
POINTER_COLUMNS = {
    ("channels", "current_cover_style_profile_id"): (
        ChannelRow.__table__.c.current_cover_style_profile_id
    ),
    ("projects", "current_cover_prompt_generation_id"): (
        ProjectRow.__table__.c.current_cover_prompt_generation_id
    ),
    ("projects", "current_cover_image_generation_id"): (
        ProjectRow.__table__.c.current_cover_image_generation_id
    ),
}


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0030_cover_generation.py"
    )
    spec = importlib.util.spec_from_file_location("cover_generation_0030", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    return module


def _load_migration_0032() -> ModuleType:
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


def _load_migration_0037() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0037_cover_image_visual_review.py"
    )
    spec = importlib.util.spec_from_file_location("cover_image_visual_review_0037", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    return module


def _load_migration_0042() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0042_cover_image_uploads.py"
    )
    spec = importlib.util.spec_from_file_location("cover_image_uploads_0042", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    return module


class _RecordedUpgrade:
    """Materialize migration DDL into real Table objects so constraints resolve."""

    def __init__(self) -> None:
        self.metadata = sa.MetaData()
        for stub in ("accounts", "channels", "projects", "character_images"):
            sa.Table(
                stub,
                self.metadata,
                sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            )
        self.tables: dict[str, sa.Table] = {}
        self.indexes: dict[str, set[tuple[str, ...]]] = {}
        self.added: list[tuple[str, sa.Column[Any]]] = []


def _run_upgrade(*modules: ModuleType) -> _RecordedUpgrade:
    recorded = _RecordedUpgrade()

    def create_table(name: str, *args: object, **kwargs: object) -> None:
        recorded.tables[name] = sa.Table(name, recorded.metadata, *args)

    def create_index(
        name: str, table: str, columns: list[str], *args: object, **kwargs: object
    ) -> None:
        recorded.indexes.setdefault(table, set()).add(tuple(columns))

    def add_column(table: str, column: sa.Column[Any], *args: object, **kwargs: object) -> None:
        recorded.added.append((table, column))
        recorded.metadata.tables[table].append_column(column)

    def alter_column(
        table: str, column_name: str, *args: object, **kwargs: object
    ) -> None:
        recorded.metadata.tables[table].c[column_name].nullable = bool(
            kwargs.get("nullable", True)
        )

    def create_check_constraint(
        name: str, table: str, condition: object, *args: object, **kwargs: object
    ) -> None:
        recorded.metadata.tables[table].append_constraint(
            sa.CheckConstraint(condition, name=name)
        )

    for module in modules:
        module.op.create_table = create_table
        module.op.create_index = create_index
        module.op.add_column = add_column
        module.op.alter_column = alter_column
        module.op.create_check_constraint = create_check_constraint
        module.upgrade()
    return recorded


class _RecordedDowngrade:
    """记录 0042 downgrade 的调用顺序；可模拟是否仍存在上传版本行。"""

    def __init__(self, upload_rows: int) -> None:
        self.upload_rows = upload_rows
        self.calls: list[tuple[object, ...]] = []

    def get_bind(self) -> _RecordedDowngrade:
        return self

    def scalar(self, statement: object) -> int:
        self.calls.append(("scalar", str(statement)))
        return self.upload_rows

    def alter_column(
        self, table: str, column_name: str, *args: object, **kwargs: object
    ) -> None:
        self.calls.append(("alter_column", table, column_name, kwargs.get("nullable")))

    def drop_constraint(
        self, name: str, table: str, *args: object, **kwargs: object
    ) -> None:
        self.calls.append(("drop_constraint", name, table))

    def drop_column(self, table: str, column_name: str, *args: object, **kwargs: object) -> None:
        self.calls.append(("drop_column", table, column_name))


def _default_signature(column: sa.Column[Any]) -> tuple[bool, str | None]:
    if column.server_default is None:
        return (False, None)
    return (True, str(column.server_default.arg))


def _foreign_keys(column: sa.Column[Any]) -> set[tuple[str, str | None]]:
    return {(fk.target_fullname, fk.ondelete) for fk in column.foreign_keys}


def _unique_column_sets(table: sa.Table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }


def _check_sqltexts(table: sa.Table) -> set[str]:
    return {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def test_cover_migration_revision_chain_and_upgrade_shape() -> None:
    module = _load_migration()
    assert module.revision == "0030"
    assert module.down_revision == "0029"

    recorded = _run_upgrade(module)
    assert list(recorded.tables) == list(COVER_TABLES)
    assert [(table, column.name) for table, column in recorded.added] == list(POINTER_COLUMNS)


def test_cover_migration_tables_match_model_metadata() -> None:
    # 模型是全部迁移的叠加：0030 建表 + 0032 新表与追加列 + 0037 复审列
    # + 0042 上传版本来源与可空列。
    recorded = _run_upgrade(
        _load_migration(),
        _load_migration_0032(),
        _load_migration_0037(),
        _load_migration_0042(),
    )

    for table_name, model_table in MODEL_TABLES.items():
        migration_table = recorded.tables[table_name]
        assert set(migration_table.c.keys()) == set(model_table.c.keys()), table_name
        for column_name, migration_column in migration_table.c.items():
            model_column = model_table.c[column_name]
            assert migration_column.nullable == model_column.nullable, (
                table_name,
                column_name,
            )
            assert str(migration_column.type) == str(model_column.type), (
                table_name,
                column_name,
            )
            assert _default_signature(migration_column) == _default_signature(
                model_column
            ), (table_name, column_name)
            assert _foreign_keys(migration_column) == _foreign_keys(model_column), (
                table_name,
                column_name,
            )
        assert {column.name for column in migration_table.primary_key.columns} == {
            column.name for column in model_table.primary_key.columns
        }, table_name
        assert _unique_column_sets(migration_table) == _unique_column_sets(model_table), (
            table_name
        )
        assert _check_sqltexts(migration_table) == _check_sqltexts(model_table), table_name
        assert recorded.indexes.get(table_name, set()) == {
            tuple(index.columns.keys()) for index in model_table.indexes
        }, table_name


def test_cover_migration_pointer_columns_match_models() -> None:
    recorded = _run_upgrade(_load_migration())
    captured = {(table, column.name): column for table, column in recorded.added}

    for key, model_column in POINTER_COLUMNS.items():
        migration_column = captured[key]
        assert migration_column.nullable is True
        assert migration_column.nullable == model_column.nullable
        assert str(migration_column.type) == str(model_column.type)
        assert not migration_column.foreign_keys
        assert not model_column.foreign_keys


def test_cover_migration_downgrade_drops_columns_then_tables() -> None:
    module = _load_migration()
    dropped_columns: list[tuple[str, str]] = []
    dropped_tables: list[str] = []
    module.op.drop_column = lambda table, column, *args, **kwargs: dropped_columns.append(
        (table, column)
    )
    module.op.drop_table = lambda name, *args, **kwargs: dropped_tables.append(name)
    module.downgrade()

    assert dropped_columns == [
        ("projects", "current_cover_image_generation_id"),
        ("projects", "current_cover_prompt_generation_id"),
        ("channels", "current_cover_style_profile_id"),
    ]
    assert dropped_tables == [
        "cover_image_generations",
        "cover_prompt_generations",
        "cover_style_profiles",
        "cover_style_drafts",
        "cover_ref_images",
    ]


def test_cover_models_resolve_on_shared_metadata_without_cycles() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        metadata = canonical_metadata()
        sorted_names = {table.name for table in metadata.sorted_tables}

    assert set(COVER_TABLES).issubset(set(metadata.tables))
    assert set(COVER_TABLES).issubset(sorted_names)
    cycle_warnings = [
        item
        for item in caught
        if issubclass(item.category, SAWarning)
        and "unresolvable cycles" in str(item.message).lower()
    ]
    assert cycle_warnings == []


def test_cover_upload_migration_revision_chain_and_shape() -> None:
    module = _load_migration_0042()
    assert module.revision == "0042"
    assert module.down_revision == "0041"

    recorded = _run_upgrade(
        _load_migration(),
        _load_migration_0032(),
        _load_migration_0037(),
        _load_migration_0042(),
    )
    table = recorded.tables["cover_image_generations"]
    assert table.c["source"].nullable is False
    assert _default_signature(table.c["source"]) == (True, "generated")
    for column_name in ("prompt_generation_id", "model", "size", "quality"):
        assert table.c[column_name].nullable is True, column_name
    assert _check_sqltexts(table) >= {"source IN ('generated', 'upload')"}


def test_cover_upload_migration_downgrade_refuses_upload_rows() -> None:
    module = _load_migration_0042()
    recorded = _RecordedDowngrade(upload_rows=1)
    module.op.get_bind = recorded.get_bind
    module.op.alter_column = recorded.alter_column
    module.op.drop_constraint = recorded.drop_constraint
    module.op.drop_column = recorded.drop_column

    with pytest.raises(RuntimeError, match="uploaded cover versions"):
        module.downgrade()

    assert [call[0] for call in recorded.calls] == ["scalar"]


def test_cover_upload_migration_downgrade_restores_not_null_then_drops() -> None:
    module = _load_migration_0042()
    recorded = _RecordedDowngrade(upload_rows=0)
    module.op.get_bind = recorded.get_bind
    module.op.alter_column = recorded.alter_column
    module.op.drop_constraint = recorded.drop_constraint
    module.op.drop_column = recorded.drop_column

    module.downgrade()

    assert [call[0] for call in recorded.calls] == [
        "scalar",
        "alter_column",
        "alter_column",
        "alter_column",
        "alter_column",
        "drop_constraint",
        "drop_column",
    ]
    assert [call[2] for call in recorded.calls if call[0] == "alter_column"] == [
        "quality",
        "size",
        "model",
        "prompt_generation_id",
    ]
    assert all(
        call[3] is False for call in recorded.calls if call[0] == "alter_column"
    )
    assert recorded.calls[-2] == ("drop_constraint", "source", "cover_image_generations")
    assert recorded.calls[-1] == ("drop_column", "cover_image_generations", "source")
