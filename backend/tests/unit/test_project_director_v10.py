"""v10 unit prompt and retry feedback (USER_DECISIONS §45).

Background: project 765dca11 (the first v9 project) paused because unit 20 failed
director_output_adjacent_diversity_insufficient three times. v9 nudged the model toward an
object shot that changed only subject_focus, and every retry resent the identical prompt at
temperature 0.0 under the same Idempotency-Key, so three attempts were effectively one.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from test_project_director import Result, complete, context, create
from test_project_director_quality_contract import MultiShotModel, _base_payload, _director
from test_project_director_v9 import _FixedClock, _fixed_context, _FixedIds

from tubeflow_test.application.project_director import (
    PROJECT_DIRECTOR_LEGACY_PROMPT_REF,
    PROJECT_DIRECTOR_PROMPT_REF,
    PROJECT_DIRECTOR_V9_PROMPT_REF,
    ProjectDirectorService,
    _provider_call_unit,
    _v10_unit_prompt,
)
from tubeflow_test.domain.project_director import (
    ProjectDirectorRunStatus,
    ProjectDirectorTrigger,
    ProjectDirectorUnit,
    ProjectDirectorUnitKind,
)

ADJACENT = "director_output_adjacent_diversity_insufficient"


def _golden(ref: str) -> tuple[str, str]:
    svc = ProjectDirectorService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        _FixedIds(),
        _FixedClock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.0},
    )
    run, units = svc.prepare_run(
        context=_fixed_context(),
        idempotency_key="k",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
        unit_prompt_ref=ref,
    )
    blob = "\n".join(
        f"{item.prompt_ref}|{item.canonical_request_sha256}|{item.prompt_content}"
        for item in units
    )
    return run.canonical_input_sha256, hashlib.sha256(blob.encode()).hexdigest()


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (
            PROJECT_DIRECTOR_LEGACY_PROMPT_REF,
            (
                "9a4c0a29428a0b6f455200e5a7c42aa47cb6b4dc84e72b05f94766688d7f43c2",
                "ff248c19e4dd44bbbdf97f1954ff9a6447b3cbffab79fba0b9bf0172a58b5212",
            ),
        ),
        (
            # Golden values produced by main at e6d073b (before v10) for the same input.
            PROJECT_DIRECTOR_V9_PROMPT_REF,
            (
                "e12fa33eda3aaeb0c4af99d067898d649690a754c7fe8c00a2edb6fa6337d4a0",
                "e42c4b7211a8ba6fbc83fbf92e73f8a368986d77bfa7b38630f611dcd5d7fc78",
            ),
        ),
    ],
)
def test_v8_and_v9_prompts_stay_byte_identical(ref: str, expected: tuple[str, str]) -> None:
    assert _golden(ref) == expected


def _shot_prompts(ref: str, sentence_count: int = 12) -> list[str]:
    value = context(sentence_count)
    svc, _repository, _clock = _director(value, MultiShotModel(1, same_semantics=False))
    _run, units = svc.prepare_run(
        context=value,
        idempotency_key=f"prompts-{ref}",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
        unit_prompt_ref=ref,
    )
    return [
        item.prompt_content
        for item in units
        if item.kind is ProjectDirectorUnitKind.SHOT_PLAN
    ]


def test_v10_tightens_diversity_guidance_and_allows_single_shot() -> None:
    prompts = _shot_prompts(PROJECT_DIRECTOR_PROMPT_REF)
    assert prompts
    for prompt in prompts:
        assert "which changes subject_focus and usually semantic_event" not in prompt
        assert "change semantic_event too, because changing subject_focus alone" in prompt
        assert "can differ only in semantic_event, so they are always invalid" in prompt
        assert "needs at least two shots" not in prompt
        # v9 pacing and the unchanged contract are kept
        for kept in (
            "about one shot per TARGET sentence",
            "the way an editor cuts B-roll",
            "at least two dimensions must differ",
            "exactly these thirteen keys",
            "never describe a character's face, hair, age, body, skin, or clothing",
        ):
            assert kept in prompt, kept
    assert any(
        "return one shot" in prompt and "aim for at least two shots" in prompt
        for prompt in prompts
    )


def test_v10_builder_fails_closed_when_v9_text_drifts() -> None:
    with pytest.raises(ValueError, match="v10 cannot be built"):
        _v10_unit_prompt("unrelated text", 3)


# --- pinning -----------------------------------------------------------------------


def _seed_run(svc, repository, value, ref: str, clock, *, succeed: bool):
    run, units = svc.prepare_run(
        context=value,
        idempotency_key=f"seed-{ref}-{succeed}",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
        unit_prompt_ref=ref,
    )
    repository.create_run(run, units)
    if succeed:
        assert complete(svc, value, run.run_id, clock).status is ProjectDirectorRunStatus.SUCCEEDED
    return run


def _unit_refs(repository, value, run_id) -> set[str]:
    return {
        unit.prompt_ref
        for unit in repository.list_units(value.owner_id, run_id)
        if unit.kind is not ProjectDirectorUnitKind.GLOBAL_PLAN
    }


def test_new_project_uses_v10() -> None:
    value = context(9)
    svc, repository, _clock = _director(value, MultiShotModel(1, same_semantics=False))
    run = create(svc, value, "fresh")
    assert _unit_refs(repository, value, run.run_id) == {PROJECT_DIRECTOR_PROMPT_REF}


def test_successful_v9_project_stays_on_v9_with_same_canonical_input() -> None:
    value = context(9)
    svc, repository, clock = _director(value, MultiShotModel(1, same_semantics=False))
    first = _seed_run(
        svc, repository, value, PROJECT_DIRECTOR_V9_PROMPT_REF, clock, succeed=True
    )
    again = create(svc, value, "v9-again", trigger=ProjectDirectorTrigger.AUTO_RESUME)
    assert again.canonical_input_sha256 == first.canonical_input_sha256
    assert _unit_refs(repository, value, again.run_id) == {PROJECT_DIRECTOR_V9_PROMPT_REF}


def test_failed_v9_project_moves_to_v10() -> None:
    value = context(9)
    svc, repository, clock = _director(value, MultiShotModel(1, same_semantics=False))
    first, units = svc.prepare_run(
        context=value,
        idempotency_key="v9-failed",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
        unit_prompt_ref=PROJECT_DIRECTOR_V9_PROMPT_REF,
    )
    repository.create_run(first, units)
    # 765dca11 形态：v9 运行以 FAILED 终止（相邻多样性重试耗尽）。
    repository.runs[first.run_id] = replace(
        repository.runs[first.run_id], status=ProjectDirectorRunStatus.FAILED
    )
    again = create(svc, value, "v9-failed-resume", trigger=ProjectDirectorTrigger.AUTO_RESUME)
    assert _unit_refs(repository, value, again.run_id) == {PROJECT_DIRECTOR_PROMPT_REF}
    assert again.canonical_input_sha256 != first.canonical_input_sha256


def test_v8_project_stays_on_v8() -> None:
    value = context(9)
    svc, repository, clock = _director(value, MultiShotModel(1, same_semantics=False))
    first = _seed_run(
        svc, repository, value, PROJECT_DIRECTOR_LEGACY_PROMPT_REF, clock, succeed=True
    )
    again = create(svc, value, "v8-again", trigger=ProjectDirectorTrigger.MANUAL_REGENERATE)
    assert again.canonical_input_sha256 == first.canonical_input_sha256
    assert _unit_refs(repository, value, again.run_id) == {PROJECT_DIRECTOR_LEGACY_PROMPT_REF}


# --- retry feedback ------------------------------------------------------------------


def _shot_unit(last_failure_code: str | None, attempt_count: int = 2) -> ProjectDirectorUnit:
    value = context(6)
    svc, _repository, _clock = _director(value, MultiShotModel(1, same_semantics=False))
    _run, units = svc.prepare_run(
        context=value,
        idempotency_key="unit",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
    )
    unit = next(item for item in units if item.kind is ProjectDirectorUnitKind.SHOT_PLAN)
    return replace(unit, last_failure_code=last_failure_code, attempt_count=attempt_count)


def test_retry_after_diversity_rejection_carries_notice_and_new_key() -> None:
    unit = _shot_unit(ADJACENT)
    call = _provider_call_unit(unit)
    assert call.prompt_content.startswith(unit.prompt_content)
    assert "RETRY NOTICE" in call.prompt_content and ADJACENT in call.prompt_content
    assert "change subject_focus and semantic_event together" in call.prompt_content
    assert "this overrides any minimum shot count above" in call.prompt_content
    assert call.provider_idempotency_key != unit.provider_idempotency_key
    assert len(call.provider_idempotency_key) == 64
    # The persisted unit identity does not change.
    assert call.prompt_sha256 == unit.prompt_sha256
    assert call.canonical_request_sha256 == unit.canonical_request_sha256
    # Each attempt gets its own key.
    assert (
        _provider_call_unit(_shot_unit(ADJACENT, attempt_count=3)).provider_idempotency_key
        != call.provider_idempotency_key
    )


def test_retry_after_other_output_rejection_gets_generic_notice() -> None:
    call = _provider_call_unit(_shot_unit("director_output_sentence_id_uuid_invalid"))
    assert "rejected with code director_output_sentence_id_uuid_invalid" in call.prompt_content
    assert "Re-read the output contract above" in call.prompt_content


@pytest.mark.parametrize("code", [None, "provider_http_429", "TimeoutError"])
def test_first_attempt_and_transport_retries_are_resent_unchanged(code: str | None) -> None:
    unit = _shot_unit(code)
    assert _provider_call_unit(unit) is unit


class _FixesAfterNotice(MultiShotModel):
    """Returns a two-shot unit that differs in no controlled axis until it sees the notice."""

    def __init__(self) -> None:
        super().__init__(2, same_semantics=True)
        self.shot_prompts: list[str] = []
        self.shot_keys: list[str] = []

    def generate(self, unit: ProjectDirectorUnit):  # type: ignore[no-untyped-def]
        if unit.kind is ProjectDirectorUnitKind.SHOT_PLAN:
            self.shot_prompts.append(unit.prompt_content)
            self.shot_keys.append(unit.provider_idempotency_key)
            if "RETRY NOTICE" in unit.prompt_content:
                self.calls.append(unit.ordinal)
                return Result(_base_payload(unit))
        return super().generate(unit)


def test_run_recovers_from_diversity_rejection_on_second_attempt() -> None:
    value = context(3)
    model = _FixesAfterNotice()
    svc, repository, clock = _director(value, model)
    run = create(svc, value, "feedback")
    finished = complete(svc, value, run.run_id, clock)

    assert finished.status is ProjectDirectorRunStatus.SUCCEEDED
    assert len(model.shot_prompts) == 2
    assert "RETRY NOTICE" not in model.shot_prompts[0]
    assert ADJACENT in model.shot_prompts[1]
    assert model.shot_keys[0] != model.shot_keys[1]
    stored = [
        unit
        for unit in repository.list_units(value.owner_id, run.run_id)
        if unit.kind is ProjectDirectorUnitKind.SHOT_PLAN
    ]
    assert all("RETRY NOTICE" not in unit.prompt_content for unit in stored)
