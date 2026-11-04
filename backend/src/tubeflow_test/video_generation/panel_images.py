from __future__ import annotations

import hashlib
import math
import statistics
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, UnidentifiedImageError

from tubeflow_test.domain.storyboards import GRID_LAYOUT_V1, GridLayout, grid_layout_for
from tubeflow_test.video_generation.domain import VideoRuleViolation

TITLE_BAND_PIXELS = 54
_DARK_CHANNEL_MAX = 16
_SEPARATOR_FRACTION = 0.85

# USER_DECISIONS §46/§47：crop-grid-v2。无角标带；按"行/列均值显著偏离邻域中位数"识别分隔带，
# 可识别纯黑、白黑相间边框与手绘墨线类分隔。2026-11-04 Gate 1（515 张既有故事板）后修订（§47）：
# 去掉格内暗线核验（31/42 为桌沿、地平线等画面内容误判）；分隔偏离等分位置时在 ±25% 单格范围内
# 找贯穿全长的分隔带；两条边框线夹浅色缝合并为一条分隔；先剪去白色页边；像素常量随图幅等比放大。
CROP_GRID_V2 = "crop-grid-v2"
_V2_REFERENCE_SIZE = (1672, 941)  # 像素常量按此 16:9 故事板尺寸标定，更大的图等比放大
_V2_DELTA = 40.0  # 分隔线行/列亮度均值与邻域中位数之差下限
_V2_NEIGHBOURHOOD = 40
_V2_GAP = 2  # 同一分隔带内允许的非分隔行/列间隙
_V2_BAND_MAX = 32  # 单条分隔带（含外框）最大宽度
_V2_FRAMED_MAX = 48  # 两条边框线夹浅色缝合并后的最大总宽
_V2_RADIUS_MIN = 16  # 分隔带中心距期望网格线的最小容差（px）
_V2_RADIUS_FRACTION = 0.04  # 容差占单格尺寸比例
_V2_WIDE_FRACTION = 0.25  # 严格容差内找不到时的放宽搜索范围（占单格尺寸）
_V2_MIN_CELL_FRACTION = 0.6  # 放宽搜索得到的每格尺寸不得小于等分尺寸的 60%
_V2_INSET = 3  # 每格四边内缩，去除抗锯齿残边
_V2_WHITE_MIN = 240.0  # 白色页边：整行/列均值不低于此值
_V2_MARGIN_MAX_FRACTION = 0.15  # 每边最多剪去 15% 的白色页边
_V2_SOURCE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
# 暗尾上限：识别留黑空格与分隔合并后的暗尾。
_V2_DARK_MAX = 64.0
_V2_BAND_SEGMENTS = 16
_V2_BAND_SEGMENT_SHARE = 0.9
_V2_BAND_SIDE = (4, 16)  # 分隔带两侧对照条：距带 4–16 px


@dataclass(frozen=True, slots=True)
class DerivedPanelImage:
    content: bytes
    shot_order: int
    crop_xyxy: tuple[int, int, int, int]
    dimensions: tuple[int, int]
    derivation: str


def derive_shot_panels(
    content: bytes,
    *,
    source_sha256: str,
    panel_count: int,
    aspect_ratio: str,
    derivation: Mapping[str, object],
    layout_version: str = GRID_LAYOUT_V1,
) -> tuple[DerivedPanelImage, ...]:
    """Deterministically crop real panels; never edit the source or infer scene transforms."""
    if not content or hashlib.sha256(content).hexdigest() != source_sha256:
        raise VideoRuleViolation("shot_panel_source_hash_mismatch")
    version, scale, gamma_by_shot = _derivation_options(derivation)
    if version == CROP_GRID_V2:
        return _derive_grid_v2(
            content, panel_count, aspect_ratio, layout_version, version, gamma_by_shot
        )
    if layout_version != GRID_LAYOUT_V1:
        # 角标带裁切只适用于历史 v1 版式（带 SHOT 角标）的故事板。
        raise VideoRuleViolation("shot_panel_derivation_unsupported")
    try:
        layout = grid_layout_for(panel_count, aspect_ratio)
    except ValueError as exc:
        raise VideoRuleViolation("shot_panel_layout_invalid") from exc
    try:
        with Image.open(BytesIO(content)) as opened:
            if opened.format != "PNG":
                raise VideoRuleViolation("shot_panel_source_invalid")
            opened.load()
            image = opened.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VideoRuleViolation("shot_panel_source_invalid") from exc

    red, green, blue = image.split()
    channels = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    dark = channels.point(lambda value: 1 if value <= _DARK_CHANNEL_MAX else 0, mode="L")
    if panel_count == 1:
        if image.height <= TITLE_BAND_PIXELS:
            raise VideoRuleViolation("shot_panel_crop_unsafe")
        _assert_content_present(image.crop((0, TITLE_BAND_PIXELS, image.width, image.height)))
        left, right = _horizontal_edges(dark)
        top, bottom = _vertical_edges(dark)
    else:
        # Multi-panel content may legitimately touch the outer frame with dark pixels.
        left, right, top, bottom = 0, image.width, 0, image.height
    vertical = _separators(dark, image, layout.cols - 1, axis="x", left=left, right=right)
    horizontal = _separators(dark, image, layout.rows - 1, axis="y", left=top, right=bottom)
    x_segments = _segments(left, right, vertical)
    y_segments = _segments(top, bottom, horizontal)
    panels: list[DerivedPanelImage] = []
    for order in range(1, layout.rows * layout.cols + 1):
        row, column = divmod(order - 1, layout.cols)
        x0, x1 = x_segments[column]
        y0, y1 = y_segments[row]
        box = (x0, y0 + TITLE_BAND_PIXELS, x1, y1)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise VideoRuleViolation("shot_panel_crop_unsafe")
        crop = image.crop(box)
        if order in layout.empty_panels:
            red, green, blue = crop.split()
            maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
            maximum_value = maximum.getextrema()[1]
            if not isinstance(maximum_value, int) or maximum_value > _DARK_CHANNEL_MAX:
                raise VideoRuleViolation("shot_panel_blank_required")
            continue
        _assert_content_present(crop)
        derivative = crop
        if scale != 1:
            derivative = derivative.resize(
                (derivative.width * scale, derivative.height * scale),
                Image.Resampling.LANCZOS,
            )
        gamma = gamma_by_shot.get(order)
        suffix = ""
        if gamma is not None:
            lookup = [round(((value / 255) ** gamma) * 255) for value in range(256)]
            derivative = derivative.point(lookup * 3)
            suffix = f"+gamma{str(gamma).replace('.', '')}-v1"
        output = BytesIO()
        derivative.save(output, format="PNG")
        panels.append(
            DerivedPanelImage(
                output.getvalue(),
                order,
                box,
                derivative.size,
                version + suffix,
            )
        )
    if len(panels) != panel_count:
        raise VideoRuleViolation("shot_panel_count_mismatch")
    return tuple(panels)


def _derivation_options(
    derivation: Mapping[str, object],
) -> tuple[str, int, dict[int, float]]:
    version = derivation.get("version")
    if version not in {"crop-title54-v1", "crop-title54-lanczos2x-v1", CROP_GRID_V2}:
        raise VideoRuleViolation("shot_panel_derivation_unsupported")
    scale = 2 if version == "crop-title54-lanczos2x-v1" else 1
    if (
        derivation.get("scale", scale) != scale
        or derivation.get("resampling", "lanczos") != "lanczos"
    ):
        raise VideoRuleViolation("shot_panel_derivation_unsupported")
    raw_gamma = derivation.get("gamma_by_shot", {})
    if not isinstance(raw_gamma, Mapping):
        raise VideoRuleViolation("shot_panel_derivation_unsupported")
    gamma: dict[int, float] = {}
    for raw_order, raw_value in raw_gamma.items():
        try:
            order = int(raw_order)
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise VideoRuleViolation("shot_panel_derivation_unsupported") from exc
        if order < 1 or isinstance(raw_value, bool) or not 0.1 <= value <= 10:
            raise VideoRuleViolation("shot_panel_derivation_unsupported")
        gamma[order] = value
    return str(version), scale, gamma


def _fraction(dark: Image.Image, box: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = box
    area = (x1 - x0) * (y1 - y0)
    return dark.crop(box).histogram()[1] / area if area else 0.0


def _horizontal_edges(dark: Image.Image) -> tuple[int, int]:
    width, height = dark.size
    left = 0
    while left < width and _fraction(dark, (left, 0, left + 1, height)) >= _SEPARATOR_FRACTION:
        left += 1
    right = width
    while right > left and _fraction(dark, (right - 1, 0, right, height)) >= _SEPARATOR_FRACTION:
        right -= 1
    return left, right


def _vertical_edges(dark: Image.Image) -> tuple[int, int]:
    width, height = dark.size
    top = 0
    while top < height and _fraction(dark, (0, top, width, top + 1)) >= _SEPARATOR_FRACTION:
        top += 1
    bottom = height
    while bottom > top and _fraction(dark, (0, bottom - 1, width, bottom)) >= _SEPARATOR_FRACTION:
        bottom -= 1
    return top, bottom


def _separators(
    dark: Image.Image,
    image: Image.Image,
    count: int,
    *,
    axis: str,
    left: int,
    right: int,
) -> tuple[tuple[int, int], ...]:
    if count == 0:
        return ()
    if left >= right:
        raise VideoRuleViolation("shot_panel_crop_unsafe")
    size = right - left
    cell = size / (count + 1)
    radius = max(2, math.ceil(cell * 0.1))
    result: list[tuple[int, int]] = []
    for index in range(1, count + 1):
        expected = left + cell * index
        start = max(left + 1, math.floor(expected - radius))
        end = min(right - 1, math.ceil(expected + radius))
        dark_candidates: list[bool] = []
        uniform_candidates: list[bool] = []
        for position in range(start, end + 1):
            box = (
                (position, 0, position + 1, dark.height)
                if axis == "x"
                else (0, position, dark.width, position + 1)
            )
            dark_candidates.append(_fraction(dark, box) >= _SEPARATOR_FRACTION)
            uniform_candidates.append(
                dark_candidates[-1] and _dominant_fraction(image, box) >= 0.5
            )
        dark_groups = _candidate_groups(dark_candidates, start)
        uniform_groups = _candidate_groups(uniform_candidates, start)
        if not uniform_groups:
            raise VideoRuleViolation("shot_panel_gutter_not_found")
        distances = [abs((start + end) / 2 - expected) for start, end in uniform_groups]
        nearest = min(distances)
        winners = [
            group
            for group, distance in zip(uniform_groups, distances, strict=True)
            if distance == nearest
        ]
        if len(winners) != 1:
            raise VideoRuleViolation("shot_panel_gutter_ambiguous")
        uniform = winners[0]
        containers = [
            group for group in dark_groups
            if group[0] <= uniform[0] and group[1] >= uniform[1]
        ]
        if len(containers) != 1:
            raise VideoRuleViolation("shot_panel_gutter_ambiguous")
        dark_group = containers[0]
        selected = dark_group if dark_group[1] - dark_group[0] + 1 <= 32 else uniform
        if selected[1] - selected[0] + 1 > 32:
            raise VideoRuleViolation("shot_panel_gutter_ambiguous")
        result.append(selected)
    return tuple(result)


def _candidate_groups(candidates: list[bool], start: int) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    group_start: int | None = None
    for offset, candidate in enumerate((*candidates, False)):
        position = start + offset
        if candidate and group_start is None:
            group_start = position
        elif not candidate and group_start is not None:
            groups.append((group_start, position - 1))
            group_start = None
    return groups


def _dominant_fraction(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = box
    area = (x1 - x0) * (y1 - y0)
    colors = image.crop(box).getcolors(area)
    return max(count for count, _color in colors) / area if colors else 0.0


def _segments(
    left: int,
    right: int,
    separators: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    cursor = left
    for start, end in separators:
        result.append((cursor, start))
        cursor = end + 1
    result.append((cursor, right))
    return tuple(result)


def _assert_content_present(image: Image.Image) -> None:
    red, green, blue = image.split()
    channels = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    maximum_value = channels.getextrema()[1]
    if not isinstance(maximum_value, int) or maximum_value <= _DARK_CHANNEL_MAX:
        raise VideoRuleViolation("shot_panel_empty")


# --- crop-grid-v2 (USER_DECISIONS §46, revised by §47) --------------------------------


@dataclass(frozen=True, slots=True)
class _V2Pixels:
    """crop-grid-v2 pixel constants scaled to the sheet size."""

    neighbourhood: int
    gap: int
    band_max: int
    framed_max: int
    radius_min: int
    inset: int
    side: tuple[int, int]

    @classmethod
    def for_image(cls, image: Image.Image) -> _V2Pixels:
        reference_width, reference_height = _V2_REFERENCE_SIZE
        factor = max(1.0, min(image.width / reference_width, image.height / reference_height))

        def scaled(value: int) -> int:
            return max(1, round(value * factor))

        return cls(
            scaled(_V2_NEIGHBOURHOOD),
            scaled(_V2_GAP),
            scaled(_V2_BAND_MAX),
            scaled(_V2_FRAMED_MAX),
            scaled(_V2_RADIUS_MIN),
            scaled(_V2_INSET),
            (scaled(_V2_BAND_SIDE[0]), scaled(_V2_BAND_SIDE[1])),
        )


def _derive_grid_v2(
    content: bytes,
    panel_count: int,
    aspect_ratio: str,
    layout_version: str,
    version: str,
    gamma_by_shot: Mapping[int, float],
) -> tuple[DerivedPanelImage, ...]:
    """Crop each shot panel of a grid sheet whose gutters may be black, framed or inked.

    Only shot panels are returned; empty grid cells are never sent to H3, so their
    content is not inspected (§46 decision D3). The inside of a panel is not checked
    for further dividing lines (§47): picture content such as table edges and horizons
    cannot be told apart from a drawn sub-panel border by a line profile.
    """
    try:
        layout = grid_layout_for(panel_count, aspect_ratio, layout_version)
    except ValueError as exc:
        raise VideoRuleViolation("shot_panel_layout_invalid") from exc
    try:
        with Image.open(BytesIO(content)) as opened:
            if opened.format not in _V2_SOURCE_FORMATS:
                raise VideoRuleViolation("shot_panel_source_invalid")
            opened.load()
            image = opened.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if isinstance(exc, VideoRuleViolation):
            raise
        raise VideoRuleViolation("shot_panel_source_invalid") from exc
    boxes = grid_v2_panel_boxes(image, layout, panel_count)
    panels: list[DerivedPanelImage] = []
    for order, box in enumerate(boxes, 1):
        crop = image.crop(box)
        _assert_content_present(crop)
        derivative = crop
        gamma = gamma_by_shot.get(order)
        suffix = ""
        if gamma is not None:
            lookup = [round(((value / 255) ** gamma) * 255) for value in range(256)]
            derivative = derivative.point(lookup * 3)
            suffix = f"+gamma{str(gamma).replace('.', '')}-v1"
        output = BytesIO()
        derivative.save(output, format="PNG")
        panels.append(
            DerivedPanelImage(output.getvalue(), order, box, derivative.size, version + suffix)
        )
    return tuple(panels)


def grid_v2_panel_boxes(
    image: Image.Image, layout: GridLayout, panel_count: int
) -> tuple[tuple[int, int, int, int], ...]:
    """Boxes of the shot panels 1..panel_count in reading order (row-major).

    White page margins and an outer frame are trimmed first. Row gutters are then
    located using only the columns that hold a shot in every shot row; column gutters
    are located inside each row band, so rows whose gutters do not line up still crop.
    Empty trailing cells are not cropped: when they are black they merge with the
    gutter, so the shot edge is taken where the dark tail of the row (or sheet) begins.
    """
    pixels = _V2Pixels.for_image(image)
    luminance = image.convert("L").convert("F")
    x0, x1 = _v2_outer(_v2_means(luminance, "x"), pixels)
    y0, y1 = _v2_outer(_v2_means(luminance, "y"), pixels)
    if x1 - x0 < 2 * pixels.band_max or y1 - y0 < 2 * pixels.band_max:
        raise VideoRuleViolation("shot_panel_crop_unsafe")
    shot_rows = math.ceil(panel_count / layout.cols)
    last_row_cells = panel_count - (shot_rows - 1) * layout.cols
    full_columns = layout.cols if shot_rows == 1 else min(layout.cols, last_row_cells)
    column_width = (x1 - x0) / layout.cols
    column_strip = luminance.crop(
        (x0, 0, round(x0 + column_width * full_columns), luminance.height)
    )
    row_spans = _v2_spans(column_strip, "y", y0, y1, layout.rows, shot_rows, pixels)
    boxes: list[tuple[int, int, int, int]] = []
    for row_index, (top, bottom) in enumerate(row_spans):
        cells = min(layout.cols, panel_count - row_index * layout.cols)
        band = luminance.crop((0, top, luminance.width, bottom))
        for left, right in _v2_spans(band, "x", x0, x1, layout.cols, cells, pixels):
            inset = pixels.inset
            box = (left + inset, top + inset, right - inset, bottom - inset)
            if box[0] >= box[2] or box[1] >= box[3]:
                raise VideoRuleViolation("shot_panel_crop_unsafe")
            boxes.append(box)
    return tuple(boxes)


def _v2_spans(
    region: Image.Image,
    axis: str,
    lo: int,
    hi: int,
    total: int,
    used: int,
    pixels: _V2Pixels,
) -> tuple[tuple[int, int], ...]:
    """Spans of the first `used` of `total` cells between lo and hi along `axis`."""
    means = _v2_means(region, axis)
    cell = (hi - lo) / total
    radius = max(pixels.radius_min, math.ceil(cell * _V2_RADIUS_FRACTION))
    wide = max(radius, math.ceil(cell * _V2_WIDE_FRACTION))
    separators: list[tuple[int, int]] = []
    for index in range(1, used):
        expected = lo + cell * index
        separator = _v2_separator(means, expected, radius, lo, hi, pixels)
        if separator is None:
            separator = _v2_wide_separator(region, axis, means, expected, wide, lo, hi, pixels)
        if separator is None:
            raise VideoRuleViolation("shot_panel_gutter_not_found")
        separators.append(separator)
    end = hi
    if used < total:
        expected = lo + cell * used
        boundary = _v2_separator(means, expected, radius, lo, hi, pixels)
        if boundary is None:
            boundary = _v2_wide_separator(region, axis, means, expected, wide, lo, hi, pixels)
        end = (
            boundary[0]
            if boundary is not None
            else _v2_dark_tail(means, expected, radius, lo, hi, cell, pixels)
        )
    spans = _segments(lo, end, tuple(separators))
    if any(stop - start < cell * _V2_MIN_CELL_FRACTION for start, stop in spans):
        raise VideoRuleViolation("shot_panel_crop_unsafe")
    return spans


def _v2_means(luminance: Image.Image, axis: str) -> list[float]:
    """Mean luminance of every column (axis x) or row (axis y)."""
    size = (luminance.width, 1) if axis == "x" else (1, luminance.height)
    reduced = luminance.resize(size, Image.Resampling.BOX)
    return [float(value) for value in array("f", reduced.tobytes())]


def _v2_extreme(
    means: Sequence[float], lo: int, hi: int, pixels: _V2Pixels
) -> list[bool]:
    marks = [False] * len(means)
    for position in range(max(0, lo), min(len(means), hi)):
        start = max(0, position - pixels.neighbourhood)
        end = min(len(means), position + pixels.neighbourhood + 1)
        marks[position] = (
            abs(means[position] - statistics.median(means[start:end])) >= _V2_DELTA
        )
    return marks


def _v2_runs(
    marks: Sequence[bool], lo: int, hi: int, pixels: _V2Pixels
) -> list[tuple[int, int]]:
    """Runs of marked lines; two frame lines around a light gap become one gutter."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    last = lo
    for position in range(lo, hi):
        if not marks[position]:
            continue
        if start is None:
            start = position
        elif position - last - 1 > pixels.gap:
            runs.append((start, last))
            start = position
        last = position
    if start is not None:
        runs.append((start, last))
    merged: list[tuple[int, int]] = []
    for run in runs:
        if (
            merged
            and merged[-1][1] - merged[-1][0] + 1 <= pixels.band_max
            and run[1] - run[0] + 1 <= pixels.band_max
            and run[1] - merged[-1][0] + 1 <= pixels.framed_max
        ):
            merged[-1] = (merged[-1][0], run[1])
        else:
            merged.append(run)
    return merged


def _v2_outer(means: Sequence[float], pixels: _V2Pixels) -> tuple[int, int]:
    """Trim white page margins (at most 15% per edge), then an outer frame."""
    size = len(means)
    limit = math.floor(size * _V2_MARGIN_MAX_FRACTION)
    low = 0
    while low < limit and means[low] >= _V2_WHITE_MIN:
        low += 1
    high = size
    while size - high < limit and means[high - 1] >= _V2_WHITE_MIN:
        high -= 1
    marks = _v2_extreme(means, low, high, pixels)
    frame_low, frame_high = low, high
    while frame_low - low < pixels.band_max and frame_low + 1 < high and (
        marks[frame_low] or marks[frame_low + 1]
    ):
        frame_low += 1
    while high - frame_high < pixels.band_max and frame_high - 2 > frame_low and (
        marks[frame_high - 1] or marks[frame_high - 2]
    ):
        frame_high -= 1
    return frame_low, frame_high


def _v2_separator(
    means: Sequence[float],
    expected: float,
    radius: int,
    lo: int,
    hi: int,
    pixels: _V2Pixels,
) -> tuple[int, int] | None:
    """The gutter band nearest the expected grid line, or None when there is none."""
    start = max(lo + 1, math.floor(expected - radius - pixels.framed_max))
    end = min(hi - 1, math.ceil(expected + radius + pixels.framed_max))
    candidates = [
        run
        for run in _v2_runs(_v2_extreme(means, start, end, pixels), start, end, pixels)
        if run[1] - run[0] + 1 <= pixels.framed_max
        and abs((run[0] + run[1]) / 2 - expected) <= radius
    ]
    return _v2_nearest(candidates, expected)


def _v2_wide_separator(
    region: Image.Image,
    axis: str,
    means: Sequence[float],
    expected: float,
    wide: int,
    lo: int,
    hi: int,
    pixels: _V2Pixels,
) -> tuple[int, int] | None:
    """A gutter off the equal-grid position: only bands that run the whole length count."""
    start = max(lo + 1, math.floor(expected - wide))
    end = min(hi - 1, math.ceil(expected + wide))
    candidates = [
        run
        for run in _v2_runs(_v2_extreme(means, start, end, pixels), start, end, pixels)
        if run[1] - run[0] + 1 <= pixels.framed_max
        and _v2_is_gutter_band(region, axis, run[0], run[1], pixels)
    ]
    if len(candidates) > 1:
        raise VideoRuleViolation("shot_panel_gutter_ambiguous")
    return candidates[0] if candidates else None


def _v2_nearest(candidates: list[tuple[int, int]], expected: float) -> tuple[int, int] | None:
    if not candidates:
        return None
    distances = [abs((run[0] + run[1]) / 2 - expected) for run in candidates]
    nearest = min(distances)
    winners = [
        run for run, distance in zip(candidates, distances, strict=True) if distance == nearest
    ]
    if len(winners) != 1:
        raise VideoRuleViolation("shot_panel_gutter_ambiguous")
    return winners[0]


def _v2_dark_tail(
    means: Sequence[float],
    expected: float,
    radius: int,
    lo: int,
    hi: int,
    cell: float,
    pixels: _V2Pixels,
) -> int:
    """Where a black empty tail (gutter + empty cells) begins; it must start at the grid line."""
    start = hi
    while start > lo and means[start - 1] <= _V2_DARK_MAX:
        start -= 1
    if hi - start < cell / 2 or abs(start - expected) > radius + pixels.band_max:
        raise VideoRuleViolation("shot_panel_gutter_not_found")
    return start


def _v2_is_gutter_band(
    luminance: Image.Image, axis: str, start: int, end: int, pixels: _V2Pixels
) -> bool:
    """True when, along almost the whole length, some line of the band stands out from both
    of its sides in the same direction (darker or lighter).

    Testing single lines (not the band mean) keeps black/white "sandwich" frames detectable;
    a dividing line that covers only part of the length (a half-height gutter of a different
    layout, a pillar, a tally mark) fails.
    """
    length = luminance.height if axis == "x" else luminance.width
    near, far = pixels.side
    hits = 0
    for index in range(_V2_BAND_SEGMENTS):
        a = length * index // _V2_BAND_SEGMENTS
        b = length * (index + 1) // _V2_BAND_SEGMENTS
        if b <= a:
            continue
        before = _v2_region_mean(luminance, axis, start - far, start - near, a, b)
        after = _v2_region_mean(luminance, axis, end + 1 + near, end + 1 + far, a, b)
        if before is None or after is None:
            continue
        for line in range(start, end + 1):
            value = _v2_region_mean(luminance, axis, line, line + 1, a, b)
            if value is None:
                continue
            darker = before - value >= _V2_DELTA and after - value >= _V2_DELTA
            lighter = value - before >= _V2_DELTA and value - after >= _V2_DELTA
            if darker or lighter:
                hits += 1
                break
    return hits >= math.ceil(_V2_BAND_SEGMENTS * _V2_BAND_SEGMENT_SHARE)


def _v2_region_mean(
    luminance: Image.Image, axis: str, lo: int, hi: int, a: int, b: int
) -> float | None:
    """Mean over [lo, hi) across the band axis and [a, b) along it; None when off-image."""
    across = luminance.width if axis == "x" else luminance.height
    lo, hi = max(0, lo), min(across, hi)
    if hi <= lo:
        return None
    box = (lo, a, hi, b) if axis == "x" else (a, lo, b, hi)
    region = luminance.crop(box).resize((1, 1), Image.Resampling.BOX)
    return float(array("f", region.tobytes())[0])


def check_storyboard_panel_layout(
    path: Path, *, panel_count: int, aspect_ratio: str, layout_version: str
) -> None:
    """Storyboard-time dry run of crop-grid-v2 (§46 W4); raises VideoRuleViolation."""
    content = path.read_bytes()
    derive_shot_panels(
        content,
        source_sha256=hashlib.sha256(content).hexdigest(),
        panel_count=panel_count,
        aspect_ratio=aspect_ratio,
        derivation={"version": CROP_GRID_V2},
        layout_version=layout_version,
    )
