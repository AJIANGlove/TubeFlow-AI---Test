"""模板集合的数据形状（不依赖 SQLAlchemy / Web 框架）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CoverTemplateImageMeta:
    """一张已存储图片的元信息；字节按需单独读取，列表不载入 BLOB。"""

    width: int
    height: int
    byte_size: int
    sha256: str
    media_type: str
    image_format: str


@dataclass(frozen=True, slots=True)
class CoverTemplateItem:
    """一个模板 = 一张封面（可为空）+ 一段提示词 + 名称 + 顺序。"""

    id: UUID
    name: str
    position: int
    prompt: str
    cover: CoverTemplateImageMeta | None
    preview: CoverTemplateImageMeta | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CoverTemplateCollection:
    channel_id: UUID
    items: tuple[CoverTemplateItem, ...]

    @property
    def updated_at(self) -> datetime | None:
        if not self.items:
            return None
        return max(item.updated_at for item in self.items)


def empty_collection(channel_id: UUID) -> CoverTemplateCollection:
    return CoverTemplateCollection(channel_id, ())
