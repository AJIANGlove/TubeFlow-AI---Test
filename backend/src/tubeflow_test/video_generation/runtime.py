from __future__ import annotations

import hashlib
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter
from minio import Minio
from minio.error import S3Error
from sqlalchemy import Engine, and_, create_engine, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.config import H3NodePool, Settings
from tubeflow_test.video_generation.api import AuthenticatedOwner
from tubeflow_test.video_generation.authority import (
    NarrationAuthorityPort,
    ReferencePolicyAuthorityPort,
    StoryboardAuthorityPort,
    VideoWriteGuard,
)
from tubeflow_test.video_generation.factory import (
    VideoFeatureDependencies,
    build_video_feature,
)
from tubeflow_test.video_generation.gpu_probe import HttpGpuStatusProbe
from tubeflow_test.video_generation.node_health import refresh_h3_node_health
from tubeflow_test.video_generation.orchestration import (
    AssetStore,
    AuthoritativeGroupInput,
    StallPolicy,
)
from tubeflow_test.video_generation.persistence import (
    H3_NODE_CONFIG_LOCK_SCOPE,
    H3NodeRecord,
    VideoAttemptRecord,
)
from tubeflow_test.video_generation.project_routing import initialize_migrated_project_routes


def ensure_private_bucket(minio: Minio, bucket: str) -> None:
    """Create the private feature bucket idempotently; any storage outage fails startup."""
    if minio.bucket_exists(bucket):
        return
    try:
        minio.make_bucket(bucket)
    except S3Error as exc:
        if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise


def build_production_video_router(
    settings: Settings,
    *,
    narration_authority: NarrationAuthorityPort,
    storyboard_authority: StoryboardAuthorityPort,
    authenticated_owner: AuthenticatedOwner,
    write_guard: VideoWriteGuard,
) -> APIRouter:
    return build_production_video_runtime(
        settings,
        narration_authority=narration_authority,
        storyboard_authority=storyboard_authority,
        authenticated_owner=authenticated_owner,
        write_guard=write_guard,
    ).router


def build_production_video_runtime(
    settings: Settings,
    *,
    narration_authority: NarrationAuthorityPort | None = None,
    storyboard_authority: StoryboardAuthorityPort | None = None,
    authenticated_owner: AuthenticatedOwner,
    write_guard: VideoWriteGuard,
    database_engine: Engine | None = None,
    minio: Minio | None = None,
    group_loader: Callable[[Session, UUID, UUID], AuthoritativeGroupInput] | None = None,
    asset_store: AssetStore | None = None,
    refresh_health: bool = True,
    reference_policy_authority: ReferencePolicyAuthorityPort | None = None,
) -> VideoFeatureDependencies:
    """Concrete API composition used by the central application when H3 is configured."""
    if not settings.h3_configured:
        raise RuntimeError("production H3 runtime requires at least one configured node")
    if asset_store is None:
        raise RuntimeError("production H3 runtime requires a canonical staged archive store")
    engine = database_engine or create_engine(settings.database_url, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    token_by_ref = {
        f"configured-h3-node:{node_id}": token
        for pool in settings.h3_node_pools
        for node_id, token in zip(pool.node_ids, pool.node_tokens, strict=True)
    }
    with sessions.begin() as session:
        _bind_configured_h3_nodes(session, settings)
        initialize_migrated_project_routes(session, settings)

    minio = minio or Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    ensure_private_bucket(minio, settings.minio_bucket)
    # 未配置探测端点或共享 token 时注入 None：编排器回退到始终探测失败的保守实现。
    gpu_probe = (
        HttpGpuStatusProbe(
            urls_by_node_id={
                node_id: url
                for pool in settings.h3_node_pools
                if pool.node_gpu_status_urls
                for node_id, url in zip(pool.node_ids, pool.node_gpu_status_urls, strict=True)
            },
            token=settings.h3_gpu_agent_token.get_secret_value(),
        )
        if settings.h3_gpu_probe_configured
        else None
    )
    feature = build_video_feature(
        database_engine=engine,
        group_loader=group_loader,
        narration_authority=narration_authority,
        storyboard_authority=storyboard_authority,
        write_guard=write_guard,
        token_resolver=token_by_ref.__getitem__,
        authenticated_owner=authenticated_owner,
        asset_store=asset_store,
        stall_policy=StallPolicy(
            accepted_seconds=settings.h3_stall_accepted_seconds,
            running_seconds=settings.h3_stall_running_seconds,
            poll_seconds=settings.h3_stalled_poll_seconds,
        ),
        gpu_probe=gpu_probe,
        reference_policy_authority=reference_policy_authority,
    )
    if refresh_health:
        refresh_h3_node_health(sessions, feature.engine, token_by_ref.__getitem__)
    return feature


def _bind_configured_h3_nodes(session: Session, settings: Settings) -> None:
    """Serialize and re-read the complete node identity set in one transaction."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
            {"scope": H3_NODE_CONFIG_LOCK_SCOPE},
        )
    node_ids = [node_id for pool in settings.h3_node_pools for node_id in pool.node_ids]
    instance_ids = [value for pool in settings.h3_node_pools for value in pool.node_instance_ids]
    if len(set(node_ids)) != len(node_ids):
        raise RuntimeError("configured H3 node ids must be unique")
    if len(set(instance_ids)) != len(instance_ids):
        raise RuntimeError("configured H3 instance identities must be unique")
    for pool in settings.h3_node_pools:
        _bind_node_pool(session, pool)
    session.flush()
    for node_id in settings.h3_retired_node_ids:
        retired = session.get(H3NodeRecord, node_id)
        if retired is not None:
            _assert_node_retirement_safe(session, retired)
            retired.enabled = False
            retired.health = "OFFLINE"
    _disable_removed_nodes(
        session, {node_id for pool in settings.h3_node_pools for node_id in pool.node_ids}
    )


def _assert_node_retirement_safe(session: Session, node: H3NodeRecord) -> None:
    active_attempt = session.scalar(
        select(VideoAttemptRecord.attempt_id).where(
            VideoAttemptRecord.node_id == node.node_id,
            or_(
                VideoAttemptRecord.slot_claimed.is_(True),
                VideoAttemptRecord.stage.not_in((
                    "SUCCEEDED", "FAILED", "CANCELLED", "ARCHIVED",
                )),
                VideoAttemptRecord.archive_stage == "CLEANUP_PENDING",
                and_(
                    VideoAttemptRecord.stage == "SUCCEEDED",
                    VideoAttemptRecord.archive_stage.not_in(("COMPLETE", "QC_FAILED")),
                ),
            ),
        ).limit(1)
    )
    if node.claimed_slots > 0 or active_attempt is not None:
        raise RuntimeError(f"active H3 node cannot be retired: {node.node_id}")


def _bind_node_pool(session: Session, pool: H3NodePool) -> None:
    for node_id, instance_id, url, token, capacity in zip(
        pool.node_ids,
        pool.node_instance_ids,
        pool.node_urls,
        pool.node_tokens,
        pool.node_capacities,
        strict=True,
    ):
        row = session.get(H3NodeRecord, node_id)
        token_sha256 = hashlib.sha256(token.encode()).hexdigest()
        values = {
            "base_url": url,
            "instance_identity": instance_id,
            "token_secret_ref": f"configured-h3-node:{node_id}",
            "token_sha256": token_sha256,
            "capacity": capacity,
            "enabled": True,
            "capability": "video.reference_images_to_video",
            "model_key": pool.model_key,
            "model_version": pool.model_version,
            "workflow_id": pool.workflow_id,
            "workflow_version": pool.workflow_version,
            "manifest_sha256": pool.manifest_sha256,
        }
        if row is None:
            session.add(
                H3NodeRecord(
                    node_id=node_id,
                    claimed_slots=0,
                    health="UNKNOWN",
                    **values,
                )
            )
        else:
            drifted = _assert_node_identity_change_safe(
                session,
                row,
                url,
                instance_id,
                token_sha256,
                capability="video.reference_images_to_video",
                model_key=pool.model_key,
                model_version=pool.model_version,
                workflow_id=pool.workflow_id,
                workflow_version=pool.workflow_version,
                manifest_sha256=pool.manifest_sha256,
                capacity=capacity,
            )
            for key, value in values.items():
                setattr(row, key, value)
            if drifted:
                row.health = "UNKNOWN"


def _node_has_bound_attempt(session: Session, node_id: str) -> bool:
    return (
        session.scalar(
            select(VideoAttemptRecord.attempt_id).where(
                VideoAttemptRecord.node_id == node_id,
                or_(
                    VideoAttemptRecord.slot_claimed.is_(True),
                    VideoAttemptRecord.archive_stage == "CLEANUP_PENDING",
                    and_(
                        VideoAttemptRecord.archive_stage != "COMPLETE",
                        VideoAttemptRecord.staged_ids != [],
                    ),
                    VideoAttemptRecord.stage.in_(
                        (
                            "NOT_SUBMITTED",
                            "SUBMITTING",
                            "SUBMITTED",
                            "RUNNING",
                            "REMOTE_STATE_UNKNOWN",
                        )
                    ),
                    and_(
                        VideoAttemptRecord.stage == "SUCCEEDED",
                        VideoAttemptRecord.archive_stage != "COMPLETE",
                    ),
                ),
            )
        )
        is not None
    )


def _disable_removed_nodes(session: Session, configured_ids: set[str]) -> None:
    for persisted_id in session.scalars(select(H3NodeRecord.node_id)):
        if persisted_id in configured_ids:
            continue
        if _node_has_bound_attempt(session, persisted_id):
            raise RuntimeError(f"bound H3 node cannot disappear from configuration: {persisted_id}")
        removed = session.get(H3NodeRecord, persisted_id)
        if removed is not None:
            removed.enabled = False
            removed.health = "OFFLINE"


def _assert_node_identity_change_safe(
    session: Session,
    row: H3NodeRecord,
    base_url: str,
    instance_identity: str,
    token_sha256: str,
    *,
    capability: str | None = None,
    model_key: str | None = None,
    model_version: str | None = None,
    workflow_id: str | None = None,
    workflow_version: str | None = None,
    manifest_sha256: str | None = None,
    capacity: int | None = None,
) -> bool:
    drifted = (
        row.base_url != base_url
        or row.instance_identity != instance_identity
        or row.token_sha256 != token_sha256
        or (capability is not None and row.capability != capability)
        or (model_key is not None and row.model_key != model_key)
        or (model_version is not None and row.model_version != model_version)
        or (workflow_id is not None and row.workflow_id != workflow_id)
        or (workflow_version is not None and row.workflow_version != workflow_version)
        or (manifest_sha256 is not None and row.manifest_sha256 != manifest_sha256)
        or (capacity is not None and row.capacity != capacity)
    )
    if drifted and _node_has_bound_attempt(session, row.node_id):
        raise RuntimeError(f"H3 node identity drift is unsafe for bound attempts: {row.node_id}")
    return drifted
