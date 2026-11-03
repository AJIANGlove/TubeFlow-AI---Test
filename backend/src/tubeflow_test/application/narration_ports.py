from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import (
    AudioAsset,
    AudioQcResult,
    NarratedSentence,
    NarrationAttempt,
    NarrationAttemptState,
    NarrationGroup,
    ScriptSentence,
    ScriptVersion,
    TimelineEntry,
)
from tubeflow_test.sentence_split.domain import SentenceSplitPlan

# EL 远端 id 前缀：`NarrationAttempt.remote_id` 存 `el:{request-id}`，
# 与 CosyVoice 的远端任务 id 明确区分（拼接链只接受 EL 前缀）。
EL_REQUEST_ID_PREFIX = "el:"



@dataclass(frozen=True, slots=True)
class VoiceReference:
    """配音源音色引用（§23.1）。

    `provider` 判别分支：cosyvoice 用 `voice_version_id` + 参考 ZIP；
    elevenlabs 用 `cloned_voice_id` + 源音频哈希（无 reference ZIP）。
    """

    voice_version_id: UUID | None
    reference_zip: bytes
    reference_sha256: str
    provider: str = "cosyvoice"
    cloned_voice_id: UUID | None = None
    cloned_voice_name: str = ""


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    project_id: UUID
    owner_id: UUID
    voice_version_id: UUID | None
    sentence: ScriptSentence
    idempotency_key: str
    language: SupportedLocale
    reference_zip: bytes
    reference_sha256: str
    provider_call_id: str
    emotion: str = "calm"
    expected_output_names: tuple[str, ...] = ()
    parent_attempt_id: str | None = None
    original_complete_manifest: tuple[str, ...] = ()
    original_manifest_sha256: str | None = None
    original_zip_asset_id: str | None = None
    retry_subset_manifest: tuple[str, ...] = ()
    retry_subset_manifest_sha256: str | None = None
    # §23.1：配音源路由判别字段与 EL 载荷（逻辑克隆音色 id）。
    provider: str = "cosyvoice"
    cloned_voice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    task_id: str
    state: str
    output_zip: bytes | None
    # §23.7：EL 响应头 request-id（对账凭证，同时用于上一句的 stitching 链）。
    provider_request_id: str | None = None
    # 批量路径的逐句远端 id：(句序, 远端 id)。EL 逐句合成但共用一次批量簿记，
    # 必须逐句回传，拼接链与 `provider_request_id` 对账列才拿得到事实。
    provider_request_ids: tuple[tuple[int, str], ...] = ()
    # 批量路径的逐句确定性失败：(句序, 错误码)。配合 `NarrationBatchFailure` 使用，
    # 让批内已成功的句仍能落盘，不被整批重投（避免重复向 Provider 付费）。
    failed_sequences: tuple[int, ...] = ()
    errors: tuple[tuple[int, str], ...] = ()


class NarrationBatchFailure(DomainError):
    """批量提交部分失败：ZIP 与逐句远端 id 对成功句有效，权威失败码在 `errors`。"""

    def __init__(self, message: str, result: SynthesisResult | None = None) -> None:
        super().__init__("", message)
        self.result = result


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    payload: bytes
    manifest: dict[str, object]


@dataclass(frozen=True, slots=True)
class NarrationJob:
    job_id: str
    owner_id: UUID
    project_id: UUID
    sentence_sequences: tuple[int, ...]
    state: str
    failure_sequences: tuple[int, ...] = ()
    # AUTO 自动重投的持久化调度时间戳（ISO，含时区）；None 表示可立即分发。
    not_before: str | None = None


def not_before_pending(value: str | None, now: datetime) -> bool:
    """持久化的 not_before 时间戳是否仍未到期；缺失或无法解析时视为可立即分发。"""
    if not value:
        return False
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return False
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant > now


def previous_request_ids(
    attempts: tuple[NarrationAttempt, ...], *, limit: int = 3
) -> tuple[str, ...]:
    """EL request stitching 链（§23.7）：取紧邻前置已 COMPLETED 句的 provider request-id。

    按句序排序、按 request-id 去重（同一句重试不产生重复条目），最多 3 条（EL 上限）；
    仅接受 `el:` 前缀的远端 id（CosyVoice 的远端 id 语义不同，不得混入 EL 链）。
    断链、换 key、跨批自然表现为候选不足，调用方按实际长度发送。
    """
    ordered: list[tuple[int, str]] = []
    seen: set[str] = set()
    for attempt in attempts:
        if attempt.state is not NarrationAttemptState.COMPLETED:
            continue
        request_id = (attempt.remote_id or "").strip()
        if not request_id.startswith(EL_REQUEST_ID_PREFIX):
            continue
        if request_id in seen:
            continue
        seen.add(request_id)
        ordered.append((attempt.sentence_sequence, request_id))
    if limit <= 0:
        return ()
    ordered.sort(key=lambda item: item[0])
    return tuple(item[1] for item in ordered[-limit:])


class CosyVoiceProviderPort(Protocol):
    async def submit(self, request: SynthesisRequest) -> SynthesisResult: ...
    async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult: ...


class NarrationRepositoryPort(Protocol):
    def find_by_idempotency_key(self, owner_id: UUID, key: str) -> NarratedSentence | None: ...
    def attempt(self, owner_id: UUID, key: str) -> NarrationAttempt | None: ...
    def list_attempts(self, owner_id: UUID, project_id: UUID) -> tuple[NarrationAttempt, ...]: ...
    def save_attempt(self, attempt: NarrationAttempt) -> None: ...
    def claim_submission(
        self, attempt: NarrationAttempt, claim_token: str, lease_until: datetime
    ) -> NarrationAttempt | None: ...
    def record_event(self, attempt: NarrationAttempt, event: str) -> None: ...
    def save_completed(self, attempt: NarrationAttempt, narration: NarratedSentence) -> None: ...
    def list_narrated(self, owner_id: UUID, project_id: UUID) -> tuple[NarratedSentence, ...]: ...
    def save_timeline(
        self,
        owner_id: UUID,
        project_id: UUID,
        timeline: tuple[TimelineEntry, ...],
        groups: tuple[NarrationGroup, ...],
    ) -> None: ...
    def get_script(self, owner_id: UUID, project_id: UUID) -> ScriptVersion | None: ...
    def save_script(self, owner_id: UUID, project_id: UUID, script: ScriptVersion) -> None: ...
    def get_timeline(self, owner_id: UUID, project_id: UUID) -> tuple[TimelineEntry, ...]: ...
    def get_groups(self, owner_id: UUID, project_id: UUID) -> tuple[NarrationGroup, ...]: ...
    def get_job(self, owner_id: UUID, project_id: UUID) -> NarrationJob | None: ...
    def pending_jobs(self) -> tuple[NarrationJob, ...]: ...
    def mark_job_dispatched(self, job_id: str) -> None: ...
    def finish_job(self, job_id: str, state: str, failures: tuple[int, ...]) -> None: ...
    def save_provider_checkpoint(self, attempt: NarrationAttempt, asset: AudioAsset) -> None: ...
    def provider_checkpoint(self, owner_id: UUID, key: str) -> AudioAsset | None: ...
    def set_checkpoint_ledger(
        self,
        attempt: NarrationAttempt,
        state: str,
        asset: AudioAsset | None = None,
        receipt: dict[str, object] | None = None,
    ) -> None: ...
    def reconcile_and_enqueue(
        self, attempt: NarrationAttempt, event: str, job: NarrationJob | None
    ) -> None: ...
    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None: ...
    def project_auto_budget_round(self, owner_id: UUID, project_id: UUID) -> int:
        """当前预算轮次（projects.auto_budget_round）；MANUAL 恒为 1。"""
        ...
    def schedule_auto_retry(
        self, attempt: NarrationAttempt | None, event: str, job: NarrationJob
    ) -> bool:
        """AUTO 自动重投入队（等价 reconcile_and_enqueue 的单句 job 机制）。

        按确定性 job_id 去重：同一 job_id 已存在（当前/排队/已完成）时不重复入队，
        返回 False；否则持久化尝试状态迁移（如提供）并入队，返回 True。
        """
        ...
    def save_blocking_reason(
        self, owner_id: UUID, project_id: UUID, reasons: dict[int, str] | None
    ) -> None: ...
    def blocking_reason(self, owner_id: UUID, project_id: UUID) -> dict[str, object] | None: ...


class AudioArchivePort(Protocol):
    def save(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        kind: str,
        payload: bytes,
        metadata: dict[str, object],
    ) -> AudioAsset: ...
    def load(self, *, owner_id: UUID, project_id: UUID, asset_id: str) -> bytes: ...


class AudioNormalizerPort(Protocol):
    def normalize(
        self, payload: bytes, *, strict_loudness: bool = True
    ) -> NormalizedAudio: ...


class AudioInspectorPort(Protocol):
    def inspect(self, payload: bytes) -> AudioQcResult: ...


class ProjectSnapshotPort(Protocol):
    def get_draft_script(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, SupportedLocale, UUID | None]: ...
    def confirm_and_get_script(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, SupportedLocale, UUID | None]: ...
    def confirm_and_enqueue(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        sentence_splits: SentenceSplitPlan | None = None,
    ) -> tuple[str, SupportedLocale, UUID | None, NarrationJob]: ...
    def get_frozen_narration(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, SupportedLocale, UUID | None]: ...


class VoiceReferencePort(Protocol):
    def get_reference(
        self, owner_id: UUID, project_id: UUID, locale: SupportedLocale
    ) -> VoiceReference: ...


class NarrationJobStarterPort(Protocol):
    async def start(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        sentence_sequences: tuple[int, ...],
        job_id: str,
    ) -> str: ...


class ProjectWriteGuardPort(Protocol):
    def assert_writable(self, owner_id: UUID, project_id: UUID) -> None: ...
