from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContentQcThresholds:
    algorithm: str = "content-qc-v1"
    sample_interval_seconds: float = 1.0
    grid_columns: int = 8
    grid_rows: int = 6
    black_max_luma: int = 20
    black_max_stddev: float = 2.5
    min_region_area_fraction: float = 0.25
    min_region_fill_ratio: float = 0.9
    min_sustained_frames: int = 2
    min_bbox_overlap: float = 0.5
    continuity_tolerance_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not self.algorithm:
            raise ValueError("content QC algorithm identifier must be non-empty")
        if not math.isfinite(self.sample_interval_seconds) or self.sample_interval_seconds <= 0:
            raise ValueError("sample interval must be a positive finite seconds value")
        if self.grid_columns < 1 or self.grid_rows < 1:
            raise ValueError("tile grid dimensions must be positive")
        if not 0 <= self.black_max_luma <= 255:
            raise ValueError("black tile max luma must be within 0..255")
        if not math.isfinite(self.black_max_stddev) or self.black_max_stddev < 0:
            raise ValueError("black tile stddev ceiling must be a non-negative finite value")
        if not 0 < self.min_region_area_fraction <= 1:
            raise ValueError("region area fraction must be within (0, 1]")
        if not 0 < self.min_region_fill_ratio <= 1:
            raise ValueError("region fill ratio must be within (0, 1]")
        if self.min_sustained_frames < 1:
            raise ValueError("sustained frame minimum must be at least 1")
        if not 0 < self.min_bbox_overlap <= 1:
            raise ValueError("bbox overlap ratio must be within (0, 1]")
        if not 0 <= self.continuity_tolerance_seconds < self.sample_interval_seconds:
            raise ValueError("continuity tolerance must be within [0, sample interval)")


DEFAULT_THRESHOLDS = ContentQcThresholds()


@dataclass(frozen=True, slots=True)
class TileStat:
    mean: float
    max_luma: int
    stddev: float


@dataclass(frozen=True, slots=True)
class FrameStats:
    second: float
    tiles: tuple[tuple[TileStat, ...], ...]


@dataclass(frozen=True, slots=True)
class BlackRegionFinding:
    start_second: float
    end_second: float
    bbox_columns: tuple[int, int]
    bbox_rows: tuple[int, int]
    area_fraction: float


@dataclass(frozen=True, slots=True)
class _RegionCandidate:
    columns: tuple[int, int]
    rows: tuple[int, int]


@dataclass(slots=True)
class _Segment:
    start_second: float
    end_second: float
    columns: tuple[int, int]
    rows: tuple[int, int]
    last_columns: tuple[int, int]
    last_rows: tuple[int, int]
    frame_count: int


def find_sustained_black_regions(
    frames: Sequence[FrameStats],
    thresholds: ContentQcThresholds = DEFAULT_THRESHOLDS,
) -> tuple[BlackRegionFinding, ...]:
    frame_list = list(frames)
    if not frame_list:
        raise ValueError("frames must be non-empty")
    for frame in frame_list:
        _validate_frame(frame, thresholds)
    for previous, current in zip(frame_list, frame_list[1:], strict=False):
        if current.second <= previous.second:
            raise ValueError("frame seconds must be strictly increasing")

    findings: list[BlackRegionFinding] = []
    open_segments: list[_Segment] = []
    for index, frame in enumerate(frame_list):
        if index > 0 and not _is_continuous(
            frame_list[index - 1].second, frame.second, thresholds
        ):
            findings.extend(_close_segments(open_segments, thresholds))
            open_segments = []
        extended: set[int] = set()
        next_open: list[_Segment] = []
        for candidate in _region_candidates(frame, thresholds):
            best: _Segment | None = None
            best_ratio = 0.0
            for segment in open_segments:
                if id(segment) in extended:
                    continue
                ratio = _overlap_ratio(
                    segment.last_columns, segment.last_rows, candidate
                )
                if ratio >= thresholds.min_bbox_overlap and ratio > best_ratio:
                    best = segment
                    best_ratio = ratio
            if best is None:
                next_open.append(
                    _Segment(
                        start_second=frame.second,
                        end_second=frame.second,
                        columns=candidate.columns,
                        rows=candidate.rows,
                        last_columns=candidate.columns,
                        last_rows=candidate.rows,
                        frame_count=1,
                    )
                )
            else:
                extended.add(id(best))
                best.end_second = frame.second
                best.columns = (
                    min(best.columns[0], candidate.columns[0]),
                    max(best.columns[1], candidate.columns[1]),
                )
                best.rows = (
                    min(best.rows[0], candidate.rows[0]),
                    max(best.rows[1], candidate.rows[1]),
                )
                best.last_columns = candidate.columns
                best.last_rows = candidate.rows
                best.frame_count += 1
                next_open.append(best)
        findings.extend(
            _close_segments(
                [segment for segment in open_segments if id(segment) not in extended],
                thresholds,
            )
        )
        open_segments = next_open
    findings.extend(_close_segments(open_segments, thresholds))
    return tuple(findings)


def _validate_frame(frame: FrameStats, thresholds: ContentQcThresholds) -> None:
    if not math.isfinite(frame.second) or frame.second < 0:
        raise ValueError("frame seconds must be non-negative finite values")
    if len(frame.tiles) != thresholds.grid_rows:
        raise ValueError("frame tile grid shape does not match thresholds")
    if any(len(row) != thresholds.grid_columns for row in frame.tiles):
        raise ValueError("frame tile grid shape does not match thresholds")


def _is_continuous(previous: float, current: float, thresholds: ContentQcThresholds) -> bool:
    return (
        abs(current - previous - thresholds.sample_interval_seconds)
        <= thresholds.continuity_tolerance_seconds
    )


def _is_black(tile: TileStat, thresholds: ContentQcThresholds) -> bool:
    return (
        tile.max_luma <= thresholds.black_max_luma
        and tile.stddev <= thresholds.black_max_stddev
    )


def _region_candidates(
    frame: FrameStats, thresholds: ContentQcThresholds
) -> list[_RegionCandidate]:
    rows = thresholds.grid_rows
    columns = thresholds.grid_columns
    black = [[_is_black(tile, thresholds) for tile in row] for row in frame.tiles]
    visited = [[False] * columns for _ in range(rows)]
    candidates: list[_RegionCandidate] = []
    for start_row in range(rows):
        for start_column in range(columns):
            if visited[start_row][start_column] or not black[start_row][start_column]:
                continue
            members = _flood_fill(black, visited, start_row, start_column)
            min_row = min(member[0] for member in members)
            max_row = max(member[0] for member in members)
            min_column = min(member[1] for member in members)
            max_column = max(member[1] for member in members)
            bbox_area = (max_column - min_column + 1) * (max_row - min_row + 1)
            if bbox_area / (columns * rows) < thresholds.min_region_area_fraction:
                continue
            if len(members) / bbox_area < thresholds.min_region_fill_ratio:
                continue
            candidates.append(
                _RegionCandidate(
                    columns=(min_column, max_column), rows=(min_row, max_row)
                )
            )
    return candidates


def _flood_fill(
    black: list[list[bool]],
    visited: list[list[bool]],
    start_row: int,
    start_column: int,
) -> list[tuple[int, int]]:
    row_count = len(black)
    column_count = len(black[0])
    members: list[tuple[int, int]] = []
    queue = deque([(start_row, start_column)])
    visited[start_row][start_column] = True
    while queue:
        row, column = queue.popleft()
        members.append((row, column))
        for delta_row, delta_column in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            next_row = row + delta_row
            next_column = column + delta_column
            if (
                0 <= next_row < row_count
                and 0 <= next_column < column_count
                and black[next_row][next_column]
                and not visited[next_row][next_column]
            ):
                visited[next_row][next_column] = True
                queue.append((next_row, next_column))
    return members


def _overlap_ratio(
    columns: tuple[int, int], rows: tuple[int, int], candidate: _RegionCandidate
) -> float:
    width = min(columns[1], candidate.columns[1]) - max(columns[0], candidate.columns[0]) + 1
    height = min(rows[1], candidate.rows[1]) - max(rows[0], candidate.rows[0]) + 1
    if width <= 0 or height <= 0:
        return 0.0
    intersection = width * height
    base_area = (columns[1] - columns[0] + 1) * (rows[1] - rows[0] + 1)
    candidate_area = (candidate.columns[1] - candidate.columns[0] + 1) * (
        candidate.rows[1] - candidate.rows[0] + 1
    )
    return intersection / min(base_area, candidate_area)


def _close_segments(
    segments: Sequence[_Segment], thresholds: ContentQcThresholds
) -> list[BlackRegionFinding]:
    findings: list[BlackRegionFinding] = []
    for segment in segments:
        if segment.frame_count < thresholds.min_sustained_frames:
            continue
        area = (segment.columns[1] - segment.columns[0] + 1) * (
            segment.rows[1] - segment.rows[0] + 1
        )
        findings.append(
            BlackRegionFinding(
                start_second=segment.start_second,
                end_second=segment.end_second,
                bbox_columns=segment.columns,
                bbox_rows=segment.rows,
                area_fraction=area / (thresholds.grid_columns * thresholds.grid_rows),
            )
        )
    return findings
