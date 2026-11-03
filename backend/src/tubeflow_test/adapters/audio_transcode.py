"""音频转码适配器：把 Provider 原生音频（如 EL MP3）解码重采样为项目权威 raw WAV 合同。

权威合同（与 CosyVoice 产物一致，§1「产物同构」）：`pcm_f32le` / 24 kHz / 单声道 WAV。
这样 EL 句音频经既有 raw 质检 → 响度归一化 → 实测时长 → 装组链路时零改动。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

RAW_SAMPLE_RATE_HZ = 24_000


class AudioTranscodePort(Protocol):
    def to_raw_wav(self, payload: bytes) -> bytes: ...


class FfmpegRawWavTranscoder:
    def __init__(self, executable: str, *, timeout_seconds: float = 120.0) -> None:
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    def to_raw_wav(self, payload: bytes) -> bytes:
        if not payload:
            raise ValueError("provider audio payload is empty")
        with tempfile.TemporaryDirectory(prefix="tubeflow-transcode-") as directory:
            source = Path(directory) / "provider-audio"
            target = Path(directory) / "raw.wav"
            source.write_bytes(payload)
            command = (
                self._executable,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-acodec",
                "pcm_f32le",
                "-ar",
                str(RAW_SAMPLE_RATE_HZ),
                "-ac",
                "1",
                str(target),
            )
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    timeout=self._timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ValueError("FFmpeg audio transcode could not complete") from error
            if result.returncode != 0 or not target.is_file():
                raise ValueError("FFmpeg audio transcode could not complete")
            return target.read_bytes()
