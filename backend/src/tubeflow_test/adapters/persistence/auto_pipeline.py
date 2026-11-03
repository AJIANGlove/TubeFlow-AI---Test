"""AUTO 编排器的 PostgreSQL 适配器：运行记录仓储 + 权威事实只读视图。

事实全部来自既有权威表（projects、narration_*、project_director_runs、
storyboard_generations、video_generation_attempts、composition_*），不复制
阶段判定逻辑；计数公式复用 SqlAlchemyIdentityProjectRepository.workflow_counts。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.models import (
    AutoPipelineRunRow,
    ProjectRow,
)
from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.adapters.persistence.models import (
    CompositionInputRevisionRow,
    CompositionJobRow,
    CompositionOutboxRow,
)
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    AuthoritativeNarrationMembershipRecord,
    AuthoritativeNarrationSentenceRecord,
    NarrationProjectRecord,
)
from tubeflow_test.adapters.persistence.storyboards.models import StoryboardGenerationRow
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    ProjectDirectorGroupCandidateRow,
    ProjectDirectorMaterializationRow,
    ProjectDirectorRunRow,
)
from tubeflow_test.auto_pipeline.domain import AutoPipelineRun, AutoPipelineState
from tubeflow_test.auto_pipeline.ports import (
    AutoPipelineFacts,
    OversizedGroupFact,
    StoryboardGroupFact,
    VideoGroupFact,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_generation.persistence import (
    GroupCurrentVideoRecord,
    VideoAttemptRecord,
)

# §5.5/§15.7：单句实测配音超过 15000ms 的分组阻止视频生成。
OVERSIZED_SENTENCE_THRESHOLD_MS = 15_000

_ACTIVE_STATES = (AutoPipelineState.RUNNING.value, AutoPipelineState.PAUSED.value)


def _run_from_row(row: AutoPipelineRunRow) -> AutoPipelineRun:
    return AutoPipelineRun(
        row.id,
        row.owner_id,
        row.project_id,
        AutoPipelineState(row.state),
        row.reason_code,
        int(row.authorization_round),
        dict(row.payload or {}),
        row.created_at,
        row.updated_at,
    )


def _write_row(row: AutoPipelineRunRow, run: AutoPipelineRun) -> None:
    row.state = run.state.value
    row.reason_code = run.reason_code
    row.authorization_round = run.authorization_round
    row.payload = dict(run.payload)
    row.updated_at = run.updated_at


def create_active_run_in_session(session: Session, run: AutoPipelineRun) -> AutoPipelineRun:
    """确认配音事务内的幂等创建：已有 RUNNING/PAUSED 运行时返回既有记录。"""
    existing = session.scalar(
        select(AutoPipelineRunRow).where(
            AutoPipelineRunRow.project_id == run.project_id,
            AutoPipelineRunRow.state.in_(_ACTIVE_STATES),
        )
    )
    if existing is not None:
        return _run_from_row(existing)
    row = AutoPipelineRunRow(
        id=run.run_id,
        owner_id=run.owner_id,
        project_id=run.project_id,
        state=run.state.value,
        reason_code=run.reason_code,
        authorization_round=run.authorization_round,
        payload=dict(run.payload),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
    session.add(row)
    session.flush()
    return run


class _SqlRunLock:
    def __init__(self, session: Session, row: AutoPipelineRunRow | None) -> None:
        self._session = session
        self._row = row

    @property
    def run(self) -> AutoPipelineRun | None:
        return _run_from_row(self._row) if self._row is not None else None

    def save(self, run: AutoPipelineRun) -> None:
        if self._row is None:
            raise DomainError("not_found", "Auto pipeline run was not found")
        _write_row(self._row, run)
        self._session.flush()


class SqlAutoPipelineRunRepository:
    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def create_active(self, run: AutoPipelineRun) -> AutoPipelineRun:
        with self._sessions() as session, session.begin():
            return create_active_run_in_session(session, run)

    def active_run(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None:
        with self._sessions() as session:
            row = session.scalar(
                select(AutoPipelineRunRow).where(
                    AutoPipelineRunRow.project_id == project_id,
                    AutoPipelineRunRow.owner_id == owner_id,
                    AutoPipelineRunRow.state.in_(_ACTIVE_STATES),
                )
            )
            return _run_from_row(row) if row is not None else None

    def latest_run(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None:
        with self._sessions() as session:
            row = session.scalar(
                select(AutoPipelineRunRow)
                .where(
                    AutoPipelineRunRow.project_id == project_id,
                    AutoPipelineRunRow.owner_id == owner_id,
                )
                .order_by(AutoPipelineRunRow.created_at.desc(), AutoPipelineRunRow.id.desc())
                .limit(1)
            )
            return _run_from_row(row) if row is not None else None

    def list_running(self) -> tuple[AutoPipelineRun, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(AutoPipelineRunRow)
                .where(AutoPipelineRunRow.state == AutoPipelineState.RUNNING.value)
                .order_by(AutoPipelineRunRow.created_at, AutoPipelineRunRow.id)
            ).all()
            return tuple(_run_from_row(row) for row in rows)

    @contextmanager
    def lock_active(self, owner_id: UUID, project_id: UUID) -> Iterator[_SqlRunLock]:
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(AutoPipelineRunRow)
                .where(
                    AutoPipelineRunRow.project_id == project_id,
                    AutoPipelineRunRow.state.in_(_ACTIVE_STATES),
                )
                .with_for_update()
            )
            if row is None or row.owner_id != owner_id:
                yield _SqlRunLock(session, None)
            else:
                yield _SqlRunLock(session, row)

    def resume_paused(self, owner_id: UUID, project_id: UUID, *, now: datetime) -> AutoPipelineRun:
        """原子恢复：同一事务内校验 PAUSED、项目预算轮次 +1、运行置 RUNNING。"""
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(AutoPipelineRunRow)
                .where(
                    AutoPipelineRunRow.project_id == project_id,
                    AutoPipelineRunRow.state.in_(_ACTIVE_STATES),
                )
                .with_for_update()
            )
            if row is None or row.owner_id != owner_id:
                raise DomainError("not_found", "Resource not found")
            current = _run_from_row(row)
            if current.state is not AutoPipelineState.PAUSED:
                raise DomainError(
                    "auto_pipeline_not_paused",
                    "Only a paused auto pipeline can be resumed",
                )
            current_round = session.scalar(
                select(ProjectRow.auto_budget_round).where(ProjectRow.id == project_id)
            )
            if current_round is None:
                raise DomainError("not_found", "Resource not found")
            new_round = int(current_round) + 1
            session.execute(
                update(ProjectRow)
                .where(ProjectRow.id == project_id)
                .values(auto_budget_round=new_round)
            )
            resumed = current.resumed(new_round, now)
            _write_row(row, resumed)
            session.flush()
            return resumed

    def resume_paused_for_reason(
        self, owner_id: UUID, project_id: UUID, *, reason_code: str, now: datetime
    ) -> AutoPipelineRun | None:
        """按原因码守卫的原子恢复：仅当活动运行 PAUSED 且原因码匹配才恢复，否则返回 None。

        用于「修正保存即续跑」（§20）：只在暂停原因与被修复对象一致时才开启新一轮
        授权，避免无关保存误重置其他阶段的预算。
        """
        with self._sessions() as session, session.begin():
            row = session.scalar(
                select(AutoPipelineRunRow)
                .where(
                    AutoPipelineRunRow.project_id == project_id,
                    AutoPipelineRunRow.state.in_(_ACTIVE_STATES),
                )
                .with_for_update()
            )
            if row is None or row.owner_id != owner_id:
                return None
            current = _run_from_row(row)
            if current.state is not AutoPipelineState.PAUSED or current.reason_code != reason_code:
                return None
            current_round = session.scalar(
                select(ProjectRow.auto_budget_round).where(ProjectRow.id == project_id)
            )
            if current_round is None:
                return None
            new_round = int(current_round) + 1
            session.execute(
                update(ProjectRow)
                .where(ProjectRow.id == project_id)
                .values(auto_budget_round=new_round)
            )
            resumed = current.resumed(new_round, now)
            _write_row(row, resumed)
            session.flush()
            return resumed


class SqlAutoPipelineFacts:
    """advance 的权威事实只读视图（单次 load 内一致性由各 COUNT/最新行查询保证）。"""

    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def load(self, owner_id: UUID, project_id: UUID) -> AutoPipelineFacts:
        with self._sessions() as session:
            project = session.get(ProjectRow, project_id)
            if project is None or project.owner_id != owner_id:
                raise DomainError("not_found", "Resource not found")
            budget_round = int(project.auto_budget_round or 1)
            counts = SqlAlchemyIdentityProjectRepository(session).workflow_counts(
                owner_id, project_id
            )
            groups_total = counts["groups"]
            unblocked = groups_total - counts["blocked"]
            narration_ready = (
                project.snapshot_confirmed_at is not None
                and counts["sentences"] > 0
                and counts["ready_sentences"] == counts["sentences"]
                and counts["grouped_sentences"] == counts["sentences"]
                and groups_total > 0
            )
            director_run = session.scalar(
                select(ProjectDirectorRunRow)
                .where(
                    ProjectDirectorRunRow.owner_id == owner_id,
                    ProjectDirectorRunRow.project_id == project_id,
                )
                .order_by(ProjectDirectorRunRow.created_at.desc(), ProjectDirectorRunRow.id.desc())
                .limit(1)
            )
            composition_in_flight, composition_failed_code = self._composition_state(
                session, owner_id, project_id, budget_round
            )
            return AutoPipelineFacts(
                production_mode=str(project.production_mode or "MANUAL"),
                budget_round=budget_round,
                narration_ready=narration_ready,
                narration_blocking=self._narration_blocking(session, owner_id, project_id),
                oversized_groups=self._oversized_groups(session, owner_id, project_id),
                director_preplan_done=counts.get("director_preplan_revisions", 0) > 0,
                director_latest_run_id=director_run.id if director_run is not None else None,
                director_latest_status=(
                    str(director_run.status) if director_run is not None else None
                ),
                director_latest_failure_code=(
                    director_run.failure_code if director_run is not None else None
                ),
                materialization_ready=_materialization_ready(session, owner_id, project_id),
                storyboards_complete=unblocked > 0 and counts["storyboards"] == unblocked,
                storyboard_groups=self._storyboard_groups(session, owner_id, project_id),
                videos_complete=unblocked > 0 and counts["videos"] == unblocked,
                video_groups=self._video_groups(
                    session, owner_id, project_id, budget_round
                ),
                composition_current_exists=counts["compositions"] > 0,
                composition_in_flight=composition_in_flight,
                composition_failed_code=composition_failed_code,
            )

    # ------------------------------------------------------------------

    @staticmethod
    def _narration_blocking(
        session: Session, owner_id: UUID, project_id: UUID
    ) -> dict[int, str] | None:
        record = session.get(NarrationProjectRecord, project_id)
        if record is None or record.owner_id != owner_id:
            return None
        value = record.payload.get("blocking_reason")
        if not isinstance(value, dict):
            return None
        sequences = value.get("sequences")
        if not isinstance(sequences, dict) or not sequences:
            return None
        return {int(seq): str(code) for seq, code in sequences.items()}

    @staticmethod
    def _oversized_groups(
        session: Session, owner_id: UUID, project_id: UUID
    ) -> tuple[OversizedGroupFact, ...]:
        rows = session.execute(
            select(
                AuthoritativeNarrationGroupRecord.id,
                AuthoritativeNarrationGroupRecord.sequence,
                AuthoritativeNarrationSentenceRecord.sequence.label("sentence_sequence"),
                AuthoritativeNarrationSentenceRecord.measured_duration_ms,
            )
            .join(
                AuthoritativeNarrationMembershipRecord,
                AuthoritativeNarrationMembershipRecord.group_id
                == AuthoritativeNarrationGroupRecord.id,
            )
            .join(
                AuthoritativeNarrationSentenceRecord,
                AuthoritativeNarrationSentenceRecord.id
                == AuthoritativeNarrationMembershipRecord.sentence_id,
            )
            .where(
                AuthoritativeNarrationGroupRecord.owner_id == owner_id,
                AuthoritativeNarrationGroupRecord.project_id == project_id,
                AuthoritativeNarrationGroupRecord.video_blocked.is_(True),
                AuthoritativeNarrationSentenceRecord.measured_duration_ms
                > OVERSIZED_SENTENCE_THRESHOLD_MS,
            )
            .order_by(
                AuthoritativeNarrationGroupRecord.sequence,
                AuthoritativeNarrationMembershipRecord.ordinal,
            )
        ).all()
        by_group: dict[UUID, tuple[int, list[tuple[int, int]]]] = {}
        for group_id, group_sequence, sentence_sequence, measured_ms in rows:
            entry = by_group.setdefault(group_id, (int(group_sequence), []))
            entry[1].append((int(sentence_sequence), int(measured_ms)))
        return tuple(
            OversizedGroupFact(group_id, sequence, tuple(sentences))
            for group_id, (sequence, sentences) in by_group.items()
        )

    @staticmethod
    def _storyboard_groups(
        session: Session, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardGroupFact, ...]:
        rows = session.execute(
            select(
                StoryboardGenerationRow,
                AuthoritativeNarrationGroupRecord.sequence,
            )
            .join(
                AuthoritativeNarrationGroupRecord,
                AuthoritativeNarrationGroupRecord.id == StoryboardGenerationRow.group_id,
            )
            .where(
                StoryboardGenerationRow.owner_id == owner_id,
                StoryboardGenerationRow.project_id == project_id,
            )
        ).all()
        latest: dict[UUID, tuple[StoryboardGenerationRow, int]] = {}
        for row, sequence in rows:
            current = latest.get(row.group_id)
            if current is None or row.version > current[0].version:
                latest[row.group_id] = (row, int(sequence))
        facts: list[StoryboardGroupFact] = []
        for row, sequence in latest.values():
            inputs = row.input_json or {}
            checkpoint = row.checkpoint_json or {}
            facts.append(
                StoryboardGroupFact(
                    group_id=row.group_id,
                    group_sequence=sequence,
                    latest_status=str(row.status),
                    latest_failure_code=(
                        None
                        if checkpoint.get("failure_code") is None
                        else str(checkpoint.get("failure_code"))
                    ),
                    latest_budget_round=int(cast(int, inputs.get("budget_round", 1))),
                )
            )
        return tuple(sorted(facts, key=lambda item: item.group_sequence))

    @staticmethod
    def _video_groups(
        session: Session, owner_id: UUID, project_id: UUID, budget_round: int
    ) -> tuple[VideoGroupFact, ...]:
        group_rows = session.execute(
            select(
                AuthoritativeNarrationGroupRecord.id,
                AuthoritativeNarrationGroupRecord.sequence,
                GroupCurrentVideoRecord.attempt_id,
            )
            .outerjoin(
                GroupCurrentVideoRecord,
                GroupCurrentVideoRecord.group_id == AuthoritativeNarrationGroupRecord.id,
            )
            .where(
                AuthoritativeNarrationGroupRecord.owner_id == owner_id,
                AuthoritativeNarrationGroupRecord.project_id == project_id,
                AuthoritativeNarrationGroupRecord.video_blocked.is_(False),
            )
            .order_by(AuthoritativeNarrationGroupRecord.sequence)
        ).all()
        attempt_rows = session.scalars(
            select(VideoAttemptRecord).where(
                VideoAttemptRecord.owner_id == owner_id,
                VideoAttemptRecord.project_id == project_id,
            )
        ).all()
        latest: dict[UUID, VideoAttemptRecord] = {}
        submissions: dict[UUID, int] = {}
        for row in attempt_rows:
            if int(row.budget_round or 1) == budget_round:
                submissions[row.group_id] = submissions.get(row.group_id, 0) + 1
            current = latest.get(row.group_id)
            if current is None or (row.intent_sequence, row.attempt_id) > (
                current.intent_sequence,
                current.attempt_id,
            ):
                latest[row.group_id] = row
        facts: list[VideoGroupFact] = []
        for group_id, sequence, current_attempt_id in group_rows:
            attempt = latest.get(group_id)
            facts.append(
                VideoGroupFact(
                    group_id=group_id,
                    group_sequence=int(sequence),
                    has_current_video=current_attempt_id is not None,
                    latest_stage=str(attempt.stage) if attempt is not None else None,
                    latest_archive_stage=(
                        str(attempt.archive_stage) if attempt is not None else None
                    ),
                    latest_error_code=attempt.error_code if attempt is not None else None,
                    latest_budget_round=(
                        int(attempt.budget_round or 1) if attempt is not None else 1
                    ),
                    submissions=submissions.get(group_id, 0),
                )
            )
        return tuple(facts)

    @staticmethod
    def _composition_state(
        session: Session, owner_id: UUID, project_id: UUID, budget_round: int
    ) -> tuple[bool, str | None]:
        """返回 (in_flight, failed_code)；既定失败仅对当前授权轮次成立。"""
        outbox = session.execute(
            select(
                CompositionOutboxRow.state,
                CompositionOutboxRow.workflow_generation,
            )
            .join(
                CompositionInputRevisionRow,
                CompositionInputRevisionRow.id == CompositionOutboxRow.revision_id,
            )
            .where(
                CompositionOutboxRow.owner_id == owner_id,
                CompositionOutboxRow.project_id == project_id,
            )
            .order_by(CompositionInputRevisionRow.created_at.desc(), CompositionOutboxRow.id.desc())
            .limit(1)
        ).first()
        job = session.scalars(
            select(CompositionJobRow)
            .where(
                CompositionJobRow.owner_id == owner_id,
                CompositionJobRow.project_id == project_id,
            )
            .order_by(CompositionJobRow.intent_sequence.desc())
            .limit(1)
        ).first()
        in_flight = False
        if outbox is not None and str(outbox[0]) == "PENDING":
            in_flight = True
        if job is not None and job.state == "RUNNING":
            in_flight = True
        if outbox is not None and (
            job is None or int(outbox[1]) > int(job.workflow_generation)
        ):
            # 已入队/已派发但活动未认领，或自动重试已排程（新代际）。
            in_flight = True
        failed_code: str | None = None
        job_round = (
            _composition_key_round(job.idempotency_key, owner_id, project_id)
            if job is not None
            else None
        )
        if (
            job is not None
            and job.state == "FAILED"
            and job_round == budget_round
            and not in_flight
        ):
            failed_code = job.error_code or "composition_failed"
        return in_flight, failed_code


def _composition_key_round(key: str, owner_id: UUID, project_id: UUID) -> int:
    """从合成幂等键解析预算轮次；无 :rN 后缀的历史键视为第 1 轮。"""
    prefix = f"compose:{owner_id}:{project_id}:"
    raw = key.removeprefix(prefix)
    head, sep, tail = raw.rpartition(":r")
    if sep and len(head) == 64 and tail.isdigit():
        return int(tail)
    return 1

def _materialization_ready(session: Session, owner_id: UUID, project_id: UUID) -> bool:
    """物化就绪 = 存在至少带一个候选子行的物化（孤儿父行属不完整写入，不算就绪）。"""
    row = session.scalar(
        select(ProjectDirectorMaterializationRow.id)
        .where(
            ProjectDirectorMaterializationRow.owner_id == owner_id,
            ProjectDirectorMaterializationRow.project_id == project_id,
            select(ProjectDirectorGroupCandidateRow.id)
            .where(
                ProjectDirectorGroupCandidateRow.materialization_id
                == ProjectDirectorMaterializationRow.id
            )
            .limit(1)
            .exists(),
        )
        .limit(1)
    )
    return row is not None

