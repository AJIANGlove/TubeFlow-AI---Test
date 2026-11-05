"""§50（2026-11-05）导演阶段停滞治理：末次确定性合并、按违规对的重试提示、调用日志与认领重试。"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from test_project_director import (
    MemoryRepository,
    Model,
    Result,
    complete,
    context,
    create,
    shot_json,
)
from test_project_director_quality_contract import _director

from tubeflow_test.application.plan_convergence import (
    ADJACENT_DIVERSITY_MERGE_VERSION,
    merge_insufficiently_different_shots,
)
from tubeflow_test.application.project_director import (
    PROJECT_DIRECTOR_MAX_ATTEMPTS,
    _provider_call_unit,
)
from tubeflow_test.domain.project_director import (
    ProjectDirectorRunStatus,
    ProjectDirectorTrigger,
    ProjectDirectorUnit,
    ProjectDirectorUnitKind,
    ProjectDirectorUnitStatus,
)
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink

ADJACENT = "director_output_adjacent_diversity_insufficient"
COVERAGE = "director_output_sentence_coverage_invalid"


# --- 纯函数：adjacent-diversity-merge-v1 ---------------------------------------------


def _shot(order: int, event: str, focus: str, time: str = "SAME_MOMENT") -> DirectorShot:
    return DirectorShot(
        uuid4(),
        order,
        f"scene {order}",
        "MEDIUM",
        "EYE_LEVEL",
        f"action {order}",
        f"function {order}",
        f"description {order}",
        event,
        focus,
        time,
        "SAME_LOCATION",
    )


def test_merge_keeps_the_front_shot_and_unions_links() -> None:
    first = _shot(1, "REVEAL_EVIDENCE", "OBJECT_OR_EVIDENCE")
    second = _shot(2, "ESTABLISH_CONTEXT", "OBJECT_OR_EVIDENCE")
    third = _shot(3, "PERFORM_ACTION", "PRIMARY_SUBJECT", "LATER")
    s1, s2, s3 = uuid4(), uuid4(), uuid4()
    links = (
        ShotSentenceLink(first.shot_id, s1),
        ShotSentenceLink(second.shot_id, s1),
        ShotSentenceLink(second.shot_id, s2),
        ShotSentenceLink(third.shot_id, s3),
    )
    shots, merged_links, pairs = merge_insufficiently_different_shots((first, second, third), links)
    assert pairs == ((1, 2),)
    assert [item.shot_id for item in shots] == [first.shot_id, third.shot_id]
    assert [item.order for item in shots] == [1, 2]
    kept = shots[0]
    # 前镜全部字段不变（不拼接后镜文本，避免一格两个时刻）。
    assert replace(kept, order=1) == first
    assert merged_links == (
        ShotSentenceLink(first.shot_id, s1),
        ShotSentenceLink(first.shot_id, s2),
        ShotSentenceLink(third.shot_id, s3),
    )


def test_merge_cascades_until_every_pair_differs_or_one_shot_remains() -> None:
    shots = (
        _shot(1, "REVEAL_EVIDENCE", "OBJECT_OR_EVIDENCE"),
        _shot(2, "ESTABLISH_CONTEXT", "OBJECT_OR_EVIDENCE"),
        _shot(3, "SHOW_CONSEQUENCE", "OBJECT_OR_EVIDENCE"),
    )
    sentence = uuid4()
    links = tuple(ShotSentenceLink(item.shot_id, sentence) for item in shots)
    merged, merged_links, pairs = merge_insufficiently_different_shots(shots, links)
    assert pairs == ((1, 2), (1, 3))
    assert len(merged) == 1 and merged[0].shot_id == shots[0].shot_id
    assert merged_links == (ShotSentenceLink(shots[0].shot_id, sentence),)


def test_merge_is_identity_without_violations() -> None:
    shots = (
        _shot(1, "REVEAL_EVIDENCE", "OBJECT_OR_EVIDENCE"),
        _shot(2, "PERFORM_ACTION", "PRIMARY_SUBJECT"),
    )
    links = tuple(ShotSentenceLink(item.shot_id, uuid4()) for item in shots)
    assert merge_insufficiently_different_shots(shots, links) == (shots, links, ())


# --- 端到端：AUTO 末次合并、MANUAL 不变 ------------------------------------------------


class _OneAxisModel(Model):
    """第 1 镜关联第 1 句、第 2 镜关联其余句；两镜只差 semantic_event（CELESTE 第 40 单元形态）。"""

    def __init__(self, *, comply_after: int | None = None) -> None:
        super().__init__()
        self.shot_prompts: list[str] = []
        self.comply_after = comply_after

    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        self.shot_prompts.append(unit.prompt_content)
        first = shot_json(unit, 1, 1, links=(unit.sentences[0].sentence_id,))
        rest = tuple(item.sentence_id for item in unit.sentences[1:])
        second = shot_json(unit, 2, 2, links=rest)
        for shot, event in ((first, "REVEAL_EVIDENCE"), (second, "ESTABLISH_CONTEXT")):
            shot.update(
                semantic_event=event,
                subject_focus="OBJECT_OR_EVIDENCE",
                time_relation="SAME_MOMENT",
                location_relation="SAME_LOCATION",
            )
        if self.comply_after is not None and len(self.shot_prompts) > self.comply_after:
            second.update(subject_focus="PRIMARY_SUBJECT")
        return Result({"output_locale": unit.required_locale, "shots": [first, second]})


def _run(mode: str, model: _OneAxisModel, sentences: int = 3):  # type: ignore[no-untyped-def]
    value = context(sentences)
    director, repository, clock = _director(value, model)
    repository.production_mode = mode
    run = create(director, value, f"stall-{mode}")
    finished = complete(director, value, run.run_id, clock)
    shot_units = [
        unit
        for unit in repository.list_units(value.owner_id, run.run_id)
        if unit.kind is ProjectDirectorUnitKind.SHOT_PLAN
    ]
    return value, repository, finished, shot_units


def test_auto_final_rejection_is_merged_and_the_run_succeeds() -> None:
    model = _OneAxisModel()
    value, repository, finished, units = _run("AUTO", model)
    assert finished.status is ProjectDirectorRunStatus.SUCCEEDED
    (unit,) = units
    assert unit.attempt_count == PROJECT_DIRECTOR_MAX_ATTEMPTS
    assert len(unit.shots) == 1
    assert {item.sentence_id for item in unit.sentence_links} == {
        item.sentence_id for item in unit.sentences
    }
    assert unit.shots[0].semantic_event.value == "REVEAL_EVIDENCE"
    assert repository.response_diagnostics[unit.unit_id] == {
        "adjacent_diversity_merge": ADJACENT_DIVERSITY_MERGE_VERSION,
        "merged_pairs": "1+2",
        "shots_before": 2,
        "shots_after": 1,
    }
    # 第 2 次提示带违规对，第 3 次为末次合并指令；持久化提示词不含附加文本。
    assert "RETRY NOTICE" not in model.shot_prompts[0]
    assert "The rejected pair was shot 1 and shot 2" in model.shot_prompts[1]
    assert "only semantic_event changed" in model.shot_prompts[1]
    assert "FINAL ATTEMPT NOTICE" in model.shot_prompts[2]
    assert "Merge shot 1 and shot 2 into ONE shot" in model.shot_prompts[2]
    assert "RETRY NOTICE" not in unit.prompt_content and "FINAL" not in unit.prompt_content


def test_manual_final_rejection_still_fails_the_run() -> None:
    value, repository, finished, units = _run("MANUAL", _OneAxisModel())
    assert finished.status is ProjectDirectorRunStatus.FAILED
    assert finished.failure_code == ADJACENT
    (unit,) = units
    assert unit.status is ProjectDirectorUnitStatus.FAILED
    assert repository.response_diagnostics[unit.unit_id]["changed_axes"] == 1


def test_auto_output_that_complies_after_feedback_is_not_merged() -> None:
    model = _OneAxisModel(comply_after=1)
    value, repository, finished, units = _run("AUTO", model)
    assert finished.status is ProjectDirectorRunStatus.SUCCEEDED
    (unit,) = units
    assert unit.attempt_count == 2
    assert len(unit.shots) == 2
    assert "adjacent_diversity_merge" not in repository.response_diagnostics.get(unit.unit_id, {})


class _OneAxisWithoutReceipt(_OneAxisModel):
    def generate(self, unit: ProjectDirectorUnit) -> Result:
        result = super().generate(unit)
        if unit.kind is ProjectDirectorUnitKind.SHOT_PLAN:
            result.receipt = "   "
        return result


def test_final_rejection_with_invalid_provenance_is_not_merged() -> None:
    value, repository, finished, units = _run("AUTO", _OneAxisWithoutReceipt())
    assert finished.status is ProjectDirectorRunStatus.FAILED
    assert finished.failure_code == ADJACENT


# --- 重试提示 -------------------------------------------------------------------------


def _unit(attempt: int, diagnostics: dict[str, str | int | bool]) -> ProjectDirectorUnit:
    value = context(3)
    director, _repository, _clock = _director(value, _OneAxisModel())
    _run_value, units = director.prepare_run(
        context=value,
        idempotency_key="notice",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
    )
    unit = next(item for item in units if item.kind is ProjectDirectorUnitKind.SHOT_PLAN)
    return replace(
        unit,
        last_failure_code=ADJACENT,
        attempt_count=attempt,
        response_diagnostics=diagnostics,
    )


PAIR = {
    "adjacent_pair_after_order": 2,
    "changed_axes": 1,
    "previous_axes": "PERFORM_ACTION|PRIMARY_SUBJECT|LATER|SAME_LOCATION",
    "current_axes": "REVEAL_EVIDENCE|PRIMARY_SUBJECT|LATER|SAME_LOCATION",
}


def test_second_attempt_notice_names_the_rejected_pair() -> None:
    call = _provider_call_unit(_unit(2, PAIR))
    assert "change subject_focus and semantic_event together" in call.prompt_content
    assert (
        "The rejected pair was shot 2 and shot 3: only semantic_event changed, while "
        "subject_focus (PRIMARY_SUBJECT), time_relation (LATER), location_relation "
        "(SAME_LOCATION) stayed the same."
    ) in call.prompt_content
    assert "FINAL ATTEMPT" not in call.prompt_content


def test_final_attempt_notice_asks_to_merge_the_pair() -> None:
    call = _provider_call_unit(_unit(PROJECT_DIRECTOR_MAX_ATTEMPTS, PAIR))
    assert "FINAL ATTEMPT NOTICE" in call.prompt_content
    assert "Merge shot 2 and shot 3 into ONE shot that shows a single moment" in (
        call.prompt_content
    )


@pytest.mark.parametrize("diagnostics", [{}, {"adjacent_pair_after_order": "x"}])
def test_notices_without_usable_diagnostics_fall_back(
    diagnostics: dict[str, str | int | bool],
) -> None:
    second = _provider_call_unit(_unit(2, diagnostics)).prompt_content
    assert "RETRY NOTICE" in second and "The rejected pair" not in second
    final = _provider_call_unit(_unit(PROJECT_DIRECTOR_MAX_ATTEMPTS, diagnostics)).prompt_content
    assert "Merge the two adjacent shots that differ in fewer than two dimensions" in final


# --- 覆盖拒收诊断 ---------------------------------------------------------------------


class _DropsLastSentence(Model):
    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        kept = tuple(item.sentence_id for item in unit.sentences[:-1])
        return Result(
            {"output_locale": unit.required_locale, "shots": [shot_json(unit, 1, 1, links=kept)]}
        )


def test_coverage_rejection_records_missing_positions() -> None:
    value = context(3)
    director, repository, clock = _director(value, _DropsLastSentence())
    run = create(director, value, "coverage")
    finished = complete(director, value, run.run_id, clock)
    assert finished.failure_code == COVERAGE
    unit = next(
        item
        for item in repository.list_units(value.owner_id, run.run_id)
        if item.kind is ProjectDirectorUnitKind.SHOT_PLAN
    )
    assert repository.response_diagnostics[unit.unit_id] == {
        "unit_sentence_count": 3,
        "missing_sentence_count": 1,
        "missing_sentence_positions": "3",
    }


# --- 调用日志 -------------------------------------------------------------------------


def test_calls_and_rejections_are_logged_without_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tubeflow_test.application.project_director")
    _run("AUTO", _OneAxisModel())
    calls = [r.getMessage() for r in caplog.records if "call finished" in r.getMessage()]
    rejected = [r.getMessage() for r in caplog.records if "unit rejected" in r.getMessage()]
    merged = [r.getMessage() for r in caplog.records if "unit merged" in r.getMessage()]
    assert len(calls) == 1 + PROJECT_DIRECTOR_MAX_ATTEMPTS  # 全局规划 1 次 + 单元 3 次
    assert all("duration_ms=" in item and "outcome=returned" in item for item in calls)
    assert len(rejected) == PROJECT_DIRECTOR_MAX_ATTEMPTS - 1
    assert all("terminal=False" in item and ADJACENT in item for item in rejected)
    assert len(merged) == 1 and ADJACENT_DIVERSITY_MERGE_VERSION in merged[0]
    assert not any("description-" in r.getMessage() for r in caplog.records)


# --- 认领竞态 -------------------------------------------------------------------------


class _RacingRepository(MemoryRepository):
    """开启 race 后的下一次认领前，模拟另一个并发活动先认领了同一单元。"""

    def __init__(self, value) -> None:  # type: ignore[no-untyped-def]
        super().__init__(value)
        self.race = False
        self.raced_unit: UUID | None = None

    def claim_unit(  # type: ignore[override]
        self,
        owner_id: UUID,
        run_id: UUID,
        unit_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
    ) -> ProjectDirectorUnit | None:
        if self.race:
            self.race = False
            self.raced_unit = unit_id
            super().claim_unit(owner_id, run_id, unit_id, uuid4(), now, lease_until)
        return super().claim_unit(owner_id, run_id, unit_id, token, now, lease_until)


def test_losing_a_claim_race_claims_the_next_unit() -> None:
    value = context(9)
    model = Model()
    director, _repository, _clock = _director(value, model)
    repository = _RacingRepository(value)
    director._repository = repository  # noqa: SLF001
    run = create(director, value, "race")
    director.advance(value.owner_id, run.run_id)  # 全局规划
    shot_units = [
        item
        for item in repository.list_units(value.owner_id, run.run_id)
        if item.kind is ProjectDirectorUnitKind.SHOT_PLAN
    ]
    assert len(shot_units) >= 2
    model.calls.clear()
    repository.race = True
    director.advance(value.owner_id, run.run_id)
    units = {item.unit_id: item for item in repository.list_units(value.owner_id, run.run_id)}
    # 第一个可认领单元被「别的活动」占住；本次 advance 认领并执行了下一个单元，而不是空手返回。
    assert repository.raced_unit == shot_units[0].unit_id
    assert units[shot_units[0].unit_id].status is ProjectDirectorUnitStatus.LEASED
    assert units[shot_units[0].unit_id].attempt_count == 0
    assert model.calls == [shot_units[1].ordinal]
    assert units[shot_units[1].unit_id].status is ProjectDirectorUnitStatus.SUCCEEDED


def test_sql_unit_mapping_round_trips_the_diagnostics() -> None:
    from tubeflow_test.adapters.persistence.storyboards import project_director_repository as sql

    unit = _unit(3, PAIR)
    row = sql._unit_row(unit)  # noqa: SLF001
    assert row.response_diagnostics_json == PAIR
    assert sql._unit(row).response_diagnostics == PAIR  # noqa: SLF001
