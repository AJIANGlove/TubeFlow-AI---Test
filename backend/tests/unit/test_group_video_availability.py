from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tubeflow_test.config import Settings
from tubeflow_test.video_generation.api import router
from tubeflow_test.video_generation.availability import (
    H3Availability,
    H3ContractRequirements,
)
from tubeflow_test.video_generation.persistence import H3NodeRecord

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
MANIFEST = "a" * 64


def _settings(*, tokens: tuple[str, ...] = ("a", "b", "c")) -> Settings:
    return Settings(
        h3_node_urls=("http://a", "http://b", "http://c"),
        h3_node_ids=("node-a", "node-b", "node-c"),
        h3_node_instance_ids=("instance-a", "instance-b", "instance-c"),
        h3_node_tokens=tokens,
        h3_node_capacities=(1, 1, 1),
        h3_workflow_manifest_sha256=MANIFEST,
    )


def _sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    H3NodeRecord.__table__.create(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _node(
    node_id: str,
    *,
    heartbeat_at: datetime = NOW,
    health: str = "READY",
    capacity: int = 1,
    claimed_slots: int = 0,
    manifest_sha256: str = MANIFEST,
) -> H3NodeRecord:
    return H3NodeRecord(
        node_id=node_id,
        base_url=f"http://{node_id}",
        instance_identity=f"instance-{node_id}",
        token_secret_ref=f"secret-{node_id}",
        token_sha256="b" * 64,
        capacity=capacity,
        claimed_slots=claimed_slots,
        enabled=True,
        health=health,
        last_heartbeat_at=heartbeat_at,
        capability="video.reference_images_to_video",
        model_key="minimax-h3-community",
        model_version="2026-08-13-community-duration-v1",
        workflow_id="h3.community.reference-images-to-video",
        workflow_version="1.1.0-mvp",
        manifest_sha256=manifest_sha256,
    )


def _availability(
    sessions: sessionmaker[Session], settings: Settings | None = None
) -> H3Availability:
    requirements = H3ContractRequirements.from_settings(settings or _settings())
    return H3Availability(sessions, requirements, clock=lambda: NOW)


def test_contract_is_available_for_fresh_healthy_compatible_node_and_is_safe() -> None:
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(_node("node-a"))
    app = FastAPI()
    app.include_router(router)
    app.state.group_video_availability = _availability(sessions)

    response = TestClient(app).get("/group-videos/contract")

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "status": "AVAILABLE",
        "message": "Video service is available.",
    }
    rendered = response.text
    assert "http://node-a" not in rendered
    assert "secret-node-a" not in rendered
    assert "node-a" not in rendered


def test_contract_is_unavailable_when_heartbeat_is_older_than_30_seconds() -> None:
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(_node("node-a", heartbeat_at=NOW - timedelta(seconds=31)))

    assert _availability(sessions)() == {
        "available": False,
        "status": "UNAVAILABLE",
        "message": "No healthy compatible video service node was observed in the last 30 seconds.",
    }


def test_contract_is_unavailable_when_manifest_is_incompatible() -> None:
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(_node("node-a", manifest_sha256="c" * 64))

    assert _availability(sessions)()["available"] is False


def test_full_but_fresh_healthy_compatible_node_remains_available() -> None:
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(_node("node-a", capacity=2, claimed_slots=2))

    assert _availability(sessions)()["available"] is True


def test_any_fresh_healthy_compatible_node_makes_multi_node_contract_available() -> None:
    sessions = _sessions()
    with sessions.begin() as session:
        session.add_all(
            (
                _node("node-a", health="OFFLINE"),
                _node("node-b", manifest_sha256="c" * 64),
                _node("node-c", capacity=1, claimed_slots=1),
            )
        )

    assert _availability(sessions)()["available"] is True


def test_missing_required_h3_configuration_fails_closed_without_querying_nodes() -> None:
    sessions = _sessions()
    with sessions.begin() as session:
        session.add(_node("node-a"))
    requirements = H3ContractRequirements.from_settings(_settings(tokens=("a", "", "c")))

    assert requirements is None
    assert H3Availability(sessions, requirements, clock=lambda: NOW)() == {
        "available": False,
        "status": "NOT_CONFIGURED",
        "message": "Video service is not configured.",
    }
