from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from tubeflow_test.adapters.persistence.identity_project.models import Base


class ProjectDirectorRunRow(Base):
    __tablename__ = "project_director_runs"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_project_director_runs_owner_key"),
        UniqueConstraint("owner_id", "id", name="uq_project_director_runs_owner_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    canonical_input_sha256: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict[str, object]] = mapped_column(JSON)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_revision: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(128))
    parameters_json: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    total_units: Mapped[int] = mapped_column(Integer)
    state_revision: Mapped[int] = mapped_column(Integer, default=0)
    auto_retries_used: Mapped[int] = mapped_column(Integer, default=0)
    revision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "project_director_revisions.id",
            name="fk_project_director_runs_revision_id_project_director_revisions",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    failure_code: Mapped[str | None] = mapped_column(String(128))
    retry_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_recovery_audit_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    auto_input_change_audit_json: Mapped[dict[str, object] | None] = mapped_column(JSON)
    recovery_source_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "project_director_runs.id",
            name="fk_project_director_runs_recovery_source_run_id",
            ondelete="SET NULL",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectDirectorUnitRow(Base):
    __tablename__ = "project_director_units"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_project_director_units_run_ordinal"),
        UniqueConstraint(
            "owner_id",
            "provider_idempotency_key",
            name="uq_project_director_units_owner_provider_key",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_director_runs.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    sentences_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    context_before_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    context_after_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    provider_idempotency_key: Mapped[str] = mapped_column(String(64))
    canonical_request_sha256: Mapped[str] = mapped_column(String(64))
    prompt_ref: Mapped[str] = mapped_column(String(64))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    prompt_content: Mapped[str] = mapped_column(Text)
    required_locale: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(128))
    parameters_json: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    state_revision: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_failure_code: Mapped[str | None] = mapped_column(String(128))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_receipt: Mapped[str | None] = mapped_column(Text)
    raw_response_sha256: Mapped[str | None] = mapped_column(String(64))
    response_diagnostics_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    shots_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    links_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    failure_code: Mapped[str | None] = mapped_column(String(128))
    plan_document: Mapped[str | None] = mapped_column(Text)
    plan_document_sha256: Mapped[str | None] = mapped_column(String(64))
    reused_from_unit_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "project_director_units.id",
            name="fk_project_director_units_reused_from_unit_id",
            ondelete="SET NULL",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectDirectorRevisionRow(Base):
    __tablename__ = "project_director_revisions"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_project_director_revisions_run"),
        UniqueConstraint(
            "owner_id",
            "project_id",
            "revision_number",
            name="uq_project_director_revisions_owner_project_revision",
        ),
        UniqueConstraint("owner_id", "id", name="uq_project_director_revisions_owner_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("project_director_runs.id", ondelete="CASCADE"))
    revision_number: Mapped[int] = mapped_column(Integer)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_revision: Mapped[int] = mapped_column(Integer)
    sentence_ids_json: Mapped[list[str]] = mapped_column(JSON)
    shots_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    links_json: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    quality_conflicts_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectDirectorCurrentRow(Base):
    __tablename__ = "project_director_current"
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_director_revisions.id", ondelete="SET NULL")
    )
    pointer_version: Mapped[int] = mapped_column(Integer, default=0)


class ProjectDirectorMaterializationRow(Base):
    __tablename__ = "project_director_materializations"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "canonical_input_sha256",
            name="uq_project_director_materializations_owner_input",
        ),
        UniqueConstraint("owner_id", "id", name="uq_project_director_materializations_owner_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    preplan_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_director_revisions.id", ondelete="CASCADE")
    )
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_revision: Mapped[int] = mapped_column(Integer)
    algorithm_version: Mapped[str] = mapped_column(String(64))
    canonical_input_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProjectDirectorGroupCandidateRow(Base):
    __tablename__ = "project_director_group_candidates"
    __table_args__ = (
        UniqueConstraint(
            "materialization_id",
            "group_id",
            name="uq_project_director_group_candidates_materialization_group",
        ),
        UniqueConstraint(
            "materialization_id",
            "group_sequence",
            name="uq_project_director_group_candidates_materialization_sequence",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    materialization_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_director_materializations.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), index=True
    )
    group_sequence: Mapped[int] = mapped_column(Integer)
    shots_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    links_json: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    source_shot_links_json: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    conflicts_json: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    review_state: Mapped[str] = mapped_column(String(16))
    final_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("storyboard_director_revisions.id", ondelete="SET NULL")
    )


class DirectorRevisionLineageRow(Base):
    __tablename__ = "director_revision_lineage"
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_director_revisions.id", ondelete="CASCADE"), primary_key=True
    )
    project_director_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_director_revisions.id", ondelete="CASCADE"), index=True
    )
    materialization_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_director_materializations.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_director_group_candidates.id", ondelete="CASCADE")
    )
