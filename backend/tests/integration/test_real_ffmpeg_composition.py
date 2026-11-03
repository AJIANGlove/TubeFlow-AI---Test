from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from tubeflow_test.adapters.ffmpeg import FFmpegComposer, SafeSubprocessRunner
from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.composition import (
    CompositionRequest,
    GroupInput,
    NarrationSegmentInput,
    SentenceTiming,
)


class FixtureResolver:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve_read(self, asset_id: str) -> Path:
        return self.root / asset_id

    def unique_candidate(self, project_id: str) -> Path:
        return self.root / "candidate.mp4"


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg integration fixture requires local ffmpeg and ffprobe",
)
def test_real_generated_fixture_meets_frame_and_mix_contract(tmp_path: Path) -> None:
    for index in range(2):
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=1280x720:r=24:d=1.2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000:duration=1.2",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(tmp_path / f"video{index}.mp4"),
            ],
            check=True,
            capture_output=True,
        )
        for sentence_index, duration in enumerate((0.5, 0.501)):
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=880:sample_rate=44100:duration={duration}",
                    "-c:a",
                    "pcm_s16le",
                    str(tmp_path / f"tts{index}-{sentence_index}.wav"),
                ],
                check=True,
                capture_output=True,
            )
    text = "fixture narration"
    request = CompositionRequest(
        "fixture",
        "a" * 64,
        tuple(
            GroupInput(
                f"g{i}",
                i,
                f"video{i}.mp4",
                (
                    NarrationSegmentInput(f"s{i}-0", f"tts{i}-0.wav", 500, 24000, 48000),
                    NarrationSegmentInput(f"s{i}-1", f"tts{i}-1.wav", 501, 24048, 48000),
                ),
                1001,
            )
            for i in range(2)
        ),
        (SentenceTiming("s", 0, text, hashlib.sha256(text.encode()).hexdigest(), 0, 2002),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )
    _, manifest = FFmpegComposer(SafeSubprocessRunner(30), FixtureResolver(tmp_path)).compose(
        request
    )
    assert abs(manifest.output_probe.frame_count - 48) <= 1
