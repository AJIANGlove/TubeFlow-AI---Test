from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import subprocess

import pytest

from tubeflow_test.adapters.audio_qc import FfmpegAudioNormalizer, inspect_wav


def _float32_wav(values: list[float], *, rate: int = 24_000) -> bytes:
    samples = b"".join(struct.pack("<f", value) for value in values)
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _ffmpeg_normalizer() -> FfmpegAudioNormalizer:
    executable = os.environ.get("TUBEFLOW_TEST_FFMPEG") or shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg executable is not available")
    version = subprocess.run(
        (executable, "-version"), capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    return FfmpegAudioNormalizer(executable, ffmpeg_version=version)


@pytest.mark.integration
def test_short_high_peak_float32_audio_normalizes_deterministically() -> None:
    rate = 24_000
    frame_count = 24_960
    values = [
        (0.99 if (index // 2_400) % 2 == 0 else -0.99)
        if index % 2_400 == 0
        else 0.12 * math.sin(2 * math.pi * 220 * index / rate)
        + 0.04 * math.sin(2 * math.pi * 440 * index / rate)
        for index in range(frame_count)
    ]
    raw = _float32_wav(values, rate=rate)
    assert inspect_wav(raw).passed

    normalizer = _ffmpeg_normalizer()
    first = normalizer.normalize(raw)
    second = normalizer.normalize(raw)
    first_qc = inspect_wav(first.payload)
    verified = first.manifest["verified_loudness"]

    assert hashlib.sha256(first.payload).digest() == hashlib.sha256(second.payload).digest()
    assert first_qc.passed
    assert (first_qc.sample_rate_hz, first_qc.channels, first_qc.sample_width_bytes) == (
        48_000,
        1,
        2,
    )
    assert first_qc.frame_count == frame_count * 2
    assert isinstance(verified, dict)
    assert abs(float(verified["integrated_lufs"]) - -16.0) <= 1.0
    assert float(verified["true_peak_dbtp"]) <= -0.8


@pytest.mark.integration
def test_short_silent_float32_audio_still_fails_closed() -> None:
    raw = _float32_wav([0.0] * 7_200)
    qc = inspect_wav(raw)
    assert not qc.passed and qc.reason == "near_silent_audio"

    with pytest.raises(ValueError):
        _ffmpeg_normalizer().normalize(raw)


@pytest.mark.integration
def test_quiet_short_float32_audio_normalizes_via_widened_gain_window() -> None:
    # 2026-10-14 生产缺陷回归：低幅度短句（约 -26 LUFS）在旧 ±6 dB 窗口下必然失败；
    # 扩窗后应正常归一化，且 manifest 记录的首轮增益明显大于 6 dB，证明走的是
    # 加宽后的短音频回退路径。
    rate = 24_000
    frame_count = 11_520  # 0.48s，对应生产失败样本规格
    values = [0.073 * math.sin(2 * math.pi * 220 * index / rate) for index in range(frame_count)]
    raw = _float32_wav(values, rate=rate)
    assert inspect_wav(raw).passed

    result = _ffmpeg_normalizer().normalize(raw)
    fallback = result.manifest["fallback"]
    verified = result.manifest["verified_loudness"]

    assert isinstance(fallback, dict)
    assert fallback["recipe_id"] == "short-f32le-24k-gain-limiter-v3"
    assert float(fallback["initial_gain_db"]) > 6.0
    assert abs(float(fallback["initial_gain_db"])) <= 12.0
    assert isinstance(verified, dict)
    assert abs(float(verified["integrated_lufs"]) - -16.0) <= 1.0
    assert float(verified["true_peak_dbtp"]) <= -0.8
    assert inspect_wav(result.payload).passed


@pytest.mark.integration
def test_elevenlabs_best_effort_accepts_gain_above_initial_window() -> None:
    # 2026-10-28：ElevenLabs 线路（strict_loudness=False）不再因"需要超过 12 dB 增益"
    # 在渲染前判 audio_loudness_unreachable；本样本约 -29.7 LUFS（需约 13.7 dB）。
    rate = 24_000
    frame_count = 11_520  # 0.48s
    values = [0.05 * math.sin(2 * math.pi * 220 * index / rate) for index in range(frame_count)]
    raw = _float32_wav(values, rate=rate)
    assert inspect_wav(raw).passed

    normalizer = _ffmpeg_normalizer()
    with pytest.raises(ValueError):
        normalizer.normalize(raw)  # 严格模式（CosyVoice）仍必须拒绝

    result = normalizer.normalize(raw, strict_loudness=False)
    fallback = result.manifest["fallback"]
    verified = result.manifest["verified_loudness"]

    assert isinstance(fallback, dict)
    assert fallback["initial_gain_clamped"] is True
    assert float(fallback["initial_gain_db"]) > 12.0
    assert float(fallback["gain_db"]) <= 15.0
    assert result.manifest["loudness_policy"] == "best_effort"
    assert result.manifest["loudness_gate"] == "inside"
    assert isinstance(verified, dict)
    assert abs(float(verified["integrated_lufs"]) - -16.0) <= 1.0
    assert float(verified["true_peak_dbtp"]) <= -0.8
    output_qc = inspect_wav(result.payload)
    assert output_qc.passed
    assert output_qc.frame_count == frame_count * 2


@pytest.mark.integration
def test_elevenlabs_best_effort_keeps_audio_when_final_gain_cap_cannot_reach_target() -> None:
    # 极端轻的样本（约 -42 LUFS，需 26 dB）在 15 dB 最终上限下仍到不了 -16 LUFS：
    # EL 线路接受该输出并标记 loudness_gate=outside，时长契约不被破坏。
    rate = 24_000
    frame_count = 11_520
    values = [0.012 * math.sin(2 * math.pi * 220 * index / rate) for index in range(frame_count)]
    raw = _float32_wav(values, rate=rate)
    assert inspect_wav(raw).passed

    result = _ffmpeg_normalizer().normalize(raw, strict_loudness=False)
    fallback = result.manifest["fallback"]
    verified = result.manifest["verified_loudness"]
    duration = result.manifest["duration_contract"]

    assert isinstance(fallback, dict)
    assert fallback["initial_gain_clamped"] is True
    assert float(fallback["gain_db"]) == pytest.approx(15.0)
    assert result.manifest["loudness_gate"] == "outside"
    assert isinstance(verified, dict)
    assert float(verified["integrated_lufs"]) < -17.0
    assert isinstance(duration, dict) and duration["cross_product_delta"] == 0
    assert inspect_wav(result.payload).passed
