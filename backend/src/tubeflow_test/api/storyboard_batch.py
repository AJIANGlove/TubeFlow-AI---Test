from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from tubeflow_test.application.storyboard_batch import (
    StoryboardBatchGenerateResult,
    StoryboardBatchGroupOverview,
    StoryboardBatchSeedResult,
    StoryboardBatchService,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchGenerateInput(StrictModel):
    group_ids: list[UUID] = Field(min_length=1)
    force: bool = False


def _overview_view(item: StoryboardBatchGroupOverview) -> dict[str, object]:
    return {
        "group_id": item.group_id,
        "group_sequence": item.group_sequence,
        "has_draft": item.has_draft,
        "draft_review_state": item.draft_review_state,
        "has_applied": item.has_applied,
        "storyboard_status": item.storyboard_status,
        "storyboard_version": item.storyboard_version,
        "current_generation_id": item.current_generation_id,
        "can_generate": item.can_generate,
    }


def _seed_view(item: StoryboardBatchSeedResult) -> dict[str, object]:
    return {
        "group_id": item.group_id,
        "group_sequence": item.group_sequence,
        "outcome": item.outcome,
        "review_state": item.review_state,
        "message": item.message,
    }


def _generate_view(item: StoryboardBatchGenerateResult) -> dict[str, object]:
    return {
        "group_id": item.group_id,
        "group_sequence": item.group_sequence,
        "outcome": item.outcome,
        "generation_id": item.generation_id,
        "message": item.message,
    }


def create_storyboard_batch_router(
    batch: StoryboardBatchService,
    current_account: Callable[[], Account] | None = None,
) -> APIRouter:
    if current_account is None:

        def session_not_wired() -> Account:
            raise HTTPException(
                401, detail={"code": "invalid_session", "message": "Session is invalid"}
            )

        current_account = session_not_wired
    router = APIRouter(prefix="/projects/{project_id}/storyboard-batch", tags=["storyboard-batch"])

    def call[T](operation: Callable[[], T]) -> T:
        try:
            return operation()
        except DomainError as exc:
            raise HTTPException(
                404 if exc.code == "not_found" else 409,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                422, detail={"code": "invalid_request", "message": str(exc)}
            ) from exc

    @router.get("/overview")
    def overview(
        project_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> dict[str, object]:
        items = call(lambda: batch.overview(account.id, project_id))
        return {"items": [_overview_view(item) for item in items]}

    @router.post("/director-drafts")
    def seed_director_drafts(
        project_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> dict[str, object]:
        results = call(lambda: batch.seed_drafts(account.id, project_id))
        return {"results": [_seed_view(item) for item in results]}

    @router.post("/grid-sheet-generations", status_code=status.HTTP_202_ACCEPTED)
    def generate_grid_sheets(
        project_id: UUID,
        body: BatchGenerateInput,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        results = call(
            lambda: batch.generate(
                account.id, project_id, tuple(body.group_ids), force=body.force
            )
        )
        return {"results": [_generate_view(item) for item in results]}

    return router
