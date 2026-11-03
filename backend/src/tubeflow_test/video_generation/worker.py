from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, not_, or_, select
from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from tubeflow_test.video_generation.orchestration import DurableGroupVideoOrchestrator
from tubeflow_test.video_generation.persistence import VideoAttemptRecord, VideoOutboxRecord
from tubeflow_test.video_generation.temporal import (
    GroupVideoActivities,
    GroupVideoRecoveryWorkflow,
    RecoverVideoCommand,
)

MAX_BACKOFF_SECONDS = 300

logger = logging.getLogger(__name__)


def create_video_worker(
    client: Client,
    service: DurableGroupVideoOrchestrator,
    *,
    task_queue: str,
    auto_kick: Callable[[UUID, UUID], None] | None = None,
) -> Worker:
    activities = GroupVideoActivities(service, auto_kick)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[GroupVideoRecoveryWorkflow],
        activities=[activities.recover, activities.archive],
        # S6：默认 100 会让 activity 线程数在故障时膨胀到 600+；
        # 32 足以覆盖 6 个槽位的长任务 + 轮询/归档快任务，并与连接池 20+20 匹配。
        max_concurrent_activities=32,
    )


async def dispatch_video_outbox_once(
    client: Client,
    sessions: sessionmaker[Session],
    *,
    task_queue: str,
    stalled_poll_seconds: float = 300.0,
) -> int:
    """Durably dispatch undispatched intents with a stable Temporal workflow id."""
    now = datetime.now(UTC)
    token = uuid4().hex
    with sessions() as session, session.begin():
        rows = tuple(
            session.scalars(
                select(VideoOutboxRecord)
                .where(
                    VideoOutboxRecord.dispatched_at.is_(None),
                    or_(
                        VideoOutboxRecord.terminal_state.is_(None),
                        VideoOutboxRecord.terminal_state == "UNKNOWN",
                    ),
                    VideoOutboxRecord.next_attempt_at <= now,
                    (VideoOutboxRecord.lease_expires_at.is_(None))
                    | (VideoOutboxRecord.lease_expires_at < now),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.lease_token = token
            row.lease_expires_at = now + timedelta(minutes=2)
        commands = tuple((row.owner_id, row.attempt_id) for row in rows)
    dispatched = 0
    for owner_id, attempt_id in commands:
        workflow_id = f"group-video-{attempt_id}"
        try:
            await client.start_workflow(
                GroupVideoRecoveryWorkflow.run,
                RecoverVideoCommand(
                    owner_id, attempt_id, stalled_poll_seconds=stalled_poll_seconds
                ),
                id=workflow_id,
                task_queue=task_queue,
            )
        except WorkflowAlreadyStartedError:
            pass
        except Exception as exc:
            if _uncertain_dispatch_error(exc):
                try:
                    await client.get_workflow_handle(workflow_id).describe()
                except Exception as describe_exc:
                    _record_dispatch_failure(
                        sessions,
                        attempt_id,
                        token,
                        error=(
                            f"start={_dispatch_error_label(exc)};"
                            f"describe={_dispatch_error_label(describe_exc)}"
                        ),
                        uncertain=True,
                    )
                    continue
            else:
                _record_dispatch_failure(
                    sessions,
                    attempt_id,
                    token,
                    error=_dispatch_error_label(exc),
                    uncertain=False,
                )
                continue
        if _ack_dispatch(sessions, attempt_id, token):
            dispatched += 1
    return dispatched


def _record_dispatch_failure(
    sessions: sessionmaker[Session],
    attempt_id: object,
    token: str,
    *,
    error: str,
    uncertain: bool,
) -> None:
    with sessions() as session, session.begin():
        failed_row = session.get(VideoOutboxRecord, attempt_id)
        if failed_row is None or failed_row.lease_token != token:
            return
        failed_row.retry_count += 1
        failed_row.last_error = error[:128]
        failed_row.lease_token = None
        failed_row.lease_expires_at = None
        if uncertain:
            failed_row.terminal_state = "UNKNOWN"
            delay = min(MAX_BACKOFF_SECONDS, 2 ** min(failed_row.retry_count, 8))
            failed_row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        else:
            failed_row.terminal_state = "DEAD_LETTER"


def _ack_dispatch(sessions: sessionmaker[Session], attempt_id: object, token: str) -> bool:
    with sessions() as session, session.begin():
        row = session.get(VideoOutboxRecord, attempt_id)
        if row is None or row.lease_token != token:
            return False
        row.dispatched_at = datetime.now(UTC)
        row.lease_token = None
        row.lease_expires_at = None
        row.terminal_state = "DISPATCHED"
        return True


def _uncertain_dispatch_error(exc: Exception) -> bool:
    if isinstance(exc, RPCError):
        return exc.status not in {
            RPCStatusCode.INVALID_ARGUMENT,
            RPCStatusCode.FAILED_PRECONDITION,
            RPCStatusCode.PERMISSION_DENIED,
            RPCStatusCode.UNAUTHENTICATED,
            RPCStatusCode.UNIMPLEMENTED,
            RPCStatusCode.OUT_OF_RANGE,
        }
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _dispatch_error_label(exc: Exception) -> str:
    if isinstance(exc, RPCError):
        return f"RPCError:{exc.status.name}"
    return type(exc).__name__


async def relay_video_outbox(
    client: Client,
    sessions: sessionmaker[Session],
    *,
    task_queue: str,
    interval_seconds: float = 1.0,
    stalled_poll_seconds: float = 300.0,
) -> None:
    """Supervised relay loop; a failed dispatch remains leased only until its short expiry."""
    while True:
        reset = redrive_stale_video_outbox(sessions)
        if reset:
            logger.info("redrive_stale_video_outbox reset %d dispatched rows", reset)
        await dispatch_video_outbox_once(
            client,
            sessions,
            task_queue=task_queue,
            stalled_poll_seconds=stalled_poll_seconds,
        )
        await asyncio.sleep(interval_seconds)


def _as_utc(value: datetime) -> datetime:
    """SQLite 回读的 datetime 是 naive；统一按 UTC 解释后再比较。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def redrive_stale_video_outbox(
    sessions: sessionmaker[Session], *, stale_after: timedelta = timedelta(minutes=5)
) -> int:
    """Make canceled/failed workflow dispatches eligible for the same-attempt redrive.

    S6 修复：旧实现只看 outbox 的 ``dispatched_at``，会把「workflow 仍在正常推进、
    只是没跑完」的 attempt 反复重置成未派发，是 300+ 僵尸 workflow 的制造机。
    现在追加两个闸门：

    1. 活性闸门——attempt 自身必须在 ``stale_after`` 内没有推进过；
    2. 退避闸门——按 ``retry_count`` 指数退避（5/10/20/40/60 分钟封顶）。

    两者同时满足才重置，且重置时累加 ``retry_count``，让真失败的恢复速度可控。
    """
    now = datetime.now(UTC)
    cutoff = now - stale_after
    reset = 0
    with sessions() as session, session.begin():
        rows = tuple(
            session.execute(
                select(VideoOutboxRecord, VideoAttemptRecord.updated_at)
                .join(
                    VideoAttemptRecord,
                    VideoAttemptRecord.attempt_id == VideoOutboxRecord.attempt_id,
                )
                .where(
                    VideoOutboxRecord.dispatched_at < cutoff,
                    not_(
                        or_(
                            and_(
                                VideoAttemptRecord.stage == "ARCHIVED",
                                VideoAttemptRecord.archive_stage == "COMPLETE",
                            ),
                            and_(
                                VideoAttemptRecord.stage.in_({"FAILED", "CANCELLED"}),
                                VideoAttemptRecord.archive_stage != "CLEANUP_PENDING",
                                VideoAttemptRecord.staged_ids == [],
                            ),
                            # QC 失败等待用户决策，自动化不再 redrive 空转。
                            and_(
                                VideoAttemptRecord.stage == "SUCCEEDED",
                                VideoAttemptRecord.archive_stage == "QC_FAILED",
                            ),
                        )
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for row, attempt_updated_at in rows:
            backoff = stale_after * min(2 ** min(row.retry_count, 4), 12)
            dispatched_at = _as_utc(row.dispatched_at)
            if dispatched_at is not None and dispatched_at > now - backoff:
                continue
            if attempt_updated_at is not None and _as_utc(attempt_updated_at) > cutoff:
                continue
            row.dispatched_at = None
            row.lease_token = None
            row.lease_expires_at = None
            row.next_attempt_at = now
            row.terminal_state = None
            row.retry_count += 1
            reset += 1
    return reset


async def run_video_worker(
    client: Client,
    sessions: sessionmaker[Session],
    service: DurableGroupVideoOrchestrator,
    *,
    task_queue: str,
    health_refresh: Callable[[], int] | None = None,
    stalled_poll_seconds: float = 300.0,
) -> None:
    """Run Temporal polling and the durable outbox relay under one fail-fast lifecycle."""
    worker = create_video_worker(client, service, task_queue=task_queue)
    relay = asyncio.create_task(
        relay_video_outbox(
            client,
            sessions,
            task_queue=task_queue,
            stalled_poll_seconds=stalled_poll_seconds,
        ),
        name="group-video-outbox-relay",
    )
    health = (
        asyncio.create_task(_refresh_health_forever(health_refresh), name="group-video-node-health")
        if health_refresh is not None
        else None
    )
    lease_sweeper = asyncio.create_task(
        _refresh_health_forever(service.sweep_expired_slots),
        name="group-video-slot-lease-sweeper",
    )
    try:
        await asyncio.gather(
            worker.run(), relay, lease_sweeper, *([health] if health is not None else [])
        )
    finally:
        relay.cancel()
        lease_sweeper.cancel()
        if health is not None:
            health.cancel()
        await worker.shutdown()


async def _refresh_health_forever(refresh: Callable[[], int]) -> None:
    while True:
        await asyncio.to_thread(refresh)
        await asyncio.sleep(10)
