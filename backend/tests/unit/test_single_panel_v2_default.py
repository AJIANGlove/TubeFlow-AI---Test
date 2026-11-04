"""v4 single-panel prompt, layout-driven reference policy and AUTO ladder (USER_DECISIONS §46)."""

from __future__ import annotations

# Prompt fixture strings are data; keep their exact line structure.
# ruff: noqa: E501
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from test_h3_candidate_routing import route_session
from test_single_panel_prompt import Provider, _valid
from test_single_panel_replay import _prepared_attempt, _with_panels
from test_video_content_qc_ladder import RecordingCompiler
from test_video_orchestration import setup

from tubeflow_test.adapters.persistence.production_authority import (
    GRID_V2_REFERENCE_POLICY,
    SqlCanonicalVideoGroupLoader,
    single_panel_policy_settings,
)
from tubeflow_test.adapters.persistence.storyboards.repository import _grid_layout_version
from tubeflow_test.config import Settings
from tubeflow_test.domain.storyboards import DirectorShot, ShotTimeRange
from tubeflow_test.video_generation.domain import (
    ReferencePolicy,
    ReferenceStrategy,
    ShotPanelImage,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import (
    _authorize,
    _compiler_input,
    single_panel_strategy,
)
from tubeflow_test.video_generation.persistence import H3ProjectWorkflowRouteRecord
from tubeflow_test.video_generation.project_routing import reference_input_fingerprint
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import (
    SinglePanelPromptCompilerInput,
    SinglePanelReference,
    VideoPromptStrategy,
    VideoPromptViolation,
    insert_single_panel_binding_line,
    validate_single_panel_compiled_prompt,
)
from tubeflow_test.video_prompt.templates import (
    build_single_panel_user_payload,
    build_system_prompt,
    build_user_payload,
)

V4 = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_SINGLE_PANEL
V3 = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1
GRID_V2 = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-grid-v2"})
TITLE54 = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-title54-v1"})

# Golden values produced by main at 3af818d (before §46) for the same inputs.
GOLDEN_SYSTEM = {
    VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1: "c617daef9307f94e52dc0c998a90cdf25738e931fee6529213ae7b2932403fa9",
    VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION: "add7dbf75c101c9d08f4721e47606b11346d41e43aeecd0b06f38f3e186bd136",
    VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3: "37c016b926434682805bdb5f6c04d3c3e735667ba1d3d080e761df08ce76d6df",
}
GOLDEN_V3_PAYLOAD = "e0b8377cc08cd9c52b3066cc22d8476e96d6c431c8647b7d1c8b92a7da52d755"
GOLDEN_V3_FINGERPRINT = "5be5e3069f6baa8db33f4eea91456b900d9ca79a038f1c8f2f537f2a0d003dfe"


def _fixed_input(strategy: VideoPromptStrategy | None = None) -> SinglePanelPromptCompilerInput:
    shots = tuple(
        DirectorShot(
            UUID(int=index),
            index,
            f"scene {index}",
            "medium",
            "eye level",
            f"action {index}",
            "explain",
            f"description {index}",
        )
        for index in range(1, 4)
    )
    ranges = tuple(ShotTimeRange(index, (index - 1) * 2000, index * 2000) for index in range(1, 4))
    panels = tuple(
        SinglePanelReference(f"Picture {index + 1}", index, "a" * 64, "b" * 64, "crop-title54-v1")
        for index in range(1, 4)
    )
    extra = {} if strategy is None else {"strategy": strategy}
    return SinglePanelPromptCompilerInput(
        ("Picture 1",), panels, shots, ranges, 6, "16:9", "ink wash", "video-prompt:k", **extra
    )


# --- prompt -------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", sorted(GOLDEN_SYSTEM, key=str))
def test_existing_system_prompts_stay_byte_identical(strategy: VideoPromptStrategy) -> None:
    text = build_system_prompt(strategy, duration_seconds=12)
    assert hashlib.sha256(text.encode()).hexdigest() == GOLDEN_SYSTEM[strategy]


def test_v3_single_panel_payload_and_fingerprint_stay_byte_identical() -> None:
    value = _fixed_input()
    assert value.strategy is V3
    payload = json.dumps(build_single_panel_user_payload(value), sort_keys=True, ensure_ascii=False)
    assert hashlib.sha256(payload.encode()).hexdigest() == GOLDEN_V3_PAYLOAD
    assert value.fingerprint == GOLDEN_V3_FINGERPRINT


def test_v4_single_panel_system_prompt_combines_single_panel_and_v4_rules() -> None:
    text = build_system_prompt(V4, duration_seconds=12)
    # single-panel block prefixes, no composite panel coordinates
    assert '"[Shot 1] <Picture m>:"' in text
    assert '"[Shot n] At {cut_at}, cut to <Picture m>:"' in text
    assert "Panel n (row r, column c)" not in text
    assert "single-panel references" in text
    # v4 identity: no appearance, character reference N only
    assert "严禁为角色主体描写任何外观" in text
    assert "the person in character reference 1" in text
    assert "纯外观与身份定义" not in text
    # v4 camera whitelist, negatives and extra rules
    assert "Static camera, 0% movement" in text
    assert "duplicated characters" in text
    for rule in ("17.", "18.", "19.", "20.", "21.", "22."):
        assert f"\n{rule} " in text
    assert "同一人物在同一画面中只能出现一次" in text


def test_v4_single_panel_payload_uses_its_own_format_example() -> None:
    value = _fixed_input(V4)
    payload = build_single_panel_user_payload(value)
    assert payload["strategy"] == V4.value
    example = str(payload["format_example"])
    assert "the person in character reference 1" in example
    assert "[Shot 1] <Picture 2>:" in example
    assert value.fingerprint != _fixed_input().fingerprint


def test_composite_input_rejects_both_single_panel_strategies() -> None:
    source, *_rest = setup()
    value = _compiler_input(
        source, _authorize(source), VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3, source.generation_id
    )
    for strategy in (V3, V4):
        with pytest.raises(VideoPromptViolation, match="single_panel_reference_required"):
            build_user_payload(replace(value, strategy=strategy))


def test_single_panel_input_rejects_composite_strategy() -> None:
    with pytest.raises(VideoPromptViolation, match="single_panel_prompt_strategy_required"):
        _fixed_input(VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION)


def _v4_input():  # type: ignore[no-untyped-def]
    source, _repository, _engine, assets, _service = setup()
    source = _with_panels(source, assets, panel_count=2)
    generation = _authorize(source)
    value = _compiler_input(source, generation, V4, generation.generation_id)
    assert isinstance(value, SinglePanelPromptCompilerInput)
    assert value.strategy is V4
    return value


def test_v4_single_panel_compile_and_pronoun_rejection() -> None:
    value = _v4_input()
    compiled = VideoPromptCompiler(Provider([_valid(value)]), model="fake", strategy=V4).compile(value)  # type: ignore[arg-type]
    assert compiled.strategy is V4
    validate_single_panel_compiled_prompt(compiled.text, expected=value, binding_inserted=True)

    gendered = insert_single_panel_binding_line(_valid(value), value).replace(
        "the investigator studies", "she studies"
    )
    with pytest.raises(VideoPromptViolation, match="h3_prompt_gendered_pronoun_forbidden: she"):
        validate_single_panel_compiled_prompt(gendered, expected=value, binding_inserted=True)
    # v3 single-panel keeps its old contract: no pronoun rule.
    v3_value = replace(value, strategy=V3)
    validate_single_panel_compiled_prompt(
        insert_single_panel_binding_line(_valid(v3_value), v3_value).replace(
            "the investigator studies", "she studies"
        ),
        expected=v3_value,
        binding_inserted=True,
    )


def test_strategy_follows_the_derivation() -> None:
    assert single_panel_strategy(GRID_V2) is V4
    assert single_panel_strategy(TITLE54) is V3
    assert GRID_V2_REFERENCE_POLICY == GRID_V2


# --- reference policy follows the storyboard layout -----------------------------------


class _Derivatives:
    def __init__(self, assets) -> None:  # type: ignore[no-untyped-def]
        self.assets = assets
        self.calls: list[tuple[ReferencePolicy, str]] = []

    def derive(self, _session, value, policy, *, for_intent):  # type: ignore[no-untyped-def]
        self.calls.append((policy, value.storyboard_grid_layout_version))
        return (
            ShotPanelImage(
                self.assets.add(b"panel-v2"),
                1,
                value.storyboard_image.sha256,
                "crop-grid-v2",
            ),
        )


def _loader(assets) -> tuple[SqlCanonicalVideoGroupLoader, _Derivatives]:  # type: ignore[no-untyped-def]
    requested, allowed = single_panel_policy_settings(Settings(_env_file=None))
    derivatives = _Derivatives(assets)
    return (
        SqlCanonicalVideoGroupLoader(
            Settings(_env_file=None),
            reference_policy=requested,
            single_panel_allowed=allowed,
            panel_derivatives=derivatives,  # type: ignore[arg-type]
        ),
        derivatives,
    )


def _route(session, project_id, strategy=None, derivation=None) -> None:  # type: ignore[no-untyped-def]
    session.add(
        H3ProjectWorkflowRouteRecord(
            project_id=project_id,
            profile="candidate",
            bound_at=datetime.now(UTC),
            reference_strategy=strategy,
            reference_derivation_json=derivation,
        )
    )
    session.flush()


def test_v2_storyboard_binds_grid_v2_policy_without_allowlist() -> None:
    source, _repository, _engine, assets, _service = setup()
    source = replace(source, storyboard_grid_layout_version="grid-v2")
    loader, derivatives = _loader(assets)
    with route_session(source.project_id) as session, session.begin():
        _route(session, source.project_id)
        bound = loader.apply_reference_policy(session, source, for_intent=True)
        route = session.get(H3ProjectWorkflowRouteRecord, source.project_id)
        assert route is not None
        assert route.reference_strategy == "single_panel_v1"
        assert route.reference_derivation_json == {"version": "crop-grid-v2"}
    assert bound.reference_policy == GRID_V2
    assert len(bound.shot_panel_images) == 1
    assert derivatives.calls == [(GRID_V2, "grid-v2")]


def test_v1_storyboard_keeps_composite_by_default() -> None:
    source, _repository, _engine, assets, _service = setup()
    loader, derivatives = _loader(assets)
    with route_session(source.project_id) as session, session.begin():
        _route(session, source.project_id)
        bound = loader.apply_reference_policy(session, source, for_intent=True)
    assert bound.reference_policy is None and bound.shot_panel_images == ()
    assert derivatives.calls == []


def test_v2_storyboard_on_a_composite_route_fails_closed() -> None:
    source, _repository, _engine, assets, _service = setup()
    source = replace(source, storyboard_grid_layout_version="grid-v2")
    loader, _derivatives = _loader(assets)
    with route_session(source.project_id) as session, session.begin():
        _route(session, source.project_id, "composite", None)
        with pytest.raises(VideoRuleViolation, match="reference_policy_already_bound"):
            loader.apply_reference_policy(session, source, for_intent=True)


def test_layout_version_is_read_from_storyboard_input_json() -> None:
    assert _grid_layout_version({"prompt": "x"}) == "grid-v1"
    assert _grid_layout_version({"grid_layout_version": "grid-v2"}) == "grid-v2"
    assert _grid_layout_version(None) == "grid-v1"


# --- AUTO ladder ----------------------------------------------------------------------


def _production_attempt(policy: ReferencePolicy):  # type: ignore[no-untyped-def]
    compiler = RecordingCompiler(VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION)
    source, repository, engine, assets, service = setup()
    source = _with_panels(source, assets, panel_count=2)
    source = replace(
        source,
        reference_policy=policy,
        reference_input_sha256=reference_input_fingerprint(
            source.authority_snapshot, policy, source.shot_panel_images
        ),
        storyboard_grid_layout_version="grid-v2" if policy == GRID_V2 else "grid-v1",
    )
    repository.groups[source.group_id] = source
    service._prompt_compiler = compiler
    attempt = service.trigger(source.owner_id, source.group_id, "single-panel-ladder")
    return attempt, service, compiler, engine


def test_production_single_panel_attempt_uses_v4_and_is_resubmittable() -> None:
    attempt, service, compiler, engine = _production_attempt(GRID_V2)
    assert attempt.input_snapshot["video_prompt"]["strategy"] == V4.value  # type: ignore[index]
    panels = attempt.input_snapshot["shot_panel_assets"]  # type: ignore[index]

    chain = [attempt]
    for index in range(2, 7):
        chain.append(service._build_auto_replacement(chain[-1], next_attempt_index=index))
    seeds = [item.request["parameters"]["seed"] for item in chain]  # type: ignore[index]
    assert len(set(seeds)) == len(seeds)
    for item in chain[1:]:
        snapshot = item.input_snapshot or {}
        assert snapshot["shot_panel_assets"] == panels
        assert snapshot["reference_input_sha256"] == attempt.input_snapshot["reference_input_sha256"]  # type: ignore[index]
        assert snapshot["video_prompt"]["strategy"] == V4.value
        assert [ref["asset_sha256"] for ref in item.request["references"]] == [  # type: ignore[index]
            ref["asset_sha256"] for ref in attempt.request["references"]  # type: ignore[index]
        ]
        assert not any("degraded" in marker for marker in item.retry_evidence)
    # Steps 3, 4 and 6 recompile, all with the v4 single-panel strategy (no composite step 4).
    recompiled = [call["input"] for call in compiler.calls[1:]]
    assert len(recompiled) == 3
    assert all(isinstance(value, SinglePanelPromptCompilerInput) for value in recompiled)
    assert all(value.strategy is V4 for value in recompiled)
    assert compiler.calls[1]["repair_reason"] is None or isinstance(compiler.calls[1]["repair_reason"], str)
    assert engine.calls == []


def test_allowlisted_title54_attempt_keeps_v3_single_panel_on_the_ladder() -> None:
    attempt, service, compiler, _engine = _production_attempt(TITLE54)
    assert attempt.input_snapshot["video_prompt"]["strategy"] == V3.value  # type: ignore[index]
    replacement = service._build_auto_replacement(attempt, next_attempt_index=4)
    assert replacement.input_snapshot["video_prompt"]["strategy"] == V3.value  # type: ignore[index]
    assert compiler.calls[-1]["input"].strategy is V3  # type: ignore[union-attr]


def test_frozen_replay_is_still_not_resubmittable() -> None:
    source, _repository, engine, assets, service = setup()
    source = _with_panels(source, assets)
    with pytest.raises(VideoRuleViolation, match="single_panel_automatic_resubmit_not_supported"):
        service._build_auto_replacement(_prepared_attempt(source), next_attempt_index=2)
    assert engine.calls == []
