import pytest

from tubeflow_test.api.public_status import (
    PublicStatus,
    deletion_public_state,
    public_state,
)


def test_unknown_remote_state_only_allows_recovery_query() -> None:
    state = public_state("REMOTE_STATE_UNKNOWN")
    assert state.public_status is PublicStatus.RECOVERY_REQUIRED
    assert state.allowed_actions == ("RECONCILE", "REFRESH")
    assert "RETRY" not in state.allowed_actions


def test_public_status_maps_success_and_failure_without_internal_details() -> None:
    assert public_state("SUCCEEDED").public_status is PublicStatus.SUCCEEDED
    assert public_state("FAILED").public_status is PublicStatus.FAILED


def test_public_status_maps_narration_queue_and_dispatch_states() -> None:
    assert public_state("PENDING").public_status is PublicStatus.QUEUED
    assert public_state("INTENT_RECORDED").public_status is PublicStatus.QUEUED
    assert public_state("DISPATCHED").public_status is PublicStatus.RUNNING
    assert public_state("SUBMISSION_STARTED").public_status is PublicStatus.RUNNING


def test_unrecognized_public_status_fails_closed() -> None:
    state = public_state("NEW_INTERNAL_PROVIDER_STATE")
    assert state.public_status is PublicStatus.RECOVERY_REQUIRED
    assert state.allowed_actions == ("REFRESH",)
    assert "RETRY" not in state.allowed_actions


@pytest.mark.parametrize(
    ("raw_status", "public_status", "message", "allowed_actions"),
    (
        ("ACTIVE", PublicStatus.NOT_STARTED, "Project deletion has not started.", ()),
        (
            "DELETING",
            PublicStatus.RUNNING,
            "Project deletion is in progress.",
            ("REFRESH",),
        ),
        (
            "DELETE_FAILED",
            PublicStatus.FAILED,
            "Project deletion could not be completed. Retry the existing deletion.",
            ("RETRY_DELETE", "REFRESH"),
        ),
        ("DELETED", PublicStatus.SUCCEEDED, "Project deletion completed.", ()),
    ),
)
def test_deletion_public_status_is_stable(
    raw_status: str,
    public_status: PublicStatus,
    message: str,
    allowed_actions: tuple[str, ...],
) -> None:
    state = deletion_public_state(raw_status)
    assert state.public_status is public_status
    assert state.message == message
    assert state.allowed_actions == allowed_actions


def test_unknown_deletion_status_only_allows_refresh() -> None:
    state = deletion_public_state("INTERNAL_PROVIDER_STATE")
    assert state.public_status is PublicStatus.RECOVERY_REQUIRED
    assert state.allowed_actions == ("REFRESH",)
