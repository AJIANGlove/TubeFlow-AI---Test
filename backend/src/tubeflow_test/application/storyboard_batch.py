from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tubeflow_test.application.ports import IdentifierFactory
from tubeflow_test.application.storyboards import (
    DirectorPlanService,
    StoryboardGenerationService,
    StoryboardRepository,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    ProjectDirectorGroupCandidate,
    ProjectDirectorMaterialization,
)
from tubeflow_test.domain.storyboards import (
    ContinuousObservationApproval,
    DirectorShot,
    GenerationStatus,
    ReviewState,
)


@dataclass(frozen=True, slots=True)
class StoryboardBatchGroupOverview:
    group_id: UUID
    group_sequence: int
    has_draft: bool
    draft_review_state: str | None
    has_applied: bool
    storyboard_status: str | None
    storyboard_version: int | None
    current_generation_id: UUID | None
    can_generate: bool


@dataclass(frozen=True, slots=True)
class StoryboardBatchSeedResult:
    group_id: UUID
    group_sequence: int
    outcome: str
    review_state: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class StoryboardBatchGenerateResult:
    group_id: UUID
    group_sequence: int
    outcome: str
    generation_id: UUID | None = None
    message: str | None = None


_NON_TERMINAL = {
    GenerationStatus.INTENT_PERSISTED,
    GenerationStatus.SUBMITTING,
    GenerationStatus.SUBMITTED,
    GenerationStatus.RUNNING,
    GenerationStatus.DOWNLOADED,
    GenerationStatus.QC_PASSED,
    GenerationStatus.OBJECT_STORED,
    GenerationStatus.REMOTE_STATE_UNKNOWN,
}

# AUTO 全链路自动生产的连续观察系统审批理由（2026-10-12 用户决策，USER_DECISIONS §20）：
# 依 §15.8 自动模式已取消逐组人工批准；审批由系统附加并随不可变修订留痕，非人工批准。
AUTO_CONTINUOUS_OBSERVATION_REASON = (
    "AUTO 全链路自动生产系统审批：依 §15.8/§20 取消逐组人工批准，"
    "相邻镜头差异不足由系统记录并放行，非人工批准。"
)


def _system_approvals_for(
    shots: tuple[DirectorShot, ...],
    existing: tuple[ContinuousObservationApproval, ...] = (),
) -> tuple[ContinuousObservationApproval, ...]:
    """为未获批的歧义相邻对（changes_from < 2）生成系统审批；已获批对不重复。"""
    approved = {(item.previous_shot_id, item.next_shot_id) for item in existing}
    return tuple(
        ContinuousObservationApproval(
            previous.shot_id, shot.shot_id, AUTO_CONTINUOUS_OBSERVATION_REASON
        )
        for previous, shot in zip(shots, shots[1:], strict=False)
        if shot.changes_from(previous) < 2 and (previous.shot_id, shot.shot_id) not in approved
    )


class StoryboardMaterializationPort(Protocol):
    def current_materialization(
        self, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorMaterialization: ...


class StoryboardBatchService:
    """Project-scoped batch orchestration for director drafts and grid sheets.

    Every method keeps per-group isolation: one failing group never aborts the
    batch, and no failure path ever retries or resubmits a remote task on its
    own — generation intents are only created by the user's explicit batch
    action.
    """

    def __init__(
        self,
        storyboards: StoryboardRepository,
        director: DirectorPlanService,
        generations: StoryboardGenerationService,
        materializations: StoryboardMaterializationPort,
        ids: IdentifierFactory,
    ) -> None:
        self._storyboards = storyboards
        self._director = director
        self._generations = generations
        self._materializations = materializations
        self._ids = ids

    def overview(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardBatchGroupOverview, ...]:
        candidates = self._candidates(owner_id, project_id)
        items: list[StoryboardBatchGroupOverview] = []
        for candidate in candidates:
            group_id = candidate.group_id
            draft = self._storyboards.get_draft_revision(owner_id, group_id)
            applied = self._storyboards.get_applied_revision(owner_id, group_id)
            current = self._storyboards.get_current_generation(owner_id, group_id)
            history = self._storyboards.list_generation_history(owner_id, group_id)
            latest = history[0] if history else None
            status: str | None = None
            version: int | None = None
            if current is not None and current.status is GenerationStatus.SUCCEEDED:
                status, version = current.status.value, current.version
            elif latest is not None:
                status, version = latest.status.value, latest.version
            items.append(
                StoryboardBatchGroupOverview(
                    group_id,
                    candidate.group_sequence,
                    draft is not None,
                    draft.review_state.value if draft is not None else None,
                    applied is not None,
                    status,
                    version,
                    current.generation_id if current is not None else None,
                    (draft is not None and draft.may_apply)
                    or (draft is None and applied is not None),
                )
            )
        return tuple(items)

    def seed_drafts(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        auto_approve_ambiguous: bool = False,
    ) -> tuple[StoryboardBatchSeedResult, ...]:
        results: list[StoryboardBatchSeedResult] = []
        for candidate in self._candidates(owner_id, project_id):
            group_id = candidate.group_id
            try:
                if self._storyboards.get_draft_revision(owner_id, group_id) is not None:
                    results.append(
                        StoryboardBatchSeedResult(
                            group_id, candidate.group_sequence, "already_draft"
                        )
                    )
                    continue
                if self._storyboards.get_applied_revision(owner_id, group_id) is not None:
                    results.append(
                        StoryboardBatchSeedResult(
                            group_id, candidate.group_sequence, "already_applied"
                        )
                    )
                    continue
                revision = self._director.edit_draft(
                    owner_id=owner_id,
                    group_id=group_id,
                    shots=candidate.shots,
                    links=candidate.sentence_links,
                    expected_draft_revision_id=None,
                    continuous_observation_approvals=(
                        _system_approvals_for(candidate.shots) if auto_approve_ambiguous else ()
                    ),
                    source_candidate_id=candidate.candidate_id,
                )
                results.append(
                    StoryboardBatchSeedResult(
                        group_id,
                        candidate.group_sequence,
                        "seeded",
                        review_state=revision.review_state.value,
                    )
                )
            except (DomainError, ValueError) as exc:
                results.append(
                    StoryboardBatchSeedResult(
                        group_id, candidate.group_sequence, "error", message=str(exc)
                    )
                )
        return tuple(results)

    def repair_ambiguous_drafts(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardBatchSeedResult, ...]:
        """AUTO 恢复路径：为仍处 REQUIRED 的既有草稿附加系统审批并取代修订（§20 决策）。

        仅处理「有草稿、REQUIRED、无已应用修订」的组；无草稿或结构性冲突的组
        不在本方法范围，仍由上层如实暂停。
        """
        results: list[StoryboardBatchSeedResult] = []
        for candidate in self._candidates(owner_id, project_id):
            group_id = candidate.group_id
            draft = self._storyboards.get_draft_revision(owner_id, group_id)
            if (
                draft is None
                or draft.review_state is not ReviewState.REQUIRED
                or self._storyboards.get_applied_revision(owner_id, group_id) is not None
            ):
                continue
            additions = _system_approvals_for(draft.shots, draft.continuous_observation_approvals)
            if not additions:
                continue
            try:
                revision = self._director.edit_draft(
                    owner_id=owner_id,
                    group_id=group_id,
                    shots=draft.shots,
                    links=draft.sentence_links,
                    expected_draft_revision_id=draft.revision_id,
                    continuous_observation_approvals=(
                        draft.continuous_observation_approvals + additions
                    ),
                )
                results.append(
                    StoryboardBatchSeedResult(
                        group_id,
                        candidate.group_sequence,
                        "repaired",
                        review_state=revision.review_state.value,
                    )
                )
            except (DomainError, ValueError) as exc:
                results.append(
                    StoryboardBatchSeedResult(
                        group_id, candidate.group_sequence, "error", message=str(exc)
                    )
                )
        return tuple(results)

    def generate(
        self, owner_id: UUID, project_id: UUID, group_ids: tuple[UUID, ...], *, force: bool
    ) -> tuple[StoryboardBatchGenerateResult, ...]:
        candidates = self._candidates(owner_id, project_id)
        sequences = {item.group_id: item.group_sequence for item in candidates}
        batch_id = self._ids.new()
        results: list[StoryboardBatchGenerateResult] = []
        for group_id in dict.fromkeys(group_ids):
            sequence = sequences.get(group_id, 0)
            try:
                results.append(
                    self._generate_one(owner_id, group_id, sequence, force=force, batch_id=batch_id)
                )
            except DomainError as exc:
                outcome = "unresolved" if exc.code == "remote_state_unresolved" else "error"
                results.append(
                    StoryboardBatchGenerateResult(group_id, sequence, outcome, message=exc.message)
                )
            except ValueError as exc:
                results.append(
                    StoryboardBatchGenerateResult(group_id, sequence, "error", message=str(exc))
                )
        return tuple(results)

    def _generate_one(
        self, owner_id: UUID, group_id: UUID, sequence: int, *, force: bool, batch_id: UUID
    ) -> StoryboardBatchGenerateResult:
        history = self._storyboards.list_generation_history(owner_id, group_id)
        if history and history[0].status in _NON_TERMINAL:
            outcome = (
                "unresolved"
                if history[0].status is GenerationStatus.REMOTE_STATE_UNKNOWN
                else "already_running"
            )
            return StoryboardBatchGenerateResult(group_id, sequence, outcome)
        current = self._storyboards.get_current_generation(owner_id, group_id)
        if (
            not force
            and current is not None
            and current.status is GenerationStatus.SUCCEEDED
        ):
            return StoryboardBatchGenerateResult(
                group_id, sequence, "skipped_succeeded", generation_id=current.generation_id
            )
        draft = self._storyboards.get_draft_revision(owner_id, group_id)
        applied = self._storyboards.get_applied_revision(owner_id, group_id)
        if draft is not None:
            if not draft.may_apply:
                return StoryboardBatchGenerateResult(group_id, sequence, "needs_review")
            applied = self._director.apply_draft(
                owner_id=owner_id, group_id=group_id, expected_draft_revision_id=draft.revision_id
            )
        elif applied is None:
            return StoryboardBatchGenerateResult(group_id, sequence, "no_director")
        generation = self._generations.create(
            group_id=group_id,
            owner_id=owner_id,
            expected_applied_revision_id=applied.revision_id,
            idempotency_key=f"batch-{batch_id}-{group_id}",
        )
        return StoryboardBatchGenerateResult(
            group_id, sequence, "queued", generation_id=generation.generation_id
        )

    def _candidates(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[ProjectDirectorGroupCandidate, ...]:
        materialization = self._materializations.current_materialization(owner_id, project_id)
        return tuple(sorted(materialization.candidates, key=lambda item: item.group_sequence))
