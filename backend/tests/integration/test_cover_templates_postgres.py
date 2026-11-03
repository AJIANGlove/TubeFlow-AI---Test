"""频道模板（多模板版）的 PostgreSQL 集成测试。

覆盖：真实迁移建表、创建/自动编号、手动保存与幂等、BYTEA 往返、压缩尺寸、
排序、删除、跨账号隔离。
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image

from tubeflow_test.adapters.persistence.identity_project.models import AccountRow, ChannelRow
from tubeflow_test.cover_templates.repository import SqlCoverTemplateRepository
from tubeflow_test.cover_templates.service import CoverTemplateService
from tubeflow_test.domain.errors import DomainError

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (40, 80, 120)).save(buffer, format="PNG")
    return buffer.getvalue()


def _service(postgres_database: object) -> CoverTemplateService:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    return CoverTemplateService(
        SqlCoverTemplateRepository(sessions.begin), clock=lambda: NOW
    )


def _seed_channel(postgres_database: object, owner_id: UUID) -> UUID:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    channel_id = uuid4()
    with sessions.begin() as session:
        session.add(
            AccountRow(
                id=owner_id,
                username=f"owner-{uuid4().hex[:8]}",
                password_hash="hashed",
                role="user",
                active=True,
                session_generation=1,
            )
        )
        session.add(
            ChannelRow(
                id=channel_id,
                owner_id=owner_id,
                url="https://example.test/t",
                name="模板频道",
            )
        )
    return channel_id


def test_create_save_reorder_and_upload(postgres_database: object) -> None:
    owner_id = uuid4()
    channel_id = _seed_channel(postgres_database, owner_id)
    service = _service(postgres_database)

    assert service.view(owner_id, channel_id).items == ()

    first = service.create(owner_id, channel_id)
    second = service.create(owner_id, channel_id, "手写名称")
    third = service.create(owner_id, channel_id)
    assert [item.name for item in (first, second, third)] == ["模板 1", "手写名称", "模板 2"]

    saved = service.update(
        owner_id,
        channel_id,
        first.id,
        name="模板 1",
        prompt="第一版",
        save_key="key-1",
    )
    assert saved.prompt == "第一版"
    retried = service.update(
        owner_id,
        channel_id,
        first.id,
        name="模板 1",
        prompt="不应覆盖",
        save_key="key-1",
    )
    assert retried.prompt == "第一版"

    payload = _png(2000, 1000)
    uploaded = service.upload_cover(
        owner_id, channel_id, first.id, filename="cover.png", content=payload
    )
    assert uploaded.cover is not None
    assert (uploaded.cover.width, uploaded.cover.height) == (2000, 1000)
    assert uploaded.cover.byte_size == len(payload)
    assert uploaded.preview is not None
    assert (uploaded.preview.width, uploaded.preview.height) == (1280, 640)

    original, media_type, _ = service.media(owner_id, channel_id, first.id, "original")
    assert original == payload
    assert media_type == "image/png"
    preview, preview_type, _ = service.media(owner_id, channel_id, first.id, "preview")
    assert preview_type == "image/webp"
    with Image.open(BytesIO(preview)) as image:
        assert image.size == (1280, 640)

    ordered = service.reorder(
        owner_id, channel_id, (third.id, first.id, second.id)
    )
    assert [item.id for item in ordered.items] == [third.id, first.id, second.id]

    service.delete(owner_id, channel_id, second.id)
    remaining = service.view(owner_id, channel_id).items
    assert [item.id for item in remaining] == [third.id, first.id]

    with pytest.raises(DomainError) as missing:
        service.view(uuid4(), channel_id)
    assert missing.value.code == "not_found"
    with pytest.raises(DomainError):
        service.update(
            uuid4(), channel_id, first.id, name="x", prompt="y", save_key="k"
        )


def test_reorder_rejects_partial_set(postgres_database: object) -> None:
    owner_id = uuid4()
    channel_id = _seed_channel(postgres_database, owner_id)
    service = _service(postgres_database)
    first = service.create(owner_id, channel_id)
    service.create(owner_id, channel_id)
    with pytest.raises(DomainError) as mismatch:
        service.reorder(owner_id, channel_id, (first.id,))
    assert mismatch.value.code == "cover_template_order_mismatch"
