from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_storyboard_auto_retry import (
    NOW,
    create,
    insert_project,
    minimal_generation,
    setup,
    sql_repositories,
)

from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.adapters.persistence.storyboards.models import StoryboardOutboxRow
from tubeflow_test.application.storyboard_recovery import StoryboardRecoverySupervisor
from tubeflow_test.domain.storyboards import GenerationStatus


class Probe:
    def __init__(self, active: bool | None) -> None:
        self.active = active
        self.calls: list[str] = []

    async def is_active(self, workflow_id: str) -> bool | None:
        self.calls.append(workflow_id)
        return self.active


def unknown_attempt(tmp_path: Path, mode: str = "AUTO"):
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [TimeoutError("lost")], mode=mode
    )
    first = create(service, owner, group, revision.revision_id)
    unknown = service.resume(first.generation_id, owner)
    assert unknown.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    return repo, service, provider, owner, first


@pytest.mark.asyncio
async def test_supervisor_requeues_terminated_recovery_once_without_provider_call(
    tmp_path: Path,
) -> None:
    repo, service, provider, owner, first = unknown_attempt(tmp_path)
    repo.recovery_pending.clear()
    probe = Probe(False)
    supervisor = StoryboardRecoverySupervisor(repo, service, probe, NOW)
    assert await supervisor.sweep_once() == 1
    assert len(repo.recovery_events) == 2
    assert repo.recovery_events[-1][1] != repo.recovery_events[0][1]
    assert provider.submissions == 1
    assert await supervisor.sweep_once() == 0
    assert len(repo.recovery_events) == 2


@pytest.mark.asyncio
async def test_supervisor_skips_active_or_inconclusive_workflow(tmp_path: Path) -> None:
    repo, service, provider, _, _ = unknown_attempt(tmp_path)
    repo.recovery_pending.clear()
    inconclusive = []
    supervisor = StoryboardRecoverySupervisor(repo, service, Probe(True), NOW, inconclusive.append)
    assert await supervisor.sweep_once() == 0
    assert inconclusive == []
    supervisor = StoryboardRecoverySupervisor(repo, service, Probe(None), NOW, inconclusive.append)
    assert await supervisor.sweep_once() == 0
    assert len(inconclusive) == 1
    assert inconclusive[0].generation.generation_id == repo.recovery_events[0][0]
    assert len(repo.recovery_events) == 1
    assert provider.submissions == 1


@pytest.mark.asyncio
async def test_supervisor_does_not_backfill_historical_unknown(tmp_path: Path) -> None:
    repo, service, provider, _, _ = unknown_attempt(tmp_path)
    repo.recovery_pending.clear()
    supervisor = StoryboardRecoverySupervisor(repo, service, Probe(False), NOW + timedelta(days=1))
    assert await supervisor.sweep_once() == 0
    assert len(repo.recovery_events) == 1
    assert provider.submissions == 1


@pytest.mark.asyncio
async def test_supervisor_requeues_old_generation_with_new_recovery_outbox(tmp_path: Path) -> None:
    repo, service, _, owner, first = unknown_attempt(tmp_path)
    repo.recovery_pending.clear()
    repo.recovery_event_times[first.generation_id] = NOW + timedelta(days=2)
    supervisor = StoryboardRecoverySupervisor(
        repo, service, Probe(False), NOW + timedelta(days=1)
    )
    assert await supervisor.sweep_once() == 1
    assert len(repo.recovery_events) == 2


@pytest.mark.asyncio
async def test_supervisor_ignores_manual_unknown(tmp_path: Path) -> None:
    repo, service, provider, _, _ = unknown_attempt(tmp_path, mode="MANUAL")
    repo.recovery_pending.clear()
    supervisor = StoryboardRecoverySupervisor(repo, service, Probe(False), NOW)
    assert await supervisor.sweep_once() == 0
    assert provider.submissions == 1


def test_sql_recovery_candidates_and_authorize_dedupe(tmp_path: Path) -> None:
    repo, _, sessions = sql_repositories(tmp_path / "recovery-candidates.sqlite")
    value = minimal_generation(status=GenerationStatus.REMOTE_STATE_UNKNOWN, created_at=NOW)
    insert_project(sessions, value, "AUTO")
    with sessions.begin() as session:
        AuthoritativeNarrationGroupRecord.__table__.create(session.get_bind())
        session.add(
            AuthoritativeNarrationGroupRecord(
                id=value.group_id,
                project_id=value.project_id,
                owner_id=value.owner_id,
                snapshot_sha256="7" * 64,
                snapshot_revision=1,
                sequence=1,
                start_frame=0,
                end_frame=48_000,
                sample_rate_hz=48_000,
                start_ms=0,
                end_ms=1_000,
                video_blocked=False,
                block_reason=None,
            )
        )
    old_workflow_id = "storyboard-grid-recovery-old"
    with sessions.begin() as session:
        session.add(
            StoryboardOutboxRow(
                id=uuid4(),
                owner_id=value.owner_id,
                project_id=value.project_id,
                aggregate_type="STORYBOARD_RECOVERY",
                aggregate_id=value.generation_id,
                event_type="STORYBOARD_RECOVERY_REQUESTED",
                request_sha256=value.canonical_request_sha256,
                workflow_id=old_workflow_id,
                state="ACKED",
                acknowledged_at=NOW,
                next_attempt_at=NOW - timedelta(seconds=1),
                created_at=NOW - timedelta(seconds=1),
            )
        )
    candidates = repo.list_unknown_recovery_candidates(NOW)
    assert len(candidates) == 1
    assert candidates[0].generation.generation_id == value.generation_id
    assert candidates[0].latest_workflow_id == old_workflow_id
    assert candidates[0].group_sequence == 1
    assert not candidates[0].has_pending_recovery
    repo.authorize_generation_recovery(value.owner_id, value.generation_id, uuid4(), NOW)
    repo.authorize_generation_recovery(value.owner_id, value.generation_id, uuid4(), NOW)
    with sessions() as session:
        events = session.query(StoryboardOutboxRow).all()
        assert len(events) == 2
        assert len({event.workflow_id for event in events}) == 2
    candidates = repo.list_unknown_recovery_candidates(NOW)
    assert len(candidates) == 1 and candidates[0].has_pending_recovery
    assert candidates[0].latest_workflow_id != old_workflow_id
    assert repo.list_unknown_recovery_candidates(NOW + timedelta(days=1)) == ()
