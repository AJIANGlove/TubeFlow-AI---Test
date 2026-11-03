from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_generation.domain import (
    RetryableVideoIntentFailure,
    VideoRuleViolation,
)

# 与 orchestration.AttemptStage 对齐的非终态阶段；远端未知态单独归类。
_IN_FLIGHT_STAGES = frozenset(
    {"CREATED", "STAGING", "NOT_SUBMITTED", "SUBMITTING", "SUBMITTED", "RUNNING", "SUCCEEDED"}
)
_UNKNOWN_STAGE = "REMOTE_STATE_UNKNOWN"

# S6：单项目「在途视频组」上限。
# 140 组一次性推成 queued 会在几秒内起 140 个常驻 Temporal workflow，
# 把单线程 workflow executor 与 activity 池一起压死（2026-10-22 事故根因）。
# 默认 2 × 三卡槽位 = 12；可用环境变量覆盖，设为 0 表示不限制（回退旧行为）。
_DEFAULT_IN_FLIGHT_LIMIT = 12


def _in_flight_limit() -> int:
    raw = os.environ.get("TUBEFLOW_VIDEO_BATCH_MAX_IN_FLIGHT")
    if raw is None or not raw.strip():
        return _DEFAULT_IN_FLIGHT_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_IN_FLIGHT_LIMIT

PENDING_STATUS = "PENDING"
# C3：AUTO 项目意图阶段可重试失败暂存状态；由 AUTO 编排按退避翻回 PENDING。
RETRY_WAIT_STATUS = "RETRY_WAIT"
# 2026-10-31：H3 无空闲槽位时的等待态——不暂停、不计 6 次预算，按容量释放推进。
WAITING_CAPACITY_STATUS = "WAITING_CAPACITY"
# 容量不足的稳定码（与 video_generation.domain.VideoCapacityUnavailable 一致）。
CAPACITY_UNAVAILABLE_CODE = "video_capacity_unavailable"
# C3：同一组"提交 + 意图失败"合计预算耗尽后的终态状态（同时作为 AUTO 暂停码）。
SUBMISSION_BUDGET_EXHAUSTED_STATUS = "submission_budget_exhausted"


@dataclass(frozen=True, slots=True)
class VideoBatchGroupState:
    group_id: UUID
    sequence: int
    video_blocked: bool
    block_reason: str | None
    storyboard_generation_id: UUID | None
    has_current_video: bool
    latest_attempt_stage: str | None
    latest_attempt_stalled: bool = False


@dataclass(frozen=True, slots=True)
class VideoBatchGroupOverview:
    group_id: UUID
    sequence: int
    eligible: bool
    reason: str | None
    video_blocked: bool
    has_current_video: bool
    latest_attempt_stage: str | None
    latest_attempt_stalled: bool = False


@dataclass(frozen=True, slots=True)
class VideoBatchItemView:
    group_id: UUID
    sequence: int
    status: str
    attempt_id: UUID | None
    message: str | None


@dataclass(frozen=True, slots=True)
class VideoBatchJobView:
    job_id: UUID
    owner_id: UUID
    project_id: UUID
    force: bool
    total: int
    created_at: datetime
    completed_at: datetime | None
    items: tuple[VideoBatchItemView, ...]


class VideoBatchRepository(Protocol):
    def list_group_states(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[VideoBatchGroupState, ...]: ...


class VideoBatchJobRepository(Protocol):
    def create_job(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        force: bool,
        items: tuple[tuple[UUID, int, str], ...],
    ) -> UUID: ...

    def get_job(self, job_id: UUID) -> VideoBatchJobView | None: ...

    def next_pending_group(self, job_id: UUID) -> tuple[UUID, int] | None: ...

    def mark_item(
        self,
        job_id: UUID,
        group_id: UUID,
        status: str,
        *,
        attempt_id: UUID | None = None,
        message: str | None = None,
    ) -> None: ...

    def complete_job(self, job_id: UUID) -> None: ...

    def list_unfinished_job_ids(self) -> tuple[UUID, ...]: ...

    def oldest_waiting_job_id(self) -> UUID | None: ...

    def reopen_failed_items(self, job_id: UUID) -> int: ...


class VideoBatchIntentWriter(Protocol):
    def create(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        expected_storyboard_generation_id: UUID,
        idempotency_key: str,
    ) -> UUID: ...


def _skip_reason(state: VideoBatchGroupState, *, force: bool) -> str | None:
    """返回不可提交原因；None 表示该组可提交。判定顺序与已确认规则一致。"""
    if state.video_blocked:
        return state.block_reason or "video_blocked"
    if state.storyboard_generation_id is None:
        return "no_current_storyboard"
    if state.has_current_video and not force:
        return "has_current_video"
    if state.latest_attempt_stage == _UNKNOWN_STAGE:
        return "remote_acceptance_unknown"
    if state.latest_attempt_stage in _IN_FLIGHT_STAGES:
        return "already_running"
    return None


class GroupVideoBatchService:
    """项目级批量 H3 意图编排：一次用户点击创建持久化批量任务并逐项异步推进。

    每组独立幂等键（batch-{job_id}-{group_id}），中断后可安全续跑；单组失败
    不中断批次；任何失败路径都不会自动重试或重复提交远端任务。执行并发由
    既有节点槽位调度决定，本服务不做并发控制。
    """

    def __init__(
        self,
        repository: VideoBatchRepository,
        writer: VideoBatchIntentWriter,
        ids: Callable[[], UUID],
        jobs: VideoBatchJobRepository,
        in_flight_limit: int | None = None,
    ) -> None:
        self._repository = repository
        self._writer = writer
        self._ids = ids
        self._jobs = jobs
        self._in_flight_limit = in_flight_limit

    def overview(
        self, owner_id: UUID, project_id: UUID, *, force: bool = False
    ) -> tuple[VideoBatchGroupOverview, ...]:
        items: list[VideoBatchGroupOverview] = []
        for state in self._repository.list_group_states(owner_id, project_id):
            reason = _skip_reason(state, force=force)
            items.append(
                VideoBatchGroupOverview(
                    group_id=state.group_id,
                    sequence=state.sequence,
                    eligible=reason is None,
                    reason=reason,
                    video_blocked=state.video_blocked,
                    has_current_video=state.has_current_video,
                    latest_attempt_stage=state.latest_attempt_stage,
                    latest_attempt_stalled=state.latest_attempt_stalled,
                )
            )
        return tuple(items)

    def start(
        self, owner_id: UUID, project_id: UUID, group_ids: tuple[UUID, ...], *, force: bool
    ) -> VideoBatchJobView:
        """登记批量任务并立即返回；逐项意图创建由 process_next/process_all 推进。"""
        states = {
            state.group_id: state
            for state in self._repository.list_group_states(owner_id, project_id)
        }
        items: list[tuple[UUID, int, str]] = []
        for group_id in dict.fromkeys(group_ids):
            state = states.get(group_id)
            if state is None:
                items.append((group_id, 0, "not_found"))
                continue
            reason = _skip_reason(state, force=force)
            items.append((group_id, state.sequence, reason or PENDING_STATUS))
        job_id = self._jobs.create_job(owner_id, project_id, force=force, items=tuple(items))
        view = self._jobs.get_job(job_id)
        assert view is not None
        return view

    def get_job(self, owner_id: UUID, job_id: UUID) -> VideoBatchJobView | None:
        view = self._jobs.get_job(job_id)
        if view is None or view.owner_id != owner_id:
            return None
        return view

    def process_next(self, job_id: UUID) -> bool:
        """推进一个待处理分组；返回 False 表示批次已全部处理完。"""
        pending = self._jobs.next_pending_group(job_id)
        if pending is None:
            view = self._jobs.get_job(job_id)
            if view is not None and any(
                item.status in {RETRY_WAIT_STATUS, WAITING_CAPACITY_STATUS}
                for item in view.items
            ):
                # C3：仍有等待重试/等待容量的项时不收口 job，否则后续翻回 PENDING
                # 会失去宿主。
                return False
            self._jobs.complete_job(job_id)
            return False
        view = self._jobs.get_job(job_id)
        if view is None:
            return False
        group_id, _sequence = pending
        states = {
            state.group_id: state
            for state in self._repository.list_group_states(view.owner_id, view.project_id)
        }
        state = states.get(group_id)
        reason = "not_found" if state is None else _skip_reason(state, force=view.force)
        if reason is not None:
            self._jobs.mark_item(job_id, group_id, reason)
            return True
        assert state is not None and state.storyboard_generation_id is not None
        try:
            attempt_id = self._writer.create(
                owner_id=view.owner_id,
                group_id=group_id,
                expected_storyboard_generation_id=state.storyboard_generation_id,
                idempotency_key=f"batch-{job_id}-{group_id}",
            )
        except RetryableVideoIntentFailure as exc:
            # C3：提示词类可重试失败 → RETRY_WAIT（message 存稳定码，按 30/60/120s
            # 退避并计入每组 6 次预算）。
            # 2026-10-31：容量不足 → WAITING_CAPACITY（不计预算，等槽位释放后再试）。
            status = (
                WAITING_CAPACITY_STATUS
                if exc.code == CAPACITY_UNAVAILABLE_CODE
                else RETRY_WAIT_STATUS
            )
            self._jobs.mark_item(job_id, group_id, status, message=exc.code)
            return True
        except (DomainError, VideoRuleViolation) as exc:
            self._jobs.mark_item(job_id, group_id, "error", message=str(exc))
            return True
        self._jobs.mark_item(job_id, group_id, "queued", attempt_id=attempt_id)
        return True

    def process_all(
        self,
        job_id: UUID,
        *,
        max_in_flight: int | None = None,
        respect_capacity_queue: bool = False,
    ) -> None:
        """推进整批，但受单项目「在途组数」上限约束。

        S6：旧实现无条件跑到底，一次把 140 组全推成 queued，等价于同时起 140 个
        常驻 Temporal workflow。达到上限后保留 PENDING，由下一次 kick 继续推进，
        从而把常驻 workflow 数量压在单线程 workflow executor 能承受的范围内。

        respect_capacity_queue（AUTO 专用）：有更早的等待容量项目时本轮不认领槽位，
        保证多项目按 job 创建时间先来先服务；MANUAL 手动点击不排队。
        """
        if respect_capacity_queue:
            head = self._jobs.oldest_waiting_job_id()
            if head is not None and head != job_id:
                return
        limit = max_in_flight if max_in_flight is not None else self._in_flight_limit
        if limit is None:
            limit = _in_flight_limit()
        view = self._jobs.get_job(job_id)
        if view is None:
            return
        while True:
            if limit > 0 and self._count_in_flight(view.owner_id, view.project_id) >= limit:
                return
            if not self.process_next(job_id):
                return

    def _count_in_flight(self, owner_id: UUID, project_id: UUID) -> int:
        return sum(
            1
            for state in self._repository.list_group_states(owner_id, project_id)
            if state.latest_attempt_stage in _IN_FLIGHT_STAGES
        )

    def mark_item(
        self, job_id: UUID, group_id: UUID, status: str, *, message: str | None = None
    ) -> None:
        """供 AUTO 编排推进意图重试状态（PENDING / 预算耗尽终态）。"""
        self._jobs.mark_item(job_id, group_id, status, message=message)

    def reopen_failed_items(self, job_id: UUID) -> int:
        """新一轮授权：把上一轮终态失败项翻回 PENDING 重新排队。"""
        return self._jobs.reopen_failed_items(job_id)

    def unfinished_jobs(self) -> tuple[UUID, ...]:
        return self._jobs.list_unfinished_job_ids()
