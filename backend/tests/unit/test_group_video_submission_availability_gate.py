from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.video_generation.api import create_video_router
from tubeflow_test.video_generation.orchestration import ArchiveStage, AttemptStage


class ProbeWriter:
    def __init__(self) -> None:
        self.calls = 0
        self.attempt_id = uuid4()
        self.group_id = uuid4()
        self.generation_id = uuid4()
        self.project_id = uuid4()

    def create(self, **_kwargs: object) -> UUID:
        self.calls += 1
        return self.attempt_id

    def get(self, _owner_id: UUID, attempt_id: UUID) -> object | None:
        if attempt_id != self.attempt_id:
            return None
        return SimpleNamespace(
            attempt_id=attempt_id,
            project_id=self.project_id,
            group_id=self.group_id,
            generation_id=self.generation_id,
            stage=AttemptStage.CREATED,
            archive_stage=ArchiveStage.NOT_STARTED,
        )


def _client(writer: ProbeWriter, availability: object) -> TestClient:
    owner = Account(uuid4(), "owner", "hash", AccountRole.USER)
    app = FastAPI()
    app.state.group_video_availability = availability
    app.include_router(create_video_router(writer, lambda: owner))
    return TestClient(app)


@pytest.mark.parametrize(
    ("contract", "expected_code"),
    [
        (
            {
                "available": False,
                "status": "NOT_CONFIGURED",
                "message": "Video service is not configured.",
            },
            "h3_not_configured",
        ),
        (
            {
                "available": False,
                "status": "UNAVAILABLE",
                "message": "No fresh compatible node heartbeat.",
            },
            "h3_unavailable",
        ),
    ],
)
def test_unavailable_service_fails_before_writing_attempt(
    contract: dict[str, object], expected_code: str
) -> None:
    writer = ProbeWriter()
    response = _client(writer, lambda: contract).post(
        f"/group-videos/groups/{writer.group_id}/generations",
        headers={"Idempotency-Key": "manual-h3-contract-key"},
        json={"expected_storyboard_generation_id": str(writer.generation_id)},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == expected_code
    assert writer.calls == 0


def test_availability_probe_failure_fails_closed_before_writing_attempt() -> None:
    writer = ProbeWriter()

    def unavailable() -> object:
        raise RuntimeError("database details must not escape")

    response = _client(writer, unavailable).post(
        f"/group-videos/groups/{writer.group_id}/generations",
        headers={"Idempotency-Key": "manual-h3-contract-key"},
        json={"expected_storyboard_generation_id": str(writer.generation_id)},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "h3_unavailable",
        "message": "Video service availability could not be verified.",
    }
    assert "database details" not in response.text
    assert writer.calls == 0


def test_fresh_compatible_service_allows_manual_attempt_creation() -> None:
    writer = ProbeWriter()
    response = _client(
        writer,
        lambda: {
            "available": True,
            "status": "AVAILABLE",
            "message": "Video service is available.",
        },
    ).post(
        f"/group-videos/groups/{writer.group_id}/generations",
        headers={"Idempotency-Key": "manual-h3-contract-key"},
        json={"expected_storyboard_generation_id": str(writer.generation_id)},
    )

    assert response.status_code == 202
    assert response.json()["attempt_id"] == str(writer.attempt_id)
    assert writer.calls == 1
