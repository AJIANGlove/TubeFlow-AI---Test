"""v9 unit prompt (pacing-first, B-roll coverage) applies only to new projects.

Projects whose latest director run did not use v9 stay pinned to v8, byte for byte, so an
AUTO resume of an existing project sees no canonical input change (USER_DECISIONS §15.13)
and never re-runs its director.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from test_project_director import Clock, Ids, MemoryRepository, Model, complete, context, create

from tubeflow_test.application.project_director import (
    PROJECT_DIRECTOR_LEGACY_PROMPT_REF,
    PROJECT_DIRECTOR_PLAN_PROMPT_REF,
    PROJECT_DIRECTOR_PROMPT_REF,
    PROJECT_DIRECTOR_V9_PROMPT_REF,
    ProjectDirectorService,
    _v9_unit_prompt,
)
from tubeflow_test.domain.project_director import (
    ProjectDirectorInputSnapshot,
    ProjectDirectorRunStatus,
    ProjectDirectorSentenceSnapshot,
    ProjectDirectorTrigger,
    ProjectDirectorUnitKind,
)
from tubeflow_test.domain.storyboards import CharacterAssetSnapshot


def _service(value: ProjectDirectorInputSnapshot) -> tuple[
    ProjectDirectorService, MemoryRepository, Clock
]:
    repository = MemoryRepository(value)
    clock = Clock()
    return (
        ProjectDirectorService(
            repository,
            Model(),
            Ids(),
            clock,
            model_name="gpt-5.6-sol",
            parameters={"temperature": 0.0},
        ),
        repository,
        clock,
    )


def _shot_unit_refs(repository: MemoryRepository, value, run_id: UUID) -> set[str]:
    return {
        unit.prompt_ref
        for unit in repository.list_units(value.owner_id, run_id)
        if unit.kind is not ProjectDirectorUnitKind.GLOBAL_PLAN
    }


# --- legacy (v8) prompt is byte-identical to the code before this change -------------


def _fixed_context() -> ProjectDirectorInputSnapshot:
    texts = [f"Sentence number {i} about the small shop." for i in range(1, 12)]
    texts.insert(5, "---")
    return ProjectDirectorInputSnapshot(
        owner_id=UUID(int=1),
        project_id=UUID(int=2),
        snapshot_sha256="b" * 64,
        snapshot_revision=1,
        sentences=tuple(
            ProjectDirectorSentenceSnapshot(
                UUID(int=100 + index), index, text, hashlib.sha256(text.encode()).hexdigest()
            )
            for index, text in enumerate(texts, 1)
        ),
        characters=(
            CharacterAssetSnapshot(1, UUID(int=3), UUID(int=4), "c" * 64, "image/png", 10),
        ),
        visual_style="documentary realism",
        locale="en-US",
        aspect_ratio="16:9",
    )


class _FixedIds:
    def __init__(self) -> None:
        self.value = 1000

    def new(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 2, tzinfo=UTC)


def test_legacy_prompt_matches_pre_v9_golden_hashes() -> None:
    # Golden values were produced by the pre-change code (prompt v8) for the same input.
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
        unit_prompt_ref=PROJECT_DIRECTOR_LEGACY_PROMPT_REF,
    )
    blob = "\n".join(
        f"{item.prompt_ref}|{item.canonical_request_sha256}|{item.prompt_content}"
        for item in units
    )
    assert run.canonical_input_sha256 == (
        "9a4c0a29428a0b6f455200e5a7c42aa47cb6b4dc84e72b05f94766688d7f43c2"
    )
    assert hashlib.sha256(blob.encode()).hexdigest() == (
        "ff248c19e4dd44bbbdf97f1954ff9a6447b3cbffab79fba0b9bf0172a58b5212"
    )


# --- v9 prompt content -----------------------------------------------------------------


def _unit_prompts(ref: str) -> list[str]:
    value = context(12)
    svc, _repository, _clock = _service(value)
    _run, units = svc.prepare_run(
        context=value,
        idempotency_key=f"prompt-{ref}",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
        unit_prompt_ref=ref,
    )
    return [
        item.prompt_content
        for item in units
        if item.kind is ProjectDirectorUnitKind.SHOT_PLAN
    ]


def test_v9_replaces_single_shot_bias_with_pacing_and_broll_coverage() -> None:
    for prompt in _unit_prompts(PROJECT_DIRECTOR_V9_PROMPT_REF):
        assert "Return exactly one shot unless" not in prompt
        assert "If an additional shot cannot be justified" not in prompt
        assert "Do not invent unsupported rooms" not in prompt
        assert "about one shot per TARGET sentence" in prompt
        assert "the way an editor cuts B-roll" in prompt
        assert "never reuse the same combination of scene, shot_size, and camera_viewpoint" in (
            prompt
        )
        assert "reduce the number of shots only as a last resort" in prompt
        assert "REVEAL_EVIDENCE|OBJECT_OR_EVIDENCE|SAME_MOMENT|SAME_LOCATION" in prompt
        assert "Do not invent locations, events, people, or facts" in prompt
        assert "never describe a character's face, hair, age, body, skin, or clothing" in prompt
        assert "Only the referenced characters may appear as identifiable people" in prompt
        # Unchanged contract
        for kept in (
            "at least two dimensions must differ",
            "never relabel semantic tokens merely to pass validation",
            "invalid because only semantic_event differs",
            "exactly these thirteen keys",
            "Link only TARGET sentence_ids",
            "must remain visibly uncertain",
            "EXTREME_WIDE, WIDE, MEDIUM, CLOSE_UP",
        ):
            assert kept in prompt, kept


def _synthetic_legacy(shot_cap: int) -> str:
    return (
        "x Return exactly one shot unless TARGET explicitly contains multiple genuinely "
        "different narrative events. If an additional shot cannot be justified by an "
        "independent narrative event, return one shot. Use additional shots only for "
        "genuinely different events or semantic functions. The unit never requires more "
        f"than {shot_cap} shots. Micro-movements, progress steps in one composition, or "
        "changing only crop, shot size, or camera angle never justify an additional shot. "
        "Do not invent unsupported rooms, streets, archives, tables, scrolls, tools, anatomy "
        "graphics, or other scene dressing. "
        "If any pair differs by fewer than two dimensions, reduce the number of shots and "
        "rewrite the unit before returning; because only semantic_event differs. y"
    )


def test_v9_keeps_shot_cap_and_minimum_two_shots_for_three_sentence_units() -> None:
    three = _v9_unit_prompt(_synthetic_legacy(6), 3, 6)
    assert "Never return more than 6 shots." in three
    assert "this unit of 3 sentences needs at least two shots" in three
    two = _v9_unit_prompt(_synthetic_legacy(4), 2, 4)
    assert "Never return more than 4 shots." in two
    assert "needs at least two shots" not in two


def test_v9_builder_fails_closed_when_v8_text_drifts() -> None:
    with pytest.raises(ValueError, match="drifted"):
        _v9_unit_prompt("an unrelated prompt", 3, 6)


def test_prepare_run_rejects_unknown_prompt_ref() -> None:
    value = context(5)
    svc, _repository, _clock = _service(value)
    with pytest.raises(ValueError, match="unknown project director unit prompt"):
        svc.prepare_run(
            context=value,
            idempotency_key="bad",
            trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
            unit_prompt_ref="project-director-unit-v7",
        )


def test_prompt_version_changes_canonical_input() -> None:
    value = context(8)
    svc, _repository, _clock = _service(value)
    runs = {
        ref: svc.prepare_run(
            context=value,
            idempotency_key=f"canon-{ref}",
            trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
            unit_prompt_ref=ref,
        )[0]
        for ref in (PROJECT_DIRECTOR_PROMPT_REF, PROJECT_DIRECTOR_LEGACY_PROMPT_REF)
    }
    assert (
        runs[PROJECT_DIRECTOR_PROMPT_REF].canonical_input_sha256
        != runs[PROJECT_DIRECTOR_LEGACY_PROMPT_REF].canonical_input_sha256
    )


# --- per-project pinning ---------------------------------------------------------------


def test_new_project_without_runs_uses_newest_prompt() -> None:
    value = context(9)
    svc, repository, _clock = _service(value)
    run = create(svc, value, "new-project")
    assert _shot_unit_refs(repository, value, run.run_id) == {PROJECT_DIRECTOR_PROMPT_REF}
    plan_refs = {
        unit.prompt_ref
        for unit in repository.list_units(value.owner_id, run.run_id)
        if unit.kind is ProjectDirectorUnitKind.GLOBAL_PLAN
    }
    assert plan_refs == {PROJECT_DIRECTOR_PLAN_PROMPT_REF}


def test_default_prepare_run_is_newest_prompt_for_narration_confirmation_path() -> None:
    value = context(6)
    svc, _repository, _clock = _service(value)
    _run, units = svc.prepare_run(
        context=value,
        idempotency_key="confirm",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
    )
    assert {
        unit.prompt_ref for unit in units if unit.kind is ProjectDirectorUnitKind.SHOT_PLAN
    } == {PROJECT_DIRECTOR_PROMPT_REF}


@pytest.mark.parametrize(
    "trigger",
    [ProjectDirectorTrigger.AUTO_RESUME, ProjectDirectorTrigger.MANUAL_REGENERATE],
)
def test_existing_v8_project_stays_on_v8_and_keeps_canonical_input(
    trigger: ProjectDirectorTrigger,
) -> None:
    value = context(9)
    svc, repository, clock = _service(value)
    first, units = svc.prepare_run(
        context=value,
        idempotency_key="existing-v8",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
        unit_prompt_ref=PROJECT_DIRECTOR_LEGACY_PROMPT_REF,
    )
    repository.create_run(first, units)
    assert complete(svc, value, first.run_id, clock).status is ProjectDirectorRunStatus.SUCCEEDED

    again = create(svc, value, f"existing-v8-{trigger.value}", trigger=trigger)

    assert again.run_id != first.run_id
    assert again.canonical_input_sha256 == first.canonical_input_sha256
    assert _shot_unit_refs(repository, value, again.run_id) == {
        PROJECT_DIRECTOR_LEGACY_PROMPT_REF
    }


def test_project_started_on_newest_prompt_stays_on_it() -> None:
    # 2026-11-03：新项目默认已是 v10；v9 成功运行的钉版另见 test_project_director_v10.py。
    value = context(9)
    svc, repository, clock = _service(value)
    first = create(svc, value, "v9-first")
    assert complete(svc, value, first.run_id, clock).status is ProjectDirectorRunStatus.SUCCEEDED
    again = create(
        svc, value, "v9-again", trigger=ProjectDirectorTrigger.MANUAL_REGENERATE
    )
    assert again.canonical_input_sha256 == first.canonical_input_sha256
    assert _shot_unit_refs(repository, value, again.run_id) == {PROJECT_DIRECTOR_PROMPT_REF}
