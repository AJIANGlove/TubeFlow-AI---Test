from __future__ import annotations

import hashlib
from collections.abc import Callable
from uuid import UUID

import httpx

from tubeflow_test.video_generation.engine import EngineNode, HttpKineStackEngine

ATTEMPT_ID = UUID("11111111-1111-4111-8111-111111111111")
STAGED_ID = UUID("22222222-2222-4222-8222-222222222222")
OUTPUT_ID = UUID("33333333-3333-4333-8333-333333333333")
MANIFEST_SHA256 = "a" * 64
OUTPUT_SHA256 = "b" * 64
TIMESTAMP = "2026-08-31T12:00:00+00:00"


def _node() -> EngineNode:
    return EngineNode(
        "node-a",
        "http://kinestack-node/engine/v1",
        "native-token",
        1,
        model_key="minimax-h3-community",
        model_version="2026-08-13-community-duration-v1",
        workflow_id="h3.community.reference-images-to-video",
        workflow_version="1.1.0-mvp",
        manifest_sha256=MANIFEST_SHA256,
    )


def _native_execution(status: str = "ACCEPTED") -> dict[str, object]:
    phase = {
        "ACCEPTED": "ACCEPTED",
        "RUNNING": "GENERATING",
        "SUCCEEDED": "SUCCEEDED",
        "FAILED": "FAILED",
        "CANCELLED": "CANCELLED",
    }[status]
    return {
        "attempt_id": str(ATTEMPT_ID),
        "status": status,
        "remote_acceptance": "ACCEPTED" if status != "ACCEPTED" else "NOT_SUBMITTED",
        "progress": 1.0 if status == "SUCCEEDED" else 0.0,
        "phase": phase,
        "phase_detail": {
            "reference_images_total": 2,
            "reference_images_uploaded": 0 if status == "ACCEPTED" else 2,
            "current_reference_ordinal": None,
            "upload_attempt": None,
        },
        "workflow": {
            "capability": "video.reference_images_to_video",
            "model_key": "minimax-h3-community",
            "model_version": "2026-08-13-community-duration-v1",
            "workflow_id": "h3.community.reference-images-to-video",
            "workflow_version": "1.1.0-mvp",
            "workflow_manifest_sha256": MANIFEST_SHA256,
        },
        "failure": None,
        "submitted_at": TIMESTAMP,
        "started_at": None,
        "completed_at": TIMESTAMP if status in {"SUCCEEDED", "FAILED", "CANCELLED"} else None,
        "updated_at": TIMESTAMP,
    }


def _engine(handler: Callable[[httpx.Request], httpx.Response]) -> HttpKineStackEngine:
    return HttpKineStackEngine(httpx.Client(transport=httpx.MockTransport(handler)))


def test_current_native_readiness_and_capabilities_shapes_are_accepted_without_instance_id(
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer native-token"
        seen.append(request.url.path)
        if request.url.path.endswith("/health/ready"):
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "engine_version": "community-h3-0.4.0",
                    "comfyui": "ready",
                    "storage": "ready",
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "engine_version": "community-h3-0.4.0",
                "generated_at": TIMESTAMP,
                "gpu_devices": [
                    {
                        "device_key": "remote-gpu-0",
                        "display_name": "RTX 4090 D 48GB",
                        "total_vram_bytes": 50_894_602_240,
                    }
                ],
                "items": [
                    {
                        "capability": "video.reference_images_to_video",
                        "model_key": "minimax-h3-community",
                        "model_version": "2026-08-13-community-duration-v1",
                        "workflow_id": "h3.community.reference-images-to-video",
                        "workflow_version": "1.1.0-mvp",
                        "workflow_manifest_sha256": MANIFEST_SHA256,
                        "status": "AVAILABLE",
                    }
                ],
            },
            request=request,
        )

    engine = _engine(handler)
    readiness = engine.readiness(_node())
    capabilities = engine.capabilities(_node())

    assert readiness == {
        "status": "ready",
        "engine_version": "community-h3-0.4.0",
        "comfyui": "ready",
        "storage": "ready",
    }
    assert "instance_id" not in readiness
    assert capabilities["items"][0]["workflow_manifest_sha256"] == MANIFEST_SHA256
    assert seen == ["/engine/v1/health/ready", "/engine/v1/capabilities"]


def test_native_staging_execution_output_and_empty_204_receipts_are_normalized() -> None:
    image = b"native-reference"
    image_sha256 = hashlib.sha256(image).hexdigest()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer native-token"
        path = request.url.path.removeprefix("/engine/v1")
        seen.append((request.method, path))
        if (request.method, path) == ("POST", "/staged-assets"):
            assert request.headers["Idempotency-Key"] == "stage-native-contract"
            return httpx.Response(
                201,
                json={
                    "id": str(STAGED_ID),
                    "sha256": image_sha256,
                    "size_bytes": len(image),
                    "media_type": "image/png",
                    "expires_at": TIMESTAMP,
                },
                request=request,
            )
        if (request.method, path) == ("DELETE", f"/staged-assets/{STAGED_ID}"):
            return httpx.Response(204, request=request)
        if (request.method, path) == ("POST", "/executions"):
            assert request.headers["Idempotency-Key"] == "submit-native-contract"
            return httpx.Response(202, json=_native_execution(), request=request)
        if (request.method, path) == ("GET", f"/executions/{ATTEMPT_ID}"):
            return httpx.Response(200, json=_native_execution("RUNNING"), request=request)
        if (request.method, path) == ("POST", f"/executions/{ATTEMPT_ID}/cancel"):
            return httpx.Response(200, json=_native_execution("CANCELLED"), request=request)
        if (request.method, path) == ("GET", f"/executions/{ATTEMPT_ID}/outputs"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": str(OUTPUT_ID),
                            "role": "primary",
                            "media_type": "video/mp4",
                            "size_bytes": 123,
                            "sha256": OUTPUT_SHA256,
                        }
                    ]
                },
                request=request,
            )
        if (
            request.method,
            path,
        ) == ("GET", f"/executions/{ATTEMPT_ID}/outputs/{OUTPUT_ID}/content"):
            return httpx.Response(200, content=b"native-mp4", request=request)
        if (
            request.method,
            path,
        ) == ("POST", f"/executions/{ATTEMPT_ID}/archive-confirmations"):
            return httpx.Response(204, request=request)
        raise AssertionError(f"unexpected native request: {request.method} {path}")

    engine = _engine(handler)
    node = _node()
    staged = engine.stage_asset(
        node,
        content=image,
        media_type="image/png",
        filename="reference.png",
        expected_sha256=image_sha256,
        idempotency_key="stage-native-contract",
    )
    deleted = engine.delete_staged_asset(node, str(STAGED_ID))
    submitted = engine.submit(
        node,
        idempotency_key="submit-native-contract",
        body={"attempt_id": str(ATTEMPT_ID)},
    )
    inspected = engine.inspect(node, ATTEMPT_ID)
    cancelled = engine.cancel(node, ATTEMPT_ID, idempotency_key="cancel-native-contract")
    outputs = engine.outputs(node, ATTEMPT_ID)
    content = engine.download(node, ATTEMPT_ID, str(OUTPUT_ID))
    archived = engine.confirm_archive(
        node,
        ATTEMPT_ID,
        idempotency_key="archive-native-contract",
    )

    assert staged["schema"] == "kinestack.staged-asset.v1"
    assert staged["status"] == "STORED"
    assert staged["id"] == str(STAGED_ID)
    assert deleted == {
        "id": str(STAGED_ID),
        "schema": "kinestack.staged-asset-deletion.v1",
        "status": "DELETED",
    }
    for execution in (submitted, inspected, cancelled):
        assert execution["schema"] == "kinestack.execution.v1"
        assert execution["attempt_id"] == str(ATTEMPT_ID)
        assert "phase_detail" in execution
    assert outputs["items"][0] == {
        "id": str(OUTPUT_ID),
        "role": "primary",
        "media_type": "video/mp4",
        "size_bytes": 123,
        "sha256": OUTPUT_SHA256,
        "schema": "kinestack.output.v1",
        "attempt_id": str(ATTEMPT_ID),
    }
    assert content == b"native-mp4"
    assert archived == {
        "schema": "kinestack.archive-confirmation.v1",
        "status": "CONFIRMED",
        "attempt_id": str(ATTEMPT_ID),
        "confirmed": True,
    }
    assert seen == [
        ("POST", "/staged-assets"),
        ("DELETE", f"/staged-assets/{STAGED_ID}"),
        ("POST", "/executions"),
        ("GET", f"/executions/{ATTEMPT_ID}"),
        ("POST", f"/executions/{ATTEMPT_ID}/cancel"),
        ("GET", f"/executions/{ATTEMPT_ID}/outputs"),
        ("GET", f"/executions/{ATTEMPT_ID}/outputs/{OUTPUT_ID}/content"),
        ("POST", f"/executions/{ATTEMPT_ID}/archive-confirmations"),
    ]
