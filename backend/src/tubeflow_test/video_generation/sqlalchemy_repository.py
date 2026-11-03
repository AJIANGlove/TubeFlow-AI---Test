from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, nulls_first, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.storyboards.models import StoryboardCurrentRow
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    VideoCapacityUnavailable,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.engine import EngineNode, NodeHealth
from tubeflow_test.video_generation.orchestration import (
    SUBMISSION_BUDGET_EXHAUSTED,
    ArchiveStage,
    AttemptStage,
    AuthoritativeGroupInput,
    PersistentAttempt,
    RequestedAction,
    validate_attempt_transition,
    validate_reference_intent,
)
from tubeflow_test.video_generation.persistence import (
    H3_NODE_CONFIG_LOCK_SCOPE,
    GroupCurrentVideoRecord,
    H3NodeRecord,
    ProviderAuditEventRecord,
    VideoAttemptRecord,
    VideoOutboxRecord,
)
from tubeflow_test.video_generation.provider_audit import ProviderAuditEvent

SessionFactory = Callable[[], Session]
GroupLoader = Callable[[Session, UUID, UUID], AuthoritativeGroupInput]
TokenResolver = Callable[[str], str]

logger = logging.getLogger(__name__)

_CAPACITY_WARNING_MIN_INTERVAL_SECONDS = 60.0
_last_capacity_warning_at = 0.0


class SqlAlchemyVideoRepository:
    """PostgreSQL repository with transaction-scoped attempt and capacity mutations."""

    def __init__(
        self,
        sessions: SessionFactory,
        group_loader: GroupLoader,
        token_resolver: TokenResolver,
        intent_group_loader: Callable[..., AuthoritativeGroupInput] | None = None,
    ) -> None:
        self._sessions = sessions
        self._group_loader = group_loader
        self._token_resolver = token_resolver
        self._intent_group_loader = intent_group_loader

    def append_provider_event(self, event: ProviderAuditEvent) -> None:
        with self._sessions() as session, session.begin():
            session.execute(
                select(VideoAttemptRecord.attempt_id)
                .where(VideoAttemptRecord.attempt_id == event.attempt_id)
                .with_for_update()
            ).one()
            sequence = (
                session.scalar(
                    select(func.max(ProviderAuditEventRecord.event_sequence)).where(
                        ProviderAuditEventRecord.attempt_id == event.attempt_id
                    )
                )
                or 0
            ) + 1
            session.add(
                ProviderAuditEventRecord(
                    event_id=uuid4(),
                    attempt_id=event.attempt_id,
                    owner_id=event.owner_id,
                    project_id=event.project_id,
                    group_id=event.group_id,
                    event_sequence=sequence,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    provider=event.provider,
                    node_id=event.node_id,
                    schema_version=event.schema_version,
                    workflow_id=event.workflow_id,
                    workflow_version=event.workflow_version,
                    model_key=event.model_key,
                    model_version=event.model_version,
                    local_idempotency_key=event.local_idempotency_key,
                    provider_task_key=event.provider_task_key,
                    request_sha256=event.request_sha256,
                    receipt_json=event.receipt,
                    receipt_sha256=event.receipt_sha256,
                    error_code=event.error_code,
                )
            )

    def load_authoritative_group(self, owner_id: UUID, group_id: UUID) -> AuthoritativeGroupInput:
        with self._sessions() as session, session.begin():
            return self._group_loader(session, owner_id, group_id)

    def find_attempt(self, owner_id: UUID, idempotency_key: str) -> PersistentAttempt | None:
        with self._sessions() as session:
            row = session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.owner_id == owner_id,
                    VideoAttemptRecord.idempotency_key == idempotency_key,
                )
            )
            return _attempt(row) if row else None

    def find_unresolved_group(self, owner_id: UUID, group_id: UUID) -> PersistentAttempt | None:
        with self._sessions() as session:
            row = session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.owner_id == owner_id,
                    VideoAttemptRecord.group_id == group_id,
                    VideoAttemptRecord.stage == AttemptStage.REMOTE_STATE_UNKNOWN.value,
                )
            )
            return _attempt(row) if row else None

    def assert_no_other_unresolved(self, attempt: PersistentAttempt) -> None:
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, attempt.owner_id, attempt.group_id)
            row = session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.owner_id == attempt.owner_id,
                    VideoAttemptRecord.group_id == attempt.group_id,
                    VideoAttemptRecord.stage == AttemptStage.REMOTE_STATE_UNKNOWN.value,
                    VideoAttemptRecord.attempt_id != attempt.attempt_id,
                )
            )
            if row is not None:
                raise VideoRuleViolation("remote_acceptance_unknown")

    def commit_submitting(self, attempt: PersistentAttempt) -> PersistentAttempt:
        """Linearize final H3 authority and SUBMITTING before the external POST."""
        with self._sessions() as probe:
            postgres = probe.bind is not None and probe.bind.dialect.name == "postgresql"
        if postgres:
            # 权威装配是多次只读查询，必须在 projects 行级写锁之外完成；
            # 锁内只保留廉价的指针/修订校验，避免长事务放大锁等待。
            with self._sessions() as session:
                source = self._group_loader(session, attempt.owner_id, attempt.group_id)
            persisted = (attempt.input_snapshot or {}).get("authority")
            if not persisted or source.authority_snapshot != persisted:
                raise VideoRuleViolation("immutable_video_input_changed")
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, attempt.owner_id, attempt.group_id)
            if not postgres:
                source = self._group_loader(session, attempt.owner_id, attempt.group_id)
                persisted = (attempt.input_snapshot or {}).get("authority")
                if not persisted or source.authority_snapshot != persisted:
                    raise VideoRuleViolation("immutable_video_input_changed")
                row = session.scalar(
                    select(VideoAttemptRecord)
                    .where(VideoAttemptRecord.attempt_id == attempt.attempt_id)
                    .with_for_update()
                )
                if row is None or row.revision != attempt.revision:
                    raise VideoRuleViolation("attempt_revision_conflict")
                validate_attempt_transition(_attempt(row), attempt)
                _copy(row, attempt)
                row.revision += 1
                row.updated_at = datetime.now(UTC)
                session.flush()
                return _attempt(row)
            project = session.scalar(
                select(ProjectRow)
                .where(
                    ProjectRow.id == attempt.project_id,
                    ProjectRow.owner_id == attempt.owner_id,
                    ProjectRow.production_state == "ACTIVE",
                )
                .with_for_update()
            )
            current = session.scalar(
                select(StoryboardCurrentRow)
                .where(
                    StoryboardCurrentRow.owner_id == attempt.owner_id,
                    StoryboardCurrentRow.group_id == attempt.group_id,
                    StoryboardCurrentRow.generation_id == attempt.generation_id,
                )
                .with_for_update()
            )
            row = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            )
            if project is None or current is None or row is None:
                raise VideoRuleViolation("immutable_video_input_changed")
            if row.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            unresolved = session.scalar(
                select(VideoAttemptRecord.attempt_id).where(
                    VideoAttemptRecord.owner_id == attempt.owner_id,
                    VideoAttemptRecord.group_id == attempt.group_id,
                    VideoAttemptRecord.stage == AttemptStage.REMOTE_STATE_UNKNOWN.value,
                    VideoAttemptRecord.attempt_id != attempt.attempt_id,
                )
            )
            if unresolved is not None:
                raise VideoRuleViolation("remote_acceptance_unknown")
            validate_attempt_transition(_attempt(row), attempt)
            _copy(row, attempt)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
            return _attempt(row)

    def get_attempt(self, attempt_id: UUID) -> PersistentAttempt | None:
        with self._sessions() as session:
            row = session.get(VideoAttemptRecord, attempt_id)
            return _attempt(row) if row else None

    def create_attempt(self, attempt: PersistentAttempt) -> PersistentAttempt:
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, attempt.owner_id, attempt.group_id)
            existing = session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.owner_id == attempt.owner_id,
                    VideoAttemptRecord.idempotency_key == attempt.idempotency_key,
                )
            )
            if existing is not None:
                value = _attempt(existing)
                if value.generation_sha256 != attempt.generation_sha256:
                    raise VideoRuleViolation("idempotency_conflict")
                return value
            active = session.scalar(
                select(VideoAttemptRecord.attempt_id).where(
                    VideoAttemptRecord.owner_id == attempt.owner_id,
                    VideoAttemptRecord.group_id == attempt.group_id,
                    VideoAttemptRecord.stage.in_(
                        {
                            AttemptStage.CREATED.value,
                            AttemptStage.NOT_SUBMITTED.value,
                            AttemptStage.STAGING.value,
                            AttemptStage.SUBMITTING.value,
                            AttemptStage.SUBMITTED.value,
                            AttemptStage.RUNNING.value,
                            AttemptStage.REMOTE_STATE_UNKNOWN.value,
                        }
                    ),
                )
            )
            if active is not None:
                raise VideoRuleViolation("active_group_video_attempt_exists")
            unresolved = session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.owner_id == attempt.owner_id,
                    VideoAttemptRecord.group_id == attempt.group_id,
                    VideoAttemptRecord.stage == AttemptStage.REMOTE_STATE_UNKNOWN.value,
                )
            )
            if unresolved is not None:
                raise VideoRuleViolation("remote_acceptance_unknown")
            if self._intent_group_loader is not None:
                source = self._intent_group_loader(
                    session, attempt.owner_id, attempt.group_id, for_intent=True
                )
                validate_reference_intent(source, attempt)
            row = _record(attempt)
            row.intent_sequence = 1 + int(
                session.scalar(
                    select(func.coalesce(func.max(VideoAttemptRecord.intent_sequence), 0)).where(
                        VideoAttemptRecord.owner_id == attempt.owner_id,
                        VideoAttemptRecord.group_id == attempt.group_id,
                    )
                )
                or 0
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
                    created_at = datetime.now(UTC)
                    session.add(
                        VideoOutboxRecord(
                            attempt_id=attempt.attempt_id,
                            owner_id=attempt.owner_id,
                            event_type="VIDEO_GENERATION_REQUESTED",
                            created_at=created_at,
                            next_attempt_at=created_at,
                        )
                    )
            except IntegrityError:
                existing = session.scalar(
                    select(VideoAttemptRecord).where(
                        VideoAttemptRecord.owner_id == attempt.owner_id,
                        VideoAttemptRecord.idempotency_key == attempt.idempotency_key,
                    )
                )
                if existing is None:
                    raise
                value = _attempt(existing)
                if value.generation_sha256 != attempt.generation_sha256:
                    raise VideoRuleViolation("idempotency_conflict") from None
                return value
            return _attempt(row)

    def count_group_attempts(
        self, owner_id: UUID, group_id: UUID, budget_round: int | None = None
    ) -> int:
        with self._sessions() as session:
            conditions = [
                VideoAttemptRecord.owner_id == owner_id,
                VideoAttemptRecord.group_id == group_id,
            ]
            if budget_round is not None:
                conditions.append(VideoAttemptRecord.budget_round == budget_round)
            return int(
                session.scalar(
                    select(func.count()).select_from(VideoAttemptRecord).where(*conditions)
                )
                or 0
            )

    def project_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        with self._sessions() as session:
            value = session.scalar(
                select(ProjectRow.auto_budget_round).where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                )
            )
            return int(value or 1)

    def supersede_attempt(
        self,
        superseded: PersistentAttempt,
        replacement: PersistentAttempt,
        event: ProviderAuditEvent,
        *,
        delay_seconds: float,
        max_submissions: int,
    ) -> tuple[PersistentAttempt, PersistentAttempt]:
        """Atomically persist the superseded update, its replacement and the outbox intent.

        The replacement idempotency key is deterministic per superseded attempt, so
        replay after a crash can never create a second submission for the same trigger.
        """
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, superseded.owner_id, superseded.group_id)
            existing = session.scalar(
                select(VideoAttemptRecord).where(
                    VideoAttemptRecord.owner_id == replacement.owner_id,
                    VideoAttemptRecord.idempotency_key == replacement.idempotency_key,
                )
            )
            if existing is not None:
                if existing.generation_sha256 != replacement.generation_sha256:
                    raise VideoRuleViolation("idempotency_conflict")
                row = session.get(VideoAttemptRecord, superseded.attempt_id)
                if row is None:
                    raise VideoRuleViolation("unknown attempt")
                return _attempt(row), _attempt(existing)
            row = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == superseded.attempt_id)
                .with_for_update()
            )
            if row is None:
                raise VideoRuleViolation("unknown attempt")
            if (event.attempt_id, event.owner_id, event.project_id, event.group_id) != (
                row.attempt_id,
                row.owner_id,
                row.project_id,
                row.group_id,
            ):
                raise VideoRuleViolation("provider_audit_identity_mismatch")
            if row.revision != superseded.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(VideoAttemptRecord)
                    .where(
                        VideoAttemptRecord.owner_id == superseded.owner_id,
                        VideoAttemptRecord.group_id == superseded.group_id,
                        VideoAttemptRecord.budget_round == superseded.budget_round,
                    )
                )
                or 0
            )
            if count >= max_submissions:
                raise VideoRuleViolation(SUBMISSION_BUDGET_EXHAUSTED)
            validate_attempt_transition(_attempt(row), superseded)
            _copy(row, superseded)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            sequence = (
                session.scalar(
                    select(func.max(ProviderAuditEventRecord.event_sequence)).where(
                        ProviderAuditEventRecord.attempt_id == row.attempt_id
                    )
                )
                or 0
            ) + 1
            session.add(
                ProviderAuditEventRecord(
                    event_id=uuid4(),
                    attempt_id=row.attempt_id,
                    owner_id=row.owner_id,
                    project_id=row.project_id,
                    group_id=row.group_id,
                    event_sequence=sequence,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    provider=event.provider,
                    node_id=event.node_id,
                    schema_version=event.schema_version,
                    workflow_id=event.workflow_id,
                    workflow_version=event.workflow_version,
                    model_key=event.model_key,
                    model_version=event.model_version,
                    local_idempotency_key=event.local_idempotency_key,
                    provider_task_key=event.provider_task_key,
                    request_sha256=event.request_sha256,
                    receipt_json=event.receipt,
                    receipt_sha256=event.receipt_sha256,
                    error_code=event.error_code,
                )
            )
            new_row = _record(replacement)
            new_row.intent_sequence = 1 + int(
                session.scalar(
                    select(func.coalesce(func.max(VideoAttemptRecord.intent_sequence), 0)).where(
                        VideoAttemptRecord.owner_id == replacement.owner_id,
                        VideoAttemptRecord.group_id == replacement.group_id,
                    )
                )
                or 0
            )
            session.add(new_row)
            session.flush()
            created_at = datetime.now(UTC)
            session.add(
                VideoOutboxRecord(
                    attempt_id=replacement.attempt_id,
                    owner_id=replacement.owner_id,
                    event_type="VIDEO_GENERATION_REQUESTED",
                    created_at=created_at,
                    next_attempt_at=created_at + timedelta(seconds=delay_seconds),
                )
            )
            session.flush()
            return _attempt(row), _attempt(new_row)

    def save_attempt(self, attempt: PersistentAttempt) -> PersistentAttempt:
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, attempt.owner_id, attempt.group_id)
            row = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            )
            if row is None:
                raise VideoRuleViolation("unknown attempt")
            if row.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            validate_attempt_transition(_attempt(row), attempt)
            _copy(row, attempt)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
            return _attempt(row)

    def save_attempt_with_event(
        self, attempt: PersistentAttempt, event: ProviderAuditEvent
    ) -> PersistentAttempt:
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, attempt.owner_id, attempt.group_id)
            row = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            )
            if row is None:
                raise VideoRuleViolation("unknown attempt")
            if (event.attempt_id, event.owner_id, event.project_id, event.group_id) != (
                row.attempt_id,
                row.owner_id,
                row.project_id,
                row.group_id,
            ):
                raise VideoRuleViolation("provider_audit_identity_mismatch")
            if row.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            validate_attempt_transition(_attempt(row), attempt)
            _copy(row, attempt)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            sequence = (
                session.scalar(
                    select(func.max(ProviderAuditEventRecord.event_sequence)).where(
                        ProviderAuditEventRecord.attempt_id == row.attempt_id
                    )
                )
                or 0
            ) + 1
            session.add(
                ProviderAuditEventRecord(
                    event_id=uuid4(),
                    attempt_id=row.attempt_id,
                    owner_id=row.owner_id,
                    project_id=row.project_id,
                    group_id=row.group_id,
                    event_sequence=sequence,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    provider=event.provider,
                    node_id=event.node_id,
                    schema_version=event.schema_version,
                    workflow_id=event.workflow_id,
                    workflow_version=event.workflow_version,
                    model_key=event.model_key,
                    model_version=event.model_version,
                    local_idempotency_key=event.local_idempotency_key,
                    provider_task_key=event.provider_task_key,
                    request_sha256=event.request_sha256,
                    receipt_json=event.receipt,
                    receipt_sha256=event.receipt_sha256,
                    error_code=event.error_code,
                )
            )
            session.flush()
            return _attempt(row)

    @staticmethod
    def _lock_group_scope(session: Session, owner_id: UUID, group_id: UUID) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                {"scope": f"{owner_id}:{group_id}"},
            )

    def is_current(self, group_id: UUID, attempt_id: UUID) -> bool:
        with self._sessions() as session:
            row = session.get(GroupCurrentVideoRecord, group_id)
            return row is not None and row.attempt_id == attempt_id

    def mark_current(self, attempt: PersistentAttempt) -> PersistentAttempt:
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, attempt.owner_id, attempt.group_id)
            row = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            )
            if row is None:
                raise VideoRuleViolation("unknown attempt")
            if row.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            validate_attempt_transition(_attempt(row), attempt)
            pointer = session.get(GroupCurrentVideoRecord, attempt.group_id)
            if pointer is None:
                pointer = GroupCurrentVideoRecord(
                    group_id=attempt.group_id,
                    attempt_id=attempt.attempt_id,
                    generation_id=attempt.generation_id,
                    intent_sequence=attempt.intent_sequence,
                    updated_at=datetime.now(UTC),
                )
                session.add(pointer)
            else:
                if attempt.intent_sequence > pointer.intent_sequence:
                    pointer.attempt_id = attempt.attempt_id
                    pointer.generation_id = attempt.generation_id
                    pointer.intent_sequence = attempt.intent_sequence
                    pointer.updated_at = datetime.now(UTC)
            _copy(row, attempt)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
            return _attempt(row)

    def mark_current_if_absent(self, attempt: PersistentAttempt) -> bool:
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, attempt.owner_id, attempt.group_id)
            row = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            )
            if row is None:
                raise VideoRuleViolation("unknown attempt")
            if row.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            validate_attempt_transition(_attempt(row), attempt)
            pointer = session.get(GroupCurrentVideoRecord, attempt.group_id)
            if pointer is not None:
                return False
            session.add(
                GroupCurrentVideoRecord(
                    group_id=attempt.group_id,
                    attempt_id=attempt.attempt_id,
                    generation_id=attempt.generation_id,
                    intent_sequence=attempt.intent_sequence,
                    updated_at=datetime.now(UTC),
                )
            )
            _copy(row, attempt)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
            return True

    def select_current(self, owner_id: UUID, group_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        with self._sessions() as session, session.begin():
            self._lock_group_scope(session, owner_id, group_id)
            row = session.scalar(
                select(VideoAttemptRecord)
                .where(
                    VideoAttemptRecord.attempt_id == attempt_id,
                    VideoAttemptRecord.owner_id == owner_id,
                    VideoAttemptRecord.group_id == group_id,
                    VideoAttemptRecord.stage == AttemptStage.ARCHIVED.value,
                    VideoAttemptRecord.archive_stage == ArchiveStage.COMPLETE.value,
                    VideoAttemptRecord.archived_asset_json.is_not(None),
                    VideoAttemptRecord.probe_evidence_json["qc_passed"].as_boolean().is_(True),
                )
                .with_for_update()
            )
            if row is None:
                raise VideoRuleViolation("video candidate not found")
            pointer = session.get(GroupCurrentVideoRecord, group_id)
            if pointer is None:
                session.add(
                    GroupCurrentVideoRecord(
                        group_id=group_id,
                        attempt_id=attempt_id,
                        generation_id=row.generation_id,
                        intent_sequence=row.intent_sequence,
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                pointer.attempt_id = attempt_id
                pointer.generation_id = row.generation_id
                pointer.intent_sequence = row.intent_sequence
                pointer.updated_at = datetime.now(UTC)
            return _attempt(row)

    def get_current(self, owner_id: UUID, group_id: UUID) -> PersistentAttempt | None:
        with self._sessions() as session:
            row = session.scalar(
                select(VideoAttemptRecord)
                .join(
                    GroupCurrentVideoRecord,
                    GroupCurrentVideoRecord.attempt_id == VideoAttemptRecord.attempt_id,
                )
                .where(
                    GroupCurrentVideoRecord.group_id == group_id,
                    VideoAttemptRecord.owner_id == owner_id,
                )
            )
            return _attempt(row) if row else None

    def list_history(self, owner_id: UUID, group_id: UUID) -> tuple[PersistentAttempt, ...]:
        with self._sessions() as session:
            return tuple(
                _attempt(row)
                for row in session.scalars(
                    select(VideoAttemptRecord)
                    .where(
                        VideoAttemptRecord.owner_id == owner_id,
                        VideoAttemptRecord.group_id == group_id,
                    )
                    .order_by(VideoAttemptRecord.updated_at.desc())
                )
            )

    def sweep_expired_slots(self, *, grace: timedelta = timedelta(minutes=5)) -> int:
        """回收过期槽位租约。

        S6：旧实现只回收 ``FAILED/CANCELLED/ARCHIVED``，任何卡在 STAGING 的租约
        即使过期也永不归还，6 个槽位被占死时系统无法自愈。现在扩展为：

        * 终态：租约一过期立即回收（保持原语义）；
        * 未提交远端的非终态（STAGING / NOT_SUBMITTED / SUBMITTING）：
          租约过期超过 ``grace`` 才回收，且必须通过 token fencing。

        ``SUBMITTED`` / ``RUNNING`` 刻意不回收——远端可能仍在生成，回收后重新提交
        会产生重复视频。这两类由 stalled 检测与人工介入处理。
        """
        now = datetime.now(UTC)
        cutoff = now - grace
        terminal_stages = {
            AttemptStage.FAILED.value,
            AttemptStage.CANCELLED.value,
            AttemptStage.ARCHIVED.value,
        }
        unsubmitted_stages = {
            AttemptStage.STAGING.value,
            AttemptStage.NOT_SUBMITTED.value,
            AttemptStage.SUBMITTING.value,
        }
        reclaimed = 0
        with self._sessions() as session, session.begin():
            rows = tuple(
                session.execute(
                    select(
                        VideoAttemptRecord.attempt_id,
                        VideoAttemptRecord.slot_lease_token,
                    )
                    .where(
                        VideoAttemptRecord.slot_claimed.is_(True),
                        VideoAttemptRecord.slot_lease_expires_at < now,
                        or_(
                            VideoAttemptRecord.remote_acceptance.is_(None),
                            VideoAttemptRecord.remote_acceptance != "UNKNOWN",
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for attempt_id, observed_token in rows:
                attempt = session.scalar(
                    select(VideoAttemptRecord)
                    .where(VideoAttemptRecord.attempt_id == attempt_id)
                    .with_for_update()
                )
                if attempt is None or not attempt.slot_claimed:
                    continue
                # fencing：读取后 token 若被续租改写，说明该 attempt 仍活着。
                if attempt.slot_lease_token != observed_token:
                    continue
                expires_at = attempt.slot_lease_expires_at
                if expires_at is None:
                    continue
                expires_at = _as_utc(expires_at)
                if expires_at >= now:
                    continue
                if attempt.stage not in terminal_stages:
                    if attempt.stage not in unsubmitted_stages:
                        continue
                    if expires_at >= cutoff:
                        continue
                node = session.scalar(
                    select(H3NodeRecord)
                    .where(H3NodeRecord.node_id == attempt.node_id)
                    .with_for_update()
                )
                if node is None or node.claimed_slots <= 0:
                    continue
                node.claimed_slots -= 1
                attempt.slot_claimed = False
                attempt.slot_lease_token = None
                attempt.slot_lease_owner = None
                attempt.slot_lease_expires_at = None
                attempt.revision += 1
                reclaimed += 1
        return reclaimed

    def claim_compatible_node(
        self, attempt_id: UUID, workflow: ImmutableWorkflowSnapshot
    ) -> EngineNode:
        with self._sessions() as session, session.begin():
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                    {"scope": H3_NODE_CONFIG_LOCK_SCOPE},
                )
            attempt = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt_id)
                .with_for_update()
            )
            if attempt is None:
                raise VideoRuleViolation("unknown attempt")
            if attempt.slot_claimed:
                return self._node_by_id(session, attempt.node_id, workflow, lock=False)
            if attempt.node_id is not None:
                node = self._node_by_id(session, attempt.node_id, workflow, lock=True)
                row = session.get(H3NodeRecord, attempt.node_id)
                assert row is not None
                stalled = (
                    session.scalar(
                        select(func.count())
                        .select_from(VideoAttemptRecord)
                        .where(
                            VideoAttemptRecord.node_id == attempt.node_id,
                            VideoAttemptRecord.slot_claimed.is_(True),
                            VideoAttemptRecord.stalled_at.is_not(None),
                        )
                    )
                    or 0
                )
                if row.claimed_slots - stalled >= row.capacity:
                    raise VideoRuleViolation("assigned H3 node has no capacity")
            else:
                stalled_sq = _stalled_slots_subquery()
                completed_sq = (
                    select(
                        VideoAttemptRecord.node_id.label("completed_node_id"),
                        func.max(VideoAttemptRecord.updated_at).label("last_completed_at"),
                    )
                    .where(
                        VideoAttemptRecord.stage.in_(
                            (
                                AttemptStage.ARCHIVED.value,
                                AttemptStage.FAILED.value,
                                AttemptStage.CANCELLED.value,
                            )
                        )
                    )
                    .group_by(VideoAttemptRecord.node_id)
                    .subquery()
                )
                effective_free = (
                    H3NodeRecord.capacity
                    - H3NodeRecord.claimed_slots
                    + func.coalesce(stalled_sq.c.stalled_slots, 0)
                )
                row = session.scalar(
                    select(H3NodeRecord)
                    .outerjoin(stalled_sq, stalled_sq.c.stalled_node_id == H3NodeRecord.node_id)
                    .outerjoin(
                        completed_sq, completed_sq.c.completed_node_id == H3NodeRecord.node_id
                    )
                    .where(
                        H3NodeRecord.enabled.is_(True),
                        H3NodeRecord.health == NodeHealth.READY.value,
                        effective_free > 0,
                        H3NodeRecord.capability == "video.reference_images_to_video",
                        H3NodeRecord.model_key == workflow.model_key,
                        H3NodeRecord.model_version == workflow.model_version,
                        H3NodeRecord.workflow_id == workflow.workflow_id,
                        H3NodeRecord.workflow_version == workflow.workflow_version,
                        H3NodeRecord.manifest_sha256 == workflow.workflow_manifest_sha256,
                    )
                    .order_by(
                        effective_free.desc(),
                        nulls_first(completed_sq.c.last_completed_at.asc()),
                        H3NodeRecord.node_id,
                    )
                    .with_for_update(of=H3NodeRecord, skip_locked=True)
                )
                if row is None:
                    _log_capacity_starvation(session)
                    raise VideoCapacityUnavailable()
                node = _node(row, self._token_resolver(row.token_secret_ref))
                attempt.node_id = row.node_id
            row.claimed_slots += 1
            attempt.slot_claimed = True
            attempt.slot_lease_token = attempt.slot_lease_token or uuid4().hex
            attempt.slot_lease_owner = "group-video-worker"
            attempt.slot_lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
            attempt.revision += 1
            attempt.updated_at = datetime.now(UTC)
            return node

    def get_assigned_node(
        self, attempt_id: UUID, workflow: ImmutableWorkflowSnapshot
    ) -> EngineNode:
        with self._sessions() as session:
            attempt = session.get(VideoAttemptRecord, attempt_id)
            if attempt is None:
                raise VideoRuleViolation("unknown attempt")
            return self._node_by_id(session, attempt.node_id, workflow, lock=False)

    def release_node(self, attempt_id: UUID, lease_token: str) -> None:
        with self._sessions() as session, session.begin():
            attempt = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt_id)
                .with_for_update()
            )
            if attempt is None:
                raise VideoRuleViolation("unknown attempt")
            if not attempt.slot_claimed or attempt.node_id is None:
                return
            if attempt.slot_lease_token != lease_token:
                raise VideoRuleViolation("slot_lease_conflict")
            node = session.scalar(
                select(H3NodeRecord)
                .where(H3NodeRecord.node_id == attempt.node_id)
                .with_for_update()
            )
            if node is None or node.claimed_slots <= 0:
                raise VideoRuleViolation("H3 node slot accounting is inconsistent")
            node.claimed_slots -= 1
            attempt.slot_claimed = False
            attempt.slot_lease_token = None
            attempt.slot_lease_owner = None
            attempt.slot_lease_expires_at = None
            attempt.revision += 1
            attempt.updated_at = datetime.now(UTC)

    def renew_slot(self, attempt_id: UUID, lease_token: str) -> None:
        with self._sessions() as session, session.begin():
            attempt = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt_id)
                .with_for_update()
            )
            if attempt is None:
                raise VideoRuleViolation("unknown attempt")
            if not attempt.slot_claimed:
                return
            if attempt.slot_lease_token != lease_token:
                raise VideoRuleViolation("slot_lease_conflict")
            attempt.slot_lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
            attempt.revision += 1
            attempt.updated_at = datetime.now(UTC)

    def rotate_slot_lease(
        self, attempt_id: UUID, lease_token: str, lease_owner: str
    ) -> PersistentAttempt:
        with self._sessions() as session, session.begin():
            attempt = session.scalar(
                select(VideoAttemptRecord)
                .where(VideoAttemptRecord.attempt_id == attempt_id)
                .with_for_update()
            )
            if (
                attempt is None
                or not attempt.slot_claimed
                or attempt.slot_lease_token != lease_token
            ):
                raise VideoRuleViolation("slot_lease_conflict")
            attempt.slot_lease_token = uuid4().hex
            attempt.slot_lease_owner = lease_owner
            attempt.slot_lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
            attempt.revision += 1
            attempt.updated_at = datetime.now(UTC)
            session.flush()
            return _attempt(attempt)

    def _node_by_id(
        self,
        session: Session,
        node_id: str | None,
        workflow: ImmutableWorkflowSnapshot,
        *,
        lock: bool,
    ) -> EngineNode:
        if node_id is None:
            raise VideoRuleViolation("attempt has no assigned node")
        query = select(H3NodeRecord).where(H3NodeRecord.node_id == node_id)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None or not _matches(row, workflow):
            raise VideoRuleViolation("assigned H3 node capability or version no longer matches")
        return _node(row, self._token_resolver(row.token_secret_ref))


def _matches(row: H3NodeRecord, workflow: ImmutableWorkflowSnapshot) -> bool:
    return (
        row.enabled
        and row.capability == "video.reference_images_to_video"
        and row.model_key == workflow.model_key
        and row.model_version == workflow.model_version
        and row.workflow_id == workflow.workflow_id
        and row.workflow_version == workflow.workflow_version
        and row.manifest_sha256 == workflow.workflow_manifest_sha256
    )


def _node(row: H3NodeRecord, token: str) -> EngineNode:
    return EngineNode(
        row.node_id,
        row.base_url,
        token,
        row.capacity,
        NodeHealth(row.health),
        row.claimed_slots,
        True,
        row.model_key,
        row.model_version,
        row.workflow_id,
        row.workflow_version,
        row.manifest_sha256,
    )


def _asset_json(asset: MediaAssetRef | None) -> dict[str, object] | None:
    if asset is None:
        return None
    return {
        "asset_id": str(asset.asset_id),
        "sha256": asset.sha256,
        "media_type": asset.media_type,
        "size_bytes": asset.size_bytes,
    }


def _asset(value: dict[str, object] | None) -> MediaAssetRef | None:
    if value is None:
        return None
    return MediaAssetRef(
        UUID(str(value["asset_id"])),
        str(value["sha256"]),
        str(value["media_type"]),
        int(str(value["size_bytes"])),
    )


def _as_utc(value: datetime) -> datetime:
    """SQLite 回读的 datetime 是 naive；统一按 UTC 解释后再比较。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _record(value: PersistentAttempt) -> VideoAttemptRecord:
    row = VideoAttemptRecord(attempt_id=value.attempt_id, updated_at=datetime.now(UTC))
    _copy(row, value)
    return row


def _copy(row: VideoAttemptRecord, value: PersistentAttempt) -> None:
    row.owner_id = value.owner_id
    row.project_id = value.project_id
    row.group_id = value.group_id
    row.generation_id = value.generation_id
    row.idempotency_key = value.idempotency_key
    row.superseded_attempt_id = value.superseded_attempt_id
    row.generation_sha256 = value.generation_sha256
    row.request_sha256 = value.request_sha256
    row.request_json = value.request
    row.node_id = value.node_id
    row.remote_id = value.remote_id
    row.stage = value.stage.value
    row.staged_ids = list(value.staged_ids)
    row.cleaned_staged_ids = list(value.cleaned_staged_ids)
    row.archive_stage = value.archive_stage.value
    row.output_id = value.output_id
    row.output_sha256 = value.output_sha256
    row.archived_asset_json = _asset_json(value.archived_asset)
    row.error_code = value.error_code
    row.retry_evidence = list(value.retry_evidence)
    row.slot_claimed = value.slot_claimed
    row.slot_lease_token = value.slot_lease_token
    row.slot_lease_owner = value.slot_lease_owner
    row.slot_lease_expires_at = (
        datetime.fromisoformat(value.slot_lease_expires_at) if value.slot_lease_expires_at else None
    )
    row.narration_validation_json = value.narration_validation
    row.intent_sequence = value.intent_sequence
    row.input_snapshot_json = value.input_snapshot
    row.probe_evidence_json = value.probe_evidence
    row.output_size_bytes = value.output_size_bytes
    row.output_media_type = value.output_media_type
    row.remote_phase = value.remote_phase
    row.remote_acceptance = value.remote_acceptance
    row.cancel_dispatched = value.cancel_dispatched
    row.cancel_idempotency_key = value.cancel_idempotency_key
    row.cancel_inspect_count = value.cancel_inspect_count
    row.archive_confirm_dispatched = value.archive_confirm_dispatched
    row.archive_confirm_idempotency_key = value.archive_confirm_idempotency_key
    row.archive_confirm_inspect_count = value.archive_confirm_inspect_count
    row.raw_receipt_json = value.raw_receipt
    row.remote_phase_changed_at = (
        datetime.fromisoformat(value.remote_phase_changed_at)
        if value.remote_phase_changed_at
        else None
    )
    row.stalled_at = datetime.fromisoformat(value.stalled_at) if value.stalled_at else None
    row.requested_action = value.requested_action.value if value.requested_action else None
    row.cleanup_terminal_stage = (
        value.cleanup_terminal_stage.value if value.cleanup_terminal_stage else None
    )
    row.budget_round = value.budget_round


def _attempt(row: VideoAttemptRecord) -> PersistentAttempt:
    return PersistentAttempt(
        row.attempt_id,
        row.owner_id,
        row.project_id,
        row.group_id,
        row.generation_id,
        row.idempotency_key,
        row.generation_sha256,
        row.request_sha256,
        row.request_json,
        AttemptStage(row.stage),
        row.node_id,
        row.remote_id,
        tuple(row.staged_ids),
        tuple(row.cleaned_staged_ids),
        ArchiveStage(row.archive_stage),
        row.output_id,
        row.output_sha256,
        _asset(row.archived_asset_json),
        row.error_code,
        tuple(row.retry_evidence),
        row.slot_claimed,
        row.input_snapshot_json,
        row.probe_evidence_json,
        row.output_size_bytes,
        row.output_media_type,
        row.revision,
        row.remote_phase,
        row.raw_receipt_json,
        RequestedAction(row.requested_action) if row.requested_action else None,
        AttemptStage(row.cleanup_terminal_stage) if row.cleanup_terminal_stage else None,
        False,
        row.remote_acceptance,
        row.slot_lease_token,
        row.slot_lease_owner,
        row.slot_lease_expires_at.isoformat() if row.slot_lease_expires_at else None,
        row.narration_validation_json,
        row.intent_sequence,
        row.cancel_dispatched,
        row.cancel_idempotency_key,
        row.archive_confirm_dispatched,
        row.archive_confirm_idempotency_key,
        row.archive_confirm_inspect_count,
        row.cancel_inspect_count,
        row.remote_phase_changed_at.isoformat() if row.remote_phase_changed_at else None,
        row.stalled_at.isoformat() if row.stalled_at else None,
        row.superseded_attempt_id,
        budget_round=int(row.budget_round or 1),
    )


def _stalled_slots_subquery() -> Subquery:
    """Stalled attempts keep their slot lease but are excluded from scheduling capacity."""
    return (
        select(
            VideoAttemptRecord.node_id.label("stalled_node_id"),
            func.count().label("stalled_slots"),
        )
        .where(
            VideoAttemptRecord.slot_claimed.is_(True),
            VideoAttemptRecord.stalled_at.is_not(None),
        )
        .group_by(VideoAttemptRecord.node_id)
        .subquery()
    )


def _log_capacity_starvation(session: Session) -> None:
    """Rate-limited WARN with a per-node capacity snapshot when no node can accept work."""
    global _last_capacity_warning_at
    now = time.monotonic()
    if now - _last_capacity_warning_at < _CAPACITY_WARNING_MIN_INTERVAL_SECONDS:
        return
    _last_capacity_warning_at = now
    stalled_sq = _stalled_slots_subquery()
    snapshot = session.execute(
        select(
            H3NodeRecord.node_id,
            H3NodeRecord.health,
            H3NodeRecord.claimed_slots,
            H3NodeRecord.capacity,
            func.coalesce(stalled_sq.c.stalled_slots, 0),
        )
        .outerjoin(stalled_sq, stalled_sq.c.stalled_node_id == H3NodeRecord.node_id)
        .where(H3NodeRecord.enabled.is_(True))
        .order_by(H3NodeRecord.node_id)
    ).all()
    logger.warning(
        "no compatible H3 node has capacity; enabled node snapshot "
        "(node_id, health, claimed, capacity, stalled): %s",
        [tuple(row) for row in snapshot],
    )
