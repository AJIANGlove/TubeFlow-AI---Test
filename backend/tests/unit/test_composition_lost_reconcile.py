from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.persistence.identity_project.models import AutoPipelineRunRow
from tubeflow_test.adapters.persistence.media_repository import SqlAlchemyCompositionOutbox
from tubeflow_test.adapters.persistence.models import (
    CompositionJobRow,
    CompositionOutboxRow,
)

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PROJECT = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _harness(path: Path):
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )
    CompositionJobRow.__table__.create(engine)
    CompositionOutboxRow.__table__.create(engine)
    AutoPipelineRunRow.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "production_state VARCHAR(16), production_mode VARCHAR(16))"
            )
        )
        connection.execute(
            text("CREATE TABLE project_purge_jobs (project_id CHAR(32) PRIMARY KEY)")
        )
        connection.execute(
            text(
                "CREATE TABLE composition_input_revisions "
                "(id CHAR(32) PRIMARY KEY, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE composition_versions (id CHAR(32) PRIMARY KEY, "
                "project_id CHAR(32), owner_id CHAR(32), current BOOLEAN)"
            )
        )
    sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def uow():
        session = sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    return SqlAlchemyCompositionOutbox(uow), sessions


def _seed(
    sessions: sessionmaker[Session],
    *,
    mode: str,
    job_state: str = "RUNNING",
    attempt: int = 1,
    budget_round: int = 1,
    dispatched_at: datetime | None = None,
    revision_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    job_id, outbox_id = uuid4(), uuid4()
    revision_id = revision_id or uuid4()
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state, production_mode) "
                "VALUES (:id, :owner, 'ACTIVE', :mode)"
            ),
            {"id": PROJECT.hex, "owner": OWNER.hex, "mode": mode},
        )
        session.add(
            CompositionJobRow(
                id=job_id,
                project_id=PROJECT,
                owner_id=OWNER,
                idempotency_key=f"compose:{OWNER}:{PROJECT}:{'d' * 64}",
                state=job_state,
                attempt=attempt,
                workflow_generation=1,
                execution_token=uuid4(),
            )
        )
        session.add(
            CompositionOutboxRow(
                id=outbox_id,
                project_id=PROJECT,
                owner_id=OWNER,
                revision_id=revision_id,
                inputs_sha256="d" * 64,
                workflow_id=f"composition:{OWNER}:{PROJECT}:{'d' * 64}:g1",
                state="DISPATCHED",
                attempt=1,
                workflow_generation=1,
                budget_round=budget_round,
                next_attempt_at=datetime.now(UTC) - timedelta(hours=2),
                dispatched_at=dispatched_at or (datetime.now(UTC) - timedelta(hours=2)),
            )
        )
    return job_id, outbox_id


def _row(sessions: sessionmaker[Session], model, row_id: UUID):
    with sessions() as session:
        return session.get(model, row_id)


def test_auto_lost_dispatch_is_failed_and_rescheduled(tmp_path: Path) -> None:
    outbox, sessions = _harness(tmp_path / "auto.sqlite")
    job_id, outbox_id = _seed(sessions, mode="AUTO")
    scheduled = outbox.reconcile_lost_dispatches(
        older_than=datetime.now(UTC) - timedelta(minutes=35),
        max_attempts=3,
        backoff_seconds=(5.0, 15.0, 30.0),
    )
    assert scheduled == 1
    job = _row(sessions, CompositionJobRow, job_id)
    assert job.state == "FAILED" and job.error_code == "composition_workflow_lost"
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.state == "PENDING"
    assert envelope.workflow_generation == 2
    assert envelope.workflow_id.endswith(":g2")
    assert envelope.next_attempt_at is not None


def test_manual_lost_dispatch_fails_without_reschedule(tmp_path: Path) -> None:
    outbox, sessions = _harness(tmp_path / "manual.sqlite")
    job_id, outbox_id = _seed(sessions, mode="MANUAL")
    scheduled = outbox.reconcile_lost_dispatches(
        older_than=datetime.now(UTC) - timedelta(minutes=35),
        max_attempts=3,
        backoff_seconds=(5.0, 15.0, 30.0),
    )
    assert scheduled == 0
    job = _row(sessions, CompositionJobRow, job_id)
    assert job.state == "FAILED" and job.error_code == "composition_workflow_lost"
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.state == "DISPATCHED"
    assert envelope.workflow_generation == 1


def test_budget_exhausted_lost_dispatch_fails_without_reschedule(tmp_path: Path) -> None:
    outbox, sessions = _harness(tmp_path / "exhausted.sqlite")
    job_id, outbox_id = _seed(sessions, mode="AUTO", attempt=3)
    scheduled = outbox.reconcile_lost_dispatches(
        older_than=datetime.now(UTC) - timedelta(minutes=35),
        max_attempts=3,
        backoff_seconds=(5.0, 15.0, 30.0),
    )
    assert scheduled == 0
    job = _row(sessions, CompositionJobRow, job_id)
    assert job.state == "FAILED"
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.state == "DISPATCHED"


def test_fresh_dispatch_and_terminal_jobs_are_untouched(tmp_path: Path) -> None:
    outbox, sessions = _harness(tmp_path / "fresh.sqlite")
    fresh_id, fresh_outbox_id = _seed(
        sessions, mode="AUTO", dispatched_at=datetime.now(UTC)
    )
    scheduled = outbox.reconcile_lost_dispatches(
        older_than=datetime.now(UTC) - timedelta(minutes=35),
        max_attempts=3,
        backoff_seconds=(5.0, 15.0, 30.0),
    )
    assert scheduled == 0
    job = _row(sessions, CompositionJobRow, fresh_id)
    assert job.state == "RUNNING"
    envelope = _row(sessions, CompositionOutboxRow, fresh_outbox_id)
    assert envelope.state == "DISPATCHED"

def test_failed_job_with_stale_dispatch_is_rescheduled_with_round_scope(tmp_path: Path) -> None:
    # 崩溃打断在失败处理前的 FAILED 任务：最新代 DISPATCHED 且无更新代际时，按轮次
    # 命名空间重排（§15.9；workflow_id 带 :rN:gN，跨轮不重名）。
    outbox, sessions = _harness(tmp_path / "failed-round.sqlite")
    job_id, outbox_id = _seed(sessions, mode="AUTO", job_state="FAILED", budget_round=2)
    scheduled = outbox.reconcile_lost_dispatches(
        older_than=datetime.now(UTC) - timedelta(minutes=35),
        max_attempts=3,
        backoff_seconds=(5.0, 15.0, 30.0),
    )
    assert scheduled == 1
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.state == "PENDING"
    assert envelope.workflow_generation == 2
    assert envelope.workflow_id.endswith(":r2:g2")


def test_stale_dispatch_with_newer_generation_is_skipped(tmp_path: Path) -> None:
    # 已有更新代际承接的历史滞留行：不得再重排（生产实测的代际冲突来源）。
    outbox, sessions = _harness(tmp_path / "newer-gen.sqlite")
    job_id, outbox_id = _seed(sessions, mode="AUTO", job_state="FAILED", budget_round=1)
    with sessions.begin() as session:
        session.add(
            CompositionOutboxRow(
                id=uuid4(),
                project_id=PROJECT,
                owner_id=OWNER,
                revision_id=uuid4(),
                inputs_sha256="d" * 64,
                workflow_id=f"composition:{OWNER}:{PROJECT}:{'d' * 64}:g2",
                state="DISPATCHED",
                attempt=1,
                workflow_generation=2,
                next_attempt_at=datetime.now(UTC) - timedelta(hours=2),
                dispatched_at=datetime.now(UTC) - timedelta(hours=2),
                budget_round=1,
            )
        )
    scheduled = outbox.reconcile_lost_dispatches(
        older_than=datetime.now(UTC) - timedelta(minutes=35),
        max_attempts=3,
        backoff_seconds=(5.0, 15.0, 30.0),
    )
    assert scheduled == 0
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.workflow_generation == 1
    assert envelope.state == "DISPATCHED"


# ---------------------------------------------------------------------------
# §17.3：COMPLETED 残留收口与 close_superseded
# ---------------------------------------------------------------------------


def _seed_revision(
    sessions: sessionmaker[Session], revision_id: UUID, created_at: datetime
) -> None:
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO composition_input_revisions (id, created_at) "
                "VALUES (:id, :created)"
            ),
            {"id": revision_id.hex, "created": created_at.strftime("%Y-%m-%d %H:%M:%S.%f")},
        )


def _seed_completed_run(sessions: sessionmaker[Session], *, completed_at: datetime) -> None:
    with sessions.begin() as session:
        session.add(
            AutoPipelineRunRow(
                id=uuid4(),
                owner_id=OWNER,
                project_id=PROJECT,
                state="COMPLETED",
                authorization_round=11,
                payload={"phase": "completed"},
                created_at=completed_at - timedelta(days=1),
                updated_at=completed_at,
            )
        )


def _seed_current_version(sessions: sessionmaker[Session]) -> None:
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO composition_versions (id, project_id, owner_id, current) "
                "VALUES (:id, :project, :owner, 1)"
            ),
            {"id": uuid4().hex, "project": PROJECT.hex, "owner": OWNER.hex},
        )


def _reconcile(outbox: SqlAlchemyCompositionOutbox) -> int:
    return outbox.reconcile_lost_dispatches(
        older_than=datetime.now(UTC) - timedelta(minutes=35),
        max_attempts=3,
        backoff_seconds=(5.0, 15.0, 30.0),
    )


def test_completed_run_residue_is_superseded(tmp_path: Path) -> None:
    # §17.3：COMPLETED 运行的完成前残留行收口；job 簿记保持真实终态不动。
    outbox, sessions = _harness(tmp_path / "completed.sqlite")
    revision_id = uuid4()
    job_id, outbox_id = _seed(
        sessions, mode="AUTO", job_state="FAILED", attempt=3, revision_id=revision_id
    )
    completed = datetime.now(UTC) - timedelta(hours=1)
    _seed_revision(sessions, revision_id, completed - timedelta(hours=2))
    _seed_completed_run(sessions, completed_at=completed)
    assert _reconcile(outbox) == 0
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.state == "SUPERSEDED"
    assert envelope.lease_token is None and envelope.lease_until is None
    job = _row(sessions, CompositionJobRow, job_id)
    assert job.state == "FAILED"


def test_post_completion_revision_keeps_auto_retry_semantics(tmp_path: Path) -> None:
    # §17.3：完成后的新 revision（用户手动重试）不收口，保持既有 AUTO 重排语义。
    outbox, sessions = _harness(tmp_path / "post-completion.sqlite")
    revision_id = uuid4()
    _job_id, outbox_id = _seed(
        sessions, mode="AUTO", job_state="FAILED", attempt=1, budget_round=2,
        revision_id=revision_id,
    )
    completed = datetime.now(UTC) - timedelta(hours=2)
    _seed_revision(sessions, revision_id, completed + timedelta(hours=1))
    _seed_completed_run(sessions, completed_at=completed)
    assert _reconcile(outbox) == 1
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.state == "PENDING"
    assert envelope.workflow_generation == 2


def test_running_composition_job_blocks_closure(tmp_path: Path) -> None:
    # §17.3：项目仍有 RUNNING 合成任务时在途对账优先，不执行收口。
    outbox, sessions = _harness(tmp_path / "running.sqlite")
    revision_id = uuid4()
    job_id, outbox_id = _seed(sessions, mode="AUTO", job_state="RUNNING", revision_id=revision_id)
    completed = datetime.now(UTC) - timedelta(hours=1)
    _seed_revision(sessions, revision_id, completed - timedelta(hours=2))
    _seed_completed_run(sessions, completed_at=completed)
    assert _reconcile(outbox) == 1
    envelope = _row(sessions, CompositionOutboxRow, outbox_id)
    assert envelope.state == "PENDING"
    job = _row(sessions, CompositionJobRow, job_id)
    assert job.state == "FAILED" and job.error_code == "composition_workflow_lost"


def test_close_superseded_requires_current_version(tmp_path: Path) -> None:
    outbox, sessions = _harness(tmp_path / "close.sqlite")
    revision_id = uuid4()
    _job_id, outbox_id = _seed(
        sessions, mode="AUTO", job_state="FAILED", attempt=3, revision_id=revision_id
    )
    # 无当前成片：不动任何行。
    assert outbox.close_superseded(OWNER, PROJECT) == 0
    assert _row(sessions, CompositionOutboxRow, outbox_id).state == "DISPATCHED"

    _seed_current_version(sessions)
    with sessions.begin() as session:
        session.add(
            CompositionOutboxRow(
                id=uuid4(),
                project_id=PROJECT,
                owner_id=OWNER,
                revision_id=uuid4(),
                inputs_sha256="e" * 64,
                workflow_id=f"composition:{OWNER}:{PROJECT}:{'e' * 64}:g1",
                state="PENDING",
                attempt=0,
                workflow_generation=1,
                budget_round=12,
                next_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
                dispatched_at=None,
            )
        )
    assert outbox.close_superseded(OWNER, PROJECT) == 2
    assert _row(sessions, CompositionOutboxRow, outbox_id).state == "SUPERSEDED"
    # 幂等：再次收口无行可动。
    assert outbox.close_superseded(OWNER, PROJECT) == 0

