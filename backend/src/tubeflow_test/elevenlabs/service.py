"""ElevenLabs 号池编排：入库验证、状态流转、失败轮换、配额缓存与事件流水。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from tubeflow_test.domain.errors import DomainError

from .domain import (
    EVENT_CAP,
    FailureClassification,
    KeyStatus,
    PoolKey,
    RecheckOutcome,
    classify_key_failure,
    event_duplicate,
    select_current_key,
    truncate_last_error,
)
from .ports import ElevenlabsKeyPoolPort, ElevenlabsQuotaPort, PoolKeyEntry, PoolKeyEvent

EVENT_ADDED = "added"
EVENT_ENABLED = "enabled"
EVENT_DISABLED = "disabled"
EVENT_MARKED_EXHAUSTED = "marked_exhausted"
EVENT_MARKED_INVALID = "marked_invalid"
EVENT_TRANSIENT_FAILURE = "transient_failure"
EVENT_QUOTA_REFRESHED = "quota_refreshed"
EVENT_QUOTA_REFRESH_FAILED = "quota_refresh_failed"


class ElevenlabsKeyPoolService:
    def __init__(
        self,
        pool: ElevenlabsKeyPoolPort,
        quota: ElevenlabsQuotaPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._quota = quota
        self._clock = clock or (lambda: datetime.now(UTC))

    def list_keys(self) -> tuple[PoolKeyEntry, ...]:
        return self._pool.list_keys()

    def list_events(self, *, limit: int = 50) -> tuple[PoolKeyEvent, ...]:
        return self._pool.list_events(limit=limit)

    def current_key(self) -> PoolKeyEntry | None:
        members = tuple(
            PoolKey(
                entry.id,
                entry.label,
                entry.status,
                entry.added_at,
                entry.status_at,
                entry.last_error,
            )
            for entry in self._pool.list_keys()
        )
        selected = select_current_key(members)
        return self._pool.get_key(selected.id) if selected is not None else None

    def active_keys(self) -> tuple[PoolKeyEntry, ...]:
        """全部 active key，按 added_at 最早优先（与 current_key 的选择顺序一致）。"""
        members = tuple(
            PoolKey(
                entry.id,
                entry.label,
                entry.status,
                entry.added_at,
                entry.status_at,
                entry.last_error,
            )
            for entry in self._pool.list_keys()
        )
        ordered = sorted(
            (item for item in members if item.status == "active"),
            key=lambda member: (member.added_at, str(member.id)),
        )
        entries = tuple(self._pool.get_key(item.id) for item in ordered)
        return tuple(entry for entry in entries if entry is not None)

    def key_entry(self, key_id: UUID) -> PoolKeyEntry | None:
        return self._pool.get_key(key_id)

    async def refresh_slot_quota(self, key_id: UUID) -> PoolKeyEntry | None:
        """音色槽位自检前的免费配额刷新（§23.5）；key 不存在返回 None。"""
        entry = self._pool.get_key(key_id)
        if entry is None:
            return None
        try:
            quota = await self._quota.subscription(entry.api_key)
        except DomainError as error:
            if error.code == "elevenlabs_key_invalid":
                self._mark(key_id, "invalid", error.message)
            else:
                self._record_event(
                    key_id, EVENT_QUOTA_REFRESH_FAILED, f"{error.code}: {error.message}"
                )
            return None
        self._pool.save_quota(key_id, quota, self._clock())
        return self._pool.get_key(key_id)

    async def add_key(self, *, api_key: str, label: str = "") -> PoolKeyEntry:
        api_key = api_key.strip()
        if not api_key:
            raise DomainError("elevenlabs_key_invalid", "API key 为空。")
        if self._pool.find_by_api_key(api_key) is not None:
            raise DomainError("elevenlabs_key_duplicate", "该 API key 已在号池中。")
        quota = await self._quota.subscription(api_key)
        entry = self._pool.add_key(api_key=api_key, label=label.strip(), now=self._clock())
        self._pool.save_quota(entry.id, quota, self._clock())
        self._record_event(entry.id, EVENT_ADDED, f"tier={quota.tier}")
        return self._require(entry.id)

    async def enable(self, key_id: UUID) -> PoolKeyEntry:
        entry = self._require(key_id)
        if entry.status != "active":
            self._pool.set_status(key_id, "active", last_error=None, now=self._clock())
            self._record_event(key_id, EVENT_ENABLED, f"from {entry.status}")
        try:
            quota = await self._quota.subscription(entry.api_key)
        except DomainError as error:
            self._record_event(key_id, EVENT_QUOTA_REFRESH_FAILED, f"{error.code}: {error.message}")
        else:
            self._pool.save_quota(key_id, quota, self._clock())
        return self._require(key_id)

    def disable(self, key_id: UUID) -> PoolKeyEntry:
        entry = self._require(key_id)
        if entry.status == "disabled":
            return entry
        self._pool.set_status(key_id, "disabled", last_error=entry.last_error, now=self._clock())
        self._record_event(key_id, EVENT_DISABLED, f"from {entry.status}")
        return self._require(key_id)

    def delete(self, key_id: UUID) -> None:
        if not self._pool.delete_key(key_id):
            raise DomainError("not_found", "Resource not found.")

    async def refresh_quota(self, key_id: UUID) -> PoolKeyEntry:
        entry = self._require(key_id)
        try:
            quota = await self._quota.subscription(entry.api_key)
        except DomainError as error:
            if error.code == "elevenlabs_key_invalid":
                self._mark(key_id, "invalid", error.message)
            else:
                self._record_event(
                    key_id, EVENT_QUOTA_REFRESH_FAILED, f"{error.code}: {error.message}"
                )
            raise
        self._pool.save_quota(key_id, quota, self._clock())
        self._record_event(
            key_id,
            EVENT_QUOTA_REFRESHED,
            f"tier={quota.tier} characters={quota.character_count}/{quota.character_limit}",
        )
        if 0 < quota.character_limit <= quota.character_count:
            self._mark(
                key_id,
                "exhausted",
                f"characters {quota.character_count}/{quota.character_limit}",
            )
        return self._require(key_id)

    def rotate_on_failure(
        self,
        key_id: UUID,
        *,
        status_code: int | None,
        recheck: RecheckOutcome | None,
        detail: str,
    ) -> FailureClassification:
        entry = self._require(key_id)
        classification = classify_key_failure(status_code, recheck)
        if classification == "transient":
            self._record_event(key_id, EVENT_TRANSIENT_FAILURE, truncate_last_error(detail))
            return classification
        if entry.status != classification:
            self._mark(key_id, classification, detail)
        return classification

    def _require(self, key_id: UUID) -> PoolKeyEntry:
        entry = self._pool.get_key(key_id)
        if entry is None:
            raise DomainError("not_found", "Resource not found.")
        return entry

    def _mark(self, key_id: UUID, status: KeyStatus, detail: str) -> None:
        entry = self._require(key_id)
        if entry.status == status:
            return
        truncated = truncate_last_error(detail)
        self._pool.set_status(key_id, status, last_error=truncated, now=self._clock())
        event = EVENT_MARKED_EXHAUSTED if status == "exhausted" else EVENT_MARKED_INVALID
        self._record_event(key_id, event, truncated)

    def _record_event(self, key_id: UUID, event: str, detail: str) -> None:
        now = self._clock()
        recent = tuple(
            (item.event, item.created_at)
            for item in self._pool.list_events(key_id=key_id, limit=EVENT_CAP)
        )
        if event_duplicate(recent, event, now):
            return
        self._pool.record_event(key_id, event, detail, now)
        self._pool.trim_events(key_id, EVENT_CAP)
