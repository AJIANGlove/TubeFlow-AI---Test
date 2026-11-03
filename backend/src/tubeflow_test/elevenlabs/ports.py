"""ElevenLabs 号池的端口与出入仓储的记录类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from .domain import KeyStatus, QuotaSnapshot


@dataclass(frozen=True, slots=True)
class PoolKeyEntry:
    id: UUID
    api_key: str
    label: str
    status: KeyStatus
    added_at: datetime
    status_at: datetime
    last_error: str | None
    quota_tier: str | None
    character_count: int | None
    character_limit: int | None
    voice_slots_used: int | None
    voice_limit: int | None
    quota_refreshed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PoolKeyEvent:
    id: int
    key_id: UUID
    event: str
    detail: str
    created_at: datetime


class ElevenlabsKeyPoolPort(Protocol):
    def list_keys(self) -> tuple[PoolKeyEntry, ...]: ...
    def get_key(self, key_id: UUID) -> PoolKeyEntry | None: ...
    def find_by_api_key(self, api_key: str) -> PoolKeyEntry | None: ...
    def add_key(self, *, api_key: str, label: str, now: datetime) -> PoolKeyEntry: ...
    def set_status(
        self, key_id: UUID, status: KeyStatus, *, last_error: str | None, now: datetime
    ) -> PoolKeyEntry | None: ...
    def delete_key(self, key_id: UUID) -> bool: ...
    def save_quota(self, key_id: UUID, quota: QuotaSnapshot, refreshed_at: datetime) -> None: ...
    def record_event(self, key_id: UUID, event: str, detail: str, now: datetime) -> None: ...
    def list_events(
        self, *, key_id: UUID | None = None, limit: int = 50
    ) -> tuple[PoolKeyEvent, ...]: ...
    def trim_events(self, key_id: UUID, keep: int) -> None: ...


class ElevenlabsQuotaPort(Protocol):
    async def subscription(self, api_key: str) -> QuotaSnapshot: ...
