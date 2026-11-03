"""号池管理 API 装配回归：必须接真实 EL 配额端口（2026-10-13「添加」422 缺陷）。

缺陷回顾：`create_app` 曾将号池路由的配额端口装配为占位实现
（`ElevenlabsQuotaUnavailable`，抛 `elevenlabs_unreachable` 且不发任何网络请求），
导致管理页「添加 key」必然失败。本文件锁定修复后的装配契约：

1. 生产运行时（create_narration_runtime）暴露 `elevenlabs_quota`，号池复用同一实例；
2. 测试注入的 runtime 替身未携带该字段时，装配层构造真实 `ElevenLabsQuotaAdapter`；
3. 两者皆无时快速失败，而不是静默回落到占位实现。
"""

from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import APIRouter

from tubeflow_test.adapters.elevenlabs import ElevenLabsQuotaAdapter
from tubeflow_test.api import app as app_module
from tubeflow_test.identity_project.domain import Account, AccountRole


def _narration_fake(**extra: object) -> SimpleNamespace:
    owner = Account(UUID(int=1), "owner", "hash", AccountRole.USER)
    return SimpleNamespace(
        service=object(),
        current_owner=lambda: owner,
        http_client=httpx.AsyncClient(),
        **extra,
    )


def _identity_with_database() -> SimpleNamespace:
    """含 database 的 identity 替身：命名空间加载必须走到号池路由装配。"""
    return SimpleNamespace(
        router=APIRouter(prefix="/identity"),
        database=SimpleNamespace(session=lambda: None),
    )


def _capture_pool_service(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def capture(service: object) -> APIRouter:
        captured["service"] = service
        return APIRouter()

    monkeypatch.setattr(
        app_module, "create_identity_project_runtime", lambda _settings: _identity_with_database()
    )
    monkeypatch.setattr(app_module, "create_elevenlabs_pool_router", capture)
    return captured


def test_pool_api_reuses_the_runtime_quota_port(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_pool_service(monkeypatch)
    probe = object()  # 任意哨兵：只验证同一实例被透传
    runtime = _narration_fake(elevenlabs_quota=probe)

    app_module.create_app(narration_runtime=runtime)  # type: ignore[arg-type]

    service = captured["service"]
    # 私有属性访问：锁定装配把 runtime 的配额端口原样透传（缺陷防回归）
    assert service._quota is probe  # type: ignore[attr-defined]


def test_pool_api_falls_back_to_a_real_quota_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_pool_service(monkeypatch)
    runtime = _narration_fake()  # 注入替身：无 elevenlabs_quota 字段

    app_module.create_app(narration_runtime=runtime)  # type: ignore[arg-type]

    service = captured["service"]
    assert isinstance(service._quota, ElevenLabsQuotaAdapter)  # type: ignore[attr-defined]


def test_pool_api_fails_fast_without_any_quota_source(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_pool_service(monkeypatch)
    runtime = SimpleNamespace(service=object(), current_owner=lambda: None)  # 无端口也无 client

    with pytest.raises(RuntimeError, match="quota port or an http client"):
        app_module.create_app(narration_runtime=runtime)  # type: ignore[arg-type]

    assert "service" not in captured
