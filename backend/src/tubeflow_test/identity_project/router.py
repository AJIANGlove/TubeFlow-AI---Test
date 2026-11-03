# ruff: noqa: B008
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tubeflow_test.domain.common import AspectRatio, SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.http_range import RangeNotSatisfiable, parse_single_range

from .channel_deletion import ChannelDeletionService
from .cloned_voice_service import ClonedVoiceService
from .cloned_voices import ClonedVoiceUpload
from .domain import (
    MAX_CHANNEL_DEFAULT_VISUAL_STYLE,
    Account,
    AccountRole,
    ClonedVoice,
    Project,
)
from .media_session import (
    MEDIA_SESSION_COOKIE,
    authenticate_media_account,
    clear_media_session_cookie,
    invalid_session_with_cookie_clear,
    set_media_session_cookie,
)
from .service import IdentityProjectService

if TYPE_CHECKING:
    from tubeflow_test.auto_pipeline.service import AutoPipelineService


# §23.3：产品侧不自设音频规格上限，仅设上传体积护栏（EL 侧再校验可解码性）。
CLONED_VOICE_MAX_BYTES = 100 * 1024 * 1024


def decode_channel_confirmation(*, plain: str | None, encoded: str | None) -> str:
    """频道删除确认值：B64（UTF-8）优先，明文头仅兼容 ASCII 脚本调用。"""
    if encoded is not None:
        try:
            return base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, ValueError) as error:
            raise DomainError(
                "channel_deletion_confirmation_required",
                "请输入完整的频道名称或频道 URL 以确认删除。",
            ) from error
    if plain is not None:
        return plain
    raise DomainError(
        "channel_deletion_confirmation_required",
        "请输入完整的频道名称或频道 URL 以确认删除。",
    )



class ApiInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(ApiInput):
    username: str
    password: str


class PasswordInput(ApiInput):
    password: str


class TokenResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    account: dict[str, Any]


class ChannelInput(ApiInput):
    url: str


class ChannelPatch(ApiInput):
    url: str | None = None
    default_voice_version_id: UUID | None = None
    default_character_image_ids: list[UUID] | None = Field(default=None, max_length=3)
    name: str | None = Field(default=None, max_length=30)
    # §23.1：频道默认配音源与默认克隆音色（改选源时必须同时给出该源下的默认音色）。
    default_tts_provider: str | None = None
    default_cloned_voice_id: UUID | None = None
    # 频道默认画面风格：空串=清空，显式 null 由下方校验拒绝。
    default_visual_style: str | None = Field(
        default=None, max_length=MAX_CHANNEL_DEFAULT_VISUAL_STYLE
    )

    @model_validator(mode="after")
    def reject_empty_or_null_update(self) -> ChannelPatch:
        if not self.model_fields_set:
            raise ValueError("Channel update must contain at least one field")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProjectInput(ApiInput):
    title: str
    channel_id: UUID
    script: str
    character_image_ids: tuple[UUID, ...]
    # 未传/空串时回落频道默认画面风格（服务层校验非空）。
    visual_style: str | None = None
    voice_version_id: UUID | None = None
    locale: SupportedLocale
    aspect_ratio: AspectRatio
    expected_duration_seconds: int | None = None
    # §23.1：新建项目默认继承频道默认配音源，冻结前可改选；两列按源二选一。
    tts_provider: str | None = None
    cloned_voice_id: UUID | None = None


class ProjectPatch(ApiInput):
    expected_revision: int
    script: str | None = None
    character_image_ids: tuple[UUID, ...] | None = None
    visual_style: str | None = None
    voice_version_id: UUID | None = None
    locale: SupportedLocale | None = None
    aspect_ratio: AspectRatio | None = None
    expected_duration_seconds: int | None = None
    title: str | None = None
    tts_provider: str | None = None
    cloned_voice_id: UUID | None = None

    @model_validator(mode="after")
    def reject_null_production_fields(self) -> ProjectPatch:
        for field in self.model_fields_set - {"expected_revision", "expected_duration_seconds"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ConfirmInput(ApiInput):
    expected_revision: int


ServiceFactory = (
    IdentityProjectService | Callable[[], IdentityProjectService | Iterator[IdentityProjectService]]
)


@dataclass(frozen=True, slots=True)
class IdentityProjectDependencies:
    current_session: Callable[..., Iterator[IdentityProjectService]]
    current_account: Callable[..., Account]
    media_account: Callable[..., Account]
    admin: Callable[..., Account]


def _immutable_media_response(content: bytes, media_type: str, request: Request) -> Response:
    size = len(content)
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    protected_cache = "private, no-cache"
    try:
        selected = parse_single_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": protected_cache,
                "Content-Range": f"bytes */{size}",
                "ETag": etag,
            },
        )
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": protected_cache,
                "ETag": etag,
            },
        )
    payload = content
    status_code = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": protected_cache,
        "Content-Length": str(size),
        "ETag": etag,
    }
    if selected is not None:
        payload = content[selected.start : selected.end + 1]
        status_code = 206
        headers["Content-Length"] = str(selected.length)
        headers["Content-Range"] = f"bytes {selected.start}-{selected.end}/{size}"
    return Response(
        b"" if request.method == "HEAD" else payload,
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


def create_dependencies(
    factory: ServiceFactory, *, cookie_secure: bool = False
) -> IdentityProjectDependencies:
    def current_session() -> Iterator[IdentityProjectService]:
        if not callable(factory):
            yield factory
            return
        produced = factory()
        if isinstance(produced, IdentityProjectService):
            yield produced
        else:
            yield from produced

    def current_account(
        authorization: str | None = Header(default=None),
        svc: IdentityProjectService = Depends(current_session),
    ) -> Account:
        try:
            if not authorization or not authorization.startswith("Bearer "):
                raise DomainError("invalid_session", "Session is invalid")
            return svc.authenticate(authorization[7:])
        except DomainError as error:
            raise _http(error, 401) from error

    def admin(account: Account = Depends(current_account)) -> Account:
        if account.role is not AccountRole.ADMIN or not account.active:
            raise _http(DomainError("forbidden", "Administrator access required"), 403)
        return account

    def media_account(
        authorization: str | None = Header(default=None),
        cookie_token: str | None = Cookie(default=None, alias=MEDIA_SESSION_COOKIE),
        svc: IdentityProjectService = Depends(current_session),
    ) -> Account:
        return authenticate_media_account(
            svc,
            authorization=authorization,
            cookie_token=cookie_token,
            cookie_secure=cookie_secure,
        )

    return IdentityProjectDependencies(current_session, current_account, media_account, admin)


def create_router(
    factory: ServiceFactory,
    dependencies: IdentityProjectDependencies | None = None,
    *,
    cookie_secure: bool = False,
    cloned_voice_service: Callable[[], ClonedVoiceService] | None = None,
    channel_deletion_service: Callable[[], ChannelDeletionService] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/identity", tags=["identity"])
    dependencies = dependencies or create_dependencies(factory)
    service = dependencies.current_session
    actor = dependencies.current_account
    media_actor = dependencies.media_account
    admin = dependencies.admin

    def cloned_voices() -> ClonedVoiceService:
        if cloned_voice_service is None:
            raise _http(
                DomainError("cloned_voice_unavailable", "Cloned voice management is unavailable"),
                503,
            )
        return cloned_voice_service()

    def channel_deletions() -> ChannelDeletionService:
        if channel_deletion_service is None:
            raise _http(
                DomainError("channel_deletion_unavailable", "Channel deletion is unavailable"),
                503,
            )
        return channel_deletion_service()

    def authenticated_session(
        authorization: str | None = Header(default=None),
        svc: IdentityProjectService = Depends(service),
    ) -> tuple[Account, str]:
        try:
            if not authorization or not authorization.startswith("Bearer "):
                raise DomainError("invalid_session", "Session is invalid")
            return svc.authenticate(authorization[7:]), authorization[7:]
        except DomainError as error:
            raise invalid_session_with_cookie_clear(secure=cookie_secure) from error

    @router.get("/setup/status")
    def setup_status(svc: IdentityProjectService = Depends(service)) -> dict[str, bool]:
        complete = svc.setup_complete()
        return {"initialized": complete, "initialization_available": not complete}

    @router.post("/setup/initialize", status_code=201)
    def initialize(
        data: Credentials, svc: IdentityProjectService = Depends(service)
    ) -> dict[str, Any]:
        return _call(lambda: _account(svc.initialize_admin(data.username, data.password)))

    @router.get("/projects")
    def list_projects(
        search: str | None = None,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> list[dict[str, Any]]:
        projects = svc.list_projects(account)
        if search:
            needle = search.strip().casefold()
            projects = tuple(item for item in projects if needle in item.title.casefold())
        result: list[dict[str, Any]] = []
        for item in projects:
            payload = _project(item)
            payload["flow"] = svc.project_flow(account, item)
            result.append(payload)
        return result

    @router.post("/login")
    def login(
        data: Credentials,
        response: Response,
        svc: IdentityProjectService = Depends(service),
    ) -> TokenResponse:
        def build_response() -> TokenResponse:
            token = svc.login(data.username, data.password)
            account = svc.authenticate(token)
            result = TokenResponse(
                token=token,
                expires_in_seconds=12 * 60 * 60,
                account=_account(account),
            )
            set_media_session_cookie(response, token, secure=cookie_secure)
            return result

        return _call(build_response)

    @router.post("/logout", status_code=204)
    def logout(
        response: Response,
        value: tuple[Account, str] = Depends(authenticated_session),
        svc: IdentityProjectService = Depends(service),
    ) -> None:
        svc.logout(value[0], value[1])
        clear_media_session_cookie(response, secure=cookie_secure)

    @router.get("/me")
    def me(account: Account = Depends(actor)) -> dict[str, Any]:
        return _account(account)

    @router.get("/admin/users")
    def users(
        account: Account = Depends(admin),
        svc: IdentityProjectService = Depends(service),
    ) -> list[dict[str, Any]]:
        return _call(lambda: [_account(item) for item in svc.list_accounts(account)])

    @router.post("/admin/users", status_code=201)
    def create_user(
        data: Credentials,
        account: Account = Depends(admin),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(lambda: _account(svc.create_account(account, data.username, data.password)))

    @router.post("/admin/users/{account_id}/disable", status_code=204)
    def disable_user(
        account_id: UUID,
        account: Account = Depends(admin),
        svc: IdentityProjectService = Depends(service),
    ) -> None:
        _call(lambda: svc.disable_account(account, account_id))

    @router.post("/admin/users/{account_id}/reset-password", status_code=204)
    def reset_user(
        account_id: UUID,
        data: PasswordInput,
        account: Account = Depends(admin),
        svc: IdentityProjectService = Depends(service),
    ) -> None:
        _call(lambda: svc.reset_password(account, account_id, data.password))

    @router.post("/channels", status_code=201)
    def create_channel(
        data: ChannelInput,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(lambda: _channel(svc.create_channel(account, data.url)))

    @router.get("/channels")
    def list_channels(
        account: Account = Depends(actor), svc: IdentityProjectService = Depends(service)
    ) -> list[dict[str, Any]]:
        return [_channel(item) for item in svc.list_channels(account)]

    @router.get("/channels/{channel_id}")
    def get_channel(
        channel_id: UUID,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(lambda: _channel(svc.get_channel(account, channel_id)))

    @router.patch("/channels/{channel_id}")
    def patch_channel(
        channel_id: UUID,
        data: ChannelPatch,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(
            lambda: _channel(
                svc.update_channel(
                    account,
                    channel_id,
                    data.url,
                    default_voice_version_id=data.default_voice_version_id,
                    default_character_image_ids=data.default_character_image_ids,
                    name=data.name,
                    default_tts_provider=data.default_tts_provider,
                    default_cloned_voice_id=data.default_cloned_voice_id,
                    default_visual_style=data.default_visual_style,
                )
            )
        )

    @router.delete("/channels/{channel_id}")
    async def delete_channel(
        channel_id: UUID,
        account: Account = Depends(actor),
        deletions: ChannelDeletionService = Depends(channel_deletions),
        x_confirm_channel: str | None = Header(default=None, max_length=4096),
        x_confirm_channel_b64: str | None = Header(default=None, max_length=8192),
    ) -> dict[str, Any]:
        confirmation = _call(
            lambda: decode_channel_confirmation(
                plain=x_confirm_channel, encoded=x_confirm_channel_b64
            )
        )
        result = await _call_async(deletions.delete(account, channel_id, confirmation))
        summary = result.summary
        return {
            "id": str(summary.channel_id),
            "status": "DELETED",
            "deleted_assets": {
                "character_images": summary.character_images,
                "cloned_voices": summary.cloned_voices,
                "cover_ref_images": summary.cover_ref_images,
                "cover_character_cards": summary.cover_character_cards,
                "cover_templates": summary.cover_templates,
                "description_templates": summary.description_templates,
            },
            "remote_voice_cleanup_failures": len(result.remote_voice_failures),
            "object_cleanup_failures": len(result.object_cleanup_failures),
        }

    @router.get("/channels/{channel_id}/cloned-voices")
    async def list_cloned_voices(
        channel_id: UUID,
        account: Account = Depends(actor),
        cloned: ClonedVoiceService = Depends(cloned_voices),
    ) -> list[dict[str, Any]]:
        voices = cloned.list_channel_voices(account.id, channel_id)
        return [_cloned_voice(item) for item in voices]

    @router.post("/channels/{channel_id}/cloned-voices", status_code=201)
    async def upload_cloned_voice(
        channel_id: UUID,
        file: UploadFile = File(...),
        name: str = Form(...),
        remark: str = Form(default=""),
        account: Account = Depends(actor),
        cloned: ClonedVoiceService = Depends(cloned_voices),
    ) -> dict[str, Any]:
        content = await file.read(CLONED_VOICE_MAX_BYTES + 1)
        if len(content) > CLONED_VOICE_MAX_BYTES:
            raise _http(
                DomainError(
                    "cloned_voice_source_too_large",
                    "Cloned voice source audio exceeds the upload limit",
                ),
                413,
            )
        upload = ClonedVoiceUpload(
            name,
            remark,
            file.filename or "",
            content,
            file.content_type or "application/octet-stream",
        )
        result = await _call_async(cloned.upload(account.id, channel_id, upload))
        return {
            "voice": _cloned_voice(result.voice),
            "accounts": [
                {
                    "key_id": str(item.key_id),
                    "label": item.label,
                    "ok": item.ok,
                    "el_voice_id": item.el_voice_id,
                    "error_code": item.error_code,
                    "error_message": item.error_message,
                }
                for item in result.outcomes
            ],
        }

    @router.delete("/channels/{channel_id}/cloned-voices/{cloned_voice_id}", status_code=204)
    async def delete_cloned_voice(
        channel_id: UUID,
        cloned_voice_id: UUID,
        account: Account = Depends(actor),
        cloned: ClonedVoiceService = Depends(cloned_voices),
    ) -> None:
        await _call_async(cloned.delete(account.id, channel_id, cloned_voice_id))

    @router.head(
        "/channels/{channel_id}/cloned-voices/{cloned_voice_id}/source", include_in_schema=False
    )
    @router.get("/channels/{channel_id}/cloned-voices/{cloned_voice_id}/source")
    async def cloned_voice_source(
        channel_id: UUID,
        cloned_voice_id: UUID,
        request: Request,
        account: Account = Depends(media_actor),
        cloned: ClonedVoiceService = Depends(cloned_voices),
    ) -> Response:
        voice, content = _call(lambda: cloned.get_source(account.id, channel_id, cloned_voice_id))
        return _immutable_media_response(content, voice.source_media_type, request)

    @router.post("/channels/{channel_id}/character-images", status_code=201)
    async def upload_image(
        channel_id: UUID,
        file: UploadFile = File(...),
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        content = await file.read(10 * 1024 * 1024 + 1)
        return _call(lambda: _image(svc.upload_image(account, channel_id, content)))

    @router.get("/channels/{channel_id}/character-images")
    def images(
        channel_id: UUID,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> list[dict[str, Any]]:
        return [_image(item) for item in svc.list_images(account, channel_id)]

    @router.head(
        "/channels/{channel_id}/character-images/{image_id}/content", include_in_schema=False
    )
    @router.get("/channels/{channel_id}/character-images/{image_id}/content")
    def image_content(
        channel_id: UUID,
        image_id: UUID,
        request: Request,
        account: Account = Depends(media_actor),
        svc: IdentityProjectService = Depends(service),
    ) -> Response:
        image, content = _call(lambda: svc.get_image(account, channel_id, image_id))
        return Response(
            b"" if request.method == "HEAD" else content,
            media_type=image.asset.media_type,
            headers={"Content-Length": str(len(content))},
        )

    @router.get("/voices")
    def voices(
        account: Account = Depends(actor), svc: IdentityProjectService = Depends(service)
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for v in svc.list_voice_versions(account):
            metadata = json.loads(v.catalog_metadata_json)
            result.append(
                {
                    "id": str(v.id),
                    "voice_id": v.voice_id,
                    "version": v.version,
                    "display_name": v.display_name,
                    "supported_locales": [x.value for x in v.supported_locales],
                    "supported_language_codes": list(v.raw_language_codes),
                    "metadata_sha256": v.metadata_sha256,
                    "gender": metadata["gender"],
                    "age_band": metadata["age_band"],
                    "accent": metadata["accent"],
                    "accent_zh": metadata["accent_zh"],
                    "tags": metadata["tags"],
                    "description": metadata["description"],
                    "author": metadata["author"],
                    "source_site": metadata["source_site"],
                    "avatar_url": f"/identity/voices/{v.id}/avatar",
                    "preview_url": f"/identity/voices/{v.id}/preview",
                }
            )
        return result

    @router.head("/voices/{voice_version_id}/{kind}", include_in_schema=False)
    @router.get("/voices/{voice_version_id}/{kind}")
    def voice_media(
        voice_version_id: UUID,
        kind: str,
        request: Request,
        account: Account = Depends(media_actor),
        svc: IdentityProjectService = Depends(service),
    ) -> Response:
        if kind not in {"preview", "avatar"}:
            raise _http(DomainError("not_found", "Resource not found"), 404)
        content, media_type = _call(lambda: svc.get_voice_media(account, voice_version_id, kind))
        return _immutable_media_response(content, media_type, request)

    @router.post("/projects", status_code=201)
    def create_project(
        data: ProjectInput,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(
            lambda: _project(
                svc.create_project(
                    account,
                    channel_id=data.channel_id,
                    title=data.title,
                    script=data.script,
                    image_ids=data.character_image_ids,
                    visual_style=data.visual_style,
                    voice_version_id=data.voice_version_id,
                    locale=data.locale,
                    aspect_ratio=data.aspect_ratio,
                    expected_duration_seconds=data.expected_duration_seconds,
                    tts_provider=data.tts_provider,
                    cloned_voice_id=data.cloned_voice_id,
                )
            )
        )

    @router.get("/projects/{project_id}")
    def get_project(
        project_id: UUID,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(lambda: _project(svc.get_project(account, project_id)))

    @router.get("/projects/{project_id}/workflow-summary")
    def workflow_summary(
        project_id: UUID,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, object]:
        return _call(lambda: svc.workflow_summary(account, project_id))

    @router.post("/projects/{project_id}/auto-pipeline/resume")
    def resume_auto_pipeline(
        project_id: UUID,
        request: Request,
        account: Account = Depends(actor),
    ) -> dict[str, Any]:
        # §15.9：仅 PAUSED 可恢复；恢复 = 新一轮授权（预算轮次 +1）并立即推进。
        pipeline: AutoPipelineService | None = getattr(
            request.app.state, "auto_pipeline_service", None
        )
        if pipeline is None:
            raise _http(
                DomainError("auto_pipeline_unavailable", "Auto pipeline is unavailable"), 503
            )
        run = _call(lambda: pipeline.resume(account.id, project_id))
        return {
            "state": run.state.value,
            "reason_code": run.reason_code,
            "authorization_round": run.authorization_round,
        }

    @router.patch("/projects/{project_id}")
    def patch_project(
        project_id: UUID,
        data: ProjectPatch,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        changes = data.model_dump(exclude_unset=True, exclude={"expected_revision"})
        return _call(
            lambda: _project(svc.edit_project(account, project_id, data.expected_revision, changes))
        )

    @router.post("/projects/{project_id}/confirm-narration")
    def confirm(
        project_id: UUID,
        data: ConfirmInput,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(
            lambda: _project(
                svc.confirm_narration(
                    account, project_id, data.expected_revision, datetime.now(UTC)
                )
            )
        )

    @router.post("/projects/{project_id}/publication")
    def publish_project(
        project_id: UUID,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(
            lambda: _project(
                svc.set_project_publication(account, project_id, True, datetime.now(UTC))
            )
        )

    @router.delete("/projects/{project_id}/publication")
    def unpublish_project(
        project_id: UUID,
        account: Account = Depends(actor),
        svc: IdentityProjectService = Depends(service),
    ) -> dict[str, Any]:
        return _call(
            lambda: _project(
                svc.set_project_publication(account, project_id, False, datetime.now(UTC))
            )
        )

    return router


def _account(value: Account) -> dict[str, Any]:
    return {
        "id": str(value.id),
        "username": value.username,
        "role": value.role.value,
        "active": value.active,
    }


def _channel(value: Any) -> dict[str, Any]:
    return {
        "id": str(value.id),
        "url": value.url,
        "name": value.name,
        "default_voice_version_id": (
            str(value.default_voice_version_id)
            if value.default_voice_version_id is not None
            else None
        ),
        "default_character_image_ids": [str(item) for item in value.default_character_image_ids],
        "has_cover_style": getattr(value, "current_cover_style_profile_id", None) is not None,
        "default_tts_provider": getattr(value, "default_tts_provider", "cosyvoice"),
        "default_cloned_voice_id": (
            str(value.default_cloned_voice_id)
            if getattr(value, "default_cloned_voice_id", None) is not None
            else None
        ),
        "default_visual_style": getattr(value, "default_visual_style", "") or "",
    }


def _cloned_voice(value: ClonedVoice) -> dict[str, Any]:
    return {
        "id": str(value.id),
        "channel_id": str(value.channel_id),
        "name": value.name,
        "remark": value.remark,
        "source_sha256": value.source_sha256,
        "source_size_bytes": value.source_size_bytes,
        "source_media_type": value.source_media_type,
        "created_at": value.created_at.isoformat(),
        # 试听即源音频（§23.3，无独立 preview 资产）。
        "source_url": f"/identity/channels/{value.channel_id}/cloned-voices/{value.id}/source",
    }


def _image(value: Any) -> dict[str, Any]:
    return {
        "id": str(value.id),
        "sha256": value.asset.sha256,
        "media_type": value.asset.media_type,
        "size_bytes": value.asset.size_bytes,
        "width": value.width,
        "height": value.height,
    }


def _project(value: Project) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(value.id),
        "title": value.title,
        "channel_id": str(value.channel_id),
        "script": value.script,
        "character_image_ids": [str(x) for x in value.character_image_ids],
        "visual_style": value.visual_style,
        "voice_version_id": (
            str(value.voice_version_id) if value.voice_version_id is not None else None
        ),
        "locale": value.locale.value,
        "aspect_ratio": value.aspect_ratio.value,
        "expected_duration_seconds": value.expected_duration_seconds,
        "draft_revision": value.draft_revision,
        "production_mode": value.production_mode,
        "tts_provider": value.tts_provider,
        "cloned_voice_id": (
            str(value.cloned_voice_id) if value.cloned_voice_id is not None else None
        ),
        "created_at": (
            value.created_at.isoformat() if value.created_at is not None else None
        ),
        "published_at": (
            value.published_at.isoformat() if value.published_at is not None else None
        ),
        "has_current_cover": value.current_cover_image_generation_id is not None,
        "current_cover_image_generation_id": (
            str(value.current_cover_image_generation_id)
            if value.current_cover_image_generation_id is not None
            else None
        ),
        "snapshot": None,
    }
    if value.snapshot:
        result["snapshot"] = {
            "script": value.snapshot.script,
            "characters": [
                {
                    "ordinal": x.ordinal,
                    "character_image_id": str(x.character_image_id),
                    "asset_id": str(x.asset_id),
                    "sha256": x.sha256,
                    "media_type": x.media_type,
                    "size_bytes": x.size_bytes,
                }
                for x in value.snapshot.characters
            ],
            "visual_style": value.snapshot.visual_style,
            "tts_provider": value.snapshot.tts_provider,
            "voice_version_id": (
                str(value.snapshot.voice_version_id)
                if value.snapshot.voice_version_id is not None
                else None
            ),
            "voice_metadata_sha256": value.snapshot.voice_metadata_sha256,
            "voice_catalog_source_sha256": value.snapshot.voice_catalog_source_sha256,
            "voice_reference_zip": (
                _voice_fact(value.snapshot.voice_reference_zip)
                if value.snapshot.voice_reference_zip is not None
                else None
            ),
            "voice_preview": (
                _voice_fact(value.snapshot.voice_preview)
                if value.snapshot.voice_preview is not None
                else None
            ),
            "voice_avatar": (
                _voice_fact(value.snapshot.voice_avatar)
                if value.snapshot.voice_avatar is not None
                else None
            ),
            "cloned_voice_id": (
                str(value.snapshot.cloned_voice_id)
                if value.snapshot.cloned_voice_id is not None
                else None
            ),
            "cloned_voice_name": value.snapshot.cloned_voice_name,
            "cloned_voice_source_sha256": value.snapshot.cloned_voice_source_sha256,
            "locale": value.snapshot.locale.value,
            "aspect_ratio": value.snapshot.aspect_ratio.value,
            "channel_url": value.snapshot.channel_url,
            "expected_duration_seconds": value.snapshot.expected_duration_seconds,
            "confirmed_at": value.snapshot.confirmed_at.isoformat(),
        }
    return result


def _voice_fact(value: Any) -> dict[str, Any]:
    return {
        "asset_id": str(value.asset_id),
        "sha256": value.sha256,
        "media_type": value.media_type,
        "size_bytes": value.size_bytes,
    }


def _http(error: DomainError, fallback: int = 422) -> HTTPException:
    codes = {
        "admin_already_initialized": 409,
        "project_already_confirmed": 409,
        "project_snapshot_locked": 409,
        "project_purging": 409,
        "project_composition_required": 409,
        "project_publication_conflict": 409,
        "admin_lockout_forbidden": 409,
        "invalid_username": 409,
        "voice_version_conflict": 409,
        "invalid_credentials": 401,
        "invalid_session": 401,
        "not_found": 404,
        "forbidden": 403,
        "auto_pipeline_not_paused": 409,
        # §23.5：删除保护（频道默认/冻结快照/未冻结草稿）与同名冲突。
        "cloned_voice_channel_default": 409,
        "cloned_voice_frozen_snapshot": 409,
        "cloned_voice_draft_reference": 409,
        "cloned_voice_name_conflict": 409,
        "cloned_voice_unavailable": 503,
        # 2026-10-20 频道删除：仅空频道可删；确认值缺失/不匹配；服务未装配。
        "channel_has_projects": 409,
        "channel_deletion_confirmation_required": 422,
        "channel_deletion_unavailable": 503,
    }
    return HTTPException(
        codes.get(error.code, fallback), detail={"code": error.code, "message": error.message}
    )


def _call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except DomainError as error:
        raise _http(error) from error


async def _call_async[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except DomainError as error:
        raise _http(error) from error
