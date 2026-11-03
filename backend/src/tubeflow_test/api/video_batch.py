from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from tubeflow_test.application.video_batch import (
    GroupVideoBatchService,
    VideoBatchItemView,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account
from tubeflow_test.video_generation.api import _require_h3_available


class BatchGenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_ids: list[UUID]
    force: bool = False


def _overview_view(item) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "group_id": str(item.group_id),
        "group_sequence": item.sequence,
        "eligible": item.eligible,
        "reason": item.reason,
        "video_blocked": item.video_blocked,
        "has_current_video": item.has_current_video,
        "latest_attempt_stage": item.latest_attempt_stage,
        "latest_attempt_stalled": item.latest_attempt_stalled,
    }


def _item_view(item: VideoBatchItemView) -> dict[str, object]:
    return {
        "group_id": str(item.group_id),
        "group_sequence": item.sequence,
        "status": item.status,
        "attempt_id": str(item.attempt_id) if item.attempt_id is not None else None,
        "message": item.message,
    }


def _job_view(view) -> dict[str, object]:  # type: ignore[no-untyped-def]
    queued = sum(1 for item in view.items if item.status == "queued")
    pending = sum(1 for item in view.items if item.status == "PENDING")
    return {
        "job_id": str(view.job_id),
        "total": view.total,
        "queued": queued,
        "pending": pending,
        "completed": view.completed_at is not None,
        "items": [_item_view(item) for item in view.items],
    }


def create_video_batch_router(
    batch: GroupVideoBatchService,
    current_account: Callable[[], Account],
    kick: Callable[[UUID], None] | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/projects/{project_id}/group-video-batch", tags=["group-video-batch"]
    )

    def call[T](operation: Callable[[], T]) -> T:
        try:
            return operation()
        except DomainError as exc:
            raise HTTPException(
                404 if exc.code == "not_found" else 409,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    @router.get("/overview")
    def overview(
        project_id: UUID,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        items = call(lambda: batch.overview(account.id, project_id))
        return {"items": [_overview_view(item) for item in items]}

    @router.post("/generations", status_code=status.HTTP_202_ACCEPTED)
    def generate(
        project_id: UUID,
        body: BatchGenerateInput,
        background_tasks: BackgroundTasks,
        request: Request,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        _require_h3_available(request)
        view = call(
            lambda: batch.start(
                account.id, project_id, tuple(body.group_ids), force=body.force
            )
        )
        if kick is not None:
            background_tasks.add_task(kick, view.job_id)
        return _job_view(view)

    @router.get("/jobs/{job_id}")
    def job_status(
        job_id: UUID,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        view = call(lambda: batch.get_job(account.id, job_id))
        if view is None:
            raise HTTPException(
                404, detail={"code": "not_found", "message": "Batch job was not found"}
            )
        return _job_view(view)

    return router
