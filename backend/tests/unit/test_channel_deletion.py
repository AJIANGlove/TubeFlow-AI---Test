"""频道删除（2026-10-20 用户确认）的服务层与 HTTP 合同测试。

覆盖：确认值口径（名称优先/未命名回退 URL）、项目门禁零副作用、非本人 404、
资产与远端副本清理、清理失败如实计数、二次删除 404、服务未装配 503。
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.ports import PoolKeyEntry
from tubeflow_test.identity_project.adapters import MemoryRepository
from tubeflow_test.identity_project.channel_deletion import ChannelDeletionService
from tubeflow_test.identity_project.cloned_voice_service import ClonedVoiceService
from tubeflow_test.identity_project.domain import (
    Account,
    AccountRole,
    Channel,
    channel_deletion_confirmation,
)
from tubeflow_test.identity_project.ports import (
    ChannelDeletionSummary,
    ClonedVoiceRemoteCopy,
    ClonedVoiceRemoteFailure,
)
from tubeflow_test.identity_project.router import create_router
from tubeflow_test.identity_project.service import IdentityProjectService

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
OWNER = Account(uuid4(), "owner", "hashed:x", AccountRole.USER)


class PlainPasswords:
    def hash(self, password: str) -> str:
        return f"hashed:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


class _DeletionRepo:
    """内存实现：与 SQL 适配器同语义（项目门禁、确认值、行删除）。"""

    def __init__(self, repo: MemoryRepository) -> None:
        self.repo = repo
        self.project_counts: dict[UUID, int] = {}
        self.template_counts: dict[UUID, int] = {}
        self.assets: dict[
            UUID, tuple[int, int, int, int, tuple[str, ...], tuple[ClonedVoiceRemoteCopy, ...]]
        ] = {}

    def delete_channel(
        self, owner_id: UUID, channel_id: UUID, confirmation: str
    ) -> ChannelDeletionSummary:
        channel = self.repo.channel(owner_id, channel_id)
        if channel is None:
            raise DomainError("not_found", "Resource not found")
        count = self.project_counts.get(channel_id, 0)
        if count:
            raise DomainError(
                "channel_has_projects",
                f"该频道下还有 {count} 个项目，请先删除项目后再删除频道。",
            )
        if confirmation != channel_deletion_confirmation(channel):
            raise DomainError(
                "channel_deletion_confirmation_required",
                "请输入完整的频道名称或频道 URL 以确认删除。",
            )
        self.repo.channels.pop(channel_id, None)
        counts = self.assets.get(channel_id, (0, 0, 0, 0, (), ()))
        return ChannelDeletionSummary(
            channel_id, *counts, cover_templates=self.template_counts.get(channel_id, 0)
        )


class _Objects:
    def __init__(self, fail: frozenset[str] = frozenset()) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    def delete_exact(self, key: str) -> None:
        if key in self.fail:
            raise RuntimeError("minio unavailable")
        self.deleted.append(key)


class _Remote:
    def __init__(self, failures: tuple[ClonedVoiceRemoteFailure, ...] = ()) -> None:
        self.calls: list[tuple[ClonedVoiceRemoteCopy, ...]] = []
        self.failures = failures
        self.raise_error = False

    async def delete_remote_copies(
        self, copies: tuple[ClonedVoiceRemoteCopy, ...]
    ) -> tuple[ClonedVoiceRemoteFailure, ...]:
        self.calls.append(copies)
        if self.raise_error:
            raise RuntimeError("elevenlabs unavailable")
        return self.failures


def _wired(
    *,
    name: str | None = None,
    fail_keys: frozenset[str] = frozenset(),
) -> tuple[ChannelDeletionService, Channel, MemoryRepository, _Objects, _Remote, _DeletionRepo]:
    repo = MemoryRepository()
    channel = Channel(uuid4(), OWNER.id, "https://www.youtube.com/@delete-me", name=name)
    repo.add_channel(channel)
    deletion_repo = _DeletionRepo(repo)
    objects = _Objects(fail_keys)
    remote = _Remote()
    return (
        ChannelDeletionService(deletion_repo, objects, remote),
        channel,
        repo,
        objects,
        remote,
        deletion_repo,
    )


def _code(operation: object) -> str:
    with pytest.raises(DomainError) as raised:
        operation()  # type: ignore[operator]
    return raised.value.code


# ---------------------------------------------------------------- 确认值口径


def test_named_channel_requires_exact_name() -> None:
    service, channel, *_ = _wired(name="测试频道")
    assert channel_deletion_confirmation(channel) == "测试频道"
    assert _code(lambda: asyncio.run(
        service.delete(OWNER, channel.id, "频道 01")
    )) == "channel_deletion_confirmation_required"
    assert _code(lambda: asyncio.run(
        service.delete(OWNER, channel.id, channel.url)
    )) == "channel_deletion_confirmation_required"


def test_unnamed_channel_uses_full_url() -> None:
    service, channel, repo, objects, remote, _ = _wired(name="  ")
    assert channel_deletion_confirmation(channel) == channel.url
    result = asyncio.run(service.delete(OWNER, channel.id, channel.url))
    assert result.summary.channel_id == channel.id
    assert channel.id not in repo.channels
    assert objects.deleted == []
    assert remote.calls == []


# ---------------------------------------------------------------- 门禁与归属


def test_channel_with_projects_is_rejected_without_side_effects() -> None:
    service, channel, repo, objects, remote, deletion_repo = _wired(name="有项目")
    deletion_repo.project_counts[channel.id] = 3
    assert _code(lambda: asyncio.run(
        service.delete(OWNER, channel.id, "有项目")
    )) == "channel_has_projects"
    assert channel.id in repo.channels
    assert objects.deleted == []
    assert remote.calls == []


def test_other_owner_cannot_delete_channel() -> None:
    service, channel, repo, *_ = _wired(name="他人频道")
    stranger = Account(uuid4(), "stranger", "hash", AccountRole.USER)
    assert _code(lambda: asyncio.run(
        service.delete(stranger, channel.id, "他人频道")
    )) == "not_found"
    assert channel.id in repo.channels


def test_second_delete_is_not_found() -> None:
    service, channel, *_ = _wired(name="只删一次")
    asyncio.run(service.delete(OWNER, channel.id, "只删一次"))
    assert _code(lambda: asyncio.run(
        service.delete(OWNER, channel.id, "只删一次")
    )) == "not_found"


# ---------------------------------------------------------------- 清理语义


def test_delete_removes_objects_and_remote_copies() -> None:
    service, channel, _repo, objects, remote, deletion_repo = _wired(name="清理频道")
    copies = (ClonedVoiceRemoteCopy(uuid4(), uuid4(), "el-voice-1"),)
    deletion_repo.assets[channel.id] = (
        2,
        1,
        3,
        2,
        ("image-key-1", "image-key-2", "cloned-key-1", "cover-key-1"),
        copies,
    )
    result = asyncio.run(service.delete(OWNER, channel.id, "清理频道"))
    assert result.summary.character_images == 2
    assert result.summary.cloned_voices == 1
    assert result.summary.cover_ref_images == 3
    assert result.summary.cover_character_cards == 2
    assert objects.deleted == [
        "image-key-1",
        "image-key-2",
        "cloned-key-1",
        "cover-key-1",
    ]
    assert remote.calls == [copies]
    assert result.object_cleanup_failures == ()
    assert result.remote_voice_failures == ()


def test_object_cleanup_failure_is_reported_not_hidden() -> None:
    service, channel, _repo, objects, _remote, deletion_repo = _wired(
        name="删不干净", fail_keys=frozenset({"image-key-1"})
    )
    deletion_repo.assets[channel.id] = (1, 0, 0, 0, ("image-key-1", "image-key-2"), ())
    result = asyncio.run(
        service.delete(OWNER, channel.id, "删不干净")
    )
    assert objects.deleted == ["image-key-2"]
    assert result.object_cleanup_failures == ("image-key-1",)


def test_remote_cleanup_failure_is_counted() -> None:
    copy = ClonedVoiceRemoteCopy(uuid4(), uuid4(), "el-voice-1")
    service, channel, _repo, _objects, remote, deletion_repo = _wired(name="远端失败")
    deletion_repo.assets[channel.id] = (0, 1, 0, 0, (), (copy,))
    remote.failures = (ClonedVoiceRemoteFailure(copy.cloned_voice_id, copy.pool_key_id),)
    result = asyncio.run(service.delete(OWNER, channel.id, "远端失败"))
    assert result.remote_voice_failures == (
        ClonedVoiceRemoteFailure(copy.cloned_voice_id, copy.pool_key_id),
    )


def test_remote_cleanup_crash_is_counted_as_failure() -> None:
    copy = ClonedVoiceRemoteCopy(uuid4(), uuid4(), "el-voice-1")
    service, channel, *_rest, remote, deletion_repo = _wired(name="远端异常")
    deletion_repo.assets[channel.id] = (0, 1, 0, 0, (), (copy,))
    remote.raise_error = True
    result = asyncio.run(service.delete(OWNER, channel.id, "远端异常"))
    assert len(result.remote_voice_failures) == 1


# ------------------------------------------------- ClonedVoiceService 远端清理


class _Pool:
    def __init__(self, entries: dict[UUID, PoolKeyEntry]) -> None:
        self.entries = entries

    def key_entry(self, key_id: UUID) -> PoolKeyEntry | None:
        return self.entries.get(key_id)


class _Cloner:
    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.deleted: list[tuple[str, str]] = []
        self.failing = failing

    async def delete_voice(self, api_key: str, voice_id: str) -> None:
        if voice_id in self.failing:
            raise DomainError("provider_unreachable", "EL 调用失败")
        self.deleted.append((api_key, voice_id))


def _cloned_service(entries: dict[UUID, PoolKeyEntry], cloner: _Cloner) -> ClonedVoiceService:
    return ClonedVoiceService(
        MemoryRepository(),
        _Objects(),
        cloner,
        _Pool(entries),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def _pool_entry(key_id: UUID, api_key: str) -> PoolKeyEntry:
    return PoolKeyEntry(
        id=key_id,
        api_key=api_key,
        label=api_key,
        status="active",
        added_at=NOW,
        status_at=NOW,
        last_error=None,
        quota_tier=None,
        character_count=None,
        character_limit=None,
        voice_slots_used=None,
        voice_limit=None,
        quota_refreshed_at=None,
    )


def test_delete_remote_copies_only_touches_provider() -> None:
    key_id = uuid4()
    cloner = _Cloner()
    service = _cloned_service({key_id: _pool_entry(key_id, "key-1")}, cloner)
    voice_id = uuid4()
    failures = asyncio.run(
        service.delete_remote_copies(
            (ClonedVoiceRemoteCopy(voice_id, key_id, "el-voice-1"),)
        )
    )
    assert failures == ()
    assert cloner.deleted == [("key-1", "el-voice-1")]


def test_delete_remote_copies_reports_missing_key_and_provider_error() -> None:
    known = uuid4()
    cloner = _Cloner(failing=frozenset({"el-broken"}))
    service = _cloned_service({known: _pool_entry(known, "key-1")}, cloner)
    missing = uuid4()
    copies = (
        ClonedVoiceRemoteCopy(uuid4(), missing, "el-missing"),
        ClonedVoiceRemoteCopy(uuid4(), known, "el-broken"),
    )
    failures = asyncio.run(service.delete_remote_copies(copies))
    assert len(failures) == 2
    assert {item.pool_key_id for item in failures} == {missing, known}


# ---------------------------------------------------------------- HTTP 合同


def _http_client(
    *, configured: bool = True, fail_keys: frozenset[str] = frozenset()
) -> tuple[TestClient, MemoryRepository, _DeletionRepo, UUID, _Objects, _Remote]:
    repo = MemoryRepository()
    identity = IdentityProjectService(
        repo,
        PlainPasswords(),
        setup_username="admin",
        setup_password="password1",
    )
    deletion_repo = _DeletionRepo(repo)
    objects = _Objects(fail_keys)
    remote = _Remote()
    service = ChannelDeletionService(deletion_repo, objects, remote)
    app = FastAPI()
    app.include_router(
        create_router(
            identity,
            channel_deletion_service=(lambda: service) if configured else None,
        )
    )
    client = TestClient(app)
    client.post(
        "/identity/setup/initialize",
        json={"username": "admin", "password": "password1"},
    )
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    account_id = UUID(client.get("/identity/me").json()["id"])
    return client, repo, deletion_repo, account_id, objects, remote


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_http_delete_channel_with_base64_name() -> None:
    client, repo, deletion_repo, account_id, _objects, _remote = _http_client()
    created = client.post("/identity/channels", json={"url": "https://example.test/a"}).json()
    channel = Channel(UUID(created["id"]), account_id, created["url"], name="中文频道")
    repo.channels[channel.id] = channel
    deletion_repo.assets[channel.id] = (2, 1, 3, 2, (), ())

    response = client.delete(
        f"/identity/channels/{channel.id}",
        headers={"X-Confirm-Channel-B64": _b64("中文频道")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(channel.id)
    assert body["status"] == "DELETED"
    assert body["deleted_assets"] == {
        "character_images": 2,
        "cloned_voices": 1,
        "cover_ref_images": 3,
        "cover_character_cards": 2,
        "cover_templates": 0,
        "description_templates": 0,
    }
    assert body["remote_voice_cleanup_failures"] == 0
    assert body["object_cleanup_failures"] == 0
    assert str(channel.id) not in {item["id"] for item in client.get("/identity/channels").json()}
    assert "pool_key_id" not in response.text
    assert "el_voice_id" not in response.text


def test_http_delete_channel_reports_cover_template_count() -> None:
    client, repo, deletion_repo, account_id, _objects, _remote = _http_client()
    created = client.post("/identity/channels", json={"url": "https://example.test/t"}).json()
    channel = Channel(UUID(created["id"]), account_id, created["url"], name="模板频道")
    repo.channels[channel.id] = channel
    deletion_repo.template_counts[channel.id] = 1

    response = client.delete(
        f"/identity/channels/{channel.id}",
        headers={"X-Confirm-Channel-B64": _b64("模板频道")},
    )

    assert response.status_code == 200
    assert response.json()["deleted_assets"]["cover_templates"] == 1


def test_http_delete_channel_plain_header_for_ascii_url() -> None:
    client, repo, _deletion_repo, account_id, _objects, _remote = _http_client()
    created = client.post("/identity/channels", json={"url": "https://example.test/b"}).json()
    channel = Channel(UUID(created["id"]), account_id, created["url"])
    repo.channels[channel.id] = channel
    response = client.delete(
        f"/identity/channels/{channel.id}",
        headers={"X-Confirm-Channel": channel.url},
    )
    assert response.status_code == 200


def test_http_base64_header_wins_over_plain() -> None:
    client, repo, _deletion_repo, account_id, _objects, _remote = _http_client()
    created = client.post("/identity/channels", json={"url": "https://example.test/c"}).json()
    channel = Channel(UUID(created["id"]), account_id, created["url"], name="双头频道")
    repo.channels[channel.id] = channel
    response = client.delete(
        f"/identity/channels/{channel.id}",
        headers={"X-Confirm-Channel": "wrong", "X-Confirm-Channel-B64": _b64("双头频道")},
    )
    assert response.status_code == 200


def test_http_confirmation_errors_are_422() -> None:
    client, repo, _deletion_repo, account_id, _objects, _remote = _http_client()
    created = client.post("/identity/channels", json={"url": "https://example.test/d"}).json()
    channel = Channel(UUID(created["id"]), account_id, created["url"], name="需确认")
    repo.channels[channel.id] = channel

    missing = client.delete(f"/identity/channels/{channel.id}")
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "channel_deletion_confirmation_required"

    wrong = client.delete(
        f"/identity/channels/{channel.id}", headers={"X-Confirm-Channel-B64": _b64("错误")}
    )
    assert wrong.status_code == 422
    assert wrong.json()["detail"]["code"] == "channel_deletion_confirmation_required"

    invalid = client.delete(
        f"/identity/channels/{channel.id}", headers={"X-Confirm-Channel-B64": "not-base64!!"}
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "channel_deletion_confirmation_required"


def test_http_channel_with_projects_is_409() -> None:
    client, repo, deletion_repo, account_id, _objects, _remote = _http_client()
    created = client.post("/identity/channels", json={"url": "https://example.test/e"}).json()
    channel = Channel(UUID(created["id"]), account_id, created["url"], name="有项目")
    repo.channels[channel.id] = channel
    deletion_repo.project_counts[channel.id] = 2

    response = client.delete(
        f"/identity/channels/{channel.id}", headers={"X-Confirm-Channel-B64": _b64("有项目")}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "channel_has_projects"
    assert channel.id in repo.channels


def test_http_unknown_channel_is_404() -> None:
    client, _repo, _deletion_repo, _account_id, _objects, _remote = _http_client()
    response = client.delete(
        f"/identity/channels/{uuid4()}", headers={"X-Confirm-Channel-B64": _b64("任意")}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_http_unassembled_deletion_service_is_503() -> None:
    client, _repo, _deletion_repo, _account_id, _objects, _remote = _http_client(
        configured=False
    )
    response = client.delete(
        f"/identity/channels/{uuid4()}", headers={"X-Confirm-Channel-B64": _b64("任意")}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "channel_deletion_unavailable"
