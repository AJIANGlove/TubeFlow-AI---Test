from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tubeflow_test.adapters.persistence.identity_project.models import Base


class CoverRefImageRow(Base):
    __tablename__ = "cover_ref_images"
    __table_args__ = (
        CheckConstraint("scope IN ('channel', 'project')", name="scope"),
        UniqueConstraint("channel_id", "sha256", name="uq_cover_ref_images_channel_sha256"),
        UniqueConstraint("project_id", "sha256", name="uq_cover_ref_images_project_sha256"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), index=True
    )
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    image_format: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CoverStyleDraftRow(Base):
    __tablename__ = "cover_style_drafts"
    __table_args__ = (
        CheckConstraint("scope IN ('channel', 'project')", name="scope"),
        UniqueConstraint("channel_id", name="uq_cover_style_drafts_channel_id"),
        UniqueConstraint("project_id", name="uq_cover_style_drafts_project_id"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), index=True
    )
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    ref_image_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cover_ref_images.id", ondelete="SET NULL")
    )
    dims: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default="{}"
    )
    analysis: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default="{}"
    )
    analysis_dims: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default="{}"
    )
    ref_image_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    edited_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CoverStyleProfileRow(Base):
    __tablename__ = "cover_style_profiles"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", "version", name="uq_cover_style_profiles_channel_version"
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    ref_image_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cover_ref_images.id", ondelete="SET NULL")
    )
    dims: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    analysis_dims: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    edited_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cover_style_profiles.id", ondelete="SET NULL")
    )
    # 风格版本备注名（v5 §5，≤60 码点由应用层校验）。
    label: Mapped[str] = mapped_column(
        String(60), nullable=False, default="", server_default=""
    )


class CoverCharacterCardRow(Base):
    """频道级角色卡（v5 §1.1）：可变卡，每角色图一张；项目删除不清理（频道资产）。"""

    __tablename__ = "cover_character_cards"
    __table_args__ = (
        CheckConstraint("source IN ('manual', 'analysis')", name="source"),
        UniqueConstraint(
            "channel_id",
            "character_image_id",
            name="uq_cover_character_cards_channel_image",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="RESTRICT"), index=True
    )
    character_image_id: Mapped[UUID] = mapped_column(ForeignKey("character_images.id"))
    identity_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    appearance_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CoverPromptGenerationRow(Base):
    __tablename__ = "cover_prompt_generations"
    __table_args__ = (
        CheckConstraint(
            "style_source IN ('channel_profile', 'project_draft', 'none')",
            name="style_source",
        ),
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_cover_prompt_generations_owner_key"
        ),
        UniqueConstraint(
            "project_id", "version", name="uq_cover_prompt_generations_project_version"
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    style_source: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_style_profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cover_style_profiles.id", ondelete="SET NULL")
    )
    style_text_used: Mapped[str] = mapped_column(Text, nullable=False)
    style_dims_used: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default="{}"
    )
    style_override_text: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    cover_text_candidates: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list, server_default="[]"
    )
    cover_text_default: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    cover_text_used: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    prompt_body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    prompt_system: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    prompt_current: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    title_used: Mapped[str] = mapped_column(Text, nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    warnings: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list, server_default="[]"
    )
    judge: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    # 生成时绑定的角色卡快照（v5 §1.1 血缘：[{card_id, character_image_id,
    # identity_text, appearance_text}]）；请求事实，create 时落库。
    character_cards_used: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=list, server_default="[]"
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default="{}"
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_stage: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CoverImageGenerationRow(Base):
    __tablename__ = "cover_image_generations"
    __table_args__ = (
        CheckConstraint("source IN ('generated', 'upload')", name="source"),
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_cover_image_generations_owner_key"
        ),
        UniqueConstraint(
            "project_id", "version", name="uq_cover_image_generations_project_version"
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    prompt_generation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cover_prompt_generations.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[str | None] = mapped_column(String(16), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 版本来源（迁移 0042）：generated=AI 出图，upload=用户直接上传。
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="generated", server_default="generated"
    )
    parameters: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict, server_default="{}"
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_receipt: Mapped[str | None] = mapped_column(Text)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    downloaded_sha256: Mapped[str | None] = mapped_column(String(64))
    downloaded_size: Mapped[int | None] = mapped_column(Integer)
    qc_width: Mapped[int | None] = mapped_column(Integer)
    qc_height: Mapped[int | None] = mapped_column(Integer)
    qc_mime: Mapped[str | None] = mapped_column(String(64))
    object_key: Mapped[str | None] = mapped_column(Text)
    object_sha256: Mapped[str | None] = mapped_column(String(64))
    failure_stage: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    visual_review: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )


class CoverOutboxRow(Base):
    """封面 outbox 事件（照 StoryboardOutboxRow）。

    两点差异：project_id 可空（频道作用域的参考图解析事件无项目）；workflow_id
    的唯一性改为部分唯一索引（仅未确认事件）——解析动作重复点击在途去重命中
    IntegrityError，确认后的重提（失败的解析再次显式点击）可复用同一确定
    workflow_id 重新入队。
    """

    __tablename__ = "cover_outbox"
    __table_args__ = (
        Index(
            "uq_cover_outbox_pending_workflow_id",
            "workflow_id",
            unique=True,
            postgresql_where=text("acknowledged_at IS NULL"),
            sqlite_where=text("acknowledged_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(192), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
