from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa


def test_channel_name_and_project_created_at_migration_columns() -> None:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0022_channel_name_and_project_created_at.py"
    )
    spec = importlib.util.spec_from_file_location("channel_name_project_created_at_0022", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    assert module.revision == "0022"
    assert module.down_revision == "0021"

    added: list[tuple[str, object]] = []
    module.op.add_column = lambda table, column, *args, **kwargs: added.append((table, column))
    module.upgrade()
    assert [table for table, _column in added] == ["channels", "projects"]
    name_column = added[0][1]
    created_column = added[1][1]
    assert name_column.name == "name"
    assert name_column.nullable is True
    assert created_column.name == "created_at"
    assert created_column.nullable is False
    assert created_column.server_default is not None

    dropped: list[tuple[str, str]] = []
    module.op.drop_column = lambda table, column, *args, **kwargs: dropped.append((table, column))
    module.downgrade()
    assert dropped == [("projects", "created_at"), ("channels", "name")]


def test_channel_default_character_images_migration_columns() -> None:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0033_channel_default_character_images.py"
    )
    spec = importlib.util.spec_from_file_location("channel_default_character_images_0033", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    assert module.revision == "0033"
    assert module.down_revision == "0032"

    added: list[tuple[str, object]] = []
    module.op.add_column = lambda table, column, *args, **kwargs: added.append((table, column))
    module.upgrade()
    assert [table for table, _column in added] == ["channels"]
    column = added[0][1]
    assert column.name == "default_character_image_ids"
    assert column.nullable is False
    assert column.server_default is not None

    dropped: list[tuple[str, str]] = []
    module.op.drop_column = lambda table, column, *args, **kwargs: dropped.append((table, column))
    module.downgrade()
    assert dropped == [("channels", "default_character_image_ids")]


def test_project_publication_migration_columns_and_ledger() -> None:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0039_project_publication.py"
    )
    spec = importlib.util.spec_from_file_location("project_publication_0039", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    assert module.revision == "0039"
    assert module.down_revision == "0038"

    added: list[tuple[str, object]] = []
    tables: list[tuple[str, tuple[object, ...]]] = []
    indexes: list[tuple[str, str, tuple[str, ...]]] = []
    module.op.add_column = lambda table, column, *args, **kwargs: added.append((table, column))
    module.op.create_table = lambda name, *columns, **kwargs: tables.append((name, columns))
    module.op.create_index = lambda name, table, columns, **kwargs: indexes.append(
        (name, table, tuple(columns))
    )
    module.upgrade()

    assert [table for table, _column in added] == ["projects"]
    published_column = added[0][1]
    assert published_column.name == "published_at"
    assert published_column.nullable is True

    assert [name for name, _columns in tables] == ["project_publication_events"]
    columns = {column.name: column for column in tables[0][1] if isinstance(column, sa.Column)}
    assert set(columns) == {
        "id",
        "project_id",
        "owner_id",
        "actor_account_id",
        "action",
        "occurred_at",
    }
    project_fk = next(iter(columns["project_id"].foreign_keys))
    assert project_fk.ondelete == "CASCADE"
    assert columns["action"].type.length == 16
    assert columns["occurred_at"].server_default is not None
    checks = [
        column for column in tables[0][1] if isinstance(column, sa.CheckConstraint)
    ]
    assert len(checks) == 1
    assert checks[0].name == "action"

    assert indexes == [
        (
            "ix_project_publication_events_project_id",
            "project_publication_events",
            ("project_id",),
        ),
        (
            "ix_project_publication_events_owner_id",
            "project_publication_events",
            ("owner_id",),
        ),
    ]

    dropped_indexes: list[str] = []
    dropped_tables: list[str] = []
    dropped_columns: list[tuple[str, str]] = []
    module.op.drop_index = lambda name, **kwargs: dropped_indexes.append(name)
    module.op.drop_table = lambda name, **kwargs: dropped_tables.append(name)
    module.op.drop_column = lambda table, column, *args, **kwargs: dropped_columns.append(
        (table, column)
    )
    module.downgrade()
    assert dropped_indexes == [
        "ix_project_publication_events_owner_id",
        "ix_project_publication_events_project_id",
    ]
    assert dropped_tables == ["project_publication_events"]
    assert dropped_columns == [("projects", "published_at")]


def test_channel_default_visual_style_migration_columns() -> None:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0038_channel_default_visual_style.py"
    )
    spec = importlib.util.spec_from_file_location("channel_default_visual_style_0038", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    assert module.revision == "0038"
    assert module.down_revision == "0037"

    added: list[tuple[str, object]] = []
    module.op.add_column = lambda table, column, *args, **kwargs: added.append((table, column))
    module.upgrade()
    assert [table for table, _column in added] == ["channels"]
    column = added[0][1]
    assert column.name == "default_visual_style"
    assert column.nullable is False
    assert column.server_default is not None

    dropped: list[tuple[str, str]] = []
    module.op.drop_column = lambda table, column, *args, **kwargs: dropped.append((table, column))
    module.downgrade()
    assert dropped == [("channels", "default_visual_style")]
