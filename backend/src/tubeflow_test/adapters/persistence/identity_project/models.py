from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class AccountRow(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("username", name="uq_accounts_username"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    session_generation: Mapped[int] = mapped_column(Integer, nullable=False)


class SessionRow(Base):
    __tablename__ = "account_sessions"
    token_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)


class StagedObjectRow(Base):
    __tablename__ = "staged_objects"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING_UPLOAD', 'STAGED', 'ATTACHED', 'FINALIZED', 'CLEANUP_PENDING')",
            name="state",
        ),
        CheckConstraint(
            "ownership_scope IN ('GLOBAL', 'OWNER', 'PROJECT')",
            name="ownership_scope",
        ),
        CheckConstraint(
            "(ownership_scope = 'GLOBAL' AND owner_id IS NULL AND project_id IS NULL) OR "
            "(ownership_scope = 'OWNER' AND owner_id IS NOT NULL AND project_id IS NULL) OR "
            "(ownership_scope = 'PROJECT' AND owner_id IS NOT NULL AND project_id IS NOT NULL)",
            name="scope_ownership",
        ),
        Index("ix_staged_objects_cleanup", "state", "cleanup_after", "lease_until"),
        UniqueConstraint("object_key", name="uq_staged_objects_object_key"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    object_key: Mapped[str] = mapped_column(Text)
    logical_object_key: Mapped[str | None] = mapped_column(Text, index=True)
    project_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str | None] = mapped_column(String(32))
    ownership_scope: Mapped[str] = mapped_column(
        String(16), default="GLOBAL", server_default="GLOBAL"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING_UPLOAD", server_default="PENDING_UPLOAD"
    )
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    media_type: Mapped[str | None] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    execution_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reference_type: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class SystemInitializationRow(Base):
    __tablename__ = "system_initialization"
    __table_args__ = (
        CheckConstraint("singleton = 1", name="singleton"),
        UniqueConstraint("admin_account_id", name="uq_system_initialization_admin_account_id"),
    )
    singleton: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    admin_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelRow(Base):
    __tablename__ = "channels"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(String(30))
    default_voice_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("voice_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # 默认角色 IP：JSON 存 UUID 字符串列表（有序，≤3，空=未设置）。
    default_character_image_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
        server_default="[]",
    )
    # 封面频道风格当前指针：无 FK（照 snapshot_voice_version_id 等指针列先例），
    # 避免 channels/projects 与 cover_* 表形成循环依赖；指向 cover_style_profiles.id。
    current_cover_style_profile_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    # §23.1：频道默认配音源（cosyvoice/elevenlabs）与该源下的默认音色。
    # default_cloned_voice_id 无 FK（理由同 current_cover_style_profile_id：避免
    # channels↔cloned_voices 建表环）；归属校验在服务层。
    default_tts_provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="cosyvoice"
    )
    default_cloned_voice_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True, index=True
    )
    # 频道默认画面风格（空串=未设置）：只用于新建项目预填/回落，只影响未来项目，
    # 不进入项目快照；项目确认配音时按项目自身的 visual_style 冻结。
    default_visual_style: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class ClonedVoiceRow(Base):
    """§23.3：频道共享的 EL IVC 克隆音色（源音频为频道级资产，无 TTL）。"""

    __tablename__ = "cloned_voices"
    __table_args__ = (
        UniqueConstraint("channel_id", "name", name="uq_cloned_voices_channel_name"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    remark: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClonedVoiceAccountRow(Base):
    """§23.4：(克隆音色, 号池 key) → 该账号远端 el_voice_id 映射。"""

    __tablename__ = "cloned_voice_accounts"
    cloned_voice_id: Mapped[UUID] = mapped_column(
        ForeignKey("cloned_voices.id", ondelete="CASCADE"), primary_key=True
    )
    pool_key_id: Mapped[UUID] = mapped_column(
        ForeignKey("elevenlabs_keys.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    el_voice_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CharacterImageRow(Base):
    __tablename__ = "character_images"
    __table_args__ = (
        UniqueConstraint("channel_id", "sha256", name="uq_character_images_channel_sha256"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), index=True
    )
    asset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)


class VoiceVersionRow(Base):
    __tablename__ = "voice_versions"
    __table_args__ = (
        UniqueConstraint("voice_id", "version", name="uq_voice_versions_voice_version"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    voice_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False)
    reference_zip_asset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    reference_zip_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    reference_zip_media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_zip_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_asset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    preview_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    preview_media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    avatar_asset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    avatar_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_sha256: Mapped[str] = mapped_column(String(64))
    catalog_source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_zip_sha256: Mapped[str] = mapped_column(String(64))
    preview_sha256: Mapped[str] = mapped_column(String(64))
    avatar_sha256: Mapped[str] = mapped_column(String(64))
    supported_locales: Mapped[list[str]] = mapped_column(ARRAY(String(8)))
    raw_language_codes: Mapped[list[str]] = mapped_column(ARRAY(String(16)), nullable=False)
    catalog_metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class VoiceCatalogImportRow(Base):
    __tablename__ = "voice_catalog_imports"
    source_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectRow(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "production_state IN ('ACTIVE', 'PURGING')",
            name="production_state",
        ),
        CheckConstraint(
            "production_mode IN ('MANUAL', 'AUTO')",
            name="production_mode",
        ),
        CheckConstraint("draft_revision > 0", name="draft_revision"),
        CheckConstraint("production_revision > 0", name="production_revision"),
        UniqueConstraint("owner_id", "id", name="uq_projects_owner_id"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("channels.id"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    character_image_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False
    )
    visual_style: Mapped[str] = mapped_column(Text, nullable=False)
    # EL 项目没有 CosyVoice 公共音色；两列按 tts_provider 二选一（领域层门禁保证）。
    voice_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("voice_versions.id"), nullable=True
    )
    tts_provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="cosyvoice"
    )
    cloned_voice_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cloned_voices.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(String(5), nullable=False)
    expected_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 人工发布标记：NULL = 未发布；只由发布接口直写，不进入编辑白名单与项目快照。
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    draft_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    production_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    production_mode: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="MANUAL"
    )
    # §15.9：AUTO 一键生产的当前预算轮次；恢复时 +1，各阶段按轮重置 3 次预算。
    auto_budget_round: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    production_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    snapshot_script: Mapped[str | None] = mapped_column(Text)
    snapshot_character_image_ids: Mapped[list[UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True))
    )
    snapshot_visual_style: Mapped[str | None] = mapped_column(Text)
    snapshot_voice_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    snapshot_locale: Mapped[str | None] = mapped_column(String(8))
    snapshot_aspect_ratio: Mapped[str | None] = mapped_column(String(5))
    snapshot_channel_url: Mapped[str | None] = mapped_column(Text)
    snapshot_voice_metadata_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_catalog_source_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_reference_asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    snapshot_voice_reference_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_reference_media_type: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_reference_size_bytes: Mapped[int | None] = mapped_column(Integer)
    snapshot_voice_preview_asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    snapshot_voice_preview_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_preview_media_type: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_preview_size_bytes: Mapped[int | None] = mapped_column(Integer)
    snapshot_voice_avatar_asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    snapshot_voice_avatar_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_avatar_media_type: Mapped[str | None] = mapped_column(String(64))
    snapshot_voice_avatar_size_bytes: Mapped[int | None] = mapped_column(Integer)
    # §23.1：快照冻结的配音源与其逻辑音色事实（EL 分支不要求三件媒体事实）。
    snapshot_tts_provider: Mapped[str | None] = mapped_column(Text)
    snapshot_cloned_voice_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    snapshot_cloned_voice_name: Mapped[str | None] = mapped_column(Text)
    snapshot_cloned_voice_source_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_expected_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    snapshot_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 封面当前指针：无 FK（理由同 ChannelRow.current_cover_style_profile_id），
    # 分别指向 cover_prompt_generations.id / cover_image_generations.id。
    current_cover_prompt_generation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    current_cover_image_generation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )


class ProjectPublicationEventRow(Base):
    """项目人工发布/取消发布的审计台账；随项目物理删除（FK CASCADE）。"""

    __tablename__ = "project_publication_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('PUBLISHED', 'UNPUBLISHED')",
            name="action",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    actor_account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AutoPipelineRunRow(Base):
    """§15 一键自动生产运行记录；同一项目至多一条 RUNNING/PAUSED（部分唯一索引）。"""

    __tablename__ = "auto_pipeline_runs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('RUNNING', 'PAUSED', 'COMPLETED')",
            name="state",
        ),
        CheckConstraint("authorization_round > 0", name="authorization_round"),
        Index(
            "uq_auto_pipeline_runs_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("state IN ('RUNNING', 'PAUSED')"),
            sqlite_where=text("state IN ('RUNNING', 'PAUSED')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    authorization_round: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectSnapshotCharacterRow(Base):
    __tablename__ = "project_snapshot_characters"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "ordinal", name="uq_project_snapshot_characters_project_ordinal"
        ),
        UniqueConstraint(
            "project_id",
            "character_image_id",
            name="uq_project_snapshot_characters_project_character",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    character_image_id: Mapped[UUID] = mapped_column(ForeignKey("character_images.id"))
    asset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
