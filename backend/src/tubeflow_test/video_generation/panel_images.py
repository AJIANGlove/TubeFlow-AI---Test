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

# USER_DECISIONS §46：crop-grid-v2。无角标带；按"行均值显著偏离邻域中位数"识别分隔带，
# 可识别纯黑、白黑相间边框与手绘墨线类分隔（fa3a0b30 13 张整图实测，旧裁切 2/13）。
CROP_GRID_V2 = "crop-grid-v2"
_V2_DELTA = 40.0  # 分隔线行/列亮度均值与 ±40 px 邻域中位数之差下限
_V2_NEIGHBOURHOOD = 40
_V2_GAP = 2  # 同一分隔带内允许的非分隔行/列间隙
_V2_BAND_MAX = 32  # 分隔带（含外框）最大宽度
_V2_RADIUS_MIN = 16  # 分隔带中心距期望网格线的最小容差（px）
_V2_RADIUS_FRACTION = 0.04  # 容差占单格尺寸比例
_V2_INSET = 3  # 每格四边内缩，去除抗锯齿残边
_V2_INTERIOR_MARGIN = 0.1  # 版式核验：每格两侧各 10% 之外的中段不得出现完整分隔带
_V2_SOURCE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
# "暗线"上限：中段版式核验只认暗色分隔（门框、柱子等浅色长直线不会误判，fa3a0b30 g7 第 1 格
# 门框即为反例）；同一上限也用于识别留黑空格与分隔合并后的暗尾。
_V2_INTERIOR_DARK_MAX = 64.0


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


# --- crop-grid-v2 (USER_DECISIONS §46) ------------------------------------------------


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
    content is not inspected (§46 decision D3).
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
        _assert_no_interior_gutter(crop)
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

    Row gutters are located first, using only the columns that hold a shot in every
    shot row; column gutters are then located inside each row band, so rows whose
    gutters do not line up still crop. Empty trailing cells are not cropped: when they
    are black they merge with the gutter, so the shot edge is taken where the dark
    tail of the row (or of the sheet) begins.
    """
    luminance = image.convert("L").convert("F")
    x0, x1 = _v2_outer(_v2_means(luminance, "x"))
    y0, y1 = _v2_outer(_v2_means(luminance, "y"))
    if x1 - x0 < 2 * _V2_BAND_MAX or y1 - y0 < 2 * _V2_BAND_MAX:
        raise VideoRuleViolation("shot_panel_crop_unsafe")
    shot_rows = math.ceil(panel_count / layout.cols)
    last_row_cells = panel_count - (shot_rows - 1) * layout.cols
    full_columns = layout.cols if shot_rows == 1 else min(layout.cols, last_row_cells)
    column_width = (x1 - x0) / layout.cols
    column_strip = luminance.crop(
        (x0, 0, round(x0 + column_width * full_columns), luminance.height)
    )
    row_spans = _v2_spans(_v2_means(column_strip, "y"), y0, y1, layout.rows, shot_rows)
    boxes: list[tuple[int, int, int, int]] = []
    for row_index, (top, bottom) in enumerate(row_spans):
        cells = min(layout.cols, panel_count - row_index * layout.cols)
        band = luminance.crop((0, top, luminance.width, bottom))
        for left, right in _v2_spans(_v2_means(band, "x"), x0, x1, layout.cols, cells):
            box = (left + _V2_INSET, top + _V2_INSET, right - _V2_INSET, bottom - _V2_INSET)
            if box[0] >= box[2] or box[1] >= box[3]:
                raise VideoRuleViolation("shot_panel_crop_unsafe")
            boxes.append(box)
    return tuple(boxes)


def _v2_spans(
    means: Sequence[float], lo: int, hi: int, total: int, used: int
) -> tuple[tuple[int, int], ...]:
    """Spans of the first `used` of `total` equal cells between lo and hi."""
    cell = (hi - lo) / total
    radius = max(_V2_RADIUS_MIN, math.ceil(cell * _V2_RADIUS_FRACTION))
    separators = []
    for index in range(1, used):
        separator = _v2_separator(means, lo + cell * index, radius, lo, hi)
        if separator is None:
            raise VideoRuleViolation("shot_panel_gutter_not_found")
        separators.append(separator)
    end = hi
    if used < total:
        expected = lo + cell * used
        boundary = _v2_separator(means, expected, radius, lo, hi)
        end = boundary[0] if boundary is not None else _v2_dark_tail(
            means, expected, radius, lo, hi, cell
        )
    return _segments(lo, end, tuple(separators))


def _v2_means(luminance: Image.Image, axis: str) -> list[float]:
    """Mean luminance of every column (axis x) or row (axis y)."""
    size = (luminance.width, 1) if axis == "x" else (1, luminance.height)
    reduced = luminance.resize(size, Image.Resampling.BOX)
    return [float(value) for value in array("f", reduced.tobytes())]


def _v2_extreme(means: Sequence[float], lo: int, hi: int) -> list[bool]:
    marks = [False] * len(means)
    for position in range(max(0, lo), min(len(means), hi)):
        start = max(0, position - _V2_NEIGHBOURHOOD)
        end = min(len(means), position + _V2_NEIGHBOURHOOD + 1)
        marks[position] = (
            abs(means[position] - statistics.median(means[start:end])) >= _V2_DELTA
        )
    return marks


def _v2_runs(marks: Sequence[bool], lo: int, hi: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    last = lo
    for position in range(lo, hi):
        if not marks[position]:
            continue
        if start is None:
            start = position
        elif position - last - 1 > _V2_GAP:
            runs.append((start, last))
            start = position
        last = position
    if start is not None:
        runs.append((start, last))
    return runs


def _v2_outer(means: Sequence[float]) -> tuple[int, int]:
    """Trim an outer frame (at most _V2_BAND_MAX px per edge)."""
    size = len(means)
    marks = _v2_extreme(means, 0, size)
    low = 0
    while low < min(_V2_BAND_MAX, size - 1) and (marks[low] or marks[low + 1]):
        low += 1
    high = size
    while size - high < _V2_BAND_MAX and high - 2 > low and (marks[high - 1] or marks[high - 2]):
        high -= 1
    return low, high


def _v2_separator(
    means: Sequence[float], expected: float, radius: int, lo: int, hi: int
) -> tuple[int, int] | None:
    """The gutter band nearest the expected grid line, or None when there is none."""
    start = max(lo + 1, math.floor(expected - radius - _V2_BAND_MAX))
    end = min(hi - 1, math.ceil(expected + radius + _V2_BAND_MAX))
    candidates = [
        run
        for run in _v2_runs(_v2_extreme(means, start, end), start, end)
        if run[1] - run[0] + 1 <= _V2_BAND_MAX and abs((run[0] + run[1]) / 2 - expected) <= radius
    ]
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
    means: Sequence[float], expected: float, radius: int, lo: int, hi: int, cell: float
) -> int:
    """Where a black empty tail (gutter + empty cells) begins; it must start at the grid line."""
    start = hi
    while start > lo and means[start - 1] <= _V2_INTERIOR_DARK_MAX:
        start -= 1
    if hi - start < cell / 2 or abs(start - expected) > radius + _V2_BAND_MAX:
        raise VideoRuleViolation("shot_panel_gutter_not_found")
    return start


def _assert_no_interior_gutter(crop: Image.Image) -> None:
    """Layout check: a crop must not contain another gutter band in its middle 80%.

    A sheet drawn with more panels than the requested grid shows up here instead of
    leaking two shots into one reference.
    """
    luminance = crop.convert("L").convert("F")
    for axis in ("x", "y"):
        means = _v2_means(luminance, axis)
        size = len(means)
        lo = math.ceil(size * _V2_INTERIOR_MARGIN)
        hi = math.floor(size * (1 - _V2_INTERIOR_MARGIN))
        marks = _v2_extreme(means, lo, hi)
        for start, end in _v2_runs(marks, lo, hi):
            width = end - start + 1
            if 2 <= width <= _V2_BAND_MAX and _v2_is_gutter_band(luminance, axis, start, end):
                raise VideoRuleViolation("shot_panel_layout_mismatch")


_V2_BAND_SEGMENTS = 16
_V2_BAND_SEGMENT_SHARE = 0.9
_V2_BAND_SIDE = (4, 16)  # neighbour strip on each side of a band: from 4 to 16 px away


def _v2_is_gutter_band(luminance: Image.Image, axis: str, start: int, end: int) -> bool:
    """True when, along almost the whole length, some dark line is darker than both sides.

    Testing single lines (not the band mean) keeps black/white "sandwich" frames detectable.
    """
    length = luminance.height if axis == "x" else luminance.width
    near, far = _V2_BAND_SIDE
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
            if (
                value is not None
                and value <= _V2_INTERIOR_DARK_MAX
                and before - value >= _V2_DELTA
                and after - value >= _V2_DELTA
            ):
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
    return float(region.getpixel((0, 0)))


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
