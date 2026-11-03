"""ElevenLabs 号池公开管理 API（局域网测试阶段无认证，USER_DECISIONS §23.6）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Never
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.ports import PoolKeyEntry, PoolKeyEvent
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService


class AddKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=200)


def create_router(service: ElevenlabsKeyPoolService) -> APIRouter:
    router = APIRouter(prefix="/elevenlabs-pool", tags=["elevenlabs-pool"])

    @router.get("/keys")
    def list_keys() -> dict[str, object]:
        return {"keys": [_key_json(entry) for entry in service.list_keys()]}

    @router.post("/keys", status_code=status.HTTP_201_CREATED)
    async def add_key(data: AddKeyRequest) -> dict[str, object]:
        entry = await _call_async(service.add_key(api_key=data.api_key, label=data.label))
        return {"key": _key_json(entry)}

    @router.post("/keys/{key_id}/enable")
    async def enable(key_id: UUID) -> dict[str, object]:
        return {"key": _key_json(await _call_async(service.enable(key_id)))}

    @router.post("/keys/{key_id}/disable")
    def disable(key_id: UUID) -> dict[str, object]:
        return {"key": _key_json(_call(lambda: service.disable(key_id)))}

    @router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete(key_id: UUID) -> None:
        _call(lambda: service.delete(key_id))

    @router.post("/keys/{key_id}/refresh-quota")
    async def refresh_quota(key_id: UUID) -> dict[str, object]:
        return {"key": _key_json(await _call_async(service.refresh_quota(key_id)))}

    @router.get("/events")
    def events(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> dict[str, object]:
        return {"events": [_event_json(item) for item in service.list_events(limit=limit)]}

    return router


def _key_json(entry: PoolKeyEntry) -> dict[str, object]:
    quota: dict[str, object] | None = None
    if entry.quota_refreshed_at is not None:
        quota = {
            "tier": entry.quota_tier,
            "character_count": entry.character_count,
            "character_limit": entry.character_limit,
            "voice_slots_used": entry.voice_slots_used,
            "voice_limit": entry.voice_limit,
            "refreshed_at": entry.quota_refreshed_at.isoformat(),
        }
    return {
        "id": str(entry.id),
        "label": entry.label,
        "api_key": entry.api_key,
        "status": entry.status,
        "added_at": entry.added_at.isoformat(),
        "status_at": entry.status_at.isoformat(),
        "last_error": entry.last_error,
        "quota": quota,
    }


def _event_json(item: PoolKeyEvent) -> dict[str, object]:
    return {
        "id": item.id,
        "key_id": str(item.key_id),
        "event": item.event,
        "detail": item.detail,
        "created_at": item.created_at.isoformat(),
    }


def _call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except DomainError as error:
        _raise_http(error)


async def _call_async[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except DomainError as error:
        _raise_http(error)


def _raise_http(error: DomainError) -> Never:
    code = (
        status.HTTP_409_CONFLICT
        if error.code == "elevenlabs_key_duplicate"
        else status.HTTP_404_NOT_FOUND
        if error.code == "not_found"
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    raise HTTPException(code, detail={"code": error.code, "message": error.message}) from error
