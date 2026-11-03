from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import sessionmaker
from starlette.types import Receive, Scope, Send

from tubeflow_test.adapters.deepseek_description import DeepSeekDescriptionProvider
from tubeflow_test.adapters.elevenlabs import ElevenLabsHttpClient, ElevenLabsQuotaAdapter
from tubeflow_test.adapters.persistence.elevenlabs import SqlElevenlabsKeyPool
from tubeflow_test.adapters.persistence.media_repository import production_active_job_gates
from tubeflow_test.adapters.persistence.narration import SqlStoryboardNarrationAuthority
from tubeflow_test.adapters.persistence.production_authority import (
    SqlCanonicalCompositionInputs,
    canonical_video_authorities,
    single_panel_policy_settings,
)
from tubeflow_test.adapters.persistence.shot_panel_assets import ShotPanelAssetService
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
)
from tubeflow_test.adapters.persistence.video_prompt_diagnostics import (
    SqlVideoPromptDiagnostics,
)
from tubeflow_test.api.cover_runtime import create_cover_runtime_router
from tubeflow_test.api.elevenlabs_pool import create_router as create_elevenlabs_pool_router
from tubeflow_test.api.media_runtime import create_media_runtime
from tubeflow_test.api.narration_router import create_router as create_narration_router
from tubeflow_test.api.narration_runtime import (
    NarrationRuntime,
    SqlProjectWriteGuard,
    create_narration_runtime,
)
from tubeflow_test.api.storyboard_runtime import create_storyboard_runtime_router
from tubeflow_test.api.video_batch import create_video_batch_router
from tubeflow_test.application.storyboard_batch import StoryboardBatchService
from tubeflow_test.application.video_batch import GroupVideoBatchService
from tubeflow_test.auto_pipeline.factory import build_auto_pipeline_service
from tubeflow_test.config import Settings
from tubeflow_test.cover_templates.api import create_cover_templates_router
from tubeflow_test.cover_templates.repository import SqlCoverTemplateRepository
from tubeflow_test.cover_templates.service import CoverTemplateService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.ports import ElevenlabsQuotaPort
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService
from tubeflow_test.identity_project.domain import Account
from tubeflow_test.identity_project.runtime import create_identity_project_runtime
from tubeflow_test.production_lifecycle import raise_lifecycle_errors
from tubeflow_test.video_descriptions.api import create_video_description_router
from tubeflow_test.video_descriptions.ports import DescriptionProvider
from tubeflow_test.video_descriptions.repository import SqlVideoDescriptionRepository
from tubeflow_test.video_descriptions.service import VideoDescriptionService
from tubeflow_test.video_descriptions.unconfigured import UnconfiguredDescriptionProvider
from tubeflow_test.video_generation.adapters import build_h3_archive_adapter
from tubeflow_test.video_generation.api import (
    VIDEO_CORS_HEADERS,
    ManualH3Writer,
    create_video_router,
)
from tubeflow_test.video_generation.api import router as video_router
from tubeflow_test.video_generation.availability import (
    H3Availability,
    H3ContractRequirements,
)
from tubeflow_test.video_generation.persistence import (
    ManualH3IntentWriter,
    SqlVideoBatchJobRepository,
    SqlVideoBatchRepository,
)
from tubeflow_test.video_generation.runtime import build_production_video_runtime
from tubeflow_test.video_prompt.compiler import build_video_prompt_compiler


class MediaRuntimeMount(Protocol):
    router: APIRouter
    startup: Callable[[], None]


@asynccontextmanager
async def _runtime_lifespan(
    _app: FastAPI,
    narration: object,
    media: MediaRuntimeMount | None,
    start_narration_relay: bool,
    closers: list[Callable[[], None]],
    identity_startup: Callable[[], None] | None = None,
) -> AsyncIterator[None]:
    primary: BaseException | None = None
    try:
        if identity_startup is not None:
            identity_startup()
        if media is not None:
            media.startup()
        start = getattr(narration, "start", None)
        if start_narration_relay and start is not None:
            await start()
        yield
    except BaseException as error:
        primary = error
    finally:
        errors: list[BaseException] = []
        try:
            close = getattr(narration, "close", None)
            if start_narration_relay and close is not None:
                await close()
            else:
                client = getattr(narration, "http_client", None)
                if client is not None:
                    await client.aclose()
        except BaseException as error:
            errors.append(error)
        for close_resource in reversed(closers):
            try:
                close_resource()
            except BaseException as error:
                errors.append(error)
        raise_lifecycle_errors("production API lifecycle failed", primary, errors)


class _DescriptionClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _description_provider(settings: Settings) -> DescriptionProvider:
    if not settings.video_prompt_provider_configured:
        return UnconfiguredDescriptionProvider()
    return DeepSeekDescriptionProvider(
        settings.video_prompt_provider_url,
        settings.video_prompt_provider_token.get_secret_value(),
        model=settings.video_prompt_provider_model,
        timeout_seconds=settings.video_prompt_provider_timeout_seconds,
    )


def _elevenlabs_pool_quota(
    settings: Settings, runtime: NarrationRuntime
) -> ElevenlabsQuotaPort:
    """号池管理 API 的 EL 配额端口。

    生产运行时（create_narration_runtime）在 `elevenlabs_quota` 上暴露与配音链路
    共享的适配器，号池必须复用同一实例（共享 per-key 并发信号量）；测试注入的
    runtime 替身可能不携带该字段，此时基于其 `http_client` 单独构造真实适配器——
    绝不回落到占位实现（2026-10-13「添加」返回 422 `elevenlabs_unreachable` 的
    根源即为占位装配）。
    """
    quota = cast(ElevenlabsQuotaPort | None, getattr(runtime, "elevenlabs_quota", None))
    if quota is not None:
        return quota
    client = getattr(runtime, "http_client", None)
    if client is None:
        raise RuntimeError(
            "ElevenLabs pool API requires a quota port or an http client on the runtime"
        )
    return ElevenLabsQuotaAdapter(
        ElevenLabsHttpClient(
            settings.elevenlabs_api_base,
            client=client,
            per_key_concurrency=settings.elevenlabs_per_key_concurrency,
        )
    )


def create_app(
    settings: Settings | None = None,
    *,
    narration_runtime: NarrationRuntime | None = None,
    media_runtime: MediaRuntimeMount | None = None,
    production_video_router: APIRouter | None = None,
    h3_writer: ManualH3IntentWriter | ManualH3Writer | None = None,
    current_account: Callable[[], Account] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    narration_runtime_was_injected = narration_runtime is not None
    resource_closers: list[Callable[[], None]] = []
    h3_writer_was_injected = h3_writer is not None
    video_batch_service: GroupVideoBatchService | None = None
    storyboard_batch_services: list[StoryboardBatchService] = []
    identity = create_identity_project_runtime(settings)
    if narration_runtime is None:
        if all(hasattr(identity, name) for name in ("database", "storage", "tokens")):
            narration_runtime = create_narration_runtime(
                settings,
                database=identity.database,
                storage=identity.storage,
                tokens=identity.tokens,
            )
        else:
            narration_runtime = create_narration_runtime(settings)
    @asynccontextmanager
    async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
        if video_batch_service is not None:
            for job_id in video_batch_service.unfinished_jobs():
                asyncio.create_task(
                    asyncio.to_thread(video_batch_service.process_all, job_id)
                )
        async with _runtime_lifespan(
            application,
            narration_runtime,
            media_runtime,
            narration_runtime_was_injected,
            resource_closers,
            getattr(identity, "startup", None),
        ):
            yield

    app = FastAPI(
        title="TubeFlow AI Test API",
        version="0.1.0",
        lifespan=_lifespan,
    )
    h3_contract_requirements = H3ContractRequirements.from_settings(settings)
    app.state.group_video_availability = H3Availability(None, h3_contract_requirements)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Confirm-Project-Title",
            "X-Confirm-Project-Title-B64",
            "X-Confirm-Channel",
            "X-Confirm-Channel-B64",
            "Idempotency-Key",
            *VIDEO_CORS_HEADERS,
        ],
    )
    app.include_router(
        create_narration_router(narration_runtime.service, narration_runtime.current_owner)
    )
    app.include_router(video_router)
    app.include_router(identity.router)
    identity_database = getattr(identity, "database", None)
    if identity_database is not None:
        app.include_router(
            create_elevenlabs_pool_router(
                ElevenlabsKeyPoolService(
                    SqlElevenlabsKeyPool(identity_database.session),
                    _elevenlabs_pool_quota(settings, narration_runtime),
                )
            )
        )
    database = getattr(narration_runtime, "database", None)
    if database is not None:
        if hasattr(database, "engine"):
            resource_closers.append(database.engine.dispose)
            app.state.group_video_availability = H3Availability(
                sessionmaker(database.engine, expire_on_commit=False),
                h3_contract_requirements,
            )
        narration_authority = SqlStoryboardNarrationAuthority(
            database.session, narration_runtime.storage
        )
        app.include_router(
            create_storyboard_runtime_router(
                settings,
                narration_runtime.current_owner,
                media_account=getattr(
                    narration_runtime, "media_account", narration_runtime.current_owner
                ),
                work_dir=Path("var/storyboards"),
                narration_authority=narration_authority,
                database=database,
                minio_client=(
                    narration_runtime.storage.client
                    if hasattr(narration_runtime, "storage")
                    else None
                ),
                closers=resource_closers,
                batch_out=storyboard_batch_services,
            )
        )
        app.include_router(
            create_cover_runtime_router(
                settings,
                narration_runtime.current_owner,
                media_account=getattr(
                    narration_runtime, "media_account", narration_runtime.current_owner
                ),
                work_dir=Path("var/covers"),
                database=database,
                minio_client=(
                    narration_runtime.storage.client
                    if hasattr(narration_runtime, "storage")
                    else None
                ),
                closers=resource_closers,
            )
        )
        # 临时功能（2026-10-21）：封面页顶部频道级模板，独立于 cover_* 域。
        # 测试替身可能只提供 session/engine，没有 unit_of_work 时不装配该路由。
        if hasattr(database, "unit_of_work"):
            app.include_router(
                create_cover_templates_router(
                    CoverTemplateService(SqlCoverTemplateRepository(database.unit_of_work)),
                    narration_runtime.current_owner,
                    media_account=getattr(
                        narration_runtime, "media_account", narration_runtime.current_owner
                    ),
                )
            )
            description_storage = getattr(narration_runtime, "storage", None)
            if description_storage is not None:
                description_provider = _description_provider(settings)
                resource_closers.append(description_provider.close)
                app.include_router(
                    create_video_description_router(
                        VideoDescriptionService(
                            SqlVideoDescriptionRepository(
                                database.unit_of_work, description_storage
                            ),
                            description_provider,
                            _DescriptionClock(),
                            model=settings.video_prompt_provider_model,
                            provider_timeout_seconds=settings.video_prompt_provider_timeout_seconds,
                        ),
                        narration_runtime.current_owner,
                    )
                )
        if (
            settings.h3_configured
            and not h3_writer_was_injected
            and production_video_router is None
        ):
            sessions = sessionmaker(database.engine, expire_on_commit=False)
            video_asset_store = build_h3_archive_adapter(sessions, narration_runtime.storage)
            requested_policy, policy_allowed = single_panel_policy_settings(settings)
            narration_video_authority, storyboard_video_authority, loader = (
                canonical_video_authorities(
                    settings,
                    reference_policy=requested_policy,
                    single_panel_allowed=policy_allowed,
                    panel_derivatives=ShotPanelAssetService(
                        sessions, narration_runtime.storage, video_asset_store
                    ),
                )
            )
            video = build_production_video_runtime(
                settings,
                narration_authority=narration_video_authority,
                storyboard_authority=storyboard_video_authority,
                authenticated_owner=narration_runtime.current_owner_id,
                write_guard=SqlProjectWriteGuard(database.session).assert_writable,
                database_engine=database.engine,
                minio=narration_runtime.storage.client,
                asset_store=video_asset_store,
                refresh_health=False,
                reference_policy_authority=loader,
            )
            production_video_router = video.router
            resource_closers.append(video.close)
            if hasattr(database, "unit_of_work"):
                storyboard_repository = SqlAlchemyStoryboardRepository(
                    database.unit_of_work, narration_authority
                )
                video_prompt_compiler = build_video_prompt_compiler(
                    settings,
                    SqlVideoPromptDiagnostics(database.unit_of_work).record,
                )
                if video_prompt_compiler is not None:
                    resource_closers.append(video_prompt_compiler.close)
                h3_writer = ManualH3IntentWriter(
                    database.unit_of_work,
                    storyboard_repository,
                    loader,
                    prompt_compiler=video_prompt_compiler,
                    intent_group_loader=loader,
                )
                video_batch_service = GroupVideoBatchService(
                    SqlVideoBatchRepository(sessions),
                    h3_writer,
                    uuid4,
                    SqlVideoBatchJobRepository(sessions),
                )
                current_account = narration_runtime.current_owner
        if media_runtime is None and hasattr(database, "engine"):
            sessions = sessionmaker(database.engine, expire_on_commit=False)
            media_runtime = cast(
                MediaRuntimeMount,
                create_media_runtime(
                    settings,
                    narration_runtime.current_media_owner,
                    SqlCanonicalCompositionInputs(sessions),
                    production_active_job_gates(),
                    private_root=Path("var/media"),
                    database=database,
                    storage=narration_runtime.storage,
                    media_owner=getattr(
                        narration_runtime,
                        "cookie_media_owner",
                        narration_runtime.current_media_owner,
                    ),
                ),
            )
    if database is not None and hasattr(database, "engine"):
        app.state.auto_pipeline_service = build_auto_pipeline_service(
            database=database,
            narration_service=narration_runtime.service,
            project_director=getattr(narration_runtime, "project_director", None),
            storyboards=(storyboard_batch_services[0] if storyboard_batch_services else None),
            videos=video_batch_service,
            composition_repository=getattr(media_runtime, "composition_repository", None),
            composition_outbox=getattr(media_runtime, "composition_outbox", None),
        )
    app.state.identity_project_runtime = identity
    app.state.narration_runtime = narration_runtime

    if media_runtime is not None:
        app.include_router(media_runtime.router)
    if production_video_router is not None:
        app.include_router(production_video_router)
    if (
        h3_writer is not None
        and current_account is not None
        and (settings.h3_configured or h3_writer_was_injected)
    ):
        app.include_router(create_video_router(h3_writer, current_account))
        if video_batch_service is not None:
            app.include_router(
                create_video_batch_router(
                    video_batch_service,
                    current_account,
                    kick=video_batch_service.process_all,
                )
            )
    elif h3_writer is not None and current_account is not None:
        unavailable = APIRouter(prefix="/group-videos", tags=["group-videos"])

        @unavailable.post("/groups/{group_id}/generations", status_code=503)
        async def h3_unavailable(group_id: str) -> JSONResponse:
            del group_id
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "h3_not_configured",
                        "message": (
                            "Manual H3 generation is unavailable until nodes are configured."
                        ),
                    }
                },
            )

        app.include_router(unavailable)

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, error: DomainError) -> JSONResponse:
        status_code = {"not_found": 404, "forbidden": 403, "invalid_session": 401}.get(
            error.code, 409 if error.code.endswith("locked") else 422
        )
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    frontend_dir = settings.frontend_dir
    if frontend_dir is not None and (frontend_dir / "index.html").is_file():
        api_roots = {
            "api",
            "identity",
            "projects",
            "narration",
            "storyboards",
            "storyboard-groups",
            "group-videos",
            "elevenlabs-pool",
            "health",
            "docs",
            "redoc",
            "openapi.json",
        }

        @app.get("/{spa_path:path}", include_in_schema=False)
        async def frontend(spa_path: str) -> FileResponse:
            root = spa_path.split("/", 1)[0]
            if root in api_roots:
                raise DomainError("not_found", "Resource not found")
            # 静态资源不加指纹文件名，必须强制 revalidate，防止部署后新旧模块混载。
            headers = {"Cache-Control": "no-cache"}
            candidate = (frontend_dir / spa_path).resolve()
            if candidate.is_relative_to(frontend_dir.resolve()) and candidate.is_file():
                return FileResponse(candidate, headers=headers)
            return FileResponse(frontend_dir / "index.html", headers=headers)

    return app


_runtime_app: FastAPI | None = None


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    """Uvicorn ASGI target; construction occurs on lifespan startup.

    This keeps module import side-effect free while still failing container startup
    closed when the database/schema or pre-initialization credentials are absent.
    """
    global _runtime_app
    if _runtime_app is None:
        _runtime_app = create_app()
    await _runtime_app(scope, receive, send)


def run() -> None:
    import uvicorn

    uvicorn.run("tubeflow_test.api.app:app", host="0.0.0.0", port=8000, reload=False)
