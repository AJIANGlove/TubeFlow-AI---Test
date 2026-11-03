"""频道模板表（临时功能）。

- `ChannelCoverTemplateRow`（2026-10-21 旧结构）：一频道一行、三列文本，**已冻结**，
  仅用于回滚留档与频道删除；新代码不再读写。
- `ChannelCoverTemplateItemRow`（2026-10-27 新结构）：一频道多行，每行
  「一张封面 + 一段提示词 + 名称 + 顺序」，支持增删与拖动排序。

两张表都以 `channels.id` 为 RESTRICT 外键，频道删除在同一事务内显式清理。
图片（原图 + 压缩图）直接存 BYTEA，不落对象存储：删行即删干净，避免孤儿对象。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tubeflow_test.adapters.persistence.identity_project.models import Base


class ChannelCoverTemplateRow(Base):
    """旧结构（冻结）：只供回滚与频道删除引用，不再被 API 读取。"""

    __tablename__ = "channel_cover_templates"

    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("channels.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    cover_original: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cover_original_media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cover_original_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cover_original_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_original_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_original_byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cover_original_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cover_preview: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cover_preview_media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cover_preview_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cover_preview_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_preview_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_preview_byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cover_preview_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_a: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    prompt_b: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    prompt_c: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChannelCoverTemplateItemRow(Base):
    """新结构：一个模板 = 一张封面 + 一段提示词，可增删、可排序。"""

    __tablename__ = "channel_cover_template_items"
    __table_args__ = (
        Index(
            "ix_channel_cover_template_items_channel_position",
            "channel_id",
            "position",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    channel_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("channels.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(30), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # 原图：≤10MB，点击放大与「复制图片」使用这一版。
    cover_original: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cover_original_media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cover_original_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cover_original_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_original_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_original_byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cover_original_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 压缩图：长边 1280 的 WebP，仅用于卡片缩略图。
    cover_preview: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cover_preview_media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cover_preview_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cover_preview_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_preview_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cover_preview_byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cover_preview_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 手动保存的幂等键：同键重试直接返回当前行，不重复覆盖。
    last_save_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
