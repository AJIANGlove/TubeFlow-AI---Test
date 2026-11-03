from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    VideoCapacityUnavailable,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.engine import EngineNode, NodeHealth
from tubeflow_test.video_generation.orchestration import (
    SUBMISSION_BUDGET_EXHAUSTED,
    ArchiveStage,
    AttemptStage,
    AuthoritativeGroupInput,
    PersistentAttempt,
    validate_attempt_transition,
)
from tubeflow_test.video_generation.provider_audit import ProviderAuditEvent


class InMemoryVideoRepository:
    """Deterministic feature-test repository implementing the production port."""

    def __init__(
        self, groups: tuple[AuthoritativeGroupInput, ...], nodes: tuple[EngineNode, ...]
    ) -> None:
        if len(nodes) != 3:
            raise ValueError("exactly three H3 nodes must be configured")
        self.groups = {item.group_id: item for item in groups}
        self.nodes = {item.node_id: item for item in nodes}
        self.attempts: dict[UUID, PersistentAttempt] = {}
        self.keys: dict[str, UUID] = {}
        self.current: dict[UUID, UUID] = {}
        self._lock = RLock()
        self.provider_events: list[ProviderAuditEvent] = []
        self.scheduled_delays: dict[UUID, float] = {}
        self._release_order: dict[str, int] = {}
        self._release_counter = 0
        # AUTO 测试钩子：项目预算轮次（§15.9）；默认第 1 轮。
        self.budget_rounds: dict[tuple[UUID, UUID], int] = {}

    def append_provider_event(self, event: ProviderAuditEvent) -> None:
        with self._lock:
            self.provider_events.append(event)

    def load_authoritative_group(self, owner_id: UUID, group_id: UUID) -> AuthoritativeGroupInput:
        item = self.groups.get(group_id)
        if item is None or item.owner_id != owner_id:
            raise VideoRuleViolation("narration group not found")
        return item

    def find_attempt(self, owner_id: UUID, idempotency_key: str) -> PersistentAttempt | None:
        with self._lock:
            attempt_id = self.keys.get(f"{owner_id}:{idempotency_key}")
            return self.attempts.get(attempt_id) if attempt_id else None

    def find_unresolved_group(self, owner_id: UUID, group_id: UUID) -> PersistentAttempt | None:
        with self._lock:
            return next(
                (
                    item
                    for item in self.attempts.values()
                    if item.owner_id == owner_id
                    and item.group_id == group_id
                    and item.stage is AttemptStage.REMOTE_STATE_UNKNOWN
                ),
                None,
            )

    def assert_no_other_unresolved(self, attempt: PersistentAttempt) -> None:
        unresolved = self.find_unresolved_group(attempt.owner_id, attempt.group_id)
        if unresolved is not None and unresolved.attempt_id != attempt.attempt_id:
            raise VideoRuleViolation("remote_acceptance_unknown")

    def commit_submitting(self, attempt: PersistentAttempt) -> PersistentAttempt:
        with self._lock:
            source = self.load_authoritative_group(attempt.owner_id, attempt.group_id)
            persisted = (attempt.input_snapshot or {}).get("authority")
            if not persisted or source.authority_snapshot != persisted:
                raise VideoRuleViolation("immutable_video_input_changed")
            self.assert_no_other_unresolved(attempt)
            return self.save_attempt(attempt)

    def get_attempt(self, attempt_id: UUID) -> PersistentAttempt | None:
        with self._lock:
            return self.attempts.get(attempt_id)

    def create_attempt(self, attempt: PersistentAttempt) -> PersistentAttempt:
        with self._lock:
            scoped_key = f"{attempt.owner_id}:{attempt.idempotency_key}"
            existing_id = self.keys.get(scoped_key)
            if existing_id is not None:
                existing = self.attempts[existing_id]
                if existing.generation_sha256 != attempt.generation_sha256:
                    raise VideoRuleViolation("idempotency_conflict")
                return existing
            if any(
                item.owner_id == attempt.owner_id
                and item.group_id == attempt.group_id
                and item.stage
                in {
                    AttemptStage.CREATED,
                    AttemptStage.NOT_SUBMITTED,
                    AttemptStage.STAGING,
                    AttemptStage.SUBMITTING,
                    AttemptStage.SUBMITTED,
                    AttemptStage.RUNNING,
                    AttemptStage.REMOTE_STATE_UNKNOWN,
                }
                for item in self.attempts.values()
            ):
                raise VideoRuleViolation("active_group_video_attempt_exists")
            sequence = 1 + max(
                (
                    item.intent_sequence
                    for item in self.attempts.values()
                    if item.owner_id == attempt.owner_id and item.group_id == attempt.group_id
                ),
                default=0,
            )
            attempt = replace(attempt, intent_sequence=sequence)
            self.attempts[attempt.attempt_id] = attempt
            self.keys[scoped_key] = attempt.attempt_id
            return attempt

    def count_group_attempts(
        self, owner_id: UUID, group_id: UUID, budget_round: int | None = None
    ) -> int:
        with self._lock:
            return sum(
                1
                for item in self.attempts.values()
                if item.owner_id == owner_id
                and item.group_id == group_id
                and (budget_round is None or item.budget_round == budget_round)
            )

    def project_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        return self.budget_rounds.get((owner_id, project_id), 1)

    def supersede_attempt(
        self,
        superseded: PersistentAttempt,
        replacement: PersistentAttempt,
        event: ProviderAuditEvent,
        *,
        delay_seconds: float,
        max_submissions: int,
    ) -> tuple[PersistentAttempt, PersistentAttempt]:
        """原子化旧尝试终态与替代尝试入库；同一替代幂等键重放不产生第二次提交。"""
        with self._lock:
            scoped_key = f"{replacement.owner_id}:{replacement.idempotency_key}"
            existing_id = self.keys.get(scoped_key)
            if existing_id is not None:
                existing = self.attempts[existing_id]
                if existing.generation_sha256 != replacement.generation_sha256:
                    raise VideoRuleViolation("idempotency_conflict")
                current = self.attempts[superseded.attempt_id]
                return current, existing
            if (event.attempt_id, event.owner_id, event.project_id, event.group_id) != (
                superseded.attempt_id,
                superseded.owner_id,
                superseded.project_id,
                superseded.group_id,
            ):
                raise VideoRuleViolation("provider_audit_identity_mismatch")
            if self.count_group_attempts(
                superseded.owner_id, superseded.group_id, budget_round=superseded.budget_round
            ) >= (max_submissions):
                raise VideoRuleViolation(SUBMISSION_BUDGET_EXHAUSTED)
            saved = self.save_attempt(superseded)
            self.provider_events.append(event)
            sequence = 1 + max(
                (
                    item.intent_sequence
                    for item in self.attempts.values()
                    if item.owner_id == replacement.owner_id
                    and item.group_id == replacement.group_id
                ),
                default=0,
            )
            replacement = replace(replacement, intent_sequence=sequence)
            self.attempts[replacement.attempt_id] = replacement
            self.keys[scoped_key] = replacement.attempt_id
            self.scheduled_delays[replacement.attempt_id] = delay_seconds
            return saved, replacement

    def save_attempt(self, attempt: PersistentAttempt) -> PersistentAttempt:
        with self._lock:
            current = self.attempts.get(attempt.attempt_id)
            if current is None:
                raise VideoRuleViolation("unknown attempt")
            if current.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            validate_attempt_transition(current, attempt)
            saved = replace(attempt, revision=attempt.revision + 1)
            self.attempts[attempt.attempt_id] = saved
            return saved

    def save_attempt_with_event(
        self, attempt: PersistentAttempt, event: ProviderAuditEvent
    ) -> PersistentAttempt:
        with self._lock:
            if (event.attempt_id, event.owner_id, event.project_id, event.group_id) != (
                attempt.attempt_id,
                attempt.owner_id,
                attempt.project_id,
                attempt.group_id,
            ):
                raise VideoRuleViolation("provider_audit_identity_mismatch")
            saved = self.save_attempt(attempt)
            self.provider_events.append(event)
            return saved

    def is_current(self, group_id: UUID, attempt_id: UUID) -> bool:
        with self._lock:
            return self.current.get(group_id) == attempt_id

    def mark_current(self, attempt: PersistentAttempt) -> PersistentAttempt:
        with self._lock:
            current = self.attempts[attempt.attempt_id]
            if current.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            validate_attempt_transition(current, attempt)
            existing_id = self.current.get(attempt.group_id)
            existing = self.attempts.get(existing_id) if existing_id else None
            saved = replace(attempt, revision=attempt.revision + 1)
            self.attempts[attempt.attempt_id] = saved
            if existing is None or attempt.intent_sequence > existing.intent_sequence:
                self.current[attempt.group_id] = attempt.attempt_id
            return saved

    def mark_current_if_absent(self, attempt: PersistentAttempt) -> bool:
        with self._lock:
            current = self.attempts[attempt.attempt_id]
            if current.revision != attempt.revision:
                raise VideoRuleViolation("attempt_revision_conflict")
            validate_attempt_transition(current, attempt)
            if attempt.group_id in self.current:
                return False
            saved = replace(attempt, revision=attempt.revision + 1)
            self.attempts[attempt.attempt_id] = saved
            self.current[attempt.group_id] = attempt.attempt_id
            return True

    def select_current(self, owner_id: UUID, group_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        with self._lock:
            attempt = self.attempts.get(attempt_id)
            if (
                attempt is None
                or attempt.owner_id != owner_id
                or attempt.group_id != group_id
                or attempt.stage is not AttemptStage.ARCHIVED
                or attempt.archive_stage is not ArchiveStage.COMPLETE
                or attempt.archived_asset is None
                or (attempt.probe_evidence or {}).get("qc_passed") is not True
            ):
                raise VideoRuleViolation("video candidate not found")
            self.current[group_id] = attempt_id
            return attempt

    def get_current(self, owner_id: UUID, group_id: UUID) -> PersistentAttempt | None:
        with self._lock:
            attempt_id = self.current.get(group_id)
            attempt = self.attempts.get(attempt_id) if attempt_id else None
            return attempt if attempt is not None and attempt.owner_id == owner_id else None

    def list_history(self, owner_id: UUID, group_id: UUID) -> tuple[PersistentAttempt, ...]:
        with self._lock:
            return tuple(
                item
                for item in self.attempts.values()
                if item.owner_id == owner_id and item.group_id == group_id
            )

    def sweep_expired_slots(self) -> int:
        now = datetime.now(UTC)
        reclaimed = 0
        with self._lock:
            for attempt_id, attempt in tuple(self.attempts.items()):
                expires = (
                    datetime.fromisoformat(attempt.slot_lease_expires_at)
                    if attempt.slot_lease_expires_at
                    else None
                )
                if (
                    attempt.slot_claimed
                    and expires is not None
                    and expires < now
                    and attempt.remote_acceptance != "UNKNOWN"
                    and attempt.stage
                    in {AttemptStage.FAILED, AttemptStage.CANCELLED, AttemptStage.ARCHIVED}
                ):
                    assert attempt.node_id is not None
                    node = self.nodes[attempt.node_id]
                    self.nodes[node.node_id] = replace(
                        node, active_executions=max(0, node.active_executions - 1)
                    )
                    self.attempts[attempt_id] = replace(
                        attempt,
                        slot_claimed=False,
                        slot_lease_token=None,
                        slot_lease_owner=None,
                        slot_lease_expires_at=None,
                        revision=attempt.revision + 1,
                    )
                    reclaimed += 1
        return reclaimed

    def claim_compatible_node(
        self, attempt_id: UUID, workflow: ImmutableWorkflowSnapshot
    ) -> EngineNode:
        with self._lock:
            attempt = self.attempts[attempt_id]

            def effective_available(node: EngineNode) -> int:
                stalled = sum(
                    1
                    for item in self.attempts.values()
                    if item.node_id == node.node_id and item.slot_claimed and item.stalled_at
                )
                return node.max_concurrent - node.active_executions + stalled

            if attempt.node_id is not None:
                node = self.nodes[attempt.node_id]
                _compatible(node, workflow)
                if not attempt.slot_claimed:
                    if effective_available(node) <= 0:
                        raise VideoRuleViolation("assigned H3 node has no capacity")
                    self.nodes[node.node_id] = replace(
                        node, active_executions=node.active_executions + 1
                    )
                    self.attempts[attempt_id] = replace(
                        attempt,
                        slot_claimed=True,
                        slot_lease_token=attempt.slot_lease_token or uuid4().hex,
                        slot_lease_owner="group-video-worker",
                        slot_lease_expires_at=(
                            datetime.now(UTC) + timedelta(minutes=2)
                        ).isoformat(),
                        revision=attempt.revision + 1,
                    )
                return self.nodes[node.node_id]
            eligible = [
                node
                for node in self.nodes.values()
                if node.health is NodeHealth.READY
                and effective_available(node) > 0
                and _matches(node, workflow)
            ]
            if not eligible:
                raise VideoCapacityUnavailable()
            node = min(
                eligible,
                key=lambda item: (
                    -effective_available(item),
                    self._release_order.get(item.node_id, -1),
                    item.node_id,
                ),
            )
            claimed = replace(node, active_executions=node.active_executions + 1)
            self.nodes[node.node_id] = claimed
            self.attempts[attempt_id] = replace(
                attempt,
                node_id=node.node_id,
                slot_claimed=True,
                slot_lease_token=uuid4().hex,
                slot_lease_owner="group-video-worker",
                slot_lease_expires_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                revision=attempt.revision + 1,
            )
            return claimed

    def release_node(self, attempt_id: UUID, lease_token: str) -> None:
        with self._lock:
            attempt = self.attempts[attempt_id]
            if not attempt.slot_claimed or attempt.node_id is None:
                return
            if attempt.slot_lease_token != lease_token:
                raise VideoRuleViolation("slot_lease_conflict")
            node = self.nodes[attempt.node_id]
            self.nodes[node.node_id] = replace(
                node, active_executions=max(0, node.active_executions - 1)
            )
            self._release_order[node.node_id] = self._release_counter
            self._release_counter += 1
            self.attempts[attempt_id] = replace(
                attempt,
                slot_claimed=False,
                slot_lease_token=None,
                slot_lease_owner=None,
                slot_lease_expires_at=None,
                revision=attempt.revision + 1,
            )

    def renew_slot(self, attempt_id: UUID, lease_token: str) -> None:
        with self._lock:
            attempt = self.attempts[attempt_id]
            if not attempt.slot_claimed:
                return
            if attempt.slot_lease_token != lease_token:
                raise VideoRuleViolation("slot_lease_conflict")
            self.attempts[attempt_id] = replace(
                attempt,
                slot_lease_expires_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                revision=attempt.revision + 1,
            )

    def rotate_slot_lease(
        self, attempt_id: UUID, lease_token: str, lease_owner: str
    ) -> PersistentAttempt:
        with self._lock:
            attempt = self.attempts[attempt_id]
            if not attempt.slot_claimed or attempt.slot_lease_token != lease_token:
                raise VideoRuleViolation("slot_lease_conflict")
            rotated = replace(
                attempt,
                slot_lease_token=uuid4().hex,
                slot_lease_owner=lease_owner,
                slot_lease_expires_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                revision=attempt.revision + 1,
            )
            self.attempts[attempt_id] = rotated
            return rotated

    def get_assigned_node(
        self, attempt_id: UUID, workflow: ImmutableWorkflowSnapshot
    ) -> EngineNode:
        with self._lock:
            attempt = self.attempts[attempt_id]
            if attempt.node_id is None:
                raise VideoRuleViolation("attempt has no assigned node")
            node = self.nodes[attempt.node_id]
            _compatible(node, workflow)
            return node


def _matches(node: EngineNode, workflow: ImmutableWorkflowSnapshot) -> bool:
    return (
        node.supports_reference_images_to_video
        and node.model_key == workflow.model_key
        and node.model_version == workflow.model_version
        and node.workflow_id == workflow.workflow_id
        and node.workflow_version == workflow.workflow_version
        and node.manifest_sha256 == workflow.workflow_manifest_sha256
    )


def _compatible(node: EngineNode, workflow: ImmutableWorkflowSnapshot) -> None:
    if not _matches(node, workflow):
        raise VideoRuleViolation("assigned H3 node capability or version no longer matches")
