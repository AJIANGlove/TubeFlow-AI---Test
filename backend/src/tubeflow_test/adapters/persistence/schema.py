from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table, Text, Uuid

from tubeflow_test.adapters.persistence.identity_project.models import Base

# 0045 diagnostics uses raw SQL rather than an ORM record; include its table in
# the canonical schema without changing that adapter's ownership or behavior.
_video_prompt_compilations = Table(
    "video_prompt_compilations",
    Base.metadata,
    Column("id", Uuid, primary_key=True),
    Column("owner_id", Uuid, nullable=False),
    Column("project_id", Uuid, nullable=False),
    Column("group_id", Uuid),
    Column("attempt_id", Uuid),
    Column("strategy", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("payload_sha256", String(64)),
    Column("attempt_index", Integer, nullable=False),
    Column("reason", String(200), nullable=False),
    Column("output_sha256", String(64)),
    Column("output_text", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index(
        "ix_video_prompt_compilations_project_created",
        "project_id",
        "group_id",
        "created_at",
    ),
)


def canonical_metadata() -> MetaData:
    """Register every production table on the single declarative metadata."""
    from tubeflow_test.adapters.persistence import elevenlabs as _elevenlabs_models
    from tubeflow_test.adapters.persistence import models as _core_models
    from tubeflow_test.adapters.persistence import narration as _narration_models
    from tubeflow_test.adapters.persistence.cover import models as _cover_models
    from tubeflow_test.adapters.persistence.storyboards import models as _storyboard_models
    from tubeflow_test.adapters.persistence.storyboards import (
        project_director_models as _project_director_models,
    )
    from tubeflow_test.cover_templates import models as _cover_template_models
    from tubeflow_test.video_descriptions import models as _description_models
    from tubeflow_test.video_generation import persistence as _h3_models

    del (
        _core_models,
        _narration_models,
        _cover_models,
        _cover_template_models,
        _description_models,
        _storyboard_models,
        _project_director_models,
        _h3_models,
        _elevenlabs_models,
    )
    # Force resolution here so unresolved or stale cross-domain FKs fail at startup/tests.
    tuple(Base.metadata.sorted_tables)
    return Base.metadata
