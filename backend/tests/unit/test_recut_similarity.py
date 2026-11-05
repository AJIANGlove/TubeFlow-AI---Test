"""recut-similarity-v1（USER_DECISIONS §48.5）：回切两格的差异度量，只用合成图。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from test_panel_images_v2 import _panel, _sheet

from tubeflow_test.domain.storyboards import GRID_LAYOUT_V2
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.panel_images import panel_distance, recut_panel_distances


def _write(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "sheet.png"
    path.write_bytes(content)
    return path


def _distances(path: Path, pairs: tuple[tuple[int, int], ...]) -> tuple[float, ...]:
    return recut_panel_distances(
        path, panel_count=6, aspect_ratio="16:9", layout_version=GRID_LAYOUT_V2, pairs=pairs
    )


def test_identical_recut_panels_are_near_zero_and_different_ones_are_far(tmp_path: Path) -> None:
    same = _write(
        tmp_path, _sheet(2, 3, panel=lambda size, order: _panel(size, 1 if order == 6 else order))
    )
    (near,) = _distances(same, ((1, 6),))
    other = tmp_path / "other"
    other.mkdir()
    different = _write(other, _sheet(2, 3))
    (far,) = _distances(different, ((1, 6),))
    assert 0 <= near < 0.02
    assert far > 0.2
    assert far > 10 * near


def test_one_distance_per_pair_in_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path, _sheet(2, 3, panel=lambda size, order: _panel(size, 1 if order == 6 else order))
    )
    values = _distances(path, ((1, 6), (2, 5)))
    assert len(values) == 2
    assert values[0] < values[1]


def test_flat_panels() -> None:
    grey = Image.new("RGB", (80, 45), (120, 120, 120))
    assert panel_distance(grey, grey.copy()) == 0.0
    assert panel_distance(grey, Image.new("RGB", (80, 45), (200, 200, 200))) == 1.0
    assert panel_distance(grey, _panel((80, 45), 3)) == 1.0


def test_distance_ignores_small_crop_size_differences() -> None:
    picture = _panel((825, 460), 4)
    assert panel_distance(picture, picture.resize((818, 455))) < 0.02


def test_sheets_that_cannot_be_cropped_raise(tmp_path: Path) -> None:
    white = tmp_path / "white.png"
    Image.new("RGB", (1672, 941), "white").save(white, "PNG")
    with pytest.raises(VideoRuleViolation):
        _distances(white, ((1, 6),))
    gif = tmp_path / "sheet.gif"
    Image.new("RGB", (1672, 941), "black").save(gif, "GIF")
    with pytest.raises(VideoRuleViolation, match="shot_panel_source_invalid"):
        _distances(gif, ((1, 6),))
