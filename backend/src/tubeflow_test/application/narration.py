from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from tubeflow_test.adapters.audio_qc import inspect_wav
from tubeflow_test.adapters.cosyvoice import extract_batch_audio, extract_sentence_audio
from tubeflow_test.application.narration_ports import (
    AudioArchivePort,
    AudioInspectorPort,
    AudioNormalizerPort,
    CosyVoiceProviderPort,
    NarrationBatchFailure,
    NarrationJob,
    NarrationJobStarterPort,
    NarrationRepositoryPort,
    ProjectSnapshotPort,
    ProjectWriteGuardPort,
    SynthesisRequest,
    SynthesisResult,
    VoiceReferencePort,
    not_before_pending,
)
from tubeflow_test.domain.errors import (
    DomainError,
    NarrationIntentConflictError,
    ProviderRateLimitedError,
)
from tubeflow_test.domain.narration import (
    AudioQcResult,
    NarratedSentence,
    NarrationAttempt,
    NarrationAttemptState,
    ScriptVersion,
    authoritative_group_id,
    authoritative_sentence_id,
    build_measured_timeline,
    group_timeline,
    is_nonvisual_script_separator,
    narration_auto_retry_delay_seconds,
    narration_auto_retry_exhausted,
    narration_auto_retryable,
    narration_idempotency_key,
    narration_provider_blocking_code,
    segment_pasted_script,
)
from tubeflow_test.sentence_split.domain import SentenceSplitPlan
from tubeflow_test.sentence_split.service import SentenceSplitService

logger = logging.getLogger(__name__)


class SubmitSentenceNarration:
    def __init__(
        self,
        provider: CosyVoiceProviderPort,
        repository: NarrationRepositoryPort,
        archive: AudioArchivePort,
        normalizer: AudioNormalizerPort,
        raw_inspector: AudioInspectorPort | None = None,
        submission_lease_seconds: int = 1200,
        *,
        providers: Mapping[str, CosyVoiceProviderPort] | None = None,
    ) -> None:
        self._provider, self._repository, self._archive, self._normalizer = (
            provider,
            repository,
            archive,
            normalizer,
        )
        self._providers = dict(providers or {})
        self._raw_inspector = raw_inspector
        self._submission_lease_seconds = submission_lease_seconds

    def provider_for(self, request: SynthesisRequest) -> CosyVoiceProviderPort:
        """按 §23.1 冻结的配音源路由；未装配该源时 fail-closed。"""
        if request.provider in self._providers:
            return self._providers[request.provider]
        if request.provider in {"", "cosyvoice"}:
            return self._provider
        raise DomainError(
            "provider_not_configured", f"Provider {request.provider} is not configured."
        )

    async def execute(self, request: SynthesisRequest) -> SynthesisResult:
        request_hash = _request_hash(request)
        existing = self._repository.find_by_idempotency_key(
            request.owner_id, request.idempotency_key
        )
        if existing is not None:
            return SynthesisResult(existing.provider_call_id, "already_archived", None)
        attempt = self._repository.attempt(request.owner_id, request.idempotency_key)
        checkpoint = self._repository.provider_checkpoint(request.owner_id, request.idempotency_key)
        if attempt is not None:
            if attempt.request_sha256 != request_hash:
                # 同 batch 路径：内容身份由幂等键与句序保证，簿记漂移不算冲突。
                if attempt.sentence_sequence != request.sentence.sequence:
                    raise NarrationIntentConflictError(
                        "idempotency_conflict",
                        "Idempotency key was used for a different request.",
                    )
                self._repository.record_event(attempt, "intent_continuation_rebuilt")
            if checkpoint is None:
                raise DomainError(
                    "manual_reconciliation_required",
                    "A persisted narration attempt is immutable and cannot be resubmitted.",
                )
        else:
            attempt = NarrationAttempt(
                request.project_id,
                request.owner_id,
                request.sentence.sequence,
                request.idempotency_key,
                request_hash,
                NarrationAttemptState.INTENT_RECORDED,
                request.provider_call_id,
                parent_attempt_id=request.parent_attempt_id,
                original_complete_manifest=request.original_complete_manifest,
                original_manifest_sha256=request.original_manifest_sha256,
                original_zip_asset_id=request.original_zip_asset_id,
                retry_subset_manifest=request.retry_subset_manifest,
                retry_subset_manifest_sha256=request.retry_subset_manifest_sha256,
                # §15.9：AUTO 记录当前预算轮次；MANUAL 恒为 1（且不再多查一次）。
                budget_round=(
                    self._repository.project_auto_budget_round(
                        request.owner_id, request.project_id
                    )
                    if self._is_auto(request)
                    else 1
                ),
            )
            self._repository.save_attempt(attempt)
            self._repository.record_event(attempt, "intent_recorded")
        if checkpoint is not None:
            result = SynthesisResult(
                attempt.provider_call_id,
                "completed",
                self._archive.load(
                    owner_id=request.owner_id,
                    project_id=request.project_id,
                    asset_id=checkpoint.asset_id,
                ),
            )
        else:
            attempt = replace(attempt, state=NarrationAttemptState.SUBMISSION_STARTED)
            self._repository.save_attempt(attempt)
            self._repository.record_event(attempt, "provider_submission_started")
            try:
                result = await self.provider_for(request).submit(request)
            except asyncio.CancelledError:
                cancelled = replace(
                    attempt,
                    state=NarrationAttemptState.REMOTE_STATE_UNKNOWN,
                    safe_error_code="activity_cancelled_after_submission_started",
                )
                self._repository.save_attempt(cancelled)
                self._repository.record_event(cancelled, "activity_cancelled_unknown")
                raise
            except DomainError as error:
                if error.code == "provider_submission_unknown":
                    state = NarrationAttemptState.REMOTE_STATE_UNKNOWN
                elif error.code == "provider_not_reached":
                    state = NarrationAttemptState.SAFE_TO_RETRY
                elif error.code in {"provider_output_invalid", "provider_output_missing"}:
                    state = NarrationAttemptState.OUTPUT_INVALID
                elif error.code == "provider_rate_limited" and self._is_auto(request):
                    # §15.3（仅 AUTO）：429 属临时故障，可自动退避重试；MANUAL 仍为既定失败。
                    state = NarrationAttemptState.SAFE_TO_RETRY
                else:
                    state = NarrationAttemptState.PROVIDER_REJECTED
                failed = replace(
                    attempt,
                    state=state,
                    safe_error_code=error.code,
                    retry_not_before=(
                        self._auto_retry_not_before(request, error)
                        if state is NarrationAttemptState.SAFE_TO_RETRY
                        else None
                    ),
                )
                self._repository.save_attempt(failed)
                self._repository.record_event(failed, "provider_submission_failed")
                raise
        if result.output_zip is None or result.state != "completed":
            self._repository.save_attempt(
                replace(
                    attempt,
                    state=NarrationAttemptState.REMOTE_STATE_UNKNOWN,
                    remote_id=result.task_id,
                    safe_error_code="provider_result_incomplete",
                )
            )
            raise DomainError(
                "provider_submission_unknown",
                "CosyVoice did not return an immediately archivable result.",
            )
        attempt = replace(attempt, remote_id=result.task_id)
        self._repository.save_attempt(attempt)
        self._repository.record_event(attempt, "provider_response_received")
        if checkpoint is None:
            self._repository.set_checkpoint_ledger(attempt, "PENDING_UPLOAD")
            checkpoint = self._archive.save(
                owner_id=request.owner_id,
                project_id=request.project_id,
                kind="narration_provider_zip",
                payload=result.output_zip,
                metadata={
                    "idempotency_key": request.idempotency_key,
                    "provider_call_id": request.provider_call_id,
                    "request_sha256": request_hash,
                    "sentence_sequence": request.sentence.sequence,
                },
            )
            self._repository.set_checkpoint_ledger(attempt, "STAGED", checkpoint)
            self._repository.save_provider_checkpoint(attempt, checkpoint)
            self._repository.set_checkpoint_ledger(attempt, "ATTACHED", checkpoint)
            self._repository.record_event(attempt, "provider_output_checkpointed")
        phase = "output_validation"
        try:
            raw_payload = extract_sentence_audio(
                result.output_zip,
                f"{request.sentence.sequence:04d}-{request.sentence.text_sha256[:12]}.wav",
            )
            phase = "archive"
            self._complete_raw(
                request,
                attempt,
                raw_payload,
                provider_request_id=result.provider_request_id,
                remote_id=result.task_id,
            )
        except (DomainError, OSError, RuntimeError, ValueError) as error:
            state = (
                NarrationAttemptState.ARCHIVE_FAILED
                if phase == "archive"
                else NarrationAttemptState.OUTPUT_INVALID
            )
            failure_code = getattr(error, "code", "processing_failed")
            failed = replace(
                attempt, state=state, safe_error_code=failure_code
            )
            # 2026-10-27：阶段与真实错误码落到日志，避免下次只能靠上机复现归因。
            logger.warning(
                "narration unit processing failed: project=%s sequence=%s phase=%s code=%s error=%s: %s",
                request.project_id,
                request.sentence.sequence,
                phase,
                failure_code,
                type(error).__name__,
                str(error)[:300],
            )
            self._repository.save_attempt(failed)
            self._repository.record_event(failed, f"{phase}_failed")
            if isinstance(error, DomainError):
                raise
            raise DomainError(
                "narration_processing_failed",
                "Narration processing failed after provider submission.",
            ) from error
        return result

    async def execute_batch(self, requests: tuple[SynthesisRequest, ...]) -> tuple[int, ...]:
        if not requests or len(requests) > 20:
            raise DomainError("invalid_batch", "Narration batch must contain 1 to 20 units.")
        all_expected = {
            name
            for request in requests
            for name in (
                request.expected_output_names
                or (f"{request.sentence.sequence:04d}-{request.sentence.text_sha256[:12]}.wav",)
            )
        }
        active: list[tuple[SynthesisRequest, NarrationAttempt]] = []
        waiting_for_lease = False
        blocked_sequences: list[int] = []
        for request in requests:
            if (
                self._repository.find_by_idempotency_key(request.owner_id, request.idempotency_key)
                is not None
            ):
                continue
            request_hash = _request_hash(request)
            attempt = self._repository.attempt(request.owner_id, request.idempotency_key)
            if attempt is None:
                attempt = NarrationAttempt(
                    request.project_id,
                    request.owner_id,
                    request.sentence.sequence,
                    request.idempotency_key,
                    request_hash,
                    NarrationAttemptState.INTENT_RECORDED,
                    request.provider_call_id,
                    parent_attempt_id=request.parent_attempt_id,
                    original_complete_manifest=request.original_complete_manifest,
                    original_manifest_sha256=request.original_manifest_sha256,
                    original_zip_asset_id=request.original_zip_asset_id,
                    retry_subset_manifest=request.retry_subset_manifest,
                    retry_subset_manifest_sha256=request.retry_subset_manifest_sha256,
                    budget_round=_budget_round_of_key(request.idempotency_key),
                )
                self._repository.save_attempt(attempt)
                self._repository.record_event(attempt, "batch_intent_recorded")
            elif attempt.request_sha256 != request_hash:
                # 幂等键由（项目+句子文本+音色）内容派生：句序一致即同一意图。
                # 续投/未知态恢复的重建请求在批次簿记字段（清单、检查点资产）上
                # 允许漂移，沿用既有记录继续（§15.4 恢复路径），不算冲突。
                if attempt.sentence_sequence != request.sentence.sequence:
                    raise NarrationIntentConflictError(
                        "idempotency_conflict", "Narration batch intent changed."
                    )
                self._repository.record_event(attempt, "batch_intent_continuation_rebuilt")
            attempt = _correct_attempt_round(self._repository, attempt, request)
            if attempt.state is NarrationAttemptState.PROVIDER_REJECTED:
                # 终态必须先于检查点判定：PROVIDER_REJECTED 在状态机里没有出边，
                # 若先走检查点补写分支，恢复会抛 stale_attempt_transition 并让整个活动
                # 反复失败直到 schedule_to_close 耗尽（2026-10-29 生产事故根因）。
                blocked_sequences.append(request.sentence.sequence)
                continue
            checkpoint = self._repository.provider_checkpoint(
                request.owner_id, request.idempotency_key
            )
            if checkpoint is not None:
                payload = await asyncio.to_thread(
                    self._archive.load,
                    owner_id=request.owner_id,
                    project_id=request.project_id,
                    asset_id=checkpoint.asset_id,
                )
                matched = extract_batch_audio(payload, all_expected)
                name = f"{request.sentence.sequence:04d}-{request.sentence.text_sha256[:12]}.wav"
                raw = matched.get(name)
                if raw is not None:
                    try:
                        await asyncio.to_thread(self._complete_raw, request, attempt, raw)
                    except (DomainError, OSError, RuntimeError, ValueError) as error:
                        failed = replace(
                            attempt,
                            state=NarrationAttemptState.OUTPUT_INVALID,
                            safe_error_code=getattr(error, "code", "processing_failed"),
                        )
                        self._repository.save_attempt(failed)
                        self._repository.record_event(
                            failed, "checkpoint_unit_processing_failed"
                        )
                        blocked_sequences.append(request.sentence.sequence)
                else:
                    missing = replace(
                        attempt,
                        state=NarrationAttemptState.SAFE_TO_RETRY,
                        safe_error_code="provider_output_missing",
                        retry_not_before=self._auto_retry_not_before(request),
                    )
                    self._repository.save_attempt(missing)
                    self._repository.record_event(missing, "checkpoint_unit_missing")
                    blocked_sequences.append(request.sentence.sequence)
                continue
            if attempt.state not in {
                NarrationAttemptState.INTENT_RECORDED,
                NarrationAttemptState.SAFE_TO_RETRY,
                NarrationAttemptState.SUBMISSION_STARTED,
            }:
                continue
            started = self._repository.claim_submission(
                attempt,
                str(uuid4()),
                datetime.now(UTC) + timedelta(seconds=self._submission_lease_seconds),
            )
            if started is None:
                current = self._repository.attempt(request.owner_id, request.idempotency_key)
                if (
                    current is not None
                    and current.state is NarrationAttemptState.SUBMISSION_STARTED
                ):
                    waiting_for_lease = True
                else:
                    blocked_sequences.append(request.sentence.sequence)
                continue
            self._repository.record_event(started, "batch_submission_started")
            active.append((request, started))
        if not active:
            if waiting_for_lease:
                raise DomainError(
                    "submission_lease_active",
                    "Another worker still owns the durable provider submission lease.",
                )
            return tuple(sorted(set(blocked_sequences)))
        batch_requests = tuple(item[0] for item in active)
        batch_failure: NarrationBatchFailure | None = None
        partial: dict[int, str] = {}
        try:
            result = await self.provider_for(batch_requests[0]).submit_batch(batch_requests)
        except asyncio.CancelledError:
            for _, attempt in active:
                unknown = replace(
                    attempt,
                    state=NarrationAttemptState.REMOTE_STATE_UNKNOWN,
                    safe_error_code="activity_cancelled_during_submission",
                )
                self._repository.save_attempt(unknown)
                self._repository.record_event(unknown, "batch_submission_cancelled")
            raise
        except NarrationBatchFailure as failure:
            # §23.7：批内部分失败——成功句照常归档，失败句按逐句**权威失败码**落状态，
            # 避免整批重投造成重复付费。
            batch_failure = failure
            partial = dict(failure.result.errors) if failure.result is not None else {}
            if failure.result is None or failure.result.output_zip is None:
                for request, attempt in active:
                    code = partial.get(request.sentence.sequence, "provider_rejected")
                    state, not_before = self._submission_failure_state(request, code)
                    failed = replace(
                        attempt,
                        state=state,
                        safe_error_code=code,
                        retry_not_before=not_before,
                    )
                    self._repository.save_attempt(failed)
                    self._repository.record_event(failed, "batch_submission_failed")
                raise
            result = failure.result
        except DomainError as error:
            for request, attempt in active:
                state, not_before = self._submission_failure_state(
                    request, error.code, error=error
                )
                failed = replace(
                    attempt,
                    state=state,
                    safe_error_code=error.code,
                    retry_not_before=not_before,
                )
                self._repository.save_attempt(failed)
                self._repository.record_event(failed, "batch_submission_failed")
            raise
        if result.output_zip is None or result.state not in {"completed", "partial"}:
            for _, attempt in active:
                unknown = replace(
                    attempt,
                    state=NarrationAttemptState.REMOTE_STATE_UNKNOWN,
                    safe_error_code="provider_submission_unknown",
                )
                self._repository.save_attempt(unknown)
                self._repository.record_event(unknown, "batch_submission_unknown")
            raise DomainError("provider_submission_unknown", "Batch result is incomplete.")
        first_request = active[0][0]
        # §23.7：批量路径逐句回填 Provider 远端 id：完成态由 `_complete_raw` 随
        # `save_completed` 一次性落库（含对账列），失败句保留确认后的真实原因。
        remote_ids = dict(result.provider_request_ids)
        expected = {
            f"{request.sentence.sequence:04d}-{request.sentence.text_sha256[:12]}.wav"
            for request, _ in active
        }
        for _, attempt in active:
            self._repository.set_checkpoint_ledger(attempt, "PENDING_UPLOAD")
        try:
            checkpoint = await asyncio.to_thread(
                self._archive.save,
                owner_id=first_request.owner_id,
                project_id=first_request.project_id,
                kind="narration_provider_zip",
                payload=result.output_zip,
                metadata={
                    "provider_call_id": first_request.provider_call_id,
                    "provider_request_key": first_request.idempotency_key,
                    "unit_count": len(active),
                    "manifest": sorted(expected),
                    "zip_sha256": hashlib.sha256(result.output_zip).hexdigest(),
                    "zip_size_bytes": len(result.output_zip),
                },
            )
        except (DomainError, OSError, RuntimeError, ValueError) as error:
            for _, attempt in active:
                failed = replace(
                    attempt,
                    state=NarrationAttemptState.ARCHIVE_FAILED,
                    safe_error_code=getattr(error, "code", "checkpoint_archive_failed"),
                )
                self._repository.save_attempt(failed)
                self._repository.record_event(failed, "batch_checkpoint_archive_failed")
            raise
        except asyncio.CancelledError:
            for _, attempt in active:
                unknown = replace(
                    attempt,
                    state=NarrationAttemptState.REMOTE_STATE_UNKNOWN,
                    safe_error_code="activity_cancelled_during_checkpoint",
                )
                self._repository.save_attempt(unknown)
                self._repository.record_event(unknown, "batch_checkpoint_cancelled")
            raise
        try:
            matched = extract_batch_audio(result.output_zip, expected)
        except (DomainError, OSError, RuntimeError, ValueError) as error:
            fallback_code = getattr(error, "code", "provider_zip_invalid")
            for request, attempt in active:
                # 2026-10-31：空/残缺 ZIP 时仍保留服务商逐句权威失败码，不再统一覆盖成
                # provider_output_invalid——否则真实原因（如 provider_rejected）会被掩盖，
                # 诊断与 AUTO 阻断原因都会失真。
                authoritative = partial.get(request.sentence.sequence)
                if authoritative:
                    state, not_before = self._submission_failure_state(request, authoritative)
                    failed = replace(
                        attempt,
                        state=state,
                        safe_error_code=authoritative,
                        retry_not_before=not_before,
                    )
                    self._repository.save_attempt(failed)
                    self._repository.record_event(
                        failed,
                        "batch_unit_rejected"
                        if state is NarrationAttemptState.PROVIDER_REJECTED
                        else "batch_unit_retryable",
                    )
                    continue
                failed = replace(
                    attempt,
                    state=NarrationAttemptState.OUTPUT_INVALID,
                    safe_error_code=fallback_code,
                )
                self._repository.save_attempt(failed)
                self._repository.record_event(failed, "batch_zip_invalid")
            raise
        # Provider 明确拒绝的句子不挂批次检查点：它不在 ZIP 里，若照样挂上批次资产，
        # 恢复路径会试图把终态改写成 SAFE_TO_RETRY 而被状态机拒绝（2026-10-29 事故）。
        # 未被拒绝但 ZIP 里缺失的句子仍挂检查点，保持"崩溃重放不重复提交"的既有语义。
        for request, attempt in active:
            if request.sentence.sequence in partial:
                continue
            self._repository.set_checkpoint_ledger(attempt, "STAGED", checkpoint)
            self._repository.save_provider_checkpoint(attempt, checkpoint)
            self._repository.set_checkpoint_ledger(attempt, "ATTACHED", checkpoint)
        receipt = {
            "call_id": first_request.provider_call_id,
            "provider_request_key": first_request.idempotency_key,
            "manifest": sorted(expected),
            "zip_sha256": checkpoint.sha256,
            "zip_size_bytes": checkpoint.size_bytes,
            "members": sorted(matched),
        }
        for request, attempt in active:
            if request.sentence.sequence in partial:
                continue
            self._repository.set_checkpoint_ledger(attempt, "ATTACHED", checkpoint, receipt)
        failures: list[int] = []
        for request, attempt in active:
            name = f"{request.sentence.sequence:04d}-{request.sentence.text_sha256[:12]}.wav"
            raw = matched.get(name)
            if raw is None:
                # §23.7：Provider 已给出该句权威失败码时用它，不用笼统的 output_missing；
                # 状态按单句路径同一规则映射（连接没建立 ≠ 服务商既定拒绝）。
                authoritative = partial.get(request.sentence.sequence)
                if authoritative:
                    state, not_before = self._submission_failure_state(request, authoritative)
                    rejected = replace(
                        attempt,
                        state=state,
                        safe_error_code=authoritative,
                        retry_not_before=not_before,
                    )
                    self._repository.save_attempt(rejected)
                    self._repository.record_event(
                        rejected,
                        "batch_unit_rejected"
                        if state is NarrationAttemptState.PROVIDER_REJECTED
                        else "batch_unit_retryable",
                    )
                    failures.append(request.sentence.sequence)
                    continue
                missing = replace(
                    attempt,
                    state=NarrationAttemptState.SAFE_TO_RETRY,
                    safe_error_code="provider_output_missing",
                    retry_not_before=self._auto_retry_not_before(request),
                )
                self._repository.save_attempt(missing)
                self._repository.record_event(missing, "batch_unit_missing")
                failures.append(request.sentence.sequence)
                continue
            try:
                await asyncio.to_thread(
                    self._complete_raw,
                    request,
                    attempt,
                    raw,
                    provider_request_id=remote_ids.get(request.sentence.sequence),
                    remote_id=remote_ids.get(request.sentence.sequence),
                )
            except (DomainError, OSError, RuntimeError, ValueError) as error:
                failed = replace(
                    attempt,
                    state=NarrationAttemptState.OUTPUT_INVALID,
                    safe_error_code=getattr(error, "code", "processing_failed"),
                )
                self._repository.save_attempt(failed)
                self._repository.record_event(failed, "batch_unit_processing_failed")
                failures.append(request.sentence.sequence)
        if batch_failure is not None:
            # 成功句已归档；失败句状态已落库，向上抛出真实原因驱动既有重试/暂停语义。
            raise batch_failure
        return tuple(sorted(set((*blocked_sequences, *failures))))

    def _complete_raw(
        self,
        request: SynthesisRequest,
        attempt: NarrationAttempt,
        raw_payload: bytes,
        *,
        provider_request_id: str | None = None,
        remote_id: str | None = None,
    ) -> None:
        raw_qc = (
            self._raw_inspector.inspect(raw_payload)
            if self._raw_inspector is not None
            else inspect_wav(raw_payload)
        )
        if not raw_qc.passed:
            raise DomainError("audio_qc_failed", "CosyVoice raw WAV failed technical QC.")
        # 2026-10-28：响度门仅对 ElevenLabs 线路放宽（用户确认）。EL 只保证可合成，
        # 不保证句间音量一致；仍保留转码/静音/时长守恒等硬校验，响度未达 -16 LUFS
        # ±1 或 TP 超 -0.8 dBTP 时不再判失败，只记 loudness_gate=outside 继续。
        strict_loudness = request.provider != "elevenlabs"
        normalized = self._normalizer.normalize(
            raw_payload, strict_loudness=strict_loudness
        )
        if normalized.manifest.get("loudness_gate") == "outside":
            logger.warning(
                "narration loudness outside gate but accepted: project=%s seq=%s "
                "provider=%s integrated_lufs=%s true_peak_dbtp=%s",
                request.project_id,
                request.sentence.sequence,
                request.provider,
                (normalized.manifest.get("verified_loudness") or {}).get("integrated_lufs"),
                (normalized.manifest.get("verified_loudness") or {}).get("true_peak_dbtp"),
            )
        normalized_qc = inspect_wav(normalized.payload)
        if not normalized_qc.passed:
            raise DomainError("audio_qc_failed", "Normalized narration WAV failed technical QC.")
        if (
            normalized_qc.sample_rate_hz != 48_000
            or normalized_qc.channels != 1
            or normalized_qc.sample_width_bytes != 2
            or raw_qc.frame_count is None
            or raw_qc.sample_rate_hz is None
            or normalized_qc.frame_count is None
            or normalized_qc.sample_rate_hz is None
            or abs(
                raw_qc.frame_count * normalized_qc.sample_rate_hz
                - normalized_qc.frame_count * raw_qc.sample_rate_hz
            )
            > max(raw_qc.sample_rate_hz, normalized_qc.sample_rate_hz)
        ):
            raise DomainError(
                "normalized_audio_invalid",
                "Normalized narration violates the authoritative audio contract.",
            )
        metadata = {
            "sentence_sequence": request.sentence.sequence,
            "text_sha256": request.sentence.text_sha256,
            "idempotency_key": request.idempotency_key,
            "provider_call_id": request.provider_call_id,
            "reference_package_sha256": request.reference_sha256,
        }
        raw_asset = self._archive.save(
            owner_id=request.owner_id,
            project_id=request.project_id,
            kind="narration_raw",
            payload=raw_payload,
            metadata={**metadata, "qc": _qc_manifest(raw_qc)},
        )
        normalized_asset = self._archive.save(
            owner_id=request.owner_id,
            project_id=request.project_id,
            kind="narration_normalized",
            payload=normalized.payload,
            metadata={
                **metadata,
                "source_raw_sha256": raw_asset.sha256,
                "normalization": normalized.manifest,
                "qc": _qc_manifest(normalized_qc),
            },
        )
        if raw_asset.asset_id == normalized_asset.asset_id:
            raise DomainError("audio_archive_invalid", "Raw and normalized assets must differ.")
        narration = NarratedSentence(
            request.project_id,
            request.owner_id,
            request.voice_version_id,
            request.language,
            request.provider_call_id,
            request.reference_sha256,
            request.sentence,
            request.idempotency_key,
            raw_asset,
            normalized_asset,
            raw_qc,
            normalized_qc,
            normalized.manifest,
            provider_request_id=provider_request_id,
        )
        completed = replace(attempt, state=NarrationAttemptState.COMPLETED)
        if remote_id:
            # §23.7：远端 id 必须随完成态一起落库，否则拼接链与对账列永久为空。
            completed = replace(completed, remote_id=remote_id)
        self._repository.save_completed(completed, narration)
        self._repository.record_event(completed, "archive_completed")

    def _is_auto(self, request: SynthesisRequest) -> bool:
        return (
            self._repository.project_production_mode(request.owner_id, request.project_id)
            == "AUTO"
        )

    def _auto_retry_not_before(
        self, request: SynthesisRequest, error: DomainError | None = None
    ) -> str | None:
        """AUTO 项目 SAFE_TO_RETRY 失败的持久化退避时间戳；MANUAL 恒为 None。"""
        if not self._is_auto(request):
            return None
        budget_round = self._repository.project_auto_budget_round(
            request.owner_id, request.project_id
        )
        count = sum(
            1
            for item in self._repository.list_attempts(request.owner_id, request.project_id)
            if item.sentence_sequence == request.sentence.sequence
            and item.budget_round == budget_round
        )
        retry_after = (
            error.retry_after_seconds if isinstance(error, ProviderRateLimitedError) else None
        )
        delay = narration_auto_retry_delay_seconds(count, retry_after)
        return (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()

    def _submission_failure_state(
        self,
        request: SynthesisRequest,
        code: str,
        *,
        error: DomainError | None = None,
    ) -> tuple[NarrationAttemptState, str | None]:
        """逐句提交失败 → (权威状态, 退避时间)；单句与批量路径共用同一映射。

        连接未建立（provider_not_reached）属于可重试的网络事实，绝不可落成既定拒绝；
        AUTO 项目的限流同理。其余（真 4xx、审核拒绝等）才是 Provider 既定拒绝。
        """
        if code == "provider_submission_unknown":
            return NarrationAttemptState.REMOTE_STATE_UNKNOWN, None
        if code == "provider_not_reached":
            return NarrationAttemptState.SAFE_TO_RETRY, self._auto_retry_not_before(request)
        if code == "provider_rate_limited" and self._is_auto(request):
            return (
                NarrationAttemptState.SAFE_TO_RETRY,
                self._auto_retry_not_before(request, error),
            )
        return NarrationAttemptState.PROVIDER_REJECTED, None

    async def recover(self, owner_id: object, idempotency_key: str) -> SynthesisResult:
        del owner_id
        raise DomainError(
            "manual_reconciliation_required", "This CosyVoice service has no recovery query API."
        )

    def rebuild_authoritative_timeline(self, request: SynthesisRequest) -> None:
        script = self._repository.get_script(request.owner_id, request.project_id)
        if script is None:
            raise DomainError("narration_not_ready", "Immutable script is not archived.")
        narrated = self._repository.list_narrated(request.owner_id, request.project_id)
        timeline = build_measured_timeline(script, narrated)
        self._repository.save_timeline(
            request.owner_id, request.project_id, timeline, group_timeline(timeline)
        )


def _budget_round_of_key(key: str) -> int:
    match = re.search(r":r(\d+)(?::|$)", key)
    return int(match.group(1)) if match else 1


def _correct_attempt_round(
    repository: NarrationRepositoryPort,
    attempt: NarrationAttempt,
    request: SynthesisRequest,
) -> NarrationAttempt:
    """键内预算轮次是权威（构造时写入）；历史缺陷记录（键轮次与字段不符）续投时纠正。"""
    key_round = _budget_round_of_key(request.idempotency_key)
    if key_round == attempt.budget_round:
        return attempt
    corrected = replace(attempt, budget_round=key_round)
    repository.save_attempt(corrected)
    repository.record_event(corrected, "budget_round_corrected")
    return corrected


def _request_hash(request: SynthesisRequest) -> str:
    payload = {
        "project_id": str(request.project_id),
        "owner_id": str(request.owner_id),
        "voice_version_id": (
            str(request.voice_version_id) if request.voice_version_id is not None else None
        ),
        "cloned_voice_id": (
            str(request.cloned_voice_id) if request.cloned_voice_id is not None else None
        ),
        "provider": request.provider,
        "sequence": request.sentence.sequence,
        "text_sha256": request.sentence.text_sha256,
        "locale": request.language.value,
        "reference_sha256": request.reference_sha256,
        "emotion": request.emotion,
        "expected_output_names": sorted(request.expected_output_names),
        "provider_call_id": request.provider_call_id,
        "parent_attempt_id": request.parent_attempt_id,
        "original_complete_manifest": sorted(request.original_complete_manifest),
        "original_manifest_sha256": request.original_manifest_sha256,
        "original_zip_asset_id": request.original_zip_asset_id,
        "retry_subset_manifest": sorted(request.retry_subset_manifest),
        "retry_subset_manifest_sha256": request.retry_subset_manifest_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _qc_manifest(qc: AudioQcResult) -> dict[str, object]:
    from dataclasses import asdict

    return {key: value for key, value in asdict(qc).items() if value is not None}


def _attempt_ordinal(key: str) -> int:
    marker = ":attempt:"
    if marker not in key:
        return 1
    try:
        return int(key.rsplit(marker, 1)[1])
    except ValueError:
        return 0


class ProjectNarrationService:
    def __init__(
        self,
        projects: ProjectSnapshotPort,
        voices: VoiceReferencePort,
        repository: NarrationRepositoryPort,
        submitter: SubmitSentenceNarration,
        jobs: NarrationJobStarterPort,
        write_guard: ProjectWriteGuardPort,
        *,
        director_materializer: Callable[[UUID, UUID], object | None] | None = None,
        sentence_splitter: SentenceSplitService | None = None,
    ) -> None:
        self._projects, self._voices, self._repository, self._submitter, self._jobs = (
            projects,
            voices,
            repository,
            submitter,
            jobs,
        )
        self._write_guard = write_guard
        self._director_materializer = director_materializer
        self._sentence_splitter = sentence_splitter

    def preflight(self, owner_id: object, project_id: object, pasted_text: str) -> ScriptVersion:
        from uuid import UUID

        if not isinstance(owner_id, UUID) or not isinstance(project_id, UUID):
            raise DomainError("invalid_request", "Owner and project identifiers are invalid.")
        self._write_guard.assert_writable(owner_id, project_id)
        _, locale, _ = self._projects.get_draft_script(owner_id, project_id)
        return segment_pasted_script(pasted_text, locale)

    async def confirm_and_start(self, owner_id: object, project_id: object) -> object:
        from uuid import UUID

        if not isinstance(owner_id, UUID) or not isinstance(project_id, UUID):
            raise DomainError("invalid_request", "Owner and project identifiers are invalid.")
        self._write_guard.assert_writable(owner_id, project_id)
        sentence_splits: SentenceSplitPlan | None = None
        if (
            self._repository.project_production_mode(owner_id, project_id) == "AUTO"
            and not self._snapshot_frozen(owner_id, project_id)
        ):
            # §15.7：冻结快照前对估算超长风险句做语义二级拆分；agent 失败则整体提交失败。
            if self._sentence_splitter is None:
                raise DomainError(
                    "sentence_split_not_configured",
                    "Sentence split agent is required for AUTO projects.",
                )
            draft_text, locale, _voice_version_id = self._projects.get_draft_script(
                owner_id, project_id
            )
            sentence_splits = await self._sentence_splitter.plan_splits(
                segment_pasted_script(draft_text, locale)
            )
        _text, _locale, _voice_version_id, job = self._projects.confirm_and_enqueue(
            owner_id, project_id, sentence_splits=sentence_splits
        )
        return {"project_id": str(project_id), "job_id": job.job_id, "state": "QUEUED"}

    def _snapshot_frozen(self, owner_id: UUID, project_id: UUID) -> bool:
        try:
            self._projects.get_frozen_narration(owner_id, project_id)
        except DomainError:
            return False
        return True

    async def dispatch_pending_jobs(self) -> None:
        failures: list[str] = []
        for job in self._repository.pending_jobs():
            try:
                await self._jobs.start(
                    owner_id=job.owner_id,
                    project_id=job.project_id,
                    sentence_sequences=job.sentence_sequences,
                    job_id=job.job_id,
                )
            except Exception as error:  # Outbox remains pending for the supervised retry loop.
                failures.append(f"{job.job_id}:{type(error).__name__}")
                continue
            self._repository.mark_job_dispatched(job.job_id)
        if failures:
            raise RuntimeError("Temporal narration dispatch failed: " + ",".join(failures))

    async def synthesize_sentence(self, owner_id: UUID, project_id: UUID, sequence: int) -> None:
        self._write_guard.assert_writable(owner_id, project_id)
        script = self._repository.get_script(owner_id, project_id)
        if script is None:
            raise DomainError("narration_not_ready", "Immutable script is not archived.")
        sentence = next((item for item in script.sentences if item.sequence == sequence), None)
        if sentence is None:
            raise DomainError("not_found", "Resource not found.")
        if is_nonvisual_script_separator(sentence.text):
            raise DomainError(
                "nonvisual_sentence_not_narratable",
                "Non-visual separator sentences are never narrated.",
            )
        if any(
            item.sentence.sequence == sequence
            for item in self._repository.list_narrated(owner_id, project_id)
        ):
            return
        frozen_text, locale, voice_version_id = self._projects.get_frozen_narration(
            owner_id, project_id
        )
        if segment_pasted_script(frozen_text, locale).source_sha256 != script.source_sha256:
            raise DomainError("project_snapshot_mismatch", "Frozen narration snapshot changed.")
        reference = self._voices.get_reference(owner_id, project_id, locale)
        voice_identity = reference.voice_version_id or reference.cloned_voice_id
        base_key = narration_idempotency_key(str(project_id), sentence, str(voice_identity))
        attempts = tuple(
            item
            for item in self._repository.list_attempts(owner_id, project_id)
            if item.sentence_sequence == sequence
        )
        auto_mode = self._repository.project_production_mode(owner_id, project_id) == "AUTO"
        budget_round = (
            self._repository.project_auto_budget_round(owner_id, project_id)
            if auto_mode
            else 1
        )
        # §15.9：AUTO 恢复后新一轮授权内重新计数；旧轮终态尝试不再阻断新提交。
        round_attempts = tuple(item for item in attempts if item.budget_round == budget_round)
        latest = (
            max(round_attempts, key=lambda item: _attempt_ordinal(item.idempotency_key))
            if round_attempts
            else None
        )
        checkpoint = (
            self._repository.provider_checkpoint(owner_id, latest.idempotency_key)
            if latest is not None
            else None
        )
        if (
            latest is not None
            and latest.state is not NarrationAttemptState.SAFE_TO_RETRY
            and checkpoint is None
        ):
            raise DomainError(
                "manual_reconciliation_required",
                "The latest narration attempt requires an explicit disposition.",
            )
        round_base = base_key if budget_round == 1 else f"{base_key}:r{budget_round}"
        key = (
            latest.idempotency_key
            if latest is not None and checkpoint is not None
            else round_base
            if not round_attempts
            else f"{round_base}:attempt:{len(round_attempts) + 1}"
        )
        request = SynthesisRequest(
            project_id,
            owner_id,
            voice_version_id,
            sentence,
            key,
            locale,
            reference.reference_zip,
            reference.reference_sha256,
            key,
            provider=reference.provider,
            cloned_voice_id=reference.cloned_voice_id,
        )
        await self._submitter.execute(request)

    async def synthesize_batch(
        self, owner_id: UUID, project_id: UUID, sequences: tuple[int, ...]
    ) -> tuple[int, ...]:
        if not sequences or len(sequences) > 20 or len(set(sequences)) != len(sequences):
            raise DomainError("invalid_batch", "Narration batch must contain 1 to 20 units.")
        self._write_guard.assert_writable(owner_id, project_id)
        script = self._repository.get_script(owner_id, project_id)
        if script is None:
            raise DomainError("narration_not_ready", "Immutable script is not archived.")
        frozen_text, locale, voice_version_id = self._projects.get_frozen_narration(
            owner_id, project_id
        )
        if segment_pasted_script(frozen_text, locale).source_sha256 != script.source_sha256:
            raise DomainError("project_snapshot_mismatch", "Frozen narration snapshot changed.")
        reference = self._voices.get_reference(owner_id, project_id, locale)
        voice_identity = reference.voice_version_id or reference.cloned_voice_id
        narrated_sequences = {
            item.sentence.sequence for item in self._repository.list_narrated(owner_id, project_id)
        }
        requests: list[SynthesisRequest] = []
        blocked: list[int] = []
        all_attempts = self._repository.list_attempts(owner_id, project_id)
        auto_mode = self._repository.project_production_mode(owner_id, project_id) == "AUTO"
        budget_round = (
            self._repository.project_auto_budget_round(owner_id, project_id)
            if auto_mode
            else 1
        )
        gate_now = datetime.now(UTC)
        batch_generation = len(all_attempts)
        batch_call_id = (
            "batch_"
            + hashlib.sha256(f"{project_id}:{sequences}:{batch_generation}".encode()).hexdigest()
        )
        batch_manifest = tuple(
            f"{sentence.sequence:04d}-{sentence.text_sha256[:12]}.wav"
            for sentence in script.sentences
            if sentence.sequence in sequences
        )
        for sequence in sequences:
            sentence = next((item for item in script.sentences if item.sequence == sequence), None)
            if sentence is None:
                blocked.append(sequence)
                continue
            base_key = narration_idempotency_key(str(project_id), sentence, str(voice_identity))
            attempts = tuple(item for item in all_attempts if item.sentence_sequence == sequence)
            round_attempts = tuple(
                item for item in attempts if item.budget_round == budget_round
            )
            latest = (
                max(round_attempts, key=lambda item: _attempt_ordinal(item.idempotency_key))
                if round_attempts
                else None
            )
            checkpoint = (
                self._repository.provider_checkpoint(owner_id, latest.idempotency_key)
                if latest is not None
                else None
            )
            if sequence in narrated_sequences and checkpoint is None:
                continue
            if (
                auto_mode
                and latest is not None
                and latest.state is NarrationAttemptState.SAFE_TO_RETRY
                and not (latest.safe_error_code or "").startswith("manual:")
                and not_before_pending(latest.retry_not_before, gate_now)
            ):
                # AUTO：退避窗口内的失败句暂不重投，由 finish_job 的持久化调度接管。
                blocked.append(sequence)
                continue
            if (
                latest is not None
                and latest.state
                not in {
                    NarrationAttemptState.SAFE_TO_RETRY,
                    NarrationAttemptState.INTENT_RECORDED,
                    NarrationAttemptState.SUBMISSION_STARTED,
                }
                and checkpoint is None
            ):
                blocked.append(sequence)
                continue
            key = (
                latest.idempotency_key
                if latest is not None
                and latest.state is not NarrationAttemptState.SAFE_TO_RETRY
                and (
                    checkpoint is not None
                    or latest.state
                    in {
                        NarrationAttemptState.INTENT_RECORDED,
                        NarrationAttemptState.SUBMISSION_STARTED,
                    }
                )
                else (
                    base_key
                    if budget_round == 1
                    else f"{base_key}:r{budget_round}"
                )
                if not round_attempts
                else (
                    f"{base_key}:attempt:{len(round_attempts) + 1}"
                    if budget_round == 1
                    else f"{base_key}:r{budget_round}:attempt:{len(round_attempts) + 1}"
                )
            )
            related_names = tuple(
                f"{related.sequence:04d}-{related.text_sha256[:12]}.wav"
                for related in script.sentences
                if latest is not None
                and latest.state is not NarrationAttemptState.SAFE_TO_RETRY
                and any(
                    item.sentence_sequence == related.sequence
                    and item.provider_call_id == latest.provider_call_id
                    for item in all_attempts
                )
            )
            provider_call_id = (
                latest.provider_call_id
                if latest is not None and latest.state is not NarrationAttemptState.SAFE_TO_RETRY
                else batch_call_id
            )
            is_retry = latest is not None and latest.state is NarrationAttemptState.SAFE_TO_RETRY
            retry_parent = latest if is_retry else None
            original_complete_manifest = (
                retry_parent.original_complete_manifest
                if retry_parent is not None
                else related_names or batch_manifest
            )
            if not original_complete_manifest:
                raise DomainError(
                    "narration_lineage_missing",
                    "The immutable original provider manifest is unavailable.",
                )
            original_manifest_sha256 = hashlib.sha256(
                "\n".join(original_complete_manifest).encode()
            ).hexdigest()
            if (
                retry_parent is not None
                and retry_parent.original_manifest_sha256 != original_manifest_sha256
            ):
                raise DomainError(
                    "narration_lineage_invalid",
                    "The immutable original provider manifest hash is inconsistent.",
                )
            retry_subset_manifest = batch_manifest if is_retry else ()
            requests.append(
                SynthesisRequest(
                    project_id,
                    owner_id,
                    voice_version_id,
                    sentence,
                    key,
                    locale,
                    reference.reference_zip,
                    reference.reference_sha256,
                    provider_call_id,
                    expected_output_names=related_names or batch_manifest,
                    parent_attempt_id=(
                        retry_parent.idempotency_key if retry_parent is not None else None
                    ),
                    original_complete_manifest=original_complete_manifest,
                    original_manifest_sha256=original_manifest_sha256,
                    original_zip_asset_id=(
                        checkpoint.asset_id if checkpoint is not None and is_retry else None
                    ),
                    retry_subset_manifest=retry_subset_manifest,
                    retry_subset_manifest_sha256=(
                        hashlib.sha256("\n".join(retry_subset_manifest).encode()).hexdigest()
                        if retry_subset_manifest
                        else None
                    ),
                    provider=reference.provider,
                    cloned_voice_id=reference.cloned_voice_id,
                )
            )
        if requests:
            blocked.extend(await self._submitter.execute_batch(tuple(requests)))
        return tuple(sorted(set(blocked)))

    def finalize_timeline(self, owner_id: UUID, project_id: UUID) -> None:
        self._write_guard.assert_writable(owner_id, project_id)
        script = self._repository.get_script(owner_id, project_id)
        if script is None or not script.sentences:
            raise DomainError("narration_not_ready", "Immutable script is not archived.")
        first = next(
            (
                item
                for item in script.sentences
                if not is_nonvisual_script_separator(item.text)
            ),
            None,
        )
        if first is None:
            raise DomainError("narration_not_ready", "Immutable script is not archived.")
        _, locale, voice_version_id = self._projects.get_frozen_narration(owner_id, project_id)
        reference = self._voices.get_reference(owner_id, project_id, locale)
        voice_identity = reference.voice_version_id or reference.cloned_voice_id
        key = narration_idempotency_key(str(project_id), first, str(voice_identity))
        self._submitter.rebuild_authoritative_timeline(
            SynthesisRequest(
                project_id,
                owner_id,
                voice_version_id,
                first,
                key,
                locale,
                reference.reference_zip,
                reference.reference_sha256,
                key,
                provider=reference.provider,
                cloned_voice_id=reference.cloned_voice_id,
            )
        )

    def finish_job(
        self, owner_id: UUID, project_id: UUID, job_id: str, failures: tuple[int, ...]
    ) -> None:
        script = self._repository.get_script(owner_id, project_id)
        if script is None:
            self._repository.finish_job(job_id, "FAILED", failures)
            return
        narrated = self._repository.list_narrated(owner_id, project_id)
        narrated_count = len(narrated)
        expected_count = sum(
            1 for item in script.sentences if not is_nonvisual_script_separator(item.text)
        )
        auto_mode = self._repository.project_production_mode(owner_id, project_id) == "AUTO"
        if not failures and narrated_count == expected_count:
            self.finalize_timeline(owner_id, project_id)
            if self._director_materializer is not None:
                self._director_materializer(owner_id, project_id)
            if auto_mode:
                self._repository.save_blocking_reason(owner_id, project_id, None)
            self._repository.finish_job(job_id, "COMPLETED", ())
            return
        state = "FAILED" if narrated_count == 0 else "PARTIAL_FAILED"
        unresolved = tuple(
            sorted(
                set(failures)
                | {
                    item.sequence
                    for item in script.sentences
                    if not is_nonvisual_script_separator(item.text)
                    and item.sequence
                    not in {narration.sentence.sequence for narration in narrated}
                }
            )
        )
        if auto_mode:
            # §15.2/§15.4：AUTO 项目失败句按预算自动重投；无法自动恢复的落项目级阻断原因。
            blocked = self._schedule_auto_retries(owner_id, project_id, narrated, unresolved)
            self._repository.save_blocking_reason(owner_id, project_id, blocked or None)
        self._repository.finish_job(job_id, state, unresolved)

    def _schedule_auto_retries(
        self,
        owner_id: UUID,
        project_id: UUID,
        narrated: tuple[NarratedSentence, ...],
        unresolved: tuple[int, ...],
        *,
        skip_unattempted: bool = False,
    ) -> dict[int, str]:
        """逐句自动重投；返回无法自动恢复的阻断原因（句序 → 稳定原因码）。

        §15.9：提交预算按当前预算轮次（projects.auto_budget_round）计数；恢复后
        新一轮内旧轮终态尝试不再占用预算，重投 job_id 按轮次确定性派生。
        skip_unattempted（恢复路径）：从未产生过尝试的句子由自身 job 链的
        finish_job 覆盖，此处不重复入队。
        """
        attempts = self._repository.list_attempts(owner_id, project_id)
        budget_round = self._repository.project_auto_budget_round(owner_id, project_id)
        narrated_sequences = {item.sentence.sequence for item in narrated}
        blocked: dict[int, str] = {}
        now = datetime.now(UTC)
        for sequence in unresolved:
            if sequence in narrated_sequences:
                continue
            if skip_unattempted and not any(
                item.sentence_sequence == sequence for item in attempts
            ):
                continue
            sentence_attempts = [
                item
                for item in attempts
                if item.sentence_sequence == sequence and item.budget_round == budget_round
            ]
            latest = (
                max(
                    sentence_attempts,
                    key=lambda item: _attempt_ordinal(item.idempotency_key),
                )
                if sentence_attempts
                else None
            )
            if latest is not None and not narration_auto_retryable(latest.state):
                blocked[sequence] = (
                    narration_provider_blocking_code(latest.safe_error_code)
                    # §23.6：号池无可用 key / key 失效属 Provider 账号级事实，
                    # 不消耗对象预算、不自动重投，由用户手动恢复开启新一轮授权。
                    or (
                        "narration_provider_rejected"
                        if latest.state is NarrationAttemptState.PROVIDER_REJECTED
                        else "narration_archive_failed"
                        if latest.state is NarrationAttemptState.ARCHIVE_FAILED
                        else "narration_submission_incomplete"
                    )
                )
                continue
            if narration_auto_retry_exhausted(len(sentence_attempts)):
                blocked[sequence] = "narration_submission_budget_exhausted"
                continue
            if (
                latest is not None
                and latest.retry_not_before
                and not_before_pending(latest.retry_not_before, now)
            ):
                not_before: str | None = latest.retry_not_before
            elif sentence_attempts:
                not_before = (
                    now
                    + timedelta(
                        seconds=narration_auto_retry_delay_seconds(len(sentence_attempts))
                    )
                ).isoformat()
            else:
                not_before = None
            job = NarrationJob(
                "narration_auto_retry_"
                + hashlib.sha256(
                    f"{project_id}:{sequence}:r{budget_round}:{len(sentence_attempts) + 1}".encode()
                ).hexdigest(),
                owner_id,
                project_id,
                (sequence,),
                "PENDING",
                not_before=not_before,
            )
            # 未知态/质检失败迁移到 SAFE_TO_RETRY；已是该态时原样记录调度事件。
            retryable_attempt = (
                replace(latest, state=NarrationAttemptState.SAFE_TO_RETRY)
                if latest is not None
                and latest.state is not NarrationAttemptState.SAFE_TO_RETRY
                else latest
            )
            self._repository.schedule_auto_retry(retryable_attempt, "auto_retry_scheduled", job)
        return blocked

    def resume_auto_narration(self, owner_id: UUID, project_id: UUID) -> dict[int, str]:
        """§15.9 恢复入口：按当前预算轮次重估未解决句子并重新入队。

        幂等：重投 job_id 由 (project, 句序, 轮次, 轮内序号) 确定性派生，重复调用
        不产生第二个排队任务；仍无法自动恢复的句子保留并返回稳定阻断原因。
        仅 AUTO 项目适用；MANUAL 项目返回空映射且不改动任何状态。
        """
        if self._repository.project_production_mode(owner_id, project_id) != "AUTO":
            return {}
        script = self._repository.get_script(owner_id, project_id)
        if script is None:
            return {}
        narrated = self._repository.list_narrated(owner_id, project_id)
        narrated_sequences = {item.sentence.sequence for item in narrated}
        unresolved = tuple(
            sorted(
                item.sequence
                for item in script.sentences
                if not is_nonvisual_script_separator(item.text)
                and item.sequence not in narrated_sequences
            )
        )
        if not unresolved:
            self._repository.save_blocking_reason(owner_id, project_id, None)
            return {}
        blocked = self._schedule_auto_retries(
            owner_id, project_id, narrated, unresolved, skip_unattempted=True
        )
        self._repository.save_blocking_reason(owner_id, project_id, blocked or None)
        return blocked

    def status(self, owner_id: object, project_id: object) -> dict[str, object]:
        from uuid import UUID

        if not isinstance(owner_id, UUID) or not isinstance(project_id, UUID):
            raise DomainError("invalid_request", "Owner and project identifiers are invalid.")
        self._projects.get_draft_script(owner_id, project_id)
        script = self._repository.get_script(owner_id, project_id)
        narrated = self._repository.list_narrated(owner_id, project_id)
        timeline = self._repository.get_timeline(owner_id, project_id)
        groups = self._repository.get_groups(owner_id, project_id)
        attempts = self._repository.list_attempts(owner_id, project_id)
        job = self._repository.get_job(owner_id, project_id)
        unknown = any(item.state is NarrationAttemptState.REMOTE_STATE_UNKNOWN for item in attempts)
        authoritative_ready = False
        if script is not None:
            try:
                measured = build_measured_timeline(script, narrated)
                measured_groups = group_timeline(measured)
                authoritative_ready = measured == timeline and measured_groups == groups
            except DomainError:
                authoritative_ready = False
        state = (
            "REMOTE_STATE_UNKNOWN"
            if unknown
            else "READY"
            if authoritative_ready
            else job.state
            if job is not None
            else "RUNNING"
            if any(item.state is NarrationAttemptState.SUBMISSION_STARTED for item in attempts)
            else "QUEUED"
            if any(item.state is NarrationAttemptState.INTENT_RECORDED for item in attempts)
            else "NOT_READY"
        )
        latest_attempts: dict[int, NarrationAttempt] = {}
        for attempt in attempts:
            previous = latest_attempts.get(attempt.sentence_sequence)
            if previous is None or _attempt_ordinal(attempt.idempotency_key) > _attempt_ordinal(
                previous.idempotency_key
            ):
                latest_attempts[attempt.sentence_sequence] = attempt
        narrated_sequences = {item.sentence.sequence for item in narrated}
        sentence_statuses: list[dict[str, object]] = []
        for sentence in script.sentences if script is not None else ():
            assert script is not None
            if is_nonvisual_script_separator(sentence.text):
                continue
            latest = latest_attempts.get(sentence.sequence)
            sentence_statuses.append(
                {
                    "sentence_id": authoritative_sentence_id(
                        project_id, script.source_sha256, sentence.sequence
                    ),
                    "sequence": sentence.sequence,
                    "text": sentence.text,
                    "idempotency_key": latest.idempotency_key if latest is not None else None,
                    "safe_error_code": latest.safe_error_code if latest is not None else None,
                    "state": (
                        "COMPLETED"
                        if sentence.sequence in narrated_sequences
                        else "MANUAL_REVIEW_REQUIRED"
                        if latest is not None
                        and latest.state is NarrationAttemptState.SAFE_TO_RETRY
                        and (job is None or job.state not in {"PENDING", "DISPATCHED"})
                        else latest.state.value
                        if latest is not None
                        else "QUEUED"
                        if job is not None and job.state in {"PENDING", "DISPATCHED"}
                        else "NOT_STARTED"
                    ),
                }
            )
        sentence_views = (
            tuple(
                {
                    **asdict(item),
                    "sentence_id": authoritative_sentence_id(
                        project_id, script.source_sha256, item.sentence.sequence
                    ),
                }
                for item in narrated
            )
            if script is not None
            else ()
        )
        group_views = (
            tuple(
                {
                    **asdict(item),
                    "group_id": authoritative_group_id(
                        project_id, script.source_sha256, item.sequence
                    ),
                }
                for item in groups
            )
            if script is not None
            else ()
        )
        return {
            "project_id": str(project_id),
            "state": state,
            "attempts": attempts,
            "sentences": sentence_views,
            "sentence_statuses": sentence_statuses,
            "timeline": timeline,
            "groups": group_views,
            "total_duration_ms": timeline[-1].end_ms if timeline else 0,
            "job": job,
        }

    async def reconcile(
        self,
        owner_id: object,
        project_id: object,
        idempotency_key: str,
        resolution: str,
        external_evidence: str,
    ) -> None:
        from uuid import UUID

        if not isinstance(owner_id, UUID) or not isinstance(project_id, UUID):
            raise DomainError("invalid_request", "Owner and project identifiers are invalid.")
        self._write_guard.assert_writable(owner_id, project_id)
        self._projects.get_draft_script(owner_id, project_id)
        attempt = self._repository.attempt(owner_id, idempotency_key)
        if attempt is None or attempt.project_id != project_id:
            raise DomainError("not_found", "Resource not found.")
        evidence_hash = hashlib.sha256(external_evidence.strip().encode()).hexdigest()
        disposition_code = f"manual:{resolution}:{evidence_hash}"
        if attempt.safe_error_code and attempt.safe_error_code.startswith("manual:"):
            if attempt.safe_error_code == disposition_code:
                return
            raise DomainError("idempotency_conflict", "This attempt already has a disposition.")
        if attempt.state not in {
            NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            NarrationAttemptState.OUTPUT_INVALID,
            NarrationAttemptState.ARCHIVE_FAILED,
            NarrationAttemptState.SAFE_TO_RETRY,
        }:
            raise DomainError(
                "reconciliation_not_allowed", "Attempt does not require reconciliation."
            )
        states = {
            "CONFIRMED_NOT_ACCEPTED": NarrationAttemptState.SAFE_TO_RETRY,
            "CONFIRMED_FAILED": NarrationAttemptState.PROVIDER_REJECTED,
            "RETRY_MISSING": NarrationAttemptState.SAFE_TO_RETRY,
            "RETRY_INVALID_OUTPUT": NarrationAttemptState.SAFE_TO_RETRY,
        }
        state = states.get(resolution)
        if state is None:
            raise DomainError(
                "invalid_reconciliation", "Manual reconciliation resolution is invalid."
            )
        if resolution == "CONFIRMED_NOT_ACCEPTED" and attempt.state not in {
            NarrationAttemptState.REMOTE_STATE_UNKNOWN,
        }:
            raise DomainError(
                "invalid_reconciliation",
                "A received provider result cannot be dispositioned as not accepted.",
            )
        if (
            resolution == "RETRY_MISSING"
            and attempt.state is not NarrationAttemptState.SAFE_TO_RETRY
        ):
            raise DomainError(
                "invalid_reconciliation", "Only a manifest-confirmed missing unit can be retried."
            )
        if (
            resolution == "RETRY_INVALID_OUTPUT"
            and attempt.state is not NarrationAttemptState.OUTPUT_INVALID
        ):
            raise DomainError(
                "invalid_reconciliation",
                "Only a locally rejected provider output can be confirmed for retry.",
            )
        if resolution == "CONFIRMED_NOT_ACCEPTED" and not external_evidence.strip():
            raise DomainError(
                "reconciliation_evidence_required",
                "External evidence is required before creating a new provider attempt.",
            )
        reconciled = replace(
            attempt,
            state=state,
            safe_error_code=disposition_code,
        )
        job: NarrationJob | None = None
        if state is NarrationAttemptState.SAFE_TO_RETRY:
            job_id = "narration_reconcile_" + hashlib.sha256(
                f"{project_id}:{idempotency_key}:{resolution}".encode()
            ).hexdigest()
            script = self._repository.get_script(owner_id, project_id)
            if script is None:
                raise DomainError("narration_not_ready", "Immutable script is not archived.")
            job = NarrationJob(
                job_id,
                owner_id,
                project_id,
                (attempt.sentence_sequence,),
                "PENDING",
            )
        self._repository.reconcile_and_enqueue(reconciled, "manual_reconciliation", job)


class PendingNarrationDispatcher:
    """Supervised outbox dispatcher; startup immediately drains work left by a lost response."""

    def __init__(self, service: ProjectNarrationService, interval_seconds: float = 2.0) -> None:
        self._service = service
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self.failure_count = 0
        self.last_error: str | None = None

    @property
    def healthy(self) -> bool:
        return self._task is not None and not self._task.done() and self.last_error is None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="narration-outbox-dispatcher")
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def run(self) -> None:
        while True:
            try:
                await self._service.dispatch_pending_jobs()
                self.failure_count = 0
                self.last_error = None
                delay = self._interval_seconds
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.failure_count += 1
                self.last_error = f"{type(error).__name__}: {error}"
                delay = min(self._interval_seconds * (2**self.failure_count), 60.0)
            await asyncio.sleep(delay)
