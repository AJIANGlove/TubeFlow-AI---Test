from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class NarrationWorkflowState(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class SynthesizeSentenceCommand:
    owner_id: str
    project_id: str
    sentence_sequence: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RecoverSentenceCommand:
    owner_id: str
    project_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class BuildMeasuredTimelineCommand:
    owner_id: str
    project_id: str


def may_submit_provider(state: NarrationWorkflowState | None) -> bool:
    """Only a brand-new user-triggered intent may cross the Provider boundary."""
    return state is None


def recovery_requires_manual_reconciliation(state: NarrationWorkflowState) -> bool:
    return state is NarrationWorkflowState.REMOTE_STATE_UNKNOWN
