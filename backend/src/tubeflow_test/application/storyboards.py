from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from tubeflow_test.application.ports import Clock, IdentifierFactory, ObjectStoragePort
from tubeflow_test.application.storyboard_failover import (
    StoryboardFailoverPolicy,
    StoryboardFailoverPosition,
)
from tubeflow_test.application.storyboard_prompt import (
    apply_prompt_safety,
    build_grid_sheet_prompt,
)
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.director_generation import DirectorSentenceSnapshot
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    GRID_LAYOUT_V1,
    GRID_LAYOUT_V2,
    SHOT_TIMING_PROJECTION_V1,
    STORYBOARD_AUTO_MAX_SUBMISSIONS,
    CharacterAssetSnapshot,
    ContinuousObservationApproval,
    DirectorLocationRelation,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
    GenerationStatus,
    ReviewState,
    ShotSentenceLink,
    ShotTimeRange,
    ShotTimingProjectionError,
    StoryboardAsset,
    StoryboardAssetKind,
    StoryboardGeneration,
    VideoStoryboardBinding,
    projected_shot_time_ranges_ms,
    storyboard_auto_retry_delay_seconds,
)

# §15.2/§15.3（仅 AUTO 项目）：connect 阶段失败与 429 在 3 次提交预算内自动退避重提，
# 429 尊重 Retry-After；预算用尽后以该稳定码落 FAILED 终态。MANUAL 项目不适用。
STORYBOARD_SUBMISSION_BUDGET_EXHAUSTED_FAILURE = "failover_budget_exhausted"
# §15.4（仅 AUTO 项目）：未知态自动对账无果（Provider 查无此任务）时自动重提，
# 前次尝试以 reconcile_no_remote_task 落 FAILED；预算用尽后未知链以
# remote_state_unknown_exhausted 稳定终态结束。MANUAL 项目不适用。
STORYBOARD_RECONCILE_NO_REMOTE_TASK_FAILURE = "reconcile_no_remote_task"
STORYBOARD_UNKNOWN_BUDGET_EXHAUSTED_FAILURE = "remote_state_unknown_exhausted"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StoryboardContext:
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    sentence_ids: tuple[UUID, ...]
    narration_ready: bool
    character_assets: tuple[CharacterAssetSnapshot, ...]
    style: str
    sentences: tuple[DirectorSentenceSnapshot, ...] = ()
    locale: str = ""
    aspect_ratio: str = ""


@dataclass(frozen=True, slots=True)
class StoryboardRecoveryCandidate:
    generation: StoryboardGeneration
    latest_workflow_id: str | None
    has_pending_recovery: bool
    group_sequence: int = 0


@dataclass(frozen=True, slots=True)
class StoryboardProviderConfig:
    model: str
    parameters: dict[str, str | int | float | bool]


def canonical_storyboard_request_sha256(
    *,
    owner_id: UUID,
    group_id: UUID,
    revision_id: UUID,
    prompt: str,
    characters: list[dict[str, object]],
    style: str,
    model: str,
    parameters: Mapping[str, str | int | float | bool],
    provider: str = "",
) -> str:
    body = {
        "owner_id": str(owner_id),
        "group_id": str(group_id),
        "revision_id": str(revision_id),
        "prompt": prompt,
        "characters": characters,
        "style": style,
        "model": model,
        "parameters": parameters,
    }
    # 0036 前历史哈希不包含 Provider；只有新链显式绑定时才纳入，避免旧资产失真。
    if provider:
        body["provider"] = provider
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def canonical_character_payload(
    *,
    ordinal: int,
    character_image_id: UUID,
    asset_id: UUID,
    sha256: str,
    media_type: str,
    size_bytes: int,
) -> dict[str, object]:
    """角色资产在权威请求哈希中的唯一序列化口径（1 基序号）。

    故事板生成端与 H3 装配校验端必须使用同一构造函数；装配端从
    project_snapshot_characters（0 基 ordinal）重建时以 ordinal + 1 调用。
    """
    return {
        "ordinal": ordinal,
        "character_image_id": str(character_image_id),
        "asset_id": str(asset_id),
        "sha256": sha256,
        "media_type": media_type,
        "size_bytes": size_bytes,
    }


class StoryboardRepository(Protocol):
    def context(self, owner_id: UUID, group_id: UUID) -> StoryboardContext | None: ...
    def get_applied_revision(
        self, owner_id: UUID, group_id: UUID
    ) -> DirectorPlanRevision | None: ...
    def get_draft_revision(self, owner_id: UUID, group_id: UUID) -> DirectorPlanRevision | None: ...
    def create_revision(
        self,
        revision: DirectorPlanRevision,
        expected_draft_revision_id: UUID | None,
        source_candidate_id: UUID | None = None,
    ) -> DirectorPlanRevision: ...
    def apply_revision(
        self, owner_id: UUID, group_id: UUID, revision_id: UUID, expected_draft_revision_id: UUID
    ) -> DirectorPlanRevision: ...
    def find_generation_by_key(self, owner_id: UUID, key: str) -> StoryboardGeneration | None: ...
    def find_unresolved_generation(
        self, owner_id: UUID, group_id: UUID, canonical_hash: str
    ) -> StoryboardGeneration | None: ...
    def create_generation(self, generation: StoryboardGeneration) -> StoryboardGeneration: ...
    def get_generation(
        self, owner_id: UUID, generation_id: UUID
    ) -> StoryboardGeneration | None: ...
    def save_generation(self, generation: StoryboardGeneration) -> StoryboardGeneration: ...
    def claim_submit(self, owner_id: UUID, generation_id: UUID) -> StoryboardGeneration | None: ...
    def claim_processing(
        self,
        owner_id: UUID,
        generation_id: UUID,
        expected_revision: int,
        token: UUID,
        now: datetime,
        lease_until: datetime,
    ) -> StoryboardGeneration | None: ...
    def release_processing(self, owner_id: UUID, generation_id: UUID, token: UUID) -> None: ...
    def complete_and_switch_current(
        self, generation: StoryboardGeneration
    ) -> StoryboardGeneration: ...
    def get_current_generation(
        self, owner_id: UUID, group_id: UUID
    ) -> StoryboardGeneration | None: ...
    def list_generation_history(
        self, owner_id: UUID, group_id: UUID
    ) -> tuple[StoryboardGeneration, ...]: ...
    def select_current_generation(
        self, owner_id: UUID, group_id: UUID, generation_id: UUID
    ) -> StoryboardGeneration: ...
    def authorize_generation_recovery(
        self, owner_id: UUID, generation_id: UUID, event_id: UUID, now: datetime
    ) -> StoryboardGeneration: ...
    def list_unknown_recovery_candidates(
        self, since: datetime
    ) -> tuple[StoryboardRecoveryCandidate, ...]: ...
    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None: ...
    def project_auto_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        """当前预算轮次（projects.auto_budget_round）；MANUAL 恒为 1。"""
        ...

    def project_grid_layout_version(self, owner_id: UUID, project_id: UUID) -> str | None:
        """项目最早一次故事板生成的网格版式版本；尚无任何生成时为 None（§46 钉版）。"""
        ...

    def save_generation_and_request_recovery(
        self, generation: StoryboardGeneration, event_id: UUID, now: datetime
    ) -> StoryboardGeneration: ...
    def supersede_with_auto_retry(
        self,
        failed: StoryboardGeneration,
        *,
        failure_stage: str,
        failure_code: str,
        successor: StoryboardGeneration,
        next_attempt_at: datetime,
    ) -> StoryboardGeneration: ...
    def get_video_binding(
        self, owner_id: UUID, video_id: UUID
    ) -> VideoStoryboardBinding | None: ...


class StoryboardBindingAuthorityPort(Protocol):
    """Called by the user-triggered H3 transaction; never creates an H3 intent."""

    def bind_current_in_transaction(
        self, transaction: object, binding: VideoStoryboardBinding
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderSubmission:
    task_id: str
    receipt: str


@dataclass(frozen=True, slots=True)
class ProviderPoll:
    status: GenerationStatus
    receipt: str
    download_token: str | None = None
    expected_sha256: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class GridSheetReference:
    ordinal: int
    media_type: str
    sha256: str
    content: bytes


class GridSheetImageProvider(Protocol):
    def close(self) -> None: ...

    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[GridSheetReference, ...] = ()
    ) -> ProviderSubmission: ...
    def reconcile(self, idempotency_key: str) -> ProviderSubmission | None: ...
    def poll(self, task_id: str) -> ProviderPoll: ...
    def download(self, download_token: str, destination: Path) -> None: ...


class ProviderReconcileUnsupportedError(Exception):
    """The provider cannot reconcile a submission without a trusted task id."""


class ProviderSubmitNotDeliveredError(Exception):
    """The submit provably never reached the provider (connect-stage failure)."""


class ProviderSubmitRejectedError(Exception):
    """The provider definitively rejected the submit; nothing is running remotely."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str | None = None,
        submission: ProviderSubmission | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.submission = submission


class ProviderChannelUnavailableError(Exception):
    """Authentication or balance failure that disables the remaining channel route."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str = "provider_channel_unavailable",
        submission: ProviderSubmission | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.submission = submission


class ProviderSubmitServerError(Exception):
    """A definite provider 5xx response; an optional task receipt prevents resubmission."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        submission: ProviderSubmission | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.submission = submission


class ProviderSubmitThrottledError(Exception):
    """The provider answered HTTP 429; the submit may be retried after a backoff."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        submission: ProviderSubmission | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.submission = submission


class StoryboardAspectRatioError(ValueError):
    """最终图片画幅不符合固定横屏合同。"""


class StoryboardPanelLayoutError(ValueError):
    """§46：网格版式 v2 的故事板无法被确定性逐格裁切（按可重试输出波动处理）。"""


STORYBOARD_PANEL_LAYOUT_UNREADABLE = "storyboard_panel_layout_unreadable"


class StoryboardPanelLayoutCheck(Protocol):
    """对已下载、已过技术质检的整图做逐格裁切演练；无法安全裁切时抛 ValueError。"""

    def __call__(
        self, path: Path, *, panel_count: int, aspect_ratio: str, layout_version: str
    ) -> None: ...


class StoryboardTechnicalQc(Protocol):
    def inspect(
        self, path: Path, expected_sha256: str | None = None
    ) -> tuple[str, int, int, str, int]: ...


def _submit_failure_code(exc: Exception) -> str:
    return type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__


_PROVIDER_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_PROVIDER_BEARER_PATTERN = re.compile(r"\bBearer\s+[^\s\"'<>]+", re.IGNORECASE)
_PROVIDER_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|signature)(\s*[=:]\s*|\s+)([^\s,;\"'<>]+)"
)
_PROVIDER_WHITESPACE_PATTERN = re.compile(r"\s+")


def provider_safe_error_text(value: object) -> str:
    """把 Provider 错误压缩为可公开文本，移除 URL、Bearer 与键值秘密。"""
    if isinstance(value, dict):
        for key in ("type", "message", "code"):
            if key in value:
                return provider_safe_error_text(value[key])
        return "provider_failed"
    if value is None:
        return "provider_failed"
    text = _PROVIDER_WHITESPACE_PATTERN.sub(" ", str(value)).strip()
    if not text:
        return "provider_failed"
    text = _PROVIDER_URL_PATTERN.sub("<redacted-url>", text)
    text = _PROVIDER_BEARER_PATTERN.sub("Bearer <redacted>", text)
    text = _PROVIDER_SECRET_PATTERN.sub(r"\1\2<redacted>", text)
    return text[:128]


_PROVIDER_TERMINAL_FAILURE_CODES = {
    "input_moderation",
    "output_moderation",
    "content_against_policy",
    "content_policy_violation",
    "invalid_request_error",
    "invalid_prompt",
    "parameter_invalid",
}


def _provider_failure_may_failover(failure_code: str | None) -> bool:
    # 只对明确的生成失败转移；审核与参数类稳定码不得通过换模型绕过。
    return (failure_code or "").strip().casefold() not in _PROVIDER_TERMINAL_FAILURE_CODES


def _not_found() -> DomainError:
    return DomainError("not_found", "Storyboard resource was not found")


def _all_semantics_unspecified(shot: DirectorShot) -> bool:
    return (
        shot.semantic_event is DirectorSemanticEvent.UNSPECIFIED
        and shot.subject_focus is DirectorSubjectFocus.UNSPECIFIED
        and shot.time_relation is DirectorTimeRelation.UNSPECIFIED
        and shot.location_relation is DirectorLocationRelation.UNSPECIFIED
    )


class DirectorPlanService:
    def __init__(self, repo: StoryboardRepository, ids: IdentifierFactory, clock: Clock) -> None:
        self._repo, self._ids, self._clock = repo, ids, clock

    def edit_draft(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        shots: tuple[DirectorShot, ...],
        links: tuple[ShotSentenceLink, ...],
        expected_draft_revision_id: UUID | None,
        continuous_observation_approvals: tuple[ContinuousObservationApproval, ...],
        source_candidate_id: UUID | None = None,
    ) -> DirectorPlanRevision:
        shots = self._inherit_controlled_semantics(owner_id, group_id, shots)
        revision = self.prepare_draft(
            owner_id=owner_id,
            group_id=group_id,
            shots=shots,
            links=links,
            continuous_observation_approvals=continuous_observation_approvals,
        )
        return self._repo.create_revision(
            revision,
            expected_draft_revision_id,
            source_candidate_id,
        )

    def _inherit_controlled_semantics(
        self, owner_id: UUID, group_id: UUID, shots: tuple[DirectorShot, ...]
    ) -> tuple[DirectorShot, ...]:
        """PUT 合同不携带受控语义字段：按 shot_id 从当前草稿继承，保证全链路同一把多样性尺子。

        已带完整语义的镜头（候选播种/系统修复路径）原样保留；全新镜头保持
        UNSPECIFIED，相邻混合对由 changes_from 既有回退分支按文本字段比较。
        """
        if all(shot.has_controlled_semantics for shot in shots):
            return shots
        current = self._repo.get_draft_revision(owner_id, group_id)
        if current is None:
            return shots
        inherited = {shot.shot_id: shot for shot in current.shots if shot.has_controlled_semantics}
        if not inherited:
            return shots
        return tuple(
            self._with_inherited_semantics(shot, inherited.get(shot.shot_id)) for shot in shots
        )

    @staticmethod
    def _with_inherited_semantics(shot: DirectorShot, source: DirectorShot | None) -> DirectorShot:
        if source is None or shot.has_controlled_semantics or not _all_semantics_unspecified(shot):
            return shot
        return replace(
            shot,
            semantic_event=source.semantic_event,
            subject_focus=source.subject_focus,
            time_relation=source.time_relation,
            location_relation=source.location_relation,
        )

    def prepare_draft(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        shots: tuple[DirectorShot, ...],
        links: tuple[ShotSentenceLink, ...],
        continuous_observation_approvals: tuple[ContinuousObservationApproval, ...],
    ) -> DirectorPlanRevision:
        context = self._repo.context(owner_id, group_id)
        if context is None:
            raise _not_found()
        if not context.narration_ready:
            raise DomainError("narration_not_ready", "Real narration gate has not passed")
        approved = {
            (item.previous_shot_id, item.next_shot_id) for item in continuous_observation_approvals
        }
        ambiguous = any(
            shot.changes_from(previous) < 2 and (previous.shot_id, shot.shot_id) not in approved
            for previous, shot in zip(shots, shots[1:], strict=False)
        )
        review = (
            ReviewState.REQUIRED
            if ambiguous
            else ReviewState.APPROVED
            if continuous_observation_approvals
            else ReviewState.NOT_REQUIRED
        )
        revision = DirectorPlanRevision(
            self._ids.new(),
            owner_id,
            context.project_id,
            group_id,
            1,
            DirectorRevisionKind.DRAFT,
            shots,
            links,
            context.sentence_ids,
            review,
            continuous_observation_approvals,
            self._clock.now(),
        )
        return revision

    def apply_draft(
        self, *, owner_id: UUID, group_id: UUID, expected_draft_revision_id: UUID
    ) -> DirectorPlanRevision:
        draft = self._repo.get_draft_revision(owner_id, group_id)
        context = self._repo.context(owner_id, group_id)
        if draft is None or context is None:
            raise _not_found()
        if not context.narration_ready:
            raise DomainError("narration_not_ready", "Real narration gate has not passed")
        if not draft.may_apply:
            raise DomainError("approval_required", "Human diversity approval is required")
        return self._repo.apply_revision(
            owner_id, group_id, draft.revision_id, expected_draft_revision_id
        )

    def current_draft(self, owner_id: UUID, group_id: UUID) -> DirectorPlanRevision:
        value = self._repo.get_draft_revision(owner_id, group_id)
        if value is None:
            raise _not_found()
        return value

    def current_applied(self, owner_id: UUID, group_id: UUID) -> DirectorPlanRevision:
        value = self._repo.get_applied_revision(owner_id, group_id)
        if value is None:
            raise _not_found()
        return value


class StoryboardGenerationService:
    def __init__(
        self,
        repo: StoryboardRepository,
        provider: GridSheetImageProvider | Mapping[str, GridSheetImageProvider],
        storage: ObjectStoragePort,
        qc: StoryboardTechnicalQc,
        ids: IdentifierFactory,
        clock: Clock,
        work_dir: Path,
        failover_policy: StoryboardFailoverPolicy,
        *,
        single_panel_new_projects: bool = False,
        panel_layout_check: StoryboardPanelLayoutCheck | None = None,
    ) -> None:
        self._repo, self._provider, self._storage, self._qc = repo, provider, storage, qc
        self._ids, self._clock, self._work_dir, self._policy = ids, clock, work_dir, failover_policy
        # §46：开关只决定"尚无任何故事板"的项目钉哪一版网格；已钉项目永远沿用。
        self._single_panel_new_projects = single_panel_new_projects
        self._panel_layout_check = panel_layout_check

    def create(
        self,
        *,
        group_id: UUID,
        owner_id: UUID,
        expected_applied_revision_id: UUID,
        idempotency_key: str,
    ) -> StoryboardGeneration:
        context = self._repo.context(owner_id, group_id)
        applied = self._repo.get_applied_revision(owner_id, group_id)
        if context is None or applied is None:
            raise _not_found()
        if not context.narration_ready:
            raise DomainError("narration_not_ready", "Real narration gate has not passed")
        if applied.revision_id != expected_applied_revision_id:
            raise DomainError("revision_conflict", "Applied director revision changed")
        ranges = self._shot_time_ranges(applied, context)
        layout_version = self._grid_layout_version(owner_id, context.project_id)
        prompt = build_grid_sheet_prompt(
            style=context.style,
            aspect_ratio=context.aspect_ratio,
            shots=applied.shots,
            ranges=ranges,
            character_count=len(context.character_assets),
            layout_version=layout_version,
        )
        # 违禁/高风险表达识别与规避（storyboard-prompt-safety-v1）：规则命中时对
        # 生图提示词做确定性最小替换并留档；镜头字段快照不因此改变。
        prompt, prompt_safety = apply_prompt_safety(prompt)
        aspect_ratio = context.aspect_ratio or "16:9"
        if aspect_ratio != "16:9":
            raise DomainError(
                "aspect_ratio_not_supported", "Storyboard generation is fixed to landscape 16:9"
            )
        position = self._policy.first
        parameters = dict(position.step.parameters)
        canonical = self._canonical_hash(
            owner_id,
            group_id,
            applied,
            prompt,
            context,
            parameters,
            provider=position.step.provider,
            model=position.step.model,
        )
        provider_key = hashlib.sha256(f"{owner_id}:{idempotency_key}".encode()).hexdigest()
        existing = self._repo.find_generation_by_key(owner_id, idempotency_key)
        if existing is not None:
            if existing.canonical_request_sha256 != canonical:
                raise DomainError(
                    "idempotency_conflict", "Idempotency key is bound to another request"
                )
            return existing
        if self._repo.find_unresolved_generation(owner_id, group_id, canonical) is not None:
            raise DomainError(
                "remote_state_unresolved",
                "An earlier grid submission with the same input must be reconciled",
            )
        generation_id = self._ids.new()
        generation = StoryboardGeneration(
            generation_id,
            owner_id,
            context.project_id,
            group_id,
            applied.revision_id,
            applied.shots,
            applied.sentence_links,
            1,
            StoryboardAssetKind.GRID_SHEET,
            prompt,
            context.character_assets,
            context.style,
            position.step.model,
            parameters,
            idempotency_key,
            provider_key,
            canonical,
            GenerationStatus.INTENT_PERSISTED,
            self._clock.now(),
            shot_time_ranges_snapshot=ranges,
            shot_time_ranges_algorithm=SHOT_TIMING_PROJECTION_V1,
            budget_round=self._repo.project_auto_budget_round(owner_id, context.project_id),
            prompt_safety=prompt_safety,
            provider=position.step.provider,
            failover_chain_id=generation_id,
            failover_policy_version=self._policy.version,
            failover_route_index=position.route_index,
            failover_model_attempt=position.model_attempt,
            failover_total_attempt=position.total_attempt,
            grid_layout_version=layout_version,
        )
        created = self._repo.create_generation(generation)
        if created.canonical_request_sha256 != canonical:
            raise DomainError("idempotency_conflict", "Idempotency key is bound to another request")
        return created

    def resume(self, generation_id: UUID, owner_id: UUID) -> StoryboardGeneration:
        generation = self._repo.get_generation(owner_id, generation_id)
        if generation is None:
            raise _not_found()
        if generation.status in {GenerationStatus.SUCCEEDED, GenerationStatus.FAILED}:
            return generation
        if generation.status is GenerationStatus.INTENT_PERSISTED:
            context = self._repo.context(owner_id, generation.group_id)
            if (
                context is None
                or not context.narration_ready
                or context.project_id != generation.project_id
            ):
                raise DomainError("narration_not_ready", "Real narration gate has not passed")
            aspect_ratio = context.aspect_ratio or str(
                generation.parameters.get("aspect_ratio")
                or generation.parameters.get("aspectRatio")
                or "16:9"
            )
            if aspect_ratio != "16:9":
                return self._repo.save_generation(
                    replace(
                        generation,
                        status=GenerationStatus.FAILED,
                        failure_stage="submit",
                        failure_code="aspect_ratio_not_supported",
                    )
                )
            claimed = self._repo.claim_submit(owner_id, generation_id)
            if claimed is None:
                return self._repo.get_generation(owner_id, generation_id) or generation
            try:
                references = tuple(
                    GridSheetReference(
                        item.ordinal,
                        item.media_type,
                        item.sha256,
                        self._storage.read_verified(
                            MediaAssetRef(
                                item.asset_id, item.sha256, item.media_type, item.size_bytes
                            )
                        ),
                    )
                    for item in claimed.character_assets_snapshot
                )
            except (OSError, ValueError, DomainError) as exc:
                # 本地参考图读取发生在任何 Provider 调用之前，不能伪装成远端 UNKNOWN。
                return self._repo.save_generation(
                    replace(
                        claimed,
                        status=GenerationStatus.FAILED,
                        failure_stage="reference_read",
                        failure_code=getattr(exc, "code", type(exc).__name__),
                    )
                )
            try:
                submission = self._provider_for(claimed).submit_grid_sheet(claimed, references)
            except ProviderChannelUnavailableError as exc:
                if exc.submission is not None:
                    submission = exc.submission
                else:
                    logger.error(
                        "storyboard_provider_channel_unavailable "
                        "provider=%s project=%s group=%s failure=%s",
                        claimed.provider or "grsai",
                        claimed.project_id,
                        claimed.group_id,
                        exc.failure_code,
                    )
                    return self._auto_resubmit(
                        claimed,
                        failure_stage="submit",
                        failure_code=exc.failure_code,
                        retry_after_seconds=None,
                        exhausted_failure_code=exc.failure_code,
                        skip_channel=True,
                    )
            except ProviderSubmitRejectedError as exc:
                if exc.submission is not None:
                    submission = exc.submission
                else:
                    # 审核拒绝、明确参数错误和其他保守分类的 4xx 不换模型绕过。
                    return self._repo.save_generation(
                        replace(
                            claimed,
                            status=GenerationStatus.FAILED,
                            failure_stage="submit",
                            failure_code=exc.failure_code or _submit_failure_code(exc),
                        )
                    )
            except ProviderSubmitThrottledError as exc:
                if exc.submission is not None:
                    submission = exc.submission
                else:
                    return self._submit_retryable_failure(
                        claimed,
                        failure_code="provider_http_429",
                        retry_after_seconds=exc.retry_after_seconds,
                    )
            except ProviderSubmitServerError as exc:
                if exc.submission is None:
                    return self._submit_retryable_failure(
                        claimed,
                        failure_code="provider_http_5xx",
                        retry_after_seconds=exc.retry_after_seconds,
                    )
                # 已收到可信任务身份时只接管原任务，绝不因 5xx 再提交。
                submission = exc.submission
            except ProviderSubmitNotDeliveredError as exc:
                return self._submit_retryable_failure(
                    claimed,
                    failure_code=_submit_failure_code(exc),
                    retry_after_seconds=None,
                )
            except Exception as exc:
                # 读超时、连接状态不明或无效响应无法判断是否送达：保留 UNKNOWN 并按恢复规则处理。
                return self._save_unknown(
                    replace(
                        claimed,
                        status=GenerationStatus.REMOTE_STATE_UNKNOWN,
                        failure_stage="submit",
                        failure_code=type(exc).__name__,
                    )
                )
            return self._repo.save_generation(
                replace(
                    claimed,
                    status=GenerationStatus.SUBMITTED,
                    provider_task_id=submission.task_id,
                    provider_receipt=submission.receipt,
                    submission_receipt=submission.receipt,
                )
            )
        now = self._clock.now()
        claimed_processing = self._repo.claim_processing(
            owner_id,
            generation_id,
            generation.state_revision,
            self._ids.new(),
            now,
            now + timedelta(minutes=30),
        )
        if claimed_processing is None:
            return self._repo.get_generation(owner_id, generation_id) or generation
        generation = claimed_processing
        if (
            generation.status
            in {GenerationStatus.SUBMITTING, GenerationStatus.REMOTE_STATE_UNKNOWN}
            and generation.provider_task_id is None
        ):
            if generation.failure_stage == "submit" and generation.failure_code == "ConnectError":
                return self._submit_retryable_failure(
                    generation,
                    failure_code="ConnectError",
                    retry_after_seconds=None,
                )
            try:
                reconciled = self._provider_for(generation).reconcile(
                    generation.provider_idempotency_key
                )
            except ProviderReconcileUnsupportedError as exc:
                if (
                    generation.status is GenerationStatus.REMOTE_STATE_UNKNOWN
                    and self._auto_production(generation.owner_id, generation.project_id)
                ):
                    return self._auto_resubmit(
                        generation,
                        failure_stage="reconcile",
                        failure_code=type(exc).__name__,
                        retry_after_seconds=None,
                        exhausted_failure_code=STORYBOARD_UNKNOWN_BUDGET_EXHAUSTED_FAILURE,
                    )
                return self._save_unknown(
                    replace(
                        generation,
                        status=GenerationStatus.REMOTE_STATE_UNKNOWN,
                        failure_stage="reconcile",
                        failure_code=type(exc).__name__,
                    )
                )
            except Exception as exc:
                return self._unknown_query_failure(
                    generation, failure_stage="reconcile", failure_code=type(exc).__name__
                )
            if reconciled is None:
                return self._unknown_query_failure(
                    generation,
                    failure_stage="reconcile",
                    failure_code=STORYBOARD_RECONCILE_NO_REMOTE_TASK_FAILURE,
                )
            generation = self._repo.save_generation(
                replace(
                    generation,
                    status=GenerationStatus.SUBMITTED,
                    provider_task_id=reconciled.task_id,
                    provider_receipt=reconciled.receipt,
                    failure_stage=None,
                    failure_code=None,
                    recovery_failure_count=0,
                )
            )
            return generation
        if generation.status in {
            GenerationStatus.SUBMITTED,
            GenerationStatus.RUNNING,
            GenerationStatus.REMOTE_STATE_UNKNOWN,
        }:
            if generation.provider_task_id is None:
                return generation
            try:
                poll = self._provider_for(generation).poll(generation.provider_task_id)
            except Exception as exc:
                return self._unknown_query_failure(
                    generation, failure_stage="poll", failure_code=type(exc).__name__
                )
            if poll.status is not GenerationStatus.SUCCEEDED:
                updated = replace(
                    generation,
                    status=poll.status,
                    provider_receipt=poll.receipt,
                    failure_stage="provider" if poll.status is GenerationStatus.FAILED else None,
                    failure_code=poll.failure_code,
                    recovery_failure_count=0,
                )
                if poll.status is GenerationStatus.FAILED and _provider_failure_may_failover(
                    poll.failure_code
                ):
                    return self._auto_resubmit(
                        updated,
                        failure_stage="provider",
                        failure_code=poll.failure_code or "provider_failed",
                        retry_after_seconds=None,
                        exhausted_failure_code=STORYBOARD_SUBMISSION_BUDGET_EXHAUSTED_FAILURE,
                    )
                return self._repo.save_generation(updated)
            if poll.download_token is None:
                raise DomainError(
                    "invalid_provider_result", "Successful provider result has no download token"
                )
            if poll.expected_sha256 is None:
                raise DomainError(
                    "invalid_provider_result", "Successful provider result has no content hash"
                )
            generation = self._repo.save_generation(
                replace(
                    generation,
                    status=GenerationStatus.DOWNLOADED,
                    provider_receipt=poll.receipt,
                    download_token=poll.download_token,
                    expected_sha256=poll.expected_sha256,
                    recovery_failure_count=0,
                )
            )
            if generation.status is not GenerationStatus.DOWNLOADED:
                return generation
            path = self._path(generation)
            try:
                self._download_atomic(generation, poll.download_token, path)
                digest, width, height, mime, size = self._qc.inspect(
                    path, generation.expected_sha256
                )
                self._check_panel_layout(generation, path)
            except StoryboardPanelLayoutError as exc:
                return self._retry_panel_layout_failure(generation, exc)
            except StoryboardAspectRatioError as exc:
                # 画幅不符是 Provider 单次输出波动（同模型同参数下其余组均正常），
                # 按可重试失败进入既有换路预算；预算耗尽才终态，避免整条流水线等人恢复。
                logger.warning(
                    "storyboard_aspect_ratio_rejected project=%s group=%s generation=%s reason=%s",
                    generation.project_id,
                    generation.group_id,
                    generation.generation_id,
                    exc,
                )
                return self._retry_aspect_ratio_failure(generation)
            except (OSError, ValueError) as exc:
                self._repo.save_generation(
                    replace(
                        generation,
                        failure_stage="download_or_qc",
                        failure_code=type(exc).__name__,
                    )
                )
                raise DomainError(
                    "storyboard_processing_failed", "Storyboard download or QC failed"
                ) from exc
            generation = self._repo.save_generation(
                replace(
                    generation,
                    status=GenerationStatus.QC_PASSED,
                    downloaded_sha256=digest,
                    downloaded_size=size,
                    qc_width=width,
                    qc_height=height,
                    qc_mime=mime,
                    failure_stage=None,
                    failure_code=None,
                )
            )
            if generation.status is not GenerationStatus.QC_PASSED:
                return generation
        if generation.status is GenerationStatus.DOWNLOADED:
            path = self._path(generation)
            try:
                if not path.exists() or generation.failure_stage == "download_or_qc":
                    if generation.download_token is None:
                        raise DomainError("invalid_checkpoint", "Download checkpoint is incomplete")
                    self._download_atomic(generation, generation.download_token, path)
                digest, width, height, mime, size = self._qc.inspect(
                    path, generation.expected_sha256
                )
                self._check_panel_layout(generation, path)
            except StoryboardPanelLayoutError as exc:
                return self._retry_panel_layout_failure(generation, exc)
            except StoryboardAspectRatioError as exc:
                # 画幅不符是 Provider 单次输出波动（同模型同参数下其余组均正常），
                # 按可重试失败进入既有换路预算；预算耗尽才终态，避免整条流水线等人恢复。
                logger.warning(
                    "storyboard_aspect_ratio_rejected project=%s group=%s generation=%s reason=%s",
                    generation.project_id,
                    generation.group_id,
                    generation.generation_id,
                    exc,
                )
                return self._retry_aspect_ratio_failure(generation)
            except (OSError, ValueError) as exc:
                self._repo.save_generation(
                    replace(
                        generation,
                        failure_stage="download_or_qc",
                        failure_code=type(exc).__name__,
                    )
                )
                raise DomainError(
                    "storyboard_processing_failed", "Storyboard download or QC failed"
                ) from exc
            generation = self._repo.save_generation(
                replace(
                    generation,
                    status=GenerationStatus.QC_PASSED,
                    downloaded_sha256=digest,
                    downloaded_size=size,
                    qc_width=width,
                    qc_height=height,
                    qc_mime=mime,
                    failure_stage=None,
                    failure_code=None,
                )
            )
            if generation.status is not GenerationStatus.QC_PASSED:
                return generation
        if generation.status is GenerationStatus.QC_PASSED:
            if generation.downloaded_sha256 is None or generation.qc_mime is None:
                raise DomainError("invalid_checkpoint", "QC checkpoint is incomplete")
            try:
                media = self._storage.put_verified(
                    owner_id=owner_id,
                    project_id=generation.project_id,
                    source=self._path(generation),
                    media_type=generation.qc_mime,
                    expected_sha256=generation.downloaded_sha256,
                )
            except (OSError, ValueError) as exc:
                self._repo.save_generation(
                    replace(
                        generation,
                        failure_stage="object_write",
                        failure_code=type(exc).__name__,
                    )
                )
                raise DomainError(
                    "storyboard_processing_failed", "Storyboard object storage failed"
                ) from exc
            if (
                media.sha256 != generation.downloaded_sha256
                or media.media_type != generation.qc_mime
                or media.size_bytes != generation.downloaded_size
            ):
                raise DomainError(
                    "storage_integrity_error", "Stored storyboard metadata does not match QC"
                )
            generation = self._repo.save_generation(
                replace(
                    generation,
                    status=GenerationStatus.OBJECT_STORED,
                    object_asset=media,
                    failure_stage=None,
                    failure_code=None,
                )
            )
            if generation.status is not GenerationStatus.OBJECT_STORED:
                return generation
        if generation.status is GenerationStatus.OBJECT_STORED:
            if (
                generation.object_asset is None
                or generation.qc_width is None
                or generation.qc_height is None
                or generation.qc_mime is None
            ):
                raise DomainError("invalid_checkpoint", "Object checkpoint is incomplete")
            asset = StoryboardAsset(
                self._ids.new(),
                generation.generation_id,
                StoryboardAssetKind.GRID_SHEET,
                generation.object_asset,
                generation.qc_width,
                generation.qc_height,
                generation.qc_mime.removeprefix("image/").upper(),
            )
            completed = self._repo.complete_and_switch_current(
                replace(generation, status=GenerationStatus.SUCCEEDED, asset=asset)
            )
            self._cleanup_temp(completed)
            return completed
        return generation

    def request_recovery(self, owner_id: UUID, generation_id: UUID) -> StoryboardGeneration:
        return self._repo.authorize_generation_recovery(
            owner_id, generation_id, self._ids.new(), self._clock.now()
        )

    def _provider_for(self, generation: StoryboardGeneration) -> GridSheetImageProvider:
        if isinstance(self._provider, Mapping):
            # 0036 前历史记录没有 Provider 列；§6.9 已明确历史故事板均为 grsai。
            key = generation.provider or "grsai"
            provider = self._provider.get(key)
            if provider is None:
                raise DomainError(
                    "provider_not_configured", f"Storyboard provider is not configured: {key}"
                )
            return provider
        return self._provider

    def _auto_production(self, owner_id: UUID, project_id: UUID) -> bool:
        # 自动对账事件仍按 AUTO 规则登记；项目缺失时按 MANUAL 保守处理。
        return self._repo.project_production_mode(owner_id, project_id) == "AUTO"

    def _save_unknown(self, generation: StoryboardGeneration) -> StoryboardGeneration:
        # §15.4（仅 AUTO）：落未知态的同事务登记自动对账事件（GridSheetRecoveryWorkflow）；
        # MANUAL 项目维持人工对账入口，行为不变。
        generation = replace(
            generation,
            unknown_origin_stage=generation.unknown_origin_stage or generation.failure_stage,
            unknown_origin_code=generation.unknown_origin_code or generation.failure_code,
        )
        if self._auto_production(generation.owner_id, generation.project_id):
            return self._repo.save_generation_and_request_recovery(
                generation, self._ids.new(), self._clock.now()
            )
        return self._repo.save_generation(generation)

    def _unknown_query_failure(
        self,
        generation: StoryboardGeneration,
        *,
        failure_stage: str,
        failure_code: str,
    ) -> StoryboardGeneration:
        if not self._auto_production(generation.owner_id, generation.project_id):
            return self._save_unknown(
                replace(
                    generation,
                    status=GenerationStatus.REMOTE_STATE_UNKNOWN,
                    failure_stage=failure_stage,
                    failure_code=failure_code,
                )
            )
        failure_count = generation.recovery_failure_count + 1
        updated = replace(
            generation,
            status=GenerationStatus.REMOTE_STATE_UNKNOWN,
            failure_stage=failure_stage,
            failure_code=failure_code,
            recovery_failure_count=failure_count,
        )
        if failure_count >= 3:
            return self._auto_resubmit(
                updated,
                failure_stage=failure_stage,
                failure_code=failure_code,
                retry_after_seconds=storyboard_auto_retry_delay_seconds(failure_count),
                exhausted_failure_code=STORYBOARD_UNKNOWN_BUDGET_EXHAUSTED_FAILURE,
            )
        return self._save_unknown(updated)

    def _submit_retryable_failure(
        self,
        failed: StoryboardGeneration,
        *,
        failure_code: str,
        retry_after_seconds: float | None,
    ) -> StoryboardGeneration:
        # AUTO 与手动故事板共用同一用户动作授权下的故障转移链。
        return self._auto_resubmit(
            failed,
            failure_stage="submit",
            failure_code=failure_code,
            retry_after_seconds=retry_after_seconds,
            exhausted_failure_code=STORYBOARD_SUBMISSION_BUDGET_EXHAUSTED_FAILURE,
        )

    def _grid_layout_version(self, owner_id: UUID, project_id: UUID) -> str:
        """§46 钉版：沿用项目最早一次生成的版式；首张故事板按开关选 v2 或 v1。"""
        pinned = self._repo.project_grid_layout_version(owner_id, project_id)
        if pinned is not None:
            return pinned
        return GRID_LAYOUT_V2 if self._single_panel_new_projects else GRID_LAYOUT_V1

    def _check_panel_layout(self, generation: StoryboardGeneration, path: Path) -> None:
        """v2 故事板必须能被逐格裁切；v1 不检查（存量行为不变）。"""
        if generation.grid_layout_version != GRID_LAYOUT_V2:
            return
        if self._panel_layout_check is None:
            raise DomainError(
                "storyboard_panel_layout_check_not_configured",
                "Grid layout v2 requires the panel layout check",
            )
        try:
            self._panel_layout_check(
                path,
                panel_count=len(generation.shots_snapshot),
                aspect_ratio="16:9",
                layout_version=generation.grid_layout_version,
            )
        except ValueError as exc:
            raise StoryboardPanelLayoutError(str(exc)) from exc

    def _retry_panel_layout_failure(
        self, failed: StoryboardGeneration, exc: StoryboardPanelLayoutError
    ) -> StoryboardGeneration:
        """§46：不可裁切的整图与画幅不符同等处理——进入既有故障转移链（至多 14 次）。"""
        logger.warning(
            "storyboard_panel_layout_rejected project=%s group=%s generation=%s reason=%s",
            failed.project_id,
            failed.group_id,
            failed.generation_id,
            exc,
        )
        return self._auto_resubmit(
            failed,
            failure_stage="download_or_qc",
            failure_code=STORYBOARD_PANEL_LAYOUT_UNREADABLE,
            retry_after_seconds=None,
            exhausted_failure_code=STORYBOARD_PANEL_LAYOUT_UNREADABLE,
        )

    def _retry_aspect_ratio_failure(self, failed: StoryboardGeneration) -> StoryboardGeneration:
        """画幅不符 → 进入既有故障转移链（同模型 2 次、逐路由，至多 14 次）。

        终态原因码保持 `aspect_ratio_not_supported`，预算耗尽后 AUTO 仍按原语义暂停，
        但单次抖动不再直接停线。
        """
        return self._auto_resubmit(
            failed,
            failure_stage="download_or_qc",
            failure_code="aspect_ratio_not_supported",
            retry_after_seconds=None,
            exhausted_failure_code="aspect_ratio_not_supported",
        )

    def _bind_failover_lineage(self, failed: StoryboardGeneration) -> StoryboardGeneration | None:
        if failed.failover_chain_id is not None:
            return failed
        # 0036 前的首个在途尝试可绑定到匹配模型；多步旧链不猜测预算或根链。
        if failed.auto_attempt != 1:
            return None
        provider = failed.provider or "grsai"
        for route_index, step in enumerate(self._policy.steps):
            if step.provider == provider and step.model == failed.model:
                return replace(
                    failed,
                    provider=provider,
                    failover_chain_id=failed.generation_id,
                    failover_policy_version=self._policy.version,
                    failover_route_index=route_index,
                    failover_model_attempt=1,
                    failover_total_attempt=1,
                )
        return None

    def _auto_resubmit(
        self,
        failed: StoryboardGeneration,
        *,
        failure_stage: str,
        failure_code: str,
        retry_after_seconds: float | None,
        exhausted_failure_code: str,
        skip_channel: bool = False,
    ) -> StoryboardGeneration:
        bound = self._bind_failover_lineage(failed)
        if bound is None:
            return self._repo.save_generation(
                replace(
                    failed,
                    status=GenerationStatus.FAILED,
                    failure_stage=failure_stage,
                    failure_code=failure_code,
                )
            )
        position = self._policy.next_position(
            route_index=int(bound.failover_route_index or 0),
            model_attempt=int(bound.failover_model_attempt or 0),
            total_attempt=int(bound.failover_total_attempt or 0),
            skip_channel=skip_channel,
        )
        if position is None:
            terminal_code = (
                exhausted_failure_code
                if int(bound.failover_total_attempt or 0) >= STORYBOARD_AUTO_MAX_SUBMISSIONS
                else failure_code
            )
            return self._repo.save_generation(
                replace(
                    bound,
                    status=GenerationStatus.FAILED,
                    failure_stage=failure_stage,
                    failure_code=terminal_code,
                )
            )
        now = self._clock.now()
        delay = storyboard_auto_retry_delay_seconds(bound.auto_attempt, retry_after_seconds)
        return self._repo.supersede_with_auto_retry(
            bound,
            failure_stage=failure_stage,
            failure_code=failure_code,
            successor=self._auto_successor(bound, position, now),
            next_attempt_at=now + timedelta(seconds=delay),
        )

    def _auto_successor(
        self,
        failed: StoryboardGeneration,
        position: StoryboardFailoverPosition,
        now: datetime,
    ) -> StoryboardGeneration:
        # 创意输入不变；Provider/模型/参数与每次真实请求哈希按新位置重算。
        key = f"auto-retry-{failed.generation_id}"
        parameters = dict(position.step.parameters)
        canonical = canonical_storyboard_request_sha256(
            owner_id=failed.owner_id,
            group_id=failed.group_id,
            revision_id=failed.applied_revision_id,
            prompt=failed.prompt,
            characters=[
                canonical_character_payload(
                    ordinal=item.ordinal,
                    character_image_id=item.character_image_id,
                    asset_id=item.asset_id,
                    sha256=item.sha256,
                    media_type=item.media_type,
                    size_bytes=item.size_bytes,
                )
                for item in failed.character_assets_snapshot
            ],
            style=failed.style_snapshot,
            model=position.step.model,
            parameters=parameters,
            provider=position.step.provider,
        )
        return replace(
            failed,
            generation_id=self._ids.new(),
            version=1,  # 仓储在同一事务内按组重算 version
            idempotency_key=key,
            provider_idempotency_key=hashlib.sha256(
                f"{failed.owner_id}:{key}".encode()
            ).hexdigest(),
            canonical_request_sha256=canonical,
            status=GenerationStatus.INTENT_PERSISTED,
            created_at=now,
            state_revision=0,
            provider=position.step.provider,
            model=position.step.model,
            parameters=parameters,
            provider_task_id=None,
            provider_receipt=None,
            submission_receipt=None,
            recovery_failure_count=0,
            unknown_origin_stage=None,
            unknown_origin_code=None,
            download_token=None,
            expected_sha256=None,
            downloaded_sha256=None,
            downloaded_size=None,
            qc_width=None,
            qc_height=None,
            qc_mime=None,
            object_asset=None,
            failure_stage=None,
            failure_code=None,
            asset=None,
            execution_token=None,
            lease_until=None,
            auto_attempt=position.total_attempt,
            supersedes_generation_id=failed.generation_id,
            failover_route_index=position.route_index,
            failover_model_attempt=position.model_attempt,
            failover_total_attempt=position.total_attempt,
        )

    def status(self, generation_id: UUID, owner_id: UUID) -> StoryboardGeneration:
        value = self._repo.get_generation(owner_id, generation_id)
        if value is None:
            raise _not_found()
        return value

    def release(self, generation: StoryboardGeneration) -> None:
        if generation.execution_token is not None:
            self._repo.release_processing(
                generation.owner_id, generation.generation_id, generation.execution_token
            )

    def current(self, owner_id: UUID, group_id: UUID) -> StoryboardGeneration:
        generation = self._repo.get_current_generation(owner_id, group_id)
        if generation is None:
            raise _not_found()
        return generation

    def history(self, owner_id: UUID, group_id: UUID) -> tuple[StoryboardGeneration, ...]:
        if self._repo.context(owner_id, group_id) is None:
            raise _not_found()
        return self._repo.list_generation_history(owner_id, group_id)

    def select_current(
        self, owner_id: UUID, group_id: UUID, generation_id: UUID
    ) -> StoryboardGeneration:
        return self._repo.select_current_generation(owner_id, group_id, generation_id)

    def detail(self, owner_id: UUID, generation_id: UUID) -> StoryboardGeneration:
        value = self._repo.get_generation(owner_id, generation_id)
        if value is None:
            raise _not_found()
        return value

    def image_bytes(self, owner_id: UUID, generation_id: UUID) -> tuple[bytes, str, str]:
        value = self.detail(owner_id, generation_id)
        if value.status is not GenerationStatus.SUCCEEDED or value.asset is None:
            raise _not_found()
        payload = self._storage.read_verified(value.asset.media)
        if hashlib.sha256(payload).hexdigest() != value.asset.media.sha256:
            raise DomainError("storage_integrity_error", "Storyboard image hash changed")
        return payload, value.asset.media.media_type, value.asset.media.sha256

    def _path(self, generation: StoryboardGeneration) -> Path:
        return (
            self._work_dir / str(generation.owner_id) / str(generation.generation_id) / "grid.image"
        )

    def _download_atomic(
        self, generation: StoryboardGeneration, token: str, destination: Path
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(f"{destination.name}.{self._ids.new()}.part")
        try:
            self._provider_for(generation).download(token, part)
            os.replace(part, destination)
        finally:
            part.unlink(missing_ok=True)

    def _cleanup_temp(self, generation: StoryboardGeneration) -> None:
        path = self._path(generation)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
            path.parent.parent.rmdir()
        except OSError:
            pass

    def _shot_time_ranges(
        self, revision: DirectorPlanRevision, context: StoryboardContext
    ) -> tuple[ShotTimeRange, ...]:
        if not context.sentences:
            raise DomainError("narration_not_ready", "Real narration gate has not passed")
        try:
            # 2026-10-16：新生成一律用 monotone-projection-v1 投影；算法键随生成输入
            # 持久化（create 写入 shot_time_ranges_algorithm），门禁按键双算法自洽。
            return projected_shot_time_ranges_ms(
                revision.shots,
                revision.sentence_links,
                {item.sentence_id: (item.start_ms, item.end_ms) for item in context.sentences},
            )
        except ShotTimingProjectionError as exc:
            # MANUAL 草稿链接形态无解时不做自动合并（自动合并只在重映射物化阶段
            # 发生），以稳定码阻止生成、由人工调整链接后重试。
            raise DomainError(
                "shot_timing_unresolvable",
                "Shot timing cannot be projected; adjust the director draft links",
            ) from exc
        except ValueError as exc:
            raise DomainError("narration_not_ready", "Measured timeline is incomplete") from exc

    def _canonical_hash(
        self,
        owner_id: UUID,
        group_id: UUID,
        revision: DirectorPlanRevision,
        prompt: str,
        context: StoryboardContext,
        parameters: dict[str, str | int | float | bool],
        *,
        provider: str,
        model: str,
    ) -> str:
        return canonical_storyboard_request_sha256(
            owner_id=owner_id,
            group_id=group_id,
            revision_id=revision.revision_id,
            prompt=prompt,
            characters=[
                canonical_character_payload(
                    ordinal=item.ordinal,
                    character_image_id=item.character_image_id,
                    asset_id=item.asset_id,
                    sha256=item.sha256,
                    media_type=item.media_type,
                    size_bytes=item.size_bytes,
                )
                for item in context.character_assets
            ],
            style=context.style,
            model=model,
            parameters=parameters,
            provider=provider,
        )
