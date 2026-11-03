from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.adapters.persistence.elevenlabs import InMemoryElevenlabsKeyPool
from tubeflow_test.api.elevenlabs_pool import create_router
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.domain import QuotaSnapshot
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


class _Quota:
    def __init__(self) -> None:
        self.error: DomainError | None = None

    async def subscription(self, api_key: str) -> QuotaSnapshot:
        if self.error is not None:
            raise self.error
        return QuotaSnapshot("creator", 10, 100_000, 1, 30, "active", True, None)


def _client() -> tuple[TestClient, _Quota]:
    quota = _Quota()
    service = ElevenlabsKeyPoolService(InMemoryElevenlabsKeyPool(), quota, clock=lambda: NOW)
    app = FastAPI()
    app.include_router(create_router(service))
    return TestClient(app), quota


def test_key_lifecycle_over_http() -> None:
    client, _ = _client()
    created = client.post("/elevenlabs-pool/keys", json={"api_key": "sk-live", "label": "主号"})
    assert created.status_code == 201
    key = created.json()["key"]
    assert key["status"] == "active"
    assert key["api_key"] == "sk-live"
    assert key["quota"]["character_limit"] == 100_000
    assert key["quota"]["refreshed_at"] == NOW.isoformat()

    listed = client.get("/elevenlabs-pool/keys")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["keys"]] == [key["id"]]

    disabled = client.post(f"/elevenlabs-pool/keys/{key['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["key"]["status"] == "disabled"

    enabled = client.post(f"/elevenlabs-pool/keys/{key['id']}/enable")
    assert enabled.status_code == 200
    assert enabled.json()["key"]["status"] == "active"

    refreshed = client.post(f"/elevenlabs-pool/keys/{key['id']}/refresh-quota")
    assert refreshed.status_code == 200

    events = client.get("/elevenlabs-pool/events?limit=50")
    assert events.status_code == 200
    names = [item["event"] for item in events.json()["events"]]
    assert {"added", "disabled", "enabled", "quota_refreshed"} <= set(names)

    deleted = client.delete(f"/elevenlabs-pool/keys/{key['id']}")
    assert deleted.status_code == 204
    assert client.get("/elevenlabs-pool/keys").json()["keys"] == []


def test_add_key_error_mapping() -> None:
    client, quota = _client()
    quota.error = DomainError("elevenlabs_key_invalid", "401")
    invalid = client.post("/elevenlabs-pool/keys", json={"api_key": "sk-bad"})
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "elevenlabs_key_invalid"

    quota.error = None
    assert client.post("/elevenlabs-pool/keys", json={"api_key": "sk-live"}).status_code == 201
    duplicate = client.post("/elevenlabs-pool/keys", json={"api_key": "sk-live"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "elevenlabs_key_duplicate"

    quota.error = DomainError("elevenlabs_unreachable", "timeout")
    unreachable = client.post("/elevenlabs-pool/keys", json={"api_key": "sk-new"})
    assert unreachable.status_code == 422
    assert unreachable.json()["detail"]["code"] == "elevenlabs_unreachable"


def test_unknown_key_is_404() -> None:
    client, _ = _client()
    missing = "00000000-0000-0000-0000-000000000001"
    assert client.post(f"/elevenlabs-pool/keys/{missing}/disable").status_code == 404
    assert client.post(f"/elevenlabs-pool/keys/{missing}/enable").status_code == 404
    assert client.post(f"/elevenlabs-pool/keys/{missing}/refresh-quota").status_code == 404
    assert client.delete(f"/elevenlabs-pool/keys/{missing}").status_code == 404


def test_add_key_requires_api_key() -> None:
    client, _ = _client()
    assert client.post("/elevenlabs-pool/keys", json={"api_key": ""}).status_code == 422
    assert client.post("/elevenlabs-pool/keys", json={"label": "x"}).status_code == 422
