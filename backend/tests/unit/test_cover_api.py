"""封面 API 测试：频道/项目两组路由、所有权隔离、未冻结门禁、multipart 校验、
202 + 幂等两分支、面板三操作、列表与当前、受保护媒体（Cookie/Range/版本绑定）。

全部内存 fake（仓储/存储/Provider），零真实调用零网络；202 只落意图与 outbox
事件（dispatcher 只在 worker 进程），测试直接断言 outbox 记录。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.in_memory_cover_providers import (
    FakeCoverImageProvider,
    FakeCoverPromptProvider,
    FakeCoverRefAnalysisProvider,
)
from tubeflow_test.api.cover import create_cover_router
from tubeflow_test.application.cover_characters import CoverCharacterService
from tubeflow_test.application.cover_images import CoverImageService
from tubeflow_test.application.cover_ports import (
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    CoverCharacterImageRef,
    CoverImageGeneration,
    CoverPromptGeneration,
)
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_prompts import CoverPromptService
from tubeflow_test.application.cover_providers import CoverImageResult, CoverLlmResult
from tubeflow_test.application.cover_style import CoverStyleService
from tubeflow_test.identity_project.domain import Account, AccountRole

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[tuple[str, str]] = []

    def put_at_key(self, key: str, body: bytes, media_type: str, expected_sha256: str) -> None:
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("hash mismatch")
        self.puts.append((key, media_type))
        self.objects.setdefault(key, body)

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.objects[key]
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("integrity mismatch")
        return body


class FakeProjects:
    def __init__(self) -> None:
        self.contexts: dict[tuple[UUID, UUID], CoverProjectContext] = {}

    def add(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        channel_id: UUID | None = None,
        title: str = "测试标题",
        script: str | None = "冻结文案全文",
        locale: str = "zh-CN",
    ) -> None:
        self.contexts[(owner_id, project_id)] = CoverProjectContext(
            project_id, channel_id or uuid4(), title, script, locale
        )

    def project_cover_context(self, owner_id: UUID, project_id: UUID) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


class FakeQc:
    def inspect(
        self, path: Path, expected_sha256: str | None = None
    ) -> tuple[str, int, int, str, int]:
        payload = path.read_bytes()
        return hashlib.sha256(payload).hexdigest(), 2560, 1440, "image/png", len(payload)


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def gif_bytes(width: int = 1280, height: int = 720) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="GIF")
    return buffer.getvalue()


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = InMemoryCoverRepository()
        self.storage = FakeStorage()
        self.projects = FakeProjects()
        self.analysis = FakeCoverRefAnalysisProvider()
        self.prompt_provider = FakeCoverPromptProvider()
        self.image_provider = FakeCoverImageProvider()
        self.style = CoverStyleService(
            self.repo, self.repo, self.projects, self.storage, self.analysis, Ids(), Clock()
        )
        self.prompts = CoverPromptService(
            self.repo, self.projects, self.prompt_provider, Ids(), Clock(), model="fake-prompt"
        )
        self.images = CoverImageService(
            self.repo,
            self.projects,
            self.image_provider,
            FakeQc(),
            self.storage,
            Ids(),
            Clock(),
            tmp_path,
            model="fake-image",
            size="2560x1440",
            quality="high",
        )
        self.characters = CoverCharacterService(
            self.repo, self.repo, self.storage, self.analysis, Ids(), Clock()
        )
        self.owner = Account(uuid4(), "owner", "hash", AccountRole.USER)
        self.other = Account(uuid4(), "other", "hash", AccountRole.USER)

    def app(self, *, account: Account | None = None, media: Account | None = None) -> FastAPI:
        actor = account if account is not None else self.owner
        media_actor = media if media is not None else actor
        app = FastAPI()
        app.include_router(
            create_cover_router(
                self.style,
                self.prompts,
                self.images,
                lambda: actor,
                characters=self.characters,
                media_account=lambda: media_actor,
            )
        )
        return app

    def channel(self, owner: Account | None = None) -> UUID:
        channel_id = uuid4()
        self.repo.add_channel((owner or self.owner).id, channel_id)
        return channel_id

    def character_image(
        self, channel_id: UUID, *, owner: Account | None = None, content: bytes | None = None
    ) -> UUID:
        """登记一张频道角色图并把字节放入假存储（供卡归属校验与解析取字节）。"""
        owner = owner or self.owner
        payload = content if content is not None else png_bytes()
        image_id = uuid4()
        object_key = f"character-images/{owner.id}/{image_id}"
        self.storage.objects[object_key] = payload
        self.repo.add_character_image(
            CoverCharacterImageRef(
                image_id,
                owner.id,
                channel_id,
                object_key,
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                "image/png",
            )
        )
        return image_id

    def project(
        self, owner: Account | None = None, *, channel_id: UUID | None = None, frozen: bool = True
    ) -> UUID:
        owner = owner or self.owner
        project_id = uuid4()
        self.repo.set_project_state(owner.id, project_id, "ACTIVE")
        self.projects.add(
            owner.id,
            project_id,
            channel_id=channel_id or self.channel(owner),
            script="冻结文案全文" if frozen else None,
        )
        return project_id

    def completed_prompt(self, project_id: UUID, *, owner: Account | None = None) -> UUID:
        owner = owner or self.owner
        created = self.repo.create_prompt_generation(_prompt_generation(owner.id, project_id))
        done = self.repo.complete_prompt_generation(
            replace(
                created,
                status=COVER_STATUS_SUCCEEDED,
                cover_text_candidates=[{"text": "到此为止"}, {"text": "别再错过"}],
                cover_text_default="到此为止",
                cover_text_used="到此为止",
                prompt_body="维度文本。",
                prompt_system="系统版提示词",
                prompt_current="系统版提示词",
            )
        )
        self.repo.set_current_prompt_generation(owner.id, project_id, done.id)
        return done.id

    def completed_image(
        self,
        project_id: UUID,
        *,
        owner: Account | None = None,
        content: bytes = b"cover-png-bytes",
        set_current: bool = True,
    ) -> UUID:
        owner = owner or self.owner
        prompt_id = self.completed_prompt(project_id, owner=owner)
        created = self.repo.create_image_generation(
            _image_generation(owner.id, project_id, prompt_id)
        )
        object_key = f"cover-images/{owner.id}/{created.id}"
        self.storage.objects[object_key] = content
        digest = hashlib.sha256(content).hexdigest()
        done = self.repo.complete_image_generation_and_switch_current(
            replace(
                created,
                status=COVER_STATUS_SUCCEEDED,
                provider_receipt="receipt-1",
                expected_sha256=digest,
                downloaded_sha256=digest,
                downloaded_size=len(content),
                qc_width=2560,
                qc_height=1440,
                qc_mime="image/png",
                object_key=object_key,
                object_sha256=digest,
            ),
            switch_current=set_current,
        )
        return done.id


def _prompt_generation(owner_id: UUID, project_id: UUID) -> CoverPromptGeneration:
    return CoverPromptGeneration(
        uuid4(),
        owner_id,
        project_id,
        0,
        "none",
        None,
        "",
        {},
        None,
        "brief",
        [],
        "",
        "",
        "",
        "",
        "",
        "测试标题",
        "b" * 64,
        [],
        None,
        "fake-prompt",
        {},
        "seed-prompt-key",
        COVER_STATUS_INTENT_PERSISTED,
        None,
        None,
        NOW,
    )


def _image_generation(owner_id: UUID, project_id: UUID, prompt_id: UUID) -> CoverImageGeneration:
    return CoverImageGeneration(
        uuid4(),
        owner_id,
        project_id,
        prompt_id,
        0,
        "final prompt",
        "fake-image",
        "2560x1440",
        "high",
        {},
        "seed-image-key",
        "d" * 64,
        "c" * 64,
        COVER_STATUS_INTENT_PERSISTED,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        NOW,
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_openapi_contract_lists_all_cover_endpoints(harness: Harness) -> None:
    schema = harness.app().openapi()
    paths = schema["paths"]
    expected = {
        "/identity/channels/{channel_id}/cover-ref-images": {"post"},
        "/identity/channels/{channel_id}/cover-ref-images/{image_id}/media": {"get"},
        "/identity/channels/{channel_id}/cover-style": {"get"},
        "/identity/channels/{channel_id}/cover-style/draft": {"put"},
        "/identity/channels/{channel_id}/cover-style/analyze": {"post"},
        "/identity/channels/{channel_id}/cover-style/profiles": {"post"},
        "/identity/channels/{channel_id}/cover-style/current": {"post"},
        "/identity/channels/{channel_id}/cover-character-cards": {"post", "get"},
        "/identity/channels/{channel_id}/cover-character-cards/{card_id}/analyze": {"post"},
        "/projects/{project_id}/cover/ref-images": {"post"},
        "/projects/{project_id}/cover/ref-images/{image_id}/media": {"get"},
        "/projects/{project_id}/cover/style": {"get"},
        "/projects/{project_id}/cover/style/draft": {"put"},
        "/projects/{project_id}/cover/style/analyze": {"post"},
        "/projects/{project_id}/cover/style/promote": {"post"},
        "/projects/{project_id}/cover/prompts": {"post", "get"},
        "/projects/{project_id}/cover/prompts/current": {"patch", "post"},
        "/projects/{project_id}/cover/images": {"post", "get"},
        "/projects/{project_id}/cover/images/uploads": {"post"},
        "/projects/{project_id}/cover/images/current": {"post"},
        "/projects/{project_id}/cover/images/{generation_id}/media": {"get"},
    }
    for path, methods in expected.items():
        assert path in paths, path
        assert {method for method in paths[path] if method != "parameters"} == methods
        for method in paths[path]:
            operation = paths[path][method]
            assert operation.get("tags") == ["cover"]
    for path in ("/projects/{project_id}/cover/prompts", "/projects/{project_id}/cover/images"):
        parameters = paths[path]["post"]["parameters"]
        header = next(item for item in parameters if item["name"] == "Idempotency-Key")
        assert header["in"] == "header" and header["required"] is True


def test_channel_upload_draft_analyze_promote_and_switch(harness: Harness) -> None:
    client = TestClient(harness.app())
    channel_id = harness.channel()
    content = png_bytes()

    uploaded = client.post(
        f"/identity/channels/{channel_id}/cover-ref-images",
        files={"file": ("ref.PNG", content, "image/png")},
    )
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    assert body["public_status"] == "SUCCEEDED"
    ref_image_id = body["ref_image_id"]

    saved = client.put(
        f"/identity/channels/{channel_id}/cover-style/draft",
        json={"ref_image_id": ref_image_id, "dims": ["subject"], "edited_text": ""},
    )
    assert saved.status_code == 200
    assert saved.json()["draft"]["dims"] == {"selected": ["subject"]}

    queued = client.post(f"/identity/channels/{channel_id}/cover-style/analyze")
    assert queued.status_code == 202
    assert queued.json()["enqueued"] is True
    assert queued.json()["reused"] is False
    assert queued.json()["public_status"] == "QUEUED"
    assert len(harness.repo.outbox) == 1  # 意图已落 outbox，dispatcher 不在 API 进程

    # 重复点击在途去重：不再入队新事件。
    again = client.post(f"/identity/channels/{channel_id}/cover-style/analyze")
    assert again.status_code == 202
    assert again.json()["enqueued"] is False and again.json()["reused"] is False
    assert len(harness.repo.outbox) == 1

    # 工作流活动（假 Provider）完成后缓存命中：显式点击直接完成、零新调用。
    harness.analysis.queue(
        CoverLlmResult({"subject": {"人物身份": "青年女性"}}, "r1", "m", "a" * 64)
    )
    assert harness.style.run_ref_analysis(harness.owner.id, "channel", channel_id) == "SUCCEEDED"
    reused = client.post(f"/identity/channels/{channel_id}/cover-style/analyze")
    assert reused.status_code == 202
    assert reused.json()["reused"] is True
    assert reused.json()["public_status"] == "SUCCEEDED"

    # force=true（「重新解析」）：有效缓存也被清键强制重解（先模拟 dispatcher 确认在途事件）。
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    sentinel = datetime(2999, 1, 1, tzinfo=UTC)
    token = uuid4()
    while True:
        event = harness.repo.claim_outbox(sentinel, sentinel + timedelta(minutes=1), token)
        if event is None:
            break
        assert harness.repo.acknowledge_outbox(event.event_id, token, sentinel)
    forced = client.post(
        f"/identity/channels/{channel_id}/cover-style/analyze", json={"force": True}
    )
    assert forced.status_code == 202
    assert forced.json()["reused"] is False and forced.json()["enqueued"] is True
    assert forced.json()["draft"]["has_analysis"] is False
    assert forced.json()["draft"]["edited_text"] == "主体：人物身份：青年女性"

    # 强制重解析成功后缓存重建，恢复后续用例前置状态。
    harness.analysis.queue(
        CoverLlmResult({"subject": {"人物身份": "青年女性"}}, "r2", "m", "b" * 64)
    )
    assert harness.style.run_ref_analysis(harness.owner.id, "channel", channel_id) == "SUCCEEDED"

    promoted = client.post(f"/identity/channels/{channel_id}/cover-style/profiles")
    assert promoted.status_code == 201
    profile = promoted.json()
    assert profile["version"] == 1 and profile["is_current"] is True
    assert profile["edited_text"] == "主体：人物身份：青年女性"

    second = client.post(f"/identity/channels/{channel_id}/cover-style/profiles")
    assert second.json()["version"] == 2
    switched = client.post(
        f"/identity/channels/{channel_id}/cover-style/current",
        json={"profile_id": profile["profile_id"]},
    )
    assert switched.status_code == 200
    assert switched.json()["version"] == 1 and switched.json()["is_current"] is True

    view = client.get(f"/identity/channels/{channel_id}/cover-style")
    assert view.status_code == 200
    payload = view.json()
    assert payload["current_profile"]["version"] == 1
    assert [item["version"] for item in payload["profiles"]] == [2, 1]
    assert payload["draft"]["has_analysis"] is True
    assert len(payload["ref_images"]) == 1
    assert payload["public_status"] == "SUCCEEDED"

    invalid = client.post(
        f"/identity/channels/{channel_id}/cover-style/current",
        json={"profile_id": str(uuid4())},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_cover_candidate"


def test_upload_validation_failures_and_no_orphan_bytes(harness: Harness) -> None:
    client = TestClient(harness.app())
    channel_id = harness.channel()
    content = png_bytes()

    bad_ext = client.post(
        f"/identity/channels/{channel_id}/cover-ref-images",
        files={"file": ("ref.gif", content, "image/gif")},
    )
    assert bad_ext.status_code == 422
    assert bad_ext.json()["detail"]["code"] == "invalid_image_type"

    too_big = client.post(
        f"/identity/channels/{channel_id}/cover-ref-images",
        files={"file": ("ref.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
    )
    assert too_big.status_code == 422
    assert too_big.json()["detail"]["code"] == "invalid_image_size"

    garbage = client.post(
        f"/identity/channels/{channel_id}/cover-ref-images",
        files={"file": ("ref.png", b"not an image", "image/png")},
    )
    assert garbage.status_code == 422
    assert garbage.json()["detail"]["code"] == "invalid_image"

    # 频道不存在/跨账号：鉴权失败先于字节落盘，不得产生孤儿对象。
    missing = client.post(
        f"/identity/channels/{uuid4()}/cover-ref-images",
        files={"file": ("ref.png", content, "image/png")},
    )
    assert missing.status_code == 404
    assert harness.storage.puts == [] and harness.storage.objects == {}


def test_project_side_requires_frozen_project(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project(frozen=False)
    endpoints = [
        ("POST", f"/projects/{project_id}/cover/ref-images", None),
        ("GET", f"/projects/{project_id}/cover/style", None),
        ("PUT", f"/projects/{project_id}/cover/style/draft", {"dims": []}),
        ("POST", f"/projects/{project_id}/cover/style/analyze", None),
        ("POST", f"/projects/{project_id}/cover/style/promote", None),
        ("POST", f"/projects/{project_id}/cover/prompts", {}),
        ("GET", f"/projects/{project_id}/cover/prompts", None),
        ("PATCH", f"/projects/{project_id}/cover/prompts/current", {"reset": True}),
        ("POST", f"/projects/{project_id}/cover/images", None),
        ("POST", f"/projects/{project_id}/cover/images/uploads", None),
        ("GET", f"/projects/{project_id}/cover/images", None),
        ("POST", f"/projects/{project_id}/cover/images/current", {"generation_id": str(uuid4())}),
    ]
    for method, url, json_body in endpoints:
        headers = {}
        if url.endswith(("/prompts", "/images")) and method == "POST":
            headers["Idempotency-Key"] = "key"
        if method == "POST" and url.endswith(("/ref-images", "/images/uploads")):
            response = client.post(url, files={"file": ("ref.png", png_bytes(), "image/png")})
        else:
            response = client.request(method, url, json=json_body, headers=headers)
        assert response.status_code == 409, (method, url, response.text)
        assert response.json()["detail"]["code"] == "cover_project_not_frozen"


def test_cross_account_resources_are_not_found(harness: Harness) -> None:
    owner_client = TestClient(harness.app())
    channel_id = harness.channel()
    project_id = harness.project()
    owner_client.put(
        f"/projects/{project_id}/cover/style/draft",
        json={"dims": ["subject"], "edited_text": "项目风格"},
    )
    harness.completed_prompt(project_id)
    harness.completed_image(project_id)

    other_client = TestClient(harness.app(account=harness.other, media=harness.other))
    assert other_client.get(f"/identity/channels/{channel_id}/cover-style").status_code == 404
    for url in (
        f"/projects/{project_id}/cover/style",
        f"/projects/{project_id}/cover/prompts",
        f"/projects/{project_id}/cover/images",
    ):
        assert other_client.get(url).status_code == 404, url
    generation_id = harness.repo.project_current_image[(harness.owner.id, project_id)]
    assert (
        other_client.get(f"/projects/{project_id}/cover/images/{generation_id}/media").status_code
        == 404
    )
    # 属主仍可访问。
    assert owner_client.get(f"/projects/{project_id}/cover/style").status_code == 200


def test_prompt_create_202_idempotency_replay_and_conflict(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project()
    headers = {"Idempotency-Key": "prompt-key-1"}

    created = client.post(
        f"/projects/{project_id}/cover/prompts",
        json={"brief": "突出反差", "cover_text": "我不要了"},
        headers=headers,
    )
    assert created.status_code == 202
    body = created.json()
    assert body["version"] == 1
    assert body["status"] == "INTENT_PERSISTED"
    assert body["public_status"] == "QUEUED"
    assert body["cover_text_used"] == "我不要了"
    assert body["cover_text_candidates"] == [{"text": "我不要了"}]
    assert len(harness.repo.outbox) == 1

    replay = client.post(
        f"/projects/{project_id}/cover/prompts",
        json={"brief": "突出反差", "cover_text": "我不要了"},
        headers=headers,
    )
    assert replay.status_code == 202
    assert replay.json()["generation_id"] == body["generation_id"]
    assert len(harness.repo.outbox) == 1  # 重放零重复提交

    conflict = client.post(
        f"/projects/{project_id}/cover/prompts",
        json={"brief": "换一个请求"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    missing_key = client.post(f"/projects/{project_id}/cover/prompts", json={})
    assert missing_key.status_code == 422


def test_prompt_view_and_panel_three_operations(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project()
    generation_id = harness.completed_prompt(project_id)

    view = client.get(f"/projects/{project_id}/cover/prompts")
    assert view.status_code == 200
    payload = view.json()
    assert payload["current"]["generation_id"] == str(generation_id)
    assert payload["current"]["is_current"] is True
    assert payload["current"]["cover_text_candidates"] == [
        {"text": "到此为止"},
        {"text": "别再错过"},
    ]
    assert len(payload["items"]) == 1

    edited = client.patch(
        f"/projects/{project_id}/cover/prompts/current",
        json={"prompt_current": "手工改过的提示词"},
    )
    assert edited.status_code == 200
    assert edited.json()["prompt_current"] == "手工改过的提示词"
    assert edited.json()["prompt_system"] == "系统版提示词"

    reset = client.patch(f"/projects/{project_id}/cover/prompts/current", json={"reset": True})
    assert reset.json()["prompt_current"] == "系统版提示词"

    chosen = client.patch(
        f"/projects/{project_id}/cover/prompts/current",
        json={"cover_text_choice": "别再错过"},
    )
    assert chosen.status_code == 200
    assert "别再错过" in chosen.json()["prompt_current"]
    assert chosen.json()["prompt_current"] != "系统版提示词"

    unknown = client.patch(
        f"/projects/{project_id}/cover/prompts/current",
        json={"cover_text_choice": "不存在的候选"},
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "invalid_cover_candidate"

    # 面板请求必须恰好一个操作。
    for body in ({}, {"reset": True, "prompt_current": "x"}, {"reset": False}):
        rejected = client.patch(f"/projects/{project_id}/cover/prompts/current", json=body)
        assert rejected.status_code == 422, body


def test_image_create_view_and_current_switch(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project()
    harness.completed_prompt(project_id)
    headers = {"Idempotency-Key": "image-key-1"}

    created = client.post(f"/projects/{project_id}/cover/images", headers=headers)
    assert created.status_code == 202
    body = created.json()
    assert body["version"] == 1 and body["status"] == "INTENT_PERSISTED"
    assert body["public_status"] == "QUEUED"
    assert len(harness.repo.outbox) == 2  # 提示词一条 + 出图一条

    replay = client.post(f"/projects/{project_id}/cover/images", headers=headers)
    assert replay.json()["generation_id"] == body["generation_id"]
    assert len(harness.repo.outbox) == 2

    # 面板文本变化 → canonical 变化 → 同键冲突。
    client.patch(f"/projects/{project_id}/cover/prompts/current", json={"prompt_current": "改过"})
    conflict = client.post(f"/projects/{project_id}/cover/images", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    # 出图活动（假 Provider + 假 QC + 内存存储）走通后：首成功自动当前。
    content = png_bytes(16, 16)
    harness.image_provider.queue(
        CoverImageResult(content, hashlib.sha256(content).hexdigest(), "receipt-1", "b64_json")
    )
    done = harness.images.generate(harness.owner.id, UUID(body["generation_id"]))
    assert done.status == COVER_STATUS_SUCCEEDED
    assert harness.repo.current_image_generation_for_project(harness.owner.id, project_id)

    view = client.get(f"/projects/{project_id}/cover/images")
    payload = view.json()
    assert payload["current"]["generation_id"] == body["generation_id"]
    assert payload["current"]["media_url"] == (
        f"/projects/{project_id}/cover/images/{body['generation_id']}/media"
    )
    assert payload["current"]["public_status"] == "SUCCEEDED"
    assert payload["current"]["size_bytes"] == len(content)

    # 第二个成功版本不自动当前，由用户明确切换。
    second_content = png_bytes(24, 24)
    harness.image_provider.queue(
        CoverImageResult(
            second_content,
            hashlib.sha256(second_content).hexdigest(),
            "receipt-2",
            "b64_json",
        )
    )
    second = client.post(
        f"/projects/{project_id}/cover/images", headers={"Idempotency-Key": "image-key-2"}
    )
    assert second.status_code == 202
    harness.images.generate(harness.owner.id, UUID(second.json()["generation_id"]))
    view = client.get(f"/projects/{project_id}/cover/images")
    assert view.json()["current"]["generation_id"] == body["generation_id"]
    assert len(view.json()["items"]) == 2

    switched = client.post(
        f"/projects/{project_id}/cover/images/current",
        json={"generation_id": second.json()["generation_id"]},
    )
    assert switched.status_code == 200
    assert switched.json()["is_current"] is True
    assert (
        client.get(f"/projects/{project_id}/cover/images").json()["current"]["generation_id"]
        == second.json()["generation_id"]
    )


def test_image_media_cookie_range_etag_and_version_binding(harness: Harness) -> None:
    project_id = harness.project()
    content = png_bytes(16, 16)
    generation_id = harness.completed_image(project_id, content=content)
    digest = hashlib.sha256(content).hexdigest()
    url = f"/projects/{project_id}/cover/images/{generation_id}/media"

    client = TestClient(harness.app())
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["accept-ranges"] == "bytes"

    partial = client.get(url, headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert partial.content == content[:10]
    assert partial.headers["content-range"] == f"bytes 0-9/{len(content)}"

    unsatisfiable = client.get(url, headers={"Range": f"bytes={len(content) + 1}-"})
    assert unsatisfiable.status_code == 416

    cached = client.get(url, headers={"If-None-Match": f'"{digest}"'})
    assert cached.status_code == 304

    head = client.head(url)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(content))

    # 版本绑定：其他项目路径 / 未成功版本 / 不存在版本一律 404。
    other_project = harness.project()
    bound = f"/projects/{other_project}/cover/images/{generation_id}/media"
    assert client.get(bound).status_code == 404
    assert client.get(f"/projects/{project_id}/cover/images/{uuid4()}/media").status_code == 404

    # 媒体端点走 media_account（媒体 Cookie 依赖），不是写端点的 Bearer 依赖。
    app = FastAPI()
    app.include_router(
        create_cover_router(
            harness.style,
            harness.prompts,
            harness.images,
            lambda: (_ for _ in ()).throw(
                HTTPException(401, detail={"code": "invalid_session", "message": "x"})
            ),
            characters=harness.characters,
            media_account=lambda: harness.owner,
        )
    )
    assert TestClient(app).get(url).status_code == 200


def test_upload_cover_image_201_then_200_reused(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project()  # 冻结项目，无提示词版本
    content = png_bytes(1280, 720)
    url = f"/projects/{project_id}/cover/images/uploads"

    created = client.post(url, files={"file": ("cover.png", content, "image/png")})
    assert created.status_code == 201
    body = created.json()
    assert body["reused"] is False
    assert body["source"] == "upload"
    assert body["status"] == "SUCCEEDED"
    assert body["public_status"] == "SUCCEEDED"
    assert body["version"] == 1
    assert body["prompt_generation_id"] is None
    assert body["model"] is None and body["size"] is None and body["quality"] is None
    assert (body["width"], body["height"]) == (1280, 720)
    assert body["is_current"] is True
    assert body["media_url"] == (
        f"/projects/{project_id}/cover/images/{body['generation_id']}/media"
    )

    reused = client.post(url, files={"file": ("cover-again.png", content, "image/png")})
    assert reused.status_code == 200
    assert reused.json()["reused"] is True
    assert reused.json()["generation_id"] == body["generation_id"]

    listing = client.get(f"/projects/{project_id}/cover/images").json()
    assert len(listing["items"]) == 1
    assert listing["current"]["generation_id"] == body["generation_id"]
    assert listing["current"]["source"] == "upload"
    assert "UPLOAD_COVER_IMAGE" in listing["allowed_actions"]
    assert harness.repo.outbox == {}

    media = client.get(body["media_url"])
    assert media.status_code == 200
    assert media.content == content
    assert media.headers["content-type"].startswith("image/png")


def test_upload_cover_image_validation_and_cross_account(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project()
    url = f"/projects/{project_id}/cover/images/uploads"

    for filename, content, code in (
        ("cover.gif", gif_bytes(), "invalid_image_type"),
        ("cover.png", png_bytes(1024, 576), "invalid_image_dimensions"),
        ("cover.png", png_bytes(1360, 720), "invalid_image_aspect_ratio"),
    ):
        response = client.post(url, files={"file": (filename, content, "image/png")})
        assert response.status_code == 422, (filename, response.text)
        assert response.json()["detail"]["code"] == code

    assert harness.storage.objects == {}
    assert client.get(f"/projects/{project_id}/cover/images").json()["items"] == []

    stranger = TestClient(harness.app(account=harness.other, media=harness.other))
    missing = stranger.post(
        url, files={"file": ("cover.png", png_bytes(1280, 720), "image/png")}
    )
    assert missing.status_code == 404
    assert harness.storage.objects == {}


def test_project_draft_promotes_to_channel_style(harness: Harness) -> None:
    client = TestClient(harness.app())
    channel_id = harness.channel()
    project_id = harness.project(channel_id=channel_id)
    client.put(
        f"/projects/{project_id}/cover/style/draft",
        json={"dims": ["style"], "edited_text": "风格：艺术流派：写实摄影"},
    )

    promoted = client.post(f"/projects/{project_id}/cover/style/promote")
    assert promoted.status_code == 201
    assert promoted.json()["version"] == 1
    assert promoted.json()["edited_text"] == "风格：艺术流派：写实摄影"

    channel_view = client.get(f"/identity/channels/{channel_id}/cover-style")
    assert channel_view.json()["current_profile"]["version"] == 1

    # 项目侧 GET 风格视图：草稿 + 参考图，无频道版本列表。
    project_view = client.get(f"/projects/{project_id}/cover/style")
    payload = project_view.json()
    assert payload["draft"]["edited_text"] == "风格：艺术流派：写实摄影"
    assert payload["profiles"] == [] and payload["current_profile"] is None


# ---------- v5：角色卡 / 参考图媒体 / 提示词版本切换 / 风格 label ----------


def test_character_card_upsert_list_analyze_and_isolation(harness: Harness) -> None:
    client = TestClient(harness.app())
    channel_id = harness.channel()
    image_id = harness.character_image(channel_id)

    created = client.post(
        f"/identity/channels/{channel_id}/cover-character-cards",
        json={
            "character_image_id": str(image_id),
            "identity_text": " 小艾 ",
            "appearance_text": "二头身",
        },
    )
    assert created.status_code == 200
    card = created.json()
    assert card["identity_text"] == "小艾" and card["source"] == "manual"
    assert card["character_media_url"] == (
        f"/identity/channels/{channel_id}/character-images/{image_id}/content"
    )
    card_id = card["card_id"]

    # 同角色图再次提交 = 改卡（每图一卡），id 不变。
    updated = client.post(
        f"/identity/channels/{channel_id}/cover-character-cards",
        json={"character_image_id": str(image_id), "identity_text": "小艾v2"},
    )
    assert updated.status_code == 200
    assert updated.json()["card_id"] == card_id
    assert updated.json()["identity_text"] == "小艾v2"
    assert updated.json()["appearance_text"] == ""

    listing = client.get(f"/identity/channels/{channel_id}/cover-character-cards")
    assert listing.status_code == 200
    assert [item["card_id"] for item in listing.json()["items"]] == [card_id]

    # 显式解析 202：落 outbox；重复点击在途去重；活动完成后卡被解析结果覆盖。
    queued = client.post(f"/identity/channels/{channel_id}/cover-character-cards/{card_id}/analyze")
    assert queued.status_code == 202
    assert queued.json()["enqueued"] is True
    assert queued.json()["public_status"] == "QUEUED"
    assert len(harness.repo.outbox) == 1
    again = client.post(f"/identity/channels/{channel_id}/cover-character-cards/{card_id}/analyze")
    assert again.status_code == 202 and again.json()["enqueued"] is False
    assert len(harness.repo.outbox) == 1
    harness.analysis.queue(
        CoverLlmResult(
            {"identity": "原创角色小艾", "appearance": "二次元二头身"}, "r1", "m", "a" * 64
        )
    )
    assert harness.characters.run_card_analysis(harness.owner.id, UUID(card_id)) == "SUCCEEDED"
    stored = harness.repo.get_character_card(harness.owner.id, UUID(card_id))
    assert stored is not None
    assert stored.identity_text == "原创角色小艾" and stored.source == "analysis"

    # 校验失败：身份超长 422；角色图不存在/跨频道 404；路径频道与卡不一致 404。
    too_long = client.post(
        f"/identity/channels/{channel_id}/cover-character-cards",
        json={"character_image_id": str(image_id), "identity_text": "长" * 101},
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "cover_character_card_identity_too_long"
    missing_image = client.post(
        f"/identity/channels/{channel_id}/cover-character-cards",
        json={"character_image_id": str(uuid4()), "identity_text": "x"},
    )
    assert missing_image.status_code == 404
    other_channel = harness.channel()
    wrong_scope = client.post(
        f"/identity/channels/{other_channel}/cover-character-cards",
        json={"character_image_id": str(image_id), "identity_text": "x"},
    )
    assert wrong_scope.status_code == 404
    mismatched = client.post(
        f"/identity/channels/{other_channel}/cover-character-cards/{card_id}/analyze"
    )
    assert mismatched.status_code == 404

    # 跨账号：频道与卡一律 404。
    other_client = TestClient(harness.app(account=harness.other, media=harness.other))
    assert (
        other_client.get(f"/identity/channels/{channel_id}/cover-character-cards").status_code
        == 404
    )
    foreign = other_client.post(
        f"/identity/channels/{channel_id}/cover-character-cards",
        json={"character_image_id": str(image_id), "identity_text": "x"},
    )
    assert foreign.status_code == 404
    assert (
        other_client.post(
            f"/identity/channels/{channel_id}/cover-character-cards/{card_id}/analyze"
        ).status_code
        == 404
    )


def test_ref_image_media_both_scopes_range_etag_and_isolation(harness: Harness) -> None:
    client = TestClient(harness.app())
    channel_id = harness.channel()
    project_id = harness.project(channel_id=channel_id)
    channel_content = png_bytes(8, 8)
    project_content = png_bytes(12, 12)
    channel_image = client.post(
        f"/identity/channels/{channel_id}/cover-ref-images",
        files={"file": ("ref.png", channel_content, "image/png")},
    ).json()["ref_image_id"]
    project_image = client.post(
        f"/projects/{project_id}/cover/ref-images",
        files={"file": ("ref.png", project_content, "image/png")},
    ).json()["ref_image_id"]

    for url, content in (
        (
            f"/identity/channels/{channel_id}/cover-ref-images/{channel_image}/media",
            channel_content,
        ),
        (f"/projects/{project_id}/cover/ref-images/{project_image}/media", project_content),
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.content == content
        assert response.headers["content-type"].startswith("image/png")
        assert response.headers["etag"] == f'"{hashlib.sha256(content).hexdigest()}"'
        partial = client.get(url, headers={"Range": "bytes=0-3"})
        assert partial.status_code == 206 and partial.content == content[:4]
        unsatisfiable = client.get(url, headers={"Range": f"bytes={len(content) + 1}-"})
        assert unsatisfiable.status_code == 416
        cached = client.get(
            url, headers={"If-None-Match": f'"{hashlib.sha256(content).hexdigest()}"'}
        )
        assert cached.status_code == 304
        head = client.head(url)
        assert head.status_code == 200 and head.content == b""

    # 作用域绑定：频道图不走项目路径、项目图不走频道路径、跨频道/跨项目 404。
    assert (
        client.get(f"/projects/{project_id}/cover/ref-images/{channel_image}/media").status_code
        == 404
    )
    assert (
        client.get(
            f"/identity/channels/{channel_id}/cover-ref-images/{project_image}/media"
        ).status_code
        == 404
    )
    other_project = harness.project(channel_id=channel_id)
    assert (
        client.get(f"/projects/{other_project}/cover/ref-images/{project_image}/media").status_code
        == 404
    )
    other_channel = harness.channel()
    assert (
        client.get(
            f"/identity/channels/{other_channel}/cover-ref-images/{channel_image}/media"
        ).status_code
        == 404
    )

    # 跨账号 404；媒体端点走 media_account（媒体 Cookie 依赖）。
    other_client = TestClient(harness.app(account=harness.other, media=harness.other))
    assert (
        other_client.get(
            f"/identity/channels/{channel_id}/cover-ref-images/{channel_image}/media"
        ).status_code
        == 404
    )
    assert (
        other_client.get(f"/projects/{project_id}/cover/ref-images/{project_image}/media").status_code
        == 404
    )
    app = FastAPI()
    app.include_router(
        create_cover_router(
            harness.style,
            harness.prompts,
            harness.images,
            lambda: (_ for _ in ()).throw(
                HTTPException(401, detail={"code": "invalid_session", "message": "x"})
            ),
            characters=harness.characters,
            media_account=lambda: harness.owner,
        )
    )
    bearer_less = TestClient(app)
    assert (
        bearer_less.get(
            f"/identity/channels/{channel_id}/cover-ref-images/{channel_image}/media"
        ).status_code
        == 200
    )
    assert (
        bearer_less.get(f"/projects/{project_id}/cover/ref-images/{project_image}/media").status_code
        == 200
    )


def test_prompt_current_version_switch(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project()
    first_id = harness.completed_prompt(project_id)

    # 第二个 SUCCEEDED 版本（不自动当前）。
    created = harness.repo.create_prompt_generation(
        replace(_prompt_generation(harness.owner.id, project_id), idempotency_key="second-key")
    )
    second = harness.repo.complete_prompt_generation(
        replace(
            created,
            status=COVER_STATUS_SUCCEEDED,
            cover_text_candidates=[{"text": "别再错过"}],
            cover_text_default="别再错过",
            cover_text_used="别再错过",
            prompt_body="第二版维度文本。",
            prompt_system="第二版系统提示词",
            prompt_current="第二版系统提示词",
        )
    )
    assert client.get(f"/projects/{project_id}/cover/prompts").json()["current"][
        "generation_id"
    ] == str(first_id)

    switched = client.post(
        f"/projects/{project_id}/cover/prompts/current", json={"version_id": str(second.id)}
    )
    assert switched.status_code == 200
    assert switched.json()["generation_id"] == str(second.id)
    assert switched.json()["is_current"] is True
    assert client.get(f"/projects/{project_id}/cover/prompts").json()["current"][
        "generation_id"
    ] == str(second.id)

    # 非法候选：不存在 / 非本项目 / 未成功一律 invalid_cover_candidate（422）。
    foreign_project = harness.project()
    foreign_intent = harness.repo.create_prompt_generation(
        replace(
            _prompt_generation(harness.owner.id, foreign_project),
            idempotency_key="foreign-key",
        )
    )
    foreign_done = harness.repo.complete_prompt_generation(
        replace(
            foreign_intent,
            status=COVER_STATUS_SUCCEEDED,
            prompt_body="其他项目",
            prompt_system="其他项目",
            prompt_current="其他项目",
        )
    )
    for candidate in (uuid4(), foreign_done.id):
        rejected = client.post(
            f"/projects/{project_id}/cover/prompts/current",
            json={"version_id": str(candidate)},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "invalid_cover_candidate"
    pending = harness.repo.create_prompt_generation(
        replace(_prompt_generation(harness.owner.id, project_id), idempotency_key="pending-key")
    )
    rejected = client.post(
        f"/projects/{project_id}/cover/prompts/current", json={"version_id": str(pending.id)}
    )
    assert rejected.status_code == 422

    # 未冻结 409；跨账号 404。
    unfrozen = harness.project(frozen=False)
    response = client.post(
        f"/projects/{unfrozen}/cover/prompts/current", json={"version_id": str(first_id)}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "cover_project_not_frozen"
    other_client = TestClient(harness.app(account=harness.other, media=harness.other))
    assert (
        other_client.post(
            f"/projects/{project_id}/cover/prompts/current", json={"version_id": str(first_id)}
        ).status_code
        == 404
    )


def test_profile_label_and_prompt_create_v5_fields(harness: Harness) -> None:
    client = TestClient(harness.app())
    channel_id = harness.channel()
    project_id = harness.project(channel_id=channel_id)
    client.put(
        f"/identity/channels/{channel_id}/cover-style/draft",
        json={"dims": ["style"], "edited_text": "风格：艺术流派：写实摄影"},
    )

    labeled = client.post(
        f"/identity/channels/{channel_id}/cover-style/profiles", json={"label": " 首发风格 "}
    )
    assert labeled.status_code == 201
    assert labeled.json()["label"] == "首发风格"
    profile_id = labeled.json()["profile_id"]

    too_long = client.post(
        f"/identity/channels/{channel_id}/cover-style/profiles", json={"label": "长" * 61}
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "cover_style_label_too_long"

    unlabeled = client.post(f"/identity/channels/{channel_id}/cover-style/profiles")
    assert unlabeled.status_code == 201 and unlabeled.json()["label"] == ""
    view = client.get(f"/identity/channels/{channel_id}/cover-style").json()
    assert [item["label"] for item in view["profiles"]] == ["", "首发风格"]

    # 按版本生成：style_profile_id 选用指定版本文本；血缘记录该版本。
    created = client.post(
        f"/projects/{project_id}/cover/prompts",
        json={"style_profile_id": profile_id},
        headers={"Idempotency-Key": "v5-key-1"},
    )
    assert created.status_code == 202
    body = created.json()
    assert body["style_source"] == "channel_profile"
    assert body["channel_style_profile_id"] == profile_id
    assert body["character_cards_used"] == []

    # 非法组合：style_source=project/none 带 style_profile_id → 422。
    for source in ("project", "none"):
        rejected = client.post(
            f"/projects/{project_id}/cover/prompts",
            json={"style_source": source, "style_profile_id": profile_id},
            headers={"Idempotency-Key": f"v5-{source}"},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "cover_style_source_invalid"
    # 跨频道版本 → invalid_cover_candidate。
    foreign_channel = harness.channel()
    client.put(
        f"/identity/channels/{foreign_channel}/cover-style/draft",
        json={"dims": ["style"], "edited_text": "风格：艺术流派：水彩"},
    )
    foreign_profile = client.post(
        f"/identity/channels/{foreign_channel}/cover-style/profiles"
    ).json()["profile_id"]
    rejected = client.post(
        f"/projects/{project_id}/cover/prompts",
        json={"style_profile_id": foreign_profile},
        headers={"Idempotency-Key": "v5-foreign"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "invalid_cover_candidate"

    # 显式不绑定（[]）与默认绑定都落 character_cards_used 血缘。
    image_id = harness.character_image(channel_id)
    card = client.post(
        f"/identity/channels/{channel_id}/cover-character-cards",
        json={
            "character_image_id": str(image_id),
            "identity_text": "小艾",
            "appearance_text": "二头身",
        },
    ).json()
    bound = client.post(
        f"/projects/{project_id}/cover/prompts",
        json={"character_card_ids": [card["card_id"]]},
        headers={"Idempotency-Key": "v5-key-2"},
    )
    assert bound.status_code == 202
    lineage = bound.json()["character_cards_used"]
    assert [item["card_id"] for item in lineage] == [card["card_id"]]
    assert lineage[0]["identity_text"] == "小艾"
    unbound = client.post(
        f"/projects/{project_id}/cover/prompts",
        json={"character_card_ids": []},
        headers={"Idempotency-Key": "v5-key-3"},
    )
    assert unbound.status_code == 202
    assert unbound.json()["character_cards_used"] == []


def test_image_view_exposes_visual_review(harness: Harness) -> None:
    client = TestClient(harness.app())
    project_id = harness.project()
    prompt_id = harness.completed_prompt(project_id)
    created = harness.repo.create_image_generation(
        _image_generation(harness.owner.id, project_id, prompt_id)
    )
    object_key = f"cover-images/{harness.owner.id}/{created.id}"
    content = b"cover-png-bytes"
    harness.storage.objects[object_key] = content
    digest = hashlib.sha256(content).hexdigest()
    review = {
        "status": "succeeded",
        "ok": False,
        "issues": [
            {
                "kind": "text_spelling",
                "severity": "blocker",
                "description": "拼写错误",
                "suggested_edit": "改为 X",
            }
        ],
        "model": "gpt-5.5",
        "receipt": "receipt-r1",
    }
    harness.repo.complete_image_generation_and_switch_current(
        replace(
            created,
            status=COVER_STATUS_SUCCEEDED,
            downloaded_size=len(content),
            object_key=object_key,
            object_sha256=digest,
            visual_review=review,
        ),
        switch_current=True,
    )
    payload = client.get(f"/projects/{project_id}/cover/images").json()
    public_review = {key: value for key, value in review.items() if key != "receipt"}
    assert payload["current"]["visual_review"] == public_review
    assert payload["items"][0]["visual_review"] == public_review
    # 回执只落库不公开（与既有不暴露回执的边界一致）
    stored = harness.repo.get_image_generation(harness.owner.id, created.id)
    assert stored is not None and stored.visual_review == review
