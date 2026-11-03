from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.models import (
    ProjectRow,
    ProjectSnapshotCharacterRow,
)
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    AuthoritativeNarrationMembershipRecord,
    AuthoritativeNarrationSentenceRecord,
    NarrationProjectRecord,
)
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorPointerRow,
    DirectorRevisionRow,
    StoryboardOutboxRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    DirectorRevisionLineageRow,
    ProjectDirectorCurrentRow,
    ProjectDirectorGroupCandidateRow,
    ProjectDirectorMaterializationRow,
    ProjectDirectorRevisionRow,
    ProjectDirectorRunRow,
    ProjectDirectorUnitRow,
)
from tubeflow_test.application.project_director import (
    _DETERMINISTIC_SEPARATOR_RECEIPT,
    ProjectDirectorUnitBinding,
    prepare_manual_reprocessing,
)
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import authoritative_sentence_id, segment_pasted_script
from tubeflow_test.domain.project_director import (
    PROJECT_DIRECTOR_WORKER_LOST_FAILURE,
    MaterializationStatus,
    ProjectDirectorConflict,
    ProjectDirectorGroupCandidate,
    ProjectDirectorGroupContext,
    ProjectDirectorInputSnapshot,
    ProjectDirectorMaterialization,
    ProjectDirectorRevision,
    ProjectDirectorRun,
    ProjectDirectorRunProgress,
    ProjectDirectorRunStatus,
    ProjectDirectorSentenceSnapshot,
    ProjectDirectorSourceShotLink,
    ProjectDirectorTrigger,
    ProjectDirectorUnit,
    ProjectDirectorUnitKind,
    ProjectDirectorUnitStatus,
)
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    ContinuousObservationApproval,
    DirectorLocationRelation,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
    ReviewState,
    ShotSentenceLink,
)
from tubeflow_test.sentence_split.domain import (
    apply_sentence_split_plan,
    sentence_split_plan_from_json,
)

logger = logging.getLogger(__name__)


class SqlAlchemyProjectDirectorRepository:
    def __init__(self, unit_of_work: Callable[[], AbstractContextManager[Session]]) -> None:
        self._uow = unit_of_work

    def project_context(
        self, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorInputSnapshot | None:
        with self._uow() as session:
            return self.project_context_in_session(session, owner_id, project_id)

    def project_context_in_session(
        self, session: Session, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorInputSnapshot | None:
        project = session.scalar(
            select(ProjectRow).where(
                ProjectRow.id == project_id,
                ProjectRow.owner_id == owner_id,
                ProjectRow.production_state == "ACTIVE",
            )
        )
        if (
            project is None
            or project.snapshot_confirmed_at is None
            or project.snapshot_script is None
            or project.snapshot_locale is None
            or project.snapshot_visual_style is None
            or project.snapshot_aspect_ratio is None
        ):
            return None
        narration = session.get(NarrationProjectRecord, project_id)
        if narration is None or narration.owner_id != owner_id:
            return None
        encoded = narration.payload.get("script")
        if not isinstance(encoded, dict):
            return None
        script = segment_pasted_script(
            project.snapshot_script, SupportedLocale(project.snapshot_locale)
        )
        split_plan = sentence_split_plan_from_json(narration.payload.get("sentence_splits"))
        if split_plan is not None and split_plan.splits:
            # AUTO 项目冻结时的拆句血缘参与同一确定性句子比对。
            script = apply_sentence_split_plan(script, split_plan)
        if (
            encoded.get("source_sha256") != script.source_sha256
            or encoded.get("rule_version") != script.rule_version
            or encoded.get("language") != script.language.value
        ):
            raise DomainError("project_snapshot_mismatch", "Frozen director script changed")
        encoded_sentences = encoded.get("sentences")
        if not isinstance(encoded_sentences, list) or len(encoded_sentences) != len(
            script.sentences
        ):
            raise DomainError("project_snapshot_mismatch", "Frozen director sentences changed")
        sentences = tuple(
            ProjectDirectorSentenceSnapshot(
                authoritative_sentence_id(project_id, script.source_sha256, item.sequence),
                item.sequence,
                item.text,
                item.text_sha256,
            )
            for item in script.sentences
        )
        expected_encoded = [
            {
                "sequence": item.sequence,
                "text": item.text,
                "text_sha256": item.text_sha256,
            }
            for item in script.sentences
        ]
        if encoded_sentences != expected_encoded:
            raise DomainError("project_snapshot_mismatch", "Frozen director sentences changed")
        character_rows = session.scalars(
            select(ProjectSnapshotCharacterRow)
            .where(ProjectSnapshotCharacterRow.project_id == project_id)
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
        return ProjectDirectorInputSnapshot(
            owner_id=owner_id,
            project_id=project_id,
            snapshot_sha256=script.source_sha256,
            snapshot_revision=1,
            sentences=sentences,
            characters=characters,
            visual_style=project.snapshot_visual_style,
            locale=project.snapshot_locale,
            aspect_ratio=project.snapshot_aspect_ratio,
        )

    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None:
        with self._uow() as session:
            return session.scalar(
                select(ProjectRow.production_mode).where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                )
            )

    def find_run_by_key(self, owner_id: UUID, key: str) -> ProjectDirectorRun | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDirectorRunRow).where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.idempotency_key == key,
                )
            )
            return _run(row) if row is not None else None

    def find_blocking_run(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRun | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDirectorRunRow)
                .where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.project_id == project_id,
                    or_(
                        ProjectDirectorRunRow.status.in_(
                            (
                                ProjectDirectorRunStatus.INTENT_PERSISTED.value,
                                ProjectDirectorRunStatus.RUNNING.value,
                                ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN.value,
                            )
                        ),
                        ProjectDirectorRunRow.retry_blocked.is_(True),
                    ),
                )
                .order_by(ProjectDirectorRunRow.created_at.desc(), ProjectDirectorRunRow.id.desc())
                .limit(1)
            )
            return _run(row) if row is not None else None

    def create_run(
        self,
        run: ProjectDirectorRun,
        units: tuple[ProjectDirectorUnit, ...],
        *,
        confirm_new_input: bool = False,
        expected_latest_run_id: UUID | None = None,
        auto_input_change_audit: dict[str, object] | None = None,
    ) -> ProjectDirectorRun:
        try:
            with self._uow() as session:
                return self.create_run_in_session(
                    session,
                    run,
                    units,
                    confirm_new_input=confirm_new_input,
                    expected_latest_run_id=expected_latest_run_id,
                    auto_input_change_audit=auto_input_change_audit,
                )
        except IntegrityError as exc:
            existing = self.find_run_by_key(run.owner_id, run.idempotency_key)
            if (
                existing is not None
                and existing.canonical_input_sha256 == run.canonical_input_sha256
            ):
                return existing
            raise DomainError("idempotency_conflict", "Project director run conflicts") from exc

    def create_run_in_session(
        self,
        session: Session,
        run: ProjectDirectorRun,
        units: tuple[ProjectDirectorUnit, ...],
        *,
        confirm_new_input: bool = False,
        expected_latest_run_id: UUID | None = None,
        auto_input_change_audit: dict[str, object] | None = None,
    ) -> ProjectDirectorRun:
        _lock_active_project(session, run.owner_id, run.project_id)
        existing = session.scalar(
            select(ProjectDirectorRunRow).where(
                ProjectDirectorRunRow.owner_id == run.owner_id,
                ProjectDirectorRunRow.idempotency_key == run.idempotency_key,
            )
        )
        if existing is not None:
            value = _run(existing)
            if value.canonical_input_sha256 != run.canonical_input_sha256:
                raise DomainError("idempotency_conflict", "Project director run conflicts")
            return value
        blocking_rows = session.scalars(
            select(ProjectDirectorRunRow)
            .where(
                ProjectDirectorRunRow.owner_id == run.owner_id,
                ProjectDirectorRunRow.project_id == run.project_id,
                or_(
                    ProjectDirectorRunRow.status.in_(
                        (
                            ProjectDirectorRunStatus.INTENT_PERSISTED.value,
                            ProjectDirectorRunStatus.RUNNING.value,
                            ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN.value,
                        )
                    ),
                    ProjectDirectorRunRow.retry_blocked.is_(True),
                ),
            )
            .order_by(ProjectDirectorRunRow.created_at.desc(), ProjectDirectorRunRow.id.desc())
            .with_for_update()
        ).all()
        stored_units = units
        audit: dict[str, object] | None = None
        if run.trigger in {
            ProjectDirectorTrigger.MANUAL_REGENERATE,
            ProjectDirectorTrigger.AUTO_RESUME,
        }:
            source_row = session.scalar(
                select(ProjectDirectorRunRow)
                .where(
                    ProjectDirectorRunRow.owner_id == run.owner_id,
                    ProjectDirectorRunRow.project_id == run.project_id,
                )
                .order_by(ProjectDirectorRunRow.created_at.desc(), ProjectDirectorRunRow.id.desc())
                .limit(1)
                .with_for_update()
            )
            in_flight = session.scalar(
                select(ProjectDirectorUnitRow.id)
                .where(
                    ProjectDirectorUnitRow.owner_id == run.owner_id,
                    ProjectDirectorUnitRow.project_id == run.project_id,
                    ProjectDirectorUnitRow.status.in_(
                        (
                            ProjectDirectorUnitStatus.LEASED.value,
                            ProjectDirectorUnitStatus.SUBMITTING.value,
                        )
                    ),
                )
                .limit(1)
            )
            if in_flight is not None:
                raise DomainError("director_run_unresolved", "A director unit is still in progress")
            source_unit_rows = (
                session.scalars(
                    select(ProjectDirectorUnitRow)
                    .where(ProjectDirectorUnitRow.run_id == source_row.id)
                    .order_by(ProjectDirectorUnitRow.ordinal)
                    .with_for_update()
                ).all()
                if source_row is not None
                else []
            )
            run, stored_units, closed_source = prepare_manual_reprocessing(
                run,
                units,
                _run(source_row) if source_row is not None else None,
                tuple(_unit(item) for item in source_unit_rows),
                tuple(_run(item) for item in blocking_rows),
                confirm_new_input=confirm_new_input,
                expected_latest_run_id=expected_latest_run_id,
            )
            if closed_source is not None:
                assert source_row is not None
                audit = {
                    "action": "manual_evidence_waived",
                    "actor_id": str(run.owner_id),
                    "occurred_at": run.created_at.isoformat(),
                    "source_run_id": str(source_row.id),
                    "new_run_id": str(run.run_id),
                    "source_failure_code": source_row.failure_code,
                    "source_canonical_input_sha256": source_row.canonical_input_sha256,
                    "new_canonical_input_sha256": run.canonical_input_sha256,
                    "confirm_new_input": confirm_new_input,
                    "unknown_unit_ids": [
                        str(item.id)
                        for item in source_unit_rows
                        if item.status == ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN.value
                    ],
                }
                source_row.status = closed_source.status.value
                source_row.failure_code = closed_source.failure_code
                source_row.state_revision = closed_source.state_revision
                for item in source_unit_rows:
                    if item.status == ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN.value:
                        item.status = ProjectDirectorUnitStatus.FAILED.value
                        item.state_revision += 1
        elif blocking_rows:
            blocking = blocking_rows[0]
            code = "director_retry_blocked" if blocking.retry_blocked else "director_run_unresolved"
            raise DomainError(code, "Project director run is unresolved")
        row = _run_row(run)
        row.manual_recovery_audit_json = audit
        row.auto_input_change_audit_json = auto_input_change_audit
        session.add(row)
        session.add_all(_unit_row(item) for item in stored_units)
        session.add(
            StoryboardOutboxRow(
                id=run.run_id,
                owner_id=run.owner_id,
                project_id=run.project_id,
                aggregate_type="PROJECT_DIRECTOR",
                aggregate_id=run.run_id,
                event_type="PROJECT_DIRECTOR_RUN_REQUESTED",
                request_sha256=run.canonical_input_sha256,
                workflow_id=f"project-director-{run.owner_id}-{run.run_id}",
                state="PENDING",
                next_attempt_at=run.created_at,
                created_at=run.created_at,
            )
        )
        session.flush()
        return run

    def get_run(self, owner_id: UUID, run_id: UUID) -> ProjectDirectorRun | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDirectorRunRow).where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.id == run_id,
                )
            )
            return _run(row) if row is not None else None

    def latest_run(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRun | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDirectorRunRow)
                .where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.project_id == project_id,
                )
                .order_by(ProjectDirectorRunRow.created_at.desc(), ProjectDirectorRunRow.id.desc())
                .limit(1)
            )
            return _run(row) if row is not None else None

    def next_claimable_unit(
        self, owner_id: UUID, run_id: UUID, now: datetime
    ) -> ProjectDirectorUnit | None:
        with self._uow() as session:
            run = session.scalar(
                select(ProjectDirectorRunRow).where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.id == run_id,
                )
            )
            if run is None or run.status not in {
                ProjectDirectorRunStatus.INTENT_PERSISTED.value,
                ProjectDirectorRunStatus.RUNNING.value,
            }:
                return None
            row = session.scalar(
                select(ProjectDirectorUnitRow)
                .where(
                    ProjectDirectorUnitRow.owner_id == owner_id,
                    ProjectDirectorUnitRow.run_id == run_id,
                    or_(
                        and_(
                            ProjectDirectorUnitRow.status
                            == ProjectDirectorUnitStatus.INTENT_PERSISTED.value,
                            or_(
                                ProjectDirectorUnitRow.next_attempt_at.is_(None),
                                ProjectDirectorUnitRow.next_attempt_at <= now,
                            ),
                            or_(
                                ProjectDirectorUnitRow.kind
                                != ProjectDirectorUnitKind.SHOT_PLAN.value,
                                ProjectDirectorUnitRow.plan_document_sha256.isnot(None),
                            ),
                        ),
                        and_(
                            ProjectDirectorUnitRow.status == ProjectDirectorUnitStatus.LEASED.value,
                            ProjectDirectorUnitRow.lease_until.isnot(None),
                            ProjectDirectorUnitRow.lease_until < now,
                        ),
                    ),
                )
                .order_by(ProjectDirectorUnitRow.ordinal)
                .limit(1)
            )
            return _unit(row) if row is not None else None

    def find_stale_submitting_unit(
        self, owner_id: UUID, run_id: UUID, now: datetime
    ) -> ProjectDirectorUnit | None:
        with self._uow() as session:
            run = session.scalar(
                select(ProjectDirectorRunRow).where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.id == run_id,
                )
            )
            if run is None or run.status not in {
                ProjectDirectorRunStatus.INTENT_PERSISTED.value,
                ProjectDirectorRunStatus.RUNNING.value,
            }:
                return None
            row = session.scalar(
                select(ProjectDirectorUnitRow)
                .where(
                    ProjectDirectorUnitRow.owner_id == owner_id,
                    ProjectDirectorUnitRow.run_id == run_id,
                    ProjectDirectorUnitRow.status == ProjectDirectorUnitStatus.SUBMITTING.value,
                    ProjectDirectorUnitRow.lease_until.isnot(None),
                    ProjectDirectorUnitRow.lease_until < now,
                )
                .order_by(ProjectDirectorUnitRow.ordinal)
                .limit(1)
            )
            return _unit(row) if row is not None else None

    def reclaim_expired_units(
        self, owner_id: UUID, project_id: UUID, *, now: datetime
    ) -> tuple[UUID, ...]:
        """恢复创建前的孤儿单元回收：收尾终态运行中租约已过期的在途单元。

        背景（2026-10-14 缺陷）：worker 崩溃、或运行因并发单元先到终态而被工作流
        放弃后，遗留的 LEASED/SUBMITTING 单元不会再被任何流程回收（stale 回收只在
        非终态运行的 advance 内执行），却会永久阻塞新一轮恢复创建
        （director_run_unresolved）。本方法只处理「所属 run 已终态
        （FAILED/REMOTE_STATE_UNKNOWN）」且租约确已过期的单元：
        - SUBMITTING（远端调用可能已发出）：按 worker 丢失语义落 FAILED，
          失败码 `worker_lost_after_call_started`，不伪造“远端未受理”；
        - LEASED（调用从未开始，attempt_count 未递增）：回退 INTENT_PERSISTED。
        活跃运行（INTENT_PERSISTED/RUNNING）中的单元一律不动，仍由 advance 的
        stale 路径负责回收。返回被回收单元 id。
        """
        reclaimed: list[UUID] = []
        with self._uow() as session:
            _lock_active_project(session, owner_id, project_id)
            rows = session.scalars(
                select(ProjectDirectorUnitRow)
                .join(
                    ProjectDirectorRunRow,
                    ProjectDirectorRunRow.id == ProjectDirectorUnitRow.run_id,
                )
                .where(
                    ProjectDirectorUnitRow.owner_id == owner_id,
                    ProjectDirectorUnitRow.project_id == project_id,
                    ProjectDirectorUnitRow.status.in_(
                        (
                            ProjectDirectorUnitStatus.LEASED.value,
                            ProjectDirectorUnitStatus.SUBMITTING.value,
                        )
                    ),
                    ProjectDirectorUnitRow.lease_until.isnot(None),
                    ProjectDirectorUnitRow.lease_until < now,
                    ProjectDirectorRunRow.status.in_(
                        (
                            ProjectDirectorRunStatus.FAILED.value,
                            ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN.value,
                        )
                    ),
                )
                .order_by(ProjectDirectorUnitRow.run_id, ProjectDirectorUnitRow.ordinal)
                .with_for_update()
            ).all()
            for row in rows:
                if row.status == ProjectDirectorUnitStatus.SUBMITTING.value:
                    row.status = ProjectDirectorUnitStatus.FAILED.value
                    row.failure_code = PROJECT_DIRECTOR_WORKER_LOST_FAILURE[:128]
                    row.last_failure_code = PROJECT_DIRECTOR_WORKER_LOST_FAILURE[:128]
                else:
                    row.status = ProjectDirectorUnitStatus.INTENT_PERSISTED.value
                row.lease_token = None
                row.lease_until = None
                row.next_attempt_at = None
                row.state_revision += 1
                reclaimed.append(row.id)
            return tuple(reclaimed)

    def claim_unit(
        self,
        owner_id: UUID,
        run_id: UUID,
        unit_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
    ) -> ProjectDirectorUnit | None:
        with self._uow() as session:
            run = session.scalar(
                select(ProjectDirectorRunRow)
                .where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.id == run_id,
                )
                .with_for_update()
            )
            if run is not None:
                _lock_active_project(session, owner_id, run.project_id)
            row = session.scalar(
                select(ProjectDirectorUnitRow)
                .where(
                    ProjectDirectorUnitRow.owner_id == owner_id,
                    ProjectDirectorUnitRow.run_id == run_id,
                    ProjectDirectorUnitRow.id == unit_id,
                )
                .with_for_update()
            )
            if (
                run is None
                or row is None
                or run.status
                not in {
                    ProjectDirectorRunStatus.INTENT_PERSISTED.value,
                    ProjectDirectorRunStatus.RUNNING.value,
                }
            ):
                return None
            claimable_intent = (
                row.status == ProjectDirectorUnitStatus.INTENT_PERSISTED.value
                and (row.next_attempt_at is None or row.next_attempt_at <= now)
                and (
                    row.kind != ProjectDirectorUnitKind.SHOT_PLAN.value
                    or row.plan_document_sha256 is not None
                )
            )
            claimable_expired_lease = (
                row.status == ProjectDirectorUnitStatus.LEASED.value
                and row.lease_until is not None
                and row.lease_until < now
            )
            if not (claimable_intent or claimable_expired_lease):
                return None
            row.status = ProjectDirectorUnitStatus.LEASED.value
            row.lease_token = token
            row.lease_until = lease_until
            row.state_revision += 1
            run.status = ProjectDirectorRunStatus.RUNNING.value
            run.state_revision += 1
            session.flush()
            return _unit(row)

    def mark_unit_call_started(self, unit: ProjectDirectorUnit) -> ProjectDirectorUnit | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDirectorUnitRow)
                .where(
                    ProjectDirectorUnitRow.owner_id == unit.owner_id,
                    ProjectDirectorUnitRow.id == unit.unit_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.status != ProjectDirectorUnitStatus.LEASED.value
                or row.state_revision != unit.state_revision
                or row.lease_token != unit.lease_token
            ):
                return None
            row.status = ProjectDirectorUnitStatus.SUBMITTING.value
            row.attempt_count += 1
            row.state_revision += 1
            session.flush()
            return _unit(row)

    def complete_unit(
        self,
        unit: ProjectDirectorUnit,
        shots: tuple[DirectorShot, ...],
        links: tuple[ShotSentenceLink, ...],
        receipt: str,
        raw_response_sha256: str,
    ) -> ProjectDirectorRun:
        with self._uow() as session:
            run, row = self._locked_run_unit(session, unit)
            if row.status == ProjectDirectorUnitStatus.SUCCEEDED.value:
                return _run(run)
            deterministic_separator = (
                row.kind == ProjectDirectorUnitKind.NONVISUAL_SEPARATOR.value
                and row.status == ProjectDirectorUnitStatus.LEASED.value
                and not shots
                and not links
                and receipt == _DETERMINISTIC_SEPARATOR_RECEIPT
            )
            if (
                row.status != ProjectDirectorUnitStatus.SUBMITTING.value
                and not deterministic_separator
            ) or (row.state_revision != unit.state_revision or row.lease_token != unit.lease_token):
                return _run(run)
            row.status = ProjectDirectorUnitStatus.SUCCEEDED.value
            row.state_revision += 1
            row.lease_token = None
            row.lease_until = None
            row.provider_receipt = receipt
            row.raw_response_sha256 = raw_response_sha256
            row.shots_json = [_shot_json(item) for item in shots]
            row.links_json = [_link_json(item) for item in links]
            row.failure_code = None
            row.last_failure_code = None
            row.next_attempt_at = None
            run.state_revision += 1
            session.flush()
            return _run(run)

    def complete_plan_unit(
        self,
        unit: ProjectDirectorUnit,
        plan_document: str,
        receipt: str,
        raw_response_sha256: str,
        bindings: tuple[ProjectDirectorUnitBinding, ...],
    ) -> ProjectDirectorRun:
        with self._uow() as session:
            run, row = self._locked_run_unit(session, unit)
            if row.status == ProjectDirectorUnitStatus.SUCCEEDED.value:
                return _run(run)
            if (
                row.status != ProjectDirectorUnitStatus.SUBMITTING.value
                or row.state_revision != unit.state_revision
                or row.lease_token != unit.lease_token
            ):
                return _run(run)
            binding_rows: list[ProjectDirectorUnitRow] = []
            for binding in bindings:
                target = session.scalar(
                    select(ProjectDirectorUnitRow)
                    .where(
                        ProjectDirectorUnitRow.owner_id == unit.owner_id,
                        ProjectDirectorUnitRow.run_id == unit.run_id,
                        ProjectDirectorUnitRow.id == binding.unit_id,
                    )
                    .with_for_update()
                )
                if (
                    target is None
                    or target.status != ProjectDirectorUnitStatus.INTENT_PERSISTED.value
                    or target.plan_document_sha256 is not None
                ):
                    raise DomainError(
                        "director_plan_bind_conflict",
                        "Project director plan binding target changed",
                    )
                binding_rows.append(target)
            row.status = ProjectDirectorUnitStatus.SUCCEEDED.value
            row.state_revision += 1
            row.lease_token = None
            row.lease_until = None
            row.provider_receipt = receipt
            row.raw_response_sha256 = raw_response_sha256
            row.plan_document = plan_document
            # Every run owns at least one shot plan unit, so bindings is never empty;
            # all bindings share the plan document hash supplied by the service.
            row.plan_document_sha256 = bindings[0].plan_document_sha256 if bindings else None
            row.failure_code = None
            row.last_failure_code = None
            row.next_attempt_at = None
            for binding, target in zip(bindings, binding_rows, strict=True):
                target.prompt_content = binding.prompt_content
                target.prompt_sha256 = binding.prompt_sha256
                target.canonical_request_sha256 = binding.canonical_request_sha256
                target.plan_document_sha256 = binding.plan_document_sha256
                target.state_revision += 1
            run.state_revision += 1
            session.flush()
            return _run(run)

    def schedule_unit_retry(
        self,
        unit: ProjectDirectorUnit,
        *,
        failure_code: str,
        next_attempt_at: datetime,
        provider_receipt: str | None = None,
        raw_response_sha256: str | None = None,
        response_diagnostics: dict[str, str | int | bool] | None = None,
    ) -> ProjectDirectorRun | None:
        with self._uow() as session:
            # §15.2/§5.16：整轮 2× 自动重试总上限只对 MANUAL 项目生效；AUTO 项目
            # 不设整轮上限（每单元 3 次上限由应用层控制），计数仍照常累加。
            updated = cast(
                CursorResult[object],
                session.execute(
                    update(ProjectDirectorRunRow)
                    .where(
                        ProjectDirectorRunRow.owner_id == unit.owner_id,
                        ProjectDirectorRunRow.id == unit.run_id,
                        or_(
                            ProjectDirectorRunRow.auto_retries_used
                            < 2 * ProjectDirectorRunRow.total_units,
                            select(ProjectRow.id)
                            .where(
                                ProjectRow.id == ProjectDirectorRunRow.project_id,
                                ProjectRow.production_mode == "AUTO",
                            )
                            .exists(),
                        ),
                    )
                    .values(
                        auto_retries_used=ProjectDirectorRunRow.auto_retries_used + 1,
                        state_revision=ProjectDirectorRunRow.state_revision + 1,
                    )
                ),
            )
            if updated.rowcount != 1:
                session.rollback()
                return None
            run = session.scalar(
                select(ProjectDirectorRunRow)
                .where(
                    ProjectDirectorRunRow.owner_id == unit.owner_id,
                    ProjectDirectorRunRow.id == unit.run_id,
                )
                .with_for_update()
            )
            row = session.scalar(
                select(ProjectDirectorUnitRow)
                .where(
                    ProjectDirectorUnitRow.owner_id == unit.owner_id,
                    ProjectDirectorUnitRow.id == unit.unit_id,
                )
                .with_for_update()
            )
            if run is None or row is None:
                raise DomainError("not_found", "Project director unit was not found")
            row.status = ProjectDirectorUnitStatus.INTENT_PERSISTED.value
            row.state_revision += 1
            row.lease_token = None
            row.lease_until = None
            row.provider_receipt = provider_receipt
            row.raw_response_sha256 = raw_response_sha256
            row.response_diagnostics_json = dict(response_diagnostics or {})
            row.last_failure_code = failure_code[:128]
            row.next_attempt_at = next_attempt_at
            session.flush()
            return _run(run)

    def fail_unit(
        self,
        unit: ProjectDirectorUnit,
        *,
        failure_code: str,
        remote_state_unknown: bool,
        provider_receipt: str | None = None,
        raw_response_sha256: str | None = None,
        response_diagnostics: dict[str, str | int | bool] | None = None,
    ) -> ProjectDirectorRun:
        status = (
            ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN
            if remote_state_unknown
            else ProjectDirectorUnitStatus.FAILED
        )
        run_status = (
            ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
            if remote_state_unknown
            else ProjectDirectorRunStatus.FAILED
        )
        with self._uow() as session:
            run, row = self._locked_run_unit(session, unit)
            if row.status in {
                ProjectDirectorUnitStatus.SUCCEEDED.value,
                ProjectDirectorUnitStatus.FAILED.value,
                ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN.value,
            }:
                return _run(run)
            row.status = status.value
            row.state_revision += 1
            row.lease_token = None
            row.lease_until = None
            row.failure_code = failure_code[:128]
            row.last_failure_code = failure_code[:128]
            row.next_attempt_at = None
            row.provider_receipt = provider_receipt
            row.raw_response_sha256 = raw_response_sha256
            row.response_diagnostics_json = dict(response_diagnostics or {})
            if run.status not in {
                ProjectDirectorRunStatus.SUCCEEDED.value,
                ProjectDirectorRunStatus.FAILED.value,
                ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN.value,
            }:
                run.status = run_status.value
                run.failure_code = failure_code[:128]
                run.state_revision += 1
            session.flush()
            return _run(run)

    def list_units(self, owner_id: UUID, run_id: UUID) -> tuple[ProjectDirectorUnit, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(ProjectDirectorUnitRow)
                .where(
                    ProjectDirectorUnitRow.owner_id == owner_id,
                    ProjectDirectorUnitRow.run_id == run_id,
                )
                .order_by(ProjectDirectorUnitRow.ordinal)
            ).all()
            return tuple(_unit(row) for row in rows)

    def complete_run(
        self, run: ProjectDirectorRun, revision: ProjectDirectorRevision
    ) -> ProjectDirectorRun:
        try:
            with self._uow() as session:
                _lock_active_project(session, run.owner_id, run.project_id)
                row = session.scalar(
                    select(ProjectDirectorRunRow)
                    .where(
                        ProjectDirectorRunRow.owner_id == run.owner_id,
                        ProjectDirectorRunRow.id == run.run_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise DomainError("not_found", "Project director run was not found")
                if row.status == ProjectDirectorRunStatus.SUCCEEDED.value:
                    return _run(row)
                successful = cast(
                    int,
                    session.scalar(
                        select(func.count(ProjectDirectorUnitRow.id)).where(
                            ProjectDirectorUnitRow.run_id == run.run_id,
                            ProjectDirectorUnitRow.status
                            == ProjectDirectorUnitStatus.SUCCEEDED.value,
                        )
                    ),
                )
                if successful != row.total_units:
                    return _run(row)
                revision_number = (
                    cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(
                                    func.max(ProjectDirectorRevisionRow.revision_number), 0
                                )
                            ).where(
                                ProjectDirectorRevisionRow.owner_id == run.owner_id,
                                ProjectDirectorRevisionRow.project_id == run.project_id,
                            )
                        ),
                    )
                    + 1
                )
                stored = replace(revision, revision_number=revision_number)
                session.add(_revision_row(stored))
                session.flush()
                row.status = ProjectDirectorRunStatus.SUCCEEDED.value
                row.revision_id = stored.revision_id
                row.failure_code = None
                row.state_revision += 1
                pointer = session.get(ProjectDirectorCurrentRow, (run.owner_id, run.project_id))
                if pointer is None:
                    session.add(
                        ProjectDirectorCurrentRow(
                            owner_id=run.owner_id,
                            project_id=run.project_id,
                            revision_id=stored.revision_id,
                            pointer_version=1,
                        )
                    )
                else:
                    pointer.revision_id = stored.revision_id
                    pointer.pointer_version += 1
                session.flush()
                return _run(row)
        except IntegrityError:
            current = self.get_run(run.owner_id, run.run_id)
            if current is not None and current.status is ProjectDirectorRunStatus.SUCCEEDED:
                return current
            raise

    def resolve_unknown(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        failure_code: str,
        retry_blocked: bool,
    ) -> ProjectDirectorRun:
        with self._uow() as session:
            run = session.scalar(
                select(ProjectDirectorRunRow)
                .where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.id == run_id,
                )
                .with_for_update()
            )
            if run is None:
                raise DomainError("not_found", "Project director run was not found")
            if (
                run.status == ProjectDirectorRunStatus.FAILED.value
                and run.failure_code == failure_code
                and run.retry_blocked is retry_blocked
            ):
                return _run(run)
            if run.status != ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN.value:
                raise DomainError(
                    "reconciliation_not_allowed", "Project director run is not uncertain"
                )
            # Only v4 runs can reach manual resolution, so unit rows here always
            # carry the v4 shape; the row is updated directly without deserializing.
            unit = session.scalar(
                select(ProjectDirectorUnitRow)
                .where(
                    ProjectDirectorUnitRow.run_id == run_id,
                    ProjectDirectorUnitRow.status
                    == ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN.value,
                )
                .order_by(ProjectDirectorUnitRow.ordinal)
                .limit(1)
                .with_for_update()
            )
            if unit is None:
                raise DomainError(
                    "reconciliation_not_allowed", "Project director unit is not uncertain"
                )
            unit.status = ProjectDirectorUnitStatus.FAILED.value
            unit.failure_code = failure_code[:128]
            unit.state_revision += 1
            run.status = ProjectDirectorRunStatus.FAILED.value
            run.failure_code = failure_code[:128]
            run.retry_blocked = retry_blocked
            run.state_revision += 1
            session.flush()
            return _run(run)

    def run_progress(self, owner_id: UUID, run_id: UUID) -> ProjectDirectorRunProgress:
        # Aggregate-only read: unit payloads are never deserialized here because
        # immutable pre-v4 rows may not satisfy the v4 domain contract.
        with self._uow() as session:
            run = session.scalar(
                select(ProjectDirectorRunRow).where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.id == run_id,
                )
            )
            if run is None:
                raise DomainError("not_found", "Project director run was not found")
            status_counts: dict[str, int] = {
                cast(str, status): cast(int, count)
                for status, count in session.execute(
                    select(ProjectDirectorUnitRow.status, func.count())
                    .where(ProjectDirectorUnitRow.run_id == run_id)
                    .group_by(ProjectDirectorUnitRow.status)
                ).all()
            }
            unsent = cast(
                int,
                session.scalar(
                    select(func.count(ProjectDirectorUnitRow.id)).where(
                        ProjectDirectorUnitRow.run_id == run_id,
                        ProjectDirectorUnitRow.status
                        == ProjectDirectorUnitStatus.INTENT_PERSISTED.value,
                        ProjectDirectorUnitRow.attempt_count == 0,
                    )
                ),
            )
            retry_pending = cast(
                int,
                session.scalar(
                    select(func.count(ProjectDirectorUnitRow.id)).where(
                        ProjectDirectorUnitRow.run_id == run_id,
                        ProjectDirectorUnitRow.status
                        == ProjectDirectorUnitStatus.INTENT_PERSISTED.value,
                        ProjectDirectorUnitRow.attempt_count > 0,
                    )
                ),
            )
            failed = session.execute(
                select(ProjectDirectorUnitRow.ordinal, ProjectDirectorUnitRow.kind)
                .where(
                    ProjectDirectorUnitRow.run_id == run_id,
                    ProjectDirectorUnitRow.status.in_(
                        (
                            ProjectDirectorUnitStatus.FAILED.value,
                            ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN.value,
                        )
                    ),
                )
                .order_by(ProjectDirectorUnitRow.ordinal)
                .limit(1)
            ).first()
            return ProjectDirectorRunProgress(
                succeeded_units=status_counts.get(ProjectDirectorUnitStatus.SUCCEEDED.value, 0),
                inflight_units=status_counts.get(ProjectDirectorUnitStatus.LEASED.value, 0)
                + status_counts.get(ProjectDirectorUnitStatus.SUBMITTING.value, 0),
                retry_pending_units=retry_pending,
                unsent_units=unsent,
                failed_unit_ordinal=failed[0] if failed is not None else None,
                failed_unit_kind=(
                    ProjectDirectorUnitKind(cast(str, failed[1])) if failed is not None else None
                ),
                auto_retries_used=run.auto_retries_used,
                auto_retry_ceiling=2 * run.total_units,
            )

    def current_revision(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRevision | None:
        with self._uow() as session:
            pointer = session.get(ProjectDirectorCurrentRow, (owner_id, project_id))
            if pointer is None or pointer.revision_id is None:
                return None
            row = session.scalar(
                select(ProjectDirectorRevisionRow).where(
                    ProjectDirectorRevisionRow.owner_id == owner_id,
                    ProjectDirectorRevisionRow.id == pointer.revision_id,
                )
            )
            return _revision(row) if row is not None else None

    def materialization_groups(
        self,
        owner_id: UUID,
        project_id: UUID,
        snapshot_sha256: str,
        snapshot_revision: int,
    ) -> tuple[ProjectDirectorGroupContext, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(AuthoritativeNarrationGroupRecord)
                .where(
                    AuthoritativeNarrationGroupRecord.owner_id == owner_id,
                    AuthoritativeNarrationGroupRecord.project_id == project_id,
                    AuthoritativeNarrationGroupRecord.snapshot_sha256 == snapshot_sha256,
                    AuthoritativeNarrationGroupRecord.snapshot_revision == snapshot_revision,
                )
                .order_by(AuthoritativeNarrationGroupRecord.sequence)
            ).all()
            memberships_by_group: dict[UUID, list[AuthoritativeNarrationMembershipRecord]] = {}
            sentence_ids: set[UUID] = set()
            for row in rows:
                memberships = list(
                    session.scalars(
                        select(AuthoritativeNarrationMembershipRecord)
                        .where(AuthoritativeNarrationMembershipRecord.group_id == row.id)
                        .order_by(AuthoritativeNarrationMembershipRecord.ordinal)
                    ).all()
                )
                memberships_by_group[row.id] = memberships
                sentence_ids.update(item.sentence_id for item in memberships)
            # 2026-10-16：随组上下文携带句级实测时间轴（monotone-projection-v1
            # 可行性检查输入）；句子与组在同一权威写入中落库，缺失即数据不一致。
            ranges_by_id: dict[UUID, tuple[int, int]] = (
                {
                    sentence.id: (sentence.start_ms, sentence.end_ms)
                    for sentence in session.scalars(
                        select(AuthoritativeNarrationSentenceRecord).where(
                            AuthoritativeNarrationSentenceRecord.id.in_(sentence_ids)
                        )
                    ).all()
                }
                if sentence_ids
                else {}
            )
            result: list[ProjectDirectorGroupContext] = []
            for row in rows:
                memberships = memberships_by_group[row.id]
                if any(item.sentence_id not in ranges_by_id for item in memberships):
                    raise DomainError("narration_not_ready", "Narration groups are inconsistent")
                result.append(
                    ProjectDirectorGroupContext(
                        row.id,
                        row.sequence,
                        tuple(item.sentence_id for item in memberships),
                        tuple(ranges_by_id[item.sentence_id] for item in memberships),
                    )
                )
            if result and [item.sequence for item in result] != list(range(1, len(result) + 1)):
                raise DomainError("narration_not_ready", "Narration groups are inconsistent")
            return tuple(result)

    def find_materialization(
        self, owner_id: UUID, canonical_hash: str
    ) -> ProjectDirectorMaterialization | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDirectorMaterializationRow).where(
                    ProjectDirectorMaterializationRow.owner_id == owner_id,
                    ProjectDirectorMaterializationRow.canonical_input_sha256 == canonical_hash,
                )
            )
            if row is not None and _candidate_row_count(session, row) == 0:
                logger.warning(
                    "ignoring degenerate director materialization %s (no candidates)", row.id
                )
                return None
            return _materialization(session, row) if row is not None else None

    def latest_materialization(
        self, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorMaterialization | None:
        with self._uow() as session:
            pointer = session.get(ProjectDirectorCurrentRow, (owner_id, project_id))
            if pointer is None or pointer.revision_id is None:
                return None
            row = session.scalar(
                select(ProjectDirectorMaterializationRow)
                .where(
                    ProjectDirectorMaterializationRow.owner_id == owner_id,
                    ProjectDirectorMaterializationRow.project_id == project_id,
                    ProjectDirectorMaterializationRow.preplan_revision_id == pointer.revision_id,
                )
                .order_by(
                    ProjectDirectorMaterializationRow.created_at.desc(),
                    ProjectDirectorMaterializationRow.id.desc(),
                )
                .limit(1)
            )
            if row is not None and _candidate_row_count(session, row) == 0:
                logger.warning(
                    "ignoring degenerate director materialization %s (no candidates)", row.id
                )
                return None
            return _materialization(session, row) if row is not None else None

    def persist_materialization(
        self, value: ProjectDirectorMaterialization
    ) -> ProjectDirectorMaterialization:
        try:
            with self._uow() as session:
                _lock_active_project(session, value.owner_id, value.project_id)
                existing = session.scalar(
                    select(ProjectDirectorMaterializationRow).where(
                        ProjectDirectorMaterializationRow.owner_id == value.owner_id,
                        ProjectDirectorMaterializationRow.canonical_input_sha256
                        == value.canonical_input_sha256,
                    )
                )
                if existing is not None:
                    if _candidate_row_count(session, existing) == 0:
                        # 历史不完整写入的孤儿行（父行无候选子行）：精确删除该对象后
                        # 按确定性结果重写；候选由分组与修订确定性推导，不丢业务内容。
                        logger.warning(
                            "deleting degenerate director materialization %s (no candidates)",
                            existing.id,
                        )
                        session.delete(existing)
                        session.flush()
                    else:
                        return _materialization(session, existing)
                session.add(
                    ProjectDirectorMaterializationRow(
                        id=value.materialization_id,
                        owner_id=value.owner_id,
                        project_id=value.project_id,
                        preplan_revision_id=value.preplan_revision_id,
                        snapshot_sha256=value.snapshot_sha256,
                        snapshot_revision=value.snapshot_revision,
                        algorithm_version=value.algorithm_version,
                        canonical_input_sha256=value.canonical_input_sha256,
                        status=value.status.value,
                        created_at=value.created_at,
                    )
                )
                # These models declare no relationships, so the unit of work has no
                # mapper-level dependency edge and does not guarantee parent-before-child
                # insert ordering; flush explicitly before inserting candidates.
                session.flush()
                stored_candidates: list[ProjectDirectorGroupCandidate] = []
                for candidate in value.candidates:
                    final = candidate.final_revision
                    if final is not None:
                        pointer = session.scalar(
                            select(DirectorPointerRow)
                            .where(
                                DirectorPointerRow.owner_id == value.owner_id,
                                DirectorPointerRow.group_id == candidate.group_id,
                            )
                            .with_for_update()
                        )
                        revision_number = (
                            cast(
                                int,
                                session.scalar(
                                    select(
                                        func.coalesce(
                                            func.max(DirectorRevisionRow.revision_number), 0
                                        )
                                    ).where(
                                        DirectorRevisionRow.owner_id == value.owner_id,
                                        DirectorRevisionRow.group_id == candidate.group_id,
                                    )
                                ),
                            )
                            + 1
                        )
                        final = replace(final, revision_number=revision_number)
                        session.add(_director_revision_row(final))
                        session.flush()
                        if pointer is None:
                            session.add(
                                DirectorPointerRow(
                                    owner_id=value.owner_id,
                                    group_id=candidate.group_id,
                                    draft_revision_id=final.revision_id,
                                    applied_revision_id=None,
                                )
                            )
                        else:
                            pointer.draft_revision_id = final.revision_id
                    candidate_row = ProjectDirectorGroupCandidateRow(
                        id=candidate.candidate_id,
                        materialization_id=value.materialization_id,
                        owner_id=value.owner_id,
                        project_id=value.project_id,
                        group_id=candidate.group_id,
                        group_sequence=candidate.group_sequence,
                        shots_json=[_shot_json(item) for item in candidate.shots],
                        links_json=[_link_json(item) for item in candidate.sentence_links],
                        source_shot_links_json=[
                            {
                                "source_shot_id": str(item.source_shot_id),
                                "candidate_shot_id": str(item.candidate_shot_id),
                            }
                            for item in candidate.source_shot_links
                        ],
                        conflicts_json=[_conflict_json(item) for item in candidate.conflicts],
                        review_state=candidate.review_state.value,
                        final_revision_id=final.revision_id if final else None,
                    )
                    session.add(candidate_row)
                    session.flush()
                    if final is not None:
                        session.add(
                            DirectorRevisionLineageRow(
                                revision_id=final.revision_id,
                                project_director_revision_id=value.preplan_revision_id,
                                materialization_id=value.materialization_id,
                                candidate_id=candidate.candidate_id,
                            )
                        )
                    stored_candidates.append(replace(candidate, final_revision=final))
                session.flush()
                return replace(value, candidates=tuple(stored_candidates))
        except IntegrityError:
            persisted = self.find_materialization(value.owner_id, value.canonical_input_sha256)
            if persisted is not None:
                return persisted
            raise

    @staticmethod
    def _locked_run_unit(
        session: Session, unit: ProjectDirectorUnit
    ) -> tuple[ProjectDirectorRunRow, ProjectDirectorUnitRow]:
        run = session.scalar(
            select(ProjectDirectorRunRow)
            .where(
                ProjectDirectorRunRow.owner_id == unit.owner_id,
                ProjectDirectorRunRow.id == unit.run_id,
            )
            .with_for_update()
        )
        row = session.scalar(
            select(ProjectDirectorUnitRow)
            .where(
                ProjectDirectorUnitRow.owner_id == unit.owner_id,
                ProjectDirectorUnitRow.id == unit.unit_id,
            )
            .with_for_update()
        )
        if run is None or row is None:
            raise DomainError("not_found", "Project director unit was not found")
        return run, row


def _lock_active_project(session: Session, owner_id: UUID, project_id: UUID) -> ProjectRow:
    row = session.scalar(
        select(ProjectRow)
        .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
        .with_for_update()
    )
    if row is None:
        raise DomainError("not_found", "Project was not found")
    if row.production_state != "ACTIVE":
        raise DomainError("project_purging", "Project is being permanently deleted")
    return row


def _sentence_json(value: ProjectDirectorSentenceSnapshot) -> dict[str, object]:
    return {
        "sentence_id": str(value.sentence_id),
        "sequence": value.sequence,
        "text": value.text,
        "text_sha256": value.text_sha256,
    }


def _sentence(value: dict[str, object]) -> ProjectDirectorSentenceSnapshot:
    return ProjectDirectorSentenceSnapshot(
        UUID(cast(str, value["sentence_id"])),
        cast(int, value["sequence"]),
        cast(str, value["text"]),
        cast(str, value["text_sha256"]),
    )


def _input_json(value: ProjectDirectorInputSnapshot) -> dict[str, object]:
    return {
        "owner_id": str(value.owner_id),
        "project_id": str(value.project_id),
        "snapshot_sha256": value.snapshot_sha256,
        "snapshot_revision": value.snapshot_revision,
        "sentences": [_sentence_json(item) for item in value.sentences],
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


def _input(value: dict[str, object]) -> ProjectDirectorInputSnapshot:
    return ProjectDirectorInputSnapshot(
        owner_id=UUID(cast(str, value["owner_id"])),
        project_id=UUID(cast(str, value["project_id"])),
        snapshot_sha256=cast(str, value["snapshot_sha256"]),
        snapshot_revision=cast(int, value["snapshot_revision"]),
        sentences=tuple(
            _sentence(item) for item in cast(list[dict[str, object]], value["sentences"])
        ),
        characters=tuple(
            CharacterAssetSnapshot(
                cast(int, item["ordinal"]),
                UUID(cast(str, item["character_image_id"])),
                UUID(cast(str, item["asset_id"])),
                cast(str, item["sha256"]),
                cast(str, item["media_type"]),
                cast(int, item["size_bytes"]),
            )
            for item in cast(list[dict[str, object]], value["characters"])
        ),
        visual_style=cast(str, value["visual_style"]),
        locale=cast(str, value["locale"]),
        aspect_ratio=cast(str, value["aspect_ratio"]),
    )


def _run_row(value: ProjectDirectorRun) -> ProjectDirectorRunRow:
    return ProjectDirectorRunRow(
        id=value.run_id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        trigger=value.trigger.value,
        idempotency_key=value.idempotency_key,
        canonical_input_sha256=value.canonical_input_sha256,
        input_json=_input_json(value.input_snapshot),
        snapshot_sha256=value.input_snapshot.snapshot_sha256,
        snapshot_revision=value.input_snapshot.snapshot_revision,
        model=value.model,
        parameters_json=dict(value.parameters),
        status=value.status.value,
        total_units=value.total_units,
        state_revision=value.state_revision,
        auto_retries_used=value.auto_retries_used,
        revision_id=value.revision_id,
        failure_code=value.failure_code,
        retry_blocked=value.retry_blocked,
        recovery_source_run_id=value.recovery_source_run_id,
        created_at=value.created_at,
    )


def _run(row: ProjectDirectorRunRow) -> ProjectDirectorRun:
    return ProjectDirectorRun(
        run_id=row.id,
        owner_id=row.owner_id,
        project_id=row.project_id,
        trigger=ProjectDirectorTrigger(row.trigger),
        idempotency_key=row.idempotency_key,
        canonical_input_sha256=row.canonical_input_sha256,
        input_snapshot=_input(row.input_json),
        model=row.model,
        parameters=cast(dict[str, str | int | float | bool], row.parameters_json),
        status=ProjectDirectorRunStatus(row.status),
        total_units=row.total_units,
        created_at=row.created_at,
        state_revision=row.state_revision,
        revision_id=row.revision_id,
        failure_code=row.failure_code,
        retry_blocked=row.retry_blocked,
        recovery_source_run_id=row.recovery_source_run_id,
        auto_retries_used=row.auto_retries_used,
    )


def _unit_row(value: ProjectDirectorUnit) -> ProjectDirectorUnitRow:
    return ProjectDirectorUnitRow(
        id=value.unit_id,
        run_id=value.run_id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        ordinal=value.ordinal,
        kind=value.kind.value,
        sentences_json=[_sentence_json(item) for item in value.sentences],
        context_before_json=[_sentence_json(item) for item in value.context_before],
        context_after_json=[_sentence_json(item) for item in value.context_after],
        provider_idempotency_key=value.provider_idempotency_key,
        canonical_request_sha256=value.canonical_request_sha256,
        prompt_ref=value.prompt_ref,
        prompt_sha256=value.prompt_sha256,
        prompt_content=value.prompt_content,
        required_locale=value.required_locale,
        model=value.model,
        parameters_json=dict(value.parameters),
        status=value.status.value,
        state_revision=value.state_revision,
        attempt_count=value.attempt_count,
        last_failure_code=value.last_failure_code,
        next_attempt_at=value.next_attempt_at,
        lease_token=value.lease_token,
        lease_until=value.lease_until,
        provider_receipt=value.provider_receipt,
        raw_response_sha256=value.raw_response_sha256,
        response_diagnostics_json={},
        shots_json=[_shot_json(item) for item in value.shots],
        links_json=[_link_json(item) for item in value.sentence_links],
        failure_code=value.failure_code,
        plan_document=value.plan_document,
        plan_document_sha256=value.plan_document_sha256,
        reused_from_unit_id=value.reused_from_unit_id,
        created_at=value.created_at,
    )


def _unit(row: ProjectDirectorUnitRow) -> ProjectDirectorUnit:
    return ProjectDirectorUnit(
        unit_id=row.id,
        run_id=row.run_id,
        owner_id=row.owner_id,
        project_id=row.project_id,
        ordinal=row.ordinal,
        kind=ProjectDirectorUnitKind(row.kind),
        sentences=tuple(_sentence(item) for item in row.sentences_json),
        context_before=tuple(_sentence(item) for item in row.context_before_json),
        context_after=tuple(_sentence(item) for item in row.context_after_json),
        provider_idempotency_key=row.provider_idempotency_key,
        canonical_request_sha256=row.canonical_request_sha256,
        prompt_ref=row.prompt_ref,
        prompt_sha256=row.prompt_sha256,
        prompt_content=row.prompt_content,
        required_locale=row.required_locale,
        model=row.model,
        parameters=cast(dict[str, str | int | float | bool], row.parameters_json),
        status=ProjectDirectorUnitStatus(row.status),
        created_at=row.created_at,
        state_revision=row.state_revision,
        attempt_count=row.attempt_count,
        last_failure_code=row.last_failure_code,
        next_attempt_at=row.next_attempt_at,
        lease_token=row.lease_token,
        lease_until=row.lease_until,
        provider_receipt=row.provider_receipt,
        raw_response_sha256=row.raw_response_sha256,
        shots=tuple(_shot(item) for item in row.shots_json),
        sentence_links=tuple(_link(item) for item in row.links_json),
        failure_code=row.failure_code,
        plan_document=row.plan_document,
        plan_document_sha256=row.plan_document_sha256,
        reused_from_unit_id=row.reused_from_unit_id,
    )


def _shot_json(value: DirectorShot) -> dict[str, object]:
    return {
        "shot_id": str(value.shot_id),
        "order": value.order,
        "scene": value.scene,
        "shot_size": value.shot_size,
        "camera_viewpoint": value.camera_viewpoint,
        "subject_action_outcome": value.subject_action_outcome,
        "narrative_function": value.narrative_function,
        "description": value.description,
        "semantic_event": value.semantic_event.value,
        "subject_focus": value.subject_focus.value,
        "time_relation": value.time_relation.value,
        "location_relation": value.location_relation.value,
    }


def _shot(value: dict[str, object]) -> DirectorShot:
    return DirectorShot(
        UUID(cast(str, value["shot_id"])),
        cast(int, value["order"]),
        cast(str, value["scene"]),
        cast(str, value["shot_size"]),
        cast(str, value["camera_viewpoint"]),
        cast(str, value["subject_action_outcome"]),
        cast(str, value["narrative_function"]),
        cast(str, value["description"]),
        DirectorSemanticEvent(cast(str, value.get("semantic_event", "UNSPECIFIED"))),
        DirectorSubjectFocus(cast(str, value.get("subject_focus", "UNSPECIFIED"))),
        DirectorTimeRelation(cast(str, value.get("time_relation", "UNSPECIFIED"))),
        DirectorLocationRelation(cast(str, value.get("location_relation", "UNSPECIFIED"))),
    )


def _link_json(value: ShotSentenceLink) -> dict[str, str]:
    return {"shot_id": str(value.shot_id), "sentence_id": str(value.sentence_id)}


def _link(value: dict[str, str]) -> ShotSentenceLink:
    return ShotSentenceLink(UUID(value["shot_id"]), UUID(value["sentence_id"]))


def _conflict_json(value: ProjectDirectorConflict) -> dict[str, object]:
    return {
        "code": value.code,
        "sentence_ids": [str(item) for item in value.sentence_ids],
        "shot_ids": [str(item) for item in value.shot_ids],
    }


def _conflict(value: dict[str, object]) -> ProjectDirectorConflict:
    return ProjectDirectorConflict(
        cast(str, value["code"]),
        tuple(UUID(item) for item in cast(list[str], value["sentence_ids"])),
        tuple(UUID(item) for item in cast(list[str], value["shot_ids"])),
    )


def _revision_row(value: ProjectDirectorRevision) -> ProjectDirectorRevisionRow:
    return ProjectDirectorRevisionRow(
        id=value.revision_id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        run_id=value.run_id,
        revision_number=value.revision_number,
        snapshot_sha256=value.snapshot_sha256,
        snapshot_revision=value.snapshot_revision,
        sentence_ids_json=[str(item) for item in value.sentence_ids],
        shots_json=[_shot_json(item) for item in value.shots],
        links_json=[_link_json(item) for item in value.sentence_links],
        quality_conflicts_json=[_conflict_json(item) for item in value.quality_conflicts],
        created_at=value.created_at,
    )


def _revision(row: ProjectDirectorRevisionRow) -> ProjectDirectorRevision:
    return ProjectDirectorRevision(
        revision_id=row.id,
        owner_id=row.owner_id,
        project_id=row.project_id,
        run_id=row.run_id,
        revision_number=row.revision_number,
        snapshot_sha256=row.snapshot_sha256,
        snapshot_revision=row.snapshot_revision,
        sentence_ids=tuple(UUID(item) for item in row.sentence_ids_json),
        shots=tuple(_shot(item) for item in row.shots_json),
        sentence_links=tuple(_link(item) for item in row.links_json),
        quality_conflicts=tuple(_conflict(item) for item in row.quality_conflicts_json),
        created_at=row.created_at,
    )


def _director_revision_row(value: DirectorPlanRevision) -> DirectorRevisionRow:
    return DirectorRevisionRow(
        id=value.revision_id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        group_id=value.group_id,
        revision_number=value.revision_number,
        kind=value.kind.value,
        shots_json=[_shot_json(item) for item in value.shots],
        links_json=[_link_json(item) for item in value.sentence_links],
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


def _director_revision(row: DirectorRevisionRow) -> DirectorPlanRevision:
    return DirectorPlanRevision(
        revision_id=row.id,
        owner_id=row.owner_id,
        project_id=row.project_id,
        group_id=row.group_id,
        revision_number=row.revision_number,
        kind=DirectorRevisionKind(row.kind),
        shots=tuple(_shot(item) for item in row.shots_json),
        sentence_links=tuple(_link(item) for item in row.links_json),
        group_sentence_ids=tuple(UUID(item) for item in row.group_sentence_ids_json),
        review_state=ReviewState(row.review_state),
        continuous_observation_approvals=tuple(
            ContinuousObservationApproval(
                UUID(item["previous_shot_id"]), UUID(item["next_shot_id"]), item["reason"]
            )
            for item in row.continuous_observation_approvals_json
        ),
        created_at=row.created_at,
    )


def _candidate_row_count(session: Session, row: ProjectDirectorMaterializationRow) -> int:
    return int(
        session.scalar(
            select(func.count(ProjectDirectorGroupCandidateRow.id)).where(
                ProjectDirectorGroupCandidateRow.materialization_id == row.id
            )
        )
        or 0
    )


def _materialization(
    session: Session, row: ProjectDirectorMaterializationRow
) -> ProjectDirectorMaterialization:
    candidate_rows = session.scalars(
        select(ProjectDirectorGroupCandidateRow)
        .where(ProjectDirectorGroupCandidateRow.materialization_id == row.id)
        .order_by(ProjectDirectorGroupCandidateRow.group_sequence)
    ).all()
    candidates: list[ProjectDirectorGroupCandidate] = []
    for item in candidate_rows:
        final = (
            session.get(DirectorRevisionRow, item.final_revision_id)
            if item.final_revision_id
            else None
        )
        candidates.append(
            ProjectDirectorGroupCandidate(
                candidate_id=item.id,
                group_id=item.group_id,
                group_sequence=item.group_sequence,
                shots=tuple(_shot(value) for value in item.shots_json),
                sentence_links=tuple(_link(value) for value in item.links_json),
                source_shot_links=tuple(
                    ProjectDirectorSourceShotLink(
                        UUID(value["source_shot_id"]),
                        UUID(value["candidate_shot_id"]),
                    )
                    for value in item.source_shot_links_json
                ),
                conflicts=tuple(_conflict(value) for value in item.conflicts_json),
                review_state=ReviewState(item.review_state),
                final_revision=_director_revision(final) if final is not None else None,
            )
        )
    return ProjectDirectorMaterialization(
        materialization_id=row.id,
        owner_id=row.owner_id,
        project_id=row.project_id,
        preplan_revision_id=row.preplan_revision_id,
        snapshot_sha256=row.snapshot_sha256,
        snapshot_revision=row.snapshot_revision,
        algorithm_version=row.algorithm_version,
        canonical_input_sha256=row.canonical_input_sha256,
        status=MaterializationStatus(row.status),
        candidates=tuple(candidates),
        created_at=row.created_at,
    )
