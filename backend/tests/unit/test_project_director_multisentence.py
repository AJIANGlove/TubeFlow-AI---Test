from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from test_project_director import (
    Clock,
    Ids,
    MemoryRepository,
    Model,
    Result,
    complete,
    context,
    context_with_texts,
    create,
    shot_json,
)

from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import is_nonvisual_script_separator
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorGroupContext,
    ProjectDirectorRunStatus,
    ProjectDirectorUnit,
    ProjectDirectorUnitKind,
    ProjectDirectorUnitStatus,
)


def _service(value, model: Model) -> tuple[ProjectDirectorService, MemoryRepository, Clock]:
    repository = MemoryRepository(value)
    clock = Clock()
    return (
        ProjectDirectorService(
            repository,
            model,
            Ids(),
            clock,
            model_name="gpt-5.6-sol",
            parameters={"temperature": 0.2},
        ),
        repository,
        clock,
    )


class SplitLinkModel(Model):
    """Splits sentence links across two shots inside every shot plan unit."""

    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        first, *rest = (item.sentence_id for item in unit.sentences)
        shots = [shot_json(unit, 1, unit.ordinal * 8 + 1, links=(first,))]
        if rest:
            shots.append(shot_json(unit, 2, unit.ordinal * 8 + 2, links=tuple(rest)))
        return Result({"output_locale": unit.required_locale, "shots": shots})


def test_unit_accepts_links_split_across_shots_inside_the_unit() -> None:
    value = context(3)
    service, repository, _clock = _service(value, SplitLinkModel())
    run = create(service, value, "split-links")

    service.advance(value.owner_id, run.run_id)
    result = service.advance(value.owner_id, run.run_id)

    assert result.status is ProjectDirectorRunStatus.SUCCEEDED
    unit = repository.list_units(value.owner_id, run.run_id)[1]
    assert unit.status is ProjectDirectorUnitStatus.SUCCEEDED
    assert len(unit.shots) == 2
    assert {item.sentence_id for item in unit.sentence_links} == {
        item.sentence_id for item in value.sentences
    }


class OutsideLinkModel(Model):
    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        foreign = UUID(int=424242)
        shots = [shot_json(unit, 1, unit.ordinal, links=(foreign,))]
        return Result({"output_locale": unit.required_locale, "shots": shots})


class PartialCoverageModel(Model):
    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        only_first = (unit.sentences[0].sentence_id,)
        shots = [shot_json(unit, 1, unit.ordinal, links=only_first)]
        return Result({"output_locale": unit.required_locale, "shots": shots})


@pytest.mark.parametrize(
    ("model_factory", "failure_code"),
    [
        (OutsideLinkModel, "director_output_context_link_invalid"),
        (PartialCoverageModel, "director_output_sentence_coverage_invalid"),
    ],
)
def test_invalid_unit_links_are_retried_then_terminal_with_stable_code(
    model_factory: type[Model], failure_code: str
) -> None:
    value = context(3)
    model = model_factory()
    service, repository, clock = _service(value, model)
    run = create(service, value, f"links-{failure_code}")
    service.advance(value.owner_id, run.run_id)

    before = clock.now()
    service.advance(value.owner_id, run.run_id)
    unit = repository.list_units(value.owner_id, run.run_id)[1]

    assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert unit.attempt_count == 1
    assert unit.last_failure_code == failure_code
    assert unit.next_attempt_at == before + timedelta(seconds=5)

    clock.advance_by(5)
    service.advance(value.owner_id, run.run_id)
    clock.advance_by(15)
    failed = service.advance(value.owner_id, run.run_id)

    assert failed.status is ProjectDirectorRunStatus.FAILED
    assert failed.failure_code == failure_code
    assert model.calls == [1, 2, 2, 2]
    assert service.advance(value.owner_id, run.run_id) == failed
    assert model.calls == [1, 2, 2, 2]


def test_cross_group_shot_keeps_shared_source_and_distinct_candidate_ids() -> None:
    value = context(2)
    first, second = (item.sentence_id for item in value.sentences)
    service, repository, _clock = _service(value, Model())
    complete(service, value, create(service, value, "cross-group").run_id)
    source_revision = service.current(value.owner_id, value.project_id)
    shared_source_id = source_revision.shots[0].shot_id
    repository.groups = (
        ProjectDirectorGroupContext(UUID(int=8001), 1, (first,)),
        ProjectDirectorGroupContext(UUID(int=8002), 2, (second,)),
    )

    result = service.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.SUCCEEDED
    first_link = next(
        item
        for item in result.candidates[0].source_shot_links
        if item.source_shot_id == shared_source_id
    )
    second_link = next(
        item
        for item in result.candidates[1].source_shot_links
        if item.source_shot_id == shared_source_id
    )
    assert first_link.candidate_shot_id != second_link.candidate_shot_id
    assert first_link.candidate_shot_id in {item.shot_id for item in result.candidates[0].shots}
    assert second_link.candidate_shot_id in {item.shot_id for item in result.candidates[1].shots}


def test_punctuation_and_markdown_units_never_call_provider_and_keep_every_sentence_id() -> None:
    value = context_with_texts(
        ('"', "first supported event", "---", "'", "second supported event", "***", "—")
    )
    model = Model()
    service, repository, _clock = _service(value, model)

    run = complete(service, value, create(service, value, "separator-folding").run_id)

    assert run.status is ProjectDirectorRunStatus.SUCCEEDED
    assert model.calls == [1, 3, 6]
    units = repository.list_units(value.owner_id, run.run_id)
    separators = [
        unit for unit in units if unit.kind is ProjectDirectorUnitKind.NONVISUAL_SEPARATOR
    ]
    assert [item.ordinal for item in separators] == [2, 4, 5, 7, 8]
    assert all(not unit.shots and unit.provider_receipt is not None for unit in separators)
    revision = service.current(value.owner_id, value.project_id)
    coverage = Counter(item.sentence_id for item in revision.sentence_links)
    assert coverage == Counter({item.sentence_id: 1 for item in value.sentences})


def test_separator_lineage_survives_deterministic_cross_group_materialization() -> None:
    value = context_with_texts(("first event", "---", '"', "second event", "third event"))
    model = Model()
    service, repository, _clock = _service(value, model)
    complete(service, value, create(service, value, "separator-groups").run_id)
    # §5.19：分隔句不进入配音分组，分组只携带视觉句子；重映射仍保留分隔句的镜头血缘。
    visual = tuple(
        item for item in value.sentences if not is_nonvisual_script_separator(item.text)
    )
    repository.groups = (
        ProjectDirectorGroupContext(
            UUID(int=8101), 1, tuple(item.sentence_id for item in visual[:1])
        ),
        ProjectDirectorGroupContext(
            UUID(int=8102), 2, tuple(item.sentence_id for item in visual[1:])
        ),
    )
    calls_before = tuple(model.calls)

    result = service.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.SUCCEEDED
    assert tuple(model.calls) == calls_before
    for group, candidate in zip(repository.groups, result.candidates, strict=True):
        assert {item.sentence_id for item in candidate.sentence_links} == set(group.sentence_ids)


def test_all_separator_script_fails_before_any_provider_unit_can_run() -> None:
    value = context_with_texts(("---", '"', "***"))
    service, repository, _clock = _service(value, Model())

    with pytest.raises(DomainError) as raised:
        create(service, value, "only-separators")

    assert raised.value.code == "director_script_has_no_visual_content"
    assert repository.runs == {}


def test_312_sentence_run_skips_known_separator_positions_but_covers_all_ids() -> None:
    value = context(312)
    sentences = list(value.sentences)
    for index, text in ((15, "---"), (21, '"')):
        sentences[index] = replace(
            sentences[index],
            text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
    value = replace(value, sentences=tuple(sentences))
    model = Model()
    service, repository, _clock = _service(value, model)

    run = complete(service, value, create(service, value, "312-with-separators").run_id)

    assert run.status is ProjectDirectorRunStatus.SUCCEEDED
    units = repository.list_units(value.owner_id, run.run_id)
    separator_ordinals = {
        unit.ordinal for unit in units if unit.kind is ProjectDirectorUnitKind.NONVISUAL_SEPARATOR
    }
    shot_unit_count = sum(unit.kind is ProjectDirectorUnitKind.SHOT_PLAN for unit in units)
    assert separator_ordinals and separator_ordinals.isdisjoint(model.calls)
    assert len(model.calls) == 1 + shot_unit_count
    revision = service.current(value.owner_id, value.project_id)
    assert {item.sentence_id for item in revision.sentence_links} == {
        item.sentence_id for item in value.sentences
    }
