"""封面风格服务：参考图上传、风格草稿、参考图解析编排与频道风格版本。

照 `identity_project/service.py` 的 upload_image 链与
`application/storyboards.py` 的意图/幂等模式：

- 上传/保存草稿是纯本地确定性操作，零 Provider 调用；
- 「解析参考图」是显式动作：缓存命中（草稿 (ref_image_sha256, analysis_dims)
  与当前 (图 sha256, normalize 后 dims) 一致且 edited_text 非空）直接完成、零
  Provider 调用；否则写 outbox 事件（同一 workflow_id 在途去重），由
  CoverRefAnalysisWorkflow 的活动执行 `run_ref_analysis`；
- 解析结果为空 → 稳定失败码 cover_ref_analysis_empty，不静默降级为无参考；
- 「固定为频道风格」把草稿固化为不可变版本并切 channels 指针；旧版本保留可查。

封面二进制与参考图对象键采用裸对象键模式（与 cover 模型 object_key 列匹配）：
确定性键 + PrivateObjectStorage.put_at_key 条件写（幂等、内容寻址）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from tubeflow_test.application.cover_outbox import (
    COVER_AGGREGATE_STYLE_CHANNEL,
    COVER_AGGREGATE_STYLE_PROJECT,
    CoverOutboxRepository,
)
from tubeflow_test.application.cover_ports import (
    COVER_SCOPE_CHANNEL,
    COVER_SCOPE_PROJECT,
    CoverRefImage,
    CoverRepository,
    CoverStyleDraft,
    CoverStyleProfile,
)
from tubeflow_test.application.cover_project import (
    COVER_PROJECT_NOT_FROZEN,
    CoverProjectContextPort,
    dims_from_storage,
    dims_to_storage,
)
from tubeflow_test.application.cover_providers import CoverRefAnalysisProvider
from tubeflow_test.application.ports import Clock, IdentifierFactory
from tubeflow_test.domain.cover import (
    REF_CONSTRAINT_MAX,
    build_ref_analysis_messages,
    normalize_ref_dims,
    parse_ref_text_by_child,
    render_ref_analysis_text,
    validate_ref_analysis,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.images import verify_character_image

COVER_REF_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
COVER_ANALYSIS_REUSED = "REUSED"
COVER_ANALYSIS_SUCCEEDED = "SUCCEEDED"
COVER_STYLE_LABEL_MAX = 60


class CoverObjectStoragePort(Protocol):
    """封面二进制对象存储端口（生产注入 PrivateObjectStorage）。"""

    def put_at_key(
        self, key: str, body: bytes, media_type: str, expected_sha256: str
    ) -> object: ...

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes: ...


def _not_found() -> DomainError:
    return DomainError("not_found", "Cover resource was not found")


@dataclass(frozen=True, slots=True)
class CoverRefAnalysisIntent:
    """解析动作意图结果：缓存命中 reused=True（零 Provider 调用、零事件）。"""

    draft: CoverStyleDraft
    enqueued: bool
    reused: bool
    workflow_id: str


@dataclass(frozen=True, slots=True)
class CoverStyleView:
    """风格面板只读视图；current_profile/profiles 仅频道作用域非空。"""

    draft: CoverStyleDraft | None
    ref_images: tuple[CoverRefImage, ...]
    current_profile: CoverStyleProfile | None
    profiles: tuple[CoverStyleProfile, ...]


class CoverStyleService:
    def __init__(
        self,
        repo: CoverRepository,
        outbox: CoverOutboxRepository,
        projects: CoverProjectContextPort,
        storage: CoverObjectStoragePort,
        analysis: CoverRefAnalysisProvider,
        ids: IdentifierFactory,
        clock: Clock,
    ) -> None:
        self._repo, self._outbox, self._projects = repo, outbox, projects
        self._storage, self._analysis, self._ids, self._clock = storage, analysis, ids, clock

    def upload_ref_image(
        self,
        owner_id: UUID,
        *,
        scope: str,
        channel_id: UUID | None = None,
        project_id: UUID | None = None,
        filename: str = "",
        content: bytes,
    ) -> CoverRefImage:
        """上传参考图：扩展名 + 真实媒体类型 + 可解码 + ≤10MB → 作用域去重 →
        对象存储确定性键条件写 → repo.add_ref_image（作用域存在性与属主由仓储门禁）。"""
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix not in COVER_REF_IMAGE_EXTENSIONS:
            raise DomainError(
                "invalid_image_type", "Only .jpg, .jpeg, .png, and .webp files are supported"
            )
        verified = verify_character_image(content)
        scope_id = self._scope_id(scope, channel_id, project_id)
        if scope == COVER_SCOPE_PROJECT:
            self._require_frozen_project(owner_id, scope_id)
        elif not self._repo.channel_scope_exists(owner_id, scope_id):
            # 授权预检必须先于字节落盘：鉴权失败不得产生孤儿对象字节；
            # 仓储 add_ref_image 的锁内复核仍是最终门禁。
            raise _not_found()
        for item in self._list_ref_images(owner_id, scope, scope_id):
            if item.sha256 == verified.sha256:
                return item
        object_key = f"cover-ref-images/{scope}/{scope_id}/{verified.sha256}"
        self._storage.put_at_key(
            object_key, verified.content, verified.media_type, verified.sha256
        )
        image = CoverRefImage(
            self._ids.new(),
            owner_id,
            scope,
            channel_id if scope == COVER_SCOPE_CHANNEL else None,
            project_id if scope == COVER_SCOPE_PROJECT else None,
            verified.sha256,
            object_key,
            len(verified.content),
            verified.width,
            verified.height,
            verified.media_type.removeprefix("image/").upper(),
            self._clock.now(),
        )
        return self._repo.add_ref_image(image)

    def save_draft(
        self,
        owner_id: UUID,
        *,
        scope: str,
        channel_id: UUID | None = None,
        project_id: UUID | None = None,
        ref_image_id: UUID | None,
        dims: object,
        edited_text: str,
    ) -> CoverStyleDraft:
        """保存勾选/文本：dims 经 normalize_ref_dims 规整、edited_text ≤2000 码点、
        ref_image_id 属主与作用域校验；只写草稿可变字段，不动既有解析结果。"""
        scope_id = self._scope_id(scope, channel_id, project_id)
        if scope == COVER_SCOPE_PROJECT:
            self._require_frozen_project(owner_id, scope_id)
        normalized = normalize_ref_dims(dims)
        text = "" if edited_text is None else str(edited_text)
        if len(text) > REF_CONSTRAINT_MAX:
            raise DomainError(
                "cover_style_text_too_long", "Cover style text exceeds the 2000 code point limit"
            )
        if ref_image_id is not None:
            image = self._repo.get_ref_image(owner_id, ref_image_id)
            if image is None or image.scope != scope or (
                image.channel_id if scope == COVER_SCOPE_CHANNEL else image.project_id
            ) != scope_id:
                raise _not_found()
        existing = self._get_draft(owner_id, scope, scope_id)
        draft = CoverStyleDraft(
            existing.id if existing is not None else self._ids.new(),
            owner_id,
            scope,
            channel_id if scope == COVER_SCOPE_CHANNEL else None,
            project_id if scope == COVER_SCOPE_PROJECT else None,
            ref_image_id,
            dims_to_storage(normalized),
            dict(existing.analysis) if existing is not None else {},
            dict(existing.analysis_dims) if existing is not None else {},
            existing.ref_image_sha256 if existing is not None else "",
            text,
            self._clock.now(),
        )
        return self._repo.upsert_style_draft(draft)

    def create_analysis_intent(
        self,
        owner_id: UUID,
        *,
        scope: str,
        channel_id: UUID | None = None,
        project_id: UUID | None = None,
        force: bool = False,
    ) -> CoverRefAnalysisIntent:
        """「解析参考图」显式动作：缓存命中直接完成；否则写 outbox 事件（在途去重）。

        force=True（「重新解析」）：先清缓存键（ref_image_sha256/analysis_dims），
        缓存判定自然未命中、走正常解析链路；edited_text 保留，新解析成功后才覆盖，
        失败时旧文本不丢失。
        """
        scope_id = self._scope_id(scope, channel_id, project_id)
        if scope == COVER_SCOPE_PROJECT:
            self._require_frozen_project(owner_id, scope_id)
        draft, image, dims = self._analysis_inputs(owner_id, scope, scope_id)
        if force and (draft.ref_image_sha256 or draft.analysis_dims):
            draft = self._repo.upsert_style_draft(
                replace(draft, ref_image_sha256="", analysis_dims={})
            )
        if self._cache_hit(draft, image.sha256, dims):
            return CoverRefAnalysisIntent(draft, enqueued=False, reused=True, workflow_id="")
        request_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "draft_id": str(draft.id),
                    "ref_image_sha256": image.sha256,
                    "dims": dims,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        workflow_id = f"cover-ref-analysis-{owner_id}-{draft.id}-{request_sha256}"
        enqueued = self._outbox.request_ref_analysis(
            event_id=self._ids.new(),
            owner_id=owner_id,
            project_id=scope_id if scope == COVER_SCOPE_PROJECT else None,
            aggregate_type=(
                COVER_AGGREGATE_STYLE_CHANNEL
                if scope == COVER_SCOPE_CHANNEL
                else COVER_AGGREGATE_STYLE_PROJECT
            ),
            aggregate_id=scope_id,
            request_sha256=request_sha256,
            workflow_id=workflow_id,
            now=self._clock.now(),
        )
        return CoverRefAnalysisIntent(
            draft, enqueued=enqueued, reused=False, workflow_id=workflow_id
        )

    def run_ref_analysis(self, owner_id: UUID, scope: str, scope_id: UUID) -> str:
        """解析编排（工作流活动内执行）：缓存复检 → Provider → 校验 → 渲染 → 覆盖草稿。

        渲染文本为空抛 cover_ref_analysis_empty（不静默降级为无参考）；返回
        SUCCEEDED/REUSED 供活动作终态结果。
        """
        draft, image, dims = self._analysis_inputs(owner_id, scope, scope_id)
        if self._cache_hit(draft, image.sha256, dims):
            return COVER_ANALYSIS_REUSED
        try:
            content = self._storage.get_verified(
                image.object_key, image.sha256, image.byte_size
            )
        except ValueError as exc:
            raise DomainError(
                "storage_integrity_error", "Cover reference image bytes failed integrity check"
            ) from exc
        messages = build_ref_analysis_messages(
            {"image_base64": base64.b64encode(content).decode("ascii")}, dims
        )
        result = self._analysis.analyze(messages)
        analysis = validate_ref_analysis(result.payload, dims)
        text = render_ref_analysis_text(analysis)
        if not text.strip():
            raise DomainError(
                "cover_ref_analysis_empty", "参考图解析结果为空，请调整勾选或更换参考图。"
            )
        self._repo.upsert_style_draft(
            replace(
                draft,
                analysis=analysis,
                analysis_dims=dims_to_storage(dims),
                edited_text=text,
                ref_image_sha256=image.sha256,
                updated_at=self._clock.now(),
            )
        )
        return COVER_ANALYSIS_SUCCEEDED

    def promote_to_channel(
        self,
        owner_id: UUID,
        channel_id: UUID,
        *,
        project_id: UUID | None = None,
        label: str | None = None,
    ) -> CoverStyleProfile:
        """把频道/项目草稿固化为频道风格新版本并切 channels 当前指针。

        label（v5 §5）为可选备注名：trim、≤60 码点，随版本不可变保存。
        """
        if project_id is None:
            draft = self._repo.get_style_draft_for_channel(owner_id, channel_id)
        else:
            context = self._projects.project_cover_context(owner_id, project_id)
            if context is None or context.channel_id != channel_id:
                raise _not_found()
            if context.frozen_script is None:
                raise DomainError(
                    COVER_PROJECT_NOT_FROZEN, "Cover actions require a frozen project snapshot"
                )
            draft = self._repo.get_style_draft_for_project(owner_id, project_id)
        if draft is None:
            raise _not_found()
        label_text = ("" if label is None else str(label)).strip()
        if len(label_text) > COVER_STYLE_LABEL_MAX:
            raise DomainError(
                "cover_style_label_too_long",
                "Cover style profile label exceeds the 60 code point limit",
            )
        created = self._repo.create_style_profile(
            CoverStyleProfile(
                self._ids.new(),
                owner_id,
                channel_id,
                0,
                draft.ref_image_id,
                dict(draft.dims),
                dict(draft.analysis_dims),
                draft.edited_text,
                self._clock.now(),
                None,
                label=label_text,
            )
        )
        return self._repo.set_channel_current_style_profile(owner_id, channel_id, created.id)

    def set_channel_current(
        self, owner_id: UUID, channel_id: UUID, profile_id: UUID
    ) -> CoverStyleProfile:
        """切历史版本为频道当前风格（存在性与属主校验由仓储完成）。"""
        return self._repo.set_channel_current_style_profile(owner_id, channel_id, profile_id)

    def promote_project_draft(self, owner_id: UUID, project_id: UUID) -> CoverStyleProfile:
        """把项目草稿固化为所属频道的风格新版本（D13「保存为频道风格」主入口）。"""
        context = self._projects.project_cover_context(owner_id, project_id)
        if context is None:
            raise _not_found()
        return self.promote_to_channel(owner_id, context.channel_id, project_id=project_id)

    def ref_image_media_bytes(
        self,
        owner_id: UUID,
        image_id: UUID,
        *,
        scope: str,
        channel_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> tuple[bytes, str, str]:
        """参考图受保护媒体读取（v5 §2）：属主 + 作用域绑定 + 对象完整性校验。

        返回 (字节, 媒体类型, 内容摘要)；照封面图 media 模式供媒体 Cookie/Range
        端点使用（只读媒体，不做项目冻结门禁）。
        """
        scope_id = self._scope_id(scope, channel_id, project_id)
        image = self._repo.get_ref_image(owner_id, image_id)
        if image is None or image.scope != scope or (
            image.channel_id if scope == COVER_SCOPE_CHANNEL else image.project_id
        ) != scope_id:
            raise _not_found()
        try:
            content = self._storage.get_verified(image.object_key, image.sha256, image.byte_size)
        except ValueError as exc:
            raise DomainError(
                "storage_integrity_error", "Cover reference image bytes failed integrity check"
            ) from exc
        return content, f"image/{image.image_format.lower()}", image.sha256

    def view(
        self,
        owner_id: UUID,
        *,
        scope: str,
        channel_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> CoverStyleView:
        """风格面板只读视图：草稿 + 参考图列表；频道作用域附当前风格版本与版本列表。"""
        scope_id = self._scope_id(scope, channel_id, project_id)
        if scope == COVER_SCOPE_PROJECT:
            self._require_frozen_project(owner_id, scope_id)
            return CoverStyleView(
                self._get_draft(owner_id, scope, scope_id),
                self._list_ref_images(owner_id, scope, scope_id),
                None,
                (),
            )
        if not self._repo.channel_scope_exists(owner_id, scope_id):
            raise _not_found()
        return CoverStyleView(
            self._get_draft(owner_id, scope, scope_id),
            self._list_ref_images(owner_id, scope, scope_id),
            self._repo.current_style_profile_for_channel(owner_id, scope_id),
            self._repo.list_style_profiles_for_channel(owner_id, scope_id),
        )

    def _analysis_inputs(
        self, owner_id: UUID, scope: str, scope_id: UUID
    ) -> tuple[CoverStyleDraft, CoverRefImage, list[str]]:
        draft = self._get_draft(owner_id, scope, scope_id)
        if draft is None or draft.ref_image_id is None:
            raise DomainError(
                "cover_ref_image_missing", "参考图解析需要先在草稿中选择参考图。"
            )
        image = self._repo.get_ref_image(owner_id, draft.ref_image_id)
        if image is None:
            raise DomainError(
                "cover_ref_image_missing", "参考图解析需要先在草稿中选择参考图。"
            )
        dims = normalize_ref_dims(dims_from_storage(draft.dims))
        return draft, image, dims

    @staticmethod
    def _cache_hit(draft: CoverStyleDraft, image_sha256: str, dims: list[str]) -> bool:
        if not (
            draft.ref_image_sha256 == image_sha256
            and normalize_ref_dims(dims_from_storage(draft.analysis_dims)) == dims
            and bool(draft.edited_text.strip())
        ):
            return False
        # 文本必须覆盖当前勾选维度：勾选恰好回到解析时取值、但文本已被收窄/编辑到
        # 不覆盖勾选时复用会产出下游 COVER_REF_TEXT_INVALID 必然拒绝的文本——判未命中。
        return bool(parse_ref_text_by_child(draft.edited_text, dims=dims).get("ok"))

    def _get_draft(self, owner_id: UUID, scope: str, scope_id: UUID) -> CoverStyleDraft | None:
        if scope == COVER_SCOPE_CHANNEL:
            return self._repo.get_style_draft_for_channel(owner_id, scope_id)
        return self._repo.get_style_draft_for_project(owner_id, scope_id)

    def _list_ref_images(
        self, owner_id: UUID, scope: str, scope_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        if scope == COVER_SCOPE_CHANNEL:
            return self._repo.list_ref_images_for_channel(owner_id, scope_id)
        return self._repo.list_ref_images_for_project(owner_id, scope_id)

    def _require_frozen_project(self, owner_id: UUID, project_id: UUID) -> None:
        context = self._projects.project_cover_context(owner_id, project_id)
        if context is None:
            raise _not_found()
        if context.frozen_script is None:
            raise DomainError(
                COVER_PROJECT_NOT_FROZEN, "Cover actions require a frozen project snapshot"
            )

    @staticmethod
    def _scope_id(scope: str, channel_id: UUID | None, project_id: UUID | None) -> UUID:
        if scope == COVER_SCOPE_CHANNEL and channel_id is not None:
            return channel_id
        if scope == COVER_SCOPE_PROJECT and project_id is not None:
            return project_id
        raise DomainError("invalid_cover_scope", "Cover scope is invalid")
