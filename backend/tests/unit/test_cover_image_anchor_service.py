"""封面锚图通道路由与归一化测试（v6 §18.13，全 fake + Pillow 真实解码，零网络）。

覆盖：有/无参考图路由、开关关闭回退文本通道、锚图未配置稳定失败、
归一化双哈希留痕、复审作用于归一化产物、Provider 失败落码不重提、
终态重放零重复调用、幂等键跨通道冲突。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image

from tubeflow_test.adapters.cover_image_qc import PillowCoverImageQc
from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.in_memory_cover_providers import (
    FakeCoverAnchorImageProvider,
    FakeCoverImageProvider,
    FakeCoverImageReviewProvider,
)
from tubeflow_test.application.cover_images import CoverImageService
from tubeflow_test.application.cover_ports import (
    COVER_STATUS_FAILED,
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    CoverPromptGeneration,
    CoverRefImage,
    CoverStyleProfile,
)
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_providers import CoverImageResult, CoverLlmResult
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
ANCHOR_BYTES = b"anchor-image-bytes"
ANCHOR_SHA = hashlib.sha256(ANCHOR_BYTES).hexdigest()


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_at_key(self, key: str, body: bytes, media_type: str, expected_sha256: str) -> None:
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("hash mismatch")
        self.objects.setdefault(key, body)

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.objects[key]
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("integrity mismatch")
        return body


class FakeProjects:
    def __init__(self) -> None:
        self.contexts: dict[tuple[UUID, UUID], CoverProjectContext] = {}

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


def png_bytes(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (8, 16, 32)).save(buffer, format="PNG")
    return buffer.getvalue()


def image_result(content: bytes) -> CoverImageResult:
    return CoverImageResult(content, hashlib.sha256(content).hexdigest(), "receipt-1", "b64_json")


@pytest.fixture
def harness(tmp_path: Path):
    repo = InMemoryCoverRepository()
    projects = FakeProjects()
    provider = FakeCoverImageProvider()
    anchor = FakeCoverAnchorImageProvider()
    review = FakeCoverImageReviewProvider()
    storage = FakeStorage()

    def build(*, anchor_provider=anchor, anchor_enabled: bool = True) -> CoverImageService:
        return CoverImageService(
            repo,
            projects,
            provider,
            PillowCoverImageQc(),
            storage,
            Ids(),
            Clock(),
            tmp_path / "cover-work",
            model="gpt-image-2",
            size="2560x1440",
            quality="high",
            review=review,
            review_enabled=True,
            anchor_provider=anchor_provider,
            anchor_enabled=anchor_enabled,
            anchor_model="nano-banana-pro",
        )

    return repo, projects, provider, anchor, review, storage, build


def _seed_project(repo, projects, owner_id: UUID) -> tuple[UUID, UUID]:
    channel_id = uuid4()
    project_id = uuid4()
    repo.add_channel(owner_id, channel_id)
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.contexts[(owner_id, project_id)] = CoverProjectContext(
        project_id, channel_id, "测试标题", "冻结文案", "zh-CN"
    )
    return channel_id, project_id


def _seed_anchor_profile(repo, owner_id: UUID, channel_id: UUID) -> UUID:
    ref_image_id = uuid4()
    repo.add_ref_image(
        CoverRefImage(
            ref_image_id,
            owner_id,
            "channel",
            channel_id,
            None,
            ANCHOR_SHA,
            f"cover-ref-images/channel/{channel_id}/{ANCHOR_SHA}",
            len(ANCHOR_BYTES),
            414,
            233,
            "PNG",
            NOW,
        )
    )
    profile = repo.create_style_profile(
        CoverStyleProfile(
            uuid4(), owner_id, channel_id, 1, ref_image_id, {}, {}, "风格文本", NOW, None
        )
    )
    return profile.id


def _seed_prompt(
    repo,
    owner_id: UUID,
    project_id: UUID,
    *,
    style_source: str = "channel_profile",
    profile_id: UUID | None = None,
) -> None:
    generation = repo.create_prompt_generation(
        CoverPromptGeneration(
            uuid4(),
            owner_id,
            project_id,
            0,
            style_source,
            profile_id,
            "风格文本",
            {},
            None,
            "",
            [],
            "默认文字",
            "默认文字",
            "维度文本",
            "系统版提示词",
            "面板当前提示词",
            "标题",
            "a" * 64,
            [],
            None,
            "model",
            {},
            f"prompt-key-{uuid4()}",
            COVER_STATUS_INTENT_PERSISTED,
            None,
            None,
            NOW,
        )
    )
    completed = repo.complete_prompt_generation(replace(generation, status=COVER_STATUS_SUCCEEDED))
    repo.set_current_prompt_generation(owner_id, project_id, completed.id)


def test_anchor_route_normalize_dual_hash_and_review(harness) -> None:
    repo, projects, provider, anchor, review, storage, build = harness
    service = build()
    owner_id = uuid4()
    channel_id, project_id = _seed_project(repo, projects, owner_id)
    profile_id = _seed_anchor_profile(repo, owner_id, channel_id)
    storage.objects[f"cover-ref-images/channel/{channel_id}/{ANCHOR_SHA}"] = ANCHOR_BYTES
    _seed_prompt(repo, owner_id, project_id, profile_id=profile_id)

    created = service.create(owner_id, project_id, idempotency_key="anchor-1")
    assert created.model == "nano-banana-pro"
    assert created.parameters["provider"] == "grsai-anchor"
    assert created.parameters["reference_images"][0]["sha256"] == ANCHOR_SHA

    anchor.queue(image_result(png_bytes(2752, 1536)))  # 模型原生尺寸
    review.queue(CoverLlmResult({"issues": []}, "receipt-r", "gpt-5.5", "c" * 64))
    completed = service.generate(owner_id, created.id)
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert anchor.calls and anchor.calls[0][1] == 1  # 传入 1 张参考图
    assert provider.calls == []  # 未走文本通道
    assert completed.qc_width == 2560 and completed.qc_height == 1440
    assert completed.qc_mime == "image/png"  # 归一化为 PNG
    assert completed.downloaded_sha256 != completed.object_sha256  # 原始/归档双哈希
    assert completed.downloaded_sha256 == hashlib.sha256(png_bytes(2752, 1536)).hexdigest()
    assert len(review.calls) == 1  # 复审作用于归一化后的产物
    # 归一化产物可被媒体完整性校验读取
    content, mime, digest = service.media_bytes(owner_id, project_id, completed.id)
    assert digest == completed.object_sha256 and mime == "image/png" and content
    # 终态重放零重复调用
    replay = service.generate(owner_id, created.id)
    assert replay.status == COVER_STATUS_SUCCEEDED
    assert len(anchor.calls) == 1 and len(review.calls) == 1


def test_no_reference_falls_back_to_text_channel(harness) -> None:
    repo, projects, provider, anchor, review, storage, build = harness
    service = build()
    owner_id = uuid4()
    _channel_id, project_id = _seed_project(repo, projects, owner_id)
    _seed_prompt(repo, owner_id, project_id, style_source="none", profile_id=None)

    created = service.create(owner_id, project_id, idempotency_key="text-1")
    assert created.parameters == {}
    assert created.model == "gpt-image-2"
    provider.queue(image_result(png_bytes(2560, 1440)))
    review.queue(CoverLlmResult({"issues": []}, "receipt-r", "gpt-5.5", "c" * 64))
    completed = service.generate(owner_id, created.id)
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert provider.calls == ["面板当前提示词"]
    assert anchor.calls == []
    assert completed.downloaded_sha256 == completed.object_sha256  # 文本通道无双哈希分叉


def test_anchor_disabled_falls_back_to_text_channel(harness) -> None:
    repo, projects, provider, anchor, review, storage, build = harness
    service = build(anchor_enabled=False)
    owner_id = uuid4()
    channel_id, project_id = _seed_project(repo, projects, owner_id)
    profile_id = _seed_anchor_profile(repo, owner_id, channel_id)
    _seed_prompt(repo, owner_id, project_id, profile_id=profile_id)
    created = service.create(owner_id, project_id, idempotency_key="off-1")
    assert created.parameters == {}
    provider.queue(image_result(png_bytes(2560, 1440)))
    review.queue(CoverLlmResult({"issues": []}, "receipt-r", "gpt-5.5", "c" * 64))
    completed = service.generate(owner_id, created.id)
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert anchor.calls == []


def test_anchor_unconfigured_fails_at_create(harness) -> None:
    repo, projects, _provider, _anchor, _review, _storage, build = harness
    service = build(anchor_provider=None)
    owner_id = uuid4()
    channel_id, project_id = _seed_project(repo, projects, owner_id)
    profile_id = _seed_anchor_profile(repo, owner_id, channel_id)
    _seed_prompt(repo, owner_id, project_id, profile_id=profile_id)
    with pytest.raises(DomainError) as caught:
        service.create(owner_id, project_id, idempotency_key="misconfig-1")
    assert caught.value.code == "cover_image_anchor_not_configured"


def test_anchor_provider_failure_marks_failed_without_review(harness) -> None:
    repo, projects, _provider, anchor, review, storage, build = harness
    service = build()
    owner_id = uuid4()
    channel_id, project_id = _seed_project(repo, projects, owner_id)
    profile_id = _seed_anchor_profile(repo, owner_id, channel_id)
    storage.objects[f"cover-ref-images/channel/{channel_id}/{ANCHOR_SHA}"] = ANCHOR_BYTES
    _seed_prompt(repo, owner_id, project_id, profile_id=profile_id)
    created = service.create(owner_id, project_id, idempotency_key="anchor-fail")
    anchor.queue(DomainError("cover_image_provider_timeout", "超时"))
    failed = service.generate(owner_id, created.id)
    assert failed.status == COVER_STATUS_FAILED
    assert failed.failure_stage == "generate"
    assert failed.failure_code == "cover_image_provider_timeout"
    assert review.calls == []


def test_anchor_idempotency_replay_and_cross_channel_conflict(harness) -> None:
    repo, projects, _provider, _anchor, _review, _storage, build = harness
    service = build()
    owner_id = uuid4()
    channel_id, project_id = _seed_project(repo, projects, owner_id)
    profile_id = _seed_anchor_profile(repo, owner_id, channel_id)
    _seed_prompt(repo, owner_id, project_id, profile_id=profile_id)
    first = service.create(owner_id, project_id, idempotency_key="same-key")
    replay = service.create(owner_id, project_id, idempotency_key="same-key")
    assert replay.id == first.id
    assert len(repo.image_generations) == 1
    # 同键但提示词面板文本变化 → canonical 变化 → 冲突
    repo.update_prompt_current(owner_id, first.prompt_generation_id, "改过的提示词")
    with pytest.raises(DomainError) as caught:
        service.create(owner_id, project_id, idempotency_key="same-key")
    assert caught.value.code == "idempotency_conflict"
