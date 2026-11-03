from __future__ import annotations

from datetime import timedelta

from tubeflow_test.application.ports import Clock, IdentifierFactory
from tubeflow_test.application.storyboard_outbox import (
    StoryboardOutboxRepository,
    StoryboardWorkflowStarter,
)


class StoryboardOutboxDispatcher:
    def __init__(
        self,
        repository: StoryboardOutboxRepository,
        starter: StoryboardWorkflowStarter,
        clock: Clock,
        ids: IdentifierFactory,
        *,
        lease_seconds: int = 30,
    ) -> None:
        self._repository, self._starter = repository, starter
        self._clock, self._ids, self._lease_seconds = clock, ids, lease_seconds

    async def dispatch_one(self) -> bool:
        now = self._clock.now()
        event = self._repository.claim_outbox(
            now, now + timedelta(seconds=self._lease_seconds), self._ids.new()
        )
        if event is None:
            return False
        try:
            await self._starter.start(event)
        except Exception as exc:
            delay = min(300, 2 ** min(event.attempt_count, 8))
            self._repository.retry_outbox(
                event.event_id,
                event.lease_token,
                self._clock.now() + timedelta(seconds=delay),
                type(exc).__name__,
            )
            return False
        return self._repository.acknowledge_outbox(
            event.event_id, event.lease_token, self._clock.now()
        )
