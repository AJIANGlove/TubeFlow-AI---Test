# ruff: noqa: B008
"""封面图生成 API：频道风格与项目封面两组路由（计划 §7；v5 追加角色卡、
参考图媒体预览、提示词版本切换、风格版本 label 与按版本生成）。

照 `api/storyboards.py` 的会话依赖、Idempotency-Key、202 + public_state 与
DomainError→HTTP 映射约定；受保护媒体照 `identity_project/router.py` 的
_immutable_media_response（媒体 Cookie/Bearer + Range + ETag + 版本绑定）。
所有端点按账号所有权隔离；项目侧未冻结一律 cover_project_not_frozen（409）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tubeflow_test.api.public_status import PublicState, PublicStatus, public_state
from tubeflow_test.application.cover_characters import CoverCharacterService
from tubeflow_test.application.cover_images import (
    COVER_IMAGE_UPLOAD_MAX_BYTES,
    CoverImageService,
)
from tubeflow_test.application.cover_ports import (
    COVER_SCOPE_CHANNEL,
    COVER_SCOPE_PROJECT,
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    CoverCharacterCard,
    CoverImageGeneration,
    CoverPromptGeneration,
    CoverRefImage,
    CoverStyleDraft,
    CoverStyleProfile,
)
from tubeflow_test.application.cover_project import COVER_PROJECT_NOT_FROZEN
from tubeflow_test.application.cover_prompts import CoverPromptService
from tubeflow_test.application.cover_style import CoverStyleService, CoverStyleView
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.http_range import RangeNotSatisfiable, parse_single_range
from tubeflow_test.identity_project.domain import Account

_COVER_UPLOAD_LIMIT = 10 * 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftInput(StrictModel):
    ref_image_id: UUID | None = None
    dims: list[str] = Field(default_factory=list)
    edited_text: str = ""


class SelectStyleProfileInput(StrictModel):
    profile_id: UUID


class PromoteProfileInput(StrictModel):
    label: str | None = None


class CharacterCardInput(StrictModel):
    character_image_id: UUID
    identity_text: str = ""
    appearance_text: str = ""


class PromptCreateInput(StrictModel):
    brief: str = ""
    style_override_text: str | None = None
    style_source: str | None = None
    style_profile_id: UUID | None = None
    cover_text: str | None = None
    character_card_ids: list[UUID] | None = None


class SelectPromptVersionInput(StrictModel):
    version_id: UUID


class PromptPatchInput(StrictModel):
    prompt_current: str | None = None
    reset: bool = False
    cover_text_choice: str | None = None

    @model_validator(mode="after")
    def exactly_one_operation(self) -> PromptPatchInput:
        operations = (
            (self.prompt_current is not None) + self.reset + (self.cover_text_choice is not None)
        )
        if operations != 1:
            raise ValueError(
                "Exactly one of prompt_current, reset, or cover_text_choice is required"
            )
        return self


class SelectImageInput(StrictModel):
    generation_id: UUID


class AnalyzeStyleInput(StrictModel):
    force: bool = False


def _not_found_error() -> DomainError:
    return DomainError("not_found", "Cover resource was not found")


def _cover_public_state(
    raw_status: object, *, success_actions: tuple[str, ...] = ()
) -> PublicState:
    if str(raw_status) == COVER_STATUS_INTENT_PERSISTED:
        return PublicState(PublicStatus.QUEUED, "Waiting to start.", ("REFRESH",))
    return public_state(raw_status, success_actions=success_actions)


def _ref_image_view(image: CoverRefImage) -> dict[str, object]:
    return {
        "ref_image_id": image.id,
        "scope": image.scope,
        "sha256": image.sha256,
        "image_format": image.image_format,
        "byte_size": image.byte_size,
        "width": image.width,
        "height": image.height,
        "created_at": image.created_at,
        **public_state("SUCCEEDED", success_actions=("SAVE_DRAFT", "ANALYZE_REF_IMAGE")).as_dict(),
    }


def _draft_view(draft: CoverStyleDraft) -> dict[str, object]:
    return {
        "ref_image_id": draft.ref_image_id,
        "dims": dict(draft.dims),
        "analysis_dims": dict(draft.analysis_dims),
        "edited_text": draft.edited_text,
        "ref_image_sha256": draft.ref_image_sha256 or None,
        "has_analysis": bool(draft.ref_image_sha256),
        "updated_at": draft.updated_at,
    }


def _profile_view(profile: CoverStyleProfile, *, is_current: bool) -> dict[str, object]:
    return {
        "profile_id": profile.id,
        "version": profile.version,
        "ref_image_id": profile.ref_image_id,
        "dims": dict(profile.dims),
        "edited_text": profile.edited_text,
        "label": profile.label,
        "supersedes_id": profile.supersedes_id,
        "created_at": profile.created_at,
        "is_current": is_current,
        **public_state("SUCCEEDED", success_actions=("SELECT_CURRENT",)).as_dict(),
    }


def _character_card_view(card: CoverCharacterCard) -> dict[str, object]:
    return {
        "card_id": card.id,
        "channel_id": card.channel_id,
        "character_image_id": card.character_image_id,
        "identity_text": card.identity_text,
        "appearance_text": card.appearance_text,
        "source": card.source,
        "character_media_url": (
            f"/identity/channels/{card.channel_id}/character-images/"
            f"{card.character_image_id}/content"
        ),
        "created_at": card.created_at,
        "updated_at": card.updated_at,
        **public_state(
            "SUCCEEDED", success_actions=("EDIT_CHARACTER_CARD", "ANALYZE_CHARACTER_CARD")
        ).as_dict(),
    }


def _style_view(view: CoverStyleView) -> dict[str, object]:
    current = view.current_profile
    return {
        "draft": _draft_view(view.draft) if view.draft is not None else None,
        "ref_images": [_ref_image_view(item) for item in view.ref_images],
        "current_profile": (
            _profile_view(current, is_current=True) if current is not None else None
        ),
        "profiles": [
            _profile_view(item, is_current=current is not None and item.id == current.id)
            for item in view.profiles
        ],
        **public_state(
            "SUCCEEDED",
            success_actions=(
                "UPLOAD_REF_IMAGE",
                "SAVE_DRAFT",
                "ANALYZE_REF_IMAGE",
                "PROMOTE_CHANNEL_STYLE",
            ),
        ).as_dict(),
    }


def _prompt_view(generation: CoverPromptGeneration, *, is_current: bool) -> dict[str, object]:
    return {
        "generation_id": generation.id,
        "version": generation.version,
        "status": generation.status,
        "style_source": generation.style_source,
        "channel_style_profile_id": generation.channel_style_profile_id,
        "style_override_text": generation.style_override_text,
        "brief": generation.brief,
        "cover_text_candidates": [dict(item) for item in generation.cover_text_candidates],
        "cover_text_default": generation.cover_text_default,
        "cover_text_used": generation.cover_text_used,
        "prompt_body": generation.prompt_body,
        "prompt_system": generation.prompt_system,
        "prompt_current": generation.prompt_current,
        "title_used": generation.title_used,
        "script_sha256": generation.script_sha256,
        "warnings": list(generation.warnings),
        "judge": generation.judge,
        "model": generation.model,
        "parameters": dict(generation.parameters),
        "character_cards_used": [dict(item) for item in generation.character_cards_used],
        "failure": {"stage": generation.failure_stage, "code": generation.failure_code}
        if generation.failure_stage or generation.failure_code
        else None,
        "created_at": generation.created_at,
        "is_current": is_current,
        **_cover_public_state(
            generation.status,
            success_actions=("VIEW", "EDIT_PROMPT", "GENERATE_COVER_IMAGE"),
        ).as_dict(),
    }


def _image_view(generation: CoverImageGeneration, *, is_current: bool) -> dict[str, object]:
    succeeded = generation.status == COVER_STATUS_SUCCEEDED
    return {
        "generation_id": generation.id,
        "version": generation.version,
        "status": generation.status,
        "source": generation.source,
        "prompt_generation_id": generation.prompt_generation_id,
        "model": generation.model,
        "size": generation.size,
        "quality": generation.quality,
        "canonical_request_sha256": generation.canonical_request_sha256,
        "media_url": (
            f"/projects/{generation.project_id}/cover/images/{generation.id}/media"
            if succeeded
            else None
        ),
        "width": generation.qc_width,
        "height": generation.qc_height,
        "sha256": generation.object_sha256,
        "size_bytes": generation.downloaded_size,
        # 公开视图不携带 Provider 回执（与既有不暴露回执的边界一致）。
        "visual_review": (
            {key: value for key, value in generation.visual_review.items() if key != "receipt"}
            if generation.visual_review is not None
            else None
        ),
        "failure": {"stage": generation.failure_stage, "code": generation.failure_code}
        if generation.failure_stage or generation.failure_code
        else None,
        "created_at": generation.created_at,
        "is_current": is_current,
        **_cover_public_state(
            generation.status, success_actions=("VIEW_MEDIA", "SELECT_CURRENT")
        ).as_dict(),
    }


def _immutable_media_response(
    content: bytes, media_type: str, digest: str, request: Request
) -> Response:
    size = len(content)
    etag = f'"{digest}"'
    protected_cache = "private, no-cache"
    try:
        selected = parse_single_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": protected_cache,
                "Content-Range": f"bytes */{size}",
                "ETag": etag,
            },
        )
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"Accept-Ranges": "bytes", "Cache-Control": protected_cache, "ETag": etag},
        )
    payload = content
    response_status = status.HTTP_200_OK
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": protected_cache,
        "Content-Length": str(size),
        "ETag": etag,
    }
    if selected is not None:
        payload = content[selected.start : selected.end + 1]
        response_status = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Length"] = str(selected.length)
        headers["Content-Range"] = f"bytes {selected.start}-{selected.end}/{size}"
    return Response(
        b"" if request.method == "HEAD" else payload,
        status_code=response_status,
        media_type=media_type,
        headers=headers,
    )


def create_cover_router(
    style: CoverStyleService,
    prompts: CoverPromptService,
    images: CoverImageService,
    current_account: Callable[[], Account] | None = None,
    *,
    characters: CoverCharacterService,
    media_account: Callable[[], Account] | None = None,
) -> APIRouter:
    if current_account is None:

        def session_not_wired() -> Account:
            raise HTTPException(
                401, detail={"code": "invalid_session", "message": "Session is invalid"}
            )

        current_account = session_not_wired
    media_account = media_account or current_account
    router = APIRouter(tags=["cover"])

    def call[T](operation: Callable[[], T]) -> T:
        try:
            return operation()
        except DomainError as exc:
            mapped = (
                404
                if exc.code == "not_found"
                else 409
                if exc.code
                in {
                    "revision_conflict",
                    "idempotency_conflict",
                    "project_purging",
                    COVER_PROJECT_NOT_FROZEN,
                }
                else 502
                if exc.code == "storage_integrity_error"
                else 422
            )
            raise HTTPException(mapped, detail={"code": exc.code, "message": exc.message}) from exc
        except ValueError as exc:
            raise HTTPException(
                422, detail={"code": "invalid_request", "message": str(exc)}
            ) from exc

    def current_prompt_generation(account: Account, project_id: UUID) -> CoverPromptGeneration:
        current, _versions = prompts.view(account.id, project_id)
        if current is None:
            raise _not_found_error()
        return current

    @router.post("/identity/channels/{channel_id}/cover-ref-images", status_code=201)
    async def upload_channel_ref_image(
        channel_id: UUID,
        file: UploadFile = File(...),
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        content = await file.read(_COVER_UPLOAD_LIMIT + 1)
        return _ref_image_view(
            call(
                lambda: style.upload_ref_image(
                    account.id,
                    scope=COVER_SCOPE_CHANNEL,
                    channel_id=channel_id,
                    filename=file.filename or "",
                    content=content,
                )
            )
        )

    @router.head(
        "/identity/channels/{channel_id}/cover-ref-images/{image_id}/media",
        include_in_schema=False,
    )
    @router.get("/identity/channels/{channel_id}/cover-ref-images/{image_id}/media")
    def channel_ref_image_media(
        channel_id: UUID,
        image_id: UUID,
        request: Request,
        account: Account = Depends(media_account),
    ) -> Response:
        content, media_type, digest = call(
            lambda: style.ref_image_media_bytes(
                account.id, image_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
            )
        )
        return _immutable_media_response(content, media_type, digest, request)

    @router.get("/identity/channels/{channel_id}/cover-style")
    def channel_cover_style(
        channel_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        return _style_view(
            call(lambda: style.view(account.id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id))
        )

    @router.put("/identity/channels/{channel_id}/cover-style/draft")
    def save_channel_draft(
        channel_id: UUID,
        body: DraftInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        draft = call(
            lambda: style.save_draft(
                account.id,
                scope=COVER_SCOPE_CHANNEL,
                channel_id=channel_id,
                ref_image_id=body.ref_image_id,
                dims=body.dims,
                edited_text=body.edited_text,
            )
        )
        return {
            "draft": _draft_view(draft),
            **public_state(
                "SUCCEEDED", success_actions=("ANALYZE_REF_IMAGE", "PROMOTE_CHANNEL_STYLE")
            ).as_dict(),
        }

    @router.post("/identity/channels/{channel_id}/cover-style/analyze", status_code=202)
    def analyze_channel_draft(
        channel_id: UUID,
        body: AnalyzeStyleInput | None = None,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        intent = call(
            lambda: style.create_analysis_intent(
                account.id,
                scope=COVER_SCOPE_CHANNEL,
                channel_id=channel_id,
                force=bool(body and body.force),
            )
        )
        state = (
            PublicState(PublicStatus.SUCCEEDED, "Reused the existing analysis.", ("REFRESH",))
            if intent.reused
            else PublicState(PublicStatus.QUEUED, "Waiting to start.", ("REFRESH",))
        )
        return {
            "enqueued": intent.enqueued,
            "reused": intent.reused,
            "draft": _draft_view(intent.draft),
            **state.as_dict(),
        }

    @router.post("/identity/channels/{channel_id}/cover-style/profiles", status_code=201)
    def promote_channel_draft(
        channel_id: UUID,
        body: PromoteProfileInput | None = None,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        profile = call(
            lambda: style.promote_to_channel(
                account.id, channel_id, label=body.label if body is not None else None
            )
        )
        return _profile_view(profile, is_current=True)

    @router.post("/identity/channels/{channel_id}/cover-style/current")
    def select_channel_current_profile(
        channel_id: UUID,
        body: SelectStyleProfileInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        profile = call(lambda: style.set_channel_current(account.id, channel_id, body.profile_id))
        return _profile_view(profile, is_current=True)

    @router.post("/identity/channels/{channel_id}/cover-character-cards")
    def upsert_character_card(
        channel_id: UUID,
        body: CharacterCardInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        card = call(
            lambda: characters.upsert_card(
                account.id,
                channel_id,
                character_image_id=body.character_image_id,
                identity_text=body.identity_text,
                appearance_text=body.appearance_text,
            )
        )
        return _character_card_view(card)

    @router.get("/identity/channels/{channel_id}/cover-character-cards")
    def list_character_cards(
        channel_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        cards = call(lambda: characters.list_cards(account.id, channel_id))
        return {
            "items": [_character_card_view(card) for card in cards],
            **public_state(
                "SUCCEEDED",
                success_actions=("UPSERT_CHARACTER_CARD", "ANALYZE_CHARACTER_CARD"),
            ).as_dict(),
        }

    @router.post(
        "/identity/channels/{channel_id}/cover-character-cards/{card_id}/analyze",
        status_code=202,
    )
    def analyze_character_card(
        channel_id: UUID,
        card_id: UUID,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        intent = call(
            lambda: characters.create_card_analysis_intent(account.id, channel_id, card_id)
        )
        return {
            "enqueued": intent.enqueued,
            "card": _character_card_view(intent.card),
            **PublicState(PublicStatus.QUEUED, "Waiting to start.", ("REFRESH",)).as_dict(),
        }

    @router.post("/projects/{project_id}/cover/ref-images", status_code=201)
    async def upload_project_ref_image(
        project_id: UUID,
        file: UploadFile = File(...),
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        content = await file.read(_COVER_UPLOAD_LIMIT + 1)
        return _ref_image_view(
            call(
                lambda: style.upload_ref_image(
                    account.id,
                    scope=COVER_SCOPE_PROJECT,
                    project_id=project_id,
                    filename=file.filename or "",
                    content=content,
                )
            )
        )

    @router.head(
        "/projects/{project_id}/cover/ref-images/{image_id}/media", include_in_schema=False
    )
    @router.get("/projects/{project_id}/cover/ref-images/{image_id}/media")
    def project_ref_image_media(
        project_id: UUID,
        image_id: UUID,
        request: Request,
        account: Account = Depends(media_account),
    ) -> Response:
        content, media_type, digest = call(
            lambda: style.ref_image_media_bytes(
                account.id, image_id, scope=COVER_SCOPE_PROJECT, project_id=project_id
            )
        )
        return _immutable_media_response(content, media_type, digest, request)

    @router.get("/projects/{project_id}/cover/style")
    def project_cover_style(
        project_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        return _style_view(
            call(lambda: style.view(account.id, scope=COVER_SCOPE_PROJECT, project_id=project_id))
        )

    @router.put("/projects/{project_id}/cover/style/draft")
    def save_project_draft(
        project_id: UUID,
        body: DraftInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        draft = call(
            lambda: style.save_draft(
                account.id,
                scope=COVER_SCOPE_PROJECT,
                project_id=project_id,
                ref_image_id=body.ref_image_id,
                dims=body.dims,
                edited_text=body.edited_text,
            )
        )
        return {
            "draft": _draft_view(draft),
            **public_state(
                "SUCCEEDED", success_actions=("ANALYZE_REF_IMAGE", "PROMOTE_CHANNEL_STYLE")
            ).as_dict(),
        }

    @router.post("/projects/{project_id}/cover/style/analyze", status_code=202)
    def analyze_project_draft(
        project_id: UUID,
        body: AnalyzeStyleInput | None = None,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        intent = call(
            lambda: style.create_analysis_intent(
                account.id,
                scope=COVER_SCOPE_PROJECT,
                project_id=project_id,
                force=bool(body and body.force),
            )
        )
        state = (
            PublicState(PublicStatus.SUCCEEDED, "Reused the existing analysis.", ("REFRESH",))
            if intent.reused
            else PublicState(PublicStatus.QUEUED, "Waiting to start.", ("REFRESH",))
        )
        return {
            "enqueued": intent.enqueued,
            "reused": intent.reused,
            "draft": _draft_view(intent.draft),
            **state.as_dict(),
        }

    @router.post("/projects/{project_id}/cover/style/promote", status_code=201)
    def promote_project_draft(
        project_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        profile = call(lambda: style.promote_project_draft(account.id, project_id))
        return _profile_view(profile, is_current=True)

    @router.post("/projects/{project_id}/cover/prompts", status_code=202)
    def create_prompt_generation(
        project_id: UUID,
        body: PromptCreateInput,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        generation = call(
            lambda: prompts.create(
                account.id,
                project_id,
                brief=body.brief,
                style_override_text=body.style_override_text,
                style_source=body.style_source,
                style_profile_id=body.style_profile_id,
                cover_text=body.cover_text,
                character_card_ids=body.character_card_ids,
                idempotency_key=idempotency_key,
            )
        )
        current, _versions = call(lambda: prompts.view(account.id, project_id))
        return _prompt_view(
            generation,
            is_current=current is not None and current.id == generation.id,
        )

    @router.get("/projects/{project_id}/cover/prompts")
    def prompt_generations(
        project_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        current, versions = call(lambda: prompts.view(account.id, project_id))
        return {
            "current": (_prompt_view(current, is_current=True) if current is not None else None),
            "items": [
                _prompt_view(item, is_current=current is not None and item.id == current.id)
                for item in versions
            ],
            **public_state("SUCCEEDED", success_actions=("GENERATE_COVER_PROMPT",)).as_dict(),
        }

    @router.post("/projects/{project_id}/cover/prompts/current")
    def select_current_prompt(
        project_id: UUID,
        body: SelectPromptVersionInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        generation = call(lambda: prompts.set_current(account.id, project_id, body.version_id))
        return _prompt_view(generation, is_current=True)

    @router.patch("/projects/{project_id}/cover/prompts/current")
    def patch_current_prompt(
        project_id: UUID,
        body: PromptPatchInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        current = call(lambda: current_prompt_generation(account, project_id))
        if body.reset:
            updated = call(lambda: prompts.reset_to_system(account.id, current.id))
        elif body.cover_text_choice is not None:
            updated = call(
                lambda: prompts.choose_cover_text(
                    account.id, current.id, body.cover_text_choice or ""
                )
            )
        else:
            updated = call(
                lambda: prompts.edit_prompt_current(
                    account.id, current.id, body.prompt_current or ""
                )
            )
        return _prompt_view(updated, is_current=True)

    @router.post("/projects/{project_id}/cover/images", status_code=202)
    def create_image_generation(
        project_id: UUID,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        generation = call(
            lambda: images.create(account.id, project_id, idempotency_key=idempotency_key)
        )
        current, _versions = call(lambda: images.view(account.id, project_id))
        return _image_view(
            generation,
            is_current=current is not None and current.id == generation.id,
        )

    @router.post("/projects/{project_id}/cover/images/uploads", status_code=201)
    async def upload_cover_image(
        project_id: UUID,
        response: Response,
        file: UploadFile = File(...),
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        """用户直接上传封面图并成为不可变版本（同步，无模型调用）。

        不限宽高比与最小尺寸（2026-11-05 修订）；仅限 png/jpeg/webp、≤25MB、
        ≤4000 万像素。新版本 201；同项目同字节重复上传 200 + ``reused=true``
        （仍切为当前）。
        """
        content = await file.read(COVER_IMAGE_UPLOAD_MAX_BYTES + 1)
        generation, reused = call(
            lambda: images.upload(
                account.id,
                project_id,
                filename=file.filename or "",
                content=content,
            )
        )
        current, _versions = call(lambda: images.view(account.id, project_id))
        response.status_code = status.HTTP_200_OK if reused else status.HTTP_201_CREATED
        return {
            **_image_view(
                generation,
                is_current=current is not None and current.id == generation.id,
            ),
            "reused": reused,
        }

    @router.get("/projects/{project_id}/cover/images")
    def image_generations(
        project_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        current, versions = call(lambda: images.view(account.id, project_id))
        return {
            "current": (_image_view(current, is_current=True) if current is not None else None),
            "items": [
                _image_view(item, is_current=current is not None and item.id == current.id)
                for item in versions
            ],
            **public_state(
                "SUCCEEDED",
                success_actions=("GENERATE_COVER_IMAGE", "UPLOAD_COVER_IMAGE"),
            ).as_dict(),
        }

    @router.post("/projects/{project_id}/cover/images/current")
    def select_current_image(
        project_id: UUID,
        body: SelectImageInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        generation = call(lambda: images.set_current(account.id, project_id, body.generation_id))
        return _image_view(generation, is_current=True)

    @router.head(
        "/projects/{project_id}/cover/images/{generation_id}/media", include_in_schema=False
    )
    @router.get("/projects/{project_id}/cover/images/{generation_id}/media")
    def image_media(
        project_id: UUID,
        generation_id: UUID,
        request: Request,
        account: Account = Depends(media_account),
    ) -> Response:
        content, media_type, digest = call(
            lambda: images.media_bytes(account.id, project_id, generation_id)
        )
        return _immutable_media_response(content, media_type, digest, request)

    return router
