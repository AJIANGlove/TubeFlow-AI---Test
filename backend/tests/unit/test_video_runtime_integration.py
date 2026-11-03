from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from tubeflow_test.api import app as app_module
from tubeflow_test.config import Settings
from tubeflow_test.video_generation.runtime import build_production_video_runtime


def test_central_app_mounts_production_video_router_and_cors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    dynamic = APIRouter(prefix="/group-videos")

    @dynamic.post("/attempts/{attempt_id}/reconcile")
    def reconcile(attempt_id: str) -> dict[str, str]:
        return {"attempt_id": attempt_id}

    settings = Settings(
        h3_node_urls=("http://a", "http://b", "http://c"),
        h3_node_ids=("node-a", "node-b", "node-c"),
        h3_node_instance_ids=("instance-a", "instance-b", "instance-c"),
        h3_node_tokens=("a", "b", "c"),
        h3_node_capacities=(1, 1, 1),
        h3_workflow_manifest_sha256="a" * 64,
    )
    identity = type("IdentityRuntime", (), {"router": APIRouter(prefix="/identity")})()
    narration = type(
        "NarrationRuntime",
        (),
        {
            "http_client": httpx.AsyncClient(),
            "service": object(),
            "current_owner": staticmethod(lambda: None),
        },
    )()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    monkeypatch.setattr(app_module, "create_narration_runtime", lambda *_args, **_kwargs: narration)
    monkeypatch.setattr(app_module, "create_narration_router", lambda *_args: APIRouter())
    client = TestClient(app_module.create_app(settings, production_video_router=dynamic))
    assert client.post("/group-videos/attempts/one/reconcile").status_code == 200
    response = client.options(
        "/group-videos/attempts/one/reconcile",
        headers={
            "Origin": settings.allowed_origins[0],
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Idempotency-Key",
        },
    )
    assert response.status_code == 200
    assert "idempotency-key" in response.headers["access-control-allow-headers"].lower()


def test_production_h3_rejects_legacy_archive_store_fallback() -> None:
    settings = Settings(
        h3_node_urls=("http://a", "http://b", "http://c"),
        h3_node_ids=("node-a", "node-b", "node-c"),
        h3_node_instance_ids=("instance-a", "instance-b", "instance-c"),
        h3_node_tokens=("a", "b", "c"),
        h3_node_capacities=(1, 1, 1),
        h3_workflow_manifest_sha256="a" * 64,
    )
    with pytest.raises(RuntimeError, match="canonical staged archive store"):
        build_production_video_runtime(
            settings,
            authenticated_owner=lambda: None,  # type: ignore[arg-type, return-value]
            write_guard=lambda _owner, _group: None,
        )


def test_default_h3_composition_injects_canonical_archive_adapter(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        h3_node_urls=("http://a", "http://b", "http://c"),
        h3_node_ids=("node-a", "node-b", "node-c"),
        h3_node_instance_ids=("instance-a", "instance-b", "instance-c"),
        h3_node_tokens=("a", "b", "c"),
        h3_node_capacities=(1, 1, 1),
        h3_workflow_manifest_sha256="a" * 64,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    database = type(
        "Database", (), {"engine": engine, "session": staticmethod(lambda: None)}
    )()
    storage = type("Storage", (), {"client": object()})()
    narration = type(
        "NarrationRuntime",
        (),
        {
            "database": database,
            "storage": storage,
            "service": object(),
            "current_owner": staticmethod(lambda: None),
            "current_owner_id": staticmethod(lambda: None),
            "current_media_owner": staticmethod(lambda: None),
            "http_client": httpx.AsyncClient(),
        },
    )()
    identity = type("IdentityRuntime", (), {"router": APIRouter(prefix="/identity")})()
    canonical_store = object()
    seen: dict[str, object] = {}

    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    monkeypatch.setattr(app_module, "create_narration_router", lambda *_args: APIRouter())
    monkeypatch.setattr(
        app_module, "create_storyboard_runtime_router", lambda *_args, **_kwargs: APIRouter()
    )
    monkeypatch.setattr(
        app_module, "create_cover_runtime_router", lambda *_args, **_kwargs: APIRouter()
    )
    monkeypatch.setattr(
        app_module, "build_h3_archive_adapter", lambda _sessions, value: canonical_store
    )

    def build_video(*_args, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return type(
            "Feature",
            (),
            {"router": APIRouter(), "service": object(), "close": staticmethod(lambda: None)},
        )()

    monkeypatch.setattr(app_module, "build_production_video_runtime", build_video)
    monkeypatch.setattr(
        app_module,
        "create_media_runtime",
        lambda *_args, **_kwargs: type(
            "Media", (), {"router": APIRouter(), "startup": staticmethod(lambda: None)}
        )(),
    )
    app_module.create_app(settings, narration_runtime=narration)  # type: ignore[arg-type]
    assert seen["asset_store"] is canonical_store
