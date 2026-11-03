from __future__ import annotations

from uuid import uuid4

import pytest

from tubeflow_test.domain.storyboards import (
    DirectorShot,
    ShotSentenceLink,
    shot_cut_point_text,
    shot_time_ranges_ms,
)
from tubeflow_test.video_prompt.domain import (
    VideoPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
    validate_compiled_prompt,
)
from tubeflow_test.video_prompt.templates import (
    FEW_SHOT_STORYBOARD_V2,
    build_system_prompt,
    build_user_payload,
)


def test_cut_point_text_formats_mm_ss_mmm() -> None:
    assert shot_cut_point_text(0) == "00:00.000"
    assert shot_cut_point_text(8700) == "00:08.700"
    assert shot_cut_point_text(65000) == "01:05.000"


def _v2_input() -> VideoPromptCompilerInput:
    shots = tuple(
        DirectorShot(
            uuid4(),
            order,
            f"scene {order}",
            "WIDE",
            "OBJECTIVE",
            f"action {order}",
            f"function {order}",
            f"description {order}",
        )
        for order in range(1, 7)
    )
    boundaries = [0, 2000, 3800, 5600, 7500, 9700, 12000]
    sentence_ids = [uuid4() for _ in range(6)]
    links = tuple(
        ShotSentenceLink(shot.shot_id, sentence_id)
        for shot, sentence_id in zip(shots, sentence_ids, strict=True)
    )
    ranges_map = {
        sentence_id: (boundaries[index], boundaries[index + 1])
        for index, sentence_id in enumerate(sentence_ids)
    }
    ranges = shot_time_ranges_ms(shots, links, ranges_map)
    return VideoPromptCompilerInput(
        ("@图一",),
        "@图二",
        shots,
        ranges,
        12,
        "16:9",
        "photorealistic rooftop night",
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2,
        "v2-key",
    )


def test_v2_system_prompt_carries_output_side_contract() -> None:
    prompt = build_system_prompt(VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2, duration_seconds=12)
    assert "Treat each panel as a separate chronological shot beat" in prompt
    assert "from left to right and top to bottom" in prompt
    assert "[Shot n]" in prompt and "cut to" in prompt
    assert "No storyboard grid" in prompt
    assert "Maintain " in prompt
    assert "fully_preserved" in prompt
    assert "禁止人物对白" in prompt
    assert "镜头块数量必须与 shot_plan 条目数完全一致" in prompt
    assert "repair_reason" in prompt and "expected/actual" in prompt
    assert "严禁照抄示例的镜头数量或编号" in prompt


def test_v2_payload_has_cut_points_and_example() -> None:
    value = _v2_input()
    payload = build_user_payload(value)
    plan = payload["shot_plan"]
    assert "cut_at" not in plan[0]
    assert plan[1]["cut_at"] == "00:02.000"
    assert plan[2]["cut_at"] == "00:03.800"
    assert plan[5]["cut_at"] == "00:09.700"
    assert payload["format_example"] == FEW_SHOT_STORYBOARD_V2


def test_v2_reference_example_passes_validation() -> None:
    """改造后的真实成功案例必须自身满足 v2 合同（绑定行插入后的完整形态）。"""
    value = _v2_input()
    validate_compiled_prompt(FEW_SHOT_STORYBOARD_V2, expected=value)


@pytest.mark.parametrize(
    "mutate, code",
    [
        (
            lambda t: t.replace(
                "Treat each panel as a separate chronological shot beat", "Treat panels"
            ),
            "h3_prompt_storyboard_framing_missing",
        ),
        (
            lambda t: t.replace("At 00:03.800, cut to", "At 00:03.700, cut to"),
            "h3_prompt_cut_point_missing",
        ),
        (lambda t: t.replace("[Shot 4]", "[Shot 5]"), "h3_prompt_shot_blocks_invalid"),
        (
            lambda t: t.replace("Maintain one facial identity", "Keep one facial identity"),
            "h3_prompt_consistency_checklist_missing",
        ),
        (lambda t: t.replace("No storyboard grid", "No grids"), "h3_prompt_negative_rules_missing"),
        (
            lambda t: t.replace("non_diegetic_music:\nN/A", "non_diegetic_music:\n112 BPM edm"),
            "h3_prompt_music_forbidden",
        ),
    ],
)
def test_v2_validation_rejects_contract_violations(mutate: object, code: str) -> None:
    value = _v2_input()
    broken = mutate(FEW_SHOT_STORYBOARD_V2)  # type: ignore[operator]
    with pytest.raises(VideoPromptViolation, match=code):
        validate_compiled_prompt(broken, expected=value)


def test_v2_shot_block_violation_exposes_expected_and_actual_orders() -> None:
    value = _v2_input()
    broken = FEW_SHOT_STORYBOARD_V2.replace("[Shot 4]", "[Shot 5]")
    with pytest.raises(VideoPromptViolation) as captured:
        validate_compiled_prompt(broken, expected=value)
    assert "expected=[1, 2, 3, 4, 5, 6]" in str(captured.value)
    assert "actual=[1, 2, 3, 5, 5, 6]" in str(captured.value)
