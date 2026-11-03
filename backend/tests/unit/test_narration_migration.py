from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def test_explicit_narration_migration_creates_all_feature_tables() -> None:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0002_narration_pipeline.py"
    spec = importlib.util.spec_from_file_location("narration_migration_0002", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    created: list[str] = []
    module.op.create_table = lambda name, *args, **kwargs: created.append(name)
    module.op.create_index = lambda *args, **kwargs: None
    module.upgrade()
    assert created == [
        "narration_attempt_records",
        "narration_project_records",
        "narration_attempt_event_records",
        "authoritative_narration_groups",
        "authoritative_narration_sentences",
        "authoritative_narration_memberships",
    ]
