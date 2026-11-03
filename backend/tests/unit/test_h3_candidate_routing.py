from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, Uuid, create_engine, insert
from sqlalchemy.orm import Session

from tubeflow_test.config import Settings
from tubeflow_test.video_generation.persistence import H3ProjectWorkflowRouteRecord
from tubeflow_test.video_generation.project_routing import resolve_project_workflow


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "h3_node_urls": ("http://old",),
        "h3_node_ids": ("old",),
        "h3_node_instance_ids": ("old-instance",),
        "h3_node_tokens": ("old-token",),
        "h3_node_capacities": (1,),
        "h3_workflow_manifest_sha256": "a" * 64,
        "h3_candidate_node_urls": ("http://candidate",),
        "h3_candidate_node_ids": ("candidate",),
        "h3_candidate_node_instance_ids": ("candidate-instance",),
        "h3_candidate_node_tokens": ("candidate-token",),
        "h3_candidate_node_capacities": (1,),
        "h3_candidate_workflow_manifest_sha256": "b" * 64,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def route_session(project_id: UUID) -> Session:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    projects = Table("projects", metadata, Column("id", Uuid, primary_key=True))
    Table(
        "video_generation_attempts", metadata,
        Column("attempt_id", Uuid, primary_key=True), Column("project_id", Uuid),
    )
    metadata.create_all(engine)
    H3ProjectWorkflowRouteRecord.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(insert(projects).values(id=project_id))
    return Session(engine)


def test_single_legacy_node_and_single_candidate_are_valid() -> None:
    value = settings()
    assert value.h3_configured
    assert [len(pool.node_ids) for pool in value.h3_node_pools] == [1, 1]


def test_three_legacy_nodes_can_coexist_with_candidate() -> None:
    value = settings(
        h3_node_urls=("http://a", "http://b", "http://c"),
        h3_node_ids=("a", "b", "c"),
        h3_node_instance_ids=("ia", "ib", "ic"),
        h3_node_tokens=("ta", "tb", "tc"), h3_node_capacities=(1, 1, 1),
    )
    assert sum(len(pool.node_ids) for pool in value.h3_node_pools) == 4


def test_duplicate_identity_across_pools_is_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        settings(h3_candidate_node_ids=("old",))
    with pytest.raises(ValueError, match="unique"):
        settings(h3_candidate_node_instance_ids=("old-instance",))


def test_candidate_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="align"):
        settings(h3_candidate_node_tokens=())
    with pytest.raises(ValueError, match="sha256"):
        settings(h3_candidate_workflow_manifest_sha256="")
    with pytest.raises(ValueError, match="approved"):
        settings(h3_candidate_workflow_id="unapproved")


def test_migrated_project_remains_legacy_even_when_allowlisted() -> None:
    project_id = uuid4()
    with route_session(project_id) as session, session.begin():
        session.add(H3ProjectWorkflowRouteRecord(
            project_id=project_id, profile="legacy", bound_at=datetime.now(UTC),
        ))
        session.flush()
        snapshot = resolve_project_workflow(
            session, project_id, settings(h3_candidate_project_ids=(project_id,))
        )
        assert snapshot.workflow_id == "h3.community.reference-images-to-video"
        assert snapshot.workflow_manifest_sha256 == "a" * 64


def test_candidate_binding_is_immutable_after_configuration_changes() -> None:
    project_id = uuid4()
    with route_session(project_id) as session:
        with session.begin():
            first = resolve_project_workflow(
                session, project_id, settings(h3_candidate_project_ids=(project_id,))
            )
        with session.begin():
            second = resolve_project_workflow(
                session, project_id, settings(h3_candidate_workflow_manifest_sha256="c" * 64)
            )
        assert first == second
        assert second.workflow_id == "h3.tubeflow.storyboard-to-video"
        assert second.workflow_manifest_sha256 == "b" * 64
        assert second.model_version == "2026-08-13-community-duration-v1"
        assert second.resolution_preset == "preview"


def test_unlisted_new_project_is_locked_to_legacy() -> None:
    project_id = uuid4()
    with route_session(project_id) as session:
        with session.begin():
            first = resolve_project_workflow(session, project_id, settings())
        with session.begin():
            second = resolve_project_workflow(
                session, project_id, settings(h3_candidate_project_ids=(project_id,))
            )
        assert first == second
        assert second.workflow_id == "h3.community.reference-images-to-video"


def test_existing_attempt_prevents_candidate_adoption_without_migration_marker() -> None:
    project_id = uuid4()
    with route_session(project_id) as session, session.begin():
        table = Table("video_generation_attempts", MetaData(), autoload_with=session.connection())
        session.execute(insert(table).values(attempt_id=uuid4().hex, project_id=project_id.hex))
        snapshot = resolve_project_workflow(
            session, project_id, settings(h3_candidate_project_ids=(project_id,))
        )
        assert snapshot.workflow_id == "h3.community.reference-images-to-video"


def test_runtime_binds_both_pools_without_rewriting_legacy_identity() -> None:
    from tubeflow_test.video_generation.persistence import H3NodeRecord
    from tubeflow_test.video_generation.runtime import _bind_configured_h3_nodes

    engine = create_engine("sqlite://")
    H3NodeRecord.__table__.create(engine)
    with Session(engine) as session, session.begin():
        _bind_configured_h3_nodes(session, settings())
        legacy = session.get(H3NodeRecord, "old")
        candidate = session.get(H3NodeRecord, "candidate")
        assert legacy is not None and candidate is not None
        legacy.health = "READY"
        _bind_configured_h3_nodes(session, settings())
        assert legacy.workflow_id == "h3.community.reference-images-to-video"
        assert legacy.manifest_sha256 == "a" * 64
        assert legacy.health == "READY"
        assert candidate.workflow_id == "h3.tubeflow.storyboard-to-video"
        assert candidate.manifest_sha256 == "b" * 64


def test_scheduler_rejects_cross_pool_workflow() -> None:
    from tubeflow_test.video_generation.persistence import H3NodeRecord
    from tubeflow_test.video_generation.project_routing import configured_workflow
    from tubeflow_test.video_generation.sqlalchemy_repository import _matches

    value = settings()
    legacy = configured_workflow(value, "legacy")
    candidate = configured_workflow(value, "candidate")
    node = H3NodeRecord(
        node_id="candidate", enabled=True, capability="video.reference_images_to_video",
        model_key=candidate.model_key, model_version=candidate.model_version,
        workflow_id=candidate.workflow_id, workflow_version=candidate.workflow_version,
        manifest_sha256=candidate.workflow_manifest_sha256,
    )
    assert _matches(node, candidate)
    assert not _matches(node, legacy)


def test_migration_locks_all_existing_projects_to_legacy() -> None:
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import select

    path = Path(__file__).parents[2] / "migrations/versions/0029_h3_project_workflow_routes.py"
    spec = importlib.util.spec_from_file_location("route_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    projects = Table("projects", metadata, Column("id", Uuid, primary_key=True))
    metadata.create_all(engine)
    old_ids = (uuid4(), uuid4())
    with engine.begin() as connection:
        connection.execute(insert(projects), [{"id": value} for value in old_ids])
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        # 0029 predates the current route columns; select only its actual schema.
        route = H3ProjectWorkflowRouteRecord.__table__
        rows = connection.execute(
            select(route.c.project_id, route.c.profile, route.c.workflow_snapshot_json)
        ).all()
        assert {row.project_id for row in rows} == set(old_ids)
        assert all(row.profile == "legacy" and row.workflow_snapshot_json is None for row in rows)


def test_candidate_health_counts_only_its_exact_registered_contract() -> None:
    from sqlalchemy.orm import sessionmaker

    from tubeflow_test.video_generation.availability import H3Availability, H3ContractRequirements
    from tubeflow_test.video_generation.persistence import H3NodeRecord
    from tubeflow_test.video_generation.runtime import _bind_configured_h3_nodes

    engine = create_engine("sqlite://")
    H3NodeRecord.__table__.create(engine)
    sessions = sessionmaker(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        _bind_configured_h3_nodes(session, settings())
        candidate = session.get(H3NodeRecord, "candidate")
        assert candidate is not None
        candidate.health = "READY"
        candidate.last_heartbeat_at = now
    available = H3Availability(
        sessions, H3ContractRequirements.from_settings(settings()), clock=lambda: now
    )
    assert available()["available"] is True
    with sessions.begin() as session:
        candidate = session.get(H3NodeRecord, "candidate")
        assert candidate is not None
        candidate.manifest_sha256 = "c" * 64
    assert available()["available"] is False


def test_startup_pins_migrated_project_before_later_legacy_config_change() -> None:
    from tubeflow_test.video_generation.project_routing import initialize_migrated_project_routes

    project_id = uuid4()
    with route_session(project_id) as session:
        with session.begin():
            session.add(H3ProjectWorkflowRouteRecord(
                project_id=project_id, profile="legacy", bound_at=datetime.now(UTC),
            ))
            session.flush()
            initialize_migrated_project_routes(session, settings())
        with session.begin():
            initialize_migrated_project_routes(
                session, settings(h3_workflow_manifest_sha256="c" * 64)
            )
            snapshot = resolve_project_workflow(
                session, project_id, settings(h3_workflow_manifest_sha256="c" * 64)
            )
        assert snapshot.workflow_manifest_sha256 == "a" * 64
