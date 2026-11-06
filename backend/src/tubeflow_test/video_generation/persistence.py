from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    select,
    text,
    update,
)
from sqlalchemy import UniqueConstraint as Unique
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.schema import conv
from sqlalchemy.types import Uuid

from tubeflow_test.adapters.persistence.identity_project.models import Base, ProjectRow
from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.adapters.persistence.storyboards.models import (
    StoryboardCurrentRow,
    StoryboardGenerationRow,
    StoryboardMediaRow,
)
from tubeflow_test.application.storyboards import StoryboardBindingAuthorityPort
from tubeflow_test.application.video_batch import (
    VideoBatchGroupState,
    VideoBatchItemView,
    VideoBatchJobView,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import VideoStoryboardBinding
from tubeflow_test.video_generation.domain import canonical_sha256
from tubeflow_test.video_generation.orchestration import (
    EXPERIMENT_ARM_CADENCE,
    AuthoritativeGroupInput,
    compile_video_prompt,
    derive_cadence_variant_prompt,
    experiment_block,
    prepare_manual_video_intent,
    require_experiment_source,
)
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import CompiledVideoPrompt, VideoPromptStrategy

VideoGenerationBase = Base


JsonObject = JSON().with_variant(JSONB(), "postgresql")
H3_NODE_CONFIG_LOCK_SCOPE = "tubeflow:h3-node-config"


class H3ProjectWorkflowRouteRecord(VideoGenerationBase):
    __tablename__ = "h3_project_workflow_routes"
    __table_args__ = (
        CheckConstraint("profile IN ('legacy', 'candidate')", name="profile"),
        CheckConstraint(
            "reference_strategy IS NULL OR reference_strategy IN ('composite', 'single_panel_v1')",
            name="reference_strategy",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    profile: Mapped[str] = mapped_column(String(16))
    workflow_snapshot_json: Mapped[dict[str, object] | None] = mapped_column(JsonObject)
    reference_strategy: Mapped[str | None] = mapped_column(String(32))
    reference_derivation_json: Mapped[dict[str, object] | None] = mapped_column(JsonObject)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VideoAttemptRecord(VideoGenerationBase):
    __tablename__ = "video_generation_attempts"
    __table_args__ = (
        Unique("owner_id", "idempotency_key", name="uq_video_attempt_owner_key"),
        Unique("attempt_id", "output_sha256", name="uq_video_attempt_output"),
        Unique(
            "attempt_id",
            "owner_id",
            "project_id",
            "group_id",
            name="uq_video_attempt_audit_identity",
        ),
        Index(
            "uq_video_attempt_active_group",
            "owner_id",
            "group_id",
            unique=True,
            postgresql_where=text(
                "stage IN ('CREATED','NOT_SUBMITTED','STAGING','SUBMITTING',"
                "'REMOTE_STATE_UNKNOWN','SUBMITTED','RUNNING')"
            ),
            sqlite_where=text(
                "stage IN ('CREATED','NOT_SUBMITTED','STAGING','SUBMITTING',"
                "'REMOTE_STATE_UNKNOWN','SUBMITTED','RUNNING')"
            ),
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), index=True
    )
    generation_id: Mapped[UUID] = mapped_column(
        ForeignKey("storyboard_generations.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    superseded_attempt_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    generation_sha256: Mapped[str] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict[str, object]] = mapped_column(JsonObject)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("h3_engine_nodes.node_id"), index=True)
    remote_id: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(String(32))
    staged_ids: Mapped[list[str]] = mapped_column(JsonObject, default=list, server_default="[]")
    cleaned_staged_ids: Mapped[list[str]] = mapped_column(
        JsonObject, default=list, server_default="[]"
    )
    archive_stage: Mapped[str] = mapped_column(String(32))
    output_id: Mapped[str | None] = mapped_column(Text)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    archived_asset_json: Mapped[dict[str, object] | None] = mapped_column(JsonObject)
    error_code: Mapped[str | None] = mapped_column(String(128))
    retry_evidence: Mapped[list[str]] = mapped_column(JsonObject, default=list, server_default="[]")
    slot_claimed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    slot_lease_token: Mapped[str | None] = mapped_column(String(64))
    slot_lease_owner: Mapped[str | None] = mapped_column(String(128))
    slot_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    narration_validation_json: Mapped[dict[str, object] | None] = mapped_column(JsonObject)
    intent_sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    input_snapshot_json: Mapped[dict[str, object] | None] = mapped_column(JsonObject)
    probe_evidence_json: Mapped[dict[str, object] | None] = mapped_column(JsonObject)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    output_media_type: Mapped[str | None] = mapped_column(String(64))
    remote_phase: Mapped[str | None] = mapped_column(String(32))
    remote_acceptance: Mapped[str | None] = mapped_column(String(32))
    cancel_dispatched: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    cancel_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    cancel_inspect_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    archive_confirm_dispatched: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    archive_confirm_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    archive_confirm_inspect_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    raw_receipt_json: Mapped[dict[str, object] | None] = mapped_column(JsonObject)
    remote_phase_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # §15.9 预算轮次：自动重提/QC 重生成与取消重提均在被取代尝试的轮次内计数。
    budget_round: Mapped[int] = mapped_column(Integer, server_default="1")
    requested_action: Mapped[str | None] = mapped_column(String(32))
    cleanup_terminal_stage: Mapped[str | None] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderAuditEventRecord(VideoGenerationBase):
    __tablename__ = "provider_audit_events"
    __table_args__ = (
        Unique("attempt_id", "event_sequence", name="uq_provider_audit_attempt_sequence"),
        CheckConstraint("event_sequence > 0", name="provider_audit_positive_sequence"),
        ForeignKeyConstraint(
            ["attempt_id", "owner_id", "project_id", "group_id"],
            [
                "video_generation_attempts.attempt_id",
                "video_generation_attempts.owner_id",
                "video_generation_attempts.project_id",
                "video_generation_attempts.group_id",
            ],
            name="fk_provider_audit_attempt_identity",
            ondelete="CASCADE",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    attempt_id: Mapped[UUID] = mapped_column(index=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), index=True
    )
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(32))
    node_id: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(512))
    workflow_id: Mapped[str] = mapped_column(String(128))
    workflow_version: Mapped[str] = mapped_column(String(64))
    model_key: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64))
    local_idempotency_key: Mapped[str] = mapped_column(String(128))
    provider_task_key: Mapped[str | None] = mapped_column(Text)
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    receipt_json: Mapped[dict[str, object]] = mapped_column(JsonObject)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(128))


class H3NodeRecord(VideoGenerationBase):
    __tablename__ = "h3_engine_nodes"
    __table_args__ = (
        CheckConstraint("capacity > 0", name="positive_capacity"),
        CheckConstraint("claimed_slots >= 0", name="claimed_capacity"),
    )

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(Text)
    instance_identity: Mapped[str] = mapped_column(String(128), default="test-instance")
    token_secret_ref: Mapped[str] = mapped_column(Text)
    token_sha256: Mapped[str] = mapped_column(String(64), default="")
    capacity: Mapped[int] = mapped_column(Integer)
    claimed_slots: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    health: Mapped[str] = mapped_column(String(16), default="READY", server_default="READY")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capability: Mapped[str] = mapped_column(String(128))
    model_key: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(128))
    workflow_version: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))


class VideoOutboxRecord(VideoGenerationBase):
    __tablename__ = "video_generation_outbox"
    __table_args__ = (
        CheckConstraint(
            "terminal_state IS NULL OR terminal_state IN ('UNKNOWN', 'DISPATCHED', 'DEAD_LETTER')",
            name=conv("ck_video_outbox_terminal_state"),
        ),
        Index("ix_video_outbox_delivery_due", "terminal_state", "next_attempt_at"),
    )

    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("video_generation_attempts.attempt_id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(128))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    terminal_state: Mapped[str | None] = mapped_column(String(16))
    lease_token: Mapped[str | None] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GroupCurrentVideoRecord(VideoGenerationBase):
    __tablename__ = "group_current_videos"

    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), primary_key=True
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("video_generation_attempts.attempt_id"), unique=True, nullable=False
    )
    generation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    intent_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


VIDEO_GENERATION_METADATA = VideoGenerationBase.metadata
VIDEO_GENERATION_TABLES = (
    H3NodeRecord.__table__,
    VideoAttemptRecord.__table__,
    VideoOutboxRecord.__table__,
    GroupCurrentVideoRecord.__table__,
    ProviderAuditEventRecord.__table__,
)


class VideoBatchJobRecord(VideoGenerationBase):
    __tablename__ = "video_batch_jobs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    force: Mapped[bool] = mapped_column(Boolean, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VideoBatchItemRecord(VideoGenerationBase):
    __tablename__ = "video_batch_items"

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("video_batch_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    message: Mapped[str | None] = mapped_column(String(256), nullable=True)


class ManualH3IntentWriter:
    """User-click transaction: canonical H3 attempt, outbox and storyboard binding."""

    def __init__(
        self,
        unit_of_work: Callable[[], AbstractContextManager[Session]],
        storyboard_authority: StoryboardBindingAuthorityPort,
        group_loader: Callable[[Session, UUID, UUID], AuthoritativeGroupInput],
        prompt_compiler: VideoPromptCompiler | None = None,
        intent_group_loader: Callable[..., AuthoritativeGroupInput] | None = None,
    ) -> None:
        self._uow = unit_of_work
        self._storyboard_authority = storyboard_authority
        self._group_loader = group_loader
        self._prompt_compiler = prompt_compiler
        self._intent_group_loader = intent_group_loader

    def create(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        expected_storyboard_generation_id: UUID,
        idempotency_key: str,
        request: dict[str, object] | None = None,
        seed: int | None = None,
        experiment_arm: str | None = None,
        prompt_source_attempt_id: UUID | None = None,
    ) -> UUID:
        # §51 抖动 A/B 实验臂（缺省 None 时行为不变）：请求身份写入快照 experiment 键。
        if experiment_arm is None and prompt_source_attempt_id is not None:
            raise DomainError(
                "experiment_request_invalid", "A prompt source requires an experiment arm"
            )
        experiment = (
            None
            if experiment_arm is None
            else experiment_block(experiment_arm, prompt_source_attempt_id)
        )
        # LLM 提示词编译放在写事务之外：只读预取 + 编译。预取失败不裁决——
        # 事务内按既有守卫顺序（purging/媒体/并发/血缘）复现 canonical 错误，
        # 指纹二次校验仍在 prepare_manual_video_intent 内执行。
        preview: AuthoritativeGroupInput | None = None
        try:
            with self._uow() as session:
                candidate = self._group_loader(session, owner_id, group_id)
            if candidate.storyboard_generation_id == expected_storyboard_generation_id:
                preview = candidate
        except Exception:
            preview = None
        attempt_id = uuid4()
        compiled = (
            self._prompt(
                preview,
                owner_id,
                expected_storyboard_generation_id,
                attempt_id,
                idempotency_key,
                seed,
                prompt_source_attempt_id,
                experiment is not None,
            )
            if preview is not None
            else None
        )
        try:
            return self._create_once(
                owner_id=owner_id,
                group_id=group_id,
                expected_storyboard_generation_id=expected_storyboard_generation_id,
                idempotency_key=idempotency_key,
                request=request,
                compiled=compiled,
                attempt_id=attempt_id,
                seed=seed,
                experiment=experiment,
            )
        except IntegrityError:
            # Concurrent first-use races resolve through the owner/key unique row.
            return self._create_once(
                owner_id=owner_id,
                group_id=group_id,
                expected_storyboard_generation_id=expected_storyboard_generation_id,
                idempotency_key=idempotency_key,
                request=request,
                compiled=compiled,
                attempt_id=attempt_id,
                seed=seed,
                experiment=experiment,
            )

    def _prompt(
        self,
        source: AuthoritativeGroupInput,
        owner_id: UUID,
        storyboard_generation_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
        seed: int | None,
        prompt_source_attempt_id: UUID | None,
        experimental: bool,
    ) -> CompiledVideoPrompt:
        """普通请求与基线臂照常编译；节奏臂从同组基线臂派生，不调用编译器。"""
        if experimental:
            require_experiment_source(source)
        if prompt_source_attempt_id is None:
            return self._compile(source, attempt_id, idempotency_key, seed)
        with self._uow() as session:
            baseline = session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.attempt_id == prompt_source_attempt_id,
                    VideoAttemptRecord.owner_id == owner_id,
                )
            )
            if (
                baseline is None
                or baseline.group_id != source.group_id
                or baseline.generation_id != storyboard_generation_id
            ):
                raise DomainError(
                    "experiment_baseline_not_found",
                    "The baseline attempt is not on this group and storyboard",
                )
            snapshot = dict(baseline.input_snapshot_json or {})
        return derive_cadence_variant_prompt(
            source,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            seed=seed,
            prompt_source_attempt_id=prompt_source_attempt_id,
            baseline_snapshot=snapshot,
        )

    def _compile(
        self,
        source: AuthoritativeGroupInput,
        attempt_id: UUID,
        idempotency_key: str,
        seed: int | None,
    ) -> CompiledVideoPrompt:
        if self._prompt_compiler is None:
            raise DomainError(
                "video_prompt_compiler_not_configured",
                "Video prompt compiler is not configured",
            )
        return compile_video_prompt(
            source,
            self._prompt_compiler,
            attempt_id=attempt_id,
            idempotency_key=idempotency_key,
            seed=seed,
        )

    def get(self, owner_id: UUID, attempt_id: UUID) -> VideoAttemptRecord | None:
        with self._uow() as session:
            return session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.attempt_id == attempt_id,
                    VideoAttemptRecord.owner_id == owner_id,
                )
            )

    def _create_once(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        expected_storyboard_generation_id: UUID,
        idempotency_key: str,
        request: dict[str, object] | None,
        compiled: CompiledVideoPrompt | None,
        attempt_id: UUID,
        seed: int | None = None,
        experiment: dict[str, object] | None = None,
    ) -> UUID:
        now = datetime.now(UTC)
        with self._uow() as session:
            project_id = session.scalar(
                select(AuthoritativeNarrationGroupRecord.project_id).where(
                    AuthoritativeNarrationGroupRecord.id == group_id,
                    AuthoritativeNarrationGroupRecord.owner_id == owner_id,
                )
            )
            if project_id is None:
                raise DomainError("not_found", "H3 project was not found")
            project_state = session.scalar(
                select(ProjectRow.production_state)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .with_for_update()
            )
            if project_state is None:
                raise DomainError("not_found", "H3 project was not found")
            existing = session.scalar(
                select(VideoAttemptRecord)
                .where(
                    VideoAttemptRecord.owner_id == owner_id,
                    VideoAttemptRecord.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            if existing is not None:
                snapshot = existing.input_snapshot_json or {}
                existing_request = snapshot.get("manual_request", {})
                # 显式 seed 参与幂等比较（2026-11-03 用户决策 4）：同键换 seed 视为另一请求；
                # 未显式指定时保持既有语义（旧键不会因此冲突）。
                if (
                    existing.group_id != group_id
                    or existing.generation_id != expected_storyboard_generation_id
                    or existing_request != (request or {})
                    or (seed is not None and snapshot.get("seed") != seed)
                    # §51：实验身份（含无实验）严格参与比较；旧快照无该键即 None。
                    or snapshot.get("experiment") != experiment
                ):
                    raise DomainError("idempotency_conflict", "H3 key is bound to another request")
                return existing.attempt_id
            if project_state != "ACTIVE":
                raise DomainError("project_purging", "Project is being purged")
            generation = session.execute(
                select(
                    StoryboardGenerationRow.id,
                    StoryboardGenerationRow.project_id,
                    StoryboardGenerationRow.group_id,
                    StoryboardGenerationRow.applied_revision_id,
                ).where(
                    StoryboardGenerationRow.id == expected_storyboard_generation_id,
                    StoryboardGenerationRow.project_id == project_id,
                    StoryboardGenerationRow.group_id == group_id,
                    StoryboardGenerationRow.owner_id == owner_id,
                )
            ).one()
            current = session.scalar(
                select(StoryboardCurrentRow)
                .where(
                    StoryboardCurrentRow.owner_id == owner_id,
                    StoryboardCurrentRow.group_id == group_id,
                )
                .with_for_update()
            )
            if current is None or current.generation_id != expected_storyboard_generation_id:
                raise DomainError("revision_conflict", "H3 intent requires the current storyboard")
            active = session.scalar(
                select(VideoAttemptRecord.attempt_id)
                .where(
                    VideoAttemptRecord.owner_id == owner_id,
                    VideoAttemptRecord.group_id == group_id,
                    VideoAttemptRecord.stage.in_(
                        (
                            "CREATED",
                            "NOT_SUBMITTED",
                            "STAGING",
                            "SUBMITTING",
                            "REMOTE_STATE_UNKNOWN",
                            "SUBMITTED",
                            "RUNNING",
                        )
                    ),
                )
                .with_for_update()
            )
            if active is not None:
                raise DomainError(
                    "active_group_video_attempt_exists",
                    "An unresolved H3 attempt already exists for this group",
                )
            # Single-panel verification briefly locks the source asset in its own
            # session; finish that read before this transaction locks the same row.
            if self._intent_group_loader is None:
                source = self._group_loader(session, owner_id, group_id)
            else:
                source = self._intent_group_loader(
                    session, owner_id, group_id, for_intent=True
                )
            if source.storyboard_generation_id != expected_storyboard_generation_id:
                raise DomainError("revision_conflict", "H3 intent requires the current storyboard")
            try:
                media = session.execute(
                    select(StoryboardMediaRow.generation_id, MediaAssetRow.sha256)
                    .join(MediaAssetRow, MediaAssetRow.id == StoryboardMediaRow.asset_id)
                    .join(
                        ProjectAssetReferenceRow,
                        (ProjectAssetReferenceRow.asset_id == MediaAssetRow.id)
                        & (ProjectAssetReferenceRow.project_id == generation.project_id),
                    )
                    .where(
                        StoryboardMediaRow.generation_id == generation.id,
                        MediaAssetRow.owner_id == owner_id,
                        MediaAssetRow.kind == "STORYBOARD_GRID",
                        MediaAssetRow.purging.is_(False),
                        MediaAssetRow.deletion_state == "ACTIVE",
                        ProjectAssetReferenceRow.owner_id == owner_id,
                    )
                    .with_for_update()
                ).one()
            except NoResultFound as error:
                raise DomainError(
                    "h3_inputs_not_ready", "Canonical storyboard media is unavailable"
                ) from error
            binding = VideoStoryboardBinding(
                attempt_id,
                owner_id,
                generation.project_id,
                group_id,
                generation.id,
                generation.applied_revision_id,
                media.sha256,
            )
            self._storyboard_authority.bind_current_in_transaction(session, binding)
            if compiled is None:
                raise DomainError(
                    "video_prompt_compiler_input_changed",
                    "Video prompt compiler input changed before persistence",
                )
            input_snapshot, generation_sha256, provider_request, request_sha256 = (
                prepare_manual_video_intent(
                    source,
                    attempt_id,
                    compiled=compiled,
                    idempotency_key=idempotency_key,
                    seed=seed,
                )
            )
            input_snapshot["manual_request"] = request or {}
            if experiment is not None:
                expected_strategy = (
                    VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_SINGLE_PANEL_CADENCE_V1
                    if experiment["arm"] == EXPERIMENT_ARM_CADENCE
                    else VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_SINGLE_PANEL
                )
                if compiled.strategy is not expected_strategy:
                    raise DomainError(
                        "experiment_requires_v4_single_panel",
                        "The experiment arm did not resolve to its prompt strategy",
                    )
                input_snapshot["experiment"] = experiment
            generation_sha256 = canonical_sha256(input_snapshot)
            session.add(
                VideoAttemptRecord(
                    attempt_id=attempt_id,
                    owner_id=owner_id,
                    project_id=binding.project_id,
                    group_id=binding.group_id,
                    generation_id=binding.storyboard_generation_id,
                    idempotency_key=idempotency_key,
                    generation_sha256=generation_sha256,
                    request_sha256=request_sha256,
                    request_json=provider_request,
                    stage="CREATED",
                    staged_ids=[],
                    cleaned_staged_ids=[],
                    archive_stage="NOT_STARTED",
                    retry_evidence=[],
                    slot_claimed=False,
                    intent_sequence=0,
                    input_snapshot_json=input_snapshot,
                    revision=1,
                    updated_at=now,
                )
            )
            session.flush()
            session.add(
                VideoOutboxRecord(
                    attempt_id=attempt_id,
                    owner_id=binding.owner_id,
                    event_type="VIDEO_GENERATION_REQUESTED",
                    created_at=now,
                    next_attempt_at=now,
                )
            )
        return attempt_id


class SqlVideoBatchRepository:
    """批量 H3 编排所需的项目分组状态只读视图（PostgreSQL 权威状态）。"""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def list_group_states(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[VideoBatchGroupState, ...]:
        latest_stage = (
            select(VideoAttemptRecord.stage)
            .where(
                VideoAttemptRecord.owner_id == owner_id,
                VideoAttemptRecord.group_id == AuthoritativeNarrationGroupRecord.id,
            )
            .order_by(VideoAttemptRecord.intent_sequence.desc())
            .limit(1)
            .scalar_subquery()
            .label("latest_stage")
        )
        latest_stalled = (
            select(VideoAttemptRecord.stalled_at)
            .where(
                VideoAttemptRecord.owner_id == owner_id,
                VideoAttemptRecord.group_id == AuthoritativeNarrationGroupRecord.id,
            )
            .order_by(VideoAttemptRecord.intent_sequence.desc())
            .limit(1)
            .scalar_subquery()
            .label("latest_stalled")
        )
        with self._sessions() as session:
            rows = (
                session.execute(
                    select(
                        AuthoritativeNarrationGroupRecord.id,
                        AuthoritativeNarrationGroupRecord.sequence,
                        AuthoritativeNarrationGroupRecord.video_blocked,
                        AuthoritativeNarrationGroupRecord.block_reason,
                        StoryboardCurrentRow.generation_id,
                        GroupCurrentVideoRecord.attempt_id,
                        latest_stage,
                        latest_stalled,
                    )
                    .outerjoin(
                        StoryboardCurrentRow,
                        (
                            StoryboardCurrentRow.owner_id
                            == AuthoritativeNarrationGroupRecord.owner_id
                        )
                        & (StoryboardCurrentRow.group_id == AuthoritativeNarrationGroupRecord.id),
                    )
                    .outerjoin(
                        GroupCurrentVideoRecord,
                        GroupCurrentVideoRecord.group_id == AuthoritativeNarrationGroupRecord.id,
                    )
                    .where(
                        AuthoritativeNarrationGroupRecord.owner_id == owner_id,
                        AuthoritativeNarrationGroupRecord.project_id == project_id,
                    )
                    .order_by(AuthoritativeNarrationGroupRecord.sequence)
                )
                .mappings()
                .all()
            )
        return tuple(
            VideoBatchGroupState(
                group_id=row["id"],
                sequence=int(row["sequence"]),
                video_blocked=bool(row["video_blocked"]),
                block_reason=row["block_reason"],
                storyboard_generation_id=row["generation_id"],
                has_current_video=row["attempt_id"] is not None,
                latest_attempt_stage=row["latest_stage"],
                latest_attempt_stalled=row["latest_stalled"] is not None,
            )
            for row in rows
        )


class SqlVideoBatchJobRepository:
    """批量 H3 任务的持久化：创建、查询、逐项推进与续跑枚举。"""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def create_job(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        force: bool,
        items: tuple[tuple[UUID, int, str], ...],
    ) -> UUID:
        job_id = uuid4()
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            session.add(
                VideoBatchJobRecord(
                    id=job_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    force=force,
                    total=len(items),
                    created_at=now,
                    completed_at=None,
                )
            )
            session.flush()
            for group_id, sequence, status in items:
                session.add(
                    VideoBatchItemRecord(
                        job_id=job_id,
                        group_id=group_id,
                        sequence=sequence,
                        status=status,
                    )
                )
        return job_id

    def get_job(self, job_id: UUID) -> VideoBatchJobView | None:
        with self._sessions() as session:
            job = session.get(VideoBatchJobRecord, job_id)
            if job is None:
                return None
            items = session.scalars(
                select(VideoBatchItemRecord)
                .where(VideoBatchItemRecord.job_id == job_id)
                .order_by(VideoBatchItemRecord.sequence)
            ).all()
            return VideoBatchJobView(
                job_id=job.id,
                owner_id=job.owner_id,
                project_id=job.project_id,
                force=job.force,
                total=job.total,
                created_at=job.created_at,
                completed_at=job.completed_at,
                items=tuple(
                    VideoBatchItemView(
                        group_id=item.group_id,
                        sequence=item.sequence,
                        status=item.status,
                        attempt_id=item.attempt_id,
                        message=item.message,
                    )
                    for item in items
                ),
            )

    def next_pending_group(self, job_id: UUID) -> tuple[UUID, int] | None:
        with self._sessions() as session:
            row = session.scalar(
                select(VideoBatchItemRecord)
                .where(
                    VideoBatchItemRecord.job_id == job_id,
                    VideoBatchItemRecord.status == "PENDING",
                )
                .order_by(VideoBatchItemRecord.sequence)
                .limit(1)
            )
            return (row.group_id, row.sequence) if row is not None else None

    def mark_item(
        self,
        job_id: UUID,
        group_id: UUID,
        status: str,
        *,
        attempt_id: UUID | None = None,
        message: str | None = None,
    ) -> None:
        with self._sessions() as session, session.begin():
            row = session.get(VideoBatchItemRecord, (job_id, group_id))
            if row is None:
                return
            row.status = status
            row.attempt_id = attempt_id
            row.message = message[:256] if message else None

    def complete_job(self, job_id: UUID) -> None:
        with self._sessions() as session, session.begin():
            row = session.get(VideoBatchJobRecord, job_id)
            if row is not None and row.completed_at is None:
                row.completed_at = datetime.now(UTC)

    def list_unfinished_job_ids(self) -> tuple[UUID, ...]:
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(VideoBatchJobRecord.id).where(VideoBatchJobRecord.completed_at.is_(None))
                ).all()
            )

    def reopen_failed_items(self, job_id: UUID) -> int:
        """新一轮授权开始时把上一轮终态失败项翻回 PENDING（attempt 历史保留在别处）。"""
        with self._sessions() as session, session.begin():
            result = session.execute(
                update(VideoBatchItemRecord)
                .where(
                    VideoBatchItemRecord.job_id == job_id,
                    VideoBatchItemRecord.status.in_(
                        (
                            "error",
                            "not_found",
                            "no_current_storyboard",
                            "submission_budget_exhausted",
                        )
                    ),
                )
                .values(status="PENDING", attempt_id=None, message=None)
            )
            return int(result.rowcount or 0)

    def oldest_waiting_job_id(self) -> UUID | None:
        """最早进入「等待 H3 容量」的未完成 batch job（多项目 FIFO 的队首）。"""
        with self._sessions() as session:
            return session.scalar(
                select(VideoBatchJobRecord.id)
                .join(VideoBatchItemRecord, VideoBatchItemRecord.job_id == VideoBatchJobRecord.id)
                .where(
                    VideoBatchItemRecord.status == "WAITING_CAPACITY",
                    VideoBatchJobRecord.completed_at.is_(None),
                )
                .order_by(VideoBatchJobRecord.created_at.asc(), VideoBatchJobRecord.id.asc())
                .limit(1)
            )

    def job_force(self, job_id: UUID) -> bool | None:
        with self._sessions() as session:
            row = session.get(VideoBatchJobRecord, job_id)
            return row.force if row is not None else None
