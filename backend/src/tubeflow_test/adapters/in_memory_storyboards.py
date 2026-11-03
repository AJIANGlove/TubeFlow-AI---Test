from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from uuid import UUID

from tubeflow_test.adapters.persistence.storyboards.repository import _may_transition
from tubeflow_test.application.storyboard_lifecycle import StoryboardProjectAssets
from tubeflow_test.application.storyboards import StoryboardContext, StoryboardRecoveryCandidate
from tubeflow_test.domain.director_generation import (
    DirectorAttemptStatus,
    DirectorGenerationAttempt,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    STORYBOARD_AUTO_MAX_SUBMISSIONS,
    DirectorPlanRevision,
    DirectorRevisionKind,
    GenerationStatus,
    StoryboardGeneration,
    VideoStoryboardBinding,
)


class InMemoryStoryboardRepository:
    """Test-only transactional model of the PostgreSQL repository contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.contexts: dict[tuple[UUID, UUID], StoryboardContext] = {}
        self.revisions: dict[UUID, DirectorPlanRevision] = {}
        self.applied_by_group: dict[tuple[UUID, UUID], UUID] = {}
        self.draft_by_group: dict[tuple[UUID, UUID], UUID] = {}
        self.generations: dict[UUID, StoryboardGeneration] = {}
        self.generation_by_key: dict[tuple[UUID, str], UUID] = {}
        self.current_by_group: dict[tuple[UUID, UUID], UUID] = {}
        self.video_bindings: dict[tuple[UUID, UUID], VideoStoryboardBinding] = {}
        self.director_attempts: dict[UUID, DirectorGenerationAttempt] = {}
        self.director_attempt_by_key: dict[tuple[UUID, str], UUID] = {}
        self.project_states: dict[tuple[UUID, UUID], str] = {}
        self.production_modes: dict[tuple[UUID, UUID], str] = {}
        # AUTO 测试钩子：预算轮次（§15.9 恢复时 +1）。
        self.budget_rounds: dict[tuple[UUID, UUID], int] = {}
        # AUTO 测试钩子：自动对账事件（generation_id, workflow_id）与后继排期。
        self.recovery_events: list[tuple[UUID, str]] = []
        self.recovery_event_times: dict[UUID, datetime] = {}
        self.recovery_pending: set[UUID] = set()
        self.retry_schedules: dict[UUID, datetime] = {}

    def find_by_key(self, owner_id: UUID, key: str) -> DirectorGenerationAttempt | None:
        attempt_id = self.director_attempt_by_key.get((owner_id, key))
        return self.director_attempts.get(attempt_id) if attempt_id else None

    def find_unresolved(
        self, owner_id: UUID, group_id: UUID, canonical_hash: str
    ) -> DirectorGenerationAttempt | None:
        return next(
            (
                item
                for item in self.director_attempts.values()
                if item.owner_id == owner_id
                and item.group_id == group_id
                and item.canonical_request_sha256 == canonical_hash
                and (
                    item.status is DirectorAttemptStatus.REMOTE_STATE_UNKNOWN
                    or (
                        item.status is DirectorAttemptStatus.FAILED
                        and (item.failure_code or "").startswith("manual_confirmed_failed:")
                    )
                )
            ),
            None,
        )

    def create(self, attempt: DirectorGenerationAttempt) -> DirectorGenerationAttempt:
        with self._lock:
            existing = self.find_by_key(attempt.owner_id, attempt.idempotency_key)
            if existing:
                return existing
            self.director_attempts[attempt.attempt_id] = attempt
            self.director_attempt_by_key[(attempt.owner_id, attempt.idempotency_key)] = (
                attempt.attempt_id
            )
            return attempt

    def get(self, owner_id: UUID, attempt_id: UUID) -> DirectorGenerationAttempt | None:
        value = self.director_attempts.get(attempt_id)
        return value if value and value.owner_id == owner_id else None

    def latest_for_group(self, owner_id: UUID, group_id: UUID) -> DirectorGenerationAttempt | None:
        return max(
            (
                item
                for item in self.director_attempts.values()
                if item.owner_id == owner_id and item.group_id == group_id
            ),
            key=lambda item: (item.created_at, item.attempt_id.hex),
            default=None,
        )

    def authorize_director_recovery(
        self, owner_id: UUID, attempt_id: UUID, event_id: UUID, now: datetime
    ) -> DirectorGenerationAttempt:
        del event_id, now
        value = self.get(owner_id, attempt_id)
        if value is None:
            raise DomainError("not_found", "Director attempt was not found")
        if value.status is not DirectorAttemptStatus.REMOTE_STATE_UNKNOWN:
            raise DomainError("revision_conflict", "Director attempt is not unresolved")
        return value

    def claim(
        self, owner_id: UUID, attempt_id: UUID, token: UUID, now: datetime, lease_until: datetime
    ) -> DirectorGenerationAttempt | None:
        with self._lock:
            value = self.get(owner_id, attempt_id)
            if (
                value is not None
                and self.project_states.get((owner_id, value.input_snapshot.project_id)) != "ACTIVE"
            ):
                return None
            if value is None or not (
                value.status is DirectorAttemptStatus.INTENT_PERSISTED
                or (
                    value.status is DirectorAttemptStatus.LEASED
                    and value.lease_until is not None
                    and value.lease_until < now
                )
            ):
                return None
            claimed = replace(
                value,
                status=DirectorAttemptStatus.LEASED,
                state_revision=value.state_revision + 1,
                lease_token=token,
                lease_until=lease_until,
            )
            self.director_attempts[attempt_id] = claimed
            return claimed

    def mark_call_started(
        self, attempt: DirectorGenerationAttempt
    ) -> DirectorGenerationAttempt | None:
        with self._lock:
            value = self.get(attempt.owner_id, attempt.attempt_id)
            if (
                value is None
                or value.status is not DirectorAttemptStatus.LEASED
                or value.state_revision != attempt.state_revision
                or value.lease_token != attempt.lease_token
            ):
                return None
            started = replace(
                value,
                status=DirectorAttemptStatus.SUBMITTING,
                state_revision=value.state_revision + 1,
                lease_token=None,
                lease_until=None,
                history=(*value.history, DirectorAttemptStatus.SUBMITTING.value),
            )
            self.director_attempts[value.attempt_id] = started
            return started

    def save(self, attempt: DirectorGenerationAttempt) -> DirectorGenerationAttempt:
        with self._lock:
            value = self.get(attempt.owner_id, attempt.attempt_id)
            if value is None:
                raise DomainError("not_found", "Director attempt was not found")
            if value.state_revision != attempt.state_revision or value.status not in {
                DirectorAttemptStatus.SUBMITTING,
                DirectorAttemptStatus.REMOTE_STATE_UNKNOWN,
            }:
                return value
            stored = replace(attempt, state_revision=attempt.state_revision + 1)
            self.director_attempts[attempt.attempt_id] = stored
            return stored

    def complete_with_draft(
        self, attempt: DirectorGenerationAttempt, draft: DirectorPlanRevision
    ) -> DirectorGenerationAttempt:
        with self._lock:
            current = self.get(attempt.owner_id, attempt.attempt_id)
            key = (attempt.owner_id, attempt.group_id)
            if (
                current is None
                or current.state_revision != attempt.state_revision
                or current.status is not DirectorAttemptStatus.SUBMITTING
                or self.draft_by_group.get(key) != attempt.input_snapshot.expected_draft_revision_id
            ):
                raise DomainError("revision_conflict", "Director attempt or draft changed")
            number = 1 + max(
                (
                    item.revision_number
                    for item in self.revisions.values()
                    if item.owner_id == draft.owner_id and item.group_id == draft.group_id
                ),
                default=0,
            )
            stored_draft = replace(draft, revision_number=number)
            self.revisions[stored_draft.revision_id] = stored_draft
            self.draft_by_group[key] = stored_draft.revision_id
            completed = replace(
                attempt,
                status=DirectorAttemptStatus.SUCCEEDED,
                state_revision=attempt.state_revision + 1,
                draft_revision_id=stored_draft.revision_id,
                history=(*attempt.history, DirectorAttemptStatus.SUCCEEDED.value),
            )
            self.director_attempts[attempt.attempt_id] = completed
            return completed

    def add_context(self, context: StoryboardContext) -> None:
        self.contexts[(context.owner_id, context.group_id)] = context
        self.project_states[(context.owner_id, context.project_id)] = "ACTIVE"

    def context(self, owner_id: UUID, group_id: UUID) -> StoryboardContext | None:
        value = self.contexts.get((owner_id, group_id))
        return (
            value
            if value is not None
            and self.project_states.get((owner_id, value.project_id)) == "ACTIVE"
            else None
        )

    def activate_storyboards(self, owner_id: UUID, project_id: UUID) -> None:
        if self.project_states.get((owner_id, project_id)) == "PURGING":
            raise DomainError("project_purging", "Project is being purged")
        self.project_states[(owner_id, project_id)] = "ACTIVE"

    def begin_storyboard_purge(self, owner_id: UUID, project_id: UUID) -> None:
        if self.project_states.get((owner_id, project_id)) != "ACTIVE":
            raise DomainError("not_found", "Storyboard resource was not found")
        self.project_states[(owner_id, project_id)] = "PURGING"

    def has_active_storyboard_jobs(self, owner_id: UUID, project_id: UUID) -> bool:
        return any(
            item.owner_id == owner_id
            and item.project_id == project_id
            and item.status.value not in {"SUCCEEDED", "FAILED"}
            for item in self.generations.values()
        )

    def enumerate_storyboard_assets(
        self, owner_id: UUID, project_id: UUID
    ) -> StoryboardProjectAssets:
        assets = {
            item.asset.media.asset_id: item.asset.media
            for item in self.generations.values()
            if item.owner_id == owner_id
            and item.project_id == project_id
            and item.asset is not None
        }
        return StoryboardProjectAssets(project_id, tuple(assets.values()))

    def get_applied_revision(self, owner_id: UUID, group_id: UUID) -> DirectorPlanRevision | None:
        revision_id = self.applied_by_group.get((owner_id, group_id))
        return self.revisions.get(revision_id) if revision_id else None

    def get_draft_revision(self, owner_id: UUID, group_id: UUID) -> DirectorPlanRevision | None:
        revision_id = self.draft_by_group.get((owner_id, group_id))
        return self.revisions.get(revision_id) if revision_id else None

    def create_revision(
        self,
        revision: DirectorPlanRevision,
        expected_draft_revision_id: UUID | None,
        source_candidate_id: UUID | None = None,
    ) -> DirectorPlanRevision:
        del source_candidate_id
        with self._lock:
            key = (revision.owner_id, revision.group_id)
            current = self.draft_by_group.get(key)
            if current != expected_draft_revision_id:
                raise DomainError("revision_conflict", "Draft revision changed")
            number = 1 + max(
                (
                    item.revision_number
                    for item in self.revisions.values()
                    if item.owner_id == revision.owner_id and item.group_id == revision.group_id
                ),
                default=0,
            )
            stored = replace(revision, revision_number=number)
            self.revisions[stored.revision_id] = stored
            self.draft_by_group[key] = stored.revision_id
            return stored

    def save_revision(self, revision: DirectorPlanRevision) -> None:
        with self._lock:
            self.revisions[revision.revision_id] = revision
            key = (revision.owner_id, revision.group_id)
            target = (
                self.draft_by_group
                if revision.kind is DirectorRevisionKind.DRAFT
                else self.applied_by_group
            )
            target[key] = revision.revision_id

    def apply_revision(
        self, owner_id: UUID, group_id: UUID, revision_id: UUID, expected_draft_revision_id: UUID
    ) -> DirectorPlanRevision:
        with self._lock:
            key = (owner_id, group_id)
            if (
                self.draft_by_group.get(key) != expected_draft_revision_id
                or revision_id != expected_draft_revision_id
            ):
                raise DomainError("revision_conflict", "Draft revision changed")
            draft = self.revisions[revision_id]
            applied = replace(draft, kind=DirectorRevisionKind.APPLIED)
            self.revisions[revision_id] = applied
            self.applied_by_group[key] = revision_id
            del self.draft_by_group[key]
            return applied

    def find_generation_by_key(self, owner_id: UUID, key: str) -> StoryboardGeneration | None:
        generation_id = self.generation_by_key.get((owner_id, key))
        return self.generations.get(generation_id) if generation_id else None

    def find_unresolved_generation(
        self, owner_id: UUID, group_id: UUID, canonical_hash: str
    ) -> StoryboardGeneration | None:
        return next(
            (
                item
                for item in self.generations.values()
                if item.owner_id == owner_id
                and item.group_id == group_id
                and item.status is GenerationStatus.REMOTE_STATE_UNKNOWN
            ),
            None,
        )

    def create_generation(self, generation: StoryboardGeneration) -> StoryboardGeneration:
        with self._lock:
            key = (generation.owner_id, generation.idempotency_key)
            existing_id = self.generation_by_key.get(key)
            if existing_id:
                return self.generations[existing_id]
            if any(
                item.owner_id == generation.owner_id
                and item.group_id == generation.group_id
                and item.status not in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}
                for item in self.generations.values()
            ):
                raise DomainError(
                    "remote_state_unresolved", "Equivalent storyboard request is unresolved"
                )
            version = 1 + max(
                (
                    item.version
                    for item in self.generations.values()
                    if item.owner_id == generation.owner_id and item.group_id == generation.group_id
                ),
                default=0,
            )
            stored = replace(generation, version=version)
            self.generations[stored.generation_id] = stored
            self.generation_by_key[key] = stored.generation_id
            return stored

    def set_production_mode(self, owner_id: UUID, project_id: UUID, mode: str) -> None:
        self.production_modes[(owner_id, project_id)] = mode

    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None:
        return self.production_modes.get((owner_id, project_id))

    def project_auto_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        return self.budget_rounds.get((owner_id, project_id), 1)

    def save_generation_and_request_recovery(
        self, generation: StoryboardGeneration, event_id: UUID, now: datetime
    ) -> StoryboardGeneration:
        del event_id
        with self._lock:
            current = self.generations.get(generation.generation_id)
            if current is None or current.owner_id != generation.owner_id:
                raise DomainError("not_found", "Storyboard resource was not found")
            if current.state_revision != generation.state_revision or not _may_transition(
                current.status, generation.status
            ):
                return current
            was_unknown = current.status is GenerationStatus.REMOTE_STATE_UNKNOWN
            stored = replace(generation, state_revision=generation.state_revision + 1)
            self.generations[generation.generation_id] = stored
            if generation.status is GenerationStatus.REMOTE_STATE_UNKNOWN and not was_unknown:
                workflow_id = (
                    f"storyboard-grid-recovery-{generation.owner_id}-{generation.generation_id}"
                )
                self.recovery_events.append((generation.generation_id, workflow_id))
                self.recovery_event_times[generation.generation_id] = now
                self.recovery_pending.add(generation.generation_id)
            return stored

    def supersede_with_auto_retry(
        self,
        failed: StoryboardGeneration,
        *,
        failure_stage: str,
        failure_code: str,
        successor: StoryboardGeneration,
        next_attempt_at: datetime,
    ) -> StoryboardGeneration:
        with self._lock:
            current = self.generations.get(failed.generation_id)
            if current is None or current.owner_id != failed.owner_id:
                raise DomainError("not_found", "Storyboard resource was not found")
            if current.state_revision != failed.state_revision or not _may_transition(
                current.status, GenerationStatus.FAILED
            ):
                return current
            if failed.failover_chain_id is not None:
                if (
                    current.failover_chain_id != failed.failover_chain_id
                    or current.failover_policy_version != failed.failover_policy_version
                    or current.failover_route_index != failed.failover_route_index
                    or current.failover_model_attempt != failed.failover_model_attempt
                    or current.failover_total_attempt != failed.failover_total_attempt
                    or successor.failover_chain_id != failed.failover_chain_id
                    or successor.failover_policy_version != failed.failover_policy_version
                    or successor.supersedes_generation_id != failed.generation_id
                ):
                    raise DomainError("failover_conflict", "Storyboard failover lineage changed")
                attempts = [
                    item
                    for item in self.generations.values()
                    if item.owner_id == failed.owner_id
                    and item.failover_chain_id == failed.failover_chain_id
                ]
                latest_total = max(int(item.failover_total_attempt or 0) for item in attempts)
                latest_group_version = max(
                    item.version
                    for item in self.generations.values()
                    if item.owner_id == failed.owner_id and item.group_id == failed.group_id
                )
                if failed.version != latest_group_version:
                    return current
                if failed.failover_total_attempt != latest_total:
                    return current
                if (
                    len(attempts) != latest_total
                    or successor.failover_total_attempt != latest_total + 1
                ):
                    raise DomainError(
                        "failover_conflict", "Storyboard failover total is not contiguous"
                    )
                if not 1 <= latest_total + 1 <= STORYBOARD_AUTO_MAX_SUBMISSIONS:
                    raise DomainError(
                        "failover_budget_exhausted", "Storyboard failover budget is exhausted"
                    )
                route_attempts = [
                    item
                    for item in attempts
                    if item.failover_route_index == successor.failover_route_index
                ]
                expected_model_attempt = len(route_attempts) + 1
                if successor.failover_model_attempt != expected_model_attempt:
                    raise DomainError(
                        "failover_conflict", "Storyboard failover model attempt is invalid"
                    )
                if expected_model_attempt > 2:
                    raise DomainError(
                        "failover_budget_exhausted", "Storyboard model budget is exhausted"
                    )
                if (
                    successor.failover_route_index is None
                    or successor.failover_route_index < int(failed.failover_route_index or 0)
                    or successor.auto_attempt != successor.failover_total_attempt
                ):
                    raise DomainError(
                        "failover_conflict", "Storyboard failover route moved backwards"
                    )
                if successor.failover_route_index == failed.failover_route_index and (
                    successor.provider != failed.provider or successor.model != failed.model
                ):
                    raise DomainError(
                        "failover_conflict", "Storyboard failover model changed within a route"
                    )
            terminal = replace(
                failed,
                status=GenerationStatus.FAILED,
                failure_stage=failure_stage,
                failure_code=failure_code,
                state_revision=failed.state_revision + 1,
            )
            self.generations[failed.generation_id] = terminal
            version = 1 + max(
                (
                    item.version
                    for item in self.generations.values()
                    if item.owner_id == successor.owner_id and item.group_id == successor.group_id
                ),
                default=0,
            )
            stored = replace(successor, version=version)
            self.generations[stored.generation_id] = stored
            self.generation_by_key[(stored.owner_id, stored.idempotency_key)] = stored.generation_id
            self.retry_schedules[stored.generation_id] = next_attempt_at
            return terminal

    def get_generation(self, owner_id: UUID, generation_id: UUID) -> StoryboardGeneration | None:
        value = self.generations.get(generation_id)
        return value if value is not None and value.owner_id == owner_id else None

    def authorize_generation_recovery(
        self, owner_id: UUID, generation_id: UUID, event_id: UUID, now: datetime
    ) -> StoryboardGeneration:
        with self._lock:
            value = self.get_generation(owner_id, generation_id)
            if value is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if value.status is not GenerationStatus.REMOTE_STATE_UNKNOWN:
                raise DomainError("revision_conflict", "Storyboard generation is not unresolved")
            if generation_id not in self.recovery_pending:
                workflow_id = f"storyboard-grid-recovery-{owner_id}-{event_id}"
                self.recovery_events.append((generation_id, workflow_id))
                self.recovery_event_times[generation_id] = now
                self.recovery_pending.add(generation_id)
            return value

    def list_unknown_recovery_candidates(
        self, since: datetime
    ) -> tuple[StoryboardRecoveryCandidate, ...]:
        with self._lock:
            candidates: list[StoryboardRecoveryCandidate] = []
            for item in self.generations.values():
                if item.status is not GenerationStatus.REMOTE_STATE_UNKNOWN:
                    continue
                if self.production_modes.get((item.owner_id, item.project_id)) != "AUTO":
                    continue
                latest_version = max(
                    candidate.version
                    for candidate in self.generations.values()
                    if candidate.owner_id == item.owner_id and candidate.group_id == item.group_id
                )
                if item.version != latest_version:
                    continue
                event_time = self.recovery_event_times.get(item.generation_id)
                if item.created_at < since and (event_time is None or event_time < since):
                    continue
                latest_workflow_id = next(
                    (
                        workflow_id
                        for generation_id, workflow_id in reversed(self.recovery_events)
                        if generation_id == item.generation_id
                    ),
                    None,
                )
                candidates.append(
                    StoryboardRecoveryCandidate(
                        item, latest_workflow_id, item.generation_id in self.recovery_pending
                    )
                )
            return tuple(candidates)

    def save_generation(self, generation: StoryboardGeneration) -> StoryboardGeneration:
        with self._lock:
            current = self.generations.get(generation.generation_id)
            if current is None or current.owner_id != generation.owner_id:
                raise DomainError("not_found", "Storyboard resource was not found")
            if current.state_revision != generation.state_revision or not _may_transition(
                current.status, generation.status
            ):
                return current
            stored = replace(generation, state_revision=generation.state_revision + 1)
            self.generations[generation.generation_id] = stored
            return stored

    def claim_submit(self, owner_id: UUID, generation_id: UUID) -> StoryboardGeneration | None:
        with self._lock:
            current = self.get_generation(owner_id, generation_id)
            if current is None or current.status.value != "INTENT_PERSISTED":
                return None
            if self.project_states.get((owner_id, current.project_id)) != "ACTIVE":
                return None
            latest_version = max(
                item.version
                for item in self.generations.values()
                if item.owner_id == current.owner_id and item.group_id == current.group_id
            )
            if current.version != latest_version:
                return None
            claimed = replace(
                current,
                status=type(current.status).SUBMITTING,
                state_revision=current.state_revision + 1,
            )
            self.generations[generation_id] = claimed
            return claimed

    def claim_processing(
        self,
        owner_id: UUID,
        generation_id: UUID,
        expected_revision: int,
        token: UUID,
        now: datetime,
        lease_until: datetime,
    ) -> StoryboardGeneration | None:
        with self._lock:
            current = self.get_generation(owner_id, generation_id)
            if (
                current is not None
                and self.project_states.get((owner_id, current.project_id)) != "ACTIVE"
            ):
                return None
            if (
                current is None
                or current.state_revision != expected_revision
                or current.status
                in {
                    GenerationStatus.INTENT_PERSISTED,
                    GenerationStatus.SUCCEEDED,
                    GenerationStatus.FAILED,
                }
                or (current.lease_until is not None and current.lease_until >= now)
            ):
                return None
            claimed = replace(
                current,
                state_revision=current.state_revision + 1,
                execution_token=token,
                lease_until=lease_until,
            )
            self.generations[generation_id] = claimed
            return claimed

    def release_processing(self, owner_id: UUID, generation_id: UUID, token: UUID) -> None:
        with self._lock:
            value = self.get_generation(owner_id, generation_id)
            if value is not None and value.execution_token == token:
                self.generations[generation_id] = replace(
                    value, execution_token=None, lease_until=None
                )

    def complete_and_switch_current(self, generation: StoryboardGeneration) -> StoryboardGeneration:
        with self._lock:
            current_generation = self.generations[generation.generation_id]
            if (
                current_generation.state_revision != generation.state_revision
                or not _may_transition(current_generation.status, generation.status)
            ):
                return current_generation
            generation = replace(generation, state_revision=generation.state_revision + 1)
            self.generations[generation.generation_id] = generation
            key = (generation.owner_id, generation.group_id)
            current_id = self.current_by_group.get(key)
            current = self.generations.get(current_id) if current_id else None
            if current is None or generation.version > current.version:
                self.current_by_group[key] = generation.generation_id
            return generation

    def get_current_generation(self, owner_id: UUID, group_id: UUID) -> StoryboardGeneration | None:
        generation_id = self.current_by_group.get((owner_id, group_id))
        return self.generations.get(generation_id) if generation_id else None

    def list_generation_history(
        self, owner_id: UUID, group_id: UUID
    ) -> tuple[StoryboardGeneration, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.generations.values()
                    if item.owner_id == owner_id and item.group_id == group_id
                ),
                key=lambda item: item.version,
                reverse=True,
            )
        )

    def select_current_generation(
        self, owner_id: UUID, group_id: UUID, generation_id: UUID
    ) -> StoryboardGeneration:
        generation = self.get_generation(owner_id, generation_id)
        if (
            generation is None
            or generation.group_id != group_id
            or generation.status is not GenerationStatus.SUCCEEDED
            or generation.asset is None
        ):
            raise DomainError("invalid_storyboard_candidate", "Storyboard candidate is unavailable")
        self.current_by_group[(owner_id, group_id)] = generation_id
        return generation

    def bind_current_for_test(self, binding: VideoStoryboardBinding) -> None:
        generation = self.get_generation(binding.owner_id, binding.storyboard_generation_id)
        if (
            generation is None
            or generation.status.value != "SUCCEEDED"
            or generation.asset is None
            or generation.asset.media.sha256 != binding.storyboard_asset_sha256
            or generation.project_id != binding.project_id
            or generation.group_id != binding.group_id
            or self.current_by_group.get((binding.owner_id, binding.group_id))
            != binding.storyboard_generation_id
        ):
            raise DomainError(
                "revision_conflict", "Video intent requires the exact successful storyboard asset"
            )
        self.video_bindings[(binding.owner_id, binding.video_id)] = binding

    def get_video_binding(self, owner_id: UUID, video_id: UUID) -> VideoStoryboardBinding | None:
        return self.video_bindings.get((owner_id, video_id))
