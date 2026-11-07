"""§53（2026-11-07）：镜头数被拒后的重试提示写明返回镜头数与上限，并要求合并相邻句。

依据：E2E 项目 93d717fa 第 5 单元（8 句，上限 min(2×8, 8) = 8）三次都返回 9 镜以上，
重试只收到通用提示，运行以 director_output_shot_count_invalid 暂停。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_project_director import Model, Result, complete, context, create, shot_json
from test_project_director_quality_contract import _director

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

SHOT_COUNT = "director_output_shot_count_invalid"


class _TooManyShots(Model):
    """返回比上限多一镜（每镜关联一句，多出的一镜关联最后一句）；comply_after 次之后按句一镜。"""

    def __init__(self, *, comply_after: int | None = None) -> None:
        super().__init__()
        self.shot_prompts: list[str] = []
        self.idempotency_keys: list[str] = []
        self.comply_after = comply_after

    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        self.shot_prompts.append(unit.prompt_content)
        self.idempotency_keys.append(unit.provider_idempotency_key)
        ids = [item.sentence_id for item in unit.sentences]
        count = min(2 * len(ids), 8) + 1
        if self.comply_after is not None and len(self.shot_prompts) > self.comply_after:
            count = len(ids)
        shots = [
            shot_json(unit, order, order, links=(ids[min(order, len(ids)) - 1],))
            for order in range(1, count + 1)
        ]
        return Result({"output_locale": unit.required_locale, "shots": shots})


def _run(model: _TooManyShots, sentences: int):  # type: ignore[no-untyped-def]
    value = context(sentences)
    director, repository, clock = _director(value, model)
    repository.production_mode = "AUTO"
    run = create(director, value, f"shot-count-{sentences}")
    finished = complete(director, value, run.run_id, clock)
    (unit,) = (
        item
        for item in repository.list_units(value.owner_id, run.run_id)
        if item.kind is ProjectDirectorUnitKind.SHOT_PLAN
    )
    return repository, finished, unit


# --- 端到端 ---------------------------------------------------------------------------


def test_eight_sentence_unit_gets_the_count_and_the_limit_and_then_complies() -> None:
    model = _TooManyShots(comply_after=1)
    repository, finished, unit = _run(model, 8)
    assert finished.status is ProjectDirectorRunStatus.SUCCEEDED
    assert unit.attempt_count == 2
    assert len(unit.shots) == 8
    assert "RETRY NOTICE" not in model.shot_prompts[0]
    assert (
        f"rejected with code {SHOT_COUNT}: it returned 9 shots, but this TARGET allows at most "
        "8 shots. Return no more than 8 shots."
    ) in model.shot_prompts[1]
    assert "link consecutive sentences that show the same moment" in model.shot_prompts[1]
    assert "overrides any per-sentence pacing guidance above" in model.shot_prompts[1]
    # 只改发往服务商的视图：持久化提示词不含附加文本，重试换用逐次幂等键。
    assert "RETRY NOTICE" not in unit.prompt_content
    assert model.idempotency_keys[0] != model.idempotency_keys[1]


def test_rejection_records_only_counts_and_exhaustion_still_fails_the_run() -> None:
    model = _TooManyShots()
    repository, finished, unit = _run(model, 3)
    assert finished.status is ProjectDirectorRunStatus.FAILED
    assert finished.failure_code == SHOT_COUNT
    assert unit.status is ProjectDirectorUnitStatus.FAILED
    assert unit.attempt_count == PROJECT_DIRECTOR_MAX_ATTEMPTS
    assert repository.response_diagnostics[unit.unit_id] == {
        "returned_shot_count": 7,
        "shot_count_maximum": 6,
    }
    for prompt in model.shot_prompts[1:]:
        assert "it returned 7 shots, but this TARGET allows at most 6 shots" in prompt
    # 预算、暂停语义与末次合并不变：镜头数拒收不触发 adjacent-diversity 合并。
    assert len(model.shot_prompts) == PROJECT_DIRECTOR_MAX_ATTEMPTS
    assert "FINAL ATTEMPT NOTICE" not in model.shot_prompts[-1]


# --- 提示文本 -------------------------------------------------------------------------


def _unit(sentences: int, diagnostics: dict[str, str | int | bool]) -> ProjectDirectorUnit:
    value = context(sentences)
    director, _repository, _clock = _director(value, Model())
    _run_value, units = director.prepare_run(
        context=value,
        idempotency_key="shot-count-notice",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
    )
    unit = next(item for item in units if item.kind is ProjectDirectorUnitKind.SHOT_PLAN)
    return replace(
        unit,
        last_failure_code=SHOT_COUNT,
        attempt_count=2,
        response_diagnostics=diagnostics,
    )


@pytest.mark.parametrize(
    "diagnostics",
    [
        {},
        {"returned_shot_count": "nine", "shot_count_maximum": "eight"},
        {"returned_shot_count": True, "shot_count_maximum": False},
        {"shot_count_maximum": 0},
    ],
)
def test_missing_or_malformed_diagnostics_fall_back_to_the_unit_limit(
    diagnostics: dict[str, str | int | bool],
) -> None:
    prompt = _provider_call_unit(_unit(3, diagnostics)).prompt_content
    assert "it returned too many shots, but this TARGET allows at most 6 shots" in prompt


def test_zero_shots_asks_for_between_one_and_the_limit() -> None:
    prompt = _provider_call_unit(
        _unit(3, {"returned_shot_count": 0, "shot_count_maximum": 6})
    ).prompt_content
    assert f"rejected with code {SHOT_COUNT}: it returned no shots. Return between 1 and 6" in (
        prompt
    )
    assert "allows at most" not in prompt


def test_limit_is_capped_at_eight_for_long_units() -> None:
    prompt = _provider_call_unit(_unit(8, {})).prompt_content
    assert "allows at most 8 shots" in prompt


def test_other_rejections_keep_the_generic_notice() -> None:
    unit = replace(_unit(3, {}), last_failure_code="director_output_locale_declared_mismatch")
    prompt = _provider_call_unit(unit).prompt_content
    assert (
        "RETRY NOTICE: your previous answer was rejected with code "
        "director_output_locale_declared_mismatch. Re-read the output contract above"
    ) in prompt
    assert "allows at most" not in prompt
