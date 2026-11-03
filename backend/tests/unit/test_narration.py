from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import struct
import subprocess
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_STORED, ZipFile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.adapters.audio_qc import (
    AudioProcessingError,
    FfmpegAudioNormalizer,
    FfprobeAudioInspector,
    PcmWavNormalizer,
    inspect_wav,
)
from tubeflow_test.adapters.cosyvoice import (
    CosyVoiceHttpAdapter,
    extract_batch_audio,
    extract_sentence_audio,
)
from tubeflow_test.adapters.persistence.narration import (
    InMemoryAudioArchive,
    InMemoryNarrationRepository,
)
from tubeflow_test.api.narration_router import create_router
from tubeflow_test.application.narration import (
    PendingNarrationDispatcher,
    ProjectNarrationService,
    SubmitSentenceNarration,
    _request_hash,
)
from tubeflow_test.application.narration_ports import (
    NarrationJob,
    NormalizedAudio,
    SynthesisRequest,
    SynthesisResult,
    VoiceReference,
)
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError, NarrationIntentConflictError
from tubeflow_test.domain.narration import (
    ESTIMATED_RISK_THRESHOLD_MS,
    AudioAsset,
    NarratedSentence,
    NarrationAttempt,
    NarrationAttemptState,
    NarrationGroup,
    ScriptSentence,
    ScriptVersion,
    assert_video_generation_allowed,
    authoritative_group_id,
    authoritative_sentence_id,
    build_measured_timeline,
    estimate_narration_duration_ms,
    frozen_script_sha256,
    is_nonvisual_script_separator,
    narration_idempotency_key,
    segment_pasted_script,
)
from tubeflow_test.identity_project.domain import Account, AccountRole

OWNER = UUID("00000000-0000-0000-0000-000000000001")
OTHER = UUID("00000000-0000-0000-0000-000000000002")
PROJECT = UUID("00000000-0000-0000-0000-000000000010")
VOICE = UUID("00000000-0000-0000-0000-000000000020")
COSYVOICE_TOKEN = "dedicated-cosyvoice-token"


def _wav(duration_ms: int, *, width: int = 2, amplitude: int = 1000) -> bytes:
    output = io.BytesIO()
    rate = 8000
    samples = duration_ms * rate // 1000
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        if width == 1:
            high = max(0, min(255, 128 + amplitude))
            low = max(0, min(255, 128 - amplitude))
            frames = bytes((high, low)) * (samples // 2) + bytes((high,)) * (samples % 2)
        else:
            frames = (struct.pack("<h", amplitude) + struct.pack("<h", -amplitude)) * (
                samples // 2
            ) + struct.pack("<h", amplitude) * (samples % 2)
        audio.writeframes(frames)
    return output.getvalue()


def _zip(payload: bytes, name: str = "0001.wav") -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_STORED) as archive:
        archive.writestr(name, payload)
    return output.getvalue()


def _request(duration_ms: int = 1000) -> tuple[SynthesisRequest, bytes]:
    script = segment_pasted_script("This is a sentence.", SupportedLocale.EN_US)
    sentence = script.sentences[0]
    reference = b"reference-zip"
    key = narration_idempotency_key(str(PROJECT), sentence, str(VOICE))
    request = SynthesisRequest(
        PROJECT,
        OWNER,
        VOICE,
        sentence,
        key,
        SupportedLocale.EN_US,
        reference,
        hashlib.sha256(reference).hexdigest(),
        key,
    )
    return request, _zip(_wav(duration_ms), f"0001-{sentence.text_sha256[:12]}.wav")


@pytest.mark.parametrize(
    ("locale", "text"),
    [
        (SupportedLocale.EN_US, "This is one.\nA heading without punctuation\nAnd this is two!"),
        (SupportedLocale.JA_JP, "これは一文です。\n改行の文も保存します\n次の文です！"),
        (SupportedLocale.KO_KR, "이것은 첫 문장입니다.\n줄바꿈 문장입니다\n다음 문장입니다!"),
        (SupportedLocale.ES_ES, "Esta es una frase.\nLa línea sigue\nLa segunda frase."),
        (SupportedLocale.FR_FR, "Ceci est une phrase.\nUne ligne continue\nLa deuxième phrase."),
        (SupportedLocale.PT_BR, "Esta é uma frase.\nUma linha continua\nA segunda frase."),
    ],
)
def test_segmentation_v3_preserves_every_non_whitespace_character(
    locale: SupportedLocale, text: str
) -> None:
    script = segment_pasted_script(text, locale)
    assert script.rule_version == "punctuation-v3"
    assert "".join(
        "".join(script.sentences[i].text.split()) for i in range(len(script.sentences))
    ) == "".join(text.split())
    assert any(
        "punctuation" in sentence.text
        or "改行" in sentence.text
        or "줄바꿈" in sentence.text
        or "línea" in sentence.text
        or "ligne" in sentence.text
        or "linha" in sentence.text
        for sentence in script.sentences
    )


@pytest.mark.parametrize(
    ("locale", "text"),
    [
        (SupportedLocale.EN_US, "Hello world. Hola mundo."),
        (SupportedLocale.KO_KR, "안녕하세요. Hello world."),
        (SupportedLocale.JA_JP, "これは日本語です。 안녕하세요."),
        (SupportedLocale.ES_ES, "Cats sleep."),
        (SupportedLocale.FR_FR, "Cats sleep."),
        (SupportedLocale.PT_BR, "Cats sleep."),
    ],
)
def test_mixed_language_fails_closed(locale: SupportedLocale, text: str) -> None:
    with pytest.raises(DomainError) as failure:
        segment_pasted_script(text, locale)
    assert failure.value.code == "script_language_mismatch"


def test_japanese_kanji_with_kana_is_allowed() -> None:
    assert segment_pasted_script("東京で映像を作ります。", SupportedLocale.JA_JP).sentences
    assert segment_pasted_script("東京映像制作。", SupportedLocale.JA_JP).sentences


@pytest.mark.parametrize(
    ("locale", "text"),
    [
        (SupportedLocale.EN_US, "Cats sleep."),
        (SupportedLocale.ES_ES, "Gatos duermen."),
        (SupportedLocale.FR_FR, "Chats dorment."),
        (SupportedLocale.PT_BR, "Gatos dormem."),
        (SupportedLocale.JA_JP, "東京映像制作。"),
        (SupportedLocale.KO_KR, "고양이는 잠을 잡니다."),
    ],
)
def test_common_short_target_language_sentences_are_accepted(
    locale: SupportedLocale, text: str
) -> None:
    assert segment_pasted_script(text, locale).sentences


def test_japanese_with_clear_english_span_is_rejected() -> None:
    with pytest.raises(DomainError, match="mixed"):
        segment_pasted_script("これは日本語です。 Cats sleep.", SupportedLocale.JA_JP)


@pytest.mark.parametrize("duration", [3000, 15000, 15001])
@pytest.mark.asyncio
async def test_archive_qc_timeline_and_group_boundaries(duration: int) -> None:
    request, output = _request(duration)
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, segment_pasted_script(request.sentence.text, request.language))
    provider = _Provider(output)
    archive = InMemoryAudioArchive()
    use_case = SubmitSentenceNarration(provider, repo, archive, PcmWavNormalizer())
    await use_case.execute(request)
    use_case.rebuild_authoritative_timeline(request)
    narration = repo.list_narrated(OWNER, PROJECT)[0]
    assert narration.raw_asset.asset_id != narration.normalized_asset.asset_id
    assert narration.raw_asset.sha256 != narration.normalized_asset.sha256
    assert narration.sentence.text_sha256 == request.sentence.text_sha256
    assert narration.normalized_qc.measured_duration_ms == duration
    groups = repo.get_groups(OWNER, PROJECT)
    assert groups[0].target_duration_ms == duration
    assert groups[0].video_blocked is (duration > 15000)
    # 单句超 15 秒的分组按已确认规则跳过，项目级门禁不再因被阻止组失败
    assert_video_generation_allowed(groups)


def test_unflagged_overlong_group_still_fails_the_project_video_gate() -> None:
    overlong = NarrationGroup(1, (1,), 0, 15001, 15001, False)
    with pytest.raises(DomainError, match="exceeds"):
        assert_video_generation_allowed((overlong,))

    with pytest.raises(DomainError, match="not ready"):
        assert_video_generation_allowed(())


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


class _SequenceProvider(_Provider):
    def __init__(self, outputs: list[bytes]) -> None:
        super().__init__()
        self.outputs = outputs

    async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
        self.submissions += 1
        return SynthesisResult(requests[0].provider_call_id, "completed", self.outputs.pop(0))


class _ProjectPort:
    def __init__(self, text: str, locale: SupportedLocale = SupportedLocale.EN_US) -> None:
        self.value = (text, locale, VOICE)

    def get_draft_script(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        assert (owner_id, project_id) == (OWNER, PROJECT)
        return self.value

    def get_frozen_narration(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        return self.get_draft_script(owner_id, project_id)


class _VoicePort:
    def get_reference(
        self, owner_id: UUID, project_id: UUID, locale: SupportedLocale
    ) -> VoiceReference:
        # §23.1：音色引用按项目（配音源）解析，不再按 voice_version_id 解析。
        assert (owner_id, project_id, locale) == (OWNER, PROJECT, SupportedLocale.EN_US)
        payload = b"reference-zip"
        return VoiceReference(VOICE, payload, hashlib.sha256(payload).hexdigest())


class _Writable:
    def assert_writable(self, owner_id: UUID, project_id: UUID) -> None:
        assert (owner_id, project_id) == (OWNER, PROJECT)


class _Starter:
    def __init__(self, fail_once: bool = False) -> None:
        self.fail_once, self.calls = fail_once, 0

    async def start(self, **_kwargs: object) -> str:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RuntimeError("response lost")
        return "job"


def _multi_zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_STORED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


@pytest.mark.asyncio
async def test_elevenlabs_narrated_sentence_round_trips_without_public_voice() -> None:
    """§23.1/§23.7：EL 句没有公共音色 id，且需保留 request-id 对账凭证。

    `_narrated_json` 若把 None 写成 "None" 字面量，重放该句时会以 UUID 解析失败，
    表现为续跑/恢复时整批 500。
    """
    from tubeflow_test.adapters.persistence.narration import (
        _narrated_from_json,
        _narrated_json,
    )
    from tubeflow_test.domain.narration import (
        AudioAsset,
        AudioQcResult,
        NarratedSentence,
        ScriptSentence,
    )

    sentence = ScriptSentence(1, "Hello there.", "b" * 64)
    asset = AudioAsset("asset-1", "c" * 64, "audio/wav", 128)
    qc = AudioQcResult(
        passed=True,
        measured_duration_ms=1_000,
        codec="pcm_f32le",
        sample_rate_hz=24_000,
        channels=1,
        sample_width_bytes=4,
        frame_count=24_000,
        rms=0.1,
        peak=100,
        active_sample_ratio=0.9,
    )
    narration = NarratedSentence(
        PROJECT,
        OWNER,
        None,
        SupportedLocale.EN_US,
        "batch-call-el",
        "d" * 64,
        sentence,
        "key-el",
        asset,
        asset,
        qc,
        qc,
        {"loudnorm": "passed"},
        provider_request_id="req-el-1",
    )

    restored = _narrated_from_json(_narrated_json(narration))

    assert restored.voice_version_id is None
    assert restored.provider_request_id == "req-el-1"
    assert restored.sentence == sentence


@pytest.mark.asyncio
async def test_partial_batch_failure_keeps_successful_units_and_marks_authoritative_code() -> None:
    """§23.7：批内确定性失败时，已成功句必须归档，失败句用 Provider 权威码落状态。

    否则整批重投会让已成功句再次提交，重复扣字符费。
    """
    from tubeflow_test.application.narration_ports import NarrationBatchFailure, SynthesisResult

    script = segment_pasted_script(
        "This is one sentence. This is another sentence.", SupportedLocale.EN_US
    )
    reference = b"reference-zip"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"key-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            "batch-call-el",
        )
        for sentence in script.sentences
    )
    first_name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"

    class _PartialProvider(_Provider):
        async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
            self.submissions += 1
            result = SynthesisResult(
                "el:req-1",
                "partial",
                _multi_zip({first_name: _wav(500)}),
                provider_request_id="req-1",
                provider_request_ids=((1, "el:req-1"),),
                failed_sequences=(2,),
                errors=((2, "provider_content_rejected"),),
            )
            raise NarrationBatchFailure("partial", result)

    repo = InMemoryNarrationRepository()
    submitter = SubmitSentenceNarration(
        _PartialProvider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()
    )

    with pytest.raises(NarrationBatchFailure):
        await submitter.execute_batch(requests)

    first = repo.attempt(OWNER, "key-1")
    second = repo.attempt(OWNER, "key-2")
    assert first is not None and first.state is NarrationAttemptState.COMPLETED
    assert first.remote_id == "el:req-1"
    assert len(repo.list_narrated(OWNER, PROJECT)) == 1
    assert second is not None
    assert second.state is NarrationAttemptState.PROVIDER_REJECTED
    assert second.safe_error_code == "provider_content_rejected"


@pytest.mark.asyncio
async def test_partial_batch_with_empty_zip_keeps_authoritative_code() -> None:
    """2026-10-31 事故：批内全部失败时 ZIP 为空，解包失败不得覆盖服务商权威失败码。

    第 199 句（仅 U+200B）被 EL 拒绝后，原实现把 provider_rejected 改写成
    provider_output_invalid，使后续 6 次重试全部按 output_invalid 记账并掩盖真实原因。
    """
    from tubeflow_test.application.narration_ports import NarrationBatchFailure, SynthesisResult

    script = segment_pasted_script("This is one sentence.", SupportedLocale.EN_US)
    reference = b"reference-zip"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"key-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            "batch-call-el",
        )
        for sentence in script.sentences
    )

    class _EmptyZipProvider(_Provider):
        async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
            self.submissions += 1
            result = SynthesisResult(
                "el:req-1",
                "partial",
                _multi_zip({}),
                provider_request_id="req-1",
                provider_request_ids=(),
                failed_sequences=(1,),
                errors=((1, "provider_rejected"),),
            )
            raise NarrationBatchFailure("partial", result)

    repo = InMemoryNarrationRepository()
    submitter = SubmitSentenceNarration(
        _EmptyZipProvider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()
    )

    with pytest.raises(DomainError):
        await submitter.execute_batch(requests)

    attempt = repo.attempt(OWNER, "key-1")
    assert attempt is not None
    assert attempt.state is NarrationAttemptState.PROVIDER_REJECTED
    assert attempt.safe_error_code == "provider_rejected"


@pytest.mark.asyncio
async def test_partial_batch_connect_failure_is_retryable_without_checkpoint() -> None:
    """连接未建立（provider_not_reached）必须落 SAFE_TO_RETRY，且不得挂批次检查点。

    2026-10-29 生产事故：批量部分失败把网络失败写成 PROVIDER_REJECTED，且把整批 ZIP
    挂到该句上，恢复时撞"终态无出边"，活动空转 35 分钟后整条 AUTO 停下等人恢复。
    """
    from tubeflow_test.application.narration_ports import NarrationBatchFailure, SynthesisResult

    script = segment_pasted_script(
        "This is one sentence. This is another sentence.", SupportedLocale.EN_US
    )
    reference = b"reference-zip"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"key-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            "batch-call-el",
        )
        for sentence in script.sentences
    )
    first_name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"

    class _ConnectFailureProvider(_Provider):
        async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
            self.submissions += 1
            result = SynthesisResult(
                "el:req-1",
                "partial",
                _multi_zip({first_name: _wav(500)}),
                provider_request_id="req-1",
                provider_request_ids=((1, "el:req-1"),),
                failed_sequences=(2,),
                errors=((2, "provider_not_reached"),),
            )
            raise NarrationBatchFailure("partial", result)

    repo = InMemoryNarrationRepository()
    repo.production_mode = "AUTO"
    submitter = SubmitSentenceNarration(
        _ConnectFailureProvider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()
    )

    with pytest.raises(NarrationBatchFailure):
        await submitter.execute_batch(requests)

    first = repo.attempt(OWNER, "key-1")
    second = repo.attempt(OWNER, "key-2")
    assert first is not None and first.state is NarrationAttemptState.COMPLETED
    assert second is not None
    assert second.state is NarrationAttemptState.SAFE_TO_RETRY
    assert second.safe_error_code == "provider_not_reached"
    assert second.retry_not_before is not None
    assert repo.provider_checkpoint(OWNER, "key-1") is not None
    assert repo.provider_checkpoint(OWNER, "key-2") is None
    assert any(event == "batch_unit_retryable" for _attempt, event in repo.events)


@pytest.mark.asyncio
async def test_batch_replay_after_terminal_rejection_does_not_raise_stale_transition() -> None:
    """终态拒绝的句子在活动重试时必须被跳过，不得抛 stale_attempt_transition。"""
    from tubeflow_test.application.narration_ports import NarrationBatchFailure, SynthesisResult

    script = segment_pasted_script(
        "This is one sentence. This is another sentence.", SupportedLocale.EN_US
    )
    reference = b"reference-zip"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"key-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            "batch-call-el",
        )
        for sentence in script.sentences
    )
    first_name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"

    class _RejectedProvider(_Provider):
        async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
            self.submissions += 1
            result = SynthesisResult(
                "el:req-1",
                "partial",
                _multi_zip({first_name: _wav(500)}),
                provider_request_id="req-1",
                provider_request_ids=((1, "el:req-1"),),
                failed_sequences=(2,),
                errors=((2, "provider_content_rejected"),),
            )
            raise NarrationBatchFailure("partial", result)

    provider = _RejectedProvider()
    repo = InMemoryNarrationRepository()
    submitter = SubmitSentenceNarration(
        provider, repo, InMemoryAudioArchive(), PcmWavNormalizer()
    )

    with pytest.raises(NarrationBatchFailure):
        await submitter.execute_batch(requests)
    rejected = repo.attempt(OWNER, "key-2")
    assert rejected is not None
    assert rejected.state is NarrationAttemptState.PROVIDER_REJECTED

    assert await submitter.execute_batch(requests) == (2,)
    assert provider.submissions == 1


@pytest.mark.asyncio
async def test_batch_result_persists_per_unit_provider_remote_ids() -> None:
    """§23.7：批量结果里的逐句远端 id 必须落到对应 attempt（拼接链与对账列的事实源）。"""
    script = segment_pasted_script(
        "This is one sentence. This is another sentence.", SupportedLocale.EN_US
    )
    reference = b"reference-zip"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"key-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            "batch-call-el",
        )
        for sentence in script.sentences
    )

    class _RemoteIdProvider(_Provider):
        async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
            self.submissions += 1
            members = {
                f"{item.sentence.sequence:04d}-{item.sentence.text_sha256[:12]}.wav": _wav(500)
                for item in requests
            }
            return SynthesisResult(
                "el:req-2",
                "completed",
                _multi_zip(members),
                provider_request_id="req-2",
                provider_request_ids=((1, "el:req-1"), (2, "el:req-2")),
            )

    repo = InMemoryNarrationRepository()
    submitter = SubmitSentenceNarration(
        _RemoteIdProvider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()
    )

    assert await submitter.execute_batch(requests) == ()

    first = repo.attempt(OWNER, "key-1")
    second = repo.attempt(OWNER, "key-2")
    assert first is not None and first.remote_id == "el:req-1"
    assert second is not None and second.remote_id == "el:req-2"
    assert first.state is NarrationAttemptState.COMPLETED
    assert second.state is NarrationAttemptState.COMPLETED


@pytest.mark.asyncio
async def test_partial_batch_archives_success_and_retries_only_missing_member() -> None:
    script = segment_pasted_script(
        "This is one sentence. This is another sentence.", SupportedLocale.EN_US
    )
    reference = b"reference-zip"
    call_id = "batch-call-1"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"key-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            call_id,
        )
        for sentence in script.sentences
    )
    first_name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"
    repo = InMemoryNarrationRepository()
    archive = InMemoryAudioArchive()
    provider = _Provider(_multi_zip({first_name: _wav(500)}))
    submitter = SubmitSentenceNarration(provider, repo, archive, PcmWavNormalizer())
    failures = await submitter.execute_batch(requests)
    assert failures == (2,)
    assert provider.submissions == 1
    assert repo.find_by_idempotency_key(OWNER, "key-1") is not None
    assert repo.attempt(OWNER, "key-2").state is NarrationAttemptState.SAFE_TO_RETRY  # type: ignore[union-attr]
    assert repo.checkpoint_states[(OWNER, "key-1")] == "ATTACHED"
    assert repo.checkpoint_states[(OWNER, "key-2")] == "ATTACHED"

    second = script.sentences[1]
    second_name = f"0002-{second.text_sha256[:12]}.wav"
    retry = replace(requests[1], idempotency_key="key-2:attempt:2", provider_call_id="batch-call-2")
    retry_provider = _Provider(_multi_zip({second_name: _wav(500)}))
    retry_submitter = SubmitSentenceNarration(retry_provider, repo, archive, PcmWavNormalizer())
    assert await retry_submitter.execute_batch((retry,)) == ()
    assert retry_provider.submissions == 1
    assert len(repo.list_narrated(OWNER, PROJECT)) == 2


@pytest.mark.asyncio
async def test_service_partial_retry_creates_new_attempt_and_ignores_old_batch_checkpoint() -> None:
    text = "This is one sentence. This is another sentence."
    script = segment_pasted_script(text, SupportedLocale.EN_US)
    names = [f"{item.sequence:04d}-{item.text_sha256[:12]}.wav" for item in script.sentences]
    provider = _SequenceProvider(
        [_multi_zip({names[0]: _wav(500)}), _multi_zip({names[1]: _wav(500)})]
    )
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, script)
    service = ProjectNarrationService(
        _ProjectPort(text),  # type: ignore[arg-type]
        _VoicePort(),
        repo,
        SubmitSentenceNarration(provider, repo, InMemoryAudioArchive(), PcmWavNormalizer()),
        _Starter(),
        _Writable(),
    )
    assert await service.synthesize_batch(OWNER, PROJECT, (1, 2)) == (2,)
    assert await service.synthesize_batch(OWNER, PROJECT, (2,)) == ()
    attempts = [item for item in repo.list_attempts(OWNER, PROJECT) if item.sentence_sequence == 2]
    assert len(attempts) == 2
    assert attempts[0].idempotency_key != attempts[1].idempotency_key
    assert attempts[1].parent_attempt_id == attempts[0].idempotency_key
    complete_manifest = tuple(names)
    complete_hash = hashlib.sha256("\n".join(complete_manifest).encode()).hexdigest()
    retry_manifest = (names[1],)
    retry_hash = hashlib.sha256("\n".join(retry_manifest).encode()).hexdigest()
    assert attempts[0].original_complete_manifest == complete_manifest
    assert attempts[0].original_manifest_sha256 == complete_hash
    assert attempts[1].original_complete_manifest == complete_manifest
    assert attempts[1].original_manifest_sha256 == complete_hash
    assert attempts[1].retry_subset_manifest == retry_manifest
    assert attempts[1].retry_subset_manifest_sha256 == retry_hash
    assert attempts[1].original_zip_asset_id is not None
    assert provider.submissions == 2
    assert len(repo.list_narrated(OWNER, PROJECT)) == 2


@pytest.mark.asyncio
async def test_active_submission_lease_is_waiting_not_false_success() -> None:
    request, output = _request()
    repo = InMemoryNarrationRepository()
    submitter = SubmitSentenceNarration(
        _Provider(output), repo, InMemoryAudioArchive(), PcmWavNormalizer()
    )
    intent = NarrationAttempt(
        request.project_id,
        request.owner_id,
        request.sentence.sequence,
        request.idempotency_key,
        _request_hash(request),
        NarrationAttemptState.INTENT_RECORDED,
        request.provider_call_id,
    )
    repo.save_attempt(intent)
    assert repo.claim_submission(intent, "winner", datetime.now(UTC) + timedelta(minutes=5))
    with pytest.raises(DomainError) as waiting:
        await submitter.execute_batch((request,))
    assert waiting.value.code == "submission_lease_active"


@pytest.mark.asyncio
async def test_checkpoint_replay_preserves_processing_failure_without_resubmission() -> None:
    request, output = _request(500)

    class FailingNormalizer:
        def normalize(self, _payload: bytes, **_kwargs: object) -> object:
            raise ValueError("normalizer failed")

    repo = InMemoryNarrationRepository()
    archive = InMemoryAudioArchive()
    first_provider = _Provider(output)
    first = SubmitSentenceNarration(
        first_provider,
        repo,
        archive,
        FailingNormalizer(),  # type: ignore[arg-type]
    )

    assert await first.execute_batch((request,)) == (request.sentence.sequence,)
    checkpoint = repo.provider_checkpoint(OWNER, request.idempotency_key)
    assert checkpoint is not None
    assert first_provider.submissions == 1

    restarted_provider = _Provider(
        error=AssertionError("provider must not be called"),  # type: ignore[arg-type]
    )
    restarted = SubmitSentenceNarration(
        restarted_provider,
        repo,
        archive,
        FailingNormalizer(),  # type: ignore[arg-type]
    )

    assert await restarted.execute_batch((request,)) == (request.sentence.sequence,)
    assert restarted_provider.submissions == 0
    replayed_checkpoint = repo.provider_checkpoint(OWNER, request.idempotency_key)
    assert replayed_checkpoint is not None
    assert replayed_checkpoint.asset_id == checkpoint.asset_id


@pytest.mark.asyncio
async def test_mixed_batch_preserves_checkpoint_failure_with_fresh_success() -> None:
    checkpoint_request, checkpoint_output = _request(500)
    checkpoint_raw = _wav(500)
    fresh_sentence = ScriptSentence(
        2,
        "This is another sentence.",
        hashlib.sha256(b"This is another sentence.").hexdigest(),
    )
    fresh_request = replace(
        checkpoint_request,
        sentence=fresh_sentence,
        idempotency_key="fresh-key",
        provider_call_id="fresh-call",
    )
    fresh_name = f"0002-{fresh_sentence.text_sha256[:12]}.wav"
    fresh_output = _multi_zip({fresh_name: _wav(600)})

    class SelectiveFailingNormalizer:
        def normalize(self, payload: bytes, **_kwargs: object) -> object:
            if payload == checkpoint_raw:
                raise ValueError("checkpoint normalizer failed")
            return PcmWavNormalizer().normalize(payload)

    class RecordingProvider(_Provider):
        def __init__(self, output: bytes) -> None:
            super().__init__(output)
            self.batch_sequences: list[tuple[int, ...]] = []

        async def submit_batch(
            self, requests: tuple[SynthesisRequest, ...]
        ) -> SynthesisResult:
            self.batch_sequences.append(tuple(item.sentence.sequence for item in requests))
            return await super().submit_batch(requests)

    repo = InMemoryNarrationRepository()
    archive = InMemoryAudioArchive()
    initial = SubmitSentenceNarration(
        _Provider(checkpoint_output),
        repo,
        archive,
        SelectiveFailingNormalizer(),  # type: ignore[arg-type]
    )
    assert await initial.execute_batch((checkpoint_request,)) == (
        checkpoint_request.sentence.sequence,
    )
    assert repo.provider_checkpoint(OWNER, checkpoint_request.idempotency_key) is not None

    provider = RecordingProvider(fresh_output)
    mixed = SubmitSentenceNarration(
        provider,
        repo,
        archive,
        SelectiveFailingNormalizer(),  # type: ignore[arg-type]
    )

    failures = await mixed.execute_batch((checkpoint_request, fresh_request))
    assert provider.submissions == 1
    assert provider.batch_sequences == [(fresh_request.sentence.sequence,)]
    assert repo.find_by_idempotency_key(OWNER, fresh_request.idempotency_key) is not None
    assert failures == (checkpoint_request.sentence.sequence,)


@pytest.mark.asyncio
async def test_checkpoint_crash_replay_marks_missing_member_retryable() -> None:
    script = segment_pasted_script(
        "This is one sentence. This is another sentence.", SupportedLocale.EN_US
    )
    reference = b"reference-zip"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"crash-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            "crash-call",
            expected_output_names=tuple(
                f"{item.sequence:04d}-{item.text_sha256[:12]}.wav" for item in script.sentences
            ),
        )
        for sentence in script.sentences
    )
    first_name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"

    class CrashAfterCheckpointRepository(InMemoryNarrationRepository):
        attached = 0

        def set_checkpoint_ledger(
            self,
            attempt: NarrationAttempt,
            state: str,
            asset: AudioAsset | None = None,
            receipt: dict[str, object] | None = None,
        ) -> None:
            super().set_checkpoint_ledger(attempt, state, asset, receipt)
            if state == "ATTACHED":
                self.attached += 1
                if self.attached == 2:
                    raise RuntimeError("worker crashed after checkpoint")

    repo = CrashAfterCheckpointRepository()
    archive = InMemoryAudioArchive()
    first = SubmitSentenceNarration(
        _Provider(_multi_zip({first_name: _wav(500)})),
        repo,
        archive,
        PcmWavNormalizer(),
    )
    with pytest.raises(RuntimeError, match="worker crashed"):
        await first.execute_batch(requests)
    restarted = SubmitSentenceNarration(
        _Provider(error=AssertionError("provider must not be called")),  # type: ignore[arg-type]
        repo,
        archive,
        PcmWavNormalizer(),
    )
    assert await restarted.execute_batch(requests) == (2,)
    assert repo.attempt(OWNER, "crash-2").state is NarrationAttemptState.SAFE_TO_RETRY  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_supervised_dispatcher_retries_pending_job_after_start_response_loss() -> None:
    text = "This is one sentence."
    script = segment_pasted_script(text, SupportedLocale.EN_US)
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, script)
    job = NarrationJob("job", OWNER, PROJECT, (1,), "PENDING")
    repo.save_job(job)
    starter = _Starter(fail_once=True)
    service = ProjectNarrationService(
        _ProjectPort(text),  # type: ignore[arg-type]
        _VoicePort(),
        repo,
        SubmitSentenceNarration(_Provider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()),
        starter,
        _Writable(),
    )
    dispatcher = PendingNarrationDispatcher(service, interval_seconds=0.01)
    await dispatcher.start()
    await asyncio.sleep(0.04)
    await dispatcher.stop()
    assert starter.calls >= 2
    assert repo.get_job(OWNER, PROJECT).state == "DISPATCHED"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_supervised_dispatcher_survives_database_failure_with_backoff() -> None:
    class FlakyService:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch_pending_jobs(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database unavailable")

    service = FlakyService()
    dispatcher = PendingNarrationDispatcher(service, interval_seconds=0.005)  # type: ignore[arg-type]
    await dispatcher.start()
    await asyncio.sleep(0.04)
    await dispatcher.stop()
    assert service.calls >= 2
    assert dispatcher.failure_count == 0
    assert dispatcher.last_error is None


@pytest.mark.asyncio
async def test_intent_is_persisted_then_duplicate_click_replays_archive() -> None:
    request, output = _request()
    repo = InMemoryNarrationRepository()
    provider = _Provider(output)
    use_case = SubmitSentenceNarration(provider, repo, InMemoryAudioArchive(), PcmWavNormalizer())
    await use_case.execute(request)
    assert (await use_case.execute(request)).state == "already_archived"
    assert provider.submissions == 1
    assert repo.attempt(OWNER, request.idempotency_key).state is NarrationAttemptState.COMPLETED  # type: ignore[union-attr]
    assert repo.find_by_idempotency_key(OTHER, request.idempotency_key) is None


@pytest.mark.asyncio
async def test_unknown_submission_survives_restart_and_never_resubmits() -> None:
    request, output = _request()
    repo = InMemoryNarrationRepository()
    first_provider = _Provider(error=DomainError("provider_submission_unknown", "unknown"))
    with pytest.raises(DomainError):
        await SubmitSentenceNarration(
            first_provider, repo, InMemoryAudioArchive(), PcmWavNormalizer()
        ).execute(request)
    second_provider = _Provider(output)
    with pytest.raises(DomainError) as blocked:
        await SubmitSentenceNarration(
            second_provider, repo, InMemoryAudioArchive(), PcmWavNormalizer()
        ).execute(request)
    assert blocked.value.code == "manual_reconciliation_required"
    assert second_provider.submissions == 0


@pytest.mark.asyncio
async def test_invalid_provider_zip_becomes_terminal_and_never_resubmits() -> None:
    request, _ = _request()
    repo = InMemoryNarrationRepository()
    first = _Provider(_zip(b"not-a-wav"))
    archive = InMemoryAudioArchive()
    with pytest.raises(DomainError):
        await SubmitSentenceNarration(first, repo, archive, PcmWavNormalizer()).execute(request)
    assert (
        repo.attempt(OWNER, request.idempotency_key).state is NarrationAttemptState.OUTPUT_INVALID
    )  # type: ignore[union-attr]
    _, valid_output = _request()
    second = _Provider(valid_output)
    with pytest.raises(DomainError) as blocked:
        await SubmitSentenceNarration(second, repo, archive, PcmWavNormalizer()).execute(request)
    assert blocked.value.code == "provider_output_invalid"
    assert second.submissions == 0
    assert [event for _, event in repo.events][-1] == "output_validation_failed"


@pytest.mark.asyncio
async def test_activity_cancellation_after_submit_start_is_unknown_and_not_retried() -> None:
    request, _ = _request()
    repo = InMemoryNarrationRepository()

    class BlockingProvider:
        async def submit(self, request: SynthesisRequest) -> SynthesisResult:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    task = asyncio.create_task(
        SubmitSentenceNarration(
            BlockingProvider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()
        ).execute(request)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        repo.attempt(OWNER, request.idempotency_key).state
        is NarrationAttemptState.REMOTE_STATE_UNKNOWN
    )  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_batch_cancellation_marks_every_started_unit_unknown() -> None:
    script = segment_pasted_script(
        "This is one sentence. This is another sentence.", SupportedLocale.EN_US
    )
    reference = b"reference-zip"
    requests = tuple(
        SynthesisRequest(
            PROJECT,
            OWNER,
            VOICE,
            sentence,
            f"cancel-{sentence.sequence}",
            SupportedLocale.EN_US,
            reference,
            hashlib.sha256(reference).hexdigest(),
            "cancel-call",
        )
        for sentence in script.sentences
    )

    class BlockingBatchProvider(_Provider):
        async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
            del requests
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    repo = InMemoryNarrationRepository()
    task = asyncio.create_task(
        SubmitSentenceNarration(
            BlockingBatchProvider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()
        ).execute_batch(requests)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {
        repo.attempt(OWNER, request.idempotency_key).state
        for request in requests  # type: ignore[union-attr]
    } == {NarrationAttemptState.REMOTE_STATE_UNKNOWN}


def test_stale_activity_cannot_overwrite_unknown_with_completed() -> None:
    request, _ = _request()
    repo = InMemoryNarrationRepository()
    request_hash = hashlib.sha256(b"request").hexdigest()
    intent = NarrationAttempt(
        PROJECT,
        OWNER,
        1,
        request.idempotency_key,
        request_hash,
        NarrationAttemptState.INTENT_RECORDED,
        request.provider_call_id,
    )
    repo.save_attempt(intent)
    started = replace(intent, state=NarrationAttemptState.SUBMISSION_STARTED)
    repo.save_attempt(started)
    repo.save_attempt(replace(started, state=NarrationAttemptState.REMOTE_STATE_UNKNOWN))
    with pytest.raises(DomainError) as stale:
        repo.save_attempt(replace(started, state=NarrationAttemptState.COMPLETED))
    assert stale.value.code == "stale_attempt_transition"


def test_job_persists_partial_failed_and_failure_manifest() -> None:
    repo = InMemoryNarrationRepository()
    repo.save_job(NarrationJob("job-1", OWNER, PROJECT, (1, 2, 3), "PENDING"))
    repo.mark_job_dispatched("job-1")
    repo.finish_job("job-1", "PARTIAL_FAILED", (2, 3))
    job = repo.get_job(OWNER, PROJECT)
    assert job is not None
    assert job.state == "PARTIAL_FAILED"
    assert job.failure_sequences == (2, 3)


def test_late_dispatch_ack_cannot_overwrite_completed_job() -> None:
    repo = InMemoryNarrationRepository()
    repo.save_job(NarrationJob("job-cas", OWNER, PROJECT, (1,), "PENDING"))
    repo.finish_job("job-cas", "COMPLETED", ())
    repo.mark_job_dispatched("job-cas")
    assert repo.get_job(OWNER, PROJECT).state == "COMPLETED"  # type: ignore[union-attr]


def test_long_timeline_accumulates_pcm_frames_before_projecting_milliseconds() -> None:
    sentences = tuple(
        ScriptSentence(
            index, f"sentence {index}", hashlib.sha256(f"sentence {index}".encode()).hexdigest()
        )
        for index in range(1, 101)
    )
    script = ScriptVersion(SupportedLocale.EN_US, "test", "a" * 64, sentences)
    payload = _wav_frames(1001, 24_000)
    qc = inspect_wav(payload)
    narrated = tuple(
        NarratedSentence(
            PROJECT,
            OWNER,
            VOICE,
            SupportedLocale.EN_US,
            f"call-{item.sequence}",
            "b" * 64,
            item,
            f"key-{item.sequence}",
            AudioAsset(f"raw-{item.sequence}", "c" * 64, "audio/wav", len(payload)),
            AudioAsset(f"normalized-{item.sequence}", "d" * 64, "audio/wav", len(payload)),
            qc,
            qc,
            {"recipe": "test"},
        )
        for item in sentences
    )
    timeline = build_measured_timeline(script, narrated)
    assert timeline[-1].end_ms == round(100 * 1001 * 1000 / 24_000)
    assert timeline[-1].end_ms != 100 * round(1001 * 1000 / 24_000)


def _wav_frames(frame_count: int, rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<h", 1200) * frame_count)
    return output.getvalue()


def _float32_provider_wav(duration_ms: int = 300) -> bytes:
    rate = 24_000
    frames = duration_ms * rate // 1000
    samples = b"".join(
        struct.pack("<f", 0.1 if index % 2 == 0 else -0.1) for index in range(frames)
    )
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_real_cosyvoice_pcm_f32le_contract_is_qced_without_weakening_normalized_gate() -> None:
    qc = inspect_wav(_float32_provider_wav())
    assert qc.passed
    assert (qc.codec, qc.sample_rate_hz, qc.channels, qc.sample_width_bytes) == (
        "pcm_f32le",
        24_000,
        1,
        4,
    )
    assert not inspect_wav(_float32_provider_wav(1)[:-1]).passed


@pytest.mark.integration
def test_ffmpeg_normalizer_produces_seekable_strict_wav() -> None:
    executable = os.environ.get("TUBEFLOW_TEST_FFMPEG") or shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg executable is not available")
    version = subprocess.run(
        (executable, "-version"), capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    normalized = FfmpegAudioNormalizer(executable, ffmpeg_version=version).normalize(_wav(1000))
    qc = inspect_wav(normalized.payload)
    assert qc.passed and qc.sample_rate_hz == 48_000 and qc.sample_width_bytes == 2
    assert normalized.payload[:4] == b"RIFF"
    ffprobe = Path(executable).with_name(
        "ffprobe.exe" if Path(executable).suffix.lower() == ".exe" else "ffprobe"
    )
    raw_qc = FfprobeAudioInspector(str(ffprobe)).inspect(_float32_provider_wav())
    assert raw_qc.passed and raw_qc.codec == "pcm_f32le"


def test_wav_qc_rejects_truncation_and_near_silence_for_8_and_16_bit() -> None:
    valid = _wav(1000)
    assert inspect_wav(valid).passed
    assert inspect_wav(valid[:-11]).reason in {
        "truncated_audio",
        "wav_size_mismatch",
        "audio_not_decodable",
    }
    assert inspect_wav(_wav(1000, width=1, amplitude=0)).reason == "near_silent_audio"
    assert inspect_wav(_wav(1000, width=2, amplitude=1)).reason == "near_silent_audio"
    assert inspect_wav(valid[:-11]).measured_duration_ms is None


def test_zip_manifest_rejects_unknown_duplicate_traversal_and_oversized_member() -> None:
    with pytest.raises(DomainError):
        extract_sentence_audio(_zip(_wav(100), "unknown.wav"), "0001.wav")
    with pytest.raises(DomainError):
        extract_sentence_audio(_zip(_wav(100), "../0001.wav"), "0001.wav")
    duplicate = io.BytesIO()
    with ZipFile(duplicate, "w") as archive:
        archive.writestr("0001.wav", _wav(100))
        archive.writestr("0001.wav", _wav(100))
    with pytest.raises(DomainError):
        extract_sentence_audio(duplicate.getvalue(), "0001.wav")


@pytest.mark.asyncio
async def test_provider_contract_formats_text_and_rejects_non_zip_content_type() -> None:
    seen = b""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = await request.aread()
        return httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})

    request, _ = _request()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as invalid:
            await CosyVoiceHttpAdapter("http://cosyvoice.local", client=client).submit(request)
    assert invalid.value.code == "provider_output_invalid"
    assert "情感: calm".encode() in seen and "译文: This is a sentence.".encode() in seen
    assert b'name="language"' not in seen and b"X-Task-Id" not in seen


@pytest.mark.asyncio
async def test_provider_sends_bearer_token_and_call_id() -> None:
    request, output = _request()
    seen_headers: httpx.Headers | None = None

    async def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers
        seen_headers = http_request.headers
        return httpx.Response(
            200,
            content=output,
            headers={"content-type": "application/zip"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CosyVoiceHttpAdapter(
            "https://cosyvoice.example.test:8443",
            token=COSYVOICE_TOKEN,
            max_concurrency=4,
            client=client,
        ).submit(request)

    assert result.state == "completed"
    assert seen_headers is not None
    assert seen_headers["authorization"] == f"Bearer {COSYVOICE_TOKEN}"
    assert seen_headers["x-tubeflow-call-id"] == request.provider_call_id


@pytest.mark.parametrize("status_code", [401, 403])
@pytest.mark.asyncio
async def test_provider_auth_rejection_has_stable_error(status_code: int) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    request, _ = _request()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CosyVoiceHttpAdapter(
            "https://cosyvoice.example.test:8443",
            token=COSYVOICE_TOKEN,
            max_concurrency=4,
            client=client,
        )
        with pytest.raises(DomainError) as rejected:
            await adapter.submit(request)

    assert rejected.value.code == "provider_auth_failed"


@pytest.mark.asyncio
async def test_provider_never_exceeds_four_concurrent_submissions() -> None:
    request, output = _request()
    active = 0
    peak = 0
    counter_lock = asyncio.Lock()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        async with counter_lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.02)
        async with counter_lock:
            active -= 1
        return httpx.Response(
            200,
            content=output,
            headers={"content-type": "application/zip"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CosyVoiceHttpAdapter(
            "https://cosyvoice.example.test:8443",
            token=COSYVOICE_TOKEN,
            max_concurrency=4,
            client=client,
        )
        results = await asyncio.gather(*(adapter.submit(request) for _ in range(9)))

    assert peak == 4
    assert all(result.state == "completed" and result.output_zip == output for result in results)


@pytest.mark.asyncio
async def test_provider_batch_uses_repeated_txt_fields_and_partial_zip_reconciles_manifest() -> (
    None
):
    first, _ = _request()
    second_sentence = ScriptSentence(
        2, "This is another sentence.", hashlib.sha256(b"This is another sentence.").hexdigest()
    )
    second = SynthesisRequest(
        PROJECT,
        OWNER,
        VOICE,
        second_sentence,
        first.idempotency_key + "-2",
        first.language,
        first.reference_zip,
        first.reference_sha256,
        first.provider_call_id,
    )
    first_name = f"0001-{first.sentence.text_sha256[:12]}.wav"
    seen = b""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = await request.aread()
        return httpx.Response(
            200, content=_zip(_wav(100), first_name), headers={"content-type": "application/zip"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CosyVoiceHttpAdapter("http://cosyvoice.local", client=client).submit_batch(
            (first, second)
        )
    assert result.task_id == first.provider_call_id
    assert seen.count(b'name="txt_files"') == 2
    assert f"0001-{first.sentence.text_sha256[:12]}.txt".encode() in seen
    assert f"0002-{second_sentence.text_sha256[:12]}.txt".encode() in seen
    matched = extract_batch_audio(
        result.output_zip or b"",
        {first_name, f"0002-{second_sentence.text_sha256[:12]}.wav"},
    )
    assert set(matched) == {first_name}


@pytest.mark.asyncio
async def test_provider_read_timeout_is_unknown_and_no_recovery_endpoint_is_invented() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost response", request=request)

    request, _ = _request()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CosyVoiceHttpAdapter("http://cosyvoice.local", client=client)
        with pytest.raises(DomainError) as unknown:
            await adapter.submit(request)
    assert unknown.value.code == "provider_submission_unknown"


def test_feature_router_owner_comes_only_from_authenticated_context_and_forbids_extra() -> None:
    actor = Account(OWNER, "owner", "hash", AccountRole.USER)

    class Service:
        seen_owner: UUID | None = None

        def preflight(self, owner_id: UUID, project_id: UUID, pasted_text: str) -> object:
            self.seen_owner = owner_id
            assert project_id == PROJECT
            return segment_pasted_script(pasted_text, SupportedLocale.EN_US)

        def status(self, owner_id: UUID, project_id: UUID) -> dict[str, object]:
            self.seen_owner = owner_id
            return {
                "project_id": str(project_id),
                "state": "NOT_READY",
                "sentences": (),
                "timeline": (),
                "groups": (),
                "attempts": (),
            }

    service = Service()
    app = FastAPI()
    app.include_router(create_router(service, lambda: actor))  # type: ignore[arg-type]
    client = TestClient(app)
    request = {"project_id": str(PROJECT), "pasted_text": "This is valid English."}
    response = client.post("/narration/preflight", json=request)
    assert response.status_code == 200, response.text
    assert service.seen_owner == OWNER
    rejected = client.post("/narration/preflight", json={**request, "owner_id": str(OTHER)})
    assert rejected.status_code == 422
    missing_evidence = client.post(
        "/narration/recover",
        json={
            "project_id": str(PROJECT),
            "idempotency_key": "attempt-key",
            "resolution": "CONFIRMED_NOT_ACCEPTED",
        },
    )
    assert missing_evidence.status_code == 422


def test_preflight_response_marks_separators_and_estimated_over_limit() -> None:
    actor = Account(OWNER, "owner", "hash", AccountRole.USER)

    class Service:
        def preflight(self, owner_id: UUID, project_id: UUID, pasted_text: str) -> object:
            return segment_pasted_script(pasted_text, SupportedLocale.EN_US)

    app = FastAPI()
    app.include_router(create_router(Service(), lambda: actor))  # type: ignore[arg-type]
    client = TestClient(app)
    long_sentence = " ".join(["word"] * 50) + "."
    response = client.post(
        "/narration/preflight",
        json={
            "project_id": str(PROJECT),
            "pasted_text": "Short one.\n---\n" + long_sentence,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["estimated_risk_threshold_ms"] == ESTIMATED_RISK_THRESHOLD_MS
    sentences = payload["sentences"]
    assert [item["is_separator"] for item in sentences] == [False, True, False]
    assert sentences[1]["estimated_duration_ms"] is None
    assert sentences[0]["estimated_over_limit"] is False
    assert sentences[2]["estimated_duration_ms"] == 20000
    assert sentences[2]["estimated_over_limit"] is True


def test_recovery_returns_the_updated_public_async_contract() -> None:
    actor = Account(OWNER, "owner", "hash", AccountRole.USER)

    class RecoveryService:
        seen: tuple[UUID, UUID, str, str, str] | None = None

        async def reconcile(
            self,
            owner_id: UUID,
            project_id: UUID,
            idempotency_key: str,
            resolution: str,
            external_evidence: str,
        ) -> None:
            self.seen = (
                owner_id,
                project_id,
                idempotency_key,
                resolution,
                external_evidence,
            )

        def status(self, owner_id: UUID, project_id: UUID) -> dict[str, object]:
            assert owner_id == OWNER
            return {
                "project_id": str(project_id),
                "state": "PENDING",
                "sentences": (),
                "timeline": (),
                "groups": (),
                "attempts": (),
            }

    service = RecoveryService()
    app = FastAPI()
    app.include_router(create_router(service, lambda: actor))  # type: ignore[arg-type]
    response = TestClient(app).post(
        "/narration/recover",
        json={
            "project_id": str(PROJECT),
            "idempotency_key": "attempt-key",
            "resolution": "CONFIRMED_NOT_ACCEPTED",
            "external_evidence": "Provider console confirms no task was accepted.",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["public_status"] == "QUEUED"
    assert payload["allowed_actions"] == ["REFRESH"]
    assert service.seen == (
        OWNER,
        PROJECT,
        "attempt-key",
        "CONFIRMED_NOT_ACCEPTED",
        "Provider console confirms no task was accepted.",
    )


@pytest.mark.asyncio
async def test_status_exposes_stable_authoritative_ids_and_total_duration() -> None:
    request, output = _request(1250)
    script = segment_pasted_script(request.sentence.text, request.language)
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, script)
    submitter = SubmitSentenceNarration(
        _Provider(output), repo, InMemoryAudioArchive(), PcmWavNormalizer()
    )
    await submitter.execute(request)
    submitter.rebuild_authoritative_timeline(request)
    service = ProjectNarrationService(
        _ProjectPort(request.sentence.text),
        _VoicePort(),
        repo,
        submitter,
        _Starter(),
        _Writable(),
    )
    app = FastAPI()
    app.include_router(
        create_router(service, lambda: Account(OWNER, "owner", "hash", AccountRole.USER))
    )

    first = TestClient(app).get(f"/narration/projects/{PROJECT}/status")
    second = TestClient(app).get(f"/narration/projects/{PROJECT}/status")

    assert first.status_code == 200
    assert first.json() == second.json()
    payload = first.json()
    sentence = payload["sentences"][0]
    group = payload["groups"][0]
    assert sentence["sentence_id"] == str(
        authoritative_sentence_id(PROJECT, script.source_sha256, 1)
    )
    assert sentence["sentence"]["text"] == request.sentence.text
    assert sentence["normalized_asset"]["asset_id"] == payload["timeline"][0]["audio_asset_id"]
    assert group["group_id"] == str(authoritative_group_id(PROJECT, script.source_sha256, 1))
    assert group["sentence_sequences"] == [1]
    assert payload["total_duration_ms"] == 1250
    assert payload["public_status"] == "SUCCEEDED"


def test_status_maps_persistent_job_and_unknown_attempt_to_public_states() -> None:
    request, _ = _request()
    script = segment_pasted_script(request.sentence.text, request.language)
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, script)
    repo.save_job(NarrationJob("job", OWNER, PROJECT, (1,), "PENDING"))
    service = ProjectNarrationService(
        _ProjectPort(request.sentence.text),
        _VoicePort(),
        repo,
        SubmitSentenceNarration(
            _Provider(), repo, InMemoryAudioArchive(), PcmWavNormalizer()
        ),
        _Starter(),
        _Writable(),
    )
    app = FastAPI()
    app.include_router(
        create_router(service, lambda: Account(OWNER, "owner", "hash", AccountRole.USER))
    )
    client = TestClient(app)

    queued = client.get(f"/narration/projects/{PROJECT}/status").json()
    assert queued["state"] == "PENDING"
    assert queued["public_status"] == "QUEUED"
    assert queued["allowed_actions"] == ["REFRESH"]

    repo.mark_job_dispatched("job")
    running = client.get(f"/narration/projects/{PROJECT}/status").json()
    assert running["state"] == "DISPATCHED"
    assert running["public_status"] == "RUNNING"

    repo.save_attempt(
        NarrationAttempt(
            PROJECT,
            OWNER,
            1,
            request.idempotency_key,
            _request_hash(request),
            NarrationAttemptState.REMOTE_STATE_UNKNOWN,
            request.provider_call_id,
        )
    )
    recovery = client.get(f"/narration/projects/{PROJECT}/status").json()
    assert recovery["state"] == "REMOTE_STATE_UNKNOWN"
    assert recovery["public_status"] == "RECOVERY_REQUIRED"
    assert recovery["allowed_actions"] == ["RECONCILE", "REFRESH"]


def test_segmentation_v3_keeps_closing_quotes_with_preceding_sentence() -> None:
    script = segment_pasted_script(
        'It is not "what year did people start using caves."'
        ' Not "which tribe did it first."',
        SupportedLocale.EN_US,
    )
    assert [item.text for item in script.sentences] == [
        'It is not "what year did people start using caves."',
        'Not "which tribe did it first."',
    ]


def test_segmentation_v3_marks_punctuation_only_lines_as_separators() -> None:
    script = segment_pasted_script(
        "Real sentence one.\n\n---\n\nReal sentence two.", SupportedLocale.EN_US
    )
    assert [item.text for item in script.sentences] == [
        "Real sentence one.",
        "---",
        "Real sentence two.",
    ]
    assert [is_nonvisual_script_separator(item.text) for item in script.sentences] == [
        False,
        True,
        False,
    ]


def test_segmentation_v3_rejects_script_without_narratable_sentences() -> None:
    with pytest.raises(DomainError, match="no sentences"):
        segment_pasted_script("---\n***", SupportedLocale.EN_US)


def test_nonvisual_separator_treats_zero_width_only_text_as_separator() -> None:
    """2026-10-31 生产事故：单独一行 U+200B 曾是"隐形句"，提交 TTS 后永久失败。"""
    assert is_nonvisual_script_separator("\u200b") is True
    assert is_nonvisual_script_separator("  \u200b\ufeff ") is True
    assert is_nonvisual_script_separator("---") is True
    # 空串保持既有语义：空串不是分隔句（由上层判为空稿）。
    assert is_nonvisual_script_separator("") is False
    assert is_nonvisual_script_separator("배가 아픕니다.") is False



def test_segmentation_v3_splits_overlong_sentence_at_em_dash() -> None:
    left = " ".join(["alpha"] * 20)
    right = " ".join(["beta"] * 20)
    text = f"{left} — {right}."
    script = segment_pasted_script(text, SupportedLocale.EN_US)
    assert [item.text for item in script.sentences] == [f"{left} —", f"{right}."]
    assert "".join("".join(item.text.split()) for item in script.sentences) == "".join(
        text.split()
    )


def test_segmentation_v3_splits_overlong_sentence_balanced_at_commas() -> None:
    part = " ".join(["word"] * 12)
    text = f"{part}, {part}, {part}, {part}."
    script = segment_pasted_script(text, SupportedLocale.EN_US)
    assert len(script.sentences) == 2
    assert all(len(item.text.split()) <= 35 for item in script.sentences)
    assert "".join("".join(item.text.split()) for item in script.sentences) == "".join(
        text.split()
    )


def test_segmentation_v3_keeps_sentence_within_budget_untouched() -> None:
    text = " ".join(["plain"] * 30) + "."
    script = segment_pasted_script(text, SupportedLocale.EN_US)
    assert len(script.sentences) == 1


def test_segmentation_v3_never_splits_separator_even_when_overlong() -> None:
    dashes = "-" * 200
    script = segment_pasted_script(
        f"Intro sentence.\n{dashes}\nOutro sentence.", SupportedLocale.EN_US
    )
    assert [item.text for item in script.sentences] == [
        "Intro sentence.",
        dashes,
        "Outro sentence.",
    ]


def test_segmentation_v3_split_is_deterministic() -> None:
    part = " ".join(["token"] * 15)
    text = f"{part}, {part}, {part}."
    first = segment_pasted_script(text, SupportedLocale.EN_US)
    second = segment_pasted_script(text, SupportedLocale.EN_US)
    assert first.sentences == second.sentences
    assert first.source_sha256 == second.source_sha256


def test_measured_timeline_skips_separator_sentences_without_narration() -> None:
    script = segment_pasted_script(
        "First sentence.\n---\nSecond sentence.", SupportedLocale.EN_US
    )
    payload = _wav_frames(1200, 24_000)
    qc = inspect_wav(payload)
    narrated = tuple(
        NarratedSentence(
            PROJECT,
            OWNER,
            VOICE,
            SupportedLocale.EN_US,
            f"call-{item.sequence}",
            "b" * 64,
            item,
            f"key-{item.sequence}",
            AudioAsset(f"raw-{item.sequence}", "c" * 64, "audio/wav", len(payload)),
            AudioAsset(f"normalized-{item.sequence}", "d" * 64, "audio/wav", len(payload)),
            qc,
            qc,
            {"recipe": "test"},
        )
        for item in (script.sentences[0], script.sentences[2])
    )
    timeline = build_measured_timeline(script, narrated)
    assert [entry.sentence_sequence for entry in timeline] == [1, 3]
    assert timeline[1].start_ms == timeline[0].end_ms


def test_estimate_narration_duration_ms_flags_only_long_texts() -> None:
    long_text = " ".join(["word"] * 50)
    assert estimate_narration_duration_ms(long_text, SupportedLocale.EN_US) == 20_000
    short_text = " ".join(["word"] * 10)
    estimate = estimate_narration_duration_ms(short_text, SupportedLocale.EN_US)
    assert 0 < estimate < ESTIMATED_RISK_THRESHOLD_MS


@pytest.mark.asyncio
async def test_reconcile_retry_invalid_output_moves_to_safe_to_retry_and_enqueues_job() -> None:
    text = "This is one sentence. This is another sentence."
    script = segment_pasted_script(text, SupportedLocale.EN_US)
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, script)
    service = ProjectNarrationService(
        _ProjectPort(text),  # type: ignore[arg-type]
        _VoicePort(),
        repo,
        SubmitSentenceNarration(
            _SequenceProvider([]), repo, InMemoryAudioArchive(), PcmWavNormalizer()
        ),
        _Starter(),
        _Writable(),
    )
    repo.save_attempt(
        NarrationAttempt(
            PROJECT,
            OWNER,
            1,
            "key-1",
            "a" * 64,
            NarrationAttemptState.OUTPUT_INVALID,
            "call-1",
        )
    )
    await service.reconcile(
        OWNER, PROJECT, "key-1", "RETRY_INVALID_OUTPUT", "user confirmed retry"
    )
    saved = repo.attempt(OWNER, "key-1")
    assert saved is not None and saved.state is NarrationAttemptState.SAFE_TO_RETRY
    assert saved.safe_error_code.startswith("manual:RETRY_INVALID_OUTPUT:")
    assert any(job.project_id == PROJECT for job in repo.pending_jobs())


@pytest.mark.asyncio
async def test_reconcile_retry_invalid_output_rejects_other_states() -> None:
    text = "This is one sentence."
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, segment_pasted_script(text, SupportedLocale.EN_US))
    service = ProjectNarrationService(
        _ProjectPort(text),  # type: ignore[arg-type]
        _VoicePort(),
        repo,
        SubmitSentenceNarration(
            _SequenceProvider([]), repo, InMemoryAudioArchive(), PcmWavNormalizer()
        ),
        _Starter(),
        _Writable(),
    )
    repo.save_attempt(
        NarrationAttempt(
            PROJECT,
            OWNER,
            1,
            "key-1",
            "a" * 64,
            NarrationAttemptState.SAFE_TO_RETRY,
            "call-1",
        )
    )
    with pytest.raises(DomainError) as captured:
        await service.reconcile(OWNER, PROJECT, "key-1", "RETRY_INVALID_OUTPUT", "wrong state")
    assert captured.value.code == "invalid_reconciliation"


class _RecoveryProvider(_Provider):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[tuple[int, ...]] = []

    async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
        self.submissions += 1
        self.batches.append(tuple(item.sentence.sequence for item in requests))
        output = _multi_zip({
            f"{item.sentence.sequence:04d}-{item.sentence.text_sha256[:12]}.wav": (
                b"invalid audio"
                if self.submissions == 1 and item.sentence.sequence in {2, 3}
                else _wav(500)
            )
            for item in requests
        })
        return SynthesisResult(requests[0].provider_call_id, "completed", output)


async def _failed_recovery_project() -> tuple[
    ProjectNarrationService, InMemoryNarrationRepository, _RecoveryProvider, TestClient
]:
    text = "This is one sentence. This is another sentence. This is the final sentence."
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, segment_pasted_script(text, SupportedLocale.EN_US))
    repo.save_job(NarrationJob("original", OWNER, PROJECT, (1, 2, 3), "DISPATCHED"))
    provider = _RecoveryProvider()
    service = ProjectNarrationService(
        _ProjectPort(text),  # type: ignore[arg-type]
        _VoicePort(), repo,
        SubmitSentenceNarration(provider, repo, InMemoryAudioArchive(), PcmWavNormalizer()),
        _Starter(), _Writable(),
    )
    failures = await service.synthesize_batch(OWNER, PROJECT, (1, 2, 3))
    assert failures == (2, 3)
    service.finish_job(OWNER, PROJECT, "original", failures)
    app = FastAPI()
    actor = Account(OWNER, "owner", "hash", AccountRole.USER)
    app.include_router(create_router(service, lambda: actor))
    return service, repo, provider, TestClient(app)


def _recover_sentence(client: TestClient, key: str) -> httpx.Response:
    return client.post("/narration/recover", json={
        "project_id": str(PROJECT),
        "idempotency_key": key,
        "resolution": "RETRY_INVALID_OUTPUT",
        "external_evidence": "User confirms regenerating the narration rejected by audio QC.",
    })


@pytest.mark.asyncio
async def test_invalid_output_api_exposes_sentences_and_recovers_only_authorized_units() -> None:
    service, repo, provider, client = await _failed_recovery_project()
    rows = client.get(f"/narration/projects/{PROJECT}/status").json()["sentence_statuses"]
    assert [row["sequence"] for row in rows] == [1, 2, 3]
    assert rows[0]["public_status"] == "SUCCEEDED"
    assert rows[0]["allowed_actions"] == ["VIEW_AUDIO"]
    for row in rows[1:]:
        assert row["contract_version"] == "narration-sentence.v1"
        assert row["public_status"] == "RECOVERY_REQUIRED"
        assert row["safe_error_code"] and row["message"]
        assert row["allowed_actions"] == ["RETRY_INVALID_OUTPUT", "REFRESH"]
        assert "state" not in row
    original_audio = repo.list_narrated(OWNER, PROJECT)
    recovered = _recover_sentence(client, rows[1]["idempotency_key"])
    assert recovered.status_code == 202
    assert provider.batches == [(1, 2, 3)]
    queued = recovered.json()["sentence_statuses"][1]
    assert queued["public_status"] == "QUEUED"
    assert queued["safe_error_code"] is None
    assert queued["allowed_actions"] == ["REFRESH"]
    job = repo.get_job(OWNER, PROJECT)
    assert job is not None and job.sentence_sequences == (2,) and job.state == "PENDING"
    repo.mark_job_dispatched(job.job_id)
    failures = await service.synthesize_batch(OWNER, PROJECT, job.sentence_sequences)
    service.finish_job(OWNER, PROJECT, job.job_id, failures)
    final_rows = client.get(f"/narration/projects/{PROJECT}/status").json()["sentence_statuses"]
    assert final_rows[1]["public_status"] == "SUCCEEDED"
    assert final_rows[2]["allowed_actions"] == ["RETRY_INVALID_OUTPUT", "REFRESH"]
    assert provider.batches == [(1, 2, 3), (2,)]
    assert original_audio[0] in repo.list_narrated(OWNER, PROJECT)
    assert _recover_sentence(client, rows[1]["idempotency_key"]).status_code == 202
    assert repo.pending_jobs() == ()
    assert provider.batches == [(1, 2, 3), (2,)]


@pytest.mark.asyncio
async def test_multiple_recoveries_preserve_active_job_and_replay_is_idempotent() -> None:
    service, repo, provider, client = await _failed_recovery_project()
    keys = {
        item.sentence_sequence: item.idempotency_key
        for item in repo.list_attempts(OWNER, PROJECT)
    }
    assert _recover_sentence(client, keys[2]).status_code == 202
    conflict = client.post("/narration/recover", json={
        "project_id": str(PROJECT),
        "idempotency_key": keys[2],
        "resolution": "RETRY_INVALID_OUTPUT",
        "external_evidence": "Different evidence for the same recovery.",
    })
    assert conflict.status_code == 409
    first = repo.get_job(OWNER, PROJECT)
    assert first is not None
    repo.mark_job_dispatched(first.job_id)
    assert _recover_sentence(client, keys[3]).status_code == 202
    assert _recover_sentence(client, keys[2]).status_code == 202
    assert repo.get_job(OWNER, PROJECT).job_id == first.job_id  # type: ignore[union-attr]
    assert repo.pending_jobs() == ()
    failures = await service.synthesize_batch(OWNER, PROJECT, first.sentence_sequences)
    service.finish_job(OWNER, PROJECT, first.job_id, failures)
    second = repo.get_job(OWNER, PROJECT)
    assert second is not None and second.job_id != first.job_id
    assert second.sentence_sequences == (3,) and second.state == "PENDING"
    repo.finish_job(first.job_id, "PARTIAL_FAILED", (3,))
    repo.mark_job_dispatched(first.job_id)
    assert repo.get_job(OWNER, PROJECT) == second
    assert repo.pending_jobs() == (second,)
    repo.mark_job_dispatched(second.job_id)
    failures = await service.synthesize_batch(OWNER, PROJECT, second.sentence_sequences)
    service.finish_job(OWNER, PROJECT, second.job_id, failures)
    payload = client.get(f"/narration/projects/{PROJECT}/status").json()
    assert payload["public_status"] == "SUCCEEDED"
    assert all(row["public_status"] == "SUCCEEDED" for row in payload["sentence_statuses"])
    assert provider.batches == [(1, 2, 3), (2,), (3,)]
    assert _recover_sentence(client, keys[2]).status_code == 202
    assert _recover_sentence(client, keys[3]).status_code == 202
    assert repo.pending_jobs() == ()


@pytest.mark.asyncio
async def test_invalid_output_api_rejects_completed_unknown_and_unowned_attempts() -> None:
    _service, repo, provider, client = await _failed_recovery_project()
    completed = next(
        item for item in repo.list_attempts(OWNER, PROJECT) if item.sentence_sequence == 1
    )
    response = _recover_sentence(client, completed.idempotency_key)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "reconciliation_not_allowed"
    repo.save_attempt(NarrationAttempt(
        PROJECT, OWNER, 4, "unknown-attempt", "a" * 64,
        NarrationAttemptState.REMOTE_STATE_UNKNOWN, "unknown-call",
    ))
    response = _recover_sentence(client, "unknown-attempt")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_reconciliation"
    assert _recover_sentence(client, "not-owned").status_code == 404
    assert provider.submissions == 1


@pytest.mark.parametrize(
    ("attempt_state", "job_state", "expected_status", "expected_actions"),
    [
        (NarrationAttemptState.REMOTE_STATE_UNKNOWN, "FAILED",
         "RECOVERY_REQUIRED", ["RECONCILE", "REFRESH"]),
        (NarrationAttemptState.ARCHIVE_FAILED, "FAILED",
         "RECOVERY_REQUIRED", ["MANUAL_REVIEW", "REFRESH"]),
        (NarrationAttemptState.PROVIDER_REJECTED, "FAILED",
         "FAILED", ["MANUAL_REVIEW", "REFRESH"]),
        (NarrationAttemptState.SAFE_TO_RETRY, "FAILED",
         "RECOVERY_REQUIRED", ["MANUAL_REVIEW", "REFRESH"]),
        (NarrationAttemptState.SAFE_TO_RETRY, "PENDING", "QUEUED", ["REFRESH"]),
    ],
)
def test_sentence_public_contract_only_offers_safe_actions(
    attempt_state: NarrationAttemptState,
    job_state: str,
    expected_status: str,
    expected_actions: list[str],
) -> None:
    text = "This is a sentence."
    repo = InMemoryNarrationRepository()
    repo.save_script(OWNER, PROJECT, segment_pasted_script(text, SupportedLocale.EN_US))
    repo.save_job(NarrationJob("status-job", OWNER, PROJECT, (1,), job_state))
    repo.save_attempt(NarrationAttempt(
        PROJECT, OWNER, 1, "status-key", "a" * 64, attempt_state, "status-call",
        safe_error_code="processing_failed",
    ))
    provider = _RecoveryProvider()
    service = ProjectNarrationService(
        _ProjectPort(text),  # type: ignore[arg-type]
        _VoicePort(), repo,
        SubmitSentenceNarration(provider, repo, InMemoryAudioArchive(), PcmWavNormalizer()),
        _Starter(), _Writable(),
    )
    app = FastAPI()
    actor = Account(OWNER, "owner", "hash", AccountRole.USER)
    app.include_router(create_router(service, lambda: actor))
    rows = TestClient(app).get(f"/narration/projects/{PROJECT}/status").json()["sentence_statuses"]
    assert len(rows) == 1
    assert rows[0]["public_status"] == expected_status
    assert rows[0]["allowed_actions"] == expected_actions
    assert "RETRY_INVALID_OUTPUT" not in rows[0]["allowed_actions"]
    assert provider.submissions == 0

@pytest.mark.asyncio
async def test_batch_intent_records_key_derived_budget_round() -> None:
    # 回归（2026-09-17 真实验收）：批路径构造的 attempt 必须记录键内预算轮次，
    # 曾落成默认 1 导致恢复重建被误判冲突并热循环。
    script = segment_pasted_script("This is one sentence.", SupportedLocale.EN_US)
    reference = b"reference-zip"
    repo = InMemoryNarrationRepository()
    archive = InMemoryAudioArchive()
    name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"
    provider = _Provider(_multi_zip({name: _wav(500)}))
    submitter = SubmitSentenceNarration(provider, repo, archive, PcmWavNormalizer())
    request = SynthesisRequest(
        PROJECT,
        OWNER,
        VOICE,
        script.sentences[0],
        "tts_abc:r3",
        SupportedLocale.EN_US,
        reference,
        hashlib.sha256(reference).hexdigest(),
        "batch-call-r3",
    )
    assert await submitter.execute_batch((request,)) == ()
    attempt = repo.attempt(OWNER, "tts_abc:r3")
    assert attempt is not None
    assert attempt.budget_round == 3


@pytest.mark.asyncio
async def test_batch_continuation_rebuild_with_drifted_bookkeeping_is_not_a_conflict() -> None:
    # 回归：同一幂等键的续投重建在簿记字段（provider_call_id/清单/检查点资产）漂移时
    # 不得报 idempotency_conflict——键由（项目+句子文本+音色）派生，句序一致即同一意图。
    script = segment_pasted_script("This is one sentence.", SupportedLocale.EN_US)
    reference = b"reference-zip"
    repo = InMemoryNarrationRepository()
    archive = InMemoryAudioArchive()
    repo.save_attempt(
        NarrationAttempt(
            PROJECT,
            OWNER,
            1,
            "tts_drift:r2",
            "stale-bookkeeping-hash",
            NarrationAttemptState.INTENT_RECORDED,
            "batch-call-a",
            original_complete_manifest=("0001-old.wav",),
            original_manifest_sha256="old",
            retry_subset_manifest=("0001-old.wav",),
            retry_subset_manifest_sha256="old-sub",
            budget_round=2,
        )
    )
    name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"
    provider = _Provider(_multi_zip({name: _wav(500)}))
    submitter = SubmitSentenceNarration(provider, repo, archive, PcmWavNormalizer())
    rebuilt = SynthesisRequest(
        PROJECT,
        OWNER,
        VOICE,
        script.sentences[0],
        "tts_drift:r2",
        SupportedLocale.EN_US,
        reference,
        hashlib.sha256(reference).hexdigest(),
        "batch-call-b",
    )
    assert await submitter.execute_batch((rebuilt,)) == ()
    assert provider.submissions == 1
    attempt = repo.attempt(OWNER, "tts_drift:r2")
    assert attempt is not None and attempt.state is NarrationAttemptState.COMPLETED


@pytest.mark.asyncio
async def test_batch_continuation_corrects_legacy_mislabeled_budget_round() -> None:
    # 回归：历史缺陷记录（键含 :r2 但字段为 1）续投时按键权威轮次纠正。
    script = segment_pasted_script("This is one sentence.", SupportedLocale.EN_US)
    reference = b"reference-zip"
    repo = InMemoryNarrationRepository()
    archive = InMemoryAudioArchive()
    repo.save_attempt(
        NarrationAttempt(
            PROJECT,
            OWNER,
            1,
            "tts_legacy:r2",
            "stale-hash",
            NarrationAttemptState.INTENT_RECORDED,
            "batch-call-a",
            budget_round=1,
        )
    )
    name = f"0001-{script.sentences[0].text_sha256[:12]}.wav"
    provider = _Provider(_multi_zip({name: _wav(500)}))
    submitter = SubmitSentenceNarration(provider, repo, archive, PcmWavNormalizer())
    rebuilt = SynthesisRequest(
        PROJECT,
        OWNER,
        VOICE,
        script.sentences[0],
        "tts_legacy:r2",
        SupportedLocale.EN_US,
        reference,
        hashlib.sha256(reference).hexdigest(),
        "batch-call-b",
    )
    assert await submitter.execute_batch((rebuilt,)) == ()
    attempt = repo.attempt(OWNER, "tts_legacy:r2")
    assert attempt is not None
    assert attempt.budget_round == 2
    assert attempt.state is NarrationAttemptState.COMPLETED


@pytest.mark.asyncio
async def test_batch_genuine_intent_conflict_stays_rejected() -> None:
    # 不变式防御：同一幂等键承载不同句序 = 真正的意图冲突，且不可重试。
    script = segment_pasted_script("This is one sentence.", SupportedLocale.EN_US)
    reference = b"reference-zip"
    repo = InMemoryNarrationRepository()
    archive = InMemoryAudioArchive()
    repo.save_attempt(
        NarrationAttempt(
            PROJECT,
            OWNER,
            99,
            "tts_conflict",
            "other-hash",
            NarrationAttemptState.INTENT_RECORDED,
            "batch-call-a",
        )
    )
    provider = _Provider(_multi_zip({}))
    submitter = SubmitSentenceNarration(provider, repo, archive, PcmWavNormalizer())
    request = SynthesisRequest(
        PROJECT,
        OWNER,
        VOICE,
        script.sentences[0],
        "tts_conflict",
        SupportedLocale.EN_US,
        reference,
        hashlib.sha256(reference).hexdigest(),
        "batch-call-b",
    )
    with pytest.raises(NarrationIntentConflictError):
        await submitter.execute_batch((request,))
    assert provider.submissions == 0

def test_frozen_script_sha256_matches_segmentation_basis_under_normalization() -> None:
    """§22：快照哈希基线 = NFC 归一化 + 去首尾空白，与 source_sha256 恒等。"""
    for text in (
        "This is a sentence.",
        "This is a sentence.\n",
        "  This is a sentence.",
        "This is a sentence.\r\n\r\n",
        "First. Second! Third?",
    ):
        assert (
            frozen_script_sha256(text)
            == segment_pasted_script(text, SupportedLocale.EN_US).source_sha256
        )
    # NFC 分解态与组合态归一为同一哈希（é 的两种 Unicode 表示）
    assert frozen_script_sha256("Café.") == frozen_script_sha256("Café.")
    # 首尾空白不漂移；正文差异必漂移
    assert frozen_script_sha256("This is a sentence.") == frozen_script_sha256(
        "This is a sentence.\n"
    )
    assert frozen_script_sha256("This is a sentence.") != frozen_script_sha256(
        "This is a sentence!"
    )
    # 空文本不抛异常：哈希为确定值，门禁比对自然失败（fail-closed）
    assert frozen_script_sha256("") == hashlib.sha256(b"").hexdigest()


@pytest.mark.asyncio
async def test_elevenlabs_loudness_outside_gate_is_recorded_and_accepted() -> None:
    """§37（2026-10-28）：EL 线路响度未达门不再判失败，只把 loudness_gate 记为 outside。"""
    request, output = _request()
    request = replace(request, provider="elevenlabs")

    class BestEffortNormalizer:
        def normalize(
            self, payload: bytes, *, strict_loudness: bool = True
        ) -> NormalizedAudio:
            assert strict_loudness is False
            base = PcmWavNormalizer().normalize(payload)
            manifest = dict(base.manifest)
            manifest["loudness_policy"] = "best_effort"
            manifest["loudness_gate"] = "outside"
            manifest["verified_loudness"] = {
                "integrated_lufs": -25.4,
                "true_peak_dbtp": -3.1,
            }
            return NormalizedAudio(base.payload, manifest)

    repo = InMemoryNarrationRepository()
    submitter = SubmitSentenceNarration(
        _Provider(output),
        repo,
        InMemoryAudioArchive(),
        BestEffortNormalizer(),
        providers={"elevenlabs": _Provider(output)},
    )

    assert await submitter.execute_batch((request,)) == ()
    attempts = repo.list_attempts(OWNER, PROJECT)
    assert attempts[-1].state is NarrationAttemptState.COMPLETED
    narrated = repo.list_narrated(OWNER, PROJECT)
    assert narrated[-1].normalization_manifest["loudness_gate"] == "outside"


@pytest.mark.asyncio
async def test_cosyvoice_loudness_unreachable_still_invalidates_attempt() -> None:
    """CosyVoice 线路零变化：同一失败码仍把尝试标记为 OUTPUT_INVALID。"""
    request, output = _request()

    class UnreachableNormalizer:
        def normalize(
            self, payload: bytes, *, strict_loudness: bool = True
        ) -> NormalizedAudio:
            del payload
            assert strict_loudness is True
            raise AudioProcessingError(
                "audio_loudness_unreachable", "FFmpeg normalized loudness is outside tolerance"
            )

    repo = InMemoryNarrationRepository()
    submitter = SubmitSentenceNarration(
        _Provider(output), repo, InMemoryAudioArchive(), UnreachableNormalizer()
    )

    assert await submitter.execute_batch((request,)) == (request.sentence.sequence,)
    attempts = repo.list_attempts(OWNER, PROJECT)
    assert attempts[-1].state is NarrationAttemptState.OUTPUT_INVALID
    assert attempts[-1].safe_error_code == "audio_loudness_unreachable"


def test_segmentation_v3_keeps_zero_width_line_but_marks_it_as_separator() -> None:
    """2026-10-31：切句不剥离零宽字符（保持既有冻结脚本哈希稳定），
    由分隔符判定把「仅 Cf 字符」的句子排除出配音与时间轴。"""
    script = segment_pasted_script(
        "첫 문장입니다.\n\u200b\n둘째 문장입니다.", SupportedLocale.KO_KR
    )
    assert [item.text for item in script.sentences] == [
        "첫 문장입니다.",
        "\u200b",
        "둘째 문장입니다.",
    ]
    assert is_nonvisual_script_separator(script.sentences[1].text) is True