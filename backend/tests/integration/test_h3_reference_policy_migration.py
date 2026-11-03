from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from conftest import PostgresDatabase, ProductionParentChain
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from tubeflow_test.video_generation.persistence import (
    H3ProjectWorkflowRouteRecord,
    VideoAttemptRecord,
)


def _config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    config.attributes["database_url"] = database_url
    return config


def _add_project_without_attempt(session: Session, ids: ProductionParentChain) -> None:
    project_id = uuid4()
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
            "owner_id": ids.owner_id,
            "channel_id": ids.channel_id,
            "title": "reference-policy-no-attempt",
            "script": "No H3 attempt exists for this project.",
            "character_image_ids": [ids.character_id],
            "visual_style": "documentary",
            "voice_version_id": ids.voice_id,
            "locale": "en-US",
            "aspect_ratio": "16:9",
            "draft_revision": 1,
            "production_state": "ACTIVE",
            "production_revision": 1,
        },
    )
    # This fixture runs at 0045, before the route's 0046 columns exist.
    session.execute(
        H3ProjectWorkflowRouteRecord.__table__.insert().values(
            project_id=project_id,
            profile="legacy",
            bound_at=datetime.now(UTC),
        )
    )


@pytest.mark.integration
def test_0046_backfills_composite_only_for_projects_with_attempts(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    ids, storyboard_generation_id = storyboard_parent_chain
    config = _config(postgres_database.url)
    command.downgrade(config, "0045")
    with postgres_database.sessions.begin() as session:
        session.execute(
            H3ProjectWorkflowRouteRecord.__table__.insert().values(
                project_id=ids.project_id,
                profile="legacy",
                bound_at=datetime.now(UTC),
            )
        )
        session.add(
            VideoAttemptRecord(
                attempt_id=uuid4(),
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                group_id=ids.group_id,
                generation_id=storyboard_generation_id,
                idempotency_key="reference-policy-backfill",
                generation_sha256="1" * 64,
                request_sha256="2" * 64,
                request_json={},
                stage="CREATED",
                staged_ids=[],
                cleaned_staged_ids=[],
                archive_stage="NOT_STARTED",
                retry_evidence=[],
                slot_claimed=False,
                intent_sequence=1,
                updated_at=datetime.now(UTC),
            )
        )
        _add_project_without_attempt(session, ids)
    command.upgrade(config, "0046")

    with postgres_database.sessions.begin() as session:
        routes = tuple(session.scalars(select(H3ProjectWorkflowRouteRecord)))
        by_project = {row.project_id: row for row in routes}
        assert by_project[ids.project_id].reference_strategy == "composite"
        assert by_project[ids.project_id].reference_derivation_json is None
        no_attempt = next(row for row in routes if row.project_id != ids.project_id)
        assert no_attempt.reference_strategy is None
        assert no_attempt.reference_derivation_json is None

    constraints = {
        item["name"]
        for item in inspect(postgres_database.engine).get_check_constraints(
            "h3_project_workflow_routes"
        )
    }
    assert "ck_h3_project_workflow_routes_reference_strategy" in constraints


@pytest.mark.integration
def test_0046_allows_downgrade_without_single_panel_or_derivation(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    ids = production_parent_chain
    with postgres_database.sessions.begin() as session:
        session.add(
            H3ProjectWorkflowRouteRecord(
                project_id=ids.project_id,
                profile="legacy",
                reference_strategy="composite",
                bound_at=datetime.now(UTC),
            )
        )
    config = _config(postgres_database.url)
    command.downgrade(config, "0045")
    columns = {
        item["name"]
        for item in inspect(postgres_database.engine).get_columns("h3_project_workflow_routes")
    }
    assert "reference_strategy" not in columns
    assert "reference_derivation_json" not in columns

    command.upgrade(config, "0046")
    with postgres_database.sessions.begin() as session:
        route = session.get(H3ProjectWorkflowRouteRecord, ids.project_id)
        assert route is not None
        assert route.reference_strategy is None
        assert route.reference_derivation_json is None


@pytest.mark.integration
def test_0046_rejects_unknown_reference_strategy(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), postgres_database.sessions.begin() as session:
        session.add(
            H3ProjectWorkflowRouteRecord(
                project_id=production_parent_chain.project_id,
                profile="legacy",
                reference_strategy="unsupported",
                bound_at=datetime.now(UTC),
            )
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("strategy", "derivation"),
    [
        ("single_panel_v1", None),
        ("composite", {"version": "crop-title54-v1"}),
    ],
)
def test_0046_rejects_downgrade_with_single_panel_or_derivation(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
    strategy: str,
    derivation: dict[str, object] | None,
) -> None:
    ids = production_parent_chain
    with postgres_database.sessions.begin() as session:
        session.add(
            H3ProjectWorkflowRouteRecord(
                project_id=ids.project_id,
                profile="legacy",
                reference_strategy=strategy,
                reference_derivation_json=derivation,
                bound_at=datetime.now(UTC),
            )
        )
    with pytest.raises(RuntimeError, match="Cannot downgrade while a single-panel"):
        command.downgrade(_config(postgres_database.url), "0045")
