from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.api.video_batch import create_video_batch_router
from tubeflow_test.application.video_batch import (
    VideoBatchGroupOverview,
    VideoBatchItemView,
    VideoBatchJobView,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account, AccountRole


def _account() -> Account:
    return Account(uuid4(), "owner", "hash", AccountRole.USER)


class FakeBatch:
    def __init__(self) -> None:
        self.start_calls: list[tuple[UUID, UUID, tuple[UUID, ...], bool]] = []

    def overview(self, owner_id: UUID, project_id: UUID):  # type: ignore[no-untyped-def]
        return (
            VideoBatchGroupOverview(
                group_id=uuid4(),
                sequence=1,
                eligible=True,
                reason=None,
                video_blocked=False,
                has_current_video=False,
                latest_attempt_stage=None,
                latest_attempt_stalled=True,
            ),
        )

    def start(
        self, owner_id: UUID, project_id: UUID, group_ids: tuple[UUID, ...], *, force: bool
    ) -> VideoBatchJobView:
        self.start_calls.append((owner_id, project_id, group_ids, force))
        job_id = uuid4()
        self.job_id = job_id
        self.owner_id = owner_id
        return VideoBatchJobView(
            job_id=job_id,
            owner_id=owner_id,
            project_id=project_id,
            force=force,
            total=2,
            created_at=datetime.now(UTC),
            completed_at=None,
            items=(
                VideoBatchItemView(group_ids[0], 1, "PENDING", None, None),
                VideoBatchItemView(group_ids[1], 2, "has_current_video", None, None),
            ),
        )

    def get_job(self, owner_id: UUID, job_id: UUID) -> VideoBatchJobView | None:
        if not self.start_calls:
            return None
        owner, project, group_ids, force = self.start_calls[0]
        if owner_id != owner or job_id != self.job_id:
            return None
        return VideoBatchJobView(
            job_id=self.job_id,
            owner_id=owner,
            project_id=project,
            force=force,
            total=2,
            created_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            items=(
                VideoBatchItemView(group_ids[0], 1, "queued", uuid4(), None),
                VideoBatchItemView(group_ids[1], 2, "has_current_video", None, None),
            ),
        )


def _client(
    batch: FakeBatch, *, available: bool, kick=None  # type: ignore[no-untyped-def]
) -> TestClient:
    app = FastAPI()
    account = _account()
    app.include_router(
        create_video_batch_router(batch, lambda: account, kick=kick)  # type: ignore[arg-type]
    )
    app.state.group_video_availability = lambda: {"available": available}
    return TestClient(app)


def test_overview_returns_stable_public_shape() -> None:
    batch = FakeBatch()
    client = _client(batch, available=True)
    project_id = uuid4()

    response = client.get(f"/projects/{project_id}/group-video-batch/overview")

    assert response.status_code == 200
    (item,) = response.json()["items"]
    assert item["eligible"] is True
    assert item["reason"] is None
    assert item["latest_attempt_stalled"] is True


def test_generate_returns_job_immediately_and_schedules_processing() -> None:
    batch = FakeBatch()
    kicked: list[UUID] = []
    client = _client(batch, available=True, kick=kicked.append)
    project_id, group_one, group_two = uuid4(), uuid4(), uuid4()

    response = client.post(
        f"/projects/{project_id}/group-video-batch/generations",
        json={"group_ids": [str(group_one), str(group_two)], "force": True},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["total"] == 2
    assert [item["status"] for item in body["items"]] == ["PENDING", "has_current_video"]
    assert kicked == [UUID(body["job_id"])]
    assert batch.start_calls[0][2] == (group_one, group_two)
    assert batch.start_calls[0][3] is True


def test_job_status_returns_progress_and_404_for_unknown() -> None:
    batch = FakeBatch()
    client = _client(batch, available=True)
    project_id, group_one, group_two = uuid4(), uuid4(), uuid4()
    created = client.post(
        f"/projects/{project_id}/group-video-batch/generations",
        json={"group_ids": [str(group_one), str(group_two)], "force": False},
    )
    job_id = created.json()["job_id"]

    response = client.get(f"/projects/{project_id}/group-video-batch/jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["completed"] is True
    assert [item["status"] for item in body["items"]] == ["queued", "has_current_video"]

    missing = client.get(f"/projects/{project_id}/group-video-batch/jobs/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"


def test_generate_fails_closed_when_h3_unavailable() -> None:
    client = _client(FakeBatch(), available=False)

    response = client.post(
        f"/projects/{uuid4()}/group-video-batch/generations",
        json={"group_ids": [str(uuid4())], "force": False},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "h3_unavailable"


class FailingBatch(FakeBatch):
    def overview(self, owner_id: UUID, project_id: UUID):  # type: ignore[no-untyped-def]
        raise DomainError("not_found", "Resource not found")


def test_domain_error_maps_to_stable_http_error() -> None:
    client = _client(FailingBatch(), available=True)

    response = client.get(f"/projects/{uuid4()}/group-video-batch/overview")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
