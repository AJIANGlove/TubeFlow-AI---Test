from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from tubeflow_test.domain.storyboards import CharacterAssetSnapshot


class DirectorAttemptStatus(StrEnum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    LEASED = "LEASED"
    SUBMITTING = "SUBMITTING"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DirectorSentenceSnapshot:
    sentence_id: UUID
    order: int
    text: str
    text_sha256: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.order < 1 or not self.text or len(self.text_sha256) != 64:
            raise ValueError("director sentence snapshot is invalid")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("director sentence time range is invalid")


@dataclass(frozen=True, slots=True)
class DirectorInputSnapshot:
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    expected_draft_revision_id: UUID | None
    sentences: tuple[DirectorSentenceSnapshot, ...]
    characters: tuple[CharacterAssetSnapshot, ...]
    visual_style: str
    locale: str
    aspect_ratio: str


@dataclass(frozen=True, slots=True)
class DirectorGenerationAttempt:
    attempt_id: UUID
    owner_id: UUID
    group_id: UUID
    idempotency_key: str
    provider_idempotency_key: str
    canonical_request_sha256: str
    input_snapshot: DirectorInputSnapshot
    prompt_ref: str
    prompt_sha256: str
    prompt_content: str
    model: str
    parameters: dict[str, str | int | float | bool]
    status: DirectorAttemptStatus
    created_at: datetime
    state_revision: int = 0
    provider_receipt: str | None = None
    raw_response_sha256: str | None = None
    draft_revision_id: UUID | None = None
    failure_code: str | None = None
    history: tuple[str, ...] = ()
    lease_token: UUID | None = None
    lease_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class DirectorModelResult:
    payload: dict[str, object]
    receipt: str
    raw_response_sha256: str
