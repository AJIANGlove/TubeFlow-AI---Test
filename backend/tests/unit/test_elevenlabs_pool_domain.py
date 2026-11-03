from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from tubeflow_test.elevenlabs.domain import (
    EVENT_CAP,
    LAST_ERROR_MAX_CHARS,
    KeyStatus,
    PoolKey,
    RecheckOutcome,
    classify_key_failure,
    event_duplicate,
    select_current_key,
    truncate_last_error,
)

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def _key(status: KeyStatus, added_at: datetime, key_id: UUID | None = None) -> PoolKey:
    return PoolKey(
        id=key_id or uuid4(),
        label="",
        status=status,
        added_at=added_at,
        status_at=added_at,
        last_error=None,
    )


def test_402_is_exhausted_without_recheck() -> None:
    assert classify_key_failure(402, None) == "exhausted"
    assert classify_key_failure(402, RecheckOutcome(reachable=True, rejected=False)) == "exhausted"


def test_401_requires_recheck_before_marking() -> None:
    assert classify_key_failure(401, None) == "transient"
    assert classify_key_failure(401, RecheckOutcome(reachable=False, rejected=False)) == "transient"
    assert classify_key_failure(401, RecheckOutcome(reachable=True, rejected=True)) == "invalid"
    assert classify_key_failure(401, RecheckOutcome(reachable=True, rejected=False)) == "exhausted"


def test_request_level_failures_are_always_transient() -> None:
    for status_code in (400, 403, 404, 409, 429, 500, 503, None):
        assert classify_key_failure(status_code, None) == "transient"
        assert (
            classify_key_failure(status_code, RecheckOutcome(reachable=True, rejected=True))
            == "transient"
        )


def test_select_current_key_picks_earliest_active() -> None:
    older = _key("active", NOW)
    newer = _key("active", NOW + timedelta(minutes=1))
    disabled = _key("disabled", NOW - timedelta(hours=1))
    assert select_current_key(()) is None
    assert select_current_key((disabled,)) is None
    assert select_current_key((newer, disabled, older)) == older


def test_select_current_key_breaks_added_at_ties_by_id() -> None:
    low = _key("active", NOW, UUID("00000000-0000-0000-0000-000000000001"))
    high = _key("active", NOW, UUID("00000000-0000-0000-0000-000000000002"))
    assert select_current_key((high, low)) == low


def test_truncate_last_error_caps_at_500_chars() -> None:
    assert truncate_last_error("short") == "short"
    assert len(truncate_last_error("x" * 600)) == LAST_ERROR_MAX_CHARS


def test_event_duplicate_window() -> None:
    recent = (("marked_exhausted", NOW),)
    assert event_duplicate(recent, "marked_exhausted", NOW + timedelta(seconds=59))
    assert not event_duplicate(recent, "marked_exhausted", NOW + timedelta(seconds=61))
    assert not event_duplicate(recent, "marked_invalid", NOW + timedelta(seconds=1))
    assert not event_duplicate((), "added", NOW)


def test_event_cap_is_50() -> None:
    assert EVENT_CAP == 50
