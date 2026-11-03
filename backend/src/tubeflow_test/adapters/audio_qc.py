from __future__ import annotations

import audioop
import io
import json
import math
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from tubeflow_test.application.narration_ports import NormalizedAudio
from tubeflow_test.domain.narration import AudioQcResult

NORMALIZATION_VERSION = "pcm-s16le-mono-48000-loudnorm-v4"

_LOUDNESS_TARGET_LUFS = -16.0
_LOUDNESS_TARGET_TRUE_PEAK_DBTP = -1.0
_LOUDNESS_MAX_DEVIATION_LU = 1.0
_LOUDNESS_MAX_TRUE_PEAK_DBTP = -0.8
_SHORT_AUDIO_MAX_FRAMES_24KHZ = 72_000
# 2026-10-14 缺陷修复：CosyVoice 对超短句（单词句等）产出的音频响度可低至
# 约 -26 LUFS（实测失败样本 input_i=-25.75，需要 +9.75 dB 增益），原 ±6 dB
# 初始增益窗口把这类合法短句全部挡在短音频回退路径之外 → 归一化必然失败
# （落 processing_failed），句子在每一轮预算内反复失败直至耗尽。
# 现把初始增益窗口扩至 ±12 dB、最终增益上限 15 dB；输出仍由限幅器与闭环
# 响度门（±1 LU / true peak ≤ -0.8 dBTP）逐次验证，越界依旧 fail-closed。
# 证据与复现：runtime/operations/stuckpoints_fix_20261014.md。
_SHORT_AUDIO_MAX_INITIAL_GAIN_DB = 12.0
_SHORT_AUDIO_MAX_CORRECTION_DB = 3.0
_SHORT_AUDIO_MAX_CORRECTION_STEPS = 2
_SHORT_AUDIO_MAX_FINAL_GAIN_DB = 15.0
_SHORT_AUDIO_LIMITER_LINEAR = 0.881048874
_SHORT_AUDIO_FALLBACK_RECIPE = "short-f32le-24k-gain-limiter-v3"
# 2026-10-27 缺陷修复：>3s 的音频只走两遍 loudnorm(linear=true)，遇到"积分响度低、
# 峰值余量又不足以补到 -16 LUFS"的合法片段（实测 seq 229：-23.11 LUFS / TP -3.49，
# 需 +7.11 dB 但只剩 2.5 dB 余量）时线增益必然停在 -17.43 LUFS，偏差 1.43 LU 超门，
# 每轮预算内以同一条无码 ValueError 反复失败直至耗尽。现主路径完全不变，仅在过不
# 了门时回落到短音频同款「增益 + 限幅 + 闭环校正」，输出仍受同一响度门约束。
_LONG_AUDIO_FALLBACK_RECIPE = "long-f32le-24k-gain-limiter-v1"


class AudioProcessingError(ValueError):
    """带稳定失败码的 ValueError，供配音尝试记录直接落库。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def inspect_wav(payload: bytes) -> AudioQcResult:
    if not payload:
        return AudioQcResult(False, None, None, "empty_audio")
    try:
        with wave.open(io.BytesIO(payload), "rb") as audio:
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            rate = audio.getframerate()
            frames_declared = audio.getnframes()
            compression = audio.getcomptype()
            frames = audio.readframes(frames_declared + 1)
        expected_bytes = frames_declared * channels * width
        if compression != "NONE" or channels <= 0 or width not in {1, 2} or rate <= 0:
            return AudioQcResult(False, None, None, "unsupported_pcm")
        if len(frames) != expected_bytes:
            return AudioQcResult(False, None, "pcm_wav", "truncated_audio")
        expected_riff_size = len(payload) - 8
        if (
            len(payload) < 44
            or payload[:4] != b"RIFF"
            or payload[8:12] != b"WAVE"
            or struct.unpack("<I", payload[4:8])[0] != expected_riff_size
        ):
            return AudioQcResult(False, None, "pcm_wav", "wav_size_mismatch")
        duration = round(frames_declared * 1000 / rate)
        if duration <= 0 or not frames:
            return AudioQcResult(False, None, "pcm_wav", "empty_audio")
        rms = float(audioop.rms(frames, width))
        peak = int(audioop.max(frames, width))
        full_scale = 127 if width == 1 else 32767
        # 8-bit WAV is unsigned while audioop uses signed bytes. Bias it around zero first.
        centered = audioop.bias(frames, 1, -128) if width == 1 else frames
        rms = float(audioop.rms(centered, width))
        peak = int(audioop.max(centered, width))
        activity_frames = centered
        if channels == 2:
            activity_frames = audioop.tomono(centered, width, 0.5, 0.5)
        if width != 2:
            activity_frames = audioop.lin2lin(activity_frames, width, 2)
        samples = struct.iter_unpack("<h", activity_frames)
        active = sum(abs(sample[0]) >= 328 for sample in samples)
        active_ratio = active / frames_declared
        result = AudioQcResult(
            True,
            duration,
            "pcm_wav",
            None,
            rate,
            channels,
            width,
            frames_declared,
            rms,
            peak,
            active_ratio,
        )
        if rms / full_scale < 0.0025 or active_ratio < 0.01:
            return AudioQcResult(
                False,
                duration,
                "pcm_wav",
                "near_silent_audio",
                rate,
                channels,
                width,
                frames_declared,
                rms,
                peak,
                active_ratio,
            )
        return result
    except (EOFError, wave.Error, ValueError, struct.error):
        return _inspect_float32_wav(payload)


def _inspect_float32_wav(payload: bytes) -> AudioQcResult:
    try:
        if (
            len(payload) < 44
            or payload[:4] != b"RIFF"
            or payload[8:12] != b"WAVE"
            or struct.unpack_from("<I", payload, 4)[0] != len(payload) - 8
        ):
            raise ValueError
        offset = 12
        fmt: bytes | None = None
        data: bytes | None = None
        while offset + 8 <= len(payload):
            chunk_id = payload[offset : offset + 4]
            size = struct.unpack_from("<I", payload, offset + 4)[0]
            start, end = offset + 8, offset + 8 + size
            if end > len(payload):
                raise ValueError
            if chunk_id == b"fmt ":
                if fmt is not None:
                    raise ValueError
                fmt = payload[start:end]
            elif chunk_id == b"data":
                if data is not None:
                    raise ValueError
                data = payload[start:end]
            offset = end + (size & 1)
        if fmt is None or data is None or len(fmt) < 16:
            raise ValueError
        audio_format, channels, rate, _, block_align, bits = struct.unpack_from("<HHIIHH", fmt)
        if audio_format == 0xFFFE:
            # WAVE_FORMAT_EXTENSIBLE：ffmpeg 写 32-bit float WAV 的默认封装
            # （fmt 40 字节；SubFormat GUID 前 2 字节为实际格式码，IEEE float = 0x0003）。
            # 2026-10-14 缺陷：这里曾只认裸 tag=3，导致 EL provider 产物 100% 被拒
            # （全批 audio_qc_failed），真实 ffmpeg 产物复验后补齐。
            if len(fmt) < 40 or struct.unpack_from("<H", fmt, 16)[0] < 22:
                return AudioQcResult(False, None, "pcm_f32le", "unsupported_pcm")
            audio_format = struct.unpack_from("<H", fmt, 24)[0]
        if audio_format != 3 or channels != 1 or rate != 24_000 or bits != 32 or block_align != 4:
            return AudioQcResult(False, None, "pcm_f32le", "unsupported_pcm")
        if not data or len(data) % 4:
            raise ValueError
        values = tuple(value[0] for value in struct.iter_unpack("<f", data))
        if any(not math.isfinite(value) or abs(value) > 1.0 for value in values):
            return AudioQcResult(False, None, "pcm_f32le", "invalid_float_samples")
        frames = len(values)
        rms = math.sqrt(sum(value * value for value in values) / frames)
        peak = max(abs(value) for value in values)
        active_ratio = sum(abs(value) >= 0.01 for value in values) / frames
        duration = round(frames * 1000 / rate)
        passed = rms >= 0.0025 and active_ratio >= 0.01
        return AudioQcResult(
            passed,
            duration,
            "pcm_f32le",
            None if passed else "near_silent_audio",
            rate,
            channels,
            4,
            frames,
            rms,
            round(peak * 2**31),
            active_ratio,
        )
    except (ValueError, struct.error, OverflowError):
        return AudioQcResult(False, None, None, "audio_not_decodable")


class FfprobeAudioInspector:
    def __init__(self, executable: str) -> None:
        self._executable = executable

    def inspect(self, payload: bytes) -> AudioQcResult:
        with tempfile.TemporaryDirectory(prefix="tubeflow-raw-probe-") as directory:
            path = Path(directory) / "provider.wav"
            path.write_bytes(payload)
            try:
                result = subprocess.run(
                    (
                        self._executable,
                        "-v",
                        "error",
                        "-select_streams",
                        "a:0",
                        "-show_entries",
                        "stream=codec_name,sample_rate,channels",
                        "-of",
                        "json",
                        str(path),
                    ),
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                return AudioQcResult(False, None, None, "ffprobe_failed")
        if result.returncode != 0:
            return AudioQcResult(False, None, None, "ffprobe_failed")
        try:
            streams = json.loads(result.stdout).get("streams", [])
            stream = streams[0]
            if (
                stream.get("codec_name") != "pcm_f32le"
                or int(stream.get("sample_rate", 0)) != 24_000
                or int(stream.get("channels", 0)) != 1
            ):
                return AudioQcResult(False, None, None, "provider_audio_contract_mismatch")
        except (json.JSONDecodeError, IndexError, AttributeError, TypeError, ValueError):
            return AudioQcResult(False, None, None, "ffprobe_failed")
        return inspect_wav(payload)


class PcmWavNormalizer:
    """Test normalizer; production uses FFmpeg two-pass loudnorm."""

    def normalize(self, payload: bytes, *, strict_loudness: bool = True) -> NormalizedAudio:
        del strict_loudness  # 该测试归一化器不做响度处理，接口对齐生产端口。
        qc = inspect_wav(payload)
        if not qc.passed:
            raise ValueError(f"raw WAV failed QC: {qc.reason}")
        with wave.open(io.BytesIO(payload), "rb") as source:
            frames = source.readframes(source.getnframes())
            width, channels, rate = (
                source.getsampwidth(),
                source.getnchannels(),
                source.getframerate(),
            )
        if width == 1:
            frames = audioop.bias(frames, 1, -128)
        if channels == 2:
            frames = audioop.tomono(frames, width, 0.5, 0.5)
        elif channels != 1:
            raise ValueError("only mono or stereo WAV is supported")
        if width != 2:
            frames = audioop.lin2lin(frames, width, 2)
            width = 2
        if rate != 48_000:
            frames, _ = audioop.ratecv(frames, width, 1, rate, 48_000, None)
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(48_000)
            target.writeframes(frames)
        return NormalizedAudio(
            output.getvalue(),
            {
                "normalizer": "stdlib-wave-audioop",
                "version": NORMALIZATION_VERSION,
                "codec": "pcm_s16le",
                "channels": 1,
                "sample_rate_hz": 48_000,
            },
        )


class FfmpegAudioNormalizer:
    """Production normalizer; the exact executable version and arguments become lineage."""

    def __init__(self, executable: str, *, ffmpeg_version: str) -> None:
        if not executable or not ffmpeg_version:
            raise ValueError("FFmpeg executable and version are required")
        self._executable, self._version = executable, ffmpeg_version

    @staticmethod
    def _parse_loudness_report(
        stderr: bytes,
        *,
        phase: str,
        require_transform_fields: bool = True,
    ) -> dict[str, float]:
        text = stderr.decode("utf-8", errors="replace")
        start, end = text.rfind("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"FFmpeg loudness {phase} was incomplete")
        field_names: tuple[str, ...] = ("input_i", "input_tp")
        if require_transform_fields:
            field_names += ("input_lra", "input_thresh", "target_offset")
        try:
            report = json.loads(text[start : end + 1])
            if not isinstance(report, dict):
                raise TypeError
            values = {name: float(report[name]) for name in field_names}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"FFmpeg loudness {phase} was invalid") from error
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError(f"FFmpeg loudness {phase} was invalid")
        return values

    @staticmethod
    def _within_loudness_gate(report: dict[str, float]) -> bool:
        integrated_lufs = report["input_i"]
        true_peak_dbtp = report["input_tp"]
        return (
            math.isfinite(integrated_lufs)
            and math.isfinite(true_peak_dbtp)
            and abs(integrated_lufs - _LOUDNESS_TARGET_LUFS) <= _LOUDNESS_MAX_DEVIATION_LU
            and true_peak_dbtp <= _LOUDNESS_MAX_TRUE_PEAK_DBTP
        )

    @staticmethod
    def _duration_matches(source: AudioQcResult, output: AudioQcResult) -> bool:
        if (
            source.frame_count is None
            or source.sample_rate_hz is None
            or output.frame_count is None
            or output.sample_rate_hz is None
        ):
            return False
        return abs(
            source.frame_count * output.sample_rate_hz - output.frame_count * source.sample_rate_hz
        ) <= max(source.sample_rate_hz, output.sample_rate_hz)

    @staticmethod
    def _short_source(source: AudioQcResult) -> bool:
        return (
            source.passed
            and source.codec == "pcm_f32le"
            and source.sample_rate_hz == 24_000
            and source.channels == 1
            and source.sample_width_bytes == 4
            and source.frame_count is not None
            and 0 < source.frame_count <= _SHORT_AUDIO_MAX_FRAMES_24KHZ
        )

    @staticmethod
    def _fallback_format_supported(source: AudioQcResult, *, long_audio: bool) -> bool:
        """增益+限幅回落只接受与短音频一致的源格式；长短仅差帧数区间。"""
        if (
            not source.passed
            or source.codec != "pcm_f32le"
            or source.sample_rate_hz != 24_000
            or source.channels != 1
            or source.sample_width_bytes != 4
            or source.frame_count is None
        ):
            return False
        if long_audio:
            return source.frame_count > _SHORT_AUDIO_MAX_FRAMES_24KHZ
        return 0 < source.frame_count <= _SHORT_AUDIO_MAX_FRAMES_24KHZ

    @staticmethod
    def _short_fallback_eligible(source: AudioQcResult, gain_db: float) -> bool:
        return (
            FfmpegAudioNormalizer._fallback_format_supported(source, long_audio=False)
            and math.isfinite(gain_db)
            and abs(gain_db) <= _SHORT_AUDIO_MAX_INITIAL_GAIN_DB
        )

    @staticmethod
    def _long_fallback_eligible(source: AudioQcResult, gain_db: float) -> bool:
        """>3s 源启用增益+限幅回落的资格：格式与短音频一致，仅去掉帧数上限。"""
        return (
            FfmpegAudioNormalizer._fallback_format_supported(source, long_audio=True)
            and math.isfinite(gain_db)
            and abs(gain_db) <= _SHORT_AUDIO_MAX_INITIAL_GAIN_DB
        )

    @staticmethod
    def _short_fallback_filter(gain_db: float, expected_output_frames: int) -> str:
        return (
            "aresample=48000,"
            f"volume={gain_db:.6f}dB,"
            "alimiter="
            f"limit={_SHORT_AUDIO_LIMITER_LINEAR:.9f}:"
            "attack=5:release=50:level=false:latency=true,"
            f"atrim=end_sample={expected_output_frames}"
        )

    def _analyze(
        self,
        path: Path,
        recipe: tuple[str, ...],
        *,
        phase: str,
        require_transform_fields: bool = True,
    ) -> dict[str, float]:
        arguments = tuple(str(path) if item == "{input}" else item for item in recipe)
        completed = subprocess.run(
            (self._executable, *arguments),
            capture_output=True,
            check=False,
            timeout=120,
            shell=False,
        )
        if completed.returncode != 0:
            raise ValueError(f"FFmpeg loudness {phase} failed")
        return self._parse_loudness_report(
            completed.stderr,
            phase=phase,
            require_transform_fields=require_transform_fields,
        )

    def _render_and_verify(
        self,
        source_path: Path,
        output_path: Path,
        audio_filter: str,
        output_recipe: tuple[str, ...],
        analysis_recipe: tuple[str, ...],
        source_qc: AudioQcResult,
    ) -> tuple[bytes, AudioQcResult, dict[str, float]]:
        arguments = tuple(
            str(source_path)
            if item == "{input}"
            else str(output_path)
            if item == "{output}"
            else audio_filter
            if item == "{audio_filter}"
            else item
            for item in output_recipe
        )
        completed = subprocess.run(
            (self._executable, *arguments),
            capture_output=True,
            check=False,
            timeout=120,
            shell=False,
        )
        if completed.returncode != 0 or not output_path.is_file():
            raise ValueError("FFmpeg normalization failed")
        normalized = output_path.read_bytes()
        output_qc = inspect_wav(normalized)
        if not output_qc.passed:
            raise ValueError("FFmpeg produced an invalid normalized WAV")
        if not self._duration_matches(source_qc, output_qc):
            raise ValueError("FFmpeg normalized audio duration changed")
        verified = self._analyze(
            output_path,
            analysis_recipe,
            phase="verification",
            require_transform_fields=False,
        )
        return normalized, output_qc, verified

    def _normalize_short(
        self,
        input_path: Path,
        directory: Path,
        output_recipe: tuple[str, ...],
        analysis_recipe: tuple[str, ...],
        source_qc: AudioQcResult,
        measured: dict[str, float],
        *,
        long_audio: bool = False,
        strict_loudness: bool = True,
    ) -> tuple[bytes, AudioQcResult, dict[str, float], dict[str, object]]:
        # Below the loudnorm 3s measurement window LRA and target_offset are
        # unmeasurable, so short inputs take the bounded gain+limiter recipe
        # directly instead of the two-pass loudnorm render.
        # 2026-10-27：>3s 音频仅在两遍 loudnorm 过不了响度门时作为回落进入本路径，
        # 判定与输出门槛完全一致（同一响度门、同一时长守恒），仅配方标识不同。
        # 2026-10-28：strict_loudness=False 仅由 ElevenLabs 线路启用——不因"需要超过
        # 12 dB 增益"提前判失败，也不因响度门未过而抛错；仍受 15 dB 最终增益上限、
        # 限幅器、时长守恒与 48k/mono/s16 契约约束，未达标只记 loudness_gate=outside。
        gain_db = _LOUDNESS_TARGET_LUFS - measured["input_i"]
        supported = self._fallback_format_supported(source_qc, long_audio=long_audio)
        eligible = (
            supported
            and math.isfinite(gain_db)
            and abs(gain_db) <= _SHORT_AUDIO_MAX_INITIAL_GAIN_DB
        )
        initial_gain_clamped = False
        if not eligible:
            if strict_loudness or not supported or not math.isfinite(gain_db):
                raise AudioProcessingError(
                    "audio_loudness_unreachable",
                    "FFmpeg normalized loudness is outside tolerance",
                )
            gain_db = round(
                max(
                    -_SHORT_AUDIO_MAX_FINAL_GAIN_DB,
                    min(_SHORT_AUDIO_MAX_FINAL_GAIN_DB, gain_db),
                ),
                6,
            )
            initial_gain_clamped = True
        assert source_qc.frame_count is not None and source_qc.sample_rate_hz is not None
        expected_output_frames = round(source_qc.frame_count * 48_000 / source_qc.sample_rate_hz)
        initial_gain_db = round(gain_db, 6)
        fallback_filter = self._short_fallback_filter(initial_gain_db, expected_output_frames)
        normalized, output_qc, preflight = self._render_and_verify(
            input_path,
            directory / ("normalized-long-preflight.wav" if long_audio else "normalized-short-preflight.wav"),
            fallback_filter,
            output_recipe,
            analysis_recipe,
            source_qc,
        )
        correction_steps_db: list[float] = []
        correction_trace: list[dict[str, object]] = []
        final_gain_db = initial_gain_db
        verified = preflight
        for correction_index in range(1, _SHORT_AUDIO_MAX_CORRECTION_STEPS + 1):
            if self._within_loudness_gate(verified):
                break
            requested_correction_db = _LOUDNESS_TARGET_LUFS - verified["input_i"]
            if not math.isfinite(requested_correction_db):
                if strict_loudness:
                    raise AudioProcessingError(
                        "audio_loudness_unreachable",
                        "FFmpeg normalized loudness is outside tolerance",
                    )
                break
            correction_db = round(
                max(
                    -_SHORT_AUDIO_MAX_CORRECTION_DB,
                    min(_SHORT_AUDIO_MAX_CORRECTION_DB, requested_correction_db),
                ),
                6,
            )
            candidate_gain_db = round(final_gain_db + correction_db, 6)
            if (
                abs(correction_db) < 1e-6
                or not math.isfinite(candidate_gain_db)
                or abs(candidate_gain_db) > _SHORT_AUDIO_MAX_FINAL_GAIN_DB
            ):
                if strict_loudness:
                    raise AudioProcessingError(
                        "audio_loudness_unreachable",
                        "FFmpeg normalized loudness is outside tolerance",
                    )
                break
            correction_steps_db.append(correction_db)
            final_gain_db = candidate_gain_db
            fallback_filter = self._short_fallback_filter(final_gain_db, expected_output_frames)
            fallback_output_path = directory / (
                f"normalized-{'long' if long_audio else 'short'}-correction-{correction_index}.wav"
            )
            normalized, output_qc, verified = self._render_and_verify(
                input_path,
                fallback_output_path,
                fallback_filter,
                output_recipe,
                analysis_recipe,
                source_qc,
            )
            gate_passed = self._within_loudness_gate(verified)
            correction_trace.append(
                {
                    "attempt": correction_index,
                    "requested_step_db": round(requested_correction_db, 6),
                    "applied_step_db": round(correction_db, 6),
                    "cumulative_gain_db": round(final_gain_db, 6),
                    "verified_integrated_lufs": verified["input_i"],
                    "verified_true_peak_dbtp": verified["input_tp"],
                    "gate_passed": gate_passed,
                }
            )
        gate_inside = self._within_loudness_gate(verified)
        if not gate_inside and strict_loudness:
            raise AudioProcessingError(
                "audio_loudness_unreachable",
                "FFmpeg normalized loudness is outside tolerance",
            )
        fallback_manifest: dict[str, object] = {
            "recipe_id": (
                _LONG_AUDIO_FALLBACK_RECIPE if long_audio else _SHORT_AUDIO_FALLBACK_RECIPE
            ),
            "loudness_policy": "strict" if strict_loudness else "best_effort",
            "loudness_gate": "inside" if gate_inside else "outside",
            "initial_gain_clamped": initial_gain_clamped,
            "filter_template": (
                "aresample=48000,volume={gain_db}dB,"
                "alimiter=limit=0.881048874:attack=5:release=50:"
                "level=false:latency=true,"
                "atrim=end_sample={expected_output_frames}"
            ),
            "gain_db": round(final_gain_db, 6),
            "initial_gain_db": round(initial_gain_db, 6),
            "initial_gain_limit_db": _SHORT_AUDIO_MAX_INITIAL_GAIN_DB,
            "gain_correction_db": round(sum(correction_steps_db), 6),
            "gain_correction_steps_db": [round(item, 6) for item in correction_steps_db],
            "correction_trace": correction_trace,
            "gain_correction_step_limit_db": _SHORT_AUDIO_MAX_CORRECTION_DB,
            "max_correction_steps": _SHORT_AUDIO_MAX_CORRECTION_STEPS,
            "final_gain_limit_db": _SHORT_AUDIO_MAX_FINAL_GAIN_DB,
            "preflight_loudness": {
                "integrated_lufs": preflight["input_i"],
                "true_peak_dbtp": preflight["input_tp"],
            },
            "limiter_linear": _SHORT_AUDIO_LIMITER_LINEAR,
            "max_source_frames_24khz": (
                None if long_audio else _SHORT_AUDIO_MAX_FRAMES_24KHZ
            ),
            "source_frame_count": source_qc.frame_count,
            "expected_output_frames": expected_output_frames,
        }
        return normalized, output_qc, verified, fallback_manifest

    def normalize(self, payload: bytes, *, strict_loudness: bool = True) -> NormalizedAudio:
        analysis_recipe = (
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-i",
            "{input}",
            "-af",
            "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        )
        output_recipe = (
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            "{input}",
            "-map_metadata",
            "-1",
            "-af",
            "{audio_filter}",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "{output}",
        )
        source_qc = inspect_wav(payload)
        if not source_qc.passed:
            raise ValueError(f"raw WAV failed QC: {source_qc.reason}")
        selected_path = "two-pass-loudnorm"
        fallback_manifest: dict[str, object] | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="tubeflow-narration-") as directory:
                workdir = Path(directory)
                input_path = workdir / "input.wav"
                input_path.write_bytes(payload)
                short_source = self._short_source(source_qc)
                measured = self._analyze(
                    input_path,
                    analysis_recipe,
                    phase="analysis",
                    require_transform_fields=not short_source,
                )
                if short_source:
                    normalized, output_qc, verified, fallback_manifest = self._normalize_short(
                        input_path,
                        workdir,
                        output_recipe,
                        analysis_recipe,
                        source_qc,
                        measured,
                        strict_loudness=strict_loudness,
                    )
                    selected_path = _SHORT_AUDIO_FALLBACK_RECIPE
                else:
                    loudnorm = (
                        "loudnorm=I=-16:TP=-1:LRA=11:linear=true:"
                        f"measured_I={measured['input_i']:.6f}:"
                        f"measured_TP={measured['input_tp']:.6f}:"
                        f"measured_LRA={measured['input_lra']:.6f}:"
                        f"measured_thresh={measured['input_thresh']:.6f}:"
                        f"offset={measured['target_offset']:.6f}"
                    )
                    normalized, output_qc, verified = self._render_and_verify(
                        input_path,
                        workdir / "normalized-primary.wav",
                        loudnorm,
                        output_recipe,
                        analysis_recipe,
                        source_qc,
                    )
                    if not self._within_loudness_gate(verified):
                        # 线增益不足的合法长句：改用限幅器补足动态余量，仍过同一响度门。
                        normalized, output_qc, verified, fallback_manifest = (
                            self._normalize_short(
                                input_path,
                                workdir,
                                output_recipe,
                                analysis_recipe,
                                source_qc,
                                measured,
                                long_audio=True,
                                strict_loudness=strict_loudness,
                            )
                        )
                        selected_path = _LONG_AUDIO_FALLBACK_RECIPE
        except (subprocess.TimeoutExpired, OSError) as error:
            raise ValueError("FFmpeg normalization could not complete") from error
        integrated_lufs = verified["input_i"]
        true_peak_dbtp = verified["input_tp"]
        assert source_qc.frame_count is not None and source_qc.sample_rate_hz is not None
        assert output_qc.frame_count is not None and output_qc.sample_rate_hz is not None
        duration_delta = abs(
            source_qc.frame_count * output_qc.sample_rate_hz
            - output_qc.frame_count * source_qc.sample_rate_hz
        )
        manifest: dict[str, object] = {
            "normalizer": "ffmpeg",
            "ffmpeg_version": self._version,
            "recipe_version": NORMALIZATION_VERSION,
            "selected_path": selected_path,
            "analysis_arguments": list(analysis_recipe),
            "output_arguments": list(output_recipe),
            "loudnorm": {"integrated_lufs": -16, "true_peak_dbtp": -1, "lra": 11},
            "source_loudness_analysis": measured,
            "verified_loudness": {
                "integrated_lufs": integrated_lufs,
                "true_peak_dbtp": true_peak_dbtp,
            },
            "duration_contract": {
                "source_frame_count": source_qc.frame_count,
                "source_sample_rate_hz": source_qc.sample_rate_hz,
                "output_frame_count": output_qc.frame_count,
                "output_sample_rate_hz": output_qc.sample_rate_hz,
                "cross_product_delta": duration_delta,
                "cross_product_tolerance": max(source_qc.sample_rate_hz, output_qc.sample_rate_hz),
            },
            "codec": "pcm_s16le",
            "channels": 1,
            "sample_rate_hz": 48_000,
            "loudness_policy": "strict" if strict_loudness else "best_effort",
            "loudness_gate": (
                "inside" if self._within_loudness_gate(verified) else "outside"
            ),
        }
        if fallback_manifest is not None:
            manifest["fallback"] = fallback_manifest
        return NormalizedAudio(
            normalized,
            manifest,
        )
