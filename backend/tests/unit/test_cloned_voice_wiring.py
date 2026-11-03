"""克隆音色管理 API 装配回归：生产路由器必须携带真实克隆服务（2026-10-13 503 缺陷）。

缺陷回顾：`create_identity_project_runtime` 曾调用 `create_router(...)` 时不传
`cloned_voice_service` 参数，克隆路由依赖在 None 时直接抛 503
`cloned_voice_unavailable`（上传请求 0.117s 内零逻辑失败，与号池占位同类：功能已
实现但生产未接线）。本文件锁定修复契约：

1. 生产路由器（真实 runtime 产物）的克隆路由依赖可解析出 `ClonedVoiceService`；
2. 服务部件为真实适配器：`ElevenLabsHttpClient` 与 `ElevenlabsKeyPoolService`，
   源音频存储共用 runtime 的 `PrivateObjectStorage`；
3. 惰性单例：重复解析返回同一实例（不按请求重建 EL 适配器）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from fastapi.routing import APIRoute

from tubeflow_test.adapters.elevenlabs import ElevenLabsHttpClient
from tubeflow_test.config import Settings
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService
from tubeflow_test.identity_project.cloned_voice_service import ClonedVoiceService
from tubeflow_test.identity_project.runtime import (
    IdentityProjectRuntime,
    create_identity_project_runtime,
)


def _runtime() -> IdentityProjectRuntime:
    return create_identity_project_runtime(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            identity_setup_username="admin",
            identity_setup_password="password1",
        )
    )


def _cloned_voices_dependency(runtime: IdentityProjectRuntime) -> Callable[[], ClonedVoiceService]:
    """取克隆上传路由上挂载的 `cloned_voices` 依赖（503 缺陷暴露点）。"""
    for route in runtime.router.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path != "/identity/channels/{channel_id}/cloned-voices":
            continue
        if "POST" not in route.methods:
            continue
        stack = list(route.dependant.dependencies)
        while stack:
            dependency = stack.pop()
            call = dependency.call
            if call is not None and getattr(call, "__name__", "") == "cloned_voices":
                return cast("Callable[[], ClonedVoiceService]", call)
            stack.extend(dependency.dependencies)
    raise AssertionError("cloned_voices dependency is not attached to the upload route")


def test_upload_route_resolves_a_real_cloned_voice_service() -> None:
    runtime = _runtime()
    try:
        dependency = _cloned_voices_dependency(runtime)
        service = dependency()  # 修复前：此处抛 503 cloned_voice_unavailable
        assert isinstance(service, ClonedVoiceService)
        assert isinstance(service._cloner, ElevenLabsHttpClient)
        assert isinstance(service._pool, ElevenlabsKeyPoolService)
        assert service._storage is runtime.storage
    finally:
        runtime.database.engine.dispose()


def test_cloned_voice_service_is_a_lazy_singleton() -> None:
    runtime = _runtime()
    try:
        dependency = _cloned_voices_dependency(runtime)
        assert dependency() is dependency()
    finally:
        runtime.database.engine.dispose()
