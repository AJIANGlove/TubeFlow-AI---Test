"""封面图服务测试：create 幂等/门禁、生成 → QC → 归档 → 终态与当前指针。

覆盖：无当前提示词拒绝、未冻结拒绝、首成功自动当前、后续成功不自动当前、用户
明确切换、Provider 失败/QC 失败落 FAILED 稳定码、终态重放零重复 Provider 调用。
全部内存 fake + Pillow 真实解码（零网络）。
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
from tubeflow_test.adapters.in_memory_cover_providers import FakeCoverImageProvider
from tubeflow_test.application.cover_images import CoverImageService
from tubeflow_test.application.cover_ports import (
    COVER_STATUS_FAILED,
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    CoverPromptGeneration,
)
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_providers import CoverImageResult
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


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

    def add(self, owner_id: UUID, project_id: UUID, *, script: str | None = "冻结文案") -> None:
        self.contexts[(owner_id, project_id)] = CoverProjectContext(
            project_id, uuid4(), "测试标题", script, "zh-CN"
        )

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


def png_bytes(width: int = 2560, height: int = 1440) -> bytes:
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
    storage = FakeStorage()
    service = CoverImageService(
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
    )
    return repo, projects, provider, storage, service


def _project(repo, projects, owner_id: UUID) -> UUID:
    project_id = uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.add(owner_id, project_id)
    return project_id


def _prompt_generation(
    owner_id: UUID, project_id: UUID, *, prompt_current: str
) -> CoverPromptGeneration:
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
        "",
        [],
        "默认文字",
        "默认文字",
        "维度文本",
        "系统版提示词",
        prompt_current,
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


def _seed_current_prompt(
    repo, owner_id: UUID, project_id: UUID, *, prompt_current: str = "面板当前提示词"
) -> CoverPromptGeneration:
    generation = repo.create_prompt_generation(
        _prompt_generation(owner_id, project_id, prompt_current=prompt_current)
    )
    completed = repo.complete_prompt_generation(replace(generation, status=COVER_STATUS_SUCCEEDED))
    return repo.set_current_prompt_generation(owner_id, project_id, completed.id)


def test_create_requires_current_prompt_and_frozen_project(harness) -> None:
    repo, projects, _provider, _storage, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    with pytest.raises(DomainError) as missing_prompt:
        service.create(owner_id, project_id, idempotency_key="k1")
    assert missing_prompt.value.code == "cover_prompt_missing"

    unfrozen = uuid4()
    repo.set_project_state(owner_id, unfrozen, "ACTIVE")
    projects.add(owner_id, unfrozen, script=None)
    with pytest.raises(DomainError) as not_frozen:
        service.create(owner_id, unfrozen, idempotency_key="k2")
    assert not_frozen.value.code == "cover_project_not_frozen"
    with pytest.raises(DomainError) as unknown:
        service.create(owner_id, uuid4(), idempotency_key="k3")
    assert unknown.value.code == "not_found"

    _seed_current_prompt(repo, owner_id, project_id, prompt_current="   ")
    with pytest.raises(DomainError) as empty:
        service.create(owner_id, project_id, idempotency_key="k4")
    assert empty.value.code == "cover_prompt_empty"


def test_generate_success_first_auto_current_second_not(harness) -> None:
    repo, projects, provider, storage, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    _seed_current_prompt(repo, owner_id, project_id)

    first = service.create(owner_id, project_id, idempotency_key="img-1")
    assert first.status == COVER_STATUS_INTENT_PERSISTED
    assert first.prompt == "面板当前提示词"
    provider.queue(image_result(png_bytes()))
    completed_first = service.generate(owner_id, first.id)
    assert completed_first.status == COVER_STATUS_SUCCEEDED
    assert completed_first.qc_width == 2560 and completed_first.qc_height == 1440
    assert completed_first.qc_mime == "image/png"
    assert completed_first.object_key == f"cover-images/{owner_id}/{first.id}"
    assert completed_first.object_sha256 == completed_first.downloaded_sha256
    assert storage.objects[completed_first.object_key]  # 归档字节存在
    current = repo.current_image_generation_for_project(owner_id, project_id)
    assert current is not None and current.id == first.id  # 首成功自动当前

    second = service.create(owner_id, project_id, idempotency_key="img-2")
    assert second.version == 2
    provider.queue(image_result(png_bytes()))
    completed_second = service.generate(owner_id, second.id)
    assert completed_second.status == COVER_STATUS_SUCCEEDED
    current = repo.current_image_generation_for_project(owner_id, project_id)
    assert current is not None and current.id == first.id  # 后续成功不自动当前

    switched = service.set_current(owner_id, project_id, second.id)
    assert switched.id == second.id
    assert repo.current_image_generation_for_project(owner_id, project_id).id == second.id  # type: ignore[union-attr]


def test_create_idempotency_replay_and_conflict(harness) -> None:
    repo, projects, provider, _storage, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    _seed_current_prompt(repo, owner_id, project_id)
    first = service.create(owner_id, project_id, idempotency_key="same")
    replay = service.create(owner_id, project_id, idempotency_key="same")
    assert replay.id == first.id
    assert len(repo.image_generations) == 1
    image_events = [
        record
        for record in repo.outbox.values()
        if record.event_type == "COVER_IMAGE_GENERATION_REQUESTED"
    ]
    assert len(image_events) == 1  # 重放不重复写事件
    assert provider.calls == []

    # 面板文本变化 → canonical 变化 → 同键冲突。
    repo.update_prompt_current(owner_id, first.prompt_generation_id, "改过的提示词")
    with pytest.raises(DomainError) as conflict:
        service.create(owner_id, project_id, idempotency_key="same")
    assert conflict.value.code == "idempotency_conflict"


def test_generate_provider_failure_marks_failed_with_stable_code(harness) -> None:
    repo, projects, provider, _storage, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    _seed_current_prompt(repo, owner_id, project_id)
    generation = service.create(owner_id, project_id, idempotency_key="img-fail")
    provider.queue(DomainError("cover_image_provider_http_500", "boom"))
    failed = service.generate(owner_id, generation.id)
    assert failed.status == COVER_STATUS_FAILED
    assert failed.failure_stage == "generate"
    assert failed.failure_code == "cover_image_provider_http_500"
    with pytest.raises(DomainError) as invalid:
        service.set_current(owner_id, project_id, generation.id)
    assert invalid.value.code == "invalid_cover_candidate"


def test_generate_qc_failure_marks_failed(harness) -> None:
    repo, projects, provider, _storage, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    _seed_current_prompt(repo, owner_id, project_id)
    generation = service.create(owner_id, project_id, idempotency_key="img-qc")
    provider.queue(image_result(png_bytes(1280, 720)))  # 尺寸不符
    failed = service.generate(owner_id, generation.id)
    assert failed.status == COVER_STATUS_FAILED
    assert failed.failure_stage == "qc"
    assert failed.failure_code == "cover_image_qc_size_mismatch"


def test_generate_terminal_replay_makes_no_duplicate_provider_calls(harness) -> None:
    repo, projects, provider, storage, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    _seed_current_prompt(repo, owner_id, project_id)
    generation = service.create(owner_id, project_id, idempotency_key="img-replay")
    provider.queue(image_result(png_bytes()))
    completed = service.generate(owner_id, generation.id)
    assert completed.status == COVER_STATUS_SUCCEEDED
    replay = service.generate(owner_id, generation.id)
    assert replay.status == COVER_STATUS_SUCCEEDED
    assert len(provider.calls) == 1
    assert len(storage.puts) == 1
