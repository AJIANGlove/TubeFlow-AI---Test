# ruff: noqa: B008
"""频道模板 HTTP 边界（临时功能，2026-10-27 多模板版）。

路由前缀沿用 identity 频道作用域；所有操作按频道归属校验，非本人一律 404。
模板 = 封面图 + 一段提示词；文本修改必须显式保存（PATCH，带幂等键），
图片上传即时生效。
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict

from tubeflow_test.cover_templates.domain import (
    CoverTemplateCollection,
    CoverTemplateItem,
)
from tubeflow_test.cover_templates.images import MAX_UPLOAD_BYTES
from tubeflow_test.cover_templates.service import CoverTemplateService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.http_range import RangeNotSatisfiable, parse_single_range
from tubeflow_test.identity_project.domain import Account


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateItemInput(StrictModel):
    name: str | None = None


class SaveItemInput(StrictModel):
    name: str = ""
    prompt: str = ""


class ReorderInput(StrictModel):
    item_ids: list[UUID]


def _cover_view(item: CoverTemplateItem, channel_id: UUID) -> dict[str, object] | None:
    cover = item.cover
    if cover is None:
        return None
    preview = item.preview
    base = f"/identity/channels/{channel_id}/cover-templates/items/{item.id}/cover/media"
    return {
        "width": cover.width,
        "height": cover.height,
        "byte_size": cover.byte_size,
        "image_format": cover.image_format,
        "sha256": cover.sha256,
        "media_url_original": f"{base}/original",
        "media_url_preview": f"{base}/preview",
        "preview": (
            {
                "width": preview.width,
                "height": preview.height,
                "byte_size": preview.byte_size,
                "image_format": preview.image_format,
            }
            if preview is not None
            else None
        ),
    }


def _item_view(item: CoverTemplateItem, channel_id: UUID) -> dict[str, object]:
    return {
        "item_id": item.id,
        "name": item.name,
        "position": item.position,
        "prompt": item.prompt,
        "cover": _cover_view(item, channel_id),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _view(collection: CoverTemplateCollection) -> dict[str, object]:
    return {
        "channel_id": collection.channel_id,
        "items": [_item_view(item, collection.channel_id) for item in collection.items],
        "updated_at": collection.updated_at,
    }


def _immutable_media_response(
    content: bytes, media_type: str, digest: str, request: Request
) -> Response:
    """与 identity/cover 媒体同口径：私有缓存 + ETag + Range。"""
    size = len(content)
    etag = f'"{digest}"'
    cache = "private, no-cache"
    try:
        selected = parse_single_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": cache,
                "Content-Range": f"bytes */{size}",
                "ETag": etag,
            },
        )
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"Accept-Ranges": "bytes", "Cache-Control": cache, "ETag": etag},
        )
    payload = content
    response_status = status.HTTP_200_OK
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache,
        "Content-Length": str(size),
        "ETag": etag,
    }
    if selected is not None:
        payload = content[selected.start : selected.end + 1]
        response_status = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Length"] = str(selected.length)
        headers["Content-Range"] = f"bytes {selected.start}-{selected.end}/{size}"
    return Response(
        b"" if request.method == "HEAD" else payload,
        status_code=response_status,
        media_type=media_type,
        headers=headers,
    )


def create_cover_templates_router(
    service: CoverTemplateService,
    current_account: Callable[[], Account],
    *,
    media_account: Callable[[], Account] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["cover-templates"])
    media_account = media_account or current_account

    def call[T](operation: Callable[[], T]) -> T:
        try:
            return operation()
        except DomainError as exc:
            mapped = (
                404
                if exc.code == "not_found"
                else 409
                if exc.code in {"cover_template_order_mismatch", "revision_conflict"}
                else 422
            )
            raise HTTPException(mapped, detail={"code": exc.code, "message": exc.message}) from exc

    @router.get("/identity/channels/{channel_id}/cover-templates")
    def read_templates(
        channel_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        return _view(call(lambda: service.view(account.id, channel_id)))

    @router.post(
        "/identity/channels/{channel_id}/cover-templates/items", status_code=201
    )
    def create_item(
        channel_id: UUID,
        body: CreateItemInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        item = call(lambda: service.create(account.id, channel_id, body.name))
        return _item_view(item, channel_id)

    @router.patch("/identity/channels/{channel_id}/cover-templates/items/{item_id}")
    def save_item(
        channel_id: UUID,
        item_id: UUID,
        body: SaveItemInput,
        account: Account = Depends(current_account),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        item = call(
            lambda: service.update(
                account.id,
                channel_id,
                item_id,
                name=body.name,
                prompt=body.prompt,
                save_key=idempotency_key or "",
            )
        )
        return _item_view(item, channel_id)

    @router.delete("/identity/channels/{channel_id}/cover-templates/items/{item_id}")
    def delete_item(
        channel_id: UUID,
        item_id: UUID,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        call(lambda: service.delete(account.id, channel_id, item_id))
        return {"item_id": item_id, "deleted": True}

    @router.put("/identity/channels/{channel_id}/cover-templates/items/order")
    def reorder_items(
        channel_id: UUID,
        body: ReorderInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        return _view(
            call(lambda: service.reorder(account.id, channel_id, tuple(body.item_ids)))
        )

    @router.post(
        "/identity/channels/{channel_id}/cover-templates/items/{item_id}/cover"
    )
    async def upload_cover(
        channel_id: UUID,
        item_id: UUID,
        file: UploadFile = File(...),
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        item = call(
            lambda: service.upload_cover(
                account.id,
                channel_id,
                item_id,
                filename=file.filename or "",
                content=content,
            )
        )
        return _item_view(item, channel_id)

    @router.head(
        "/identity/channels/{channel_id}/cover-templates/items/{item_id}/cover/media/{variant}",
        include_in_schema=False,
    )
    @router.get(
        "/identity/channels/{channel_id}/cover-templates/items/{item_id}/cover/media/{variant}"
    )
    def cover_media(
        channel_id: UUID,
        item_id: UUID,
        variant: str,
        request: Request,
        account: Account = Depends(media_account),
    ) -> Response:
        content, media_type, digest = call(
            lambda: service.media(account.id, channel_id, item_id, variant)
        )
        return _immutable_media_response(content, media_type, digest, request)

    return router
