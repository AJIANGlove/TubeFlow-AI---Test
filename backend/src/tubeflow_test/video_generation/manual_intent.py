from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_generation.orchestration import (
    AuthoritativeGroupInput,
    DurableGroupVideoOrchestrator,
    PersistentAttempt,
)


class HydratedManualH3IntentWriter:
    """Manual API adapter that persists a worker-ready immutable H3 attempt."""

    def __init__(
        self,
        service: DurableGroupVideoOrchestrator,
        sessions: Callable[[], Session],
        group_loader: Callable[[Session, UUID, UUID], AuthoritativeGroupInput],
    ) -> None:
        self._service = service
        self._sessions = sessions
        self._group_loader = group_loader

    def create(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        expected_storyboard_generation_id: UUID,
        idempotency_key: str,
        request: dict[str, object] | None = None,
    ) -> UUID:
        del request
        with self._sessions() as session:
            source = self._group_loader(session, owner_id, group_id)
        if source.storyboard_generation_id != expected_storyboard_generation_id:
            raise DomainError("revision_conflict", "H3 intent requires the current storyboard")
        return self._service.trigger(owner_id, group_id, idempotency_key).attempt_id

    def get(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt | None:
        try:
            return self._service.get(owner_id, attempt_id)
        except ValueError:
            return None
