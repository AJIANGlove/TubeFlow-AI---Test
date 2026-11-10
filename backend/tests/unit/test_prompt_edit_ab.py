"""§54 motion naturalness (2026-11-09): the manual-H3 edited-prompt experiment arm."""

from __future__ import annotations

# ruff: noqa: E501
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_jitter_cadence_ab import (
    CADENCE,
    GRID_V2,
    SEED,
    TITLE54,
    V4,
    _baseline,
    _client,
    _post,
    _source,
    _v4_value,
    _writer,
    replace_namespace,
)
from test_single_panel_prompt import Provider, _valid
from test_video_content_qc_ladder import RecordingCompiler

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_generation.api import ManualH3Input
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.orchestration import (
    EXPERIMENT_ARM_BASELINE,
    EXPERIMENT_ARM_CADENCE,
    EXPERIMENT_ARM_EDITED,
    PROMPT_EDIT_AB_V1,
    derive_cadence_variant_prompt,
    derive_edited_variant_prompt,
    experiment_block,
    prepare_manual_video_intent,
)
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import (
    NON_COMPILABLE_STRATEGIES,
    SINGLE_PANEL_STRATEGIES,
    V4_SINGLE_PANEL_STRATEGIES,
    VideoPromptStrategy,
    VideoPromptViolation,
)

EDITED = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_SINGLE_PANEL_EDITED_V1
# Round-2 style edit (Pending issues/2026-11-09/MOTION_NATURALNESS): name what else moves.
MOTION = " The two crew members keep rowing; the boat rocks gently and the water ripples around the hull."


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _with_motion(text: str) -> str:
    out = text.replace("with a measured push-in.", "with a measured push-in." + MOTION, 1)
    assert out != text
    return out


def _production(source):  # type: ignore[no-untyped-def]
    """A production-like source attempt: compiled v4 prompt, no experiment block."""
    attempt_id, snapshot, request = _baseline(source, key="production-g116")
    snapshot.pop("experiment")
    return attempt_id, snapshot, request


def _derive(source, source_id: UUID, snapshot: dict[str, object], text: str, **changes: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "attempt_id": uuid4(),
        "idempotency_key": "edit-g116-m1-s1",
        "seed": SEED,
        "prompt_source_attempt_id": source_id,
        "source_snapshot": snapshot,
        "edited_prompt": text,
        "edited_prompt_sha256": _sha(text),
        **changes,
    }
    return derive_edited_variant_prompt(source, **arguments)  # type: ignore[arg-type]


# --- strategy -------------------------------------------------------------------------------


def test_edited_strategy_is_v4_single_panel_but_never_compiled() -> None:
    assert EDITED in SINGLE_PANEL_STRATEGIES
    assert EDITED in V4_SINGLE_PANEL_STRATEGIES
    assert frozenset({EDITED}) == NON_COMPILABLE_STRATEGIES
    source, _repository, _service = _source()
    value = _v4_value(source)
    provider = Provider([_valid(value)])
    compiler = VideoPromptCompiler(provider, model="fake", strategy=V4)  # type: ignore[arg-type]
    with pytest.raises(VideoPromptViolation, match="h3_prompt_strategy_not_compilable"):
        compiler.compile(replace(value, strategy=EDITED))
    assert provider.payloads == []


# --- experiment identity ----------------------------------------------------------------------


def test_experiment_block_for_the_edited_arm() -> None:
    source_id, sha = uuid4(), "a" * 64
    assert experiment_block(
        EXPERIMENT_ARM_EDITED, source_id, variant_label="m1-crew_water", edited_prompt_sha256=sha
    ) == {
        "name": PROMPT_EDIT_AB_V1,
        "arm": "edited_v1",
        "prompt_source_attempt_id": str(source_id),
        "variant_label": "m1-crew_water",
        "edited_prompt_sha256": sha,
    }


@pytest.mark.parametrize(
    ("arm", "has_source", "label", "sha"),
    [
        ("edited_v1", False, "m1", "a" * 64),
        ("edited_v1", True, None, "a" * 64),
        ("edited_v1", True, "m1", None),
        ("edited_v1", True, "M1", "a" * 64),
        ("edited_v1", True, "-m1", "a" * 64),
        ("edited_v1", True, "m" * 49, "a" * 64),
        ("edited_v1", True, "m1", "A" * 64),
        ("edited_v1", True, "m1", "a" * 63),
        ("baseline", False, "m1", None),
        ("baseline", False, None, "a" * 64),
        ("cadence_v1", True, "m1", None),
        ("cadence_v1", True, None, "a" * 64),
    ],
)
def test_experiment_block_rejects_malformed_edited_identities(
    arm: str, has_source: bool, label: str | None, sha: str | None
) -> None:
    with pytest.raises(VideoRuleViolation, match="experiment_request_invalid"):
        experiment_block(
            arm,
            uuid4() if has_source else None,
            variant_label=label,
            edited_prompt_sha256=sha,
        )


def test_the_older_arm_shapes_are_unchanged() -> None:
    assert set(experiment_block(EXPERIMENT_ARM_BASELINE, None)) == {
        "name",
        "arm",
        "prompt_source_attempt_id",
    }
    assert set(experiment_block(EXPERIMENT_ARM_CADENCE, uuid4())) == {
        "name",
        "arm",
        "prompt_source_attempt_id",
    }


# --- derivation -------------------------------------------------------------------------------


def test_unchanged_text_replays_the_production_request_exactly() -> None:
    source, _repository, _service = _source()
    source_id, snapshot, production_request = _production(source)
    text = str(snapshot["video_prompt"]["text"])  # type: ignore[index]
    attempt_id, key = uuid4(), "edit-g116-r0-s1"
    compiled = _derive(source, source_id, snapshot, text, attempt_id=attempt_id, idempotency_key=key)
    assert compiled.text == text
    assert compiled.strategy is EDITED
    assert compiled.model == "deepseek-test"
    assert compiled.provider_receipt == f"edited-from-attempt:{source_id}"
    assert compiled.repaired is False
    edited_snapshot, _sha, request, _request_sha = prepare_manual_video_intent(
        source, attempt_id, compiled=compiled, idempotency_key=key, seed=SEED
    )
    assert edited_snapshot["video_prompt"]["strategy"] == EDITED.value
    assert request["prompt"] == production_request["prompt"]
    assert request["parameters"] == production_request["parameters"]
    assert request["references"] == production_request["references"]
    assert request["workflow"] == production_request["workflow"]


def test_an_edited_text_and_another_seed_change_only_the_prompt_and_seed() -> None:
    source, _repository, _service = _source()
    source_id, snapshot, production_request = _production(source)
    text = _with_motion(str(snapshot["video_prompt"]["text"]))  # type: ignore[index]
    attempt_id, key = uuid4(), "edit-g116-m1-s2"
    compiled = _derive(
        source, source_id, snapshot, text, attempt_id=attempt_id, idempotency_key=key, seed=SEED + 1
    )
    assert compiled.text == text
    _snapshot, _sha, request, _request_sha = prepare_manual_video_intent(
        source, attempt_id, compiled=compiled, idempotency_key=key, seed=SEED + 1
    )
    assert request["prompt"] == text
    assert request["parameters"]["seed"] == SEED + 1  # type: ignore[index]
    assert {k: v for k, v in request["parameters"].items() if k != "seed"} == {  # type: ignore[union-attr]
        k: v
        for k, v in production_request["parameters"].items()  # type: ignore[union-attr]
        if k != "seed"
    }
    assert request["references"] == production_request["references"]
    assert request["workflow"] == production_request["workflow"]


def test_suppressive_wording_may_be_removed_but_the_required_rules_may_not() -> None:
    source, _repository, _service = _source()
    source_id, snapshot, _request = _production(source)
    text = str(snapshot["video_prompt"]["text"])  # type: ignore[index]
    with_stable = text.replace("with a measured push-in.", "with a measured push-in, stable camera.", 1)
    without_stable = _with_motion(text)
    _derive(source, source_id, snapshot, with_stable)
    _derive(source, source_id, snapshot, without_stable)
    for broken, code in (
        (text.replace("Maintain ", "Keep ", 1), "h3_prompt_negative_rules_missing"),
        (text.replace("No storyboard grid", "No grid", 1), "h3_prompt_negative_rules_missing"),
        (text.replace("split screen", "split view", 1), "h3_prompt_negative_rules_missing"),
    ):
        with pytest.raises(VideoRuleViolation, match=f"experiment_prompt_invalid: {code}"):
            _derive(source, source_id, snapshot, broken)


def test_cadence_and_edited_attempts_are_valid_sources() -> None:
    source, _repository, _service = _source()
    baseline_id, baseline_snapshot, _request = _baseline(source)
    cadence_id, key = uuid4(), "ab-cadence"
    cadence = derive_cadence_variant_prompt(
        source,
        attempt_id=cadence_id,
        idempotency_key=key,
        seed=SEED,
        prompt_source_attempt_id=baseline_id,
        baseline_snapshot=baseline_snapshot,
    )
    cadence_snapshot, *_rest = prepare_manual_video_intent(
        source, cadence_id, compiled=cadence, idempotency_key=key, seed=SEED
    )
    assert cadence_snapshot["video_prompt"]["strategy"] == CADENCE.value
    _derive(source, cadence_id, cadence_snapshot, cadence.text)
    edited_id = uuid4()
    edited = _derive(source, cadence_id, cadence_snapshot, cadence.text, attempt_id=edited_id)
    edited_snapshot, *_rest = prepare_manual_video_intent(
        source, edited_id, compiled=edited, idempotency_key="edit-chain", seed=SEED
    )
    again = _derive(source, edited_id, edited_snapshot, _with_motion(edited.text))
    assert again.provider_receipt == f"edited-from-attempt:{edited_id}"


def _broken_source(snapshot: dict[str, object], change: str) -> dict[str, object]:
    value = {**snapshot, "video_prompt": dict(snapshot["video_prompt"])}  # type: ignore[arg-type]
    if change == "no_prompt":
        value.pop("video_prompt")
    elif change == "v3_strategy":
        value["video_prompt"]["strategy"] = "tubeflow_storyboard_v3_single_panel_opt_in_v1"  # type: ignore[index]
    elif change == "no_text":
        value["video_prompt"].pop("text")  # type: ignore[union-attr]
    elif change == "other_references":
        value["reference_input_sha256"] = "0" * 64
    elif change == "other_authority":
        value["authority"] = {"changed": True}
    return value


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("no_prompt", "experiment_prompt_source_invalid"),
        ("v3_strategy", "experiment_prompt_source_invalid"),
        ("no_text", "experiment_prompt_source_invalid"),
        ("other_references", "video_reference_input_changed"),
        ("other_authority", "video_reference_input_changed"),
    ],
)
def test_the_source_must_be_a_v4_attempt_on_the_same_inputs(change: str, code: str) -> None:
    source, _repository, _service = _source()
    source_id, snapshot, _request = _production(source)
    text = str(snapshot["video_prompt"]["text"])  # type: ignore[index]
    with pytest.raises(VideoRuleViolation, match=code):
        _derive(source, source_id, _broken_source(snapshot, change), text)


def test_the_request_must_carry_the_exact_text_and_a_seed() -> None:
    source, _repository, _service = _source()
    source_id, snapshot, _request = _production(source)
    text = str(snapshot["video_prompt"]["text"])  # type: ignore[index]
    with pytest.raises(VideoRuleViolation, match="experiment_edited_prompt_hash_mismatch"):
        _derive(source, source_id, snapshot, _with_motion(text), edited_prompt_sha256=_sha(text))
    with pytest.raises(VideoRuleViolation, match="experiment_request_invalid"):
        _derive(source, source_id, snapshot, text, seed=None)


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda t: t.replace("visual_asset_bindings:", "bindings:", 1), "h3_prompt_binding_line_invalid"),
        (lambda t: t.replace("[Shot 2]", "[Shot 3]", 1), "h3_prompt_single_panel_mapping_missing"),
        (lambda t: t.replace("the investigator studies", "she studies", 1), "h3_prompt_gendered_pronoun_forbidden"),
        (lambda t: t.replace("measured push-in.", "measured push-in toward <Picture 3>.", 1), "h3_prompt_cross_shot_reference_forbidden"),
        (lambda t: t.replace("non_diegetic_music: N/A", "non_diegetic_music: piano", 1), "h3_prompt_music_forbidden"),
        (lambda t: t + "x" * 20000, "h3_prompt_too_long"),
    ],
)
def test_the_edited_text_passes_the_full_v4_single_panel_contract(edit, code: str) -> None:  # type: ignore[no-untyped-def]
    source, _repository, _service = _source()
    source_id, snapshot, _request = _production(source)
    text = edit(str(snapshot["video_prompt"]["text"]))  # type: ignore[index]
    with pytest.raises(VideoRuleViolation, match=f"experiment_prompt_invalid: {code}"):
        _derive(source, source_id, snapshot, text)


def test_edited_arm_only_runs_on_crop_grid_v2_groups() -> None:
    source, _repository, _service = _source(GRID_V2)
    source_id, snapshot, _request = _production(source)
    text = str(snapshot["video_prompt"]["text"])  # type: ignore[index]
    other, _repository, _service = _source(TITLE54)
    with pytest.raises(VideoRuleViolation, match="experiment_requires_v4_single_panel"):
        _derive(other, source_id, snapshot, text)


def test_edited_attempts_never_take_the_auto_paths() -> None:
    source, repository, service = _source()
    service._prompt_compiler = RecordingCompiler(V4)
    attempt = service.trigger(source.owner_id, source.group_id, "auto-edited")
    repository.groups[source.group_id] = replace(source, production_mode="AUTO")
    assert service._auto_mode(attempt) is True
    edited = replace(
        attempt,
        input_snapshot={
            **(attempt.input_snapshot or {}),
            "experiment": experiment_block(
                EXPERIMENT_ARM_EDITED, uuid4(), variant_label="m1", edited_prompt_sha256="a" * 64
            ),
        },
    )
    assert service._auto_mode(edited) is False


# --- manual writer ----------------------------------------------------------------------------


def test_writer_validates_the_edited_text_against_its_source_row_without_compiling() -> None:
    source, _repository, _service = _source()
    source_id, snapshot, _request = _production(source)
    text = _with_motion(str(snapshot["video_prompt"]["text"]))  # type: ignore[index]
    row = SimpleNamespace(
        group_id=source.group_id,
        generation_id=source.storyboard_generation_id,
        input_snapshot_json=snapshot,
    )
    compiler = RecordingCompiler(V4)
    compiled = _writer([row], compiler)._prompt(
        source,
        source.owner_id,
        source.storyboard_generation_id,
        uuid4(),
        "k",
        SEED + 1,
        source_id,
        True,
        edited_prompt=text,
        edited_prompt_sha256=_sha(text),
    )
    assert compiled.strategy is EDITED
    assert compiled.text == text
    assert compiler.calls == []
    for other in (
        None,
        replace_namespace(row, group_id=uuid4()),
        replace_namespace(row, generation_id=uuid4()),
    ):
        with pytest.raises(DomainError, match="prompt source attempt is not on this group"):
            _writer([other], compiler)._prompt(
                source,
                source.owner_id,
                source.storyboard_generation_id,
                uuid4(),
                "k",
                SEED,
                source_id,
                True,
                edited_prompt=text,
                edited_prompt_sha256=_sha(text),
            )
    assert compiler.calls == []


def test_writer_rejects_mismatched_edit_fields_before_any_work() -> None:
    writer = _writer([])
    common = {
        "owner_id": uuid4(),
        "group_id": uuid4(),
        "expected_storyboard_generation_id": uuid4(),
        "idempotency_key": "k",
        "seed": SEED,
    }
    with pytest.raises(DomainError, match="edited prompt requires the edited_v1 arm"):
        writer.create(**common, edited_prompt="x")  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="edited prompt requires the edited_v1 arm"):
        writer.create(  # type: ignore[arg-type]
            **common, experiment_arm="cadence_v1", prompt_source_attempt_id=uuid4(), edited_prompt="x"
        )
    with pytest.raises(DomainError, match="edited prompt requires the edited_v1 arm"):
        writer.create(**common, experiment_arm="edited_v1", prompt_source_attempt_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(VideoRuleViolation, match="experiment_request_invalid"):
        writer.create(  # type: ignore[arg-type]
            **common,
            experiment_arm="edited_v1",
            prompt_source_attempt_id=uuid4(),
            edited_prompt="x",
            edited_prompt_sha256=_sha("x"),
            variant_label="Bad Label",
        )


@pytest.mark.parametrize(
    ("requested", "conflict"),
    [("same", False), ("other_text", True), ("other_label", True), ("cadence", True), (None, True)],
)
def test_writer_idempotency_compares_the_edited_identity(requested: str | None, conflict: bool) -> None:
    group, generation, existing_id, source_id = uuid4(), uuid4(), uuid4(), uuid4()
    blocks = {
        None: None,
        "same": experiment_block(EXPERIMENT_ARM_EDITED, source_id, variant_label="m1", edited_prompt_sha256=_sha("a")),
        "other_text": experiment_block(EXPERIMENT_ARM_EDITED, source_id, variant_label="m1", edited_prompt_sha256=_sha("b")),
        "other_label": experiment_block(EXPERIMENT_ARM_EDITED, source_id, variant_label="m2", edited_prompt_sha256=_sha("a")),
        "cadence": experiment_block(EXPERIMENT_ARM_CADENCE, source_id),
    }
    existing = SimpleNamespace(
        attempt_id=existing_id,
        group_id=group,
        generation_id=generation,
        input_snapshot_json={"manual_request": {}, "seed": SEED, "experiment": blocks["same"]},
    )
    writer = _writer([uuid4(), "ACTIVE", existing])
    call = lambda: writer._create_once(  # noqa: E731
        owner_id=uuid4(),
        group_id=group,
        expected_storyboard_generation_id=generation,
        idempotency_key="k",
        request=None,
        compiled=None,
        attempt_id=uuid4(),
        seed=SEED,
        experiment=blocks[requested],
    )
    if conflict:
        with pytest.raises(DomainError, match="bound to another request"):
            call()
    else:
        assert call() == existing_id


# --- HTTP contract ----------------------------------------------------------------------------


def _edited_body(text: str = "edited prompt", **changes: object) -> dict[str, object]:
    return {
        "expected_storyboard_generation_id": str(uuid4()),
        "seed": SEED,
        "experiment_arm": "edited_v1",
        "prompt_source_attempt_id": str(uuid4()),
        "edited_prompt": text,
        "edited_prompt_sha256": _sha(text),
        "variant_label": "m1-crew_water",
        **changes,
    }


def test_input_model_accepts_the_edited_arm() -> None:
    body = ManualH3Input.model_validate(_edited_body())
    assert body.experiment_arm == "edited_v1"
    assert body.edited_prompt == "edited prompt"
    plain = ManualH3Input.model_validate({"expected_storyboard_generation_id": str(uuid4())})
    assert (plain.edited_prompt, plain.edited_prompt_sha256, plain.variant_label) == (None, None, None)


@pytest.mark.parametrize(
    "changes",
    [
        {"edited_prompt": None},
        {"edited_prompt_sha256": None},
        {"variant_label": None},
        {"prompt_source_attempt_id": None},
        {"seed": None},
        {"edited_prompt": ""},
        {"edited_prompt": "x" * 20001},
        {"edited_prompt_sha256": "A" * 64},
        {"edited_prompt_sha256": "a" * 63},
        {"variant_label": "Bad Label"},
        {"variant_label": "m" * 49},
        {"experiment_arm": "baseline", "prompt_source_attempt_id": None},
        {"experiment_arm": "cadence_v1"},
        {"experiment_arm": None, "prompt_source_attempt_id": None},
    ],
)
def test_input_model_rejects_malformed_edited_bodies(changes: dict[str, object]) -> None:
    body = {key: value for key, value in _edited_body(**changes).items() if value is not None}
    with pytest.raises(ValidationError):
        ManualH3Input.model_validate(body)


def test_http_passes_the_edited_fields_and_keeps_older_arms_unchanged() -> None:
    client, writer = _client()
    body = _edited_body()
    body.pop("expected_storyboard_generation_id")
    response = _post(client, body)
    assert response.status_code == 202, response.text
    assert writer.seen["experiment_arm"] == "edited_v1"
    assert writer.seen["edited_prompt"] == "edited prompt"
    assert writer.seen["edited_prompt_sha256"] == _sha("edited prompt")
    assert writer.seen["variant_label"] == "m1-crew_water"
    _post(client, {"seed": SEED, "experiment_arm": "cadence_v1", "prompt_source_attempt_id": str(uuid4())})
    assert "edited_prompt" not in writer.seen
    _post(client, {"seed": SEED})
    assert "experiment_arm" not in writer.seen and "edited_prompt" not in writer.seen


def test_http_rejects_a_malformed_edited_body_with_422() -> None:
    client, writer = _client()
    body = _edited_body(edited_prompt_sha256="A" * 64)
    body.pop("expected_storyboard_generation_id")
    response = _post(client, body)
    assert response.status_code == 422, response.text
    assert writer.seen == {}
