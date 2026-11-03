from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from test_video_orchestration import setup

from tubeflow_test.adapters.persistence.production_authority import (
    SqlCanonicalNarrationAuthority,
    SqlCanonicalStoryboardAuthority,
)
from tubeflow_test.api import app as app_module
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.video_generation.api import create_group_video_router, create_video_router
from tubeflow_test.video_generation.authority import LatestAuthorityLoader
from tubeflow_test.video_generation.manual_intent import HydratedManualH3IntentWriter
from tubeflow_test.video_generation.orchestration import (
    DELIVER_WITH_DEFECT_MARKER,
    ArchiveStage,
    AttemptStage,
)


def test_manual_h3_post_calls_writer_with_authenticated_owner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    owner, group, generation, attempt = uuid4(), uuid4(), uuid4(), uuid4()
    project = uuid4()

    class Writer:
        seen: dict[str, object] = {}

        def create(self, **kwargs: object) -> UUID:
            self.seen = kwargs
            return attempt

        def get(self, owner_id: UUID, attempt_id: UUID) -> object | None:
            if (owner_id, attempt_id) != (owner, attempt):
                return None
            return SimpleNamespace(
                attempt_id=attempt,
                project_id=project,
                group_id=group,
                generation_id=generation,
                stage=AttemptStage.CREATED,
                archive_stage=ArchiveStage.NOT_STARTED,
                remote_phase_changed_at=None,
                stalled_at=None,
            )

    writer = Writer()
    account = Account(owner, "user", "hash", AccountRole.USER)
    identity = type("IdentityRuntime", (), {"router": APIRouter(prefix="/identity")})()
    narration = type(
        "NarrationRuntime",
        (),
        {
            "service": object(),
            "current_owner": staticmethod(lambda: account),
            "http_client": httpx.AsyncClient(),
            "database": type("Database", (), {"session": staticmethod(lambda: None)})(),
            "storage": type("Storage", (), {"client": object()})(),
        },
    )()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    monkeypatch.setattr(
        app_module, "create_storyboard_runtime_router", lambda *_args, **_kwargs: APIRouter()
    )
    monkeypatch.setattr(
        app_module, "create_cover_runtime_router", lambda *_args, **_kwargs: APIRouter()
    )
    app = app_module.create_app(
        narration_runtime=narration,  # type: ignore[arg-type]
        h3_writer=writer,  # type: ignore[arg-type]
        current_account=lambda: account,
    )
    app.state.group_video_availability = lambda: {
        "available": True,
        "status": "AVAILABLE",
        "message": "Video service is available.",
    }
    response = TestClient(app).post(
        f"/group-videos/groups/{group}/generations",
        headers={"Idempotency-Key": "manual-h3"},
        json={"expected_storyboard_generation_id": str(generation)},
    )
    assert response.status_code == 202, response.text
    assert response.json() == {
        "attempt_id": str(attempt),
        "project_id": str(project),
        "group_id": str(group),
        "generation_id": str(generation),
        "stage": "CREATED",
        "public_status": "QUEUED",
        "message": "Video generation is waiting to start.",
        "allowed_actions": ["CANCEL", "REFRESH"],
        "remote_wait_status": None,
        "remote_wait_seconds": None,
    }
    assert writer.seen == {
        "owner_id": owner,
        "group_id": group,
        "expected_storyboard_generation_id": generation,
        "idempotency_key": "manual-h3",
    }


def test_openapi_has_exactly_one_manual_h3_post(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    account = Account(uuid4(), "user", "hash", AccountRole.USER)
    identity = type("IdentityRuntime", (), {"router": APIRouter(prefix="/identity")})()
    narration = type(
        "NarrationRuntime",
        (),
        {
            "service": object(),
            "current_owner": staticmethod(lambda: account),
            "http_client": httpx.AsyncClient(),
            "database": type("Database", (), {"session": staticmethod(lambda: None)})(),
            "storage": type("Storage", (), {"client": object()})(),
        },
    )()
    monkeypatch.setattr(app_module, "create_identity_project_runtime", lambda _settings: identity)
    monkeypatch.setattr(
        app_module, "create_storyboard_runtime_router", lambda *_args, **_kwargs: APIRouter()
    )
    monkeypatch.setattr(
        app_module, "create_cover_runtime_router", lambda *_args, **_kwargs: APIRouter()
    )
    writer = type("Writer", (), {"create": lambda self, **kwargs: uuid4()})()
    app = app_module.create_app(
        narration_runtime=narration,  # type: ignore[arg-type]
        h3_writer=writer,  # type: ignore[arg-type]
        current_account=lambda: account,
    )
    posts = {
        path
        for path, operations in app.openapi()["paths"].items()
        if "post" in operations and path.startswith("/group-videos/groups/")
    }
    assert posts == {"/group-videos/groups/{group_id}/generations"}


@pytest.mark.parametrize(
    ("stage", "archive_stage", "public_status", "allowed_actions"),
    [
        (AttemptStage.CREATED, ArchiveStage.NOT_STARTED, "QUEUED", ["CANCEL", "REFRESH"]),
        (
            AttemptStage.SUBMITTING,
            ArchiveStage.NOT_STARTED,
            "RECOVERY_REQUIRED",
            ["RECONCILE", "CANCEL", "REFRESH"],
        ),
        (
            AttemptStage.REMOTE_STATE_UNKNOWN,
            ArchiveStage.NOT_STARTED,
            "RECOVERY_REQUIRED",
            ["RECONCILE", "CANCEL", "REFRESH"],
        ),
        (
            AttemptStage.RUNNING,
            ArchiveStage.NOT_STARTED,
            "RUNNING",
            ["RECONCILE", "CANCEL", "REFRESH"],
        ),
        (AttemptStage.FAILED, ArchiveStage.NOT_STARTED, "FAILED", ["RETRY", "REFRESH"]),
        (AttemptStage.SUCCEEDED, ArchiveStage.QC_FAILED, "FAILED", ["RETRY", "REFRESH"]),
        (
            AttemptStage.ARCHIVED,
            ArchiveStage.COMPLETE,
            "SUCCEEDED",
            ["VIEW_MEDIA", "SELECT_CURRENT"],
        ),
    ],
)
def test_production_attempt_get_exposes_only_stable_public_status(
    stage: AttemptStage,
    archive_stage: ArchiveStage,
    public_status: str,
    allowed_actions: list[str],
) -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, f"status-{stage.value}")
    repository.attempts[attempt.attempt_id] = replace(
        attempt,
        stage=stage,
        archive_stage=archive_stage,
        node_id="provider-node-secret",
        remote_id="provider-task-secret",
        raw_receipt={"token": "Bearer SECRET"},
        error_code="provider_internal_secret",
        probe_evidence={"message": "provider SECRET", "qc_passed": False},
    )
    app = FastAPI()
    app.include_router(create_group_video_router(service, lambda: source.owner_id))

    response = TestClient(app).get(f"/group-videos/attempts/{attempt.attempt_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["public_status"] == public_status
    assert payload["stage"] == stage.value
    assert payload["allowed_actions"] == allowed_actions
    assert set(("public_status", "stage", "message", "allowed_actions")) <= payload.keys()
    assert not {
        "archive_stage",
        "error_code",
        "node_id",
        "remote_id",
        "raw_receipt",
        "request",
        "idempotency_key",
        "qc",
    } & payload.keys()
    assert "provider-node-secret" not in response.text
    assert "provider-task-secret" not in response.text
    assert "Bearer SECRET" not in response.text
    assert "provider_internal_secret" not in response.text


@pytest.mark.parametrize("manual_first", [False, True])
def test_duplicate_attempt_get_router_order_keeps_same_safe_public_contract(
    manual_first: bool,
) -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, "router-order")
    repository.attempts[attempt.attempt_id] = replace(
        attempt,
        stage=AttemptStage.REMOTE_STATE_UNKNOWN,
        node_id="hidden-node",
        remote_id="hidden-remote-task",
        raw_receipt={"token": "hidden-token"},
    )
    account = Account(source.owner_id, "user", "hash", AccountRole.USER)

    class Writer:
        def create(self, **_kwargs: object) -> UUID:
            return attempt.attempt_id

        def get(self, owner_id: UUID, attempt_id: UUID) -> object | None:
            if owner_id != source.owner_id:
                return None
            return repository.get_attempt(attempt_id)

    production = create_group_video_router(service, lambda: source.owner_id)
    manual = create_video_router(Writer(), lambda: account)  # type: ignore[arg-type]
    app = FastAPI()
    first, second = (manual, production) if manual_first else (production, manual)
    app.include_router(first)
    app.include_router(second)

    response = TestClient(app).get(f"/group-videos/attempts/{attempt.attempt_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["public_status"] == "RECOVERY_REQUIRED"
    assert payload["allowed_actions"] == ["RECONCILE", "CANCEL", "REFRESH"]
    assert "hidden-node" not in response.text
    assert "hidden-remote-task" not in response.text
    assert "hidden-token" not in response.text
    assert "RETRY" not in payload["allowed_actions"]


def test_manual_h3_intent_is_hydrated_for_worker_consumption() -> None:
    source, repository, _engine, _assets, service = setup()

    class Sessions:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    writer = HydratedManualH3IntentWriter(
        service,
        Sessions,  # type: ignore[arg-type]
        lambda _session, owner_id, group_id: (
            source
            if (owner_id, group_id) == (source.owner_id, source.group_id)
            else (_ for _ in ()).throw(AssertionError("unexpected authority lookup"))
        ),
    )
    attempt_id = writer.create(
        owner_id=source.owner_id,
        group_id=source.group_id,
        expected_storyboard_generation_id=source.storyboard_generation_id,
        idempotency_key="manual-h3-worker-ready",
    )
    persisted = repository.get_attempt(attempt_id)
    assert persisted is not None
    assert persisted.input_snapshot is not None
    assert persisted.input_snapshot["storyboard_generation_id"] == str(
        source.storyboard_generation_id
    )
    assert persisted.stage.value == "CREATED"


def test_production_video_authority_ports_compose_canonical_snapshot() -> None:
    source, _repository, _engine, _assets, _service = setup()

    class Loader:
        def __call__(self, session: object, owner_id: UUID, group_id: UUID):  # type: ignore[no-untyped-def]
            assert session is authority_session
            assert (owner_id, group_id) == (source.owner_id, source.group_id)
            return source

    class AuthoritySession:
        def scalar(self, _statement: object) -> object:
            return object()

    authority_session = AuthoritySession()
    loader = LatestAuthorityLoader(
        SqlCanonicalNarrationAuthority(Loader()),  # type: ignore[arg-type]
        SqlCanonicalStoryboardAuthority(),
    )
    assert loader(authority_session, source.owner_id, source.group_id) is source  # type: ignore[arg-type]


_DEFECT_FINDINGS = [
    {
        "start_second": 0.0,
        "end_second": 4.0,
        "bbox_columns": [0, 1279],
        "bbox_rows": [0, 719],
        "area_fraction": 0.97,
    }
]


def _archived_attempt(repository, attempt, **overrides: object):  # type: ignore[no-untyped-def]
    archived = replace(
        attempt,
        stage=AttemptStage.ARCHIVED,
        archive_stage=ArchiveStage.COMPLETE,
        archived_asset=MediaAssetRef(uuid4(), "a" * 64, "video/mp4", 1024),
        **overrides,
    )
    repository.attempts[attempt.attempt_id] = archived
    return archived


def test_group_video_views_expose_defect_delivery_marker_and_findings() -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, "defect-view")
    archived = _archived_attempt(
        repository,
        attempt,
        retry_evidence=(DELIVER_WITH_DEFECT_MARKER,),
        probe_evidence={
            "video_duration_ms": 4000,
            "qc_passed": False,
            "error_code": "video_content_black_region",
            "content_qc": {
                "algorithm": "sustained_black_region_v1",
                "findings": _DEFECT_FINDINGS,
            },
        },
    )
    repository.current[source.group_id] = archived.attempt_id
    app = FastAPI()
    app.include_router(create_group_video_router(service, lambda: source.owner_id))
    client = TestClient(app)

    history = client.get(f"/group-videos/groups/{source.group_id}/videos")
    assert history.status_code == 200
    item = history.json()["items"][0]
    assert item["delivered_with_defect"] is True
    assert item["content_defect"] == {
        "code": "video_content_black_region",
        "findings": _DEFECT_FINDINGS,
    }
    # candidate 语义不变：带病交付 qc_passed=False，不可作为切换候选。
    assert item["candidate"] is False
    assert item["qc_passed"] is False
    assert item["current"] is True

    current = client.get(f"/group-videos/groups/{source.group_id}/video/current")
    assert current.status_code == 200
    assert current.json()["delivered_with_defect"] is True
    assert current.json()["content_defect"]["findings"] == _DEFECT_FINDINGS


def test_group_video_views_default_defect_fields_for_healthy_attempt() -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, "healthy-view")
    _archived_attempt(
        repository,
        attempt,
        probe_evidence={"video_duration_ms": 3000, "qc_passed": True},
    )
    app = FastAPI()
    app.include_router(create_group_video_router(service, lambda: source.owner_id))

    response = TestClient(app).get(f"/group-videos/groups/{source.group_id}/videos")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["delivered_with_defect"] is False
    assert item["content_defect"] is None
    assert item["candidate"] is True


@pytest.mark.parametrize(
    ("marker", "probe", "expected_defect"),
    (
        # 有标记但 probe 缺 content_qc 块：只透出标记，不伪造缺陷细节。
        ((DELIVER_WITH_DEFECT_MARKER,), {"qc_passed": False}, None),
        # 其他 error_code 的 content_qc 证据不组装缺陷视图。
        (
            (),
            {
                "qc_passed": False,
                "error_code": "video_content_qc_sampler_error",
                "content_qc": {"algorithm": "a", "findings": []},
            },
            None,
        ),
        # 无标记的黑屏 QC 失败（未交付）：透出缺陷证据但不标记带病交付。
        (
            (),
            {
                "qc_passed": False,
                "error_code": "video_content_black_region",
                "content_qc": {"algorithm": "a", "findings": _DEFECT_FINDINGS},
            },
            {"code": "video_content_black_region", "findings": _DEFECT_FINDINGS},
        ),
    ),
)
def test_group_video_view_content_defect_requires_black_region_findings(
    marker: tuple[str, ...],
    probe: dict[str, object],
    expected_defect: dict[str, object] | None,
) -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, f"defect-edge-{uuid4()}")
    _archived_attempt(repository, attempt, retry_evidence=marker, probe_evidence=probe)
    app = FastAPI()
    app.include_router(create_group_video_router(service, lambda: source.owner_id))

    response = TestClient(app).get(f"/group-videos/groups/{source.group_id}/videos")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["delivered_with_defect"] is bool(marker)
    assert item["content_defect"] == expected_defect
