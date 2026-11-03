"""ElevenLabs 配音 Provider（实现既有 Provider 端口，§23.4/§23.7/§23.8）。

要点：
- 逐句合成（EL 无批量概念）：`submit_batch` 逐句串行调用 `submit`，`submit` 保持单句幂等语义。
- 产物同构：EL 返回的原生音频经 ffmpeg 解码重采样为 `pcm_f32le/24kHz/mono` WAV，
  按 `{seq:04d}-{text_sha256[:12]}.wav` 打包成单条目 ZIP 返回，下游链路零改动。
- 逻辑音色按账号解析：缺失映射时用频道源音频在该账号自动补克隆（§23.4）。
- 失败处置：402 / 复核确认的 401额度 判定为「账号病」→ 标记该 key 后换新 key 原地续跑
  （不消耗该句的 3 次提交预算）；429/5xx/超时按既有稳定码抛出走 §15 退避（AUTO）。
- 全池无 active key → `provider_quota_exhausted`（项目暂停原因，§23.6）。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from tubeflow_test.adapters.audio_qc import FfprobeAudioInspector
from tubeflow_test.adapters.audio_transcode import AudioTranscodePort
from tubeflow_test.adapters.elevenlabs import ElevenLabsHttpClient
from tubeflow_test.application.narration_ports import (
    EL_REQUEST_ID_PREFIX,
    NarrationBatchFailure,
    NarrationRepositoryPort,
    SynthesisRequest,
    SynthesisResult,
    previous_request_ids,
)
from tubeflow_test.domain.errors import DomainError, ProviderHttpStatusError
from tubeflow_test.elevenlabs.ports import PoolKeyEntry
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService
from tubeflow_test.identity_project.cloned_voices import (
    ClonedVoiceCatalogPort,
    ClonedVoiceClonePort,
    ClonedVoiceSourceStorage,
)
from tubeflow_test.identity_project.domain import ClonedVoice, ClonedVoiceAccount

MAX_TEXT_CHARS = 10_000
# §23.6：账号级失败（额度耗尽/失效）换 key 原地续跑；其余错误按既有稳定码抛出。
_ROTATION_ERROR_CODES = frozenset({"provider_quota_exhausted", "provider_auth_failed"})


class KeyRotationNeeded(Exception):
    """当前 key 已确认不可用（额度耗尽/失效），需要换 key 原地续跑该句。"""

    def __init__(self, status_code: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ElevenLabsNarrationProvider:
    def __init__(
        self,
        client: ElevenLabsHttpClient,
        repository: NarrationRepositoryPort,
        pool: ElevenlabsKeyPoolService,
        projects: ClonedVoiceCatalogPort,
        cloner: ClonedVoiceClonePort,
        storage: ClonedVoiceSourceStorage,
        transcoder: AudioTranscodePort,
        *,
        model_id: str,
        voice_settings: dict[str, float | bool] | None = None,
        duration_guard_seconds: int = 60,
        duration_probe: FfprobeAudioInspector | None = None,
        clock: Callable[[], datetime] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        self._client = client
        self._repository = repository
        self._pool = pool
        self._projects = projects
        self._cloner = cloner
        self._storage = storage
        self._transcoder = transcoder
        self._model_id = model_id
        # §35：None = 不发送 voice_settings（回退 v2 时保持原行为）；配置后逐请求透传。
        self._voice_settings = dict(voice_settings) if voice_settings else None
        self._duration_guard_seconds = duration_guard_seconds
        self._probe = duration_probe
        self._clock = clock or (lambda: datetime.now(UTC))
        self._progress = progress

    async def submit(self, request: SynthesisRequest) -> SynthesisResult:
        raw, entry, request_id = await self._synthesize_with_rotation(request)
        return self._package(request, entry, raw, request_id)

    async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
        """EL 无批量语义：逐句串行合成，但必须返回**含批内全部句**的一个 ZIP。

        下游 `SubmitSentenceNarration.execute_batch` 按 CosyVoice 合同用
        `extract_batch_audio(output_zip, expected)` 取全部成员；若只回最后一句，
        批内其它句会被判 `provider_output_missing` 并重复向 EL 提交、重复扣字符费。
        单句确定性失败时**不丢弃同批已成功句**：仍打包返回，并用
        `NarrationBatchFailure` 携带逐句失败码（否则整批重投也会重复付费）。
        """
        units: list[tuple[SynthesisRequest, PoolKeyEntry, str | None, bytes]] = []
        failures: list[tuple[int, str]] = []
        for request in requests:
            try:
                raw, entry, request_id = await self._synthesize_with_rotation(request)
            except DomainError as error:
                failures.append((request.sentence.sequence, error.code))
                continue
            units.append((request, entry, request_id, raw))
            if self._progress is not None:
                self._progress(request.sentence.sequence)
        if not units and not failures:
            raise DomainError("invalid_batch", "Narration batch must contain at least one unit.")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for request, _entry, _request_id, raw in units:
                archive.writestr(_unit_name(request), raw)
        last_request_id = units[-1][2] if units else None
        last_entry = units[-1][1] if units else None
        result = SynthesisResult(
            task_id=(
                f"{EL_REQUEST_ID_PREFIX}{last_request_id}"
                if last_request_id
                else f"el:{last_entry.id}" if last_entry is not None else "el:batch"
            ),
            state="partial" if failures else "completed",
            output_zip=buffer.getvalue(),
            provider_request_id=last_request_id,
            provider_request_ids=tuple(
                (request.sentence.sequence, f"{EL_REQUEST_ID_PREFIX}{request_id}")
                for request, _entry, request_id, _raw in units
                if request_id
            ),
            failed_sequences=tuple(sequence for sequence, _code in failures),
            errors=tuple(failures),
        )
        if failures:
            raise NarrationBatchFailure(
                "ElevenLabs batch submission failed for "
                + ",".join(str(sequence) for sequence, _code in failures),
                result,
            )
        return result

    async def _synthesize_with_rotation(
        self, request: SynthesisRequest
    ) -> tuple[bytes, PoolKeyEntry, str | None]:
        cloned_voice_id = self._require_cloned_voice(request)
        text = request.sentence.text
        if len(text) > MAX_TEXT_CHARS:
            # §15.3 预防性拦截：v2 单请求字符上限 10000。
            raise DomainError(
                "provider_rejected", "Sentence exceeds the ElevenLabs per-request character limit."
            )
        previous = previous_request_ids(
            self._repository.list_attempts(request.owner_id, request.project_id)
        )
        tried: set[UUID] = set()
        while True:
            entry = self._next_key(tried)
            tried.add(entry.id)
            try:
                voice_id = await self._resolve_voice(entry, cloned_voice_id)
                outcome = await self._client.synthesize(
                    entry.api_key,
                    voice_id,
                    text,
                    model_id=self._model_id,
                    previous_request_ids=previous,
                    voice_settings=self._voice_settings,
                )
                raw = self._transcoder.to_raw_wav(outcome.audio)
                self._assert_duration(raw, request)
                return raw, entry, outcome.request_id
            except DomainError as error:
                if error.code not in _ROTATION_ERROR_CODES:
                    raise
                self._pool.rotate_on_failure(
                    entry.id,
                    status_code=(
                        error.status_code
                        if isinstance(error, ProviderHttpStatusError)
                        else None
                    ),
                    recheck=None,
                    detail=error.message,
                )
                if not self._pool.active_keys():
                    raise DomainError(
                        "provider_quota_exhausted",
                        "No usable ElevenLabs key remains in the pool.",
                    ) from error

    def _next_key(self, tried: set[UUID]) -> PoolKeyEntry:
        for entry in self._pool.active_keys():
            if entry.id not in tried:
                return entry
        raise DomainError(
            "provider_quota_exhausted", "No usable ElevenLabs key remains in the pool."
        )

    def _require_cloned_voice(self, request: SynthesisRequest) -> UUID:
        cloned_voice_id = request.cloned_voice_id
        if request.provider != "elevenlabs":
            raise DomainError("provider_not_configured", "Request is not an ElevenLabs narration.")
        if cloned_voice_id is None:
            raise DomainError(
                "cloned_voice_unavailable", "The narration request has no frozen cloned voice."
            )
        return cloned_voice_id

    async def _resolve_voice(self, entry: PoolKeyEntry, cloned_voice_id: UUID) -> str:
        """按当前账号解析远端 voice_id；缺失时用频道源音频自动补克隆（§23.4）。"""
        mapping = self._projects.cloned_voice_account(cloned_voice_id, entry.id)
        if mapping is not None and mapping.el_voice_id:
            return mapping.el_voice_id
        voice = self._projects.cloned_voice_by_id(cloned_voice_id)
        if voice is None:
            raise DomainError(
                "cloned_voice_unavailable", "The frozen cloned voice is unavailable."
            )
        return await self._clone_into_account(entry, voice)

    async def _clone_into_account(self, entry: PoolKeyEntry, voice: ClonedVoice) -> str:
        source = self._verified_source(voice)
        try:
            el_voice_id = await self._cloner.clone_voice(
                entry.api_key,
                voice.name,
                audio=source,
                filename=f"source{_source_suffix(voice.source_object_key)}",
                description=voice.remark,
            )
        except DomainError as error:
            if error.code in {"provider_quota_exhausted", "provider_auth_failed"}:
                raise KeyRotationNeeded(None, error.message) from error
            raise
        try:
            self._projects.save_cloned_voice_account(
                ClonedVoiceAccount(voice.id, entry.id, el_voice_id, self._clock())
            )
        except DomainError:
            # §23.4：跨进程并发补克隆由唯一约束兜底，冲突时重读映射。
            existing = self._projects.cloned_voice_account(voice.id, entry.id)
            if existing is not None and existing.el_voice_id:
                return existing.el_voice_id
            raise
        return el_voice_id

    def _verified_source(self, voice: ClonedVoice) -> bytes:
        try:
            return self._storage.get_verified(
                voice.source_object_key, voice.source_sha256, voice.source_size_bytes
            )
        except ValueError as error:
            raise DomainError(
                "cloned_voice_source_mismatch",
                "The cloned voice source audio failed integrity verification.",
            ) from error

    def _package(
        self,
        request: SynthesisRequest,
        entry: PoolKeyEntry,
        raw: bytes,
        request_id: str | None,
    ) -> SynthesisResult:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(_unit_name(request), raw)
        return SynthesisResult(
            # `el:` 前缀标记 EL 远端 id：拼接链只接受该前缀（§23.7）。
            task_id=f"{EL_REQUEST_ID_PREFIX}{request_id}" if request_id else f"el:{entry.id}",
            state="completed",
            output_zip=buffer.getvalue(),
            provider_request_id=request_id,
        )

    def _assert_duration(self, raw: bytes, request: SynthesisRequest) -> None:
        """时长护栏（§23.7）：实测时长超阈值判异常，落既有 OUTPUT_INVALID 重配语义。"""
        if self._probe is None:
            return
        qc = self._probe.inspect(raw)
        frames = qc.frame_count
        rate = qc.sample_rate_hz
        if frames is None or rate is None or rate <= 0:
            return
        seconds = frames / rate
        if seconds > self._duration_guard_seconds:
            raise DomainError(
                "provider_output_invalid",
                (
                    "ElevenLabs output exceeds the duration guard "
                    f"({seconds:.1f}s > {self._duration_guard_seconds}s)."
                ),
            )


def _unit_name(request: SynthesisRequest) -> str:
    """与 CosyVoice 产物同构的 ZIP 成员名（下游 `extract_batch_audio` 依赖该格式）。"""
    return f"{request.sentence.sequence:04d}-{request.sentence.text_sha256[:12]}.wav"


def _source_suffix(object_key: str) -> str:
    name = object_key.rsplit("/", 1)[-1]
    marker = name.rfind(".")
    return name[marker:] if marker >= 0 else ".wav"
