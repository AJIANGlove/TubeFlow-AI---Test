from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tubeflow_test.video_generation.adapters import FfprobeVideoProbe
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.orchestration import _verify_media

FFMPEG_DIR = Path(os.environ.get("TUBEFLOW_TEST_FFMPEG_DIR", ""))
FFMPEG = FFMPEG_DIR / "ffmpeg.exe"
FFPROBE = FFMPEG_DIR / "ffprobe.exe"


def _sample(tmp_path: Path, *, audio_duration: float, audio_offset: float = 0.0) -> bytes:
    output = tmp_path / f"sample-{audio_duration}-{audio_offset}.mp4"
    command = [
        str(FFMPEG),
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=1056x608:r=24:d=1",
    ]
    if audio_offset:
        command.extend(["-itsoffset", str(audio_offset)])
    command.extend(
        [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=48000:cl=stereo:d={audio_duration}",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-t",
            "1",
            "-y",
            str(output),
        ]
    )
    subprocess.run(command, check=True, capture_output=True)
    return output.read_bytes()


@pytest.mark.skipif(not (FFMPEG.is_file() and FFPROBE.is_file()), reason="local ffmpeg unavailable")
def test_real_ffprobe_requires_native_audio_to_cover_complete_video(tmp_path: Path) -> None:
    probe = FfprobeVideoProbe(str(FFPROBE))
    complete = probe.inspect(_sample(tmp_path, audio_duration=1.0))
    _verify_media(complete, 900, native_size=(1056, 608))
    assert complete.audio_codec == "aac"
    assert complete.audio_start_ms == 0

    for payload in (
        _sample(tmp_path, audio_duration=0.001),
        _sample(tmp_path, audio_duration=1.0, audio_offset=0.2),
        _sample(tmp_path, audio_duration=0.5),
    ):
        with pytest.raises(VideoRuleViolation, match="complete video"):
            _verify_media(probe.inspect(payload), 900, native_size=(1056, 608))
