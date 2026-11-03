from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.config import Settings
from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    ReferencePolicy,
    ReferenceStrategy,
    ShotPanelImage,
    VideoRuleViolation,
    canonical_json,
    canonical_sha256,
)
from tubeflow_test.video_generation.native_size import (
    WORKFLOW_NATIVE_SIZE_UNKNOWN,
    is_registered,
)
from tubeflow_test.video_generation.persistence import (
    H3ProjectWorkflowRouteRecord,
    VideoAttemptRecord,
)


def configured_workflow(settings: Settings, profile: str) -> ImmutableWorkflowSnapshot:
    pool = next(pool for pool in settings.h3_node_pools if pool.profile == profile)
    if profile == "candidate" and not pool.node_ids:
        raise VideoRuleViolation("candidate_video_route_not_configured")
    return ImmutableWorkflowSnapshot(
        pool.model_key, pool.model_version, pool.workflow_id, pool.workflow_version,
        pool.manifest_sha256, "preview",
        {
            "capability": "video.reference_images_to_video",
            "resolution_preset": "preview",
            "model_key": pool.model_key,
            "model_version": pool.model_version,
            "workflow_id": pool.workflow_id,
            "workflow_version": pool.workflow_version,
        },
    )


def _snapshot_payload(snapshot: ImmutableWorkflowSnapshot) -> dict[str, object]:
    return {
        "model_key": snapshot.model_key,
        "model_version": snapshot.model_version,
        "workflow_id": snapshot.workflow_id,
        "workflow_version": snapshot.workflow_version,
        "workflow_manifest_sha256": snapshot.workflow_manifest_sha256,
        "resolution_preset": snapshot.resolution_preset,
        "manifest": json.loads(canonical_json(snapshot.manifest)),
    }


def initialize_migrated_project_routes(session: Session, settings: Settings) -> None:
    """Fill migration markers once, before any candidate can serve new projects."""
    routes = tuple(session.scalars(
        select(H3ProjectWorkflowRouteRecord).where(
            H3ProjectWorkflowRouteRecord.profile == "legacy",
            H3ProjectWorkflowRouteRecord.workflow_snapshot_json.is_(None),
        ).with_for_update()
    ))
    if routes:
        snapshot = configured_workflow(settings, "legacy")
        for route in routes:
            route.workflow_snapshot_json = _snapshot_payload(snapshot)
        session.flush()


def resolve_project_workflow(
    session: Session, project_id: UUID, settings: Settings
) -> ImmutableWorkflowSnapshot:
    """Pin one provider route per project without changing its business snapshot.

    Migration-created rows always select legacy. New projects use the configured
    default or explicit candidate allowlist before their first H3 intent.
    The caller owns the transaction and must commit the binding with the intent.
    """
    if session.scalar(
        select(ProjectRow.id).where(ProjectRow.id == project_id).with_for_update()
    ) is None:
        raise VideoRuleViolation("video_project_not_found")
    route = session.get(H3ProjectWorkflowRouteRecord, project_id)
    if route is None:
        has_attempt = session.scalar(
            select(VideoAttemptRecord.attempt_id)
            .where(VideoAttemptRecord.project_id == project_id).limit(1)
        ) is not None
        profile = (
            "candidate"
            if not has_attempt and (
                settings.h3_default_workflow_profile == "candidate"
                or project_id in settings.h3_candidate_project_ids
            )
            else "legacy"
        )
        route = H3ProjectWorkflowRouteRecord(
            project_id=project_id, profile=profile, bound_at=datetime.now(UTC),
        )
        session.add(route)
    if route.workflow_snapshot_json is None:
        snapshot = configured_workflow(settings, route.profile)
        _assert_native_size_registered(snapshot)
        route.workflow_snapshot_json = _snapshot_payload(snapshot)
        session.flush()
        return snapshot
    raw = route.workflow_snapshot_json
    manifest = raw.get("manifest")
    if not isinstance(manifest, dict):
        raise VideoRuleViolation("video_project_route_invalid")
    return _assert_native_size_registered(
        ImmutableWorkflowSnapshot(
            str(raw["model_key"]), str(raw["model_version"]), str(raw["workflow_id"]),
            str(raw["workflow_version"]), str(raw["workflow_manifest_sha256"]),
            str(raw["resolution_preset"]), manifest,
        )
    )


def _assert_native_size_registered(
    snapshot: ImmutableWorkflowSnapshot,
) -> ImmutableWorkflowSnapshot:
    """提交前护栏：未登记原生尺寸的工作流不得创建意图。

    质检按工作流原生尺寸校验（见 native_size）；若工作流身份未登记，质检必然
    fail-closed。与其让一组视频先烧 12–20 分钟 GPU 再在质检被拒，不如在意图创建
    时直接给出稳定失败码。
    """
    if not is_registered(snapshot.workflow_id, snapshot.workflow_version):
        raise VideoRuleViolation(WORKFLOW_NATIVE_SIZE_UNKNOWN)
    return snapshot


def resolve_reference_policy(
    session: Session,
    project_id: UUID,
    requested: ReferencePolicy,
    *,
    for_intent: bool,
    single_panel_allowed: bool,
) -> ReferencePolicy:
    """Resolve a candidate without writes, or bind it with the real intent transaction."""
    route = session.get(
        H3ProjectWorkflowRouteRecord, project_id, with_for_update=for_intent
    )
    if route is None and for_intent:
        route = H3ProjectWorkflowRouteRecord(
            project_id=project_id, profile="legacy", bound_at=datetime.now(UTC)
        )
        session.add(route)
        session.flush()
    bound = route.reference_strategy if route is not None else None
    if (
        bound == ReferenceStrategy.COMPOSITE.value
        and requested.strategy is not ReferenceStrategy.COMPOSITE
    ):
        raise VideoRuleViolation("reference_policy_already_bound")
    if bound == ReferenceStrategy.SINGLE_PANEL_V1.value:
        if route is None or route.reference_derivation_json is None:
            raise VideoRuleViolation("reference_policy_invalid")
        selected = ReferencePolicy(
            ReferenceStrategy.SINGLE_PANEL_V1, route.reference_derivation_json
        )
    elif bound == ReferenceStrategy.COMPOSITE.value:
        if route is not None and route.reference_derivation_json is not None:
            raise VideoRuleViolation("reference_policy_invalid")
        selected = ReferencePolicy()
    else:
        selected = requested
    if selected.strategy is ReferenceStrategy.SINGLE_PANEL_V1 and not single_panel_allowed:
        raise VideoRuleViolation("single_panel_opt_in_disabled")
    if route is not None and route.reference_strategy is None and for_intent:
        route.reference_strategy = selected.strategy.value
        route.reference_derivation_json = selected.derivation_payload
        session.flush()
    return selected


def reference_input_fingerprint(
    authority_snapshot: dict[str, object] | None,
    policy: ReferencePolicy,
    panels: tuple[ShotPanelImage, ...],
) -> str:
    if not authority_snapshot:
        raise VideoRuleViolation("authoritative_video_input_required")
    return canonical_sha256(
        {
            "authority": authority_snapshot,
            "reference_policy": {
                "strategy": policy.strategy.value,
                "derivation": policy.derivation_payload,
            },
            "panels": [
                {
                    "id": str(panel.asset.asset_id),
                    "sha256": panel.asset.sha256,
                    "media_type": panel.asset.media_type,
                    "size_bytes": panel.asset.size_bytes,
                    "shot_order": panel.shot_order,
                    "source_storyboard_sha256": panel.source_storyboard_sha256,
                    "derivation": panel.derivation,
                }
                for panel in panels
            ],
        }
    )
