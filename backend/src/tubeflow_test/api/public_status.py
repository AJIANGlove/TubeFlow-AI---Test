from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PublicStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class PublicState:
    public_status: PublicStatus
    message: str
    allowed_actions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "public_status": self.public_status.value,
            "message": self.message,
            "allowed_actions": list(self.allowed_actions),
        }


def public_state(
    raw_status: object,
    *,
    success_actions: tuple[str, ...] = (),
    running_actions: tuple[str, ...] = ("REFRESH",),
    failure_actions: tuple[str, ...] = ("RETRY",),
) -> PublicState:
    raw = getattr(raw_status, "value", raw_status)
    state = str(raw or "NOT_STARTED")
    if state in {"REMOTE_STATE_UNKNOWN", "UNKNOWN"}:
        return PublicState(
            PublicStatus.RECOVERY_REQUIRED,
            "The remote result is uncertain. Query the existing attempt before any new submission.",
            ("RECONCILE", "REFRESH"),
        )
    if state in {"READY", "COMPLETED", "SUCCEEDED", "ARCHIVED"}:
        return PublicState(PublicStatus.SUCCEEDED, "Completed successfully.", success_actions)
    if state in {"FAILED", "PARTIAL_FAILED", "PROVIDER_REJECTED", "OUTPUT_INVALID"}:
        return PublicState(PublicStatus.FAILED, "The attempt failed.", failure_actions)
    if state == "CANCELLED":
        return PublicState(PublicStatus.CANCELLED, "The attempt was cancelled.", ())
    if state in {
        "INTENT_PERSISTED",
        "INTENT_RECORDED",
        "CREATED",
        "PENDING",
        "QUEUED",
        "NOT_SUBMITTED",
    }:
        return PublicState(PublicStatus.QUEUED, "Waiting to start.", running_actions)
    if state in {"DISPATCHED", "SUBMISSION_STARTED", "RUNNING", "PROCESSING"}:
        return PublicState(PublicStatus.RUNNING, "Processing.", running_actions)
    if state in {"NOT_READY", "NOT_STARTED"}:
        return PublicState(PublicStatus.NOT_STARTED, "Not started.", ())
    return PublicState(
        PublicStatus.RECOVERY_REQUIRED,
        "The task status is not recognized. Refresh before taking further action.",
        ("REFRESH",),
    )


def deletion_public_state(raw_status: object) -> PublicState:
    """Expose deletion progress without leaking cleanup implementation details."""
    raw = getattr(raw_status, "value", raw_status)
    state = str(raw or "ACTIVE")
    if state == "ACTIVE":
        return PublicState(PublicStatus.NOT_STARTED, "Project deletion has not started.", ())
    if state == "DELETING":
        return PublicState(
            PublicStatus.RUNNING,
            "Project deletion is in progress.",
            ("REFRESH",),
        )
    if state == "DELETE_FAILED":
        return PublicState(
            PublicStatus.FAILED,
            "Project deletion could not be completed. Retry the existing deletion.",
            ("RETRY_DELETE", "REFRESH"),
        )
    if state == "DELETED":
        return PublicState(PublicStatus.SUCCEEDED, "Project deletion completed.", ())
    return PublicState(
        PublicStatus.RECOVERY_REQUIRED,
        "Project deletion status is uncertain. Refresh before taking further action.",
        ("REFRESH",),
    )
