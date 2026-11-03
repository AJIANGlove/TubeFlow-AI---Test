from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tubeflow_test.adapters.persistence.identity_project.models import Base
from tubeflow_test.adapters.persistence.identity_project.models import (
    StagedObjectRow as StagedObjectRow,
)


class MediaAssetRow(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint(
            "ownership_scope IN ('GLOBAL', 'OWNER', 'PROJECT')", name="ownership_scope"
        ),
        CheckConstraint(
            "ownership_scope != 'PROJECT' OR project_id IS NOT NULL",
            name="project_scope_requires_project",
        ),
        CheckConstraint("deletion_state IN ('ACTIVE', 'DELETING')", name="deletion_state"),
        UniqueConstraint("object_key", name="uq_media_assets_object_key"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    ownership_scope: Mapped[str] = mapped_column(
        String(16), default="PROJECT", server_default="PROJECT"
    )
    kind: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), default=dict
    )
    shared: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    purging: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    deletion_state: Mapped[str] = mapped_column(
        String(16), default="ACTIVE", server_default="ACTIVE"
    )
    deletion_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    deletion_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class DeletionTombstoneRow(Base):
    __tablename__ = "deletion_tombstones"
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    object_count: Mapped[int] = mapped_column(Integer)


class CompositionVersionRow(Base):
    __tablename__ = "composition_versions"
    __table_args__ = (
        Index(
            "uq_composition_current_project",
            "project_id",
            unique=True,
            postgresql_where=text("current"),
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    video_asset_id: Mapped[UUID] = mapped_column(ForeignKey("media_assets.id"))
    srt_asset_id: Mapped[UUID] = mapped_column(ForeignKey("media_assets.id"))
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    intent_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    current: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class ProjectPurgeJobRow(Base):
    __tablename__ = "project_purge_jobs"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(24))
    dispatch_error_code: Mapped[str | None] = mapped_column(String(64))
    object_results: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    workflow_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    execution_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PurgeOutboxRow(Base):
    __tablename__ = "purge_outbox"
    __table_args__ = (UniqueConstraint("project_id", name="uq_purge_outbox_project_id"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_purge_jobs.project_id", ondelete="CASCADE")
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    workflow_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class CompositionJobRow(Base):
    __tablename__ = "composition_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_composition_jobs_idempotency_key"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(24))
    status_detail: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(128))
    intent_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    workflow_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    composition_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class CompositionInputRevisionRow(Base):
    __tablename__ = "composition_input_revisions"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    inputs_sha256: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)


class CompositionOutboxRow(Base):
    __tablename__ = "composition_outbox"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_composition_outbox_revision_id"),
        UniqueConstraint("workflow_id", name="uq_composition_outbox_workflow_id"),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("composition_input_revisions.id", ondelete="CASCADE")
    )
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(24))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    workflow_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    # §15.9：同一 inputs_hash 的提交预算按预算轮次独立计数（恢复后重置）。
    budget_round: Mapped[int] = mapped_column(Integer, server_default="1")


class ProjectAssetReferenceRow(Base):
    __tablename__ = "project_asset_references"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), primary_key=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )


class ProjectPurgeObjectRow(Base):
    __tablename__ = "project_purge_objects"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_purge_jobs.project_id", ondelete="CASCADE"), primary_key=True
    )
    object_key: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    staged_object_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    object_kind: Mapped[str] = mapped_column(String(16), default="MEDIA", server_default="MEDIA")
    sha256: Mapped[str | None] = mapped_column(String(64))
    owner_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    external_reference_count: Mapped[int] = mapped_column(Integer)
    deletion_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    deletion_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
