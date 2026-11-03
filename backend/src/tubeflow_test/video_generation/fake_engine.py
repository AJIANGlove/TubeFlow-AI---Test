from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

from tubeflow_test.video_generation.engine import (
    EngineAttemptNotFound,
    EngineNode,
    EngineRejected,
    EngineUncertain,
)


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_stage_at: int | None = None
        self.lose_submit_response = False
        self.output = b"fake-mp4-with-native-audio"
        self.output_sha256_override: str | None = None
        self.inspect_status = "RUNNING"
        self.remote_acceptance: str | None = None
        self.remote_task_id: str | None = None
        self.last_task_key: str | None = None
        self.inspect_not_found = False
        self.inspect_error_once = False
        self.lose_inspect_response = False
        self.reject_submit_status: int | None = None
        self.reject_submit_retry_after: float | None = None
        self.fail_delete_once = False
        self.lose_delete_response_once = False
        self.lose_archive_response_once = False
        self.lose_cancel_response_once = False
        self.archive_confirmed = False
        self.cancel_status_after_loss = "CANCELLED"
        self.archive_confirmation_effects: set[str] = set()
        self.cancel_effects: set[str] = set()
        self._submissions: dict[UUID, dict[str, Any]] = {}
        self._staged: set[str] = set()
        self._staged_keys: dict[str, str] = {}

    def readiness(self, node: EngineNode) -> dict[str, Any]:
        return {"status": "ready"}

    def capabilities(self, node: EngineNode) -> dict[str, Any]:
        return {"items": [{"capability": "video.reference_images_to_video"}]}

    def stage_asset(
        self,
        node: EngineNode,
        *,
        content: bytes,
        media_type: str,
        filename: str,
        expected_sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.calls.append((node.node_id, "stage"))
        if self.fail_stage_at == sum(1 for _, call in self.calls if call == "stage"):
            raise RuntimeError("configured upload failure")
        staged_id = self._staged_keys.setdefault(idempotency_key, str(uuid4()))
        self._staged.add(staged_id)
        return {
            "id": staged_id,
            "schema": "kinestack.staged-asset.v1",
            "status": "STORED",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "media_type": media_type,
        }

    def delete_staged_asset(self, node: EngineNode, staged_asset_id: str) -> dict[str, Any]:
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("configured cleanup failure")
        existed = staged_asset_id in self._staged
        self._staged.discard(staged_asset_id)
        self.calls.append((node.node_id, f"delete:{staged_asset_id}"))
        if self.lose_delete_response_once:
            self.lose_delete_response_once = False
            raise EngineUncertain("delete response lost")
        return {
            "id": staged_asset_id,
            "schema": "kinestack.staged-asset-deletion.v1",
            "status": "DELETED" if existed else "NOT_FOUND",
        }

    def submit(
        self, node: EngineNode, *, idempotency_key: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        attempt = UUID(str(body["attempt_id"]))
        staged = body.get("staged_assets", [])
        if any(
            isinstance(item, dict) and str(item.get("staged_asset_id")) not in self._staged
            for item in staged
        ):
            raise RuntimeError("STAGED_ASSET_NOT_FOUND")
        self.calls.append((node.node_id, "submit"))
        if self.reject_submit_status is not None:
            # 明确错误状态码：引擎给出了既定拒绝响应，提交未进入未知态。
            raise EngineRejected(self.reject_submit_status, self.reject_submit_retry_after)
        self._submissions.setdefault(attempt, body)
        if self.lose_submit_response:
            raise EngineUncertain("accepted response lost")
        result: dict[str, Any] = {
            "attempt_id": str(attempt),
            "status": "ACCEPTED",
            "schema": "kinestack.execution.v1",
        }
        if self.remote_acceptance is not None:
            result["remote_acceptance"] = self.remote_acceptance
        if self.remote_task_id is not None:
            result["id"] = self.remote_task_id
        return result

    def inspect(self, node: EngineNode, attempt_id: UUID | str) -> dict[str, Any]:
        self.last_task_key = str(attempt_id)
        self.calls.append((node.node_id, "inspect"))
        if self.inspect_not_found:
            self.inspect_not_found = False
            raise EngineAttemptNotFound("same attempt was not accepted")
        if self.inspect_error_once:
            self.inspect_error_once = False
            raise EngineUncertain("inspect response uncertain")
        if self.lose_inspect_response:
            raise EngineUncertain("inspect response uncertain")
        result: dict[str, Any] = {
            "attempt_id": str(attempt_id),
            "status": self.inspect_status,
            "schema": "kinestack.execution.v1",
        }
        if self.remote_acceptance is not None:
            result["remote_acceptance"] = self.remote_acceptance
        if self.archive_confirmed:
            result["archive_confirmed"] = True
        return result

    def cancel(
        self, node: EngineNode, attempt_id: UUID | str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self.last_task_key = str(attempt_id)
        self.calls.append((node.node_id, "cancel"))
        self.cancel_effects.add(idempotency_key)
        if self.lose_cancel_response_once:
            self.lose_cancel_response_once = False
            self.inspect_status = self.cancel_status_after_loss
            raise EngineUncertain("cancel response lost")
        self.inspect_status = "CANCELLED"
        return {
            "attempt_id": str(attempt_id),
            "schema": "kinestack.execution.v1",
            "status": "CANCELLED",
        }

    def outputs(self, node: EngineNode, attempt_id: UUID | str) -> dict[str, Any]:
        self.last_task_key = str(attempt_id)
        digest = self.output_sha256_override or hashlib.sha256(self.output).hexdigest()
        return {
            "items": [
                {
                    "id": str(uuid4()),
                    "attempt_id": str(attempt_id),
                    "schema": "kinestack.output.v1",
                    "media_type": "video/mp4",
                    "size_bytes": len(self.output),
                    "sha256": digest,
                }
            ]
        }

    def download(self, node: EngineNode, attempt_id: UUID | str, output_id: str) -> bytes:
        self.last_task_key = str(attempt_id)
        return self.output

    def confirm_archive(
        self, node: EngineNode, attempt_id: UUID | str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self.calls.append((node.node_id, "archive"))
        self.archive_confirmation_effects.add(idempotency_key)
        self.archive_confirmed = True
        if self.lose_archive_response_once:
            self.lose_archive_response_once = False
            raise EngineUncertain("archive confirmation response lost")
        return {
            "attempt_id": str(attempt_id),
            "schema": "kinestack.archive-confirmation.v1",
            "status": "CONFIRMED",
            "confirmed": True,
        }
