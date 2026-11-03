from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import PostgresDatabase, ProductionParentChain
from sqlalchemy import text

from tubeflow_test.config import Settings
from tubeflow_test.video_generation.project_routing import (
    initialize_migrated_project_routes,
    resolve_project_workflow,
)


@pytest.mark.integration
@pytest.mark.parametrize("postgres_database", ["0028"], indirect=True)
def test_migration_keeps_existing_projects_legacy_and_allows_only_new_candidate(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    existing_id = production_parent_chain.project_id
    second_id, new_id = uuid4(), uuid4()

    def add_project(session, project_id):  # type: ignore[no-untyped-def]
        # 钉版模式（0028/0029）没有 0035 才引入的列，ORM 插入/读取会把当前模型的
        # 全部列带进 SQL 必然失败；按钉版可用的基础列用 Core 写入。
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, channel_id, title, script, "
                "character_image_ids, visual_style, voice_version_id, locale, "
                "aspect_ratio, draft_revision, production_state, production_revision) "
                "VALUES (:id, :owner_id, :channel_id, :title, :script, "
                ":character_image_ids, :visual_style, :voice_version_id, :locale, "
                ":aspect_ratio, :draft_revision, :production_state, :production_revision)"
            ),
            {
                "id": project_id,
                "owner_id": production_parent_chain.owner_id,
                "channel_id": production_parent_chain.channel_id,
                "title": "route fixture",
                "script": "Original immutable content.",
                "character_image_ids": [production_parent_chain.character_id],
                "visual_style": "documentary",
                "voice_version_id": production_parent_chain.voice_id,
                "locale": "en-US",
                "aspect_ratio": "16:9",
                "draft_revision": 1,
                "production_state": "ACTIVE",
                "production_revision": 1,
            },
        )

    with postgres_database.sessions.begin() as session:
        add_project(session, second_id)
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.attributes["database_url"] = postgres_database.url
    command.upgrade(config, "0029")
    settings = Settings(
        _env_file=None, h3_workflow_manifest_sha256="a" * 64,
        h3_candidate_node_urls=("http://candidate",), h3_candidate_node_ids=("candidate",),
        h3_candidate_node_instance_ids=("candidate-instance",),
        h3_candidate_node_tokens=("candidate-token",), h3_candidate_node_capacities=(1,),
        h3_candidate_workflow_manifest_sha256="b" * 64,
        h3_candidate_project_ids=(existing_id, second_id, new_id),
    )
    with postgres_database.sessions.begin() as session:
        # 0029 predates the route's current ORM columns; assert this pinned schema via Core SQL.
        routes = tuple(session.execute(
            text("SELECT project_id, profile FROM h3_project_workflow_routes")
        ))
        assert {row.project_id for row in routes} == {existing_id, second_id}
        assert all(row.profile == "legacy" for row in routes)
    # Do not hold the fixture transaction open while Alembic changes the table.
    command.upgrade(config, "head")
    with postgres_database.sessions.begin() as session:
        initialize_migrated_project_routes(session, settings)
        add_project(session, new_id)
        session.flush()
        for old_id in (existing_id, second_id):
            old = resolve_project_workflow(session, old_id, settings)
            assert old.workflow_id == "h3.community.reference-images-to-video"
            assert old.workflow_manifest_sha256 == "a" * 64
        candidate = resolve_project_workflow(session, new_id, settings)
        assert candidate.workflow_id == "h3.tubeflow.storyboard-to-video"
        assert candidate.workflow_manifest_sha256 == "b" * 64
    with postgres_database.sessions.begin() as session:
        locked = resolve_project_workflow(
            session, new_id,
            settings.model_copy(update={"h3_candidate_project_ids": ()}),
        )
        assert locked == candidate
        old_row = session.execute(
            text(
                "SELECT script, snapshot_script, production_revision "
                "FROM projects WHERE id = :id"
            ),
            {"id": second_id},
        ).one()
        assert old_row.script == "Original immutable content."
        assert old_row.snapshot_script is None
        assert old_row.production_revision == 1
