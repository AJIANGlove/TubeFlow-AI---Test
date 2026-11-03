"""AUTO 阶段用时的只读投影（纯函数，无 I/O）。

输入是 `auto_pipeline_runs.payload`（JSON 安全类型）与运行状态；输出给
workflow-summary 的 `auto_pipeline.timing`，不改变任何状态、不做补算。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .domain import AUTO_PIPELINE_PHASES, as_non_negative_int, to_epoch_ms

_WORK_PHASES = tuple(phase for phase in AUTO_PIPELINE_PHASES if phase != "completed")


def phase_timings(
    payload: Mapping[str, object], state: str | None, now: datetime
) -> dict[str, object] | None:
    """投影阶段用时；没有任何计时数据（历史运行）时返回 None。

    - 只暴露已结算阶段与当前阶段；未进入的阶段不出现（页面显示「—」）。
    - 当前阶段在 PAUSED 时冻结在 `paused_at_ms`，RUNNING 时累加到响应时刻。
    - `total_elapsed_ms` 仅当计时从 narration 起完整覆盖时给出；否则 None。
    """
    from_phase = payload.get("timing_from_phase")
    if not isinstance(from_phase, str) or from_phase not in AUTO_PIPELINE_PHASES:
        return None
    now_ms = to_epoch_ms(now)
    phases: dict[str, dict[str, object]] = {}
    raw_elapsed = payload.get("phase_elapsed_ms")
    if isinstance(raw_elapsed, dict):
        for key, value in raw_elapsed.items():
            if key not in _WORK_PHASES:
                continue
            elapsed = as_non_negative_int(value)
            if elapsed is None:
                continue
            phases[key] = {"elapsed_ms": elapsed, "running": False}
    current = payload.get("phase")
    current_started = as_non_negative_int(payload.get("phase_started_ms"))
    if current in _WORK_PHASES and current_started is not None:
        paused_at = as_non_negative_int(payload.get("paused_at_ms"))
        phase_end_ms: int | None
        if state == "RUNNING":
            phase_end_ms = now_ms
        elif state == "PAUSED":
            # 暂停中必须有暂停起点；缺失说明 payload 不完整，不猜测当前阶段用时。
            phase_end_ms = paused_at
        else:
            phase_end_ms = None
        if phase_end_ms is not None:
            paused_in_phase = as_non_negative_int(payload.get("phase_paused_ms")) or 0
            partial = max(0, phase_end_ms - current_started - paused_in_phase)
            existing = phases.get(str(current)) or {}
            previous = as_non_negative_int(existing.get("elapsed_ms")) or 0
            phases[str(current)] = {
                "elapsed_ms": previous + partial,
                "running": state == "RUNNING",
            }
    run_paused = as_non_negative_int(payload.get("run_paused_ms")) or 0
    paused_ms = run_paused
    if state == "PAUSED":
        paused_at = as_non_negative_int(payload.get("paused_at_ms"))
        if paused_at is not None:
            paused_ms += max(0, now_ms - paused_at)
    total_elapsed_ms: int | None = None
    run_started = as_non_negative_int(payload.get("run_started_ms"))
    if run_started is not None and from_phase == "narration":
        completed_ms = as_non_negative_int(payload.get("completed_ms"))
        paused_at = as_non_negative_int(payload.get("paused_at_ms"))
        total_end_ms: int | None
        if state == "COMPLETED":
            total_end_ms = completed_ms
        elif state == "PAUSED":
            total_end_ms = paused_at
        else:
            total_end_ms = now_ms
        if total_end_ms is not None:
            total_elapsed_ms = max(0, total_end_ms - run_started - run_paused)
    return {
        "from_phase": from_phase,
        "total_elapsed_ms": total_elapsed_ms,
        "paused_ms": paused_ms,
        "phases": phases,
    }
