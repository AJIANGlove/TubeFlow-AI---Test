from __future__ import annotations

from uuid import uuid4

import pytest

from tubeflow_test.application.storyboard_prompt import (
    apply_prompt_safety,
    build_grid_sheet_prompt,
)
from tubeflow_test.domain.storyboards import (
    DirectorShot,
    GridLayout,
    ShotTimeRange,
    grid_layout_for,
)


def _shot(order: int) -> DirectorShot:
    return DirectorShot(
        uuid4(),
        order,
        f"scene-{order}",
        "WIDE",
        "EYE_LEVEL",
        f"action-{order}",
        "铺垫",
        f"description-{order}",
    )


def _shots(count: int) -> tuple[DirectorShot, ...]:
    return tuple(_shot(order) for order in range(1, count + 1))


def _ranges(count: int) -> tuple[ShotTimeRange, ...]:
    return tuple(
        ShotTimeRange(order, (order - 1) * 3000, order * 3000) for order in range(1, count + 1)
    )


def _prompt(count: int, aspect_ratio: str = "16:9", character_count: int = 0) -> str:
    return build_grid_sheet_prompt(
        style="ink",
        aspect_ratio=aspect_ratio,
        shots=_shots(count),
        ranges=_ranges(count),
        character_count=character_count,
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, GridLayout(1, 1, ())),
        (2, GridLayout(1, 2, ())),
        (3, GridLayout(1, 3, ())),
        (4, GridLayout(2, 2, ())),
        (5, GridLayout(2, 3, (6,))),
        (6, GridLayout(2, 3, ())),
    ],
)
def test_grid_layout_landscape(count: int, expected: GridLayout) -> None:
    assert grid_layout_for(count, "16:9") == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, GridLayout(1, 1, ())),
        (2, GridLayout(2, 1, ())),
        (3, GridLayout(3, 1, ())),
        (4, GridLayout(2, 2, ())),
        (5, GridLayout(3, 2, (6,))),
        (6, GridLayout(3, 2, ())),
    ],
)
def test_grid_layout_portrait(count: int, expected: GridLayout) -> None:
    assert grid_layout_for(count, "9:16") == expected


def test_grid_layout_defaults_to_landscape_when_aspect_ratio_unset() -> None:
    assert grid_layout_for(2, "") == GridLayout(1, 2, ())


@pytest.mark.parametrize(
    ("count", "aspect_ratio"),
    [(0, "16:9"), (7, "16:9"), (2, "1:1"), (2, "4:3")],
)
def test_grid_layout_rejects_unsupported_input(count: int, aspect_ratio: str) -> None:
    with pytest.raises(ValueError):
        grid_layout_for(count, aspect_ratio)


def test_prompt_declares_grid_layout_and_reading_order() -> None:
    prompt = _prompt(3)
    assert "strict grid of 1 row and 3 columns" in prompt
    assert "left-to-right, top-to-bottom" in prompt
    assert "Panel 2 (row 1, column 2): WIDE, EYE_LEVEL." in prompt


def test_prompt_portrait_transposes_grid() -> None:
    prompt = _prompt(3, aspect_ratio="9:16")
    assert "strict grid of 3 rows and 1 column" in prompt
    assert "Panel 3 (row 3, column 1)" in prompt


def test_prompt_marks_empty_cell_for_five_panels() -> None:
    prompt = _prompt(5)
    assert "strict grid of 2 rows and 3 columns" in prompt
    assert "Panel 6 must stay completely black and empty." in prompt


def test_prompt_six_panels_use_full_grid_without_blank_panel() -> None:
    """§24（2026-10-14）：6 格为 2×3 整格全用、不设留黑；Panel 6 正常声明。"""
    prompt = _prompt(6)
    assert "strict grid of 2 rows and 3 columns" in prompt
    assert "Panel 6 (row 2, column 3): WIDE, EYE_LEVEL." in prompt
    assert "must stay completely black and empty" not in prompt


def test_prompt_six_panels_portrait_uses_three_by_two() -> None:
    prompt = _prompt(6, aspect_ratio="9:16")
    assert "strict grid of 3 rows and 2 columns" in prompt
    assert "Panel 6 (row 3, column 2)" in prompt
    assert "must stay completely black and empty" not in prompt


def test_prompt_single_panel_has_no_grid() -> None:
    prompt = _prompt(1)
    assert "single full-frame panel with no grid lines" in prompt
    assert "Panel 1 (row 1, column 1)" in prompt


def test_prompt_keeps_caption_labels_verbatim() -> None:
    prompt = _prompt(2)
    assert 'Caption: "SHOT 1 · 0.0s–3.0s".' in prompt
    assert 'Caption: "SHOT 2 · 3.0s–6.0s".' in prompt
    assert "print its caption text exactly as quoted" in prompt
    assert "must not contain any other text" in prompt


def test_prompt_drops_nonvisual_fields() -> None:
    prompt = _prompt(2)
    assert "narrative_function" not in prompt
    assert "铺垫" not in prompt
    assert "sentences=" not in prompt


def test_prompt_binds_reference_characters_globally() -> None:
    prompt = _prompt(2, character_count=2)
    assert "Reference images 1 to 2 are the story characters" in prompt
    assert "identical face, hairstyle and clothing in every panel" in prompt
    # 参考图只作身份参考，不得保留其姿势、构图与布局（2026-11-03 已接受修复方向）
    assert "Use the reference images only for identity" in prompt
    assert "Do not copy their pose, gesture, facial expression, framing" in prompt
    assert "take priority over every non-identity feature" in prompt
    assert prompt.index("only for identity") < prompt.index("Panel 1 (row 1, column 1)")
    assert "is still one person" in prompt


def test_prompt_omits_character_binding_without_references() -> None:
    assert "Reference images" not in _prompt(2)


def test_prompt_embeds_style_and_consistency_directives() -> None:
    prompt = _prompt(2)
    assert "Art style for every panel: ink." in prompt
    assert "consistent across all panels" in prompt


def test_prompt_safety_applies_minimal_auditable_replacements() -> None:
    prompt = (
        "Panel 1: the seventeen-year-old bride receives ceremonial care. "
        "Panel 2: 17岁的少女站在门前。 "
        "Panel 3: a disturbing fungal consequence spreads across the wall. "
        "Panel 4: attendants prepare the being bathed ritual. "
        'Caption: "SHOT 1 · 0.0s–3.0s".'
    )

    sanitized, applied = apply_prompt_safety(prompt)

    assert "seventeen-year-old" not in sanitized
    assert "the bride receives ceremonial care" in sanitized
    assert "17岁" not in sanitized and "少女站在门前" in sanitized
    assert "disturbing" not in sanitized
    assert "fungal consequence" in sanitized
    assert "being bathed" not in sanitized
    assert "receiving ceremonial cleansing" in sanitized
    assert 'Caption: "SHOT 1 · 0.0s–3.0s".' in sanitized
    assert applied == (
        "age_marker_hyphenated",
        "age_marker_zh",
        "body_ritual_wording",
        "sensational_wording",
    )


def test_prompt_safety_is_deterministic_idempotent_and_quiet_when_clean() -> None:
    prompt = _prompt(2)
    sanitized, applied = apply_prompt_safety(prompt)

    assert sanitized == prompt and applied == ()
    assert apply_prompt_safety(sanitized) == (sanitized, ())
    first = apply_prompt_safety("a 15-year-old witness and a bathed attendant")
    second = apply_prompt_safety("a 15-year-old witness and a bathed attendant")
    assert first == second


def test_prompt_safety_handles_plain_and_numeral_age_forms() -> None:
    sanitized, applied = apply_prompt_safety(
        "The 17 year old apprentice meets 十五岁的学徒 and a gruesome scene."
    )

    assert "17 year old" not in sanitized
    assert "十五岁" not in sanitized
    assert "gruesome" not in sanitized
    assert set(applied) == {
        "age_marker_plain",
        "age_marker_zh_numeral",
        "sensational_wording",
    }
