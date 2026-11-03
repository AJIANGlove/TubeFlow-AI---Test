from __future__ import annotations

from uuid import uuid4

from tubeflow_test.domain.storyboards import (
    DirectorShot,
    ShotSentenceLink,
    shot_time_ranges_ms,
)
from tubeflow_test.video_prompt.domain import VideoPromptCompilerInput, VideoPromptStrategy
from tubeflow_test.video_prompt.templates import (
    FEW_SHOT_FORMAT_EXAMPLE,
    build_system_prompt,
    build_user_payload,
)


def _input(strategy: VideoPromptStrategy) -> VideoPromptCompilerInput:
    shot = DirectorShot(
        uuid4(), 1, "storm plain", "WIDE", "LOW_ANGLE", "runs", "hook", "he runs"
    )
    sentence_id = uuid4()
    ranges = shot_time_ranges_ms(
        (shot,), (ShotSentenceLink(shot.shot_id, sentence_id),), {sentence_id: (0, 4000)}
    )
    return VideoPromptCompilerInput(
        ("@图一", "@图二"),
        "@图三",
        (shot,),
        ranges,
        4,
        "16:9",
        "retro storybook",
        strategy,
        "key",
    )


def test_base_system_prompt_is_strategy_agnostic_v6() -> None:
    prompt = build_system_prompt(VideoPromptStrategy.KINESTACK_V6_PORT, duration_seconds=4)
    assert "[0.0s–4.0s]" in prompt
    assert "分格画面" not in prompt
    assert "format_example" not in prompt
    assert "禁止人物对白" in prompt


def test_enhanced_system_prompt_adds_storyboard_binding_and_negatives() -> None:
    prompt = build_system_prompt(VideoPromptStrategy.TUBEFLOW_ENHANCED_V1, duration_seconds=4)
    assert "分格画面" in prompt
    assert "no split screen" in prompt
    assert "SHOT {order}" in prompt
    assert "format_example" in prompt


def test_user_payload_structure_and_shot_ranges() -> None:
    value = _input(VideoPromptStrategy.TUBEFLOW_ENHANCED_V1)
    payload = build_user_payload(value)
    assert payload["json_task"] == "compile_minimax_h3_video_prompt"
    assert payload["generation_context"] == {"duration_seconds": 4, "aspect_ratio": "16:9"}
    roles = payload["reference_roles"]
    assert [role["role"] for role in roles] == ["CHARACTER", "CHARACTER", "STORYBOARD"]
    assert roles[-1]["label"] == "@图三" and roles[-1]["picture"] == "Picture 3"
    assert payload["shot_plan"][0]["time_range"] == "0.0s–4.0s"
    assert payload["format_example"] == FEW_SHOT_FORMAT_EXAMPLE
    assert "no_dialogue" in payload["fixed_constraints"]


def test_v6_payload_omits_format_example_and_repair_reason_is_optional() -> None:
    value = _input(VideoPromptStrategy.KINESTACK_V6_PORT)
    payload = build_user_payload(value)
    assert "format_example" not in payload
    assert "repair_reason" not in payload
    repaired = build_user_payload(value, repair_reason="h3_prompt_music_forbidden")
    assert repaired["repair_reason"] == "h3_prompt_music_forbidden"


def test_v3_rules_forbid_subject_number_for_storyboard() -> None:
    """2026-10-27：故事板曾被写成 (S2)，导致 retention 行逐字校验失败。"""
    prompt = build_system_prompt(
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3, duration_seconds=5
    )

    assert '"- Picture {N}:"' in prompt
    assert "严禁为它分配 (Sx) 编号" in prompt
    assert "不得把主语写成 (Sx)" in prompt
    assert "故事板不是出场主体" in prompt


def test_v2_rules_keep_subject_numbering_unchanged() -> None:
    """v2 对照版本不得被本次收紧影响（回归保护）。"""
    prompt = build_system_prompt(
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2, duration_seconds=5
    )

    assert "严禁为它分配 (Sx) 编号" not in prompt
