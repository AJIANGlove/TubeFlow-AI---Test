from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    NarrationAttemptRecord,
    NarrationProjectRecord,
)
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorGenerationAttemptRow,
    StoryboardGenerationRow,
    StoryboardOutboxRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    ProjectDirectorCurrentRow,
    ProjectDirectorMaterializationRow,
    ProjectDirectorRunRow,
)
from tubeflow_test.application.composition import (
    CompositionAttempt,
    CompositionClaim,
    CompositionVersion,
    composition_from_payload,
)
from tubeflow_test.application.composition_outbox import (
    CompositionDispatchReceipt,
    CompositionEnvelope,
)
from tubeflow_test.application.cover_ports import COVER_SCOPE_PROJECT
from tubeflow_test.application.deletion import PurgeClaim
from tubeflow_test.application.media_authority import AuthoritativeCompositionInputPort
from tubeflow_test.application.purge_outbox import PurgeEnvelope
from tubeflow_test.domain.composition import CompositionRequest
from tubeflow_test.domain.deletion import (
    DeletionObject,
    DeletionRecord,
    DeletionStatus,
    ObjectDeletionResult,
    Tombstone,
    aggregate_deletion_status,
)
from tubeflow_test.domain.director_generation import DirectorAttemptStatus
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import NarrationAttemptState
from tubeflow_test.domain.project_director import ProjectDirectorRunStatus
from tubeflow_test.domain.storyboards import GenerationStatus
from tubeflow_test.video_generation.orchestration import ArchiveStage, AttemptStage
from tubeflow_test.video_generation.persistence import VideoAttemptRecord

from .cover.models import CoverImageGenerationRow, CoverRefImageRow
from .identity_project.models import (
    AutoPipelineRunRow,
    CharacterImageRow,
    ProjectRow,
    VoiceVersionRow,
)
from .identity_project.write_gate import SqlProjectWriteGate as SharedProjectWriteGate
from .models import (
    CompositionInputRevisionRow,
    CompositionJobRow,
    CompositionOutboxRow,
    CompositionVersionRow,
    DeletionTombstoneRow,
    MediaAssetRow,
    ProjectAssetReferenceRow,
    ProjectPurgeJobRow,
    ProjectPurgeObjectRow,
    PurgeOutboxRow,
    StagedObjectRow,
)

SessionFactory = Callable[[], Session]


def _composition_workflow_id(
    owner_id: UUID, project_id: UUID, digest: str, budget_round: int, generation: int
) -> str:
    # §15.9：AUTO 第 2 轮起 workflow_id 携带 :rN 命名空间，跨轮重提不再撞唯一约束。
    scope = f":r{budget_round}" if budget_round > 1 else ""
    suffix = f":g{generation}" if generation > 1 else ""
    return f"composition:{owner_id}:{project_id}:{digest}{scope}{suffix}"


def _split_composition_key_digest(idempotency_key: str, prefix: str) -> tuple[str, int] | None:
    """合成幂等键解析为 (inputs_digest, 预算轮次)；AUTO 键带 :rN 后缀，历史键为第 1 轮。"""
    if not idempotency_key.startswith(prefix):
        return None
    raw = idempotency_key.removeprefix(prefix)
    head, sep, tail = raw.rpartition(":r")
    if sep and len(head) == 64 and tail.isdigit() and int(tail) >= 1:
        return head, int(tail)
    if len(raw) == 64:
        return raw, 1
    return None


def composition_key_scope(sessions: SessionFactory) -> Callable[[UUID, UUID], str]:
    """ManualCompositionService 的幂等键预算轮次后缀：AUTO 第 2 轮起为 :rN，否则为空。"""

    def scope(project_id: UUID, owner_id: UUID) -> str:
        with sessions() as session:
            row = session.execute(
                select(ProjectRow.production_mode, ProjectRow.auto_budget_round).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            ).one_or_none()
            if row is None or str(row[0]) != "AUTO":
                return ""
            budget_round = int(row[1] or 1)
            return f":r{budget_round}" if budget_round > 1 else ""

    return scope


def reconcile_staged_objects(
    sessions: SessionFactory,
    storage: PrivateObjectStorage,
    *,
    older_than_seconds: int = 3600,
) -> int:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=older_than_seconds)
    claims: list[tuple[UUID, UUID, int]] = []
    with sessions() as session, session.begin():
        rows = tuple(
            session.scalars(
                select(StagedObjectRow)
                .where(
                    StagedObjectRow.state.in_(
                        ("PENDING_UPLOAD", "STAGED", "CLEANUP_PENDING")
                    ),
                    (
                        (
                            (StagedObjectRow.state != "CLEANUP_PENDING")
                            & (StagedObjectRow.created_at < cutoff)
                            & (
                                StagedObjectRow.lease_until.is_(None)
                                | (StagedObjectRow.lease_until < now)
                            )
                        )
                        | (
                            (StagedObjectRow.state == "CLEANUP_PENDING")
                            & (
                                StagedObjectRow.cleanup_after.is_(None)
                                | (StagedObjectRow.cleanup_after <= now)
                            )
                        )
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            if _staged_object_is_referenced(session, row.object_key):
                if row.purpose == "PROJECT_DELETION":
                    _defer_project_deletion_tombstone(row, now)
                else:
                    row.state = "FINALIZED"
                    row.execution_token = None
                    row.lease_until = None
                    row.cleanup_after = None
                continue
            if row.state == "CLEANUP_PENDING" and row.execution_token is not None:
                claims.append((row.id, row.execution_token, row.generation))
            else:
                token = uuid4()
                row.state = "CLEANUP_PENDING"
                row.generation += 1
                row.execution_token = token
                row.lease_until = None
                row.cleanup_after = now
                row.cleanup_attempt = 0
                claims.append((row.id, token, row.generation))
    removed = 0
    for stage_id, token, generation in claims:
        with sessions() as session:
            object_key = session.scalar(
                select(StagedObjectRow.object_key).where(
                    StagedObjectRow.id == stage_id,
                    StagedObjectRow.state == "CLEANUP_PENDING",
                    StagedObjectRow.execution_token == token,
                    StagedObjectRow.generation == generation,
                )
            )
        if object_key is None:
            continue
        with sessions() as session:
            claimed_row = session.get(StagedObjectRow, stage_id)
            if claimed_row is None or _staged_object_is_referenced(session, object_key):
                continue
            expected_sha256, expected_size = claimed_row.sha256, claimed_row.size_bytes
            deletion_tombstone = claimed_row.purpose == "PROJECT_DELETION"
        deletion_error: Exception | None = None
        try:
            storage.delete_exact(object_key)
        except Exception as error:  # a lost delete response is reconciled below
            deletion_error = error
        try:
            still_exists = storage.exists(object_key)
        except Exception:
            continue
        if still_exists:
            try:
                if not deletion_tombstone:
                    if expected_sha256 is None or expected_size is None:
                        continue
                    storage.get_verified(object_key, expected_sha256, expected_size)
                storage.delete_exact(object_key)
                still_exists = storage.exists(object_key)
            except Exception:
                continue
        if still_exists:
            continue
        with sessions() as session, session.begin():
            final_row = session.scalar(
                select(StagedObjectRow)
                .where(
                    StagedObjectRow.id == stage_id,
                    StagedObjectRow.state == "CLEANUP_PENDING",
                    StagedObjectRow.execution_token == token,
                    StagedObjectRow.generation == generation,
                )
                .with_for_update()
            )
            if final_row is None:
                continue
            if _staged_object_is_referenced(session, final_row.object_key):
                if final_row.purpose == "PROJECT_DELETION":
                    _defer_project_deletion_tombstone(final_row, now)
                else:
                    final_row.state = "FINALIZED"
                    final_row.execution_token = None
                    final_row.cleanup_after = None
                continue
            first_confirmation = final_row.cleanup_checked_at is None
            final_row.cleanup_checked_at = now
            # Tombstones are intentionally durable. A writer can be paused for an
            # unbounded time after its last DB CAS and still issue a late PUT.
            final_row.cleanup_attempt += 1
            retry_seconds = min(3600, 2 ** min(final_row.cleanup_attempt, 12))
            final_row.cleanup_after = now + timedelta(seconds=retry_seconds)
            if first_confirmation or deletion_error is not None:
                removed += 1
    return removed


def _defer_project_deletion_tombstone(row: StagedObjectRow, now: datetime) -> None:
    """Keep exact-key deletion evidence durable while a shared reference survives."""
    row.state = "CLEANUP_PENDING"
    row.generation += 1
    row.execution_token = uuid4()
    row.lease_until = None
    row.cleanup_checked_at = now
    row.cleanup_attempt += 1
    retry_seconds = min(3600, 2 ** min(row.cleanup_attempt, 12))
    row.cleanup_after = now + timedelta(seconds=retry_seconds)


def _staged_object_is_referenced(session: Session, object_key: str) -> bool:
    if session.scalar(select(MediaAssetRow.id).where(MediaAssetRow.object_key == object_key)):
        return True
    if session.scalar(
        select(CharacterImageRow.id).where(CharacterImageRow.object_key == object_key)
    ):
        return True
    return (
        session.scalar(
            select(VoiceVersionRow.id).where(
                (VoiceVersionRow.reference_zip_object_key == object_key)
                | (VoiceVersionRow.preview_object_key == object_key)
                | (VoiceVersionRow.avatar_object_key == object_key)
            )
        )
        is not None
    )


class ActiveJobGate(Protocol):
    name: str

    def has_active(self, session: Session, project_id: UUID) -> bool: ...


class SqlMappedActiveJobGate:
    """Binds a latest feature job table without naming or importing legacy rows."""

    def __init__(
        self,
        name: str,
        model: type[Any],
        project_column: Any,
        state_column: Any,
        active_states: tuple[str, ...],
    ) -> None:
        self.name = name
        self._model = model
        self._project_column = project_column
        self._state_column = state_column
        self._active_states = active_states

    def has_active(self, session: Session, project_id: UUID) -> bool:
        return (
            session.scalar(
                select(self._project_column)
                .select_from(self._model)
                .where(
                    self._project_column == project_id,
                    self._state_column.in_(self._active_states),
                )
                .limit(1)
            )
            is not None
        )


class SqlNarrationActiveJobGate:
    name = "narration"

    def has_active(self, session: Session, project_id: UUID) -> bool:
        state = NarrationProjectRecord.payload["job"]["state"].as_string()
        job_active = session.scalar(
            select(NarrationProjectRecord.project_id)
            .where(
                NarrationProjectRecord.project_id == project_id,
                state.in_(("PENDING", "DISPATCHED")),
            )
            .limit(1)
        ) is not None
        if job_active:
            return True
        active_attempt_states = tuple(
            state.value
            for state in (
                NarrationAttemptState.INTENT_RECORDED,
                NarrationAttemptState.SUBMISSION_STARTED,
                NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            )
        )
        attempt_state = NarrationAttemptRecord.payload["attempt"]["state"].as_string()
        return session.scalar(
            select(NarrationAttemptRecord.id)
            .where(
                NarrationAttemptRecord.project_id == project_id,
                attempt_state.in_(active_attempt_states),
            )
            .limit(1)
        ) is not None


class SqlStoryboardActiveJobGate:
    name = "storyboard"

    def has_active(self, session: Session, project_id: UUID) -> bool:
        generation_states = tuple(
            status.value
            for status in GenerationStatus
            if status not in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}
        )
        generation = session.scalar(
            select(StoryboardGenerationRow.id)
            .where(
                StoryboardGenerationRow.project_id == project_id,
                StoryboardGenerationRow.status.in_(generation_states),
            )
            .limit(1)
        )
        if generation is not None:
            return True
        director_states = tuple(
            status.value
            for status in DirectorAttemptStatus
            if status not in {DirectorAttemptStatus.SUCCEEDED, DirectorAttemptStatus.FAILED}
        )
        director = session.scalar(
            select(DirectorGenerationAttemptRow.id)
            .join(
                AuthoritativeNarrationGroupRecord,
                AuthoritativeNarrationGroupRecord.id == DirectorGenerationAttemptRow.group_id,
            )
            .where(
                AuthoritativeNarrationGroupRecord.project_id == project_id,
                DirectorGenerationAttemptRow.status.in_(director_states),
            )
            .limit(1)
        )
        if director is not None:
            return True
        project_director = session.scalar(
            select(ProjectDirectorRunRow.id)
            .where(
                ProjectDirectorRunRow.project_id == project_id,
                ProjectDirectorRunRow.status.in_(
                    (
                        ProjectDirectorRunStatus.INTENT_PERSISTED.value,
                        ProjectDirectorRunStatus.RUNNING.value,
                        ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN.value,
                    )
                ),
            )
            .limit(1)
        )
        if project_director is not None:
            return True
        current_revision_id = session.scalar(
            select(ProjectDirectorCurrentRow.revision_id)
            .where(
                ProjectDirectorCurrentRow.project_id == project_id,
                ProjectDirectorCurrentRow.revision_id.is_not(None),
            )
            .limit(1)
        )
        if current_revision_id is not None:
            narration_ready = session.scalar(
                select(AuthoritativeNarrationGroupRecord.id)
                .where(AuthoritativeNarrationGroupRecord.project_id == project_id)
                .limit(1)
            )
            materialization = session.scalar(
                select(ProjectDirectorMaterializationRow.id)
                .where(
                    ProjectDirectorMaterializationRow.project_id == project_id,
                    ProjectDirectorMaterializationRow.preplan_revision_id
                    == current_revision_id,
                )
                .limit(1)
            )
            if narration_ready is not None and materialization is None:
                return True
        return session.scalar(
            select(StoryboardOutboxRow.id)
            .where(
                StoryboardOutboxRow.project_id == project_id,
                StoryboardOutboxRow.acknowledged_at.is_(None),
            )
            .limit(1)
        ) is not None


class SqlCompositionActiveJobGate:
    name = "composition"

    def has_active(self, session: Session, project_id: UUID) -> bool:
        running = session.scalar(
            select(CompositionJobRow.project_id)
            .where(
                CompositionJobRow.project_id == project_id,
                CompositionJobRow.state == "RUNNING",
            )
            .limit(1)
        ) is not None
        if running:
            return True
        outboxes = session.scalars(
            select(CompositionOutboxRow).where(
                CompositionOutboxRow.project_id == project_id,
                CompositionOutboxRow.state.in_(("PENDING", "CLAIMED", "DISPATCHED")),
            )
        )
        for outbox in outboxes:
            if outbox.state != "DISPATCHED":
                return True
            terminal_job = session.scalar(
                select(CompositionJobRow.id).where(
                    CompositionJobRow.project_id == project_id,
                    CompositionJobRow.idempotency_key.endswith(outbox.inputs_sha256),
                    CompositionJobRow.state.in_(("SUCCEEDED", "FAILED")),
                )
            )
            if terminal_job is None:
                return True
        return False


class SqlH3ActiveJobGate:
    name = "h3"

    def has_active(self, session: Session, project_id: UUID) -> bool:
        attempts = session.scalars(
            select(VideoAttemptRecord).where(VideoAttemptRecord.project_id == project_id)
        )
        for row in attempts:
            archived = (
                row.stage == AttemptStage.ARCHIVED.value
                and row.archive_stage == ArchiveStage.COMPLETE.value
            )
            terminal_failure = (
                row.stage in {AttemptStage.FAILED.value, AttemptStage.CANCELLED.value}
                and row.archive_stage == ArchiveStage.NOT_STARTED.value
                and not row.staged_ids
                and row.cleanup_terminal_stage is None
                and row.requested_action is None
                and not row.slot_claimed
            )
            if not archived and not terminal_failure:
                return True
        return False


def production_active_job_gates() -> tuple[ActiveJobGate, ...]:
    return (
        SqlNarrationActiveJobGate(),
        SqlStoryboardActiveJobGate(),
        SqlH3ActiveJobGate(),
        SqlCompositionActiveJobGate(),
    )


def has_cross_domain_active_jobs(
    session: Session, project_id: UUID, gates: tuple[ActiveJobGate, ...]
) -> bool:
    return any(gate.has_active(session, project_id) for gate in gates)


class SqlProjectWriteGate:
    """Shared fail-closed gate for narration, storyboard, H3, and composition writes."""

    def assert_writable(self, session: Session, project_id: UUID, owner_id: UUID) -> None:
        project = session.scalar(
            select(ProjectRow.id)
            .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            .with_for_update()
        )
        if project is None:
            raise DomainError("project_not_found", "project was not found")
        if session.get(ProjectPurgeJobRow, project_id) is not None:
            raise DomainError("project_purging", "project is being deleted")


class SqlAlchemyCompositionRepository:
    def __init__(
        self,
        sessions: SessionFactory,
        storage: PrivateObjectStorage,
        authority: AuthoritativeCompositionInputPort,
        *,
        lease_seconds: int = 900,
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._authority = authority
        self._lease_seconds = lease_seconds

    def load_authoritative(self, project_id: UUID, owner_id: UUID) -> CompositionRequest | None:
        with self._sessions() as session:
            project = session.scalar(
                select(ProjectRow).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            if project is None:
                raise DomainError("project_not_found", "project was not found")
            if session.get(ProjectPurgeJobRow, project_id) is not None:
                raise DomainError("project_purging", "project is being deleted")
            if project.snapshot_confirmed_at is None:
                raise DomainError("project_not_ready", "project snapshot is not confirmed")
        authoritative = self._authority.load(project_id, owner_id)
        if authoritative is None:
            raise DomainError(
                "composition_inputs_not_ready", "authoritative media inputs are not ready"
            )
        return authoritative

    def capture_revision(self, project_id: UUID, owner_id: UUID) -> tuple[UUID, str]:
        request = self.load_authoritative(project_id, owner_id)
        if request is None:
            raise DomainError("composition_inputs_not_ready", "composition inputs are not ready")
        payload = json.loads(json.dumps(asdict(request)))
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        revision_id = uuid4()
        with self._sessions() as session, session.begin():
            SqlProjectWriteGate().assert_writable(session, project_id, owner_id)
            session.add(
                CompositionInputRevisionRow(
                    id=revision_id,
                    project_id=project_id,
                    owner_id=owner_id,
                    inputs_sha256=digest,
                    payload_json=payload,
                    created_at=datetime.now(UTC),
                )
            )
        return revision_id, digest

    def enqueue_composition(self, project_id: UUID, owner_id: UUID) -> CompositionDispatchReceipt:
        request = self.load_authoritative(project_id, owner_id)
        if request is None:
            raise DomainError("composition_inputs_not_ready", "composition inputs are not ready")
        payload = json.loads(json.dumps(asdict(request)))
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            SqlProjectWriteGate().assert_writable(session, project_id, owner_id)
            # §15.9：AUTO 项目的合成幂等命名空间按预算轮次隔离；MANUAL 行为不变。
            production_mode = session.scalar(
                select(ProjectRow.production_mode).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            budget_round = 1
            if production_mode == "AUTO":
                budget_round = int(
                    session.scalar(
                        select(ProjectRow.auto_budget_round).where(
                            ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                        )
                    )
                    or 1
                )
            if production_mode != "AUTO":
                budget_round = 1
            scope = f":r{budget_round}" if production_mode == "AUTO" and budget_round > 1 else ""
            workflow_id = _composition_workflow_id(owner_id, project_id, digest, budget_round, 1)
            idempotency_key = f"compose:{owner_id}:{project_id}:{digest}{scope}"
            existing = session.scalar(
                select(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.inputs_sha256 == digest,
                    CompositionOutboxRow.project_id == project_id,
                    CompositionOutboxRow.owner_id == owner_id,
                    CompositionOutboxRow.budget_round == budget_round,
                )
                .order_by(CompositionOutboxRow.workflow_generation.desc())
                .limit(1)
            )
            if existing is not None:
                job = session.scalar(
                    select(CompositionJobRow).where(
                        CompositionJobRow.idempotency_key == idempotency_key,
                        CompositionJobRow.project_id == project_id,
                        CompositionJobRow.owner_id == owner_id,
                    )
                )
                state = (
                    job.state
                    if job is not None
                    and job.workflow_generation == existing.workflow_generation
                    else existing.state
                )
                return CompositionDispatchReceipt(
                    existing.revision_id,
                    existing.workflow_id,
                    state,
                    existing.workflow_generation,
                )
            revision_id = uuid4()
            session.add(
                CompositionInputRevisionRow(
                    id=revision_id,
                    project_id=project_id,
                    owner_id=owner_id,
                    inputs_sha256=digest,
                    payload_json=payload,
                    created_at=now,
                )
            )
            session.add(
                CompositionOutboxRow(
                    id=uuid4(),
                    project_id=project_id,
                    owner_id=owner_id,
                    revision_id=revision_id,
                    inputs_sha256=digest,
                    workflow_id=workflow_id,
                    state="PENDING",
                    attempt=0,
                    workflow_generation=1,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=now,
                    dispatched_at=None,
                    last_error_code=None,
                    budget_round=budget_round,
                )
            )
            return CompositionDispatchReceipt(revision_id, workflow_id, "PENDING", 1)

    def retry_composition(
        self, project_id: UUID, owner_id: UUID
    ) -> CompositionDispatchReceipt:
        with self._sessions() as session, session.begin():
            SqlProjectWriteGate().assert_writable(session, project_id, owner_id)
            job = session.scalar(
                select(CompositionJobRow)
                .where(
                    CompositionJobRow.project_id == project_id,
                    CompositionJobRow.owner_id == owner_id,
                )
                .order_by(CompositionJobRow.intent_sequence.desc(), CompositionJobRow.id.desc())
                .with_for_update()
                .limit(1)
            )
            if job is None:
                raise DomainError(
                    "composition_retry_not_available",
                    "No failed composition attempt is available for retry",
                )
            prefix = f"compose:{owner_id}:{project_id}:"
            parsed_key = _split_composition_key_digest(job.idempotency_key, prefix)
            if parsed_key is None:
                raise DomainError(
                    "composition_state_corrupt",
                    "Composition attempt input binding is invalid",
                )
            digest, key_round = parsed_key
            outbox = session.scalar(
                select(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.project_id == project_id,
                    CompositionOutboxRow.owner_id == owner_id,
                    CompositionOutboxRow.inputs_sha256 == digest,
                    CompositionOutboxRow.budget_round == key_round,
                )
                .order_by(CompositionOutboxRow.workflow_generation.desc())
                .with_for_update()
                .limit(1)
            )
            if outbox is None:
                raise DomainError(
                    "composition_state_corrupt",
                    "Composition workflow dispatch record is unavailable",
                )

            if outbox.workflow_generation > job.workflow_generation:
                if outbox.state in {"PENDING", "DISPATCHED"}:
                    return CompositionDispatchReceipt(
                        outbox.revision_id,
                        outbox.workflow_id,
                        outbox.state,
                        outbox.workflow_generation,
                    )
                # SUPERSEDED（完成时收口）由下方统一重新武装；其余状态按不确定处理。
                if outbox.state != "SUPERSEDED":
                    raise DomainError(
                        "composition_state_uncertain",
                        "Composition retry status is uncertain; refresh before retrying",
                    )
            elif outbox.workflow_generation != job.workflow_generation:
                raise DomainError(
                    "composition_state_uncertain",
                    "Composition retry status is uncertain; refresh before retrying",
                )
            if job.state == "RUNNING":
                return CompositionDispatchReceipt(
                    outbox.revision_id,
                    outbox.workflow_id,
                    "RUNNING",
                    outbox.workflow_generation,
                )
            if job.state != "FAILED":
                raise DomainError(
                    "composition_retry_not_available",
                    "Only the latest failed composition attempt can be retried",
                )
            if outbox.state not in {"DISPATCHED", "SUPERSEDED"}:
                raise DomainError(
                    "composition_state_uncertain",
                    "Composition retry status is uncertain; refresh before retrying",
                )

            generation = outbox.workflow_generation + 1
            outbox.workflow_generation = generation
            outbox.workflow_id = _composition_workflow_id(
                owner_id, project_id, digest, outbox.budget_round, generation
            )
            outbox.state = "PENDING"
            outbox.attempt = 0
            outbox.lease_token = None
            outbox.lease_until = None
            outbox.next_attempt_at = datetime.now(UTC)
            outbox.dispatched_at = None
            outbox.last_error_code = None
            return CompositionDispatchReceipt(
                outbox.revision_id,
                outbox.workflow_id,
                outbox.state,
                outbox.workflow_generation,
            )

    def load_revision(
        self, revision_id: UUID, project_id: UUID, owner_id: UUID
    ) -> CompositionRequest:
        with self._sessions() as session:
            row = session.scalar(
                select(CompositionInputRevisionRow).where(
                    CompositionInputRevisionRow.id == revision_id,
                    CompositionInputRevisionRow.project_id == project_id,
                    CompositionInputRevisionRow.owner_id == owner_id,
                )
            )
            if row is None:
                raise DomainError(
                    "composition_revision_not_found", "composition revision was not found"
                )
            return composition_from_payload(row.payload_json)

    def begin_composition(
        self,
        project_id: UUID,
        owner_id: UUID,
        idempotency_key: str,
        workflow_generation: int,
    ) -> CompositionClaim:
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            owned = session.scalar(
                select(ProjectRow.id)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .with_for_update()
            )
            if owned is None:
                raise DomainError("project_not_found", "project was not found")
            if session.get(ProjectPurgeJobRow, project_id) is not None:
                raise DomainError("project_purging", "project is being deleted")
            job = session.scalar(
                select(CompositionJobRow).where(
                    CompositionJobRow.idempotency_key == idempotency_key
                )
            )
            if job is None:
                token = uuid4()
                intent_sequence = int(
                    session.scalar(
                        select(func.coalesce(func.max(CompositionJobRow.intent_sequence), 0)).where(
                            CompositionJobRow.project_id == project_id,
                            CompositionJobRow.owner_id == owner_id,
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    CompositionJobRow(
                        id=uuid4(),
                        project_id=project_id,
                        owner_id=owner_id,
                        idempotency_key=idempotency_key,
                        state="RUNNING",
                        intent_sequence=intent_sequence,
                        attempt=1,
                        workflow_generation=workflow_generation,
                        lease_until=now + timedelta(seconds=self._lease_seconds),
                        execution_token=token,
                        composition_version_id=None,
                    )
                )
                return CompositionClaim(token)
            if job.state == "SUCCEEDED" and job.composition_version_id:
                row = session.get(CompositionVersionRow, job.composition_version_id)
                if row is None:
                    raise DomainError(
                        "composition_state_corrupt", "composition result is unavailable"
                    )
                return CompositionClaim(
                    None,
                    CompositionVersion(
                        row.id, str(row.video_asset_id), str(row.srt_asset_id), row.manifest_json
                    ),
                )
            if workflow_generation < job.workflow_generation:
                return CompositionClaim(None)
            if job.state == "RUNNING" and workflow_generation > job.workflow_generation:
                return CompositionClaim(None)
            if (
                job.state == "RUNNING"
                and job.lease_until
                and job.lease_until >= now
            ):
                return CompositionClaim(None)
            if job.state == "FAILED":
                if workflow_generation != job.workflow_generation + 1:
                    raise DomainError(
                        "composition_retry_required",
                        "A failed composition requires an explicit new retry generation",
                    )
                job.workflow_generation = workflow_generation
            if job.state in {"FAILED", "RUNNING"}:
                token = uuid4()
                job.state = "RUNNING"
                job.attempt += 1
                job.lease_until = now + timedelta(seconds=self._lease_seconds)
                job.execution_token = token
                job.error_code = None
                return CompositionClaim(token)
            raise DomainError("composition_job_invalid", "composition job state is invalid")

    def fail_composition(
        self, idempotency_key: str, execution_token: UUID, error_code: str
    ) -> None:
        with self._sessions() as session, session.begin():
            job = session.scalar(
                select(CompositionJobRow).where(
                    CompositionJobRow.idempotency_key == idempotency_key
                )
            )
            if (
                job is not None
                and job.state == "RUNNING"
                and job.execution_token == execution_token
            ):
                job.state = "FAILED"
                job.lease_until = None
                job.error_code = error_code

    def schedule_auto_retry(
        self,
        project_id: UUID,
        owner_id: UUID,
        idempotency_key: str,
        execution_token: UUID,
        *,
        max_attempts: int,
        backoff_seconds: tuple[float, ...],
    ) -> bool:
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            production_mode = session.scalar(
                select(ProjectRow.production_mode).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            if production_mode != "AUTO":
                return False
            if session.get(ProjectPurgeJobRow, project_id) is not None:
                return False
            job = session.scalar(
                select(CompositionJobRow)
                .where(
                    CompositionJobRow.idempotency_key == idempotency_key,
                    CompositionJobRow.project_id == project_id,
                    CompositionJobRow.owner_id == owner_id,
                )
                .with_for_update()
            )
            if (
                job is None
                or job.state != "FAILED"
                or job.execution_token != execution_token
                or job.attempt >= max_attempts
            ):
                return False
            prefix = f"compose:{owner_id}:{project_id}:"
            parsed_key = _split_composition_key_digest(job.idempotency_key, prefix)
            if parsed_key is None:
                return False
            digest, key_round = parsed_key
            outbox = session.scalar(
                select(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.project_id == project_id,
                    CompositionOutboxRow.owner_id == owner_id,
                    CompositionOutboxRow.inputs_sha256 == digest,
                    CompositionOutboxRow.budget_round == key_round,
                )
                .order_by(CompositionOutboxRow.workflow_generation.desc())
                .with_for_update()
                .limit(1)
            )
            if (
                outbox is None
                or outbox.workflow_generation != job.workflow_generation
                or outbox.state != "DISPATCHED"
            ):
                return False
            generation = outbox.workflow_generation + 1
            outbox.workflow_generation = generation
            outbox.workflow_id = _composition_workflow_id(
                owner_id, project_id, digest, key_round, generation
            )
            outbox.state = "PENDING"
            outbox.attempt = 0
            outbox.lease_token = None
            outbox.lease_until = None
            delay = backoff_seconds[min(job.attempt - 1, len(backoff_seconds) - 1)]
            outbox.next_attempt_at = now + timedelta(seconds=delay)
            outbox.dispatched_at = None
            outbox.last_error_code = None
            return True

    def activate_candidate(
        self,
        project_id: UUID,
        owner_id: UUID,
        video: Path,
        srt: bytes,
        manifest: dict[str, object],
        idempotency_key: str,
        execution_token: UUID,
    ) -> CompositionVersion:
        video_key, srt_key = str(uuid4()), str(uuid4())
        video_token, srt_token = uuid4(), uuid4()
        with self._sessions() as session, session.begin():
            SqlProjectWriteGate().assert_writable(session, project_id, owner_id)
            session.add_all(
                [
                    StagedObjectRow(
                        object_key=video_key,
                        logical_object_key=video_key,
                        project_id=project_id,
                        owner_id=owner_id,
                        purpose="COMPOSITION",
                        ownership_scope="PROJECT",
                        created_at=datetime.now(UTC),
                        generation=1,
                        execution_token=video_token,
                        lease_until=datetime.now(UTC) + timedelta(minutes=5),
                    ),
                    StagedObjectRow(
                        object_key=srt_key,
                        logical_object_key=srt_key,
                        project_id=project_id,
                        owner_id=owner_id,
                        purpose="SRT",
                        ownership_scope="PROJECT",
                        created_at=datetime.now(UTC),
                        generation=1,
                        execution_token=srt_token,
                        lease_until=datetime.now(UTC) + timedelta(minutes=5),
                    ),
                ]
            )
        video_object = self._storage.put(video.read_bytes(), "video/mp4", object_key=video_key)
        try:
            srt_object = self._storage.put(srt, "application/x-subrip", object_key=srt_key)
        except BaseException:
            raise
        uploads_staged = True
        with self._sessions() as session, session.begin():
            for object_key, token in ((video_key, video_token), (srt_key, srt_token)):
                result = cast(
                    CursorResult[Any],
                    session.execute(
                        update(StagedObjectRow)
                        .where(
                            StagedObjectRow.object_key == object_key,
                            StagedObjectRow.state == "PENDING_UPLOAD",
                            StagedObjectRow.generation == 1,
                            StagedObjectRow.execution_token == token,
                        )
                        .values(state="STAGED", lease_until=None)
                    ),
                )
                if not result.rowcount:
                    uploads_staged = False
                    break
        if not uploads_staged:
            raise DomainError("composition_staging_lost", "composition upload claim was lost")
        candidate_id, video_id, srt_id = uuid4(), uuid4(), uuid4()
        try:
            with self._sessions() as session, session.begin():
                owned = session.scalar(
                    select(ProjectRow.id)
                    .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                    .with_for_update()
                )
                if owned is None:
                    raise DomainError("project_not_found", "project was not found")
                if session.get(ProjectPurgeJobRow, project_id) is not None:
                    raise DomainError("project_purging", "project is being deleted")
                job = session.scalar(
                    select(CompositionJobRow)
                    .where(
                        CompositionJobRow.idempotency_key == idempotency_key,
                        CompositionJobRow.project_id == project_id,
                        CompositionJobRow.owner_id == owner_id,
                    )
                    .with_for_update()
                )
                if job is None or job.state != "RUNNING" or job.execution_token != execution_token:
                    raise DomainError(
                        "composition_claim_lost", "composition claim is no longer active"
                    )
                for object_key, token in ((video_key, video_token), (srt_key, srt_token)):
                    attached = cast(
                        CursorResult[Any],
                        session.execute(
                            update(StagedObjectRow)
                            .where(
                                StagedObjectRow.object_key == object_key,
                                StagedObjectRow.state == "STAGED",
                                StagedObjectRow.generation == 1,
                                StagedObjectRow.execution_token == token,
                            )
                            .values(state="ATTACHED")
                        ),
                    )
                    if not attached.rowcount:
                        raise DomainError(
                            "composition_staging_lost", "composition upload claim was lost"
                        )
                session.add_all(
                    [
                        MediaAssetRow(
                            id=video_id,
                            owner_id=owner_id,
                            project_id=project_id,
                            kind="COMPOSITION",
                            object_key=video_object.key,
                            sha256=video_object.sha256,
                            media_type=video_object.media_type,
                            size_bytes=video_object.size_bytes,
                            metadata_json={},
                        ),
                        MediaAssetRow(
                            id=srt_id,
                            owner_id=owner_id,
                            project_id=project_id,
                            kind="SRT",
                            object_key=srt_object.key,
                            sha256=srt_object.sha256,
                            media_type=srt_object.media_type,
                            size_bytes=srt_object.size_bytes,
                            metadata_json={},
                        ),
                    ]
                )
                session.flush()
                session.add(
                    CompositionVersionRow(
                        id=candidate_id,
                        project_id=project_id,
                        owner_id=owner_id,
                        video_asset_id=video_id,
                        srt_asset_id=srt_id,
                        manifest_json=manifest,
                        current=False,
                    )
                )
                job.state = "SUCCEEDED"
                job.composition_version_id = candidate_id
                job.lease_until = None
                job.error_code = None
                session.execute(
                    update(StagedObjectRow)
                    .where(
                        StagedObjectRow.object_key.in_((video_key, srt_key)),
                        StagedObjectRow.state == "ATTACHED",
                        StagedObjectRow.generation == 1,
                        StagedObjectRow.execution_token.in_((video_token, srt_token)),
                    )
                    .values(state="FINALIZED", execution_token=None, lease_until=None)
                )
        except BaseException:
            with self._sessions() as session:
                committed = session.scalar(
                    select(CompositionVersionRow).where(
                        CompositionVersionRow.id == candidate_id,
                        CompositionVersionRow.video_asset_id == video_id,
                        CompositionVersionRow.srt_asset_id == srt_id,
                    )
                )
                if committed is not None:
                    return CompositionVersion(
                        candidate_id, str(video_id), str(srt_id), committed.manifest_json
                    )
            raise
        return CompositionVersion(candidate_id, str(video_id), str(srt_id), manifest)

    def project_production_mode(self, project_id: UUID, owner_id: UUID) -> str | None:
        with self._sessions() as session:
            return session.scalar(
                select(ProjectRow.production_mode).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )

    def current(self, project_id: UUID, owner_id: UUID) -> CompositionVersion | None:
        with self._sessions() as session:
            project = session.scalar(
                select(ProjectRow.id).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            if project is None:
                return None
            if session.get(ProjectPurgeJobRow, project_id) is not None:
                raise DomainError("project_purging", "project is being deleted")
            row = session.scalar(
                select(CompositionVersionRow).where(
                    CompositionVersionRow.project_id == project_id,
                    CompositionVersionRow.owner_id == owner_id,
                    CompositionVersionRow.current.is_(True),
                )
            )
            return (
                CompositionVersion(
                    row.id, str(row.video_asset_id), str(row.srt_asset_id), row.manifest_json, True
                )
                if row
                else None
            )

    def history(self, project_id: UUID, owner_id: UUID) -> tuple[CompositionVersion, ...]:
        with self._sessions() as session:
            owned = session.scalar(
                select(ProjectRow.id).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            if owned is None:
                raise DomainError("project_not_found", "project was not found")
            return tuple(
                CompositionVersion(
                    row.id,
                    str(row.video_asset_id),
                    str(row.srt_asset_id),
                    row.manifest_json,
                    row.current,
                )
                for row in session.scalars(
                    select(CompositionVersionRow)
                    .where(
                        CompositionVersionRow.project_id == project_id,
                        CompositionVersionRow.owner_id == owner_id,
                    )
                    .order_by(
                        CompositionVersionRow.intent_sequence.desc(), CompositionVersionRow.id
                    )
                )
            )

    def attempts(self, project_id: UUID, owner_id: UUID) -> tuple[CompositionAttempt, ...]:
        with self._sessions() as session:
            if session.scalar(
                select(ProjectRow.id).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            ) is None:
                raise DomainError("project_not_found", "project was not found")
            return tuple(
                CompositionAttempt(
                    row.id,
                    row.state,
                    row.attempt,
                    row.error_code,
                    row.composition_version_id,
                    row.workflow_generation,
                )
                for row in session.scalars(
                    select(CompositionJobRow)
                    .where(
                        CompositionJobRow.project_id == project_id,
                        CompositionJobRow.owner_id == owner_id,
                    )
                    .order_by(CompositionJobRow.intent_sequence.desc(), CompositionJobRow.id)
                )
            )

    def select_current(
        self, project_id: UUID, owner_id: UUID, version_id: UUID
    ) -> CompositionVersion:
        with self._sessions() as session, session.begin():
            SqlProjectWriteGate().assert_writable(session, project_id, owner_id)
            candidate = session.scalar(
                select(CompositionVersionRow)
                .where(
                    CompositionVersionRow.id == version_id,
                    CompositionVersionRow.project_id == project_id,
                    CompositionVersionRow.owner_id == owner_id,
                )
                .with_for_update()
            )
            if candidate is None:
                raise DomainError(
                    "invalid_composition_candidate", "composition candidate unavailable"
                )
            assets = tuple(
                session.scalars(
                    select(MediaAssetRow).where(
                        MediaAssetRow.id.in_((candidate.video_asset_id, candidate.srt_asset_id)),
                        MediaAssetRow.owner_id == owner_id,
                        MediaAssetRow.project_id == project_id,
                        MediaAssetRow.deletion_state == "ACTIVE",
                        MediaAssetRow.purging.is_(False),
                    )
                )
            )
            if len(assets) != 2:
                raise DomainError(
                    "invalid_composition_candidate", "composition candidate unavailable"
                )
            session.execute(
                update(CompositionVersionRow)
                .where(
                    CompositionVersionRow.project_id == project_id,
                    CompositionVersionRow.owner_id == owner_id,
                    CompositionVersionRow.current.is_(True),
                )
                .values(current=False)
            )
            candidate.current = True
            return CompositionVersion(
                candidate.id,
                str(candidate.video_asset_id),
                str(candidate.srt_asset_id),
                candidate.manifest_json,
                True,
            )


class SqlAlchemyDeletionRepository:
    def __init__(
        self, sessions: SessionFactory, active_job_gates: tuple[ActiveJobGate, ...]
    ) -> None:
        required = {"narration", "storyboard", "h3", "composition"}
        if {gate.name for gate in active_job_gates} != required:
            raise ValueError("all production active-job gates must be configured")
        self._sessions = sessions
        self._active_job_gates = active_job_gates

    def project_title(self, project_id: UUID, owner_id: UUID) -> str | None:
        with self._sessions() as session:
            return session.scalar(
                select(ProjectRow.title).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )

    def get_for_owner(
        self, project_id: UUID, owner_id: UUID, claim: PurgeClaim | None = None
    ) -> DeletionRecord | None:
        with self._sessions() as session:
            project = session.scalar(
                select(ProjectRow.id).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            job = session.get(ProjectPurgeJobRow, project_id)
            if claim is not None and not self._claim_matches(job, claim):
                raise DomainError("stale_purge_workflow", "purge cleanup claim is stale")
            if job is not None and job.owner_id != owner_id:
                return None
            if project is None and job is None:
                return None
            assets = tuple(
                session.scalars(
                    select(ProjectPurgeObjectRow).where(
                        ProjectPurgeObjectRow.project_id == project_id
                    )
                )
            )
            results = tuple(_result(item) for item in (job.object_results if job else []))
            status = DeletionStatus(job.state) if job else DeletionStatus.ACTIVE
            return DeletionRecord(
                project_id,
                owner_id,
                status,
                tuple(
                    DeletionObject(
                        a.object_key,
                        "PROJECT",
                        project_id,
                        a.external_reference_count > 0,
                    )
                    for a in assets
                ),
                results,
                job.dispatch_error_code if job else None,
            )

    def tombstone(self, project_id: UUID) -> Tombstone | None:
        with self._sessions() as session:
            row = session.get(DeletionTombstoneRow, project_id)
            if row is None:
                return None
            return Tombstone(row.project_id, row.deleted_at, row.object_count)

    def begin_with_lock_and_outbox(self, project_id: UUID, owner_id: UUID) -> bool:
        with self._sessions() as session, session.begin():
            write_gate = SharedProjectWriteGate(session)
            project = write_gate.lock_writable(owner_id, project_id)
            if session.get(ProjectPurgeJobRow, project_id) is not None:
                raise DomainError("project_purging", "project is being deleted")
            if has_cross_domain_active_jobs(session, project_id, self._active_job_gates):
                return False
            write_gate.freeze_for_purge(owner_id, project_id, locked_row=project)
            session.add(
                ProjectPurgeJobRow(
                    project_id=project_id,
                    owner_id=owner_id,
                    state="DELETING",
                    dispatch_error_code=None,
                    object_results=[],
                    workflow_generation=1,
                    execution_token=None,
                    lease_until=None,
                )
            )
            referenced_asset_ids = select(ProjectAssetReferenceRow.asset_id).where(
                ProjectAssetReferenceRow.project_id == project_id,
                ProjectAssetReferenceRow.owner_id == owner_id,
            )
            assets = tuple(
                session.scalars(
                    select(MediaAssetRow)
                    .where(
                        MediaAssetRow.owner_id == owner_id,
                        (MediaAssetRow.project_id == project_id)
                        | (MediaAssetRow.id.in_(referenced_asset_ids)),
                    )
                    .order_by(MediaAssetRow.id)
                    .with_for_update()
                )
            )
            for asset in assets:
                external_references = session.scalar(
                    select(func.count())
                    .select_from(ProjectAssetReferenceRow)
                    .where(
                        ProjectAssetReferenceRow.asset_id == asset.id,
                        ProjectAssetReferenceRow.project_id != project_id,
                    )
                )
                session.add(
                    ProjectPurgeObjectRow(
                        project_id=project_id,
                        object_key=asset.object_key,
                        asset_id=asset.id,
                        sha256=asset.sha256,
                        owner_id=owner_id,
                        external_reference_count=int(external_references or 0),
                        object_kind="MEDIA",
                    )
                )
            staged = tuple(
                session.scalars(
                    select(StagedObjectRow)
                    .where(
                        StagedObjectRow.project_id == project_id,
                        StagedObjectRow.owner_id == owner_id,
                        StagedObjectRow.ownership_scope == "PROJECT",
                    )
                    .with_for_update()
                )
            )
            media_keys = {asset.object_key for asset in assets}
            for journal in staged:
                if journal.object_key in media_keys:
                    continue
                journal.state = "CLEANUP_PENDING"
                journal.generation += 1
                journal.execution_token = uuid4()
                session.add(
                    ProjectPurgeObjectRow(
                        project_id=project_id,
                        object_key=journal.object_key,
                        asset_id=None,
                        staged_object_id=journal.id,
                        object_kind="STAGED",
                        sha256=journal.sha256,
                        owner_id=owner_id,
                        external_reference_count=0,
                        deletion_token=journal.execution_token,
                        deletion_generation=journal.generation,
                    )
                )
            # 封面项目作用域裸对象键（非 media_assets/staged_objects 登记的资产）：
            # 精确枚举进清理清单，行清理由 projects 删除时的 FK CASCADE/SET NULL 完成。
            known_keys = media_keys | {journal.object_key for journal in staged}
            cover_keys = tuple(
                session.scalars(
                    select(CoverRefImageRow.object_key).where(
                        CoverRefImageRow.owner_id == owner_id,
                        CoverRefImageRow.project_id == project_id,
                        CoverRefImageRow.scope == COVER_SCOPE_PROJECT,
                    )
                )
            ) + tuple(
                session.scalars(
                    select(CoverImageGenerationRow.object_key).where(
                        CoverImageGenerationRow.owner_id == owner_id,
                        CoverImageGenerationRow.project_id == project_id,
                        CoverImageGenerationRow.object_key.is_not(None),
                    )
                )
            )
            for object_key in dict.fromkeys(cover_keys):
                if object_key in known_keys:
                    continue
                session.add(
                    ProjectPurgeObjectRow(
                        project_id=project_id,
                        object_key=object_key,
                        asset_id=None,
                        object_kind="COVER",
                        sha256=None,
                        owner_id=owner_id,
                        external_reference_count=0,
                    )
                )
            session.execute(
                update(MediaAssetRow)
                .where(MediaAssetRow.project_id == project_id)
                .values(purging=True)
            )
            session.add(
                PurgeOutboxRow(
                    id=uuid4(),
                    project_id=project_id,
                    owner_id=owner_id,
                    dispatched_at=None,
                    lease_until=None,
                    attempt=0,
                    last_error_code=None,
                    workflow_generation=1,
                )
            )
            return True

    def reenqueue(self, project_id: UUID, owner_id: UUID) -> None:
        with self._sessions() as session, session.begin():
            job = session.scalar(
                select(ProjectPurgeJobRow)
                .where(
                    ProjectPurgeJobRow.project_id == project_id,
                    ProjectPurgeJobRow.owner_id == owner_id,
                )
                .with_for_update()
            )
            if job is None or job.state not in {"DELETING", "DELETE_FAILED"}:
                raise DomainError("project_not_deleting", "project is not ready for retry")
            outbox = session.scalar(
                select(PurgeOutboxRow)
                .where(PurgeOutboxRow.project_id == project_id)
                .with_for_update()
            )
            if outbox is None:
                outbox = PurgeOutboxRow(
                    id=uuid4(),
                    project_id=project_id,
                    owner_id=owner_id,
                    dispatched_at=None,
                    lease_until=None,
                    attempt=0,
                    last_error_code=None,
                    workflow_generation=1,
                )
                session.add(outbox)
            else:
                outbox.workflow_generation += 1
                outbox.dispatched_at = None
                outbox.lease_until = None
                outbox.last_error_code = None
            job.workflow_generation = outbox.workflow_generation
            job.execution_token = None
            job.lease_until = None
            job.dispatch_error_code = None
            job.state = "DELETING"

    def claim_clean(
        self, project_id: UUID, owner_id: UUID, generation: int | None
    ) -> PurgeClaim | None:
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            statement = select(ProjectPurgeJobRow).where(
                ProjectPurgeJobRow.project_id == project_id,
                ProjectPurgeJobRow.owner_id == owner_id,
            )
            if generation is not None:
                statement = statement.where(ProjectPurgeJobRow.workflow_generation == generation)
            job = session.scalar(statement.with_for_update())
            if job is None:
                return None
            if job.execution_token is not None and job.lease_until and job.lease_until >= now:
                return None
            token = uuid4()
            job.execution_token = token
            job.lease_until = now + timedelta(minutes=30)
            return PurgeClaim(project_id, owner_id, job.workflow_generation, token)

    def renew_clean_claim(self, claim: PurgeClaim) -> bool:
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            job = session.scalar(
                select(ProjectPurgeJobRow)
                .where(ProjectPurgeJobRow.project_id == claim.project_id)
                .with_for_update()
            )
            if not self._claim_matches(job, claim, now=now):
                return False
            assert job is not None
            job.lease_until = now + timedelta(minutes=30)
            return True

    def record_dispatch_failure(self, project_id: UUID, code: str) -> None:
        with self._sessions() as session, session.begin():
            session.get_one(ProjectPurgeJobRow, project_id).dispatch_error_code = code

    def save_results(
        self, claim: PurgeClaim, results: tuple[ObjectDeletionResult, ...]
    ) -> None:
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(ProjectPurgeJobRow)
                .where(ProjectPurgeJobRow.project_id == claim.project_id)
                .with_for_update()
            )
            if not self._claim_matches(row, claim):
                raise DomainError("stale_purge_workflow", "purge cleanup claim is stale")
            assert row is not None
            row.object_results = [_result_dict(item) for item in results]
            row.state = aggregate_deletion_status(results).value

    def replace_with_tombstone(
        self, claim: PurgeClaim, tombstone: Tombstone
    ) -> tuple[str, ...]:
        with self._sessions() as session, session.begin():
            job = session.scalar(
                select(ProjectPurgeJobRow)
                .where(ProjectPurgeJobRow.project_id == tombstone.project_id)
                .with_for_update()
            )
            if job is None:
                raise DomainError("project_not_deleting", "project is not ready for cleanup")
            if not self._claim_matches(job, claim):
                raise DomainError("stale_purge_workflow", "purge cleanup claim is stale")
            manifests = tuple(
                session.scalars(
                    select(ProjectPurgeObjectRow)
                    .where(ProjectPurgeObjectRow.project_id == tombstone.project_id)
                    .with_for_update()
                )
            )
            latest = {
                result.object_key: result
                for result in (_result(item) for item in job.object_results)
            }
            deletable = tuple(
                item
                for item in manifests
                if (result := latest.get(item.object_key)) is not None
                and result.succeeded
                and not result.skipped_shared
            )
            if len(latest) != len(manifests) or len(deletable) != tombstone.object_count:
                raise DomainError(
                    "project_delete_incomplete", "purge evidence does not match frozen manifest"
                )
            manifest_asset_ids = tuple(
                item.asset_id for item in manifests if item.asset_id is not None
            )
            deletable_asset_ids = tuple(
                item.asset_id
                for item in deletable
                if item.object_kind == "MEDIA" and item.asset_id is not None
            )
            deletable_journals = tuple(
                item
                for item in deletable
                if item.object_kind == "STAGED" and item.staged_object_id is not None
            )
            skipped_shared = tuple(
                item
                for item in manifests
                if item.object_kind == "MEDIA"
                and item.asset_id is not None
                and (result := latest.get(item.object_key)) is not None
                and result.succeeded
                and result.skipped_shared
            )
            session.execute(
                delete(ProjectAssetReferenceRow).where(
                    ProjectAssetReferenceRow.project_id == tombstone.project_id,
                    ProjectAssetReferenceRow.owner_id == job.owner_id,
                    ProjectAssetReferenceRow.asset_id.in_(manifest_asset_ids),
                )
            )
            if deletable_asset_ids:
                session.execute(
                    update(MediaAssetRow)
                    .where(
                        MediaAssetRow.id.in_(deletable_asset_ids),
                        MediaAssetRow.owner_id == job.owner_id,
                        MediaAssetRow.project_id == tombstone.project_id,
                        MediaAssetRow.ownership_scope == "PROJECT",
                    )
                    .values(ownership_scope="OWNER")
                )
            late_deletable: list[str] = []
            for item in skipped_shared:
                media = session.scalar(
                    select(MediaAssetRow)
                    .where(
                        MediaAssetRow.id == item.asset_id,
                        MediaAssetRow.owner_id == job.owner_id,
                    )
                    .with_for_update()
                )
                if media is None:
                    continue
                external_references = tuple(
                    session.execute(
                        select(ProjectAssetReferenceRow, ProjectRow)
                        .join(ProjectRow, ProjectRow.id == ProjectAssetReferenceRow.project_id)
                        .where(
                            ProjectAssetReferenceRow.asset_id == media.id,
                            ProjectAssetReferenceRow.project_id != tombstone.project_id,
                        )
                        .with_for_update(of=ProjectAssetReferenceRow)
                    )
                )
                if not external_references:
                    elected_elsewhere = None
                    if media.deletion_state == "DELETING":
                        elected_elsewhere = session.scalar(
                            select(ProjectPurgeObjectRow.object_key)
                            .join(
                                ProjectPurgeJobRow,
                                ProjectPurgeJobRow.project_id
                                == ProjectPurgeObjectRow.project_id,
                            )
                            .where(
                                ProjectPurgeObjectRow.asset_id == media.id,
                                ProjectPurgeObjectRow.project_id != tombstone.project_id,
                                ProjectPurgeObjectRow.deletion_token
                                == media.deletion_token,
                                ProjectPurgeObjectRow.deletion_generation
                                == media.deletion_generation,
                            )
                        )
                    if elected_elsewhere is not None:
                        continue
                    token = item.deletion_token or uuid4()
                    item.deletion_token = token
                    item.deletion_generation += 1
                    media.deletion_state = "DELETING"
                    media.deletion_token = token
                    media.deletion_generation = item.deletion_generation
                    late_deletable.append(item.object_key)
                    continue
                if any(
                    reference.owner_id != job.owner_id or project.owner_id != job.owner_id
                    for reference, project in external_references
                ):
                    raise DomainError(
                        "project_delete_conflict",
                        "shared survivor reference crosses account ownership",
                    )
                if media.ownership_scope == "OWNER" and media.project_id is None:
                    media.shared = True
                    media.purging = False
                    continue
                if media.ownership_scope == "GLOBAL" and media.project_id is None:
                    media.shared = True
                    media.purging = False
                    continue
                if (
                    media.ownership_scope == "PROJECT"
                    and media.project_id == tombstone.project_id
                ):
                    media.ownership_scope = "OWNER"
                    media.project_id = None
                    media.shared = True
                    media.purging = False
                    continue
                raise DomainError(
                    "project_delete_conflict",
                    "shared survivor has incompatible canonical ownership",
                )
            if late_deletable:
                return tuple(late_deletable)
            for item in deletable_journals:
                retained_journal = session.scalar(
                    select(StagedObjectRow.id).where(
                        StagedObjectRow.id == item.staged_object_id,
                        StagedObjectRow.project_id == tombstone.project_id,
                        StagedObjectRow.owner_id == job.owner_id,
                        StagedObjectRow.ownership_scope == "PROJECT",
                        StagedObjectRow.state == "CLEANUP_PENDING",
                        StagedObjectRow.execution_token == item.deletion_token,
                        StagedObjectRow.generation == item.deletion_generation,
                    )
                )
                if retained_journal is None:
                    raise DomainError(
                        "project_delete_conflict",
                        "staged object cleanup claim changed before authoritative cleanup",
                    )
            self._take_over_staged_objects(session, job.owner_id, tombstone.project_id)
            session.execute(
                delete(ProjectRow).where(
                    ProjectRow.id == tombstone.project_id, ProjectRow.owner_id == job.owner_id
                )
            )
            for item in deletable:
                if item.object_kind != "MEDIA" or item.asset_id is None:
                    continue
                media = session.scalar(
                    select(MediaAssetRow)
                    .where(MediaAssetRow.id == item.asset_id)
                    .with_for_update()
                )
                if media is None:
                    continue
                remaining_references = session.scalar(
                    select(func.count())
                    .select_from(ProjectAssetReferenceRow)
                    .where(ProjectAssetReferenceRow.asset_id == item.asset_id)
                )
                if (
                    remaining_references
                    or media.owner_id != job.owner_id
                    or media.deletion_state != "DELETING"
                    or media.deletion_token != item.deletion_token
                    or media.deletion_generation != item.deletion_generation
                ):
                    raise DomainError(
                        "project_delete_conflict",
                        "frozen media assets changed before authoritative cleanup",
                    )
                session.delete(media)
            session.execute(
                delete(PurgeOutboxRow).where(PurgeOutboxRow.project_id == tombstone.project_id)
            )
            session.execute(
                delete(ProjectPurgeJobRow).where(
                    ProjectPurgeJobRow.project_id == tombstone.project_id
                )
            )
            session.add(
                DeletionTombstoneRow(
                    project_id=tombstone.project_id,
                    deleted_at=tombstone.deleted_at,
                    object_count=tombstone.object_count,
                )
            )
            return ()

    def take_over_staged_objects(self, owner_id: UUID, project_id: UUID) -> int:
        """Convert project-private upload evidence into anonymous deletion tombstones."""
        with self._sessions() as session, session.begin():
            return self._take_over_staged_objects(session, owner_id, project_id)

    @staticmethod
    def _take_over_staged_objects(
        session: Session, owner_id: UUID, project_id: UUID
    ) -> int:
        rows = tuple(
            session.scalars(
                select(StagedObjectRow)
                .where(
                    StagedObjectRow.ownership_scope == "PROJECT",
                    StagedObjectRow.owner_id == owner_id,
                    StagedObjectRow.project_id == project_id,
                )
                .with_for_update()
            )
        )
        for row in rows:
            row.ownership_scope = "GLOBAL"
            row.owner_id = None
            row.project_id = None
            row.purpose = "PROJECT_DELETION"
            row.kind = None
            row.state = "CLEANUP_PENDING"
            row.sha256 = None
            row.size_bytes = None
            row.media_type = None
            row.generation += 1
            row.execution_token = uuid4()
            row.asset_id = None
            row.reference_type = None
            row.lease_until = None
            row.cleanup_after = datetime.now(UTC)
            row.cleanup_checked_at = None
            row.cleanup_attempt = 0
            row.logical_object_key = None
        return len(rows)

    def is_shared_referenced(self, object_key: str, claim: PurgeClaim) -> bool:
        with self._sessions() as session, session.begin():
            job = session.scalar(
                select(ProjectPurgeJobRow)
                .where(ProjectPurgeJobRow.project_id == claim.project_id)
                .with_for_update()
            )
            if not self._claim_matches(job, claim):
                raise DomainError("stale_purge_workflow", "purge cleanup claim is stale")
            manifest = session.scalar(
                select(ProjectPurgeObjectRow).where(
                    ProjectPurgeObjectRow.project_id == claim.project_id,
                    ProjectPurgeObjectRow.object_key == object_key,
                ).with_for_update()
            )
            if manifest is None or manifest.object_kind in {"STAGED", "COVER"}:
                return False
            if manifest.asset_id is None:
                raise DomainError("invalid_purge_manifest", "media purge entry has no asset id")
            media = session.scalar(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.id == manifest.asset_id,
                    MediaAssetRow.owner_id == manifest.owner_id,
                )
                .with_for_update()
            )
            if media is None:
                return False
            session.execute(
                delete(ProjectAssetReferenceRow).where(
                    ProjectAssetReferenceRow.project_id == claim.project_id,
                    ProjectAssetReferenceRow.asset_id == media.id,
                    ProjectAssetReferenceRow.owner_id == manifest.owner_id,
                )
            )
            remaining = session.scalar(
                select(func.count())
                .select_from(ProjectAssetReferenceRow)
                .where(ProjectAssetReferenceRow.asset_id == media.id)
            )
            if remaining:
                return True
            token = manifest.deletion_token or uuid4()
            manifest.deletion_token = token
            manifest.deletion_generation += 1
            media.deletion_state = "DELETING"
            media.deletion_token = token
            media.deletion_generation = manifest.deletion_generation
            return False

    @staticmethod
    def _claim_matches(
        job: ProjectPurgeJobRow | None,
        claim: PurgeClaim,
        *,
        now: datetime | None = None,
    ) -> bool:
        checked_at = now or datetime.now(UTC)
        return bool(
            job is not None
            and job.project_id == claim.project_id
            and job.owner_id == claim.owner_id
            and job.workflow_generation == claim.workflow_generation
            and job.execution_token == claim.execution_token
            and job.lease_until is not None
            and job.lease_until >= checked_at
        )


def _result_dict(item: ObjectDeletionResult) -> dict[str, Any]:
    return {
        "object_key": item.object_key,
        "attempt": item.attempt,
        "attempted_at": item.attempted_at.isoformat(),
        "succeeded": item.succeeded,
        "error_code": item.error_code,
        "skipped_shared": item.skipped_shared,
    }


def _result(item: dict[str, Any]) -> ObjectDeletionResult:
    return ObjectDeletionResult(
        str(item["object_key"]),
        int(item["attempt"]),
        datetime.fromisoformat(str(item["attempted_at"])),
        bool(item["succeeded"]),
        str(item["error_code"]) if item.get("error_code") else None,
        bool(item.get("skipped_shared", False)),
    )


class SqlAlchemyCompositionOutbox:
    def __init__(self, sessions: SessionFactory, *, lease_seconds: int = 30) -> None:
        self._sessions = sessions
        self._lease_seconds = lease_seconds

    def reconcile_lost_dispatches(
        self,
        *,
        older_than: datetime,
        max_attempts: int,
        backoff_seconds: tuple[float, ...],
    ) -> int:
        """对账超时失联的 DISPATCHED 工作流（容器/Worker 崩溃心跳超时死亡）。"""
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            rows = session.scalars(
                select(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.state == "DISPATCHED",
                    CompositionOutboxRow.dispatched_at.is_not(None),
                    CompositionOutboxRow.dispatched_at < older_than,
                )
                .with_for_update(skip_locked=True)
            ).all()
            scheduled = 0
            for outbox in rows:
                # §17.3：COMPLETED 运行的完成前残留行收口为 SUPERSEDED（不再重排/派发）；
                # 完成后新 revision（如用户手动重试）不在此列，保持既有重试语义；
                # 项目仍有 RUNNING 合成任务时不动（在途对账优先）。
                run_completed_at = session.scalar(
                    select(AutoPipelineRunRow.updated_at)
                    .where(
                        AutoPipelineRunRow.project_id == outbox.project_id,
                        AutoPipelineRunRow.owner_id == outbox.owner_id,
                        AutoPipelineRunRow.state == "COMPLETED",
                    )
                    .order_by(
                        AutoPipelineRunRow.created_at.desc(), AutoPipelineRunRow.id.desc()
                    )
                    .limit(1)
                )
                if run_completed_at is not None:
                    revision_created_at = session.scalar(
                        select(CompositionInputRevisionRow.created_at).where(
                            CompositionInputRevisionRow.id == outbox.revision_id
                        )
                    )
                    running_job_id = session.scalar(
                        select(CompositionJobRow.id).where(
                            CompositionJobRow.project_id == outbox.project_id,
                            CompositionJobRow.owner_id == outbox.owner_id,
                            CompositionJobRow.state == "RUNNING",
                        )
                    )
                    if (
                        revision_created_at is not None
                        and revision_created_at <= run_completed_at
                        and running_job_id is None
                    ):
                        outbox.state = "SUPERSEDED"
                        outbox.lease_token = None
                        outbox.lease_until = None
                        continue
                latest_generation = session.scalar(
                    select(func.max(CompositionOutboxRow.workflow_generation)).where(
                        CompositionOutboxRow.project_id == outbox.project_id,
                        CompositionOutboxRow.owner_id == outbox.owner_id,
                        CompositionOutboxRow.inputs_sha256 == outbox.inputs_sha256,
                        CompositionOutboxRow.budget_round == outbox.budget_round,
                    )
                )
                if latest_generation != outbox.workflow_generation:
                    # 已有更新代际承接，本行属历史滞留，不再重排（避免代际冲突）。
                    continue
                job = session.scalar(
                    select(CompositionJobRow)
                    .where(
                        CompositionJobRow.project_id == outbox.project_id,
                        CompositionJobRow.owner_id == outbox.owner_id,
                        CompositionJobRow.workflow_generation
                        == outbox.workflow_generation,
                        CompositionJobRow.state.in_(("RUNNING", "FAILED")),
                    )
                    .with_for_update()
                )
                if job is None:
                    continue
                if job.state == "RUNNING":
                    # 工作流失联：如实落失败与稳定码，不伪造成功。
                    job.state = "FAILED"
                    job.error_code = "composition_workflow_lost"
                    outbox.last_error_code = "composition_workflow_lost"
                production_mode = session.scalar(
                    select(ProjectRow.production_mode).where(
                        ProjectRow.id == outbox.project_id,
                        ProjectRow.owner_id == outbox.owner_id,
                    )
                )
                purging = session.scalar(
                    select(ProjectPurgeJobRow.project_id).where(
                        ProjectPurgeJobRow.project_id == outbox.project_id
                    )
                )
                if (
                    production_mode != "AUTO"
                    or purging is not None
                    or job.attempt >= max_attempts
                ):
                    continue
                # AUTO：按既有自动重试语义排程新代际（5s/15s/30s 退避、轮次命名空间）。
                generation = outbox.workflow_generation + 1
                outbox.workflow_generation = generation
                outbox.workflow_id = _composition_workflow_id(
                    outbox.owner_id,
                    outbox.project_id,
                    outbox.inputs_sha256,
                    outbox.budget_round,
                    generation,
                )
                outbox.state = "PENDING"
                outbox.attempt = 0
                outbox.lease_token = None
                outbox.lease_until = None
                delay = backoff_seconds[min(job.attempt - 1, len(backoff_seconds) - 1)]
                outbox.next_attempt_at = now + timedelta(seconds=delay)
                outbox.dispatched_at = None
                scheduled += 1
        return scheduled

    def close_superseded(self, owner_id: UUID, project_id: UUID) -> int:
        """项目已产出当前成片后收口全部非终态行；无当前成片时不动。返回收口行数。"""
        with self._sessions() as session, session.begin():
            current_exists = session.scalar(
                select(func.count())
                .select_from(CompositionVersionRow)
                .where(
                    CompositionVersionRow.project_id == project_id,
                    CompositionVersionRow.owner_id == owner_id,
                    CompositionVersionRow.current.is_(True),
                )
            )
            if not current_exists:
                return 0
            rows = session.scalars(
                select(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.project_id == project_id,
                    CompositionOutboxRow.owner_id == owner_id,
                    CompositionOutboxRow.state.in_(("PENDING", "DISPATCHED")),
                )
                .with_for_update()
            ).all()
            for row in rows:
                row.state = "SUPERSEDED"
                row.lease_token = None
                row.lease_until = None
            return len(rows)

    def claim(self, *, now: datetime | None = None) -> CompositionEnvelope | None:
        claimed_at = now or datetime.now(UTC)
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.state == "PENDING",
                    CompositionOutboxRow.next_attempt_at <= claimed_at,
                    (
                        CompositionOutboxRow.lease_until.is_(None)
                        | (CompositionOutboxRow.lease_until < claimed_at)
                    ),
                )
                .order_by(CompositionOutboxRow.next_attempt_at, CompositionOutboxRow.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            token = uuid4()
            row.lease_token = token
            row.lease_until = claimed_at + timedelta(seconds=self._lease_seconds)
            row.attempt += 1
            return CompositionEnvelope(
                row.id,
                row.project_id,
                row.owner_id,
                row.revision_id,
                row.inputs_sha256,
                row.workflow_id,
                row.attempt,
                row.workflow_generation,
                token,
            )

    def succeeded(self, envelope: CompositionEnvelope) -> None:
        with self._sessions() as session, session.begin():
            session.execute(
                update(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.id == envelope.id,
                    CompositionOutboxRow.workflow_generation == envelope.workflow_generation,
                    CompositionOutboxRow.lease_token == envelope.lease_token,
                    CompositionOutboxRow.state == "PENDING",
                )
                .values(
                    state="DISPATCHED",
                    dispatched_at=datetime.now(UTC),
                    lease_token=None,
                    lease_until=None,
                    last_error_code=None,
                )
            )

    def failed(
        self,
        envelope: CompositionEnvelope,
        error_code: str,
        *,
        now: datetime | None = None,
    ) -> None:
        failed_at = now or datetime.now(UTC)
        delay_seconds = min(300, 2 ** min(envelope.attempt - 1, 8))
        with self._sessions() as session, session.begin():
            session.execute(
                update(CompositionOutboxRow)
                .where(
                    CompositionOutboxRow.id == envelope.id,
                    CompositionOutboxRow.workflow_generation == envelope.workflow_generation,
                    CompositionOutboxRow.lease_token == envelope.lease_token,
                    CompositionOutboxRow.state == "PENDING",
                )
                .values(
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=failed_at + timedelta(seconds=delay_seconds),
                    last_error_code=error_code,
                )
            )


class SqlAlchemyPurgeOutbox:
    def __init__(self, sessions: SessionFactory, *, lease_seconds: int = 30) -> None:
        self._sessions = sessions
        self._lease_seconds = lease_seconds

    def claim(self) -> PurgeEnvelope | None:
        now = datetime.now(UTC)
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(PurgeOutboxRow)
                .where(
                    PurgeOutboxRow.dispatched_at.is_(None),
                    (PurgeOutboxRow.lease_until.is_(None) | (PurgeOutboxRow.lease_until < now)),
                )
                .order_by(PurgeOutboxRow.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.lease_until = now + timedelta(seconds=self._lease_seconds)
            row.lease_token = uuid4()
            row.attempt += 1
            return PurgeEnvelope(
                row.id,
                row.project_id,
                row.owner_id,
                row.attempt,
                row.workflow_generation,
                row.lease_token,
            )

    def succeeded(self, envelope: PurgeEnvelope) -> None:
        with self._sessions() as session, session.begin():
            session.execute(
                update(PurgeOutboxRow)
                .where(
                    PurgeOutboxRow.id == envelope.id,
                    PurgeOutboxRow.workflow_generation == envelope.workflow_generation,
                    PurgeOutboxRow.lease_token == envelope.lease_token,
                )
                .values(
                    dispatched_at=datetime.now(UTC),
                    lease_until=None,
                    lease_token=None,
                    last_error_code=None,
                )
            )

    def failed(self, envelope: PurgeEnvelope, error_code: str) -> None:
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(PurgeOutboxRow)
                .where(
                    PurgeOutboxRow.id == envelope.id,
                    PurgeOutboxRow.workflow_generation == envelope.workflow_generation,
                    PurgeOutboxRow.lease_token == envelope.lease_token,
                )
                .with_for_update()
            )
            if row is None:
                return
            row.lease_until = datetime.now(UTC) + timedelta(seconds=self._lease_seconds)
            row.lease_token = None
            row.last_error_code = error_code
            job = session.get(ProjectPurgeJobRow, row.project_id)
            if job is not None:
                job.dispatch_error_code = error_code
