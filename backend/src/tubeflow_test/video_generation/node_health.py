from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.video_generation.engine import EngineGateway, EngineNode, NodeHealth
from tubeflow_test.video_generation.persistence import H3NodeRecord


def refresh_h3_node_health(
    sessions: sessionmaker[Session],
    engine: EngineGateway,
    token_resolver: Callable[[str], str],
) -> int:
    """Probe outside DB transactions, then atomically publish each observed health fact."""
    with sessions() as session:
        snapshots = tuple(
            (
                row.node_id,
                _fingerprint(row),
                EngineNode(
                    row.node_id,
                    row.base_url,
                    token_resolver(row.token_secret_ref),
                    row.capacity,
                    NodeHealth.OFFLINE,
                    row.claimed_slots,
                    True,
                    row.model_key,
                    row.model_version,
                    row.workflow_id,
                    row.workflow_version,
                    row.manifest_sha256,
                ),
            )
            for row in session.scalars(select(H3NodeRecord).where(H3NodeRecord.enabled.is_(True)))
        )
    ready = 0
    for node_id, fingerprint, node in snapshots:
        health = "OFFLINE"
        try:
            readiness = engine.readiness(node)
            capabilities = engine.capabilities(node).get("items", [])
            compatible = any(
                isinstance(item, dict)
                and item.get("capability") == "video.reference_images_to_video"
                and item.get("model_key") == node.model_key
                and item.get("model_version") == node.model_version
                and item.get("workflow_id") == node.workflow_id
                and item.get("workflow_version") == node.workflow_version
                and item.get("workflow_manifest_sha256") == node.manifest_sha256
                and str(item.get("status", "")).upper() == "AVAILABLE"
                for item in capabilities
            )
            if str(readiness.get("status", "")).lower() == "ready" and compatible:
                health = "READY"
        except Exception:
            health = "OFFLINE"
        with sessions() as session, session.begin():
            current = session.get(H3NodeRecord, node_id, with_for_update=True)
            if current is not None and _fingerprint(current) == fingerprint:
                current.health = health
                if health == "READY":
                    current.last_heartbeat_at = datetime.now(UTC)
                    ready += 1
    return ready


def _fingerprint(row: H3NodeRecord) -> tuple[object, ...]:
    return (
        row.instance_identity,
        row.base_url,
        row.token_secret_ref,
        row.token_sha256,
        row.capacity,
        row.enabled,
        row.capability,
        row.model_key,
        row.model_version,
        row.workflow_id,
        row.workflow_version,
        row.manifest_sha256,
    )
