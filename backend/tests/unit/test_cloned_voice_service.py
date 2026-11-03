"""§23.3/§23.4/§23.5 克隆音色管理编排测试（假 Provider + 内存仓储，不触真实网络）。

覆盖：逐账号克隆与如实报告、槽位满自动清理（保护跳过）、删除保护三类引用、源音频试听。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.domain import QuotaSnapshot
from tubeflow_test.elevenlabs.ports import PoolKeyEntry
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService
from tubeflow_test.identity_project.adapters import MemoryRepository
from tubeflow_test.identity_project.cloned_voice_service import ClonedVoiceService
from tubeflow_test.identity_project.cloned_voices import ClonedVoiceUpload
from tubeflow_test.identity_project.domain import (
    Account,
    AccountRole,
    Channel,
    ClonedVoice,
    ClonedVoiceAccount,
    ClonedVoiceUsage,
    TtsProvider,
)

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


class _Quota:
    def __init__(self, slots: int | None = 1, limit: int | None = 30) -> None:
        self.slots, self.limit = slots, limit
        self.calls: list[str] = []

    async def subscription(self, api_key: str) -> QuotaSnapshot:
        self.calls.append(api_key)
        return QuotaSnapshot(
            "creator", 100, 130_000, self.slots, self.limit, "active", True, None
        )

class _Pool:
    """号池端口假件（InMemory 语义；仅实现编排用到的读写）。"""

    def __init__(self) -> None:
        self.keys: dict[UUID, PoolKeyEntry] = {}

    def list_keys(self) -> tuple[PoolKeyEntry, ...]:
        return tuple(self.keys.values())

    def get_key(self, key_id: UUID) -> PoolKeyEntry | None:
        return self.keys.get(key_id)

    def find_by_api_key(self, api_key: str) -> PoolKeyEntry | None:
        return next((item for item in self.keys.values() if item.api_key == api_key), None)

    def add_key(self, *, api_key: str, label: str, now: datetime) -> PoolKeyEntry:
        raise NotImplementedError

    def set_status(
        self, key_id: UUID, status: str, *, last_error: str | None, now: datetime
    ) -> PoolKeyEntry | None:
        return None

    def delete_key(self, key_id: UUID) -> bool:
        return False

    def save_quota(self, key_id: UUID, quota: QuotaSnapshot, refreshed_at: datetime) -> None:
        entry = self.keys.get(key_id)
        if entry is None:
            return
        self.keys[key_id] = PoolKeyEntry(
            entry.id,
            entry.api_key,
            entry.label,
            entry.status,
            entry.added_at,
            entry.status_at,
            entry.last_error,
            quota.tier,
            quota.character_count,
            quota.character_limit,
            quota.voice_slots_used,
            quota.voice_limit,
            refreshed_at,
        )

    def record_event(self, key_id: UUID, event: str, detail: str, now: datetime) -> None:
        return None

    def list_events(
        self, *, key_id: UUID | None = None, limit: int = 50
    ) -> tuple[object, ...]:
        return ()

    def trim_events(self, key_id: UUID, keep: int) -> None:
        return None


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def put_at_key(self, key: str, body: bytes, media_type: str, expected_sha256: str) -> object:
        assert body
        self.objects[key] = body

        class _Stored:
            key = ""

        stored = _Stored()
        stored.key = key
        return stored

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        return self.objects[key]

    def delete_exact(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


class _Cloner:
    def __init__(self, *, fail_keys: frozenset[str] = frozenset()) -> None:
        self.cloned: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.account_voices: dict[str, tuple[str, ...]] = {}
        self.fail_keys = fail_keys
        self._counter = 0
        # 由 `_service` 注入：远端删除后账号槽位占用随之下降（模拟 EL 侧事实）。
        self.quota: _Quota | None = None

    async def clone_voice(
        self, api_key: str, name: str, *, audio: bytes, filename: str, description: str = ""
    ) -> str:
        assert audio and filename and name
        if api_key in self.fail_keys:
            raise DomainError("provider_rejected", "账号克隆被拒")
        self._counter += 1
        voice_id = f"el-{api_key}-{self._counter}"
        self.cloned.append((api_key, voice_id))
        self.account_voices[api_key] = (*self.account_voices.get(api_key, ()), voice_id)
        if self.quota is not None and self.quota.slots is not None:
            self.quota.slots += 1
        return voice_id

    async def list_account_voices(self, api_key: str) -> tuple[str, ...]:
        return self.account_voices.get(api_key, ())

    async def delete_voice(self, api_key: str, voice_id: str) -> None:
        self.deleted.append((api_key, voice_id))
        self.account_voices[api_key] = tuple(
            item for item in self.account_voices.get(api_key, ()) if item != voice_id
        )
        if self.quota is not None and self.quota.slots is not None:
            self.quota.slots = max(self.quota.slots - 1, 0)


def _service(
    slots: int | None = 1,
    limit: int | None = 30,
    *,
    fail_keys: frozenset[str] = frozenset(),
) -> tuple[ClonedVoiceService, MemoryRepository, _Cloner, _Storage, ElevenlabsKeyPoolService]:
    repository = MemoryRepository()
    storage = _Storage()
    quota = _Quota(slots, limit)
    cloner = _Cloner(fail_keys=fail_keys)
    cloner.quota = quota
    pool = ElevenlabsKeyPoolService(_Pool(), quota, clock=lambda: NOW)
    service = ClonedVoiceService(repository, storage, cloner, pool, clock=lambda: NOW)
    return service, repository, cloner, storage, pool


def _add_key(pool: ElevenlabsKeyPoolService, api_key: str, *, added_at: datetime = NOW) -> UUID:
    key_id = uuid4()
    pool._pool.keys[key_id] = PoolKeyEntry(  # type: ignore[attr-defined]
        id=key_id,
        api_key=api_key,
        label=api_key,
        status="active",
        added_at=added_at,
        status_at=added_at,
        last_error=None,
        quota_tier=None,
        character_count=None,
        character_limit=None,
        voice_slots_used=None,
        voice_limit=None,
        quota_refreshed_at=None,
    )
    return key_id


def _channel(repository: MemoryRepository, owner: Account) -> Channel:
    channel = Channel(uuid4(), owner.id, "https://example.test/channel")
    repository.add_channel(channel)
    return channel


def _owner() -> Account:
    return Account(uuid4(), "owner", "hash", AccountRole.USER)


def _upload(name: str = "旁白") -> ClonedVoiceUpload:
    return ClonedVoiceUpload(name, "备注", "sample.wav", b"audio-source", "audio/wav")


def test_upload_clones_into_every_active_account_and_records_mappings() -> None:
    service, repository, cloner, storage, pool = _service()
    owner = _owner()
    channel = _channel(repository, owner)
    first = _add_key(pool, "sk-first", added_at=NOW)
    second = _add_key(pool, "sk-second", added_at=NOW + timedelta(minutes=1))

    uploaded = asyncio.run(service.upload(owner.id, channel.id, _upload()))

    assert uploaded.failures == ()
    assert [item.el_voice_id for item in uploaded.outcomes] == [
        "el-sk-first-1",
        "el-sk-second-2",
    ]
    assert repository.cloned_voice_account(uploaded.voice.id, first) is not None
    assert repository.cloned_voice_account(uploaded.voice.id, second) is not None
    assert uploaded.voice.source_object_key.endswith("/source.wav")
    assert storage.objects[uploaded.voice.source_object_key] == b"audio-source"
    assert cloner.cloned == [("sk-first", "el-sk-first-1"), ("sk-second", "el-sk-second-2")]


def test_upload_reports_single_account_failure_without_blocking_others() -> None:
    service, repository, cloner, _, pool = _service(fail_keys=frozenset({"sk-bad"}))
    owner = _owner()
    channel = _channel(repository, owner)
    _add_key(pool, "sk-bad", added_at=NOW)
    _add_key(pool, "sk-good", added_at=NOW + timedelta(minutes=1))

    uploaded = asyncio.run(service.upload(owner.id, channel.id, _upload()))

    assert [item.ok for item in uploaded.outcomes] == [False, True]
    assert [item.error_code for item in uploaded.failures] == ["provider_rejected"]
    assert cloner.cloned == [("sk-good", "el-sk-good-1")]


def test_upload_auto_cleans_unreferenced_clone_when_slots_are_full() -> None:
    service, repository, cloner, storage, pool = _service(slots=2, limit=2)
    owner = _owner()
    channel = _channel(repository, owner)
    key_id = _add_key(pool, "sk-first")
    older = ClonedVoice(
        uuid4(),
        channel.id,
        owner.id,
        "更旧音色",
        "",
        "cloned-voices/older/source.wav",
        "a" * 64,
        10,
        "audio/wav",
        NOW - timedelta(days=6),
    )
    old = ClonedVoice(
        uuid4(),
        channel.id,
        owner.id,
        "旧音色",
        "",
        "cloned-voices/old/source.wav",
        "b" * 64,
        10,
        "audio/wav",
        NOW - timedelta(days=5),
    )
    for item, el_voice_id in ((older, "el-older"), (old, "el-old")):
        repository.add_cloned_voice(item)
        repository.save_cloned_voice_account(
            ClonedVoiceAccount(item.id, key_id, el_voice_id, NOW)
        )
        storage.objects[item.source_object_key] = b"old"
    cloner.account_voices["sk-first"] = ("el-older", "el-old")

    uploaded = asyncio.run(service.upload(owner.id, channel.id, _upload()))

    # 槽位 2/2 已满 → 只清理最旧的一条（needed=1），随后成功克隆。
    assert uploaded.failures == ()
    assert cloner.deleted == [("sk-first", "el-older")]
    # 清理后本地孤儿行与源音频同步删除；未清理的那条仍在。
    assert repository.cloned_voice_by_id(older.id) is None
    assert older.source_object_key in storage.deleted
    assert repository.cloned_voice_by_id(old.id) is not None


def test_slot_block_keeps_protected_clone_and_reports_honestly() -> None:
    service, repository, cloner, _, pool = _service(slots=2, limit=2)
    owner = _owner()
    channel = _channel(repository, owner)
    key_id = _add_key(pool, "sk-first")
    protected = ClonedVoice(
        uuid4(),
        channel.id,
        owner.id,
        "频道默认",
        "",
        "cloned-voices/protected/source.wav",
        "c" * 64,
        10,
        "audio/wav",
        NOW - timedelta(days=9),
    )
    repository.add_cloned_voice(protected)
    repository.save_cloned_voice_account(ClonedVoiceAccount(protected.id, key_id, "el-kept", NOW))
    repository.save_channel(
        Channel(
            channel.id,
            owner.id,
            channel.url,
            default_tts_provider=TtsProvider.ELEVENLABS.value,
            default_cloned_voice_id=protected.id,
        )
    )
    cloner.account_voices["sk-first"] = ("el-kept",)

    uploaded = asyncio.run(service.upload(owner.id, channel.id, _upload()))

    assert [item.error_code for item in uploaded.failures] == ["provider_voice_slots_full"]
    assert cloner.deleted == []
    assert repository.cloned_voice_by_id(protected.id) is not None


def test_delete_removes_remote_local_and_bytes() -> None:
    service, repository, cloner, storage, pool = _service()
    owner = _owner()
    channel = _channel(repository, owner)
    key_id = _add_key(pool, "sk-first")

    uploaded = asyncio.run(service.upload(owner.id, channel.id, _upload()))
    voice = uploaded.voice
    assert cloner.account_voices["sk-first"] == ("el-sk-first-1",)

    asyncio.run(service.delete(owner.id, channel.id, voice.id))

    assert cloner.deleted == [("sk-first", "el-sk-first-1")]
    assert repository.cloned_voice_by_id(voice.id) is None
    assert voice.source_object_key in storage.deleted
    assert repository.cloned_voice_account(voice.id, key_id) is None


def test_delete_is_blocked_by_draft_reference() -> None:
    service, repository, _, _, _ = _service()
    owner = _owner()
    channel = _channel(repository, owner)
    voice = ClonedVoice(
        uuid4(), channel.id, owner.id, "草稿音色", "", "k", "d" * 64, 1, "audio/wav", NOW
    )
    repository.add_cloned_voice(voice)
    repository.cloned_voice_usage = lambda cloned_voice_id: ClonedVoiceUsage(  # type: ignore[method-assign]
        draft_references=1
    )

    with pytest.raises(DomainError) as captured:
        asyncio.run(service.delete(owner.id, channel.id, voice.id))

    assert captured.value.code == "cloned_voice_draft_reference"
    assert repository.cloned_voice_by_id(voice.id) is not None


def test_get_source_returns_verified_bytes_and_scopes_to_channel() -> None:
    service, repository, _, storage, _ = _service()
    owner = _owner()
    channel = _channel(repository, owner)
    other = _channel(repository, owner)
    voice = ClonedVoice(
        uuid4(),
        channel.id,
        owner.id,
        "旁白",
        "",
        "cloned-voices/own/source.wav",
        "e" * 64,
        len(b"payload"),
        "audio/wav",
        NOW,
    )
    repository.add_cloned_voice(voice)
    storage.objects[voice.source_object_key] = b"payload"

    loaded, content = service.get_source(owner.id, channel.id, voice.id)

    assert loaded.id == voice.id and content == b"payload"
    with pytest.raises(DomainError) as captured:
        service.get_source(owner.id, other.id, voice.id)
    assert captured.value.code == "not_found"


def test_upload_rejects_duplicate_name_and_empty_source() -> None:
    service, repository, _, _, _ = _service()
    owner = _owner()
    channel = _channel(repository, owner)

    asyncio.run(service.upload(owner.id, channel.id, _upload("同名")))
    with pytest.raises(DomainError) as conflict:
        asyncio.run(service.upload(owner.id, channel.id, _upload("同名")))
    assert conflict.value.code == "cloned_voice_name_conflict"

    with pytest.raises(DomainError) as empty:
        asyncio.run(
            service.upload(
                owner.id,
                channel.id,
                ClonedVoiceUpload("新名", "", "a.wav", b"", "audio/wav"),
            )
        )
    assert empty.value.code == "invalid_cloned_voice_source"


def test_upload_clamps_slots_when_quota_is_unknown() -> None:
    """配额未知（未刷新过）→ 先免费刷新；刷新失败则如实报槽位不可用，不盲目克隆。"""
    service, repository, cloner, _, pool = _service(slots=None, limit=None)
    owner = _owner()
    channel = _channel(repository, owner)
    _add_key(pool, "sk-first")

    uploaded = asyncio.run(service.upload(owner.id, channel.id, _upload()))

    assert [item.error_code for item in uploaded.failures] == ["provider_voice_slots_full"]
    assert cloner.cloned == []


# ---------------------------------------------------------------------------
# HTTP 合同（§23.3）：走 `/identity` 前缀，Bearer + 频道所有权校验，试听用受保护媒体端点。
# ---------------------------------------------------------------------------


def _client(
    service: ClonedVoiceService,
    repository: MemoryRepository,
) -> tuple[TestClient, str]:
    from tubeflow_test.identity_project.router import create_router
    from tubeflow_test.identity_project.service import IdentityProjectService

    passwords = _Passwords()
    identity = IdentityProjectService(
        repository, passwords, setup_username="admin", setup_password="password1"
    )
    identity.initialize_admin("admin", "password1")
    app = FastAPI()
    app.include_router(create_router(identity, cloned_voice_service=lambda: service))
    client = TestClient(app)
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    return client, token


class _Passwords:
    def hash(self, password: str) -> str:
        return f"plain:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


def test_cloned_voice_http_upload_list_source_and_delete() -> None:
    service, repository, cloner, storage, pool = _service()
    _add_key(pool, "sk-first")
    client, token = _client(service, repository)
    headers = {"Authorization": f"Bearer {token}"}
    owner = repository.list_accounts()[0]
    channel = _channel(repository, owner)

    empty = client.get(f"/identity/channels/{channel.id}/cloned-voices", headers=headers)
    assert empty.status_code == 200 and empty.json() == []

    created = client.post(
        f"/identity/channels/{channel.id}/cloned-voices",
        headers=headers,
        data={"name": "旁白", "remark": "试听"},
        files={"file": ("sample.wav", b"audio-source", "audio/wav")},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    voice_id = payload["voice"]["id"]
    assert payload["voice"]["name"] == "旁白"
    assert payload["accounts"] == [
        {
            "key_id": payload["accounts"][0]["key_id"],
            "label": "sk-first",
            "ok": True,
            "el_voice_id": "el-sk-first-1",
            "error_code": "",
            "error_message": "",
        }
    ]

    listed = client.get(f"/identity/channels/{channel.id}/cloned-voices", headers=headers)
    assert [item["id"] for item in listed.json()] == [voice_id]
    assert listed.json()[0]["source_url"].endswith(f"/{voice_id}/source")

    source = client.get(
        f"/identity/channels/{channel.id}/cloned-voices/{voice_id}/source", headers=headers
    )
    assert source.status_code == 200
    assert source.content == b"audio-source"
    assert source.headers["cache-control"] == "private, no-cache"
    etag = source.headers["etag"]
    not_modified = client.get(
        f"/identity/channels/{channel.id}/cloned-voices/{voice_id}/source",
        headers={**headers, "If-None-Match": etag},
    )
    assert not_modified.status_code == 304
    partial = client.get(
        f"/identity/channels/{channel.id}/cloned-voices/{voice_id}/source",
        headers={**headers, "Range": "bytes=0-4"},
    )
    assert partial.status_code == 206 and partial.content == b"audio"

    deleted = client.delete(
        f"/identity/channels/{channel.id}/cloned-voices/{voice_id}", headers=headers
    )
    assert deleted.status_code == 204
    assert repository.cloned_voice_by_id(UUID(voice_id)) is None
    assert storage.deleted


def test_cloned_voice_http_requires_session_ownership_and_reports_protection() -> None:
    service, repository, _, _, pool = _service()
    _add_key(pool, "sk-first")
    client, token = _client(service, repository)
    owner = repository.list_accounts()[0]
    channel = _channel(repository, owner)

    unauthenticated = client.get(f"/identity/channels/{channel.id}/cloned-voices")
    assert unauthenticated.status_code in {401, 403, 422}

    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        f"/identity/channels/{channel.id}/cloned-voices",
        headers=headers,
        data={"name": "旁白"},
        files={"file": ("sample.wav", b"audio-source", "audio/wav")},
    )
    voice_id = created.json()["voice"]["id"]
    # 设为频道默认后受保护：同码 409，删除被拒。
    patched = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_tts_provider": "elevenlabs", "default_cloned_voice_id": voice_id},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["default_tts_provider"] == "elevenlabs"
    blocked = client.delete(
        f"/identity/channels/{channel.id}/cloned-voices/{voice_id}", headers=headers
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "cloned_voice_channel_default"


def test_cloned_voice_http_switch_provider_without_default_voice_is_blocked() -> None:
    service, repository, _, _, pool = _service()
    _add_key(pool, "sk-first")
    client, token = _client(service, repository)
    owner = repository.list_accounts()[0]
    channel = _channel(repository, owner)
    headers = {"Authorization": f"Bearer {token}"}

    rejected = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_tts_provider": "elevenlabs"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "channel_default_voice_required"

