from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, Session, mapped_column

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.identity_project.models import Base, ProjectRow
from tubeflow_test.adapters.persistence.models import MediaAssetRow, StagedObjectRow
from tubeflow_test.adapters.persistence.staged_claims import lock_staged_claim_identity
from tubeflow_test.application.narration_authority import AuthoritativeNarrationGroup
from tubeflow_test.application.narration_ports import (
    NarrationJob,
    NarrationRepositoryPort,
    not_before_pending,
)
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import (
    AudioAsset,
    NarratedSentence,
    NarrationAttempt,
    NarrationAttemptState,
    NarrationGroup,
    ScriptVersion,
    TimelineEntry,
    assert_video_generation_allowed,
    authoritative_group_id,
    authoritative_sentence_id,
    is_nonvisual_script_separator,
)


class NarrationAttemptRecord(Base):
    __tablename__ = "narration_attempt_records"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_narration_attempt_records_owner_key"
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    # §23.7：EL TTS 响应头 request-id（对账凭证 = payload.remote_id；本列为可查询副本）。
    provider_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class NarrationProjectRecord(Base):
    __tablename__ = "narration_project_records"
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


class NarrationAttemptEventRecord(Base):
    __tablename__ = "narration_attempt_event_records"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


NarrationStagedObjectRecord = StagedObjectRow


class AuthoritativeNarrationGroupRecord(Base):
    __tablename__ = "authoritative_narration_groups"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "snapshot_revision",
            "sequence",
            name="uq_narration_groups_project_revision_sequence",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_revision: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    start_frame: Mapped[int] = mapped_column(BigInteger)
    end_frame: Mapped[int] = mapped_column(BigInteger)
    sample_rate_hz: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    video_blocked: Mapped[bool] = mapped_column(Boolean)
    block_reason: Mapped[str | None] = mapped_column(String(128))


class AuthoritativeNarrationSentenceRecord(Base):
    __tablename__ = "authoritative_narration_sentences"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "snapshot_revision",
            "sequence",
            name="uq_narration_sentences_project_revision_sequence",
        ),
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_revision: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    raw_asset_id: Mapped[UUID] = mapped_column(ForeignKey("media_assets.id"))
    normalized_asset_id: Mapped[UUID] = mapped_column(ForeignKey("media_assets.id"))
    raw_qc_passed: Mapped[bool] = mapped_column(Boolean)
    normalized_qc_passed: Mapped[bool] = mapped_column(Boolean)
    measured_frame_count: Mapped[int] = mapped_column(BigInteger)
    sample_rate_hz: Mapped[int] = mapped_column(Integer)
    measured_duration_ms: Mapped[int] = mapped_column(BigInteger)
    start_frame: Mapped[int] = mapped_column(BigInteger)
    end_frame: Mapped[int] = mapped_column(BigInteger)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)


class AuthoritativeNarrationMembershipRecord(Base):
    __tablename__ = "authoritative_narration_memberships"
    __table_args__ = (
        UniqueConstraint("sentence_id", name="uq_authoritative_narration_memberships_sentence_id"),
        UniqueConstraint(
            "group_id", "ordinal", name="uq_authoritative_narration_memberships_group_ordinal"
        ),
    )
    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), primary_key=True
    )
    sentence_id: Mapped[UUID] = mapped_column(
        ForeignKey("authoritative_narration_sentences.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class InMemoryNarrationRepository(NarrationRepositoryPort):
    """Feature-local transactional fake; production SQL adapter follows the same owner keys."""

    def __init__(self) -> None:
        self._attempts: dict[tuple[UUID, str], NarrationAttempt] = {}
        self._narrated: dict[tuple[UUID, str], NarratedSentence] = {}
        self._scripts: dict[tuple[UUID, UUID], ScriptVersion] = {}
        self._timelines: dict[tuple[UUID, UUID], tuple[TimelineEntry, ...]] = {}
        self._groups: dict[tuple[UUID, UUID], tuple[NarrationGroup, ...]] = {}
        self.events: list[tuple[NarrationAttempt, str]] = []
        self._jobs: dict[tuple[UUID, UUID], NarrationJob] = {}
        self._pending_recovery_jobs: dict[tuple[UUID, UUID], list[NarrationJob]] = {}
        self._finished_jobs: dict[str, NarrationJob] = {}
        self._checkpoints: dict[tuple[UUID, str], AudioAsset] = {}
        self.checkpoint_states: dict[tuple[UUID, str], str] = {}
        self._claims: dict[tuple[UUID, str], str] = {}
        # 测试可覆盖生产模式；默认 MANUAL 保持既有行为。
        self.production_mode = "MANUAL"
        # 测试可覆盖预算轮次（§15.9 恢复）；默认第 1 轮。
        self.auto_budget_round = 1
        self._blocking_reasons: dict[tuple[UUID, UUID], dict[str, object]] = {}

    def find_by_idempotency_key(self, owner_id: UUID, key: str) -> NarratedSentence | None:
        return self._narrated.get((owner_id, key))

    def attempt(self, owner_id: UUID, key: str) -> NarrationAttempt | None:
        return self._attempts.get((owner_id, key))

    def list_attempts(self, owner_id: UUID, project_id: UUID) -> tuple[NarrationAttempt, ...]:
        values = [
            item
            for (owner, _), item in self._attempts.items()
            if owner == owner_id and item.project_id == project_id
        ]
        return tuple(sorted(values, key=lambda item: item.sentence_sequence))

    def save_attempt(self, attempt: NarrationAttempt) -> None:
        key = (attempt.owner_id, attempt.idempotency_key)
        existing = self._attempts.get(key)
        if existing is not None and existing.request_sha256 != attempt.request_sha256:
            raise DomainError("idempotency_conflict", "Idempotency request hash changed.")
        if existing is not None:
            _assert_attempt_transition(existing.state, attempt.state)
            _assert_claim_owner(existing, attempt)
        self._attempts[key] = attempt

    def claim_submission(
        self, attempt: NarrationAttempt, claim_token: str, lease_until: datetime
    ) -> NarrationAttempt | None:
        key = (attempt.owner_id, attempt.idempotency_key)
        current = self._attempts.get(key)
        if current is None or current.request_sha256 != attempt.request_sha256:
            raise DomainError("idempotency_conflict", "Narration intent is missing or changed.")
        claim = self._claims.get(key)
        if current.state is NarrationAttemptState.SUBMISSION_STARTED:
            if claim and claim > datetime.now(UTC).isoformat():
                return None
            unknown = replace(
                current,
                state=NarrationAttemptState.REMOTE_STATE_UNKNOWN,
                safe_error_code="submission_lease_expired",
            )
            self._attempts[key] = unknown
            return None
        if current.state not in {
            NarrationAttemptState.INTENT_RECORDED,
            NarrationAttemptState.SAFE_TO_RETRY,
        }:
            return None
        started = replace(
            current,
            state=NarrationAttemptState.SUBMISSION_STARTED,
            claim_token=claim_token,
            lease_until=lease_until.isoformat(),
        )
        self._attempts[key] = started
        self._claims[key] = lease_until.isoformat()
        return started

    def record_event(self, attempt: NarrationAttempt, event: str) -> None:
        self.events.append((attempt, event))

    def save_completed(self, attempt: NarrationAttempt, narration: NarratedSentence) -> None:
        # These two writes model one database transaction and never expose half-completion.
        self.save_attempt(attempt)
        self._narrated[(narration.owner_id, narration.idempotency_key)] = narration

    def list_narrated(self, owner_id: UUID, project_id: UUID) -> tuple[NarratedSentence, ...]:
        values = [
            item
            for (owner, _), item in self._narrated.items()
            if owner == owner_id and item.project_id == project_id
        ]
        return tuple(sorted(values, key=lambda item: item.sentence.sequence))

    def save_script(self, owner_id: UUID, project_id: UUID, script: ScriptVersion) -> None:
        key = (owner_id, project_id)
        existing = self._scripts.get(key)
        if existing is not None and existing != script:
            raise DomainError(
                "project_snapshot_locked", "Immutable narration script cannot change."
            )
        self._scripts[key] = script

    def get_script(self, owner_id: UUID, project_id: UUID) -> ScriptVersion | None:
        return self._scripts.get((owner_id, project_id))

    def save_timeline(
        self,
        owner_id: UUID,
        project_id: UUID,
        timeline: tuple[TimelineEntry, ...],
        groups: tuple[NarrationGroup, ...],
    ) -> None:
        if any(entry.end_ms <= entry.start_ms for entry in timeline):
            raise DomainError("timeline_invalid", "Measured timeline is invalid.")
        self._timelines[(owner_id, project_id)] = timeline
        self._groups[(owner_id, project_id)] = groups

    def get_timeline(self, owner_id: UUID, project_id: UUID) -> tuple[TimelineEntry, ...]:
        return self._timelines.get((owner_id, project_id), ())

    def get_groups(self, owner_id: UUID, project_id: UUID) -> tuple[NarrationGroup, ...]:
        return self._groups.get((owner_id, project_id), ())

    def save_job(self, job: NarrationJob) -> None:
        self._jobs.setdefault((job.owner_id, job.project_id), job)

    def get_job(self, owner_id: UUID, project_id: UUID) -> NarrationJob | None:
        return self._jobs.get((owner_id, project_id))

    def pending_jobs(self) -> tuple[NarrationJob, ...]:
        now = datetime.now(UTC)
        return tuple(
            job
            for job in self._jobs.values()
            if job.state == "PENDING" and not not_before_pending(job.not_before, now)
        )

    def mark_job_dispatched(self, job_id: str) -> None:
        for key, job in self._jobs.items():
            if job.job_id == job_id and job.state == "PENDING":
                self._jobs[key] = NarrationJob(
                    job.job_id,
                    job.owner_id,
                    job.project_id,
                    job.sentence_sequences,
                    "DISPATCHED",
                    job.failure_sequences,
                )
                return

    def finish_job(self, job_id: str, state: str, failures: tuple[int, ...]) -> None:
        if state not in {"COMPLETED", "PARTIAL_FAILED", "FAILED"}:
            raise ValueError("invalid narration job terminal state")
        if job_id in self._finished_jobs:
            return
        for key, job in self._jobs.items():
            if job.job_id == job_id:
                completed = replace(job, state=state, failure_sequences=failures)
                self._finished_jobs[job_id] = completed
                pending = self._pending_recovery_jobs.get(key, [])
                self._jobs[key] = pending.pop(0) if pending else completed
                return
        raise DomainError("not_found", "Narration job is missing.")

    def save_provider_checkpoint(self, attempt: NarrationAttempt, asset: AudioAsset) -> None:
        self._checkpoints.setdefault((attempt.owner_id, attempt.idempotency_key), asset)

    def provider_checkpoint(self, owner_id: UUID, key: str) -> AudioAsset | None:
        return self._checkpoints.get((owner_id, key))

    def set_checkpoint_ledger(
        self,
        attempt: NarrationAttempt,
        state: str,
        asset: AudioAsset | None = None,
        receipt: dict[str, object] | None = None,
    ) -> None:
        if state not in {"PENDING_UPLOAD", "STAGED", "ATTACHED"}:
            raise ValueError("invalid checkpoint state")
        self.checkpoint_states[(attempt.owner_id, attempt.idempotency_key)] = state
        if asset is not None:
            self._checkpoints[(attempt.owner_id, attempt.idempotency_key)] = asset
        del receipt

    def reconcile_and_enqueue(
        self, attempt: NarrationAttempt, event: str, job: NarrationJob | None
    ) -> None:
        existing = self.attempt(attempt.owner_id, attempt.idempotency_key)
        if existing is None:
            raise DomainError("not_found", "Narration attempt is missing.")
        if _reconciliation_replayed(existing, attempt):
            return
        self.save_attempt(attempt)
        self.record_event(attempt, event)
        if job is not None:
            key = (job.owner_id, job.project_id)
            current = self._jobs.get(key)
            if current is not None and current.state in {"PENDING", "DISPATCHED"}:
                self._pending_recovery_jobs.setdefault(key, []).append(job)
            else:
                if current is not None:
                    self._finished_jobs.setdefault(current.job_id, current)
                self._jobs[key] = job

    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None:
        del owner_id, project_id
        return self.production_mode

    def project_auto_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        del owner_id, project_id
        return self.auto_budget_round

    def schedule_auto_retry(
        self, attempt: NarrationAttempt | None, event: str, job: NarrationJob
    ) -> bool:
        key = (job.owner_id, job.project_id)
        current = self._jobs.get(key)
        if (
            (current is not None and current.job_id == job.job_id)
            or any(
                item.job_id == job.job_id for item in self._pending_recovery_jobs.get(key, [])
            )
            or job.job_id in self._finished_jobs
        ):
            return False
        if attempt is not None:
            self.save_attempt(attempt)
            self.record_event(attempt, event)
        if current is not None and current.state in {"PENDING", "DISPATCHED"}:
            self._pending_recovery_jobs.setdefault(key, []).append(job)
        else:
            if current is not None:
                self._finished_jobs.setdefault(current.job_id, current)
            self._jobs[key] = job
        return True

    def save_blocking_reason(
        self, owner_id: UUID, project_id: UUID, reasons: dict[int, str] | None
    ) -> None:
        key = (owner_id, project_id)
        if reasons is None:
            self._blocking_reasons.pop(key, None)
            return
        self._blocking_reasons[key] = {
            "sequences": {
                str(sequence): code for sequence, code in sorted(reasons.items())
            },
            "recorded_at": datetime.now(UTC).isoformat(),
        }

    def blocking_reason(self, owner_id: UUID, project_id: UUID) -> dict[str, object] | None:
        return self._blocking_reasons.get((owner_id, project_id))


class InMemoryAudioArchive:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.assets: dict[str, tuple[AudioAsset, str]] = {}

    def save(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        kind: str,
        payload: bytes,
        metadata: dict[str, object],
    ) -> AudioAsset:
        del owner_id, project_id, metadata
        if not payload or kind not in {
            "narration_raw",
            "narration_normalized",
            "narration_provider_zip",
        }:
            raise ValueError("invalid narration archive input")
        asset_id = str(uuid4())
        self.objects[asset_id] = payload
        media_type = "application/zip" if kind == "narration_provider_zip" else "audio/wav"
        asset = AudioAsset(asset_id, hashlib.sha256(payload).hexdigest(), media_type, len(payload))
        self.assets[asset_id] = (asset, kind)
        return asset

    def load(self, *, owner_id: UUID, project_id: UUID, asset_id: str) -> bytes:
        del owner_id, project_id
        payload = self.objects[asset_id]
        asset, kind = self.assets[asset_id]
        if (
            kind != "narration_provider_zip"
            or asset.media_type != "application/zip"
            or len(payload) != asset.size_bytes
            or hashlib.sha256(payload).hexdigest() != asset.sha256
        ):
            raise DomainError("checkpoint_integrity_failed", "Provider checkpoint is invalid.")
        return payload


class PrivateNarrationAudioArchive:
    def __init__(
        self,
        storage: PrivateObjectStorage,
        session_factory: Callable[[], Session],
        *,
        after_final_commit: Callable[[], None] | None = None,
    ) -> None:
        self._storage, self._session_factory = storage, session_factory
        self._after_final_commit = after_final_commit

    def save(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        kind: str,
        payload: bytes,
        metadata: dict[str, object],
    ) -> AudioAsset:
        if kind not in {"narration_raw", "narration_normalized", "narration_provider_zip"}:
            raise ValueError("unsupported narration asset kind")
        if not payload:
            raise ValueError("empty narration assets are not accepted")
        idempotency_key = metadata.get("idempotency_key") or metadata.get("provider_request_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("narration archive requires an idempotency key")
        media_type = "application/zip" if kind == "narration_provider_zip" else "audio/wav"
        digest = hashlib.sha256(payload).hexdigest()
        sentence_identity = metadata.get("sentence_sequence")
        if sentence_identity is None:
            manifest = metadata.get("manifest")
            if not isinstance(manifest, list) or not all(
                isinstance(item, str) for item in manifest
            ):
                raise ValueError("narration archive requires a sentence or manifest identity")
            sentence_identity = hashlib.sha256("\n".join(manifest).encode()).hexdigest()
        canonical_name = (
            f"{owner_id}:{project_id}:{idempotency_key}:{sentence_identity}:{kind}:{digest}"
        )
        asset_id = uuid5(NAMESPACE_URL, f"tubeflow:narration:asset:{canonical_name}")
        stage_id = uuid5(NAMESPACE_URL, f"tubeflow:narration:stage:{canonical_name}")
        execution_token = uuid5(NAMESPACE_URL, f"tubeflow:narration:execution:{canonical_name}")
        logical_key = f"narration/{asset_id}"
        won_elsewhere = False
        with self._session_factory() as session, session.begin():
            SqlNarrationRepository._lock_active_project(session, owner_id, project_id)
            lock_staged_claim_identity(
                session,
                owner_id=owner_id,
                ownership_scope="PROJECT",
                project_id=project_id,
                logical_object_key=logical_key,
            )
            stage = session.scalar(
                select(NarrationStagedObjectRecord)
                .where(NarrationStagedObjectRecord.logical_object_key == logical_key)
                .order_by(
                    NarrationStagedObjectRecord.generation.desc(),
                    NarrationStagedObjectRecord.created_at.desc(),
                    NarrationStagedObjectRecord.id.desc(),
                )
                .with_for_update()
                .limit(1)
            )
            if stage is not None and stage.state == "FINALIZED":
                finalized_stage_id = stage.id
            else:
                finalized_stage_id = None
            if stage is None:
                generation, actual_key = 1, logical_key
            elif stage.state == "CLEANUP_PENDING":
                self._assert_stage_facts(
                    stage, owner_id, project_id, kind, digest, media_type, len(payload), asset_id
                )
                generation = stage.generation + 1
                stage_id = uuid4()
                execution_token = uuid4()
                actual_key = f"{logical_key}.generation-{generation}-{uuid4().hex}"
                stage = None
            else:
                self._assert_stage_facts(
                    stage, owner_id, project_id, kind, digest, media_type, len(payload), asset_id
                )
                generation, actual_key = stage.generation, stage.object_key
                if stage.execution_token is not None:
                    execution_token = stage.execution_token
            if stage is None:
                session.add(
                    NarrationStagedObjectRecord(
                        id=stage_id,
                        owner_id=owner_id,
                        project_id=project_id,
                        object_key=actual_key,
                        logical_object_key=logical_key,
                        sha256=digest,
                        size_bytes=len(payload),
                        media_type=media_type,
                        purpose="narration",
                        kind=kind,
                        ownership_scope="PROJECT",
                        state="PENDING_UPLOAD",
                        created_at=datetime.now(UTC),
                        generation=generation,
                        execution_token=execution_token,
                        lease_until=datetime.now(UTC) + timedelta(minutes=5),
                        asset_id=asset_id,
                    )
                )
        if finalized_stage_id is not None:
            return self._reconcile_finalized(
                finalized_stage_id, owner_id, project_id, kind, digest, media_type, len(payload)
            )
        stored = self._storage.put_at_key(actual_key, payload, media_type, digest)
        finalized_after_mark_loss: UUID | None = None
        with self._session_factory() as session, session.begin():
            SqlNarrationRepository._lock_active_project(session, owner_id, project_id)
            marked = cast(
                CursorResult[Any],
                session.execute(
                    update(NarrationStagedObjectRecord)
                    .where(
                        NarrationStagedObjectRecord.id == stage_id,
                        NarrationStagedObjectRecord.state.in_(("PENDING_UPLOAD", "STAGED")),
                        NarrationStagedObjectRecord.generation == generation,
                        NarrationStagedObjectRecord.execution_token == execution_token,
                    )
                    .values(state="STAGED", lease_until=None)
                ),
            )
            if not marked.rowcount:
                latest = session.scalar(
                    select(NarrationStagedObjectRecord)
                    .where(NarrationStagedObjectRecord.logical_object_key == logical_key)
                    .order_by(
                        NarrationStagedObjectRecord.generation.desc(),
                        NarrationStagedObjectRecord.created_at.desc(),
                        NarrationStagedObjectRecord.id.desc(),
                    )
                    .limit(1)
                )
                if latest is not None and latest.state == "FINALIZED":
                    finalized_after_mark_loss = latest.id
                else:
                    raise DomainError(
                        "archive_staging_lost", "Narration staging claim was lost."
                    )

        if finalized_after_mark_loss is not None:
            return self._reconcile_finalized(
                finalized_after_mark_loss,
                owner_id,
                project_id,
                kind,
                digest,
                media_type,
                len(payload),
            )

        with self._session_factory() as session, session.begin():
            SqlNarrationRepository._lock_active_project(session, owner_id, project_id)
            stage = session.scalar(
                select(NarrationStagedObjectRecord)
                .where(NarrationStagedObjectRecord.id == stage_id)
                .with_for_update()
            )
            if stage is None:
                raise DomainError("archive_staging_lost", "Narration staging journal is invalid.")
            if stage.state == "FINALIZED":
                won_elsewhere = True
            else:
                self._assert_stage_facts(
                    stage, owner_id, project_id, kind, digest, media_type, len(payload), asset_id
                )
                if (
                    stage.state != "STAGED"
                    or stage.generation != generation
                    or stage.execution_token != execution_token
                    or stage.object_key != actual_key
                ):
                    raise DomainError("archive_staging_lost", "Narration staging claim was lost.")
                session.add(
                    MediaAssetRow(
                        id=asset_id,
                        owner_id=owner_id,
                        project_id=project_id,
                        kind=kind,
                        object_key=stored.key,
                        sha256=stored.sha256,
                        media_type=stored.media_type,
                        size_bytes=stored.size_bytes,
                        metadata_json={**metadata, "staging_journal_id": str(stage_id)},
                        shared=False,
                    )
                )
                stage.state = "FINALIZED"
                stage.execution_token = None
                stage.lease_until = None
        if won_elsewhere:
            return self._reconcile_finalized(
                stage_id, owner_id, project_id, kind, digest, media_type, len(payload)
            )
        if self._after_final_commit is not None:
            self._after_final_commit()
        return AudioAsset(str(asset_id), stored.sha256, stored.media_type, stored.size_bytes)

    @staticmethod
    def _assert_stage_facts(
        stage: NarrationStagedObjectRecord,
        owner_id: UUID,
        project_id: UUID,
        kind: str,
        digest: str,
        media_type: str,
        size_bytes: int,
        asset_id: UUID,
    ) -> None:
        if (
            stage.owner_id != owner_id
            or stage.project_id != project_id
            or stage.kind != kind
            or stage.sha256 != digest
            or stage.media_type != media_type
            or stage.size_bytes != size_bytes
            or stage.asset_id != asset_id
        ):
            raise DomainError("archive_identity_conflict", "Narration archive identity changed.")

    def _reconcile_finalized(
        self,
        stage_id: UUID,
        owner_id: UUID,
        project_id: UUID,
        kind: str,
        digest: str,
        media_type: str,
        size_bytes: int,
    ) -> AudioAsset:
        with self._session_factory() as session:
            stage = session.get(NarrationStagedObjectRecord, stage_id)
            if stage is None or stage.state != "FINALIZED" or stage.asset_id is None:
                raise DomainError("archive_staging_lost", "Finalized narration asset is missing.")
            self._assert_stage_facts(
                stage, owner_id, project_id, kind, digest, media_type, size_bytes, stage.asset_id
            )
            row = session.get(MediaAssetRow, stage.asset_id)
            if not self._finalized_facts_match(
                row, stage, owner_id, project_id, kind, digest, media_type, size_bytes
            ):
                raise DomainError(
                    "archive_integrity_failed", "Finalized narration asset is invalid."
                )
            assert row is not None
            snapshot = (
                stage.generation,
                stage.asset_id,
                stage.object_key,
                row.sha256,
                row.size_bytes,
            )
        try:
            self._storage.get_verified(snapshot[2], snapshot[3], snapshot[4])
        except Exception as error:
            raise DomainError(
                "archive_integrity_failed", "Finalized narration bytes are invalid."
            ) from error
        with self._session_factory() as session:
            stage = session.get(NarrationStagedObjectRecord, stage_id)
            row = session.get(MediaAssetRow, snapshot[1])
            if (
                stage is None
                or stage.state != "FINALIZED"
                or stage.generation != snapshot[0]
                or stage.asset_id != snapshot[1]
                or stage.object_key != snapshot[2]
                or not self._finalized_facts_match(
                    row, stage, owner_id, project_id, kind, digest, media_type, size_bytes
                )
            ):
                raise DomainError(
                    "archive_integrity_failed", "Finalized narration asset changed."
                )
            assert row is not None
            return AudioAsset(str(row.id), row.sha256, row.media_type, row.size_bytes)

    @staticmethod
    def _finalized_facts_match(
        row: MediaAssetRow | None,
        stage: NarrationStagedObjectRecord,
        owner_id: UUID,
        project_id: UUID,
        kind: str,
        digest: str,
        media_type: str,
        size_bytes: int,
    ) -> bool:
        return bool(
            row is not None
            and row.owner_id == owner_id
            and row.project_id == project_id
            and row.kind == kind
            and row.object_key == stage.object_key
            and row.sha256 == digest
            and row.media_type == media_type
            and row.size_bytes == size_bytes
        )

    def load(self, *, owner_id: UUID, project_id: UUID, asset_id: str) -> bytes:
        with self._session_factory() as session:
            row = session.get(MediaAssetRow, UUID(asset_id))
            if row is None or row.owner_id != owner_id or row.project_id != project_id:
                raise DomainError("not_found", "Resource not found.")
            if row.kind != "narration_provider_zip" or row.media_type != "application/zip":
                raise DomainError("checkpoint_integrity_failed", "Provider checkpoint is invalid.")
            payload = self._storage.get(row.object_key)
            if len(payload) != row.size_bytes or hashlib.sha256(payload).hexdigest() != row.sha256:
                raise DomainError("checkpoint_integrity_failed", "Provider checkpoint is invalid.")
            return payload


class SqlNarrationRepository(NarrationRepositoryPort):
    """PostgreSQL-backed owner-scoped narration state using feature-local tables."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def find_by_idempotency_key(self, owner_id: UUID, key: str) -> NarratedSentence | None:
        row = self._attempt_row(owner_id, key)
        narration = row.payload.get("narration") if row else None
        return _narrated_from_json(narration) if isinstance(narration, dict) else None

    def attempt(self, owner_id: UUID, key: str) -> NarrationAttempt | None:
        row = self._attempt_row(owner_id, key)
        value = row.payload.get("attempt") if row else None
        return _attempt_from_json(value) if isinstance(value, dict) else None

    def list_attempts(self, owner_id: UUID, project_id: UUID) -> tuple[NarrationAttempt, ...]:
        with self._session_factory() as session:
            rows = (
                session.query(NarrationAttemptRecord)
                .filter_by(owner_id=owner_id, project_id=project_id)
                .all()
            )
            values = [
                _attempt_from_json(value)
                for row in rows
                if isinstance((value := row.payload.get("attempt")), dict)
            ]
            return tuple(sorted(values, key=lambda item: item.sentence_sequence))

    def save_attempt(self, attempt: NarrationAttempt) -> None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, attempt.owner_id, attempt.project_id)
            row = self._query_attempt(
                session, attempt.owner_id, attempt.idempotency_key, for_update=True
            )
            if row is not None and row.request_sha256 != attempt.request_sha256:
                raise DomainError("idempotency_conflict", "Idempotency request hash changed.")
            payload = dict(row.payload) if row else {}
            payload["attempt"] = _attempt_json(attempt)
            if row is None:
                row = NarrationAttemptRecord(
                    owner_id=attempt.owner_id,
                    project_id=attempt.project_id,
                    idempotency_key=attempt.idempotency_key,
                    request_sha256=attempt.request_sha256,
                    revision=0,
                    payload=payload,
                )
                session.add(row)
            else:
                persisted = row.payload.get("attempt")
                if isinstance(persisted, dict):
                    current = _attempt_from_json(persisted)
                    _assert_attempt_transition(current.state, attempt.state)
                    _assert_claim_owner(current, attempt)
                row.payload = payload
                row.revision += 1
            # §23.7：本列存可对账的裸 request-id（EL 控制台/响应头形态）；
            # attempt.remote_id 另带 `el:` 前缀用于拼接链判别，两者同源不漂移。
            row.provider_request_id = _bare_provider_request_id(attempt.remote_id)

    def claim_submission(
        self, attempt: NarrationAttempt, claim_token: str, lease_until: datetime
    ) -> NarrationAttempt | None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, attempt.owner_id, attempt.project_id)
            row = self._query_attempt(
                session, attempt.owner_id, attempt.idempotency_key, for_update=True
            )
            if row is None or row.request_sha256 != attempt.request_sha256:
                raise DomainError("idempotency_conflict", "Narration intent is missing or changed.")
            value = row.payload.get("attempt")
            if not isinstance(value, dict):
                raise DomainError("not_found", "Narration attempt is missing.")
            current = _attempt_from_json(value)
            payload = dict(row.payload)
            claim = payload.get("submission_claim")
            if current.state is NarrationAttemptState.SUBMISSION_STARTED:
                lease = claim.get("lease_until") if isinstance(claim, dict) else None
                if isinstance(lease, str) and datetime.fromisoformat(lease) > datetime.now(UTC):
                    return None
                unknown = replace(
                    current,
                    state=NarrationAttemptState.REMOTE_STATE_UNKNOWN,
                    safe_error_code="submission_lease_expired",
                )
                payload["attempt"] = _attempt_json(unknown)
                payload["submission_claim"] = {
                    **(claim if isinstance(claim, dict) else {}),
                    "expired": True,
                }
                row.payload = payload
                row.revision += 1
                return None
            if current.state not in {
                NarrationAttemptState.INTENT_RECORDED,
                NarrationAttemptState.SAFE_TO_RETRY,
            }:
                return None
            started = replace(
                current,
                state=NarrationAttemptState.SUBMISSION_STARTED,
                claim_token=claim_token,
                lease_until=lease_until.isoformat(),
            )
            payload["attempt"] = _attempt_json(started)
            payload["submission_claim"] = {
                "claim_token": claim_token,
                "lease_until": lease_until.isoformat(),
            }
            row.payload = payload
            row.revision += 1
            return started

    def record_event(self, attempt: NarrationAttempt, event: str) -> None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, attempt.owner_id, attempt.project_id)
            session.add(
                NarrationAttemptEventRecord(
                    owner_id=attempt.owner_id,
                    project_id=attempt.project_id,
                    idempotency_key=attempt.idempotency_key,
                    state=attempt.state.value,
                    event=event,
                    payload=_attempt_json(attempt),
                )
            )

    def save_completed(self, attempt: NarrationAttempt, narration: NarratedSentence) -> None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, attempt.owner_id, attempt.project_id)
            row = self._query_attempt(
                session, attempt.owner_id, attempt.idempotency_key, for_update=True
            )
            if row is None or row.request_sha256 != attempt.request_sha256:
                raise DomainError("idempotency_conflict", "Narration intent is missing or changed.")
            persisted = row.payload.get("attempt")
            if isinstance(persisted, dict):
                current = _attempt_from_json(persisted)
                _assert_attempt_transition(current.state, attempt.state)
                _assert_claim_owner(current, attempt)
            payload = dict(row.payload)
            payload["attempt"] = _attempt_json(attempt)
            payload["narration"] = _narrated_json(narration)
            row.payload = payload
            row.revision += 1

    def list_narrated(self, owner_id: UUID, project_id: UUID) -> tuple[NarratedSentence, ...]:
        with self._session_factory() as session:
            rows = (
                session.query(NarrationAttemptRecord)
                .filter_by(owner_id=owner_id, project_id=project_id)
                .all()
            )
            values = [
                _narrated_from_json(value)
                for row in rows
                if isinstance((value := row.payload.get("narration")), dict)
            ]
            return tuple(sorted(values, key=lambda item: item.sentence.sequence))

    def save_script(self, owner_id: UUID, project_id: UUID, script: ScriptVersion) -> None:
        with self._session_factory() as session, session.begin():
            row = session.get(NarrationProjectRecord, project_id)
            if row is not None and row.owner_id != owner_id:
                raise DomainError("not_found", "Resource not found.")
            payload = dict(row.payload) if row else {}
            encoded = _script_json(script)
            if "script" in payload and payload["script"] != encoded:
                raise DomainError(
                    "project_snapshot_locked", "Immutable narration script cannot change."
                )
            payload["script"] = encoded
            if row is None:
                session.add(
                    NarrationProjectRecord(
                        project_id=project_id, owner_id=owner_id, payload=payload
                    )
                )
            else:
                row.payload = payload

    def get_script(self, owner_id: UUID, project_id: UUID) -> ScriptVersion | None:
        value = self._project_value(owner_id, project_id, "script")
        return _script_from_json(value) if isinstance(value, dict) else None

    def save_timeline(
        self,
        owner_id: UUID,
        project_id: UUID,
        timeline: tuple[TimelineEntry, ...],
        groups: tuple[NarrationGroup, ...],
    ) -> None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, owner_id, project_id)
            row = session.get(NarrationProjectRecord, project_id)
            if row is None or row.owner_id != owner_id:
                raise DomainError("not_found", "Resource not found.")
            payload = dict(row.payload)
            payload["timeline"] = [_timeline_json(item) for item in timeline]
            payload["groups"] = [_group_json(item) for item in groups]
            row.payload = payload
            script_value = payload.get("script")
            if not isinstance(script_value, dict):
                raise DomainError("narration_not_ready", "Immutable script is missing.")
            script = _script_from_json(script_value)
            attempts = (
                session.query(NarrationAttemptRecord)
                .filter_by(owner_id=owner_id, project_id=project_id)
                .all()
            )
            narrated = {
                item.sentence.sequence: item
                for attempt_row in attempts
                if isinstance((value := attempt_row.payload.get("narration")), dict)
                for item in (_narrated_from_json(value),)
            }
            session.query(AuthoritativeNarrationGroupRecord).filter_by(
                project_id=project_id
            ).delete(synchronize_session=False)
            session.query(AuthoritativeNarrationSentenceRecord).filter_by(
                project_id=project_id
            ).delete(synchronize_session=False)
            sentence_ids: dict[int, UUID] = {}
            by_sequence = {item.sentence_sequence: item for item in timeline}
            for sentence in script.sentences:
                if is_nonvisual_script_separator(sentence.text):
                    # 非视觉分隔句无配音与时间轴条目，不进入权威句子表。
                    continue
                entry = by_sequence[sentence.sequence]
                narration = narrated[sentence.sequence]
                sentence_id = authoritative_sentence_id(
                    project_id, script.source_sha256, sentence.sequence
                )
                sentence_ids[sentence.sequence] = sentence_id
                qc = narration.normalized_qc
                session.add(
                    AuthoritativeNarrationSentenceRecord(
                        id=sentence_id,
                        project_id=project_id,
                        owner_id=owner_id,
                        snapshot_sha256=script.source_sha256,
                        snapshot_revision=1,
                        sequence=sentence.sequence,
                        text=sentence.text,
                        text_sha256=sentence.text_sha256,
                        raw_asset_id=UUID(narration.raw_asset.asset_id),
                        normalized_asset_id=UUID(narration.normalized_asset.asset_id),
                        raw_qc_passed=narration.raw_qc.passed,
                        normalized_qc_passed=narration.normalized_qc.passed,
                        measured_frame_count=int(qc.frame_count or 0),
                        sample_rate_hz=entry.sample_rate_hz,
                        measured_duration_ms=int(qc.measured_duration_ms or 0),
                        start_frame=entry.start_frame,
                        end_frame=entry.end_frame,
                        start_ms=entry.start_ms,
                        end_ms=entry.end_ms,
                    )
                )
            session.flush()
            for group in groups:
                members = [by_sequence[sequence] for sequence in group.sentence_sequences]
                group_id = authoritative_group_id(project_id, script.source_sha256, group.sequence)
                session.add(
                    AuthoritativeNarrationGroupRecord(
                        id=group_id,
                        project_id=project_id,
                        owner_id=owner_id,
                        snapshot_sha256=script.source_sha256,
                        snapshot_revision=1,
                        sequence=group.sequence,
                        start_frame=members[0].start_frame,
                        end_frame=members[-1].end_frame,
                        sample_rate_hz=members[0].sample_rate_hz,
                        start_ms=group.start_ms,
                        end_ms=group.end_ms,
                        video_blocked=group.video_blocked,
                        block_reason=group.block_reason,
                    )
                )
                session.flush()
                for ordinal, sequence in enumerate(group.sentence_sequences, 1):
                    session.add(
                        AuthoritativeNarrationMembershipRecord(
                            group_id=group_id,
                            sentence_id=sentence_ids[sequence],
                            ordinal=ordinal,
                        )
                    )

    def get_timeline(self, owner_id: UUID, project_id: UUID) -> tuple[TimelineEntry, ...]:
        value = self._project_value(owner_id, project_id, "timeline")
        return tuple(_timeline_from_json(item) for item in value) if isinstance(value, list) else ()

    def get_groups(self, owner_id: UUID, project_id: UUID) -> tuple[NarrationGroup, ...]:
        value = self._project_value(owner_id, project_id, "groups")
        return tuple(_group_from_json(item) for item in value) if isinstance(value, list) else ()

    def get_job(self, owner_id: UUID, project_id: UUID) -> NarrationJob | None:
        value = self._project_value(owner_id, project_id, "job")
        return _job_from_json(value) if isinstance(value, dict) else None

    def pending_jobs(self) -> tuple[NarrationJob, ...]:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            rows = session.query(NarrationProjectRecord).all()
            return tuple(
                _job_from_json(value)
                for row in rows
                if isinstance((value := row.payload.get("job")), dict)
                and value.get("state") == "PENDING"
                and not not_before_pending(
                    value.get("not_before")
                    if isinstance(value.get("not_before"), str)
                    else None,
                    now,
                )
            )

    def mark_job_dispatched(self, job_id: str) -> None:
        with self._session_factory() as session, session.begin():
            rows = session.query(NarrationProjectRecord).all()
            for row in rows:
                value = row.payload.get("job")
                if not isinstance(value, dict) or value.get("job_id") != job_id:
                    continue
                self._lock_active_project(session, row.owner_id, row.project_id)
                session.refresh(row, with_for_update=True)
                value = row.payload.get("job")
                if (
                    isinstance(value, dict)
                    and value.get("job_id") == job_id
                    and value.get("state") == "PENDING"
                ):
                    payload = dict(row.payload)
                    payload["job"] = {
                        **value,
                        "state": "DISPATCHED",
                        "revision": int(value.get("revision", 0)) + 1,
                    }
                    row.payload = payload
                return

    def finish_job(self, job_id: str, state: str, failures: tuple[int, ...]) -> None:
        if state not in {"COMPLETED", "PARTIAL_FAILED", "FAILED"}:
            raise ValueError("invalid narration job terminal state")
        with self._session_factory() as session, session.begin():
            rows = session.query(NarrationProjectRecord).all()
            for row in rows:
                value = row.payload.get("job")
                finished = dict(cast(dict[str, object], row.payload.get("finished_jobs", {})))
                if job_id in finished:
                    return
                if not isinstance(value, dict) or value.get("job_id") != job_id:
                    continue
                self._lock_active_project(session, row.owner_id, row.project_id)
                session.refresh(row, with_for_update=True)
                payload = dict(row.payload)
                finished = dict(cast(dict[str, object], payload.get("finished_jobs", {})))
                if job_id in finished:
                    return
                value = payload.get("job")
                if not isinstance(value, dict) or value.get("job_id") != job_id:
                    raise DomainError("not_found", "Narration job is missing.")
                completed = {
                    **value,
                    "state": state,
                    "failure_sequences": list(failures),
                }
                finished[job_id] = completed
                pending = list(cast(list[dict[str, object]], payload.get(
                    "pending_recovery_jobs", []
                )))
                payload["job"] = pending.pop(0) if pending else completed
                payload["finished_jobs"] = finished
                payload["pending_recovery_jobs"] = pending
                row.payload = payload
                return
        raise DomainError("not_found", "Narration job is missing.")

    def save_provider_checkpoint(self, attempt: NarrationAttempt, asset: AudioAsset) -> None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, attempt.owner_id, attempt.project_id)
            row = self._query_attempt(
                session, attempt.owner_id, attempt.idempotency_key, for_update=True
            )
            if row is None:
                raise DomainError("not_found", "Narration attempt is missing.")
            payload = dict(row.payload)
            payload.setdefault("provider_checkpoint", _asset_json(asset))
            row.payload = payload
            row.revision += 1

    def provider_checkpoint(self, owner_id: UUID, key: str) -> AudioAsset | None:
        row = self._attempt_row(owner_id, key)
        value = row.payload.get("provider_checkpoint") if row else None
        return _asset_from_json(value) if isinstance(value, dict) else None

    def set_checkpoint_ledger(
        self,
        attempt: NarrationAttempt,
        state: str,
        asset: AudioAsset | None = None,
        receipt: dict[str, object] | None = None,
    ) -> None:
        if state not in {"PENDING_UPLOAD", "STAGED", "ATTACHED"}:
            raise ValueError("invalid checkpoint state")
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, attempt.owner_id, attempt.project_id)
            row = self._query_attempt(
                session, attempt.owner_id, attempt.idempotency_key, for_update=True
            )
            if row is None:
                raise DomainError("not_found", "Narration attempt is missing.")
            payload = dict(row.payload)
            ledger: dict[str, object] = {"state": state}
            if asset is not None:
                ledger["asset"] = _asset_json(asset)
            if receipt is not None:
                ledger["provider_receipt"] = receipt
            payload["checkpoint_ledger"] = ledger
            if state in {"STAGED", "ATTACHED"} and asset is not None:
                payload["provider_checkpoint"] = _asset_json(asset)
            row.payload = payload
            row.revision += 1

    def reconcile_and_enqueue(
        self, attempt: NarrationAttempt, event: str, job: NarrationJob | None
    ) -> None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, attempt.owner_id, attempt.project_id)
            row = self._query_attempt(
                session, attempt.owner_id, attempt.idempotency_key, for_update=True
            )
            if row is None:
                raise DomainError("not_found", "Narration attempt is missing.")
            persisted = row.payload.get("attempt")
            if not isinstance(persisted, dict):
                raise DomainError("not_found", "Narration attempt is missing.")
            previous = _attempt_from_json(persisted)
            if _reconciliation_replayed(previous, attempt):
                return
            _assert_attempt_transition(previous.state, attempt.state)
            payload = dict(row.payload)
            payload["attempt"] = _attempt_json(attempt)
            row.payload = payload
            row.revision += 1
            session.add(
                NarrationAttemptEventRecord(
                    owner_id=attempt.owner_id,
                    project_id=attempt.project_id,
                    idempotency_key=attempt.idempotency_key,
                    state=attempt.state.value,
                    event=event,
                    payload=_attempt_json(attempt),
                )
            )
            if job is not None:
                project_row = session.get(
                    NarrationProjectRecord, job.project_id, with_for_update=True
                )
                if project_row is None or project_row.owner_id != job.owner_id:
                    raise DomainError("not_found", "Narration project is missing.")
                project_payload = dict(project_row.payload)
                current = project_payload.get("job")
                if isinstance(current, dict) and current.get("state") in {"PENDING", "DISPATCHED"}:
                    pending = list(cast(list[dict[str, object]], project_payload.get(
                        "pending_recovery_jobs", []
                    )))
                    pending.append(_job_json(job))
                    project_payload["pending_recovery_jobs"] = pending
                else:
                    if isinstance(current, dict):
                        finished = dict(cast(dict[str, object], project_payload.get(
                            "finished_jobs", {}
                        )))
                        finished.setdefault(str(current["job_id"]), current)
                        project_payload["finished_jobs"] = finished
                    project_payload["job"] = _job_json(job)
                project_row.payload = project_payload

    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None:
        with self._session_factory() as session:
            row = session.get(ProjectRow, project_id)
            if row is None or row.owner_id != owner_id:
                return None
            return str(row.production_mode or "MANUAL")

    def project_auto_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        with self._session_factory() as session:
            row = session.get(ProjectRow, project_id)
            if row is None or row.owner_id != owner_id:
                return 1
            return int(row.auto_budget_round or 1)

    def schedule_auto_retry(
        self, attempt: NarrationAttempt | None, event: str, job: NarrationJob
    ) -> bool:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, job.owner_id, job.project_id)
            project_row = session.get(
                NarrationProjectRecord, job.project_id, with_for_update=True
            )
            if project_row is None or project_row.owner_id != job.owner_id:
                raise DomainError("not_found", "Narration project is missing.")
            project_payload = dict(project_row.payload)
            current = project_payload.get("job")
            pending = list(
                cast(list[dict[str, object]], project_payload.get("pending_recovery_jobs", []))
            )
            finished = dict(cast(dict[str, object], project_payload.get("finished_jobs", {})))
            if (
                (isinstance(current, dict) and current.get("job_id") == job.job_id)
                or any(item.get("job_id") == job.job_id for item in pending)
                or job.job_id in finished
            ):
                return False
            if attempt is not None:
                row = self._query_attempt(
                    session, attempt.owner_id, attempt.idempotency_key, for_update=True
                )
                if row is None or row.request_sha256 != attempt.request_sha256:
                    raise DomainError(
                        "idempotency_conflict", "Narration intent is missing or changed."
                    )
                persisted = row.payload.get("attempt")
                if isinstance(persisted, dict):
                    _assert_attempt_transition(
                        _attempt_from_json(persisted).state, attempt.state
                    )
                attempt_payload = dict(row.payload)
                attempt_payload["attempt"] = _attempt_json(attempt)
                row.payload = attempt_payload
                row.revision += 1
                session.add(
                    NarrationAttemptEventRecord(
                        owner_id=attempt.owner_id,
                        project_id=attempt.project_id,
                        idempotency_key=attempt.idempotency_key,
                        state=attempt.state.value,
                        event=event,
                        payload=_attempt_json(attempt),
                    )
                )
            if isinstance(current, dict) and current.get("state") in {"PENDING", "DISPATCHED"}:
                pending.append(_job_json(job))
                project_payload["pending_recovery_jobs"] = pending
            else:
                if isinstance(current, dict):
                    finished.setdefault(str(current["job_id"]), current)
                    project_payload["finished_jobs"] = finished
                project_payload["job"] = _job_json(job)
            project_row.payload = project_payload
            return True

    def save_blocking_reason(
        self, owner_id: UUID, project_id: UUID, reasons: dict[int, str] | None
    ) -> None:
        with self._session_factory() as session, session.begin():
            self._lock_active_project(session, owner_id, project_id)
            row = session.get(NarrationProjectRecord, project_id, with_for_update=True)
            if row is None or row.owner_id != owner_id:
                raise DomainError("not_found", "Narration project is missing.")
            payload = dict(row.payload)
            if reasons is None:
                payload.pop("blocking_reason", None)
            else:
                payload["blocking_reason"] = {
                    "sequences": {
                        str(sequence): code for sequence, code in sorted(reasons.items())
                    },
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
            row.payload = payload

    def blocking_reason(self, owner_id: UUID, project_id: UUID) -> dict[str, object] | None:
        value = self._project_value(owner_id, project_id, "blocking_reason")
        return dict(value) if isinstance(value, dict) else None

    def _attempt_row(self, owner_id: UUID, key: str) -> NarrationAttemptRecord | None:
        with self._session_factory() as session:
            return self._query_attempt(session, owner_id, key)

    @staticmethod
    def _lock_active_project(session: Session, owner_id: UUID, project_id: UUID) -> ProjectRow:
        project = (
            session.query(ProjectRow)
            .filter_by(id=project_id, owner_id=owner_id)
            .with_for_update()
            .one_or_none()
        )
        if project is None:
            raise DomainError("not_found", "Resource not found.")
        if project.production_state != "ACTIVE":
            raise DomainError("project_purging", "Project is being permanently deleted.")
        return project

    @staticmethod
    def _query_attempt(
        session: Session, owner_id: UUID, key: str, *, for_update: bool = False
    ) -> NarrationAttemptRecord | None:
        query = session.query(NarrationAttemptRecord).filter_by(
            owner_id=owner_id, idempotency_key=key
        )
        return (query.with_for_update() if for_update else query).one_or_none()

    def _project_value(self, owner_id: UUID, project_id: UUID, key: str) -> object:
        with self._session_factory() as session:
            row = session.get(NarrationProjectRecord, project_id)
            if row is None or row.owner_id != owner_id:
                return None
            return row.payload.get(key)


class SqlNarrationVideoGate:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        storage: PrivateObjectStorage,
    ) -> None:
        self._session_factory, self._storage = session_factory, storage

    def assert_project_video_allowed(self, project_id: UUID) -> None:
        with self._session_factory() as session:
            sentence_count = (
                session.query(AuthoritativeNarrationSentenceRecord)
                .filter_by(project_id=project_id, snapshot_revision=1)
                .count()
            )
            rows = (
                session.query(AuthoritativeNarrationGroupRecord)
                .filter_by(project_id=project_id, snapshot_revision=1)
                .order_by(AuthoritativeNarrationGroupRecord.sequence)
                .all()
            )
            if sentence_count == 0 or not rows:
                raise DomainError("narration_not_ready", "Measured narration is not ready.")
            authority_sentences = (
                session.query(AuthoritativeNarrationSentenceRecord)
                .filter_by(project_id=project_id, snapshot_revision=1)
                .order_by(AuthoritativeNarrationSentenceRecord.sequence)
                .all()
            )
            for item in authority_sentences:
                raw = session.get(MediaAssetRow, item.raw_asset_id)
                normalized = session.get(MediaAssetRow, item.normalized_asset_id)
                if (
                    not item.raw_qc_passed
                    or not item.normalized_qc_passed
                    or item.sample_rate_hz != 48_000
                    or item.measured_frame_count <= 0
                    or raw is None
                    or normalized is None
                    or raw.owner_id != item.owner_id
                    or normalized.owner_id != item.owner_id
                    or raw.project_id != project_id
                    or normalized.project_id != project_id
                    or raw.kind != "narration_raw"
                    or normalized.kind != "narration_normalized"
                ):
                    raise DomainError(
                        "narration_not_ready", "Narration assets or QC are incomplete."
                    )
                for asset in (raw, normalized):
                    try:
                        payload = self._storage.get(asset.object_key)
                    except Exception as error:
                        raise DomainError(
                            "narration_not_ready", "Narration asset bytes are missing."
                        ) from error
                    if (
                        len(payload) != asset.size_bytes
                        or hashlib.sha256(payload).hexdigest() != asset.sha256
                    ):
                        raise DomainError(
                            "narration_not_ready", "Narration asset integrity failed."
                        )
            if any(
                current.end_frame != following.start_frame
                for current, following in zip(
                    authority_sentences, authority_sentences[1:], strict=False
                )
            ):
                raise DomainError("narration_not_ready", "Narration timeline is not continuous.")
            groups: list[NarrationGroup] = []
            membership_count = 0
            membership_sentence_ids: set[UUID] = set()
            for row in rows:
                memberships = (
                    session.query(AuthoritativeNarrationMembershipRecord)
                    .filter_by(group_id=row.id)
                    .order_by(AuthoritativeNarrationMembershipRecord.ordinal)
                    .all()
                )
                membership_count += len(memberships)
                if len({item.ordinal for item in memberships}) != len(memberships):
                    raise DomainError("narration_not_ready", "Narration relation is ambiguous.")
                if any(item.sentence_id in membership_sentence_ids for item in memberships):
                    raise DomainError("narration_not_ready", "Narration sentence is grouped twice.")
                membership_sentence_ids.update(item.sentence_id for item in memberships)
                sentence_rows = [
                    session.get(AuthoritativeNarrationSentenceRecord, item.sentence_id)
                    for item in memberships
                ]
                if any(item is None for item in sentence_rows):
                    raise DomainError("narration_not_ready", "Narration relation is incomplete.")
                sequences = tuple(item.sequence for item in sentence_rows if item is not None)
                groups.append(
                    NarrationGroup(
                        row.sequence,
                        sequences,
                        row.start_ms,
                        row.end_ms,
                        row.end_ms - row.start_ms,
                        row.video_blocked,
                        row.block_reason,
                    )
                )
            if membership_count != sentence_count or membership_sentence_ids != {
                item.id for item in authority_sentences
            }:
                raise DomainError("narration_not_ready", "Narration membership is incomplete.")
            assert_video_generation_allowed(tuple(groups))


def _bare_provider_request_id(remote_id: str | None) -> str | None:
    """`provider_request_id` 列只存裸 request-id（`el:` 前缀属拼接链的判别标记）。"""
    if remote_id is None:
        return None
    prefix = "el:"
    return remote_id[len(prefix) :] if remote_id.startswith(prefix) else remote_id


def _attempt_json(value: NarrationAttempt) -> dict[str, object]:    return {
        "project_id": str(value.project_id),
        "owner_id": str(value.owner_id),
        "sentence_sequence": value.sentence_sequence,
        "idempotency_key": value.idempotency_key,
        "request_sha256": value.request_sha256,
        "state": value.state.value,
        "provider_call_id": value.provider_call_id,
        "remote_id": value.remote_id,
        "safe_error_code": value.safe_error_code,
        "claim_token": value.claim_token,
        "lease_until": value.lease_until,
        "parent_attempt_id": value.parent_attempt_id,
        "original_complete_manifest": list(value.original_complete_manifest),
        "original_manifest_sha256": value.original_manifest_sha256,
        "original_zip_asset_id": value.original_zip_asset_id,
        "retry_subset_manifest": list(value.retry_subset_manifest),
        "retry_subset_manifest_sha256": value.retry_subset_manifest_sha256,
        "retry_not_before": value.retry_not_before,
        "budget_round": value.budget_round,
    }


def _assert_attempt_transition(
    current: NarrationAttemptState, requested: NarrationAttemptState
) -> None:
    if current is requested:
        return
    allowed = {
        NarrationAttemptState.INTENT_RECORDED: {NarrationAttemptState.SUBMISSION_STARTED},
        NarrationAttemptState.SUBMISSION_STARTED: {
            NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            NarrationAttemptState.SAFE_TO_RETRY,
            NarrationAttemptState.PROVIDER_REJECTED,
            NarrationAttemptState.OUTPUT_INVALID,
            NarrationAttemptState.ARCHIVE_FAILED,
            NarrationAttemptState.COMPLETED,
        },
        NarrationAttemptState.REMOTE_STATE_UNKNOWN: {
            NarrationAttemptState.SAFE_TO_RETRY,
            NarrationAttemptState.PROVIDER_REJECTED,
        },
        NarrationAttemptState.OUTPUT_INVALID: {
            NarrationAttemptState.SAFE_TO_RETRY,
            NarrationAttemptState.PROVIDER_REJECTED,
            NarrationAttemptState.COMPLETED,
        },
        NarrationAttemptState.ARCHIVE_FAILED: {
            NarrationAttemptState.PROVIDER_REJECTED,
            NarrationAttemptState.COMPLETED,
        },
    }
    if requested not in allowed.get(current, set()):
        raise DomainError(
            "stale_attempt_transition",
            "A stale activity cannot overwrite the authoritative narration state.",
        )


def _assert_claim_owner(current: NarrationAttempt, requested: NarrationAttempt) -> None:
    if (
        current.state is NarrationAttemptState.SUBMISSION_STARTED
        and current.claim_token is not None
        and current.claim_token != requested.claim_token
    ):
        raise DomainError(
            "stale_submission_claim",
            "A worker that does not own the active submission lease cannot update the attempt.",
        )


def _job_from_json(value: dict[str, object]) -> NarrationJob:
    sequences = value.get("sentence_sequences")
    if not isinstance(sequences, list):
        raise ValueError("invalid narration job")
    failures = value.get("failure_sequences", [])
    if not isinstance(failures, list):
        raise ValueError("invalid narration job failures")
    return NarrationJob(
        str(value["job_id"]),
        UUID(str(value["owner_id"])),
        UUID(str(value["project_id"])),
        tuple(int(str(item)) for item in sequences),
        str(value["state"]),
        tuple(int(str(item)) for item in failures),
        not_before=str(value["not_before"]) if value.get("not_before") else None,
    )


def _asset_json(value: AudioAsset) -> dict[str, object]:
    return {
        "asset_id": value.asset_id,
        "sha256": value.sha256,
        "media_type": value.media_type,
        "size_bytes": value.size_bytes,
    }


def _asset_from_json(value: dict[str, object]) -> AudioAsset:
    return AudioAsset(
        str(value["asset_id"]),
        str(value["sha256"]),
        str(value["media_type"]),
        int(str(value["size_bytes"])),
    )


def _attempt_from_json(value: dict[str, object]) -> NarrationAttempt:
    from tubeflow_test.domain.narration import NarrationAttemptState

    original_manifest = value.get("original_complete_manifest", [])
    retry_manifest = value.get("retry_subset_manifest", [])
    if not isinstance(original_manifest, list) or not isinstance(retry_manifest, list):
        raise ValueError("invalid narration attempt manifest")
    return NarrationAttempt(
        UUID(str(value["project_id"])),
        UUID(str(value["owner_id"])),
        int(str(value["sentence_sequence"])),
        str(value["idempotency_key"]),
        str(value["request_sha256"]),
        NarrationAttemptState(str(value["state"])),
        str(value["provider_call_id"]),
        str(value["remote_id"]) if value.get("remote_id") else None,
        str(value["safe_error_code"]) if value.get("safe_error_code") else None,
        str(value["claim_token"]) if value.get("claim_token") else None,
        str(value["lease_until"]) if value.get("lease_until") else None,
        str(value["parent_attempt_id"]) if value.get("parent_attempt_id") else None,
        tuple(str(item) for item in original_manifest),
        str(value["original_manifest_sha256"]) if value.get("original_manifest_sha256") else None,
        str(value["original_zip_asset_id"]) if value.get("original_zip_asset_id") else None,
        tuple(str(item) for item in retry_manifest),
        str(value["retry_subset_manifest_sha256"])
        if value.get("retry_subset_manifest_sha256")
        else None,
        str(value["retry_not_before"]) if value.get("retry_not_before") else None,
        int(str(value.get("budget_round", 1))),
    )


def _script_json(value: ScriptVersion) -> dict[str, object]:
    return {
        "language": value.language.value,
        "rule_version": value.rule_version,
        "source_sha256": value.source_sha256,
        "sentences": [
            {"sequence": item.sequence, "text": item.text, "text_sha256": item.text_sha256}
            for item in value.sentences
        ],
    }


def _script_from_json(value: dict[str, object]) -> ScriptVersion:
    from tubeflow_test.domain.narration import ScriptSentence

    items = value["sentences"]
    if not isinstance(items, list):
        raise ValueError("invalid persisted script")
    return ScriptVersion(
        SupportedLocale(str(value["language"])),
        str(value["rule_version"]),
        str(value["source_sha256"]),
        tuple(
            ScriptSentence(int(str(item["sequence"])), str(item["text"]), str(item["text_sha256"]))
            for item in items
            if isinstance(item, dict)
        ),
    )


def _narrated_json(value: NarratedSentence) -> dict[str, object]:
    from dataclasses import asdict

    result = asdict(value)
    result["project_id"] = str(value.project_id)
    result["owner_id"] = str(value.owner_id)
    # §23.1：EL 项目没有公共音色，voice_version_id 为 None，不得写成 "None" 字面量。
    result["voice_version_id"] = (
        str(value.voice_version_id) if value.voice_version_id is not None else None
    )
    result["locale"] = value.locale.value
    return result


def _narrated_from_json(value: dict[str, object]) -> NarratedSentence:
    from tubeflow_test.domain.narration import AudioQcResult, ScriptSentence

    def asset(item: object) -> AudioAsset:
        assert isinstance(item, dict)
        return AudioAsset(
            str(item["asset_id"]),
            str(item["sha256"]),
            str(item["media_type"]),
            int(str(item["size_bytes"])),
        )

    def qc(item: object) -> AudioQcResult:
        assert isinstance(item, dict)
        return AudioQcResult(**item)

    sentence = value["sentence"]
    assert isinstance(sentence, dict)
    manifest = value["normalization_manifest"]
    assert isinstance(manifest, dict)
    raw_voice_version_id = value.get("voice_version_id")
    provider_request_id = value.get("provider_request_id")
    return NarratedSentence(
        UUID(str(value["project_id"])),
        UUID(str(value["owner_id"])),
        UUID(str(raw_voice_version_id)) if raw_voice_version_id else None,
        SupportedLocale(str(value["locale"])),
        str(value["provider_call_id"]),
        str(value["reference_package_sha256"]),
        ScriptSentence(
            int(str(sentence["sequence"])), str(sentence["text"]), str(sentence["text_sha256"])
        ),
        str(value["idempotency_key"]),
        asset(value["raw_asset"]),
        asset(value["normalized_asset"]),
        qc(value["raw_qc"]),
        qc(value["normalized_qc"]),
        dict(manifest),
        provider_request_id=(
            str(provider_request_id) if provider_request_id is not None else None
        ),
    )


def _timeline_json(value: TimelineEntry) -> dict[str, object]:
    return {
        "sentence_sequence": value.sentence_sequence,
        "start_ms": value.start_ms,
        "end_ms": value.end_ms,
        "audio_asset_id": value.audio_asset_id,
        "start_frame": value.start_frame,
        "end_frame": value.end_frame,
        "sample_rate_hz": value.sample_rate_hz,
    }


def _timeline_from_json(value: object) -> TimelineEntry:
    assert isinstance(value, dict)
    return TimelineEntry(
        int(str(value["sentence_sequence"])),
        int(str(value["start_ms"])),
        int(str(value["end_ms"])),
        str(value["audio_asset_id"]),
        int(str(value["start_frame"])),
        int(str(value["end_frame"])),
        int(str(value["sample_rate_hz"])),
    )


def _group_json(value: NarrationGroup) -> dict[str, object]:
    return {
        "sequence": value.sequence,
        "sentence_sequences": list(value.sentence_sequences),
        "start_ms": value.start_ms,
        "end_ms": value.end_ms,
        "target_duration_ms": value.target_duration_ms,
        "video_blocked": value.video_blocked,
        "block_reason": value.block_reason,
    }


def _group_from_json(value: object) -> NarrationGroup:
    assert isinstance(value, dict)
    sequences = value["sentence_sequences"]
    assert isinstance(sequences, list)
    return NarrationGroup(
        int(str(value["sequence"])),
        tuple(int(str(item)) for item in sequences),
        int(str(value["start_ms"])),
        int(str(value["end_ms"])),
        int(str(value["target_duration_ms"])),
        bool(value["video_blocked"]),
        str(value["block_reason"]) if value.get("block_reason") else None,
    )


class SqlStoryboardNarrationAuthority:
    """Concrete adapter from canonical narration rows to storyboard's read contract."""

    def __init__(
        self, session_factory: Callable[[], Session], storage: PrivateObjectStorage
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage

    def current_group(self, owner_id: UUID, group_id: UUID) -> AuthoritativeNarrationGroup | None:
        first = self._load_group_snapshot(owner_id, group_id)
        if first is None:
            return None
        group, audio_objects = first
        try:
            for _asset_id, object_key, sha256, size_bytes in audio_objects:
                self._storage.get_verified(object_key, sha256, size_bytes)
        except Exception as error:
            raise DomainError(
                "narration_not_ready", "Authoritative narration audio is unavailable."
            ) from error
        if self._load_group_snapshot(owner_id, group_id) != first:
            raise DomainError(
                "narration_not_ready", "Authoritative narration changed during verification."
            )
        return group

    def _load_group_snapshot(
        self, owner_id: UUID, group_id: UUID
    ) -> tuple[AuthoritativeNarrationGroup, tuple[tuple[UUID, str, str, int], ...]] | None:
        from tubeflow_test.application.narration_authority import (
            AuthoritativeNarrationGroup,
            AuthoritativeNarrationSentence,
        )

        with self._session_factory() as session:
            group = session.get(AuthoritativeNarrationGroupRecord, group_id)
            if group is None or group.owner_id != owner_id:
                return None
            memberships = (
                session.query(AuthoritativeNarrationMembershipRecord)
                .filter_by(group_id=group_id)
                .order_by(AuthoritativeNarrationMembershipRecord.ordinal)
                .all()
            )
            sentences: list[AuthoritativeNarrationSentence] = []
            audio_objects: list[tuple[UUID, str, str, int]] = []
            asset_ids: set[UUID] = set()
            for membership in memberships:
                row = session.get(AuthoritativeNarrationSentenceRecord, membership.sentence_id)
                if (
                    row is None
                    or row.owner_id != owner_id
                    or row.project_id != group.project_id
                    or row.snapshot_revision != group.snapshot_revision
                    or row.snapshot_sha256 != group.snapshot_sha256
                ):
                    raise DomainError("narration_not_ready", "Narration membership is incomplete.")
                sentence_asset_ids = {row.raw_asset_id, row.normalized_asset_id}
                if len(sentence_asset_ids) != 2 or asset_ids.intersection(sentence_asset_ids):
                    raise DomainError(
                        "narration_not_ready", "Narration assets must be unique per sentence."
                    )
                asset_ids.update(sentence_asset_ids)
                raw = session.get(MediaAssetRow, row.raw_asset_id)
                normalized = session.get(MediaAssetRow, row.normalized_asset_id)
                if not _valid_storyboard_narration_asset(
                    raw, owner_id, group.project_id, row.raw_asset_id, "narration_raw"
                ) or not _valid_storyboard_narration_asset(
                    normalized,
                    owner_id,
                    group.project_id,
                    row.normalized_asset_id,
                    "narration_normalized",
                ):
                    raise DomainError("narration_not_ready", "Narration assets are missing.")
                assert raw is not None and normalized is not None
                sentences.append(
                    AuthoritativeNarrationSentence(
                        row.id,
                        membership.ordinal,
                        row.text,
                        row.text_sha256,
                        row.raw_asset_id,
                        row.normalized_asset_id,
                        raw.sha256,
                        normalized.sha256,
                        row.raw_qc_passed and row.normalized_qc_passed,
                        row.measured_frame_count,
                        row.sample_rate_hz,
                        row.start_ms,
                        row.end_ms,
                        row.start_frame,
                        row.end_frame,
                        raw.size_bytes,
                        normalized.size_bytes,
                    )
                )
                audio_objects.extend(
                    (
                        (raw.id, raw.object_key, raw.sha256, raw.size_bytes),
                        (
                            normalized.id,
                            normalized.object_key,
                            normalized.sha256,
                            normalized.size_bytes,
                        ),
                    )
                )
            authority = AuthoritativeNarrationGroup(
                group.id,
                group.owner_id,
                group.project_id,
                group.snapshot_sha256,
                group.snapshot_revision,
                group.start_ms,
                group.end_ms,
                group.video_blocked,
                tuple(sentences),
            )
            return authority, tuple(audio_objects)


def _valid_storyboard_narration_asset(
    asset: MediaAssetRow | None,
    owner_id: UUID,
    project_id: UUID,
    asset_id: UUID,
    kind: str,
) -> bool:
    return bool(
        asset is not None
        and asset.id == asset_id
        and asset.owner_id == owner_id
        and asset.project_id == project_id
        and asset.ownership_scope == "PROJECT"
        and asset.deletion_state == "ACTIVE"
        and not asset.purging
        and asset.kind == kind
        and asset.media_type == "audio/wav"
        and len(asset.sha256) == 64
        and asset.size_bytes > 0
        and asset.object_key
    )


def _reconciliation_replayed(current: NarrationAttempt, requested: NarrationAttempt) -> bool:
    if current.safe_error_code and current.safe_error_code.startswith("manual:"):
        if (
            current.safe_error_code == requested.safe_error_code
            and current.state is requested.state
        ):
            return True
        raise DomainError("idempotency_conflict", "This attempt already has a disposition.")
    return False


def _job_json(job: NarrationJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "owner_id": str(job.owner_id),
        "project_id": str(job.project_id),
        "sentence_sequences": list(job.sentence_sequences),
        "state": job.state,
        "failure_sequences": list(job.failure_sequences),
        "not_before": job.not_before,
    }
