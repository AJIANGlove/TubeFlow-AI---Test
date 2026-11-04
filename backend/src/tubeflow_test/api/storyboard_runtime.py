from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from minio import Minio
from temporalio.client import Client

from tubeflow_test.adapters.director_model import OpenAiCompatibleDirectorModel
from tubeflow_test.adapters.grsai_grid_sheet import GrsaiGridSheetImageProvider
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.storyboards.asset_catalog import SqlAlchemyAssetCatalog
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
)
from tubeflow_test.adapters.pix_grid_sheet import PixGridSheetImageProvider
from tubeflow_test.adapters.storyboard_image_qc import PillowStoryboardTechnicalQc
from tubeflow_test.adapters.storyboard_object_storage import MinioStoryboardObjectStorage
from tubeflow_test.adapters.storyboard_provider import HttpGridSheetImageProvider
from tubeflow_test.application.director_generation import DirectorGenerationService
from tubeflow_test.application.narration_authority import NarrationAuthorityPort
from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.application.storyboard_batch import StoryboardBatchService
from tubeflow_test.application.storyboard_dispatcher import StoryboardOutboxDispatcher
from tubeflow_test.application.storyboard_failover import (
    DEFAULT_STORYBOARD_FAILOVER_POLICY,
    StoryboardFailoverPolicy,
    StoryboardFailoverStep,
)
from tubeflow_test.application.storyboard_recovery import StoryboardRecoverySupervisor
from tubeflow_test.application.storyboards import (
    DirectorPlanService,
    GridSheetImageProvider,
    StoryboardGenerationService,
    StoryboardRecoveryCandidate,
)
from tubeflow_test.config import Settings
from tubeflow_test.identity_project.domain import Account
from tubeflow_test.video_generation.panel_images import check_storyboard_panel_layout
from tubeflow_test.workflows.storyboard_runner import SupervisedStoryboardRunner
from tubeflow_test.workflows.storyboards import (
    StoryboardActivities,
    TemporalStoryboardRecoveryProbe,
    TemporalStoryboardWorkflowStarter,
    create_storyboard_worker,
)

from .project_director import create_project_director_router
from .storyboard_batch import create_storyboard_batch_router
from .storyboards import create_storyboard_router

STORYBOARD_REQUIRED_CORS_METHODS = ("GET", "POST", "PUT")
STORYBOARD_REQUIRED_CORS_HEADERS = ("Authorization", "Content-Type", "Idempotency-Key")


def _grid_sheet_providers(
    settings: Settings,
) -> tuple[dict[str, GridSheetImageProvider], StoryboardFailoverPolicy]:
    if settings.storyboard_provider_kind == "grsai":
        providers: dict[str, GridSheetImageProvider] = {
            "grsai": GrsaiGridSheetImageProvider(
                settings.storyboard_provider_url,
                settings.storyboard_provider_token,
                settings.storyboard_provider_timeout_seconds,
                max_concurrency=settings.storyboard_provider_max_concurrency,
            ),
            "pix": PixGridSheetImageProvider(
                settings.storyboard_pix_provider_url,
                settings.storyboard_pix_provider_token.get_secret_value(),
                settings.storyboard_pix_provider_timeout_seconds,
                max_concurrency=settings.storyboard_provider_max_concurrency,
            ),
        }
        return providers, DEFAULT_STORYBOARD_FAILOVER_POLICY
    if settings.storyboard_provider_kind == "contract":
        provider = HttpGridSheetImageProvider(
            settings.storyboard_provider_url,
            settings.storyboard_provider_token,
            settings.storyboard_provider_timeout_seconds,
        )
        return {"contract": provider}, StoryboardFailoverPolicy(
            steps=(
                StoryboardFailoverStep(
                    "contract",
                    settings.storyboard_provider_model,
                    {"quality": settings.storyboard_provider_quality},
                ),
            )
        )
    raise ValueError(f"unsupported storyboard provider kind: {settings.storyboard_provider_kind}")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidFactory:
    def new(self) -> UUID:
        return uuid4()


def create_storyboard_runtime_router(
    settings: Settings,
    current_account: Callable[..., Account],
    *,
    work_dir: Path,
    narration_authority: NarrationAuthorityPort,
    database: Database | None = None,
    minio_client: Minio | None = None,
    closers: list[Callable[[], None]] | None = None,
    media_account: Callable[..., Account] | None = None,
    batch_out: list[StoryboardBatchService] | None = None,
) -> APIRouter:
    """Construct every production storyboard dependency without central app mutation."""
    if not callable(current_account):

        def current_account_dependency() -> Account:
            raise HTTPException(
                401, detail={"code": "invalid_session", "message": "Session is invalid"}
            )

        current_account = current_account_dependency
    database = database or Database(settings.database_url)
    repository = SqlAlchemyStoryboardRepository(
        database.unit_of_work,
        narration_authority,
    )
    project_director_repository = SqlAlchemyProjectDirectorRepository(database.unit_of_work)
    providers, failover_policy = _grid_sheet_providers(settings)
    minio = minio_client or Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    storage = MinioStoryboardObjectStorage(
        minio, settings.minio_bucket, SqlAlchemyAssetCatalog(database.unit_of_work)
    )
    ids, clock = UuidFactory(), SystemClock()
    director = DirectorPlanService(repository, ids, clock)
    director_model = OpenAiCompatibleDirectorModel(
        settings.director_provider_url,
        settings.director_provider_token.get_secret_value(),
        settings.director_provider_timeout_seconds,
    )
    project_director = ProjectDirectorService(
        project_director_repository,
        director_model,
        ids,
        clock,
        model_name=settings.director_provider_model,
        parameters={"temperature": 0.0},
    )
    generations = StoryboardGenerationService(
        repository,
        providers,
        storage,
        PillowStoryboardTechnicalQc(),
        ids,
        clock,
        work_dir,
        failover_policy,
        single_panel_new_projects=settings.storyboard_single_panel_new_projects,
        panel_layout_check=check_storyboard_panel_layout,
    )
    batch = StoryboardBatchService(repository, director, generations, project_director, ids)
    if batch_out is not None:
        batch_out.append(batch)
    if closers is not None:
        closers.append(director_model.close)
        closers.extend(provider.close for provider in providers.values())

    router = APIRouter()
    router.include_router(
        create_storyboard_router(
            director,
            generations,
            current_account,
            media_account=media_account,
        )
    )
    router.include_router(
        create_project_director_router(
            project_director,
            current_account,
            provider_configured=settings.director_provider_configured,
        )
    )
    router.include_router(create_storyboard_batch_router(batch, current_account))
    return router


async def create_storyboard_supervised_runner(
    settings: Settings,
    *,
    work_dir: Path,
    narration_authority: NarrationAuthorityPort,
    task_queue: str = "storyboards",
    database: Database | None = None,
    temporal_client: Client | None = None,
    minio_client: Minio | None = None,
    activity_executor: Executor,
    auto_kick: Callable[[UUID, UUID], None] | None = None,
    recovery_inconclusive: Callable[[StoryboardRecoveryCandidate], None] | None = None,
) -> SupervisedStoryboardRunner:
    database = database or Database(settings.database_url)
    repository = SqlAlchemyStoryboardRepository(
        database.unit_of_work,
        narration_authority,
    )
    project_director_repository = SqlAlchemyProjectDirectorRepository(database.unit_of_work)
    minio = minio_client or Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    storage = MinioStoryboardObjectStorage(
        minio, settings.minio_bucket, SqlAlchemyAssetCatalog(database.unit_of_work)
    )
    ids, clock = UuidFactory(), SystemClock()
    plans = DirectorPlanService(repository, ids, clock)
    director_model = OpenAiCompatibleDirectorModel(
        settings.director_provider_url,
        settings.director_provider_token.get_secret_value(),
        settings.director_provider_timeout_seconds,
    )
    director = DirectorGenerationService(
        repository,
        repository,
        plans,
        director_model,
        ids,
        clock,
        model_name=settings.director_provider_model,
        parameters={"temperature": 0.2},
    )
    project_director = ProjectDirectorService(
        project_director_repository,
        director_model,
        ids,
        clock,
        model_name=settings.director_provider_model,
        parameters={"temperature": 0.0},
    )
    try:
        providers, failover_policy = _grid_sheet_providers(settings)
    except BaseException:
        director_model.close()
        raise
    grids = StoryboardGenerationService(
        repository,
        providers,
        storage,
        PillowStoryboardTechnicalQc(),
        ids,
        clock,
        work_dir,
        failover_policy,
        single_panel_new_projects=settings.storyboard_single_panel_new_projects,
        panel_layout_check=check_storyboard_panel_layout,
    )
    client = temporal_client or await Client.connect(settings.temporal_target)
    activities = StoryboardActivities(director, grids, project_director, auto_kick=auto_kick)
    batch = StoryboardBatchService(repository, plans, grids, project_director, ids)
    dispatcher = StoryboardOutboxDispatcher(
        repository,
        TemporalStoryboardWorkflowStarter(
            client, task_queue, parallelism=settings.director_parallelism
        ),
        clock,
        ids,
    )
    try:
        return SupervisedStoryboardRunner(
            create_storyboard_worker(client, task_queue, activities, activity_executor),
            dispatcher,
            closers=(
                director_model.close,
                *tuple(provider.close for provider in providers.values()),
            ),
            batch=batch,
            recovery_sweeper=StoryboardRecoverySupervisor(
                repository,
                grids,
                TemporalStoryboardRecoveryProbe(client),
                datetime.now(UTC),
                recovery_inconclusive,
            ),
        )
    except BaseException:
        for provider in providers.values():
            provider.close()
        director_model.close()
        raise
