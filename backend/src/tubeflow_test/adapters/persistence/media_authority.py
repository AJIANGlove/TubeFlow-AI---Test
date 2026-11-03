from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from tubeflow_test.application.media_authority import (
    CompositionProjectSnapshot,
    LatestFeatureCompositionInputs,
    LatestH3Group,
    LatestNarrationSentence,
    RelationalCurrentH3Reader,
    RelationalNarrationReader,
    RelationalSnapshotReader,
)
from tubeflow_test.domain.composition import CompositionRequest

from .media_repository import SessionFactory


class SqlTransactionalCompositionInputs:
    """Reads the three latest feature authorities in one database transaction.

    Concrete central table mappings implement the three narrow readers. This adapter
    intentionally has no import or fallback for legacy production version tables.
    """

    def __init__(
        self,
        sessions: SessionFactory,
        narration: RelationalNarrationReader[Session],
        h3: RelationalCurrentH3Reader[Session],
        snapshots: RelationalSnapshotReader[Session],
    ) -> None:
        self._sessions = sessions
        self._narration = narration
        self._h3 = h3
        self._snapshots = snapshots

    def load(self, project_id: UUID, owner_id: UUID) -> CompositionRequest | None:
        with self._sessions() as session, session.begin():
            narration = self._narration.timeline_in_session(session, project_id, owner_id)
            groups = self._h3.current_archived_groups_in_session(session, project_id, owner_id)
            snapshot = self._snapshots.snapshot_in_session(session, project_id, owner_id)
        return LatestFeatureCompositionInputs(
            _FixedNarration(narration), _FixedH3(groups), _FixedSnapshot(snapshot)
        ).load(project_id, owner_id)


class _FixedNarration:
    def __init__(self, value: tuple[LatestNarrationSentence, ...]) -> None:
        self._value = value

    def timeline(self, project_id: UUID, owner_id: UUID) -> tuple[LatestNarrationSentence, ...]:
        return self._value


class _FixedH3:
    def __init__(self, value: tuple[LatestH3Group, ...]) -> None:
        self._value = value

    def current_groups(self, project_id: UUID, owner_id: UUID) -> tuple[LatestH3Group, ...]:
        return self._value


class _FixedSnapshot:
    def __init__(self, value: CompositionProjectSnapshot | None) -> None:
        self._value = value

    def snapshot(self, project_id: UUID, owner_id: UUID) -> CompositionProjectSnapshot | None:
        return self._value
