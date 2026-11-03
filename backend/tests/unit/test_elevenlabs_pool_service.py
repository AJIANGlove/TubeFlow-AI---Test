from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tubeflow_test.adapters.persistence.elevenlabs import InMemoryElevenlabsKeyPool
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.domain import EVENT_CAP, QuotaSnapshot, RecheckOutcome
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def _quota(
    character_count: int = 100,
    character_limit: int = 100_000,
    tier: str = "creator",
) -> QuotaSnapshot:
    return QuotaSnapshot(
        tier=tier,
        character_count=character_count,
        character_limit=character_limit,
        voice_slots_used=2,
        voice_limit=30,
        status="active",
        can_use_instant_voice_cloning=True,
        next_reset_unix=None,
    )


class _Quota:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.results: list[QuotaSnapshot | DomainError] = []

    def push(self, result: QuotaSnapshot | DomainError) -> None:
        self.results.append(result)

    async def subscription(self, api_key: str) -> QuotaSnapshot:
        self.calls.append(api_key)
        result = self.results.pop(0) if self.results else _quota()
        if isinstance(result, DomainError):
            raise result
        return result


def _service() -> tuple[
    ElevenlabsKeyPoolService, InMemoryElevenlabsKeyPool, _Quota, list[datetime]
]:
    pool = InMemoryElevenlabsKeyPool()
    quota = _Quota()
    clock = [NOW]
    service = ElevenlabsKeyPoolService(pool, quota, clock=lambda: clock[0])
    return service, pool, quota, clock


@pytest.mark.asyncio
async def test_add_key_verifies_subscription_and_caches_quota() -> None:
    service, _, quota, _ = _service()
    entry = await service.add_key(api_key="sk-live", label=" 主号 ")
    assert quota.calls == ["sk-live"]
    assert entry.status == "active"
    assert entry.label == "主号"
    assert entry.quota_tier == "creator"
    assert entry.character_count == 100
    assert entry.voice_limit == 30
    assert entry.quota_refreshed_at == NOW
    events = service.list_events()
    assert [item.event for item in events] == ["added"]


@pytest.mark.asyncio
async def test_add_key_strips_whitespace() -> None:
    service, _, quota, _ = _service()
    entry = await service.add_key(api_key="  sk-live  ")
    assert entry.api_key == "sk-live"
    assert quota.calls == ["sk-live"]


@pytest.mark.asyncio
async def test_add_key_rejects_invalid_key_without_storing() -> None:
    service, pool, quota, _ = _service()
    quota.push(DomainError("elevenlabs_key_invalid", "401"))
    with pytest.raises(DomainError) as captured:
        await service.add_key(api_key="sk-bad")
    assert captured.value.code == "elevenlabs_key_invalid"
    assert pool.list_keys() == ()
    assert service.list_events() == ()


@pytest.mark.asyncio
async def test_add_key_rejects_unreachable_without_storing() -> None:
    service, pool, quota, _ = _service()
    quota.push(DomainError("elevenlabs_unreachable", "timeout"))
    with pytest.raises(DomainError) as captured:
        await service.add_key(api_key="sk-slow")
    assert captured.value.code == "elevenlabs_unreachable"
    assert pool.list_keys() == ()


@pytest.mark.asyncio
async def test_add_key_rejects_duplicates_before_calling_provider() -> None:
    service, _, quota, _ = _service()
    await service.add_key(api_key="sk-live")
    with pytest.raises(DomainError) as captured:
        await service.add_key(api_key="sk-live")
    assert captured.value.code == "elevenlabs_key_duplicate"
    assert quota.calls == ["sk-live"]


@pytest.mark.asyncio
async def test_add_key_rejects_empty() -> None:
    service, _, quota, _ = _service()
    with pytest.raises(DomainError) as captured:
        await service.add_key(api_key="   ")
    assert captured.value.code == "elevenlabs_key_invalid"
    assert quota.calls == []


@pytest.mark.asyncio
async def test_enable_is_idempotent_and_recovers_status() -> None:
    service, pool, _, clock = _service()
    entry = await service.add_key(api_key="sk-live")
    service.disable(entry.id)
    clock[0] = NOW + timedelta(minutes=1)
    enabled = await service.enable(entry.id)
    assert enabled.status == "active"
    assert enabled.status_at == clock[0]
    assert enabled.last_error is None
    again = await service.enable(entry.id)
    assert again.status_at == clock[0]
    events = [item.event for item in service.list_events()]
    assert events.count("enabled") == 1


@pytest.mark.asyncio
async def test_enable_survives_quota_refresh_failure() -> None:
    service, pool, quota, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    service.disable(entry.id)
    quota.push(DomainError("elevenlabs_unreachable", "timeout"))
    enabled = await service.enable(entry.id)
    assert enabled.status == "active"
    assert "quota_refresh_failed" in [item.event for item in service.list_events()]


@pytest.mark.asyncio
async def test_disable_is_idempotent() -> None:
    service, _, _, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    disabled = service.disable(entry.id)
    assert disabled.status == "disabled"
    assert disabled.last_error is None
    service.disable(entry.id)
    events = [item.event for item in service.list_events()]
    assert events.count("disabled") == 1


@pytest.mark.asyncio
async def test_disable_missing_key_is_not_found() -> None:
    service, _, _, _ = _service()
    with pytest.raises(DomainError) as captured:
        service.disable(uuid4())
    assert captured.value.code == "not_found"


@pytest.mark.asyncio
async def test_delete_removes_key_and_its_events() -> None:
    service, _, _, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    service.delete(entry.id)
    assert service.list_keys() == ()
    assert service.list_events() == ()
    with pytest.raises(DomainError) as captured:
        service.delete(entry.id)
    assert captured.value.code == "not_found"


@pytest.mark.asyncio
async def test_refresh_quota_marks_invalid_on_auth_failure() -> None:
    service, _, quota, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    quota.push(DomainError("elevenlabs_key_invalid", "401"))
    with pytest.raises(DomainError) as captured:
        await service.refresh_quota(entry.id)
    assert captured.value.code == "elevenlabs_key_invalid"
    updated = service.list_keys()[0]
    assert updated.status == "invalid"
    assert updated.last_error is not None
    assert "marked_invalid" in [item.event for item in service.list_events()]


@pytest.mark.asyncio
async def test_refresh_quota_marks_exhausted_when_usage_reaches_limit() -> None:
    service, _, quota, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    quota.push(_quota(character_count=100_000, character_limit=100_000))
    updated = await service.refresh_quota(entry.id)
    assert updated.status == "exhausted"
    assert updated.character_count == 100_000
    events = [item.event for item in service.list_events()]
    assert "quota_refreshed" in events
    assert "marked_exhausted" in events


@pytest.mark.asyncio
async def test_refresh_quota_unreachable_keeps_status() -> None:
    service, _, quota, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    quota.push(DomainError("elevenlabs_unreachable", "timeout"))
    with pytest.raises(DomainError) as captured:
        await service.refresh_quota(entry.id)
    assert captured.value.code == "elevenlabs_unreachable"
    assert service.list_keys()[0].status == "active"
    assert "quota_refresh_failed" in [item.event for item in service.list_events()]


@pytest.mark.asyncio
async def test_rotate_on_failure_marks_exhausted_once() -> None:
    service, _, _, clock = _service()
    entry = await service.add_key(api_key="sk-live")
    assert service.rotate_on_failure(entry.id, status_code=402, recheck=None, detail="402") == (
        "exhausted"
    )
    assert service.list_keys()[0].status == "exhausted"
    clock[0] = NOW + timedelta(minutes=5)
    assert service.rotate_on_failure(entry.id, status_code=402, recheck=None, detail="402") == (
        "exhausted"
    )
    events = [item.event for item in service.list_events()]
    assert events.count("marked_exhausted") == 1


@pytest.mark.asyncio
async def test_rotate_on_failure_marks_invalid_only_after_recheck_rejects() -> None:
    service, _, _, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    recheck = RecheckOutcome(reachable=True, rejected=True)
    outcome = service.rotate_on_failure(entry.id, status_code=401, recheck=recheck, detail="401")
    assert outcome == "invalid"
    updated = service.list_keys()[0]
    assert updated.status == "invalid"
    assert updated.last_error == "401"


@pytest.mark.asyncio
async def test_rotate_on_failure_transient_never_marks_key() -> None:
    service, _, _, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    for status_code, recheck in (
        (403, None),
        (409, None),
        (503, None),
        (None, None),
        (401, None),
        (401, RecheckOutcome(reachable=False, rejected=False)),
    ):
        outcome = service.rotate_on_failure(
            entry.id, status_code=status_code, recheck=recheck, detail="x"
        )
        assert outcome == "transient"
    assert service.list_keys()[0].status == "active"
    assert "transient_failure" in [item.event for item in service.list_events()]


@pytest.mark.asyncio
async def test_rotate_on_failure_truncates_last_error() -> None:
    service, _, _, _ = _service()
    entry = await service.add_key(api_key="sk-live")
    service.rotate_on_failure(entry.id, status_code=402, recheck=None, detail="x" * 600)
    assert len(service.list_keys()[0].last_error or "") == 500


@pytest.mark.asyncio
async def test_events_dedup_same_content_within_60_seconds() -> None:
    service, _, _, clock = _service()
    entry = await service.add_key(api_key="sk-live")
    for _ in range(3):
        service.rotate_on_failure(entry.id, status_code=403, recheck=None, detail="same")
        clock[0] = clock[0] + timedelta(seconds=10)
    events = [item for item in service.list_events() if item.event == "transient_failure"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_events_roll_over_cap_per_key() -> None:
    service, _, _, clock = _service()
    entry = await service.add_key(api_key="sk-live")
    other = await service.add_key(api_key="sk-other")
    for index in range(EVENT_CAP + 5):
        clock[0] = NOW + timedelta(seconds=61 * (index + 1))
        service.rotate_on_failure(entry.id, status_code=403, recheck=None, detail=f"f{index}")
    key_events = service.list_events(limit=500)
    assert len([item for item in key_events if item.key_id == entry.id]) == EVENT_CAP
    assert any(item.key_id == other.id for item in key_events)


@pytest.mark.asyncio
async def test_current_key_selects_earliest_active() -> None:
    service, _, _, clock = _service()
    first = await service.add_key(api_key="sk-first")
    clock[0] = NOW + timedelta(minutes=1)
    second = await service.add_key(api_key="sk-second")
    current = service.current_key()
    assert current is not None and current.id == first.id
    service.disable(first.id)
    current = service.current_key()
    assert current is not None and current.id == second.id
    service.disable(second.id)
    assert service.current_key() is None
