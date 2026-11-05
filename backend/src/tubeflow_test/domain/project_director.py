from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tubeflow_test.domain.narration import is_nonvisual_script_separator
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    DirectorPlanRevision,
    DirectorShot,
    ReviewState,
    ShotSentenceLink,
)


class ProjectDirectorRunStatus(StrEnum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    RUNNING = "RUNNING"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


# 提交后 worker 丢失（远端调用可能已发出的未知态）的稳定失败码：advance 的
# stale 单元回收与恢复创建前的孤儿单元回收共用该码（2026-10-14）。
PROJECT_DIRECTOR_WORKER_LOST_FAILURE = "worker_lost_after_call_started"


class ProjectDirectorUnitKind(StrEnum):
    GLOBAL_PLAN = "GLOBAL_PLAN"
    SHOT_PLAN = "SHOT_PLAN"
    NONVISUAL_SEPARATOR = "NONVISUAL_SEPARATOR"


class ProjectDirectorUnitStatus(StrEnum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    LEASED = "LEASED"
    SUBMITTING = "SUBMITTING"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ProjectDirectorTrigger(StrEnum):
    NARRATION_CONFIRMATION = "NARRATION_CONFIRMATION"
    MANUAL_REGENERATE = "MANUAL_REGENERATE"
    # §15.9（仅 AUTO）：一键生产恢复后的新一轮运行；沿用 MANUAL_REGENERATE 的前缀复用机制。
    AUTO_RESUME = "AUTO_RESUME"


class MaterializationStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class ProjectDirectorSentenceSnapshot:
    sentence_id: UUID
    sequence: int
    text: str
    text_sha256: str

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.text.strip() or len(self.text_sha256) != 64:
            raise ValueError("project director sentence snapshot is invalid")


@dataclass(frozen=True, slots=True)
class ProjectDirectorInputSnapshot:
    owner_id: UUID
    project_id: UUID
    snapshot_sha256: str
    snapshot_revision: int
    sentences: tuple[ProjectDirectorSentenceSnapshot, ...]
    characters: tuple[CharacterAssetSnapshot, ...]
    visual_style: str
    locale: str
    aspect_ratio: str

    def __post_init__(self) -> None:
        if len(self.snapshot_sha256) != 64 or self.snapshot_revision < 1:
            raise ValueError("project director snapshot identity is invalid")
        if not self.sentences or [item.sequence for item in self.sentences] != list(
            range(1, len(self.sentences) + 1)
        ):
            raise ValueError("project director sentences must be ordered and contiguous")
        if len({item.sentence_id for item in self.sentences}) != len(self.sentences):
            raise ValueError("project director sentence ids must be unique")
        if not self.characters or not self.visual_style.strip():
            raise ValueError("project director visual context is incomplete")
        if not self.locale.strip() or self.aspect_ratio not in {"16:9", "9:16"}:
            raise ValueError("project director locale or aspect ratio is invalid")


@dataclass(frozen=True, slots=True)
class ProjectDirectorRun:
    run_id: UUID
    owner_id: UUID
    project_id: UUID
    trigger: ProjectDirectorTrigger
    idempotency_key: str
    canonical_input_sha256: str
    input_snapshot: ProjectDirectorInputSnapshot
    model: str
    parameters: dict[str, str | int | float | bool]
    status: ProjectDirectorRunStatus
    total_units: int
    created_at: datetime
    state_revision: int = 0
    revision_id: UUID | None = None
    failure_code: str | None = None
    retry_blocked: bool = False
    recovery_source_run_id: UUID | None = None
    auto_retries_used: int = 0

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.canonical_input_sha256) != 64:
            raise ValueError("project director run identity is invalid")
        if self.total_units < 1:
            raise ValueError("project director run must contain at least one unit")
        if self.state_revision < 0 or not self.model.strip():
            raise ValueError("project director run state is invalid")
        if self.auto_retries_used < 0:
            raise ValueError("project director run retry counter is invalid")
        if self.status is ProjectDirectorRunStatus.SUCCEEDED and self.revision_id is None:
            raise ValueError("successful project director run must reference a revision")
        if (
            self.recovery_source_run_id is not None
            and self.trigger
            not in {ProjectDirectorTrigger.MANUAL_REGENERATE, ProjectDirectorTrigger.AUTO_RESUME}
        ):
            raise ValueError("project director recovery must be manually triggered")


@dataclass(frozen=True, slots=True)
class ProjectDirectorUnit:
    unit_id: UUID
    run_id: UUID
    owner_id: UUID
    project_id: UUID
    ordinal: int
    kind: ProjectDirectorUnitKind
    sentences: tuple[ProjectDirectorSentenceSnapshot, ...]
    context_before: tuple[ProjectDirectorSentenceSnapshot, ...]
    context_after: tuple[ProjectDirectorSentenceSnapshot, ...]
    provider_idempotency_key: str
    canonical_request_sha256: str
    prompt_ref: str
    prompt_sha256: str
    prompt_content: str
    required_locale: str
    model: str
    parameters: dict[str, str | int | float | bool]
    status: ProjectDirectorUnitStatus
    created_at: datetime
    state_revision: int = 0
    lease_token: UUID | None = None
    lease_until: datetime | None = None
    provider_receipt: str | None = None
    raw_response_sha256: str | None = None
    shots: tuple[DirectorShot, ...] = ()
    sentence_links: tuple[ShotSentenceLink, ...] = ()
    failure_code: str | None = None
    reused_from_unit_id: UUID | None = None
    attempt_count: int = 0
    last_failure_code: str | None = None
    next_attempt_at: datetime | None = None
    plan_document: str | None = None
    plan_document_sha256: str | None = None
    # 最近一次落库的安全诊断（不含模型正文，§40.1）：重试排程时为拒收诊断，
    # 确定性合并完成时为合并审计（§50）。只读展示与重试提示使用，不入任何哈希。
    response_diagnostics: dict[str, str | int | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("project director unit ordinal is invalid")
        if len(self.provider_idempotency_key) != 64:
            raise ValueError("project director provider idempotency key must be SHA-256")
        if len(self.canonical_request_sha256) != 64 or len(self.prompt_sha256) != 64:
            raise ValueError("project director unit hashes are invalid")
        if (
            not self.prompt_ref
            or not self.prompt_content
            or not self.required_locale.strip()
            or not self.model
        ):
            raise ValueError("project director unit provider input is incomplete")
        if self.state_revision < 0 or self.attempt_count < 0:
            raise ValueError("project director unit revision or attempt count is invalid")
        if self.plan_document is not None and not self.plan_document.strip():
            raise ValueError("project director plan document must be non-empty")
        if self.plan_document_sha256 is not None and len(self.plan_document_sha256) != 64:
            raise ValueError("project director plan document hash must be SHA-256")
        if (
            self.reused_from_unit_id is not None
            and self.status is not ProjectDirectorUnitStatus.SUCCEEDED
        ):
            raise ValueError("reused project director unit must already be successful")
        if self.kind is ProjectDirectorUnitKind.GLOBAL_PLAN:
            self._validate_global_plan()
        elif self.kind is ProjectDirectorUnitKind.NONVISUAL_SEPARATOR:
            self._validate_nonvisual_separator()
        else:
            self._validate_shot_plan()

    def _validate_global_plan(self) -> None:
        if self.sentences:
            raise ValueError("global plan project director unit must not own sentences")
        if self.status is ProjectDirectorUnitStatus.SUCCEEDED and (
            self.plan_document is None or self.plan_document_sha256 is None
        ):
            raise ValueError("successful global plan unit must persist its plan document")
        if self.shots or self.sentence_links:
            raise ValueError("global plan project director unit must not contain shots")

    def _validate_nonvisual_separator(self) -> None:
        if len(self.sentences) != 1 or not is_nonvisual_script_separator(self.sentences[0].text):
            raise ValueError("nonvisual project director unit must own one separator sentence")
        if self.status is ProjectDirectorUnitStatus.SUCCEEDED and (
            self.shots or self.sentence_links
        ):
            raise ValueError("nonvisual project director unit must only preserve lineage")

    def _validate_shot_plan(self) -> None:
        if not 1 <= len(self.sentences) <= 8:
            raise ValueError("shot plan project director unit must own 1 to 8 sentences")
        sequences = [item.sequence for item in self.sentences]
        if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
            raise ValueError("shot plan project director unit sentences must be contiguous")
        if any(is_nonvisual_script_separator(item.text) for item in self.sentences):
            raise ValueError("shot plan project director unit must not own a separator")
        if self.status is not ProjectDirectorUnitStatus.SUCCEEDED:
            return
        if not 1 <= len(self.shots) <= min(2 * len(self.sentences), 8):
            raise ValueError("successful shot plan unit shot count is outside its budget")
        if [item.order for item in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("project director unit shot order is invalid")
        shot_ids = {item.shot_id for item in self.shots}
        if len(shot_ids) != len(self.shots):
            raise ValueError("project director unit shot ids must be unique")
        if {item.shot_id for item in self.sentence_links} != shot_ids:
            raise ValueError("every project director unit shot must link at least one sentence")
        unit_sentence_ids = {item.sentence_id for item in self.sentences}
        if not {item.sentence_id for item in self.sentence_links}.issubset(unit_sentence_ids):
            raise ValueError("project director unit links a sentence outside its unit")
        if {item.sentence_id for item in self.sentence_links} != unit_sentence_ids:
            raise ValueError("project director unit must cover every unit sentence")


@dataclass(frozen=True, slots=True)
class ProjectDirectorRunProgress:
    succeeded_units: int
    inflight_units: int
    retry_pending_units: int
    unsent_units: int
    failed_unit_ordinal: int | None
    failed_unit_kind: ProjectDirectorUnitKind | None
    auto_retries_used: int
    auto_retry_ceiling: int

    def __post_init__(self) -> None:
        counters = (
            self.succeeded_units,
            self.inflight_units,
            self.retry_pending_units,
            self.unsent_units,
            self.auto_retries_used,
            self.auto_retry_ceiling,
        )
        if any(value < 0 for value in counters):
            raise ValueError("project director run progress counters must be non-negative")
        if (self.failed_unit_ordinal is None) != (self.failed_unit_kind is None):
            raise ValueError("project director run progress failure identity is inconsistent")
        if self.failed_unit_ordinal is not None and self.failed_unit_ordinal < 1:
            raise ValueError("project director run progress failed ordinal is invalid")


@dataclass(frozen=True, slots=True)
class ProjectDirectorConflict:
    code: str
    sentence_ids: tuple[UUID, ...]
    shot_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("project director conflict code is required")


@dataclass(frozen=True, slots=True)
class ProjectDirectorRevision:
    revision_id: UUID
    owner_id: UUID
    project_id: UUID
    run_id: UUID
    revision_number: int
    snapshot_sha256: str
    snapshot_revision: int
    sentence_ids: tuple[UUID, ...]
    shots: tuple[DirectorShot, ...]
    sentence_links: tuple[ShotSentenceLink, ...]
    quality_conflicts: tuple[ProjectDirectorConflict, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.revision_number < 1 or len(self.snapshot_sha256) != 64:
            raise ValueError("project director revision identity is invalid")
        if not self.shots or [item.order for item in self.shots] != list(
            range(1, len(self.shots) + 1)
        ):
            raise ValueError("project director revision shot order is invalid")
        if not self.sentence_ids or len(set(self.sentence_ids)) != len(self.sentence_ids):
            raise ValueError("project director revision sentence ids are invalid")
        shot_ids = {item.shot_id for item in self.shots}
        if len(shot_ids) != len(self.shots):
            raise ValueError("project director revision shot ids must be unique")
        if {item.shot_id for item in self.sentence_links} != shot_ids:
            raise ValueError("every project director shot must link at least one sentence")
        if {item.sentence_id for item in self.sentence_links} != set(self.sentence_ids):
            raise ValueError("project director revision must cover every frozen sentence")
        if not {item.sentence_id for item in self.sentence_links}.issubset(set(self.sentence_ids)):
            raise ValueError("project director revision links an unknown sentence")


@dataclass(frozen=True, slots=True)
class ProjectDirectorGroupContext:
    group_id: UUID
    sequence: int
    sentence_ids: tuple[UUID, ...]
    # 2026-10-16：组内句级实测时间轴（与 sentence_ids 位置对齐），供
    # monotone-projection-v1 可行性检查；空 = 未携带（历史调用方跳过检查）。
    sentence_ranges_ms: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if self.sequence < 1 or not self.sentence_ids:
            raise ValueError("project director group context is invalid")
        if self.sentence_ranges_ms and (
            len(self.sentence_ranges_ms) != len(self.sentence_ids)
            or any(start < 0 or end <= start for start, end in self.sentence_ranges_ms)
        ):
            raise ValueError("project director group sentence ranges are invalid")


@dataclass(frozen=True, slots=True)
class ProjectDirectorSourceShotLink:
    source_shot_id: UUID
    candidate_shot_id: UUID


@dataclass(frozen=True, slots=True)
class ProjectDirectorGroupCandidate:
    candidate_id: UUID
    group_id: UUID
    group_sequence: int
    shots: tuple[DirectorShot, ...]
    sentence_links: tuple[ShotSentenceLink, ...]
    source_shot_links: tuple[ProjectDirectorSourceShotLink, ...]
    conflicts: tuple[ProjectDirectorConflict, ...]
    review_state: ReviewState
    final_revision: DirectorPlanRevision | None = None

    def __post_init__(self) -> None:
        candidate_ids = {item.shot_id for item in self.shots}
        lineage_candidate_ids = {item.candidate_shot_id for item in self.source_shot_links}
        if candidate_ids != lineage_candidate_ids:
            raise ValueError("every candidate shot must retain source shot lineage")
        # §24（2026-10-14）：确定性相邻合并使一个候选可保留多个来源镜头（多父血缘）；
        # 校验放宽为「每候选至少 1 来源（上方集合相等）且 (source, candidate) 对唯一」。
        lineage_pairs = {
            (item.source_shot_id, item.candidate_shot_id) for item in self.source_shot_links
        }
        if len(lineage_pairs) != len(self.source_shot_links):
            raise ValueError("candidate source shot lineage pairs must be unique")


@dataclass(frozen=True, slots=True)
class ProjectDirectorMaterialization:
    materialization_id: UUID
    owner_id: UUID
    project_id: UUID
    preplan_revision_id: UUID
    snapshot_sha256: str
    snapshot_revision: int
    algorithm_version: str
    canonical_input_sha256: str
    status: MaterializationStatus
    candidates: tuple[ProjectDirectorGroupCandidate, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if len(self.snapshot_sha256) != 64 or len(self.canonical_input_sha256) != 64:
            raise ValueError("project director materialization hashes are invalid")
        if not self.algorithm_version.strip():
            raise ValueError("project director materialization is incomplete")
        # REVIEW_REQUIRED 允许零候选（映射全数失败=需人工处理）；其余状态必须有候选。
        if not self.candidates and self.status is not MaterializationStatus.REVIEW_REQUIRED:
            raise ValueError("project director materialization is incomplete")
        sequences = [item.group_sequence for item in self.candidates]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("project director materialization groups must be ordered")
