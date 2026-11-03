"""视频简介 SQL 模型：频道模板、生成尝试幂等与项目简介不可变版本。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
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


class ChannelDescriptionTemplateCounterRow(Base):
    """频道级递增版本计数：模板硬删除后也不重用旧版本号。"""

    __tablename__ = "channel_description_template_counters"
    __table_args__ = (CheckConstraint("next_version > 1", name="next_version"),)

    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("channels.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    next_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")


class ChannelDescriptionTemplateRow(Base):
    __tablename__ = "channel_description_templates"
    __table_args__ = (
        UniqueConstraint("channel_id", "version", name="uq_channel_description_templates_version"),
        CheckConstraint("version > 0", name="version"),
        Index("ix_channel_description_templates_channel", "channel_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("channels.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectDescriptionAttemptRow(Base):
    """同步简介生成的持久幂等意图；RUNNING 部分唯一索引防同项目并发重复外呼。"""

    __tablename__ = "project_description_attempts"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')", name="status"),
        UniqueConstraint("idempotency_key", name="uq_project_description_attempts_key"),
        Index(
            "uq_description_attempt_active_project",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
            sqlite_where=text("status = 'RUNNING'"),
        ),
        Index("ix_project_description_attempts_project", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    template_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="RUNNING")
    error_code: Mapped[str | None] = mapped_column(String(128))
    description_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectDescriptionRow(Base):
    __tablename__ = "project_descriptions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_number"),
        CheckConstraint("source IN ('generated', 'manual')", name="source"),
        UniqueConstraint("project_id", "version_number", name="uq_project_descriptions_version"),
        Index(
            "uq_project_descriptions_current",
            "project_id",
            unique=True,
            postgresql_where=text("current"),
            sqlite_where=text("current = 1"),
        ),
        Index("ix_project_descriptions_project", "project_id", "version_number"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    base_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    prompt_template_version: Mapped[int | None] = mapped_column(Integer)
    prompt_template_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_template_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    title_used: Mapped[str] = mapped_column(Text, nullable=False)
    channel_name_used: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    srt_asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    srt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    disclaimer_key: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    provider_receipt: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    raw_response_sha256: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
