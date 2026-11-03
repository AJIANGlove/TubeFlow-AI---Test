from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from tubeflow_test.video_generation.orchestration import AuthoritativeGroupInput


class NarrationAuthorityPort(Protocol):
    """Latest stable-UUID sentence/group authority; legacy production rows are forbidden."""

    def load_group(
        self, session: Session, owner_id: UUID, group_id: UUID
    ) -> AuthoritativeGroupInput: ...


class StoryboardAuthorityPort(Protocol):
    """Binds the latest approved board/version/current pointer to narration authority."""

    def bind_current(
        self, session: Session, narration: AuthoritativeGroupInput
    ) -> AuthoritativeGroupInput: ...


class VideoWriteGuard(Protocol):
    def __call__(self, owner_id: UUID, project_id: UUID) -> None: ...


class ReferencePolicyAuthorityPort(Protocol):
    def apply_reference_policy(
        self,
        session: Session,
        narration: AuthoritativeGroupInput,
        *,
        for_intent: bool,
    ) -> AuthoritativeGroupInput: ...


class LatestAuthorityLoader:
    def __init__(
        self,
        narration: NarrationAuthorityPort,
        storyboard: StoryboardAuthorityPort,
        reference_policy: ReferencePolicyAuthorityPort | None = None,
    ) -> None:
        self._narration = narration
        self._storyboard = storyboard
        self._reference_policy = reference_policy

    def __call__(
        self,
        session: Session,
        owner_id: UUID,
        group_id: UUID,
        *,
        for_intent: bool = False,
    ) -> AuthoritativeGroupInput:
        narration = self._narration.load_group(session, owner_id, group_id)
        bound = self._storyboard.bind_current(session, narration)
        if self._reference_policy is None:
            return bound
        return self._reference_policy.apply_reference_policy(
            session, bound, for_intent=for_intent
        )

    def apply_reference_policy(
        self,
        session: Session,
        narration: AuthoritativeGroupInput,
        *,
        for_intent: bool,
    ) -> AuthoritativeGroupInput:
        if self._reference_policy is None:
            return narration
        return self._reference_policy.apply_reference_policy(
            session, narration, for_intent=for_intent
        )
