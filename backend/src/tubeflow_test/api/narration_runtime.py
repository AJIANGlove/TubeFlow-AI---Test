from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import Cookie, Header
from minio import Minio
from sqlalchemy.orm import Session
from temporalio.client import Client

from tubeflow_test.adapters.audio_qc import FfmpegAudioNormalizer, FfprobeAudioInspector
from tubeflow_test.adapters.audio_transcode import FfmpegRawWavTranscoder
from tubeflow_test.adapters.cosyvoice import CosyVoiceHttpAdapter
from tubeflow_test.adapters.deepseek_sentence_split import DeepSeekSentenceSplitProvider
from tubeflow_test.adapters.elevenlabs import ElevenLabsHttpClient, ElevenLabsQuotaAdapter
from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.auto_pipeline import create_active_run_in_session
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.elevenlabs import SqlElevenlabsKeyPool
from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.adapters.persistence.narration import (
    NarrationProjectRecord,
    PrivateNarrationAudioArchive,
    SqlNarrationRepository,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
)
from tubeflow_test.api.media_router import AuthenticatedOwner
from tubeflow_test.application.elevenlabs_narration import ElevenLabsNarrationProvider
from tubeflow_test.application.narration import (
    PendingNarrationDispatcher,
    ProjectNarrationService,
    SubmitSentenceNarration,
)
from tubeflow_test.application.narration_ports import NarrationJob, VoiceReference
from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.auto_pipeline.domain import new_auto_pipeline_run
from tubeflow_test.config import Settings
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import is_nonvisual_script_separator, segment_pasted_script
from tubeflow_test.domain.project_director import ProjectDirectorTrigger
from tubeflow_test.elevenlabs.ports import ElevenlabsQuotaPort
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService
from tubeflow_test.identity_project.adapters import Argon2PasswordHasher, OpaqueSessionTokens
from tubeflow_test.identity_project.domain import (
    Account,
    AccountRole,
    ClonedVoice,
    ClonedVoiceAccount,
    TtsProvider,
)
from tubeflow_test.identity_project.media_session import (
    MEDIA_SESSION_COOKIE,
    authenticate_media_account,
)
from tubeflow_test.identity_project.service import IdentityProjectService
from tubeflow_test.sentence_split.domain import (
    SentenceSplitPlan,
    apply_sentence_split_plan,
    sentence_split_plan_from_json,
    sentence_split_plan_to_json,
)
from tubeflow_test.sentence_split.service import SentenceSplitService
from tubeflow_test.workflows.narration import TemporalNarrationStarter


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _UuidFactory:
    def new(self) -> UUID:
        return uuid4()


class _IntentOnlyDirectorModel:
    def generate(self, _unit: object) -> object:
        raise AssertionError("Provider calls cannot run inside narration confirmation")


class SqlProjectSnapshotAdapter:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        director_model_name: str | None = None,
        director_parameters: dict[str, str | int | float | bool] | None = None,
        director_provider_configured: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._director_model_name = director_model_name
        self._director_parameters = dict(director_parameters or {})
        self._director_provider_configured = director_provider_configured

    def _session(self) -> Session:
        return self._session_factory()

    def get_draft_script(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, SupportedLocale, UUID | None]:
        with self._session() as session:
            project = SqlAlchemyIdentityProjectRepository(session).project(owner_id, project_id)
            if project is None:
                raise DomainError("not_found", "Resource not found.")
            return project.script, project.locale, project.voice_version_id

    def confirm_and_get_script(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, SupportedLocale, UUID | None]:
        with self._session() as session, session.begin():
            repository = SqlAlchemyIdentityProjectRepository(session)
            project = repository.project_for_write(owner_id, project_id)
            if project is None:
                raise DomainError("not_found", "Resource not found.")
            if project.snapshot is None:
                confirmed = IdentityProjectService(
                    repository, Argon2PasswordHasher()
                ).confirm_narration(
                    Account(owner_id, "narration-owner", "", AccountRole.USER),
                    project_id,
                    project.draft_revision,
                    datetime.now(UTC),
                )
            else:
                confirmed = project
            if confirmed.snapshot is None:
                raise RuntimeError("project confirmation did not create a snapshot")
            return (
                confirmed.snapshot.script,
                confirmed.snapshot.locale,
                confirmed.snapshot.voice_version_id,
            )

    def confirm_and_enqueue(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        sentence_splits: SentenceSplitPlan | None = None,
    ) -> tuple[str, SupportedLocale, UUID | None, NarrationJob]:
        with self._session() as session, session.begin():
            locked = (
                session.query(ProjectRow)
                .filter_by(id=project_id, owner_id=owner_id)
                .with_for_update()
                .one_or_none()
            )
            if locked is None:
                raise DomainError("not_found", "Resource not found.")
            if locked.production_state != "ACTIVE":
                raise DomainError("project_purging", "Project is being permanently deleted.")
            repository = SqlAlchemyIdentityProjectRepository(session)
            project = repository.project(owner_id, project_id)
            if project is None:
                raise DomainError("not_found", "Resource not found.")
            row = session.get(NarrationProjectRecord, project_id)
            if row is not None and row.owner_id != owner_id:
                raise DomainError("not_found", "Resource not found.")
            current_payload = dict(row.payload) if row is not None else {}
            existing_job = current_payload.get("job")
            if project.snapshot is not None and isinstance(existing_job, dict):
                job = NarrationJob(
                    str(existing_job["job_id"]),
                    owner_id,
                    project_id,
                    tuple(int(value) for value in existing_job["sentence_sequences"]),
                    str(existing_job["state"]),
                    tuple(int(value) for value in existing_job.get("failure_sequences", [])),
                    not_before=(
                        str(existing_job["not_before"])
                        if existing_job.get("not_before")
                        else None
                    ),
                )
                return project.script, project.locale, project.voice_version_id, job
            if self._director_provider_configured is False:
                raise DomainError(
                    "director_provider_not_configured",
                    "Director Provider must be configured before narration confirmation.",
                )
            persisted_splits = sentence_split_plan_from_json(
                current_payload.get("sentence_splits")
            )
            if persisted_splits is None:
                if locked.production_mode == "AUTO" and sentence_splits is None:
                    # §15.7：AUTO 项目冻结前必须经过拆句 agent（无风险句时为空计划）。
                    raise DomainError(
                        "sentence_split_required",
                        "AUTO projects require the sentence-split agent before freezing.",
                    )
                split_plan = sentence_splits
            else:
                # 重放/补建 job 时使用冻结时的拆分血缘，不应用新计划。
                split_plan = persisted_splits
            script = segment_pasted_script(project.script, project.locale)
            if split_plan is not None and split_plan.splits:
                script = apply_sentence_split_plan(script, split_plan)
            # §23.1：快照哈希加入配音源判别与对应音色身份；cosyvoice 分支保持原样。
            hash_payload: dict[str, object] = {
                "script_sha256": script.source_sha256,
                "voice_version_id": (
                    str(project.voice_version_id)
                    if project.voice_version_id is not None
                    else None
                ),
                "visual_style": project.visual_style,
                "character_image_ids": [
                    str(value) for value in project.character_image_ids
                ],
                "locale": project.locale.value,
                "aspect_ratio": project.aspect_ratio.value,
                "channel_id": str(project.channel_id),
            }
            if project.tts_provider == TtsProvider.ELEVENLABS.value:
                hash_payload["tts_provider"] = project.tts_provider
                hash_payload["cloned_voice_id"] = str(project.cloned_voice_id)
            snapshot_hash = hashlib.sha256(
                json.dumps(
                    hash_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            job = NarrationJob(
                f"narration_{snapshot_hash}",
                owner_id,
                project_id,
                tuple(
                    item.sequence
                    for item in script.sentences
                    if not is_nonvisual_script_separator(item.text)
                ),
                "PENDING",
            )
            confirmed = IdentityProjectService(
                repository, Argon2PasswordHasher()
            ).confirm_narration(
                Account(owner_id, "narration-owner", "", AccountRole.USER),
                project_id,
                project.draft_revision,
                datetime.now(UTC),
            )
            if confirmed.snapshot is None:
                raise RuntimeError("project confirmation did not create a snapshot")
            if locked.production_mode == "AUTO":
                # §15.1：一次「提交」授权全链路自动执行；同事务创建 RUNNING 运行，重入幂等。
                create_active_run_in_session(
                    session,
                    new_auto_pipeline_run(uuid4(), owner_id, project_id, datetime.now(UTC)),
                )
            encoded_script = {
                "language": script.language.value,
                "rule_version": script.rule_version,
                "source_sha256": script.source_sha256,
                "sentences": [
                    {
                        "sequence": item.sequence,
                        "text": item.text,
                        "text_sha256": item.text_sha256,
                    }
                    for item in script.sentences
                ],
            }
            job_json = {
                "job_id": job.job_id,
                "owner_id": str(owner_id),
                "project_id": str(project_id),
                "sentence_sequences": list(job.sentence_sequences),
                "state": "PENDING",
            }
            if row is None:
                record_payload: dict[str, object] = {"script": encoded_script, "job": job_json}
                if split_plan is not None:
                    record_payload["sentence_splits"] = sentence_split_plan_to_json(split_plan)
                session.add(
                    NarrationProjectRecord(
                        project_id=project_id,
                        owner_id=owner_id,
                        payload=record_payload,
                    )
                )
            else:
                payload = dict(row.payload)
                if payload.get("script") != encoded_script:
                    raise DomainError("project_snapshot_locked", "Narration script is immutable.")
                payload.setdefault("job", job_json)
                if split_plan is not None:
                    payload.setdefault("sentence_splits", sentence_split_plan_to_json(split_plan))
                row.payload = payload
            if self._director_model_name is not None:
                session.flush()
                project_director_repository = SqlAlchemyProjectDirectorRepository(
                    self._session_factory
                )
                context = project_director_repository.project_context_in_session(
                    session, owner_id, project_id
                )
                if context is None:
                    raise DomainError(
                        "project_snapshot_mismatch",
                        "Frozen project director context is incomplete.",
                    )
                project_director = ProjectDirectorService(
                    project_director_repository,
                    _IntentOnlyDirectorModel(),  # type: ignore[arg-type]
                    _UuidFactory(),
                    _UtcClock(),
                    model_name=self._director_model_name,
                    parameters=self._director_parameters,
                )
                run, units = project_director.prepare_run(
                    context=context,
                    idempotency_key=(
                        f"narration-confirm:{project_id}:{snapshot_hash[:32]}"
                    ),
                    trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
                )
                project_director_repository.create_run_in_session(session, run, units)
            return (
                confirmed.snapshot.script,
                confirmed.snapshot.locale,
                confirmed.snapshot.voice_version_id,
                job,
            )

    def get_frozen_narration(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, SupportedLocale, UUID | None]:
        with self._session() as session:
            row = session.get(ProjectRow, project_id)
            if row is None or row.owner_id != owner_id or row.snapshot_script is None:
                raise DomainError("not_found", "Resource not found.")
            if row.snapshot_locale is None:
                raise DomainError("narration_not_ready", "Project snapshot is incomplete.")
            # EL 快照没有公共音色（§23.1）：voice_version_id 允许为空，由音色引用端口解析。
            return (
                row.snapshot_script,
                SupportedLocale(row.snapshot_locale),
                row.snapshot_voice_version_id,
            )


class SqlVoiceReferenceAdapter:
    """按项目冻结的配音源解析音色引用（§23.1）：CosyVoice 用参考 ZIP，EL 用克隆音色。"""

    def __init__(
        self, session_factory: Callable[[], Session], storage: PrivateObjectStorage
    ) -> None:
        self._session_factory, self._storage = session_factory, storage

    def get_reference(
        self, owner_id: UUID, project_id: UUID, locale: SupportedLocale
    ) -> VoiceReference:
        with self._session_factory() as session:
            repository = SqlAlchemyIdentityProjectRepository(session)
            project = repository.project(owner_id, project_id)
            if project is None:
                raise DomainError("not_found", "Resource not found.")
            if project.tts_provider == TtsProvider.ELEVENLABS.value:
                return self._cloned_reference(repository, project)
            return self._public_voice_reference(repository, project.voice_version_id, locale)

    def _cloned_reference(
        self, repository: SqlAlchemyIdentityProjectRepository, project: object
    ) -> VoiceReference:
        # 快照优先（冻结事实），未冻结草稿用当前列；两者都只保存逻辑音色 id。
        snapshot = getattr(project, "snapshot", None)
        cloned_voice_id = (
            snapshot.cloned_voice_id if snapshot is not None else None
        ) or getattr(project, "cloned_voice_id", None)
        if cloned_voice_id is None:
            raise DomainError(
                "cloned_voice_unavailable", "The project has no frozen cloned voice."
            )
        cloned = repository.cloned_voice_by_id(cloned_voice_id)
        if cloned is None:
            raise DomainError(
                "cloned_voice_unavailable", "The frozen cloned voice is unavailable."
            )
        return VoiceReference(
            None,
            b"",
            cloned.source_sha256,
            provider=TtsProvider.ELEVENLABS.value,
            cloned_voice_id=cloned.id,
            cloned_voice_name=cloned.name,
        )

    def _public_voice_reference(
        self,
        repository: SqlAlchemyIdentityProjectRepository,
        voice_version_id: UUID | None,
        locale: SupportedLocale,
    ) -> VoiceReference:
        if voice_version_id is None:
            raise DomainError(
                "voice_not_available", "The project has no CosyVoice voice version."
            )
        voice = repository.voice_version(voice_version_id)
        if voice is None or not voice.enabled or locale not in voice.supported_locales:
            raise DomainError(
                "voice_not_available", "Voice version is unavailable for the locale."
            )
        media = repository.voice_media(voice_version_id, "reference")
        if media is None or media[1] != "application/zip":
            raise DomainError(
                "voice_reference_missing", "Voice reference package is unavailable."
            )
        try:
            payload = self._storage.get_verified(media[0], media[2], media[3])
        except ValueError as error:
            raise DomainError(
                "voice_reference_hash_mismatch",
                "Voice reference package failed integrity verification.",
            ) from error
        return VoiceReference(voice.id, payload, media[2])


class SqlProjectWriteGuard:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def assert_writable(self, owner_id: UUID, project_id: UUID) -> None:
        with self._session_factory() as session:
            project = session.get(ProjectRow, project_id)
            if project is None or project.owner_id != owner_id:
                raise DomainError("not_found", "Resource not found.")
            if project.production_state != "ACTIVE":
                raise DomainError("project_purging", "Project is being permanently deleted.")


class SessionScopedProjectRepository:
    """长生命周期组件（EL Provider）用的每调用一会话的身份/项目仓储视图。

    实现 EL 配音路径真正需要的方法：补克隆映射读写、频道帧源音频定位。
    每次调用开新会话，避免跨请求持有 Session（领域规则仍在 SQL 仓储内）。
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def cloned_voice_account(
        self, cloned_voice_id: UUID, pool_key_id: UUID
    ) -> ClonedVoiceAccount | None:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).cloned_voice_account(
                cloned_voice_id, pool_key_id
            )

    def cloned_voice_by_id(self, cloned_voice_id: UUID) -> ClonedVoice | None:
        with self._session_factory() as session:
            return SqlAlchemyIdentityProjectRepository(session).cloned_voice_by_id(cloned_voice_id)

    def save_cloned_voice_account(self, account: ClonedVoiceAccount) -> None:
        with self._session_factory() as session, session.begin():
            SqlAlchemyIdentityProjectRepository(session).save_cloned_voice_account(account)


@dataclass(slots=True)
class NarrationRuntime:
    service: ProjectNarrationService
    database: Database
    http_client: httpx.AsyncClient
    tokens: OpaqueSessionTokens
    storage: PrivateObjectStorage
    dispatcher: PendingNarrationDispatcher
    cookie_secure: bool = False
    # 仅意图落库的导演服务（不在配音确认内调用 Provider）；AUTO 编排器恢复运行复用。
    project_director: ProjectDirectorService | None = None
    # 与配音链路共享的 EL 配额端口（号池管理 API 复用同一适配器/信号量，2026-10-13）。
    elevenlabs_quota: ElevenlabsQuotaPort | None = None

    def _identity_service(self, session: Session) -> IdentityProjectService:
        return IdentityProjectService(
            SqlAlchemyIdentityProjectRepository(session),
            Argon2PasswordHasher(),
            opaque_tokens=self.tokens,
        )

    def current_owner(self, authorization: Annotated[str | None, Header()] = None) -> Account:
        if not authorization or not authorization.startswith("Bearer "):
            raise DomainError("invalid_session", "Session is invalid")
        with self.database.unit_of_work() as session:
            return self._identity_service(session).authenticate(
                authorization.removeprefix("Bearer ")
            )

    def media_account(
        self,
        authorization: Annotated[str | None, Header()] = None,
        cookie_token: Annotated[
            str | None, Cookie(alias=MEDIA_SESSION_COOKIE)
        ] = None,
    ) -> Account:
        with self.database.unit_of_work() as session:
            return authenticate_media_account(
                self._identity_service(session),
                authorization=authorization,
                cookie_token=cookie_token,
                cookie_secure=self.cookie_secure,
            )

    def current_owner_id(self, authorization: Annotated[str | None, Header()] = None) -> UUID:
        return self.current_owner(authorization).id

    def current_media_owner(
        self, authorization: Annotated[str | None, Header()] = None
    ) -> AuthenticatedOwner:
        return AuthenticatedOwner(self.current_owner(authorization).id)

    def cookie_media_owner(
        self,
        authorization: Annotated[str | None, Header()] = None,
        cookie_token: Annotated[
            str | None, Cookie(alias=MEDIA_SESSION_COOKIE)
        ] = None,
    ) -> AuthenticatedOwner:
        return AuthenticatedOwner(self.media_account(authorization, cookie_token).id)

    async def start(self) -> None:
        await self.dispatcher.start()

    async def close(self) -> None:
        await self.dispatcher.stop()
        await self.http_client.aclose()


def _elevenlabs_voice_settings(settings: Settings) -> dict[str, float | bool] | None:
    """§35：把 env 的 EL voice_settings 三元组装配成请求参数。

    任一项为 None 即不进入字典；三项全空时返回 None（不发送 voice_settings，
    回退 v2 时行为与既有版本一致）。
    """
    assembled = {
        key: value
        for key, value in {
            "stability": settings.elevenlabs_stability,
            "similarity_boost": settings.elevenlabs_similarity_boost,
            "use_speaker_boost": settings.elevenlabs_use_speaker_boost,
        }.items()
        if value is not None
    }
    return assembled or None


def create_narration_runtime(
    settings: Settings,
    *,
    temporal_client: Client | None = None,
    database: Database | None = None,
    storage: PrivateObjectStorage | None = None,
    tokens: OpaqueSessionTokens | None = None,
) -> NarrationRuntime:
    database = database or Database(settings.database_url)
    storage = storage or PrivateObjectStorage(
        Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        ),
        settings.minio_bucket,
    )
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(300.0), follow_redirects=False, trust_env=False
    )
    # §23.9：EL 流量单独走代理（compose 内为 mihomo 容器）；其余线路不被代理。
    proxy = settings.elevenlabs_proxy.strip()
    elevenlabs_http_client = (
        httpx.AsyncClient(
            timeout=httpx.Timeout(300.0),
            follow_redirects=False,
            trust_env=False,
            proxy=proxy,
        )
        if proxy
        else http_client
    )
    repository = SqlNarrationRepository(database.session)
    cosyvoice = CosyVoiceHttpAdapter(
        settings.cosyvoice_url,
        client=http_client,
        token=settings.cosyvoice_token.get_secret_value(),
        max_concurrency=settings.cosyvoice_max_concurrency,
    )
    elevenlabs_client = ElevenLabsHttpClient(
        settings.elevenlabs_api_base,
        client=elevenlabs_http_client,
        per_key_concurrency=settings.elevenlabs_per_key_concurrency,
    )
    # §23 号池：key 由管理页入库，配额刷新与失败轮换都走同一服务。
    elevenlabs_quota = ElevenLabsQuotaAdapter(elevenlabs_client)
    key_pool = ElevenlabsKeyPoolService(
        SqlElevenlabsKeyPool(database.session), elevenlabs_quota
    )
    submitter = SubmitSentenceNarration(
        cosyvoice,
        repository,
        PrivateNarrationAudioArchive(storage, database.session),
        FfmpegAudioNormalizer(settings.ffmpeg_executable, ffmpeg_version=settings.ffmpeg_version),
        FfprobeAudioInspector(settings.ffprobe_executable),
        providers={
            TtsProvider.ELEVENLABS.value: ElevenLabsNarrationProvider(
                elevenlabs_client,
                repository,
                key_pool,
                SessionScopedProjectRepository(database.session),
                elevenlabs_client,
                storage,
                FfmpegRawWavTranscoder(settings.ffmpeg_executable),
                model_id=settings.elevenlabs_tts_model,
                voice_settings=_elevenlabs_voice_settings(settings),
                duration_guard_seconds=settings.elevenlabs_duration_guard_seconds,
                duration_probe=FfprobeAudioInspector(settings.ffprobe_executable),
            )
        },
    )
    project_director_repository = SqlAlchemyProjectDirectorRepository(
        database.unit_of_work
    )
    project_director = ProjectDirectorService(
        project_director_repository,
        _IntentOnlyDirectorModel(),  # type: ignore[arg-type]
        _UuidFactory(),
        _UtcClock(),
        model_name=settings.director_provider_model,
        parameters={"temperature": 0.0},
    )
    sentence_splitter = SentenceSplitService(
        DeepSeekSentenceSplitProvider(
            settings.sentence_split_provider_url,
            settings.sentence_split_provider_token.get_secret_value(),
            timeout_seconds=settings.sentence_split_provider_timeout_seconds,
            client=http_client,
        )
        if settings.sentence_split_provider_configured
        else None,
        model=settings.sentence_split_provider_model,
        threshold_seconds=settings.sentence_split_estimated_seconds_threshold,
    )
    service = ProjectNarrationService(
        SqlProjectSnapshotAdapter(
            database.session,
            director_model_name=settings.director_provider_model,
            director_parameters={"temperature": 0.0},
            director_provider_configured=settings.director_provider_configured,
        ),
        SqlVoiceReferenceAdapter(database.session, storage),
        repository,
        submitter,
        TemporalNarrationStarter(
            temporal_client,
            target=settings.temporal_target,
            task_queue=settings.narration_task_queue,
        ),
        SqlProjectWriteGuard(database.session),
        director_materializer=project_director.materialize_if_ready,
        sentence_splitter=sentence_splitter,
    )
    return NarrationRuntime(
        service,
        database,
        http_client,
        tokens or OpaqueSessionTokens(),
        storage,
        PendingNarrationDispatcher(service),
        settings.cookie_secure,
        project_director,
        elevenlabs_quota=elevenlabs_quota,
    )
