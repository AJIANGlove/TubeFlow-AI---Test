"""§51 jitter A/B (2026-11-05): cadence variant strategy and manual-H3 experiment arms."""

from __future__ import annotations

# ruff: noqa: E501
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_single_panel_prompt import Provider, _valid
from test_single_panel_replay import _with_panels
from test_video_content_qc_ladder import RecordingCompiler
from test_video_orchestration import setup

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.video_generation.api import create_video_router
from tubeflow_test.video_generation.domain import (
    ReferencePolicy,
    ReferenceStrategy,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import (
    EXPERIMENT_ARM_BASELINE,
    EXPERIMENT_ARM_CADENCE,
    JITTER_CADENCE_AB_V1,
    ArchiveStage,
    AttemptStage,
    _authorize,
    _compiler_input,
    derive_cadence_variant_prompt,
    experiment_block,
    prepare_manual_video_intent,
    require_experiment_source,
)
from tubeflow_test.video_generation.persistence import ManualH3IntentWriter
from tubeflow_test.video_generation.project_routing import reference_input_fingerprint
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import (
    CADENCE_SENTENCE_V1,
    CompiledVideoPrompt,
    SinglePanelPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
    insert_cadence_sentence,
    insert_single_panel_binding_line,
    validate_single_panel_compiled_prompt,
)
from tubeflow_test.video_prompt.templates import (
    build_single_panel_user_payload,
    build_system_prompt,
)

V4 = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_SINGLE_PANEL
CADENCE = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_SINGLE_PANEL_CADENCE_V1
GRID_V2 = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-grid-v2"})
TITLE54 = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-title54-v1"})
SEED = 798160553724158
# Independent copy of Pending issues/2026-11-05/JITTER_SURVEY_20261105/README.md §2.
README_SENTENCE = 'Animate every frame at the full frame rate with smooth, continuous motion; never hold a pose for two frames or use limited animation ("animating on twos"); people, hands and objects move a little on every frame.'


def _source(policy: ReferencePolicy = GRID_V2):  # type: ignore[no-untyped-def]
    source, repository, _engine, assets, service = setup(seed=None)
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
    return source, repository, service


def _v4_value(source, attempt_id: UUID | None = None) -> SinglePanelPromptCompilerInput:  # type: ignore[no-untyped-def]
    generation = _authorize(source, idempotency_key="ab-key", seed=SEED)
    value = _compiler_input(source, generation, V4, attempt_id or uuid4())
    assert isinstance(value, SinglePanelPromptCompilerInput)
    return value


def _baseline(source, key: str = "ab-g7-s1-baseline"):  # type: ignore[no-untyped-def]
    """A persisted-like baseline arm: compiled v4 prompt, explicit seed, experiment block."""
    attempt_id = uuid4()
    generation = _authorize(source, idempotency_key=key, seed=SEED)
    value = _compiler_input(source, generation, V4, attempt_id)
    assert isinstance(value, SinglePanelPromptCompilerInput)
    text = insert_single_panel_binding_line(_valid(value), value)
    compiled = CompiledVideoPrompt(text, V4, "deepseek-test", "receipt", False, value.fingerprint)
    snapshot, _sha, request, _request_sha = prepare_manual_video_intent(
        source, attempt_id, compiled=compiled, idempotency_key=key, seed=SEED
    )
    snapshot["experiment"] = experiment_block(EXPERIMENT_ARM_BASELINE, None)
    return attempt_id, snapshot, request


# --- the variant sentence and its deterministic insertion -----------------------------


def test_cadence_sentence_is_the_readme_text_verbatim() -> None:
    assert CADENCE_SENTENCE_V1 == README_SENTENCE


def test_insertion_adds_one_line_right_before_shot_1_and_nothing_else() -> None:
    source, _repository, _service = _source()
    value = _v4_value(source)
    text = insert_single_panel_binding_line(_valid(value), value)
    out = insert_cadence_sentence(text)
    lines = out.splitlines()
    shot_1 = next(index for index, line in enumerate(lines) if line.startswith("[Shot 1]"))
    assert lines[shot_1 - 1] == README_SENTENCE
    assert lines[shot_1 - 2].startswith("Dark oil-painted illustration")
    assert out.replace(README_SENTENCE + "\n", "", 1) == text
    with pytest.raises(VideoPromptViolation, match="h3_prompt_cadence_sentence_invalid"):
        insert_cadence_sentence(out)


def test_insertion_breaks_an_inline_shot_1_onto_its_own_line() -> None:
    source, _repository, _service = _source()
    value = _v4_value(source)
    text = insert_single_panel_binding_line(_valid(value), value).replace(
        "thick texture.\n[Shot 1]", "thick texture. [Shot 1]"
    )
    out = insert_cadence_sentence(text)
    assert f"thick texture. \n{README_SENTENCE}\n[Shot 1]" in out
    validate_single_panel_compiled_prompt(
        out, expected=replace(value, strategy=CADENCE), binding_inserted=True
    )


def test_insertion_fails_closed_without_a_shot_1_block() -> None:
    source, _repository, _service = _source()
    value = _v4_value(source)
    text = insert_single_panel_binding_line(_valid(value), value).replace("[Shot 1]", "[Shot one]")
    with pytest.raises(VideoPromptViolation, match="h3_prompt_cadence_insertion_failed"):
        insert_cadence_sentence(text)


def test_cadence_validation_requires_exactly_one_sentence_and_keeps_v4_rules() -> None:
    source, _repository, _service = _source()
    value = _v4_value(source)
    cadence = replace(value, strategy=CADENCE)
    text = insert_single_panel_binding_line(_valid(value), value)
    validate_single_panel_compiled_prompt(text, expected=value, binding_inserted=True)
    validate_single_panel_compiled_prompt(
        insert_cadence_sentence(text), expected=cadence, binding_inserted=True
    )
    with pytest.raises(VideoPromptViolation, match="h3_prompt_cadence_sentence_invalid"):
        validate_single_panel_compiled_prompt(text, expected=cadence, binding_inserted=True)
    # The model output (before deterministic insertion) must not carry the sentence itself.
    raw = _valid(value).replace("[Shot 1]", README_SENTENCE + "\n[Shot 1]", 1)
    with pytest.raises(VideoPromptViolation, match="h3_prompt_cadence_sentence_invalid"):
        validate_single_panel_compiled_prompt(raw, expected=cadence, binding_inserted=False)
    gendered = insert_cadence_sentence(text).replace("the investigator studies", "she studies")
    with pytest.raises(VideoPromptViolation, match="h3_prompt_gendered_pronoun_forbidden: she"):
        validate_single_panel_compiled_prompt(gendered, expected=cadence, binding_inserted=True)


def test_cadence_sends_the_compiler_exactly_the_v4_request() -> None:
    source, _repository, _service = _source()
    value = _v4_value(source)
    cadence = replace(value, strategy=CADENCE)
    assert build_system_prompt(CADENCE, duration_seconds=12) == build_system_prompt(
        V4, duration_seconds=12
    )
    assert build_single_panel_user_payload(cadence) == build_single_panel_user_payload(value)
    assert cadence.fingerprint != value.fingerprint


def test_compiler_inserts_the_sentence_only_for_the_cadence_strategy() -> None:
    source, _repository, _service = _source()
    value = _v4_value(source)
    cadence = replace(value, strategy=CADENCE)
    provider = Provider([_valid(value), _valid(cadence)])
    compiler = VideoPromptCompiler(provider, model="fake", strategy=V4)  # type: ignore[arg-type]
    baseline = compiler.compile(value)
    variant = compiler.compile(cadence)
    assert README_SENTENCE not in baseline.text
    assert variant.strategy is CADENCE
    assert variant.text.count(README_SENTENCE) == 1
    assert variant.text.replace(README_SENTENCE + "\n", "", 1) == baseline.text
    assert [payload["strategy"] for payload in provider.payloads] == [V4.value, V4.value]


# --- experiment arms --------------------------------------------------------------------


def test_experiment_block_accepts_only_the_two_arm_shapes() -> None:
    source_id = uuid4()
    assert experiment_block(EXPERIMENT_ARM_BASELINE, None) == {
        "name": JITTER_CADENCE_AB_V1,
        "arm": "baseline",
        "prompt_source_attempt_id": None,
    }
    assert experiment_block(EXPERIMENT_ARM_CADENCE, source_id)["prompt_source_attempt_id"] == str(
        source_id
    )
    for arm, source in (("baseline", source_id), ("cadence_v1", None), ("v3", None)):
        with pytest.raises(VideoRuleViolation, match="experiment_request_invalid"):
            experiment_block(arm, source)


def test_experiments_only_run_on_v4_single_panel_groups() -> None:
    require_experiment_source(_source(GRID_V2)[0])
    with pytest.raises(VideoRuleViolation, match="experiment_requires_v4_single_panel"):
        require_experiment_source(_source(TITLE54)[0])
    composite, *_rest = setup()
    with pytest.raises(VideoRuleViolation, match="experiment_requires_v4_single_panel"):
        require_experiment_source(composite)


def test_cadence_arm_differs_from_its_baseline_only_by_the_sentence() -> None:
    source, _repository, _service = _source()
    baseline_id, baseline_snapshot, baseline_request = _baseline(source)
    attempt_id, key = uuid4(), "ab-g7-s1-cadence"
    compiled = derive_cadence_variant_prompt(
        source,
        attempt_id=attempt_id,
        idempotency_key=key,
        seed=SEED,
        prompt_source_attempt_id=baseline_id,
        baseline_snapshot=baseline_snapshot,
    )
    assert compiled.strategy is CADENCE
    assert compiled.model == "deepseek-test"
    assert compiled.provider_receipt == f"derived-from-attempt:{baseline_id}"
    baseline_text = baseline_snapshot["video_prompt"]["text"]
    assert compiled.text.replace(README_SENTENCE + "\n", "", 1) == baseline_text
    snapshot, _sha, request, _request_sha = prepare_manual_video_intent(
        source, attempt_id, compiled=compiled, idempotency_key=key, seed=SEED
    )
    assert snapshot["video_prompt"]["strategy"] == CADENCE.value
    assert request["prompt"] == compiled.text
    assert request["parameters"] == baseline_request["parameters"]
    assert request["references"] == baseline_request["references"]
    assert request["workflow"] == baseline_request["workflow"]


def _mutated(snapshot: dict[str, object], change: str) -> dict[str, object]:
    value = {**snapshot, "video_prompt": dict(snapshot["video_prompt"])}  # type: ignore[arg-type]
    if change == "no_experiment":
        value.pop("experiment")
    elif change == "cadence_arm":
        value["experiment"] = experiment_block(EXPERIMENT_ARM_CADENCE, uuid4())
    elif change == "v3_strategy":
        value["video_prompt"]["strategy"] = "tubeflow_storyboard_v3_single_panel_opt_in_v1"  # type: ignore[index]
    elif change == "other_seed":
        value["seed"] = SEED + 1
    elif change == "other_references":
        value["reference_input_sha256"] = "0" * 64
    elif change == "sentence_present":
        text = str(value["video_prompt"]["text"])  # type: ignore[index]
        value["video_prompt"]["text"] = insert_cadence_sentence(text)  # type: ignore[index]
    return value


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("no_experiment", "experiment_baseline_invalid"),
        ("cadence_arm", "experiment_baseline_invalid"),
        ("v3_strategy", "experiment_baseline_invalid"),
        ("other_seed", "experiment_seed_mismatch"),
        ("other_references", "video_reference_input_changed"),
        ("sentence_present", "experiment_prompt_invalid"),
    ],
)
def test_cadence_arm_rejects_a_mismatched_baseline(change: str, code: str) -> None:
    source, _repository, _service = _source()
    baseline_id, baseline_snapshot, _request = _baseline(source)
    with pytest.raises(VideoRuleViolation, match=code):
        derive_cadence_variant_prompt(
            source,
            attempt_id=uuid4(),
            idempotency_key="ab-cadence",
            seed=SEED,
            prompt_source_attempt_id=baseline_id,
            baseline_snapshot=_mutated(baseline_snapshot, change),
        )


def test_experiment_attempts_never_take_the_auto_paths() -> None:
    source, repository, service = _source()
    service._prompt_compiler = RecordingCompiler(V4)
    attempt = service.trigger(source.owner_id, source.group_id, "auto-experiment")
    repository.groups[source.group_id] = replace(source, production_mode="AUTO")
    assert service._auto_mode(attempt) is True
    experimental = replace(
        attempt,
        input_snapshot={
            **(attempt.input_snapshot or {}),
            "experiment": experiment_block(EXPERIMENT_ARM_BASELINE, None),
        },
    )
    assert service._auto_mode(experimental) is False


# --- manual writer ----------------------------------------------------------------------


class _Session:
    def __init__(self, scalars: list[object]) -> None:
        self.scalars = list(scalars)

    def scalar(self, _statement: object) -> object:
        return self.scalars.pop(0)


def _writer(scalars: list[object], compiler: object | None = None) -> ManualH3IntentWriter:
    @contextmanager
    def unit_of_work() -> Iterator[_Session]:
        yield _Session(scalars)

    return ManualH3IntentWriter(
        unit_of_work,  # type: ignore[arg-type]
        storyboard_authority=None,  # type: ignore[arg-type]
        group_loader=lambda *_args: None,  # type: ignore[arg-type,return-value]
        prompt_compiler=compiler,  # type: ignore[arg-type]
    )


def test_writer_derives_the_cadence_arm_from_its_baseline_row() -> None:
    source, _repository, _service = _source()
    baseline_id, baseline_snapshot, _request = _baseline(source)
    row = SimpleNamespace(
        group_id=source.group_id,
        generation_id=source.storyboard_generation_id,
        input_snapshot_json=baseline_snapshot,
    )
    compiled = _writer([row])._prompt(
        source,
        source.owner_id,
        source.storyboard_generation_id,
        uuid4(),
        "k",
        SEED,
        baseline_id,
        True,
    )
    assert compiled.strategy is CADENCE
    for other in (
        None,
        replace_namespace(row, group_id=uuid4()),
        replace_namespace(row, generation_id=uuid4()),
    ):
        with pytest.raises(DomainError, match="baseline attempt is not on this group"):
            _writer([other])._prompt(
                source,
                source.owner_id,
                source.storyboard_generation_id,
                uuid4(),
                "k",
                SEED,
                baseline_id,
                True,
            )


def replace_namespace(value: SimpleNamespace, **changes: object) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(value), **changes})


def test_writer_checks_the_group_before_compiling_a_baseline_arm() -> None:
    source, _repository, _service = _source(TITLE54)
    compiler = RecordingCompiler(V4)
    with pytest.raises(VideoRuleViolation, match="experiment_requires_v4_single_panel"):
        _writer([], compiler)._prompt(
            source, source.owner_id, source.storyboard_generation_id, uuid4(), "k", SEED, None, True
        )
    assert compiler.calls == []


def test_writer_rejects_malformed_experiment_requests_before_any_work() -> None:
    writer = _writer([])
    common = {
        "owner_id": uuid4(),
        "group_id": uuid4(),
        "expected_storyboard_generation_id": uuid4(),
        "idempotency_key": "k",
        "seed": SEED,
    }
    with pytest.raises(DomainError, match="prompt source requires an experiment arm"):
        writer.create(**common, prompt_source_attempt_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(VideoRuleViolation, match="experiment_request_invalid"):
        writer.create(**common, experiment_arm="cadence_v1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("stored", "requested", "conflict"),
    [
        (None, None, False),
        ("baseline", "baseline", False),
        ("baseline", None, True),
        (None, "baseline", True),
        ("baseline", "cadence", True),
    ],
)
def test_writer_idempotency_compares_the_experiment_identity(
    stored: str | None, requested: str | None, conflict: bool
) -> None:
    group, generation, existing_id, source_id = uuid4(), uuid4(), uuid4(), uuid4()
    blocks = {
        None: None,
        "baseline": experiment_block(EXPERIMENT_ARM_BASELINE, None),
        "cadence": experiment_block(EXPERIMENT_ARM_CADENCE, source_id),
    }
    snapshot: dict[str, object] = {"manual_request": {}, "seed": SEED}
    if blocks[stored] is not None:
        snapshot["experiment"] = blocks[stored]
    existing = SimpleNamespace(
        attempt_id=existing_id,
        group_id=group,
        generation_id=generation,
        input_snapshot_json=snapshot,
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


# --- HTTP contract ----------------------------------------------------------------------


def _client():  # type: ignore[no-untyped-def]
    owner, attempt = uuid4(), uuid4()

    class Writer:
        seen: dict[str, object] = {}

        def create(self, **kwargs: object) -> UUID:
            self.seen = kwargs
            return attempt

        def get(self, _owner_id: UUID, _attempt_id: UUID) -> object:
            return SimpleNamespace(
                attempt_id=attempt,
                project_id=uuid4(),
                group_id=uuid4(),
                generation_id=uuid4(),
                stage=AttemptStage.CREATED,
                archive_stage=ArchiveStage.NOT_STARTED,
                remote_phase_changed_at=None,
                stalled_at=None,
            )

    writer = Writer()
    account = Account(owner, "user", "hash", AccountRole.USER)
    app = FastAPI()
    app.include_router(create_video_router(writer, lambda: account))  # type: ignore[arg-type]
    app.state.group_video_availability = lambda: {
        "available": True,
        "status": "AVAILABLE",
        "message": "Video service is available.",
    }
    return TestClient(app, raise_server_exceptions=False), writer


def _post(client, body: dict[str, object], key: str = "k"):  # type: ignore[no-untyped-def]
    return client.post(
        f"/group-videos/groups/{uuid4()}/generations",
        headers={"Idempotency-Key": key},
        json={"expected_storyboard_generation_id": str(uuid4()), **body},
    )


def test_http_passes_experiment_arms_and_keeps_plain_requests_unchanged() -> None:
    client, writer = _client()
    source = uuid4()
    _post(client, {"seed": SEED})
    assert "experiment_arm" not in writer.seen and "prompt_source_attempt_id" not in writer.seen
    _post(client, {"seed": SEED, "experiment_arm": "baseline"})
    assert writer.seen["experiment_arm"] == "baseline"
    assert writer.seen["prompt_source_attempt_id"] is None
    assert writer.seen["seed"] == SEED
    _post(
        client,
        {"seed": SEED, "experiment_arm": "cadence_v1", "prompt_source_attempt_id": str(source)},
    )
    assert writer.seen["experiment_arm"] == "cadence_v1"
    assert writer.seen["prompt_source_attempt_id"] == source


@pytest.mark.parametrize(
    "body",
    [
        {"seed": SEED, "experiment_arm": "cadence_v1"},
        {"seed": SEED, "experiment_arm": "baseline", "prompt_source_attempt_id": str(uuid4())},
        {"experiment_arm": "baseline"},
        {"seed": SEED, "prompt_source_attempt_id": str(uuid4())},
        {"seed": SEED, "experiment_arm": "tubeflow_storyboard_v3"},
    ],
)
def test_http_rejects_malformed_experiment_bodies(body: dict[str, object]) -> None:
    client, writer = _client()
    response = _post(client, body)
    assert response.status_code == 422, response.text
    assert writer.seen == {}
