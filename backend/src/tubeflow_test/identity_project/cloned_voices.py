"""克隆音色（ElevenLabs IVC）的领域规则与端口。

决策依据：`docs/planning/USER_DECISIONS.md` §23.2（EL 源必须选克隆音色）、
§23.3（上传音频克隆、频道共享、无头像、试听用源音频）、§23.4（切账号自动补克隆）、
§23.5（删除保护与槽位满自动清理）。本模块不含框架、数据库与网络依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tubeflow_test.adapters.object_storage import StoredObject
from tubeflow_test.domain.errors import DomainError

from .domain import Channel, ClonedVoice, ClonedVoiceAccount, ClonedVoiceUsage

NAME_MAX_CHARS = 100
REMARK_MAX_CHARS = 2000
# 源音频后缀白名单：产品侧不自设音频规格上限（§23.3），只校验可克隆容器的形状。
SOURCE_SUFFIXES = (
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".webm",
    ".mp4",
    ".mov",
)
DEFAULT_SOURCE_SUFFIX = ".wav"


@dataclass(frozen=True, slots=True)
class ClonedVoiceUpload:
    name: str
    remark: str
    filename: str
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class CloneOutcome:
    key_id: UUID
    label: str
    ok: bool
    el_voice_id: str | None = None
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class ClonedVoiceUploadResult:
    voice: ClonedVoice
    outcomes: tuple[CloneOutcome, ...]

    @property
    def failures(self) -> tuple[CloneOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.ok)


class ClonedVoiceClonePort(Protocol):
    """IVC 克隆与远端音色清理（实现见 `adapters/elevenlabs.py`，全部经 EL 代理）。"""

    async def clone_voice(
        self, api_key: str, name: str, *, audio: bytes, filename: str, description: str = ""
    ) -> str: ...
    async def list_account_voices(self, api_key: str) -> tuple[str, ...]: ...
    async def delete_voice(self, api_key: str, voice_id: str) -> None: ...


class ClonedVoiceSourceStorage(Protocol):
    """克隆音色源音频的对象存储端口（实现见 `adapters/object_storage.py`）。"""

    def put_at_key(
        self, key: str, body: bytes, media_type: str, expected_sha256: str
    ) -> StoredObject: ...
    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes: ...
    def delete_exact(self, key: str) -> None: ...


class ClonedVoiceCatalogPort(Protocol):
    """配音期按账号解析逻辑音色所需的最小仓储视图（§23.4）。"""

    def cloned_voice_by_id(self, cloned_voice_id: UUID) -> ClonedVoice | None: ...
    def cloned_voice_account(
        self, cloned_voice_id: UUID, pool_key_id: UUID
    ) -> ClonedVoiceAccount | None: ...
    def save_cloned_voice_account(self, account: ClonedVoiceAccount) -> None: ...


class ClonedVoiceManagementPort(Protocol):
    """克隆音色管理编排（§23.3-§23.5）所需的最小仓储视图。

    生产装配（`SessionScopedClonedVoiceRepository`）按每调用一会话实现该子集；
    全量 `IdentityProjectRepository` 的 SQL 实现天然满足本协议。
    """

    def channel(self, owner_id: UUID, channel_id: UUID) -> Channel | None: ...
    def cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> ClonedVoice | None: ...
    def cloned_voice_by_id(self, cloned_voice_id: UUID) -> ClonedVoice | None: ...
    def list_cloned_voices(self, channel_id: UUID) -> tuple[ClonedVoice, ...]: ...
    def add_cloned_voice(self, voice: ClonedVoice) -> None: ...
    def delete_cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> bool: ...
    def cloned_voice_usage(self, cloned_voice_id: UUID) -> ClonedVoiceUsage: ...
    def cloned_voice_account(
        self, cloned_voice_id: UUID, pool_key_id: UUID
    ) -> ClonedVoiceAccount | None: ...
    def list_cloned_voice_accounts(
        self, cloned_voice_id: UUID
    ) -> tuple[ClonedVoiceAccount, ...]: ...
    def save_cloned_voice_account(self, account: ClonedVoiceAccount) -> None: ...
    def forget_cloned_voice_account(self, cloned_voice_id: UUID, pool_key_id: UUID) -> None: ...
    def list_cloned_voice_channels(self) -> tuple[UUID, ...]: ...


def normalize_cloned_voice_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > NAME_MAX_CHARS:
        raise DomainError(
            "invalid_cloned_voice_name",
            f"Cloned voice name must contain 1 to {NAME_MAX_CHARS} characters",
        )
    return normalized


def normalize_cloned_voice_remark(value: str) -> str:
    normalized = value.strip()
    if len(normalized) > REMARK_MAX_CHARS:
        raise DomainError(
            "invalid_cloned_voice_remark",
            f"Cloned voice remark must contain at most {REMARK_MAX_CHARS} characters",
        )
    return normalized


def source_suffix(filename: str) -> str:
    lowered = filename.strip().lower()
    for suffix in SOURCE_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return DEFAULT_SOURCE_SUFFIX


def ensure_channel_owned(channel: Channel | None, owner_id: UUID, channel_id: UUID) -> Channel:
    if channel is None or channel.owner_id != owner_id or channel.id != channel_id:
        raise DomainError("not_found", "Resource not found.")
    return channel


def ensure_unique_name(name: str, existing: tuple[ClonedVoice, ...]) -> None:
    if any(item.name.casefold() == name.casefold() for item in existing):
        raise DomainError(
            "cloned_voice_name_conflict", "A cloned voice with this name already exists"
        )


def slot_available(used: int | None, limit: int | None) -> bool | None:
    """True=有空槽、False=已满、None=配额未知（调用方需先刷新）。"""
    if used is None or limit is None or limit <= 0:
        return None
    return used < limit


def cleanup_selection(
    *,
    account_voice_ids: tuple[str, ...],
    mappings: tuple[tuple[UUID, str, ClonedVoice, ClonedVoiceUsage], ...],
    keep_el_voice_ids: frozenset[str],
    needed: int,
) -> tuple[tuple[UUID, str], ...]:
    """槽位满时的清理选择：仅完全无引用者、且确实存在于该账号的远端音色。

    `mappings` 为 (cloned_voice_id, el_voice_id, voice, usage)；`keep_el_voice_ids`
    是本次上传刚克隆成功、必须保留的映射；按 created_at 最旧优先（§23.5）。
    返回 (cloned_voice_id, el_voice_id)，最多 `needed` 条。
    """
    present = set(account_voice_ids)
    candidates = [
        (voice.created_at, str(index).zfill(8), cloned_voice_id, el_voice_id)
        for index, (cloned_voice_id, el_voice_id, voice, usage) in enumerate(mappings)
        if not usage.protected and el_voice_id not in keep_el_voice_ids and el_voice_id in present
    ]
    candidates.sort(key=lambda entry: (entry[0], entry[1]))
    return tuple(
        (cloned_voice_id, el_voice_id) for _, _, cloned_voice_id, el_voice_id in candidates
    )[: max(needed, 0)]
