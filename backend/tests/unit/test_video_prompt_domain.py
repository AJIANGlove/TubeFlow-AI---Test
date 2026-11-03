from __future__ import annotations

from uuid import uuid4

import pytest

from tubeflow_test.domain.storyboards import (
    DirectorShot,
    ShotSentenceLink,
    format_shot_time_label,
    shot_time_ranges_ms,
    shot_time_text,
)
from tubeflow_test.video_prompt.domain import (
    VideoPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
    insert_binding_line,
    render_binding_line,
    validate_compiled_prompt,
)


def _shot(order: int) -> DirectorShot:
    return DirectorShot(
        uuid4(),
        order,
        f"scene {order}",
        "WIDE",
        "OBJECTIVE",
        f"action {order}",
        f"function {order}",
        f"description {order}",
    )


def _group(
    shot_count: int = 2,
) -> tuple[tuple[DirectorShot, ...], tuple[ShotSentenceLink, ...], dict[object, tuple[int, int]]]:
    shots = tuple(_shot(order) for order in range(1, shot_count + 1))
    sentence_ids = [uuid4() for _ in range(shot_count + 1)]
    link_list = [ShotSentenceLink(shots[0].shot_id, sentence_ids[0])]
    link_list += [
        ShotSentenceLink(shots[min(index, shot_count - 1)].shot_id, sentence_id)
        for index, sentence_id in enumerate(sentence_ids[1:], 1)
    ]
    links = tuple(link_list)
    cursor = 10_000
    ranges: dict[object, tuple[int, int]] = {}
    for sentence_id in sentence_ids:
        ranges[sentence_id] = (cursor, cursor + 3_000)
        cursor += 3_000
    return shots, links, ranges


def test_shot_time_ranges_are_group_relative_and_share_boundaries() -> None:
    shots, links, ranges = _group(2)
    values = shot_time_ranges_ms(shots, links, ranges)  # type: ignore[arg-type]
    assert [(value.shot_order, value.start_ms, value.end_ms) for value in values] == [
        (1, 0, 3000),
        (2, 3000, 9000),
    ]
    assert format_shot_time_label(values[0]) == "SHOT 1 · 0.0s–3.0s"
    assert format_shot_time_label(values[1]) == "SHOT 2 · 3.0s–9.0s"


def test_shot_time_ranges_single_shot() -> None:
    shot = _shot(1)
    sentence_id = uuid4()
    values = shot_time_ranges_ms(
        (shot,), (ShotSentenceLink(shot.shot_id, sentence_id),), {sentence_id: (500, 2600)}
    )
    assert values[0].start_ms == 0 and values[0].end_ms == 2100
    assert shot_time_text(values[0]) == "0.0s–2.1s"


def test_shot_time_ranges_reject_missing_link_or_range() -> None:
    shot = _shot(1)
    with pytest.raises(ValueError, match="at least one sentence"):
        shot_time_ranges_ms((shot,), (), {})
    with pytest.raises(ValueError, match="missing a measured time range"):
        shot_time_ranges_ms((shot,), (ShotSentenceLink(shot.shot_id, uuid4()),), {})


def test_tenth_second_text_rounds_half_up() -> None:
    shot = _shot(1)
    sentence_id = uuid4()
    values = shot_time_ranges_ms(
        (shot,), (ShotSentenceLink(shot.shot_id, sentence_id),), {sentence_id: (0, 3050)}
    )
    assert values[0].end_text == "3.1"
    values = shot_time_ranges_ms(
        (shot,), (ShotSentenceLink(shot.shot_id, sentence_id),), {sentence_id: (0, 3049)}
    )
    assert values[0].end_text == "3.0"


def _compiler_input(
    strategy: VideoPromptStrategy = VideoPromptStrategy.TUBEFLOW_ENHANCED_V1,
) -> VideoPromptCompilerInput:
    shots, links, ranges = _group(2)
    return VideoPromptCompilerInput(
        ("@图一",),
        "@图二",
        shots,
        shot_time_ranges_ms(shots, links, ranges),  # type: ignore[arg-type]
        9,
        "16:9",
        "storybook style",
        strategy,
        "test-key",
    )


def _valid_prompt(value: VideoPromptCompilerInput) -> str:
    labels = " ".join(
        f"SHOT {item.shot_order} · {item.start_text}s–{item.end_text}s: ..."
        for item in value.shot_ranges
    )
    return (
        "subject_definitions:\n(S1) is the hero from Picture 1; the plan is Picture 2.\n"
        "summary: One line.\n"
        "retention_analysis: One line.\n"
        f"detailed_description: [0.0s–9.0s] {labels}\n"
        "overall_soundscape: rain and wind.\n"
        "non_diegetic_music: N/A"
    )


def test_validate_accepts_and_binding_line_inserts() -> None:
    value = _compiler_input()
    final = insert_binding_line(_valid_prompt(value), value)
    validate_compiled_prompt(final, expected=value)
    binding = render_binding_line(value)
    assert binding.startswith("visual_asset_bindings: ")
    assert "@图一" in binding and "@图二" in binding
    assert final.splitlines()[1] == binding


def test_validate_v6_port_allows_free_beats() -> None:
    value = _compiler_input(VideoPromptStrategy.KINESTACK_V6_PORT)
    text = _valid_prompt(value).replace(
        "SHOT 1 · 0.0s–3.0s: ... SHOT 2 · 3.0s–9.0s: ...", "At 0s ... At 3s ..."
    )
    validate_compiled_prompt(text, expected=value)


@pytest.mark.parametrize(
    "mutate, code",
    [
        (
            lambda t: t.replace("non_diegetic_music: N/A", "non_diegetic_music: piano"),
            "h3_prompt_music_forbidden",
        ),
        (lambda t: t.replace("[0.0s–9.0s]", "[0.0s–8.0s]"), "h3_prompt_timeline_incomplete"),
        (lambda t: t.replace("Picture 1", "image 1"), "h3_prompt_reference_missing"),
        (
            lambda t: t.replace("SHOT 2 · 3.0s–9.0s", "SHOT 2 · 3.0s–8.9s"),
            "h3_prompt_shot_range_missing",
        ),
        (lambda t: t.replace("summary:", "synopsis:"), "h3_prompt_structure_invalid"),
        (lambda t: t + "\n<d>hi</d>", "h3_prompt_dialogue_forbidden"),
        (lambda t: t.replace("summary:", "summary:\nsummary:"), "h3_prompt_structure_invalid"),
    ],
)
def test_validate_rejects_contract_violations(mutate: object, code: str) -> None:
    value = _compiler_input()
    broken = mutate(_valid_prompt(value))  # type: ignore[operator]
    with pytest.raises(VideoPromptViolation, match=code):
        validate_compiled_prompt(broken, expected=value)


def test_validate_rejects_overlong_prompt() -> None:
    value = _compiler_input()
    with pytest.raises(VideoPromptViolation, match="h3_prompt_too_long"):
        validate_compiled_prompt(_valid_prompt(value) + "x" * 20000, expected=value)


def test_compiler_input_fingerprint_is_stable_and_sensitive() -> None:
    first = _compiler_input()
    same = VideoPromptCompilerInput(
        first.character_labels,
        first.storyboard_label,
        first.shots,
        first.shot_ranges,
        first.duration_seconds,
        first.aspect_ratio,
        first.visual_style,
        first.strategy,
        "another-key",
    )
    assert same.fingerprint == first.fingerprint
    changed = VideoPromptCompilerInput(
        first.character_labels,
        first.storyboard_label,
        first.shots,
        first.shot_ranges,
        10,
        first.aspect_ratio,
        first.visual_style,
        first.strategy,
        "k",
    )
    assert changed.fingerprint != first.fingerprint


def test_compiler_input_rejects_incomplete_facts() -> None:
    value = _compiler_input()
    with pytest.raises(VideoPromptViolation, match="input_incomplete"):
        VideoPromptCompilerInput(
            (),
            value.storyboard_label,
            value.shots,
            value.shot_ranges,
            value.duration_seconds,
            value.aspect_ratio,
            value.visual_style,
            value.strategy,
            "k",
        )
    with pytest.raises(VideoPromptViolation, match="duration_out_of_range"):
        VideoPromptCompilerInput(
            value.character_labels,
            value.storyboard_label,
            value.shots,
            value.shot_ranges,
            16,
            value.aspect_ratio,
            value.visual_style,
            value.strategy,
            "k",
        )
