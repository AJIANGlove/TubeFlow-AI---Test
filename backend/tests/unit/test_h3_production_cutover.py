from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, create_engine, insert
from sqlalchemy.orm import sessionmaker
from test_h3_candidate_routing import route_session, settings

from tubeflow_test.video_generation.domain import VideoCapacityUnavailable
from tubeflow_test.video_generation.node_health import refresh_h3_node_health
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    PersistentAttempt,
)
from tubeflow_test.video_generation.persistence import (
    VIDEO_GENERATION_TABLES,
    H3NodeRecord,
    H3ProjectWorkflowRouteRecord,
    VideoAttemptRecord,
    VideoGenerationBase,
)
from tubeflow_test.video_generation.project_routing import (
    configured_workflow,
    resolve_project_workflow,
)
from tubeflow_test.video_generation.runtime import _bind_configured_h3_nodes, _disable_removed_nodes
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository, _record


def test_default_candidate_accepts_new_project_without_allowlist_and_pins_snapshot() -> None:
    project_id = uuid4()
    value = settings(h3_default_workflow_profile="candidate")
    with route_session(project_id) as session:
        with session.begin():
            first = resolve_project_workflow(session, project_id, value)
            route = session.get(H3ProjectWorkflowRouteRecord, project_id)
            assert route is not None and route.profile == "candidate"
        with session.begin():
            second = resolve_project_workflow(session, project_id, settings())
        assert first == second
        assert first.workflow_id == "h3.tubeflow.storyboard-to-video"
        assert first.workflow_version == "1.0.0"


def test_default_candidate_keeps_existing_legacy_route() -> None:
    project_id = uuid4()
    with route_session(project_id) as session:
        with session.begin():
            first = resolve_project_workflow(session, project_id, settings())
        with session.begin():
            second = resolve_project_workflow(
                session, project_id, settings(h3_default_workflow_profile="candidate")
            )
        assert first == second
        assert second.workflow_id == "h3.community.reference-images-to-video"


def test_default_candidate_does_not_adopt_existing_attempt_without_route() -> None:
    project_id = uuid4()
    with route_session(project_id) as session, session.begin():
        table = Table("video_generation_attempts", MetaData(), autoload_with=session.connection())
        session.execute(insert(table).values(attempt_id=uuid4().hex, project_id=project_id.hex))
        snapshot = resolve_project_workflow(
            session, project_id, settings(h3_default_workflow_profile="candidate")
        )
        assert snapshot.workflow_id == "h3.community.reference-images-to-video"


def test_default_candidate_requires_configured_approved_pool() -> None:
    with pytest.raises(ValueError, match="require configured candidate"):
        settings(
            h3_default_workflow_profile="candidate",
            h3_candidate_node_ids=(),
            h3_candidate_node_urls=(),
            h3_candidate_node_tokens=(),
            h3_candidate_node_instance_ids=(),
            h3_candidate_node_capacities=(),
        )
    with pytest.raises(ValueError, match="default workflow profile"):
        settings(h3_default_workflow_profile="unknown")


@pytest.mark.parametrize("node_id", ["candidate", "not-configured"])
def test_retirement_is_explicitly_limited_to_configured_legacy_nodes(node_id: str) -> None:
    with pytest.raises(ValueError, match="configured legacy pool"):
        settings(h3_retired_node_ids=(node_id,))


def test_retired_node_keeps_history_but_is_not_probed_or_scheduled_after_rebind() -> None:
    engine = create_engine("sqlite://")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    value = settings(h3_retired_node_ids=("old",), h3_default_workflow_profile="candidate")
    historical = PersistentAttempt(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "history",
        "a" * 64,
        "b" * 64,
        {},
        stage=AttemptStage.SUCCEEDED,
        archive_stage=ArchiveStage.QC_FAILED,
        node_id="old",
        staged_ids=("historical-staged",),
        slot_claimed=False,
    )
    pending = PersistentAttempt(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "new",
        "c" * 64,
        "d" * 64,
        {},
    )
    with sessions.begin() as session:
        _bind_configured_h3_nodes(session, value)
        session.add(_record(historical))
        session.add(_record(pending))
    with sessions.begin() as session:
        _bind_configured_h3_nodes(session, value)
        node = session.get(H3NodeRecord, "old")
        assert node is not None and not node.enabled and node.health == "OFFLINE"
        assert node.base_url == "http://old" and node.instance_identity == "old-instance"
        attempt = session.get(VideoAttemptRecord, historical.attempt_id)
        assert attempt is not None
        assert attempt.stage == "SUCCEEDED" and attempt.archive_stage == "QC_FAILED"
        assert attempt.staged_ids == ["historical-staged"]

    probed = []

    class Probe:
        def readiness(self, node):
            probed.append(node.node_id)
            return {"status": "ready"}

        def capabilities(self, node):
            return {
                "items": [
                    {
                        "status": "AVAILABLE",
                        "capability": "video.reference_images_to_video",
                        "model_key": node.model_key,
                        "model_version": node.model_version,
                        "workflow_id": node.workflow_id,
                        "workflow_version": node.workflow_version,
                        "workflow_manifest_sha256": node.manifest_sha256,
                    }
                ]
            }

    assert refresh_h3_node_health(sessions, Probe(), lambda ref: "token") == 1
    assert probed == ["candidate"]
    repository = SqlAlchemyVideoRepository(sessions, lambda *_: None, lambda _: "token")
    with pytest.raises(VideoCapacityUnavailable, match="no compatible"):
        repository.claim_compatible_node(pending.attempt_id, configured_workflow(value, "legacy"))
    chosen = repository.claim_compatible_node(
        pending.attempt_id, configured_workflow(value, "candidate")
    )
    assert chosen.node_id == "candidate"
    with sessions.begin() as session, pytest.raises(RuntimeError, match="cannot disappear"):
        _disable_removed_nodes(session, {"candidate"})
    with sessions.begin() as session, pytest.raises(RuntimeError, match="identity drift"):
        _bind_configured_h3_nodes(
            session,
            settings(
                h3_retired_node_ids=("old",),
                h3_node_instance_ids=("replacement",),
            ),
        )


@pytest.mark.parametrize(
    "stage,archive_stage,slot_claimed,claimed_slots",
    [
        (AttemptStage.CREATED, ArchiveStage.NOT_STARTED, False, 0),
        (AttemptStage.STAGING, ArchiveStage.NOT_STARTED, False, 0),
        (AttemptStage.RUNNING, ArchiveStage.NOT_STARTED, False, 0),
        (AttemptStage.REMOTE_STATE_UNKNOWN, ArchiveStage.NOT_STARTED, False, 0),
        (AttemptStage.SUCCEEDED, ArchiveStage.NOT_STARTED, False, 0),
        (AttemptStage.FAILED, ArchiveStage.CLEANUP_PENDING, False, 0),
        (AttemptStage.SUCCEEDED, ArchiveStage.QC_FAILED, True, 0),
        (AttemptStage.SUCCEEDED, ArchiveStage.QC_FAILED, False, 1),
    ],
)
def test_retirement_refuses_active_or_unfinished_node(
    stage, archive_stage, slot_claimed, claimed_slots,
) -> None:
    engine = create_engine("sqlite://")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    attempt = PersistentAttempt(
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "pending", "a" * 64, "b" * 64, {},
        node_id="old", stage=stage, archive_stage=archive_stage, slot_claimed=slot_claimed,
    )
    with sessions.begin() as session:
        _bind_configured_h3_nodes(session, settings())
        row = session.get(H3NodeRecord, "old")
        assert row is not None
        row.claimed_slots = claimed_slots
        session.add(_record(attempt))
    with sessions.begin() as session, pytest.raises(RuntimeError, match="cannot be retired"):
        _bind_configured_h3_nodes(session, settings(h3_retired_node_ids=("old",)))
    with sessions() as session:
        row = session.get(H3NodeRecord, "old")
        assert row is not None and row.enabled
