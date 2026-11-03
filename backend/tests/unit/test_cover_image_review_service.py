"""出图后视觉复审服务测试：成功/阻断不阻断/失败降级/开关/重放零重复调用。

全部内存 fake + Pillow 真实解码（零网络）；复审失败绝不引发重复出图。
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
    FakeCoverImageProvider,
    FakeCoverImageReviewProvider,
)
from tubeflow_test.application.cover_images import CoverImageService
from tubeflow_test.application.cover_ports import (
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    CoverPromptGeneration,
)
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_providers import CoverImageResult, CoverLlmResult
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


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


def png_bytes(width: int = 2560, height: int = 1440) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (8, 16, 32)).save(buffer, format="PNG")
    return buffer.getvalue()


def review_result(payload: dict) -> CoverLlmResult:
    return CoverLlmResult(payload, "receipt-r1", "gpt-5.5", "b" * 64)


@pytest.fixture
def harness(tmp_path: Path):
    repo = InMemoryCoverRepository()
    projects = FakeProjects()
    provider = FakeCoverImageProvider()
    review = FakeCoverImageReviewProvider()
    storage = FakeStorage()

    def build(*, review_enabled: bool = True) -> CoverImageService:
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
            review_enabled=review_enabled,
        )

    return repo, projects, provider, review, build


def _seed(repo, projects, owner_id: UUID) -> UUID:
    project_id = uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.contexts[(owner_id, project_id)] = CoverProjectContext(
        project_id, uuid4(), "测试标题", "冻结文案", "zh-CN"
    )
    generation = repo.create_prompt_generation(
        CoverPromptGeneration(
            uuid4(),
            owner_id,
            project_id,
            0,
            "channel_profile",
            uuid4(),
            "风格：艺术流派：平涂卡通",
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
            {"cover_text": None},
            f"prompt-key-{uuid4()}",
            COVER_STATUS_INTENT_PERSISTED,
            None,
            None,
            NOW,
        )
    )
    completed = repo.complete_prompt_generation(replace(generation, status=COVER_STATUS_SUCCEEDED))
    repo.set_current_prompt_generation(owner_id, project_id, completed.id)
    return project_id


def _generate(repo, provider, service, owner_id: UUID, project_id: UUID, key: str):
    generation = service.create(owner_id, project_id, idempotency_key=key)
    provider.queue(
        CoverImageResult(png_bytes(), hashlib.sha256(png_bytes()).hexdigest(), "r1", "b64_json")
    )
    return service.generate(owner_id, generation.id)


def test_review_success_stored_and_messages_carry_two_images(harness) -> None:
    repo, projects, provider, review, build = harness
    service = build()
    owner_id = uuid4()
    project_id = _seed(repo, projects, owner_id)
    review.queue(review_result({"issues": []}))
    completed = _generate(repo, provider, service, owner_id, project_id, "img-r1")
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert completed.visual_review == {
        "status": "succeeded",
        "issues": [],
        "ok": True,
        "model": "gpt-5.5",
        "receipt": "receipt-r1",
    }
    assert len(review.calls) == 1
    messages = review.calls[0]
    content = messages[1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url", "image_url"]
    assert "平涂卡通" in content[0]["text"]
    assert "默认文字" in content[0]["text"]
    # 落库后可回读（内存仓储不丢字段）
    stored = repo.get_image_generation(owner_id, completed.id)
    assert stored is not None and stored.visual_review == completed.visual_review


def test_review_blocker_does_not_block_success_or_current_switch(harness) -> None:
    repo, projects, provider, review, build = harness
    service = build()
    owner_id = uuid4()
    project_id = _seed(repo, projects, owner_id)
    review.queue(
        review_result(
            {
                "issues": [
                    {
                        "kind": "text_spelling",
                        "severity": "blocker",
                        "description": "拼写错误",
                        "suggested_edit": "改为 X",
                    }
                ]
            }
        )
    )
    completed = _generate(repo, provider, service, owner_id, project_id, "img-r2")
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert completed.visual_review is not None and completed.visual_review["ok"] is False
    current = repo.current_image_generation_for_project(owner_id, project_id)
    assert current is not None and current.id == completed.id  # 首成功仍自动当前（不阻断）
    assert len(provider.calls) == 1  # 复审有 blocker 不自动重提


def test_review_domain_error_degrades_to_failed_without_regeneration(harness) -> None:
    repo, projects, provider, review, build = harness
    service = build()
    owner_id = uuid4()
    project_id = _seed(repo, projects, owner_id)
    review.queue(DomainError("cover_llm_provider_timeout", "超时"))
    completed = _generate(repo, provider, service, owner_id, project_id, "img-r3")
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert completed.visual_review == {"status": "failed", "code": "cover_llm_provider_timeout"}
    assert len(provider.calls) == 1


def test_review_unexpected_exception_degrades_to_failed(harness) -> None:
    repo, projects, provider, review, build = harness
    service = build()
    owner_id = uuid4()
    project_id = _seed(repo, projects, owner_id)
    review.queue(RuntimeError("boom"))
    completed = _generate(repo, provider, service, owner_id, project_id, "img-r4")
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert completed.visual_review == {"status": "failed", "code": "cover_image_review_failed"}
    assert len(provider.calls) == 1


def test_review_disabled_skips_review_calls(harness) -> None:
    repo, projects, provider, review, build = harness
    service = build(review_enabled=False)
    owner_id = uuid4()
    project_id = _seed(repo, projects, owner_id)
    completed = _generate(repo, provider, service, owner_id, project_id, "img-r5")
    assert completed.status == COVER_STATUS_SUCCEEDED
    assert completed.visual_review is None
    assert review.calls == []


def test_terminal_replay_makes_no_duplicate_review_calls(harness) -> None:
    repo, projects, provider, review, build = harness
    service = build()
    owner_id = uuid4()
    project_id = _seed(repo, projects, owner_id)
    review.queue(review_result({"issues": []}))
    generation = service.create(owner_id, project_id, idempotency_key="img-r6")
    provider.queue(
        CoverImageResult(png_bytes(), hashlib.sha256(png_bytes()).hexdigest(), "r1", "b64_json")
    )
    completed = service.generate(owner_id, generation.id)
    assert completed.status == COVER_STATUS_SUCCEEDED
    replay = service.generate(owner_id, generation.id)
    assert replay.status == COVER_STATUS_SUCCEEDED
    assert len(provider.calls) == 1
    assert len(review.calls) == 1
