from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import pytest

from tubeflow_test.config import Settings
from tubeflow_test.domain.storyboards import (
    DirectorShot,
    ShotSentenceLink,
    grid_layout_for,
    panel_row_column,
    shot_cut_point_text,
    shot_time_ranges_ms,
)
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import (
    VideoPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
    insert_binding_line,
    render_binding_line,
    validate_compiled_prompt,
)
from tubeflow_test.video_prompt.provider import VideoPromptProviderResult
from tubeflow_test.video_prompt.templates import (
    FEW_SHOT_STORYBOARD_V3,
    build_system_prompt,
    build_user_payload,
)


class StubProvider:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[Mapping[str, object]] = []

    def compile(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        model: str,
        idempotency_key: str,
    ) -> VideoPromptProviderResult:
        self.calls.append(user_payload)
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return VideoPromptProviderResult(str(outcome), "receipt", "0" * 64)

    def close(self) -> None:
        pass


def _shots(shot_count: int) -> tuple[DirectorShot, ...]:
    return tuple(
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
        for order in range(1, shot_count + 1)
    )


def _input(shot_count: int, aspect_ratio: str = "16:9") -> VideoPromptCompilerInput:
    shots = _shots(shot_count)
    sentence_ids = [uuid4() for _ in range(shot_count)]
    links = tuple(
        ShotSentenceLink(shot.shot_id, sentence_id)
        for shot, sentence_id in zip(shots, sentence_ids, strict=True)
    )
    ranges_map = {
        sentence_id: (index * 2000, (index + 1) * 2000)
        for index, sentence_id in enumerate(sentence_ids)
    }
    return VideoPromptCompilerInput(
        ("@图一",),
        "@图二",
        shots,
        shot_time_ranges_ms(shots, links, ranges_map),
        max(3, shot_count * 2),
        aspect_ratio,
        "photorealistic rooftop night",
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3,
        "v3-key",
    )


def _v3_input() -> VideoPromptCompilerInput:
    """与 FEW_SHOT_STORYBOARD_V3 示例对齐的 6 镜 16:9 输入（切点逐字一致）。"""
    shots = _shots(6)
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
    return VideoPromptCompilerInput(
        ("@图一",),
        "@图二",
        shots,
        shot_time_ranges_ms(shots, links, ranges_map),
        12,
        "16:9",
        "photorealistic rooftop night",
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3,
        "v3-key",
    )


def _valid_v3_text(value: VideoPromptCompilerInput) -> str:
    layout = grid_layout_for(len(value.shots), value.aspect_ratio)
    blocks: list[str] = []
    for shot, shot_range in zip(value.shots, value.shot_ranges, strict=True):
        row, column = panel_row_column(shot.order, layout.cols)
        panel = f"Panel {shot.order} (row {row}, column {column})"
        if shot.order == 1:
            blocks.append(f"[Shot 1] {panel} opens on scene 1.")
        else:
            cut = shot_cut_point_text(shot_range.start_ms)
            blocks.append(f"[Shot {shot.order}] At {cut}, cut to {panel}, scene {shot.order}.")
    storyboard_ordinal = len(value.character_labels) + 1
    return (
        "subject_definitions:\n"
        "(S1) is the hero with a scar.\n"
        f"- Picture {storyboard_ordinal}: The multi-panel storyboard used as the shot-order, "
        "composition, action, and style reference. Treat each panel as a separate "
        "chronological shot beat, not as one composite image.\n"
        "summary: Follow the panels from left to right and top to bottom.\n"
        "retention_analysis:\n"
        "- (S1) — fully_preserved: keep the face and scar.\n"
        f"- Picture {storyboard_ordinal} — shot_plan_reference_only: retain shot order, "
        "per-panel composition and style; never reproduce the grid layout, gutters, "
        "captions, or empty panels as image content.\n"
        "detailed_description:\n"
        "Soft dawn light, gentle film grain.\n" + "\n".join(blocks) + "\n"
        "Maintain identity and style.\n"
        "No storyboard grid, panel borders, split screen, text, or watermarks.\n"
        "overall_soundscape: wind.\n"
        "non_diegetic_music: N/A"
    )


def test_config_accepts_all_known_strategies() -> None:
    # 2026-11-03 起默认为 v4（USER_DECISIONS §43）；v3 仍为合法配置。
    assert Settings(_env_file=None).video_prompt_strategy == "tubeflow_storyboard_v4_natural_motion"
    for strategy in (
        "kinestack_v6_port",
        "tubeflow_enhanced_v1",
        "tubeflow_storyboard_v2",
        "tubeflow_storyboard_v3",
    ):
        settings = Settings(_env_file=None, video_prompt_strategy=strategy)
        assert settings.video_prompt_strategy == strategy
    with pytest.raises(ValueError, match="known template version"):
        Settings(_env_file=None, video_prompt_strategy="unknown_strategy")


def test_v3_system_prompt_carries_output_side_contract() -> None:
    prompt = build_system_prompt(VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3, duration_seconds=12)
    # 保留的 v2 要件
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
    # v3 新增要件
    assert "subject_definitions 中只允许两类条目" in prompt
    assert "严禁为角色图创建" in prompt
    assert "same … as Picture N" in prompt
    assert "h3_prompt_image_provenance_forbidden" in prompt
    assert "不得换种措辞重复违规" in prompt
    assert "from Picture N" in prompt
    assert "shot_plan_reference_only" in prompt
    assert "never reproduce the grid layout, gutters, captions, or empty panels" in prompt
    assert "[Shot 1] Panel 1 (row 1, column 1)" in prompt
    assert "cut to Panel n (row r, column c)" in prompt


def test_v3_payload_has_cut_points_panels_and_example() -> None:
    value = _v3_input()
    payload = build_user_payload(value)
    plan = payload["shot_plan"]
    assert "cut_at" not in plan[0]
    assert plan[1]["cut_at"] == "00:02.000"
    assert plan[5]["cut_at"] == "00:09.700"
    assert plan[0]["panel"] == {"index": 1, "row": 1, "column": 1}
    assert plan[3]["panel"] == {"index": 4, "row": 2, "column": 1}
    assert payload["format_example"] == FEW_SHOT_STORYBOARD_V3


@pytest.mark.parametrize(
    ("shot_count", "aspect_ratio", "expected_panels"),
    [
        (1, "16:9", [(1, 1, 1)]),
        (2, "16:9", [(1, 1, 1), (2, 1, 2)]),
        (3, "16:9", [(1, 1, 1), (2, 1, 2), (3, 1, 3)]),
        (4, "16:9", [(1, 1, 1), (2, 1, 2), (3, 2, 1), (4, 2, 2)]),
        (5, "16:9", [(1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 2, 1), (5, 2, 2)]),
        (6, "16:9", [(1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 2, 1), (5, 2, 2), (6, 2, 3)]),
        (1, "9:16", [(1, 1, 1)]),
        (2, "9:16", [(1, 1, 1), (2, 2, 1)]),
        (3, "9:16", [(1, 1, 1), (2, 2, 1), (3, 3, 1)]),
        (4, "9:16", [(1, 1, 1), (2, 1, 2), (3, 2, 1), (4, 2, 2)]),
        (5, "9:16", [(1, 1, 1), (2, 1, 2), (3, 2, 1), (4, 2, 2), (5, 3, 1)]),
        (6, "9:16", [(1, 1, 1), (2, 1, 2), (3, 2, 1), (4, 2, 2), (5, 3, 1), (6, 3, 2)]),
    ],
)
def test_v3_payload_panel_positions_match_grid_layout(
    shot_count: int, aspect_ratio: str, expected_panels: list[tuple[int, int, int]]
) -> None:
    payload = build_user_payload(_input(shot_count, aspect_ratio))
    plan = payload["shot_plan"]
    assert [entry["panel"] for entry in plan] == [
        {"index": index, "row": row, "column": column}
        for index, row, column in expected_panels
    ]


def test_v3_reference_example_passes_validation() -> None:
    """v3 格式锚定示例必须自身满足 v3 合同（绑定行插入前的原始形态）。"""
    validate_compiled_prompt(FEW_SHOT_STORYBOARD_V3, expected=_v3_input())


def test_v3_binding_line_states_usage_and_validates_pre_and_post_insertion() -> None:
    value = _v3_input()
    binding = render_binding_line(value)
    assert binding == (
        "visual_asset_bindings: Character reference 1 = @图一 (identity reference only); "
        "Multi-panel storyboard shot plan = @图二 "
        "(each shot renders the content of its same-numbered panel only)."
    )
    # compiler.compile 流程在绑定行插入前后各跑一次校验，两次都必须成立。
    validate_compiled_prompt(FEW_SHOT_STORYBOARD_V3, expected=value)
    final = insert_binding_line(FEW_SHOT_STORYBOARD_V3, value)
    validate_compiled_prompt(final, expected=value)
    assert final.splitlines()[1] == binding


def test_v3_validation_uses_portrait_transposed_layout() -> None:
    value = _input(3, "9:16")
    validate_compiled_prompt(_valid_v3_text(value), expected=value)
    broken = _valid_v3_text(value).replace("Panel 2 (row 2, column 1)", "Panel 2 (row 1, column 2)")
    with pytest.raises(VideoPromptViolation, match="h3_prompt_panel_mapping_missing"):
        validate_compiled_prompt(broken, expected=value)


def test_v3_panel_mapping_violation_exposes_expected_and_actual() -> None:
    value = _v3_input()
    broken = FEW_SHOT_STORYBOARD_V3.replace("Panel 4 (row 2, column 1)", "Panel 4")
    with pytest.raises(VideoPromptViolation) as captured:
        validate_compiled_prompt(broken, expected=value)
    reason = str(captured.value)
    assert "h3_prompt_panel_mapping_missing" in reason
    assert "expected=" in reason and "actual=" in reason
    assert "Panel 4 (row 2, column 1)" in reason


@pytest.mark.parametrize(
    "phrase",
    ["from Picture 1", "shown in Picture 1", "shown throughout Picture 1"],
)
def test_v3_subject_definitions_forbid_image_provenance(phrase: str) -> None:
    value = _v3_input()
    broken = FEW_SHOT_STORYBOARD_V3.replace(
        "adult East Asian woman,", f"adult East Asian woman {phrase},"
    )
    with pytest.raises(VideoPromptViolation, match="h3_prompt_image_provenance_forbidden"):
        validate_compiled_prompt(broken, expected=value)


@pytest.mark.parametrize(
    "invalid_line",
    [
        "- Picture 1: The character reference.",
        "- (S1): The same human character as Picture 1.",
    ],
)
def test_v3_subject_definitions_reject_character_picture_entries(
    invalid_line: str,
) -> None:
    value = _v3_input()
    broken = FEW_SHOT_STORYBOARD_V3.replace(
        "- Picture 2:", f"{invalid_line}\n- Picture 2:", 1
    )
    with pytest.raises(VideoPromptViolation, match="h3_prompt_image_provenance_forbidden"):
        validate_compiled_prompt(broken, expected=value)


def test_v3_retention_rejects_storyboard_fully_preserved_entry() -> None:
    value = _v3_input()
    broken = FEW_SHOT_STORYBOARD_V3.replace(
        "- Picture 2 — shot_plan_reference_only:", "- Picture 2 — fully_preserved:"
    )
    with pytest.raises(VideoPromptViolation, match="h3_prompt_storyboard_retention_invalid"):
        validate_compiled_prompt(broken, expected=value)


def test_v3_retention_requires_shot_plan_reference_only_entry() -> None:
    value = _v3_input()
    broken = FEW_SHOT_STORYBOARD_V3.replace("shot_plan_reference_only", "shot_reference")
    with pytest.raises(VideoPromptViolation, match="h3_prompt_storyboard_retention_invalid"):
        validate_compiled_prompt(broken, expected=value)


@pytest.mark.parametrize(
    ("mutate", "code"),
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
def test_v3_validation_keeps_v2_contract_enforced(mutate: object, code: str) -> None:
    value = _v3_input()
    broken = mutate(FEW_SHOT_STORYBOARD_V3)  # type: ignore[operator]
    with pytest.raises(VideoPromptViolation, match=code):
        validate_compiled_prompt(broken, expected=value)


def test_v3_compile_repairs_panel_mapping_once_with_reason() -> None:
    value = _input(1)
    valid = _valid_v3_text(value)
    broken = valid.replace("Panel 1 (row 1, column 1)", "Panel 1")
    provider = StubProvider([broken, valid])
    compiler = VideoPromptCompiler(provider, model="m", strategy=value.strategy)
    compiled = compiler.compile(value)
    assert compiled.repaired is True
    assert len(provider.calls) == 2
    reason = str(provider.calls[1].get("repair_reason"))
    assert "h3_prompt_panel_mapping_missing" in reason
    assert "expected=" in reason and "actual=" in reason
    # 绑定行插入后的最终文本必然再次通过全量校验（compile 内部保证）。
    assert compiled.text.splitlines()[1] == render_binding_line(value)
