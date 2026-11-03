from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from tubeflow_test.video_generation.orchestration import PersistentAttempt
from tubeflow_test.video_generation.persistence import (
    VIDEO_GENERATION_TABLES,
    VideoGenerationBase,
    VideoOutboxRecord,
)
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_generation.worker import dispatch_video_outbox_once


def outbox() -> tuple[Engine, sessionmaker[Session], PersistentAttempt]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    attempt = PersistentAttempt(
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "relay", "a" * 64, "b" * 64, {}
    )
    repository = SqlAlchemyVideoRepository(
        sessions, lambda *_args: (_ for _ in ()).throw(AssertionError()), lambda value: value
    )
    repository.create_attempt(attempt)
    return engine, sessions, attempt


@pytest.mark.asyncio
async def test_start_accepted_then_timeout_describe_confirms_one_logical_start() -> None:
    engine, sessions, attempt = outbox()

    class Handle:
        async def describe(self) -> object:
            return object()

    class Client:
        ids: list[str] = []

        async def start_workflow(self, *_args: object, **kwargs: object) -> None:
            self.ids.append(str(kwargs["id"]))
            raise TimeoutError("response lost after acceptance")

        def get_workflow_handle(self, workflow_id: str) -> Handle:
            assert workflow_id == f"group-video-{attempt.attempt_id}"
            return Handle()

    client = Client()
    assert await dispatch_video_outbox_once(client, sessions, task_queue="h3") == 1
    assert client.ids == [f"group-video-{attempt.attempt_id}"]
    with sessions() as session:
        row = session.get(VideoOutboxRecord, attempt.attempt_id)
        assert row is not None and row.terminal_state == "DISPATCHED"
    engine.dispose()


@pytest.mark.asyncio
async def test_already_started_is_success() -> None:
    engine, sessions, attempt = outbox()

    class Client:
        async def start_workflow(self, *_args: object, **kwargs: object) -> None:
            raise WorkflowAlreadyStartedError(str(kwargs["id"]), "group-video-recovery-v1")

    assert await dispatch_video_outbox_once(Client(), sessions, task_queue="h3") == 1
    with sessions() as session:
        row = session.get(VideoOutboxRecord, attempt.attempt_id)
        assert row is not None and row.terminal_state == "DISPATCHED"
    engine.dispose()


@pytest.mark.asyncio
async def test_transient_rpc_error_and_describe_error_remain_unknown() -> None:
    engine, sessions, attempt = outbox()

    class Handle:
        async def describe(self) -> object:
            raise RPCError("still unavailable", RPCStatusCode.UNAVAILABLE, b"")

    class Client:
        async def start_workflow(self, *_args: object, **_kwargs: object) -> None:
            raise RPCError("deadline", RPCStatusCode.DEADLINE_EXCEEDED, b"")

        def get_workflow_handle(self, _workflow_id: str) -> Handle:
            return Handle()

    client = Client()
    for expected_count in range(1, 12):
        assert await dispatch_video_outbox_once(client, sessions, task_queue="h3") == 0
        with sessions.begin() as session:
            row = session.get(VideoOutboxRecord, attempt.attempt_id)
            assert row is not None
            assert row.retry_count == expected_count
            assert row.terminal_state == "UNKNOWN"
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    engine.dispose()


@pytest.mark.asyncio
async def test_deterministic_rpc_contract_error_is_dead_lettered() -> None:
    engine, sessions, attempt = outbox()

    class Client:
        async def start_workflow(self, *_args: object, **_kwargs: object) -> None:
            raise RPCError("invalid", RPCStatusCode.INVALID_ARGUMENT, b"")

    assert await dispatch_video_outbox_once(Client(), sessions, task_queue="h3") == 0
    with sessions() as session:
        row = session.get(VideoOutboxRecord, attempt.attempt_id)
        assert row is not None
        assert row.terminal_state == "DEAD_LETTER"
        assert row.last_error == "RPCError:INVALID_ARGUMENT"
    engine.dispose()


@pytest.mark.asyncio
async def test_start_and_describe_uncertain_remains_unknown_past_retry_cap() -> None:
    engine, sessions, attempt = outbox()

    class Handle:
        async def describe(self) -> object:
            raise TimeoutError("describe uncertain")

    class TimeoutClient:
        ids: list[str] = []

        async def start_workflow(self, *_args: object, **_kwargs: object) -> None:
            self.ids.append(str(_kwargs["id"]))
            raise TimeoutError("start uncertain")

        def get_workflow_handle(self, workflow_id: str) -> Handle:
            assert workflow_id == f"group-video-{attempt.attempt_id}"
            return Handle()

    client = TimeoutClient()
    for expected_count in range(1, 12):
        assert await dispatch_video_outbox_once(client, sessions, task_queue="h3") == 0
        with sessions.begin() as session:
            row = session.get(VideoOutboxRecord, attempt.attempt_id)
            assert row is not None
            assert row.retry_count == expected_count
            assert row.last_error == "start=TimeoutError;describe=TimeoutError"
            assert row.lease_token is None and row.lease_expires_at is None
            assert row.terminal_state == "UNKNOWN"
            assert row.next_attempt_at is not None
            row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    assert set(client.ids) == {f"group-video-{attempt.attempt_id}"}
    engine.dispose()


@pytest.mark.asyncio
async def test_nonrecoverable_dispatch_dead_letters_without_hot_loop() -> None:
    engine, sessions, attempt = outbox()

    class InvalidClient:
        async def start_workflow(self, *_args: object, **_kwargs: object) -> None:
            raise ValueError("invalid workflow contract")

    client = InvalidClient()
    assert await dispatch_video_outbox_once(client, sessions, task_queue="h3") == 0
    assert await dispatch_video_outbox_once(client, sessions, task_queue="h3") == 0
    with sessions() as session:
        row = session.get(VideoOutboxRecord, attempt.attempt_id)
        assert row is not None
        assert (row.retry_count, row.last_error, row.terminal_state) == (
            1,
            "ValueError",
            "DEAD_LETTER",
        )
    engine.dispose()
