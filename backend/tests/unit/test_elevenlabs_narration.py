"""§23.4/§23.7/§23.8 EL 配音 Provider 测试（假 HTTP 客户端，不触真实网络）。

覆盖：产物同构（单条目 ZIP + 24kHz/mono/f32le raw WAV 文件名）、逐句串行、
按账号解析/补克隆、402 换 key 原地续跑、全池无 key → provider_quota_exhausted、
字符上限兜底、duration guard、stitching 链（仅取 el: 前缀且最多 3 条）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import struct
import wave
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from tubeflow_test.adapters.elevenlabs import ElevenLabsHttpClient, ElvSynthesisResult
from tubeflow_test.application.elevenlabs_narration import ElevenLabsNarrationProvider
from tubeflow_test.application.narration_ports import (
    EL_REQUEST_ID_PREFIX,
    SynthesisRequest,
    previous_request_ids,
)
from tubeflow_test.domain.errors import DomainError, ProviderHttpStatusError
from tubeflow_test.domain.narration import (
    NarrationAttempt,
    NarrationAttemptState,
    ScriptSentence,
)
from tubeflow_test.elevenlabs.domain import QuotaSnapshot
from tubeflow_test.elevenlabs.ports import PoolKeyEntry
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService
from tubeflow_test.identity_project.domain import ClonedVoice, ClonedVoiceAccount

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
OWNER, PROJECT, VOICE = uuid4(), uuid4(), uuid4()


def _raw_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(4)
        target.setframerate(24_000)
        target.writeframes(struct.pack("<f", 0.1) * 2_400)
    return buffer.getvalue()


class _Transcoder:
    def to_raw_wav(self, payload: bytes) -> bytes:
        assert payload
        return _raw_wav()


class _EchoTranscoder:
    """把输入原样带前缀返回，便于断言「ZIP 内每句内容互不相同、来自各自合成结果」。"""

    def to_raw_wav(self, payload: bytes) -> bytes:
        assert payload
        return b"raw:" + payload


def _zip_name(request: SynthesisRequest) -> str:
    return f"{request.sentence.sequence:04d}-{request.sentence.text_sha256[:12]}.wav"


class _Probe:
    def __init__(self, frame_count: int = 2_400, rate: int = 24_000) -> None:
        self._frame_count, self._rate = frame_count, rate

    def inspect(self, payload: bytes) -> Any:
        del payload

        class _Qc:
            frame_count = self._frame_count
            sample_rate_hz = self._rate

        return _Qc()


class _Storage:
    def __init__(self, payload: bytes = b"source-audio") -> None:
        self.payload = payload

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        return self.payload


class _Cloner:
    def __init__(self, voice_id: str = "el-cloned") -> None:
        self.voice_id = voice_id
        self.calls: list[tuple[str, str]] = []

    async def clone_voice(
        self, api_key: str, name: str, *, audio: bytes, filename: str, description: str = ""
    ) -> str:
        assert audio and filename
        self.calls.append((api_key, name))
        return self.voice_id

    async def list_account_voices(self, api_key: str) -> tuple[str, ...]:
        return ()

    async def delete_voice(self, api_key: str, voice_id: str) -> None:
        return None


class _Projects:
    def __init__(self) -> None:
        self.voices: dict[UUID, ClonedVoice] = {}
        self.accounts: dict[tuple[UUID, UUID], ClonedVoiceAccount] = {}

    def add_voice(self, voice: ClonedVoice) -> None:
        self.voices[voice.id] = voice

    def cloned_voice_by_id(self, cloned_voice_id: UUID) -> ClonedVoice | None:
        return self.voices.get(cloned_voice_id)

    def cloned_voice_account(
        self, cloned_voice_id: UUID, pool_key_id: UUID
    ) -> ClonedVoiceAccount | None:
        return self.accounts.get((cloned_voice_id, pool_key_id))

    def save_cloned_voice_account(self, account: ClonedVoiceAccount) -> None:
        self.accounts[(account.cloned_voice_id, account.pool_key_id)] = account


class _Client(ElevenLabsHttpClient):
    """只替换网络调用；保留真实适配器的参数与语义（含 request-id 传递）。"""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def synthesize(
        self,
        api_key: str,
        voice_id: str,
        text: str,
        *,
        model_id: str,
        previous_request_ids: tuple[str, ...] = (),
        voice_settings: dict[str, object] | None = None,
    ) -> ElvSynthesisResult:
        self.calls.append(
            {
                "api_key": api_key,
                "voice_id": voice_id,
                "text": text,
                "model_id": model_id,
                "previous_request_ids": previous_request_ids,
                "voice_settings": voice_settings,
            }
        )
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, ElvSynthesisResult)
        return outcome


class _PoolBody:
    def __init__(self) -> None:
        self.keys: dict[UUID, PoolKeyEntry] = {}
        self.quota: QuotaSnapshot | None = None

    def list_keys(self) -> tuple[PoolKeyEntry, ...]:
        return tuple(self.keys.values())

    def get_key(self, key_id: UUID) -> PoolKeyEntry | None:
        return self.keys.get(key_id)

    def find_by_api_key(self, api_key: str) -> PoolKeyEntry | None:
        return None

    def add_key(self, *, api_key: str, label: str, now: datetime) -> PoolKeyEntry:
        raise NotImplementedError

    def set_status(
        self, key_id: UUID, status: str, *, last_error: str | None, now: datetime
    ) -> PoolKeyEntry | None:
        entry = self.keys.get(key_id)
        if entry is None:
            return None
        updated = PoolKeyEntry(
            entry.id,
            entry.api_key,
            entry.label,
            status,  # type: ignore[arg-type]
            entry.added_at,
            now,
            last_error,
            entry.quota_tier,
            entry.character_count,
            entry.character_limit,
            entry.voice_slots_used,
            entry.voice_limit,
            entry.quota_refreshed_at,
        )
        self.keys[key_id] = updated
        return updated

    def delete_key(self, key_id: UUID) -> bool:
        return False

    def save_quota(self, key_id: UUID, quota: QuotaSnapshot, refreshed_at: datetime) -> None:
        self.quota = quota

    def record_event(self, key_id: UUID, event: str, detail: str, now: datetime) -> None:
        return None

    def list_events(
        self, *, key_id: UUID | None = None, limit: int = 50
    ) -> tuple[object, ...]:
        return ()

    def trim_events(self, key_id: UUID, keep: int) -> None:
        return None


class _QuotaPort:
    async def subscription(self, api_key: str) -> QuotaSnapshot:
        return QuotaSnapshot("creator", 1, 130_000, 1, 30, "active", True, None)


class _Repository:
    def __init__(self) -> None:
        self.attempts: dict[tuple[UUID, str], NarrationAttempt] = {}
        self.narrated: list[object] = []

    def list_attempts(self, owner_id: UUID, project_id: UUID) -> tuple[NarrationAttempt, ...]:
        return tuple(
            sorted(
                (item for (owner, _key), item in self.attempts.items() if owner == owner_id),
                key=lambda item: item.sentence_sequence,
            )
        )

    def find_by_idempotency_key(self, owner_id: UUID, key: str) -> object | None:
        return None

    def attempt(self, owner_id: UUID, key: str) -> NarrationAttempt | None:
        return self.attempts.get((owner_id, key))

    def provider_checkpoint(self, owner_id: UUID, key: str) -> object | None:
        return None

    def save_attempt(self, attempt: NarrationAttempt) -> None:
        self.attempts[(attempt.owner_id, attempt.idempotency_key)] = attempt


def _sentence(sequence: int = 1, text: str = "Hello there.") -> ScriptSentence:
    import hashlib

    return ScriptSentence(
        sequence=sequence,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _request(
    *, text: str = "Hello there.", cloned_voice_id: UUID | None = VOICE
) -> SynthesisRequest:
    from tubeflow_test.domain.common import SupportedLocale

    sentence = _sentence(text=text)
    return SynthesisRequest(
        project_id=PROJECT,
        owner_id=OWNER,
        voice_version_id=None,
        sentence=sentence,
        idempotency_key="tts_key",
        language=SupportedLocale.EN_US,
        reference_zip=b"",
        reference_sha256="a" * 64,
        provider_call_id="tts_key",
        provider="elevenlabs",
        cloned_voice_id=cloned_voice_id,
    )


def _key(pool_body: _PoolBody, api_key: str, added_minute: int = 0) -> UUID:
    key_id = uuid4()
    from datetime import timedelta

    pool_body.keys[key_id] = PoolKeyEntry(
        id=key_id,
        api_key=api_key,
        label=api_key,
        status="active",
        added_at=NOW + timedelta(minutes=added_minute),
        status_at=NOW,
        last_error=None,
        quota_tier=None,
        character_count=None,
        character_limit=None,
        voice_slots_used=None,
        voice_limit=None,
        quota_refreshed_at=None,
    )
    return key_id


def _provider(
    responses: list[object],
    *,
    projects: _Projects | None = None,
    cloner: _Cloner | None = None,
    repository: _Repository | None = None,
    pool_body: _PoolBody | None = None,
    probe: object | None = None,
    voice_settings: dict[str, float | bool] | None = None,
) -> tuple[ElevenLabsNarrationProvider, _Client, _Projects, _Repository, _PoolBody, _Cloner]:
    client = _Client(responses)
    repository = repository or _Repository()
    projects = projects or _Projects()
    cloner = cloner or _Cloner()
    pool_body = pool_body or _PoolBody()
    pool = ElevenlabsKeyPoolService(pool_body, _QuotaPort(), clock=lambda: NOW)
    provider = ElevenLabsNarrationProvider(
        client,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        pool,
        projects,
        cloner,
        _Storage(),  # type: ignore[arg-type]
        _Transcoder(),  # type: ignore[arg-type]
        model_id="eleven_multilingual_v2",
        voice_settings=voice_settings,
        duration_guard_seconds=60,
        duration_probe=probe,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return provider, client, projects, repository, pool_body, cloner


def _voice() -> ClonedVoice:
    return ClonedVoice(
        VOICE,
        uuid4(),
        OWNER,
        "旁白",
        "备注",
        "cloned-voices/c/v/source.wav",
        "b" * 64,
        12,
        "audio/wav",
        NOW,
    )


def test_submit_packages_raw_wav_as_single_entry_zip_with_el_request_id() -> None:
    provider, client, projects, repository, pool_body, _ = _provider(
        [ElvSynthesisResult(b"mp3-bytes", "req-1")]
    )
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")

    result = asyncio.run(provider.submit(_request()))

    assert client.calls[0]["model_id"] == "eleven_multilingual_v2"
    assert client.calls[0]["text"] == "Hello there."
    assert result.state == "completed"
    assert result.provider_request_id == "req-1"
    assert result.task_id == f"{EL_REQUEST_ID_PREFIX}req-1"
    import zipfile

    assert result.output_zip is not None
    with zipfile.ZipFile(io.BytesIO(result.output_zip)) as archive:
        names = archive.namelist()
        assert len(names) == 1
        expected = f"0001-{_sentence().text_sha256[:12]}.wav"
        assert names == [expected]
        payload = archive.read(expected)
    assert payload == _raw_wav()


def test_submit_omits_voice_settings_by_default() -> None:
    """§35：未配置时不向适配器传 voice_settings（v2 回退路径行为不变）。"""
    provider, client, projects, _, pool_body, _ = _provider(
        [ElvSynthesisResult(b"mp3-bytes", "req-1")]
    )
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")

    asyncio.run(provider.submit(_request()))

    assert client.calls[0]["voice_settings"] is None


def test_submit_passes_configured_voice_settings_through() -> None:
    """§35：配置后单句与批量路径都逐请求透传同一份 voice_settings。"""
    settings = {"stability": 0.5, "similarity_boost": 0.5, "use_speaker_boost": True}
    provider, client, projects, _, pool_body, _ = _provider(
        [ElvSynthesisResult(b"mp3-a", "req-1"), ElvSynthesisResult(b"mp3-b", "req-2")],
        voice_settings=settings,
    )
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")

    asyncio.run(provider.submit(_request()))
    asyncio.run(provider.submit_batch((_request(),)))

    assert client.calls[0]["voice_settings"] == settings
    assert client.calls[1]["voice_settings"] == settings
    # 适配器侧拿到的是副本，调用方事后改字典不得影响已固定的配置
    assert client.calls[0]["voice_settings"] is not settings


def test_submit_clones_into_the_current_account_when_mapping_is_missing() -> None:
    provider, client, projects, repository, pool_body, cloner = _provider(
        [ElvSynthesisResult(b"mp3", "req-2")]
    )
    projects.add_voice(_voice())
    key_id = _key(pool_body, "sk-first")

    asyncio.run(provider.submit(_request()))

    assert cloner.calls == [("sk-first", "旁白")]
    mapping = projects.cloned_voice_account(VOICE, key_id)
    assert mapping is not None and mapping.el_voice_id == "el-cloned"
    assert client.calls[0]["voice_id"] == "el-cloned"


def test_submit_rotates_key_on_quota_error_and_continues_the_same_sentence() -> None:
    provider, client, projects, repository, pool_body, cloner = _provider(
        [
            ProviderHttpStatusError("provider_quota_exhausted", "402", 402),
            ElvSynthesisResult(b"mp3", "req-3"),
        ]
    )
    projects.add_voice(_voice())
    first = _key(pool_body, "sk-exhausted")
    _key(pool_body, "sk-second", added_minute=1)

    result = asyncio.run(provider.submit(_request()))

    assert result.provider_request_id == "req-3"
    assert [call["api_key"] for call in client.calls] == ["sk-exhausted", "sk-second"]
    marked = pool_body.get_key(first)
    assert marked is not None and marked.status == "exhausted"


def test_submit_raises_quota_exhausted_when_every_key_is_gone() -> None:
    provider, _, projects, _, pool_body, _ = _provider(
        [ProviderHttpStatusError("provider_quota_exhausted", "402", 402)]
    )
    projects.add_voice(_voice())
    _key(pool_body, "sk-only")

    with pytest.raises(DomainError) as captured:
        asyncio.run(provider.submit(_request()))

    assert captured.value.code == "provider_quota_exhausted"


def test_submit_rejects_over_long_sentence_before_any_provider_call() -> None:
    provider, client, projects, _, pool_body, _ = _provider([])
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")

    with pytest.raises(DomainError) as captured:
        asyncio.run(provider.submit(_request(text="x" * 10_001)))

    assert captured.value.code == "provider_rejected"
    assert client.calls == []


def test_duration_guard_marks_output_invalid() -> None:
    provider, _, projects, _, pool_body, _ = _provider(
        [ElvSynthesisResult(b"mp3", "req-4")],
        probe=_Probe(frame_count=24_000 * 90),
    )
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")

    with pytest.raises(DomainError) as captured:
        asyncio.run(provider.submit(_request()))

    assert captured.value.code == "provider_output_invalid"


def test_submit_batch_runs_every_sentence_serially_and_reports_progress() -> None:
    provider, client, projects, _, pool_body, _ = _provider(
        [ElvSynthesisResult(b"a", "req-a"), ElvSynthesisResult(b"b", "req-b")]
    )
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")
    progress: list[int] = []
    provider._progress = progress.append  # type: ignore[attr-defined]

    second = dataclasses.replace(
        _request(text="Second sentence."), sentence=_sentence(sequence=2, text="Second sentence.")
    )
    result = asyncio.run(provider.submit_batch((_request(), second)))

    assert [call["text"] for call in client.calls] == ["Hello there.", "Second sentence."]
    assert result.provider_request_id == "req-b"
    assert progress == [1, 2]


def test_submit_batch_returns_one_zip_with_every_unit_and_per_unit_remote_ids() -> None:
    """CosyVoice 产物合同：批内全部句必须在同一个 ZIP 中，且逐句回传远端 id。

    只回最后一句会让 `execute_batch` 的 `extract_batch_audio(expected)` 把其余句判为
    `provider_output_missing`，导致重复向 EL 提交、重复扣字符费。
    """
    import zipfile

    provider, _, projects, _, pool_body, _ = _provider(
        [ElvSynthesisResult(b"first-audio", "req-a"), ElvSynthesisResult(b"second-audio", "req-b")]
    )
    provider._transcoder = _EchoTranscoder()  # type: ignore[attr-defined]
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")

    first = _request()
    second = dataclasses.replace(
        _request(text="Second sentence."), sentence=_sentence(sequence=2, text="Second sentence.")
    )
    result = asyncio.run(provider.submit_batch((first, second)))

    assert result.output_zip is not None
    with zipfile.ZipFile(io.BytesIO(result.output_zip)) as archive:
        assert sorted(archive.namelist()) == sorted([_zip_name(first), _zip_name(second)])
        assert archive.read(_zip_name(first)) == b"raw:first-audio"
        assert archive.read(_zip_name(second)) == b"raw:second-audio"
    assert result.provider_request_ids == ((1, "el:req-a"), (2, "el:req-b"))
    assert result.task_id == "el:req-b"


def test_stitching_chain_feeds_previous_request_ids_to_the_next_sentence() -> None:
    """§23.7：同项目已 COMPLETED 句的 `el:` 远端 id 必须作为 previous_request_ids 传入。"""
    provider, client, projects, repository, pool_body, _ = _provider(
        [ElvSynthesisResult(b"a", "req-1"), ElvSynthesisResult(b"b", "req-2")]
    )
    projects.add_voice(_voice())
    _key(pool_body, "sk-first")
    repository.attempts[(OWNER, "prev-key")] = NarrationAttempt(
        PROJECT,
        OWNER,
        1,
        "prev-key",
        "h" * 64,
        NarrationAttemptState.COMPLETED,
        "call-1",
        remote_id="el:req-1",
    )

    asyncio.run(provider.submit(_request()))

    assert client.calls[0]["previous_request_ids"] == ("el:req-1",)


def test_previous_request_ids_uses_only_el_completed_attempts_and_caps_at_three() -> None:
    def attempt(sequence: int, state: NarrationAttemptState, remote: str) -> NarrationAttempt:
        return NarrationAttempt(
            PROJECT,
            OWNER,
            sequence,
            f"k{sequence}",
            "h" * 64,
            state,
            f"call{sequence}",
            remote_id=remote,
        )

    attempts = (
        attempt(1, NarrationAttemptState.COMPLETED, "el:req-1"),
        attempt(2, NarrationAttemptState.COMPLETED, "cosyvoice-task"),  # 非 EL 前缀：剔除
        attempt(3, NarrationAttemptState.PROVIDER_REJECTED, "el:ignored"),  # 非 COMPLETED：剔除
        attempt(4, NarrationAttemptState.COMPLETED, "el:req-4"),
        attempt(5, NarrationAttemptState.COMPLETED, "el:req-5"),
        attempt(6, NarrationAttemptState.COMPLETED, "el:req-6"),
    )

    assert previous_request_ids(attempts) == ("el:req-4", "el:req-5", "el:req-6")
    assert previous_request_ids(attempts, limit=1) == ("el:req-6",)
    assert previous_request_ids(()) == ()
