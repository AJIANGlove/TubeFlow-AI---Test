from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from tubeflow_test.adapters.persistence.models import DeletionTombstoneRow
from tubeflow_test.api.deletion_receipt import DeletionReceiptCodec
from tubeflow_test.api.media_runtime import SqlDeletionStatus
from tubeflow_test.domain.deletion import DeletionRecord, DeletionStatus, Tombstone
from tubeflow_test.domain.errors import DomainError

_SECRET = "test-deletion-receipt-secret-is-at-least-32-bytes"


@dataclass
class Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


class Repository:
    def __init__(
        self,
        record: DeletionRecord | None,
        tombstone: Tombstone | None = None,
    ) -> None:
        self.record = record
        self.deleted = tombstone
        self.reenqueued: tuple[UUID, UUID] | None = None

    def get_for_owner(self, project_id: UUID, owner_id: UUID) -> DeletionRecord | None:
        if self.record is None:
            return None
        if (self.record.project_id, self.record.owner_id) != (project_id, owner_id):
            return None
        return self.record

    def tombstone(self, project_id: UUID) -> Tombstone | None:
        if self.deleted is None or self.deleted.project_id != project_id:
            return None
        return self.deleted

    def reenqueue(self, project_id: UUID, owner_id: UUID) -> None:
        if self.get_for_owner(project_id, owner_id) is None:
            raise DomainError("project_not_found", "project was not found")
        self.reenqueued = (project_id, owner_id)


def test_receipt_is_opaque_authenticated_and_valid_for_exactly_ten_minutes() -> None:
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    project_id, owner_id = uuid4(), uuid4()
    codec = DeletionReceiptCodec(_SECRET, now=Clock(now))

    issued = codec.issue(project_id, owner_id)
    claims = codec.verify(issued.token, project_id=project_id, owner_id=owner_id)

    assert str(project_id) not in issued.token
    assert str(owner_id) not in issued.token
    assert claims.issued_at == now
    assert claims.expires_at == now + timedelta(minutes=10)
    assert issued.expires_at == claims.expires_at


@pytest.mark.parametrize("invalid", ("tampered", "expired", "cross_project", "cross_owner"))
def test_receipt_rejects_tamper_expiry_and_cross_subject_replay(invalid: str) -> None:
    clock = Clock(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))
    project_id, owner_id = uuid4(), uuid4()
    codec = DeletionReceiptCodec(_SECRET, now=clock)
    token = codec.issue(project_id, owner_id).token
    requested_project, requested_owner = project_id, owner_id
    if invalid == "tampered":
        middle = len(token) // 2
        token = token[:middle] + ("A" if token[middle] != "A" else "B") + token[middle + 1 :]
    elif invalid == "expired":
        clock.value += timedelta(minutes=10)
    elif invalid == "cross_project":
        requested_project = uuid4()
    else:
        requested_owner = uuid4()

    with pytest.raises(DomainError) as raised:
        codec.verify(token, project_id=requested_project, owner_id=requested_owner)

    assert (raised.value.code, raised.value.message) == ("not_found", "Resource not found")


def test_active_poll_refreshes_receipt_and_final_poll_uses_ownerless_tombstone() -> None:
    clock = Clock(datetime(2026, 8, 29, 12, 0, tzinfo=UTC))
    project_id, owner_id = uuid4(), uuid4()
    record = DeletionRecord(project_id, owner_id, DeletionStatus.DELETING, ())
    repository = Repository(record)
    service = SqlDeletionStatus(  # type: ignore[arg-type]
        repository, DeletionReceiptCodec(_SECRET, now=clock)
    )
    initial = service.receipt(project_id, owner_id)

    clock.value += timedelta(minutes=1)
    running = service.status(
        project_id, owner_id, str(initial["deletion_receipt"])
    )
    assert running["status"] == DeletionStatus.DELETING
    assert running["public_status"] == "RUNNING"
    assert running["deletion_receipt"] != initial["deletion_receipt"]

    repository.record = None
    repository.deleted = Tombstone(project_id, clock.value, 3)
    completed = service.status(
        project_id, owner_id, str(running["deletion_receipt"])
    )

    assert completed == {
        "project_id": project_id,
        "status": DeletionStatus.DELETED,
        "object_count": 3,
        "succeeded_count": 3,
        "failed_count": 0,
        "errors": [],
        "deletion_receipt": running["deletion_receipt"],
        "receipt_expires_at": running["receipt_expires_at"],
        "public_status": "SUCCEEDED",
        "message": "Project deletion completed.",
        "allowed_actions": [],
    }
    assert "owner_id" not in DeletionTombstoneRow.__table__.columns
    assert repository.get_for_owner(project_id, owner_id) is None


def test_final_status_without_receipt_fails_closed() -> None:
    project_id, owner_id = uuid4(), uuid4()
    repository = Repository(None, Tombstone(project_id, datetime.now(UTC), 0))
    service = SqlDeletionStatus(  # type: ignore[arg-type]
        repository, DeletionReceiptCodec(_SECRET)
    )

    with pytest.raises(DomainError) as raised:
        service.status(project_id, owner_id, None)

    assert (raised.value.code, raised.value.message) == ("not_found", "Resource not found")


def test_retry_reuses_existing_deletion_flow_and_returns_a_valid_receipt() -> None:
    project_id, owner_id = uuid4(), uuid4()
    repository = Repository(
        DeletionRecord(project_id, owner_id, DeletionStatus.DELETE_FAILED, ())
    )
    codec = DeletionReceiptCodec(_SECRET)
    service = SqlDeletionStatus(repository, codec)  # type: ignore[arg-type]

    result = service.retry(project_id, owner_id, None)

    assert repository.reenqueued == (project_id, owner_id)
    codec.verify(str(result["deletion_receipt"]), project_id=project_id, owner_id=owner_id)
