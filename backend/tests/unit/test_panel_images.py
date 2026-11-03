from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from tubeflow_test.domain.storyboards import grid_layout_for
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.panel_images import derive_shot_panels


def _board(
    panel_count: int,
    *,
    aspect_ratio: str = "16:9",
    cell_width: int = 101,
    cell_height: int = 97,
    gutter: int = 2,
    blank_sixth: bool = True,
) -> tuple[bytes, dict[int, tuple[int, int, int]]]:
    layout = grid_layout_for(panel_count, aspect_ratio)
    width = layout.cols * cell_width + (layout.cols - 1) * gutter
    height = layout.rows * cell_height + (layout.rows - 1) * gutter
    image = Image.new("RGB", (width, height), (0, 0, 0))
    colors: dict[int, tuple[int, int, int]] = {}
    for order in range(1, panel_count + 1):
        index = order - 1
        row, column = divmod(index, layout.cols)
        color = (40 + order * 20, 80 + order * 5, 120 + order * 3)
        colors[order] = color
        origin = (column * (cell_width + gutter), row * (cell_height + gutter))
        ImageDraw.Draw(image).rectangle(
            (
                origin[0],
                origin[1],
                origin[0] + cell_width - 1,
                origin[1] + cell_height - 1,
            ),
            fill=(20, 20, 20),
        )
        ImageDraw.Draw(image).rectangle(
            (
                origin[0],
                origin[1] + 54,
                origin[0] + cell_width - 1,
                origin[1] + cell_height - 1,
            ),
            fill=color,
        )
    if panel_count == 5 and not blank_sixth:
        origin = (2 * (cell_width + gutter), cell_height + gutter)
        ImageDraw.Draw(image).rectangle(
            (origin[0], origin[1] + 54, width - 1, height - 1),
            fill=(200, 100, 50),
        )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), colors


def _derivation(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {"version": "crop-title54-v1"}
    value.update(overrides)
    return value


@pytest.mark.parametrize("panel_count", [1, 2, 3, 4, 5, 6])
def test_all_landscape_layouts_crop_exact_pixels_without_mutating_source(
    panel_count: int,
) -> None:
    content, colors = _board(panel_count)
    digest = hashlib.sha256(content).hexdigest()
    panels = derive_shot_panels(
        content,
        source_sha256=digest,
        panel_count=panel_count,
        aspect_ratio="16:9",
        derivation=_derivation(),
    )
    layout = grid_layout_for(panel_count, "16:9")
    assert len(panels) == panel_count
    assert hashlib.sha256(content).hexdigest() == digest
    assert [item.shot_order for item in panels] == list(range(1, panel_count + 1))
    for panel in panels:
        with Image.open(BytesIO(panel.content)) as image:
            assert image.size == (101, 43)
            assert set(image.getdata()) == {colors[panel.shot_order]}
        row = (panel.shot_order - 1) // layout.cols
        assert panel.crop_xyxy[1] == row * (97 + 2) + 54
        assert panel.dimensions == (101, 43)
        assert panel.derivation == "crop-title54-v1"
    assert layout.rows >= 1


def test_portrait_five_panel_layout_skips_only_the_blank_sixth() -> None:
    content, _colors = _board(5, aspect_ratio="9:16")
    panels = derive_shot_panels(
        content,
        source_sha256=hashlib.sha256(content).hexdigest(),
        panel_count=5,
        aspect_ratio="9:16",
        derivation=_derivation(),
    )
    assert [item.shot_order for item in panels] == [1, 2, 3, 4, 5]
    assert all(item.crop_xyxy[2] - item.crop_xyxy[0] == 101 for item in panels)


def test_odd_dimensions_and_one_pixel_content_are_deterministic() -> None:
    content, colors = _board(2, cell_width=1, cell_height=55, gutter=1)
    kwargs = {
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "panel_count": 2,
        "aspect_ratio": "16:9",
        "derivation": _derivation(),
    }
    first = derive_shot_panels(content, **kwargs)  # type: ignore[arg-type]
    second = derive_shot_panels(content, **kwargs)  # type: ignore[arg-type]
    assert [item.content for item in first] == [item.content for item in second]
    for panel in first:
        assert panel.dimensions == (1, 1)
        with Image.open(BytesIO(panel.content)) as image:
            assert image.getpixel((0, 0)) == colors[panel.shot_order]


def test_explicit_resize_and_per_shot_gamma_are_not_global_defaults() -> None:
    content, _colors = _board(2)
    panels = derive_shot_panels(
        content,
        source_sha256=hashlib.sha256(content).hexdigest(),
        panel_count=2,
        aspect_ratio="16:9",
        derivation=_derivation(
            version="crop-title54-lanczos2x-v1",
            scale=2,
            resampling="lanczos",
            gamma_by_shot={"2": 0.75},
        ),
    )
    assert all(item.dimensions == (202, 86) for item in panels)
    assert panels[0].content != panels[1].content
    assert "gamma075-v1" not in panels[0].derivation
    assert "gamma075-v1" in panels[1].derivation


def test_source_hash_mismatch_fails_before_image_processing() -> None:
    content, _colors = _board(1)
    with pytest.raises(VideoRuleViolation, match="shot_panel_source_hash_mismatch"):
        derive_shot_panels(
            content,
            source_sha256="0" * 64,
            panel_count=1,
            aspect_ratio="16:9",
            derivation=_derivation(),
        )


def test_missing_gutter_fails_closed() -> None:
    image = Image.new("RGB", (200, 100), (100, 100, 100))
    output = BytesIO()
    image.save(output, format="PNG")
    with pytest.raises(VideoRuleViolation, match="shot_panel_gutter_not_found"):
        derive_shot_panels(
            output.getvalue(),
            source_sha256=hashlib.sha256(output.getvalue()).hexdigest(),
            panel_count=2,
            aspect_ratio="16:9",
            derivation=_derivation(),
        )


def test_nonblank_sixth_panel_is_rejected() -> None:
    content, _colors = _board(5, blank_sixth=False)
    with pytest.raises(VideoRuleViolation, match="shot_panel_blank_required"):
        derive_shot_panels(
            content,
            source_sha256=hashlib.sha256(content).hexdigest(),
            panel_count=5,
            aspect_ratio="16:9",
            derivation=_derivation(),
        )


def test_empty_panel_content_is_rejected() -> None:
    content, _colors = _board(1)
    image = Image.open(BytesIO(content)).convert("RGB")
    ImageDraw.Draw(image).rectangle((0, 54, 100, 96), fill=(0, 0, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    with pytest.raises(VideoRuleViolation, match="shot_panel_empty"):
        derive_shot_panels(
            output.getvalue(),
            source_sha256=hashlib.sha256(output.getvalue()).hexdigest(),
            panel_count=1,
            aspect_ratio="16:9",
            derivation=_derivation(),
        )


def test_dark_scene_beside_gutter_does_not_widen_separator() -> None:
    content, _colors = _board(2)
    image = Image.open(BytesIO(content)).convert("RGB")
    for x in range(103, 202):
        for y in range(54, 97):
            image.putpixel((x, y), (x % 8, y % 8, (x + y) % 8))
    output = BytesIO()
    image.save(output, format="PNG")
    panels = derive_shot_panels(
        output.getvalue(),
        source_sha256=hashlib.sha256(output.getvalue()).hexdigest(),
        panel_count=2,
        aspect_ratio="16:9",
        derivation=_derivation(),
    )
    assert [panel.crop_xyxy for panel in panels] == [(0, 54, 101, 97), (103, 54, 204, 97)]


def test_multi_panel_outer_dark_pixels_are_not_trimmed() -> None:
    content, _colors = _board(2)
    image = Image.open(BytesIO(content)).convert("RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1, 96), fill=(0, 0, 0))
    draw.rectangle((200, 0, 201, 96), fill=(0, 0, 0))
    draw.rectangle((0, 95, 201, 96), fill=(0, 0, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    panels = derive_shot_panels(
        output.getvalue(),
        source_sha256=hashlib.sha256(output.getvalue()).hexdigest(),
        panel_count=2,
        aspect_ratio="16:9",
        derivation=_derivation(),
    )
    assert [panel.crop_xyxy for panel in panels] == [(0, 54, 101, 97), (103, 54, 204, 97)]


def test_unknown_derivation_version_is_rejected() -> None:
    content, _colors = _board(1)
    with pytest.raises(VideoRuleViolation, match="shot_panel_derivation_unsupported"):
        derive_shot_panels(
            content,
            source_sha256=hashlib.sha256(content).hexdigest(),
            panel_count=1,
            aspect_ratio="16:9",
            derivation=_derivation(version="unknown"),
        )
