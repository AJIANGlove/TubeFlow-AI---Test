from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from fractions import Fraction
from typing import Protocol
from uuid import UUID, uuid4

from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    SHOT_TIMING_PROJECTION_V1,
    DirectorLocationRelation,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
    ShotSentenceLink,
    ShotTimeRange,
    shot_time_ranges_ms,
)
from tubeflow_test.video_generation.content_qc import (
    DEFAULT_THRESHOLDS,
    BlackRegionFinding,
    ContentQcThresholds,
    FrameStats,
    find_sustained_black_regions,
)
from tubeflow_test.video_generation.domain import (
    GroupVideoGeneration,
    ImmutableWorkflowSnapshot,
    ReferencePolicy,
    RetryableVideoIntentFailure,
    ShotPanelImage,
    VideoAssetIntegrityError,
    VideoCapacityUnavailable,
    VideoRuleViolation,
    canonical_json,
    canonical_sha256,
)
from tubeflow_test.video_generation.engine import (
    EngineAttemptNotFound,
    EngineError,
    EngineGateway,
    EngineNode,
    EngineRejected,
    EngineUncertain,
    uses_durable_execution_contract,
)
from tubeflow_test.video_generation.gpu_probe import (
    GPU_PROBE_RECEIPT_SCHEMA,
    GpuProbeOutcome,
    GpuStatusProbe,
    UnavailableGpuStatusProbe,
)
from tubeflow_test.video_generation.native_size import (
    expected_native_size,
    workflow_identity,
)
from tubeflow_test.video_generation.provider_audit import (
    ProviderAuditEvent,
    canonical_redacted_receipt,
)
from tubeflow_test.video_generation.provider_audit import (
    canonical_hash as audit_hash,
)
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import (
    V3_CONTRACT_STRATEGIES,
    CompiledVideoPrompt,
    SinglePanelPromptCompilerInput,
    SinglePanelReference,
    VideoPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
)

logger = logging.getLogger(__name__)


class AttemptStage(StrEnum):
    CREATED = "CREATED"
    STAGING = "STAGING"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUBMITTING = "SUBMITTING"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class ArchiveStage(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    DOWNLOADED = "DOWNLOADED"
    QC_PASSED = "QC_PASSED"
    QC_FAILED = "QC_FAILED"
    STORED = "STORED"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    COMPLETE = "COMPLETE"


class RequestedAction(StrEnum):
    RECONCILE = "RECONCILE"
    CANCEL = "CANCEL"
    ARCHIVE = "ARCHIVE"


@dataclass(frozen=True, slots=True)
class StallPolicy:
    """远端状态零变化的停滞判定与降频轮询策略；秒数均可由部署配置覆盖。"""

    accepted_seconds: float = 1800.0
    running_seconds: float = 3600.0
    poll_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class AutoResubmitPolicy:
    """AUTO 项目的有界自动重提策略：每组提交预算与退避阶梯（秒）。

    2026-10-16 决策：QC 画面缺陷自动重试升级为梯子（换 seed → 带缺陷原因重编译 →
    换策略重编译），每组每预算轮提交预算由 3 上调为 6；该预算由全部 AUTO 重提原因
    （既定拒绝、未知态对账无果、停滞空闲、QC 失败）共享。
    """

    max_submissions: int = 6
    backoff_seconds: tuple[float, ...] = (5.0, 15.0, 30.0)


SUBMISSION_BUDGET_EXHAUSTED = "submission_budget_exhausted"
VIDEO_CONTENT_BLACK_REGION = "video_content_black_region"
VIDEO_CONTENT_QC_SAMPLER_ERROR = "video_content_qc_sampler_error"
QC_DEFECT_DELIVERED_EVENT = "QC_DEFECT_DELIVERED"
# 带病交付血缘标记：合成装载、组视频视图与 workflow_summary 缺陷透出共用同一常量。
DELIVER_WITH_DEFECT_MARKER = f"deliver_with_defect:{VIDEO_CONTENT_BLACK_REGION}"
_LADDER_STRATEGY_STEP = 4
_LADDER_STRATEGY_STEP_STRATEGY = "kinestack_v6_port"
_LADDER_RECOMPILE_STEPS = frozenset({3, 4, 6})
_LADDER_REPAIR_REASON_STEPS = frozenset({3, 6})
_AUTO_INCONCLUSIVE_RECONCILE_LIMIT = 3
_AUTO_CANCEL_STALLED_REASON = "stalled_node_idle"
_AUTO_CANCEL_STALLED_MARKER = f"auto_cancel:{_AUTO_CANCEL_STALLED_REASON}"
_AUTO_RESUBMIT_STALLED_MARKER = f"auto_resubmit:{_AUTO_CANCEL_STALLED_REASON}"


@dataclass(frozen=True, slots=True)
class AuthoritativeGroupInput:
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    generation_id: UUID
    snapshot_frozen: bool
    script_text_sha256: str
    sentence_text_sha256: tuple[str, ...]
    narrated_text_sha256: tuple[str, ...]
    sentence_durations_ms: tuple[int, ...]
    narration_qc_passed: tuple[bool, ...]
    normalized_audio_assets: tuple[MediaAssetRef | None, ...]
    timeline_ranges_ms: tuple[tuple[int, int], ...]
    group_sentence_indexes: tuple[int, ...]
    group_start_ms: int
    group_end_ms: int
    group_target_duration_ms: int
    character_images: tuple[MediaAssetRef, ...]
    storyboard_image: MediaAssetRef
    storyboard_kind: str
    storyboard_is_current: bool
    applied_revision_id: UUID
    storyboard_applied_revision_id: UUID
    prompt: str
    aspect_ratio: AspectRatio
    seed: int | str | None
    workflow: ImmutableWorkflowSnapshot
    immutable_script: str | None = None
    authoritative_group_sentence_indexes: tuple[int, ...] | None = None
    storyboard_version: int = 1
    raw_audio_assets: tuple[MediaAssetRef | None, ...] = ()
    storyboard_generation_id: UUID | None = None
    storyboard_asset_sha256: str | None = None
    sentence_sample_counts: tuple[int, ...] = ()
    sentence_sample_rates: tuple[int, ...] = ()
    timeline_ranges_frames: tuple[tuple[int, int], ...] = ()
    group_start_frame: int | None = None
    group_end_frame: int | None = None
    group_sample_rate: int | None = None
    authority_snapshot: dict[str, object] | None = None
    director_shots: tuple[DirectorShot, ...] = ()
    shot_sentence_links: tuple[ShotSentenceLink, ...] = ()
    group_sentence_ids: tuple[UUID, ...] = ()
    visual_style: str = ""
    production_mode: str = "MANUAL"
    # 2026-10-16（monotone-projection-v1）：故事板生成记录持久化的组级镜头区间与
    # 计时算法键。算法键为 monotone-projection-v1 时编译侧直接使用该持久化区间
    # （H3 门禁已校验其与实测时间轴自洽，与故事板角标同源同一份）；空 = 历史
    # 记录，编译侧按 legacy min/max 重算（行为不变）。
    storyboard_shot_ranges: tuple[ShotTimeRange, ...] = ()
    shot_time_ranges_algorithm: str = ""
    # 隔离重放的可选逐格参考；生产权威加载器未启用时保持空组，原行为不变。
    shot_panel_images: tuple[ShotPanelImage, ...] = ()
    reference_policy: ReferencePolicy | None = None
    reference_input_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PersistentAttempt:
    attempt_id: UUID
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    generation_id: UUID
    idempotency_key: str
    generation_sha256: str
    request_sha256: str
    request: dict[str, object]
    stage: AttemptStage = AttemptStage.CREATED
    node_id: str | None = None
    remote_id: str | None = None
    staged_ids: tuple[str, ...] = ()
    cleaned_staged_ids: tuple[str, ...] = ()
    archive_stage: ArchiveStage = ArchiveStage.NOT_STARTED
    output_id: str | None = None
    output_sha256: str | None = None
    archived_asset: MediaAssetRef | None = None
    error_code: str | None = None
    retry_evidence: tuple[str, ...] = ()
    slot_claimed: bool = False
    input_snapshot: dict[str, object] | None = None
    probe_evidence: dict[str, object] | None = None
    output_size_bytes: int | None = None
    output_media_type: str | None = None
    revision: int = 1
    remote_phase: str | None = None
    raw_receipt: dict[str, object] | None = None
    requested_action: RequestedAction | None = None
    cleanup_terminal_stage: AttemptStage | None = None
    is_current: bool = False
    remote_acceptance: str | None = None
    slot_lease_token: str | None = None
    slot_lease_owner: str | None = None
    slot_lease_expires_at: str | None = None
    narration_validation: dict[str, object] | None = None
    intent_sequence: int = 0
    cancel_dispatched: bool = False
    cancel_idempotency_key: str | None = None
    archive_confirm_dispatched: bool = False
    archive_confirm_idempotency_key: str | None = None
    archive_confirm_inspect_count: int = 0
    cancel_inspect_count: int = 0
    remote_phase_changed_at: str | None = None
    stalled_at: str | None = None
    superseded_attempt_id: UUID | None = None
    # §15.9：提交预算按项目预算轮次计数；恢复后新轮次重新计 3 次，历史尝试保留审计。
    budget_round: int = 1


class VideoRepository(Protocol):
    def append_provider_event(self, event: ProviderAuditEvent) -> None: ...
    def load_authoritative_group(
        self, owner_id: UUID, group_id: UUID
    ) -> AuthoritativeGroupInput: ...
    def find_attempt(self, owner_id: UUID, idempotency_key: str) -> PersistentAttempt | None: ...
    def find_unresolved_group(self, owner_id: UUID, group_id: UUID) -> PersistentAttempt | None: ...
    def assert_no_other_unresolved(self, attempt: PersistentAttempt) -> None: ...
    def commit_submitting(self, attempt: PersistentAttempt) -> PersistentAttempt: ...
    def get_attempt(self, attempt_id: UUID) -> PersistentAttempt | None: ...
    def create_attempt(self, attempt: PersistentAttempt) -> PersistentAttempt: ...
    def count_group_attempts(
        self, owner_id: UUID, group_id: UUID, budget_round: int | None = None
    ) -> int:
        """该组提交尝试计数；给定 budget_round 时只计该授权轮次（§15.9）。"""
        ...
    def project_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        """当前预算轮次（projects.auto_budget_round）；MANUAL 恒为 1。"""
        ...
    def supersede_attempt(
        self,
        superseded: PersistentAttempt,
        replacement: PersistentAttempt,
        event: ProviderAuditEvent,
        *,
        delay_seconds: float,
        max_submissions: int,
    ) -> tuple[PersistentAttempt, PersistentAttempt]: ...
    def save_attempt(self, attempt: PersistentAttempt) -> PersistentAttempt: ...
    def save_attempt_with_event(
        self, attempt: PersistentAttempt, event: ProviderAuditEvent
    ) -> PersistentAttempt: ...
    def claim_compatible_node(
        self, attempt_id: UUID, workflow: ImmutableWorkflowSnapshot
    ) -> EngineNode: ...
    def get_assigned_node(
        self, attempt_id: UUID, workflow: ImmutableWorkflowSnapshot
    ) -> EngineNode: ...
    def release_node(self, attempt_id: UUID, lease_token: str) -> None: ...
    def renew_slot(self, attempt_id: UUID, lease_token: str) -> None: ...
    def rotate_slot_lease(
        self, attempt_id: UUID, lease_token: str, lease_owner: str
    ) -> PersistentAttempt: ...
    def is_current(self, group_id: UUID, attempt_id: UUID) -> bool: ...
    def mark_current(self, attempt: PersistentAttempt) -> PersistentAttempt: ...
    def mark_current_if_absent(self, attempt: PersistentAttempt) -> bool: ...
    def select_current(
        self, owner_id: UUID, group_id: UUID, attempt_id: UUID
    ) -> PersistentAttempt: ...
    def get_current(self, owner_id: UUID, group_id: UUID) -> PersistentAttempt | None: ...
    def list_history(self, owner_id: UUID, group_id: UUID) -> tuple[PersistentAttempt, ...]: ...
    def sweep_expired_slots(self) -> int: ...


class AssetStore(Protocol):
    def read_verified(self, asset: MediaAssetRef) -> bytes: ...
    def archive_immutable(
        self,
        *,
        attempt_id: UUID,
        generation_id: UUID,
        output_id: str,
        content: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef: ...


@dataclass(frozen=True, slots=True)
class VideoMediaEvidence:
    media_type: str
    width: int
    height: int
    fps: float
    duration_ms: int
    video_tracks: int
    audio_tracks: int
    container_names: tuple[str, ...] = ()
    video_duration_ms: int | None = None
    video_frame_count: int | None = None
    audio_start_ms: int | None = None
    audio_duration_ms: int | None = None
    audio_end_ms: int | None = None
    audio_codec: str | None = None


class VideoProbe(Protocol):
    def inspect(self, content: bytes) -> VideoMediaEvidence: ...


class ContentFrameSampler(Protocol):
    """内容级 QC 抽帧器端口：从已归档视频字节抽取帧网格亮度统计。"""

    def sample(
        self, content: bytes, *, thresholds: ContentQcThresholds = DEFAULT_THRESHOLDS
    ) -> tuple[FrameStats, ...]: ...


class _ContentQcFailure(Exception):
    """内容级 QC 失败信号：携带稳定错误码与确定性证据块（不是领域违例）。"""

    def __init__(self, code: str, message: str, content_qc: dict[str, object]) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.content_qc = content_qc


_DETERMINISTIC_PRE_PROVIDER_ERRORS = {
    "complete_real_narration_required",
    "raw_and_normalized_audio_required",
    "narration_audio_bytes_changed",
    "persisted_provider_request_hash_mismatch",
    "immutable_video_input_changed",
}

# C3：AUTO 意图阶段可重试的稳定码（提示词编译输出不合格 / provider 暂不可用）。
# 其余意图阶段失败（未知远端状态、幂等/哈希不一致、输入前提不满足）不得盲重试。
_RETRYABLE_INTENT_FAILURE_CODES = frozenset(
    {
        "video_prompt_compilation_failed",
        "video_prompt_compilation_unavailable",
    }
)

# 尺寸失配的稳定规则码（历史统计按该文案精确匹配，勿改文案）。
_RESOLUTION_MISMATCH_RULE = "H3 output resolution does not match project aspect ratio"


def validate_attempt_transition(old: PersistentAttempt, new: PersistentAttempt) -> None:
    allowed: dict[AttemptStage, set[AttemptStage]] = {
        AttemptStage.CREATED: {
            AttemptStage.STAGING,
            AttemptStage.CANCELLED,
            AttemptStage.FAILED,
        },
        AttemptStage.STAGING: {
            AttemptStage.STAGING,
            AttemptStage.NOT_SUBMITTED,
            AttemptStage.SUBMITTING,
            AttemptStage.FAILED,
        },
        AttemptStage.NOT_SUBMITTED: {
            AttemptStage.STAGING,
            AttemptStage.CANCELLED,
            AttemptStage.FAILED,
        },
        AttemptStage.SUBMITTING: {
            AttemptStage.SUBMITTED,
            AttemptStage.RUNNING,
            AttemptStage.REMOTE_STATE_UNKNOWN,
            AttemptStage.FAILED,
        },
        AttemptStage.REMOTE_STATE_UNKNOWN: {
            AttemptStage.SUBMITTED,
            AttemptStage.RUNNING,
            AttemptStage.SUCCEEDED,
            AttemptStage.FAILED,
            AttemptStage.CANCELLED,
        },
        AttemptStage.SUBMITTED: {
            AttemptStage.REMOTE_STATE_UNKNOWN,
            AttemptStage.RUNNING,
            AttemptStage.SUCCEEDED,
            AttemptStage.FAILED,
            AttemptStage.CANCELLED,
        },
        AttemptStage.RUNNING: {AttemptStage.SUCCEEDED, AttemptStage.FAILED, AttemptStage.CANCELLED},
        AttemptStage.SUCCEEDED: {AttemptStage.ARCHIVED},
        AttemptStage.FAILED: set(),
        AttemptStage.CANCELLED: set(),
        AttemptStage.ARCHIVED: set(),
    }
    if _durable_candidate(old):
        allowed[AttemptStage.RUNNING].add(AttemptStage.REMOTE_STATE_UNKNOWN)
    if old.stage != new.stage and new.stage not in allowed[old.stage]:
        raise VideoRuleViolation("attempt_stage_conflict")
    archive_allowed: dict[ArchiveStage, set[ArchiveStage]] = {
        ArchiveStage.NOT_STARTED: {ArchiveStage.DOWNLOADED, ArchiveStage.CLEANUP_PENDING},
        ArchiveStage.DOWNLOADED: {ArchiveStage.STORED, ArchiveStage.CLEANUP_PENDING},
        ArchiveStage.STORED: {
            ArchiveStage.QC_PASSED,
            ArchiveStage.QC_FAILED,
            ArchiveStage.CLEANUP_PENDING,
        },
        ArchiveStage.QC_PASSED: {ArchiveStage.CONFIRMING, ArchiveStage.CLEANUP_PENDING},
        # 2026-10-16 决策：梯子耗尽的内容级缺陷（黑屏类）带病交付走 CONFIRMING 归档确认。
        ArchiveStage.QC_FAILED: {ArchiveStage.CONFIRMED, ArchiveStage.CONFIRMING},
        ArchiveStage.CONFIRMING: {ArchiveStage.CONFIRMED, ArchiveStage.CLEANUP_PENDING},
        ArchiveStage.CONFIRMED: {ArchiveStage.CLEANUP_PENDING},
        ArchiveStage.CLEANUP_PENDING: {ArchiveStage.NOT_STARTED, ArchiveStage.COMPLETE},
        ArchiveStage.COMPLETE: set(),
    }
    if (
        old.archive_stage != new.archive_stage
        and new.archive_stage not in archive_allowed[old.archive_stage]
    ):
        raise VideoRuleViolation("archive_stage_conflict")


class DurableGroupVideoOrchestrator:
    def __init__(
        self,
        repository: VideoRepository,
        engine: EngineGateway,
        assets: AssetStore,
        probe: VideoProbe,
        *,
        write_guard: Callable[[UUID, UUID], None] | None = None,
        prompt_compiler: VideoPromptCompiler | None = None,
        stall_policy: StallPolicy | None = None,
        auto_resubmit_policy: AutoResubmitPolicy | None = None,
        gpu_probe: GpuStatusProbe | None = None,
        content_sampler: ContentFrameSampler | None = None,
        content_qc_thresholds: ContentQcThresholds | None = None,
    ) -> None:
        self._repo, self._engine, self._assets, self._probe = repository, engine, assets, probe
        self._write_guard = write_guard
        self._prompt_compiler = prompt_compiler
        self._stall_policy = stall_policy or StallPolicy()
        self._auto_resubmit_policy = auto_resubmit_policy or AutoResubmitPolicy()
        self._gpu_probe: GpuStatusProbe = gpu_probe or UnavailableGpuStatusProbe()
        self._content_sampler = content_sampler
        self._content_qc_thresholds = content_qc_thresholds or DEFAULT_THRESHOLDS
        self._execution_owner = uuid4().hex

    def _event(
        self,
        attempt: PersistentAttempt,
        node: EngineNode,
        event_type: str,
        receipt: dict[str, object],
        *,
        request_sha256: str | None = None,
        provider_task_key: str | None = None,
        error_code: str | None = None,
    ) -> ProviderAuditEvent:
        safe = canonical_redacted_receipt(receipt)
        workflow = _workflow(attempt.request)
        raw_schema = safe.get("schema")
        schema_version = raw_schema[:512] if isinstance(raw_schema, str) else "kinestack.h3.v1"
        return ProviderAuditEvent(
            attempt_id=attempt.attempt_id,
            owner_id=attempt.owner_id,
            project_id=attempt.project_id,
            group_id=attempt.group_id,
            provider="kinestack",
            node_id=node.node_id,
            schema_version=schema_version,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.workflow_version,
            model_key=workflow.model_key,
            model_version=workflow.model_version,
            local_idempotency_key=attempt.idempotency_key,
            provider_task_key=provider_task_key or attempt.remote_id,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            request_sha256=request_sha256,
            receipt=safe,
            receipt_sha256=audit_hash(safe),
            error_code=error_code,
        )

    def _audit(self, *args: object, **kwargs: object) -> None:
        self._repo.append_provider_event(self._event(*args, **kwargs))  # type: ignore[arg-type]

    def _save_with_audit(
        self,
        attempt: PersistentAttempt,
        node: EngineNode,
        event_type: str,
        receipt: dict[str, object],
        *,
        request_sha256: str | None = None,
        provider_task_key: str | None = None,
        error_code: str | None = None,
    ) -> PersistentAttempt:
        return self._repo.save_attempt_with_event(
            attempt,
            self._event(
                attempt,
                node,
                event_type,
                receipt,
                request_sha256=request_sha256,
                provider_task_key=provider_task_key,
                error_code=error_code,
            ),
        )

    def trigger(self, owner_id: UUID, group_id: UUID, idempotency_key: str) -> PersistentAttempt:
        source = self._repo.load_authoritative_group(owner_id, group_id)
        if self._write_guard is not None:
            self._write_guard(owner_id, source.project_id)
        generation = _authorize(source, idempotency_key=idempotency_key)
        existing = self._repo.find_attempt(owner_id, idempotency_key)
        if existing is not None:
            # LLM 编译产物不确定，幂等比较只看确定性输入事实（video_prompt 块除外）。
            if canonical_sha256(existing.input_snapshot or {}) != existing.generation_sha256:
                raise VideoRuleViolation("persisted_provider_request_hash_mismatch")
            persisted = dict(existing.input_snapshot or {})
            persisted.pop("video_prompt", None)
            # 兼容旧键：seed 记录上线前创建的 attempt 快照 seed 为 null，比较时必须同样不派生，
            # 否则同一幂等键的合法重试会被误判为 idempotency_conflict（2026-11-03 用户决策 3）。
            comparison_generation = (
                _authorize(source) if persisted.get("seed") is None else generation
            )
            fresh = _input_snapshot(source, comparison_generation, compiled=None)
            if canonical_sha256(persisted) != canonical_sha256(fresh):
                raise VideoRuleViolation("idempotency_conflict")
            return existing
        unresolved = self._repo.find_unresolved_group(owner_id, group_id)
        if unresolved is not None:
            raise VideoRuleViolation("remote_acceptance_unknown")
        attempt_id = uuid4()
        try:
            compiled = compile_video_prompt(
                source,
                self._require_prompt_compiler(),
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
            )
        except DomainError as exc:
            # C3（2026-10-21）：AUTO 项目的意图阶段失败（提示词编译）不产生 attempt 行，
            # 因此不会进入每组 6 次提交预算；转成可重试标记交给 AUTO 编排按同一预算
            # 有界重试。MANUAL 项目与结构性输入错误维持原口径（直接失败）。
            if (
                source.production_mode == "AUTO"
                and exc.code in _RETRYABLE_INTENT_FAILURE_CODES
            ):
                raise RetryableVideoIntentFailure(exc.code, exc.message) from exc
            raise
        input_snapshot = _input_snapshot(source, generation, compiled=compiled)
        generation_sha = canonical_sha256(input_snapshot)
        request = _request(generation, attempt_id, prompt=compiled.text)
        try:
            attempt = self._repo.create_attempt(
                PersistentAttempt(
                    attempt_id,
                    owner_id,
                    source.project_id,
                    group_id,
                    source.generation_id,
                    idempotency_key,
                    generation_sha,
                    canonical_sha256(request),
                    request,
                    input_snapshot=input_snapshot,
                    # §15.9：AUTO 记录当前预算轮次；MANUAL 恒为 1（不额外读 projects）。
                    budget_round=(
                        self._repo.project_budget_round(owner_id, source.project_id)
                        if source.production_mode == "AUTO"
                        else 1
                    ),
                )
            )
        except VideoCapacityUnavailable as exc:
            # 2026-10-31：容量耗尽属瞬时事实，不是内容/规则失败。AUTO 交给编排
            # 记为「等待容量」并排队；MANUAL 维持「没有空闲节点」的同步报错。
            if source.production_mode == "AUTO":
                raise RetryableVideoIntentFailure(exc.code, exc.message) from exc
            raise VideoRuleViolation("no compatible H3 node has capacity") from exc
        return attempt

    def advance(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        """Worker-only transition; HTTP requests only persist and return an intent."""
        try:
            return self._advance_once(owner_id, attempt_id)
        except (VideoRuleViolation, VideoAssetIntegrityError) as exc:
            code = (
                "video_input_asset_integrity_failed"
                if isinstance(exc, VideoAssetIntegrityError)
                else str(exc)
            )
            if (
                isinstance(exc, VideoRuleViolation)
                and code not in _DETERMINISTIC_PRE_PROVIDER_ERRORS
            ):
                raise
            attempt = self._owned(attempt_id, owner_id)
            if attempt.stage not in {
                AttemptStage.CREATED,
                AttemptStage.STAGING,
                AttemptStage.NOT_SUBMITTED,
            }:
                blocked = replace(
                    attempt,
                    error_code=f"local_integrity_blocked:{code}",
                    retry_evidence=attempt.retry_evidence + (f"local_integrity_blocked:{code}",),
                    requested_action=None,
                )
                return self._repo.save_attempt(blocked)
            failed = replace(
                attempt,
                stage=AttemptStage.FAILED,
                error_code=code,
                retry_evidence=attempt.retry_evidence + (f"gate:{code}",),
                requested_action=None,
                archive_stage=(
                    ArchiveStage.CLEANUP_PENDING if attempt.staged_ids else ArchiveStage.NOT_STARTED
                ),
                cleanup_terminal_stage=(AttemptStage.FAILED if attempt.staged_ids else None),
            )
            failed = self._repo.save_attempt(failed)
            if not failed.staged_ids:
                self._release_slot(failed)
                failed = self._owned(attempt_id, owner_id)
            return failed

    def _advance_once(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        attempt = self._owned(attempt_id, owner_id)
        if attempt.slot_claimed:
            if attempt.slot_lease_token is None:
                raise VideoRuleViolation("slot_lease_missing")
            attempt = self._repo.rotate_slot_lease(
                attempt_id, attempt.slot_lease_token, self._execution_owner
            )
        if self._write_guard is not None:
            self._write_guard(owner_id, attempt.project_id)
        if attempt.archive_stage is ArchiveStage.CLEANUP_PENDING:
            if attempt.cleanup_terminal_stage is None and attempt.stage is AttemptStage.SUCCEEDED:
                return self.archive(owner_id, attempt_id)
            cleaned = self._cleanup_staged(attempt)
            if cleaned.stage is not AttemptStage.NOT_SUBMITTED:
                return cleaned
            attempt = cleaned
        if attempt.stage in {AttemptStage.FAILED, AttemptStage.CANCELLED} and attempt.staged_ids:
            # 终态遗留暂存资产的兜底清理出口（如人工终态覆盖产生的孤儿状态）：
            # 补齐清理意图后复用既有幂等清理流程；完成后 staged_ids 清空，
            # outbox 重驱动条件自然失效，避免终态尝试被永久重驱动。
            pending = self._save_with_audit(
                replace(
                    attempt,
                    archive_stage=ArchiveStage.CLEANUP_PENDING,
                    cleanup_terminal_stage=attempt.stage,
                ),
                self._assigned(attempt),
                "TERMINAL_LEFTOVER_CLEANUP",
                {
                    "schema": "kinestack.terminal-leftover-cleanup.v1",
                    "stage": attempt.stage.value,
                    "staged_count": len(attempt.staged_ids),
                },
            )
            return self._cleanup_staged(pending)
        if attempt.stage in {
            AttemptStage.ARCHIVED,
            AttemptStage.FAILED,
            AttemptStage.CANCELLED,
        }:
            attempt = self._auto_resubmit_after_cancel(owner_id, attempt)
            self._release_slot(attempt)
            return self._owned(attempt_id, owner_id)
        if attempt.requested_action is RequestedAction.CANCEL:
            return self._auto_resubmit_after_cancel(owner_id, self.cancel(owner_id, attempt_id))
        if attempt.requested_action is RequestedAction.ARCHIVE:
            return self.archive(owner_id, attempt_id)
        if attempt.requested_action is RequestedAction.RECONCILE:
            attempt = self._repo.save_attempt(replace(attempt, requested_action=None))
        if attempt.stage in {
            AttemptStage.CREATED,
            AttemptStage.NOT_SUBMITTED,
            AttemptStage.STAGING,
        }:
            self._verify_persisted_hashes(attempt)
            if attempt.input_snapshot is None:
                raise VideoRuleViolation("immutable_video_input_changed")
            return self._stage_and_submit(
                attempt, _generation_from_snapshot(attempt.input_snapshot)
            )
        if attempt.stage is AttemptStage.SUBMITTING:
            return self.resume(owner_id, attempt_id)
        if attempt.stage in {
            AttemptStage.REMOTE_STATE_UNKNOWN,
            AttemptStage.SUBMITTED,
            AttemptStage.RUNNING,
        }:
            return self.resume(owner_id, attempt_id)
        if attempt.stage is AttemptStage.SUCCEEDED:
            return self.archive(owner_id, attempt_id)
        return attempt

    def request_action(
        self, owner_id: UUID, attempt_id: UUID, action: RequestedAction
    ) -> PersistentAttempt:
        attempt = self._owned(attempt_id, owner_id)
        if action is RequestedAction.CANCEL and attempt.stage in {
            AttemptStage.SUCCEEDED,
            AttemptStage.ARCHIVED,
        }:
            raise VideoRuleViolation("successful video attempt cannot be cancelled")
        if attempt.stage in {AttemptStage.FAILED, AttemptStage.CANCELLED, AttemptStage.ARCHIVED}:
            return attempt
        return self._repo.save_attempt(replace(attempt, requested_action=action))

    def _claim_slot(self, attempt_id: UUID, workflow: ImmutableWorkflowSnapshot) -> EngineNode:
        """Worker 侧认领槽位：容量不足保持既有规则错误语义，由 Temporal 重试。

        意图创建（create）路径走的是「AUTO 排队等待」，这里不同：attempt 已建，
        只差槽位，交给既有活动重试策略处理，避免改变运行中任务的错误分类。
        """
        try:
            return self._repo.claim_compatible_node(attempt_id, workflow)
        except VideoCapacityUnavailable as exc:
            raise VideoRuleViolation("no compatible H3 node has capacity") from exc

    def resume(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        attempt = self._owned(attempt_id, owner_id)
        if attempt.stage in {
            AttemptStage.SUCCEEDED,
            AttemptStage.FAILED,
            AttemptStage.CANCELLED,
            AttemptStage.ARCHIVED,
        }:
            return attempt
        if attempt.node_id is None:
            raise VideoRuleViolation("attempt has no assigned node")
        workflow = _workflow(attempt.request)
        if not attempt.slot_claimed:
            self._claim_slot(attempt.attempt_id, workflow)
            attempt = self._owned(attempt_id, owner_id)
        node = self._repo.get_assigned_node(attempt.attempt_id, workflow)
        provider_task_id = _provider_task_key(attempt)
        try:
            result = self._engine.inspect(node, provider_task_id)
        except EngineAttemptNotFound:
            missing: dict[str, object] = {
                "schema": "kinestack.execution.v1",
                "task_id": _provider_task_key(attempt),
                "status": "NOT_FOUND",
            }
            if _durable_candidate(attempt) or attempt.stage in {
                AttemptStage.SUBMITTING,
                AttemptStage.REMOTE_STATE_UNKNOWN,
            }:
                if not _durable_candidate(attempt) and self._auto_mode(attempt):
                    # AUTO：404 是远端未受理的权威证据，直接自动重提（计入该组预算）。
                    return self._supersede_for_resubmit(
                        attempt,
                        node,
                        event_type="RESUBMIT_NOT_VISIBLE",
                        error_code="EngineAttemptNotFound",
                        reason="remote_not_visible",
                    )
                return self._save_with_audit(
                    replace(
                        attempt,
                        stage=AttemptStage.REMOTE_STATE_UNKNOWN,
                        remote_acceptance="UNKNOWN",
                        error_code="EngineAttemptNotFound",
                        retry_evidence=attempt.retry_evidence + ("reconcile:not_visible",),
                    ),
                    node,
                    "INSPECT_NOT_FOUND",
                    missing,
                    error_code="EngineAttemptNotFound",
                )
            raise
        except EngineError as exc:
            unresolved = replace(
                attempt,
                stage=(
                    AttemptStage.REMOTE_STATE_UNKNOWN
                    if _durable_candidate(attempt)
                    else attempt.stage
                ),
                remote_acceptance=(
                    "UNKNOWN" if _durable_candidate(attempt) else attempt.remote_acceptance
                ),
                retry_evidence=attempt.retry_evidence + (f"inspect:{type(exc).__name__}",),
                error_code=type(exc).__name__,
            )
            saved = self._save_with_audit(
                unresolved, node, "INSPECT_ERROR", {}, error_code=type(exc).__name__
            )
            if _durable_candidate(attempt) or attempt.stage is AttemptStage.REMOTE_STATE_UNKNOWN:
                if (
                    not _durable_candidate(saved)
                    and self._auto_mode(saved)
                    and _inconclusive_reconciles(saved) >= _AUTO_INCONCLUSIVE_RECONCILE_LIMIT
                ):
                    # AUTO：多轮对账无果，按未知态规则自动重提（计入该组预算）。
                    return self._supersede_for_resubmit(
                        saved,
                        node,
                        event_type="RESUBMIT_UNVERIFIABLE",
                        error_code=type(exc).__name__,
                        reason="remote_state_unverifiable",
                    )
                return saved
            raise
        try:
            _validate_task_receipt(result, attempt)
        except VideoRuleViolation:
            self._audit(
                attempt, node, "INSPECT_INVALID_RECEIPT", result, error_code="invalid_receipt"
            )
            raise
        state = str(result.get("status", "UNKNOWN"))
        acceptance = str(result.get("remote_acceptance", "")) or None
        mapped = (
            AttemptStage.REMOTE_STATE_UNKNOWN if acceptance == "UNKNOWN" else _remote_stage(state)
        )
        phase_changed_at, stalled_at = self._stall_observation(attempt, mapped, state)
        updated = replace(
            attempt,
            stage=mapped,
            remote_id=str(result.get("id", result.get("attempt_id", attempt.remote_id)))
            if result.get("id", result.get("attempt_id", attempt.remote_id))
            else None,
            remote_phase=state,
            remote_phase_changed_at=phase_changed_at,
            stalled_at=stalled_at,
            raw_receipt=canonical_redacted_receipt(result),
            remote_acceptance=acceptance,
            requested_action=(
                _pending_cancel_action(result, attempt)
                if attempt.requested_action is RequestedAction.CANCEL
                else None
            ),
            retry_evidence=attempt.retry_evidence
            + (f"poll:{state}",)
            + (
                ("reconcile:inconclusive",)
                if mapped is AttemptStage.REMOTE_STATE_UNKNOWN
                else ()
            ),
            archive_stage=(
                ArchiveStage.CLEANUP_PENDING
                if mapped in {AttemptStage.FAILED, AttemptStage.CANCELLED}
                else attempt.archive_stage
            ),
            cleanup_terminal_stage=(
                mapped
                if mapped in {AttemptStage.FAILED, AttemptStage.CANCELLED}
                else attempt.cleanup_terminal_stage
            ),
        )
        updated = self._save_with_audit(updated, node, "INSPECT_RECEIPT", result)
        if (
            mapped is AttemptStage.REMOTE_STATE_UNKNOWN
            and not _durable_candidate(updated)
            and self._auto_mode(updated)
            and _inconclusive_reconciles(updated) >= _AUTO_INCONCLUSIVE_RECONCILE_LIMIT
        ):
            # AUTO：远端持续报告未知，按未知态规则自动重提（计入该组预算）。
            return self._supersede_for_resubmit(
                updated,
                node,
                event_type="RESUBMIT_UNVERIFIABLE",
                error_code="remote_state_unverifiable",
                reason="remote_state_unverifiable",
            )
        if (
            updated.stalled_at is not None
            and mapped in {AttemptStage.SUBMITTED, AttemptStage.RUNNING}
            and self._auto_mode(updated)
        ):
            # AUTO：停滞降频轮询的每一轮对绑定节点探测一次 GPU 状态（MANUAL 维持等人工）。
            return self._stalled_gpu_decision(owner_id, updated, node)
        if mapped is AttemptStage.SUCCEEDED:
            self._release_slot(updated)
            updated = self._owned(attempt_id, owner_id)
        return updated

    def _stall_observation(
        self, attempt: PersistentAttempt, mapped: AttemptStage, state: str
    ) -> tuple[str | None, str | None]:
        """记录远端阶段计时；远端等待超时零变化时标记停滞（不改变任何远端行为）。"""
        now = datetime.now(UTC)
        remote_wait = mapped in {AttemptStage.SUBMITTED, AttemptStage.RUNNING}
        previous_stalled = _parse_dt(attempt.stalled_at)
        if attempt.remote_phase != state:
            phase_changed_at = now
            stalled_at: datetime | None = None
            if previous_stalled is not None:
                logger.info(
                    "h3 attempt %s left stalled state: remote phase %s -> %s",
                    attempt.attempt_id,
                    attempt.remote_phase,
                    state,
                )
        else:
            phase_changed_at = _parse_dt(attempt.remote_phase_changed_at) or now
            stalled_at = previous_stalled
        if not remote_wait:
            stalled_at = None
        elif stalled_at is None:
            threshold = (
                self._stall_policy.accepted_seconds
                if mapped is AttemptStage.SUBMITTED
                else self._stall_policy.running_seconds
            )
            if (now - phase_changed_at).total_seconds() >= threshold:
                stalled_at = now
                logger.warning(
                    "h3 attempt %s stalled: remote phase %s unchanged for over %ss",
                    attempt.attempt_id,
                    state,
                    threshold,
                )
        return (
            phase_changed_at.isoformat(),
            stalled_at.isoformat() if stalled_at is not None else None,
        )

    def _auto_mode(self, attempt: PersistentAttempt) -> bool:
        """AUTO 判定来自项目级不可变标记；MANUAL 项目保持人工对账合同不变。"""
        source = self._repo.load_authoritative_group(attempt.owner_id, attempt.group_id)
        return source.production_mode == "AUTO"

    def _auto_resubmit_delay(self, existing_count: int, retry_after_seconds: float | None) -> float:
        ladder = self._auto_resubmit_policy.backoff_seconds
        base = ladder[min(max(existing_count - 1, 0), len(ladder) - 1)]
        if retry_after_seconds is not None:
            return max(base, retry_after_seconds)
        return base

    def _build_auto_replacement(
        self, superseded: PersistentAttempt, *, next_attempt_index: int
    ) -> PersistentAttempt:
        """按升级梯确定性重建替代提交（2026-10-16 决策，预算内第 i 次提交，i 从 2 起）。

        i=2/5 仅换 seed；i=3/6 换 seed 并以 probe_evidence 的 content_qc 发现生成的
        repair_reason 重编译；i=4 换 seed 并以 kinestack_v6_port 策略重编译。
        重编译输入只从被取代尝试的持久化快照重建（不读库）；旧快照缺 compiler_input
        块、策略不可用或编译失败时退化为仅换 seed，retry_evidence 留证据标记。
        """
        snapshot = superseded.input_snapshot
        if not snapshot:
            raise VideoRuleViolation("immutable_video_input_changed")
        if snapshot.get("shot_panel_assets"):
            raise VideoRuleViolation("single_panel_automatic_resubmit_not_supported")
        index = next_attempt_index
        snapshot = dict(snapshot)
        snapshot["seed"] = _ladder_seed(snapshot.get("seed"), index, superseded)
        generation = _generation_from_snapshot(snapshot)
        snapshot["logical_request"] = _request(generation, None, prompt="")
        attempt_id = uuid4()
        markers: list[str] = [f"auto_ladder:attempt_{index}"]
        prompt = self._ladder_prompt(
            superseded, snapshot, generation, attempt_id=attempt_id, index=index, markers=markers
        )
        if prompt is None:
            reused = superseded.request.get("prompt")
            if not isinstance(reused, str) or not reused.strip():
                raise VideoRuleViolation("immutable_video_input_changed")
            prompt = reused
            markers.append("auto_ladder:seed_only")
        request = _request(generation, attempt_id, prompt=prompt)
        return PersistentAttempt(
            attempt_id,
            superseded.owner_id,
            superseded.project_id,
            superseded.group_id,
            superseded.generation_id,
            f"auto-resubmit:{superseded.attempt_id}",
            canonical_sha256(snapshot),
            canonical_sha256(request),
            request,
            input_snapshot=snapshot,
            retry_evidence=tuple(markers),
            superseded_attempt_id=superseded.attempt_id,
            budget_round=superseded.budget_round,
        )

    def _ladder_prompt(
        self,
        superseded: PersistentAttempt,
        snapshot: dict[str, object],
        generation: GroupVideoGeneration,
        *,
        attempt_id: UUID,
        index: int,
        markers: list[str],
    ) -> str | None:
        """梯级重编译；返回 None 表示本梯级（或退化后）复用被取代尝试的已编译提示词。"""
        if index not in _LADDER_RECOMPILE_STEPS:
            return None
        compiler = self._prompt_compiler
        if compiler is None:
            markers.append("auto_ladder:degraded:no_compiler")
            return None
        strategy = _ladder_strategy(superseded, index, compiler.strategy)
        if strategy is None:
            markers.append("auto_ladder:degraded:strategy_unavailable")
            return None
        repair_reason = (
            _content_qc_repair_reason(superseded.probe_evidence)
            if index in _LADDER_REPAIR_REASON_STEPS
            else None
        )
        try:
            value = _compiler_input_from_snapshot(
                snapshot, generation, strategy=strategy, attempt_id=attempt_id
            )
        except VideoRuleViolation as exc:
            markers.append(f"auto_ladder:degraded:{exc}")
            return None
        try:
            compiled = _compile_with_optional_reason(
                compiler, value, max_repairs=2, repair_reason=repair_reason, markers=markers
            )
        except Exception as exc:
            markers.append(f"auto_ladder:degraded:compile_failed:{type(exc).__name__}")
            return None
        snapshot["video_prompt"] = {
            "text": compiled.text,
            "strategy": compiled.strategy.value,
            "model": compiled.model,
            "provider_receipt": compiled.provider_receipt,
            "repaired": compiled.repaired,
            "input_fingerprint": compiled.input_fingerprint,
        }
        markers.append(f"auto_ladder:recompiled:{compiled.strategy.value}")
        return compiled.text

    def _supersede_for_resubmit(
        self,
        superseded: PersistentAttempt,
        node: EngineNode,
        *,
        event_type: str,
        error_code: str,
        reason: str,
        terminate: bool = True,
        retry_after_seconds: float | None = None,
    ) -> PersistentAttempt:
        """AUTO 项目的自动重提：旧尝试落终态（或 QC 保留），替代尝试计入该组提交预算。

        预算耗尽时落稳定终态并给出真实原因；MANUAL 项目从不进入此路径。
        重放安全：替代尝试已存在时幂等返回（梯子序号由首次构建时的预算计数推导，
        重复推进不得因计数漂移重建出不同内容）。
        """
        if (
            self._repo.find_attempt(
                superseded.owner_id, f"auto-resubmit:{superseded.attempt_id}"
            )
            is not None
        ):
            return superseded
        count = self._repo.count_group_attempts(
            superseded.owner_id, superseded.group_id, budget_round=superseded.budget_round
        )
        if count >= self._auto_resubmit_policy.max_submissions:
            if not terminate:
                if self._content_defect_deliverable(superseded):
                    # 2026-10-16 决策：梯子耗尽的内容级缺陷（黑屏类）不再 park，
                    # 带病交付：照常归档完成并成为当前视频，缺陷证据持久保留。
                    return self._deliver_with_defect(superseded, node)
                if f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}" in superseded.retry_evidence:
                    return superseded
                parked = replace(
                    superseded,
                    retry_evidence=superseded.retry_evidence
                    + (f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}",),
                )
                return self._save_with_audit(
                    parked, node, event_type, {}, error_code=SUBMISSION_BUDGET_EXHAUSTED
                )
            exhausted = self._terminal_superseded(
                superseded, SUBMISSION_BUDGET_EXHAUSTED, SUBMISSION_BUDGET_EXHAUSTED
            )
            return self._save_with_audit(
                exhausted, node, event_type, {}, error_code=SUBMISSION_BUDGET_EXHAUSTED
            )
        replacement = self._build_auto_replacement(superseded, next_attempt_index=count + 1)
        delay = self._auto_resubmit_delay(count, retry_after_seconds)
        updated = (
            self._terminal_superseded(superseded, error_code, reason)
            if terminate
            else replace(
                superseded,
                retry_evidence=superseded.retry_evidence + (f"auto_resubmit:{reason}",),
            )
        )
        saved, _ = self._repo.supersede_attempt(
            updated,
            replacement,
            self._event(updated, node, event_type, {}, error_code=error_code),
            delay_seconds=delay,
            max_submissions=self._auto_resubmit_policy.max_submissions,
        )
        return saved

    @staticmethod
    def _terminal_superseded(
        superseded: PersistentAttempt, error_code: str, reason: str
    ) -> PersistentAttempt:
        return replace(
            superseded,
            stage=AttemptStage.FAILED,
            archive_stage=(
                ArchiveStage.CLEANUP_PENDING
                if superseded.staged_ids
                else superseded.archive_stage
            ),
            cleanup_terminal_stage=(
                AttemptStage.FAILED if superseded.staged_ids else superseded.cleanup_terminal_stage
            ),
            error_code=error_code,
            remote_acceptance=None,
            retry_evidence=superseded.retry_evidence + (f"auto_resubmit:{reason}",),
        )

    def _stalled_gpu_decision(
        self, owner_id: UUID, attempt: PersistentAttempt, node: EngineNode
    ) -> PersistentAttempt:
        """AUTO 项目的停滞智能处置：按绑定节点的 GPU 探测结论决定继续等待或取消重提。

        节点忙或探测失败都不新增动作，维持降频恢复查询（探测失败时既有
        对账无果兜底仍生效）；只有确认节点空闲或进程死才进入取消重提。
        MANUAL 项目从不进入此路径。
        """
        observation = self._gpu_probe.probe(node)
        receipt: dict[str, object] = {
            "schema": GPU_PROBE_RECEIPT_SCHEMA,
            "status": observation.outcome.value,
        }
        logger.info(
            "h3 stalled attempt %s gpu probe on node %s: outcome=%s reachable=%s "
            "queue_running=%s queue_pending=%s detail=%s",
            attempt.attempt_id,
            node.node_id,
            observation.outcome.value,
            observation.comfyui_reachable,
            observation.queue_running,
            observation.queue_pending,
            observation.detail,
        )
        if observation.outcome is GpuProbeOutcome.BUSY:
            self._audit(attempt, node, "GPU_PROBE_BUSY", receipt)
            return attempt
        if observation.outcome is GpuProbeOutcome.PROBE_FAILED:
            self._audit(
                attempt, node, "GPU_PROBE_INCONCLUSIVE", receipt, error_code="gpu_probe_failed"
            )
            return attempt
        # 节点空闲或进程死：先走既有取消链路（引擎不可达时不假定成功），
        # 取消确认后立即自动重提（计入该组提交预算）。
        self._audit(attempt, node, "GPU_PROBE_IDLE", receipt)
        marked = attempt
        if _AUTO_CANCEL_STALLED_MARKER not in marked.retry_evidence:
            marked = self._repo.save_attempt(
                replace(
                    marked,
                    retry_evidence=marked.retry_evidence + (_AUTO_CANCEL_STALLED_MARKER,),
                )
            )
        return self._auto_resubmit_after_cancel(
            owner_id, self.cancel(owner_id, marked.attempt_id)
        )

    def _auto_resubmit_after_cancel(
        self, owner_id: UUID, attempt: PersistentAttempt
    ) -> PersistentAttempt:
        """探测触发的自动取消在确认后补齐自动重提；其余取消路径保持原样。

        旧尝试已是 CANCELLED 终态，因此保留其取消事实与不可变历史（terminate=False），
        只追加替代提交血缘；预算耗尽时落 submission_budget_exhausted 稳定终态证据。
        """
        if (
            attempt.stage is not AttemptStage.CANCELLED
            or _AUTO_CANCEL_STALLED_MARKER not in attempt.retry_evidence
            or _AUTO_RESUBMIT_STALLED_MARKER in attempt.retry_evidence
            or f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}" in attempt.retry_evidence
            or not self._auto_mode(attempt)
        ):
            return attempt
        node = self._assigned(attempt)
        return self._supersede_for_resubmit(
            attempt,
            node,
            event_type="RESUBMIT_STALLED_IDLE",
            error_code=_AUTO_CANCEL_STALLED_REASON,
            reason=_AUTO_CANCEL_STALLED_REASON,
            terminate=False,
        )

    def cancel(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        attempt = self._owned(attempt_id, owner_id)
        if attempt.stage in {AttemptStage.SUCCEEDED, AttemptStage.ARCHIVED}:
            return attempt
        if attempt.stage is AttemptStage.CREATED:
            return self._repo.save_attempt(
                replace(attempt, stage=AttemptStage.CANCELLED, requested_action=None)
            )
        if attempt.stage is AttemptStage.NOT_SUBMITTED:
            if attempt.staged_ids:
                pending = self._repo.save_attempt(
                    replace(
                        attempt,
                        archive_stage=ArchiveStage.CLEANUP_PENDING,
                        cleanup_terminal_stage=AttemptStage.CANCELLED,
                        requested_action=None,
                    )
                )
                return self._cleanup_staged(pending)
            cancelled = self._repo.save_attempt(
                replace(attempt, stage=AttemptStage.CANCELLED, requested_action=None)
            )
            self._release_slot(cancelled)
            return self._owned(attempt_id, owner_id)
        node = self._assigned(attempt)
        provider_task_id = _provider_task_key(attempt)
        try:
            observed = self._engine.inspect(node, provider_task_id)
        except EngineAttemptNotFound:
            missing: dict[str, object] = {
                "schema": "kinestack.execution.v1",
                "task_id": _provider_task_key(attempt),
                "status": "NOT_FOUND",
            }
            if attempt.cancel_dispatched:
                return self._cancel_inconclusive(
                    attempt,
                    node,
                    "CANCEL_INSPECT_NOT_FOUND",
                    missing,
                    "EngineAttemptNotFound",
                )
            if (
                not attempt.cancel_dispatched
                and attempt.stage is AttemptStage.SUBMITTING
                and attempt.remote_id is None
            ):
                return self._save_with_audit(
                    replace(
                        attempt,
                        stage=AttemptStage.REMOTE_STATE_UNKNOWN,
                        requested_action=RequestedAction.CANCEL,
                        remote_acceptance="UNKNOWN",
                        error_code="EngineAttemptNotFound",
                        retry_evidence=attempt.retry_evidence + ("cancel:inspect_404",),
                    ),
                    node,
                    "CANCEL_INSPECT_NOT_FOUND",
                    missing,
                    error_code="EngineAttemptNotFound",
                )
            return self._save_with_audit(
                replace(
                    attempt,
                    stage=AttemptStage.REMOTE_STATE_UNKNOWN,
                    requested_action=RequestedAction.CANCEL,
                    remote_acceptance="UNKNOWN",
                    retry_evidence=attempt.retry_evidence + ("cancel:inspect_404",),
                ),
                node,
                "CANCEL_INSPECT_NOT_FOUND",
                missing,
                error_code="EngineAttemptNotFound",
            )
        except EngineError as exc:
            if attempt.cancel_dispatched:
                return self._cancel_inconclusive(
                    attempt,
                    node,
                    "CANCEL_INSPECT_ERROR",
                    {},
                    type(exc).__name__,
                )
            self._save_with_audit(
                replace(attempt, error_code=type(exc).__name__),
                node,
                "CANCEL_INSPECT_ERROR",
                {},
                error_code=type(exc).__name__,
            )
            raise
        try:
            _validate_task_receipt(observed, attempt)
        except VideoRuleViolation:
            self._audit(
                attempt,
                node,
                "CANCEL_INSPECT_INVALID_RECEIPT",
                observed,
                error_code="invalid_receipt",
            )
            raise
        observed_stage = _response_stage(observed)
        if observed_stage is AttemptStage.REMOTE_STATE_UNKNOWN:
            if attempt.cancel_dispatched:
                return self._cancel_inconclusive(
                    replace(
                        attempt,
                        stage=AttemptStage.REMOTE_STATE_UNKNOWN,
                        requested_action=RequestedAction.CANCEL,
                        remote_acceptance=str(observed.get("remote_acceptance", "")) or "UNKNOWN",
                        raw_receipt=canonical_redacted_receipt(observed),
                    ),
                    node,
                    "CANCEL_INSPECT_RECEIPT",
                    observed,
                    "cancel_remote_state_unknown",
                )
            return self._save_with_audit(
                replace(
                    attempt,
                    stage=AttemptStage.REMOTE_STATE_UNKNOWN,
                    requested_action=RequestedAction.CANCEL,
                    remote_acceptance=str(observed.get("remote_acceptance", "")) or "UNKNOWN",
                    raw_receipt=canonical_redacted_receipt(observed),
                ),
                node,
                "CANCEL_INSPECT_RECEIPT",
                observed,
            )
        if observed_stage in {
            AttemptStage.SUCCEEDED,
            AttemptStage.FAILED,
            AttemptStage.CANCELLED,
        }:
            updated = replace(
                attempt,
                stage=observed_stage,
                requested_action=None,
                remote_acceptance=str(observed.get("remote_acceptance", "")) or None,
                raw_receipt=canonical_redacted_receipt(observed),
            )
        elif attempt.cancel_dispatched:
            return self._cancel_inconclusive(
                replace(
                    attempt,
                    stage=observed_stage,
                    requested_action=RequestedAction.CANCEL,
                    raw_receipt=canonical_redacted_receipt(observed),
                    error_code="cancel_reconcile_pending",
                    retry_evidence=attempt.retry_evidence + ("cancel:inspect_only",),
                ),
                node,
                "CANCEL_RECONCILE_RECEIPT",
                observed,
                "cancel_reconcile_pending",
            )
        else:
            operation_key = f"{attempt.attempt_id}:cancel"
            attempt = self._repo.save_attempt(
                replace(
                    attempt,
                    cancel_dispatched=True,
                    cancel_idempotency_key=operation_key,
                    requested_action=(
                        RequestedAction.CANCEL
                        if _durable_candidate(attempt)
                        else attempt.requested_action
                    ),
                )
            )
            try:
                response = self._engine.cancel(
                    node,
                    _provider_task_key(attempt),
                    idempotency_key=operation_key,
                )
            except EngineError as exc:
                self._save_with_audit(
                    replace(attempt, error_code=type(exc).__name__),
                    node,
                    "CANCEL_ERROR",
                    {},
                    error_code=type(exc).__name__,
                )
                raise
            try:
                _validate_cancel_receipt(response, attempt)
            except VideoRuleViolation:
                self._audit(
                    attempt,
                    node,
                    "CANCEL_INVALID_RECEIPT",
                    response,
                    error_code="invalid_receipt",
                )
                raise
            updated = replace(
                attempt,
                stage=_cancel_response_stage(response, attempt),
                requested_action=_pending_cancel_action(response, attempt),
                remote_acceptance=str(response.get("remote_acceptance", "")) or None,
                raw_receipt=canonical_redacted_receipt(response),
            )
        if updated.stage in {AttemptStage.FAILED, AttemptStage.CANCELLED}:
            updated = replace(
                updated,
                archive_stage=ArchiveStage.CLEANUP_PENDING,
                cleanup_terminal_stage=updated.stage,
            )
        updated = self._save_with_audit(
            updated,
            node,
            "CANCEL_SKIPPED_TERMINAL"
            if observed_stage
            in {AttemptStage.SUCCEEDED, AttemptStage.FAILED, AttemptStage.CANCELLED}
            else "CANCEL_RECEIPT",
            observed
            if observed_stage
            in {AttemptStage.SUCCEEDED, AttemptStage.FAILED, AttemptStage.CANCELLED}
            else response,
        )
        if updated.stage is AttemptStage.SUCCEEDED:
            self._release_slot(updated)
            updated = self._owned(attempt_id, owner_id)
        return updated

    def _cancel_inconclusive(
        self,
        attempt: PersistentAttempt,
        node: EngineNode,
        event_type: str,
        receipt: dict[str, object],
        error_code: str,
    ) -> PersistentAttempt:
        current = self._save_with_audit(
            replace(
                attempt,
                requested_action=RequestedAction.CANCEL,
                remote_acceptance="UNKNOWN",
                error_code=error_code,
                cancel_inspect_count=attempt.cancel_inspect_count + 1,
                retry_evidence=attempt.retry_evidence + ("cancel:inspect_only",),
            ),
            node,
            event_type,
            receipt,
            error_code=error_code,
        )
        operation_key = current.cancel_idempotency_key
        if current.cancel_inspect_count < 3 or operation_key is None:
            return current
        try:
            response = self._engine.cancel(
                node,
                _provider_task_key(current),
                idempotency_key=operation_key,
            )
        except EngineError as exc:
            return self._save_with_audit(
                replace(
                    current,
                    cancel_inspect_count=0,
                    error_code=type(exc).__name__,
                ),
                node,
                "CANCEL_REDRIVE_UNKNOWN",
                {},
                error_code=type(exc).__name__,
            )
        try:
            _validate_cancel_receipt(response, current)
        except VideoRuleViolation:
            self._save_with_audit(
                replace(
                    current,
                    cancel_inspect_count=0,
                    error_code="invalid_receipt",
                ),
                node,
                "CANCEL_REDRIVE_INVALID_RECEIPT",
                response,
                error_code="invalid_receipt",
            )
            raise
        updated = replace(
            current,
            stage=_cancel_response_stage(response, current),
            requested_action=_pending_cancel_action(response, current),
            cancel_inspect_count=0,
            remote_acceptance=str(response.get("remote_acceptance", "")) or None,
            raw_receipt=canonical_redacted_receipt(response),
            error_code=None,
        )
        if updated.stage in {AttemptStage.FAILED, AttemptStage.CANCELLED}:
            updated = replace(
                updated,
                archive_stage=ArchiveStage.CLEANUP_PENDING,
                cleanup_terminal_stage=updated.stage,
            )
        return self._save_with_audit(
            updated,
            node,
            "CANCEL_REDRIVE_RECEIPT",
            response,
        )

    def archive(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        attempt = self._owned(attempt_id, owner_id)
        if attempt.archive_stage is ArchiveStage.COMPLETE:
            return attempt
        self._verify_persisted_hashes(attempt)
        if attempt.stage not in {AttemptStage.SUCCEEDED, AttemptStage.ARCHIVED}:
            raise VideoRuleViolation("only a successful attempt can be archived")
        node = self._assigned(attempt)
        current = attempt
        if current.archive_stage is ArchiveStage.NOT_STARTED:
            task_key = _provider_task_key(attempt)
            outputs_receipt = self._engine.outputs(node, task_key)
            self._audit(attempt, node, "OUTPUTS_RECEIPT", outputs_receipt)
            items = outputs_receipt.get("items")
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise VideoRuleViolation("successful H3 attempt must expose exactly one MP4 output")
            item = items[0]
            _validate_output_receipt(item, attempt, task_key)
            output_id, digest = _required_text(item, "id"), _required_text(item, "sha256")
            size_bytes = _required_positive_int(item, "size_bytes")
            media_type = _required_text(item, "media_type")
            current = replace(
                current,
                archive_stage=ArchiveStage.DOWNLOADED,
                output_id=output_id,
                output_sha256=digest,
                output_size_bytes=size_bytes,
                output_media_type=media_type,
            )
            current = self._repo.save_attempt(current)
        if current.archive_stage is ArchiveStage.DOWNLOADED:
            assert current.output_id and current.output_sha256
            content = self._engine.download(node, _provider_task_key(current), current.output_id)
            digest = current.output_sha256
            if (
                current.output_size_bytes is None
                or current.output_media_type is None
                or hashlib.sha256(content).hexdigest() != digest
                or len(content) != current.output_size_bytes
            ):
                raise VideoRuleViolation("download_integrity_mismatch")
            self._audit(
                current,
                node,
                "DOWNLOAD_VERIFIED",
                {
                    "id": current.output_id,
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": current.output_media_type,
                    "schema": "tubeflow.download.v1",
                },
            )
            asset = self._assets.archive_immutable(
                generation_id=attempt.generation_id,
                attempt_id=attempt.attempt_id,
                output_id=current.output_id,
                content=content,
                media_type=current.output_media_type,
                expected_sha256=current.output_sha256,
            )
            current = replace(
                current,
                archive_stage=ArchiveStage.STORED,
                archived_asset=asset,
            )
            current = self._repo.save_attempt(current)
        if current.archive_stage is ArchiveStage.STORED:
            assert current.archived_asset is not None
            content = self._assets.read_verified(current.archived_asset)
            expected_size: tuple[int, int] | None = None
            workflow: tuple[str, str] | None = None
            evidence: VideoMediaEvidence | None = None
            try:
                evidence = self._probe.inspect(content)
                snapshot = attempt.input_snapshot
                if snapshot is None:
                    raise VideoRuleViolation("immutable_video_input_changed")
                manifest = snapshot.get("workflow_manifest")
                aspect = AspectRatio(str(snapshot["aspect_ratio"]))
                # 原生尺寸按该工作流身份解析（HD 1600x896 / legacy 1056x608）；
                # 未登记身份 fail-closed，不再用全局固定尺寸误判新工作流。
                expected_size = expected_native_size(manifest, aspect)
                workflow = workflow_identity(manifest)
                _verify_media(
                    evidence,
                    int(str(snapshot["target_duration_ms"])),
                    native_size=expected_size,
                )
                # 技术规格通过后才做内容级检测（2026-10-16 决策：黑屏类画面缺陷）。
                content_qc = self._content_qc_evidence(content)
            except _ContentQcFailure as exc:
                failed = replace(
                    current,
                    archive_stage=ArchiveStage.QC_FAILED,
                    error_code=exc.code,
                    retry_evidence=current.retry_evidence + (f"probe:{exc.code}",),
                    probe_evidence={
                        "qc_passed": False,
                        "error_code": exc.code,
                        "message": exc.message,
                        "content_qc": exc.content_qc,
                    },
                )
                current = self._save_with_audit(
                    failed,
                    node,
                    "QC_FAILED",
                    failed.probe_evidence or {},
                    error_code=exc.code,
                )
            except Exception as exc:
                probe_evidence: dict[str, object] = {
                    "qc_passed": False,
                    "error_code": type(exc).__name__,
                    "message": str(exc),
                }
                if str(exc) == _RESOLUTION_MISMATCH_RULE:
                    # 只在尺寸失配时补充期望/实际/工作流身份，便于事后归因；
                    # 其余规则的证据形状保持原样（历史统计按 message 精确匹配）。
                    probe_evidence["rule_code"] = str(exc)
                    if expected_size is not None:
                        probe_evidence["expected_width"] = expected_size[0]
                        probe_evidence["expected_height"] = expected_size[1]
                    if workflow is not None:
                        probe_evidence["workflow_id"] = workflow[0]
                        probe_evidence["workflow_version"] = workflow[1]
                    if evidence is not None:
                        probe_evidence["actual_width"] = evidence.width
                        probe_evidence["actual_height"] = evidence.height
                failed = replace(
                    current,
                    archive_stage=ArchiveStage.QC_FAILED,
                    error_code=type(exc).__name__,
                    retry_evidence=current.retry_evidence + (f"probe:{type(exc).__name__}",),
                    probe_evidence=probe_evidence,
                )
                current = self._save_with_audit(
                    failed,
                    node,
                    "QC_FAILED",
                    failed.probe_evidence or {},
                    error_code=type(exc).__name__,
                )
            else:
                assert evidence is not None
                current = replace(
                    current,
                    archive_stage=ArchiveStage.QC_PASSED,
                    error_code=None,
                    probe_evidence={
                        "qc_passed": True,
                        "media_type": evidence.media_type,
                        "width": evidence.width,
                        "height": evidence.height,
                        "fps": evidence.fps,
                        "duration_ms": evidence.duration_ms,
                        "video_tracks": evidence.video_tracks,
                        "audio_tracks": evidence.audio_tracks,
                        "container_names": list(evidence.container_names),
                        "video_duration_ms": evidence.video_duration_ms,
                        "video_frame_count": evidence.video_frame_count,
                        "audio_start_ms": evidence.audio_start_ms,
                        "audio_duration_ms": evidence.audio_duration_ms,
                        "audio_end_ms": evidence.audio_end_ms,
                        "audio_codec": evidence.audio_codec,
                        "content_qc": content_qc,
                    },
                )
                current = self._save_with_audit(
                    current, node, "QC_PASSED", current.probe_evidence or {}
                )
        if current.archive_stage is ArchiveStage.QC_FAILED:
            if self._auto_mode(current):
                # AUTO：质检失败自动重生成新版本（计入该组预算）；旧版本不可变保留。
                outcome = self._supersede_for_resubmit(
                    current,
                    node,
                    event_type="QC_REGENERATE",
                    error_code=current.error_code or "qc_failed",
                    reason="qc_failed",
                    terminate=False,
                )
                if outcome.archive_stage is not ArchiveStage.CONFIRMED:
                    return outcome
                # 带病交付（预算耗尽的内容级缺陷）：继续走既有 CONFIRMED→COMPLETE 收尾。
                current = outcome
            else:
                return current
        if current.archive_stage is ArchiveStage.QC_PASSED:
            current = self._dispatch_archive_confirm(current, node)
            if current.archive_stage is ArchiveStage.CONFIRMING:
                # 确认回执丢失：保持 CONFIRMING 等待后续对账（不重入本调用的确认轮询）。
                return current
        if current.archive_stage is ArchiveStage.CONFIRMING:
            try:
                observed = self._engine.inspect(node, _provider_task_key(current))
            except EngineAttemptNotFound:
                return self._archive_confirm_inconclusive(
                    current,
                    node,
                    "ARCHIVE_CONFIRM_INSPECT_NOT_FOUND",
                    {
                        "schema": "kinestack.execution.v1",
                        "task_id": _provider_task_key(current),
                        "status": "NOT_FOUND",
                    },
                    "EngineAttemptNotFound",
                )
            except EngineError as exc:
                return self._archive_confirm_inconclusive(
                    current,
                    node,
                    "ARCHIVE_CONFIRM_INSPECT_ERROR",
                    {},
                    type(exc).__name__,
                )
            try:
                _validate_task_receipt(observed, current)
            except VideoRuleViolation:
                self._audit(
                    current,
                    node,
                    "ARCHIVE_CONFIRM_INSPECT_INVALID_RECEIPT",
                    observed,
                    error_code="invalid_receipt",
                )
                raise
            if observed.get("archive_confirmed") is not True:
                return self._archive_confirm_inconclusive(
                    current,
                    node,
                    "ARCHIVE_CONFIRM_INSPECT",
                    observed,
                    "archive_confirmation_pending",
                )
            current = self._save_with_audit(
                replace(current, archive_stage=ArchiveStage.CONFIRMED, error_code=None),
                node,
                "ARCHIVE_CONFIRM_RECONCILED",
                observed,
            )
        if current.archive_stage in {ArchiveStage.CONFIRMED, ArchiveStage.CLEANUP_PENDING}:
            current = replace(current, archive_stage=ArchiveStage.CLEANUP_PENDING)
            current = self._repo.save_attempt(current)
            for staged_id in current.staged_ids:
                if staged_id in current.cleaned_staged_ids:
                    continue
                current = self._delete_staged(current, node, staged_id)
            current = replace(
                current, stage=AttemptStage.ARCHIVED, archive_stage=ArchiveStage.COMPLETE
            )
            current = self._repo.save_attempt(current)
            self._release_slot(current)
            current = self._owned(attempt.attempt_id, attempt.owner_id)
            # 该组首次成功归档时自动设为当前视频；已有当前视频的组仍由用户手动切换。
            self._repo.mark_current_if_absent(current)
            current = self._owned(attempt.attempt_id, attempt.owner_id)
        return current

    def _dispatch_archive_confirm(
        self, current: PersistentAttempt, node: EngineNode
    ) -> PersistentAttempt:
        """归档确认派发：CONFIRMING 入口、幂等确认调用与回执校验（QC_PASSED 与带病交付共用）。"""
        operation_key = f"{current.attempt_id}:archive-confirm"
        current = self._repo.save_attempt(
            replace(
                current,
                archive_stage=ArchiveStage.CONFIRMING,
                archive_confirm_dispatched=True,
                archive_confirm_idempotency_key=operation_key,
            )
        )
        try:
            receipt = self._engine.confirm_archive(
                node,
                _provider_task_key(current),
                idempotency_key=operation_key,
            )
        except EngineUncertain:
            return self._save_with_audit(
                replace(current, error_code="EngineUncertain"),
                node,
                "ARCHIVE_CONFIRM_UNKNOWN",
                {},
                error_code="EngineUncertain",
            )
        try:
            _validate_archive_receipt(receipt, current)
        except VideoRuleViolation:
            self._audit(
                current,
                node,
                "ARCHIVE_CONFIRM_INVALID_RECEIPT",
                receipt,
                error_code="invalid_receipt",
            )
            raise
        return self._save_with_audit(
            replace(current, archive_stage=ArchiveStage.CONFIRMED, error_code=None),
            node,
            "ARCHIVE_CONFIRMED",
            receipt,
        )

    def _content_qc_evidence(self, content: bytes) -> dict[str, object]:
        """内容级 QC：抽帧检测持续黑屏区域；命中或抽样失败都按 QC 失败处理（不放行）。"""
        thresholds = self._content_qc_thresholds
        sampler = self._content_sampler
        if sampler is None:
            # 未装配抽帧器只出现在未走 factory 的测试装配中；证据显式记录，不静默。
            return {
                "algorithm": thresholds.algorithm,
                "status": "sampler_not_configured",
                "findings": [],
            }
        try:
            frames = sampler.sample(content, thresholds=thresholds)
            findings = find_sustained_black_regions(frames, thresholds)
        except Exception as exc:
            raise _ContentQcFailure(
                VIDEO_CONTENT_QC_SAMPLER_ERROR,
                f"content QC sampling failed: {type(exc).__name__}: {exc}",
                {
                    "algorithm": thresholds.algorithm,
                    "sampler_error": f"{type(exc).__name__}: {exc}",
                    "findings": [],
                },
            ) from exc
        block: dict[str, object] = {
            "algorithm": thresholds.algorithm,
            "findings": [_black_region_evidence(finding) for finding in findings],
        }
        if findings:
            raise _ContentQcFailure(
                VIDEO_CONTENT_BLACK_REGION, _black_region_message(findings), block
            )
        return block

    @staticmethod
    def _content_defect_deliverable(attempt: PersistentAttempt) -> bool:
        """带病交付门槛（2026-10-16 决策）：仅内容级黑屏缺陷且产物已不可变入库的 AUTO QC 失败。"""
        return (
            attempt.error_code == VIDEO_CONTENT_BLACK_REGION
            and attempt.archive_stage is ArchiveStage.QC_FAILED
            and attempt.archived_asset is not None
        )

    def _deliver_with_defect(
        self, superseded: PersistentAttempt, node: EngineNode
    ) -> PersistentAttempt:
        """梯子耗尽的内容级缺陷带病交付：保留缺陷证据，复用归档确认链路完成归档。

        probe_evidence 保留 qc_passed False 与 content_qc 块，retry_evidence 追加
        deliver_with_defect 标记；标记已持久化后的重放直接续走同一幂等键的确认派发，
        不重复事件也不重复动作。
        """
        if DELIVER_WITH_DEFECT_MARKER in superseded.retry_evidence:
            marked = superseded
        else:
            marked = replace(
                superseded,
                retry_evidence=superseded.retry_evidence + (DELIVER_WITH_DEFECT_MARKER,),
            )
            marked = self._save_with_audit(
                marked,
                node,
                QC_DEFECT_DELIVERED_EVENT,
                {
                    "schema": "tubeflow.qc-defect-delivery.v1",
                    "error_code": VIDEO_CONTENT_BLACK_REGION,
                    "content_qc": (marked.probe_evidence or {}).get("content_qc"),
                },
                error_code=superseded.error_code,
            )
        return self._dispatch_archive_confirm(marked, node)

    def _archive_confirm_inconclusive(
        self,
        attempt: PersistentAttempt,
        node: EngineNode,
        event_type: str,
        receipt: dict[str, object],
        error_code: str,
    ) -> PersistentAttempt:
        current = self._save_with_audit(
            replace(
                attempt,
                error_code=error_code,
                archive_confirm_inspect_count=attempt.archive_confirm_inspect_count + 1,
            ),
            node,
            event_type,
            receipt,
            error_code=error_code,
        )
        redrive_key = current.archive_confirm_idempotency_key
        if current.archive_confirm_inspect_count < 3 or redrive_key is None:
            return current
        try:
            response = self._engine.confirm_archive(
                node,
                _provider_task_key(current),
                idempotency_key=redrive_key,
            )
        except EngineError as exc:
            return self._save_with_audit(
                replace(
                    current,
                    archive_confirm_inspect_count=0,
                    error_code=type(exc).__name__,
                ),
                node,
                "ARCHIVE_CONFIRM_REDRIVE_UNKNOWN",
                {},
                error_code=type(exc).__name__,
            )
        try:
            _validate_archive_receipt(response, current)
        except VideoRuleViolation:
            self._save_with_audit(
                replace(
                    current,
                    archive_confirm_inspect_count=0,
                    error_code="invalid_receipt",
                ),
                node,
                "ARCHIVE_CONFIRM_REDRIVE_INVALID_RECEIPT",
                response,
                error_code="invalid_receipt",
            )
            raise
        return self._save_with_audit(
            replace(
                current,
                archive_stage=ArchiveStage.CONFIRMED,
                archive_confirm_inspect_count=0,
                error_code=None,
            ),
            node,
            "ARCHIVE_CONFIRM_REDRIVE_RECEIPT",
            response,
        )

    def _stage_and_submit(
        self, attempt: PersistentAttempt, generation: GroupVideoGeneration
    ) -> PersistentAttempt:
        if attempt.narration_validation is None:
            self._verify_narration_bytes(attempt)
            attempt = self._repo.save_attempt(
                replace(
                    attempt,
                    narration_validation={
                        "version": "tts-bytes-v1",
                        "verified_at": datetime.now(UTC).isoformat(),
                    },
                )
            )
        node = self._claim_slot(attempt.attempt_id, generation.snapshot)
        refreshed = self._repo.get_attempt(attempt.attempt_id)
        if refreshed is None:
            raise VideoRuleViolation("unknown attempt")
        attempt = refreshed
        current = replace(
            attempt, node_id=node.node_id, slot_claimed=True, stage=AttemptStage.STAGING
        )
        current = self._repo.save_attempt(current)
        staged: list[str] = list(attempt.staged_ids)
        result: dict[str, object] = {}
        try:
            for reference in generation.references:
                if reference.ordinal <= len(staged):
                    continue
                content = self._assets.read_verified(reference.asset)
                result = self._engine.stage_asset(
                    node,
                    content=content,
                    media_type=reference.asset.media_type,
                    filename=f"reference-{reference.ordinal}",
                    expected_sha256=reference.asset.sha256,
                    idempotency_key=f"{attempt.attempt_id}:stage:{reference.ordinal}",
                )
                _validate_stage_receipt(result, reference.asset)
                staged.append(_required_text(result, "id"))
                current = replace(current, staged_ids=tuple(staged))
                current = self._save_with_audit(
                    current,
                    node,
                    "STAGE_RECEIPT",
                    result,
                    request_sha256=reference.asset.sha256,
                )
        except Exception as exc:
            current = replace(
                current,
                stage=AttemptStage.NOT_SUBMITTED,
                archive_stage=ArchiveStage.CLEANUP_PENDING,
                cleanup_terminal_stage=AttemptStage.NOT_SUBMITTED,
                error_code=type(exc).__name__,
            )
            current = self._save_with_audit(
                current, node, "STAGE_ERROR", result, error_code=type(exc).__name__
            )
            raise
        # Recheck after staging and before recording SUBMITTING so a deterministic
        # local drift cannot be mistaken for uncertain remote acceptance.
        self._verify_narration_bytes(current)
        execution_request = dict(current.request)
        references = execution_request.pop("references")
        assert isinstance(references, list)
        execution_request["staged_assets"] = [
            {
                "staged_asset_id": staged_id,
                "binding_key": reference["binding_key"],
                "ordinal": reference["ordinal"],
            }
            for staged_id, reference in zip(staged, references, strict=True)
            if isinstance(reference, dict)
        ]
        current = replace(
            current,
            stage=AttemptStage.SUBMITTING,
            request=execution_request,
            request_sha256=canonical_sha256(execution_request),
        )
        self._verify_narration_bytes(current)
        current = self._repo.commit_submitting(current)
        try:
            response = self._engine.submit(
                node, idempotency_key=f"{attempt.attempt_id}:submit", body=current.request
            )
        except EngineUncertain:
            current = replace(
                current,
                stage=AttemptStage.REMOTE_STATE_UNKNOWN,
                remote_acceptance="UNKNOWN",
                retry_evidence=current.retry_evidence + ("submit_response_uncertain",),
            )
            current = self._save_with_audit(
                current, node, "SUBMIT_UNKNOWN", {}, error_code="EngineUncertain"
            )
            return current
        except EngineRejected as exc:
            if self._auto_mode(current):
                return self._supersede_for_resubmit(
                    current,
                    node,
                    event_type="SUBMIT_REJECTED",
                    error_code=f"EngineRejected:{exc.status_code}",
                    reason=f"engine_rejected:{exc.status_code}",
                    retry_after_seconds=exc.retry_after_seconds,
                )
            # MANUAL 项目维持现状：既定拒绝仍落未知态，等待人工对账。
            current = replace(
                current,
                stage=AttemptStage.REMOTE_STATE_UNKNOWN,
                remote_acceptance="UNKNOWN",
                retry_evidence=current.retry_evidence + (f"submit_rejected:{exc.status_code}",),
            )
            current = self._save_with_audit(
                current,
                node,
                "SUBMIT_UNKNOWN",
                {},
                error_code=f"EngineRejected:{exc.status_code}",
            )
            return current
        except EngineError as exc:
            current = replace(
                current,
                archive_stage=ArchiveStage.CLEANUP_PENDING,
                cleanup_terminal_stage=AttemptStage.FAILED,
                error_code=type(exc).__name__,
                retry_evidence=current.retry_evidence + ("submit:deterministic_failure",),
            )
            current = self._save_with_audit(
                current, node, "SUBMIT_ERROR", {}, error_code=type(exc).__name__
            )
            return current
        acceptance = str(response.get("remote_acceptance", "")) or None
        try:
            remote_id = _validate_submit_receipt(response, current)
        except VideoRuleViolation:
            self._audit(
                current,
                node,
                "SUBMIT_INVALID_RECEIPT",
                response,
                request_sha256=current.request_sha256,
                error_code="invalid_receipt",
            )
            raise
        submitted_phase = str(response.get("status", "ACCEPTED"))
        current = replace(
            current,
            stage=_response_stage(response),
            remote_id=remote_id,
            remote_phase=submitted_phase,
            remote_phase_changed_at=(
                current.remote_phase_changed_at
                if current.remote_phase == submitted_phase
                else datetime.now(UTC).isoformat()
            ),
            raw_receipt=canonical_redacted_receipt(response),
            remote_acceptance=acceptance,
        )
        current = self._save_with_audit(
            current,
            node,
            "SUBMIT_RECEIPT",
            response,
            request_sha256=current.request_sha256,
            provider_task_key=remote_id,
        )
        return current

    def _cleanup_staged(self, attempt: PersistentAttempt) -> PersistentAttempt:
        node = self._assigned(attempt)
        current = attempt
        for staged_id in current.staged_ids:
            if staged_id in current.cleaned_staged_ids:
                continue
            current = self._delete_staged(current, node, staged_id)
        terminal = current.cleanup_terminal_stage or current.stage
        current = self._repo.save_attempt(
            replace(
                current,
                stage=terminal,
                archive_stage=ArchiveStage.NOT_STARTED,
                cleanup_terminal_stage=None,
                staged_ids=(),
                cleaned_staged_ids=(),
            )
        )
        self._release_slot(current)
        return self._owned(current.attempt_id, current.owner_id)

    def _delete_staged(
        self, attempt: PersistentAttempt, node: EngineNode, staged_id: str
    ) -> PersistentAttempt:
        try:
            receipt = self._engine.delete_staged_asset(node, staged_id)
        except EngineError as exc:
            self._save_with_audit(
                replace(attempt, error_code=type(exc).__name__),
                node,
                "STAGED_DELETE_ERROR",
                {
                    "id": staged_id,
                    "schema": "kinestack.staged-asset-deletion.v1",
                    "status": "ERROR",
                },
                error_code=type(exc).__name__,
            )
            raise
        try:
            _validate_delete_receipt(receipt, staged_id)
        except VideoRuleViolation:
            self._audit(
                attempt,
                node,
                "STAGED_DELETE_INVALID_RECEIPT",
                receipt,
                error_code="invalid_receipt",
            )
            raise
        return self._save_with_audit(
            replace(
                attempt,
                cleaned_staged_ids=attempt.cleaned_staged_ids + (staged_id,),
                error_code=None,
            ),
            node,
            "STAGED_DELETE_RECEIPT",
            receipt,
        )

    def _owned(self, attempt_id: UUID, owner_id: UUID) -> PersistentAttempt:
        attempt = self._repo.get_attempt(attempt_id)
        if attempt is None or attempt.owner_id != owner_id:
            raise VideoRuleViolation("video attempt not found")
        return attempt

    def _release_slot(self, attempt: PersistentAttempt) -> None:
        if not attempt.slot_claimed:
            return
        if attempt.slot_lease_token is None:
            raise VideoRuleViolation("slot_lease_missing")
        self._repo.release_node(attempt.attempt_id, attempt.slot_lease_token)

    def get(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        """Read-only query; never polls, submits, cancels, archives, or claims capacity."""
        attempt = self._owned(attempt_id, owner_id)
        return replace(
            attempt,
            is_current=self._repo.is_current(attempt.group_id, attempt.attempt_id),
        )

    def get_current(self, owner_id: UUID, group_id: UUID) -> PersistentAttempt:
        attempt = self._repo.get_current(owner_id, group_id)
        if attempt is None:
            raise VideoRuleViolation("group video not found")
        return replace(attempt, is_current=True)

    def list_history(self, owner_id: UUID, group_id: UUID) -> tuple[PersistentAttempt, ...]:
        current = self._repo.get_current(owner_id, group_id)
        current_id = current.attempt_id if current else None
        return tuple(
            replace(item, is_current=item.attempt_id == current_id)
            for item in self._repo.list_history(owner_id, group_id)
        )

    def select_current(self, owner_id: UUID, group_id: UUID, attempt_id: UUID) -> PersistentAttempt:
        return replace(self._repo.select_current(owner_id, group_id, attempt_id), is_current=True)

    def sweep_expired_slots(self) -> int:
        return self._repo.sweep_expired_slots()

    def _verify_persisted_hashes(self, attempt: PersistentAttempt) -> None:
        if canonical_sha256(attempt.request) != attempt.request_sha256:
            raise VideoRuleViolation("persisted_provider_request_hash_mismatch")
        if (
            attempt.input_snapshot is None
            or canonical_sha256(attempt.input_snapshot) != attempt.generation_sha256
        ):
            raise VideoRuleViolation("immutable_video_input_changed")

    def _verify_narration_bytes(self, attempt: PersistentAttempt) -> None:
        self._verify_authority_snapshot(attempt)
        snapshot = attempt.input_snapshot or {}
        sentences = snapshot.get("sentences")
        if not isinstance(sentences, list) or not sentences:
            raise VideoRuleViolation("complete_real_narration_required")
        for sentence in sentences:
            if not isinstance(sentence, dict):
                raise VideoRuleViolation("complete_real_narration_required")
            for key in ("raw_audio", "normalized_audio"):
                value = sentence.get(key)
                if not isinstance(value, dict):
                    raise VideoRuleViolation("raw_and_normalized_audio_required")
                asset = MediaAssetRef(
                    UUID(str(value["id"])),
                    str(value["sha256"]),
                    str(value["media_type"]),
                    int(str(value["size_bytes"])),
                )
                try:
                    content = self._assets.read_verified(asset)
                except VideoAssetIntegrityError as exc:
                    raise VideoRuleViolation("narration_audio_bytes_changed") from exc
                if (
                    len(content) != asset.size_bytes
                    or hashlib.sha256(content).hexdigest() != asset.sha256
                ):
                    raise VideoRuleViolation("narration_audio_bytes_changed")
        self._verify_authority_snapshot(attempt)

    def _verify_authority_snapshot(self, attempt: PersistentAttempt) -> None:
        snapshot = attempt.input_snapshot or {}
        persisted = snapshot.get("authority")
        if not isinstance(persisted, dict) or not persisted:
            raise VideoRuleViolation("immutable_video_input_changed")
        source = self._repo.load_authoritative_group(attempt.owner_id, attempt.group_id)
        if not source.authority_snapshot:
            raise VideoRuleViolation("immutable_video_input_changed")
        _authorize(source)
        current = source.authority_snapshot
        if current != persisted:
            raise VideoRuleViolation("immutable_video_input_changed")

    def _require_prompt_compiler(self) -> VideoPromptCompiler:
        if self._prompt_compiler is None:
            raise VideoRuleViolation("video_prompt_compiler_not_configured")
        return self._prompt_compiler

    def _assigned(self, attempt: PersistentAttempt) -> EngineNode:
        if attempt.node_id is None:
            raise VideoRuleViolation("attempt has no assigned node")
        workflow = _workflow(attempt.request)
        return self._repo.get_assigned_node(attempt.attempt_id, workflow)


def _remote_stage(state: str) -> AttemptStage:
    return {
        "ACCEPTED": AttemptStage.SUBMITTED,
        "QUEUED": AttemptStage.SUBMITTED,
        "GENERATING": AttemptStage.RUNNING,
        "RUNNING": AttemptStage.RUNNING,
        "SUCCEEDED": AttemptStage.SUCCEEDED,
        "FAILED": AttemptStage.FAILED,
        "CANCELLED": AttemptStage.CANCELLED,
    }.get(state, AttemptStage.REMOTE_STATE_UNKNOWN)


def _inconclusive_reconciles(attempt: PersistentAttempt) -> int:
    """已持久化的无果对账轮次：inspect 失败、404 不可见或远端回执仍为未知。"""
    return sum(
        1
        for entry in attempt.retry_evidence
        if entry.startswith(("inspect:", "reconcile:"))
    )


def _response_stage(response: dict[str, object]) -> AttemptStage:
    if str(response.get("remote_acceptance", "")) == "UNKNOWN":
        return AttemptStage.REMOTE_STATE_UNKNOWN
    return _remote_stage(str(response.get("status", "ACCEPTED")))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _authorize(
    source: AuthoritativeGroupInput,
    *,
    idempotency_key: str | None = None,
    seed: int | None = None,
) -> GroupVideoGeneration:
    """权威输入 → 不可变 generation；无显式 seed 时按幂等键派生确定 seed。

    seed 为调用方显式覆盖（实验用，见 ManualH3Input.seed），优先级最高；
    未提供时沿用故事板快照的 seed，最后才按幂等键派生。
    """
    if seed is not None:
        resolved = seed
    elif source.seed is not None:
        resolved = source.seed
    elif idempotency_key is not None:
        resolved = _derived_seed(source.group_id, idempotency_key)
    else:
        resolved = None
    if not source.snapshot_frozen:
        raise VideoRuleViolation("immutable_project_snapshot_required")
    count = len(source.sentence_text_sha256)
    if (
        source.immutable_script is None
        or hashlib.sha256(source.immutable_script.encode()).hexdigest() != source.script_text_sha256
    ):
        raise VideoRuleViolation("immutable_script_hash_mismatch")
    fields = (
        source.narrated_text_sha256,
        source.sentence_durations_ms,
        source.narration_qc_passed,
        source.normalized_audio_assets,
        source.timeline_ranges_ms,
        source.raw_audio_assets,
    )
    if count == 0 or any(len(field) != count for field in fields):
        raise VideoRuleViolation("complete_real_narration_required")
    exact_timeline = True
    if any(
        len(field) != count
        for field in (
            source.sentence_sample_counts,
            source.sentence_sample_rates,
            source.timeline_ranges_frames,
        )
    ):
        raise VideoRuleViolation("measured_timeline_inconsistent")
    frame_cursor = 0
    for index in range(count):
        if (
            source.sentence_text_sha256[index] != source.narrated_text_sha256[index]
            or not source.narration_qc_passed[index]
            or source.normalized_audio_assets[index] is None
            or source.raw_audio_assets[index] is None
            or source.sentence_durations_ms[index] <= 0
        ):
            raise VideoRuleViolation("narration_qc_or_hash_inconsistent")
        start, end = source.timeline_ranges_ms[index]
        sample_count = source.sentence_sample_counts[index]
        sample_rate = source.sentence_sample_rates[index]
        frame_start, frame_end = source.timeline_ranges_frames[index]
        expected_start = round(Fraction(frame_start * 1000, sample_rate))
        expected_end = round(Fraction(frame_end * 1000, sample_rate))
        valid_timeline = (
            sample_count > 0
            and sample_rate > 0
            and frame_start == frame_cursor
            and frame_end - frame_start == sample_count
            and (start, end) == (expected_start, expected_end)
            and source.sentence_durations_ms[index]
            == round(Fraction(sample_count * 1000, sample_rate))
        )
        frame_cursor = frame_end
        if not valid_timeline:
            raise VideoRuleViolation("measured_timeline_inconsistent")
    indexes = source.group_sentence_indexes
    authoritative_indexes = source.authoritative_group_sentence_indexes
    if (
        not indexes
        or indexes[0] < 0
        or len(set(indexes)) != len(indexes)
        or indexes != tuple(range(indexes[0], indexes[-1] + 1))
        or indexes[-1] >= count
        or authoritative_indexes is None
        or indexes != authoritative_indexes
    ):
        raise VideoRuleViolation("narration_group_inconsistent")
    # 单句超 15 秒的句子只可能属于被阻止分组；按已确认规则仅在为包含
    # 该句的目标分组生成视频时拒绝，其余正常分组不受影响。
    if any(
        Fraction(source.sentence_sample_counts[index], source.sentence_sample_rates[index]) > 15
        for index in indexes
    ):
        raise VideoRuleViolation("single_sentence_exceeds_15000ms")
    start = source.timeline_ranges_ms[indexes[0]][0]
    end = source.timeline_ranges_ms[indexes[-1]][1]
    if (start, end) != (source.group_start_ms, source.group_end_ms):
        raise VideoRuleViolation("narration_group_timing_inconsistent")
    group_sample_count: int | None = None
    group_sample_rate: int | None = None
    if exact_timeline:
        group_sample_rate = source.group_sample_rate
        if (
            group_sample_rate is None
            or source.group_start_frame is None
            or source.group_end_frame is None
            or source.group_end_frame <= source.group_start_frame
        ):
            raise VideoRuleViolation("narration_group_timing_inconsistent")
        group_sample_count = source.group_end_frame - source.group_start_frame
        selected_ranges = tuple(source.timeline_ranges_frames[index] for index in indexes)
        if (
            source.group_start_frame != selected_ranges[0][0]
            or source.group_end_frame != selected_ranges[-1][1]
            or any(source.sentence_sample_rates[index] != group_sample_rate for index in indexes)
            or sum(source.sentence_sample_counts[index] for index in indexes) != group_sample_count
        ):
            raise VideoRuleViolation("narration_group_timing_inconsistent")
        exact_ms = round(Fraction(group_sample_count * 1000, group_sample_rate))
        if exact_ms != source.group_target_duration_ms:
            raise VideoRuleViolation("narration_group_timing_inconsistent")
    if (
        source.storyboard_kind != "GRID_SHEET"
        or not source.storyboard_is_current
        or source.applied_revision_id != source.storyboard_applied_revision_id
        or source.storyboard_generation_id is None
        or source.storyboard_asset_sha256 != source.storyboard_image.sha256
    ):
        raise VideoRuleViolation("current_grid_sheet_and_applied_revision_required")
    if source.shot_panel_images and tuple(
        item.shot_order for item in source.shot_panel_images
    ) != tuple(shot.order for shot in source.director_shots):
        raise VideoRuleViolation("shot_panel_reference_mismatch")
    return GroupVideoGeneration(
        source.generation_id,
        source.project_id,
        source.group_id,
        source.character_images,
        source.storyboard_image,
        source.prompt,
        source.group_target_duration_ms,
        source.aspect_ratio,
        resolved,
        source.workflow,
        source.storyboard_generation_id,
        source.storyboard_applied_revision_id,
        source.storyboard_asset_sha256,
        group_sample_count,
        group_sample_rate,
        shot_panel_images=source.shot_panel_images,
    )


def compile_video_prompt(
    source: AuthoritativeGroupInput,
    compiler: VideoPromptCompiler,
    *,
    attempt_id: UUID,
    idempotency_key: str | None = None,
    seed: int | None = None,
) -> CompiledVideoPrompt:
    """意图创建路径的共享编译入口：权威输入 → 编译器输入 → LLM 编译。"""
    generation = _authorize(source, idempotency_key=idempotency_key, seed=seed)
    # §15.14：AUTO 项目输出不确定性校验失败可修复重试 2 次（含临时故障共享 3 次预算）；
    # MANUAL 维持既有"一次修复重试"。
    strategy = (
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1
        if source.reference_policy is not None
        else compiler.strategy
    )
    return compiler.compile(
        _compiler_input(source, generation, strategy, attempt_id),
        max_repairs=2 if source.production_mode == "AUTO" else 1,
        context={
            "owner_id": str(source.owner_id),
            "project_id": str(source.project_id),
            "group_id": str(source.group_id),
            "attempt_id": str(attempt_id),
        },
    )


def _compiler_input(
    source: AuthoritativeGroupInput,
    generation: GroupVideoGeneration,
    strategy: VideoPromptStrategy,
    attempt_id: UUID,
) -> VideoPromptCompilerInput | SinglePanelPromptCompilerInput:
    if (
        not source.director_shots
        or not source.shot_sentence_links
        or not source.group_sentence_ids
        or not source.visual_style.strip()
    ):
        raise VideoRuleViolation("video_prompt_compiler_input_incomplete")
    covered = {link.sentence_id for link in source.shot_sentence_links}
    if any(sentence_id not in covered for sentence_id in source.group_sentence_ids):
        raise VideoRuleViolation("video_prompt_compiler_input_incomplete")
    references = generation.references
    if generation.shot_panel_images:
        if strategy is not VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1:
            raise VideoRuleViolation("single_panel_prompt_strategy_required")
        panel_references = references[len(source.character_images) :]
        return SinglePanelPromptCompilerInput(
            tuple(
                f"Picture {reference.ordinal}"
                for reference in references[: len(source.character_images)]
            ),
            tuple(
                SinglePanelReference(
                    f"Picture {bound.ordinal}",
                    panel.shot_order,
                    panel.source_storyboard_sha256,
                    panel.asset.sha256,
                    panel.derivation,
                )
                for panel, bound in zip(
                    generation.shot_panel_images, panel_references, strict=True
                )
            ),
            source.director_shots,
            _group_shot_ranges(source),
            generation.provider_duration_seconds,
            generation.aspect_ratio.value,
            source.visual_style,
            f"video-prompt:{attempt_id}",
        )
    if strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1:
        raise VideoRuleViolation("single_panel_reference_required")
    return VideoPromptCompilerInput(
        tuple(reference.prompt_label for reference in references[:-1]),
        references[-1].prompt_label,
        source.director_shots,
        _group_shot_ranges(source),
        generation.provider_duration_seconds,
        generation.aspect_ratio.value,
        source.visual_style,
        strategy,
        f"video-prompt:{attempt_id}",
    )


def _group_member_ranges(source: AuthoritativeGroupInput) -> dict[UUID, tuple[int, int]]:
    return {
        sentence_id: source.timeline_ranges_ms[source.group_sentence_indexes[position]]
        for position, sentence_id in enumerate(source.group_sentence_ids)
    }


def _group_shot_ranges(source: AuthoritativeGroupInput) -> tuple[ShotTimeRange, ...]:
    """组内分镜区间（毫秒）；触发编译与快照持久化共用同一推导。

    2026-10-16（monotone-projection-v1）：故事板生成记录携带投影算法键时直接
    使用持久化区间（与故事板角标同源同一份，H3 门禁已校验自洽）；无算法键的
    历史记录按 legacy min/max 重算，行为不变。
    """
    if source.shot_time_ranges_algorithm == SHOT_TIMING_PROJECTION_V1:
        persisted = source.storyboard_shot_ranges
        if len(persisted) != len(source.director_shots) or tuple(
            value.shot_order for value in persisted
        ) != tuple(shot.order for shot in source.director_shots):
            raise VideoRuleViolation("video_prompt_compiler_input_incomplete")
        return persisted
    try:
        return shot_time_ranges_ms(
            source.director_shots, source.shot_sentence_links, _group_member_ranges(source)
        )
    except ValueError as exc:
        raise VideoRuleViolation("video_prompt_compiler_input_incomplete") from exc


def _director_shot_block(shot: DirectorShot) -> dict[str, object]:
    return {
        "shot_id": str(shot.shot_id),
        "order": shot.order,
        "scene": shot.scene,
        "shot_size": shot.shot_size,
        "camera_viewpoint": shot.camera_viewpoint,
        "subject_action_outcome": shot.subject_action_outcome,
        "narrative_function": shot.narrative_function,
        "description": shot.description,
        "semantic_event": str(shot.semantic_event),
        "subject_focus": str(shot.subject_focus),
        "time_relation": str(shot.time_relation),
        "location_relation": str(shot.location_relation),
    }


def _compiler_input_block(
    source: AuthoritativeGroupInput, generation: GroupVideoGeneration
) -> dict[str, object]:
    """梯级重编译所需的确定性编译输入事实；随输入快照持久化（重编译不读库）。

    shot_sentence_links 用句子的全局下标与文本哈希对齐，不依赖句 UUID 是否随快照保留。
    """
    if (
        not source.director_shots
        or not source.shot_sentence_links
        or not source.group_sentence_ids
        or not source.visual_style.strip()
    ):
        raise VideoRuleViolation("video_prompt_compiler_input_incomplete")
    group_positions = {
        sentence_id: position for position, sentence_id in enumerate(source.group_sentence_ids)
    }
    links: list[dict[str, object]] = []
    for link in source.shot_sentence_links:
        position = group_positions.get(link.sentence_id)
        if position is None:
            raise VideoRuleViolation("video_prompt_compiler_input_incomplete")
        sentence_index = source.group_sentence_indexes[position]
        links.append(
            {
                "shot_id": str(link.shot_id),
                "sentence_index": sentence_index,
                "sentence_text_sha256": source.sentence_text_sha256[sentence_index],
            }
        )
    references = generation.references
    if generation.shot_panel_images:
        panel_references = references[len(source.character_images) :]
        return {
            "character_labels": [
                f"Picture {reference.ordinal}"
                for reference in references[: len(source.character_images)]
            ],
            "panels": [
                {
                    "label": f"Picture {bound.ordinal}",
                    "shot_order": panel.shot_order,
                    "source_storyboard_sha256": panel.source_storyboard_sha256,
                    "derivative_sha256": panel.asset.sha256,
                    "derivation": panel.derivation,
                }
                for panel, bound in zip(
                    generation.shot_panel_images, panel_references, strict=True
                )
            ],
            "visual_style": source.visual_style,
            "director_shots": [_director_shot_block(shot) for shot in source.director_shots],
            "shot_sentence_links": links,
            "shot_time_ranges": [
                {
                    "shot_order": value.shot_order,
                    "start_ms": value.start_ms,
                    "end_ms": value.end_ms,
                }
                for value in _group_shot_ranges(source)
            ],
        }
    return {
        "character_labels": [reference.prompt_label for reference in references[:-1]],
        "storyboard_label": references[-1].prompt_label,
        "visual_style": source.visual_style,
        "director_shots": [_director_shot_block(shot) for shot in source.director_shots],
        "shot_sentence_links": links,
        "shot_time_ranges": [
            {
                "shot_order": value.shot_order,
                "start_ms": value.start_ms,
                "end_ms": value.end_ms,
            }
            for value in _group_shot_ranges(source)
        ],
    }


def _compiler_input_from_snapshot(
    snapshot: dict[str, object],
    generation: GroupVideoGeneration,
    *,
    strategy: VideoPromptStrategy,
    attempt_id: UUID,
) -> VideoPromptCompilerInput | SinglePanelPromptCompilerInput:
    """仅从持久化快照重建编译输入（梯子重编译不读库）；旧快照缺块或块无效时显式失败。"""
    if generation.shot_panel_images:
        if strategy is not VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1:
            raise VideoRuleViolation("single_panel_prompt_strategy_required")
        return _single_panel_input_from_snapshot(snapshot, generation, attempt_id)
    if strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1:
        raise VideoRuleViolation("single_panel_reference_required")
    block = snapshot.get("compiler_input")
    if not isinstance(block, dict):
        raise VideoRuleViolation("video_compiler_input_snapshot_missing")
    try:
        character_labels = tuple(
            str(value) for value in _required_block_list(block, "character_labels")
        )
        storyboard_label = str(block["storyboard_label"])
        visual_style = str(block["visual_style"])
        shots = tuple(
            _shot_from_block(item) for item in _required_block_list(block, "director_shots")
        )
        ranges = tuple(
            _shot_range_from_block(item)
            for item in _required_block_list(block, "shot_time_ranges")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VideoRuleViolation("video_compiler_input_snapshot_invalid") from exc
    try:
        return VideoPromptCompilerInput(
            character_labels,
            storyboard_label,
            shots,
            ranges,
            generation.provider_duration_seconds,
            generation.aspect_ratio.value,
            visual_style,
            strategy,
            f"video-prompt:{attempt_id}",
        )
    except VideoPromptViolation as exc:
        raise VideoRuleViolation("video_compiler_input_snapshot_invalid") from exc


def _single_panel_input_from_snapshot(
    snapshot: dict[str, object],
    generation: GroupVideoGeneration,
    attempt_id: UUID,
) -> SinglePanelPromptCompilerInput:
    block = snapshot.get("compiler_input")
    if not isinstance(block, dict):
        raise VideoRuleViolation("video_compiler_input_snapshot_missing")
    try:
        character_labels = tuple(
            str(value) for value in _required_block_list(block, "character_labels")
        )
        visual_style = str(block["visual_style"])
        shots = tuple(
            _shot_from_block(item) for item in _required_block_list(block, "director_shots")
        )
        ranges = tuple(
            _shot_range_from_block(item)
            for item in _required_block_list(block, "shot_time_ranges")
        )
        panels = tuple(
            _panel_from_block(item) for item in _required_block_list(block, "panels")
        )
        return SinglePanelPromptCompilerInput(
            character_labels,
            panels,
            shots,
            ranges,
            generation.provider_duration_seconds,
            generation.aspect_ratio.value,
            visual_style,
            f"video-prompt:{attempt_id}",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise VideoRuleViolation("video_compiler_input_snapshot_invalid") from exc


def _required_block_list(block: dict[str, object], key: str) -> list[object]:
    value = block[key]
    if not isinstance(value, list):
        raise ValueError(f"compiler_input {key} must be a list")
    return value


def _shot_range_from_block(value: object) -> ShotTimeRange:
    if not isinstance(value, dict):
        raise ValueError("shot time range entries must be objects")
    return ShotTimeRange(
        int(str(value["shot_order"])),
        int(str(value["start_ms"])),
        int(str(value["end_ms"])),
    )


def _panel_from_block(value: object) -> SinglePanelReference:
    if not isinstance(value, dict):
        raise ValueError("single panel entries must be objects")
    return SinglePanelReference(
        str(value["label"]),
        int(str(value["shot_order"])),
        str(value["source_storyboard_sha256"]),
        str(value["derivative_sha256"]),
        str(value["derivation"]),
    )


def _shot_from_block(value: object) -> DirectorShot:
    if not isinstance(value, dict):
        raise ValueError("director shot entries must be objects")
    return DirectorShot(
        UUID(str(value["shot_id"])),
        int(str(value["order"])),
        str(value["scene"]),
        str(value["shot_size"]),
        str(value["camera_viewpoint"]),
        str(value["subject_action_outcome"]),
        str(value["narrative_function"]),
        str(value["description"]),
        DirectorSemanticEvent(str(value["semantic_event"])),
        DirectorSubjectFocus(str(value["subject_focus"])),
        DirectorTimeRelation(str(value["time_relation"])),
        DirectorLocationRelation(str(value["location_relation"])),
    )


def _derived_seed(group_id: UUID, idempotency_key: str) -> int:
    """无显式 seed 时的确定性 seed：由 (group_id, idempotency_key) 派生。

    2026-11-03（AB_REVIEW_AND_FOLLOWUPS_20261103 §3）：首个 attempt 在未配置 seed 时也要记录
    可复现的 seed，使 v3/v4 可单变量对照；不同幂等键仍得到不同 seed，因此“每次重新生成结果不同”
    的语义不变。不得改用 attempt_id：trigger() 在分配 attempt_id 之前就用同一快照做幂等比较，
    那样会让合法重试误报 idempotency_conflict。取值范围与 `_ladder_seed` 的 int 分支一致。
    """
    digest = hashlib.sha256(f"{group_id}:{idempotency_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**53)


def _ladder_seed(base: object, index: int, superseded: PersistentAttempt) -> int | str:
    """梯子确定性 seed 派生；结果必须过 GroupVideoGeneration 的 _validate_seed。

    base 为 int 时取 base+i（对 2**53 取模保证 JSON 安全整数域）；base 为 str 时
    以 f"{base}#qc{i}" 标签的 SHA-256 映射到合法字符串大整数域（字面值拼接含 "#"
    无法通过 _validate_seed 的十进制约束）；base 缺失时由 group/attempt 身份派生。
    """
    if isinstance(base, bool):
        base = None
    if isinstance(base, int):
        return (base + index) % (2**53)
    if isinstance(base, str):
        digest = hashlib.sha256(f"{base}#qc{index}".encode()).digest()
        return str(2**53 + int.from_bytes(digest[:8], "big") % (2**64 - 2**53))
    digest = hashlib.sha256(f"{superseded.group_id}:{superseded.attempt_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**53)


def _ladder_strategy(
    superseded: PersistentAttempt, index: int, default: VideoPromptStrategy
) -> VideoPromptStrategy | None:
    """i=4 切换策略；其余梯级沿用原编译策略（缺失时用配置策略）。

    2026-11-03（USER_DECISIONS §43）：配置策略属于 v3 输出合同（v3／v4）时，i=4 改用配置策略；
    kinestack_v6_port 要求按图写主体外观且无反网格规则，A/B 证实会引入换人与网格泄漏。
    其他配置下保持原 kinestack_v6_port 行为。
    """
    if index == _LADDER_STRATEGY_STEP:
        if default in V3_CONTRACT_STRATEGIES:
            return default
        try:
            return VideoPromptStrategy(_LADDER_STRATEGY_STEP_STRATEGY)
        except ValueError:
            return None
    snapshot = superseded.input_snapshot or {}
    block = snapshot.get("video_prompt")
    raw = block.get("strategy") if isinstance(block, dict) else None
    if isinstance(raw, str):
        try:
            return VideoPromptStrategy(raw)
        except ValueError:
            pass
    return default


def _content_qc_repair_reason(probe_evidence: dict[str, object] | None) -> str | None:
    """由 QC 证据的 content_qc 发现确定性生成重编译原因（时段与区域，不含敏感数据）。"""
    if not isinstance(probe_evidence, dict):
        return None
    block = probe_evidence.get("content_qc")
    if not isinstance(block, dict):
        return None
    findings = block.get("findings")
    if not isinstance(findings, list) or not findings:
        return None
    parts: list[str] = []
    for finding in findings[:3]:
        if not isinstance(finding, dict):
            continue
        try:
            start = float(str(finding["start_second"]))
            end = float(str(finding["end_second"]))
            columns = finding["bbox_columns"]
            rows = finding["bbox_rows"]
            area = float(str(finding["area_fraction"]))
            assert isinstance(columns, list) and isinstance(rows, list)
            parts.append(
                f"{start:.1f}s-{end:.1f}s bbox columns {columns[0]}-{columns[1]}"
                f" rows {rows[0]}-{rows[1]} area {area:.2f}"
            )
        except (KeyError, TypeError, ValueError, IndexError, AssertionError):
            continue
    if not parts:
        return None
    return (
        "previous generated video contained sustained black screen regions ("
        + "; ".join(parts)
        + "); regenerate so every shot renders vivid non-black visual content"
        " for its entire duration"
    )


def _compile_with_optional_reason(
    compiler: VideoPromptCompiler,
    value: VideoPromptCompilerInput | SinglePanelPromptCompilerInput,
    *,
    max_repairs: int,
    repair_reason: str | None,
    markers: list[str],
) -> CompiledVideoPrompt:
    """梯级重编译调用：QC 原因经 compile 的 repair_reason 形参透传给 LLM。"""
    if repair_reason:
        markers.append("auto_ladder:repair_reason:content_qc")
        return compiler.compile(value, max_repairs=max_repairs, repair_reason=repair_reason)
    return compiler.compile(value, max_repairs=max_repairs)


def _black_region_evidence(finding: BlackRegionFinding) -> dict[str, object]:
    return {
        "start_second": finding.start_second,
        "end_second": finding.end_second,
        "bbox_columns": list(finding.bbox_columns),
        "bbox_rows": list(finding.bbox_rows),
        "area_fraction": finding.area_fraction,
    }


def _black_region_message(findings: tuple[BlackRegionFinding, ...]) -> str:
    parts = [
        f"{finding.start_second:.1f}s-{finding.end_second:.1f}s bbox columns "
        f"{finding.bbox_columns[0]}-{finding.bbox_columns[1]} rows "
        f"{finding.bbox_rows[0]}-{finding.bbox_rows[1]} area {finding.area_fraction:.2f}"
        for finding in findings[:3]
    ]
    return f"sustained black regions detected: {'; '.join(parts)}"


def _request(
    generation: GroupVideoGeneration, attempt_id: UUID | None, *, prompt: str
) -> dict[str, object]:
    return {
        "attempt_id": str(attempt_id) if attempt_id else None,
        "workflow": {
            "capability": "video.reference_images_to_video",
            "model_key": generation.snapshot.model_key,
            "model_version": generation.snapshot.model_version,
            "workflow_id": generation.snapshot.workflow_id,
            "workflow_version": generation.snapshot.workflow_version,
            "workflow_manifest_sha256": generation.snapshot.workflow_manifest_sha256,
        },
        "prompt": prompt,
        "references": [
            {
                "binding_key": ref.binding_key,
                "ordinal": ref.ordinal,
                "asset_sha256": ref.asset.sha256,
            }
            for ref in generation.references
        ],
        "parameters": {
            "duration_seconds": generation.provider_duration_seconds,
            "aspect_ratio": generation.aspect_ratio.value,
            "resolution_preset": "preview",
            "seed": generation.seed,
        },
    }


def prepare_manual_video_intent(
    source: AuthoritativeGroupInput,
    attempt_id: UUID,
    *,
    compiled: CompiledVideoPrompt,
    idempotency_key: str | None = None,
    seed: int | None = None,
) -> tuple[dict[str, object], str, dict[str, object], str]:
    """Build the exact immutable worker input and provider request before persistence."""
    if not source.authority_snapshot:
        raise VideoRuleViolation("authoritative_video_input_required")
    generation = _authorize(source, idempotency_key=idempotency_key, seed=seed)
    expected = _compiler_input(source, generation, compiled.strategy, attempt_id)
    if compiled.input_fingerprint != expected.fingerprint:
        raise VideoRuleViolation("video_prompt_compiler_input_changed")
    input_snapshot = _input_snapshot(source, generation, compiled=compiled)
    request = _request(generation, attempt_id, prompt=compiled.text)
    return (
        input_snapshot,
        canonical_sha256(input_snapshot),
        request,
        canonical_sha256(request),
    )


def validate_reference_intent(
    source: AuthoritativeGroupInput, attempt: PersistentAttempt
) -> None:
    """Recheck the preview's authority, policy and exact provider references in one transaction."""
    snapshot = attempt.input_snapshot or {}
    if snapshot.get("authority") != source.authority_snapshot:
        raise VideoRuleViolation("immutable_video_input_changed")
    if source.reference_policy is None:
        if "reference_policy" in snapshot or "reference_input_sha256" in snapshot:
            raise VideoRuleViolation("video_reference_input_changed")
    else:
        expected_policy = {
            "strategy": source.reference_policy.strategy.value,
            "derivation": source.reference_policy.derivation_payload,
        }
        if (
            source.reference_input_sha256 is None
            or snapshot.get("reference_policy") != expected_policy
            or snapshot.get("reference_input_sha256") != source.reference_input_sha256
        ):
            raise VideoRuleViolation("video_reference_input_changed")
    recorded_seed = snapshot.get("seed")
    generation = _authorize(
        source,
        idempotency_key=attempt.idempotency_key,
        seed=recorded_seed if isinstance(recorded_seed, int) and not isinstance(recorded_seed, bool) else None,
    )
    request = _request(
        generation, attempt.attempt_id, prompt=str(attempt.request.get("prompt", ""))
    )
    if canonical_sha256(request) != attempt.request_sha256:
        raise VideoRuleViolation("video_reference_input_changed")


FROZEN_SINGLE_PANEL_REPLAY_V1 = "frozen-single-panel-replay-v1"


def prepare_frozen_single_panel_replay_intent(
    source: AuthoritativeGroupInput,
    attempt_id: UUID,
    *,
    replay_id: str,
    prompt: str,
    prompt_sha256: str,
) -> tuple[dict[str, object], str, dict[str, object], str]:
    """Build an isolated replay without invoking or emulating the real prompt compiler."""
    if not source.authority_snapshot:
        raise VideoRuleViolation("authoritative_video_input_required")
    replay_id = replay_id.strip()
    generation = _authorize(source, idempotency_key=replay_id)
    if not generation.shot_panel_images:
        raise VideoRuleViolation("single_panel_reference_required")
    if not replay_id or len(replay_id) > 128:
        raise VideoRuleViolation("frozen_replay_id_invalid")
    if not prompt.strip() or len(prompt) > 20000:
        raise VideoRuleViolation("frozen_replay_prompt_invalid")
    if (
        len(prompt_sha256) != 64
        or any(character not in "0123456789abcdef" for character in prompt_sha256)
        or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != prompt_sha256
    ):
        raise VideoRuleViolation("frozen_replay_prompt_hash_mismatch")
    request = _request(generation, attempt_id, prompt=prompt)
    fingerprint = canonical_sha256(
        {
            "version": FROZEN_SINGLE_PANEL_REPLAY_V1,
            "replay_id": replay_id,
            "prompt_sha256": prompt_sha256,
            "references": request["references"],
        }
    )
    input_snapshot = _input_snapshot(source, generation, compiled=None)
    input_snapshot["video_prompt"] = {
        "text": prompt,
        "strategy": FROZEN_SINGLE_PANEL_REPLAY_V1,
        "model": "precompiled-local",
        "provider_receipt": "local-precompiled:" + prompt_sha256,
        "repaired": False,
        "input_fingerprint": fingerprint,
    }
    input_snapshot["isolated_replay"] = {
        "version": FROZEN_SINGLE_PANEL_REPLAY_V1,
        "id": replay_id,
        "real_compiler_called": False,
        "automatic_resubmit_allowed": False,
    }
    return (
        input_snapshot,
        canonical_sha256(input_snapshot),
        request,
        canonical_sha256(request),
    )


def _input_snapshot(
    source: AuthoritativeGroupInput,
    generation: GroupVideoGeneration,
    *,
    compiled: CompiledVideoPrompt | None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "owner_id": str(source.owner_id),
        "project_id": str(source.project_id),
        "group_id": str(source.group_id),
        "generation_id": str(source.generation_id),
        "storyboard_generation_id": str(source.storyboard_generation_id),
        "character_assets": [
            {
                "id": str(asset.asset_id),
                "sha256": asset.sha256,
                "media_type": asset.media_type,
                "size_bytes": asset.size_bytes,
            }
            for asset in source.character_images
        ],
        "storyboard": {
            "id": str(source.storyboard_image.asset_id),
            "sha256": source.storyboard_image.sha256,
            "media_type": source.storyboard_image.media_type,
            "size_bytes": source.storyboard_image.size_bytes,
            "version": source.storyboard_version,
        },
        "applied_revision_id": str(source.applied_revision_id),
        "target_duration_ms": source.group_target_duration_ms,
        "target_sample_count": generation.target_sample_count,
        "target_sample_rate": generation.target_sample_rate,
        "aspect_ratio": source.aspect_ratio.value,
        "prompt": source.prompt,
        "seed": generation.seed,
        "workflow_manifest_raw_sha256": source.workflow.workflow_manifest_sha256,
        "workflow_parameters_sha256": source.workflow.manifest_parameters_sha256,
        "workflow_manifest": json.loads(canonical_json(source.workflow.manifest)),
        "authority": source.authority_snapshot or {},
        "sentences": [
            {
                "text_sha256": source.sentence_text_sha256[index],
                "raw_audio": {
                    "id": str(_required_asset(source.raw_audio_assets[index]).asset_id),
                    "sha256": _required_asset(source.raw_audio_assets[index]).sha256,
                    "media_type": _required_asset(source.raw_audio_assets[index]).media_type,
                    "size_bytes": _required_asset(source.raw_audio_assets[index]).size_bytes,
                },
                "normalized_audio": {
                    "id": str(_required_asset(source.normalized_audio_assets[index]).asset_id),
                    "sha256": _required_asset(source.normalized_audio_assets[index]).sha256,
                    "media_type": _required_asset(source.normalized_audio_assets[index]).media_type,
                    "size_bytes": _required_asset(source.normalized_audio_assets[index]).size_bytes,
                },
                "duration_ms": source.sentence_durations_ms[index],
                "timeline": list(source.timeline_ranges_ms[index]),
                "sample_count": source.sentence_sample_counts[index],
                "sample_rate_hz": source.sentence_sample_rates[index],
                "frame_range": list(source.timeline_ranges_frames[index]),
                "qc_passed": source.narration_qc_passed[index],
            }
            for index in range(len(source.sentence_text_sha256))
            if source.raw_audio_assets[index] is not None
            and source.normalized_audio_assets[index] is not None
        ],
        # logical_request 只承担工作流血缘；提示词事实由 video_prompt 块与 request_json 覆盖，
        # 这里恒置空串以保证同输入的确定性哈希（LLM 产物不确定）。
        "logical_request": _request(generation, None, prompt=""),
        # 整张故事板保留原重编译快照；逐格隔离重放明确没有真实编译输入，且禁止自动重提。
        "compiler_input": (
            None
            if generation.shot_panel_images and source.reference_policy is None
            else _compiler_input_block(source, generation)
        ),
    }
    if source.reference_policy is not None:
        if source.reference_input_sha256 is None:
            raise VideoRuleViolation("reference_input_fingerprint_required")
        snapshot["reference_policy"] = {
            "strategy": source.reference_policy.strategy.value,
            "derivation": source.reference_policy.derivation_payload,
        }
        snapshot["reference_input_sha256"] = source.reference_input_sha256
    if generation.shot_panel_images:
        snapshot["shot_panel_assets"] = [
            {
                "id": str(panel.asset.asset_id),
                "sha256": panel.asset.sha256,
                "media_type": panel.asset.media_type,
                "size_bytes": panel.asset.size_bytes,
                "shot_order": panel.shot_order,
                "source_storyboard_sha256": panel.source_storyboard_sha256,
                "derivation": panel.derivation,
            }
            for panel in generation.shot_panel_images
        ]
    if compiled is not None:
        snapshot["video_prompt"] = {
            "text": compiled.text,
            "strategy": compiled.strategy.value,
            "model": compiled.model,
            "provider_receipt": compiled.provider_receipt,
            "repaired": compiled.repaired,
            "input_fingerprint": compiled.input_fingerprint,
        }
    return snapshot


def _generation_from_snapshot(snapshot: dict[str, object]) -> GroupVideoGeneration:
    characters = snapshot.get("character_assets")
    storyboard = snapshot.get("storyboard")
    logical = snapshot.get("logical_request")
    manifest = snapshot.get("workflow_manifest")
    panels = snapshot.get("shot_panel_assets", [])
    if not isinstance(characters, list) or not isinstance(storyboard, dict):
        raise VideoRuleViolation("immutable_video_input_changed")
    if (
        not isinstance(logical, dict)
        or not isinstance(manifest, dict)
        or not isinstance(panels, list)
    ):
        raise VideoRuleViolation("immutable_video_input_changed")
    workflow = logical.get("workflow")
    if not isinstance(workflow, dict):
        raise VideoRuleViolation("immutable_video_input_changed")
    return GroupVideoGeneration(
        UUID(str(snapshot["generation_id"])),
        UUID(str(snapshot["project_id"])),
        UUID(str(snapshot["group_id"])),
        tuple(_snapshot_asset(value) for value in characters),
        _snapshot_asset(storyboard),
        str(snapshot["prompt"]),
        int(str(snapshot["target_duration_ms"])),
        AspectRatio(str(snapshot["aspect_ratio"])),
        _snapshot_seed(snapshot.get("seed")),
        ImmutableWorkflowSnapshot(
            str(workflow["model_key"]),
            str(workflow["model_version"]),
            str(workflow["workflow_id"]),
            str(workflow["workflow_version"]),
            str(workflow["workflow_manifest_sha256"]),
            "preview",
            manifest,
        ),
        UUID(str(snapshot["storyboard_generation_id"])),
        UUID(str(snapshot["applied_revision_id"])),
        str(storyboard["sha256"]),
        int(str(snapshot["target_sample_count"]))
        if snapshot.get("target_sample_count") is not None
        else None,
        int(str(snapshot["target_sample_rate"]))
        if snapshot.get("target_sample_rate") is not None
        else None,
        shot_panel_images=tuple(_snapshot_panel(value) for value in panels),
    )


def _snapshot_asset(value: object) -> MediaAssetRef:
    if not isinstance(value, dict):
        raise VideoRuleViolation("immutable_video_input_changed")
    return MediaAssetRef(
        UUID(str(value["id"])),
        str(value["sha256"]),
        str(value["media_type"]),
        int(str(value["size_bytes"])),
    )


def _snapshot_panel(value: object) -> ShotPanelImage:
    if not isinstance(value, dict):
        raise VideoRuleViolation("immutable_video_input_changed")
    return ShotPanelImage(
        _snapshot_asset(value),
        int(str(value["shot_order"])),
        str(value["source_storyboard_sha256"]),
        str(value["derivation"]),
    )


def _snapshot_seed(value: object) -> int | str | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return value
    return None


def _required_asset(value: MediaAssetRef | None) -> MediaAssetRef:
    if value is None:
        raise VideoRuleViolation("raw_and_normalized_audio_required")
    return value


def _workflow(request: dict[str, object]) -> ImmutableWorkflowSnapshot:
    workflow = request["workflow"]
    assert isinstance(workflow, dict)
    return ImmutableWorkflowSnapshot(
        str(workflow["model_key"]),
        str(workflow["model_version"]),
        str(workflow["workflow_id"]),
        str(workflow["workflow_version"]),
        str(workflow["workflow_manifest_sha256"]),
        "preview",
        {},
    )


def _verify_media(
    evidence: VideoMediaEvidence,
    target_ms: int,
    *,
    native_size: tuple[int, int],
) -> None:
    if evidence.media_type != "video/mp4" or evidence.video_tracks != 1:
        raise VideoRuleViolation("H3 output must be an MP4 with exactly one video track")
    if "mp4" not in evidence.container_names:
        raise VideoRuleViolation("ffprobe container evidence must include MP4")
    if evidence.audio_tracks < 1:
        raise VideoRuleViolation("H3 native audio track evidence is required")
    if abs(evidence.fps - 24.0) > 0.01:
        raise VideoRuleViolation("H3 output must be 24fps")
    video_duration_ms = evidence.video_duration_ms
    if video_duration_ms is None and evidence.video_frame_count is not None:
        video_duration_ms = round(evidence.video_frame_count / evidence.fps * 1000)
    if video_duration_ms is None:
        video_duration_ms = evidence.duration_ms
    if video_duration_ms < target_ms:
        raise VideoRuleViolation(
            "H3 output is shorter than measured narration; regenerate this group"
        )
    frame_tolerance_ms = max(42, round(1000 / evidence.fps) + 2)
    if (
        evidence.audio_start_ms is None
        or evidence.audio_duration_ms is None
        or evidence.audio_end_ms is None
        or not evidence.audio_codec
        or evidence.audio_duration_ms <= frame_tolerance_ms
        or evidence.audio_start_ms > frame_tolerance_ms
        or evidence.audio_end_ms + frame_tolerance_ms < video_duration_ms
    ):
        raise VideoRuleViolation("H3 native audio must cover the complete video within one frame")
    expected = native_size
    if (evidence.width, evidence.height) != expected:
        raise VideoRuleViolation(_RESOLUTION_MISMATCH_RULE)


def _required_text(value: dict[str, object], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
        raise VideoRuleViolation(f"invalid provider receipt {key}")
    return raw.strip()


def _required_positive_int(value: dict[str, object], key: str) -> int:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise VideoRuleViolation(f"invalid provider receipt {key}")
    return raw


def _provider_task_key(attempt: PersistentAttempt) -> str:
    return attempt.remote_id or str(attempt.attempt_id)


def _validate_stage_receipt(receipt: dict[str, object], asset: MediaAssetRef) -> None:
    _required_text(receipt, "id")
    if (
        _required_text(receipt, "schema") != "kinestack.staged-asset.v1"
        or _required_text(receipt, "status") not in {"STORED", "EXISTS"}
        or _required_text(receipt, "sha256") != asset.sha256
        or _required_positive_int(receipt, "size_bytes") != asset.size_bytes
        or _required_text(receipt, "media_type") != asset.media_type
    ):
        raise VideoRuleViolation("staged_asset_integrity_mismatch")


def _validate_submit_receipt(receipt: dict[str, object], attempt: PersistentAttempt) -> str:
    if _required_text(receipt, "schema") != "kinestack.execution.v1":
        raise VideoRuleViolation("invalid submit receipt schema")
    if _required_text(receipt, "status") not in {"ACCEPTED", "QUEUED", "GENERATING", "RUNNING"}:
        raise VideoRuleViolation("invalid submit receipt status")
    echoed = receipt.get("attempt_id")
    remote = receipt.get("id") or receipt.get("task_id")
    if echoed is not None and echoed != str(attempt.attempt_id):
        raise VideoRuleViolation("submit receipt attempt mismatch")
    if remote is not None:
        if not isinstance(remote, str) or not remote.strip():
            raise VideoRuleViolation("invalid provider task id")
        return remote.strip()
    if echoed == str(attempt.attempt_id):
        return str(attempt.attempt_id)
    raise VideoRuleViolation("submit receipt has no correlated task identity")


def _validate_task_receipt(receipt: dict[str, object], attempt: PersistentAttempt) -> None:
    if _required_text(receipt, "schema") != "kinestack.execution.v1":
        raise VideoRuleViolation("invalid inspect receipt schema")
    identity = receipt.get("id") or receipt.get("task_id") or receipt.get("attempt_id")
    if identity != _provider_task_key(attempt):
        raise VideoRuleViolation("inspect receipt task mismatch")
    if _required_text(receipt, "status") not in {
        "ACCEPTED",
        "QUEUED",
        "GENERATING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "UNKNOWN",
    }:
        raise VideoRuleViolation("invalid inspect receipt status")


def _durable_candidate(attempt: PersistentAttempt) -> bool:
    workflow = attempt.request.get("workflow")
    return isinstance(workflow, dict) and uses_durable_execution_contract(
        str(workflow.get("workflow_id", ""))
    )


def _cancel_response_stage(
    receipt: dict[str, object], attempt: PersistentAttempt
) -> AttemptStage:
    stage = _response_stage(receipt)
    if (
        _durable_candidate(attempt)
        and attempt.stage is AttemptStage.RUNNING
        and stage is AttemptStage.SUBMITTED
    ):
        return AttemptStage.RUNNING
    return stage


def _pending_cancel_action(
    receipt: dict[str, object], attempt: PersistentAttempt
) -> RequestedAction | None:
    if _durable_candidate(attempt) and _response_stage(receipt) not in {
        AttemptStage.CANCELLED, AttemptStage.SUCCEEDED, AttemptStage.FAILED
    }:
        return RequestedAction.CANCEL
    return None


def _validate_cancel_receipt(receipt: dict[str, object], attempt: PersistentAttempt) -> None:
    _validate_task_receipt(receipt, attempt)
    if not _durable_candidate(attempt) and _required_text(receipt, "status") not in {
        "CANCELLED", "SUCCEEDED", "FAILED"
    }:
        raise VideoRuleViolation("invalid cancel receipt status")


def _validate_output_receipt(
    receipt: dict[str, object], attempt: PersistentAttempt, task_key: str
) -> None:
    _required_text(receipt, "id")
    if _required_text(receipt, "schema") != "kinestack.output.v1":
        raise VideoRuleViolation("invalid output receipt schema")
    identity = receipt.get("task_id") or receipt.get("attempt_id")
    if identity != task_key:
        raise VideoRuleViolation("output receipt task mismatch")
    digest = _required_text(receipt, "sha256")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise VideoRuleViolation("invalid output sha256")
    _required_positive_int(receipt, "size_bytes")
    if _required_text(receipt, "media_type") != "video/mp4":
        raise VideoRuleViolation("invalid output media type")


def _validate_archive_receipt(receipt: dict[str, object], attempt: PersistentAttempt) -> None:
    identity = receipt.get("task_id") or receipt.get("attempt_id")
    if (
        _required_text(receipt, "schema") != "kinestack.archive-confirmation.v1"
        or _required_text(receipt, "status") != "CONFIRMED"
        or identity != _provider_task_key(attempt)
        or receipt.get("confirmed") is not True
    ):
        raise VideoRuleViolation("invalid archive confirmation receipt")


def _validate_delete_receipt(receipt: dict[str, object], staged_id: str) -> None:
    if (
        _required_text(receipt, "schema") != "kinestack.staged-asset-deletion.v1"
        or _required_text(receipt, "id") != staged_id
        or _required_text(receipt, "status") not in {"DELETED", "NOT_FOUND"}
    ):
        raise VideoRuleViolation("invalid staged delete receipt")
