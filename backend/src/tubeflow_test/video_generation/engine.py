from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import httpx


def uses_durable_execution_contract(workflow_id: str) -> bool:
    """The candidate engine keeps execution identity across uncertain responses."""
    return workflow_id in {
        "h3.tubeflow.storyboard-to-video",
        "h3.tubeflow.storyboard-to-video-hd",
    }


class EngineError(RuntimeError):
    pass


class EngineUncertain(EngineError):
    """The request may have reached the engine; only reconcile this attempt."""


class EngineAttemptNotFound(EngineError):
    """Authoritative 404 proving this attempt id was not accepted by KineStack."""


class EngineRejected(EngineError):
    """POST /executions answered with an explicit error status (408/429/5xx).

    The engine gave a definitive rejection response, so this is not an uncertain
    outcome; only timeouts/connection failures stay classified as EngineUncertain.
    """

    def __init__(self, status_code: int, retry_after_seconds: float | None = None) -> None:
        super().__init__(f"engine rejected the submission with status {status_code}")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class EngineTimeouts:
    staging_seconds: float = 30.0
    submit_seconds: float = 30.0
    poll_seconds: float = 10.0
    download_seconds: float = 120.0


class NodeHealth(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


@dataclass(frozen=True, slots=True)
class EngineNode:
    node_id: str
    base_url: str
    bearer_token: str
    max_concurrent: int
    health: NodeHealth = NodeHealth.READY
    active_executions: int = 0
    supports_reference_images_to_video: bool = True
    model_key: str = "minimax-h3"
    model_version: str = "1"
    workflow_id: str = "h3-r2v-images"
    workflow_version: str = "1"
    manifest_sha256: str = "a" * 64

    @property
    def available_capacity(self) -> int:
        return max(0, self.max_concurrent - self.active_executions)


class EngineGateway(Protocol):
    def readiness(self, node: EngineNode) -> dict[str, Any]: ...
    def capabilities(self, node: EngineNode) -> dict[str, Any]: ...
    def stage_asset(
        self,
        node: EngineNode,
        *,
        content: bytes,
        media_type: str,
        filename: str,
        expected_sha256: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...
    def delete_staged_asset(self, node: EngineNode, staged_asset_id: str) -> dict[str, Any]: ...
    def submit(
        self, node: EngineNode, *, idempotency_key: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...
    def inspect(self, node: EngineNode, attempt_id: UUID | str) -> dict[str, Any]: ...
    def cancel(
        self, node: EngineNode, attempt_id: UUID | str, *, idempotency_key: str
    ) -> dict[str, Any]: ...
    def outputs(self, node: EngineNode, attempt_id: UUID | str) -> dict[str, Any]: ...
    def download(self, node: EngineNode, attempt_id: UUID | str, output_id: str) -> bytes: ...
    def confirm_archive(
        self, node: EngineNode, attempt_id: UUID | str, *, idempotency_key: str
    ) -> dict[str, Any]: ...


def _retry_after_seconds(raw: str | None) -> float | None:
    """Parse the numeric Retry-After form; anything else is ignored as unknowable."""
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


class HttpKineStackEngine:
    def __init__(
        self, client: httpx.Client | None = None, timeouts: EngineTimeouts | None = None
    ) -> None:
        self._client = client or httpx.Client()
        self._timeouts = timeouts or EngineTimeouts()

    def close(self) -> None:
        self._client.close()

    def _request(self, node: EngineNode, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {node.bearer_token}"
        try:
            response = self._client.request(
                method, node.base_url.rstrip("/") + path, headers=headers, **kwargs
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EngineUncertain("engine request outcome is uncertain") from exc
        if (
            method == "DELETE"
            and path.startswith("/staged-assets/")
            and response.status_code == 404
        ):
            return response
        if not 200 <= response.status_code < 300:
            if response.status_code == 404 and path.startswith("/executions/"):
                raise EngineAttemptNotFound("engine attempt was not accepted")
            if (
                method == "POST"
                and path == "/executions"
                and (response.status_code in {408, 429} or response.status_code >= 500)
            ):
                raise EngineRejected(
                    response.status_code, _retry_after_seconds(response.headers.get("Retry-After"))
                )
            raise EngineError(f"engine request failed with status {response.status_code}")
        return response

    def readiness(self, node: EngineNode) -> dict[str, Any]:
        return self._json(self._request(node, "GET", "/health/ready"))

    def capabilities(self, node: EngineNode) -> dict[str, Any]:
        return self._json(self._request(node, "GET", "/capabilities"))

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
        response = self._request(
            node,
            "POST",
            "/staged-assets",
            headers={"Idempotency-Key": idempotency_key},
            files={"file": (filename, content, media_type)},
            data={"expected_sha256": expected_sha256},
            timeout=self._timeouts.staging_seconds,
        )
        receipt = self._json(response)
        receipt.setdefault("schema", "kinestack.staged-asset.v1")
        receipt.setdefault("status", "STORED")
        return receipt

    def delete_staged_asset(self, node: EngineNode, staged_asset_id: str) -> dict[str, Any]:
        response = self._request(node, "DELETE", f"/staged-assets/{staged_asset_id}")
        if response.status_code == 404:
            return {
                "id": staged_asset_id,
                "schema": "kinestack.staged-asset-deletion.v1",
                "status": "NOT_FOUND",
            }
        if response.status_code == 204:
            return {
                "id": staged_asset_id,
                "schema": "kinestack.staged-asset-deletion.v1",
                "status": "DELETED",
            }
        return self._json(response)

    def submit(
        self, node: EngineNode, *, idempotency_key: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._request(
            node,
            "POST",
            "/executions",
            headers={"Idempotency-Key": idempotency_key},
            json=body,
            timeout=self._timeouts.submit_seconds,
        )
        try:
            receipt = self._json(response)
        except (EngineError, ValueError) as exc:
            raise EngineUncertain("engine accepted submit but receipt is invalid") from exc
        if not receipt.get("attempt_id") or not receipt.get("status"):
            raise EngineUncertain("engine accepted submit but receipt is incomplete")
        receipt.setdefault("schema", "kinestack.execution.v1")
        return receipt

    def inspect(self, node: EngineNode, attempt_id: UUID | str) -> dict[str, Any]:
        receipt = self._json(
            self._request(
                node,
                "GET",
                f"/executions/{attempt_id}",
                timeout=self._timeouts.poll_seconds,
            )
        )
        receipt.setdefault("schema", "kinestack.execution.v1")
        return receipt

    def cancel(
        self, node: EngineNode, attempt_id: UUID | str, *, idempotency_key: str
    ) -> dict[str, Any]:
        receipt = self._json(
            self._request(
                node,
                "POST",
                f"/executions/{attempt_id}/cancel",
                headers={"Idempotency-Key": idempotency_key},
            )
        )
        receipt.setdefault("schema", "kinestack.execution.v1")
        return receipt

    def outputs(self, node: EngineNode, attempt_id: UUID | str) -> dict[str, Any]:
        receipt = self._json(self._request(node, "GET", f"/executions/{attempt_id}/outputs"))
        items = receipt.get("items")
        if isinstance(items, list):
            receipt["items"] = [
                {
                    **item,
                    "schema": item.get("schema", "kinestack.output.v1"),
                    "attempt_id": item.get("attempt_id", str(attempt_id)),
                }
                if isinstance(item, dict)
                else item
                for item in items
            ]
        return receipt

    def download(self, node: EngineNode, attempt_id: UUID | str, output_id: str) -> bytes:
        return self._request(
            node,
            "GET",
            f"/executions/{attempt_id}/outputs/{output_id}/content",
            timeout=self._timeouts.download_seconds,
        ).content

    def confirm_archive(
        self, node: EngineNode, attempt_id: UUID | str, *, idempotency_key: str
    ) -> dict[str, Any]:
        response = self._request(
            node,
            "POST",
            f"/executions/{attempt_id}/archive-confirmations",
            headers={"Idempotency-Key": idempotency_key},
        )
        if response.status_code == 204:
            return {
                "attempt_id": str(attempt_id),
                "schema": "kinestack.archive-confirmation.v1",
                "status": "CONFIRMED",
                "confirmed": True,
            }
        return self._json(response)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise EngineError("engine returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise EngineError("engine returned invalid JSON")
        return value


def verify_content(content: bytes, expected_size: int, expected_sha256: str) -> None:
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise EngineError("engine output size or sha256 mismatch")


class SafeNodeScheduler:
    def select(self, nodes: tuple[EngineNode, ...]) -> EngineNode:
        eligible = [
            n
            for n in nodes
            if n.health is NodeHealth.READY
            and n.supports_reference_images_to_video
            and n.available_capacity > 0
        ]
        if not eligible:
            raise EngineError("no healthy compatible engine node has capacity")
        return max(eligible, key=lambda node: (node.available_capacity, node.node_id))
