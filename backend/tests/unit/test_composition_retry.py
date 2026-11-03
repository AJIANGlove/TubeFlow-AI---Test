from __future__ import annotations

import dataclasses
import hashlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyCompositionRepository,
)
from tubeflow_test.api.media_content import MediaContent, MinioMediaStream
from tubeflow_test.api.media_router import AuthenticatedOwner, create_media_router
from tubeflow_test.application.composition import CompositionAttempt
from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.composition import (
    CompositionRequest,
    GroupInput,
    NarrationSegmentInput,
    SentenceTiming,
)
from tubeflow_test.domain.errors import DomainError


class _Transaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _RetrySession:
    def __init__(self, project_id: UUID, job: object, outbox: object) -> None:
        self.project_id = project_id
        self.job = job
        self.outbox = outbox

    def __enter__(self) -> _RetrySession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    def get(self, _model: object, _identity: object) -> None:
        return None

    def scalar(self, statement: object) -> object | None:
        rendered = str(statement)
        if "FROM projects" in rendered:
            return SimpleNamespace(
                id=self.project_id, snapshot_confirmed_at=object()
            )
        if "FROM composition_jobs" in rendered:
            return self.job
        if "FROM composition_outbox" in rendered:
            return self.outbox
        raise AssertionError(rendered)


def _repository(session: _RetrySession) -> SqlAlchemyCompositionRepository:
    return SqlAlchemyCompositionRepository(
        lambda: session,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )


def _failed_rows() -> tuple[UUID, UUID, str, object, object]:
    project_id, owner_id, revision_id = uuid4(), uuid4(), uuid4()
    digest = "a" * 64
    job = SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        owner_id=owner_id,
        idempotency_key=f"compose:{owner_id}:{project_id}:{digest}",
        state="FAILED",
        attempt=1,
        error_code="composition_failed",
        workflow_generation=1,
        intent_sequence=4,
    )
    outbox = SimpleNamespace(
        revision_id=revision_id,
        project_id=project_id,
        owner_id=owner_id,
        inputs_sha256=digest,
        workflow_id=f"composition:{owner_id}:{project_id}:{digest}",
        state="DISPATCHED",
        attempt=1,
        workflow_generation=1,
        budget_round=1,
        lease_token=uuid4(),
        lease_until=object(),
        next_attempt_at=object(),
        dispatched_at=object(),
        last_error_code="internal",
    )
    return project_id, owner_id, digest, job, outbox


def test_failed_retry_reuses_revision_and_hash_with_one_new_workflow_generation() -> None:
    project_id, owner_id, digest, job, outbox = _failed_rows()
    old_current = SimpleNamespace(id=uuid4(), current=True)
    repository = _repository(_RetrySession(project_id, job, outbox))

    receipt = repository.retry_composition(project_id, owner_id)

    assert receipt.revision_id == outbox.revision_id
    assert outbox.inputs_sha256 == digest
    assert receipt.workflow_generation == outbox.workflow_generation == 2
    assert receipt.workflow_id == outbox.workflow_id
    assert receipt.workflow_id.endswith(":g2")
    assert receipt.state == outbox.state == "PENDING"
    assert outbox.attempt == 0
    assert outbox.dispatched_at is None
    assert outbox.lease_token is None and outbox.lease_until is None
    assert old_current.current is True

    duplicate = repository.retry_composition(project_id, owner_id)
    assert duplicate == receipt
    assert outbox.workflow_generation == 2
    assert old_current.current is True

    job.workflow_generation = 2
    job.state = "FAILED"
    outbox.state = "DISPATCHED"
    next_retry = repository.retry_composition(project_id, owner_id)
    assert next_retry.revision_id == receipt.revision_id
    assert next_retry.workflow_generation == 3
    assert next_retry.workflow_id.endswith(":g3")
    assert next_retry.workflow_id != receipt.workflow_id
    assert old_current.current is True


def test_failed_retry_rearms_superseded_row_after_completion() -> None:
    # §17.3：完成时收口的 SUPERSEDED 行，用户显式重试可重新武装（复用同一 revision 与哈希）。
    project_id, owner_id, digest, job, outbox = _failed_rows()
    outbox.state = "SUPERSEDED"
    repository = _repository(_RetrySession(project_id, job, outbox))

    receipt = repository.retry_composition(project_id, owner_id)

    assert receipt.revision_id == outbox.revision_id
    assert outbox.inputs_sha256 == digest
    assert receipt.workflow_generation == outbox.workflow_generation == 2
    assert receipt.workflow_id.endswith(":g2")
    assert receipt.state == outbox.state == "PENDING"
    assert outbox.attempt == 0
    assert outbox.dispatched_at is None


def test_failed_retry_rearms_superseded_row_with_ahead_generation() -> None:
    # 收口前自动重试已排程更新代际：显式重试在其上继续推进一个代际，不撞唯一键。
    project_id, owner_id, _digest, job, outbox = _failed_rows()
    outbox.state = "SUPERSEDED"
    outbox.workflow_generation = 3

    receipt = _repository(_RetrySession(project_id, job, outbox)).retry_composition(
        project_id, owner_id
    )

    assert receipt.workflow_generation == outbox.workflow_generation == 4
    assert receipt.workflow_id.endswith(":g4")
    assert receipt.state == outbox.state == "PENDING"


def test_running_retry_returns_same_task_without_new_generation() -> None:
    project_id, owner_id, _digest, job, outbox = _failed_rows()
    job.state = "RUNNING"
    outbox.state = "DISPATCHED"
    original_workflow_id = outbox.workflow_id

    receipt = _repository(_RetrySession(project_id, job, outbox)).retry_composition(
        project_id, owner_id
    )

    assert receipt.state == "RUNNING"
    assert receipt.workflow_generation == outbox.workflow_generation == 1
    assert receipt.workflow_id == outbox.workflow_id == original_workflow_id


def test_failed_job_can_only_begin_from_explicit_next_generation() -> None:
    project_id, owner_id, _digest, job, outbox = _failed_rows()
    repository = _repository(_RetrySession(project_id, job, outbox))

    with pytest.raises(DomainError) as error:
        repository.begin_composition(
            project_id, owner_id, job.idempotency_key, workflow_generation=1
        )
    assert error.value.code == "composition_retry_required"

    claim = repository.begin_composition(
        project_id, owner_id, job.idempotency_key, workflow_generation=2
    )
    assert claim.execution_token is not None
    assert job.state == "RUNNING"
    assert job.workflow_generation == 2
    assert job.attempt == 2
    assert job.error_code is None


def test_regular_api_replay_after_retry_finds_generation_by_immutable_hash() -> None:
    project_id, owner_id, _digest, job, outbox = _failed_rows()
    text = "narration"
    request = CompositionRequest(
        str(project_id),
        "b" * 64,
        (
            GroupInput(
                "g",
                0,
                "video",
                (NarrationSegmentInput("s", "tts", 1000, 48000, 48000),),
                1000,
            ),
        ),
        (
            SentenceTiming(
                "s", 0, text, hashlib.sha256(text.encode()).hexdigest(), 0, 1000
            ),
        ),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )
    payload = json.loads(json.dumps(dataclasses.asdict(request)))
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    job.idempotency_key = f"compose:{owner_id}:{project_id}:{digest}"
    job.state = "RUNNING"
    job.workflow_generation = 2
    outbox.inputs_sha256 = digest
    outbox.workflow_id = f"composition:{owner_id}:{project_id}:{digest}:g2"
    outbox.workflow_generation = 2
    outbox.state = "PENDING"

    class Authority:
        def load(self, _project_id: UUID, _owner_id: UUID) -> CompositionRequest:
            return request

    repository = SqlAlchemyCompositionRepository(
        lambda: _RetrySession(project_id, job, outbox),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        Authority(),
    )

    receipt = repository.enqueue_composition(project_id, owner_id)
    assert receipt.workflow_id == outbox.workflow_id
    assert receipt.workflow_generation == 2
    assert receipt.state == "RUNNING"


def test_unknown_or_succeeded_attempt_cannot_create_retry_generation() -> None:
    project_id, owner_id, _digest, job, outbox = _failed_rows()
    repository = _repository(_RetrySession(project_id, job, outbox))
    for state in ("REMOTE_STATE_UNKNOWN", "SUCCEEDED"):
        job.state = state
        with pytest.raises(DomainError) as error:
            repository.retry_composition(project_id, owner_id)
        assert error.value.code == "composition_retry_not_available"
        assert outbox.workflow_generation == 1


def test_retry_is_owner_isolated_before_attempt_lookup() -> None:
    project_id, owner_id, _digest, job, outbox = _failed_rows()

    class CrossOwnerSession(_RetrySession):
        def scalar(self, statement: object) -> object | None:
            if "FROM projects" in str(statement):
                return None
            raise AssertionError("attempt rows must not be queried before ownership passes")

    repository = _repository(CrossOwnerSession(project_id, job, outbox))
    with pytest.raises(DomainError) as error:
        repository.retry_composition(project_id, uuid4())
    assert error.value.code == "project_not_found"
    assert outbox.workflow_generation == 1


class _Noop:
    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: None


class _Authority:
    def resolve(self, *_args: object) -> MediaContent:
        return MediaContent("unused", "video/mp4", 1, "a" * 64)


class _Storage:
    def stat(self, _key: str) -> object:
        return SimpleNamespace(size=1)


def test_retry_api_and_attempts_expose_stable_composition_actions() -> None:
    project_id, owner_id = uuid4(), uuid4()
    latest = CompositionAttempt(uuid4(), "FAILED", 1, "composition_failed", None, 1)
    older = CompositionAttempt(uuid4(), "FAILED", 1, "composition_failed", None, 1)

    class Compositions:
        def attempts(self, _project_id: UUID, _owner_id: UUID) -> tuple[CompositionAttempt, ...]:
            return (latest, older)

    class Commands:
        seen: tuple[UUID, UUID] | None = None

        async def retry(self, selected_project: UUID, selected_owner: UUID) -> dict[str, object]:
            self.seen = (selected_project, selected_owner)
            return {
                "status": "PENDING",
                "workflow_id": "composition:retry:g2",
                "workflow_generation": 2,
            }

    commands = Commands()
    router = create_media_router(
        current_owner=lambda: AuthenticatedOwner(owner_id),
        compositions=Compositions(),  # type: ignore[arg-type]
        composition_commands=commands,  # type: ignore[arg-type]
        deletions=_Noop(),  # type: ignore[arg-type]
        deletion_status=_Noop(),  # type: ignore[arg-type]
        export_factory=lambda _project, _owner: iter(()),
        content_authority=_Authority(),  # type: ignore[arg-type]
        content_stream=MinioMediaStream(_Storage()),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(f"/projects/{project_id}/composition/retry")
    assert response.status_code == 202
    assert response.json() == {
        "status": "PENDING",
        "workflow_id": "composition:retry:g2",
        "workflow_generation": 2,
        "public_status": "QUEUED",
        "message": "Waiting to start.",
        "allowed_actions": ["REFRESH"],
    }
    assert commands.seen == (project_id, owner_id)

    attempts = client.get(f"/projects/{project_id}/composition/attempts").json()["items"]
    assert attempts[0]["allowed_actions"] == ["RETRY_COMPOSITION"]
    assert attempts[1]["allowed_actions"] == []
