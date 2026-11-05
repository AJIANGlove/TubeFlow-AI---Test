from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from tubeflow_test.adapters.persistence.identity_project.models import Base


class DirectorRevisionRow(Base):
    __tablename__ = "storyboard_director_revisions"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "group_id",
            "revision_number",
            name="uq_storyboard_director_revisions_owner_group_revision",
        ),
        UniqueConstraint("owner_id", "id", name="uq_storyboard_director_revisions_owner_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    shots_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    links_json: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    group_sentence_ids_json: Mapped[list[str]] = mapped_column(JSON)
    review_state: Mapped[str] = mapped_column(String(16))
    continuous_observation_approvals_json: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # §48.3（迁移 0047）：系统放行的组内非相邻构图回切；存量行为空列表。
    composition_recut_approvals_json: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )


class DirectorPointerRow(Base):
    __tablename__ = "storyboard_director_pointers"
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    draft_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("storyboard_director_revisions.id", ondelete="SET NULL")
    )
    applied_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("storyboard_director_revisions.id", ondelete="SET NULL")
    )


class StoryboardGenerationRow(Base):
    __tablename__ = "storyboard_generations"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_storyboard_generations_owner_key"),
        UniqueConstraint(
            "owner_id", "group_id", "version", name="uq_storyboard_generations_owner_group_version"
        ),
        UniqueConstraint("owner_id", "id", name="uq_storyboard_generations_owner_id"),
        Index(
            "ix_storyboard_generations_failover_chain",
            "owner_id",
            "failover_chain_id",
        ),
        CheckConstraint(
            "failover_route_index BETWEEN 0 AND 6",
            name="ck_storyboard_failover_route_index",
        ),
        CheckConstraint(
            "failover_model_attempt BETWEEN 1 AND 2",
            name="ck_storyboard_failover_model_attempt",
        ),
        CheckConstraint(
            "failover_total_attempt BETWEEN 1 AND 14",
            name="ck_storyboard_failover_total_attempt",
        ),
        CheckConstraint(
            "(provider IS NULL AND failover_chain_id IS NULL "
            "AND failover_policy_version IS NULL AND failover_route_index IS NULL "
            "AND failover_model_attempt IS NULL AND failover_total_attempt IS NULL) "
            "OR (provider IS NOT NULL AND failover_chain_id IS NOT NULL "
            "AND failover_policy_version IS NOT NULL AND failover_route_index IS NOT NULL "
            "AND failover_model_attempt IS NOT NULL AND failover_total_attempt IS NOT NULL)",
            name="ck_sb_failover_fields_all_or_none",
        ),
        UniqueConstraint(
            "owner_id",
            "failover_chain_id",
            "failover_total_attempt",
            name="uq_storyboard_failover_chain_total",
        ),
        UniqueConstraint(
            "owner_id",
            "failover_chain_id",
            "failover_route_index",
            "failover_model_attempt",
            name="uq_storyboard_failover_chain_model_attempt",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), index=True
    )
    applied_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_director_revisions.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    input_json: Mapped[dict[str, object]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    provider_idempotency_key: Mapped[str] = mapped_column(String(128))
    canonical_request_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    state_revision: Mapped[int] = mapped_column(Integer, default=0)
    execution_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider_task_id: Mapped[str | None] = mapped_column(Text)
    provider_receipt: Mapped[str | None] = mapped_column(Text)
    checkpoint_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    asset_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    provider: Mapped[str | None] = mapped_column(String(32))
    failover_chain_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    failover_policy_version: Mapped[str | None] = mapped_column(String(64))
    failover_route_index: Mapped[int | None] = mapped_column(Integer)
    failover_model_attempt: Mapped[int | None] = mapped_column(Integer)
    failover_total_attempt: Mapped[int | None] = mapped_column(Integer)


class StoryboardCurrentRow(Base):
    __tablename__ = "storyboard_current"
    __table_args__ = (
        UniqueConstraint("generation_id", name="uq_storyboard_current_generation_id"),
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), primary_key=True
    )
    generation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("storyboard_generations.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, default=0)


class StoryboardMediaRow(Base):
    __tablename__ = "storyboard_media"
    __table_args__ = (UniqueConstraint("storyboard_asset_id", name="uq_storyboard_media_asset_id"),)
    generation_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_generations.id", ondelete="CASCADE"), primary_key=True
    )
    storyboard_asset_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True
    )
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    image_format: Mapped[str] = mapped_column(String(16))


class VideoStoryboardBindingRow(Base):
    __tablename__ = "video_storyboard_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["owner_id", "storyboard_generation_id"],
            ["storyboard_generations.owner_id", "storyboard_generations.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "applied_revision_id"],
            ["storyboard_director_revisions.owner_id", "storyboard_director_revisions.id"],
            ondelete="CASCADE",
        ),
    )
    video_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(index=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), index=True
    )
    storyboard_generation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    applied_revision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    storyboard_asset_sha256: Mapped[str] = mapped_column(String(64))


class DirectorGenerationAttemptRow(Base):
    __tablename__ = "director_generation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_director_generation_attempts_owner_key"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), index=True
    )
    expected_draft_revision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    provider_idempotency_key: Mapped[str] = mapped_column(String(64))
    canonical_request_sha256: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict[str, object]] = mapped_column(JSON)
    prompt_ref: Mapped[str] = mapped_column(String(64))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    prompt_content: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(128))
    parameters_json: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    state_revision: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_receipt: Mapped[str | None] = mapped_column(Text)
    raw_response_sha256: Mapped[str | None] = mapped_column(String(64))
    draft_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("storyboard_director_revisions.id", ondelete="SET NULL")
    )
    failure_code: Mapped[str | None] = mapped_column(String(128))
    history_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryboardOutboxRow(Base):
    __tablename__ = "storyboard_outbox"
    __table_args__ = (UniqueConstraint("workflow_id", name="uq_storyboard_outbox_workflow_id"),)
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(192))
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
