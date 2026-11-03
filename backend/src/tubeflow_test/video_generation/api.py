from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account
from tubeflow_test.video_generation.availability import unconfigured_h3_contract
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.orchestration import (
    DELIVER_WITH_DEFECT_MARKER,
    VIDEO_CONTENT_BLACK_REGION,
    DurableGroupVideoOrchestrator,
    PersistentAttempt,
    RequestedAction,
)
from tubeflow_test.video_generation.persistence import ManualH3IntentWriter

router = APIRouter(prefix="/group-videos", tags=["group-videos"])
VIDEO_CORS_HEADERS = ("Idempotency-Key",)


@router.get("/contract")
def group_video_contract(request: Request) -> dict[str, object]:
    """Expose only the safe aggregate H3 availability contract."""
    availability = getattr(request.app.state, "group_video_availability", None)
    if not callable(availability):
        return unconfigured_h3_contract()
    return cast(dict[str, object], availability())


class SelectVideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID


class AuthenticatedOwner(Protocol):
    def __call__(self) -> UUID: ...


def create_group_video_router(
    service: DurableGroupVideoOrchestrator, authenticated_owner: AuthenticatedOwner
) -> APIRouter:
    """Feature-local router. Central application registration is intentionally separate."""
    router = APIRouter(prefix="/group-videos", tags=["group-videos"])

    @router.get("/attempts/{attempt_id}", response_model=None)
    def query(
        attempt_id: UUID,
        owner_id: UUID = Depends(authenticated_owner),  # noqa: B008
    ) -> dict[str, object] | JSONResponse:
        return _call(lambda: service.get(owner_id, attempt_id))

    @router.get("/groups/{group_id}/video/current", response_model=None)
    def current_video(
        group_id: UUID,
        owner_id: UUID = Depends(authenticated_owner),  # noqa: B008
    ) -> dict[str, object] | JSONResponse:
        return _call(lambda: service.get_current(owner_id, group_id))

    @router.get("/groups/{group_id}/videos", response_model=None)
    def video_history(
        group_id: UUID,
        owner_id: UUID = Depends(authenticated_owner),  # noqa: B008
    ) -> dict[str, object] | JSONResponse:
        try:
            return {"items": [_view(item) for item in service.list_history(owner_id, group_id)]}
        except VideoRuleViolation as exc:
            return _error(exc)

    @router.post("/groups/{group_id}/video/current", response_model=None)
    def select_current_video(
        group_id: UUID,
        body: SelectVideoInput,
        owner_id: UUID = Depends(authenticated_owner),  # noqa: B008
    ) -> dict[str, object] | JSONResponse:
        return _call(lambda: service.select_current(owner_id, group_id, body.attempt_id))

    @router.post("/attempts/{attempt_id}/reconcile", status_code=202, response_model=None)
    def reconcile(
        attempt_id: UUID,
        owner_id: UUID = Depends(authenticated_owner),  # noqa: B008
    ) -> dict[str, object] | JSONResponse:
        return _call(
            lambda: service.request_action(owner_id, attempt_id, RequestedAction.RECONCILE)
        )

    @router.post("/attempts/{attempt_id}/cancel", status_code=202, response_model=None)
    def cancel(
        attempt_id: UUID,
        owner_id: UUID = Depends(authenticated_owner),  # noqa: B008
    ) -> dict[str, object] | JSONResponse:
        return _call(lambda: service.request_action(owner_id, attempt_id, RequestedAction.CANCEL))

    @router.post("/attempts/{attempt_id}/archive", status_code=202, response_model=None)
    def archive(
        attempt_id: UUID,
        owner_id: UUID = Depends(authenticated_owner),  # noqa: B008
    ) -> dict[str, object] | JSONResponse:
        return _call(lambda: service.request_action(owner_id, attempt_id, RequestedAction.ARCHIVE))

    return router


class ManualH3Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_storyboard_generation_id: UUID
    # 实验用显式 seed（2026-11-03 用户决策 4）：仅当提供时覆盖派生 seed；
    # 取值范围与 _ladder_seed 一致。
    seed: int | None = Field(default=None, ge=0, le=2**53 - 1)


class ManualH3Writer(Protocol):
    def create(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        expected_storyboard_generation_id: UUID,
        idempotency_key: str,
        request: dict[str, object] | None = None,
        seed: int | None = None,
    ) -> UUID: ...

    def get(self, owner_id: UUID, attempt_id: UUID) -> PersistentAttempt | None: ...


class PublicVideoAttempt(Protocol):
    @property
    def attempt_id(self) -> UUID: ...

    @property
    def project_id(self) -> UUID: ...

    @property
    def group_id(self) -> UUID: ...

    @property
    def generation_id(self) -> UUID: ...

    @property
    def stage(self) -> object: ...

    @property
    def archive_stage(self) -> object: ...

    @property
    def remote_phase_changed_at(self) -> object: ...

    @property
    def stalled_at(self) -> object: ...


def create_video_router(
    writer: ManualH3IntentWriter | ManualH3Writer,
    current_account: Callable[[], Account],
) -> APIRouter:
    manual_router = APIRouter(prefix="/group-videos", tags=["group-videos"])

    @manual_router.post("/groups/{group_id}/generations", status_code=status.HTTP_202_ACCEPTED)
    def create_manual_h3_intent(
        group_id: UUID,
        body: ManualH3Input,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        account: Account = Depends(current_account),  # noqa: B008
    ) -> dict[str, object]:
        _require_h3_available(request)
        try:
            attempt_id = writer.create(
                owner_id=account.id,
                group_id=group_id,
                expected_storyboard_generation_id=body.expected_storyboard_generation_id,
                idempotency_key=idempotency_key,
                seed=body.seed,
            )
        except (DomainError, VideoRuleViolation) as exc:
            if isinstance(exc, VideoRuleViolation):
                raise HTTPException(409, detail={"code": str(exc), "message": str(exc)}) from exc
            code = 404 if exc.code == "not_found" else 409
            raise HTTPException(code, detail={"code": exc.code, "message": exc.message}) from exc
        attempt = writer.get(account.id, attempt_id)
        if attempt is None:
            raise HTTPException(
                500,
                detail={
                    "code": "video_attempt_unavailable",
                    "message": "The created video attempt could not be loaded.",
                },
            )
        return _public_attempt_view(attempt)

    @manual_router.get("/attempts/{attempt_id}")
    def query_manual_h3_intent(
        attempt_id: UUID,
        account: Account = Depends(current_account),  # noqa: B008
    ) -> dict[str, object]:
        attempt = writer.get(account.id, attempt_id)
        if attempt is None:
            raise HTTPException(
                404, detail={"code": "not_found", "message": "H3 attempt was not found"}
            )
        return _public_attempt_view(attempt)

    return manual_router


def _require_h3_available(request: Request) -> None:
    availability = getattr(request.app.state, "group_video_availability", None)
    if not callable(availability):
        contract = unconfigured_h3_contract()
    else:
        try:
            contract = availability()
        except Exception:
            contract = {
                "available": False,
                "status": "UNAVAILABLE",
                "message": "Video service availability could not be verified.",
            }
    if not isinstance(contract, dict) or contract.get("available") is not True:
        status_value = (
            str(contract.get("status", "UNAVAILABLE"))
            if isinstance(contract, dict)
            else "UNAVAILABLE"
        )
        message = (
            str(contract.get("message", "Video service is unavailable."))
            if isinstance(contract, dict)
            else "Video service is unavailable."
        )
        code = "h3_not_configured" if status_value == "NOT_CONFIGURED" else "h3_unavailable"
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": code, "message": message},
        )


def _compiled_prompt_block(snapshot: dict[str, object]) -> dict[str, object] | None:
    block = snapshot.get("video_prompt")
    return block if isinstance(block, dict) else None


def _compiled_prompt_text(snapshot: dict[str, object]) -> str | None:
    block = _compiled_prompt_block(snapshot)
    text = block.get("text") if block is not None else None
    return text if isinstance(text, str) and text.strip() else None


def _compiled_prompt_strategy(snapshot: dict[str, object]) -> str | None:
    block = _compiled_prompt_block(snapshot)
    strategy = block.get("strategy") if block is not None else None
    return strategy if isinstance(strategy, str) else None


def _content_defect_view(value: PersistentAttempt) -> dict[str, object] | None:
    """内容级缺陷透出：probe 证据中的 content_qc 块（保持 findings 原结构）。

    仅内容级黑屏缺陷（error_code=video_content_black_region）且确有 findings 时透出；
    其余 QC 失败形态不组装缺陷视图。
    """
    probe = value.probe_evidence or {}
    if probe.get("error_code") != VIDEO_CONTENT_BLACK_REGION:
        return None
    content_qc = probe.get("content_qc")
    if not isinstance(content_qc, dict):
        return None
    findings = content_qc.get("findings")
    if not isinstance(findings, list) or not findings:
        return None
    return {"code": VIDEO_CONTENT_BLACK_REGION, "findings": findings}


def _view(value: PersistentAttempt) -> dict[str, object]:
    snapshot = value.input_snapshot or {}
    storyboard = snapshot.get("storyboard")
    shot_panels = snapshot.get("shot_panel_assets")
    return {
        **_public_attempt_view(value),
        "archived_asset": asdict(value.archived_asset) if value.archived_asset else None,
        "storyboard_binding": storyboard if isinstance(storyboard, dict) else None,
        "shot_panel_bindings": shot_panels if isinstance(shot_panels, list) else None,
        "applied_revision_id": snapshot.get("applied_revision_id"),
        "prompt": _compiled_prompt_text(snapshot) or snapshot.get("prompt"),
        "prompt_strategy": _compiled_prompt_strategy(snapshot),
        "target_duration_ms": snapshot.get("target_duration_ms"),
        "actual_duration_ms": (value.probe_evidence or {}).get("video_duration_ms"),
        "qc_passed": (value.probe_evidence or {}).get("qc_passed"),
        "delivered_with_defect": DELIVER_WITH_DEFECT_MARKER in value.retry_evidence,
        "content_defect": _content_defect_view(value),
        "current": value.is_current,
        "candidate": bool(
            value.stage.value == "ARCHIVED"
            and value.archive_stage.value == "COMPLETE"
            and value.archived_asset is not None
            and (value.probe_evidence or {}).get("qc_passed") is True
        ),
    }


def _public_attempt_view(value: PublicVideoAttempt) -> dict[str, object]:
    state = _video_public_state(
        value.stage,
        value.archive_stage,
    )
    return {
        "attempt_id": value.attempt_id,
        "project_id": value.project_id,
        "group_id": value.group_id,
        "generation_id": value.generation_id,
        "stage": str(getattr(value.stage, "value", value.stage) or "UNKNOWN"),
        **state,
        **_remote_wait_view(value),
    }


def _remote_wait_view(value: PublicVideoAttempt) -> dict[str, object]:
    """版本化远端等待状态；节点身份与原始远端状态不进入公开合同。"""
    stage = str(getattr(value.stage, "value", value.stage) or "")
    if stage not in {"SUBMITTED", "RUNNING"}:
        return {"remote_wait_status": None, "remote_wait_seconds": None}
    changed_at = _parse_iso_dt(value.remote_phase_changed_at)
    seconds = (
        max(0, int((datetime.now(UTC) - changed_at).total_seconds()))
        if changed_at is not None
        else None
    )
    return {
        "remote_wait_status": "remote_stalled" if value.stalled_at else "waiting_remote",
        "remote_wait_seconds": seconds,
    }


def _parse_iso_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _video_public_state(raw_stage: object, raw_archive_stage: object) -> dict[str, object]:
    stage = str(getattr(raw_stage, "value", raw_stage) or "UNKNOWN")
    archive_stage = str(getattr(raw_archive_stage, "value", raw_archive_stage) or "UNKNOWN")
    if stage == "ARCHIVED" and archive_stage == "COMPLETE":
        return _public_state(
            "SUCCEEDED",
            "Video generation completed successfully.",
            ("VIEW_MEDIA", "SELECT_CURRENT"),
        )
    if archive_stage == "QC_FAILED":
        return _public_state(
            "FAILED",
            "The generated video did not pass validation.",
            ("RETRY", "REFRESH"),
        )
    if stage in {"CREATED", "STAGING", "NOT_SUBMITTED"}:
        return _public_state(
            "QUEUED",
            "Video generation is waiting to start.",
            ("CANCEL", "REFRESH"),
        )
    if stage in {"SUBMITTING", "REMOTE_STATE_UNKNOWN"}:
        return _public_state(
            "RECOVERY_REQUIRED",
            "The remote result is uncertain. Reconcile this attempt before any new submission.",
            ("RECONCILE", "CANCEL", "REFRESH"),
        )
    if stage in {"SUBMITTED", "RUNNING", "SUCCEEDED"}:
        actions = (
            ("RECONCILE", "REFRESH") if stage == "SUCCEEDED" else ("RECONCILE", "CANCEL", "REFRESH")
        )
        return _public_state(
            "RUNNING",
            "Video generation is in progress.",
            actions,
        )
    if stage == "FAILED":
        return _public_state(
            "FAILED",
            "Video generation failed. Review this attempt before starting a new generation.",
            ("RETRY", "REFRESH"),
        )
    if stage == "CANCELLED":
        return _public_state(
            "CANCELLED",
            "Video generation was cancelled.",
            ("REFRESH",),
        )
    return _public_state(
        "RECOVERY_REQUIRED",
        "Video generation status is uncertain. Refresh before taking further action.",
        ("REFRESH",),
    )


def _public_state(
    public_status: str, message: str, allowed_actions: tuple[str, ...]
) -> dict[str, object]:
    return {
        "public_status": public_status,
        "message": message,
        "allowed_actions": list(allowed_actions),
    }


def _call(operation: Callable[[], PersistentAttempt]) -> dict[str, object] | JSONResponse:
    try:
        return _view(operation())
    except VideoRuleViolation as exc:
        return _error(exc)


def _error(exc: VideoRuleViolation) -> JSONResponse:
    code = str(exc)
    status = 404 if "not found" in code else 409
    if code in {
        "invalid_seed",
        "video duration must be between 3 and 15 seconds",
        "immutable_video_input_changed",
    }:
        status = 422
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": code}},
    )
