# ruff: noqa: B008
from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.types import Send

from tubeflow_test.api.media_content import (
    MinioMediaStream,
    SqlCurrentVideoAuthority,
)
from tubeflow_test.api.public_status import deletion_public_state, public_state
from tubeflow_test.application.composition import CompositionVersion, ManualCompositionService
from tubeflow_test.application.deletion import ProjectDeletionService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.http_range import RangeNotSatisfiable, parse_single_range


@dataclass(frozen=True, slots=True)
class AuthenticatedOwner:
    id: UUID


def decode_project_title_confirmation(*, plain: str | None, encoded: str | None) -> str:
    """Resolve the deletion confirmation from either header form.

    Browsers reject HTTP header values outside ISO-8859-1, so any title with
    Chinese/Korean/emoji characters must travel base64 encoded. The plain header
    stays supported for ASCII titles sent by older clients and scripts.
    """
    if encoded is not None:
        try:
            return base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, UnicodeEncodeError, ValueError):
            raise DomainError(
                "deletion_confirmation_required",
                "exact project title confirmation is required",
            ) from None
    if plain is not None:
        return plain
    raise DomainError(
        "deletion_confirmation_required",
        "exact project title confirmation is required",
    )


class DeletionStatusReader(Protocol):
    def receipt(self, project_id: UUID, owner_id: UUID) -> dict[str, object]: ...

    def status(
        self, project_id: UUID, owner_id: UUID, receipt: str | None
    ) -> dict[str, object]: ...

    def retry(
        self, project_id: UUID, owner_id: UUID, receipt: str | None
    ) -> dict[str, object]: ...


class CompositionCommands(Protocol):
    async def start(self, project_id: UUID, owner_id: UUID) -> dict[str, object]: ...

    async def retry(self, project_id: UUID, owner_id: UUID) -> dict[str, object]: ...


class SelectCompositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID


class CleanupStreamingResponse(StreamingResponse):
    def __init__(self, content: Any, *, cleanup: Any, **kwargs: Any) -> None:
        self._cleanup = cleanup
        super().__init__(content, **kwargs)

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            self._cleanup()


def _composition_view(
    value: CompositionVersion, *, is_current: bool | None = None
) -> dict[str, object]:
    current = value.current if is_current is None else is_current
    success_actions = ("VIEW_MEDIA",) if current else ("VIEW_MEDIA", "SELECT_CURRENT")
    return {
        **asdict(value),
        "version_id": value.id,
        "current": current,
        **public_state("SUCCEEDED", success_actions=success_actions).as_dict(),
    }


def create_media_router(
    *,
    current_owner: Callable[[], AuthenticatedOwner],
    compositions: ManualCompositionService,
    composition_commands: CompositionCommands,
    deletions: ProjectDeletionService,
    deletion_status: DeletionStatusReader,
    export_factory: Callable[[UUID, UUID], Iterator[bytes]],
    content_authority: SqlCurrentVideoAuthority,
    content_stream: MinioMediaStream,
    media_owner: Callable[[], AuthenticatedOwner] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/projects", tags=["media-production"])
    media_owner = media_owner or current_owner

    @router.head("/{project_id}/media/{asset_id}/content", include_in_schema=False)
    @router.get("/{project_id}/media/{asset_id}/content")
    def media_content(
        project_id: UUID,
        asset_id: UUID,
        request: Request,
        owner: AuthenticatedOwner = Depends(media_owner),
    ) -> Response:
        content = content_authority.resolve(project_id, asset_id, owner.id)
        try:
            selected = parse_single_range(request.headers.get("range"), content.size_bytes)
        except RangeNotSatisfiable:
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{content.size_bytes}",
                },
            )
        content_stream.validate(content)
        etag = f'"{content.sha256}"'
        if request.headers.get("if-none-match") == etag:
            content_authority.revalidate(project_id, asset_id, owner.id, content)
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"Accept-Ranges": "bytes", "ETag": etag},
            )
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(selected.length if selected else content.size_bytes),
            "ETag": etag,
        }
        response_status = status.HTTP_200_OK
        if selected is not None:
            response_status = status.HTTP_206_PARTIAL_CONTENT
            headers["Content-Range"] = f"bytes {selected.start}-{selected.end}/{content.size_bytes}"
        if request.method == "HEAD":
            content_authority.revalidate(project_id, asset_id, owner.id, content)
            return Response(
                status_code=response_status,
                media_type=content.media_type,
                headers=headers,
            )
        acquired = content_stream.acquire(content, selected)
        try:
            content_authority.revalidate(project_id, asset_id, owner.id, content)
        except BaseException as error:
            acquired.abort(error)
        return StreamingResponse(
            acquired,
            status_code=response_status,
            media_type=content.media_type,
            headers=headers,
        )

    @router.get("/{project_id}/composition/current")
    @router.get("/{project_id}/preview")
    def current_composition(
        project_id: UUID, owner: AuthenticatedOwner = Depends(current_owner)
    ) -> dict[str, object]:
        return _composition_view(compositions.current(project_id, owner.id), is_current=True)

    @router.get("/{project_id}/composition/history")
    def composition_history(
        project_id: UUID, owner: AuthenticatedOwner = Depends(current_owner)
    ) -> dict[str, object]:
        return {
            "items": [
                _composition_view(item) for item in compositions.history(project_id, owner.id)
            ]
        }

    @router.get("/{project_id}/composition/attempts")
    def composition_attempts(
        project_id: UUID, owner: AuthenticatedOwner = Depends(current_owner)
    ) -> dict[str, object]:
        attempts = compositions.attempts(project_id, owner.id)
        return {
            "items": [
                {
                    **asdict(item),
                    **public_state(
                        item.state,
                        failure_actions=("RETRY_COMPOSITION",)
                        if index == 0 and item.state == "FAILED"
                        else (),
                    ).as_dict(),
                }
                for index, item in enumerate(attempts)
            ]
        }

    @router.post("/{project_id}/composition/current")
    def select_current_composition(
        project_id: UUID,
        body: SelectCompositionInput,
        owner: AuthenticatedOwner = Depends(current_owner),
    ) -> dict[str, object]:
        return _composition_view(
            compositions.select_current(project_id, owner.id, body.version_id),
            is_current=True,
        )

    @router.post("/{project_id}/composition", status_code=status.HTTP_202_ACCEPTED)
    async def compose(
        project_id: UUID, owner: AuthenticatedOwner = Depends(current_owner)
    ) -> dict[str, object]:
        result = await composition_commands.start(project_id, owner.id)
        return {
            **result,
            **public_state(
                result.get("status"), failure_actions=("RETRY_COMPOSITION",)
            ).as_dict(),
        }

    @router.post("/{project_id}/composition/retry", status_code=status.HTTP_202_ACCEPTED)
    async def retry_composition(
        project_id: UUID, owner: AuthenticatedOwner = Depends(current_owner)
    ) -> dict[str, object]:
        result = await composition_commands.retry(project_id, owner.id)
        return {
            **result,
            **public_state(
                result.get("status"), failure_actions=("RETRY_COMPOSITION",)
            ).as_dict(),
        }

    @router.get("/{project_id}/export")
    def export_project(
        project_id: UUID, owner: AuthenticatedOwner = Depends(current_owner)
    ) -> StreamingResponse:
        stream = export_factory(project_id, owner.id)
        return CleanupStreamingResponse(
            stream,
            cleanup=lambda: getattr(stream, "close", lambda: None)(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="composition.zip"'},
        )

    @router.delete("/{project_id}", status_code=status.HTTP_202_ACCEPTED)
    def delete_project(
        project_id: UUID,
        owner: AuthenticatedOwner = Depends(current_owner),
        x_confirm_project_title: str | None = Header(None),
        x_confirm_project_title_b64: str | None = Header(None, max_length=4096),
    ) -> dict[str, object]:
        confirmation = decode_project_title_confirmation(
            plain=x_confirm_project_title, encoded=x_confirm_project_title_b64
        )
        deletions.request(project_id, owner.id, confirmation=confirmation)
        return {
            "project_id": project_id,
            "status": "DELETING",
            **deletion_status.receipt(project_id, owner.id),
            **deletion_public_state("DELETING").as_dict(),
        }

    @router.get("/{project_id}/deletion")
    def get_deletion_status(
        project_id: UUID,
        owner: AuthenticatedOwner = Depends(current_owner),
        x_deletion_receipt: str | None = Header(None, max_length=2048),
    ) -> dict[str, object]:
        return deletion_status.status(project_id, owner.id, x_deletion_receipt)

    @router.post("/{project_id}/deletion/retry", status_code=status.HTTP_202_ACCEPTED)
    def retry_deletion(
        project_id: UUID,
        owner: AuthenticatedOwner = Depends(current_owner),
        x_deletion_receipt: str | None = Header(None, max_length=2048),
    ) -> dict[str, object]:
        receipt = deletion_status.retry(project_id, owner.id, x_deletion_receipt)
        return {
            "project_id": project_id,
            "status": "DELETING",
            **receipt,
            **deletion_public_state("DELETING").as_dict(),
        }

    return router
