from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypedDict
from uuid import UUID

from .domain import (
    Account,
    Channel,
    CharacterImage,
    ClonedVoice,
    ClonedVoiceAccount,
    ClonedVoiceUsage,
    Project,
    ProjectSnapshot,
    VoiceVersion,
)

WORKFLOW_RUNNING_ATTEMPT_STATES = {
    "director": {"INTENT_PERSISTED", "LEASED", "SUBMITTING"},
    "storyboard": {
        "INTENT_PERSISTED",
        "SUBMITTING",
        "SUBMITTED",
        "RUNNING",
        "DOWNLOADED",
        "QC_PASSED",
        "OBJECT_STORED",
    },
    "video": {
        "CREATED",
        "STAGING",
        "NOT_SUBMITTED",
        "SUBMITTING",
        "SUBMITTED",
        "RUNNING",
        "SUCCEEDED",
    },
    "composition": {"PENDING", "DISPATCHED", "RUNNING"},
}
WORKFLOW_SUCCESS_ATTEMPT_STATES = {
    "director": {"SUCCEEDED"},
    "storyboard": {"SUCCEEDED"},
    "video": {"ARCHIVED"},
    "composition": {"SUCCEEDED"},
}
WORKFLOW_FAILED_ATTEMPT_STATES = {
    "director": {"FAILED"},
    "storyboard": {"FAILED"},
    "video": {"FAILED"},
    "composition": {"FAILED"},
}


def aggregate_workflow_attempt_states(kind: str, states: Iterable[str]) -> str | None:
    values = tuple(states)
    if not values:
        return None

    def priority(state: str) -> tuple[int, str]:
        if state == "REMOTE_STATE_UNKNOWN":
            return 100, state
        known = (
            WORKFLOW_RUNNING_ATTEMPT_STATES[kind]
            | WORKFLOW_SUCCESS_ATTEMPT_STATES[kind]
            | WORKFLOW_FAILED_ATTEMPT_STATES[kind]
            | {"CANCELLED"}
        )
        if state not in known:
            return 95, state
        if kind == "video" and state == "SUCCEEDED":
            return 90, state
        if state in WORKFLOW_RUNNING_ATTEMPT_STATES[kind]:
            return 80, state
        if state == "CANCELLED":
            return 70, state
        if state in WORKFLOW_FAILED_ATTEMPT_STATES[kind]:
            return 60, state
        return 10, state

    return max(values, key=priority)


class VideoDefectGroupFact(TypedDict):
    """带病交付缺陷分组事实（2026-10-16 决策）：当前视频带 deliver_with_defect 标记的组。

    findings 为黑屏时段摘要列表（start_second/end_second），取自 probe 证据的
    content_qc.findings，保持原数值精度。
    """

    group_sequence: int
    error_code: str
    findings: list[dict[str, float]]


class IdentityProjectRepository(Protocol):
    def setup_complete(self) -> bool: ...
    def initialize_admin(self, account: Account, completed_at: datetime) -> bool: ...
    def add_account(self, account: Account) -> None: ...
    def list_accounts(self) -> tuple[Account, ...]: ...
    def active_admin_count(self) -> int: ...
    def account_by_username(self, username: str) -> Account | None: ...
    def account(self, account_id: UUID) -> Account | None: ...
    def disable_account(self, account_id: UUID) -> bool: ...
    def disable_account_preserving_admin(self, account_id: UUID) -> str: ...
    def reset_password(self, account_id: UUID, password_hash: str) -> bool: ...
    def logout(self, account_id: UUID, generation: int) -> None: ...
    def create_session(self, token_sha256: str, account_id: UUID, generation: int) -> None: ...
    def session_account(self, token_sha256: str) -> tuple[Account, int] | None: ...
    def revoke_session(self, token_sha256: str) -> None: ...
    def add_channel(self, channel: Channel) -> None: ...
    def list_channels(self, owner_id: UUID) -> tuple[Channel, ...]: ...
    def channel(self, owner_id: UUID, channel_id: UUID) -> Channel | None: ...
    def save_channel(self, channel: Channel) -> None: ...
    def assert_channel_writable(self, owner_id: UUID, channel_id: UUID) -> None: ...
    def add_image(self, image: CharacterImage, claim: StagedObjectClaim | None = None) -> None: ...
    def image(self, owner_id: UUID, channel_id: UUID, image_id: UUID) -> CharacterImage | None: ...
    def list_images(self, owner_id: UUID, channel_id: UUID) -> tuple[CharacterImage, ...]: ...
    def add_project(self, project: Project) -> None: ...
    def list_projects(self, owner_id: UUID) -> tuple[Project, ...]: ...
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
    def cloned_voice_ids_for_project(self, owner_id: UUID, project_id: UUID) -> tuple[UUID, ...]:
        """项目（含快照）引用的克隆音色 id 集合（去重）；无则空元组。"""
        ...
    def count_active_draft_projects_with_cloned_voice(self, cloned_voice_id: UUID) -> int:
        """引用该克隆音色的未冻结草稿数（删除保护输入）。"""
        ...
    def list_cloned_voice_channels(self) -> tuple[UUID, ...]:
        """存在克隆音色的频道 id（自动清理的遍历入口）。"""
        ...
    def workflow_counts(self, owner_id: UUID, project_id: UUID) -> dict[str, int]: ...
    def workflow_attempt_states(
        self, owner_id: UUID, project_id: UUID
    ) -> dict[str, str | None]: ...
    def auto_pipeline_run(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, str | None, dict[str, object]] | None:
        """最新一条 AUTO 一键生产运行：(state, reason_code, payload)；无运行返回 None。"""
        ...
    def auto_pipeline_completed_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        """最新一条 COMPLETED AUTO 运行的完成时间（updated_at，终态后不再变动）；无则 None。"""
        ...
    def latest_composition_dispatched_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        """最新合成 outbox 行的派发时间（与 workflow_attempt_states 同口径排序）；无则 None。"""
        ...
    def video_defect_groups(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[VideoDefectGroupFact, ...]:
        """当前视频带带病交付标记的分组事实（按组号升序）；无则空元组。"""
        ...
    def project(self, owner_id: UUID, project_id: UUID) -> Project | None: ...
    def project_for_write(self, owner_id: UUID, project_id: UUID) -> Project | None: ...
    def save_project(self, project: Project, expected_revision: int) -> bool: ...
    def confirm_project(
        self,
        owner_id: UUID,
        project_id: UUID,
        expected_revision: int,
        snapshot: ProjectSnapshot,
    ) -> bool: ...
    def project_has_current_composition(
        self, owner_id: UUID, project_id: UUID
    ) -> bool: ...
    def set_project_publication(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        published_at: datetime | None,
        actor_account_id: UUID,
        occurred_at: datetime,
    ) -> bool: ...
    def voice_version(self, voice_version_id: UUID) -> VoiceVersion | None: ...
    def list_voice_versions(self) -> tuple[VoiceVersion, ...]: ...
    def add_voice_version(
        self,
        voice: VoiceVersion,
        media: dict[str, tuple[UUID, str, str, int]],
        claims: dict[str, StagedObjectClaim] | None = None,
    ) -> bool: ...
    def lock_voice_catalog_import(self, source_sha256: str) -> None: ...
    def lock_voice_version_import(self, voice_id: str, version: int) -> None: ...
    def voice_media(
        self, voice_version_id: UUID, kind: str
    ) -> tuple[str, str, str, int] | None: ...
    def catalog_import_progress(self, source_sha256: str) -> tuple[int, str] | None: ...
    def save_catalog_import(
        self,
        source_sha256: str,
        catalog_version: int,
        expected_count: int,
        imported_count: int,
        status: str,
        updated_at: datetime,
    ) -> None: ...
    def publish_voice_catalog(
        self,
        source_sha256: str,
        catalog_version: int,
        expected_count: int,
        updated_at: datetime,
    ) -> None: ...
    def attach_staged_objects(
        self, claims: tuple[StagedObjectClaim, ...], reference_type: str
    ) -> None: ...


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, password_hash: str, password: str) -> bool: ...


@dataclass(slots=True)
class StagedObjectClaim:
    key: str
    generation: int
    execution_token: UUID


class StoredImage(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def created(self) -> bool: ...
    @property
    def claim(self) -> StagedObjectClaim: ...


class PrivateImageStorage(Protocol):
    def put(self, body: bytes, media_type: str, *, owner_id: UUID | None = None) -> StoredImage: ...
    def get(self, key: str) -> bytes: ...
    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes: ...
    def delete_exact(self, key: str) -> None: ...
    def put_at_key(
        self,
        key: str,
        body: bytes,
        media_type: str,
        expected_sha256: str,
        *,
        owner_id: UUID | None = None,
    ) -> StoredImage: ...


class OpaqueTokenPort(Protocol):
    def issue(self) -> tuple[str, str]: ...


@dataclass(frozen=True, slots=True)
class ClonedVoiceRemoteCopy:
    """频道删除时需要在 ElevenLabs 侧清理的一条副本映射。"""

    cloned_voice_id: UUID
    pool_key_id: UUID
    el_voice_id: str


@dataclass(frozen=True, slots=True)
class ClonedVoiceRemoteFailure:
    cloned_voice_id: UUID
    pool_key_id: UUID


@dataclass(frozen=True, slots=True)
class ChannelDeletionSummary:
    """频道删除事务的产物：行删除计数、待删对象键与远端副本清单。"""

    channel_id: UUID
    character_images: int
    cloned_voices: int
    cover_ref_images: int
    cover_character_cards: int
    object_keys: tuple[str, ...]
    remote_copies: tuple[ClonedVoiceRemoteCopy, ...]
    # 临时功能（2026-10-21）：频道级封面模板行（0/1）。默认 0 保持既有构造兼容。
    cover_templates: int = 0
    # §36：频道级简介模板行数（编号计数行不作为用户模板计数）。
    description_templates: int = 0


class ChannelDeletionRepository(Protocol):
    """频道删除的跨聚合写入口；确认值校验与项目门禁在事务内完成。"""

    def delete_channel(
        self, owner_id: UUID, channel_id: UUID, confirmation: str
    ) -> ChannelDeletionSummary: ...


class ClonedVoiceRemoteCleaner(Protocol):
    async def delete_remote_copies(
        self, copies: tuple[ClonedVoiceRemoteCopy, ...]
    ) -> tuple[ClonedVoiceRemoteFailure, ...]: ...

