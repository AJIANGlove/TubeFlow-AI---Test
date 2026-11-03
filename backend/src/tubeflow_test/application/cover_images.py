"""封面图服务：出图意图（幂等）与生成 → QC → 对象存储归档 → 终态编排。

照 `application/storyboards.py` 的意图/幂等/终态与 `storyboards.resume` 的
下载/QC/归档链精简为同步单趟（封面出图是同步请求-响应，无远端任务对账）：

- create：取当前提示词版本 prompt_current（trim、非空、≤PROMPT_MAX），幂等键
  = owner 域 sha256 provider 键，canonical_request_sha256 绑定
  提示词版本/文本/模型/尺寸/质量；同事务写 outbox（由仓储完成）；
- generate（CoverImageGenerationWorkflow 活动内执行）：Provider.generate →
  PillowCoverImageQc（可解码/2560x1440/格式/哈希）→ 确定性对象键条件写归档 →
  complete_image_generation_and_switch_current（此前无当前才自动切）→ 终态；
- DomainError 落 FAILED + 稳定失败码（不自动重提，用户再次点击即新授权）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

from tubeflow_test.application.cover_ports import (
    COVER_IMAGE_SOURCE_UPLOAD,
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    COVER_TERMINAL_STATUSES,
    CoverImageGeneration,
    CoverPromptGeneration,
    CoverRepository,
)
from tubeflow_test.application.cover_project import (
    COVER_PROJECT_NOT_FROZEN,
    CoverProjectContextPort,
)
from tubeflow_test.application.cover_prompts import validate_prompt_text
from tubeflow_test.application.cover_providers import (
    CoverAnchorImageProvider,
    CoverImageProvider,
    CoverImageQc,
    CoverImageReviewProvider,
)
from tubeflow_test.application.cover_style import CoverObjectStoragePort
from tubeflow_test.application.ports import Clock, IdentifierFactory
from tubeflow_test.domain.cover import (
    COVER_IMAGE_REVIEW_THUMBNAIL_HEIGHT,
    COVER_IMAGE_REVIEW_THUMBNAIL_WIDTH,
    build_cover_image_review_messages,
    validate_cover_image_review,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.images import verify_character_image

# 用户直接上传封面版本（2026-10-22）：与 AI 出图共用不可变版本序列。
# 原图字节不改写（不重编码、不剥 EXIF）；导出仍按 qc_mime 命名 cover.{ext}。
COVER_IMAGE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
COVER_IMAGE_UPLOAD_MAX_PIXELS = 40_000_000
COVER_IMAGE_UPLOAD_MIN_WIDTH = 1280
COVER_IMAGE_UPLOAD_MIN_HEIGHT = 720


def _not_found() -> DomainError:
    return DomainError("not_found", "Cover resource was not found")


def canonical_cover_image_request_sha256(
    *,
    owner_id: UUID,
    project_id: UUID,
    prompt_generation_id: UUID,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    provider: str = "",
    reference_sha256: tuple[str, ...] = (),
) -> str:
    body: dict[str, Any] = {
        "owner_id": str(owner_id),
        "project_id": str(project_id),
        "prompt_generation_id": str(prompt_generation_id),
        "prompt": prompt,
        "model": model,
        "size": size,
        "quality": quality,
    }
    # v6：锚图路径才追加通道与参考图哈希；文本路径与既有哈希逐字节一致。
    if provider:
        body["provider"] = provider
        body["reference_sha256"] = list(reference_sha256)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class CoverImageService:
    def __init__(
        self,
        repo: CoverRepository,
        projects: CoverProjectContextPort,
        provider: CoverImageProvider,
        qc: CoverImageQc,
        storage: CoverObjectStoragePort,
        ids: IdentifierFactory,
        clock: Clock,
        work_dir: Path,
        *,
        model: str,
        size: str,
        quality: str,
        review: CoverImageReviewProvider | None = None,
        review_enabled: bool = True,
        anchor_provider: CoverAnchorImageProvider | None = None,
        anchor_enabled: bool = True,
        anchor_model: str = "nano-banana-pro",
    ) -> None:
        self._repo, self._projects, self._provider = repo, projects, provider
        self._qc, self._storage = qc, storage
        self._ids, self._clock, self._work_dir = ids, clock, work_dir
        self._model, self._size, self._quality = model, size, quality
        self._review, self._review_enabled = review, review_enabled
        self._anchor_provider = anchor_provider
        self._anchor_enabled = anchor_enabled
        self._anchor_model = anchor_model

    def _resolve_anchor_references(
        self, owner_id: UUID, current: CoverPromptGeneration
    ) -> list[dict[str, Any]]:
        """锚图参考解析（§18.13①）：风格档案/项目草稿带参考图时返回对象事实清单。

        参考图对象不可变，create 解析一次写入 parameters，generate 直接按
        存储的对象事实取字节（重放/重启不再重新解析，canonical 稳定）。
        """
        if not self._anchor_enabled:
            return []
        ref_image_id: UUID | None = None
        if current.channel_style_profile_id is not None:
            profile = self._repo.get_style_profile(owner_id, current.channel_style_profile_id)
            ref_image_id = profile.ref_image_id if profile is not None else None
        elif current.style_source == "project_draft":
            draft = self._repo.get_style_draft_for_project(owner_id, current.project_id)
            ref_image_id = draft.ref_image_id if draft is not None else None
        if ref_image_id is None:
            return []
        image = self._repo.get_ref_image(owner_id, ref_image_id)
        if image is None:
            return []
        return [
            {
                "object_key": image.object_key,
                "sha256": image.sha256,
                "byte_size": image.byte_size,
            }
        ]

    def create(
        self, owner_id: UUID, project_id: UUID, *, idempotency_key: str
    ) -> CoverImageGeneration:
        context = self._projects.project_cover_context(owner_id, project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            raise DomainError(
                COVER_PROJECT_NOT_FROZEN, "Cover actions require a frozen project snapshot"
            )
        current = self._repo.current_prompt_generation_for_project(owner_id, project_id)
        if current is None:
            raise DomainError(
                "cover_prompt_missing", "Generate a cover prompt before generating an image"
            )
        prompt = validate_prompt_text(current.prompt_current)
        # v6 锚图路由（§18.13①）：当前提示词所用风格档案带参考图 → 锚图通道。
        references = self._resolve_anchor_references(owner_id, current)
        use_anchor = bool(references)
        if use_anchor and self._anchor_provider is None:
            raise DomainError(
                "cover_image_anchor_not_configured", "封面锚图出图服务未配置。"
            )
        provider_kind = "grsai-anchor" if use_anchor else "lingkeai"
        model = self._anchor_model if use_anchor else self._model
        canonical = canonical_cover_image_request_sha256(
            owner_id=owner_id,
            project_id=project_id,
            prompt_generation_id=current.id,
            prompt=prompt,
            model=model,
            size=self._size,
            quality=self._quality,
            provider=provider_kind if use_anchor else "",
            reference_sha256=tuple(str(ref["sha256"]) for ref in references),
        )
        provider_key = hashlib.sha256(f"{owner_id}:{idempotency_key}".encode()).hexdigest()
        parameters = (
            {"provider": provider_kind, "reference_images": references} if use_anchor else {}
        )
        generation = CoverImageGeneration(
            self._ids.new(),
            owner_id,
            project_id,
            current.id,
            0,
            prompt,
            model,
            self._size,
            self._quality,
            parameters,
            idempotency_key,
            provider_key,
            canonical,
            COVER_STATUS_INTENT_PERSISTED,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            self._clock.now(),
        )
        return self._repo.create_image_generation(generation)

    def upload(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        filename: str,
        content: bytes,
    ) -> tuple[CoverImageGeneration, bool]:
        """把用户上传的图片落为不可变封面版本（同步、无模型调用、无 outbox）。

        校验顺序：项目存在/冻结 → 字节与像素上限 → 格式 → 最小尺寸 → 16:9±1%。
        全部校验在写对象之前完成，422 不产生对象、不产生版本行。
        同项目同字节重复上传返回既有版本并设为当前（reused=True）。
        """
        context = self._projects.project_cover_context(owner_id, project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            raise DomainError(
                COVER_PROJECT_NOT_FROZEN, "Cover actions require a frozen project snapshot"
            )
        verified = verify_character_image(
            content,
            max_bytes=COVER_IMAGE_UPLOAD_MAX_BYTES,
            max_pixels=COVER_IMAGE_UPLOAD_MAX_PIXELS,
        )
        if (
            verified.width < COVER_IMAGE_UPLOAD_MIN_WIDTH
            or verified.height < COVER_IMAGE_UPLOAD_MIN_HEIGHT
        ):
            raise DomainError(
                "invalid_image_dimensions",
                f"Cover image must be at least {COVER_IMAGE_UPLOAD_MIN_WIDTH}x"
                f"{COVER_IMAGE_UPLOAD_MIN_HEIGHT}",
            )
        # 16:9 ±1%（整数运算，避免浮点边界误差）：|9w-16h| * 100 <= 16h。
        if abs(9 * verified.width - 16 * verified.height) * 100 > 16 * verified.height:
            raise DomainError(
                "invalid_image_aspect_ratio", "Cover image must use a 16:9 aspect ratio"
            )
        object_key = f"cover-images/uploads/{owner_id}/{project_id}/{verified.sha256}"
        self._storage.put_at_key(
            object_key, verified.content, verified.media_type, verified.sha256
        )
        generation = CoverImageGeneration(
            id=self._ids.new(),
            owner_id=owner_id,
            project_id=project_id,
            prompt_generation_id=None,
            version=0,
            prompt="",
            model=None,
            size=None,
            quality=None,
            parameters={"source_filename": filename},
            idempotency_key=f"upload:{project_id}:{verified.sha256}",
            provider_idempotency_key="",
            canonical_request_sha256=verified.sha256,
            status=COVER_STATUS_SUCCEEDED,
            provider_receipt=None,
            expected_sha256=None,
            downloaded_sha256=None,
            downloaded_size=len(verified.content),
            qc_width=verified.width,
            qc_height=verified.height,
            qc_mime=verified.media_type,
            object_key=object_key,
            object_sha256=verified.sha256,
            failure_stage=None,
            failure_code=None,
            created_at=self._clock.now(),
            visual_review=None,
            source=COVER_IMAGE_SOURCE_UPLOAD,
        )
        return self._repo.create_uploaded_image_generation(generation)

    def generate(self, owner_id: UUID, generation_id: UUID) -> CoverImageGeneration:
        """出图编排（活动内执行）；DomainError 落 FAILED 终态，其余异常交由 Temporal 重试。"""
        generation = self._repo.get_image_generation(owner_id, generation_id)
        if generation is None:
            raise _not_found()
        if generation.status in COVER_TERMINAL_STATUSES:
            return generation
        context = self._projects.project_cover_context(owner_id, generation.project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            return self._repo.mark_image_generation_failed(
                owner_id, generation_id, "generate", COVER_PROJECT_NOT_FROZEN
            )
        use_anchor = generation.parameters.get("provider") == "grsai-anchor"
        if use_anchor:
            # 锚图通道（§18.13①）：参考图字节按 parameters 中的对象事实读取。
            if self._anchor_provider is None:
                return self._repo.mark_image_generation_failed(
                    owner_id, generation_id, "generate", "cover_image_anchor_not_configured"
                )
            try:
                references = tuple(
                    self._storage.get_verified(
                        str(ref["object_key"]), str(ref["sha256"]), int(ref["byte_size"])
                    )
                    for ref in generation.parameters.get("reference_images") or []
                )
            except (KeyError, TypeError, ValueError) as exc:
                return self._repo.mark_image_generation_failed(
                    owner_id, generation_id, "generate", type(exc).__name__
                )
            try:
                result = self._anchor_provider.generate(
                    prompt=generation.prompt, references=references
                )
            except DomainError as exc:
                return self._repo.mark_image_generation_failed(
                    owner_id, generation_id, "generate", exc.code
                )
        else:
            try:
                result = self._provider.generate(prompt=generation.prompt)
            except DomainError as exc:
                return self._repo.mark_image_generation_failed(
                    owner_id, generation_id, "generate", exc.code
                )
        path = self._path(generation)
        self._write_atomic(path, result.content)
        try:
            if use_anchor:
                # 锚图通道（§18.13②）：先校验原始下载字节，再归一化到 2560x1440。
                raw_digest, digest, width, height, mime, byte_size = (
                    self._qc.inspect_and_normalize(path, result.sha256)
                )
            else:
                digest, width, height, mime, byte_size = self._qc.inspect(path, result.sha256)
                raw_digest = digest
        except DomainError as exc:
            self._cleanup_temp(generation)
            return self._repo.mark_image_generation_failed(
                owner_id, generation_id, "qc", exc.code
            )
        object_key = f"cover-images/{owner_id}/{generation_id}"
        try:
            self._storage.put_at_key(object_key, path.read_bytes(), mime, digest)
        except (OSError, ValueError) as exc:
            self._cleanup_temp(generation)
            return self._repo.mark_image_generation_failed(
                owner_id, generation_id, "object_write", type(exc).__name__
            )
        visual_review = None
        if self._review_enabled and self._review is not None:
            visual_review = self._run_review(generation, path)
        completed = self._repo.complete_image_generation_and_switch_current(
            replace(
                generation,
                status=COVER_STATUS_SUCCEEDED,
                provider_receipt=result.receipt,
                expected_sha256=result.sha256,
                # 锚图路径：原始下载哈希与归一化归档哈希分别留痕（§18.13②）。
                downloaded_sha256=raw_digest,
                downloaded_size=byte_size,
                qc_width=width,
                qc_height=height,
                qc_mime=mime,
                object_key=object_key,
                object_sha256=digest,
                visual_review=visual_review,
            ),
            switch_current=(
                self._repo.current_image_generation_for_project(
                    owner_id, generation.project_id
                )
                is None
            ),
        )
        self._cleanup_temp(completed)
        return completed

    def _run_review(self, generation: CoverImageGeneration, path: Path) -> dict[str, Any]:
        """出图后视觉复审（只呈现不阻断）。

        复审任何异常（含缩略图/消息/传输/校验）就地落 {"status":"failed"}，
        绝不上抛：活动内非 DomainError 会触发 Temporal 重试，若在出图成功后
        抛出将导致重复出图重复计额。复审失败不影响出图成功与当前指针切换。
        """
        try:
            source = (
                self._repo.get_prompt_generation(
                    generation.owner_id, generation.prompt_generation_id
                )
                if generation.prompt_generation_id is not None
                else None
            )
            thumbnail = self._qc.make_thumbnail(
                path,
                width=COVER_IMAGE_REVIEW_THUMBNAIL_WIDTH,
                height=COVER_IMAGE_REVIEW_THUMBNAIL_HEIGHT,
            )
            messages = build_cover_image_review_messages(
                title=source.title_used if source is not None else "",
                cover_text=source.cover_text_used if source is not None else "",
                style_text=source.style_text_used if source is not None else "",
                image_base64=base64.b64encode(path.read_bytes()).decode("ascii"),
                thumbnail_base64=base64.b64encode(thumbnail).decode("ascii"),
            )
            assert self._review is not None
            result = self._review.review(messages)
            validated = validate_cover_image_review(result.payload)
            return {
                "status": "succeeded",
                **validated,
                "model": result.model,
                "receipt": result.receipt,
            }
        except DomainError as exc:
            return {"status": "failed", "code": exc.code}
        except Exception:
            return {"status": "failed", "code": "cover_image_review_failed"}

    def view(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverImageGeneration | None, tuple[CoverImageGeneration, ...]]:
        context = self._projects.project_cover_context(owner_id, project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            raise DomainError(
                COVER_PROJECT_NOT_FROZEN, "Cover actions require a frozen project snapshot"
            )
        return (
            self._repo.current_image_generation_for_project(owner_id, project_id),
            self._repo.list_image_generations_for_project(owner_id, project_id),
        )

    def set_current(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverImageGeneration:
        """用户明确切换当前封面图（候选须 SUCCEEDED，由仓储校验）。"""
        context = self._projects.project_cover_context(owner_id, project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            raise DomainError(
                COVER_PROJECT_NOT_FROZEN, "Cover actions require a frozen project snapshot"
            )
        return self._repo.set_current_image_generation(owner_id, project_id, generation_id)

    def media_bytes(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> tuple[bytes, str, str]:
        """受保护媒体读取：属主 + 项目版本绑定 + SUCCEEDED + 对象完整性校验。

        返回 (字节, 媒体类型, 内容摘要)；供媒体 Cookie/Range 端点使用。
        """
        generation = self._repo.get_image_generation(owner_id, generation_id)
        if (
            generation is None
            or generation.project_id != project_id
            or generation.status != COVER_STATUS_SUCCEEDED
            or generation.object_key is None
            or generation.object_sha256 is None
            or generation.downloaded_size is None
            or generation.qc_mime is None
        ):
            raise _not_found()
        try:
            content = self._storage.get_verified(
                generation.object_key, generation.object_sha256, generation.downloaded_size
            )
        except ValueError as exc:
            raise DomainError(
                "storage_integrity_error", "Cover image bytes failed integrity check"
            ) from exc
        return content, generation.qc_mime, generation.object_sha256

    def _path(self, generation: CoverImageGeneration) -> Path:
        return (
            self._work_dir
            / str(generation.owner_id)
            / str(generation.id)
            / "cover.image"
        )

    @staticmethod
    def _write_atomic(destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(f"{destination.name}.part")
        try:
            part.write_bytes(content)
            os.replace(part, destination)
        finally:
            part.unlink(missing_ok=True)

    def _cleanup_temp(self, generation: CoverImageGeneration) -> None:
        path = self._path(generation)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
            path.parent.parent.rmdir()
        except OSError:
            pass
