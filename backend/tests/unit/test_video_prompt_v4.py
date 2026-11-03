"""v4 自然运动模板：输出合同同 v3，只改运镜与画面自然度规则。"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from tubeflow_test.config import Settings
from tubeflow_test.domain.storyboards import (
    DirectorShot,
    ShotSentenceLink,
    shot_time_ranges_ms,
)
from tubeflow_test.video_prompt.domain import (
    V3_CONTRACT_STRATEGIES,
    VideoPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
    insert_binding_line,
    render_binding_line,
    validate_compiled_prompt,
)
from tubeflow_test.video_prompt.templates import (
    FEW_SHOT_STORYBOARD_V3,
    FEW_SHOT_STORYBOARD_V4,
    build_system_prompt,
    build_user_payload,
)

V4 = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION
V3 = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3


def _input(boundaries: list[int], strategy: VideoPromptStrategy = V4) -> VideoPromptCompilerInput:
    count = len(boundaries) - 1
    shots = tuple(
        DirectorShot(
            uuid4(),
            order,
            f"scene {order}",
            "MEDIUM",
            "EYE_LEVEL",
            f"action {order}",
            f"function {order}",
            f"description {order}",
        )
        for order in range(1, count + 1)
    )
    sentence_ids = [uuid4() for _ in range(count)]
    links = tuple(
        ShotSentenceLink(shot.shot_id, sentence_id)
        for shot, sentence_id in zip(shots, sentence_ids, strict=True)
    )
    ranges_map = {
        sentence_id: (boundaries[index], boundaries[index + 1])
        for index, sentence_id in enumerate(sentence_ids)
    }
    return VideoPromptCompilerInput(
        ("@图一",),
        "@图二",
        shots,
        shot_time_ranges_ms(shots, links, ranges_map),
        max(3, -(-boundaries[-1] // 1000)),
        "16:9",
        "natural live-action",
        strategy,
        "v4-key",
    )


def _example_input() -> VideoPromptCompilerInput:
    """与 FEW_SHOT_STORYBOARD_V4 对齐的 4 镜 16:9 输入（2×2，切点逐字一致）。"""
    return _input([0, 3200, 6500, 9400, 12000])


def test_v4_shares_v3_output_contract_set() -> None:
    assert {V3, V4} == set(V3_CONTRACT_STRATEGIES)


def test_config_defaults_to_v4_and_still_accepts_v3() -> None:
    assert Settings(_env_file=None).video_prompt_strategy == V4.value
    explicit_v3 = Settings(_env_file=None, video_prompt_strategy=V3.value)
    assert explicit_v3.video_prompt_strategy == V3.value
    settings = Settings(
        _env_file=None, video_prompt_strategy="tubeflow_storyboard_v4_natural_motion"
    )
    assert settings.video_prompt_strategy == V4.value


def test_v4_system_prompt_keeps_v3_contract_and_replaces_motion_rules() -> None:
    v3_prompt = build_system_prompt(V3, duration_seconds=12)
    prompt = build_system_prompt(V4, duration_seconds=12)
    # v3 合同要件保留
    for required in (
        "Treat each panel as a separate chronological shot beat",
        "from left to right and top to bottom",
        "[Shot 1] Panel 1 (row 1, column 1)",
        "cut to Panel n (row r, column c)",
        "shot_plan_reference_only",
        "subject_definitions 中只允许两类条目",
        "h3_prompt_image_provenance_forbidden",
        "镜头块数量必须与 shot_plan 条目数完全一致",
        "禁止人物对白",
        "No storyboard grid",
        "Maintain ",
        "non_diegetic_music 必须写 N/A",
    ):
        assert required in prompt, required
    # 运镜：白名单＋数值上限，替换 v3"每镜具体量化运镜"的开放写法
    assert "具体量化运镜（距离/角度/幅度数值）" in v3_prompt
    assert "具体量化运镜（距离/角度/幅度数值）" not in prompt
    assert "Static camera, 0% movement" in prompt
    assert "N 不超过 10" in prompt and "N 不超过 15" in prompt
    assert "相邻两镜不得使用同一种运镜" in prompt
    assert "orbit" in prompt and "Dutch angle" in prompt
    # 自然度与画内文字规则
    assert "real-time, not slow motion" in prompt
    assert "不得在画面中生成任何字幕" in prompt
    assert "camera shake, jitter" in prompt
    # 每条规则编号只出现一次
    for number in range(1, 21):
        assert sum(line.startswith(f"{number}. ") for line in prompt.splitlines()) == 1


def test_v4_payload_matches_v3_payload_except_example() -> None:
    v4_value = _example_input()
    v3_value = replace(v4_value, strategy=V3)
    v4_payload = build_user_payload(v4_value)
    v3_payload = build_user_payload(v3_value)
    assert v4_payload["format_example"] == FEW_SHOT_STORYBOARD_V4
    assert v3_payload["format_example"] == FEW_SHOT_STORYBOARD_V3
    assert v4_payload["shot_plan"] == v3_payload["shot_plan"]
    plan = v4_payload["shot_plan"]
    assert [entry.get("cut_at") for entry in plan] == [
        None,
        "00:03.200",
        "00:06.500",
        "00:09.400",
    ]
    assert [entry["panel"] for entry in plan] == [
        {"index": 1, "row": 1, "column": 1},
        {"index": 2, "row": 1, "column": 2},
        {"index": 3, "row": 2, "column": 1},
        {"index": 4, "row": 2, "column": 2},
    ]


def test_v4_example_passes_v3_contract_before_and_after_binding() -> None:
    value = _example_input()
    validate_compiled_prompt(FEW_SHOT_STORYBOARD_V4, expected=value)
    final = insert_binding_line(FEW_SHOT_STORYBOARD_V4, value)
    validate_compiled_prompt(final, expected=value)
    assert final.splitlines()[1] == render_binding_line(value)
    assert "(identity reference only)" in render_binding_line(value)


def test_v4_example_follows_its_own_motion_rules() -> None:
    detailed = FEW_SHOT_STORYBOARD_V4.split("detailed_description:")[1].split(
        "overall_soundscape:"
    )[0]
    shot_lines = [line for line in detailed.splitlines() if line.startswith("[Shot ")]
    assert len(shot_lines) == 4
    moves = []
    for line in shot_lines:
        found = [
            move
            for move in ("Static camera", "Slow push-in", "Slow pull-out", "Slow pan", "Slow tilt")
            if move in line
        ]
        assert len(found) == 1, line
        moves.append(found[0])
    assert all(left != right for left, right in zip(moves, moves[1:], strict=False))
    for banned in ("orbit", "Dutch", "whip", "handheld"):
        assert banned not in detailed


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda text: text.replace(
                "- Picture 2 — shot_plan_reference_only:", "- Picture 2 — fully_preserved:"
            ),
            "h3_prompt_storyboard_retention_invalid",
        ),
        (
            lambda text: text.replace("Panel 3 (row 2, column 1)", "Panel 3 (row 1, column 3)"),
            "h3_prompt_panel_mapping_missing",
        ),
        (
            lambda text: text.replace(
                "- (S1): The shop owner,",
                "- (S1): The shop owner from Picture 1,",
            ),
            "h3_prompt_image_provenance_forbidden",
        ),
        (
            lambda text: text.replace("At 00:06.500, cut to", "At 00:06.000, cut to"),
            "h3_prompt_cut_point_missing",
        ),
        (
            lambda text: text.replace(
                "(S1) pushes the door open with one shoulder",
                "He pushes the door open with his shoulder",
            ),
            "h3_prompt_gendered_pronoun_forbidden",
        ),
    ],
)
def test_v4_enforces_v3_exit_validation(mutate: object, code: str) -> None:
    broken = mutate(FEW_SHOT_STORYBOARD_V4)  # type: ignore[operator]
    assert broken != FEW_SHOT_STORYBOARD_V4
    with pytest.raises(VideoPromptViolation, match=code):
        validate_compiled_prompt(broken, expected=_example_input())


def test_v4_subjects_defer_identity_to_reference_images() -> None:
    """The compiler never sees the images, so v4 must not let it invent appearance."""
    v3_prompt = build_system_prompt(V3, duration_seconds=12)
    prompt = build_system_prompt(V4, duration_seconds=12)
    appearance_rule = "为每个出场角色主体写一句纯外观与身份定义（五官身份、发型、体型、服装等）"
    assert appearance_rule in v3_prompt
    assert appearance_rule not in prompt
    assert "严禁为角色主体描写任何外观" in prompt
    assert "the person in character reference 1" in prompt
    assert "不得列举任何具体外观" in prompt
    assert "也只代表一个人" in prompt
    assert "extra people, duplicated characters" in prompt
    # v3 contract pieces of rule 5 / 7 survive the substitution
    assert "Treat each panel as a separate chronological shot beat" in prompt
    assert "h3_prompt_image_provenance_forbidden" in prompt


def test_v4_example_subject_has_no_invented_appearance() -> None:
    subject = FEW_SHOT_STORYBOARD_V4.split("summary:")[0]
    character_lines = [line for line in subject.splitlines() if line.startswith("- (S1)")]
    assert character_lines == [
        "- (S1): The shop owner, the person in character reference 1; reproduce that "
        "person's face, hair, body, and clothing exactly as in that image."
    ]
    retention = FEW_SHOT_STORYBOARD_V4.split("retention_analysis:")[1].split(
        "detailed_description:"
    )[0]
    assert (
        "- (S1) — fully_preserved: the same face, hair, body, and clothing as "
        "character reference 1 in every shot." in retention
    )
    for invented in ("gray", "navy", "apron", "shirt", "middle-aged", "beard"):
        assert invented not in character_lines[0]
    assert "duplicated characters" in FEW_SHOT_STORYBOARD_V4


def test_compiler_payload_strips_reference_preservation_clause() -> None:
    clause = (
        "保留原图人物特征、服装、姿势、表情、主体数量、场景布局和物品位置，"
        "仅转换画面艺术风格。"
    )
    value = replace(_example_input(), visual_style="暖色水彩插画，" + clause + "柔和光线")
    payload = build_user_payload(value)
    assert payload["visual_style"] == "暖色水彩插画，柔和光线"
    # 编译器输入指纹仍基于冻结风格原文（快照不改写）。
    assert value.visual_style.endswith("柔和光线") and clause in value.visual_style


def test_v4_rules_forbid_gendered_pronouns_in_wording() -> None:
    # 2026-11-03 用户决策 5：v4 规则明令角色只按 (Sx) 或 the person 指代。
    prompt = build_system_prompt(V4, duration_seconds=12)
    assert "he、she、him、her、his、hers" in prompt
    assert "the person" in prompt


def test_v4_example_has_no_gendered_pronouns() -> None:
    import re

    detailed = FEW_SHOT_STORYBOARD_V4.split("detailed_description:")[1].split(
        "overall_soundscape:"
    )[0]
    assert re.search(r"\b(?:he|she|him|her|his|hers)\b", detailed, re.IGNORECASE) is None
