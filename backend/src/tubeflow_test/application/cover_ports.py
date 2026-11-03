"""封面图生成仓储端口：六组实体的记录类型、状态常量与仓储 Protocol。

照 `application/storyboards.py` 的模式：Protocol 定义在应用层，SQL
（`adapters/persistence/cover/repository.py`）与内存
（`adapters/in_memory_cover.py`）两套实现行为一致，由合同测试对齐。

统一约定（与故事板仓储相同）：

- 每次公开变更为一个事务/UoW；owner 维度强隔离；
- 项目作用域的变更要求项目存在且 ACTIVE：缺失报 ``not_found``，
  PURGING 报 ``project_purging``；频道作用域的变更要求频道存在且属主匹配，
  否则报 ``not_found``；
- 幂等键冲突两分支：键绑同一请求指纹返回既有记录，键绑不同请求指纹报
  ``idempotency_conflict``（照故事板/director create 的确切语义）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

COVER_SCOPE_CHANNEL = "channel"
COVER_SCOPE_PROJECT = "project"

COVER_STATUS_INTENT_PERSISTED = "INTENT_PERSISTED"
COVER_STATUS_SUCCEEDED = "SUCCEEDED"
COVER_STATUS_FAILED = "FAILED"
COVER_TERMINAL_STATUSES = frozenset({COVER_STATUS_SUCCEEDED, COVER_STATUS_FAILED})

# 封面图版本来源（迁移 0042）：AI 出图与用户直接上传共用同一不可变版本序列。
COVER_IMAGE_SOURCE_GENERATED = "generated"
COVER_IMAGE_SOURCE_UPLOAD = "upload"


@dataclass(frozen=True, slots=True)
class CoverRefImage:
    """参考图资产（频道或项目作用域）；作用域（channel_id/project_id）+ sha256 去重。"""

    id: UUID
    owner_id: UUID
    scope: str
    channel_id: UUID | None
    project_id: UUID | None
    sha256: str
    object_key: str
    byte_size: int
    width: int
    height: int
    image_format: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CoverStyleDraft:
    """风格工作草稿（每作用域一份，可变）；upsert 只写可变字段并刷新 updated_at。"""

    id: UUID
    owner_id: UUID
    scope: str
    channel_id: UUID | None
    project_id: UUID | None
    ref_image_id: UUID | None
    dims: dict[str, Any]
    analysis: dict[str, Any]
    analysis_dims: dict[str, Any]
    ref_image_sha256: str
    edited_text: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CoverStyleProfile:
    """频道风格不可变版本；version 频道内递增，supersedes_id 链接此前最高版本。"""

    id: UUID
    owner_id: UUID
    channel_id: UUID
    version: int
    ref_image_id: UUID | None
    dims: dict[str, Any]
    analysis_dims: dict[str, Any]
    edited_text: str
    created_at: datetime
    supersedes_id: UUID | None
    # 版本备注名（v5 §5，≤60 码点由应用层校验，默认空串）。
    label: str = ""


@dataclass(frozen=True, slots=True)
class CoverCharacterCard:
    """频道级角色卡（v5 §1.1，可变卡）；每角色图一张，项目删除不清理。"""

    id: UUID
    owner_id: UUID
    channel_id: UUID
    character_image_id: UUID
    identity_text: str
    appearance_text: str
    source: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CoverCharacterImageRef:
    """角色卡引用的频道角色图事实（character_images 只读投影，供归属校验与解析取字节）。"""

    id: UUID
    owner_id: UUID
    channel_id: UUID
    object_key: str
    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class CoverPromptGeneration:
    """封面提示词版本（请求事实不可变；成品字段由 complete 落库）；version 项目内递增。"""

    id: UUID
    owner_id: UUID
    project_id: UUID
    version: int
    style_source: str
    channel_style_profile_id: UUID | None
    style_text_used: str
    style_dims_used: dict[str, Any]
    style_override_text: str | None
    brief: str
    cover_text_candidates: list[dict[str, Any]]
    cover_text_default: str
    cover_text_used: str
    prompt_body: str
    prompt_system: str
    prompt_current: str
    title_used: str
    script_sha256: str
    warnings: list[str]
    judge: dict[str, Any] | None
    model: str
    parameters: dict[str, Any]
    idempotency_key: str
    status: str
    failure_stage: str | None
    failure_code: str | None
    created_at: datetime
    # 生成时绑定的角色卡快照（v5 §1.1 血缘：[{card_id, character_image_id,
    # identity_text, appearance_text}]）；请求事实，create 时落库。
    character_cards_used: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CoverImageGeneration:
    """封面图不可变版本（字段照 StoryboardGeneration 精简）。"""

    id: UUID
    owner_id: UUID
    project_id: UUID
    # 上传版本无提示词血缘（迁移 0042 起可空）；AI 版本恒有。
    prompt_generation_id: UUID | None
    version: int
    prompt: str
    model: str | None
    size: str | None
    quality: str | None
    parameters: dict[str, Any]
    idempotency_key: str
    provider_idempotency_key: str
    canonical_request_sha256: str
    status: str
    provider_receipt: str | None
    expected_sha256: str | None
    downloaded_sha256: str | None
    downloaded_size: int | None
    qc_width: int | None
    qc_height: int | None
    qc_mime: str | None
    object_key: str | None
    object_sha256: str | None
    failure_stage: str | None
    failure_code: str | None
    created_at: datetime
    # 出图后视觉复审（2026-10-15）：{"status":"succeeded","ok","issues","model","receipt"}
    # 或 {"status":"failed","code"}；None = 未复审（开关关闭/旧版本）。只呈现不阻断。
    visual_review: dict[str, Any] | None = None
    # 版本来源（迁移 0042）：generated=AI 出图，upload=用户直接上传。
    source: str = COVER_IMAGE_SOURCE_GENERATED


def cover_prompt_request_fingerprint(value: CoverPromptGeneration) -> str:
    """提示词幂等键的请求指纹：create 时由请求决定的字段的规范化 JSON。

    提示词表无 canonical_request_sha256 专用列，指纹由请求事实字段派生
    （project/style/brief/title/script/model/parameters/character_cards_used）；
    JSON 排序键序列化使 SQL 与内存两实现判定一致。
    """
    payload = {
        "project_id": str(value.project_id),
        "style_source": value.style_source,
        "channel_style_profile_id": (
            str(value.channel_style_profile_id) if value.channel_style_profile_id else None
        ),
        "style_text_used": value.style_text_used,
        "style_dims_used": value.style_dims_used,
        "style_override_text": value.style_override_text,
        "brief": value.brief,
        "title_used": value.title_used,
        "script_sha256": value.script_sha256,
        "model": value.model,
        "parameters": value.parameters,
        "character_cards_used": value.character_cards_used,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


class CoverRepository(Protocol):
    """封面六组实体的仓储合同。"""

    def channel_scope_exists(self, owner_id: UUID, channel_id: UUID) -> bool:
        """频道存在且属主匹配（上传参考图落盘前的授权预检；写路径仍由仓储门禁复核）。"""
        ...

    def add_ref_image(self, image: CoverRefImage) -> CoverRefImage:
        """作用域内 sha256 去重：命中返回既有记录，否则插入并返回入参。"""
        ...

    def get_ref_image(self, owner_id: UUID, image_id: UUID) -> CoverRefImage | None: ...

    def list_ref_images_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        """频道作用域参考图，按 (created_at, id) 升序。"""
        ...

    def list_ref_images_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        """项目作用域参考图，按 (created_at, id) 升序。"""
        ...

    def delete_ref_images_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        """项目删除清理：删除项目作用域参考图并返回被删记录（含 object_key）。

        引用被删图的草稿/频道风格版本的 ref_image_id 置空（照 FK SET NULL 语义）；
        频道作用域数据不受影响。本操作供项目清理流程调用，不做项目状态门禁。
        """
        ...

    def get_style_draft_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> CoverStyleDraft | None: ...

    def get_style_draft_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverStyleDraft | None: ...

    def upsert_style_draft(self, draft: CoverStyleDraft) -> CoverStyleDraft:
        """无草稿则插入（id 入参）；有则仅更新可变字段并刷新 updated_at（保留既有 id）。"""
        ...

    def create_style_profile(self, profile: CoverStyleProfile) -> CoverStyleProfile:
        """频道内 version=max+1、supersedes_id=此前最高版本 id（首版 None）；并发分配冲突
        报 revision_conflict。"""
        ...

    def get_style_profile(self, owner_id: UUID, profile_id: UUID) -> CoverStyleProfile | None: ...

    def list_style_profiles_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverStyleProfile, ...]:
        """版本倒序。"""
        ...

    def current_style_profile_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> CoverStyleProfile | None: ...

    def set_channel_current_style_profile(
        self, owner_id: UUID, channel_id: UUID, profile_id: UUID
    ) -> CoverStyleProfile:
        """写 channels.current_cover_style_profile_id；候选不可用报 invalid_cover_candidate。"""
        ...

    def create_prompt_generation(self, generation: CoverPromptGeneration) -> CoverPromptGeneration:
        """项目内 version=max+1；幂等键冲突按 cover_prompt_request_fingerprint 两分支。"""
        ...

    def get_prompt_generation(
        self, owner_id: UUID, generation_id: UUID
    ) -> CoverPromptGeneration | None: ...

    def list_prompt_generations_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverPromptGeneration, ...]:
        """版本倒序。"""
        ...

    def current_prompt_generation_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverPromptGeneration | None: ...

    def update_prompt_current(
        self, owner_id: UUID, generation_id: UUID, prompt_current: str
    ) -> CoverPromptGeneration:
        """编辑面板文本：只更新当前版本的 prompt_current（不产生新版本）；仅 SUCCEEDED
        可编辑，否则报 revision_conflict。"""
        ...

    def set_current_prompt_generation(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverPromptGeneration:
        """写 projects.current_cover_prompt_generation_id；候选非 SUCCEEDED 或不属于该项目
        报 invalid_cover_candidate。"""
        ...

    def mark_prompt_generation_failed(
        self, owner_id: UUID, generation_id: UUID, failure_stage: str, failure_code: str
    ) -> CoverPromptGeneration:
        """落 FAILED 终态与失败码；已处终态（SUCCEEDED/FAILED）时返回既有记录不变（重放）。"""
        ...

    def complete_prompt_generation(
        self, generation: CoverPromptGeneration
    ) -> CoverPromptGeneration:
        """成品字段（候选/默认与所用文字/prompt 三文本/警告/评委）与状态落库；已处终态
        返回既有记录不变（重放）。不切当前指针（由 set_current_prompt_generation 负责）。"""
        ...

    def create_image_generation(self, generation: CoverImageGeneration) -> CoverImageGeneration:
        """项目内 version=max+1；幂等键冲突按 canonical_request_sha256 两分支。"""
        ...

    def create_uploaded_image_generation(
        self, generation: CoverImageGeneration
    ) -> tuple[CoverImageGeneration, bool]:
        """用户上传版本落库（不写 outbox、不派发出图）。

        项目锁内按 ``(owner, project, source='upload', object_sha256)`` 去重：
        命中则把当前指针指向既有行并返回 ``(既有, True)``；未命中则
        ``version=max+1``、插入 SUCCEEDED 行、无条件切当前，返回 ``(新行, False)``。
        """
        ...

    def get_image_generation(
        self, owner_id: UUID, generation_id: UUID
    ) -> CoverImageGeneration | None: ...

    def list_image_generations_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverImageGeneration, ...]:
        """版本倒序。"""
        ...

    def current_image_generation_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverImageGeneration | None: ...

    def complete_image_generation_and_switch_current(
        self, generation: CoverImageGeneration, *, switch_current: bool
    ) -> CoverImageGeneration:
        """QC/对象字段与状态落库；switch_current=True 时切
        projects.current_cover_image_generation_id（首个成功自动当前由应用层决定调用）。
        已处终态返回既有记录且不回切指针（重放）。"""
        ...

    def mark_image_generation_failed(
        self, owner_id: UUID, generation_id: UUID, failure_stage: str, failure_code: str
    ) -> CoverImageGeneration:
        """落 FAILED 终态与失败码；已处终态返回既有记录不变（重放）。"""
        ...

    def set_current_image_generation(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverImageGeneration:
        """写 projects.current_cover_image_generation_id；候选非 SUCCEEDED 或不属于该项目
        报 invalid_cover_candidate。"""
        ...

    def get_character_image(
        self, owner_id: UUID, image_id: UUID
    ) -> CoverCharacterImageRef | None:
        """角色图只读投影（character_images）；属主不匹配返回 None。"""
        ...

    def get_character_card(
        self, owner_id: UUID, card_id: UUID
    ) -> CoverCharacterCard | None: ...

    def list_character_cards_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverCharacterCard, ...]:
        """频道角色卡，按 (created_at, id) 升序。"""
        ...

    def get_character_cards_by_ids(
        self, owner_id: UUID, card_ids: tuple[UUID, ...]
    ) -> tuple[CoverCharacterCard, ...]:
        """按 id 批量取卡，返回顺序与入参一致（不含缺失/跨属主 id）。"""
        ...

    def upsert_character_card(self, card: CoverCharacterCard) -> CoverCharacterCard:
        """按 uq(channel_id, character_image_id) 建/改卡：无卡插入（id 入参）；有卡仅更新
        identity_text/appearance_text/source/updated_at（保留既有 id 与 created_at）。
        频道存在性与属主由仓储门禁（缺失报 not_found）。"""
        ...
