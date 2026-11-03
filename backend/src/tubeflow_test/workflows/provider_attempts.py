from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class AttemptState(StrEnum):
    NEW = "new"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RemoteAttempt:
    idempotency_key: str
    state: AttemptState
    remote_id: str | None = None


class Provider(Protocol):
    def submit(self, idempotency_key: str) -> str: ...
    def poll(self, remote_id: str) -> AttemptState: ...


def advance_attempt(attempt: RemoteAttempt, provider: Provider) -> RemoteAttempt:
    """Submit exactly once; a known remote job is only ever queried."""
    if attempt.remote_id is not None:
        return RemoteAttempt(
            attempt.idempotency_key, provider.poll(attempt.remote_id), attempt.remote_id
        )
    if attempt.state is not AttemptState.NEW:
        raise RuntimeError("an uncertain attempt without remote id must be reconciled manually")
    remote_id = provider.submit(attempt.idempotency_key)
    return RemoteAttempt(attempt.idempotency_key, AttemptState.SUBMITTED, remote_id)
