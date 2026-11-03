"""频道模板（多模板版）的服务与 HTTP 合同测试。

覆盖：空列表、创建与自动编号、名称/提示词上限、手动保存与幂等键、删除、
排序与集合不一致、上传压缩与透明、媒体变体、跨账号 404。
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tubeflow_test.cover_templates.api import create_cover_templates_router
from tubeflow_test.cover_templates.domain import (
    CoverTemplateCollection,
    CoverTemplateImageMeta,
    CoverTemplateItem,
)
from tubeflow_test.cover_templates.images import (
    PREVIEW_LONG_EDGE,
    PreparedCoverTemplate,
    StoredImage,
    prepare_cover_template,
)
from tubeflow_test.cover_templates.repository import default_template_name
from tubeflow_test.cover_templates.service import (
    NAME_LIMIT,
    PROMPT_LIMIT,
    CoverTemplateService,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account, AccountRole

NOW = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
OWNER = Account(uuid4(), "owner", "hashed:x", AccountRole.USER)
OTHER = Account(uuid4(), "other", "hashed:x", AccountRole.USER)


def _meta(image: StoredImage) -> CoverTemplateImageMeta:
    return CoverTemplateImageMeta(
        width=image.width,
        height=image.height,
        byte_size=image.byte_size,
        sha256=image.sha256,
        media_type=image.media_type,
        image_format=image.image_format,
    )


class FakeRepo:
    def __init__(self) -> None:
        self.channels: dict[UUID, UUID] = {}
        self.rows: dict[UUID, list[dict[str, object]]] = {}
        self.cover: dict[UUID, PreparedCoverTemplate] = {}

    def add_channel(self, owner_id: UUID = OWNER.id) -> UUID:
        channel_id = uuid4()
        self.channels[channel_id] = owner_id
        self.rows.setdefault(channel_id, [])
        return channel_id

    def channel_exists(self, owner_id: UUID, channel_id: UUID) -> bool:
        return self.channels.get(channel_id) == owner_id

    def _item(self, row: dict[str, object]) -> CoverTemplateItem:
        prepared = self.cover.get(row["id"])  # type: ignore[arg-type]
        return CoverTemplateItem(
            id=row["id"],  # type: ignore[arg-type]
            name=str(row["name"]),
            position=int(row["position"]),  # type: ignore[arg-type]
            prompt=str(row["prompt"]),
            cover=_meta(prepared.original) if prepared else None,
            preview=_meta(prepared.preview) if prepared else None,
            created_at=row["created_at"],  # type: ignore[arg-type]
            updated_at=row["updated_at"],  # type: ignore[arg-type]
        )

    def list_items(self, owner_id: UUID, channel_id: UUID) -> CoverTemplateCollection:
        if not self.channel_exists(owner_id, channel_id):
            raise DomainError("not_found", "Channel was not found")
        rows = sorted(self.rows[channel_id], key=lambda row: int(row["position"]))  # type: ignore[arg-type]
        return CoverTemplateCollection(channel_id, tuple(self._item(row) for row in rows))

    def create_item(
        self, owner_id: UUID, channel_id: UUID, name: str | None, now: datetime
    ) -> CoverTemplateItem:
        rows = self.rows[channel_id]
        row = {
            "id": uuid4(),
            "name": name or default_template_name([str(item["name"]) for item in rows]),
            "position": max((int(item["position"]) for item in rows), default=-1) + 1,
            "prompt": "",
            "last_save_key": None,
            "created_at": now,
            "updated_at": now,
        }
        rows.append(row)
        return self._item(row)

    def update_item(
        self,
        owner_id: UUID,
        channel_id: UUID,
        item_id: UUID,
        name: str,
        prompt: str,
        save_key: str,
        now: datetime,
    ) -> CoverTemplateItem:
        for row in self.rows[channel_id]:
            if row["id"] != item_id:
                continue
            if row["last_save_key"] == save_key:
                return self._item(row)
            row.update(name=name, prompt=prompt, last_save_key=save_key, updated_at=now)
            return self._item(row)
        raise DomainError("not_found", "Cover template was not found")

    def delete_item(self, owner_id: UUID, channel_id: UUID, item_id: UUID) -> None:
        rows = self.rows[channel_id]
        for index, row in enumerate(rows):
            if row["id"] == item_id:
                rows.pop(index)
                self.cover.pop(item_id, None)
                return
        raise DomainError("not_found", "Cover template was not found")

    def reorder(
        self, owner_id: UUID, channel_id: UUID, item_ids: tuple[UUID, ...]
    ) -> CoverTemplateCollection:
        rows = self.rows[channel_id]
        if set(item_ids) != {row["id"] for row in rows} or len(set(item_ids)) != len(item_ids):
            raise DomainError("cover_template_order_mismatch", "模板列表已变化，请刷新后重试。")
        by_id = {row["id"]: row for row in rows}
        self.rows[channel_id] = [
            {**by_id[item_id], "position": index}  # type: ignore[arg-type]
            for index, item_id in enumerate(item_ids)
        ]
        return self.list_items(owner_id, channel_id)

    def replace_cover(
        self,
        owner_id: UUID,
        channel_id: UUID,
        item_id: UUID,
        prepared: PreparedCoverTemplate,
        now: datetime,
    ) -> CoverTemplateItem:
        for row in self.rows[channel_id]:
            if row["id"] == item_id:
                self.cover[item_id] = prepared
                row["updated_at"] = now
                return self._item(row)
        raise DomainError("not_found", "Cover template was not found")

    def media(
        self, owner_id: UUID, channel_id: UUID, item_id: UUID, variant: str
    ) -> tuple[bytes, str, str]:
        prepared = self.cover.get(item_id)
        if prepared is None:
            raise DomainError("not_found", "Cover template image was not found")
        image = prepared.original if variant == "original" else prepared.preview
        return image.data, image.media_type, image.sha256


def png_bytes(width: int = 64, height: int = 36, *, alpha: bool = False) -> bytes:
    buffer = BytesIO()
    mode = "RGBA" if alpha else "RGB"
    color = (10, 20, 30, 0) if alpha else (10, 20, 30)
    image = Image.new(mode, (width, height), color)
    if alpha:
        image.putpixel((0, 0), (255, 0, 0, 255))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_client(repo: FakeRepo, account: Account) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_cover_templates_router(
            CoverTemplateService(repo, clock=lambda: NOW),
            lambda: account,
        )
    )
    return TestClient(app)


def test_empty_list_and_create_with_default_names() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    client = make_client(repo, OWNER)
    empty = client.get(f"/identity/channels/{channel_id}/cover-templates")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["updated_at"] is None

    first = client.post(f"/identity/channels/{channel_id}/cover-templates/items", json={})
    assert first.status_code == 201
    assert first.json()["name"] == "模板 1"
    assert first.json()["position"] == 0
    assert first.json()["cover"] is None

    second = client.post(
        f"/identity/channels/{channel_id}/cover-templates/items", json={"name": "  "}
    )
    assert second.json()["name"] == "模板 2"
    custom = client.post(
        f"/identity/channels/{channel_id}/cover-templates/items",
        json={"name": "  电商大促  "},
    )
    assert custom.json()["name"] == "电商大促"


def test_name_limit_and_create_rejects_unknown_channel() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    client = make_client(repo, OWNER)
    response = client.post(
        f"/identity/channels/{channel_id}/cover-templates/items",
        json={"name": "x" * (NAME_LIMIT + 1)},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "cover_template_name_too_long"
    assert (
        client.post(f"/identity/channels/{uuid4()}/cover-templates/items", json={}).status_code
        == 404
    )


def test_manual_save_requires_key_and_is_idempotent() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    item = repo.create_item(OWNER.id, channel_id, None, NOW)
    client = make_client(repo, OWNER)
    url = f"/identity/channels/{channel_id}/cover-templates/items/{item.id}"

    missing = client.patch(url, json={"name": "模板 1", "prompt": "A"})
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "invalid_parameter"

    saved = client.patch(
        url,
        json={"name": "模板 1", "prompt": "第一版"},
        headers={"Idempotency-Key": "k-1"},
    )
    assert saved.status_code == 200
    assert saved.json()["prompt"] == "第一版"

    retry = client.patch(
        url,
        json={"name": "模板 1", "prompt": "不该覆盖"},
        headers={"Idempotency-Key": "k-1"},
    )
    assert retry.status_code == 200
    assert retry.json()["prompt"] == "第一版"

    changed = client.patch(
        url,
        json={"name": "", "prompt": "第二版"},
        headers={"Idempotency-Key": "k-2"},
    )
    assert changed.status_code == 200
    assert changed.json()["prompt"] == "第二版"
    assert changed.json()["name"] == "模板 1"


def test_prompt_limit_and_nul_rejected() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    item = repo.create_item(OWNER.id, channel_id, None, NOW)
    client = make_client(repo, OWNER)
    url = f"/identity/channels/{channel_id}/cover-templates/items/{item.id}"
    too_long = client.patch(
        url,
        json={"name": "模板 1", "prompt": "x" * (PROMPT_LIMIT + 1)},
        headers={"Idempotency-Key": "k-1"},
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "cover_template_prompt_too_long"
    nul = client.patch(
        url,
        json={"name": "模板 1", "prompt": "bad\u0000text"},
        headers={"Idempotency-Key": "k-2"},
    )
    assert nul.status_code == 422
    assert nul.json()["detail"]["code"] == "invalid_parameter"


def test_delete_and_reorder() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    first = repo.create_item(OWNER.id, channel_id, None, NOW)
    second = repo.create_item(OWNER.id, channel_id, None, NOW)
    third = repo.create_item(OWNER.id, channel_id, None, NOW)
    client = make_client(repo, OWNER)
    base = f"/identity/channels/{channel_id}/cover-templates"

    reordered = client.put(
        f"{base}/items/order",
        json={"item_ids": [str(third.id), str(first.id), str(second.id)]},
    )
    assert reordered.status_code == 200
    assert [item["item_id"] for item in reordered.json()["items"]] == [
        str(third.id),
        str(first.id),
        str(second.id),
    ]

    mismatch = client.put(f"{base}/items/order", json={"item_ids": [str(third.id)]})
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "cover_template_order_mismatch"

    deleted = client.delete(f"{base}/items/{second.id}")
    assert deleted.status_code == 200
    assert [item["item_id"] for item in client.get(base).json()["items"]] == [
        str(third.id),
        str(first.id),
    ]
    assert client.delete(f"{base}/items/{second.id}").status_code == 404


def test_upload_and_media_variants() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    item = repo.create_item(OWNER.id, channel_id, None, NOW)
    client = make_client(repo, OWNER)
    base = f"/identity/channels/{channel_id}/cover-templates/items/{item.id}"
    payload = png_bytes(2000, 1000)
    uploaded = client.post(f"{base}/cover", files={"file": ("cover.png", payload, "image/png")})
    assert uploaded.status_code == 200
    cover = uploaded.json()["cover"]
    assert (cover["width"], cover["height"]) == (2000, 1000)
    assert cover["byte_size"] == len(payload)
    assert cover["preview"]["width"] == PREVIEW_LONG_EDGE
    assert cover["preview"]["height"] == 640

    original = client.get(f"{base}/cover/media/original")
    assert original.content == payload
    assert original.headers["content-type"] == "image/png"
    etag = original.headers["etag"]
    assert (
        client.get(f"{base}/cover/media/original", headers={"If-None-Match": etag}).status_code
        == 304
    )
    ranged = client.get(f"{base}/cover/media/original", headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206
    assert ranged.content == payload[:10]

    preview = client.get(f"{base}/cover/media/preview")
    assert preview.headers["content-type"] == "image/webp"
    with Image.open(BytesIO(preview.content)) as image:
        assert image.size == (1280, 640)


def test_upload_preserves_transparency() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    item = repo.create_item(OWNER.id, channel_id, None, NOW)
    client = make_client(repo, OWNER)
    base = f"/identity/channels/{channel_id}/cover-templates/items/{item.id}"
    response = client.post(
        f"{base}/cover",
        files={"file": ("alpha.png", png_bytes(400, 400, alpha=True), "image/png")},
    )
    assert response.status_code == 200
    preview = client.get(f"{base}/cover/media/preview")
    with Image.open(BytesIO(preview.content)) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 255
        assert image.getpixel((10, 10))[3] == 0


@pytest.mark.parametrize(
    ("filename", "payload", "code"),
    [
        ("cover.gif", b"GIF89a", "invalid_image_type"),
        ("cover.png", b"not an image", "invalid_image"),
        ("cover.png", b"", "invalid_image_size"),
    ],
)
def test_upload_rejects_invalid_payload(filename: str, payload: bytes, code: str) -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    item = repo.create_item(OWNER.id, channel_id, None, NOW)
    client = make_client(repo, OWNER)
    response = client.post(
        f"/identity/channels/{channel_id}/cover-templates/items/{item.id}/cover",
        files={"file": (filename, payload, "application/octet-stream")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code


def test_other_account_gets_404_everywhere() -> None:
    repo = FakeRepo()
    channel_id = repo.add_channel()
    item = repo.create_item(OWNER.id, channel_id, None, NOW)
    client = make_client(repo, OTHER)
    base = f"/identity/channels/{channel_id}/cover-templates"
    assert client.get(base).status_code == 404
    assert client.post(f"{base}/items", json={}).status_code == 404
    assert (
        client.patch(
            f"{base}/items/{item.id}",
            json={"name": "x", "prompt": "y"},
            headers={"Idempotency-Key": "k"},
        ).status_code
        == 404
    )
    assert client.delete(f"{base}/items/{item.id}").status_code == 404
    assert (
        client.put(f"{base}/items/order", json={"item_ids": [str(item.id)]}).status_code
        == 404
    )
    assert (
        client.post(
            f"{base}/items/{item.id}/cover",
            files={"file": ("cover.png", png_bytes(), "image/png")},
        ).status_code
        == 404
    )
    assert client.get(f"{base}/items/{item.id}/cover/media/preview").status_code == 404


def test_dup_name_and_default_sequence() -> None:
    assert default_template_name([]) == "模板 1"
    assert default_template_name(["模板 1", "模板 7", "手写"]) == "模板 8"
    assert default_template_name(["模板 2", "模板 2"]) == "模板 3"


def test_prepare_rejects_unknown_extension() -> None:
    with pytest.raises(DomainError) as error:
        prepare_cover_template(png_bytes(4, 4), filename="cover.bmp")
    assert error.value.code == "invalid_image_type"
