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


def test_lines_inside_a_panel_are_not_checked() -> None:
    # §47: a line inside a panel is not a reason to reject the sheet (Gate 1: 31 of 42 such
    # rejections were table edges, shelves or horizons).
    panels = _derive(_sheet(2, 2, extra_vertical_in_first=True), 4)
    assert len(panels) == 4


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
@pytest.mark.parametrize(
    ("group", "wrong"), [(1, 2), (2, 2), (9, 2), (3, 2), (10, 2), (1, 4), (4, 3), (11, 3)]
)
def test_fa3a0b30_sheets_with_the_wrong_layout_are_rejected(group: int, wrong: int) -> None:
    content = (EVIDENCE / f"sb_{group}.png").read_bytes()
    with pytest.raises(VideoRuleViolation):
        _derive(content, wrong, GRID_LAYOUT_V1)


# --- §47 revision (Gate 1 findings) -------------------------------------------------------


def _columns_sheet(
    edges: tuple[int, ...],
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    gutter: Gutter = _black,
    gutter_width: int = 12,
    margin: tuple[int, int] = (0, 0),
) -> bytes:
    """One row of panels whose gutters start at the given x positions (uneven widths allowed).

    margin = (left/right, top/bottom) white page margin around the whole grid.
    """
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    mx, my = margin
    lefts = (mx, *(edge + gutter_width for edge in edges))
    rights = (*edges, width - mx)
    for order, (left, right) in enumerate(zip(lefts, rights, strict=True), 1):
        image.paste(_panel((right - left, height - 2 * my), order), (left, my))
    for edge in edges:
        gutter(draw, (edge, my, edge + gutter_width - 1, height - my - 1))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_uneven_panel_widths_are_found_by_the_wide_search() -> None:
    # fa3a0b30-era sheets: gutters 25-125 px away from the equal-grid position.
    for edges in ((700,), (480, 1050)):
        content = _columns_sheet(edges)
        panels = _derive(content, len(edges) + 1, GRID_LAYOUT_V1)
        for panel, edge in zip(panels, edges, strict=False):
            assert abs(panel.crop_xyxy[2] - edge) <= 6
        _assert_panels_clean(panels, len(edges) + 1)


def test_panel_narrower_than_sixty_percent_of_a_cell_fails_closed() -> None:
    with pytest.raises(VideoRuleViolation):
        _derive(_columns_sheet((300,)), 2, GRID_LAYOUT_V1)  # 300 px vs 836 px cell


def test_two_frame_lines_around_a_light_gap_are_one_gutter() -> None:
    def framed(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
        x0, y0, x1, y1 = box
        draw.rectangle(box, fill=(255, 255, 255))
        draw.rectangle((x0, y0, x0 + 5, y1), fill=(15, 15, 15))
        draw.rectangle((x1 - 5, y0, x1, y1), fill=(15, 15, 15))

    panels = _derive(_columns_sheet((818,), gutter=framed, gutter_width=36), 2, GRID_LAYOUT_V1)
    assert panels[0].crop_xyxy[2] <= 818 and panels[1].crop_xyxy[0] >= 818 + 36
    _assert_panels_clean(panels, 2)


def test_white_page_margins_are_trimmed() -> None:
    panels = _derive(_columns_sheet((830,), margin=(20, 110)), 2, GRID_LAYOUT_V1)
    for panel in panels:
        assert panel.crop_xyxy[1] >= 110 and panel.crop_xyxy[3] <= HEIGHT - 110
    _assert_panels_clean(panels, 2)


def test_larger_sheets_scale_the_pixel_constants() -> None:
    # 2752x1536 provider output with a 60 px framed gutter (Gate 1 sheet 5ea4923e).
    def framed(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
        x0, y0, x1, y1 = box
        draw.rectangle(box, fill=(255, 255, 255))
        draw.rectangle((x0, y0, x0 + 7, y1), fill=(25, 25, 25))
        draw.rectangle((x1 - 7, y0, x1, y1), fill=(25, 25, 25))

    content = _columns_sheet((1346,), width=2752, height=1536, gutter=framed, gutter_width=60)
    panels = _derive(content, 2, GRID_LAYOUT_V1)
    assert panels[0].crop_xyxy[2] <= 1346 and panels[1].crop_xyxy[0] >= 1346 + 60


def test_a_different_layout_with_a_partial_gutter_still_fails() -> None:
    # 1x3 requested, but the sheet is two panels on top and one wide panel below
    # (Gate 1 sheets d41d087c, 62cbdcef): the half-height gutter is not a full-length band.
    image = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    image.paste(_panel((830, 464), 1), (0, 0))
    image.paste(_panel((830, 464), 2), (842, 0))
    image.paste(_panel((WIDTH, 465), 3), (0, 476))
    output = BytesIO()
    image.save(output, format="PNG")
    with pytest.raises(VideoRuleViolation, match="shot_panel_gutter_not_found"):
        _derive(output.getvalue(), 3, GRID_LAYOUT_V1)


GATE1 = (
    Path(__file__).resolve().parents[3]
    / "Pending issues"
    / "2026-11-04"
    / "singlepanel_test"
    / "gate1"
    / "diag"
    / "originals"
)
# Gate 1 originals: (generation id prefix, shots, crops?) - judged by eye on the diagnostics
# contact sheets; d41d087c and 62cbdcef were drawn in a different layout than requested.
GATE1_EXPECTED = (
    ("1301ce7f", 3, True),
    ("32f97765", 3, True),
    ("39e8f05f", 2, True),
    ("4669adac", 2, True),
    ("4e60a02e", 3, True),
    ("56ba11f8", 3, True),
    ("5ea4923e", 2, True),
    ("61ff3c90", 4, True),
    ("62cbdcef", 3, False),
    ("6f27f613", 3, True),
    ("af33d266", 2, True),
    ("b29ae730", 5, True),
    ("cbb85175", 2, True),
    ("d41d087c", 3, False),
    ("f42f9602", 3, True),
)


@pytest.mark.skipif(not GATE1.exists(), reason="Gate 1 originals not present")
@pytest.mark.parametrize(("prefix", "shots", "crops"), GATE1_EXPECTED)
def test_gate1_originals(prefix: str, shots: int, crops: bool) -> None:
    (path,) = GATE1.glob(f"{prefix}*.png")
    content = path.read_bytes()
    if crops:
        assert len(_derive(content, shots, GRID_LAYOUT_V1)) == shots
    else:
        with pytest.raises(VideoRuleViolation, match="shot_panel_gutter_not_found"):
            _derive(content, shots, GRID_LAYOUT_V1)
