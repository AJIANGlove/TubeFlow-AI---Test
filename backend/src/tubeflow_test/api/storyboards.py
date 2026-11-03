from collections.abc import Callable
from io import BytesIO
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from tubeflow_test.api.public_status import PublicState, PublicStatus, public_state
from tubeflow_test.application.storyboards import (
    DirectorPlanService,
    StoryboardGenerationService,
    provider_safe_error_text,
)
from tubeflow_test.auto_pipeline.domain import REASON_DIRECTOR_VALIDATION_FAILED
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    DIRECTOR_GROUP_SHOT_LIMIT,
    STORYBOARD_AUTO_MAX_SUBMISSIONS,
    ContinuousObservationApproval,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    GenerationStatus,
    ShotSentenceLink,
    StoryboardGeneration,
)
from tubeflow_test.identity_project.domain import Account


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShotInput(StrictModel):
    shot_id: UUID
    order: int = Field(ge=1, le=DIRECTOR_GROUP_SHOT_LIMIT)
    scene: str = Field(min_length=1)
    shot_size: str = Field(min_length=1)
    camera_viewpoint: str = Field(min_length=1)
    subject_action_outcome: str = Field(min_length=1)
    narrative_function: str = Field(min_length=1)
    description: str = Field(min_length=1)
    sentence_ids: list[UUID] = Field(min_length=1)


class ContinuousObservationApprovalInput(StrictModel):
    previous_shot_id: UUID
    next_shot_id: UUID
    reason: str = Field(min_length=1)


class DraftInput(StrictModel):
    shots: list[ShotInput] = Field(min_length=1, max_length=DIRECTOR_GROUP_SHOT_LIMIT)
    expected_draft_revision_id: UUID | None
    source_candidate_id: UUID | None = None
    continuous_observation_approvals: list[ContinuousObservationApprovalInput] = Field(
        default_factory=list
    )


class ApplyInput(StrictModel):
    expected_draft_revision_id: UUID


class GenerateGridSheetInput(StrictModel):
    expected_director_revision_id: UUID


class SelectStoryboardInput(StrictModel):
    generation_id: UUID


def _director_revision_public_state(revision: DirectorPlanRevision) -> PublicState:
    if revision.kind is DirectorRevisionKind.APPLIED:
        return PublicState(
            PublicStatus.SUCCEEDED,
            "Director plan is applied and ready for storyboard generation.",
            ("GENERATE_STORYBOARD",),
        )
    if revision.may_apply:
        return PublicState(
            PublicStatus.SUCCEEDED,
            "Director draft is ready to edit or generate the storyboard.",
            ("EDIT_DIRECTOR_DRAFT", "GENERATE_STORYBOARD"),
        )
    return PublicState(
        PublicStatus.SUCCEEDED,
        ("Director draft requires continuous-observation approval before it can be applied."),
        ("EDIT_DIRECTOR_DRAFT",),
    )


def _storyboard_generation_public_state(
    raw_status: object, *, success_actions: tuple[str, ...] = ()
) -> PublicState:
    raw = getattr(raw_status, "value", raw_status)
    if str(raw) == GenerationStatus.INTENT_PERSISTED.value:
        return PublicState(PublicStatus.QUEUED, "Waiting to start.", ("REFRESH",))
    if str(raw) in {
        GenerationStatus.SUBMITTING.value,
        GenerationStatus.SUBMITTED.value,
        GenerationStatus.RUNNING.value,
        GenerationStatus.DOWNLOADED.value,
        GenerationStatus.QC_PASSED.value,
        GenerationStatus.OBJECT_STORED.value,
    }:
        return PublicState(PublicStatus.RUNNING, "Processing.", ("REFRESH",))
    return public_state(raw_status, success_actions=success_actions)


def create_storyboard_router(
    director: DirectorPlanService,
    generations: StoryboardGenerationService,
    current_account: Callable[[], Account] | None = None,
    *,
    media_account: Callable[[], Account] | None = None,
) -> APIRouter:
    if current_account is None:

        def session_not_wired() -> Account:
            raise HTTPException(
                401, detail={"code": "invalid_session", "message": "Session is invalid"}
            )

        current_account = session_not_wired
    media_account = media_account or current_account
    router = APIRouter(prefix="/storyboard-groups", tags=["storyboards"])

    def call[T](operation: Callable[[], T]) -> T:
        try:
            return operation()
        except KeyError as exc:
            raise HTTPException(
                404, detail={"code": "not_found", "message": "Storyboard resource was not found"}
            ) from exc
        except DomainError as exc:
            status = (
                404
                if exc.code == "not_found"
                else 409
                if exc.code in {"revision_conflict", "idempotency_conflict"}
                else 502
                if exc.code
                in {
                    "storyboard_processing_failed",
                    "invalid_provider_result",
                    "storage_integrity_error",
                    "invalid_checkpoint",
                }
                else 422
            )
            raise HTTPException(status, detail={"code": exc.code, "message": exc.message}) from exc
        except ValueError as exc:
            raise HTTPException(
                422, detail={"code": "invalid_request", "message": str(exc)}
            ) from exc

    def applied_revision_for_generation(
        owner_id: UUID,
        group_id: UUID,
        expected_revision_id: UUID,
    ) -> DirectorPlanRevision:
        draft: DirectorPlanRevision | None = None
        applied: DirectorPlanRevision | None = None
        try:
            draft = director.current_draft(owner_id, group_id)
        except DomainError as exc:
            if exc.code != "not_found":
                raise
        if draft is not None and draft.revision_id == expected_revision_id:
            return director.apply_draft(
                owner_id=owner_id,
                group_id=group_id,
                expected_draft_revision_id=expected_revision_id,
            )
        try:
            applied = director.current_applied(owner_id, group_id)
        except DomainError as exc:
            if exc.code != "not_found":
                raise
        if applied is not None and applied.revision_id == expected_revision_id:
            return applied
        if draft is None and applied is None:
            raise DomainError("not_found", "Storyboard resource was not found")
        raise DomainError("revision_conflict", "Director revision changed")

    @router.put("/{group_id}/director/draft")
    def edit_draft(
        group_id: UUID,
        body: DraftInput,
        request: Request,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        shots = tuple(
            DirectorShot(
                item.shot_id,
                item.order,
                item.scene,
                item.shot_size,
                item.camera_viewpoint,
                item.subject_action_outcome,
                item.narrative_function,
                item.description,
            )
            for item in body.shots
        )
        links = tuple(
            ShotSentenceLink(item.shot_id, sentence_id)
            for item in body.shots
            for sentence_id in item.sentence_ids
        )
        revision = call(
            lambda: director.edit_draft(
                owner_id=account.id,
                group_id=group_id,
                shots=shots,
                links=links,
                expected_draft_revision_id=body.expected_draft_revision_id,
                source_candidate_id=body.source_candidate_id,
                continuous_observation_approvals=tuple(
                    ContinuousObservationApproval(
                        item.previous_shot_id, item.next_shot_id, item.reason
                    )
                    for item in body.continuous_observation_approvals
                ),
            )
        )
        # §20 修正保存即续跑：草稿事务已提交；仅当本次草稿可应用且该项目 AUTO
        # 运行正因此原因暂停时，按原因码守卫自动恢复（授权轮次 +1），其余不动。
        if revision.may_apply:
            pipeline = getattr(request.app.state, "auto_pipeline_service", None)
            if pipeline is not None:
                pipeline.resume_if_paused_for(
                    account.id,
                    revision.project_id,
                    reason_code=REASON_DIRECTOR_VALIDATION_FAILED,
                )
        return {
            "revision_id": revision.revision_id,
            "kind": revision.kind,
            "review_state": revision.review_state,
            **_director_revision_public_state(revision).as_dict(),
        }

    @router.post("/{group_id}/director/apply")
    def apply_draft(
        group_id: UUID,
        body: ApplyInput,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        revision = call(
            lambda: director.apply_draft(
                owner_id=account.id,
                group_id=group_id,
                expected_draft_revision_id=body.expected_draft_revision_id,
            )
        )
        return {
            "revision_id": revision.revision_id,
            "kind": revision.kind,
            **_director_revision_public_state(revision).as_dict(),
        }

    @router.post("/{group_id}/grid-sheet-generations", status_code=status.HTTP_202_ACCEPTED)
    def generate_grid_sheet(
        group_id: UUID,
        body: GenerateGridSheetInput,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        applied = call(
            lambda: applied_revision_for_generation(
                account.id,
                group_id,
                body.expected_director_revision_id,
            )
        )
        generation = call(
            lambda: generations.create(
                group_id=group_id,
                owner_id=account.id,
                expected_applied_revision_id=applied.revision_id,
                idempotency_key=idempotency_key,
            )
        )
        return {
            "generation_id": generation.generation_id,
            "kind": generation.kind,
            "status": generation.status,
            "version": generation.version,
            **_storyboard_generation_public_state(generation.status).as_dict(),
        }

    @router.get("/generations/{generation_id}")
    def generation_status(
        generation_id: UUID,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        generation = call(lambda: generations.status(generation_id, account.id))
        return {
            "generation_id": generation.generation_id,
            "status": generation.status,
            "provider": generation.provider or None,
            "model": generation.model,
            "failover": {
                "policy_version": generation.failover_policy_version,
                "route_position": int(generation.failover_route_index or 0) + 1,
                "model_attempt": generation.failover_model_attempt,
                "total_attempt": generation.failover_total_attempt,
                "max_model_attempts": 2,
                "max_total_attempts": STORYBOARD_AUTO_MAX_SUBMISSIONS,
            }
            if generation.failover_chain_id is not None
            else None,
            "failure": {
                "stage": generation.failure_stage,
                "code": provider_safe_error_text(generation.failure_code)
                if generation.failure_code
                else None,
            }
            if generation.failure_stage or generation.failure_code
            else None,
            **_storyboard_generation_public_state(
                generation.status, success_actions=("VIEW", "SELECT_CURRENT")
            ).as_dict(),
        }

    @router.post("/generations/{generation_id}/reconcile")
    def reconcile_generation(
        generation_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> dict[str, object]:
        generation = call(lambda: generations.request_recovery(account.id, generation_id))
        return {
            "generation_id": generation.generation_id,
            "status": generation.status,
            **_storyboard_generation_public_state(
                generation.status, success_actions=("VIEW", "SELECT_CURRENT")
            ).as_dict(),
        }

    def generation_view(
        value: StoryboardGeneration, *, is_current: bool = False
    ) -> dict[str, object]:
        asset = value.asset
        return {
            "generation_id": value.generation_id,
            "version": value.version,
            "status": value.status,
            "applied_revision_id": value.applied_revision_id,
            "shots": [
                {
                    "shot_id": item.shot_id,
                    "order": item.order,
                    "scene": item.scene,
                    "shot_size": item.shot_size,
                    "camera_viewpoint": item.camera_viewpoint,
                    "subject_action_outcome": item.subject_action_outcome,
                    "narrative_function": item.narrative_function,
                    "description": item.description,
                    "sentence_ids": [
                        link.sentence_id
                        for link in value.sentence_links_snapshot
                        if link.shot_id == item.shot_id
                    ],
                }
                for item in value.shots_snapshot
            ],
            "prompt": value.prompt,
            "characters": [
                {
                    "ordinal": item.ordinal,
                    "asset_id": item.asset_id,
                    "sha256": item.sha256,
                    "media_type": item.media_type,
                    "size_bytes": item.size_bytes,
                }
                for item in value.character_assets_snapshot
            ],
            "style": value.style_snapshot,
            "provider": value.provider or None,
            "model": value.model,
            "failover": {
                "policy_version": value.failover_policy_version,
                "route_position": int(value.failover_route_index or 0) + 1,
                "model_attempt": value.failover_model_attempt,
                "total_attempt": value.failover_total_attempt,
                "max_model_attempts": 2,
                "max_total_attempts": STORYBOARD_AUTO_MAX_SUBMISSIONS,
            }
            if value.failover_chain_id is not None
            else None,
            "parameters": dict(value.parameters),
            "canonical_request_sha256": value.canonical_request_sha256,
            "failure": {
                "stage": value.failure_stage,
                "code": provider_safe_error_text(value.failure_code)
                if value.failure_code
                else None,
            }
            if value.failure_stage or value.failure_code
            else None,
            "created_at": value.created_at,
            "is_current": is_current,
            **_storyboard_generation_public_state(
                value.status,
                success_actions=("VIEW_MEDIA", "SELECT_CURRENT"),
            ).as_dict(),
            "asset": {
                "storyboard_asset_id": asset.storyboard_asset_id,
                "media_asset_id": asset.media.asset_id,
                "sha256": asset.media.sha256,
                "media_type": asset.media.media_type,
                "size_bytes": asset.media.size_bytes,
                "width": asset.width,
                "height": asset.height,
            }
            if asset is not None
            else None,
        }

    @router.get("/{group_id}/current")
    def current_generation(
        group_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> dict[str, object]:
        return generation_view(
            call(lambda: generations.current(account.id, group_id)), is_current=True
        )

    @router.get("/{group_id}/history")
    def generation_history(
        group_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> list[dict[str, object]]:
        items = call(lambda: generations.history(account.id, group_id))
        try:
            current_value = generations.current(account.id, group_id)
            current = current_value.generation_id
        except DomainError:
            current = None
        return [generation_view(item, is_current=item.generation_id == current) for item in items]

    @router.post("/{group_id}/current")
    def select_current_generation(
        group_id: UUID,
        body: SelectStoryboardInput,
        account: Annotated[Account, Depends(current_account)],
    ) -> dict[str, object]:
        return generation_view(
            call(lambda: generations.select_current(account.id, group_id, body.generation_id)),
            is_current=True,
        )

    @router.get("/generations/{generation_id}/detail")
    def generation_detail(
        generation_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> dict[str, object]:
        value = call(lambda: generations.detail(account.id, generation_id))
        try:
            current = generations.current(account.id, value.group_id)
        except DomainError:
            current = None
        return generation_view(
            value, is_current=current is not None and current.generation_id == value.generation_id
        )

    @router.head("/generations/{generation_id}/image", include_in_schema=False)
    @router.get("/generations/{generation_id}/image")
    def generation_image(
        generation_id: UUID,
        request: Request,
        account: Annotated[Account, Depends(media_account)],
    ) -> Response:
        payload, media_type, digest = call(
            lambda: generations.image_bytes(account.id, generation_id)
        )
        headers = {"ETag": f'"{digest}"', "Content-Length": str(len(payload))}
        if request.method == "HEAD":
            return Response(media_type=media_type, headers=headers)
        return StreamingResponse(
            BytesIO(payload),
            media_type=media_type,
            headers=headers,
        )

    @router.get("/{group_id}/director/draft")
    def current_draft(
        group_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> dict[str, object]:
        revision = call(lambda: director.current_draft(account.id, group_id))
        return {
            "revision_id": revision.revision_id,
            "kind": revision.kind,
            "review_state": revision.review_state,
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
                    "semantic_event": shot.semantic_event.value,
                    "subject_focus": shot.subject_focus.value,
                    "time_relation": shot.time_relation.value,
                    "location_relation": shot.location_relation.value,
                    "sentence_ids": [
                        link.sentence_id
                        for link in revision.sentence_links
                        if link.shot_id == shot.shot_id
                    ],
                }
                for shot in revision.shots
            ],
            "continuous_observation_approvals": [
                {
                    "previous_shot_id": item.previous_shot_id,
                    "next_shot_id": item.next_shot_id,
                    "reason": item.reason,
                }
                for item in revision.continuous_observation_approvals
            ],
            "created_at": revision.created_at,
            **_director_revision_public_state(revision).as_dict(),
        }

    @router.get("/{group_id}/director/applied")
    def current_applied(
        group_id: UUID, account: Annotated[Account, Depends(current_account)]
    ) -> dict[str, object]:
        revision = call(lambda: director.current_applied(account.id, group_id))
        return {
            "revision_id": revision.revision_id,
            "kind": revision.kind,
            "review_state": revision.review_state,
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
                    "semantic_event": shot.semantic_event.value,
                    "subject_focus": shot.subject_focus.value,
                    "time_relation": shot.time_relation.value,
                    "location_relation": shot.location_relation.value,
                    "sentence_ids": [
                        link.sentence_id
                        for link in revision.sentence_links
                        if link.shot_id == shot.shot_id
                    ],
                }
                for shot in revision.shots
            ],
            "continuous_observation_approvals": [
                {
                    "previous_shot_id": item.previous_shot_id,
                    "next_shot_id": item.next_shot_id,
                    "reason": item.reason,
                }
                for item in revision.continuous_observation_approvals
            ],
            "created_at": revision.created_at,
            **_director_revision_public_state(revision).as_dict(),
        }

    return router
