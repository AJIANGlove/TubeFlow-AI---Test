from __future__ import annotations

import hashlib

import httpx
import pytest

from tubeflow_test.video_generation.engine import (
    EngineError,
    EngineNode,
    EngineRejected,
    EngineUncertain,
    HttpKineStackEngine,
)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_submit_explicit_error_status_is_a_deterministic_rejection(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "rejected"}, request=request)

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EngineRejected) as captured:
        engine.submit(EngineNode("node", "http://node", "token", 1), idempotency_key="k", body={})
    assert captured.value.status_code == status


def test_submit_rejection_carries_retry_after_seconds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": "busy"}, headers={"Retry-After": "7"}, request=request
        )

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EngineRejected) as captured:
        engine.submit(EngineNode("node", "http://node", "token", 1), idempotency_key="k", body={})
    assert captured.value.status_code == 429
    assert captured.value.retry_after_seconds == 7.0


def test_submit_accepted_with_invalid_receipt_stays_uncertain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, text="not-a-json-object", request=request)

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EngineUncertain):
        engine.submit(EngineNode("node", "http://node", "token", 1), idempotency_key="k", body={})


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("timeout"), httpx.ConnectError("down")])
def test_submit_transport_failures_stay_uncertain(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EngineUncertain):
        engine.submit(EngineNode("node", "http://node", "token", 1), idempotency_key="k", body={})


def test_inspect_error_status_is_not_reclassified_as_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"}, request=request)

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EngineError, match="500"):
        engine.inspect(EngineNode("node", "http://node", "token", 1), "attempt-1")


def test_submit_explicit_contract_rejection_is_deterministic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "invalid"}, request=request)

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EngineError, match="422"):
        engine.submit(EngineNode("node", "http://node", "token", 1), idempotency_key="k", body={})


def test_staged_delete_404_is_idempotent_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    assert engine.delete_staged_asset(
        EngineNode("node", "http://node", "token", 1), "staged-1"
    ) == {
        "id": "staged-1",
        "schema": "kinestack.staged-asset-deletion.v1",
        "status": "NOT_FOUND",
    }


def test_native_staged_asset_receipt_is_normalized_at_adapter_boundary() -> None:
    content = b"reference-image"
    digest = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": "2fd9c90b-b6ce-4e9f-ac95-d7cc49651463",
                "sha256": digest,
                "size_bytes": len(content),
                "media_type": "image/png",
                "expires_at": "2026-09-01T00:00:00Z",
            },
            request=request,
        )

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    receipt = engine.stage_asset(
        EngineNode("node", "http://node", "token", 1),
        content=content,
        media_type="image/png",
        filename="reference.png",
        expected_sha256=digest,
        idempotency_key="stage-key-long-enough",
    )
    assert receipt["schema"] == "kinestack.staged-asset.v1"
    assert receipt["status"] == "STORED"
    assert receipt["sha256"] == digest


def test_native_staged_delete_204_is_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, request=request)

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    assert engine.delete_staged_asset(
        EngineNode("node", "http://node", "token", 1), "staged-1"
    ) == {
        "id": "staged-1",
        "schema": "kinestack.staged-asset-deletion.v1",
        "status": "DELETED",
    }


def test_native_execution_and_output_receipts_are_normalized() -> None:
    attempt_id = "6203277f-701f-42ad-98f1-ec8d2aa2d28a"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/outputs"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "4f82f7c4-f89f-4942-93e9-1e803b04d107",
                            "role": "primary",
                            "media_type": "video/mp4",
                            "size_bytes": 123,
                            "sha256": "a" * 64,
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "attempt_id": attempt_id,
                "status": "RUNNING",
                "remote_acceptance": "ACCEPTED",
                "progress": 0.5,
                "phase": "GENERATING",
                "phase_detail": {},
                "workflow": {},
                "failure": None,
                "submitted_at": "2026-08-31T00:00:00Z",
                "started_at": "2026-08-31T00:00:01Z",
                "completed_at": None,
                "updated_at": "2026-08-31T00:00:02Z",
            },
            request=request,
        )

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    node = EngineNode("node", "http://node", "token", 1)
    execution = engine.inspect(node, attempt_id)
    output = engine.outputs(node, attempt_id)["items"][0]
    assert execution["schema"] == "kinestack.execution.v1"
    assert output["schema"] == "kinestack.output.v1"
    assert output["attempt_id"] == attempt_id


def test_archive_confirmation_uses_stable_idempotency_key() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        return httpx.Response(204, request=request)

    engine = HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))
    node = EngineNode("node", "http://node", "token", 1)
    first = engine.confirm_archive(node, "remote-7", idempotency_key="archive-op-7")
    second = engine.confirm_archive(node, "remote-7", idempotency_key="archive-op-7")
    assert seen == ["archive-op-7", "archive-op-7"]
    assert first == second == {
        "attempt_id": "remote-7",
        "schema": "kinestack.archive-confirmation.v1",
        "status": "CONFIRMED",
        "confirmed": True,
    }
