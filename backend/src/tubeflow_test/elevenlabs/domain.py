"""ElevenLabs 号池与配额的纯领域规则（无框架、网络、数据库依赖）。

判定矩阵来源：docs/GUIDE_elevenlabs.md §2.3（旧项目生产实证），
决策来源：docs/planning/USER_DECISIONS.md §23.6。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

KeyStatus = Literal["active", "exhausted", "invalid", "disabled"]
FailureClassification = Literal["exhausted", "invalid", "transient"]

KEY_STATUSES: tuple[str, ...] = ("active", "exhausted", "invalid", "disabled")

EVENT_CAP = 50
EVENT_DEDUP_SECONDS = 60
LAST_ERROR_MAX_CHARS = 500


@dataclass(frozen=True, slots=True)
class PoolKey:
    id: UUID
    label: str
    status: KeyStatus
    added_at: datetime
    status_at: datetime
    last_error: str | None


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    tier: str
    character_count: int
    character_limit: int
    voice_slots_used: int
    voice_limit: int
    status: str
    can_use_instant_voice_cloning: bool
    next_reset_unix: int | None


@dataclass(frozen=True, slots=True)
class RecheckOutcome:
    """401 复核（免费订阅接口）结果。

    reachable=False 表示复核本身不可达（网络/SDK 异常）；rejected=True 表示
    复核亦被 401/403 拒绝。
    """

    reachable: bool
    rejected: bool


def classify_key_failure(
    status_code: int | None, recheck: RecheckOutcome | None
) -> FailureClassification:
    """号池失败判定矩阵：只有确认是"账号病"才标 key；请求级瞬态永不标 key。

    - 402 → exhausted（欠费，语义确定，不查订阅）
    - 401 + 复核亦拒（401/403）→ invalid（key 真失效）
    - 401 + 复核可读 → exhausted（EL 额度耗尽也返 401，订阅接口仍可读 = key 活着）
    - 401 + 复核不可达/未复核 → transient（宁可不标也不错标）
    - 403 / 409 / 5xx / 无状态码 → transient（请求级：内容审核、音色操作并发等）
    """
    if status_code == 402:
        return "exhausted"
    if status_code == 401:
        if recheck is None or not recheck.reachable:
            return "transient"
        return "invalid" if recheck.rejected else "exhausted"
    return "transient"


def select_current_key(members: tuple[PoolKey, ...]) -> PoolKey | None:
    """号池无"当前指针"：取 added_at 最早的 active key；无 active 返回 None。"""
    active = [member for member in members if member.status == "active"]
    if not active:
        return None
    return min(active, key=lambda member: (member.added_at, str(member.id)))


def truncate_last_error(detail: str) -> str:
    return detail[:LAST_ERROR_MAX_CHARS]


def event_duplicate(
    recent: tuple[tuple[str, datetime], ...], event: str, now: datetime
) -> bool:
    """同 key 同内容 60s 去重（防刷屏冲掉事件历史）。recent 为 (event, created_at)。"""
    for past_event, created_at in recent:
        if past_event == event and abs((now - created_at).total_seconds()) < EVENT_DEDUP_SECONDS:
            return True
    return False
