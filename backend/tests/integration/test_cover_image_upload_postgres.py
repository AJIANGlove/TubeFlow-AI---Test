"""封面图上传版本的 PostgreSQL 集成测试（真实迁移 0042 + 指针/去重/导出）。

覆盖：source='upload' 与可空提示词字段落库、同字节去重不增版本、当前指针切换、
原字节经受保护媒体读回、导出 ZIP 取当前封面原图。
"""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import text

from tubeflow_test.adapters.persistence.cover.repository import SqlAlchemyCoverRepository
from tubeflow_test.api.media_runtime import _load_export_cover
from tubeflow_test.application.cover_images import CoverImageService
from tubeflow_test.application.cover_ports import COVER_IMAGE_SOURCE_UPLOAD

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def _png(width: int = 1280, height: int = 720) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (40, 80, 120)).save(buffer, format="PNG")
    return buffer.getvalue()


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put_at_key(self, key: str, body: bytes, media_type: str, expected_sha256: str) -> None:
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        with self._lock:
            self.objects.setdefault(key, body)

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.objects[key]
        assert len(body) == expected_size
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        return body

    def get(self, key: str) -> bytes:
        return self.objects[key]


class UnusedQc:
    def inspect(
        self, path: Path, expected_sha256: str | None = None
    ) -> tuple[str, int, int, str, int]:
        raise AssertionError("upload must not call image QC")


class UnusedProvider:
    def generate(self, *, prompt: str) -> object:
        raise AssertionError("upload must not call image provider")


def _service(
    postgres_database: object, tmp_path: Path, storage: FakeStorage
) -> CoverImageService:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    repo = SqlAlchemyCoverRepository(sessions.begin)
    return CoverImageService(
        repo,
        repo,
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


def _freeze_project(postgres_database: object, project_id: UUID) -> None:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        session.execute(
            text(
                "UPDATE projects SET snapshot_script = :script, snapshot_locale = :locale "
                "WHERE id = :id"
            ),
            {"script": "冻结文案全文", "locale": "zh-CN", "id": project_id},
        )


def _current_cover_id(postgres_database: object, project_id: UUID) -> UUID | None:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        return session.scalar(
            text(
                "SELECT current_cover_image_generation_id FROM projects WHERE id = :id"
            ),
            {"id": project_id},
        )


def _row(postgres_database: object, generation_id: UUID) -> tuple[object, ...]:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        return session.execute(
            text(
                "SELECT source, status, prompt_generation_id, model, size, quality, "
                "qc_width, qc_height, qc_mime, object_key, object_sha256, downloaded_size, "
                "idempotency_key FROM cover_image_generations WHERE id = :id"
            ),
            {"id": generation_id},
        ).one()


def test_uploaded_cover_persists_source_nullable_prompt_and_pointer(
    postgres_database: object, production_parent_chain: object, tmp_path: Path
) -> None:
    ids = production_parent_chain
    _freeze_project(postgres_database, ids.project_id)  # type: ignore[attr-defined]
    storage = FakeStorage()
    service = _service(postgres_database, tmp_path, storage)
    content = _png()

    generation, reused = service.upload(
        ids.owner_id,  # type: ignore[attr-defined]
        ids.project_id,  # type: ignore[attr-defined]
        filename="cover.png",
        content=content,
    )

    assert reused is False
    assert generation.source == COVER_IMAGE_SOURCE_UPLOAD
    assert generation.version == 1
    assert generation.prompt_generation_id is None
    assert (generation.qc_width, generation.qc_height) == (1280, 720)
    row = _row(postgres_database, generation.id)
    assert row[0] == "upload"
    assert row[1] == "SUCCEEDED"
    assert row[2:6] == (None, None, None, None)
    assert row[6:9] == (1280, 720, "image/png")
    assert row[9] == generation.object_key
    assert row[10] == hashlib.sha256(content).hexdigest()
    assert row[11] == len(content)
    assert row[12] == f"upload:{ids.project_id}:{hashlib.sha256(content).hexdigest()}"  # type: ignore[attr-defined]
    assert _current_cover_id(postgres_database, ids.project_id) == generation.id  # type: ignore[attr-defined]

    body, media_type, digest = service.media_bytes(
        ids.owner_id, ids.project_id, generation.id  # type: ignore[attr-defined]
    )
    assert body == content
    assert media_type == "image/png"
    assert digest == hashlib.sha256(content).hexdigest()


def test_uploaded_cover_dedupes_and_exports_original(
    postgres_database: object, production_parent_chain: object, tmp_path: Path
) -> None:
    ids = production_parent_chain
    _freeze_project(postgres_database, ids.project_id)  # type: ignore[attr-defined]
    storage = FakeStorage()
    service = _service(postgres_database, tmp_path, storage)
    content = _png()

    first, _ = service.upload(
        ids.owner_id, ids.project_id, filename="a.png", content=content  # type: ignore[attr-defined]
    )
    again, reused = service.upload(
        ids.owner_id, ids.project_id, filename="b.png", content=content  # type: ignore[attr-defined]
    )

    assert reused is True
    assert again.id == first.id
    assert len(storage.objects) == 1
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        count = session.scalar(
            text(
                "SELECT count(*) FROM cover_image_generations WHERE project_id = :id"
            ),
            {"id": ids.project_id},  # type: ignore[attr-defined]
        )
    assert count == 1

    source_root = tmp_path / "export"
    source_root.mkdir()
    with sessions.begin() as session:
        export_cover = _load_export_cover(
            session,
            storage,  # type: ignore[arg-type]
            ids.project_id,  # type: ignore[attr-defined]
            ids.owner_id,  # type: ignore[attr-defined]
            source_root,
        )
    assert export_cover is not None
    path, arcname = export_cover
    assert arcname == "cover.png"
    assert path.read_bytes() == content


def test_concurrent_upload_of_same_bytes_leaves_one_row(
    postgres_database: object, production_parent_chain: object, tmp_path: Path
) -> None:
    ids = production_parent_chain
    _freeze_project(postgres_database, ids.project_id)  # type: ignore[attr-defined]
    storage = FakeStorage()
    service = _service(postgres_database, tmp_path, storage)
    content = _png()

    def upload() -> tuple[UUID, bool]:
        generation, reused = service.upload(
            ids.owner_id,  # type: ignore[attr-defined]
            ids.project_id,  # type: ignore[attr-defined]
            filename="cover.png",
            content=content,
        )
        return generation.id, reused

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: upload(), range(2)))

    assert len({generation_id for generation_id, _ in results}) == 1
    assert any(reused for _, reused in results)
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        count = session.scalar(
            text(
                "SELECT count(*) FROM cover_image_generations WHERE project_id = :id"
            ),
            {"id": ids.project_id},  # type: ignore[attr-defined]
        )
    assert count == 1
