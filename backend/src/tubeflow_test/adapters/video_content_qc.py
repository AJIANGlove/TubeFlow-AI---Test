from __future__ import annotations

import json
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageStat

from tubeflow_test.video_generation.content_qc import (
    DEFAULT_THRESHOLDS,
    ContentQcThresholds,
    FrameStats,
    TileStat,
)


class FfmpegBlackFrameSampler:
    def __init__(
        self,
        ffmpeg_executable: str = "ffmpeg",
        ffprobe_executable: str = "ffprobe",
        timeout_seconds: float = 60.0,
        scale_width: int | None = None,
    ) -> None:
        if not ffmpeg_executable or not ffprobe_executable:
            raise ValueError("ffmpeg/ffprobe executables must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("subprocess timeout must be positive")
        if scale_width is not None and scale_width <= 0:
            raise ValueError("scale width must be positive when set")
        self._ffmpeg = ffmpeg_executable
        self._ffprobe = ffprobe_executable
        self._timeout = timeout_seconds
        self._scale_width = scale_width

    def sample(
        self, content: bytes, *, thresholds: ContentQcThresholds = DEFAULT_THRESHOLDS
    ) -> tuple[FrameStats, ...]:
        if not content:
            raise ValueError("empty video content")
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as stream:
                stream.write(content)
                path = Path(stream.name)
            duration = self._probe_duration(path)
            seconds: list[float] = []
            index = 0
            while index * thresholds.sample_interval_seconds < duration:
                seconds.append(index * thresholds.sample_interval_seconds)
                index += 1
            return tuple(self._sample_frame(path, second, thresholds) for second in seconds)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def _probe_duration(self, path: Path) -> float:
        result = subprocess.run(
            [
                self._ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        payload = json.loads(result.stdout)
        duration_raw = payload.get("format", {}).get("duration")
        if duration_raw is None:
            raise ValueError("ffprobe format duration evidence is required")
        duration = float(duration_raw)
        if duration <= 0:
            raise ValueError("video duration must be positive")
        return duration

    def _sample_frame(
        self, path: Path, second: float, thresholds: ContentQcThresholds
    ) -> FrameStats:
        command = [
            self._ffmpeg,
            "-v",
            "error",
            "-ss",
            f"{second:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
        ]
        if self._scale_width is not None:
            command.extend(["-vf", f"scale={self._scale_width}:-2"])
        command.extend(["-f", "image2pipe", "-c:v", "png", "-"])
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=self._timeout,
        )
        if not result.stdout:
            raise ValueError(f"ffmpeg produced no frame at {second:.3f}s")
        return _frame_stats_from_png(result.stdout, second, thresholds)


def _frame_stats_from_png(
    png: bytes, second: float, thresholds: ContentQcThresholds
) -> FrameStats:
    with Image.open(BytesIO(png)) as image:
        gray = image.convert("L")
        width, height = gray.size
        if width < thresholds.grid_columns or height < thresholds.grid_rows:
            raise ValueError("sampled frame is smaller than the analysis grid")
        rows: list[tuple[TileStat, ...]] = []
        for grid_row in range(thresholds.grid_rows):
            top = grid_row * height // thresholds.grid_rows
            bottom = (grid_row + 1) * height // thresholds.grid_rows
            tiles: list[TileStat] = []
            for grid_column in range(thresholds.grid_columns):
                left = grid_column * width // thresholds.grid_columns
                right = (grid_column + 1) * width // thresholds.grid_columns
                stat = ImageStat.Stat(gray.crop((left, top, right, bottom)))
                tiles.append(
                    TileStat(
                        mean=stat.mean[0],
                        max_luma=int(stat.extrema[0][1]),
                        stddev=stat.stddev[0],
                    )
                )
            rows.append(tuple(tiles))
    return FrameStats(second=second, tiles=tuple(rows))


def sample_black_frame_stats(
    content: bytes, *, thresholds: ContentQcThresholds = DEFAULT_THRESHOLDS
) -> tuple[FrameStats, ...]:
    return FfmpegBlackFrameSampler().sample(content, thresholds=thresholds)
