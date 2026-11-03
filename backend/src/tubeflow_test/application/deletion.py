from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from tubeflow_test.domain.deletion import DeletionRecord, ObjectDeletionResult, Tombstone
from tubeflow_test.domain.errors import DomainError


@dataclass(frozen=True, slots=True)
class PurgeClaim:
    project_id: UUID
    owner_id: UUID
    workflow_generation: int
    execution_token: UUID


class DeletionRepository(Protocol):
    def project_title(self, project_id: UUID, owner_id: UUID) -> str | None: ...
    def get_for_owner(
        self, project_id: UUID, owner_id: UUID, claim: PurgeClaim | None = None
    ) -> DeletionRecord | None: ...
    def begin_with_lock_and_outbox(self, project_id: UUID, owner_id: UUID) -> bool: ...
    def record_dispatch_failure(self, project_id: UUID, code: str) -> None: ...
    def save_results(
        self, claim: PurgeClaim, results: tuple[ObjectDeletionResult, ...]
    ) -> None: ...
    def replace_with_tombstone(
        self, claim: PurgeClaim, tombstone: Tombstone
    ) -> tuple[str, ...]: ...
    def is_shared_referenced(self, object_key: str, claim: PurgeClaim) -> bool: ...
    def claim_clean(
        self, project_id: UUID, owner_id: UUID, generation: int | None
    ) -> PurgeClaim | None: ...
    def renew_clean_claim(self, claim: PurgeClaim) -> bool: ...


class ExactObjectDeleter(Protocol):
    def delete_exact(self, object_key: str) -> None: ...


class ProjectDeletionService:
    def __init__(
        self,
        repository: DeletionRepository,
        objects: ExactObjectDeleter,
    ):
        self._repository = repository
        self._objects = objects

    def request(self, project_id: UUID, owner_id: UUID, *, confirmation: str) -> None:
        title = self._repository.project_title(project_id, owner_id)
        if title is None:
            raise DomainError("project_not_found", "project was not found")
        if confirmation != title:
            raise DomainError(
                "deletion_confirmation_required", "exact project title confirmation is required"
            )
        record = self._repository.get_for_owner(project_id, owner_id)
        if record is None:
            raise DomainError("project_not_found", "project was not found")
        if not self._repository.begin_with_lock_and_outbox(project_id, owner_id):
            raise DomainError(
                "project_has_running_tasks", "project is not active or has running tasks"
            )

    def clean(
        self, project_id: UUID, owner_id: UUID, *, workflow_generation: int | None = None
    ) -> None:
        claim = self._repository.claim_clean(project_id, owner_id, workflow_generation)
        if claim is None:
            raise DomainError(
                "stale_purge_workflow", "purge workflow generation is no longer active"
            )
        record = self._repository.get_for_owner(project_id, owner_id, claim)
        if record is None or record.status not in {"DELETING", "DELETE_FAILED"}:
            raise DomainError("project_not_deleting", "project is not ready for cleanup")
        latest = {item.object_key: item for item in record.results}
        results = list(record.results)
        for item in record.objects:
            if item.project_id != project_id:
                raise DomainError(
                    "unsafe_object_scope", "object project scope does not match purge"
                )
            prior = latest.get(item.object_key)
            if prior is not None and prior.succeeded and not prior.skipped_shared:
                continue
            self._renew_or_stale(claim)
            if self._repository.is_shared_referenced(item.object_key, claim):
                result = ObjectDeletionResult(
                    item.object_key,
                    1 if prior is None else prior.attempt + 1,
                    datetime.now(UTC),
                    True,
                    "SKIPPED_SHARED",
                    True,
                )
                results.append(result)
                self._repository.save_results(claim, tuple(results))
                continue
            attempt = 1 if prior is None else prior.attempt + 1
            try:
                self._renew_or_stale(claim)
                self._objects.delete_exact(item.object_key)
                result = ObjectDeletionResult(item.object_key, attempt, datetime.now(UTC), True)
            except FileNotFoundError:
                result = ObjectDeletionResult(item.object_key, attempt, datetime.now(UTC), True)
            except Exception:
                result = ObjectDeletionResult(
                    item.object_key, attempt, datetime.now(UTC), False, "object_delete_failed"
                )
            self._renew_or_stale(claim)
            results.append(result)
            self._repository.save_results(claim, tuple(results))
        latest = {item.object_key: item for item in results}
        if any(not item.succeeded for item in latest.values()):
            raise DomainError("project_delete_failed", "some project objects could not be deleted")
        for _ in range(len(record.objects) + 1):
            self._renew_or_stale(claim)
            late_deletable = self._repository.replace_with_tombstone(
                claim,
                Tombstone(
                    project_id,
                    datetime.now(UTC),
                    sum(
                        1
                        for item in latest.values()
                        if item.succeeded and not item.skipped_shared
                    ),
                ),
            )
            if not late_deletable:
                return
            for object_key in late_deletable:
                prior = latest.get(object_key)
                attempt = 1 if prior is None else prior.attempt + 1
                self._renew_or_stale(claim)
                try:
                    self._objects.delete_exact(object_key)
                    result = ObjectDeletionResult(
                        object_key, attempt, datetime.now(UTC), True
                    )
                except FileNotFoundError:
                    result = ObjectDeletionResult(
                        object_key, attempt, datetime.now(UTC), True
                    )
                except Exception:
                    result = ObjectDeletionResult(
                        object_key,
                        attempt,
                        datetime.now(UTC),
                        False,
                        "object_delete_failed",
                    )
                self._renew_or_stale(claim)
                results.append(result)
                latest[object_key] = result
                self._repository.save_results(claim, tuple(results))
                if not result.succeeded:
                    raise DomainError(
                        "project_delete_failed", "some project objects could not be deleted"
                    )
        raise DomainError(
            "project_delete_conflict", "shared asset deletion election did not converge"
        )

    def _renew_or_stale(self, claim: PurgeClaim) -> None:
        if not self._repository.renew_clean_claim(claim):
            raise DomainError("stale_purge_workflow", "purge cleanup claim is no longer active")
