from __future__ import annotations

# Prompt fixture strings are data; keep their exact line structure.
# ruff: noqa: E501
from dataclasses import replace
from uuid import uuid4

import pytest
from test_single_panel_replay import _with_panels
from test_video_orchestration import setup

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import shot_cut_point_text
from tubeflow_test.video_generation.domain import (
    ReferencePolicy,
    ReferenceStrategy,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import (
    _authorize,
    _compiler_input,
    _compiler_input_from_snapshot,
    _input_snapshot,
    prepare_manual_video_intent,
)
from tubeflow_test.video_generation.project_routing import reference_input_fingerprint
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import (
    SinglePanelPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
    insert_single_panel_binding_line,
    render_binding_line,
    validate_single_panel_compiled_prompt,
)
from tubeflow_test.video_prompt.fake_compiler import FakeVideoPromptCompiler
from tubeflow_test.video_prompt.provider import VideoPromptProviderResult
from tubeflow_test.video_prompt.templates import build_system_prompt, build_user_payload


def test_existing_v3_prompt_contract_stays_composite() -> None:
    source, _repository, _engine, _assets, _service = setup()
    generation = _authorize(source)
    value = _compiler_input(
        source,
        generation,
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3,
        generation.generation_id,
    )

    payload = build_user_payload(value)

    assert value.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3
    assert [item["role"] for item in payload["reference_roles"]] == ["CHARACTER", "STORYBOARD"]
    assert "SHOT_PANEL" not in {item["role"] for item in payload["reference_roles"]}
    assert "Multi-panel storyboard shot plan" in render_binding_line(value)


def test_composite_input_cannot_use_single_panel_strategy() -> None:
    source, _repository, _engine, _assets, _service = setup()
    value = _compiler_input(
        source,
        _authorize(source),
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3,
        source.generation_id,
    )
    invalid = replace(
        value,
        strategy=VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1,
    )
    with pytest.raises(VideoPromptViolation, match="single_panel_reference_required"):
        build_user_payload(invalid)


STRATEGY = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1


class Provider:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.payloads: list[dict[str, object]] = []

    def compile(self, *, user_payload, **_kwargs) -> VideoPromptProviderResult:  # type: ignore[no-untyped-def]
        self.payloads.append(user_payload)
        return VideoPromptProviderResult(self.outputs.pop(0), "receipt", "0" * 64)

    def close(self) -> None:
        pass


def _input():  # type: ignore[no-untyped-def]
    source, _repository, _engine, assets, _service = setup()
    source = _with_panels(source, assets, panel_count=2)
    generation = _authorize(source)
    value = _compiler_input(source, generation, STRATEGY, generation.generation_id)
    assert isinstance(value, SinglePanelPromptCompilerInput)
    return source, value


def _valid(value: SinglePanelPromptCompilerInput) -> str:
    first, second = value.panels
    definitions = "\n".join(
        f"- {panel.label}: The single-panel storyboard reference for Shot {panel.shot_order}, used as the composition, action, and style reference for Shot {panel.shot_order} only."
        for panel in value.panels
    )
    retention = "\n".join(
        f"- {panel.label} — shot_plan_reference_only: retain Shot {panel.shot_order} composition and style; never reproduce reference borders, captions, or unrelated panel content as image content."
        for panel in value.panels
    )
    cut = shot_cut_point_text(value.shot_ranges[1].start_ms)
    return (
        "subject_definitions:\n"
        "- (S1): A lantern-headed investigator with a dark cloak and one amber light.\n"
        f"{definitions}\n"
        "summary: A quiet investigation advances through the single-panel references in shot order.\n"
        "retention_analysis:\n"
        "- (S1) — fully_preserved: retain the cloak, silhouette and amber light.\n"
        f"{retention}\n"
        "detailed_description:\n"
        "Dark oil-painted illustration with restrained amber light and thick texture.\n"
        f"[Shot 1] <{first.label}>: the investigator studies one map with a measured push-in.\n"
        f"[Shot 2] At {cut}, cut to <{second.label}>: one candle flickers on the old map.\n"
        "Maintain identity, map, candle and the navy-and-amber style.\n"
        "No storyboard grid, panel borders, split screen, captions, text, logos, or watermarks.\n"
        "overall_soundscape: quiet room tone and a soft candle hiss, no voices.\n"
        "non_diegetic_music: N/A"
    )


def test_single_panel_input_payload_and_full_fake_compile() -> None:
    _source, value = _input()
    payload = build_user_payload(value)
    assert value.character_labels == ("Picture 1",)
    assert tuple(panel.label for panel in value.panels) == ("Picture 2", "Picture 3")
    assert [item["role"] for item in payload["reference_roles"]] == [
        "CHARACTER",
        "SHOT_PANEL",
        "SHOT_PANEL",
    ]
    assert payload["shot_plan"][0]["reference"] == value.panels[0].label  # type: ignore[index]
    assert "single-panel references" in build_system_prompt(STRATEGY, duration_seconds=15)

    provider = Provider([_valid(value)])
    compiled = VideoPromptCompiler(provider, model="fake", strategy=STRATEGY).compile(value)  # type: ignore[arg-type]

    assert compiled.strategy is STRATEGY
    assert compiled.input_fingerprint == value.fingerprint
    assert compiled.repaired is False
    assert "visual_asset_bindings:" in compiled.text
    validate_single_panel_compiled_prompt(compiled.text, expected=value, binding_inserted=True)
    assert len(provider.payloads) == 1


def test_single_panel_repairs_once_and_two_failures_stop() -> None:
    _source, value = _input()
    broken = _valid(value).replace("non_diegetic_music: N/A", "non_diegetic_music: piano")
    provider = Provider([broken, _valid(value)])
    compiled = VideoPromptCompiler(provider, model="fake", strategy=STRATEGY).compile(value)  # type: ignore[arg-type]
    assert compiled.repaired is True
    assert provider.payloads[1]["repair_reason"] == "h3_prompt_music_forbidden"

    provider = Provider([broken, broken])
    with pytest.raises(DomainError, match="h3_prompt_music_forbidden"):
        VideoPromptCompiler(provider, model="fake", strategy=STRATEGY).compile(value)  # type: ignore[arg-type]
    assert len(provider.payloads) == 2


def test_single_panel_composite_fallback_is_rejected() -> None:
    _source, value = _input()
    broken = _valid(value) + "\nMulti-panel storyboard shot plan"
    provider = Provider([broken, broken])
    with pytest.raises(DomainError, match="h3_prompt_composite_reference_forbidden"):
        VideoPromptCompiler(provider, model="fake", strategy=STRATEGY).compile(value)  # type: ignore[arg-type]


def test_single_panel_snapshot_restores_exact_input_and_intent() -> None:
    source, value = _input()
    policy = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-title54-v1"})
    source = replace(
        source,
        reference_policy=policy,
        reference_input_sha256=reference_input_fingerprint(
            source.authority_snapshot, policy, source.shot_panel_images
        ),
    )
    generation = _authorize(source)
    snapshot = _input_snapshot(source, generation, compiled=None)
    assert snapshot["compiler_input"]["panels"][0]["derivative_sha256"] == value.panels[0].derivative_sha256  # type: ignore[index]
    restored = _compiler_input_from_snapshot(
        snapshot, generation, strategy=STRATEGY, attempt_id=generation.generation_id
    )
    assert restored.fingerprint == value.fingerprint

    provider = Provider([_valid(value)])
    compiled = VideoPromptCompiler(provider, model="fake", strategy=STRATEGY).compile(value)  # type: ignore[arg-type]
    prepared_snapshot, generation_sha, request, request_sha = prepare_manual_video_intent(
        source, generation.generation_id, compiled=compiled
    )
    assert prepared_snapshot["video_prompt"]["strategy"] == STRATEGY.value  # type: ignore[index]
    assert generation_sha and request_sha
    assert request["references"][-1]["asset_sha256"] == value.panels[-1].derivative_sha256  # type: ignore[index]


def test_single_panel_orchestrator_uses_new_strategy_without_provider_submit() -> None:
    source, repository, engine, assets, service = setup()
    source = _with_panels(source, assets)
    policy = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-title54-v1"})
    source = replace(
        source,
        reference_policy=policy,
        reference_input_sha256=reference_input_fingerprint(
            source.authority_snapshot, policy, source.shot_panel_images
        ),
    )
    repository.groups[source.group_id] = source
    service._prompt_compiler = FakeVideoPromptCompiler(
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3
    )

    attempt = service.trigger(source.owner_id, source.group_id, "single-panel-p4")

    assert attempt.input_snapshot is not None
    assert attempt.input_snapshot["video_prompt"]["strategy"] == STRATEGY.value  # type: ignore[index]
    assert attempt.input_snapshot["compiler_input"]["panels"][0]["shot_order"] == 1  # type: ignore[index]
    assert attempt.request["references"][-1]["asset_sha256"] == source.shot_panel_images[0].asset.sha256  # type: ignore[index]
    assert engine.calls == []


def test_single_panel_cross_shot_reference_is_rejected() -> None:
    _source, value = _input()
    text = insert_single_panel_binding_line(_valid(value), value)
    broken = text.replace("one candle flickers", f"{value.panels[0].label} one candle flickers")
    with pytest.raises(VideoPromptViolation, match="h3_prompt_cross_shot_reference_forbidden"):
        validate_single_panel_compiled_prompt(broken, expected=value, binding_inserted=True)


def test_composite_strategy_is_rejected_for_single_panel_input() -> None:
    source, _value = _input()
    with pytest.raises(VideoRuleViolation, match="single_panel_prompt_strategy_required"):
        _compiler_input(source, _authorize(source), VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3, uuid4())
