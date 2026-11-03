from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.config import H3NodePool, Settings
from tubeflow_test.video_generation.engine import NodeHealth
from tubeflow_test.video_generation.persistence import H3NodeRecord

_SHA256 = re.compile(r"[0-9a-f]{64}")
H3_HEARTBEAT_FRESHNESS = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class H3ContractRequirements:
    node_ids: tuple[str, ...]
    capability: str
    model_key: str
    model_version: str
    workflow_id: str
    workflow_version: str
    manifest_sha256: str
    alternatives: tuple[H3ContractRequirements, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> H3ContractRequirements | None:
        contracts = [
            value for pool in settings.h3_node_pools
            if (value := cls.from_pool(pool)) is not None
        ]
        if not contracts:
            return None
        return replace(contracts[0], alternatives=tuple(contracts[1:]))

    @classmethod
    def from_pool(cls, pool: H3NodePool) -> H3ContractRequirements | None:
        required_values = (
            pool.node_urls, pool.node_ids, pool.node_instance_ids,
            pool.node_tokens, pool.node_capacities,
        )
        if not all(required_values) or any(
            not value for values in required_values for value in values
        ):
            return None
        if any(capacity < 1 for capacity in pool.node_capacities):
            return None
        if not all((pool.model_key, pool.model_version, pool.workflow_id, pool.workflow_version)):
            return None
        if not _SHA256.fullmatch(pool.manifest_sha256):
            return None
        return cls(
            pool.node_ids, "video.reference_images_to_video", pool.model_key,
            pool.model_version, pool.workflow_id, pool.workflow_version, pool.manifest_sha256,
        )



class H3Availability:
    def __init__(
        self,
        sessions: sessionmaker[Session] | None,
        requirements: H3ContractRequirements | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._requirements = requirements
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self) -> dict[str, object]:
        if self._requirements is None:
            return unconfigured_h3_contract()
        if self._sessions is None:
            return _unavailable_contract()
        cutoff = self._clock() - H3_HEARTBEAT_FRESHNESS
        requirements = self._requirements
        try:
            with self._sessions() as session:
                compatible = session.scalar(
                    select(H3NodeRecord.node_id)
                    .where(
                        H3NodeRecord.enabled.is_(True),
                        H3NodeRecord.health == NodeHealth.READY.value,
                        H3NodeRecord.last_heartbeat_at >= cutoff,
                        or_(
                            *(
                                and_(
                                    H3NodeRecord.node_id.in_(contract.node_ids),
                                    H3NodeRecord.capability == contract.capability,
                                    H3NodeRecord.model_key == contract.model_key,
                                    H3NodeRecord.model_version == contract.model_version,
                                    H3NodeRecord.workflow_id == contract.workflow_id,
                                    H3NodeRecord.workflow_version == contract.workflow_version,
                                    H3NodeRecord.manifest_sha256 == contract.manifest_sha256,
                                )
                                for contract in (requirements, *requirements.alternatives)
                            )
                        ),
                    )
                    .limit(1)
                )
        except SQLAlchemyError:
            return _unavailable_contract()
        if compatible is None:
            return _unavailable_contract()
        return _contract(
            True,
            "AVAILABLE",
            "Video service is available.",
        )


def _unavailable_contract() -> dict[str, object]:
    return _contract(
        False,
        "UNAVAILABLE",
        "No healthy compatible video service node was observed in the last 30 seconds.",
    )


def unconfigured_h3_contract() -> dict[str, object]:
    return _contract(
        False,
        "NOT_CONFIGURED",
        "Video service is not configured.",
    )


def _contract(available: bool, status: str, message: str) -> dict[str, object]:
    return {"available": available, "status": status, "message": message}
