"""封面运行时装配：token 未配置时注入的显式失败 stub（不静默降级）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from tubeflow_test.api.cover_runtime import (
    _UnconfiguredCoverImageProvider,
    _UnconfiguredCoverPromptProvider,
    _UnconfiguredCoverRefAnalysisProvider,
    create_cover_runtime_router,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account, AccountRole


def test_unconfigured_analysis_provider_fails_explicitly() -> None:
    provider = _UnconfiguredCoverRefAnalysisProvider()
    with pytest.raises(DomainError) as exc:
        provider.analyze([])
    assert exc.value.code == "cover_analysis_provider_not_configured"
    provider.close()


def test_unconfigured_prompt_provider_fails_explicitly() -> None:
    provider = _UnconfiguredCoverPromptProvider()
    with pytest.raises(DomainError) as exc:
        provider.complete([], temperature=0.9)
    assert exc.value.code == "cover_prompt_provider_not_configured"
    provider.close()


def test_unconfigured_image_provider_fails_explicitly() -> None:
    provider = _UnconfiguredCoverImageProvider()
    with pytest.raises(DomainError) as exc:
        provider.generate(prompt="prompt")
    assert exc.value.code == "cover_image_provider_not_configured"
    provider.close()


def test_cover_runtime_router_factory_constructs_real_dependencies(tmp_path: Path) -> None:
    from uuid import uuid4

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime-does-not-connect.db'}",
        minio_endpoint="127.0.0.1:9000",
        temporal_target="127.0.0.1:7233",
    )
    closers: list[object] = []
    router = create_cover_runtime_router(
        settings,
        lambda: Account(uuid4(), "user", "hash", AccountRole.USER),
        work_dir=tmp_path / "covers",
        closers=closers,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    paths = set(app.openapi()["paths"])
    assert "/identity/channels/{channel_id}/cover-style" in paths
    assert "/projects/{project_id}/cover/prompts" in paths
    assert "/projects/{project_id}/cover/images/{generation_id}/media" in paths
    assert len(closers) == 4  # 四个 Provider（含视觉复审）的 close 全部接入生命周期
