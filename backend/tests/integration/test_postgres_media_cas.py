from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyCompositionOutbox,
    SqlAlchemyPurgeOutbox,
)
from tubeflow_test.adapters.persistence.models import (
    CompositionInputRevisionRow,
    CompositionOutboxRow,
    ProjectPurgeJobRow,
    PurgeOutboxRow,
)


@pytest.mark.integration
def test_stale_outbox_completion_cannot_finish_new_generation(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    row_id = uuid4()
    project_id = production_parent_chain.project_id
    owner_id = production_parent_chain.owner_id
    with sessions() as session, session.begin():
        session.add(
            ProjectPurgeJobRow(
                project_id=project_id,
                owner_id=owner_id,
                state="PENDING",
                object_results=[],
                workflow_generation=1,
            )
        )
        session.flush()
        session.add(
            PurgeOutboxRow(
                id=row_id,
                project_id=project_id,
                owner_id=owner_id,
                dispatched_at=None,
                lease_until=None,
                attempt=0,
                last_error_code=None,
                workflow_generation=1,
                lease_token=None,
            )
        )
    outbox = SqlAlchemyPurgeOutbox(sessions)
    old_claim = outbox.claim()
    assert old_claim is not None
    with sessions() as session, session.begin():
        session.execute(
            update(PurgeOutboxRow)
            .where(PurgeOutboxRow.id == row_id)
            .values(workflow_generation=2, lease_token=None, lease_until=None)
        )
    outbox.succeeded(old_claim)
    with sessions() as session:
        row = session.scalar(select(PurgeOutboxRow).where(PurgeOutboxRow.id == row_id))
        assert row is not None and row.dispatched_at is None
        session.delete(row)
        session.commit()


@pytest.mark.integration
def test_composition_outbox_restart_backoff_and_stale_lease_cas(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    now = datetime.now(UTC)
    row_id, revision_id = uuid4(), uuid4()
    project_id = production_parent_chain.project_id
    owner_id = production_parent_chain.owner_id
    with sessions.begin() as session:
        session.add(
            CompositionInputRevisionRow(
                id=revision_id,
                project_id=project_id,
                owner_id=owner_id,
                inputs_sha256="a" * 64,
                payload_json={},
                created_at=now,
            )
        )
        session.flush()
        session.add(
            CompositionOutboxRow(
                id=row_id,
                project_id=project_id,
                owner_id=owner_id,
                revision_id=revision_id,
                inputs_sha256="a" * 64,
                workflow_id=f"composition:{owner_id}:{project_id}:{'a' * 64}",
                state="PENDING",
                attempt=0,
                workflow_generation=1,
                lease_token=None,
                lease_until=None,
                next_attempt_at=now,
                dispatched_at=None,
                last_error_code=None,
            )
        )

    first_process = SqlAlchemyCompositionOutbox(sessions)
    first_claim = first_process.claim(now=now)
    assert first_claim is not None
    first_process.failed(first_claim, "network_unknown", now=now)

    restarted_process = SqlAlchemyCompositionOutbox(sessions)
    assert restarted_process.claim(now=now) is None
    second_claim = restarted_process.claim(now=now + timedelta(seconds=1))
    assert second_claim is not None and second_claim.attempt == 2
    assert second_claim.workflow_id == first_claim.workflow_id
    assert second_claim.revision_id == first_claim.revision_id
    with sessions() as session, session.begin():
        session.execute(
            update(CompositionOutboxRow)
            .where(CompositionOutboxRow.id == row_id)
            .values(workflow_generation=2, lease_token=None, lease_until=None)
        )
    restarted_process.succeeded(second_claim)
    with sessions() as session:
        row = session.get(CompositionOutboxRow, row_id)
        assert row is not None and row.state == "PENDING" and row.dispatched_at is None
        session.delete(row)
        session.commit()
