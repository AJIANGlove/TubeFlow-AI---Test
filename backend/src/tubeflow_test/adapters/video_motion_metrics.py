from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from tubeflow_test.video_generation.motion_metrics import MotionSignals

_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
# Same sizes as the survey (held_frames.py) and the HF script (motion.py).
_DIFF_SIZE = (264, 152)
_SHIFT_SIZE = (528, 304)


class FfmpegOpenCvMotionExtractor:
    """Per-frame grey differences (ffmpeg tblend) and global shifts (OpenCV phase correlation)."""

    def __init__(self, ffmpeg_executable: str = "ffmpeg", timeout_seconds: float = 60.0) -> None:
        if not ffmpeg_executable:
            raise ValueError("ffmpeg executable must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("subprocess timeout must be positive")
        self._ffmpeg = ffmpeg_executable
        self._timeout = timeout_seconds

    def extract(self, content: bytes) -> MotionSignals:
        if not content:
            raise ValueError("empty video content")
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as stream:
                stream.write(content)
                path = Path(stream.name)
            return MotionSignals(self._differences(path), self._shifts(path))
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def _differences(self, path: Path) -> tuple[float, ...]:
        width, height = _DIFF_SIZE
        result = subprocess.run(
            [
                self._ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"scale={width}:{height},format=gray,tblend=all_mode=difference,signalstats,"
                "metadata=print:key=lavfi.signalstats.YAVG:file=-",
                "-f",
                "null",
                "-",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        return tuple(float(value) for value in _YAVG.findall(result.stdout))

    def _shifts(self, path: Path) -> tuple[tuple[float, float], ...]:
        width, height = _SHIFT_SIZE
        raw = subprocess.run(
            [
                self._ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"scale={width}:{height},format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=self._timeout,
        ).stdout
        if not raw or len(raw) % (width * height):
            raise ValueError("ffmpeg returned a partial grey frame stream")
        frames = np.frombuffer(raw, np.uint8).reshape(-1, height, width)
        window = cv2.createHanningWindow((width, height), cv2.CV_32F)
        shifts: list[tuple[float, float]] = []
        previous = frames[0].astype(np.float32)
        for frame in frames[1:]:
            current = frame.astype(np.float32)
            (dx, dy), _response = cv2.phaseCorrelate(previous, current, window)
            shifts.append((float(dx), float(dy)))
            previous = current
        return tuple(shifts)
