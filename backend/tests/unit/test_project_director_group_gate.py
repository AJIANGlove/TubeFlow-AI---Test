from __future__ import annotations

from uuid import UUID

import pytest
from test_project_director import complete, context, create
from test_project_director_readiness import ReadRepository, _service

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import ProjectDirectorGroupContext


@pytest.mark.parametrize("case", ["incomplete", "reordered"])
def test_materialization_rejects_incomplete_or_reordered_authoritative_groups(
    case: str,
) -> None:
    value = context(2)
    repository = ReadRepository(value)
    service = _service(repository)
    complete(service, value, create(service, value, "preplan").run_id)
    first, second = (item.sentence_id for item in value.sentences)
    repository.groups = (
        (ProjectDirectorGroupContext(UUID(int=7001), 1, (first,)),)
        if case == "incomplete"
        else (
            ProjectDirectorGroupContext(UUID(int=7001), 1, (second,)),
            ProjectDirectorGroupContext(UUID(int=7002), 2, (first,)),
        )
    )

    with pytest.raises(DomainError) as raised:
        service.materialize(value.owner_id, value.project_id)

    assert raised.value.code == "narration_not_ready"
    assert service.materialize_if_ready(value.owner_id, value.project_id) is None
    assert repository.materializations == {}
