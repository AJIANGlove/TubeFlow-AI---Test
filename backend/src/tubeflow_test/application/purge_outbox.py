from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PurgeEnvelope:
    id: UUID
    project_id: UUID
    owner_id: UUID
    attempt: int
    workflow_generation: int
    lease_token: UUID


class PurgeOutboxPort(Protocol):
    def claim(self) -> PurgeEnvelope | None: ...
    def succeeded(self, envelope: PurgeEnvelope) -> None: ...
    def failed(self, envelope: PurgeEnvelope, error_code: str) -> None: ...


class DurablePurgeDispatcher(Protocol):
    async def dispatch(
        self, project_id: UUID, owner_id: UUID, workflow_generation: int
    ) -> None: ...


class PurgeOutboxRelay:
    def __init__(self, outbox: PurgeOutboxPort, dispatcher: DurablePurgeDispatcher) -> None:
        self._outbox = outbox
        self._dispatcher = dispatcher

    async def relay_one(self) -> bool:
        envelope = self._outbox.claim()
        if envelope is None:
            return False
        try:
            await self._dispatcher.dispatch(
                envelope.project_id, envelope.owner_id, envelope.workflow_generation
            )
        except Exception:
            self._outbox.failed(envelope, "purge_dispatch_failed")
            return True
        self._outbox.succeeded(envelope)
        return True
