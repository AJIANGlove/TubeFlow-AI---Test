"""AUTO 一键生产运行记录的纯领域状态机（§15.9）。

状态迁移：RUNNING → PAUSED（预算耗尽/既定失败/实测超长）、RUNNING →
COMPLETED（合成当前版本就绪）、PAUSED → RUNNING（用户恢复=新一轮授权，
authorization_round 递增）。其余迁移一律拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

# 聚合进度视图展示用的稳定阶段标记（存入 payload["phase"]）。
AUTO_PIPELINE_PHASES = (
    "narration",
    "director_preplan",
    "director_materialization",
    "storyboard_drafts",
    "storyboard",
    "video",
    "composition",
    "completed",
)

# 稳定暂停原因码；配音阶段直接沿用 narration blocking_reason 的稳定码。
REASON_DIRECTOR_PREPLAN_FAILED = "director_preplan_failed"
REASON_DIRECTOR_VALIDATION_FAILED = "director_validation_failed"
REASON_OVERSIZED_SENTENCE = "oversized_sentence"
REASON_COMPOSITION_FAILED = "composition_failed"
# 2026-10-31：H3 容量不足改为排队等待；等待超过上限才暂停并给出该原因。
REASON_VIDEO_CAPACITY_TIMEOUT = "video_capacity_timeout"


def to_epoch_ms(now: datetime) -> int:
    """UTC 时间 → epoch 毫秒。

    `auto_pipeline_runs.payload` 是 JSON 列（未配置 json_serializer），
    datetime 无法序列化；阶段计时只允许写入 int/str/list/dict。
    """
    return int(now.timestamp() * 1000)


def as_non_negative_int(value: object) -> int | None:
    """防御式读取 payload 计时字段：只接受非负 int（bool 视为非法）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _enter_phase(payload: dict[str, object], phase: str, now_ms: int) -> dict[str, object]:
    """阶段切换统一入口：结算上一阶段、重置当前阶段起点。

    - 同阶段直接返回原 payload（`_touch` 的空写抑制依赖该语义）；
    - 历史 payload 没有 phase_started_ms 时不做任何补算，只从本次切换开始计时；
    - `completed` 且无起点（历史运行完成）不写入无意义的计时键。
    """
    if payload.get("phase") == phase:
        return payload
    updated = dict(payload)
    started = as_non_negative_int(updated.get("phase_started_ms"))
    if started is not None:
        previous = payload.get("phase")
        if (
            isinstance(previous, str)
            and previous in AUTO_PIPELINE_PHASES
            and previous != "completed"
        ):
            paused = as_non_negative_int(updated.get("phase_paused_ms")) or 0
            raw_totals = updated.get("phase_elapsed_ms")
            totals = dict(raw_totals) if isinstance(raw_totals, dict) else {}
            existing = as_non_negative_int(totals.get(previous)) or 0
            totals[previous] = existing + max(0, now_ms - started - paused)
            updated["phase_elapsed_ms"] = totals
        updated["phase_started_ms"] = now_ms
        updated["phase_paused_ms"] = 0
    elif phase != "completed":
        # 历史运行首次观测到阶段切换：从这里开始计时；之前阶段永不补算。
        updated["timing_from_phase"] = phase
        updated["phase_started_ms"] = now_ms
        updated["phase_paused_ms"] = 0
        updated.setdefault("run_paused_ms", 0)
    updated["phase"] = phase
    return updated


def storyboard_failed_reason(failure_code: str | None) -> str:
    """故事板组终态失败（预算耗尽/既定失败）的稳定原因码。"""
    code = (failure_code or "").strip() or "failed"
    return f"storyboard_failed:{code}"


def video_failed_reason(error_code: str | None) -> str:
    """组视频终态失败（预算耗尽/既定失败）的稳定原因码。"""
    code = (error_code or "").strip() or "failed"
    return f"video_failed:{code}"


class AutoPipelineState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


_ALLOWED_TRANSITIONS: dict[AutoPipelineState, frozenset[AutoPipelineState]] = {
    AutoPipelineState.RUNNING: frozenset({AutoPipelineState.PAUSED, AutoPipelineState.COMPLETED}),
    AutoPipelineState.PAUSED: frozenset({AutoPipelineState.RUNNING}),
    AutoPipelineState.COMPLETED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class AutoPipelineRun:
    """一个 AUTO 项目的一次一键生产运行；payload 只保存阶段书签与暂停细节。"""

    run_id: UUID
    owner_id: UUID
    project_id: UUID
    state: AutoPipelineState
    reason_code: str | None
    authorization_round: int
    payload: dict[str, object]
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.authorization_round < 1:
            raise ValueError("auto pipeline authorization round must be positive")
        if self.state is AutoPipelineState.PAUSED:
            if not self.reason_code or not self.reason_code.strip():
                raise ValueError("a paused auto pipeline run requires a stable reason code")
        elif self.reason_code is not None:
            raise ValueError("only a paused auto pipeline run carries a reason code")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def paused(
        self,
        reason_code: str,
        now: datetime,
        *,
        phase: str,
        details: dict[str, object] | None = None,
    ) -> AutoPipelineRun:
        """RUNNING → PAUSED；payload 合并阶段书签与暂停细节（如超长句序）。"""
        self._assert_transition(AutoPipelineState.PAUSED)
        now_ms = to_epoch_ms(now)
        # 暂停也可能直接改写阶段书签（如 oversized_sentence 从 narration 跳 video）；
        # 先合并 details，再按同一切换口径结算上一阶段，避免把旧阶段时长算进新阶段。
        payload = _enter_phase(
            {**self.payload, **(details or {})}, phase, now_ms
        )
        if as_non_negative_int(payload.get("phase_started_ms")) is not None:
            payload["paused_at_ms"] = now_ms
        return replace(
            self,
            state=AutoPipelineState.PAUSED,
            reason_code=reason_code,
            payload=payload,
            updated_at=now,
        )

    def resumed(self, authorization_round: int, now: datetime) -> AutoPipelineRun:
        """PAUSED → RUNNING；授权轮次递增并清空暂停原因（§15.9 预算重置）。"""
        self._assert_transition(AutoPipelineState.RUNNING)
        if authorization_round <= self.authorization_round:
            raise ValueError("auto pipeline resume must advance the authorization round")
        now_ms = to_epoch_ms(now)
        payload = dict(self.payload)
        paused_at = as_non_negative_int(payload.pop("paused_at_ms", None))
        if paused_at is not None:
            # 暂停期间不计入阶段用时；跨恢复累计，不重置当前阶段起点。
            delta = max(0, now_ms - paused_at)
            payload["run_paused_ms"] = (
                as_non_negative_int(payload.get("run_paused_ms")) or 0
            ) + delta
            payload["phase_paused_ms"] = (
                as_non_negative_int(payload.get("phase_paused_ms")) or 0
            ) + delta
        return replace(
            self,
            state=AutoPipelineState.RUNNING,
            reason_code=None,
            authorization_round=authorization_round,
            payload=payload,
            updated_at=now,
        )

    def completed(self, now: datetime) -> AutoPipelineRun:
        """RUNNING → COMPLETED（合成当前版本就绪）。"""
        self._assert_transition(AutoPipelineState.COMPLETED)
        now_ms = to_epoch_ms(now)
        payload = _enter_phase(dict(self.payload), "completed", now_ms)
        if as_non_negative_int(payload.get("phase_started_ms")) is not None:
            payload["completed_ms"] = now_ms
            payload["phase_started_ms"] = None
            payload["phase_paused_ms"] = 0
            payload.pop("paused_at_ms", None)
        return replace(
            self,
            state=AutoPipelineState.COMPLETED,
            payload=payload,
            updated_at=now,
        )

    def touched(
        self,
        now: datetime,
        *,
        phase: str,
        bookmarks: dict[str, object] | None = None,
    ) -> AutoPipelineRun:
        """同态推进书签（阶段标记/任务 ID）；不产生状态迁移。"""
        if phase not in AUTO_PIPELINE_PHASES:
            raise ValueError("auto pipeline phase is invalid")
        now_ms = to_epoch_ms(now)
        payload = _enter_phase(
            {**self.payload, **(bookmarks or {})}, phase, now_ms
        )
        return replace(
            self,
            payload=payload,
            updated_at=now,
        )

    def _assert_transition(self, target: AutoPipelineState) -> None:
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(
                f"auto pipeline transition {self.state.value} -> {target.value} is invalid"
            )


def new_auto_pipeline_run(
    run_id: UUID,
    owner_id: UUID,
    project_id: UUID,
    now: datetime,
) -> AutoPipelineRun:
    """确认配音事务内创建的初始 RUNNING 运行（第 1 轮授权）。"""
    now_ms = to_epoch_ms(now)
    return AutoPipelineRun(
        run_id,
        owner_id,
        project_id,
        AutoPipelineState.RUNNING,
        None,
        1,
        {
            "phase": "narration",
            # 阶段计时初值：run_started_ms 同时作为总用时起点。
            "run_started_ms": now_ms,
            "timing_from_phase": "narration",
            "phase_started_ms": now_ms,
            "phase_elapsed_ms": {},
            "phase_paused_ms": 0,
            "run_paused_ms": 0,
        },
        now,
        now,
    )
