from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageChops, UnidentifiedImageError

from tubeflow_test.domain.storyboards import grid_layout_for
from tubeflow_test.video_generation.domain import VideoRuleViolation

TITLE_BAND_PIXELS = 54
_DARK_CHANNEL_MAX = 16
_SEPARATOR_FRACTION = 0.85


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
) -> tuple[DerivedPanelImage, ...]:
    """Deterministically crop real panels; never edit the source or infer scene transforms."""
    if not content or hashlib.sha256(content).hexdigest() != source_sha256:
        raise VideoRuleViolation("shot_panel_source_hash_mismatch")
    version, scale, gamma_by_shot = _derivation_options(derivation)
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
    if version not in {"crop-title54-v1", "crop-title54-lanczos2x-v1"}:
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
