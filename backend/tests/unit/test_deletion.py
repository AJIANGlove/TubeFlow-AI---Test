from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from tubeflow_test.application.deletion import ProjectDeletionService, PurgeClaim
from tubeflow_test.domain.deletion import (
    DeletionObject,
    DeletionRecord,
    DeletionStatus,
    ObjectDeletionResult,
    Tombstone,
    aggregate_deletion_status,
)
from tubeflow_test.domain.errors import DomainError


class Repo:
    def __init__(
        self, record: DeletionRecord, *, active: bool = True, shared: set[str] | None = None
    ):
        self.record: DeletionRecord | None = record
        self.active = active
        self.shared = shared or set()
        self.tombstone: Tombstone | None = None
        self.dispatch_error = None
        self.claim: PurgeClaim | None = None

    def project_title(self, project_id: UUID, owner_id: UUID) -> str | None:
        if self.record and (self.record.project_id, self.record.owner_id) == (
            project_id,
            owner_id,
        ):
            return "Project title"
        return None

    def get_for_owner(
        self, project_id: UUID, owner_id: UUID, claim: PurgeClaim | None = None
    ) -> DeletionRecord | None:
        if claim is not None and claim != self.claim:
            return None
        return (
            self.record
            if self.record
            and (self.record.project_id, self.record.owner_id) == (project_id, owner_id)
            else None
        )

    def begin_with_lock_and_outbox(self, project_id: UUID, owner_id: UUID) -> bool:
        if not self.active or self.record is None or self.record.status != DeletionStatus.ACTIVE:
            return False
        self.record = replace(self.record, status=DeletionStatus.DELETING)
        return True

    def record_dispatch_failure(self, project_id: UUID, code: str) -> None:
        self.dispatch_error = code

    def claim_clean(
        self, project_id: UUID, owner_id: UUID, generation: int | None
    ) -> PurgeClaim | None:
        self.claim = PurgeClaim(project_id, owner_id, generation or 1, uuid4())
        return self.claim

    def renew_clean_claim(self, claim: PurgeClaim) -> bool:
        return claim == self.claim

    def save_results(self, claim: PurgeClaim, results: tuple) -> None:
        assert claim == self.claim
        assert self.record
        self.record = replace(
            self.record,
            results=results,
            status=DeletionStatus.DELETE_FAILED
            if not results[-1].succeeded
            else self.record.status,
        )

    def replace_with_tombstone(self, claim: PurgeClaim, tombstone: Tombstone) -> tuple[str, ...]:
        assert claim == self.claim
        self.tombstone = tombstone
        self.record = None
        return ()

    def is_shared_referenced(self, object_key: str, claim: PurgeClaim) -> bool:
        assert claim == self.claim
        return object_key in self.shared


class Deleter:
    def __init__(self, fail_once: set[str] | None = None, missing: set[str] | None = None):
        self.calls: list[str] = []
        self.fail_once = fail_once or set()
        self.missing = missing or set()

    def delete_exact(self, key: str) -> None:
        self.calls.append(key)
        if key in self.missing:
            raise FileNotFoundError
        if key in self.fail_once:
            self.fail_once.remove(key)
            raise OSError("secret path")


class Dispatch:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def dispatch(self, project_id: UUID, owner_id: UUID) -> None:
        self.calls += 1
        if self.fail:
            raise OSError


def record(status: DeletionStatus = DeletionStatus.ACTIVE) -> DeletionRecord:
    project = uuid4()
    return DeletionRecord(
        project,
        uuid4(),
        status,
        (
            DeletionObject("project/private", "PROJECT", project),
            DeletionObject("shared/role", "PROJECT", project),
        ),
    )


def test_request_owner_confirmation_and_active_task_race() -> None:
    item = record()
    repo = Repo(item, active=False)
    service = ProjectDeletionService(repo, Deleter())
    with pytest.raises(DomainError, match="confirmation"):
        service.request(item.project_id, item.owner_id, confirmation="bad")
    with pytest.raises(DomainError) as hidden:
        service.request(item.project_id, uuid4(), confirmation="Project title")
    assert hidden.value.code == "project_not_found"
    with pytest.raises(DomainError, match="running tasks"):
        service.request(item.project_id, item.owner_id, confirmation="Project title")
    repo.active = True
    service = ProjectDeletionService(repo, Deleter())
    service.request(item.project_id, item.owner_id, confirmation="Project title")
    assert repo.record and repo.record.status == DeletionStatus.DELETING


def test_cleanup_records_attempts_retries_idempotently_and_preserves_shared() -> None:
    item = record(DeletionStatus.DELETING)
    repo = Repo(item, shared={"shared/role"})
    deleter = Deleter({"project/private"})
    service = ProjectDeletionService(repo, deleter)
    with pytest.raises(DomainError):
        service.clean(item.project_id, item.owner_id)
    assert repo.record
    evidence = {result.object_key: result for result in repo.record.results}
    assert evidence["project/private"].error_code == "object_delete_failed"
    assert evidence["shared/role"].error_code == "SKIPPED_SHARED"
    assert evidence["shared/role"].skipped_shared
    service.clean(item.project_id, item.owner_id)
    assert deleter.calls == ["project/private", "project/private"]
    assert repo.tombstone and repo.tombstone.object_count == 1
    assert repo.record is None


def test_already_missing_object_is_idempotent_success() -> None:
    item = record(DeletionStatus.DELETING)
    repo = Repo(item)
    service = ProjectDeletionService(repo, Deleter(missing={"project/private", "shared/role"}))
    service.clean(item.project_id, item.owner_id)
    assert repo.tombstone is not None


def test_object_scope_must_be_exact_project_scope() -> None:
    with pytest.raises(DomainError):
        DeletionObject("projects/*", "PROJECT", uuid4())
    with pytest.raises(DomainError):
        DeletionObject("voice/public", "SHARED", uuid4())


def test_deletion_status_aggregates_latest_result_per_key() -> None:
    now = datetime.now(UTC)
    results = (
        ObjectDeletionResult("early", 1, now, False, "object_delete_failed"),
        ObjectDeletionResult("later", 1, now, True),
    )
    assert aggregate_deletion_status(results) == DeletionStatus.DELETE_FAILED
    recovered = results + (ObjectDeletionResult("early", 2, now, True),)
    assert aggregate_deletion_status(recovered) == DeletionStatus.DELETING
