# ruff: noqa: B008
"""STEP 11 简介 HTTP 边界：频道模板 CRUD 与项目简介生成/版本管理。"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account
from tubeflow_test.video_descriptions.domain import (
    DescriptionAttempt,
    DescriptionPageState,
    DescriptionTemplate,
    ProjectDescription,
)
from tubeflow_test.video_descriptions.service import VideoDescriptionService


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateInput(StrictModel):
    prompt: str


class GenerateInput(StrictModel):
    template_id: UUID


class SelectDescriptionInput(StrictModel):
    version_id: UUID


class EditDescriptionInput(StrictModel):
    content: str


def _template_view(value: DescriptionTemplate) -> dict[str, object]:
    return {
        "id": str(value.id),
        "channel_id": str(value.channel_id),
        "version": value.version,
        "label": f"版本 {value.version}",
        "prompt": value.prompt,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _attempt_view(value: DescriptionAttempt | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "id": str(value.id),
        "status": value.status,
        "template_id": str(value.template_id),
        "template_version": value.template_version,
        "input_sha256": value.input_sha256,
        "error_code": value.error_code,
        "description_id": str(value.description_id) if value.description_id else None,
        "created_at": value.created_at,
        "finished_at": value.finished_at,
    }


def _description_view(value: ProjectDescription) -> dict[str, object]:
    return {
        "id": str(value.id),
        "project_id": str(value.project_id),
        "version_number": value.version_number,
        "source": value.source,
        "base_version_id": str(value.base_version_id) if value.base_version_id else None,
        "prompt_template_version": value.prompt_template_version,
        "prompt_template_sha256": value.prompt_template_sha256,
        "title_used": value.title_used,
        "channel_name_used": value.channel_name_used,
        "locale": value.locale,
        "script_sha256": value.script_sha256,
        "srt_asset_id": str(value.srt_asset_id) if value.srt_asset_id else None,
        "srt_sha256": value.srt_sha256,
        "payload": value.payload,
        "content": value.content,
        "disclaimer_key": value.disclaimer_key,
        "model": value.model,
        "provider_receipt": value.provider_receipt,
        "raw_response_sha256": value.raw_response_sha256,
        "provider_calls": value.provider_calls,
        "elapsed_ms": value.elapsed_ms,
        "current": value.current,
        "created_at": value.created_at,
    }


def _state_view(value: DescriptionPageState) -> dict[str, object]:
    return {
        "project_id": str(value.project_id),
        "channel_id": str(value.channel_id),
        "channel_name": value.channel_name,
        "available": value.available,
        "blocking_reason": value.blocking_reason,
        "timeline_entries": value.timeline_entries,
        "duration_ms": value.duration_ms,
        "current": _description_view(value.current) if value.current is not None else None,
        "versions": [_description_view(item) for item in value.versions],
        "generation": _attempt_view(value.active_attempt),
    }


def create_video_description_router(
    service: VideoDescriptionService,
    current_account: Callable[[], Account],
) -> APIRouter:
    router = APIRouter(tags=["video-descriptions"])

    def call[T](operation: Callable[[], T]) -> T:
        try:
            return operation()
        except DomainError as exc:
            mapped = _status_for(exc.code)
            raise HTTPException(
                mapped, detail={"code": exc.code, "message": exc.message}
            ) from exc

    @router.get("/identity/channels/{channel_id}/description-templates")
    def list_templates(
        channel_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        items = call(lambda: service.list_templates(account.id, channel_id))
        return {"channel_id": str(channel_id), "items": [_template_view(x) for x in items]}

    @router.post("/identity/channels/{channel_id}/description-templates", status_code=201)
    def create_template(
        channel_id: UUID,
        body: TemplateInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        items = call(lambda: service.create_template(account.id, channel_id, body.prompt))
        return {"channel_id": str(channel_id), "items": [_template_view(x) for x in items]}

    @router.put("/identity/channels/{channel_id}/description-templates/{template_id}")
    def update_template(
        channel_id: UUID,
        template_id: UUID,
        body: TemplateInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        items = call(
            lambda: service.update_template(account.id, channel_id, template_id, body.prompt)
        )
        return {"channel_id": str(channel_id), "items": [_template_view(x) for x in items]}

    @router.delete("/identity/channels/{channel_id}/description-templates/{template_id}")
    def delete_template(
        channel_id: UUID,
        template_id: UUID,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        items = call(lambda: service.delete_template(account.id, channel_id, template_id))
        return {"channel_id": str(channel_id), "items": [_template_view(x) for x in items]}

    @router.get("/projects/{project_id}/description")
    def read_description(
        project_id: UUID, account: Account = Depends(current_account)
    ) -> dict[str, object]:
        return _state_view(call(lambda: service.view(account.id, project_id)))

    @router.post("/projects/{project_id}/description/generations", status_code=201)
    def generate_description(
        project_id: UUID,
        body: GenerateInput,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        created = call(
            lambda: service.generate(
                account.id, project_id, body.template_id, idempotency_key or ""
            )
        )
        response.status_code = status.HTTP_201_CREATED
        return _state_view(call(lambda: service.view(account.id, project_id))) | {
            "created": _description_view(created)
        }

    @router.post("/projects/{project_id}/description/current")
    def select_current(
        project_id: UUID,
        body: SelectDescriptionInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        call(lambda: service.select_current(account.id, project_id, body.version_id))
        return _state_view(call(lambda: service.view(account.id, project_id)))

    @router.post("/projects/{project_id}/description/versions/{version_id}/edits", status_code=201)
    def edit_description(
        project_id: UUID,
        version_id: UUID,
        body: EditDescriptionInput,
        account: Account = Depends(current_account),
    ) -> dict[str, object]:
        created = call(
            lambda: service.edit_version(account.id, project_id, version_id, body.content)
        )
        return _state_view(call(lambda: service.view(account.id, project_id))) | {
            "created": _description_view(created)
        }

    return router


def _status_for(code: str) -> int:
    if code == "not_found":
        return status.HTTP_404_NOT_FOUND
    if code in {
        "project_purging",
        "description_generation_in_progress",
        "description_composition_required",
    }:
        return status.HTTP_409_CONFLICT
    if code in {
        "description_provider_not_configured",
        "description_provider_timeout",
        "description_provider_network",
        "description_provider_unavailable",
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if code in {"description_provider_rejected", "description_output_invalid"}:
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_422_UNPROCESSABLE_CONTENT
