from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tubeflow_test.adapters.video_content_qc import (
    FfmpegBlackFrameSampler,
    sample_black_frame_stats,
)
from tubeflow_test.video_generation.content_qc import (
    ContentQcThresholds,
    FrameStats,
    TileStat,
    find_sustained_black_regions,
)

GRID_COLUMNS = 8
GRID_ROWS = 6

BLACK_TILE = TileStat(mean=4.0, max_luma=12, stddev=0.8)
TEXTURED_TILE = TileStat(mean=48.0, max_luma=181, stddev=12.0)


def _frame(second: float, black_cells: frozenset[tuple[int, int]] | None = None) -> FrameStats:
    black = black_cells if black_cells is not None else frozenset()
    return FrameStats(
        second=second,
        tiles=tuple(
            tuple(
                BLACK_TILE if (row, column) in black else TEXTURED_TILE
                for column in range(GRID_COLUMNS)
            )
            for row in range(GRID_ROWS)
        ),
    )


def _uniform_frame(second: float, tile: TileStat) -> FrameStats:
    return FrameStats(
        second=second,
        tiles=tuple(
            tuple(tile for _ in range(GRID_COLUMNS)) for _ in range(GRID_ROWS)
        ),
    )


def _box_cells(columns: range, rows: range) -> frozenset[tuple[int, int]]:
    return frozenset((row, column) for row in rows for column in columns)


def _right_half_cells() -> frozenset[tuple[int, int]]:
    return _box_cells(range(4, 8), range(0, 6))


def test_sustained_right_half_black_produces_single_finding() -> None:
    frames = tuple(_frame(float(second), _right_half_cells()) for second in range(5))
    findings = find_sustained_black_regions(frames)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.bbox_columns == (4, 7)
    assert finding.bbox_rows == (0, 5)
    assert finding.start_second == 0.0
    assert finding.end_second == 4.0
    assert finding.area_fraction == pytest.approx(0.5)


def test_single_frame_black_region_is_not_sustained() -> None:
    assert find_sustained_black_regions((_frame(0.0, _right_half_cells()),)) == ()


def test_bright_dark_tiles_are_not_flagged() -> None:
    frames = tuple(
        _uniform_frame(float(second), TileStat(mean=40.0, max_luma=60, stddev=1.0))
        for second in range(3)
    )
    assert find_sustained_black_regions(frames) == ()


def test_noisy_dark_tiles_are_not_flagged() -> None:
    frames = tuple(
        _uniform_frame(float(second), TileStat(mean=18.0, max_luma=18, stddev=12.0))
        for second in range(3)
    )
    assert find_sustained_black_regions(frames) == ()


def test_two_tile_region_is_below_area_threshold() -> None:
    frames = tuple(
        _frame(float(second), frozenset({(0, 0), (0, 1)})) for second in range(3)
    )
    assert find_sustained_black_regions(frames) == ()


def test_region_exactly_at_area_threshold_is_flagged() -> None:
    frames = tuple(
        _frame(float(second), _box_cells(range(0, 2), range(0, 6))) for second in range(3)
    )
    findings = find_sustained_black_regions(frames)
    assert len(findings) == 1
    assert findings[0].bbox_columns == (0, 1)
    assert findings[0].bbox_rows == (0, 5)
    assert findings[0].area_fraction == pytest.approx(0.25)


def test_region_just_below_area_threshold_is_not_flagged() -> None:
    frames = tuple(
        _frame(float(second), _box_cells(range(0, 2), range(0, 5))) for second in range(3)
    )
    assert find_sustained_black_regions(frames) == ()


def test_scattered_black_tiles_below_fill_ratio_are_not_flagged() -> None:
    cells = _box_cells(range(4, 8), range(0, 6)) - {(1, 5), (1, 6), (4, 5), (4, 6)}
    frames = tuple(_frame(float(second), cells) for second in range(3))
    assert find_sustained_black_regions(frames) == ()


def test_interrupted_black_region_splits_into_two_findings() -> None:
    frames = (
        _frame(0.0, _right_half_cells()),
        _frame(1.0, _right_half_cells()),
        _frame(2.0),
        _frame(3.0, _right_half_cells()),
        _frame(4.0, _right_half_cells()),
    )
    findings = find_sustained_black_regions(frames)
    assert len(findings) == 2
    assert (findings[0].start_second, findings[0].end_second) == (0.0, 1.0)
    assert (findings[1].start_second, findings[1].end_second) == (3.0, 4.0)


def test_single_frame_segments_separated_by_normal_frame_produce_no_finding() -> None:
    frames = (
        _frame(0.0, _right_half_cells()),
        _frame(1.0),
        _frame(2.0, _right_half_cells()),
    )
    assert find_sustained_black_regions(frames) == ()


def test_time_gap_breaks_continuity() -> None:
    frames = (
        _frame(0.0, _right_half_cells()),
        _frame(5.0, _right_half_cells()),
    )
    assert find_sustained_black_regions(frames) == ()


def test_shifting_bbox_merges_when_overlap_sufficient() -> None:
    frames = (
        _frame(0.0, _box_cells(range(4, 8), range(0, 6))),
        _frame(1.0, _box_cells(range(3, 7), range(0, 6))),
    )
    findings = find_sustained_black_regions(frames)
    assert len(findings) == 1
    assert findings[0].bbox_columns == (3, 7)
    assert findings[0].area_fraction == pytest.approx(30 / 48)


def test_disjoint_bbox_does_not_merge() -> None:
    frames = (
        _frame(0.0, _box_cells(range(0, 4), range(0, 6))),
        _frame(1.0, _box_cells(range(4, 8), range(0, 6))),
    )
    assert find_sustained_black_regions(frames) == ()


def test_custom_thresholds_are_respected() -> None:
    thresholds = ContentQcThresholds(min_sustained_frames=3)
    two_frames = tuple(_frame(float(second), _right_half_cells()) for second in range(2))
    assert find_sustained_black_regions(two_frames, thresholds) == ()
    three_frames = tuple(_frame(float(second), _right_half_cells()) for second in range(3))
    assert len(find_sustained_black_regions(three_frames, thresholds)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("algorithm", ""),
        ("sample_interval_seconds", 0.0),
        ("sample_interval_seconds", -1.0),
        ("sample_interval_seconds", float("nan")),
        ("grid_columns", 0),
        ("grid_rows", 0),
        ("black_max_luma", -1),
        ("black_max_luma", 256),
        ("black_max_stddev", -0.5),
        ("min_region_area_fraction", 0.0),
        ("min_region_area_fraction", 1.1),
        ("min_region_fill_ratio", 0.0),
        ("min_region_fill_ratio", 1.1),
        ("min_sustained_frames", 0),
        ("min_bbox_overlap", 0.0),
        ("min_bbox_overlap", 1.1),
        ("continuity_tolerance_seconds", -0.1),
        ("continuity_tolerance_seconds", 1.0),
    ],
)
def test_invalid_thresholds_raise(field: str, value: Any) -> None:
    overrides: dict[str, Any] = {field: value}
    with pytest.raises(ValueError):
        ContentQcThresholds(**overrides)


def test_empty_frames_raise() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        find_sustained_black_regions(())


def test_non_increasing_seconds_raise() -> None:
    with pytest.raises(ValueError, match="increasing"):
        find_sustained_black_regions((_frame(1.0), _frame(1.0)))
    with pytest.raises(ValueError, match="increasing"):
        find_sustained_black_regions((_frame(2.0), _frame(1.0)))


def test_negative_second_raises() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        find_sustained_black_regions((_frame(-1.0),))


def test_grid_shape_mismatch_raises() -> None:
    narrow = FrameStats(
        second=0.0,
        tiles=tuple(tuple(BLACK_TILE for _ in range(7)) for _ in range(6)),
    )
    with pytest.raises(ValueError, match="grid"):
        find_sustained_black_regions((narrow,))
    short = FrameStats(
        second=0.0,
        tiles=tuple(tuple(BLACK_TILE for _ in range(8)) for _ in range(5)),
    )
    with pytest.raises(ValueError, match="grid"):
        find_sustained_black_regions((short,))


_FFMPEG_DIR = os.environ.get("TUBEFLOW_TEST_FFMPEG_DIR", "")


def _resolve_executable(name: str) -> str | None:
    if _FFMPEG_DIR:
        candidate = Path(_FFMPEG_DIR) / f"{name}.exe"
        return str(candidate) if candidate.is_file() else None
    return shutil.which(name)


FFMPEG = _resolve_executable("ffmpeg")
FFPROBE = _resolve_executable("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="local ffmpeg unavailable"
)
requires_path_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


def _render_black_sample(path: Path) -> bytes:
    assert FFMPEG is not None
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=24:d=3",
            "-c:v",
            "ffv1",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def _render_split_sample(path: Path) -> bytes:
    assert FFMPEG is not None
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x180:r=24:d=3",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x180:r=24:d=3",
            "-filter_complex",
            "[0:v][1:v]hstack=inputs=2[v]",
            "-map",
            "[v]",
            "-c:v",
            "ffv1",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def _sampler() -> FfmpegBlackFrameSampler:
    assert FFMPEG is not None and FFPROBE is not None
    return FfmpegBlackFrameSampler(FFMPEG, FFPROBE)


def test_sampler_constructor_validates_arguments() -> None:
    with pytest.raises(ValueError):
        FfmpegBlackFrameSampler("", "ffprobe")
    with pytest.raises(ValueError):
        FfmpegBlackFrameSampler("ffmpeg", "")
    with pytest.raises(ValueError):
        FfmpegBlackFrameSampler(timeout_seconds=0)
    with pytest.raises(ValueError):
        FfmpegBlackFrameSampler(scale_width=0)


def test_sampler_rejects_empty_content() -> None:
    with pytest.raises(ValueError, match="empty"):
        FfmpegBlackFrameSampler().sample(b"")


@requires_ffmpeg
def test_sampler_flags_fully_black_video(tmp_path: Path) -> None:
    frames = _sampler().sample(_render_black_sample(tmp_path / "black.mkv"))
    assert len(frames) >= 3
    assert frames[0].second == 0.0
    findings = find_sustained_black_regions(frames)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.bbox_columns == (0, 7)
    assert finding.bbox_rows == (0, 5)
    assert finding.start_second == 0.0
    assert finding.end_second >= 2.0
    assert finding.area_fraction == pytest.approx(1.0)


@requires_ffmpeg
def test_sampler_flags_left_half_black_video(tmp_path: Path) -> None:
    frames = _sampler().sample(_render_split_sample(tmp_path / "split.mkv"))
    assert len(frames) >= 3
    findings = find_sustained_black_regions(frames)
    assert len(findings) == 1
    assert findings[0].bbox_columns == (0, 3)
    assert findings[0].bbox_rows == (0, 5)


@requires_path_ffmpeg
def test_convenience_function_samples_frames(tmp_path: Path) -> None:
    frames = sample_black_frame_stats(_render_black_sample(tmp_path / "convenience.mkv"))
    assert len(frames) >= 3
    findings = find_sustained_black_regions(frames)
    assert len(findings) == 1
    assert findings[0].bbox_columns == (0, 7)
