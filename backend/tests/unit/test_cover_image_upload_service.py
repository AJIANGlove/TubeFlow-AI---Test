"""上传封面图服务测试：校验顺序、版本/当前指针、同字节去重、不写 outbox。

全部内存 fake（仓储/存储/Provider），零真实调用零网络。上传是同步动作，
不产生 INTENT_PERSISTED/FAILED 版本，也不应触发任何模型或 QC 调用。
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.application.cover_images import (
    COVER_IMAGE_UPLOAD_MAX_BYTES,
    CoverImageService,
)
from tubeflow_test.application.cover_ports import (
    COVER_IMAGE_SOURCE_UPLOAD,
    COVER_STATUS_SUCCEEDED,
    CoverImageGeneration,
)
from tubeflow_test.application.cover_project import (
    COVER_PROJECT_NOT_FROZEN,
    CoverProjectContext,
)
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


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

    def add(self, owner_id: UUID, project_id: UUID, *, frozen: bool = True) -> None:
        self.contexts[(owner_id, project_id)] = CoverProjectContext(
            project_id,
            uuid4(),
            "测试标题",
            "冻结文案全文" if frozen else None,
            "zh-CN",
        )

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


class UnusedQc:
    def inspect(
        self, path: Path, expected_sha256: str | None = None
    ) -> tuple[str, int, int, str, int]:
        raise AssertionError("upload must not call image QC")

    def make_thumbnail(self, path: Path, *, width: int, height: int) -> bytes:
        raise AssertionError("upload must not call image QC")


class UnusedProvider:
    def generate(self, *, prompt: str) -> object:
        raise AssertionError("upload must not call image provider")


def png_bytes(width: int = 1280, height: int = 720) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 1280, height: int = 720) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (90, 80, 70)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def gif_bytes(width: int = 1280, height: int = 720) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="GIF")
    return buffer.getvalue()


def png_with_declared_size(width: int, height: int) -> bytes:
    """只改 IHDR 声明尺寸的 1x1 PNG：用于在解码前触发像素上限校验。"""
    data = bytearray(png_bytes(1, 1))
    struct.pack_into(">II", data, 16, width, height)
    struct.pack_into(">I", data, 29, zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


def service(
    tmp_path: Path,
) -> tuple[CoverImageService, InMemoryCoverRepository, FakeProjects, FakeStorage]:
    repo = InMemoryCoverRepository()
    projects = FakeProjects()
    storage = FakeStorage()
    images = CoverImageService(
        repo,
        projects,
        UnusedProvider(),  # type: ignore[arg-type]
        UnusedQc(),  # type: ignore[arg-type]
        storage,
        Ids(),
        Clock(),
        tmp_path,
        model="fake-image",
        size="2560x1440",
        quality="high",
    )
    return images, repo, projects, storage


def seed_project(
    repo: InMemoryCoverRepository,
    projects: FakeProjects,
    *,
    owner_id: UUID,
    frozen: bool = True,
) -> UUID:
    project_id = uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.add(owner_id, project_id, frozen=frozen)
    return project_id


def test_upload_creates_current_succeeded_version_without_prompt(tmp_path: Path) -> None:
    images, repo, projects, storage = service(tmp_path)
    owner_id = uuid4()
    project_id = seed_project(repo, projects, owner_id=owner_id)
    content = png_bytes()

    generation, reused = images.upload(
        owner_id, project_id, filename="cover.png", content=content
    )

    assert reused is False
    assert generation.source == COVER_IMAGE_SOURCE_UPLOAD
    assert generation.status == COVER_STATUS_SUCCEEDED
    assert generation.version == 1
    assert generation.prompt_generation_id is None
    assert generation.model is None and generation.size is None and generation.quality is None
    assert generation.qc_width == 1280 and generation.qc_height == 720
    assert generation.qc_mime == "image/png"
    assert generation.object_sha256 == hashlib.sha256(content).hexdigest()
    assert generation.downloaded_size == len(content)
    assert generation.parameters == {"source_filename": "cover.png"}
    assert generation.visual_review is None
    assert repo.current_image_generation_for_project(owner_id, project_id) == generation
    assert storage.objects[generation.object_key or ""] == content
    assert repo.outbox == {}


def test_upload_reuses_identical_bytes_and_switches_current(tmp_path: Path) -> None:
    images, repo, projects, storage = service(tmp_path)
    owner_id = uuid4()
    project_id = seed_project(repo, projects, owner_id=owner_id)
    first, _ = images.upload(owner_id, project_id, filename="a.png", content=png_bytes())
    second, _ = images.upload(owner_id, project_id, filename="b.png", content=jpeg_bytes())
    repo.set_current_image_generation(owner_id, project_id, first.id)
    objects_before = dict(storage.objects)

    reused, was_reused = images.upload(
        owner_id, project_id, filename="a-again.png", content=png_bytes()
    )

    assert was_reused is True
    assert reused.id == first.id
    assert reused.version == 1
    assert len(repo.list_image_generations_for_project(owner_id, project_id)) == 2
    assert repo.current_image_generation_for_project(owner_id, project_id) == first
    # 对象键含 sha256：重复上传复用同一对象，不新增对象、不新增版本。
    assert storage.objects == objects_before
    assert second.version == 2


def test_upload_version_follows_generated_cover(tmp_path: Path) -> None:
    images, repo, projects, _storage = service(tmp_path)
    owner_id = uuid4()
    project_id = seed_project(repo, projects, owner_id=owner_id)
    repo.create_image_generation(
        CoverImageGeneration(
            id=uuid4(),
            owner_id=owner_id,
            project_id=project_id,
            prompt_generation_id=uuid4(),
            version=0,
            prompt="proceed",
            model="fake-image",
            size="2560x1440",
            quality="high",
            parameters={},
            idempotency_key="generated-1",
            provider_idempotency_key="provider-1",
            canonical_request_sha256="a" * 64,
            status=COVER_STATUS_SUCCEEDED,
            provider_receipt=None,
            expected_sha256=None,
            downloaded_sha256=None,
            downloaded_size=None,
            qc_width=2560,
            qc_height=1440,
            qc_mime="image/png",
            object_key=None,
            object_sha256=None,
            failure_stage=None,
            failure_code=None,
            created_at=NOW,
        )
    )

    generation, _ = images.upload(
        owner_id, project_id, filename="cover.png", content=png_bytes()
    )

    assert generation.version == 2


def test_upload_requires_frozen_project(tmp_path: Path) -> None:
    images, repo, projects, storage = service(tmp_path)
    owner_id = uuid4()
    project_id = seed_project(repo, projects, owner_id=owner_id, frozen=False)

    with pytest.raises(DomainError) as excinfo:
        images.upload(owner_id, project_id, filename="cover.png", content=png_bytes())

    assert excinfo.value.code == COVER_PROJECT_NOT_FROZEN
    assert storage.puts == []


def test_upload_rejects_foreign_or_unknown_project_without_orphan(tmp_path: Path) -> None:
    images, repo, projects, storage = service(tmp_path)
    owner_id = uuid4()
    seed_project(repo, projects, owner_id=owner_id)

    with pytest.raises(DomainError) as excinfo:
        images.upload(uuid4(), uuid4(), filename="cover.png", content=png_bytes())

    assert excinfo.value.code == "not_found"
    assert storage.puts == []


@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("cover.gif", gif_bytes(), "invalid_image_type"),
        ("cover.png", b"x" * (COVER_IMAGE_UPLOAD_MAX_BYTES + 1), "invalid_image_size"),
        ("cover.png", png_bytes(1024, 576), "invalid_image_dimensions"),
        ("cover.png", png_with_declared_size(8000, 6000), "invalid_image_dimensions"),
        ("cover.png", png_bytes(1360, 720), "invalid_image_aspect_ratio"),
        ("cover.png", png_bytes(1300, 720), "invalid_image_aspect_ratio"),
    ],
    ids=[
        "gif-format",
        "over-25mb",
        "below-min-size",
        "over-40mp",
        "ratio-1360x720",
        "ratio-1300x720",
    ],
)
def test_upload_validation_rejects_without_writing(
    tmp_path: Path, filename: str, content: bytes, code: str
) -> None:
    images, repo, projects, storage = service(tmp_path)
    owner_id = uuid4()
    project_id = seed_project(repo, projects, owner_id=owner_id)

    with pytest.raises(DomainError) as excinfo:
        images.upload(owner_id, project_id, filename=filename, content=content)

    assert excinfo.value.code == code
    assert repo.list_image_generations_for_project(owner_id, project_id) == ()
    assert storage.puts == []


def test_upload_accepts_one_percent_ratio_tolerance(tmp_path: Path) -> None:
    images, repo, projects, _storage = service(tmp_path)
    owner_id = uuid4()
    project_id = seed_project(repo, projects, owner_id=owner_id)

    generation, _ = images.upload(
        owner_id, project_id, filename="cover.png", content=png_bytes(1290, 720)
    )

    assert (generation.qc_width, generation.qc_height) == (1290, 720)


def test_upload_preserves_original_bytes_and_media_type(tmp_path: Path) -> None:
    images, repo, projects, storage = service(tmp_path)
    owner_id = uuid4()
    project_id = seed_project(repo, projects, owner_id=owner_id)
    content = jpeg_bytes()

    generation, _ = images.upload(
        owner_id, project_id, filename="cover.jpg", content=content
    )

    assert generation.qc_mime == "image/jpeg"
    assert storage.objects[generation.object_key or ""] == content
    media, media_type, digest = images.media_bytes(owner_id, project_id, generation.id)
    assert media == content
    assert media_type == "image/jpeg"
    assert digest == hashlib.sha256(content).hexdigest()
