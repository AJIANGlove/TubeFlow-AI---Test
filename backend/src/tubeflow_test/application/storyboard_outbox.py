from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StoryboardOutboxEvent:
    event_id: UUID
    owner_id: UUID
    project_id: UUID
    aggregate_id: UUID
    event_type: str
    request_sha256: str
    workflow_id: str
    lease_token: UUID
    attempt_count: int


class StoryboardOutboxRepository(Protocol):
    def claim_outbox(
        self, now: datetime, lease_until: datetime, token: UUID
    ) -> StoryboardOutboxEvent | None: ...
    def acknowledge_outbox(self, event_id: UUID, token: UUID, now: datetime) -> bool: ...
    def retry_outbox(self, event_id: UUID, token: UUID, next_at: datetime, error: str) -> bool: ...


class StoryboardWorkflowStarter(Protocol):
    async def start(self, event: StoryboardOutboxEvent) -> None: ...
