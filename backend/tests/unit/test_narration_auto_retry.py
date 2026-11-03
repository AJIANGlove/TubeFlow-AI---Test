from __future__ import annotations

import hashlib
import io
import struct
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zipfile import ZIP_STORED, ZipFile

import pytest

from tubeflow_test.adapters.audio_qc import PcmWavNormalizer
from tubeflow_test.adapters.persistence.narration import (
    InMemoryAudioArchive,
    InMemoryNarrationRepository,
    _attempt_from_json,
    _attempt_json,
    _job_from_json,
    _job_json,
)
from tubeflow_test.application.narration import (
    ProjectNarrationService,
    SubmitSentenceNarration,
)
from tubeflow_test.application.narration_ports import (
    NarrationJob,
    SynthesisRequest,
    SynthesisResult,
    VoiceReference,
    not_before_pending,
)
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError, ProviderRateLimitedError
from tubeflow_test.domain.narration import (
    NARRATION_AUTO_RETRY_BACKOFF_SECONDS,
    NarrationAttempt,
    NarrationAttemptState,
    ScriptVersion,
    narration_auto_retry_delay_seconds,
    narration_auto_retry_exhausted,
    narration_auto_retryable,
    narration_idempotency_key,
    segment_pasted_script,
)

OWNER = UUID("00000000-0000-0000-0000-000000000001")
PROJECT = UUID("00000000-0000-0000-0000-000000000010")
VOICE = UUID("00000000-0000-0000-0000-000000000020")

TEXT_ONE = "This is one sentence."
TEXT_TWO = "This is one sentence. This is another sentence."


def _wav(duration_ms: int) -> bytes:
    output = io.BytesIO()
    rate = 8000
    samples = duration_ms * rate // 1000
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        frames = (struct.pack("<h", 1000) + struct.pack("<h", -1000)) * (samples // 2)
        frames += struct.pack("<h", 1000) * (samples % 2)
        audio.writeframes(frames)
    return output.getvalue()


def _zip_for(script: ScriptVersion, wav: bytes | None = None) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_STORED) as archive:
        for sentence in script.sentences:
            name = f"{sentence.sequence:04d}-{sentence.text_sha256[:12]}.wav"
            archive.writestr(name, wav if wav is not None else _wav(500))
    return output.getvalue()


def _script(text: str = TEXT_ONE) -> ScriptVersion:
    return segment_pasted_script(text, SupportedLocale.EN_US)


class _Provider:
    def __init__(self, output: bytes | None = None, error: DomainError | None = None) -> None:
        self.output, self.error, self.submissions = output, error, 0

    async def submit(self, request: SynthesisRequest) -> SynthesisResult:
        self.submissions += 1
        if self.error:
            raise self.error
        return SynthesisResult(request.provider_call_id, "completed", self.output)

    async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
        self.submissions += 1
        if self.error:
            raise self.error
        return SynthesisResult(requests[0].provider_call_id, "completed", self.output)


class _FlakyProvider(_Provider):
    """第一次批调用返回损坏音频（OUTPUT_INVALID），之后正常；重投子集只含被请求句。"""

    def __init__(self, script: ScriptVersion) -> None:
        super().__init__()
        self._script = script

    async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
        self.submissions += 1
        wav = b"invalid audio" if self.submissions == 1 else _wav(500)
        sentences = {
            sentence.sequence: sentence for sentence in self._script.sentences
        }
        output = io.BytesIO()
        with ZipFile(output, "w", ZIP_STORED) as archive:
            for item in requests:
                sentence = sentences[item.sentence.sequence]
                name = f"{sentence.sequence:04d}-{sentence.text_sha256[:12]}.wav"
                archive.writestr(name, wav)
        return SynthesisResult(requests[0].provider_call_id, "completed", output.getvalue())


class _ProjectPort:
    def __init__(self, text: str) -> None:
        self.value = (text, SupportedLocale.EN_US, VOICE)

    def get_draft_script(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        return self.value

    def get_frozen_narration(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        return self.value


class _VoicePort:
    def get_reference(
        self, owner_id: UUID, project_id: UUID, locale: SupportedLocale
    ) -> VoiceReference:
        del owner_id, project_id, locale
        payload = b"reference-zip"
        return VoiceReference(VOICE, payload, hashlib.sha256(payload).hexdigest())


class _Writable:
    def assert_writable(self, owner_id: UUID, project_id: UUID) -> None:
        assert (owner_id, project_id) == (OWNER, PROJECT)


class _Starter:
    def __init__(self) -> None:
        self.calls = 0

    async def start(self, **_kwargs: object) -> str:
        self.calls += 1
        return "job"


def _service(
    repo: InMemoryNarrationRepository, provider: _Provider, text: str = TEXT_ONE
) -> ProjectNarrationService:
    return ProjectNarrationService(
        _ProjectPort(text),  # type: ignore[arg-type]
        _VoicePort(),  # type: ignore[arg-type]
        repo,
        SubmitSentenceNarration(provider, repo, InMemoryAudioArchive(), PcmWavNormalizer()),
        _Starter(),  # type: ignore[arg-type]
        _Writable(),  # type: ignore[arg-type]
    )


def _attempt(
    state: NarrationAttemptState,
    ordinal: int = 1,
    sequence: int = 1,
    text: str = TEXT_ONE,
    **kwargs: object,
) -> NarrationAttempt:
    script = _script(text)
    sentence = next(item for item in script.sentences if item.sequence == sequence)
    base = narration_idempotency_key(str(PROJECT), sentence, str(VOICE))
    key = base if ordinal == 1 else f"{base}:attempt:{ordinal}"
    return NarrationAttempt(
        PROJECT, OWNER, sequence, key, "a" * 64, state, f"call-{ordinal}", **kwargs  # type: ignore[arg-type]
    )


def _setup(
    text: str = TEXT_ONE, *, mode: str = "AUTO"
) -> tuple[InMemoryNarrationRepository, ScriptVersion]:
    repo = InMemoryNarrationRepository()
    repo.production_mode = mode
    script = _script(text)
    repo.save_script(OWNER, PROJECT, script)
    return repo, script


def _scheduled_jobs(repo: InMemoryNarrationRepository) -> list[NarrationJob]:
    current = repo.get_job(OWNER, PROJECT)
    queued = repo._pending_recovery_jobs.get((OWNER, PROJECT), [])
    return ([current] if current is not None else []) + list(queued)


# --- 领域规则边界 -----------------------------------------------------------


def test_auto_retry_delay_schedule_and_retry_after() -> None:
    assert NARRATION_AUTO_RETRY_BACKOFF_SECONDS == (5.0, 15.0, 30.0)
    assert narration_auto_retry_delay_seconds(1) == 5.0
    assert narration_auto_retry_delay_seconds(2) == 15.0
    assert narration_auto_retry_delay_seconds(3) == 30.0
    assert narration_auto_retry_delay_seconds(1, retry_after_seconds=20.0) == 20.0
    assert narration_auto_retry_delay_seconds(2, retry_after_seconds=2.0) == 15.0


def test_auto_retry_budget_and_retryable_states() -> None:
    assert not narration_auto_retry_exhausted(0)
    assert not narration_auto_retry_exhausted(2)
    assert narration_auto_retry_exhausted(3)
    assert narration_auto_retryable(NarrationAttemptState.SAFE_TO_RETRY)
    assert narration_auto_retryable(NarrationAttemptState.REMOTE_STATE_UNKNOWN)
    assert narration_auto_retryable(NarrationAttemptState.OUTPUT_INVALID)
    assert not narration_auto_retryable(NarrationAttemptState.PROVIDER_REJECTED)
    assert not narration_auto_retryable(NarrationAttemptState.ARCHIVE_FAILED)
    assert not narration_auto_retryable(NarrationAttemptState.SUBMISSION_STARTED)


def test_attempt_and_job_serde_round_trip_with_scheduling_fields() -> None:
    attempt = _attempt(
        NarrationAttemptState.SAFE_TO_RETRY,
        retry_not_before="2026-09-15T00:00:00+00:00",
    )
    assert _attempt_from_json(_attempt_json(attempt)) == attempt
    job = NarrationJob(
        "job-1", OWNER, PROJECT, (1,), "PENDING", not_before="2026-09-15T00:00:00+00:00"
    )
    assert _job_from_json(_job_json(job)) == job
    legacy = _job_json(job)
    del legacy["not_before"]
    assert _job_from_json(legacy).not_before is None
    legacy_attempt = _attempt_json(attempt)
    del legacy_attempt["retry_not_before"]
    assert _attempt_from_json(legacy_attempt).retry_not_before is None


def test_not_before_pending_parsing() -> None:
    now = datetime.now(UTC)
    assert not not_before_pending(None, now)
    assert not not_before_pending("not-a-date", now)
    assert not_before_pending((now + timedelta(seconds=5)).isoformat(), now)
    assert not not_before_pending((now - timedelta(seconds=5)).isoformat(), now)


def test_pending_jobs_respect_not_before() -> None:
    future = NarrationJob(
        "future",
        OWNER,
        PROJECT,
        (1,),
        "PENDING",
        not_before=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
    )
    repo = InMemoryNarrationRepository()
    repo.save_job(future)
    assert repo.pending_jobs() == ()
    repo = InMemoryNarrationRepository()
    repo.save_job(NarrationJob("due", OWNER, PROJECT, (2,), "PENDING"))
    assert [job.job_id for job in repo.pending_jobs()] == ["due"]


# --- 429 分类修正 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_rate_limited_is_retryable_with_retry_after_timestamp() -> None:
    repo, _script_ = _setup(TEXT_TWO)
    provider = _Provider(
        error=ProviderRateLimitedError("provider_rate_limited", "429", retry_after_seconds=20.0)
    )
    service = _service(repo, provider, TEXT_TWO)
    before = datetime.now(UTC)
    with pytest.raises(DomainError) as failure:
        await service.synthesize_batch(OWNER, PROJECT, (1, 2))
    assert failure.value.code == "provider_rate_limited"
    assert provider.submissions == 1  # 批量调用一次，整批失败
    for sequence in (1, 2):
        attempts = [
            item
            for item in repo.list_attempts(OWNER, PROJECT)
            if item.sentence_sequence == sequence
        ]
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.state is NarrationAttemptState.SAFE_TO_RETRY  # AUTO：429 不再落既定失败
        assert attempt.safe_error_code == "provider_rate_limited"
        assert attempt.retry_not_before is not None
        delay = datetime.fromisoformat(attempt.retry_not_before) - before
        # Retry-After(20s) 长于固定退避(5s)，尊重 Retry-After。
        assert 19.0 <= delay.total_seconds() <= 21.0


@pytest.mark.asyncio
async def test_manual_rate_limited_stays_provider_rejected() -> None:
    repo, _script_ = _setup(TEXT_TWO, mode="MANUAL")
    provider = _Provider(error=DomainError("provider_rate_limited", "429"))
    service = _service(repo, provider, TEXT_TWO)
    with pytest.raises(DomainError) as failure:
        await service.synthesize_batch(OWNER, PROJECT, (1, 2))
    assert failure.value.code == "provider_rate_limited"
    for sequence in (1, 2):
        attempt = next(
            item
            for item in repo.list_attempts(OWNER, PROJECT)
            if item.sentence_sequence == sequence
        )
        assert attempt.state is NarrationAttemptState.PROVIDER_REJECTED  # MANUAL 不变
        assert attempt.retry_not_before is None


# --- finish_job 自动重投 ----------------------------------------------------


@pytest.mark.asyncio
async def test_auto_finish_job_schedules_retry_with_persisted_backoff() -> None:
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    provider = _Provider(error=DomainError("provider_not_reached", "connect timeout"))
    service = _service(repo, provider)
    with pytest.raises(DomainError):
        await service.synthesize_batch(OWNER, PROJECT, (1,))

    before = datetime.now(UTC)
    service.finish_job(OWNER, PROJECT, "original", (1,))

    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.job_id.startswith("narration_auto_retry_")
    assert current.sentence_sequences == (1,)
    assert current.state == "PENDING"
    assert current.not_before is not None
    delay = datetime.fromisoformat(current.not_before) - before
    assert 4.0 <= delay.total_seconds() <= 6.0  # 第 1 次失败后 5s
    assert not_before_pending(current.not_before, datetime.now(UTC))
    assert repo.pending_jobs() == ()  # 未到退避时间，不分发、不阻塞 worker
    assert repo.blocking_reason(OWNER, PROJECT) is None
    assert [event for _a, event in repo.events].count("auto_retry_scheduled") == 1
    original = repo._finished_jobs["original"]
    assert original.state == "FAILED"
    # 原 attempt 已是 SAFE_TO_RETRY（分类落库），无需状态迁移即可被重投 job 复用。
    attempt = repo.attempt(OWNER, _attempt(NarrationAttemptState.INTENT_RECORDED).idempotency_key)
    assert attempt is not None and attempt.state is NarrationAttemptState.SAFE_TO_RETRY


@pytest.mark.asyncio
async def test_auto_finish_job_resubmits_unknown_state_within_budget() -> None:
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    repo.save_attempt(
        _attempt(
            NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            safe_error_code="provider_submission_unknown",
        )
    )
    service = _service(repo, _Provider(_zip_for(_script())))
    service.finish_job(OWNER, PROJECT, "original", (1,))

    attempt = repo.attempt(OWNER, _attempt(NarrationAttemptState.INTENT_RECORDED).idempotency_key)
    assert attempt is not None
    assert attempt.state is NarrationAttemptState.SAFE_TO_RETRY  # AUTO：未知态直接重提
    assert attempt.safe_error_code == "provider_submission_unknown"
    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.state == "PENDING"
    assert current.not_before is not None  # 计入预算的第一次重投：5s 退避
    assert repo.blocking_reason(OWNER, PROJECT) is None


@pytest.mark.asyncio
async def test_auto_finish_job_resubmits_output_invalid() -> None:
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    repo.save_attempt(
        _attempt(NarrationAttemptState.OUTPUT_INVALID, safe_error_code="audio_qc_failed")
    )
    service = _service(repo, _Provider())
    service.finish_job(OWNER, PROJECT, "original", (1,))
    attempt = repo.attempt(OWNER, _attempt(NarrationAttemptState.INTENT_RECORDED).idempotency_key)
    assert attempt is not None and attempt.state is NarrationAttemptState.SAFE_TO_RETRY
    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.sentence_sequences == (1,)


@pytest.mark.asyncio
async def test_auto_finish_job_blocks_when_budget_exhausted() -> None:
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    for ordinal in (1, 2, 3):
        repo.save_attempt(
            _attempt(
                NarrationAttemptState.SAFE_TO_RETRY,
                ordinal,
                safe_error_code="provider_not_reached",
            )
        )
    service = _service(repo, _Provider())
    service.finish_job(OWNER, PROJECT, "original", (1,))

    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.job_id == "original"  # 无新 job 晋级
    assert current.state == "FAILED"
    blocking = repo.blocking_reason(OWNER, PROJECT)
    assert blocking is not None
    assert blocking["sequences"] == {"1": "narration_submission_budget_exhausted"}
    assert not any(event == "auto_retry_scheduled" for _a, event in repo.events)


@pytest.mark.asyncio
async def test_auto_finish_job_resubmits_network_not_reached() -> None:
    """连接未建立的句子必须自动重投，不得落成需要人工恢复的阻断原因。"""
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    repo.save_attempt(
        _attempt(NarrationAttemptState.SAFE_TO_RETRY, safe_error_code="provider_not_reached")
    )
    service = _service(repo, _Provider(output=_zip_for(_script_)))
    service.finish_job(OWNER, PROJECT, "original", (1,))

    current = repo.get_job(OWNER, PROJECT)
    assert current is not None
    assert current.job_id.startswith("narration_auto_retry_")
    assert repo.blocking_reason(OWNER, PROJECT) is None
    attempt = repo.attempt(OWNER, _attempt(NarrationAttemptState.INTENT_RECORDED).idempotency_key)
    assert attempt is not None and attempt.state is NarrationAttemptState.SAFE_TO_RETRY


@pytest.mark.asyncio
async def test_auto_finish_job_blocks_on_provider_rejected() -> None:
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    repo.save_attempt(
        _attempt(NarrationAttemptState.PROVIDER_REJECTED, safe_error_code="provider_rejected")
    )
    service = _service(repo, _Provider())
    service.finish_job(OWNER, PROJECT, "original", (1,))
    blocking = repo.blocking_reason(OWNER, PROJECT)
    assert blocking is not None
    assert blocking["sequences"] == {"1": "narration_provider_rejected"}
    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.job_id == "original"
    assert current.state == "FAILED"
    assert not any(event == "auto_retry_scheduled" for _a, event in repo.events)


@pytest.mark.asyncio
async def test_auto_finish_job_schedules_never_submitted_sentence_immediately() -> None:
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    service = _service(repo, _Provider())
    service.finish_job(OWNER, PROJECT, "original", (1,))  # 未产生任何 attempt
    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.job_id.startswith("narration_auto_retry_")
    assert current.not_before is None  # 零次提交：立即可分发
    assert repo.pending_jobs() == (current,)
    assert repo.list_attempts(OWNER, PROJECT) == ()


@pytest.mark.asyncio
async def test_auto_finish_job_queues_one_retry_job_per_sentence() -> None:
    repo, _script_ = _setup(TEXT_TWO)
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1, 2), "DISPATCHED"))
    repo.save_attempt(
        _attempt(
            NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            sequence=1,
            text=TEXT_TWO,
            safe_error_code="provider_submission_unknown",
        )
    )
    repo.save_attempt(
        _attempt(
            NarrationAttemptState.OUTPUT_INVALID,
            sequence=2,
            text=TEXT_TWO,
            safe_error_code="audio_qc_failed",
        )
    )
    service = _service(repo, _Provider(), TEXT_TWO)
    service.finish_job(OWNER, PROJECT, "original", (1, 2))
    jobs = _scheduled_jobs(repo)
    assert [job.sentence_sequences for job in jobs] == [(1,), (2,)]
    assert len({job.job_id for job in jobs}) == 2
    assert repo.blocking_reason(OWNER, PROJECT) is None


@pytest.mark.asyncio
async def test_manual_finish_job_does_not_auto_retry() -> None:
    repo, _script_ = _setup(mode="MANUAL")
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    repo.save_attempt(
        _attempt(NarrationAttemptState.SAFE_TO_RETRY, safe_error_code="provider_not_reached")
    )
    service = _service(repo, _Provider())
    service.finish_job(OWNER, PROJECT, "original", (1,))
    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.job_id == "original"
    assert current.state == "FAILED"
    assert current.failure_sequences == (1,)
    assert repo.blocking_reason(OWNER, PROJECT) is None
    assert not any(event == "auto_retry_scheduled" for _a, event in repo.events)


@pytest.mark.asyncio
async def test_auto_finish_job_replay_is_idempotent() -> None:
    repo, _script_ = _setup()
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1,), "DISPATCHED"))
    repo.save_attempt(
        _attempt(
            NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            safe_error_code="provider_submission_unknown",
        )
    )
    service = _service(repo, _Provider())
    service.finish_job(OWNER, PROJECT, "original", (1,))
    service.finish_job(OWNER, PROJECT, "original", (1,))  # finish activity 重试重放
    current = repo.get_job(OWNER, PROJECT)
    assert current is not None and current.job_id.startswith("narration_auto_retry_")
    assert len(repo._finished_jobs) == 1  # 原 job 只终结一次
    assert repo._pending_recovery_jobs.get((OWNER, PROJECT), []) == []  # 无重复排队
    assert [event for _a, event in repo.events].count("auto_retry_scheduled") == 1
    assert len(repo.list_attempts(OWNER, PROJECT)) == 1  # 不产生重复 attempt / 重复 job


# --- 退避闸门与完整恢复闭环 -------------------------------------------------


@pytest.mark.asyncio
async def test_auto_gate_defers_backoff_window_and_submits_when_due() -> None:
    repo, script = _setup()
    failing = _Provider(error=DomainError("provider_not_reached", "connect timeout"))
    service = _service(repo, failing)
    with pytest.raises(DomainError):
        await service.synthesize_batch(OWNER, PROJECT, (1,))
    assert failing.submissions == 1

    provider = _Provider(_zip_for(script))
    service = _service(repo, provider)
    blocked = await service.synthesize_batch(OWNER, PROJECT, (1,))
    assert blocked == (1,)
    assert provider.submissions == 0  # 退避窗口内不重投

    attempt = repo.attempt(OWNER, _attempt(NarrationAttemptState.INTENT_RECORDED).idempotency_key)
    assert attempt is not None and attempt.retry_not_before is not None
    repo.save_attempt(
        replace(
            attempt,
            retry_not_before=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
    )
    assert await service.synthesize_batch(OWNER, PROJECT, (1,)) == ()
    assert provider.submissions == 1  # 退避到期后正常重投
    assert len(repo.list_narrated(OWNER, PROJECT)) == 1


@pytest.mark.asyncio
async def test_auto_gate_ignores_manual_recovery_disposition() -> None:
    repo, script = _setup()
    failing = _Provider(error=DomainError("provider_not_reached", "connect timeout"))
    service = _service(repo, failing)
    with pytest.raises(DomainError):
        await service.synthesize_batch(OWNER, PROJECT, (1,))
    attempt = repo.attempt(OWNER, _attempt(NarrationAttemptState.INTENT_RECORDED).idempotency_key)
    assert attempt is not None
    # 用户明确恢复（manual: 处置）不受自动退避窗口限制。
    repo.save_attempt(
        replace(attempt, safe_error_code="manual:RETRY_MISSING:" + "b" * 64)
    )
    provider = _Provider(_zip_for(script))
    service = _service(repo, provider)
    assert await service.synthesize_batch(OWNER, PROJECT, (1,)) == ()
    assert provider.submissions == 1


@pytest.mark.asyncio
async def test_auto_retry_full_cycle_recovers_and_completes() -> None:
    repo, script = _setup(TEXT_TWO)
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1, 2), "DISPATCHED"))
    provider = _FlakyProvider(script)
    service = _service(repo, provider, TEXT_TWO)

    failures = await service.synthesize_batch(OWNER, PROJECT, (1, 2))
    assert failures == (1, 2)  # 第一次批调用音频损坏 → OUTPUT_INVALID
    service.finish_job(OWNER, PROJECT, "original", failures)

    first_retry = repo.get_job(OWNER, PROJECT)
    assert first_retry is not None and first_retry.job_id.startswith("narration_auto_retry_")
    assert first_retry.sentence_sequences == (1,)
    repo.mark_job_dispatched(first_retry.job_id)
    assert await service.synthesize_batch(OWNER, PROJECT, first_retry.sentence_sequences) == ()
    service.finish_job(OWNER, PROJECT, first_retry.job_id, ())

    second_retry = repo.get_job(OWNER, PROJECT)
    assert second_retry is not None and second_retry.sentence_sequences == (2,)
    assert second_retry.job_id != first_retry.job_id
    repo.mark_job_dispatched(second_retry.job_id)
    assert await service.synthesize_batch(OWNER, PROJECT, second_retry.sentence_sequences) == ()
    assert provider.submissions == 3
    service.finish_job(OWNER, PROJECT, second_retry.job_id, ())

    job = repo.get_job(OWNER, PROJECT)
    assert job is not None and job.state == "COMPLETED"
    assert len(repo.list_narrated(OWNER, PROJECT)) == 2
    assert repo.get_timeline(OWNER, PROJECT)  # 全部成功后构建实测时间轴
    assert repo.blocking_reason(OWNER, PROJECT) is None
    # 原 attempt 不可变保留；重投产生的新 attempt 完成且挂载血缘。
    sentence_one = [
        item
        for item in repo.list_attempts(OWNER, PROJECT)
        if item.sentence_sequence == 1
    ]
    assert len(sentence_one) == 2
    assert sentence_one[0].state is NarrationAttemptState.SAFE_TO_RETRY
    assert sentence_one[1].state is NarrationAttemptState.COMPLETED
    assert sentence_one[1].parent_attempt_id == sentence_one[0].idempotency_key
