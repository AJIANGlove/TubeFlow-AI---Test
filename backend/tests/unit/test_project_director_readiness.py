from __future__ import annotations

from uuid import UUID

import pytest
from test_project_director import (
    Clock,
    Ids,
    MemoryRepository,
    Model,
    complete,
    context,
    context_with_texts,
    create,
)

from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorGroupContext,
    ProjectDirectorMaterialization,
    ProjectDirectorRunStatus,
    ProjectDirectorUnit,
    ProjectDirectorUnitStatus,
)


class ReadRepository(MemoryRepository):
    def latest_materialization(
        self, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorMaterialization | None:
        values = [
            item
            for item in self.materializations.values()
            if item.owner_id == owner_id and item.project_id == project_id
        ]
        return (
            max(values, key=lambda item: (item.created_at, item.materialization_id.int))
            if values
            else None
        )


class InvalidStructuredModel(Model):
    def generate(self, unit: ProjectDirectorUnit):
        self.calls.append(unit.ordinal)
        raise ValueError("invalid_structured_response")


def _service(repository: ReadRepository, model: Model | None = None) -> ProjectDirectorService:
    return ProjectDirectorService(
        repository,
        model or Model(),
        Ids(),
        Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )


def test_materialize_if_ready_is_safe_in_both_completion_orders_and_current_is_owner_scoped() -> (
    None
):
    value = context(2)
    repository = ReadRepository(value)
    service = _service(repository)

    assert service.materialize_if_ready(value.owner_id, value.project_id) is None

    run = complete(service, value, create(service, value, "preplan").run_id)
    assert run.status is ProjectDirectorRunStatus.SUCCEEDED
    assert service.materialize_if_ready(value.owner_id, value.project_id) is None

    repository.groups = (
        ProjectDirectorGroupContext(
            UUID(int=9001),
            1,
            tuple(item.sentence_id for item in value.sentences),
        ),
    )
    materialization = service.materialize_if_ready(value.owner_id, value.project_id)

    assert materialization is not None
    assert materialization.status is MaterializationStatus.SUCCEEDED
    assert service.materialize_if_ready(value.owner_id, value.project_id) == materialization
    assert service.current_materialization(value.owner_id, value.project_id) == materialization
    with pytest.raises(DomainError, match="Project director materialization was not found"):
        service.current_materialization(UUID(int=9999), value.project_id)


def test_materialize_if_ready_propagates_non_readiness_domain_errors() -> None:
    value = context(1)
    repository = ReadRepository(value)
    service = _service(repository)
    complete(service, value, create(service, value, "preplan").run_id)

    def corrupt_groups(
        owner_id: UUID,
        project_id: UUID,
        snapshot_sha256: str,
        snapshot_revision: int,
    ) -> tuple[ProjectDirectorGroupContext, ...]:
        del owner_id, project_id, snapshot_sha256, snapshot_revision
        raise DomainError("timeline_corrupt", "Authoritative timeline is inconsistent")

    repository.materialization_groups = corrupt_groups  # type: ignore[method-assign]

    with pytest.raises(DomainError) as raised:
        service.materialize_if_ready(value.owner_id, value.project_id)
    assert raised.value.code == "timeline_corrupt"


def test_invalid_structured_provider_response_is_retried_then_stops() -> None:
    value = context(2)
    repository = ReadRepository(value)
    model = InvalidStructuredModel()
    clock = Clock()
    service = ProjectDirectorService(
        repository,
        model,
        Ids(),
        clock,
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )
    run = create(service, value, "invalid-response")

    service.advance(value.owner_id, run.run_id)
    unit = repository.list_units(value.owner_id, run.run_id)[0]
    assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert unit.attempt_count == 1
    assert unit.last_failure_code == "invalid_structured_response"

    clock.advance_by(5)
    service.advance(value.owner_id, run.run_id)
    clock.advance_by(15)
    result = service.advance(value.owner_id, run.run_id)

    assert result.status is ProjectDirectorRunStatus.FAILED
    assert result.failure_code == "invalid_structured_response"
    assert model.calls == [1, 1, 1]
    assert service.advance(value.owner_id, run.run_id) == result
    assert model.calls == [1, 1, 1]


def test_materialize_covers_visual_sentences_and_ignores_nonvisual_separators() -> None:
    value = context_with_texts(("alpha event", "---", "beta event"))
    repository = ReadRepository(value)
    service = _service(repository)
    complete(service, value, create(service, value, "preplan").run_id)

    visual_ids = tuple(item.sentence_id for item in value.sentences if item.text != "---")
    repository.groups = (ProjectDirectorGroupContext(UUID(int=9002), 1, visual_ids),)

    materialization = service.materialize_if_ready(value.owner_id, value.project_id)

    assert materialization is not None
    linked = {
        link.sentence_id
        for candidate in materialization.candidates
        for link in candidate.sentence_links
    }
    assert linked == set(visual_ids)
