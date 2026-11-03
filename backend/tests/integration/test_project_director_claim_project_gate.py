from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    ProjectDirectorRunRow,
    ProjectDirectorUnitRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
)
from tubeflow_test.api.narration_runtime import SqlProjectSnapshotAdapter
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    ProjectDirectorRunStatus,
    ProjectDirectorUnitStatus,
)


@pytest.mark.integration
def test_claim_unit_fails_closed_after_project_leaves_active_state(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    SqlProjectSnapshotAdapter(
        sessions,
        director_model_name="gpt-5.6-sol",
        director_parameters={"temperature": 0.2},
        director_provider_configured=True,
    ).confirm_and_enqueue(owner_id, project_id)

    with sessions.begin() as session:
        run = session.scalar(
            select(ProjectDirectorRunRow).where(
                ProjectDirectorRunRow.owner_id == owner_id,
                ProjectDirectorRunRow.project_id == project_id,
            )
        )
        assert run is not None
        unit = session.scalar(
            select(ProjectDirectorUnitRow)
            .where(ProjectDirectorUnitRow.run_id == run.id)
            .order_by(ProjectDirectorUnitRow.ordinal)
            .limit(1)
        )
        project = session.get(ProjectRow, project_id)
        assert unit is not None and project is not None
        run_id, unit_id = run.id, unit.id
        before_run = (run.status, run.state_revision)
        before_unit = (
            unit.status,
            unit.state_revision,
            unit.lease_token,
            unit.lease_until,
        )
        project.production_state = "PURGING"

    now = datetime(2026, 8, 31, tzinfo=UTC)
    repository = SqlAlchemyProjectDirectorRepository(sessions.begin)
    claimable = repository.next_claimable_unit(owner_id, run_id, now)
    assert claimable is not None and claimable.unit_id == unit_id
    with pytest.raises(DomainError) as raised:
        repository.claim_unit(
            owner_id,
            run_id,
            unit_id,
            uuid4(),
            now,
            now + timedelta(minutes=10),
        )
    assert raised.value.code == "project_purging"

    with sessions() as session:
        run = session.get(ProjectDirectorRunRow, run_id)
        unit = session.get(ProjectDirectorUnitRow, unit_id)
        assert run is not None and unit is not None
        assert (run.status, run.state_revision) == before_run
        assert (
            unit.status,
            unit.state_revision,
            unit.lease_token,
            unit.lease_until,
        ) == before_unit


@pytest.mark.integration
def test_repository_manual_resolution_is_idempotent_after_concurrent_terminalization(
    postgres_database: object,
    production_parent_chain: object,
) -> None:
    sessions = postgres_database.sessions
    owner_id = production_parent_chain.owner_id
    project_id = production_parent_chain.project_id
    SqlProjectSnapshotAdapter(
        sessions,
        director_model_name="gpt-5.6-sol",
        director_parameters={"temperature": 0.2},
        director_provider_configured=True,
    ).confirm_and_enqueue(owner_id, project_id)

    with sessions.begin() as session:
        run = session.scalar(
            select(ProjectDirectorRunRow).where(
                ProjectDirectorRunRow.owner_id == owner_id,
                ProjectDirectorRunRow.project_id == project_id,
            )
        )
        assert run is not None
        unit = session.scalar(
            select(ProjectDirectorUnitRow)
            .where(ProjectDirectorUnitRow.run_id == run.id)
            .order_by(ProjectDirectorUnitRow.ordinal)
            .limit(1)
        )
        assert unit is not None
        run.status = ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN.value
        run.failure_code = "transport_unknown"
        unit.status = ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN.value
        unit.failure_code = "transport_unknown"
        run_id = run.id

    repository = SqlAlchemyProjectDirectorRepository(sessions.begin)
    failure_code = f"manual_confirmed_not_accepted:{'a' * 64}"
    first = repository.resolve_unknown(
        owner_id,
        run_id,
        failure_code=failure_code,
        retry_blocked=False,
    )
    second = repository.resolve_unknown(
        owner_id,
        run_id,
        failure_code=failure_code,
        retry_blocked=False,
    )

    assert second == first
    assert second.status is ProjectDirectorRunStatus.FAILED
    with pytest.raises(DomainError) as changed:
        repository.resolve_unknown(
            owner_id,
            run_id,
            failure_code=f"manual_confirmed_not_accepted:{'b' * 64}",
            retry_blocked=False,
        )
    assert changed.value.code == "reconciliation_not_allowed"
