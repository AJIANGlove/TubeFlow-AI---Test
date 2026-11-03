from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from test_video_orchestration import setup

from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.video_generation.authority import LatestAuthorityLoader
from tubeflow_test.video_generation.engine import EngineNode
from tubeflow_test.video_generation.node_health import refresh_h3_node_health
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    PersistentAttempt,
)
from tubeflow_test.video_generation.persistence import (
    VIDEO_GENERATION_TABLES,
    H3NodeRecord,
    VideoGenerationBase,
    VideoOutboxRecord,
)
from tubeflow_test.video_generation.runtime import (
    _assert_node_identity_change_safe,
    _disable_removed_nodes,
    _node_has_bound_attempt,
    ensure_private_bucket,
)
from tubeflow_test.video_generation.sqlalchemy_repository import _record as _record_now
from tubeflow_test.video_generation.worker import redrive_stale_video_outbox

assert AuthoritativeNarrationGroupRecord.__tablename__ == "authoritative_narration_groups"

# S6：`redrive_stale_video_outbox` 现在要求「attempt 自身也已停止推进」才重派。
# 本文件的契约场景都是「workflow 丢失」，attempt 早已不再更新，因此统一把
# updated_at 造旧；活跃 attempt 不被误重派的用例见 test_redrive_skips_active_attempt。
_STALE_UPDATED_AT_BACKDATE = timedelta(minutes=10)


def _record(value):  # type: ignore[no-untyped-def]
    row = _record_now(value)
    row.updated_at = datetime.now(UTC) - _STALE_UPDATED_AT_BACKDATE
    return row


class CapabilityEngine:
    def readiness(self, node: EngineNode) -> dict[str, Any]:
        return {
            "status": "ready",
            "engine_version": "community-h3-0.4.0",
            "comfyui": "ready",
            "storage": "ready",
        }

    def capabilities(self, node: EngineNode) -> dict[str, Any]:
        return {
            "items": [
                {
                    "capability": "video.reference_images_to_video",
                    "model_key": node.model_key,
                    "model_version": node.model_version,
                    "workflow_id": node.workflow_id,
                    "workflow_version": node.workflow_version,
                    "workflow_manifest_sha256": "a" * 64,
                    "status": "AVAILABLE",
                }
            ]
        }


def test_real_capability_fields_gate_ready_and_drift_goes_offline() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            H3NodeRecord(
                node_id="node",
                base_url="http://node",
                instance_identity="instance-a",
                token_secret_ref="token",
                capacity=1,
                claimed_slots=0,
                enabled=True,
                health="OFFLINE",
                capability="video.reference_images_to_video",
                model_key="minimax-h3-community",
                model_version="2026-08-13-community-duration-v1",
                workflow_id="h3.community.reference-images-to-video",
                workflow_version="1.1.0-mvp",
                manifest_sha256="a" * 64,
            )
        )
    assert refresh_h3_node_health(sessions, CapabilityEngine(), lambda _: "secret") == 1
    with sessions() as session:
        row = session.get(H3NodeRecord, "node")
        assert row is not None and row.health == "READY"
        assert row.last_heartbeat_at is not None
    with sessions.begin() as session:
        row = session.get(H3NodeRecord, "node")
        assert row is not None
        row.manifest_sha256 = "b" * 64
    assert refresh_h3_node_health(sessions, CapabilityEngine(), lambda _: "secret") == 0
    with sessions() as session:
        row = session.get(H3NodeRecord, "node")
        assert row is not None and row.health == "OFFLINE"


def test_latest_authority_loader_composes_narration_then_current_storyboard() -> None:
    source, _repository, _engine, _assets, _service = setup()

    class Narration:
        def load_group(self, session: Session, owner_id, group_id):  # type: ignore[no-untyped-def]
            assert owner_id == source.owner_id and group_id == source.group_id
            return replace(source, storyboard_is_current=False)

    class Storyboard:
        def bind_current(self, session: Session, narration):  # type: ignore[no-untyped-def]
            return replace(narration, storyboard_is_current=True)

    loaded = LatestAuthorityLoader(Narration(), Storyboard())(
        object(), source.owner_id, source.group_id  # type: ignore[arg-type]
    )
    assert loaded.storyboard_is_current and loaded.group_id == source.group_id


def test_private_bucket_is_created_once() -> None:
    class Minio:
        exists = False
        created = 0

        def bucket_exists(self, bucket: str) -> bool:
            return self.exists

        def make_bucket(self, bucket: str) -> None:
            self.created += 1
            self.exists = True

    client = Minio()
    ensure_private_bucket(client, "private")  # type: ignore[arg-type]
    ensure_private_bucket(client, "private")  # type: ignore[arg-type]
    assert client.created == 1


def test_succeeded_unarchived_attempt_blocks_node_identity_drift_on_restart() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, group, generation, attempt_id = (uuid4() for _ in range(5))
    with sessions.begin() as session:
        node = H3NodeRecord(
            node_id="stable-node",
            base_url="http://original",
            instance_identity="gpu-host-a",
            token_secret_ref="secret-ref",
            token_sha256="a" * 64,
            capacity=1,
            claimed_slots=0,
            enabled=True,
            health="READY",
            capability="video.reference_images_to_video",
            model_key="model",
            model_version="version",
            workflow_id="workflow",
            workflow_version="1",
            manifest_sha256="b" * 64,
        )
        session.add(node)
        session.flush()
        session.add(
            _record(
                PersistentAttempt(
                    attempt_id,
                    owner,
                    project,
                    group,
                    generation,
                    "key",
                    "c" * 64,
                    "d" * 64,
                    {},
                    stage=AttemptStage.SUCCEEDED,
                    node_id=node.node_id,
                    archive_stage=ArchiveStage.NOT_STARTED,
                )
            )
        )
    with sessions() as session:
        row = session.get(H3NodeRecord, "stable-node")
        assert row is not None
        with pytest.raises(RuntimeError, match="identity drift"):
            _assert_node_identity_change_safe(
                session, row, "http://replacement", "gpu-host-b", "e" * 64
            )

        with pytest.raises(RuntimeError, match="identity drift"):
            _assert_node_identity_change_safe(
                session,
                row,
                row.base_url,
                row.instance_identity,
                row.token_sha256,
                manifest_sha256="f" * 64,
            )

        with pytest.raises(RuntimeError, match="identity drift"):
            _assert_node_identity_change_safe(
                session,
                row,
                row.base_url,
                row.instance_identity,
                row.token_sha256,
                capability="video.drifted_capability",
            )


@pytest.mark.parametrize("terminal", [AttemptStage.FAILED, AttemptStage.CANCELLED])
def test_cleaned_terminal_attempt_does_not_bind_node_identity(terminal: AttemptStage) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, group, generation, attempt_id = (uuid4() for _ in range(5))
    with sessions.begin() as session:
        node = H3NodeRecord(
            node_id="cleaned-node",
            base_url="http://original",
            instance_identity="gpu-host-a",
            token_secret_ref="secret-ref",
            token_sha256="a" * 64,
            capacity=1,
            claimed_slots=0,
            enabled=True,
            health="READY",
            capability="video.reference_images_to_video",
            model_key="model",
            model_version="version",
            workflow_id="workflow",
            workflow_version="1",
            manifest_sha256="b" * 64,
        )
        session.add(node)
        session.flush()
        session.add(
            _record(
                PersistentAttempt(
                    attempt_id,
                    owner,
                    project,
                    group,
                    generation,
                    "cleaned",
                    "c" * 64,
                    "d" * 64,
                    {},
                    stage=terminal,
                    node_id=node.node_id,
                    archive_stage=ArchiveStage.NOT_STARTED,
                    staged_ids=(),
                    slot_claimed=False,
                )
            )
        )
    with sessions() as session:
        row = session.get(H3NodeRecord, "cleaned-node")
        assert row is not None
        assert not _node_has_bound_attempt(session, row.node_id)
        _assert_node_identity_change_safe(
            session, row, "http://replacement", "gpu-host-b", "e" * 64
        )


def test_archived_complete_attempt_with_audit_staged_ids_does_not_bind_node() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, group, generation, attempt_id = (uuid4() for _ in range(5))
    with sessions.begin() as session:
        node = H3NodeRecord(
            node_id="archived-node",
            base_url="http://original",
            instance_identity="gpu-host-a",
            token_secret_ref="secret-ref",
            token_sha256="a" * 64,
            capacity=1,
            claimed_slots=0,
            enabled=True,
            health="READY",
            capability="video.reference_images_to_video",
            model_key="model",
            model_version="version",
            workflow_id="workflow",
            workflow_version="1",
            manifest_sha256="b" * 64,
        )
        session.add(node)
        session.flush()
        session.add(
            _record(
                PersistentAttempt(
                    attempt_id,
                    owner,
                    project,
                    group,
                    generation,
                    "archived",
                    "c" * 64,
                    "d" * 64,
                    {},
                    stage=AttemptStage.ARCHIVED,
                    node_id=node.node_id,
                    archive_stage=ArchiveStage.COMPLETE,
                    staged_ids=("staged-a", "staged-b"),
                    cleaned_staged_ids=("staged-a", "staged-b"),
                )
            )
        )
    with sessions() as session:
        row = session.get(H3NodeRecord, "archived-node")
        assert row is not None
        assert not _node_has_bound_attempt(session, row.node_id)
        _assert_node_identity_change_safe(
            session, row, "http://replacement", "gpu-host-b", "e" * 64
        )


def test_removed_unbound_node_is_disabled_and_excluded_from_health_refresh() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            H3NodeRecord(
                node_id="removed-node",
                base_url="http://removed",
                instance_identity="old-host",
                token_secret_ref="removed-secret",
                token_sha256="a" * 64,
                capacity=1,
                claimed_slots=0,
                enabled=True,
                health="READY",
                capability="video.reference_images_to_video",
                model_key="model",
                model_version="version",
                workflow_id="workflow",
                workflow_version="1",
                manifest_sha256="b" * 64,
            )
        )
    with sessions.begin() as session:
        _disable_removed_nodes(session, set())

    def missing_token(_secret_ref: str) -> str:
        raise AssertionError("disabled removed node token must not be resolved")

    assert refresh_h3_node_health(sessions, CapabilityEngine(), missing_token) == 0
    with sessions() as session:
        row = session.get(H3NodeRecord, "removed-node")
        assert row is not None
        assert not row.enabled and row.health == "OFFLINE"


def test_stale_nonterminal_outbox_is_durably_redriven_after_workflow_loss() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, group, generation, attempt_id = (uuid4() for _ in range(5))
    with sessions.begin() as session:
        session.add(
            _record(
                PersistentAttempt(
                    attempt_id,
                    owner,
                    project,
                    group,
                    generation,
                    "redrive",
                    "a" * 64,
                    "b" * 64,
                    {},
                )
            )
        )
        session.add(
            VideoOutboxRecord(
                attempt_id=attempt_id,
                owner_id=owner,
                event_type="VIDEO_GENERATION_REQUESTED",
                created_at=datetime.now(UTC) - timedelta(minutes=10),
                dispatched_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
    assert redrive_stale_video_outbox(sessions, stale_after=timedelta(minutes=1)) == 1
    with sessions() as session:
        row = session.get(VideoOutboxRecord, attempt_id)
        assert row is not None and row.dispatched_at is None


@pytest.mark.parametrize("terminal", [AttemptStage.FAILED, AttemptStage.CANCELLED])
def test_terminal_cleanup_pending_outbox_is_redriven_after_workflow_loss(
    terminal: AttemptStage,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, group, generation, attempt_id = (uuid4() for _ in range(5))
    old = datetime.now(UTC) - timedelta(minutes=10)
    with sessions.begin() as session:
        session.add(
            _record(
                PersistentAttempt(
                    attempt_id,
                    owner,
                    project,
                    group,
                    generation,
                    "cleanup-redrive",
                    "a" * 64,
                    "b" * 64,
                    {},
                    stage=terminal,
                    archive_stage=ArchiveStage.CLEANUP_PENDING,
                    staged_ids=("staged-a",),
                    cleanup_terminal_stage=terminal,
                )
            )
        )
        session.add(
            VideoOutboxRecord(
                attempt_id=attempt_id,
                owner_id=owner,
                event_type="VIDEO_GENERATION_REQUESTED",
                created_at=old,
                dispatched_at=old,
            )
        )
    assert redrive_stale_video_outbox(sessions, stale_after=timedelta(minutes=1)) == 1
    with sessions() as session:
        row = session.get(VideoOutboxRecord, attempt_id)
        assert row is not None and row.dispatched_at is None


def test_terminal_leftover_staged_redrive_converges_after_cleanup() -> None:
    """终态遗留暂存资产的 redrive 收敛性。

    人工终态覆盖（2026-10-13 应急）留下的孤儿状态（终态 + NOT_STARTED + 暂存非空）
    会被继续重驱动以推进兜底清理出口；清理完成（staged_ids 清空）后即退出重驱动，
    不再产生噪音循环。
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, group, generation = (uuid4() for _ in range(4))
    orphan_id, cleaned_id = uuid4(), uuid4()
    old = datetime.now(UTC) - timedelta(minutes=10)
    with sessions.begin() as session:
        session.add(
            _record(
                PersistentAttempt(
                    orphan_id,
                    owner,
                    project,
                    group,
                    generation,
                    "leftover-orphan",
                    "a" * 64,
                    "b" * 64,
                    {},
                    stage=AttemptStage.CANCELLED,
                    archive_stage=ArchiveStage.NOT_STARTED,
                    staged_ids=("staged-leftover",),
                )
            )
        )
        session.add(
            _record(
                PersistentAttempt(
                    cleaned_id,
                    owner,
                    project,
                    group,
                    generation,
                    "leftover-cleaned",
                    "a" * 64,
                    "b" * 64,
                    {},
                    stage=AttemptStage.CANCELLED,
                    archive_stage=ArchiveStage.NOT_STARTED,
                )
            )
        )
        for attempt_id in (orphan_id, cleaned_id):
            session.add(
                VideoOutboxRecord(
                    attempt_id=attempt_id,
                    owner_id=owner,
                    event_type="VIDEO_GENERATION_REQUESTED",
                    created_at=old,
                    dispatched_at=old,
                )
            )
    assert redrive_stale_video_outbox(sessions, stale_after=timedelta(minutes=1)) == 1
    with sessions() as session:
        orphan_row = session.get(VideoOutboxRecord, orphan_id)
        cleaned_row = session.get(VideoOutboxRecord, cleaned_id)
        assert orphan_row is not None and orphan_row.dispatched_at is None
        assert cleaned_row is not None and cleaned_row.dispatched_at is not None


def test_deterministic_gate_failed_outbox_is_not_redriven() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, group, generation, attempt_id = (uuid4() for _ in range(5))
    old = datetime.now(UTC) - timedelta(minutes=10)
    with sessions.begin() as session:
        session.add(
            _record(
                PersistentAttempt(
                    attempt_id,
                    owner,
                    project,
                    group,
                    generation,
                    "gate-failed",
                    "a" * 64,
                    "b" * 64,
                    {},
                    stage=AttemptStage.FAILED,
                    error_code="narration_audio_bytes_changed",
                    retry_evidence=("gate:narration_audio_bytes_changed",),
                )
            )
        )
        session.add(
            VideoOutboxRecord(
                attempt_id=attempt_id,
                owner_id=owner,
                event_type="VIDEO_GENERATION_REQUESTED",
                created_at=old,
                dispatched_at=old,
            )
        )
    assert redrive_stale_video_outbox(sessions, stale_after=timedelta(minutes=1)) == 0
    with sessions() as session:
        row = session.get(VideoOutboxRecord, attempt_id)
        assert row is not None and row.dispatched_at is not None
