"""频道模板的 SQL 适配器：一频道多行，读写都在调用方的事务边界内。

列表与保存只取元信息列，不把原图 BLOB 拉进内存；字节只由 `media()` 按变体读取。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.models import ChannelRow
from tubeflow_test.cover_templates.domain import (
    CoverTemplateCollection,
    CoverTemplateImageMeta,
    CoverTemplateItem,
)
from tubeflow_test.cover_templates.images import PreparedCoverTemplate
from tubeflow_test.cover_templates.models import ChannelCoverTemplateItemRow
from tubeflow_test.domain.errors import DomainError

_DEFAULT_NAME = re.compile(r"^模板\s*(\d+)$")

_ITEM_COLUMNS = (
    ChannelCoverTemplateItemRow.id,
    ChannelCoverTemplateItemRow.channel_id,
    ChannelCoverTemplateItemRow.name,
    ChannelCoverTemplateItemRow.position,
    ChannelCoverTemplateItemRow.prompt,
    ChannelCoverTemplateItemRow.last_save_key,
    ChannelCoverTemplateItemRow.cover_original_format,
    ChannelCoverTemplateItemRow.cover_original_width,
    ChannelCoverTemplateItemRow.cover_original_height,
    ChannelCoverTemplateItemRow.cover_original_byte_size,
    ChannelCoverTemplateItemRow.cover_original_sha256,
    ChannelCoverTemplateItemRow.cover_original_media_type,
    ChannelCoverTemplateItemRow.cover_preview_format,
    ChannelCoverTemplateItemRow.cover_preview_width,
    ChannelCoverTemplateItemRow.cover_preview_height,
    ChannelCoverTemplateItemRow.cover_preview_byte_size,
    ChannelCoverTemplateItemRow.cover_preview_sha256,
    ChannelCoverTemplateItemRow.cover_preview_media_type,
    ChannelCoverTemplateItemRow.created_at,
    ChannelCoverTemplateItemRow.updated_at,
)


def _meta(
    *,
    media_type: str | None,
    image_format: str | None,
    width: int | None,
    height: int | None,
    byte_size: int | None,
    sha256: str | None,
) -> CoverTemplateImageMeta | None:
    if not sha256 or not media_type or not width or not height:
        return None
    return CoverTemplateImageMeta(
        width=width,
        height=height,
        byte_size=int(byte_size or 0),
        sha256=sha256,
        media_type=media_type,
        image_format=image_format or "UNKNOWN",
    )


def _item(values: Mapping[Any, Any]) -> CoverTemplateItem:
    return CoverTemplateItem(
        id=values[ChannelCoverTemplateItemRow.id],
        name=values[ChannelCoverTemplateItemRow.name],
        position=values[ChannelCoverTemplateItemRow.position],
        prompt=values[ChannelCoverTemplateItemRow.prompt],
        cover=_meta(
            media_type=values[ChannelCoverTemplateItemRow.cover_original_media_type],
            image_format=values[ChannelCoverTemplateItemRow.cover_original_format],
            width=values[ChannelCoverTemplateItemRow.cover_original_width],
            height=values[ChannelCoverTemplateItemRow.cover_original_height],
            byte_size=values[ChannelCoverTemplateItemRow.cover_original_byte_size],
            sha256=values[ChannelCoverTemplateItemRow.cover_original_sha256],
        ),
        preview=_meta(
            media_type=values[ChannelCoverTemplateItemRow.cover_preview_media_type],
            image_format=values[ChannelCoverTemplateItemRow.cover_preview_format],
            width=values[ChannelCoverTemplateItemRow.cover_preview_width],
            height=values[ChannelCoverTemplateItemRow.cover_preview_height],
            byte_size=values[ChannelCoverTemplateItemRow.cover_preview_byte_size],
            sha256=values[ChannelCoverTemplateItemRow.cover_preview_sha256],
        ),
        created_at=values[ChannelCoverTemplateItemRow.created_at],
        updated_at=values[ChannelCoverTemplateItemRow.updated_at],
    )


def default_template_name(names: list[str]) -> str:
    """自动编号：已有「模板 N」里取最大 N + 1；删除后不重排，避免改名。"""
    highest = 0
    for name in names:
        match = _DEFAULT_NAME.match(name.strip())
        if match:
            highest = max(highest, int(match.group(1)))
    return f"模板 {highest + 1}"


class SqlCoverTemplateRepository:
    def __init__(self, unit_of_work: Callable[[], AbstractContextManager[Session]]) -> None:
        self._uow = unit_of_work

    def channel_exists(self, owner_id: UUID, channel_id: UUID) -> bool:
        with self._uow() as session:
            return self._owns_channel(session, owner_id, channel_id)

    def list_items(self, owner_id: UUID, channel_id: UUID) -> CoverTemplateCollection:
        with self._uow() as session:
            if not self._owns_channel(session, owner_id, channel_id):
                raise DomainError("not_found", "Channel was not found")
            rows = session.execute(
                select(*_ITEM_COLUMNS)
                .where(ChannelCoverTemplateItemRow.channel_id == channel_id)
                .order_by(
                    ChannelCoverTemplateItemRow.position,
                    ChannelCoverTemplateItemRow.created_at,
                )
            ).all()
            return CoverTemplateCollection(
                channel_id, tuple(_item(row._mapping) for row in rows)
            )

    def create_item(
        self, owner_id: UUID, channel_id: UUID, name: str | None, now: datetime
    ) -> CoverTemplateItem:
        try:
            with self._uow() as session:
                if not self._owns_channel(session, owner_id, channel_id):
                    raise DomainError("not_found", "Channel was not found")
                rows = session.execute(
                    select(
                        ChannelCoverTemplateItemRow.name,
                        ChannelCoverTemplateItemRow.position,
                    )
                    .where(ChannelCoverTemplateItemRow.channel_id == channel_id)
                    .with_for_update()
                ).all()
                item_id = uuid4()
                session.add(
                    ChannelCoverTemplateItemRow(
                        id=item_id,
                        channel_id=channel_id,
                        name=name or default_template_name([str(row.name) for row in rows]),
                        position=max((int(row.position) for row in rows), default=-1) + 1,
                        prompt="",
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
        except IntegrityError:
            # 并发写入撞主键（uuid4 概率极低）：再取一次最大位置重试。
            with self._uow() as session:
                if not self._owns_channel(session, owner_id, channel_id):
                    raise DomainError("not_found", "Channel was not found") from None
                rows = session.execute(
                    select(
                        ChannelCoverTemplateItemRow.name,
                        ChannelCoverTemplateItemRow.position,
                    ).where(ChannelCoverTemplateItemRow.channel_id == channel_id)
                ).all()
                item_id = uuid4()
                session.add(
                    ChannelCoverTemplateItemRow(
                        id=item_id,
                        channel_id=channel_id,
                        name=name or default_template_name([str(row.name) for row in rows]),
                        position=max((int(row.position) for row in rows), default=-1) + 1,
                        prompt="",
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
        return self._load_item(owner_id, channel_id, item_id)

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
        with self._uow() as session:
            row = self._locked_item(session, owner_id, channel_id, item_id)
            if row is None:
                raise DomainError("not_found", "Cover template was not found")
            if row[ChannelCoverTemplateItemRow.last_save_key] == save_key:
                return _item(row)
            session.execute(
                update(ChannelCoverTemplateItemRow)
                .where(ChannelCoverTemplateItemRow.id == item_id)
                .values(name=name, prompt=prompt, last_save_key=save_key, updated_at=now)
            )
        return self._load_item(owner_id, channel_id, item_id)

    def delete_item(self, owner_id: UUID, channel_id: UUID, item_id: UUID) -> None:
        with self._uow() as session:
            row = self._locked_item(session, owner_id, channel_id, item_id)
            if row is None:
                raise DomainError("not_found", "Cover template was not found")
            session.execute(
                delete(ChannelCoverTemplateItemRow).where(
                    ChannelCoverTemplateItemRow.id == item_id
                )
            )

    def reorder(
        self, owner_id: UUID, channel_id: UUID, item_ids: tuple[UUID, ...]
    ) -> CoverTemplateCollection:
        with self._uow() as session:
            if not self._owns_channel(session, owner_id, channel_id):
                raise DomainError("not_found", "Channel was not found")
            existing = tuple(
                session.scalars(
                    select(ChannelCoverTemplateItemRow.id)
                    .where(ChannelCoverTemplateItemRow.channel_id == channel_id)
                    .with_for_update()
                )
            )
            if len(set(item_ids)) != len(item_ids) or set(item_ids) != set(existing):
                raise DomainError(
                    "cover_template_order_mismatch",
                    "模板列表已变化，请刷新后重试。",
                )
            for position, item_id in enumerate(item_ids):
                session.execute(
                    update(ChannelCoverTemplateItemRow)
                    .where(ChannelCoverTemplateItemRow.id == item_id)
                    .values(position=position)
                )
        return self.list_items(owner_id, channel_id)

    def replace_cover(
        self,
        owner_id: UUID,
        channel_id: UUID,
        item_id: UUID,
        prepared: PreparedCoverTemplate,
        now: datetime,
    ) -> CoverTemplateItem:
        original, preview = prepared.original, prepared.preview
        with self._uow() as session:
            row = self._locked_item(session, owner_id, channel_id, item_id)
            if row is None:
                raise DomainError("not_found", "Cover template was not found")
            session.execute(
                update(ChannelCoverTemplateItemRow)
                .where(ChannelCoverTemplateItemRow.id == item_id)
                .values(
                    cover_original=original.data,
                    cover_original_media_type=original.media_type,
                    cover_original_format=original.image_format,
                    cover_original_width=original.width,
                    cover_original_height=original.height,
                    cover_original_byte_size=original.byte_size,
                    cover_original_sha256=original.sha256,
                    cover_preview=preview.data,
                    cover_preview_media_type=preview.media_type,
                    cover_preview_format=preview.image_format,
                    cover_preview_width=preview.width,
                    cover_preview_height=preview.height,
                    cover_preview_byte_size=preview.byte_size,
                    cover_preview_sha256=preview.sha256,
                    updated_at=now,
                )
            )
        return self._load_item(owner_id, channel_id, item_id)

    def media(
        self, owner_id: UUID, channel_id: UUID, item_id: UUID, variant: str
    ) -> tuple[bytes, str, str]:
        columns = (
            (
                ChannelCoverTemplateItemRow.cover_original,
                ChannelCoverTemplateItemRow.cover_original_media_type,
                ChannelCoverTemplateItemRow.cover_original_sha256,
            )
            if variant == "original"
            else (
                ChannelCoverTemplateItemRow.cover_preview,
                ChannelCoverTemplateItemRow.cover_preview_media_type,
                ChannelCoverTemplateItemRow.cover_preview_sha256,
            )
        )
        with self._uow() as session:
            row = session.execute(
                select(*columns)
                .join(ChannelRow, ChannelRow.id == ChannelCoverTemplateItemRow.channel_id)
                .where(
                    ChannelCoverTemplateItemRow.id == item_id,
                    ChannelCoverTemplateItemRow.channel_id == channel_id,
                    ChannelRow.owner_id == owner_id,
                )
            ).first()
            if row is None:
                raise DomainError("not_found", "Cover template was not found")
            data, media_type, sha256 = row
            if data is None or not media_type or not sha256:
                raise DomainError("not_found", "Cover template image was not found")
            return bytes(data), media_type, sha256

    def _load_item(
        self, owner_id: UUID, channel_id: UUID, item_id: UUID
    ) -> CoverTemplateItem:
        with self._uow() as session:
            row = session.execute(
                select(*_ITEM_COLUMNS)
                .join(ChannelRow, ChannelRow.id == ChannelCoverTemplateItemRow.channel_id)
                .where(
                    ChannelCoverTemplateItemRow.id == item_id,
                    ChannelCoverTemplateItemRow.channel_id == channel_id,
                    ChannelRow.owner_id == owner_id,
                )
            ).first()
            if row is None:
                raise DomainError("not_found", "Cover template was not found")
            return _item(row._mapping)

    @staticmethod
    def _owns_channel(session: Session, owner_id: UUID, channel_id: UUID) -> bool:
        return (
            session.scalar(
                select(ChannelRow.id).where(
                    ChannelRow.id == channel_id,
                    ChannelRow.owner_id == owner_id,
                )
            )
            is not None
        )

    @staticmethod
    def _locked_item(
        session: Session, owner_id: UUID, channel_id: UUID, item_id: UUID
    ) -> Mapping[Any, Any] | None:
        row = session.execute(
            select(*_ITEM_COLUMNS)
            .join(ChannelRow, ChannelRow.id == ChannelCoverTemplateItemRow.channel_id)
            .where(
                ChannelCoverTemplateItemRow.id == item_id,
                ChannelCoverTemplateItemRow.channel_id == channel_id,
                ChannelRow.owner_id == owner_id,
            )
            .with_for_update(of=ChannelCoverTemplateItemRow)
        ).first()
        return None if row is None else row._mapping
