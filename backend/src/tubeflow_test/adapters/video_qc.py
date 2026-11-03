from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VideoProbe:
    width: int
    height: int
    duration_ms: int
    fps: float
    has_native_audio: bool


def inspect_h3_mp4(path: Path, target_duration_ms: int) -> VideoProbe:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or not audios:
        raise ValueError("H3 output must contain one video track and native audio")
    video = videos[0]
    numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/", 1)
    fps = int(numerator) / int(denominator)
    duration_ms = round(float(payload["format"]["duration"]) * 1000)
    if abs(fps - 24.0) > 0.01:
        raise ValueError("H3 output must be 24fps")
    if duration_ms < target_duration_ms:
        raise ValueError("H3 output is shorter than measured narration; regenerate this group")
    width, height = int(video["width"]), int(video["height"])
    if width <= 0 or height <= 0:
        raise ValueError("H3 output dimensions are invalid")
    return VideoProbe(width, height, duration_ms, fps, True)
