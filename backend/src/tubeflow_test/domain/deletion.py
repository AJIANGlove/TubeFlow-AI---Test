from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tubeflow_test.domain.errors import DomainError


class DeletionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELETING = "DELETING"
    DELETE_FAILED = "DELETE_FAILED"
    DELETED = "DELETED"


@dataclass(frozen=True, slots=True)
class DeletionObject:
    object_key: str
    scope: str
    project_id: UUID
    shared_candidate: bool = False

    def __post_init__(self) -> None:
        if not self.object_key or "*" in self.object_key or "?" in self.object_key:
            raise DomainError("unsafe_object_key", "an exact object key is required")
        if self.scope != "PROJECT":
            raise DomainError("unsafe_object_scope", "only project-scoped objects may be purged")


@dataclass(frozen=True, slots=True)
class ObjectDeletionResult:
    object_key: str
    attempt: int
    attempted_at: datetime
    succeeded: bool
    error_code: str | None = None
    skipped_shared: bool = False


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    project_id: UUID
    owner_id: UUID
    status: DeletionStatus
    objects: tuple[DeletionObject, ...]
    results: tuple[ObjectDeletionResult, ...] = ()
    dispatch_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class Tombstone:
    project_id: UUID
    deleted_at: datetime
    object_count: int
    status: DeletionStatus = DeletionStatus.DELETED


def aggregate_deletion_status(
    results: tuple[ObjectDeletionResult, ...],
) -> DeletionStatus:
    latest = {item.object_key: item for item in results}
    return (
        DeletionStatus.DELETE_FAILED
        if any(not item.succeeded for item in latest.values())
        else DeletionStatus.DELETING
    )
