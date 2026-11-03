"""AUTO 编排器端口：权威事实只读视图、运行记录仓储与既有阶段服务的最小接口。

编排器不复制任何阶段逻辑，只读取权威事实并调用既有幂等服务
（StoryboardBatchService / GroupVideoBatchService / 合成入队 / 前置导演恢复运行）。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from tubeflow_test.application.storyboard_batch import (
    StoryboardBatchGenerateResult,
    StoryboardBatchGroupOverview,
    StoryboardBatchSeedResult,
)
from tubeflow_test.application.video_batch import VideoBatchJobView

from .domain import AutoPipelineRun


@dataclass(frozen=True, slots=True)
class OversizedGroupFact:
    """video_blocked 配音分组的暂停展示事实（§15.7：句序与实测时长）。"""

    group_id: UUID
    group_sequence: int
    oversized_sentences: tuple[tuple[int, int], ...]  # (sentence_sequence, measured_duration_ms)


@dataclass(frozen=True, slots=True)
class StoryboardGroupFact:
    group_id: UUID
    group_sequence: int
    latest_status: str | None
    latest_failure_code: str | None
    latest_budget_round: int


@dataclass(frozen=True, slots=True)
class VideoGroupFact:
    """单个未阻止分组的视频权威事实；latest_* 为该组最新尝试。"""

    group_id: UUID
    group_sequence: int
    has_current_video: bool
    latest_stage: str | None
    latest_archive_stage: str | None
    latest_error_code: str | None
    latest_budget_round: int
    # 当前授权轮内该组已实际提交的尝试数（不含意图阶段失败）；
    # C3 的 6 次预算 = 该值 + 意图阶段失败次数。
    submissions: int = 0

    @property
    def in_flight(self) -> bool:
        if self.latest_stage is None:
            return False
        if self.latest_stage == "SUCCEEDED" and self.latest_archive_stage == "QC_FAILED":
            return False
        return self.latest_stage in _VIDEO_IN_FLIGHT_STAGES

    def terminal_failure_code(self, budget_round: int) -> str | None:
        """当前授权轮内的终态失败码；历史轮次或未终态返回 None。"""
        if self.latest_budget_round != budget_round:
            return None
        if self.latest_stage == "SUCCEEDED" and self.latest_archive_stage != "QC_FAILED":
            return None
        if self.latest_stage in {"FAILED", "CANCELLED"} or (
            self.latest_stage == "SUCCEEDED" and self.latest_archive_stage == "QC_FAILED"
        ):
            return self.latest_error_code or "failed"
        return None

    @property
    def needs_submission(self) -> bool:
        return not self.has_current_video and not self.in_flight


_VIDEO_IN_FLIGHT_STAGES = frozenset(
    {
        "CREATED",
        "STAGING",
        "NOT_SUBMITTED",
        "SUBMITTING",
        "SUBMITTED",
        "RUNNING",
        "REMOTE_STATE_UNKNOWN",
        "SUCCEEDED",
    }
)

_STORYBOARD_IN_FLIGHT_STATUSES = frozenset(
    {
        "INTENT_PERSISTED",
        "SUBMITTING",
        "SUBMITTED",
        "RUNNING",
        "REMOTE_STATE_UNKNOWN",
        "DOWNLOADED",
        "QC_PASSED",
        "OBJECT_STORED",
    }
)


@dataclass(frozen=True, slots=True)
class AutoPipelineFacts:
    """advance 决策所需的全部权威事实快照（一次读取）。"""

    production_mode: str
    budget_round: int
    narration_ready: bool
    narration_blocking: dict[int, str] | None
    oversized_groups: tuple[OversizedGroupFact, ...]
    director_preplan_done: bool
    director_latest_run_id: UUID | None
    director_latest_status: str | None
    director_latest_failure_code: str | None
    materialization_ready: bool
    storyboards_complete: bool
    storyboard_groups: tuple[StoryboardGroupFact, ...]
    videos_complete: bool
    video_groups: tuple[VideoGroupFact, ...]
    composition_current_exists: bool
    composition_in_flight: bool
    composition_failed_code: str | None  # 仅当前授权轮内的既定终态失败

    def storyboard_terminal_failures(self) -> tuple[StoryboardGroupFact, ...]:
        """当前授权轮内终态 FAILED 的组；历史轮次的失败在恢复后不再阻断。"""
        return tuple(
            group
            for group in self.storyboard_groups
            if group.latest_status == "FAILED"
            and group.latest_budget_round == self.budget_round
        )

    def storyboard_in_flight_group_ids(self) -> frozenset[UUID]:
        """存在非终态生成的组（提交/在途/未知对账中）；这些组由批量服务逐组跳过。"""
        return frozenset(
            group.group_id
            for group in self.storyboard_groups
            if group.latest_status in _STORYBOARD_IN_FLIGHT_STATUSES
        )

    def video_terminal_failures(self) -> tuple[VideoGroupFact, ...]:
        return tuple(
            group
            for group in self.video_groups
            if not group.has_current_video
            and group.terminal_failure_code(self.budget_round) is not None
        )

    def video_needs_submission(self) -> tuple[VideoGroupFact, ...]:
        return tuple(
            group
            for group in self.video_groups
            if group.needs_submission
            and group.terminal_failure_code(self.budget_round) is None
        )


class AutoPipelineFactsPort(Protocol):
    def load(self, owner_id: UUID, project_id: UUID) -> AutoPipelineFacts: ...


class AutoPipelineRunLock(Protocol):
    """锁内的活动运行视图；save 在同一事务内持久化（持锁期间 advance 互斥）。"""

    @property
    def run(self) -> AutoPipelineRun | None: ...

    def save(self, run: AutoPipelineRun) -> None: ...


class AutoPipelineRunRepository(Protocol):
    def create_active(self, run: AutoPipelineRun) -> AutoPipelineRun:
        """幂等创建：已有 RUNNING/PAUSED 运行时返回既有记录。"""
        ...

    def active_run(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None: ...
    def latest_run(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None: ...
    def list_running(self) -> tuple[AutoPipelineRun, ...]: ...

    def lock_active(
        self, owner_id: UUID, project_id: UUID
    ) -> AbstractContextManager[AutoPipelineRunLock]:
        """对活动运行加行锁（advance 互斥）；无活动时 run 为 None。"""
        ...

    def resume_paused(
        self, owner_id: UUID, project_id: UUID, *, now: datetime
    ) -> AutoPipelineRun:
        """原子恢复：校验 PAUSED、项目预算轮次 +1、运行置 RUNNING（同事务）。"""
        ...

    def resume_paused_for_reason(
        self, owner_id: UUID, project_id: UUID, *, reason_code: str, now: datetime
    ) -> AutoPipelineRun | None:
        """按原因码守卫的原子恢复；不 PAUSED 或原因码不匹配时返回 None。"""
        ...


class NarrationAutoResumePort(Protocol):
    def resume_auto_narration(self, owner_id: UUID, project_id: UUID) -> dict[int, str]:
        """按当前预算轮次重估未解决句子并重新入队；返回仍阻断的 {句序: 稳定码}。"""
        ...


class DirectorAutoResumePort(Protocol):
    def create_recovery_run(self, owner_id: UUID, project_id: UUID, *, round: int) -> None:
        """按既有前缀复用机制创建新一轮前置导演运行（确定性幂等键）。"""
        ...

    def materialize_if_ready(self, owner_id: UUID, project_id: UUID) -> None:
        """确定性重映射（无外部 AI 调用，canonical 幂等）；不就绪时为空操作。"""
        ...


class StoryboardAutoPort(Protocol):
    def overview(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardBatchGroupOverview, ...]: ...

    def seed_drafts(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        auto_approve_ambiguous: bool = False,
    ) -> tuple[StoryboardBatchSeedResult, ...]: ...

    def repair_ambiguous_drafts(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardBatchSeedResult, ...]:
        """为仍处 REQUIRED 的既有草稿附加系统审批并取代修订；返回逐组结果。"""
        ...

    def generate(
        self, owner_id: UUID, project_id: UUID, group_ids: tuple[UUID, ...], *, force: bool
    ) -> tuple[StoryboardBatchGenerateResult, ...]: ...


class VideoAutoPort(Protocol):
    def unfinished_jobs(self) -> tuple[UUID, ...]: ...
    def get_job(self, owner_id: UUID, job_id: UUID) -> VideoBatchJobView | None: ...

    def start(
        self, owner_id: UUID, project_id: UUID, group_ids: tuple[UUID, ...], *, force: bool
    ) -> VideoBatchJobView: ...

    def process_all(
        self, job_id: UUID, *, respect_capacity_queue: bool = False
    ) -> None: ...

    def reopen_failed_items(self, job_id: UUID) -> int: ...

    def mark_item(
        self, job_id: UUID, group_id: UUID, status: str, *, message: str | None = None
    ) -> None:
        """C3：推进意图重试状态（PENDING / 预算耗尽终态）。"""
        ...


class CompositionAutoPort(Protocol):
    def start(self, owner_id: UUID, project_id: UUID) -> None:
        """按 (inputs_hash, 预算轮次) 幂等入队合成。"""
        ...

    def close_superseded(self, owner_id: UUID, project_id: UUID) -> None:
        """合成目标达成（当前成片存在）后收口该项目全部非终态 outbox 行。"""
        ...
