from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from fractions import Fraction
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.models import (
    ProjectRow,
    ProjectSnapshotCharacterRow,
)
from tubeflow_test.adapters.persistence.models import (
    MediaAssetRow,
    ProjectAssetReferenceRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.application.narration_authority import (
    AuthoritativeNarrationGroup,
    AuthoritativeNarrationSentence,
    NarrationAuthorityPort,
)
from tubeflow_test.application.storyboard_lifecycle import (
    StoryboardDeletionObject,
    StoryboardProjectAssets,
)
from tubeflow_test.application.storyboard_outbox import StoryboardOutboxEvent
from tubeflow_test.application.storyboards import StoryboardContext, StoryboardRecoveryCandidate
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.director_generation import (
    DirectorAttemptStatus,
    DirectorGenerationAttempt,
    DirectorInputSnapshot,
    DirectorSentenceSnapshot,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import frozen_script_sha256
from tubeflow_test.domain.storyboards import (
    STORYBOARD_AUTO_MAX_SUBMISSIONS,
    CharacterAssetSnapshot,
    ContinuousObservationApproval,
    DirectorLocationRelation,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
    GenerationStatus,
    ReviewState,
    ShotSentenceLink,
    ShotTimeRange,
    StoryboardAsset,
    StoryboardAssetKind,
    StoryboardGeneration,
    VideoStoryboardBinding,
)
from tubeflow_test.video_generation.persistence import VideoAttemptRecord

from .models import (
    DirectorGenerationAttemptRow,
    DirectorPointerRow,
    DirectorRevisionRow,
    StoryboardCurrentRow,
    StoryboardGenerationRow,
    StoryboardMediaRow,
    StoryboardOutboxRow,
    VideoStoryboardBindingRow,
)
from .project_director_models import (
    DirectorRevisionLineageRow,
    ProjectDirectorCurrentRow,
    ProjectDirectorGroupCandidateRow,
    ProjectDirectorMaterializationRow,
)


class SqlAlchemyStoryboardRepository:
    """PostgreSQL repository; each public mutation is one transaction/UoW."""

    def __init__(
        self,
        unit_of_work: Callable[[], AbstractContextManager[Session]],
        narration_authority: NarrationAuthorityPort | None = None,
    ) -> None:
        self._uow = unit_of_work
        self._narration_authority = narration_authority

    def context(self, owner_id: UUID, group_id: UUID) -> StoryboardContext | None:
        if self._narration_authority is None:
            raise RuntimeError("NarrationAuthorityPort is required for storyboard context")
        group = self._narration_authority.current_group(owner_id, group_id)
        if group is None:
            return None
        with self._uow() as session:
            project = session.scalar(
                select(ProjectRow).where(
                    ProjectRow.id == group.project_id,
                    ProjectRow.owner_id == owner_id,
                    ProjectRow.production_state == "ACTIVE",
                )
            )
            if project is None:
                return None
            sentences = group.sentences
            sentence_ids = tuple(item.sentence_id for item in sentences)
            media_ids = {item.raw_asset_id for item in sentences} | {
                item.normalized_asset_id for item in sentences
            }
            media = {
                item.id: item
                for item in session.scalars(
                    select(MediaAssetRow).where(
                        MediaAssetRow.id.in_(media_ids),
                        MediaAssetRow.owner_id == owner_id,
                        MediaAssetRow.project_id == project.id,
                    )
                ).all()
            }
            ready = _authoritative_narration_ready(project, group, media)
            character_rows = session.scalars(
                select(ProjectSnapshotCharacterRow)
                .where(ProjectSnapshotCharacterRow.project_id == project.id)
                .order_by(ProjectSnapshotCharacterRow.ordinal)
            ).all()
            characters = tuple(
                CharacterAssetSnapshot(
                    item.ordinal + 1,
                    item.character_image_id,
                    item.asset_id,
                    item.sha256,
                    item.media_type,
                    item.size_bytes,
                )
                for item in character_rows
            )
            if project.snapshot_confirmed_at is None or project.snapshot_visual_style is None:
                ready = False
            sentence_snapshots = [
                DirectorSentenceSnapshot(
                    item.sentence_id,
                    item.ordinal,
                    item.text,
                    item.text_sha256,
                    item.start_ms,
                    item.end_ms,
                )
                for item in sentences
            ]
            return StoryboardContext(
                owner_id,
                project.id,
                group.group_id,
                sentence_ids,
                ready,
                characters,
                project.snapshot_visual_style or "",
                tuple(sentence_snapshots),
                project.snapshot_locale or "",
                project.snapshot_aspect_ratio or "",
            )

    def activate_storyboards(self, owner_id: UUID, project_id: UUID) -> None:
        with self._uow() as session:
            project = session.scalar(
                select(ProjectRow).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            if project is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if project.production_state != "ACTIVE":
                raise DomainError("project_purging", "Project is being purged")

    def begin_storyboard_purge(self, owner_id: UUID, project_id: UUID) -> None:
        with self._uow() as session:
            state = session.scalar(
                select(ProjectRow.production_state)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .with_for_update()
            )
            if state is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if state != "ACTIVE":
                raise DomainError("project_purging", "Project is being purged")
            if _has_active_storyboard_jobs(session, owner_id, project_id):
                raise DomainError("active_jobs", "Storyboard jobs are still active")
            session.execute(
                update(ProjectRow)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .values(production_state="PURGING")
            )
            session.execute(
                update(StoryboardOutboxRow)
                .where(
                    StoryboardOutboxRow.project_id == project_id,
                    StoryboardOutboxRow.owner_id == owner_id,
                    StoryboardOutboxRow.acknowledged_at.is_(None),
                )
                .values(state="FROZEN", lease_token=None, lease_until=None)
            )

    def has_active_storyboard_jobs(self, owner_id: UUID, project_id: UUID) -> bool:
        with self._uow() as session:
            return _has_active_storyboard_jobs(session, owner_id, project_id)

    def enumerate_storyboard_assets(
        self, owner_id: UUID, project_id: UUID
    ) -> StoryboardProjectAssets:
        with self._uow() as session:
            rows = session.scalars(
                select(MediaAssetRow)
                .join(
                    ProjectAssetReferenceRow,
                    ProjectAssetReferenceRow.asset_id == MediaAssetRow.id,
                )
                .where(
                    ProjectAssetReferenceRow.owner_id == owner_id,
                    ProjectAssetReferenceRow.project_id == project_id,
                )
            ).all()
            unique = {
                item.id: MediaAssetRef(item.id, item.sha256, item.media_type, item.size_bytes)
                for item in rows
            }
            return StoryboardProjectAssets(project_id, tuple(unique.values()))

    def detach_assets_for_purge_in_transaction(
        self, transaction: object, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardDeletionObject, ...]:
        if not isinstance(transaction, Session):
            raise TypeError("Project deletion transaction must be a SQLAlchemy Session")
        session = transaction
        project = session.scalar(
            select(ProjectRow)
            .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            .with_for_update()
        )
        if project is None:
            raise DomainError("not_found", "Storyboard resource was not found")
        if project.production_state != "PURGING":
            raise DomainError("revision_conflict", "Project is not being purged")
        rows = list(
            session.scalars(
                select(MediaAssetRow)
                .join(ProjectAssetReferenceRow)
                .where(ProjectAssetReferenceRow.project_id == project_id)
                .with_for_update()
            ).all()
        )
        staged_asset_ids = session.scalars(
            select(StagedObjectRow.asset_id).where(
                StagedObjectRow.owner_id == owner_id,
                StagedObjectRow.project_id == project_id,
                StagedObjectRow.state.in_(("PENDING_UPLOAD", "STAGED", "ATTACHED")),
            )
        ).all()
        known = {row.id for row in rows}
        for asset_id in staged_asset_ids:
            if asset_id not in known:
                media = session.scalar(
                    select(MediaAssetRow)
                    .where(MediaAssetRow.id == asset_id, MediaAssetRow.owner_id == owner_id)
                    .with_for_update()
                )
                if media is not None:
                    rows.append(media)
                    known.add(media.id)
        result: list[StoryboardDeletionObject] = []
        for media in rows:
            session.execute(
                delete(ProjectAssetReferenceRow).where(
                    ProjectAssetReferenceRow.project_id == project_id,
                    ProjectAssetReferenceRow.asset_id == media.id,
                )
            )
            session.flush()
            remaining = (
                session.scalar(
                    select(func.count())
                    .select_from(ProjectAssetReferenceRow)
                    .where(ProjectAssetReferenceRow.asset_id == media.id)
                )
                or 0
            )
            deletion_token = None
            if remaining == 0:
                deletion_token = uuid4()
                media.deletion_state = "DELETING"
                media.deletion_token = deletion_token
            result.append(
                StoryboardDeletionObject(
                    MediaAssetRef(media.id, media.sha256, media.media_type, media.size_bytes),
                    media.object_key,
                    remaining == 0,
                    deletion_token,
                )
            )
        return tuple(result)

    def finalize_asset_deletion(self, asset_id: UUID, deletion_token: UUID) -> bool:
        with self._uow() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    delete(MediaAssetRow).where(
                        MediaAssetRow.id == asset_id,
                        MediaAssetRow.deletion_state == "DELETING",
                        MediaAssetRow.deletion_token == deletion_token,
                        ~select(ProjectAssetReferenceRow.asset_id)
                        .where(ProjectAssetReferenceRow.asset_id == asset_id)
                        .exists(),
                    )
                ),
            )
            return result.rowcount == 1

    def get_applied_revision(self, owner_id: UUID, group_id: UUID) -> DirectorPlanRevision | None:
        return self._revision_from_pointer(owner_id, group_id, "applied_revision_id")

    def get_draft_revision(self, owner_id: UUID, group_id: UUID) -> DirectorPlanRevision | None:
        return self._revision_from_pointer(owner_id, group_id, "draft_revision_id")

    def find_by_key(self, owner_id: UUID, key: str) -> DirectorGenerationAttempt | None:
        with self._uow() as session:
            row = session.scalar(
                select(DirectorGenerationAttemptRow).where(
                    DirectorGenerationAttemptRow.owner_id == owner_id,
                    DirectorGenerationAttemptRow.idempotency_key == key,
                )
            )
            return _director_attempt(row) if row else None

    def find_unresolved(
        self, owner_id: UUID, group_id: UUID, canonical_hash: str
    ) -> DirectorGenerationAttempt | None:
        with self._uow() as session:
            row = session.scalar(
                select(DirectorGenerationAttemptRow).where(
                    DirectorGenerationAttemptRow.owner_id == owner_id,
                    DirectorGenerationAttemptRow.group_id == group_id,
                    DirectorGenerationAttemptRow.canonical_request_sha256 == canonical_hash,
                    or_(
                        DirectorGenerationAttemptRow.status
                        == DirectorAttemptStatus.REMOTE_STATE_UNKNOWN.value,
                        DirectorGenerationAttemptRow.failure_code.like("manual_confirmed_failed:%"),
                    ),
                )
            )
            return _director_attempt(row) if row else None

    def claim_outbox(
        self, now: datetime, lease_until: datetime, token: UUID
    ) -> StoryboardOutboxEvent | None:
        with self._uow() as session:
            row = session.scalar(
                select(StoryboardOutboxRow)
                .where(
                    StoryboardOutboxRow.acknowledged_at.is_(None),
                    StoryboardOutboxRow.next_attempt_at <= now,
                    StoryboardOutboxRow.project_id.in_(
                        select(ProjectRow.id).where(ProjectRow.production_state == "ACTIVE")
                    ),
                    or_(
                        StoryboardOutboxRow.lease_until.is_(None),
                        StoryboardOutboxRow.lease_until < now,
                    ),
                )
                .order_by(StoryboardOutboxRow.created_at, StoryboardOutboxRow.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            _lock_active_project(session, row.owner_id, row.project_id)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardOutboxRow)
                    .where(
                        StoryboardOutboxRow.id == row.id,
                        or_(
                            StoryboardOutboxRow.lease_until.is_(None),
                            StoryboardOutboxRow.lease_until < now,
                        ),
                        StoryboardOutboxRow.acknowledged_at.is_(None),
                    )
                    .values(
                        state="CLAIMED",
                        lease_token=token,
                        lease_until=lease_until,
                        attempt_count=StoryboardOutboxRow.attempt_count + 1,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            session.flush()
            session.refresh(row)
            return StoryboardOutboxEvent(
                row.id,
                row.owner_id,
                row.project_id,
                row.aggregate_id,
                row.event_type,
                row.request_sha256,
                row.workflow_id,
                token,
                row.attempt_count,
            )

    def acknowledge_outbox(self, event_id: UUID, token: UUID, now: datetime) -> bool:
        with self._uow() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardOutboxRow)
                    .where(
                        StoryboardOutboxRow.id == event_id,
                        StoryboardOutboxRow.lease_token == token,
                        StoryboardOutboxRow.acknowledged_at.is_(None),
                    )
                    .values(
                        state="ACKED",
                        acknowledged_at=now,
                        lease_token=None,
                        lease_until=None,
                        last_error=None,
                    )
                ),
            )
            return result.rowcount == 1

    def retry_outbox(self, event_id: UUID, token: UUID, next_at: datetime, error: str) -> bool:
        with self._uow() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardOutboxRow)
                    .where(
                        StoryboardOutboxRow.id == event_id,
                        StoryboardOutboxRow.lease_token == token,
                        StoryboardOutboxRow.acknowledged_at.is_(None),
                    )
                    .values(
                        state="PENDING",
                        next_attempt_at=next_at,
                        lease_token=None,
                        lease_until=None,
                        last_error=error[:256],
                    )
                ),
            )
            return result.rowcount == 1

    def create(self, attempt: DirectorGenerationAttempt) -> DirectorGenerationAttempt:
        try:
            with self._uow() as session:
                _lock_active_project(session, attempt.owner_id, attempt.input_snapshot.project_id)
                session.execute(
                    select(DirectorPointerRow)
                    .where(
                        DirectorPointerRow.owner_id == attempt.owner_id,
                        DirectorPointerRow.group_id == attempt.group_id,
                    )
                    .with_for_update()
                ).one_or_none()
                unresolved = session.scalar(
                    select(DirectorGenerationAttemptRow.id).where(
                        DirectorGenerationAttemptRow.owner_id == attempt.owner_id,
                        DirectorGenerationAttemptRow.group_id == attempt.group_id,
                        DirectorGenerationAttemptRow.canonical_request_sha256
                        == attempt.canonical_request_sha256,
                        DirectorGenerationAttemptRow.status.not_in(
                            (
                                DirectorAttemptStatus.SUCCEEDED.value,
                                DirectorAttemptStatus.FAILED.value,
                            )
                        ),
                    )
                )
                if unresolved is not None:
                    raise DomainError(
                        "remote_state_unresolved", "Equivalent director request is unresolved"
                    )
                session.add(_director_attempt_row(attempt))
                session.add(
                    StoryboardOutboxRow(
                        id=attempt.attempt_id,
                        owner_id=attempt.owner_id,
                        project_id=attempt.input_snapshot.project_id,
                        aggregate_type="DIRECTOR",
                        aggregate_id=attempt.attempt_id,
                        event_type="DIRECTOR_GENERATION_REQUESTED",
                        request_sha256=attempt.canonical_request_sha256,
                        workflow_id=f"storyboard-director-{attempt.owner_id}-{attempt.attempt_id}",
                        state="PENDING",
                        next_attempt_at=attempt.created_at,
                        created_at=attempt.created_at,
                    )
                )
            return attempt
        except IntegrityError as exc:
            existing = self.find_by_key(attempt.owner_id, attempt.idempotency_key)
            if existing and existing.canonical_request_sha256 == attempt.canonical_request_sha256:
                return existing
            raise DomainError(
                "idempotency_conflict", "Idempotency key is bound to another request"
            ) from exc

    def get(self, owner_id: UUID, attempt_id: UUID) -> DirectorGenerationAttempt | None:
        with self._uow() as session:
            row = session.scalar(
                select(DirectorGenerationAttemptRow).where(
                    DirectorGenerationAttemptRow.owner_id == owner_id,
                    DirectorGenerationAttemptRow.id == attempt_id,
                )
            )
            return _director_attempt(row) if row else None

    def latest_for_group(self, owner_id: UUID, group_id: UUID) -> DirectorGenerationAttempt | None:
        with self._uow() as session:
            row = session.scalar(
                select(DirectorGenerationAttemptRow)
                .where(
                    DirectorGenerationAttemptRow.owner_id == owner_id,
                    DirectorGenerationAttemptRow.group_id == group_id,
                )
                .order_by(
                    DirectorGenerationAttemptRow.created_at.desc(),
                    DirectorGenerationAttemptRow.id.desc(),
                )
                .limit(1)
            )
            return _director_attempt(row) if row else None

    def authorize_director_recovery(
        self, owner_id: UUID, attempt_id: UUID, event_id: UUID, now: datetime
    ) -> DirectorGenerationAttempt:
        with self._uow() as session:
            row = session.scalar(
                select(DirectorGenerationAttemptRow)
                .where(
                    DirectorGenerationAttemptRow.id == attempt_id,
                    DirectorGenerationAttemptRow.owner_id == owner_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Director attempt was not found")
            project_id = UUID(cast(str, row.input_json["project_id"]))
            _lock_active_project(session, owner_id, project_id)
            if row.status != DirectorAttemptStatus.REMOTE_STATE_UNKNOWN.value:
                raise DomainError("revision_conflict", "Director attempt is not unresolved")
            session.add(
                StoryboardOutboxRow(
                    id=event_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    aggregate_type="DIRECTOR_RECOVERY",
                    aggregate_id=attempt_id,
                    event_type="DIRECTOR_RECOVERY_REQUESTED",
                    request_sha256=row.canonical_request_sha256,
                    workflow_id=f"storyboard-director-recovery-{owner_id}-{event_id}",
                    state="PENDING",
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            return _director_attempt(row)

    def claim(
        self, owner_id: UUID, attempt_id: UUID, token: UUID, now: datetime, lease_until: datetime
    ) -> DirectorGenerationAttempt | None:
        with self._uow() as session:
            candidate = session.scalar(
                select(DirectorGenerationAttemptRow).where(
                    DirectorGenerationAttemptRow.owner_id == owner_id,
                    DirectorGenerationAttemptRow.id == attempt_id,
                )
            )
            if candidate is None:
                return None
            project_id = UUID(cast(str, candidate.input_json["project_id"]))
            try:
                _lock_active_project(session, owner_id, project_id)
            except DomainError:
                return None
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(DirectorGenerationAttemptRow)
                    .where(
                        DirectorGenerationAttemptRow.owner_id == owner_id,
                        DirectorGenerationAttemptRow.id == attempt_id,
                        or_(
                            DirectorGenerationAttemptRow.status
                            == DirectorAttemptStatus.INTENT_PERSISTED.value,
                            (
                                DirectorGenerationAttemptRow.status
                                == DirectorAttemptStatus.LEASED.value
                            )
                            & (DirectorGenerationAttemptRow.lease_until < now),
                        ),
                    )
                    .values(
                        status=DirectorAttemptStatus.LEASED.value,
                        state_revision=DirectorGenerationAttemptRow.state_revision + 1,
                        lease_token=token,
                        lease_until=lease_until,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            return _director_attempt(session.get_one(DirectorGenerationAttemptRow, attempt_id))

    def mark_call_started(
        self, attempt: DirectorGenerationAttempt
    ) -> DirectorGenerationAttempt | None:
        if attempt.lease_token is None:
            return None
        with self._uow() as session:
            _lock_active_project(session, attempt.owner_id, attempt.input_snapshot.project_id)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(DirectorGenerationAttemptRow)
                    .where(
                        DirectorGenerationAttemptRow.owner_id == attempt.owner_id,
                        DirectorGenerationAttemptRow.id == attempt.attempt_id,
                        DirectorGenerationAttemptRow.status == DirectorAttemptStatus.LEASED.value,
                        DirectorGenerationAttemptRow.state_revision == attempt.state_revision,
                        DirectorGenerationAttemptRow.lease_token == attempt.lease_token,
                    )
                    .values(
                        status=DirectorAttemptStatus.SUBMITTING.value,
                        state_revision=attempt.state_revision + 1,
                        lease_token=None,
                        lease_until=None,
                        history_json=[*attempt.history, DirectorAttemptStatus.SUBMITTING.value],
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            return _director_attempt(
                session.get_one(DirectorGenerationAttemptRow, attempt.attempt_id)
            )

    def save(self, attempt: DirectorGenerationAttempt) -> DirectorGenerationAttempt:
        with self._uow() as session:
            _lock_active_project(session, attempt.owner_id, attempt.input_snapshot.project_id)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(DirectorGenerationAttemptRow)
                    .where(
                        DirectorGenerationAttemptRow.owner_id == attempt.owner_id,
                        DirectorGenerationAttemptRow.id == attempt.attempt_id,
                        DirectorGenerationAttemptRow.state_revision == attempt.state_revision,
                        DirectorGenerationAttemptRow.status.in_(
                            (
                                DirectorAttemptStatus.SUBMITTING.value,
                                DirectorAttemptStatus.REMOTE_STATE_UNKNOWN.value,
                            )
                        ),
                    )
                    .values(
                        status=attempt.status.value,
                        state_revision=attempt.state_revision + 1,
                        provider_receipt=attempt.provider_receipt,
                        raw_response_sha256=attempt.raw_response_sha256,
                        draft_revision_id=attempt.draft_revision_id,
                        failure_code=attempt.failure_code,
                        history_json=list(attempt.history),
                    )
                ),
            )
            if result.rowcount != 1:
                row = session.get_one(DirectorGenerationAttemptRow, attempt.attempt_id)
                return _director_attempt(row)
            return replace(attempt, state_revision=attempt.state_revision + 1)

    def complete_with_draft(
        self, attempt: DirectorGenerationAttempt, draft: DirectorPlanRevision
    ) -> DirectorGenerationAttempt:
        with self._uow() as session:
            _lock_active_project(session, attempt.owner_id, attempt.input_snapshot.project_id)
            pointer = session.get(
                DirectorPointerRow, (attempt.owner_id, attempt.group_id), with_for_update=True
            )
            if pointer is None:
                pointer = DirectorPointerRow(
                    owner_id=attempt.owner_id,
                    group_id=attempt.group_id,
                    draft_revision_id=None,
                    applied_revision_id=None,
                )
                session.add(pointer)
                session.flush()
            if pointer.draft_revision_id != attempt.input_snapshot.expected_draft_revision_id:
                raise DomainError("revision_conflict", "Draft revision changed")
            number = (
                cast(
                    int,
                    session.scalar(
                        select(
                            func.coalesce(func.max(DirectorRevisionRow.revision_number), 0)
                        ).where(
                            DirectorRevisionRow.owner_id == attempt.owner_id,
                            DirectorRevisionRow.group_id == attempt.group_id,
                        )
                    ),
                )
                + 1
            )
            stored_draft = replace(draft, revision_number=number)
            session.add(_revision_row(stored_draft))
            pointer.draft_revision_id = stored_draft.revision_id
            completed = replace(
                attempt,
                status=DirectorAttemptStatus.SUCCEEDED,
                draft_revision_id=stored_draft.revision_id,
                history=(*attempt.history, DirectorAttemptStatus.SUCCEEDED.value),
            )
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(DirectorGenerationAttemptRow)
                    .where(
                        DirectorGenerationAttemptRow.owner_id == attempt.owner_id,
                        DirectorGenerationAttemptRow.id == attempt.attempt_id,
                        DirectorGenerationAttemptRow.status
                        == DirectorAttemptStatus.SUBMITTING.value,
                        DirectorGenerationAttemptRow.state_revision == attempt.state_revision,
                    )
                    .values(
                        status=completed.status.value,
                        state_revision=attempt.state_revision + 1,
                        provider_receipt=completed.provider_receipt,
                        raw_response_sha256=completed.raw_response_sha256,
                        draft_revision_id=completed.draft_revision_id,
                        history_json=list(completed.history),
                    )
                ),
            )
            if result.rowcount != 1:
                raise DomainError("revision_conflict", "Director attempt changed")
            return replace(completed, state_revision=attempt.state_revision + 1)

    def _revision_from_pointer(
        self, owner_id: UUID, group_id: UUID, field: str
    ) -> DirectorPlanRevision | None:
        with self._uow() as session:
            pointer = session.get(DirectorPointerRow, (owner_id, group_id))
            revision_id = getattr(pointer, field) if pointer else None
            row = session.get(DirectorRevisionRow, revision_id) if revision_id else None
            return _revision(row) if row else None

    def create_revision(
        self,
        revision: DirectorPlanRevision,
        expected_draft_revision_id: UUID | None,
        source_candidate_id: UUID | None = None,
    ) -> DirectorPlanRevision:
        try:
            with self._uow() as session:
                _lock_active_project(session, revision.owner_id, revision.project_id)
                pointer = session.get(
                    DirectorPointerRow, (revision.owner_id, revision.group_id), with_for_update=True
                )
                if pointer is None:
                    pointer = DirectorPointerRow(
                        owner_id=revision.owner_id,
                        group_id=revision.group_id,
                        draft_revision_id=None,
                        applied_revision_id=None,
                    )
                    session.add(pointer)
                    session.flush()
                if pointer.draft_revision_id != expected_draft_revision_id:
                    raise DomainError("revision_conflict", "Draft revision changed")
                source_lineage = (
                    session.get(DirectorRevisionLineageRow, expected_draft_revision_id)
                    if expected_draft_revision_id is not None
                    else None
                )
                if source_lineage is not None and source_candidate_id not in {
                    None,
                    source_lineage.candidate_id,
                }:
                    raise DomainError(
                        "revision_conflict",
                        "Draft source candidate changed",
                    )
                source_candidate = None
                source_materialization = None
                if source_lineage is None and source_candidate_id is not None:
                    if expected_draft_revision_id is not None:
                        raise DomainError(
                            "revision_conflict",
                            "Existing draft has no matching project director source",
                        )
                    source_candidate = session.scalar(
                        select(ProjectDirectorGroupCandidateRow).where(
                            ProjectDirectorGroupCandidateRow.id == source_candidate_id,
                            ProjectDirectorGroupCandidateRow.owner_id == revision.owner_id,
                            ProjectDirectorGroupCandidateRow.project_id == revision.project_id,
                            ProjectDirectorGroupCandidateRow.group_id == revision.group_id,
                        )
                    )
                    if source_candidate is None:
                        raise DomainError(
                            "invalid_project_director_candidate",
                            "Project director candidate is unavailable",
                        )
                    current_preplan_revision_id = session.scalar(
                        select(ProjectDirectorCurrentRow.revision_id).where(
                            ProjectDirectorCurrentRow.owner_id == revision.owner_id,
                            ProjectDirectorCurrentRow.project_id == revision.project_id,
                        )
                    )
                    source_materialization = session.scalar(
                        select(ProjectDirectorMaterializationRow)
                        .where(
                            ProjectDirectorMaterializationRow.owner_id == revision.owner_id,
                            ProjectDirectorMaterializationRow.project_id == revision.project_id,
                            ProjectDirectorMaterializationRow.preplan_revision_id
                            == current_preplan_revision_id,
                        )
                        .order_by(
                            ProjectDirectorMaterializationRow.created_at.desc(),
                            ProjectDirectorMaterializationRow.id.desc(),
                        )
                        .limit(1)
                    )
                    if (
                        current_preplan_revision_id is None
                        or source_materialization is None
                        or source_candidate.materialization_id != source_materialization.id
                    ):
                        raise DomainError(
                            "revision_conflict",
                            "Project director candidate is not current",
                        )
                number = (
                    cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(func.max(DirectorRevisionRow.revision_number), 0)
                            ).where(
                                DirectorRevisionRow.owner_id == revision.owner_id,
                                DirectorRevisionRow.group_id == revision.group_id,
                            )
                        ),
                    )
                    + 1
                )
                stored = replace(revision, revision_number=number)
                session.add(_revision_row(stored))
                session.flush()
                if source_lineage is not None:
                    session.add(
                        DirectorRevisionLineageRow(
                            revision_id=stored.revision_id,
                            project_director_revision_id=(
                                source_lineage.project_director_revision_id
                            ),
                            materialization_id=source_lineage.materialization_id,
                            candidate_id=source_lineage.candidate_id,
                        )
                    )
                elif source_candidate is not None and source_materialization is not None:
                    session.add(
                        DirectorRevisionLineageRow(
                            revision_id=stored.revision_id,
                            project_director_revision_id=(
                                source_materialization.preplan_revision_id
                            ),
                            materialization_id=source_materialization.id,
                            candidate_id=source_candidate.id,
                        )
                    )
                pointer.draft_revision_id = stored.revision_id
                return stored
        except IntegrityError as exc:
            raise DomainError(
                "revision_conflict", "Concurrent revision allocation conflict"
            ) from exc

    def apply_revision(
        self, owner_id: UUID, group_id: UUID, revision_id: UUID, expected_draft_revision_id: UUID
    ) -> DirectorPlanRevision:
        with self._uow() as session:
            revision_project_id = session.scalar(
                select(DirectorRevisionRow.project_id).where(
                    DirectorRevisionRow.id == revision_id,
                    DirectorRevisionRow.owner_id == owner_id,
                    DirectorRevisionRow.group_id == group_id,
                )
            )
            if revision_project_id is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            _lock_active_project(session, owner_id, revision_project_id)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(DirectorPointerRow)
                    .where(
                        DirectorPointerRow.owner_id == owner_id,
                        DirectorPointerRow.group_id == group_id,
                        DirectorPointerRow.draft_revision_id == expected_draft_revision_id,
                    )
                    .values(draft_revision_id=None, applied_revision_id=revision_id)
                ),
            )
            if result.rowcount != 1 or revision_id != expected_draft_revision_id:
                raise DomainError("revision_conflict", "Draft revision changed")
            row = session.scalar(
                select(DirectorRevisionRow)
                .where(
                    DirectorRevisionRow.id == revision_id,
                    DirectorRevisionRow.owner_id == owner_id,
                    DirectorRevisionRow.group_id == group_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            row.kind = DirectorRevisionKind.APPLIED.value
            return _revision(row)

    def find_generation_by_key(self, owner_id: UUID, key: str) -> StoryboardGeneration | None:
        with self._uow() as session:
            row = session.scalar(
                select(StoryboardGenerationRow).where(
                    StoryboardGenerationRow.owner_id == owner_id,
                    StoryboardGenerationRow.idempotency_key == key,
                )
            )
            return _generation(session, row) if row else None

    def find_unresolved_generation(
        self, owner_id: UUID, group_id: UUID, canonical_hash: str
    ) -> StoryboardGeneration | None:
        with self._uow() as session:
            row = session.scalar(
                select(StoryboardGenerationRow).where(
                    StoryboardGenerationRow.owner_id == owner_id,
                    StoryboardGenerationRow.group_id == group_id,
                    StoryboardGenerationRow.status == GenerationStatus.REMOTE_STATE_UNKNOWN.value,
                )
            )
            return _generation(session, row) if row else None

    def create_generation(self, generation: StoryboardGeneration) -> StoryboardGeneration:
        try:
            with self._uow() as session:
                _lock_active_project(session, generation.owner_id, generation.project_id)
                existing = session.scalar(
                    select(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.owner_id == generation.owner_id,
                        StoryboardGenerationRow.idempotency_key == generation.idempotency_key,
                    )
                    .with_for_update()
                )
                if existing:
                    return _generation(session, existing)
                session.execute(
                    select(DirectorPointerRow)
                    .where(
                        DirectorPointerRow.owner_id == generation.owner_id,
                        DirectorPointerRow.group_id == generation.group_id,
                    )
                    .with_for_update()
                ).one()
                unresolved = session.scalar(
                    select(StoryboardGenerationRow.id).where(
                        StoryboardGenerationRow.owner_id == generation.owner_id,
                        StoryboardGenerationRow.group_id == generation.group_id,
                        StoryboardGenerationRow.status.not_in(
                            (
                                GenerationStatus.SUCCEEDED.value,
                                GenerationStatus.FAILED.value,
                            )
                        ),
                    )
                )
                if unresolved is not None:
                    raise DomainError(
                        "remote_state_unresolved", "Equivalent storyboard request is unresolved"
                    )
                version = (
                    cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(func.max(StoryboardGenerationRow.version), 0)
                            ).where(
                                StoryboardGenerationRow.owner_id == generation.owner_id,
                                StoryboardGenerationRow.group_id == generation.group_id,
                            )
                        ),
                    )
                    + 1
                )
                stored = replace(generation, version=version)
                session.add(_generation_row(stored))
                session.add(
                    StoryboardOutboxRow(
                        id=stored.generation_id,
                        owner_id=stored.owner_id,
                        project_id=stored.project_id,
                        aggregate_type="STORYBOARD",
                        aggregate_id=stored.generation_id,
                        event_type="STORYBOARD_GENERATION_REQUESTED",
                        request_sha256=stored.canonical_request_sha256,
                        workflow_id=f"storyboard-grid-{stored.owner_id}-{stored.generation_id}",
                        state="PENDING",
                        next_attempt_at=stored.created_at,
                        created_at=stored.created_at,
                    )
                )
                current = session.get(
                    StoryboardCurrentRow, (generation.owner_id, generation.group_id)
                )
                if current is None:
                    session.add(
                        StoryboardCurrentRow(
                            owner_id=generation.owner_id,
                            group_id=generation.group_id,
                            generation_id=None,
                            version=0,
                        )
                    )
                return stored
        except IntegrityError as exc:
            with self._uow() as session:
                existing = session.scalar(
                    select(StoryboardGenerationRow).where(
                        StoryboardGenerationRow.owner_id == generation.owner_id,
                        StoryboardGenerationRow.idempotency_key == generation.idempotency_key,
                    )
                )
                if existing is not None:
                    value = _generation(session, existing)
                    if value.canonical_request_sha256 == generation.canonical_request_sha256:
                        return value
            raise DomainError(
                "idempotency_conflict", "Concurrent generation allocation conflict"
            ) from exc

    def get_generation(self, owner_id: UUID, generation_id: UUID) -> StoryboardGeneration | None:
        with self._uow() as session:
            row = session.scalar(
                select(StoryboardGenerationRow).where(
                    StoryboardGenerationRow.id == generation_id,
                    StoryboardGenerationRow.owner_id == owner_id,
                )
            )
            return _generation(session, row) if row else None

    def list_unknown_recovery_candidates(
        self, since: datetime
    ) -> tuple[StoryboardRecoveryCandidate, ...]:
        with self._uow() as session:
            latest_versions = (
                select(
                    StoryboardGenerationRow.group_id,
                    func.max(StoryboardGenerationRow.version).label("version"),
                )
                .group_by(StoryboardGenerationRow.group_id)
                .subquery()
            )
            rows = session.execute(
                select(
                    StoryboardGenerationRow,
                    StoryboardOutboxRow.workflow_id,
                    StoryboardOutboxRow.state,
                    StoryboardOutboxRow.acknowledged_at,
                    StoryboardOutboxRow.created_at,
                    AuthoritativeNarrationGroupRecord.sequence,
                )
                .join(
                    latest_versions,
                    and_(
                        StoryboardGenerationRow.group_id == latest_versions.c.group_id,
                        StoryboardGenerationRow.version == latest_versions.c.version,
                    ),
                )
                .join(
                    AuthoritativeNarrationGroupRecord,
                    and_(
                        AuthoritativeNarrationGroupRecord.id == StoryboardGenerationRow.group_id,
                        AuthoritativeNarrationGroupRecord.owner_id
                        == StoryboardGenerationRow.owner_id,
                    ),
                )
                .join(
                    ProjectRow,
                    and_(
                        ProjectRow.id == StoryboardGenerationRow.project_id,
                        ProjectRow.owner_id == StoryboardGenerationRow.owner_id,
                        ProjectRow.production_mode == "AUTO",
                    ),
                )
                .outerjoin(
                    StoryboardOutboxRow,
                    and_(
                        StoryboardOutboxRow.aggregate_id == StoryboardGenerationRow.id,
                        StoryboardOutboxRow.event_type == "STORYBOARD_RECOVERY_REQUESTED",
                    ),
                )
                .where(
                    StoryboardGenerationRow.status
                    == GenerationStatus.REMOTE_STATE_UNKNOWN.value,
                    or_(
                        StoryboardGenerationRow.created_at >= since,
                        StoryboardOutboxRow.created_at >= since,
                    ),
                )
            ).all()
            generations: dict[UUID, StoryboardGeneration] = {}
            latest_recovery: dict[UUID, tuple[datetime, str]] = {}
            sequences: dict[UUID, int] = {}
            pending: set[UUID] = set()
            for (
                row,
                workflow_id,
                outbox_state,
                acknowledged_at,
                outbox_created_at,
                sequence,
            ) in rows:
                if row.id not in generations:
                    generations[row.id] = _generation(session, row)
                    sequences[row.id] = sequence
                if acknowledged_at is None and outbox_state in {"PENDING", "DISPATCHED"}:
                    pending.add(row.id)
                if outbox_created_at is not None and (
                    row.id not in latest_recovery or outbox_created_at > latest_recovery[row.id][0]
                ):
                    latest_recovery[row.id] = (outbox_created_at, workflow_id)
            return tuple(
                StoryboardRecoveryCandidate(
                    generations[generation_id],
                    latest_recovery.get(generation_id, (None, None))[1],
                    generation_id in pending,
                    sequences[generation_id],
                )
                for generation_id in sorted(generations, key=str)
            )

    def authorize_generation_recovery(
        self, owner_id: UUID, generation_id: UUID, event_id: UUID, now: datetime
    ) -> StoryboardGeneration:
        with self._uow() as session:
            row = session.scalar(
                select(StoryboardGenerationRow)
                .where(
                    StoryboardGenerationRow.id == generation_id,
                    StoryboardGenerationRow.owner_id == owner_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            _lock_active_project(session, owner_id, row.project_id)
            if row.status != GenerationStatus.REMOTE_STATE_UNKNOWN.value:
                raise DomainError("revision_conflict", "Storyboard generation is not unresolved")
            pending = session.scalar(
                select(StoryboardOutboxRow.id).where(
                    StoryboardOutboxRow.owner_id == owner_id,
                    StoryboardOutboxRow.aggregate_id == generation_id,
                    StoryboardOutboxRow.event_type == "STORYBOARD_RECOVERY_REQUESTED",
                    StoryboardOutboxRow.acknowledged_at.is_(None),
                )
            )
            if pending is None:
                session.add(
                    StoryboardOutboxRow(
                        id=event_id,
                        owner_id=owner_id,
                        project_id=row.project_id,
                        aggregate_type="STORYBOARD_RECOVERY",
                        aggregate_id=generation_id,
                        event_type="STORYBOARD_RECOVERY_REQUESTED",
                        request_sha256=row.canonical_request_sha256,
                        workflow_id=f"storyboard-grid-recovery-{owner_id}-{event_id}",
                        state="PENDING",
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
            return _generation(session, row)

    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None:
        with self._uow() as session:
            return session.scalar(
                select(ProjectRow.production_mode).where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                )
            )

    def project_auto_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        with self._uow() as session:
            value = session.scalar(
                select(ProjectRow.auto_budget_round).where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                )
            )
            return int(value or 1)

    def save_generation_and_request_recovery(
        self, generation: StoryboardGeneration, event_id: UUID, now: datetime
    ) -> StoryboardGeneration:
        # §15.4（仅 AUTO）：未知态落库与自动对账事件在同一事务；仅在新鲜未知转换
        # （前行不是 REMOTE_STATE_UNKNOWN）且不存在未消费的对账事件时登记，
        # 确定性 workflow_id 使重放/redrive 由 Temporal REJECT_DUPLICATE 吸收。
        with self._uow() as session:
            _lock_active_project(session, generation.owner_id, generation.project_id)
            row = session.scalar(
                select(StoryboardGenerationRow)
                .where(
                    StoryboardGenerationRow.id == generation.generation_id,
                    StoryboardGenerationRow.owner_id == generation.owner_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if row.state_revision != generation.state_revision:
                return _generation(session, row)
            if not _may_transition(GenerationStatus(row.status), generation.status):
                return _generation(session, row)
            was_unknown = row.status == GenerationStatus.REMOTE_STATE_UNKNOWN.value
            values = _generation_values(generation)
            values["state_revision"] = generation.state_revision + 1
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.id == generation.generation_id,
                        StoryboardGenerationRow.owner_id == generation.owner_id,
                        StoryboardGenerationRow.state_revision == generation.state_revision,
                        StoryboardGenerationRow.status == row.status,
                    )
                    .values(**values)
                ),
            )
            if result.rowcount != 1:
                session.expire_all()
                latest = session.get_one(StoryboardGenerationRow, generation.generation_id)
                return _generation(session, latest)
            if generation.status is GenerationStatus.REMOTE_STATE_UNKNOWN and not was_unknown:
                pending = session.scalar(
                    select(StoryboardOutboxRow.id).where(
                        StoryboardOutboxRow.owner_id == generation.owner_id,
                        StoryboardOutboxRow.aggregate_id == generation.generation_id,
                        StoryboardOutboxRow.event_type == "STORYBOARD_RECOVERY_REQUESTED",
                        StoryboardOutboxRow.acknowledged_at.is_(None),
                    )
                )
                if pending is None:
                    session.add(
                        StoryboardOutboxRow(
                            id=event_id,
                            owner_id=generation.owner_id,
                            project_id=generation.project_id,
                            aggregate_type="STORYBOARD_RECOVERY",
                            aggregate_id=generation.generation_id,
                            event_type="STORYBOARD_RECOVERY_REQUESTED",
                            request_sha256=generation.canonical_request_sha256,
                            workflow_id=(
                                f"storyboard-grid-recovery-{generation.owner_id}"
                                f"-{generation.generation_id}"
                            ),
                            state="PENDING",
                            next_attempt_at=now,
                            created_at=now,
                        )
                    )
            return replace(generation, state_revision=generation.state_revision + 1)

    def supersede_with_auto_retry(
        self,
        failed: StoryboardGeneration,
        *,
        failure_stage: str,
        failure_code: str,
        successor: StoryboardGeneration,
        next_attempt_at: datetime,
    ) -> StoryboardGeneration:
        # §15.2/§15.4（仅 AUTO）：同一事务内终结失败尝试、落库下一次尝试并排期。
        # 失败行加锁 + state_revision CAS：重放/并发/redrive 直接返回当前行，
        # 不会产生第二条后继记录；后继幂等键由应用层从失败尝试确定派生。
        try:
            with self._uow() as session:
                _lock_active_project(session, failed.owner_id, failed.project_id)
                row = session.scalar(
                    select(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.id == failed.generation_id,
                        StoryboardGenerationRow.owner_id == failed.owner_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise DomainError("not_found", "Storyboard resource was not found")
                if row.state_revision != failed.state_revision or not _may_transition(
                    GenerationStatus(row.status), GenerationStatus.FAILED
                ):
                    return _generation(session, row)
                if failed.failover_chain_id is not None:
                    # 与 create_generation 共用组级导演指针锁，串行化同组链推进和版本分配。
                    session.execute(
                        select(DirectorPointerRow)
                        .where(
                            DirectorPointerRow.owner_id == failed.owner_id,
                            DirectorPointerRow.group_id == failed.group_id,
                        )
                        .with_for_update()
                    )
                    if (
                        row.failover_chain_id != failed.failover_chain_id
                        or row.failover_policy_version != failed.failover_policy_version
                        or row.failover_route_index != failed.failover_route_index
                        or row.failover_model_attempt != failed.failover_model_attempt
                        or row.failover_total_attempt != failed.failover_total_attempt
                        or successor.failover_chain_id != failed.failover_chain_id
                        or successor.failover_policy_version != failed.failover_policy_version
                        or successor.supersedes_generation_id != failed.generation_id
                    ):
                        raise DomainError(
                            "failover_conflict", "Storyboard failover lineage changed"
                        )
                    attempts = session.scalars(
                        select(StoryboardGenerationRow).where(
                            StoryboardGenerationRow.owner_id == failed.owner_id,
                            StoryboardGenerationRow.failover_chain_id == failed.failover_chain_id,
                        )
                    ).all()
                    if any(item.failover_total_attempt is None for item in attempts):
                        raise DomainError(
                            "failover_conflict", "Storyboard failover chain is incomplete"
                        )
                    latest_total = max(
                        int(item.failover_total_attempt)
                        for item in attempts
                        if item.failover_total_attempt is not None
                    )
                    latest_group_version = cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(func.max(StoryboardGenerationRow.version), 0)
                            ).where(
                                StoryboardGenerationRow.owner_id == failed.owner_id,
                                StoryboardGenerationRow.group_id == failed.group_id,
                            )
                        ),
                    )
                    if failed.version != latest_group_version:
                        return _generation(session, row)
                    if failed.failover_total_attempt != latest_total:
                        return _generation(session, row)
                    if (
                        len(attempts) != latest_total
                        or successor.failover_total_attempt != latest_total + 1
                    ):
                        raise DomainError(
                            "failover_conflict", "Storyboard failover total is not contiguous"
                        )
                    if not 1 <= latest_total + 1 <= STORYBOARD_AUTO_MAX_SUBMISSIONS:
                        raise DomainError(
                            "failover_budget_exhausted", "Storyboard failover budget is exhausted"
                        )
                    if (
                        successor.failover_route_index is None
                        or successor.failover_model_attempt is None
                    ):
                        raise DomainError(
                            "failover_conflict", "Storyboard failover successor is incomplete"
                        )
                    route_attempts = [
                        item
                        for item in attempts
                        if item.failover_route_index == successor.failover_route_index
                    ]
                    expected_model_attempt = len(route_attempts) + 1
                    if successor.failover_model_attempt != expected_model_attempt:
                        raise DomainError(
                            "failover_conflict", "Storyboard failover model attempt is invalid"
                        )
                    if expected_model_attempt > 2:
                        raise DomainError(
                            "failover_budget_exhausted", "Storyboard model budget is exhausted"
                        )
                    if (
                        successor.failover_route_index < int(failed.failover_route_index or 0)
                        or successor.auto_attempt != successor.failover_total_attempt
                    ):
                        raise DomainError(
                            "failover_conflict", "Storyboard failover route moved backwards"
                        )
                    if successor.failover_route_index == failed.failover_route_index and (
                        successor.provider != failed.provider or successor.model != failed.model
                    ):
                        raise DomainError(
                            "failover_conflict", "Storyboard failover model changed within a route"
                        )
                terminal = replace(
                    failed,
                    status=GenerationStatus.FAILED,
                    failure_stage=failure_stage,
                    failure_code=failure_code,
                )
                values = _generation_values(terminal)
                values["state_revision"] = failed.state_revision + 1
                session.execute(
                    update(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.id == failed.generation_id,
                        StoryboardGenerationRow.owner_id == failed.owner_id,
                        StoryboardGenerationRow.state_revision == failed.state_revision,
                        StoryboardGenerationRow.status == row.status,
                    )
                    .values(**values)
                )
                version = (
                    cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(func.max(StoryboardGenerationRow.version), 0)
                            ).where(
                                StoryboardGenerationRow.owner_id == successor.owner_id,
                                StoryboardGenerationRow.group_id == successor.group_id,
                            )
                        ),
                    )
                    + 1
                )
                stored = replace(successor, version=version)
                session.add(_generation_row(stored))
                session.add(
                    StoryboardOutboxRow(
                        id=stored.generation_id,
                        owner_id=stored.owner_id,
                        project_id=stored.project_id,
                        aggregate_type="STORYBOARD",
                        aggregate_id=stored.generation_id,
                        event_type="STORYBOARD_GENERATION_REQUESTED",
                        request_sha256=stored.canonical_request_sha256,
                        workflow_id=f"storyboard-grid-{stored.owner_id}-{stored.generation_id}",
                        state="PENDING",
                        next_attempt_at=next_attempt_at,
                        created_at=stored.created_at,
                    )
                )
                current = session.get(
                    StoryboardCurrentRow, (successor.owner_id, successor.group_id)
                )
                if current is None:
                    session.add(
                        StoryboardCurrentRow(
                            owner_id=successor.owner_id,
                            group_id=successor.group_id,
                            generation_id=None,
                            version=0,
                        )
                    )
                return replace(terminal, state_revision=failed.state_revision + 1)
        except IntegrityError as exc:
            # 并发败者整事务回滚（含失败行终结）；赢家已完整提交时按重放返回当前行。
            with self._uow() as session:
                existing = session.scalar(
                    select(StoryboardGenerationRow).where(
                        StoryboardGenerationRow.owner_id == successor.owner_id,
                        StoryboardGenerationRow.idempotency_key == successor.idempotency_key,
                    )
                )
                row = session.scalar(
                    select(StoryboardGenerationRow).where(
                        StoryboardGenerationRow.id == failed.generation_id,
                        StoryboardGenerationRow.owner_id == failed.owner_id,
                    )
                )
                if existing is not None and row is not None:
                    return _generation(session, row)
            raise DomainError(
                "idempotency_conflict", "Concurrent generation allocation conflict"
            ) from exc

    def save_generation(self, generation: StoryboardGeneration) -> StoryboardGeneration:
        with self._uow() as session:
            _lock_active_project(session, generation.owner_id, generation.project_id)
            row = session.scalar(
                select(StoryboardGenerationRow).where(
                    StoryboardGenerationRow.id == generation.generation_id,
                    StoryboardGenerationRow.owner_id == generation.owner_id,
                )
            )
            if row is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if row.state_revision != generation.state_revision:
                return _generation(session, row)
            if not _may_transition(GenerationStatus(row.status), generation.status):
                return _generation(session, row)
            values = _generation_values(generation)
            values["state_revision"] = generation.state_revision + 1
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.id == generation.generation_id,
                        StoryboardGenerationRow.owner_id == generation.owner_id,
                        StoryboardGenerationRow.state_revision == generation.state_revision,
                        StoryboardGenerationRow.status == row.status,
                    )
                    .values(**values)
                ),
            )
            if result.rowcount != 1:
                session.expire_all()
                latest = session.get_one(StoryboardGenerationRow, generation.generation_id)
                return _generation(session, latest)
            return replace(generation, state_revision=generation.state_revision + 1)

    def claim_submit(self, owner_id: UUID, generation_id: UUID) -> StoryboardGeneration | None:
        with self._uow() as session:
            candidate = session.get(StoryboardGenerationRow, generation_id)
            if candidate is None or candidate.owner_id != owner_id:
                return None
            _lock_active_project(session, owner_id, candidate.project_id)
            latest_version = cast(
                int,
                session.scalar(
                    select(func.coalesce(func.max(StoryboardGenerationRow.version), 0)).where(
                        StoryboardGenerationRow.owner_id == owner_id,
                        StoryboardGenerationRow.group_id == candidate.group_id,
                    )
                ),
            )
            if candidate.version != latest_version:
                return None
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.id == generation_id,
                        StoryboardGenerationRow.owner_id == owner_id,
                        StoryboardGenerationRow.status == GenerationStatus.INTENT_PERSISTED.value,
                        StoryboardGenerationRow.project_id.in_(
                            select(ProjectRow.id).where(ProjectRow.production_state == "ACTIVE")
                        ),
                    )
                    .values(
                        status=GenerationStatus.SUBMITTING.value,
                        state_revision=StoryboardGenerationRow.state_revision + 1,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            row = session.get(StoryboardGenerationRow, generation_id)
            return _generation(session, row) if row else None

    def claim_processing(
        self,
        owner_id: UUID,
        generation_id: UUID,
        expected_revision: int,
        token: UUID,
        now: datetime,
        lease_until: datetime,
    ) -> StoryboardGeneration | None:
        with self._uow() as session:
            candidate = session.get(StoryboardGenerationRow, generation_id)
            if candidate is None or candidate.owner_id != owner_id:
                return None
            _lock_active_project(session, owner_id, candidate.project_id)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.id == generation_id,
                        StoryboardGenerationRow.owner_id == owner_id,
                        StoryboardGenerationRow.state_revision == expected_revision,
                        StoryboardGenerationRow.project_id.in_(
                            select(ProjectRow.id).where(ProjectRow.production_state == "ACTIVE")
                        ),
                        or_(
                            StoryboardGenerationRow.lease_until.is_(None),
                            StoryboardGenerationRow.lease_until < now,
                        ),
                        StoryboardGenerationRow.status.not_in(
                            (
                                GenerationStatus.INTENT_PERSISTED.value,
                                GenerationStatus.SUCCEEDED.value,
                                GenerationStatus.FAILED.value,
                            )
                        ),
                    )
                    .values(
                        state_revision=StoryboardGenerationRow.state_revision + 1,
                        execution_token=token,
                        lease_until=lease_until,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            return _generation(session, session.get_one(StoryboardGenerationRow, generation_id))

    def release_processing(self, owner_id: UUID, generation_id: UUID, token: UUID) -> None:
        with self._uow() as session:
            session.execute(
                update(StoryboardGenerationRow)
                .where(
                    StoryboardGenerationRow.owner_id == owner_id,
                    StoryboardGenerationRow.id == generation_id,
                    StoryboardGenerationRow.execution_token == token,
                )
                .values(execution_token=None, lease_until=None)
            )

    def complete_and_switch_current(self, generation: StoryboardGeneration) -> StoryboardGeneration:
        with self._uow() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                    {"scope": f"{generation.owner_id}:{generation.group_id}"},
                )
            _lock_active_project(session, generation.owner_id, generation.project_id)
            row = session.scalar(
                select(StoryboardGenerationRow)
                .where(
                    StoryboardGenerationRow.id == generation.generation_id,
                    StoryboardGenerationRow.owner_id == generation.owner_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if row.state_revision != generation.state_revision or not _may_transition(
                GenerationStatus(row.status), generation.status
            ):
                return _generation(session, row)
            assert generation.asset is not None
            media = generation.asset.media
            stored_media = session.get(StoryboardMediaRow, generation.generation_id)
            if stored_media is None:
                canonical = session.get(MediaAssetRow, media.asset_id)
                reference = session.get(
                    ProjectAssetReferenceRow, (generation.project_id, media.asset_id)
                )
                if (
                    canonical is None
                    or reference is None
                    or (
                        canonical.owner_id != generation.owner_id
                        or canonical.sha256 != media.sha256
                        or canonical.media_type != media.media_type
                        or canonical.size_bytes != media.size_bytes
                    )
                ):
                    raise DomainError(
                        "storage_integrity_error", "Canonical storyboard media facts conflict"
                    )
                session.add(
                    StoryboardMediaRow(
                        generation_id=generation.generation_id,
                        storyboard_asset_id=generation.asset.storyboard_asset_id,
                        asset_id=media.asset_id,
                        width=generation.asset.width,
                        height=generation.asset.height,
                        image_format=generation.asset.image_format,
                    )
                )
            values = _generation_values(generation)
            values["state_revision"] = generation.state_revision + 1
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StoryboardGenerationRow)
                    .where(
                        StoryboardGenerationRow.id == generation.generation_id,
                        StoryboardGenerationRow.state_revision == generation.state_revision,
                        StoryboardGenerationRow.status == row.status,
                    )
                    .values(**values)
                ),
            )
            if result.rowcount != 1:
                session.expire_all()
                return _generation(
                    session, session.get_one(StoryboardGenerationRow, generation.generation_id)
                )
            pointer = session.scalar(
                select(StoryboardCurrentRow)
                .where(
                    StoryboardCurrentRow.owner_id == generation.owner_id,
                    StoryboardCurrentRow.group_id == generation.group_id,
                )
                .with_for_update()
            )
            if pointer is None:
                pointer = StoryboardCurrentRow(
                    owner_id=generation.owner_id,
                    group_id=generation.group_id,
                    generation_id=None,
                    version=0,
                )
                session.add(pointer)
                session.flush()
            if generation.version > pointer.version:
                active_h3 = session.scalar(
                    select(VideoAttemptRecord.attempt_id).where(
                        VideoAttemptRecord.owner_id == generation.owner_id,
                        VideoAttemptRecord.group_id == generation.group_id,
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
                )
                if active_h3 is not None:
                    raise DomainError(
                        "active_group_video_attempt_exists",
                        "Current storyboard cannot change while H3 is active",
                    )
                pointer.generation_id = generation.generation_id
                pointer.version = generation.version
            return replace(generation, state_revision=generation.state_revision + 1)

    def select_current_generation(
        self, owner_id: UUID, group_id: UUID, generation_id: UUID
    ) -> StoryboardGeneration:
        with self._uow() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                    {"scope": f"{owner_id}:{group_id}"},
                )
            row = session.scalar(
                select(StoryboardGenerationRow)
                .where(
                    StoryboardGenerationRow.id == generation_id,
                    StoryboardGenerationRow.owner_id == owner_id,
                    StoryboardGenerationRow.group_id == group_id,
                    StoryboardGenerationRow.status == GenerationStatus.SUCCEEDED.value,
                )
                .with_for_update()
            )
            if row is None or session.get(StoryboardMediaRow, generation_id) is None:
                raise DomainError(
                    "invalid_storyboard_candidate", "Storyboard candidate is unavailable"
                )
            active_h3 = session.scalar(
                select(VideoAttemptRecord.attempt_id).where(
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
            )
            if active_h3 is not None:
                raise DomainError(
                    "active_group_video_attempt_exists",
                    "Current storyboard cannot change while H3 is active",
                )
            pointer = session.scalar(
                select(StoryboardCurrentRow)
                .where(
                    StoryboardCurrentRow.owner_id == owner_id,
                    StoryboardCurrentRow.group_id == group_id,
                )
                .with_for_update()
            )
            if pointer is None:
                pointer = StoryboardCurrentRow(
                    owner_id=owner_id,
                    group_id=group_id,
                    generation_id=generation_id,
                    version=row.version,
                )
                session.add(pointer)
            else:
                pointer.generation_id = generation_id
                pointer.version = row.version
            return _generation(session, row)

    def bind_current_in_transaction(
        self, transaction: object, binding: VideoStoryboardBinding
    ) -> None:
        if not isinstance(transaction, Session):
            raise TypeError("H3 binding transaction must be a SQLAlchemy Session")
        session = transaction
        _lock_active_project(session, binding.owner_id, binding.project_id)
        generation = session.scalar(
            select(StoryboardGenerationRow).where(
                StoryboardGenerationRow.id == binding.storyboard_generation_id,
                StoryboardGenerationRow.owner_id == binding.owner_id,
                StoryboardGenerationRow.applied_revision_id == binding.applied_revision_id,
            )
        )
        if generation is None:
            raise DomainError("not_found", "Storyboard resource was not found")
        media = session.get(StoryboardMediaRow, generation.id)
        canonical_asset = (
            session.scalar(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.id == media.asset_id,
                    MediaAssetRow.owner_id == binding.owner_id,
                    MediaAssetRow.purging.is_(False),
                    MediaAssetRow.deletion_state == "ACTIVE",
                )
                .with_for_update()
            )
            if media is not None
            else None
        )
        project_reference = (
            session.get(
                ProjectAssetReferenceRow,
                (binding.project_id, media.asset_id),
            )
            if media is not None
            else None
        )
        if (
            generation.status != GenerationStatus.SUCCEEDED.value
            or media is None
            or canonical_asset is None
            or canonical_asset.sha256 != binding.storyboard_asset_sha256
            or generation.project_id != binding.project_id
            or generation.group_id != binding.group_id
            or project_reference is None
        ):
            raise DomainError(
                "revision_conflict", "Video intent requires the exact successful storyboard asset"
            )
        current = session.scalar(
            select(StoryboardCurrentRow)
            .where(
                StoryboardCurrentRow.owner_id == binding.owner_id,
                StoryboardCurrentRow.group_id == binding.group_id,
            )
            .with_for_update()
        )
        if current is None or current.generation_id != binding.storyboard_generation_id:
            raise DomainError("revision_conflict", "Video intent requires the current storyboard")
        session.add(
            VideoStoryboardBindingRow(
                video_id=binding.video_id,
                owner_id=binding.owner_id,
                project_id=binding.project_id,
                group_id=binding.group_id,
                storyboard_generation_id=binding.storyboard_generation_id,
                applied_revision_id=binding.applied_revision_id,
                storyboard_asset_sha256=binding.storyboard_asset_sha256,
            )
        )

    def get_video_binding(self, owner_id: UUID, video_id: UUID) -> VideoStoryboardBinding | None:
        with self._uow() as session:
            row = session.scalar(
                select(VideoStoryboardBindingRow).where(
                    VideoStoryboardBindingRow.owner_id == owner_id,
                    VideoStoryboardBindingRow.video_id == video_id,
                )
            )
            if row is None:
                return None
            return VideoStoryboardBinding(
                row.video_id,
                row.owner_id,
                row.project_id,
                row.group_id,
                row.storyboard_generation_id,
                row.applied_revision_id,
                row.storyboard_asset_sha256,
            )

    def get_current_generation(self, owner_id: UUID, group_id: UUID) -> StoryboardGeneration | None:
        with self._uow() as session:
            pointer = session.get(StoryboardCurrentRow, (owner_id, group_id))
            if pointer is None or pointer.generation_id is None:
                return None
            row = session.scalar(
                select(StoryboardGenerationRow).where(
                    StoryboardGenerationRow.id == pointer.generation_id,
                    StoryboardGenerationRow.owner_id == owner_id,
                )
            )
            return _generation(session, row) if row else None

    def list_generation_history(
        self, owner_id: UUID, group_id: UUID
    ) -> tuple[StoryboardGeneration, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(StoryboardGenerationRow)
                .where(
                    StoryboardGenerationRow.owner_id == owner_id,
                    StoryboardGenerationRow.group_id == group_id,
                )
                .order_by(StoryboardGenerationRow.version.desc())
            ).all()
            return tuple(_generation(session, row) for row in rows)


def _lock_active_project(session: Session, owner_id: UUID, project_id: UUID) -> None:
    state = session.scalar(
        select(ProjectRow.production_state)
        .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
        .with_for_update()
    )
    if state is None:
        raise DomainError("not_found", "Storyboard resource was not found")
    if state != "ACTIVE":
        raise DomainError("project_purging", "Project is being purged")


def _has_active_storyboard_jobs(session: Session, owner_id: UUID, project_id: UUID) -> bool:
    generation = session.scalar(
        select(StoryboardGenerationRow.id)
        .where(
            StoryboardGenerationRow.owner_id == owner_id,
            StoryboardGenerationRow.project_id == project_id,
            StoryboardGenerationRow.status.not_in(
                (GenerationStatus.SUCCEEDED.value, GenerationStatus.FAILED.value)
            ),
        )
        .limit(1)
    )
    director = session.scalar(
        select(DirectorGenerationAttemptRow.id)
        .where(
            DirectorGenerationAttemptRow.owner_id == owner_id,
            DirectorGenerationAttemptRow.input_json["project_id"].as_string() == str(project_id),
            DirectorGenerationAttemptRow.status.not_in(
                (
                    DirectorAttemptStatus.SUCCEEDED.value,
                    DirectorAttemptStatus.FAILED.value,
                )
            ),
        )
        .limit(1)
    )
    h3 = session.scalar(
        select(VideoAttemptRecord.attempt_id)
        .where(
            VideoAttemptRecord.owner_id == owner_id,
            VideoAttemptRecord.project_id == project_id,
            VideoAttemptRecord.stage.not_in(("ARCHIVED", "FAILED", "CANCELLED")),
        )
        .limit(1)
    )
    return generation is not None or director is not None or h3 is not None


def _authoritative_narration_ready(
    project: ProjectRow,
    group: AuthoritativeNarrationGroup,
    media: dict[UUID, MediaAssetRow],
) -> bool:
    sentences: Sequence[AuthoritativeNarrationSentence] = group.sentences
    ready = bool(sentences) and not group.video_blocked
    ready = (
        ready
        and project.production_state == "ACTIVE"
        and project.snapshot_script is not None
        and frozen_script_sha256(project.snapshot_script) == group.snapshot_sha256
    )
    cursor = group.start_ms
    frame_cursor = sentences[0].start_frame if sentences else 0
    for index, sentence in enumerate(sentences, start=1):
        raw, normalized = media.get(sentence.raw_asset_id), media.get(sentence.normalized_asset_id)
        expected_start_ms = (
            round(Fraction(sentence.start_frame * 1000, sentence.sample_rate))
            if sentence.sample_rate > 0
            else -1
        )
        expected_end_ms = (
            round(Fraction(sentence.end_frame * 1000, sentence.sample_rate))
            if sentence.sample_rate > 0
            else -1
        )
        ready = (
            ready
            and sentence.ordinal == index
            and sentence.start_frame == frame_cursor
            and sentence.start_ms == cursor == expected_start_ms
            and sentence.end_ms == expected_end_ms
            and sentence.end_frame - sentence.start_frame == sentence.sample_count
            and sentence.qc_passed
            and hashlib.sha256(sentence.text.encode()).hexdigest() == sentence.text_sha256
            and raw is not None
            and normalized is not None
            and raw.sha256 == sentence.raw_sha256
            and normalized.sha256 == sentence.normalized_sha256
            and raw.size_bytes == sentence.raw_size_bytes
            and normalized.size_bytes == sentence.normalized_size_bytes
            and raw.owner_id == normalized.owner_id == group.owner_id
            and raw.project_id == normalized.project_id == group.project_id
            and raw.deletion_state == normalized.deletion_state == "ACTIVE"
            and not raw.purging
            and not normalized.purging
            and raw.kind == "narration_raw"
            and normalized.kind == "narration_normalized"
            and raw.media_type == normalized.media_type == "audio/wav"
        )
        cursor = sentence.end_ms
        frame_cursor = sentence.end_frame
    return ready and cursor == group.end_ms


def _shot(value: dict[str, object]) -> DirectorShot:
    return DirectorShot(
        shot_id=UUID(cast(str, value["shot_id"])),
        order=cast(int, value["order"]),
        scene=cast(str, value["scene"]),
        shot_size=cast(str, value["shot_size"]),
        camera_viewpoint=cast(str, value["camera_viewpoint"]),
        subject_action_outcome=cast(str, value["subject_action_outcome"]),
        narrative_function=cast(str, value["narrative_function"]),
        description=cast(str, value["description"]),
        semantic_event=DirectorSemanticEvent(cast(str, value.get("semantic_event", "UNSPECIFIED"))),
        subject_focus=DirectorSubjectFocus(cast(str, value.get("subject_focus", "UNSPECIFIED"))),
        time_relation=DirectorTimeRelation(cast(str, value.get("time_relation", "UNSPECIFIED"))),
        location_relation=DirectorLocationRelation(
            cast(str, value.get("location_relation", "UNSPECIFIED"))
        ),
    )


def _director_input_json(value: DirectorInputSnapshot) -> dict[str, object]:
    return {
        "owner_id": str(value.owner_id),
        "project_id": str(value.project_id),
        "group_id": str(value.group_id),
        "expected_draft_revision_id": str(value.expected_draft_revision_id)
        if value.expected_draft_revision_id
        else None,
        "sentences": [
            {
                "sentence_id": str(item.sentence_id),
                "order": item.order,
                "text": item.text,
                "text_sha256": item.text_sha256,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
            }
            for item in value.sentences
        ],
        "characters": [
            {
                "ordinal": item.ordinal,
                "character_image_id": str(item.character_image_id),
                "asset_id": str(item.asset_id),
                "sha256": item.sha256,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
            }
            for item in value.characters
        ],
        "visual_style": value.visual_style,
        "locale": value.locale,
        "aspect_ratio": value.aspect_ratio,
    }


def _director_attempt_row(value: DirectorGenerationAttempt) -> DirectorGenerationAttemptRow:
    return DirectorGenerationAttemptRow(
        id=value.attempt_id,
        owner_id=value.owner_id,
        group_id=value.group_id,
        expected_draft_revision_id=value.input_snapshot.expected_draft_revision_id,
        idempotency_key=value.idempotency_key,
        provider_idempotency_key=value.provider_idempotency_key,
        canonical_request_sha256=value.canonical_request_sha256,
        input_json=_director_input_json(value.input_snapshot),
        prompt_ref=value.prompt_ref,
        prompt_sha256=value.prompt_sha256,
        prompt_content=value.prompt_content,
        model=value.model,
        parameters_json=dict(value.parameters),
        status=value.status.value,
        state_revision=value.state_revision,
        provider_receipt=value.provider_receipt,
        raw_response_sha256=value.raw_response_sha256,
        draft_revision_id=value.draft_revision_id,
        failure_code=value.failure_code,
        history_json=list(value.history),
        lease_token=value.lease_token,
        lease_until=value.lease_until,
        created_at=value.created_at,
    )


def _director_attempt(row: DirectorGenerationAttemptRow) -> DirectorGenerationAttempt:
    data = row.input_json
    snapshot = DirectorInputSnapshot(
        row.owner_id,
        UUID(cast(str, data["project_id"])),
        row.group_id,
        row.expected_draft_revision_id,
        tuple(
            DirectorSentenceSnapshot(
                UUID(cast(str, item["sentence_id"])),
                cast(int, item["order"]),
                cast(str, item["text"]),
                cast(str, item["text_sha256"]),
                cast(int, item["start_ms"]),
                cast(int, item["end_ms"]),
            )
            for item in cast(list[dict[str, object]], data["sentences"])
        ),
        tuple(
            CharacterAssetSnapshot(
                cast(int, item["ordinal"]),
                UUID(cast(str, item["character_image_id"])),
                UUID(cast(str, item["asset_id"])),
                cast(str, item["sha256"]),
                cast(str, item["media_type"]),
                cast(int, item["size_bytes"]),
            )
            for item in cast(list[dict[str, object]], data["characters"])
        ),
        cast(str, data["visual_style"]),
        cast(str, data["locale"]),
        cast(str, data["aspect_ratio"]),
    )
    return DirectorGenerationAttempt(
        row.id,
        row.owner_id,
        row.group_id,
        row.idempotency_key,
        row.provider_idempotency_key,
        row.canonical_request_sha256,
        snapshot,
        row.prompt_ref,
        row.prompt_sha256,
        row.prompt_content,
        row.model,
        cast(dict[str, str | int | float | bool], row.parameters_json),
        DirectorAttemptStatus(row.status),
        row.created_at,
        row.state_revision,
        row.provider_receipt,
        row.raw_response_sha256,
        row.draft_revision_id,
        row.failure_code,
        tuple(row.history_json),
        row.lease_token,
        row.lease_until,
    )


def _shot_json(shot: DirectorShot) -> dict[str, object]:
    return {
        "shot_id": str(shot.shot_id),
        "order": shot.order,
        "scene": shot.scene,
        "shot_size": shot.shot_size,
        "camera_viewpoint": shot.camera_viewpoint,
        "subject_action_outcome": shot.subject_action_outcome,
        "narrative_function": shot.narrative_function,
        "description": shot.description,
        "semantic_event": shot.semantic_event.value,
        "subject_focus": shot.subject_focus.value,
        "time_relation": shot.time_relation.value,
        "location_relation": shot.location_relation.value,
    }


def _revision(row: DirectorRevisionRow) -> DirectorPlanRevision:
    return DirectorPlanRevision(
        row.id,
        row.owner_id,
        row.project_id,
        row.group_id,
        row.revision_number,
        DirectorRevisionKind(row.kind),
        tuple(_shot(item) for item in row.shots_json),
        tuple(
            ShotSentenceLink(UUID(item["shot_id"]), UUID(item["sentence_id"]))
            for item in row.links_json
        ),
        tuple(UUID(item) for item in row.group_sentence_ids_json),
        ReviewState(row.review_state),
        tuple(
            ContinuousObservationApproval(
                UUID(item["previous_shot_id"]), UUID(item["next_shot_id"]), item["reason"]
            )
            for item in row.continuous_observation_approvals_json
        ),
        row.created_at,
    )


def _revision_row(value: DirectorPlanRevision) -> DirectorRevisionRow:
    return DirectorRevisionRow(
        id=value.revision_id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        group_id=value.group_id,
        revision_number=value.revision_number,
        kind=value.kind.value,
        shots_json=[_shot_json(item) for item in value.shots],
        links_json=[
            {"shot_id": str(item.shot_id), "sentence_id": str(item.sentence_id)}
            for item in value.sentence_links
        ],
        group_sentence_ids_json=[str(item) for item in value.group_sentence_ids],
        review_state=value.review_state.value,
        continuous_observation_approvals_json=[
            {
                "previous_shot_id": str(item.previous_shot_id),
                "next_shot_id": str(item.next_shot_id),
                "reason": item.reason,
            }
            for item in value.continuous_observation_approvals
        ],
        created_at=value.created_at,
    )


def _generation_row(value: StoryboardGeneration) -> StoryboardGenerationRow:
    row = StoryboardGenerationRow(
        id=value.generation_id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        group_id=value.group_id,
        applied_revision_id=value.applied_revision_id,
        version=value.version,
        kind=value.kind.value,
        input_json={},
        idempotency_key=value.idempotency_key,
        provider_idempotency_key=value.provider_idempotency_key,
        canonical_request_sha256=value.canonical_request_sha256,
        status=value.status.value,
        state_revision=value.state_revision,
        created_at=value.created_at,
        provider_task_id=value.provider_task_id,
        provider_receipt=value.provider_receipt,
        checkpoint_json={},
        asset_id=value.asset.media.asset_id if value.asset else None,
        provider=value.provider or None,
        failover_chain_id=value.failover_chain_id,
        failover_policy_version=value.failover_policy_version,
        failover_route_index=value.failover_route_index,
        failover_model_attempt=value.failover_model_attempt,
        failover_total_attempt=value.failover_total_attempt,
    )
    _copy_generation(row, value)
    return row


def _copy_generation(row: StoryboardGenerationRow, value: StoryboardGeneration) -> None:
    row.status, row.provider_task_id, row.provider_receipt = (
        value.status.value,
        value.provider_task_id,
        value.provider_receipt,
    )
    row.provider = value.provider or None
    row.failover_chain_id = value.failover_chain_id
    row.failover_policy_version = value.failover_policy_version
    row.failover_route_index = value.failover_route_index
    row.failover_model_attempt = value.failover_model_attempt
    row.failover_total_attempt = value.failover_total_attempt
    row.input_json = {
        "shots": [_shot_json(item) for item in value.shots_snapshot],
        "links": [
            {"shot_id": str(item.shot_id), "sentence_id": str(item.sentence_id)}
            for item in value.sentence_links_snapshot
        ],
        "prompt": value.prompt,
        "prompt_safety": list(value.prompt_safety),
        "character_assets": [
            {
                "ordinal": item.ordinal,
                "character_image_id": str(item.character_image_id),
                "asset_id": str(item.asset_id),
                "sha256": item.sha256,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
            }
            for item in value.character_assets_snapshot
        ],
        "style": value.style_snapshot,
        "provider": value.provider or None,
        "model": value.model,
        "parameters": dict(value.parameters),
        "provider_idempotency_key": value.provider_idempotency_key,
        "auto_attempt": value.auto_attempt,
        "budget_round": value.budget_round,
        "supersedes_generation_id": (
            str(value.supersedes_generation_id)
            if value.supersedes_generation_id is not None
            else None
        ),
    }
    if value.shot_time_ranges_snapshot:
        row.input_json["shot_time_ranges"] = [
            {
                "order": value.shot_order,
                "start_ms": value.start_ms,
                "end_ms": value.end_ms,
            }
            for value in value.shot_time_ranges_snapshot
        ]
        if value.shot_time_ranges_algorithm:
            # monotone-projection-v1：算法键随区间持久化；历史行无此键按 legacy 自洽。
            row.input_json["shot_time_ranges_algorithm"] = value.shot_time_ranges_algorithm
    row.checkpoint_json = {
        "download_token": value.download_token,
        "expected_sha256": value.expected_sha256,
        "downloaded_sha256": value.downloaded_sha256,
        "downloaded_size": value.downloaded_size,
        "qc_width": value.qc_width,
        "qc_height": value.qc_height,
        "qc_mime": value.qc_mime,
        "object_asset": {
            "asset_id": str(value.object_asset.asset_id),
            "sha256": value.object_asset.sha256,
            "media_type": value.object_asset.media_type,
            "size_bytes": value.object_asset.size_bytes,
        }
        if value.object_asset
        else None,
        "failure_stage": value.failure_stage,
        "failure_code": value.failure_code,
        "submission_receipt": value.submission_receipt,
        "recovery_failure_count": value.recovery_failure_count,
        "unknown_origin_stage": value.unknown_origin_stage,
        "unknown_origin_code": value.unknown_origin_code,
    }
    row.asset_id = value.asset.media.asset_id if value.asset else None


def _generation_values(value: StoryboardGeneration) -> dict[str, object]:
    transient = StoryboardGenerationRow()
    _copy_generation(transient, value)
    return {
        "status": transient.status,
        "provider_task_id": transient.provider_task_id,
        "provider_receipt": transient.provider_receipt,
        "input_json": transient.input_json,
        "checkpoint_json": transient.checkpoint_json,
        "asset_id": transient.asset_id,
        "provider": transient.provider,
        "failover_chain_id": transient.failover_chain_id,
        "failover_policy_version": transient.failover_policy_version,
        "failover_route_index": transient.failover_route_index,
        "failover_model_attempt": transient.failover_model_attempt,
        "failover_total_attempt": transient.failover_total_attempt,
        "execution_token": value.execution_token,
        "lease_until": value.lease_until,
    }


def _may_transition(current: GenerationStatus, proposed: GenerationStatus) -> bool:
    if current in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}:
        return proposed is current
    allowed = {
        GenerationStatus.INTENT_PERSISTED: {
            GenerationStatus.SUBMITTING,
            GenerationStatus.FAILED,
        },
        GenerationStatus.SUBMITTING: {
            GenerationStatus.SUBMITTED,
            GenerationStatus.REMOTE_STATE_UNKNOWN,
            GenerationStatus.FAILED,
        },
        GenerationStatus.SUBMITTED: {
            GenerationStatus.SUBMITTED,
            GenerationStatus.RUNNING,
            GenerationStatus.REMOTE_STATE_UNKNOWN,
            GenerationStatus.DOWNLOADED,
            GenerationStatus.FAILED,
        },
        GenerationStatus.RUNNING: {
            GenerationStatus.RUNNING,
            GenerationStatus.REMOTE_STATE_UNKNOWN,
            GenerationStatus.DOWNLOADED,
            GenerationStatus.FAILED,
        },
        GenerationStatus.REMOTE_STATE_UNKNOWN: {
            GenerationStatus.REMOTE_STATE_UNKNOWN,
            GenerationStatus.SUBMITTED,
            GenerationStatus.RUNNING,
            GenerationStatus.DOWNLOADED,
            GenerationStatus.FAILED,
        },
        GenerationStatus.DOWNLOADED: {
            GenerationStatus.DOWNLOADED,
            GenerationStatus.QC_PASSED,
            GenerationStatus.FAILED,
        },
        GenerationStatus.QC_PASSED: {
            GenerationStatus.QC_PASSED,
            GenerationStatus.OBJECT_STORED,
        },
        GenerationStatus.OBJECT_STORED: {
            GenerationStatus.OBJECT_STORED,
            GenerationStatus.SUCCEEDED,
        },
    }
    return proposed in allowed.get(current, set())


def _generation(session: Session, row: StoryboardGenerationRow) -> StoryboardGeneration:
    inputs, checkpoint = row.input_json, row.checkpoint_json or {}
    object_data = cast(dict[str, Any] | None, checkpoint.get("object_asset"))
    object_asset = (
        MediaAssetRef(
            UUID(object_data["asset_id"]),
            object_data["sha256"],
            object_data["media_type"],
            object_data["size_bytes"],
        )
        if object_data
        else None
    )
    media_row = session.get(StoryboardMediaRow, row.id)
    canonical_asset = (
        session.get(MediaAssetRow, media_row.asset_id) if media_row is not None else None
    )
    asset = (
        StoryboardAsset(
            media_row.storyboard_asset_id,
            row.id,
            StoryboardAssetKind.GRID_SHEET,
            MediaAssetRef(
                canonical_asset.id,
                canonical_asset.sha256,
                canonical_asset.media_type,
                canonical_asset.size_bytes,
            ),
            media_row.width,
            media_row.height,
            media_row.image_format,
        )
        if media_row and canonical_asset
        else None
    )
    return StoryboardGeneration(
        row.id,
        row.owner_id,
        row.project_id,
        row.group_id,
        row.applied_revision_id,
        tuple(_shot(item) for item in cast(list[dict[str, object]], inputs["shots"])),
        tuple(
            ShotSentenceLink(UUID(item["shot_id"]), UUID(item["sentence_id"]))
            for item in cast(list[dict[str, str]], inputs["links"])
        ),
        row.version,
        StoryboardAssetKind(row.kind),
        cast(str, inputs["prompt"]),
        tuple(
            CharacterAssetSnapshot(
                cast(int, item["ordinal"]),
                UUID(cast(str, item["character_image_id"])),
                UUID(cast(str, item["asset_id"])),
                cast(str, item["sha256"]),
                cast(str, item["media_type"]),
                cast(int, item["size_bytes"]),
            )
            for item in cast(list[dict[str, object]], inputs["character_assets"])
        ),
        cast(str, inputs["style"]),
        cast(str, inputs["model"]),
        cast(dict[str, str | int | float | bool], inputs["parameters"]),
        row.idempotency_key,
        row.provider_idempotency_key,
        row.canonical_request_sha256,
        GenerationStatus(row.status),
        row.created_at,
        row.state_revision,
        row.provider_task_id,
        row.provider_receipt,
        cast(str | None, checkpoint.get("download_token")),
        cast(str | None, checkpoint.get("expected_sha256")),
        cast(str | None, checkpoint.get("downloaded_sha256")),
        cast(int | None, checkpoint.get("downloaded_size")),
        cast(int | None, checkpoint.get("qc_width")),
        cast(int | None, checkpoint.get("qc_height")),
        cast(str | None, checkpoint.get("qc_mime")),
        object_asset,
        cast(str | None, checkpoint.get("failure_stage")),
        cast(str | None, checkpoint.get("failure_code")),
        asset,
        row.execution_token,
        row.lease_until,
        shot_time_ranges_snapshot=tuple(
            ShotTimeRange(
                cast(int, item["order"]),
                cast(int, item["start_ms"]),
                cast(int, item["end_ms"]),
            )
            for item in cast(list[dict[str, object]], inputs.get("shot_time_ranges", []))
        ),
        shot_time_ranges_algorithm=cast(str, inputs.get("shot_time_ranges_algorithm") or ""),
        auto_attempt=cast(int, inputs.get("auto_attempt", 1)),
        budget_round=cast(int, inputs.get("budget_round", 1)),
        supersedes_generation_id=(
            UUID(cast(str, inputs["supersedes_generation_id"]))
            if inputs.get("supersedes_generation_id")
            else None
        ),
        prompt_safety=tuple(
            str(item) for item in cast(list[object], inputs.get("prompt_safety") or [])
        ),
        provider=row.provider or "",
        failover_chain_id=row.failover_chain_id,
        failover_policy_version=row.failover_policy_version,
        failover_route_index=row.failover_route_index,
        failover_model_attempt=row.failover_model_attempt,
        failover_total_attempt=row.failover_total_attempt,
        submission_receipt=cast(str | None, checkpoint.get("submission_receipt")),
        recovery_failure_count=cast(int, checkpoint.get("recovery_failure_count", 0)),
        unknown_origin_stage=cast(str | None, checkpoint.get("unknown_origin_stage")),
        unknown_origin_code=cast(str | None, checkpoint.get("unknown_origin_code")),
    )
