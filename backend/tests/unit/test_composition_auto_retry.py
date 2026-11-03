from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyCompositionRepository,
)
from tubeflow_test.application.composition import (
    COMPOSITION_AUTO_MAX_ATTEMPTS,
    COMPOSITION_AUTO_RETRY_BACKOFF_SECONDS,
    CompositionClaim,
    CompositionVersion,
    ManualCompositionService,
)
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


class _AutoRetrySession:
    def __init__(
        self, project_id: UUID, job: object, outbox: object, production_mode: str
    ) -> None:
        self.project_id = project_id
        self.job = job
        self.outbox = outbox
        self.production_mode = production_mode

    def __enter__(self) -> _AutoRetrySession:
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
            return self.production_mode
        if "FROM composition_jobs" in rendered:
            return self.job
        if "FROM composition_outbox" in rendered:
            return self.outbox
        raise AssertionError(rendered)


def _repository(session: _AutoRetrySession) -> SqlAlchemyCompositionRepository:
    return SqlAlchemyCompositionRepository(
        lambda: session,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )


def _failed_rows(
    *, production_mode: str = "AUTO", attempt: int = 1
) -> tuple[UUID, UUID, str, UUID, object, object, _AutoRetrySession]:
    project_id, owner_id, revision_id = uuid4(), uuid4(), uuid4()
    digest = "a" * 64
    token = uuid4()
    job = SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        owner_id=owner_id,
        idempotency_key=f"compose:{owner_id}:{project_id}:{digest}",
        state="FAILED",
        attempt=attempt,
        error_code="composition_failed",
        workflow_generation=1,
        intent_sequence=4,
        execution_token=token,
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
        lease_token=uuid4(),
        lease_until=object(),
        next_attempt_at=object(),
        dispatched_at=object(),
        last_error_code="composition_failed",
    )
    session = _AutoRetrySession(project_id, job, outbox, production_mode)
    return project_id, owner_id, digest, token, job, outbox, session


def _schedule(
    repository: SqlAlchemyCompositionRepository,
    project_id: UUID,
    owner_id: UUID,
    job: object,
    token: UUID | None = None,
) -> bool:
    return repository.schedule_auto_retry(
        project_id,
        owner_id,
        job.idempotency_key,
        token if token is not None else job.execution_token,
        max_attempts=COMPOSITION_AUTO_MAX_ATTEMPTS,
        backoff_seconds=COMPOSITION_AUTO_RETRY_BACKOFF_SECONDS,
    )


def test_auto_transient_failure_rearms_outbox_with_five_second_backoff() -> None:
    project_id, owner_id, digest, _token, job, outbox, session = _failed_rows()
    repository = _repository(session)

    scheduled = _schedule(repository, project_id, owner_id, job)

    assert scheduled is True
    assert outbox.inputs_sha256 == digest
    assert outbox.workflow_generation == 2
    assert outbox.workflow_id.endswith(":g2")
    assert outbox.state == "PENDING"
    assert outbox.attempt == 0
    assert outbox.lease_token is None and outbox.lease_until is None
    assert outbox.dispatched_at is None and outbox.last_error_code is None
    delay = (outbox.next_attempt_at - datetime.now(UTC)).total_seconds()
    assert 4.0 < delay <= 5.0
    assert job.state == "FAILED" and job.attempt == 1


def test_auto_second_failure_uses_fifteen_second_backoff() -> None:
    project_id, owner_id, _digest, _token, job, outbox, session = _failed_rows(attempt=2)
    job.workflow_generation = 2
    outbox.workflow_generation = 2
    outbox.workflow_id = f"{outbox.workflow_id}:g2"
    repository = _repository(session)

    scheduled = _schedule(repository, project_id, owner_id, job)

    assert scheduled is True
    assert outbox.workflow_generation == 3
    assert outbox.workflow_id.endswith(":g3")
    assert outbox.state == "PENDING"
    delay = (outbox.next_attempt_at - datetime.now(UTC)).total_seconds()
    assert 14.0 < delay <= 15.0


def test_auto_third_failure_is_terminal_and_keeps_failed_state() -> None:
    project_id, owner_id, _digest, _token, job, outbox, session = _failed_rows(attempt=3)
    job.workflow_generation = 3
    outbox.workflow_generation = 3
    outbox.workflow_id = f"{outbox.workflow_id}:g3"
    repository = _repository(session)

    scheduled = _schedule(repository, project_id, owner_id, job)

    assert scheduled is False
    assert outbox.workflow_generation == 3
    assert outbox.state == "DISPATCHED"
    assert job.state == "FAILED" and job.attempt == 3


def test_manual_mode_failure_is_never_auto_rescheduled() -> None:
    project_id, owner_id, _digest, _token, job, outbox, session = _failed_rows(
        production_mode="MANUAL"
    )
    repository = _repository(session)

    scheduled = _schedule(repository, project_id, owner_id, job)

    assert scheduled is False
    assert outbox.workflow_generation == 1
    assert outbox.state == "DISPATCHED"
    assert outbox.workflow_id.endswith(":g2") is False


def test_stale_execution_token_does_not_rearm() -> None:
    project_id, owner_id, _digest, _token, job, outbox, session = _failed_rows()
    repository = _repository(session)

    scheduled = _schedule(repository, project_id, owner_id, job, token=uuid4())

    assert scheduled is False
    assert outbox.workflow_generation == 1
    assert outbox.state == "DISPATCHED"


def test_newer_outbox_generation_is_not_rearmed_again() -> None:
    project_id, owner_id, _digest, _token, job, outbox, session = _failed_rows()
    outbox.workflow_generation = 2
    outbox.state = "PENDING"
    repository = _repository(session)

    scheduled = _schedule(repository, project_id, owner_id, job)

    assert scheduled is False
    assert outbox.workflow_generation == 2
    assert outbox.state == "PENDING"


def test_double_schedule_rearms_only_one_new_generation() -> None:
    project_id, owner_id, _digest, _token, job, outbox, session = _failed_rows()
    repository = _repository(session)

    first = _schedule(repository, project_id, owner_id, job)
    second = _schedule(repository, project_id, owner_id, job)

    assert first is True and second is False
    assert outbox.workflow_generation == 2
    assert outbox.workflow_id.endswith(":g2")


def _request(project_id: UUID) -> CompositionRequest:
    text = "narration"
    return CompositionRequest(
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


class _RecordingRepository:
    def __init__(self, request: CompositionRequest) -> None:
        self.request = request
        self.token = uuid4()
        self.events: list[tuple[str, object]] = []

    def load_revision(
        self, revision_id: UUID, project_id: UUID, owner_id: UUID
    ) -> CompositionRequest:
        return self.request

    def begin_composition(
        self,
        project_id: UUID,
        owner_id: UUID,
        idempotency_key: str,
        workflow_generation: int,
    ) -> CompositionClaim:
        return CompositionClaim(self.token)

    def fail_composition(
        self, idempotency_key: str, execution_token: UUID, error_code: str
    ) -> None:
        self.events.append(("fail", error_code))

    def schedule_auto_retry(
        self,
        project_id: UUID,
        owner_id: UUID,
        idempotency_key: str,
        execution_token: UUID,
        *,
        max_attempts: int,
        backoff_seconds: tuple[float, ...],
    ) -> bool:
        self.events.append(("schedule", (max_attempts, backoff_seconds)))
        return True


class _FailingComposer:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def compose(self, _request: CompositionRequest) -> tuple[object, dict[str, object]]:
        self.calls += 1
        raise self.error


def _service(
    repository: _RecordingRepository, composer: _FailingComposer
) -> ManualCompositionService:
    return ManualCompositionService(repository, composer)  # type: ignore[arg-type]


def test_service_schedules_auto_retry_after_recording_transient_failure() -> None:
    project_id, owner_id = uuid4(), uuid4()
    repository = _RecordingRepository(_request(project_id))
    composer = _FailingComposer(OSError("ffmpeg process crashed"))
    service = _service(repository, composer)

    with pytest.raises(OSError):
        service.compose_revision(uuid4(), project_id, owner_id)

    assert composer.calls == 1
    assert repository.events == [
        ("fail", "composition_failed"),
        ("schedule", (3, (5.0, 15.0, 30.0))),
    ]


@pytest.mark.parametrize(
    "code,scheduled",
    [
        ("media_command_failed", True),
        ("media_command_timeout", True),
        # 崩溃杀进程同样表现为 cancelled，属临时故障（真实清理由 purge 门禁拦截）。
        ("media_command_cancelled", True),
        ("composition_inputs_not_ready", False),
        ("group_video_too_short", False),
        ("composition_state_corrupt", False),
    ],
)
def test_service_auto_retry_follows_failure_classification(code: str, scheduled: bool) -> None:
    project_id, owner_id = uuid4(), uuid4()
    repository = _RecordingRepository(_request(project_id))
    composer = _FailingComposer(DomainError(code, "failure"))
    service = _service(repository, composer)

    with pytest.raises(DomainError) as error:
        service.compose_revision(uuid4(), project_id, owner_id)

    assert error.value.code == code
    assert repository.events[0] == ("fail", code)
    scheduled_events = [name for name, _payload in repository.events if name == "schedule"]
    assert scheduled_events == (["schedule"] if scheduled else [])


def test_retried_execution_replay_returns_existing_version_without_recomposing() -> None:
    project_id, owner_id = uuid4(), uuid4()
    existing = CompositionVersion(uuid4(), "video", "srt", {}, current=True)
    repository = _RecordingRepository(_request(project_id))
    composer = _FailingComposer(AssertionError("must not compose again"))

    def existing_claim(
        _project_id: UUID,
        _owner_id: UUID,
        _idempotency_key: str,
        _workflow_generation: int,
    ) -> CompositionClaim:
        return CompositionClaim(None, existing)

    repository.begin_composition = existing_claim  # type: ignore[method-assign]
    service = _service(repository, composer)

    result = service.compose_revision(uuid4(), project_id, owner_id)

    assert result is existing
    assert composer.calls == 0
    assert repository.events == []
