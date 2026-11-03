from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from tubeflow_test.application.storyboards import (
    StoryboardGenerationService,
    StoryboardRecoveryCandidate,
    StoryboardRepository,
)

logger = logging.getLogger(__name__)


class StoryboardRecoveryProbe(Protocol):
    async def is_active(self, workflow_id: str) -> bool | None: ...


class StoryboardRecoverySupervisor:
    def __init__(
        self,
        repository: StoryboardRepository,
        generations: StoryboardGenerationService,
        probe: StoryboardRecoveryProbe,
        since: datetime,
        on_inconclusive: Callable[[StoryboardRecoveryCandidate], None] | None = None,
    ) -> None:
        self._repository = repository
        self._generations = generations
        self._probe = probe
        self._since = since
        self._on_inconclusive = on_inconclusive

    async def sweep_once(self) -> int:
        requeued = 0
        for candidate in self._repository.list_unknown_recovery_candidates(self._since):
            if candidate.has_pending_recovery:
                continue
            if candidate.latest_workflow_id is not None:
                active = await self._probe.is_active(candidate.latest_workflow_id)
                if active is None:
                    self._notify_inconclusive(candidate)
                    continue
                if active:
                    continue
            try:
                self._generations.request_recovery(
                    candidate.generation.owner_id, candidate.generation.generation_id
                )
            except Exception:
                logger.exception(
                    "storyboard recovery requeue failed: generation=%s",
                    candidate.generation.generation_id,
                )
                self._notify_inconclusive(candidate)
                continue
            requeued += 1
        return requeued

    def _notify_inconclusive(self, candidate: StoryboardRecoveryCandidate) -> None:
        if self._on_inconclusive is not None:
            self._on_inconclusive(candidate)
