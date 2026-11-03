from __future__ import annotations

from uuid import uuid4

import pytest

from tubeflow_test.adapters.persistence.media_repository import (
    SqlProjectWriteGate,
    has_cross_domain_active_jobs,
)
from tubeflow_test.domain.errors import DomainError


class ActiveGate:
    def __init__(self, name: str, active: bool) -> None:
        self.name = name
        self.active = active
        self.calls = 0

    def has_active(self, session, project_id) -> bool:
        self.calls += 1
        return self.active


def test_cross_domain_active_gate_checks_narration_storyboard_h3_and_composition() -> None:
    gates = tuple(
        ActiveGate(name, name == "h3") for name in ("narration", "storyboard", "h3", "composition")
    )
    assert has_cross_domain_active_jobs(object(), uuid4(), gates)  # type: ignore[arg-type]
    assert [gate.calls for gate in gates] == [1, 1, 1, 0]


class PurgingSession:
    def scalar(self, statement):
        return uuid4()

    def get(self, model, key):
        return object()


def test_shared_write_gate_fails_closed_for_purging_project() -> None:
    with pytest.raises(DomainError) as error:
        SqlProjectWriteGate().assert_writable(
            PurgingSession(),
            uuid4(),
            uuid4(),  # type: ignore[arg-type]
        )
    assert error.value.code == "project_purging"
