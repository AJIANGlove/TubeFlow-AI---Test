from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from enum import StrEnum
from typing import Any, Never, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from tubeflow_test.api.public_status import public_state
from tubeflow_test.application.narration import ProjectNarrationService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import (
    ESTIMATED_RISK_THRESHOLD_MS,
    estimate_narration_duration_ms,
    is_nonvisual_script_separator,
)
from tubeflow_test.identity_project.domain import Account

# Final assembly supplies the authenticated-owner dependency. Keeping the module-level router
# empty prevents an ownerless feature endpoint from being accidentally exposed.
router = APIRouter(prefix="/narration", tags=["narration"])


class CurrentOwner(Protocol):
    def __call__(self) -> Account: ...


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    pasted_text: str


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID


class ManualResolution(StrEnum):
    CONFIRMED_NOT_ACCEPTED = "CONFIRMED_NOT_ACCEPTED"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"
    RETRY_MISSING = "RETRY_MISSING"
    RETRY_INVALID_OUTPUT = "RETRY_INVALID_OUTPUT"


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    idempotency_key: str
    resolution: ManualResolution
    external_evidence: str = Field(min_length=1, max_length=2000)


def create_router(
    service: ProjectNarrationService, current_owner: Callable[[], Account]
) -> APIRouter:
    feature = APIRouter(prefix="/narration", tags=["narration"])

    @feature.post("/preflight")
    def preflight(
        data: PreflightRequest,
        owner: Account = Depends(current_owner),  # noqa: B008
    ) -> dict[str, object]:
        script = _call(lambda: service.preflight(owner.id, data.project_id, data.pasted_text))
        sentences: list[dict[str, object]] = []
        for sentence in script.sentences:
            separator = is_nonvisual_script_separator(sentence.text)
            estimated_ms = (
                None
                if separator
                else estimate_narration_duration_ms(sentence.text, script.language)
            )
            sentences.append(
                {
                    **asdict(sentence),
                    "is_separator": separator,
                    "estimated_duration_ms": estimated_ms,
                    "estimated_over_limit": (
                        estimated_ms is not None and estimated_ms >= ESTIMATED_RISK_THRESHOLD_MS
                    ),
                }
            )
        return {
            "language": script.language.value,
            "rule_version": script.rule_version,
            "source_sha256": script.source_sha256,
            "estimated_risk_threshold_ms": ESTIMATED_RISK_THRESHOLD_MS,
            "sentences": sentences,
        }

    @feature.post("/confirm", status_code=status.HTTP_202_ACCEPTED)
    async def confirm(
        data: ProjectRequest,
        owner: Account = Depends(current_owner),  # noqa: B008
    ) -> dict[str, object]:
        result = await _call_async(service.confirm_and_start(owner.id, data.project_id))
        serialized = _serialize(result)
        if not isinstance(serialized, dict):
            raise RuntimeError("narration status must be a mapping")
        return {**serialized, **public_state(serialized.get("state")).as_dict()}

    @feature.get("/projects/{project_id}/status")
    def narration_status(
        project_id: UUID,
        owner: Account = Depends(current_owner),  # noqa: B008
    ) -> dict[str, object]:
        result = cast(
            dict[str, object], _serialize(_call(lambda: service.status(owner.id, project_id)))
        )
        state = public_state(
            result.get("state"),
            success_actions=("VIEW_TIMELINE", "GENERATE_DIRECTOR"),
            failure_actions=("MANUAL_REVIEW", "REFRESH"),
        )
        return _public_narration_payload({**result, **state.as_dict()})

    @feature.get("/projects/{project_id}/sentences")
    def sentences(
        project_id: UUID,
        owner: Account = Depends(current_owner),  # noqa: B008
    ) -> object:
        return _serialize(_call(lambda: service.status(owner.id, project_id))["sentences"])

    @feature.get("/projects/{project_id}/timeline")
    def timeline(
        project_id: UUID,
        owner: Account = Depends(current_owner),  # noqa: B008
    ) -> object:
        return _serialize(_call(lambda: service.status(owner.id, project_id))["timeline"])

    @feature.get("/projects/{project_id}/groups")
    def groups(
        project_id: UUID,
        owner: Account = Depends(current_owner),  # noqa: B008
    ) -> object:
        return _serialize(_call(lambda: service.status(owner.id, project_id))["groups"])

    @feature.post("/recover", status_code=status.HTTP_202_ACCEPTED)
    async def recover(
        data: RecoveryRequest,
        owner: Account = Depends(current_owner),  # noqa: B008
    ) -> dict[str, object]:
        await _call_async(
            service.reconcile(
                owner.id,
                data.project_id,
                data.idempotency_key,
                data.resolution.value,
                data.external_evidence,
            )
        )
        result = cast(
            dict[str, object],
            _serialize(_call(lambda: service.status(owner.id, data.project_id))),
        )
        state = public_state(
            result.get("state"),
            success_actions=("VIEW_TIMELINE", "GENERATE_DIRECTOR"),
            failure_actions=("MANUAL_REVIEW", "REFRESH"),
        )
        return _public_narration_payload({**result, **state.as_dict()})

    return feature


async def _call_async(awaitable: Awaitable[object]) -> object:
    try:
        return await awaitable
    except DomainError as error:
        _raise_http(error)


def _call[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except DomainError as error:
        _raise_http(error)


def _raise_http(error: DomainError) -> Never:
    conflicts = {
        "idempotency_conflict",
        "submission_in_progress",
        "manual_reconciliation_required",
        "video_blocked",
        "project_snapshot_locked",
    }
    not_found = {"not_found"}
    code = (
        status.HTTP_409_CONFLICT
        if error.code in conflicts
        else status.HTTP_404_NOT_FOUND
        if error.code in not_found
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    raise HTTPException(code, detail={"code": error.code, "message": error.message}) from error


def _serialize(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _serialize(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_serialize(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    return value


def _public_narration_payload(result: dict[str, object]) -> dict[str, object]:
    rows = result.get("sentence_statuses", [])
    public_rows: list[dict[str, object]] = []
    for value in rows if isinstance(rows, (tuple, list)) else ():
        if not isinstance(value, dict):
            continue
        raw = str(value.get("state", "NOT_STARTED"))
        state = public_state(
            raw,
            success_actions=("VIEW_AUDIO",),
            failure_actions=("MANUAL_REVIEW", "REFRESH"),
        ).as_dict()
        if raw == "OUTPUT_INVALID":
            state = {
                "public_status": "RECOVERY_REQUIRED",
                "message": "该句配音未通过技术质检，需要人工确认后重试。",
                "allowed_actions": ["RETRY_INVALID_OUTPUT", "REFRESH"],
            }
        elif raw == "ARCHIVE_FAILED":
            state = {
                "public_status": "RECOVERY_REQUIRED",
                "message": "该句配音归档失败，需要人工检查后恢复已有结果。",
                "allowed_actions": ["MANUAL_REVIEW", "REFRESH"],
            }
        elif raw == "MANUAL_REVIEW_REQUIRED":
            state = {
                "public_status": "RECOVERY_REQUIRED",
                "message": "该句配音尚未完成且处理任务已停止，需要人工检查。",
                "allowed_actions": ["MANUAL_REVIEW", "REFRESH"],
            }
        elif raw == "SAFE_TO_RETRY":
            state = {
                "public_status": "QUEUED",
                "message": "该句配音正在等待后端处理。",
                "allowed_actions": ["REFRESH"],
            }
        code = (
            "narration_recovery_incomplete"
            if raw == "MANUAL_REVIEW_REQUIRED"
            else value.get("safe_error_code")
        )
        safe_code = (
            code
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code)
            and state["public_status"] in {"FAILED", "RECOVERY_REQUIRED"}
            else None
        )
        public_rows.append({
            "contract_version": "narration-sentence.v1",
            "sentence_id": value.get("sentence_id"),
            "sequence": value.get("sequence"),
            "text": value.get("text"),
            "idempotency_key": value.get("idempotency_key"),
            "safe_error_code": safe_code,
            **state,
        })
    return {**result, "sentence_statuses": public_rows}
