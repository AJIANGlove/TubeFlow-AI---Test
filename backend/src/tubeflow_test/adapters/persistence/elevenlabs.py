"""ElevenLabs 号池的 PostgreSQL 仓储与 InMemory 假件。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    delete,
    select,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from tubeflow_test.adapters.persistence.identity_project.models import Base
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.domain import KeyStatus, QuotaSnapshot
from tubeflow_test.elevenlabs.ports import PoolKeyEntry, PoolKeyEvent


class ElevenlabsKeyRow(Base):
    __tablename__ = "elevenlabs_keys"
    __table_args__ = (UniqueConstraint("api_key", name="uq_elevenlabs_keys_api_key"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    status_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quota_tier: Mapped[str | None] = mapped_column(Text)
    character_count: Mapped[int | None] = mapped_column(BigInteger)
    character_limit: Mapped[int | None] = mapped_column(BigInteger)
    voice_slots_used: Mapped[int | None] = mapped_column(Integer)
    voice_limit: Mapped[int | None] = mapped_column(Integer)
    quota_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ElevenlabsKeyEventRow(Base):
    __tablename__ = "elevenlabs_key_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key_id: Mapped[UUID] = mapped_column(
        ForeignKey("elevenlabs_keys.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _entry(row: ElevenlabsKeyRow) -> PoolKeyEntry:
    return PoolKeyEntry(
        id=row.id,
        api_key=row.api_key,
        label=row.label,
        status=cast(KeyStatus, row.status),
        added_at=row.added_at,
        status_at=row.status_at,
        last_error=row.last_error,
        quota_tier=row.quota_tier,
        character_count=row.character_count,
        character_limit=row.character_limit,
        voice_slots_used=row.voice_slots_used,
        voice_limit=row.voice_limit,
        quota_refreshed_at=row.quota_refreshed_at,
    )


def _event(row: ElevenlabsKeyEventRow) -> PoolKeyEvent:
    return PoolKeyEvent(
        id=row.id,
        key_id=row.key_id,
        event=row.event,
        detail=row.detail,
        created_at=row.created_at,
    )


class SqlElevenlabsKeyPool:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def list_keys(self) -> tuple[PoolKeyEntry, ...]:
        with self._session_factory() as session:
            rows = (
                session.query(ElevenlabsKeyRow)
                .order_by(ElevenlabsKeyRow.added_at, ElevenlabsKeyRow.id)
                .all()
            )
            return tuple(_entry(row) for row in rows)

    def get_key(self, key_id: UUID) -> PoolKeyEntry | None:
        with self._session_factory() as session:
            row = session.get(ElevenlabsKeyRow, key_id)
            return _entry(row) if row is not None else None

    def find_by_api_key(self, api_key: str) -> PoolKeyEntry | None:
        with self._session_factory() as session:
            row = session.query(ElevenlabsKeyRow).filter_by(api_key=api_key).one_or_none()
            return _entry(row) if row is not None else None

    def add_key(self, *, api_key: str, label: str, now: datetime) -> PoolKeyEntry:
        with self._session_factory() as session, session.begin():
            row = ElevenlabsKeyRow(
                api_key=api_key, label=label, status="active", status_at=now, added_at=now
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                raise DomainError("elevenlabs_key_duplicate", "该 API key 已在号池中。") from error
            return _entry(row)

    def set_status(
        self, key_id: UUID, status: KeyStatus, *, last_error: str | None, now: datetime
    ) -> PoolKeyEntry | None:
        with self._session_factory() as session, session.begin():
            row = session.get(ElevenlabsKeyRow, key_id, with_for_update=True)
            if row is None:
                return None
            row.status = status
            row.last_error = last_error
            row.status_at = now
            session.flush()
            return _entry(row)

    def delete_key(self, key_id: UUID) -> bool:
        with self._session_factory() as session, session.begin():
            row = session.get(ElevenlabsKeyRow, key_id)
            if row is None:
                return False
            session.delete(row)
            return True

    def save_quota(self, key_id: UUID, quota: QuotaSnapshot, refreshed_at: datetime) -> None:
        with self._session_factory() as session, session.begin():
            row = session.get(ElevenlabsKeyRow, key_id)
            if row is None:
                return
            row.quota_tier = quota.tier
            row.character_count = quota.character_count
            row.character_limit = quota.character_limit
            row.voice_slots_used = quota.voice_slots_used
            row.voice_limit = quota.voice_limit
            row.quota_refreshed_at = refreshed_at

    def record_event(self, key_id: UUID, event: str, detail: str, now: datetime) -> None:
        with self._session_factory() as session, session.begin():
            session.add(
                ElevenlabsKeyEventRow(key_id=key_id, event=event, detail=detail, created_at=now)
            )

    def list_events(
        self, *, key_id: UUID | None = None, limit: int = 50
    ) -> tuple[PoolKeyEvent, ...]:
        with self._session_factory() as session:
            query = session.query(ElevenlabsKeyEventRow)
            if key_id is not None:
                query = query.filter_by(key_id=key_id)
            rows = query.order_by(ElevenlabsKeyEventRow.id.desc()).limit(limit).all()
            return tuple(_event(row) for row in rows)

    def trim_events(self, key_id: UUID, keep: int) -> None:
        with self._session_factory() as session, session.begin():
            stale = (
                select(ElevenlabsKeyEventRow.id)
                .where(ElevenlabsKeyEventRow.key_id == key_id)
                .order_by(ElevenlabsKeyEventRow.id.desc())
                .offset(keep)
            )
            session.execute(
                delete(ElevenlabsKeyEventRow).where(ElevenlabsKeyEventRow.id.in_(stale))
            )


class InMemoryElevenlabsKeyPool:
    def __init__(self) -> None:
        self._keys: dict[UUID, PoolKeyEntry] = {}
        self._events: list[PoolKeyEvent] = []
        self._next_event_id = 1

    def list_keys(self) -> tuple[PoolKeyEntry, ...]:
        return tuple(sorted(self._keys.values(), key=lambda entry: (entry.added_at, str(entry.id))))

    def get_key(self, key_id: UUID) -> PoolKeyEntry | None:
        return self._keys.get(key_id)

    def find_by_api_key(self, api_key: str) -> PoolKeyEntry | None:
        for entry in self._keys.values():
            if entry.api_key == api_key:
                return entry
        return None

    def add_key(self, *, api_key: str, label: str, now: datetime) -> PoolKeyEntry:
        if self.find_by_api_key(api_key) is not None:
            raise DomainError("elevenlabs_key_duplicate", "该 API key 已在号池中。")
        entry = PoolKeyEntry(
            id=uuid4(),
            api_key=api_key,
            label=label,
            status="active",
            added_at=now,
            status_at=now,
            last_error=None,
            quota_tier=None,
            character_count=None,
            character_limit=None,
            voice_slots_used=None,
            voice_limit=None,
            quota_refreshed_at=None,
        )
        self._keys[entry.id] = entry
        return entry

    def set_status(
        self, key_id: UUID, status: KeyStatus, *, last_error: str | None, now: datetime
    ) -> PoolKeyEntry | None:
        entry = self._keys.get(key_id)
        if entry is None:
            return None
        updated = replace(entry, status=status, last_error=last_error, status_at=now)
        self._keys[key_id] = updated
        return updated

    def delete_key(self, key_id: UUID) -> bool:
        if self._keys.pop(key_id, None) is None:
            return False
        self._events = [item for item in self._events if item.key_id != key_id]
        return True

    def save_quota(self, key_id: UUID, quota: QuotaSnapshot, refreshed_at: datetime) -> None:
        entry = self._keys.get(key_id)
        if entry is None:
            return
        self._keys[key_id] = replace(
            entry,
            quota_tier=quota.tier,
            character_count=quota.character_count,
            character_limit=quota.character_limit,
            voice_slots_used=quota.voice_slots_used,
            voice_limit=quota.voice_limit,
            quota_refreshed_at=refreshed_at,
        )

    def record_event(self, key_id: UUID, event: str, detail: str, now: datetime) -> None:
        self._events.append(
            PoolKeyEvent(
                id=self._next_event_id,
                key_id=key_id,
                event=event,
                detail=detail,
                created_at=now,
            )
        )
        self._next_event_id += 1

    def list_events(
        self, *, key_id: UUID | None = None, limit: int = 50
    ) -> tuple[PoolKeyEvent, ...]:
        rows = [item for item in self._events if key_id is None or item.key_id == key_id]
        rows.sort(key=lambda item: item.id, reverse=True)
        return tuple(rows[:limit])

    def trim_events(self, key_id: UUID, keep: int) -> None:
        stale = {item.id for item in self.list_events(key_id=key_id, limit=1_000_000)[keep:]}
        if stale:
            self._events = [item for item in self._events if item.id not in stale]
