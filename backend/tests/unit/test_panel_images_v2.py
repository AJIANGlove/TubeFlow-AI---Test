"""crop-grid-v2 (USER_DECISIONS §46): gutters may be black, white-framed or inked."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tubeflow_test.domain.storyboards import GRID_LAYOUT_V1, GRID_LAYOUT_V2
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.panel_images import (
    CROP_GRID_V2,
    check_storyboard_panel_layout,
    derive_shot_panels,
)

WIDTH, HEIGHT = 1672, 941
EVIDENCE = (
    Path(__file__).resolve().parents[3] / "Pending issues" / "2026-11-04" / "evidence" / "shortproj"
)
# Shots per group of test project fa3a0b30 (v1 layouts: 2 → 1×2, 3 → 1×3, 4 → 2×2).
EVIDENCE_SHOTS = (3, 3, 3, 4, 3, 4, 3, 4, 3, 3, 2, 2, 2)


def _panel(size: tuple[int, int], seed: int) -> Image.Image:
    """Smooth, mid-tone content with a few shapes (no full-length dark lines)."""
    width, height = size
    gradient = Image.linear_gradient("L").rotate(seed * 37 % 360).resize(size)
    base = Image.merge(
        "RGB",
        (
            gradient.point(lambda v: 90 + v // 3),
            gradient.point(lambda v: 120 + v // 4),
            Image.new("L", size, 140 + seed * 7 % 60),
        ),
    )
    draw = ImageDraw.Draw(base)
    draw.ellipse(
        (width // 4, height // 4, width // 4 + width // 3, height // 4 + height // 3),
        fill=(200, 170 - seed * 9 % 50, 120),
    )
    draw.rectangle(
        (width // 2, height // 2, width // 2 + width // 5, height // 2 + height // 6),
        fill=(110, 140, 90 + seed * 13 % 80),
    )
    return base


Gutter = Callable[[ImageDraw.ImageDraw, tuple[int, int, int, int]], None]


def _black(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    draw.rectangle(box, fill=(0, 0, 0))


def _white_framed(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=(250, 250, 250))
    if x1 - x0 < y1 - y0:  # vertical gutter: black core
        draw.rectangle((x0 + 3, y0, x1 - 3, y1), fill=(5, 5, 5))
    else:
        draw.rectangle((x0, y0 + 3, x1, y1 - 3), fill=(5, 5, 5))


def _inked(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=(38, 30, 26))
    # Speckles: the gutter is dark but not one flat colour.
    for index in range(0, max(x1 - x0, y1 - y0), 7):
        if x1 - x0 < y1 - y0:
            draw.point((x0 + index % (x1 - x0 + 1), y0 + index), fill=(70, 60, 50))
        else:
            draw.point((x0 + index, y0 + index % (y1 - y0 + 1)), fill=(70, 60, 50))


def _sheet(
    rows: int,
    cols: int,
    *,
    gutter: Gutter = _black,
    width: int = 12,
    frame: int = 0,
    row_offsets: tuple[int, ...] | None = None,
    blank: tuple[int, ...] = (),
    extra_vertical_in_first: bool = False,
    light_line_in_first: bool = False,
    fmt: str = "PNG",
) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    inner = (frame, frame, WIDTH - frame, HEIGHT - frame)
    cell_h = (inner[3] - inner[1] - (rows - 1) * width) / rows
    cell_w = (inner[2] - inner[0] - (cols - 1) * width) / cols
    order = 0
    for row in range(rows):
        top = round(inner[1] + row * (cell_h + width))
        bottom = round(top + cell_h)
        offset = row_offsets[row] if row_offsets else 0
        for col in range(cols):
            order += 1
            left = round(inner[0] + col * (cell_w + width)) + (offset if col else 0)
            right = round(inner[0] + (col + 1) * (cell_w + width) - width) + (
                offset if col < cols - 1 else 0
            )
            if order in blank:
                continue
            image.paste(_panel((right - left, bottom - top), order), (left, top))
            if order == 1 and extra_vertical_in_first:
                middle = (left + right) // 2
                _black(draw, (middle - 6, top, middle + 6, bottom))
            if order == 1 and light_line_in_first:
                x = left + (right - left) // 3
                draw.rectangle((x, top, x + 8, bottom), fill=(205, 200, 190))
            if col < cols - 1:
                gutter(draw, (right, top, right + width - 1, bottom))
        if row < rows - 1:
            gutter(draw, (inner[0], bottom, inner[2] - 1, bottom + width - 1))
    output = BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


def _derive(content: bytes, count: int, version: str = GRID_LAYOUT_V2, **kwargs: object):  # type: ignore[no-untyped-def]
    return derive_shot_panels(
        content,
        source_sha256=hashlib.sha256(content).hexdigest(),
        panel_count=count,
        aspect_ratio="16:9",
        derivation={"version": CROP_GRID_V2, **kwargs},
        layout_version=version,
    )


def _assert_panels_clean(panels, count: int) -> None:  # type: ignore[no-untyped-def]
    assert [panel.shot_order for panel in panels] == list(range(1, count + 1))
    for panel in panels:
        assert panel.derivation == CROP_GRID_V2
        with Image.open(BytesIO(panel.content)) as image:
            assert image.format == "PNG"
            assert image.size == panel.dimensions
            rgb = image.convert("RGB")
            # No gutter pixels remain on any edge column or row.
            for x, y in ((0, rgb.height // 2), (rgb.width - 1, rgb.height // 2)):
                assert max(rgb.getpixel((x, y))) > 60
            for x, y in ((rgb.width // 2, 0), (rgb.width // 2, rgb.height - 1)):
                assert max(rgb.getpixel((x, y))) > 60


@pytest.mark.parametrize("gutter", [_black, _white_framed, _inked])
def test_four_panel_sheet_crops_with_each_gutter_style(gutter: Gutter) -> None:
    panels = _derive(_sheet(2, 2, gutter=gutter), 4)
    _assert_panels_clean(panels, 4)
    for panel in panels:
        width, height = panel.dimensions
        assert 800 <= width <= 836 and 440 <= height <= 470


def test_three_shots_use_three_cells_and_ignore_the_fourth() -> None:
    # The empty cell is never sent to H3, so drawn content there is not a failure (§46 D3).
    panels = _derive(_sheet(2, 2), 3)
    _assert_panels_clean(panels, 3)
    blank = _derive(_sheet(2, 2, blank=(4,)), 3)
    assert [panel.crop_xyxy for panel in blank] == [panel.crop_xyxy for panel in panels]


def test_two_shots_use_the_top_row_of_a_2x2_sheet() -> None:
    panels = _derive(_sheet(2, 2, blank=(3, 4)), 2)
    _assert_panels_clean(panels, 2)
    assert all(panel.crop_xyxy[3] < HEIGHT // 2 for panel in panels)


def test_rows_with_misaligned_vertical_gutters_still_crop() -> None:
    panels = _derive(_sheet(2, 2, row_offsets=(0, 18)), 4)
    _assert_panels_clean(panels, 4)
    assert panels[2].crop_xyxy[2] - panels[0].crop_xyxy[2] >= 12


def test_outer_frame_is_trimmed() -> None:
    panels = _derive(_sheet(2, 2, frame=10), 4)
    _assert_panels_clean(panels, 4)
    assert panels[0].crop_xyxy[0] >= 10 and panels[0].crop_xyxy[1] >= 10


def test_six_and_five_shot_sheets_crop() -> None:
    _assert_panels_clean(_derive(_sheet(2, 3), 6), 6)
    _assert_panels_clean(_derive(_sheet(2, 3, blank=(6,)), 5), 5)


def test_one_shot_sheet_returns_the_whole_frame() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT))
    image.paste(_panel((WIDTH, HEIGHT), 1))
    output = BytesIO()
    image.save(output, format="PNG")
    panels = _derive(output.getvalue(), 1)
    assert len(panels) == 1
    assert panels[0].dimensions == (WIDTH - 6, HEIGHT - 6)


def test_gutter_far_from_the_grid_line_fails_closed() -> None:
    sheet = _sheet(1, 2)  # one vertical gutter in the middle, but a 2x2 grid is expected
    with pytest.raises(VideoRuleViolation, match="shot_panel_gutter_not_found"):
        _derive(sheet, 4)


def test_extra_gutter_inside_a_panel_is_a_layout_mismatch() -> None:
    with pytest.raises(VideoRuleViolation, match="shot_panel_layout_mismatch"):
        _derive(_sheet(2, 2, extra_vertical_in_first=True), 4)


def test_long_light_line_inside_a_panel_is_not_a_gutter() -> None:
    # A door frame or pillar is not a black gutter (fa3a0b30 g7 panel 1).
    _assert_panels_clean(_derive(_sheet(2, 2, light_line_in_first=True), 4), 4)


def test_jpeg_source_is_accepted_and_output_is_png() -> None:
    panels = _derive(_sheet(2, 2, fmt="JPEG"), 4)
    _assert_panels_clean(panels, 4)


def test_title_band_crop_is_refused_for_v2_sheets() -> None:
    content = _sheet(2, 2)
    with pytest.raises(VideoRuleViolation, match="shot_panel_derivation_unsupported"):
        derive_shot_panels(
            content,
            source_sha256=hashlib.sha256(content).hexdigest(),
            panel_count=4,
            aspect_ratio="16:9",
            derivation={"version": "crop-title54-v1"},
            layout_version=GRID_LAYOUT_V2,
        )


def test_crop_is_deterministic_and_leaves_the_source_unchanged() -> None:
    content = _sheet(2, 2, gutter=_inked)
    before = hashlib.sha256(content).hexdigest()
    first = _derive(content, 4)
    second = _derive(content, 4)
    assert [panel.content for panel in first] == [panel.content for panel in second]
    assert hashlib.sha256(content).hexdigest() == before


def test_storyboard_time_check_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "sheet.png"
    path.write_bytes(_sheet(2, 2))
    check_storyboard_panel_layout(
        path, panel_count=3, aspect_ratio="16:9", layout_version="grid-v2"
    )
    path.write_bytes(_sheet(1, 2))
    with pytest.raises(VideoRuleViolation):
        check_storyboard_panel_layout(
            path, panel_count=3, aspect_ratio="16:9", layout_version="grid-v2"
        )


@pytest.mark.skipif(not EVIDENCE.exists(), reason="fa3a0b30 evidence sheets not present")
@pytest.mark.parametrize(("group", "shots"), list(enumerate(EVIDENCE_SHOTS, 1)))
def test_fa3a0b30_storyboards_crop(group: int, shots: int) -> None:
    # The existing crop-title54-v1 failed on 11 of these 13 sheets (gutter_not_found).
    content = (EVIDENCE / f"sb_{group}.png").read_bytes()
    panels = _derive(content, shots, GRID_LAYOUT_V1)
    assert len(panels) == shots
    for panel in panels:
        width, height = panel.dimensions
        assert width >= 520 and height >= 440


@pytest.mark.skipif(not EVIDENCE.exists(), reason="fa3a0b30 evidence sheets not present")
@pytest.mark.parametrize(("group", "wrong"), [(4, 2), (6, 2), (8, 2), (11, 1), (12, 1), (1, 2)])
def test_fa3a0b30_sheets_with_the_wrong_layout_are_rejected(group: int, wrong: int) -> None:
    content = (EVIDENCE / f"sb_{group}.png").read_bytes()
    with pytest.raises(VideoRuleViolation):
        _derive(content, wrong, GRID_LAYOUT_V1)
