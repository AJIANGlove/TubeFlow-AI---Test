from collections.abc import Callable
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from tubeflow_test.api.public_status import PublicState, PublicStatus
from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorConflict,
    ProjectDirectorMaterialization,
    ProjectDirectorRevision,
    ProjectDirectorRun,
    ProjectDirectorRunProgress,
    ProjectDirectorRunStatus,
    ProjectDirectorTrigger,
)
from tubeflow_test.identity_project.domain import Account


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartProjectDirectorRunInput(StrictModel):
    confirm_new_input: bool = False


class ProjectDirectorManualResolution(StrEnum):
    CONFIRMED_NOT_ACCEPTED = "CONFIRMED_NOT_ACCEPTED"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"


class ResolveProjectDirectorUnknownInput(StrictModel):
    resolution: ProjectDirectorManualResolution
    external_evidence: str = Field(min_length=1, max_length=2000)


def _run_public_state(value: ProjectDirectorRun) -> PublicState:
    if value.status is ProjectDirectorRunStatus.INTENT_PERSISTED:
        return PublicState(PublicStatus.QUEUED, "Waiting to start.", ("REFRESH",))
    if value.status is ProjectDirectorRunStatus.RUNNING:
        return PublicState(PublicStatus.RUNNING, "Processing.", ("REFRESH",))
    if value.status is ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN:
        return PublicState(
            PublicStatus.RECOVERY_REQUIRED,
            (
                "The project director result is uncertain. Reprocess manually to reuse "
                "verified successful units and retry the remaining units."
            ),
            ("REGENERATE_DIRECTOR_PREPLAN", "REFRESH"),
        )
    if value.status is ProjectDirectorRunStatus.SUCCEEDED:
        return PublicState(
            PublicStatus.SUCCEEDED,
            "The latest untimed project director plan is ready.",
            ("VIEW_CURRENT", "REGENERATE_DIRECTOR_PREPLAN"),
        )
    if value.retry_blocked:
        return PublicState(
            PublicStatus.FAILED,
            "The project director run was manually confirmed failed.",
            (),
        )
    return PublicState(
        PublicStatus.FAILED,
        "The project director run stopped after an unrecoverable unit failure.",
        ("RETRY",),
    )


def _run_view(value: ProjectDirectorRun, progress: ProjectDirectorRunProgress) -> dict[str, object]:
    return {
        "run_id": value.run_id,
        "project_id": value.project_id,
        "trigger": value.trigger,
        "status": value.status,
        "total_units": value.total_units,
        "completed_units": progress.succeeded_units,
        "succeeded_units": progress.succeeded_units,
        "inflight_units": progress.inflight_units,
        "retry_pending_units": progress.retry_pending_units,
        "unsent_units": progress.unsent_units,
        "failed_unit_ordinal": progress.failed_unit_ordinal,
        "failed_unit_kind": progress.failed_unit_kind,
        "auto_retries_used": progress.auto_retries_used,
        "auto_retry_ceiling": progress.auto_retry_ceiling,
        "revision_id": value.revision_id,
        "recovery_source_run_id": value.recovery_source_run_id,
        "failure_code": value.failure_code,
        "created_at": value.created_at,
        **_run_public_state(value).as_dict(),
    }


def _conflict_view(value: ProjectDirectorConflict) -> dict[str, object]:
    return {
        "code": value.code,
        "sentence_ids": list(value.sentence_ids),
        "shot_ids": list(value.shot_ids),
    }


def _revision_view(
    value: ProjectDirectorRevision,
    run: ProjectDirectorRun,
    *,
    offset: int,
    limit: int,
) -> dict[str, object]:
    selected = value.shots[offset : offset + limit]
    selected_ids = {item.shot_id for item in selected}
    links = tuple(item for item in value.sentence_links if item.shot_id in selected_ids)
    linked_sentence_ids = {item.sentence_id for item in links}
    sentences = tuple(
        item for item in run.input_snapshot.sentences if item.sentence_id in linked_sentence_ids
    )
    has_conflicts = bool(value.quality_conflicts)
    public = (
        PublicState(
            PublicStatus.RECOVERY_REQUIRED,
            "The untimed plan is complete but contains quality conflicts.",
            ("VIEW_CURRENT", "REGENERATE_DIRECTOR_PREPLAN"),
        )
        if has_conflicts
        else PublicState(
            PublicStatus.SUCCEEDED,
            "The latest untimed project director plan is ready.",
            ("VIEW_CURRENT", "REGENERATE_DIRECTOR_PREPLAN"),
        )
    )
    return {
        "revision_id": value.revision_id,
        "run_id": value.run_id,
        "revision_number": value.revision_number,
        "total_shots": len(value.shots),
        "offset": offset,
        "limit": limit,
        "sentences": [
            {
                "sentence_id": item.sentence_id,
                "sequence": item.sequence,
                "text": item.text,
                "text_sha256": item.text_sha256,
            }
            for item in sentences
        ],
        "shots": [
            {
                "shot_id": shot.shot_id,
                "order": shot.order,
                "scene": shot.scene,
                "shot_size": shot.shot_size,
                "camera_viewpoint": shot.camera_viewpoint,
                "subject_action_outcome": shot.subject_action_outcome,
                "narrative_function": shot.narrative_function,
                "description": shot.description,
                "sentence_ids": [
                    item.sentence_id for item in links if item.shot_id == shot.shot_id
                ],
            }
            for shot in selected
        ],
        "quality_conflicts": [_conflict_view(item) for item in value.quality_conflicts],
        "created_at": value.created_at,
        **public.as_dict(),
    }


def _materialization_view(value: ProjectDirectorMaterialization) -> dict[str, object]:
    if value.status is MaterializationStatus.SUCCEEDED:
        public = PublicState(
            PublicStatus.SUCCEEDED,
            "The untimed plan was deterministically mapped to the measured narration groups.",
            ("VIEW_FINAL_DIRECTOR",),
        )
    else:
        public = PublicState(
            PublicStatus.RECOVERY_REQUIRED,
            "One or more mapped groups require manual director editing.",
            ("EDIT_FINAL_DIRECTOR",),
        )
    return {
        "materialization_id": value.materialization_id,
        "project_id": value.project_id,
        "preplan_revision_id": value.preplan_revision_id,
        "status": value.status,
        "algorithm_version": value.algorithm_version,
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "group_id": item.group_id,
                "group_sequence": item.group_sequence,
                "review_state": item.review_state,
                "source_shot_links": [
                    {
                        "source_shot_id": link.source_shot_id,
                        "candidate_shot_id": link.candidate_shot_id,
                    }
                    for link in item.source_shot_links
                ],
                "shots": [
                    {
                        "shot_id": shot.shot_id,
                        "order": shot.order,
                        "scene": shot.scene,
                        "shot_size": shot.shot_size,
                        "camera_viewpoint": shot.camera_viewpoint,
                        "subject_action_outcome": shot.subject_action_outcome,
                        "narrative_function": shot.narrative_function,
                        "description": shot.description,
                        "sentence_ids": [
                            link.sentence_id
                            for link in item.sentence_links
                            if link.shot_id == shot.shot_id
                        ],
                    }
                    for shot in item.shots
                ],
                "conflicts": [_conflict_view(conflict) for conflict in item.conflicts],
                "final_revision_id": (
                    item.final_revision.revision_id if item.final_revision is not None else None
                ),
            }
            for item in value.candidates
        ],
        "created_at": value.created_at,
        **public.as_dict(),
    }


def create_project_director_router(
    service: ProjectDirectorService,
    current_account: Callable[[], Account],
    *,
    provider_configured: bool,
) -> APIRouter:
    router = APIRouter(prefix="/projects/{project_id}", tags=["project-director"])

    def call[T](operation: Callable[[], T]) -> T:
        try:
            return operation()
        except DomainError as exc:
            code = (
                status.HTTP_404_NOT_FOUND
                if exc.code == "not_found"
                else status.HTTP_409_CONFLICT
                if exc.code
                in {
                    "idempotency_conflict",
                    "director_retry_blocked",
                    "director_run_unresolved",
                    "director_input_changed",
                    "director_recovery_source_invalid",
                    "director_recovery_history_unresolved",
                    "reconciliation_not_allowed",
                }
                else status.HTTP_422_UNPROCESSABLE_ENTITY
            )
            raise HTTPException(code, detail={"code": exc.code, "message": exc.message}) from exc

    @router.post(
        "/director-preplan/runs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_run(
        project_id: UUID,
        body: StartProjectDirectorRunInput,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        if not provider_configured:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "director_provider_not_configured",
                    "message": "Director Provider is not configured.",
                },
            )
        run = call(
            lambda: service.create_run(
                owner_id=account.id,
                project_id=project_id,
                idempotency_key=idempotency_key,
                trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
                confirm_new_input=body.confirm_new_input,
            )
        )
        return _run_view(run, call(lambda: service.progress(account.id, run.run_id)))

    @router.get("/director-preplan/runs/latest")
    def latest_run(
        project_id: UUID,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        run = call(lambda: service.latest(account.id, project_id))
        return _run_view(run, call(lambda: service.progress(account.id, run.run_id)))

    @router.get("/director-preplan/runs/{run_id}")
    def run_status(
        project_id: UUID,
        run_id: UUID,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        value = call(lambda: service.status(account.id, run_id))
        if value.project_id != project_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "Project director run was not found"},
            )
        return _run_view(value, call(lambda: service.progress(account.id, run_id)))

    @router.post("/director-preplan/runs/{run_id}/manual-resolution")
    def resolve_unknown(
        project_id: UUID,
        run_id: UUID,
        body: ResolveProjectDirectorUnknownInput,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        existing = call(lambda: service.status(account.id, run_id))
        if existing.project_id != project_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "Project director run was not found"},
            )
        run = call(
            lambda: service.resolve_unknown(
                account.id,
                run_id,
                body.resolution.value,
                body.external_evidence,
            )
        )
        return _run_view(run, call(lambda: service.progress(account.id, run_id)))

    @router.get("/director-preplan/current")
    def current_revision(
        project_id: UUID,
        account: Annotated[Account, Depends(current_account)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, object]:
        value = call(lambda: service.current(account.id, project_id))
        run = call(lambda: service.status(account.id, value.run_id))
        return _revision_view(value, run, offset=offset, limit=limit)

    @router.get("/director-materialization/current")
    def current_materialization(
        project_id: UUID,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        return _materialization_view(
            call(lambda: service.current_materialization(account.id, project_id))
        )

    return router
