from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import APIRouter
from minio import Minio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from tubeflow_test.adapters.elevenlabs import ElevenLabsHttpClient, ElevenLabsQuotaAdapter
from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.channel_deletion import SqlAlchemyChannelDeletionRepository
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.elevenlabs import SqlElevenlabsKeyPool
from tubeflow_test.adapters.persistence.identity_project import (
    RequestStagedStorage,
    SqlAlchemyIdentityProjectRepository,
    persist_public_voice_catalog,
)
from tubeflow_test.adapters.public_voice_catalog import (
    FIXED_CATALOG_SOURCE_SHA256,
    FIXED_CATALOG_VOICE_COUNT,
    import_public_voice_catalog,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.voice_catalog import PublicVoiceCatalog
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService

from .adapters import Argon2PasswordHasher, OpaqueSessionTokens
from .channel_deletion import ChannelDeletionService
from .cloned_voice_service import ClonedVoiceService
from .domain import Channel, ClonedVoice, ClonedVoiceAccount, ClonedVoiceUsage
from .router import IdentityProjectDependencies, create_dependencies, create_router
from .service import IdentityProjectService


@dataclass(slots=True)
class IdentityProjectRuntime:
    settings: Settings
    database: Database
    storage: PrivateObjectStorage
    passwords: Argon2PasswordHasher
    tokens: OpaqueSessionTokens
    dependencies: IdentityProjectDependencies = field(init=False)
    router: APIRouter

    def service_uow(self) -> Iterator[IdentityProjectService]:
        staged = RequestStagedStorage(self.database, self.storage)
        with self.database.unit_of_work() as session:
            yield IdentityProjectService(
                SqlAlchemyIdentityProjectRepository(session),
                self.passwords,
                setup_username=self.settings.identity_setup_username,
                setup_password=self.settings.identity_setup_password,
                image_storage=staged,
                opaque_tokens=self.tokens,
            )
        with suppress(Exception):
            staged.finalize()

    def import_voice_catalog(
        self, path: Path, *, catalog: PublicVoiceCatalog | None = None
    ) -> tuple[UUID, ...]:
        catalog = catalog or import_public_voice_catalog(
            path, ffmpeg_executable=self.settings.ffmpeg_executable
        )
        created: list[UUID] = []
        while True:
            staged = RequestStagedStorage(
                self.database,
                self.storage,
                ownership_scope="GLOBAL",
                reclaim_matching_unattached=True,
            )
            with self.database.unit_of_work() as session:
                repository = SqlAlchemyIdentityProjectRepository(session)
                created.extend(
                    persist_public_voice_catalog(
                        path,
                        repository,
                        staged,
                        catalog=catalog,
                        max_new_voices=1,
                    )
                )
                progress = repository.catalog_import_progress(catalog.source_sha256)
            with suppress(Exception):
                staged.finalize()
            if progress is not None and progress[1] == "completed":
                return tuple(created)

    def ensure_private_bucket(self) -> None:
        self.storage.ensure_private_bucket()

    def initialize_fixed_voice_catalog(self) -> tuple[UUID, ...]:
        path = self.settings.public_voice_catalog_path
        if path is None:
            raise DomainError(
                "fixed_voice_catalog_not_configured",
                "The approved fixed public voice catalog must be configured.",
            )
        catalog = import_public_voice_catalog(
            path, ffmpeg_executable=self.settings.ffmpeg_executable
        )
        if (
            catalog.source_sha256 != FIXED_CATALOG_SOURCE_SHA256
            or len(catalog.voices) != FIXED_CATALOG_VOICE_COUNT
        ):
            raise DomainError(
                "fixed_voice_catalog_mismatch",
                "The configured public voice catalog is not the approved fixed catalog.",
            )
        return self.import_voice_catalog(path, catalog=catalog)

    def startup(self) -> None:
        self.ensure_private_bucket()
        self.initialize_fixed_voice_catalog()

    def worker_voice_reference(self, voice_version_id: UUID) -> tuple[bytes, str]:
        """Trusted worker-only access; never exposed through the account HTTP API."""
        with self.database.unit_of_work() as session:
            repository = SqlAlchemyIdentityProjectRepository(session)
            media = repository.voice_media(voice_version_id, "reference")
            voice = repository.voice_version(voice_version_id)
            if media is None or voice is None or not voice.enabled:
                raise DomainError("not_found", "Resource not found")
            return self.storage.get_verified(media[0], media[2], media[3]), media[1]


class SessionScopedClonedVoiceRepository:
    """克隆音色管理（§23.3-§23.5）的每调用一会话仓储视图。

    克隆流程跨越 EL 外呼等待（可能数十秒），不得持有请求级会话/事务；与配音路径的
    `SessionScopedProjectRepository` 同一模式：每次调用开新会话，写操作显式提交、
    读操作即刻关闭，返回值均为纯领域对象（不携带 ORM 状态）。
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def channel(self, owner_id: UUID, channel_id: UUID) -> Channel | None:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).channel(owner_id, channel_id)

    def list_cloned_voices(self, channel_id: UUID) -> tuple[ClonedVoice, ...]:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).list_cloned_voices(channel_id)

    def cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> ClonedVoice | None:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).cloned_voice(
                channel_id, cloned_voice_id
            )

    def cloned_voice_by_id(self, cloned_voice_id: UUID) -> ClonedVoice | None:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).cloned_voice_by_id(cloned_voice_id)

    def list_cloned_voice_channels(self) -> tuple[UUID, ...]:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).list_cloned_voice_channels()

    def add_cloned_voice(self, voice: ClonedVoice) -> None:
        with self._session_factory() as session, session.begin():
            SqlAlchemyIdentityProjectRepository(session).add_cloned_voice(voice)

    def delete_cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> bool:
        with self._session_factory() as session, session.begin():
            return SqlAlchemyIdentityProjectRepository(session).delete_cloned_voice(
                channel_id, cloned_voice_id
            )

    def cloned_voice_usage(self, cloned_voice_id: UUID) -> ClonedVoiceUsage:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).cloned_voice_usage(cloned_voice_id)

    def cloned_voice_account(
        self, cloned_voice_id: UUID, pool_key_id: UUID
    ) -> ClonedVoiceAccount | None:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).cloned_voice_account(
                cloned_voice_id, pool_key_id
            )

    def list_cloned_voice_accounts(self, cloned_voice_id: UUID) -> tuple[ClonedVoiceAccount, ...]:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).list_cloned_voice_accounts(
                cloned_voice_id
            )

    def save_cloned_voice_account(self, account: ClonedVoiceAccount) -> None:
        with self._session_factory() as session, session.begin():
            SqlAlchemyIdentityProjectRepository(session).save_cloned_voice_account(account)

    def forget_cloned_voice_account(self, cloned_voice_id: UUID, pool_key_id: UUID) -> None:
        with self._session_factory() as session, session.begin():
            SqlAlchemyIdentityProjectRepository(session).forget_cloned_voice_account(
                cloned_voice_id, pool_key_id
            )


def _build_cloned_voice_service(
    settings: Settings, database: Database, storage: PrivateObjectStorage
) -> ClonedVoiceService:
    """克隆音色管理（§23.3）的生产装配。

    2026-10-13 缺陷：`create_identity_project_runtime` 曾不向 `create_router` 传
    `cloned_voice_service`，克隆路由依赖在 None 时直接 503 `cloned_voice_unavailable`
    （与号池占位同类：功能已实现但未接线）。EL 外呼与配音链路同规格（§23.9 代理、
    per-key 并发信号量），管理操作低频，与配音链路的适配器实例并存可接受。
    """
    proxy = settings.elevenlabs_proxy.strip()
    if proxy:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0),
            follow_redirects=False,
            trust_env=False,
            proxy=proxy,
        )
    else:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0),
            follow_redirects=False,
            trust_env=False,
        )
    http = ElevenLabsHttpClient(
        settings.elevenlabs_api_base,
        client=client,
        per_key_concurrency=settings.elevenlabs_per_key_concurrency,
    )
    pool = ElevenlabsKeyPoolService(
        SqlElevenlabsKeyPool(database.session), ElevenLabsQuotaAdapter(http)
    )
    return ClonedVoiceService(
        SessionScopedClonedVoiceRepository(database.session),
        storage,
        http,
        pool,
    )


def create_identity_project_runtime(
    settings: Settings,
    *,
    database: Database | None = None,
    storage: PrivateObjectStorage | None = None,
    tokens: OpaqueSessionTokens | None = None,
) -> IdentityProjectRuntime:
    database = database or Database(settings.database_url)
    username = settings.identity_setup_username
    password = settings.identity_setup_password
    placeholders = {
        "replace-with-one-time-admin-username",
        "replace-with-a-strong-one-time-password",
    }
    if username in placeholders or password in placeholders:
        raise ValueError("identity setup credentials must be explicitly configured")
    valid_credentials = bool(
        username and username == username.strip() and password and len(password) >= 8
    )
    if not valid_credentials:
        try:
            with database.unit_of_work() as session:
                setup_complete = SqlAlchemyIdentityProjectRepository(session).setup_complete()
        except SQLAlchemyError as error:
            raise ValueError(
                "identity setup credentials must be explicitly configured until setup is complete"
            ) from error
        if not setup_complete:
            raise ValueError("identity setup credentials must be explicitly configured")
    storage = storage or PrivateObjectStorage(
        Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        ),
        settings.minio_bucket,
    )
    passwords = Argon2PasswordHasher()
    tokens = tokens or OpaqueSessionTokens()
    runtime = IdentityProjectRuntime(
        settings=settings,
        database=database,
        storage=storage,
        passwords=passwords,
        tokens=tokens,
        router=APIRouter(),
    )
    runtime.dependencies = create_dependencies(
        runtime.service_uow, cookie_secure=settings.cookie_secure
    )
    # 克隆管理装配（2026-10-13）：惰性构造并在请求间共享同一实例；构造本身无网络
    # 副作用（EL 适配器只在真实调用时外呼），因此对未使用克隆功能的进程零开销。
    cloned_voice_services: dict[str, ClonedVoiceService] = {}

    def cloned_voice_service() -> ClonedVoiceService:
        service = cloned_voice_services.get("service")
        if service is None:
            service = _build_cloned_voice_service(settings, database, storage)
            cloned_voice_services["service"] = service
        return service

    runtime.router = create_router(
        runtime.service_uow,
        runtime.dependencies,
        cookie_secure=settings.cookie_secure,
        cloned_voice_service=cloned_voice_service,
        channel_deletion_service=lambda: ChannelDeletionService(
            SqlAlchemyChannelDeletionRepository(database.unit_of_work),
            storage,
            cloned_voice_service(),
        ),
    )
    return runtime
