"""封面运行时装配：Settings → 适配器 → 仓储 → dispatcher 生命周期（照 storyboard_runtime）。

三个 Provider 通道的 token 未配置时注入显式失败 stub：对应动作以稳定码
（cover_*_provider_not_configured）落 FAILED / 失败返回，不静默降级、不伪造成功。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter
from minio import Minio
from temporalio.client import Client
from temporalio.worker import Worker

from tubeflow_test.adapters.cover_image import LingkeaiCoverImageProvider
from tubeflow_test.adapters.cover_image_grsai import GrsaiCoverAnchorImageProvider
from tubeflow_test.adapters.cover_image_qc import PillowCoverImageQc
from tubeflow_test.adapters.cover_image_review import GrsaiCoverImageReviewProvider
from tubeflow_test.adapters.cover_prompt import DeepSeekCoverPromptProvider
from tubeflow_test.adapters.cover_ref_analysis import GrsaiCoverRefAnalysisProvider
from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.cover.repository import SqlAlchemyCoverRepository
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.api.cover import create_cover_router
from tubeflow_test.application.cover_characters import CoverCharacterService
from tubeflow_test.application.cover_dispatcher import CoverOutboxDispatcher
from tubeflow_test.application.cover_images import CoverImageService
from tubeflow_test.application.cover_prompts import CoverPromptService
from tubeflow_test.application.cover_style import CoverStyleService
from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account
from tubeflow_test.workflows.cover import (
    CoverActivities,
    TemporalCoverWorkflowStarter,
    create_cover_worker,
)

COVER_TASK_QUEUE = "cover"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidFactory:
    def new(self) -> UUID:
        return uuid4()


class _UnconfiguredCoverRefAnalysisProvider:
    def analyze(self, messages: Sequence[Mapping[str, Any]]) -> object:
        raise DomainError(
            "cover_analysis_provider_not_configured", "封面参考图解析服务未配置。"
        )

    def close(self) -> None:
        return None


class _UnconfiguredCoverPromptProvider:
    def complete(self, messages: Sequence[Mapping[str, Any]], *, temperature: float) -> object:
        raise DomainError("cover_prompt_provider_not_configured", "封面文本服务未配置。")

    def close(self) -> None:
        return None


class _UnconfiguredCoverImageProvider:
    def generate(self, *, prompt: str) -> object:
        raise DomainError("cover_image_provider_not_configured", "封面出图服务未配置。")

    def close(self) -> None:
        return None


class _UnconfiguredCoverImageReviewProvider:
    def review(self, messages: Sequence[Mapping[str, Any]]) -> object:
        raise DomainError(
            "cover_analysis_provider_not_configured", "封面视觉复审服务未配置。"
        )

    def close(self) -> None:
        return None


@dataclass(slots=True)
class SupervisedCoverRunner:
    worker: Worker
    dispatcher: CoverOutboxDispatcher
    closers: tuple[Callable[[], None], ...] = ()

    def close(self) -> None:
        for close in reversed(self.closers):
            close()


def _analysis_provider(settings: Settings) -> Any:
    if not settings.cover_analysis_provider_configured:
        return _UnconfiguredCoverRefAnalysisProvider()
    return GrsaiCoverRefAnalysisProvider(
        settings.cover_analysis_provider_url,
        settings.cover_analysis_provider_token.get_secret_value(),
        model=settings.cover_analysis_provider_model,
        fallback_model=settings.cover_analysis_provider_fallback_model,
        timeout_seconds=settings.cover_analysis_provider_timeout_seconds,
    )


def _prompt_provider(settings: Settings) -> Any:
    if not settings.cover_prompt_provider_configured:
        return _UnconfiguredCoverPromptProvider()
    return DeepSeekCoverPromptProvider(
        settings.cover_prompt_provider_url,
        settings.cover_prompt_provider_token.get_secret_value(),
        model=settings.cover_prompt_provider_model,
        fallback_model=settings.cover_prompt_provider_fallback_model,
        timeout_seconds=settings.cover_prompt_provider_timeout_seconds,
    )


def _image_provider(settings: Settings) -> Any:
    if not settings.cover_image_provider_configured:
        return _UnconfiguredCoverImageProvider()
    return LingkeaiCoverImageProvider(
        settings.cover_image_provider_url,
        settings.cover_image_provider_token.get_secret_value(),
        model=settings.cover_image_provider_model,
        size=settings.cover_image_provider_size,
        quality=settings.cover_image_provider_quality,
        timeout_seconds=settings.cover_image_provider_timeout_seconds,
    )


def _review_provider(settings: Settings) -> Any:
    """出图后视觉复审：复用 cover_analysis 通道配置（2026-10-15 用户确认）。"""
    if not settings.cover_analysis_provider_configured:
        return _UnconfiguredCoverImageReviewProvider()
    return GrsaiCoverImageReviewProvider(
        settings.cover_analysis_provider_url,
        settings.cover_analysis_provider_token.get_secret_value(),
        model=settings.cover_analysis_provider_model,
        fallback_model=settings.cover_analysis_provider_fallback_model,
        timeout_seconds=settings.cover_analysis_provider_timeout_seconds,
    )


def _anchor_provider(settings: Settings) -> Any:
    """封面锚图通道（v6 §18.13）：复用故事板 grsai 配置；未配置返回 None。"""
    if settings.storyboard_provider_kind != "grsai":
        return None
    if (
        not settings.storyboard_provider_url.strip()
        or not settings.storyboard_provider_token.strip()
    ):
        return None
    return GrsaiCoverAnchorImageProvider(
        settings.storyboard_provider_url,
        settings.storyboard_provider_token,
        model=settings.cover_image_anchor_model,
    )


async def create_cover_supervised_runner(
    settings: Settings,
    *,
    work_dir: Path,
    task_queue: str = COVER_TASK_QUEUE,
    database: Database | None = None,
    temporal_client: Client | None = None,
    minio_client: Minio | None = None,
    activity_executor: Executor,
) -> SupervisedCoverRunner:
    database = database or Database(settings.database_url)
    repository = SqlAlchemyCoverRepository(database.unit_of_work)
    minio = minio_client or Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    storage = PrivateObjectStorage(minio, settings.minio_bucket)
    ids, clock = UuidFactory(), SystemClock()
    analysis = _analysis_provider(settings)
    prompt_provider = _prompt_provider(settings)
    image_provider = _image_provider(settings)
    review_provider = _review_provider(settings)
    anchor_provider = _anchor_provider(settings)
    style, prompts, images, characters = _cover_services(
        settings,
        repository,
        storage,
        analysis,
        prompt_provider,
        image_provider,
        review_provider,
        anchor_provider,
        ids,
        clock,
        work_dir,
    )
    client = temporal_client or await Client.connect(settings.temporal_target)
    dispatcher = CoverOutboxDispatcher(
        repository,
        TemporalCoverWorkflowStarter(client, task_queue),
        clock,
        ids,
    )
    closers: list[Any] = [
        analysis.close,
        prompt_provider.close,
        image_provider.close,
        review_provider.close,
    ]
    if anchor_provider is not None:
        closers.append(anchor_provider.close)
    try:
        return SupervisedCoverRunner(
            create_cover_worker(
                client,
                task_queue,
                CoverActivities(style, prompts, images, characters),
                activity_executor,
            ),
            dispatcher,
            closers=tuple(closers),
        )
    except BaseException:
        analysis.close()
        prompt_provider.close()
        image_provider.close()
        review_provider.close()
        if anchor_provider is not None:
            anchor_provider.close()
        raise


def _cover_services(
    settings: Settings,
    repository: SqlAlchemyCoverRepository,
    storage: PrivateObjectStorage,
    analysis: Any,
    prompt_provider: Any,
    image_provider: Any,
    review_provider: Any,
    anchor_provider: Any,
    ids: UuidFactory,
    clock: SystemClock,
    work_dir: Path,
) -> tuple[CoverStyleService, CoverPromptService, CoverImageService, CoverCharacterService]:
    style = CoverStyleService(
        repository, repository, repository, storage, analysis, ids, clock
    )
    prompts = CoverPromptService(
        repository,
        repository,
        prompt_provider,
        ids,
        clock,
        model=settings.cover_prompt_provider_model,
    )
    images = CoverImageService(
        repository,
        repository,
        image_provider,
        PillowCoverImageQc(),
        storage,
        ids,
        clock,
        work_dir,
        model=settings.cover_image_provider_model,
        size=settings.cover_image_provider_size,
        quality=settings.cover_image_provider_quality,
        review=review_provider,
        review_enabled=settings.cover_image_review_enabled,
        anchor_provider=anchor_provider,
        anchor_enabled=settings.cover_image_anchor_enabled,
        anchor_model=settings.cover_image_anchor_model,
    )
    characters = CoverCharacterService(repository, repository, storage, analysis, ids, clock)
    return style, prompts, images, characters


def create_cover_runtime_router(
    settings: Settings,
    current_account: Callable[..., Account],
    *,
    work_dir: Path,
    database: Database | None = None,
    minio_client: Minio | None = None,
    closers: list[Callable[[], None]] | None = None,
    media_account: Callable[..., Account] | None = None,
) -> APIRouter:
    """API 进程内的封面路由装配（照 storyboard_runtime 的路由工厂）。

    只装配同步意图/读取路径；outbox dispatcher 与 Temporal worker 的生命周期在
    worker 进程（`workflows/worker.py` 的 cover-temporal-worker / cover-outbox-relay）。
    """
    database = database or Database(settings.database_url)
    repository = SqlAlchemyCoverRepository(database.unit_of_work)
    minio = minio_client or Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    storage = PrivateObjectStorage(minio, settings.minio_bucket)
    analysis = _analysis_provider(settings)
    prompt_provider = _prompt_provider(settings)
    image_provider = _image_provider(settings)
    review_provider = _review_provider(settings)
    anchor_provider = _anchor_provider(settings)
    style, prompts, images, characters = _cover_services(
        settings,
        repository,
        storage,
        analysis,
        prompt_provider,
        image_provider,
        review_provider,
        anchor_provider,
        UuidFactory(),
        SystemClock(),
        work_dir,
    )
    if closers is not None:
        closers.extend(
            (analysis.close, prompt_provider.close, image_provider.close, review_provider.close)
        )
        if anchor_provider is not None:
            closers.append(anchor_provider.close)
    return create_cover_router(
        style, prompts, images, current_account, characters=characters, media_account=media_account
    )
