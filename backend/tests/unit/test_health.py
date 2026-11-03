import asyncio
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyDeletionRepository,
    production_active_job_gates,
)
from tubeflow_test.api import app as app_module
from tubeflow_test.config import Settings
from tubeflow_test.identity_project.domain import Account, AccountRole


def test_liveness_and_identity_runtime_are_mounted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    router = APIRouter(prefix="/identity")

    @router.get("/setup/status")
    def status() -> dict[str, bool]:
        return {"complete": False}

    runtime = type("Runtime", (), {"router": router})()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: runtime)
    narration = _narration_runtime()
    app = app_module.create_app(narration_runtime=narration)  # type: ignore[arg-type]
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert TestClient(app).get("/identity/setup/status").status_code == 200
    assert app.state.identity_project_runtime is runtime


def test_production_deletion_gate_manifest_is_complete() -> None:
    gates = production_active_job_gates()
    assert tuple(gate.name for gate in gates) == (
        "narration",
        "storyboard",
        "h3",
        "composition",
    )
    SqlAlchemyDeletionRepository(lambda: None, gates)  # type: ignore[arg-type]


def test_module_asgi_target_constructs_the_same_central_app(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    router = APIRouter(prefix="/identity")
    startup_calls: list[str] = []

    @router.on_event("startup")
    def startup_gate() -> None:
        startup_calls.append("bucket-and-reconcile")

    runtime = type("Runtime", (), {"router": router})()
    narration = _narration_runtime()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: runtime)
    monkeypatch.setattr(app_module, "create_narration_runtime", lambda _settings: narration)
    monkeypatch.setattr(app_module, "_runtime_app", None)

    with TestClient(app_module.app) as client:
        assert client.get("/health/live").status_code == 200
    assert app_module._runtime_app is not None
    assert startup_calls and set(startup_calls) == {"bucket-and-reconcile"}


def _narration_runtime() -> SimpleNamespace:
    owner = Account(UUID(int=1), "owner", "hash", AccountRole.USER)

    class Service:
        async def confirm_and_start(self, owner_id: UUID, project_id: UUID) -> dict[str, object]:
            assert owner_id == owner.id
            return {"project_id": str(project_id), "job_id": "narration-job", "state": "QUEUED"}

    return SimpleNamespace(
        service=Service(),
        current_owner=lambda: owner,
        http_client=httpx.AsyncClient(),
    )


def test_production_app_mounts_real_narration_router_and_confirm_returns_202(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    identity = type("Runtime", (), {"router": APIRouter(prefix="/identity")})()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    runtime = _narration_runtime()
    project_id = UUID(int=10)
    with TestClient(app_module.create_app(narration_runtime=runtime)) as client:  # type: ignore[arg-type]
        response = client.post("/narration/confirm", json={"project_id": str(project_id)})
    assert response.status_code == 202
    assert response.json() == {
        "project_id": str(project_id),
        "job_id": "narration-job",
        "state": "QUEUED",
        "public_status": "QUEUED",
        "message": "Waiting to start.",
        "allowed_actions": ["REFRESH"],
    }


def test_create_app_mounts_media_runtime_and_runs_lifecycle(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    router = APIRouter()

    @router.get("/media-mounted")
    def mounted() -> dict[str, bool]:
        return {"mounted": True}

    class Runtime:
        started = False

        def __init__(self) -> None:
            self.router = router

        def startup(self) -> None:
            self.started = True

    identity = type("IdentityRuntime", (), {"router": APIRouter(prefix="/identity")})()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    runtime = Runtime()
    narration = _narration_runtime()
    with TestClient(
        app_module.create_app(media_runtime=runtime, narration_runtime=narration)  # type: ignore[arg-type]
    ) as client:
        assert client.get("/media-mounted").json() == {"mounted": True}
    assert runtime.started


def test_central_lifespan_runs_identity_startup_gate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    identity = type(
        "IdentityRuntime",
        (),
        {
            "router": APIRouter(prefix="/identity"),
            "startup": staticmethod(lambda: calls.append("identity")),
        },
    )()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    narration = _narration_runtime()

    with TestClient(app_module.create_app(narration_runtime=narration)) as client:  # type: ignore[arg-type]
        assert client.get("/health/live").status_code == 200

    assert calls == ["identity"]


def test_default_web_lifespan_does_not_consume_narration_outbox(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    identity = type("IdentityRuntime", (), {"router": APIRouter(prefix="/identity")})()
    started: list[str] = []
    closed: list[str] = []
    narration = _narration_runtime()

    async def start() -> None:
        started.append("relay")

    async def close() -> None:
        closed.append("runtime")

    narration.start = start
    narration.close = close
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    monkeypatch.setattr(app_module, "create_narration_runtime", lambda _settings: narration)
    with TestClient(app_module.create_app()) as client:
        assert client.get("/health/live").status_code == 200
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
    assert started == []
    assert closed == []
    assert narration.http_client.is_closed


def test_startup_failure_still_closes_every_resource() -> None:
    closed: list[str] = []

    class Media:
        def startup(self) -> None:
            raise OSError("media startup failed")

    narration = _narration_runtime()

    async def close_http() -> None:
        closed.append("http")

    narration.http_client.aclose = close_http

    def first() -> None:
        closed.append("first")
        raise ValueError("first close failed")

    def second() -> None:
        closed.append("second")
        raise RuntimeError("second close failed")

    async def run() -> None:
        async with app_module._runtime_lifespan(  # type: ignore[arg-type]
            object(), narration, Media(), False, [first, second]
        ):
            raise AssertionError("startup failure must prevent serving")

    with pytest.raises(BaseExceptionGroup) as raised:
        asyncio.run(run())
    assert closed == ["http", "second", "first"]
    rendered = repr(raised.value.exceptions)
    assert "media startup failed" in rendered
    assert "second close failed" in rendered
    assert "first close failed" in rendered


def test_frontend_statics_require_revalidation(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """§22：生产 SPA 静态资源必须 no-cache，防止部署后浏览器新旧模块混载。"""
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("console.log(1)", encoding="utf-8")
    router = APIRouter(prefix="/identity")

    @router.get("/setup/status")
    def status() -> dict[str, bool]:
        return {"complete": False}

    runtime = type("Runtime", (), {"router": router})()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: runtime)
    app = app_module.create_app(
        Settings(frontend_dir=tmp_path), narration_runtime=_narration_runtime()
    )
    client = TestClient(app)
    for path in ("/app.js", "/"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
