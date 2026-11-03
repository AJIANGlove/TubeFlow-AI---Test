"""频道模板的应用服务：归属校验、名称/长度限制、排序与上传压缩编排。

不依赖 Web 框架；仓储通过 Protocol 注入，便于单测替身。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from tubeflow_test.cover_templates.domain import (
    CoverTemplateCollection,
    CoverTemplateItem,
)
from tubeflow_test.cover_templates.images import PreparedCoverTemplate, prepare_cover_template
from tubeflow_test.cover_templates.repository import default_template_name
from tubeflow_test.domain.errors import DomainError

PROMPT_LIMIT = 20_000
NAME_LIMIT = 30
SAVE_KEY_LIMIT = 128


class CoverTemplateRepositoryPort(Protocol):
    def channel_exists(self, owner_id: UUID, channel_id: UUID) -> bool: ...

    def list_items(self, owner_id: UUID, channel_id: UUID) -> CoverTemplateCollection: ...

    def create_item(
        self, owner_id: UUID, channel_id: UUID, name: str | None, now: datetime
    ) -> CoverTemplateItem: ...

    def update_item(
        self,
        owner_id: UUID,
        channel_id: UUID,
        item_id: UUID,
        name: str,
        prompt: str,
        save_key: str,
        now: datetime,
    ) -> CoverTemplateItem: ...

    def delete_item(self, owner_id: UUID, channel_id: UUID, item_id: UUID) -> None: ...

    def reorder(
        self, owner_id: UUID, channel_id: UUID, item_ids: tuple[UUID, ...]
    ) -> CoverTemplateCollection: ...

    def replace_cover(
        self,
        owner_id: UUID,
        channel_id: UUID,
        item_id: UUID,
        prepared: PreparedCoverTemplate,
        now: datetime,
    ) -> CoverTemplateItem: ...

    def media(
        self, owner_id: UUID, channel_id: UUID, item_id: UUID, variant: str
    ) -> tuple[bytes, str, str]: ...


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _validate_prompt(value: str) -> str:
    if "\x00" in value:
        raise DomainError("invalid_parameter", "提示词模板不能包含空字符。")
    if len(value) > PROMPT_LIMIT:
        raise DomainError(
            "cover_template_prompt_too_long",
            f"提示词模板最多 {PROMPT_LIMIT} 个字符，当前 {len(value)} 个。",
        )
    return value


def _validate_name(value: str) -> str:
    if "\x00" in value:
        raise DomainError("invalid_parameter", "模板名称不能包含空字符。")
    if len(value) > NAME_LIMIT:
        raise DomainError(
            "cover_template_name_too_long",
            f"模板名称最多 {NAME_LIMIT} 个字符，当前 {len(value)} 个。",
        )
    return value


def _validate_save_key(value: str) -> str:
    key = value.strip()
    if not key or len(key) > SAVE_KEY_LIMIT:
        raise DomainError("invalid_parameter", "保存请求缺少有效的幂等键。")
    return key


class CoverTemplateService:
    def __init__(
        self,
        repository: CoverTemplateRepositoryPort,
        *,
        clock: Callable[[], datetime] = _default_clock,
        prepare: Callable[..., PreparedCoverTemplate] = prepare_cover_template,
    ) -> None:
        self._repo = repository
        self._clock = clock
        self._prepare = prepare

    def view(self, owner_id: UUID, channel_id: UUID) -> CoverTemplateCollection:
        self._require_channel(owner_id, channel_id)
        return self._repo.list_items(owner_id, channel_id)

    def create(
        self, owner_id: UUID, channel_id: UUID, name: str | None = None
    ) -> CoverTemplateItem:
        self._require_channel(owner_id, channel_id)
        cleaned = _validate_name(name.strip()) if name and name.strip() else None
        return self._repo.create_item(owner_id, channel_id, cleaned, self._clock())

    def update(
        self,
        owner_id: UUID,
        channel_id: UUID,
        item_id: UUID,
        *,
        name: str,
        prompt: str,
        save_key: str,
    ) -> CoverTemplateItem:
        self._require_channel(owner_id, channel_id)
        key = _validate_save_key(save_key)
        prompt_value = _validate_prompt(prompt)
        cleaned = _validate_name(name.strip())
        if not cleaned:
            existing = self._repo.list_items(owner_id, channel_id)
            cleaned = default_template_name(
                [item.name for item in existing.items if item.id != item_id]
            )
        return self._repo.update_item(
            owner_id, channel_id, item_id, cleaned, prompt_value, key, self._clock()
        )

    def delete(self, owner_id: UUID, channel_id: UUID, item_id: UUID) -> None:
        self._require_channel(owner_id, channel_id)
        self._repo.delete_item(owner_id, channel_id, item_id)

    def reorder(
        self, owner_id: UUID, channel_id: UUID, item_ids: tuple[UUID, ...]
    ) -> CoverTemplateCollection:
        self._require_channel(owner_id, channel_id)
        if not item_ids:
            raise DomainError("invalid_parameter", "排序列表不能为空。")
        return self._repo.reorder(owner_id, channel_id, item_ids)

    def upload_cover(
        self,
        owner_id: UUID,
        channel_id: UUID,
        item_id: UUID,
        *,
        filename: str,
        content: bytes,
    ) -> CoverTemplateItem:
        self._require_channel(owner_id, channel_id)
        prepared = self._prepare(content, filename=filename)
        return self._repo.replace_cover(
            owner_id, channel_id, item_id, prepared, self._clock()
        )

    def media(
        self, owner_id: UUID, channel_id: UUID, item_id: UUID, variant: str
    ) -> tuple[bytes, str, str]:
        if variant not in {"original", "preview"}:
            raise DomainError("not_found", "Cover template image was not found")
        self._require_channel(owner_id, channel_id)
        return self._repo.media(owner_id, channel_id, item_id, variant)

    def _require_channel(self, owner_id: UUID, channel_id: UUID) -> None:
        if not self._repo.channel_exists(owner_id, channel_id):
            raise DomainError("not_found", "Channel was not found")
