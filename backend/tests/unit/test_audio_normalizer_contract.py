from __future__ import annotations

import io
import json
import math
import struct
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from tubeflow_test.adapters.audio_qc import (
    AudioProcessingError,
    FfmpegAudioNormalizer,
    inspect_wav,
)
from tubeflow_test.domain.narration import AudioQcResult


def _float32_wav(frame_count: int = 24_000, *, rate: int = 24_000) -> bytes:
    values = [0.12 * math.sin(2 * math.pi * 220 * index / rate) for index in range(frame_count)]
    samples = b"".join(struct.pack("<f", value) for value in values)
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _pcm16_wav(frame_count: int, *, rate: int = 48_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(struct.pack("<h", 1000) * frame_count)
    return output.getvalue()


def _report(integrated_lufs: float, true_peak_dbtp: float) -> dict[str, float]:
    return {
        "input_i": integrated_lufs,
        "input_tp": true_peak_dbtp,
        "input_lra": 0.0,
        "input_thresh": integrated_lufs - 10.0,
        "target_offset": -16.0 - integrated_lufs,
    }


class _SimulatedFfmpegNormalizer(FfmpegAudioNormalizer):
    def __init__(self, render_reports: tuple[dict[str, float], ...]) -> None:
        super().__init__("ffmpeg", ffmpeg_version="test-version")
        self._render_reports = iter(render_reports)
        self.render_calls = 0

    def _analyze(
        self,
        path: Path,
        recipe: tuple[str, ...],
        *,
        phase: str,
        require_transform_fields: bool = True,
    ) -> dict[str, float]:
        del path, recipe, require_transform_fields
        assert phase == "analysis"
        return _report(-17.0, -0.1)

    def _render_and_verify(
        self,
        source_path: Path,
        output_path: Path,
        audio_filter: str,
        output_recipe: tuple[str, ...],
        analysis_recipe: tuple[str, ...],
        source_qc: AudioQcResult,
    ) -> tuple[bytes, AudioQcResult, dict[str, float]]:
        del source_path, output_path, audio_filter, output_recipe, analysis_recipe
        assert source_qc.frame_count is not None
        self.render_calls += 1
        payload = _pcm16_wav(source_qc.frame_count * 2)
        qc = inspect_wav(payload)
        assert qc.passed
        return payload, qc, next(self._render_reports)


class _RoutingProbeNormalizer(FfmpegAudioNormalizer):
    def __init__(self, analysis_report: dict[str, float], render_report: dict[str, float]) -> None:
        super().__init__("ffmpeg", ffmpeg_version="test-version")
        self._analysis_report = analysis_report
        self._render_report = render_report
        self.analysis_require_transform_fields: list[bool] = []
        self.render_calls = 0

    def _analyze(
        self,
        path: Path,
        recipe: tuple[str, ...],
        *,
        phase: str,
        require_transform_fields: bool = True,
    ) -> dict[str, float]:
        del path, recipe
        assert phase == "analysis"
        self.analysis_require_transform_fields.append(require_transform_fields)
        return dict(self._analysis_report)

    def _render_and_verify(
        self,
        source_path: Path,
        output_path: Path,
        audio_filter: str,
        output_recipe: tuple[str, ...],
        analysis_recipe: tuple[str, ...],
        source_qc: AudioQcResult,
    ) -> tuple[bytes, AudioQcResult, dict[str, float]]:
        del source_path, output_path, audio_filter, output_recipe, analysis_recipe
        assert source_qc.frame_count is not None
        self.render_calls += 1
        payload = _pcm16_wav(source_qc.frame_count * 2)
        qc = inspect_wav(payload)
        assert qc.passed
        return payload, qc, dict(self._render_report)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, "0:volume=99"])
def test_loudness_report_rejects_non_finite_or_filter_text(value: object) -> None:
    report = {
        "input_i": value,
        "input_tp": -1.0,
        "input_lra": 0.0,
        "input_thresh": -26.0,
        "target_offset": 0.0,
    }

    with pytest.raises(ValueError, match="was invalid"):
        FfmpegAudioNormalizer._parse_loudness_report(json.dumps(report).encode(), phase="test")


def test_loudness_gate_has_closed_finite_boundaries() -> None:
    gate = FfmpegAudioNormalizer._within_loudness_gate

    assert gate(_report(-17.0, -0.8))
    assert gate(_report(-15.0, -0.8))
    assert not gate(_report(-17.01, -0.8))
    assert not gate(_report(-14.99, -0.8))
    assert not gate(_report(-16.0, -0.79))
    assert not gate(_report(math.nan, -1.0))


def test_short_fallback_eligibility_enforces_format_duration_and_initial_gain() -> None:
    eligible = AudioQcResult(
        True,
        3_000,
        "pcm_f32le",
        None,
        24_000,
        1,
        4,
        72_000,
        0.1,
        1,
        1.0,
    )

    assert FfmpegAudioNormalizer._short_fallback_eligible(eligible, 12.0)
    assert FfmpegAudioNormalizer._short_fallback_eligible(eligible, -12.0)
    assert not FfmpegAudioNormalizer._short_fallback_eligible(eligible, 12.01)
    assert not FfmpegAudioNormalizer._short_fallback_eligible(eligible, -12.01)
    assert not FfmpegAudioNormalizer._short_fallback_eligible(
        replace(eligible, frame_count=72_001), 1.0
    )
    assert not FfmpegAudioNormalizer._short_fallback_eligible(
        replace(eligible, codec="pcm_wav"), 1.0
    )


def test_fallback_manifest_records_bounded_correction_without_temp_paths() -> None:
    normalizer = _SimulatedFfmpegNormalizer((_report(-18.0, -1.1), _report(-16.0, -1.1)))

    result = normalizer.normalize(_float32_wav())

    assert normalizer.render_calls == 2
    assert result.manifest["recipe_version"] == "pcm-s16le-mono-48000-loudnorm-v4"
    assert result.manifest["selected_path"] == "short-f32le-24k-gain-limiter-v3"
    fallback = result.manifest["fallback"]
    assert isinstance(fallback, dict)
    assert fallback["initial_gain_db"] == 1.0
    assert fallback["gain_correction_db"] == 2.0
    assert fallback["gain_correction_steps_db"] == [2.0]
    assert fallback["gain_db"] == 3.0
    assert fallback["initial_gain_limit_db"] == 12.0
    assert fallback["gain_correction_step_limit_db"] == 3.0
    assert fallback["max_correction_steps"] == 2
    assert fallback["final_gain_limit_db"] == 15.0
    assert fallback["correction_trace"] == [
        {
            "attempt": 1,
            "requested_step_db": 2.0,
            "applied_step_db": 2.0,
            "cumulative_gain_db": 3.0,
            "verified_integrated_lufs": -16.0,
            "verified_true_peak_dbtp": -1.1,
            "gate_passed": True,
        }
    ]
    duration = result.manifest["duration_contract"]
    assert isinstance(duration, dict) and duration["cross_product_delta"] == 0
    serialized = json.dumps(result.manifest)
    assert "tubeflow-narration-" not in serialized and "normalized-short" not in serialized


def test_fallback_uses_at_most_two_bounded_correction_steps() -> None:
    normalizer = _SimulatedFfmpegNormalizer(
        (
            _report(-30.0, -10.0),
            _report(-18.0, -1.1),
            _report(-16.5, -1.1),
        )
    )

    result = normalizer.normalize(_float32_wav())

    assert normalizer.render_calls == 3
    fallback = result.manifest["fallback"]
    assert isinstance(fallback, dict)
    assert fallback["gain_correction_steps_db"] == [3.0, 2.0]
    assert fallback["gain_correction_db"] == 5.0
    assert fallback["gain_db"] == 6.0
    trace = fallback["correction_trace"]
    assert isinstance(trace, list)
    assert [item["attempt"] for item in trace] == [1, 2]
    assert [item["requested_step_db"] for item in trace] == pytest.approx([14.0, 2.0])
    assert [item["applied_step_db"] for item in trace] == pytest.approx([3.0, 2.0])
    assert [item["cumulative_gain_db"] for item in trace] == pytest.approx([4.0, 6.0])
    assert [item["gate_passed"] for item in trace] == [False, True]


def test_corrected_fallback_still_fails_closed_when_final_gate_is_missed() -> None:
    normalizer = _SimulatedFfmpegNormalizer(
        (
            _report(-30.0, -10.0),
            _report(-18.0, -1.1),
            _report(-17.01, -1.1),
        )
    )

    with pytest.raises(ValueError, match="outside tolerance"):
        normalizer.normalize(_float32_wav())

    assert normalizer.render_calls == 3


def test_short_source_detection_boundaries() -> None:
    base = AudioQcResult(True, 3_000, "pcm_f32le", None, 24_000, 1, 4, 72_000, 0.1, 1, 1.0)

    assert FfmpegAudioNormalizer._short_source(base)
    assert FfmpegAudioNormalizer._short_source(replace(base, frame_count=1))
    assert not FfmpegAudioNormalizer._short_source(replace(base, frame_count=72_001))
    assert not FfmpegAudioNormalizer._short_source(replace(base, frame_count=0))
    assert not FfmpegAudioNormalizer._short_source(replace(base, frame_count=None))
    assert not FfmpegAudioNormalizer._short_source(replace(base, passed=False))
    assert not FfmpegAudioNormalizer._short_source(replace(base, codec="pcm_wav"))
    assert not FfmpegAudioNormalizer._short_source(replace(base, sample_rate_hz=48_000))
    assert not FfmpegAudioNormalizer._short_source(replace(base, channels=2))
    assert not FfmpegAudioNormalizer._short_source(replace(base, sample_width_bytes=2))


def test_short_source_skips_unmeasurable_loudnorm_transform_fields() -> None:
    normalizer = _RoutingProbeNormalizer(
        {"input_i": -16.5, "input_tp": -1.5},
        {"input_i": -16.2, "input_tp": -1.1},
    )

    result = normalizer.normalize(_float32_wav(24_000))

    assert normalizer.analysis_require_transform_fields == [False]
    assert normalizer.render_calls == 1
    assert result.manifest["selected_path"] == "short-f32le-24k-gain-limiter-v3"
    assert result.manifest["source_loudness_analysis"] == {"input_i": -16.5, "input_tp": -1.5}
    assert isinstance(result.manifest["fallback"], dict)


class _FixedAnalysisNormalizer(_SimulatedFfmpegNormalizer):
    """用固定的初始响度分析复现真实产物（如 CosyVoice 安静短句）的增益需求。"""

    def __init__(
        self, analysis: dict[str, float], render_reports: tuple[dict[str, float], ...]
    ) -> None:
        super().__init__(render_reports)
        self._analysis = analysis

    def _analyze(
        self,
        path: Path,
        recipe: tuple[str, ...],
        *,
        phase: str,
        require_transform_fields: bool = True,
    ) -> dict[str, float]:
        del path, recipe, require_transform_fields
        assert phase == "analysis"
        return dict(self._analysis)


def test_quiet_short_clip_within_widened_window_normalizes() -> None:
    # 2026-10-14 生产缺陷回归：seq「규모.」失败样本 input_i=-25.75（需 +9.75 dB），
    # 旧 ±6 dB 窗口下必然抛 "outside tolerance"；扩窗后首轮渲染即通过响度门。
    normalizer = _FixedAnalysisNormalizer(_report(-25.75, -13.22), (_report(-16.05, -3.47),))

    result = normalizer.normalize(_float32_wav())

    assert normalizer.render_calls == 1
    assert result.manifest["selected_path"] == "short-f32le-24k-gain-limiter-v3"
    fallback = result.manifest["fallback"]
    assert isinstance(fallback, dict)
    assert fallback["initial_gain_db"] == pytest.approx(9.75)
    assert fallback["gain_correction_db"] == 0.0
    assert fallback["correction_trace"] == []


def test_short_clip_beyond_widened_window_still_fails_closed() -> None:
    # 扩窗后的边界：需要 +14 dB（input_i=-30）仍超出 ±12 dB 窗口，fail-closed。
    normalizer = _FixedAnalysisNormalizer(_report(-30.0, -10.0), ())

    with pytest.raises(ValueError, match="outside tolerance"):
        normalizer.normalize(_float32_wav())

    assert normalizer.render_calls == 0


def test_non_short_source_keeps_two_pass_loudnorm() -> None:
    normalizer = _RoutingProbeNormalizer(_report(-17.0, -0.1), _report(-16.2, -1.1))

    result = normalizer.normalize(_float32_wav(96_000))

    assert normalizer.analysis_require_transform_fields == [True]
    assert normalizer.render_calls == 1
    assert result.manifest["selected_path"] == "two-pass-loudnorm"
    assert "fallback" not in result.manifest


def test_non_short_source_fails_closed_when_gate_is_missed() -> None:
    # 2026-10-27 起：过不了门先回落增益+限幅（本样本在 ±12dB 窗口内），
    # 回落仍不达标时照样 fail-closed，不得放行。
    normalizer = _RoutingProbeNormalizer(_report(-17.0, -0.1), _report(-30.0, -10.0))

    with pytest.raises(ValueError, match="outside tolerance"):
        normalizer.normalize(_float32_wav(96_000))

    assert normalizer.render_calls == 4


def test_non_short_source_outside_fallback_window_renders_once_then_fails() -> None:
    # 需要 +24 dB（input_i=-40）超出 ±12 dB 回落窗口：只渲染主路径一次即 fail-closed。
    normalizer = _RoutingProbeNormalizer(_report(-40.0, -0.1), _report(-17.4, -1.0))

    with pytest.raises(ValueError, match="outside tolerance"):
        normalizer.normalize(_float32_wav(96_000))

    assert normalizer.render_calls == 1


def _float32_wav_extensible(
    frame_count: int = 24_000,
    *,
    rate: int = 24_000,
    subformat_tag: int = 3,
    cb_size: int = 22,
) -> bytes:
    """ffmpeg 实际产出的 WAVEFORMATEXTENSIBLE 封装（fmt 40 字节 + SubFormat GUID）。

    2026-10-14 缺陷样本形态：EL provider 的 ffmpeg 转码产物即为此封装（tag=0xFFFE，
    SubFormat GUID 前 2 字节 = 0x0003 IEEE float），曾被 QC 误判 `unsupported_pcm`。
    """
    values = [0.12 * math.sin(2 * math.pi * 220 * index / rate) for index in range(frame_count)]
    samples = b"".join(struct.pack("<f", value) for value in values)
    guid = struct.pack("<H", subformat_tag) + bytes.fromhex("000000001000800000aa00389b71")
    extension = struct.pack("<HI", 32, 0x4)  # validBits=32, channelMask=FRONT_CENTER
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, rate, rate * 4, 4, 32)
    fmt += struct.pack("<H", cb_size) + extension + guid
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_float32_wav_in_wave_format_extensible_passes_qc() -> None:
    qc = inspect_wav(_float32_wav_extensible())

    assert qc.passed is True
    assert qc.codec == "pcm_f32le"
    assert qc.sample_rate_hz == 24_000
    assert qc.channels == 1
    assert qc.sample_width_bytes == 4
    assert qc.frame_count == 24_000


def test_extensible_container_with_non_float_subformat_is_rejected() -> None:
    qc = inspect_wav(_float32_wav_extensible(subformat_tag=1))

    assert qc.passed is False
    assert qc.reason == "unsupported_pcm"


def test_extensible_container_with_truncated_extension_is_rejected() -> None:
    qc = inspect_wav(_float32_wav_extensible(cb_size=0))

    assert qc.passed is False
    assert qc.reason == "unsupported_pcm"


def test_extensible_container_with_bare_fmt_chunk_is_rejected() -> None:
    samples = b"".join(struct.pack("<f", 0.12) for _ in range(480))
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 24_000, 96_000, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    payload = b"RIFF" + struct.pack("<I", len(body)) + body

    qc = inspect_wav(payload)

    assert qc.passed is False
    assert qc.reason == "unsupported_pcm"


# --- 2026-10-27 长音频响度回落（>3s）回归 ------------------------------------


class _LongAudioFfmpegNormalizer(FfmpegAudioNormalizer):
    """分析固定返回低响度长句；渲染按脚本返回门判定结果。"""

    def __init__(self, measured: dict[str, float], render_reports: tuple[dict[str, float], ...]) -> None:
        super().__init__("ffmpeg", ffmpeg_version="test-version")
        self._measured = measured
        self._render_reports = iter(render_reports)
        self.filters: list[str] = []

    def _analyze(
        self,
        path: Path,
        recipe: tuple[str, ...],
        *,
        phase: str,
        require_transform_fields: bool = True,
    ) -> dict[str, float]:
        del path, recipe, require_transform_fields
        assert phase == "analysis"
        return dict(self._measured)

    def _render_and_verify(
        self,
        source_path: Path,
        output_path: Path,
        audio_filter: str,
        output_recipe: tuple[str, ...],
        analysis_recipe: tuple[str, ...],
        source_qc: AudioQcResult,
    ) -> tuple[bytes, AudioQcResult, dict[str, float]]:
        del source_path, output_path, output_recipe, analysis_recipe
        assert source_qc.frame_count is not None
        self.filters.append(audio_filter)
        payload = _pcm16_wav(source_qc.frame_count * 2)
        qc = inspect_wav(payload)
        assert qc.passed
        return payload, qc, next(self._render_reports)


_LONG_AUDIO_FRAMES = 172_800  # 7.2s @24k，与真实失败样本一致


def test_long_audio_low_loudness_falls_back_and_preserves_duration() -> None:
    normalizer = _LongAudioFfmpegNormalizer(
        _report(-23.11, -3.49),
        (_report(-17.43, -1.0), _report(-16.68, -1.04)),
    )

    result = normalizer.normalize(_float32_wav(_LONG_AUDIO_FRAMES))

    assert len(normalizer.filters) == 2
    assert normalizer.filters[0].startswith("loudnorm=")
    assert normalizer.filters[1].startswith("aresample=48000,volume=")
    assert result.manifest["selected_path"] == "long-f32le-24k-gain-limiter-v1"
    fallback = result.manifest["fallback"]
    assert isinstance(fallback, dict)
    assert fallback["recipe_id"] == "long-f32le-24k-gain-limiter-v1"
    assert fallback["gain_db"] == pytest.approx(7.11)
    assert fallback["max_source_frames_24khz"] is None
    assert fallback["source_frame_count"] == _LONG_AUDIO_FRAMES
    assert fallback["expected_output_frames"] == _LONG_AUDIO_FRAMES * 2
    duration = result.manifest["duration_contract"]
    assert isinstance(duration, dict) and duration["cross_product_delta"] == 0
    verified = result.manifest["verified_loudness"]
    assert isinstance(verified, dict) and verified["integrated_lufs"] == pytest.approx(-16.68)


def test_long_audio_primary_success_does_not_take_fallback() -> None:
    normalizer = _LongAudioFfmpegNormalizer(_report(-17.0, -1.0), (_report(-16.2, -1.2),))

    result = normalizer.normalize(_float32_wav(_LONG_AUDIO_FRAMES))

    assert len(normalizer.filters) == 1
    assert result.manifest["selected_path"] == "two-pass-loudnorm"
    assert "fallback" not in result.manifest


def test_long_audio_unreachable_loudness_fails_closed_with_stable_code() -> None:
    normalizer = _LongAudioFfmpegNormalizer(
        _report(-40.0, -3.0),
        (_report(-17.4, -1.0),),
    )

    with pytest.raises(AudioProcessingError) as error:
        normalizer.normalize(_float32_wav(_LONG_AUDIO_FRAMES))

    assert error.value.code == "audio_loudness_unreachable"
    assert "outside tolerance" in str(error.value)


def test_long_fallback_eligibility_requires_above_short_window() -> None:
    long_source = AudioQcResult(
        True, 7_200, "pcm_f32le", None, 24_000, 1, 4, _LONG_AUDIO_FRAMES, 0.1, 1, 1.0
    )

    assert FfmpegAudioNormalizer._long_fallback_eligible(long_source, 7.11)
    assert not FfmpegAudioNormalizer._long_fallback_eligible(long_source, 12.01)
    assert not FfmpegAudioNormalizer._long_fallback_eligible(long_source, math.nan)
    assert not FfmpegAudioNormalizer._long_fallback_eligible(
        replace(long_source, frame_count=72_000), 7.11
    )
    assert not FfmpegAudioNormalizer._long_fallback_eligible(
        replace(long_source, sample_rate_hz=48_000), 7.11
    )


def test_long_audio_best_effort_accepts_outside_gate_after_gain_cap() -> None:
    # 2026-10-28：EL 线路（strict_loudness=False）在 15 dB 最终上限仍不足时接受输出，
    # 只把 loudness_gate 记为 outside；严格模式（上一条用例）必须继续 fail-closed。
    normalizer = _LongAudioFfmpegNormalizer(
        _report(-40.0, -3.0),
        (_report(-17.4, -1.0), _report(-17.2, -1.1)),
    )

    result = normalizer.normalize(_float32_wav(_LONG_AUDIO_FRAMES), strict_loudness=False)

    fallback = result.manifest["fallback"]
    assert len(normalizer.filters) == 2
    assert result.manifest["loudness_policy"] == "best_effort"
    assert result.manifest["loudness_gate"] == "outside"
    assert isinstance(fallback, dict)
    assert fallback["initial_gain_clamped"] is True
    assert float(fallback["gain_db"]) == pytest.approx(15.0)
    assert fallback["source_frame_count"] == _LONG_AUDIO_FRAMES
