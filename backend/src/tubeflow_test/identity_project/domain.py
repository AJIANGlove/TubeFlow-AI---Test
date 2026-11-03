from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from tubeflow_test.domain.common import AspectRatio, MediaAssetRef, SupportedLocale
from tubeflow_test.domain.errors import DomainError


class AccountRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class TtsProvider(StrEnum):
    """§23.1：项目/频道配音源；同一项目内不混用。"""

    COSYVOICE = "cosyvoice"
    ELEVENLABS = "elevenlabs"


TTS_PROVIDERS: tuple[str, ...] = (TtsProvider.COSYVOICE.value, TtsProvider.ELEVENLABS.value)

# 频道默认画面风格上限（留空=未设置）；与前端 textarea maxlength 一致。
MAX_CHANNEL_DEFAULT_VISUAL_STYLE = 500


def normalized_tts_provider(value: str | TtsProvider) -> str:
    try:
        return TtsProvider(value).value
    except ValueError as error:
        raise DomainError(
            "invalid_tts_provider", "TTS provider must be cosyvoice or elevenlabs"
        ) from error


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    username: str
    password_hash: str
    role: AccountRole
    active: bool = True
    session_generation: int = 1


@dataclass(frozen=True, slots=True)
class Channel:
    id: UUID
    owner_id: UUID
    url: str
    default_voice_version_id: UUID | None = None
    # 默认角色 IP（有序，≤3，空=未设置）：随频道保存，只影响未来新建项目，不进项目快照。
    default_character_image_ids: tuple[UUID, ...] = ()
    name: str | None = None
    # 封面频道风格当前指针（只读事实，由封面仓储直写 channels 行）。
    current_cover_style_profile_id: UUID | None = None
    # §23.1：频道默认配音源与该源下的默认音色（两列按 provider 二选一由服务层门禁保证）。
    default_tts_provider: str = TtsProvider.COSYVOICE.value
    default_cloned_voice_id: UUID | None = None
    # 频道默认画面风格（空=未设置）：新建项目时预填/回落，只影响未来项目，不进项目快照。
    default_visual_style: str = ""


def channel_deletion_confirmation(channel: Channel) -> str:
    """删除频道的确认值：有名称用完整名称，未命名用完整 URL。

    不使用「频道 NN」这类索引派生名（删除后序号会漂移，且后端不可知）。
    """
    name = (channel.name or "").strip()
    return name or channel.url


@dataclass(frozen=True, slots=True)
class ClonedVoice:
    """§23.3：用户上传音频经 EL IVC 克隆得到的频道共享音色（无头像，试听用源音频）。"""

    id: UUID
    channel_id: UUID
    owner_id: UUID
    name: str
    remark: str
    source_object_key: str
    source_sha256: str
    source_size_bytes: int
    source_media_type: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClonedVoiceAccount:
    """§23.4：同一逻辑音色在各号池账号上的远端 voice_id 映射。"""

    cloned_voice_id: UUID
    pool_key_id: UUID
    el_voice_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClonedVoiceUsage:
    """删除保护判定输入（§23.5）：三类引用任一存在即受保护。"""

    frozen_snapshot_references: int = 0
    draft_references: int = 0
    channel_default_references: int = 0

    @property
    def protected(self) -> bool:
        return bool(
            self.frozen_snapshot_references
            or self.draft_references
            or self.channel_default_references
        )

    @property
    def reason_code(self) -> str | None:
        if self.channel_default_references:
            return "cloned_voice_channel_default"
        if self.frozen_snapshot_references:
            return "cloned_voice_frozen_snapshot"
        if self.draft_references:
            return "cloned_voice_draft_reference"
        return None


def cloned_voice_delete_guard(usage: ClonedVoiceUsage) -> None:
    """手动删除守卫：被冻结快照、未冻结草稿或频道默认引用时拒绝（§23.5）。"""
    reason = usage.reason_code
    if reason is None:
        return
    messages = {
        "cloned_voice_channel_default": "Cloned voice is the channel default voice",
        "cloned_voice_frozen_snapshot": "Cloned voice is referenced by a frozen project snapshot",
        "cloned_voice_draft_reference": "Cloned voice is referenced by an unfrozen project",
    }
    raise DomainError(reason, messages[reason])


def cloned_voice_cleanup_candidates[T](
    items: tuple[tuple[T, ClonedVoiceUsage, datetime], ...], *, limit: int
) -> tuple[T, ...]:
    """槽位满时的自动清理选择器：仅完全无引用者，按 created_at 最旧优先（§23.5）。"""
    candidates = [
        (created_at, str(index).zfill(8), item)
        for index, (item, usage, created_at) in enumerate(items)
        if not usage.protected
    ]
    candidates.sort(key=lambda entry: (entry[0], entry[1]))
    return tuple(item for _, _, item in candidates[: max(limit, 0)])


def channel_default_voice_gate(
    provider: str, default_voice_version_id: UUID | None, default_cloned_voice_id: UUID | None
) -> None:
    """频道默认门禁（§23.2，对齐 §3.13）：默认源必须有该源下的默认音色。

    EL 源且频道无可用克隆音色时不能创建项目；本函数只校验"默认源与默认音色成对"。
    """
    normalized = normalized_tts_provider(provider)
    if normalized == TtsProvider.COSYVOICE.value:
        if default_voice_version_id is None:
            raise DomainError(
                "channel_default_voice_required",
                "Set a channel default public voice for the CosyVoice provider",
            )
        return
    if default_cloned_voice_id is None:
        raise DomainError(
            "channel_default_voice_required",
            "Set a channel default cloned voice for the ElevenLabs provider",
        )


def normalized_channel_default_visual_style(value: str) -> str:
    """频道默认画面风格归一化：去首尾空白，超限拒绝；空串合法（=清空/未设置）。"""
    normalized = value.strip()
    if len(normalized) > MAX_CHANNEL_DEFAULT_VISUAL_STYLE:
        raise DomainError(
            "invalid_channel_default_visual_style",
            "Channel default visual style must contain 0 to "
            f"{MAX_CHANNEL_DEFAULT_VISUAL_STYLE} characters",
        )
    return normalized



@dataclass(frozen=True, slots=True)
class CharacterImage:
    id: UUID
    owner_id: UUID
    asset: MediaAssetRef
    width: int
    height: int
    object_key: str = ""
    channel_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SnapshotCharacter:
    ordinal: int
    character_image_id: UUID
    asset_id: UUID
    sha256: str
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class VoiceMediaFact:
    asset_id: UUID
    sha256: str
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class VoiceVersion:
    id: UUID
    metadata_sha256: str
    supported_locales: tuple[SupportedLocale, ...]
    enabled: bool
    reference_zip_sha256: str = ""
    preview_sha256: str = ""
    avatar_sha256: str = ""
    voice_id: str = ""
    version: int = 1
    display_name: str = ""
    catalog_source_sha256: str = ""
    reference_zip: VoiceMediaFact | None = None
    preview: VoiceMediaFact | None = None
    avatar: VoiceMediaFact | None = None
    raw_language_codes: tuple[str, ...] = ()
    catalog_metadata_json: str = "{}"


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    script: str
    characters: tuple[SnapshotCharacter, ...]
    visual_style: str
    locale: SupportedLocale
    aspect_ratio: AspectRatio
    channel_url: str
    expected_duration_seconds: int | None
    confirmed_at: datetime
    # 配音源冻结（§23.1）：cosyvoice 分支持有公共音色与三件媒体事实；
    # elevenlabs 分支只持有逻辑克隆音色事实（id/名称/源音频 sha256）。
    tts_provider: str = TtsProvider.COSYVOICE.value
    voice_version_id: UUID | None = None
    voice_metadata_sha256: str = ""
    voice_catalog_source_sha256: str = ""
    voice_reference_zip: VoiceMediaFact | None = None
    voice_preview: VoiceMediaFact | None = None
    voice_avatar: VoiceMediaFact | None = None
    cloned_voice_id: UUID | None = None
    cloned_voice_name: str = ""
    cloned_voice_source_sha256: str = ""

    @property
    def character_image_ids(self) -> tuple[UUID, ...]:
        return tuple(item.character_image_id for item in self.characters)


@dataclass(frozen=True, slots=True)
class Project:
    id: UUID
    owner_id: UUID
    channel_id: UUID
    script: str
    character_image_ids: tuple[UUID, ...]
    visual_style: str
    locale: SupportedLocale
    aspect_ratio: AspectRatio
    expected_duration_seconds: int | None = None
    draft_revision: int = 1
    snapshot: ProjectSnapshot | None = None
    production_state: str = "ACTIVE"
    production_revision: int = 1
    title: str = "Untitled project"
    created_at: datetime | None = None
    production_mode: str = "MANUAL"
    # 封面图当前指针（只读事实，由封面仓储直写 projects 行）。
    current_cover_image_generation_id: UUID | None = None
    # §23.1：项目配音源与所选音色；voice_version_id 仅 cosyvoice 分支非空。
    tts_provider: str = TtsProvider.COSYVOICE.value
    voice_version_id: UUID | None = None
    cloned_voice_id: UUID | None = None
    # 人工发布标记：NULL = 未发布；只由发布接口直写，不进入 edited() 白名单与项目快照。
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.title.strip() or len(self.title.strip()) > 200:
            raise DomainError(
                "invalid_project_title", "Project title must contain 1 to 200 characters"
            )
        if not 1 <= len(self.character_image_ids) <= 8:
            raise DomainError(
                "invalid_character_image_count", "A project requires 1 to 8 character images"
            )
        if len(set(self.character_image_ids)) != len(self.character_image_ids):
            raise DomainError("duplicate_character_image", "Character images must be unique")
        if not self.script.strip():
            raise DomainError("invalid_script", "Script must not be blank")
        if self.expected_duration_seconds is not None and self.expected_duration_seconds <= 0:
            raise DomainError("invalid_expected_duration", "Expected duration must be positive")
        if self.production_mode not in ("MANUAL", "AUTO"):
            raise DomainError(
                "invalid_production_mode", "Production mode must be MANUAL or AUTO"
            )
        _assert_voice_assignment(self.tts_provider, self.voice_version_id, self.cloned_voice_id)

    def edited(self, **changes: Any) -> Project:
        if self.production_state != "ACTIVE":
            raise DomainError("project_purging", "Project is being permanently deleted")
        management_changes = {"title"}
        allowed = {
            "script",
            "character_image_ids",
            "visual_style",
            "voice_version_id",
            "cloned_voice_id",
            "tts_provider",
            "locale",
            "aspect_ratio",
            "expected_duration_seconds",
        }
        if set(changes) - allowed - management_changes:
            raise DomainError("project_field_not_editable", "Project field is not editable")
        if self.snapshot is not None and set(changes) - management_changes:
            raise DomainError(
                "project_snapshot_locked", "Project is locked after narration confirmation"
            )
        return replace(self, **changes, draft_revision=self.draft_revision + 1)


def _assert_voice_assignment(
    provider: str, voice_version_id: UUID | None, cloned_voice_id: UUID | None
) -> None:
    """配音源与音色二选一（§23.1、§23.2）：cosyvoice 用公共音色，elevenlabs 用克隆音色。"""
    normalized = normalized_tts_provider(provider)
    if normalized == TtsProvider.COSYVOICE.value:
        if voice_version_id is None:
            raise DomainError(
                "channel_default_voice_required", "A CosyVoice project requires a public voice"
            )
        return
    if cloned_voice_id is None:
        raise DomainError(
            "channel_default_voice_required",
            "An ElevenLabs project requires a channel cloned voice",
        )
