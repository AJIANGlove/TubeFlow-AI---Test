from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from PIL import Image
from test_storyboard_sql_repository import generation

from tubeflow_test.adapters.grsai_grid_sheet import GrsaiGridSheetImageProvider
from tubeflow_test.application.storyboards import (
    GridSheetReference,
    ProviderChannelUnavailableError,
    ProviderReconcileUnsupportedError,
    ProviderSubmitNotDeliveredError,
    ProviderSubmitRejectedError,
    ProviderSubmitServerError,
    ProviderSubmitThrottledError,
)
from tubeflow_test.domain.storyboards import GenerationStatus


def png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 36), "white").save(stream, "PNG")
    return stream.getvalue()


def provider_with(handler) -> GrsaiGridSheetImageProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GrsaiGridSheetImageProvider("https://grsai.test", "sk-test", client=client)


def test_grsai_submit_uploads_references_then_creates_task(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/client/resource/newUploadTokenZH":
            seen.append(("token", request.read().decode()))
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "token": "upload-token",
                        "key": "ref-1.png",
                        "url": "https://up.test",
                        "domain": "https://files.test",
                    },
                },
            )
        if request.url.host == "up.test":
            seen.append(("upload", request.headers["Content-Type"]))
            assert b"private-image-bytes" in request.read()
            return httpx.Response(200, json={"key": "ref-1.png"})
        if path == "/v1/draw/completions":
            payload = json.loads(request.read())
            seen.append(("submit", json.dumps(payload, ensure_ascii=False)))
            return httpx.Response(200, json={"code": 0, "data": {"id": "task-1"}})
        raise AssertionError(f"unexpected request: {request.url}")

    provider = provider_with(handler)
    item = generation(1, GenerationStatus.INTENT_PERSISTED)
    submission = provider.submit_grid_sheet(
        item, (GridSheetReference(1, "image/png", "a" * 64, b"private-image-bytes"),)
    )

    assert submission.task_id == "task-1"
    assert seen[0][0] == "token" and seen[1][0] == "upload" and seen[2][0] == "submit"
    submit_payload = json.loads(seen[2][1])
    assert submit_payload["urls"] == ["https://files.test/ref-1.png"]
    assert submit_payload["webHook"] == "-1"
    assert submit_payload["model"] == item.model
    assert submit_payload["prompt"] == item.prompt


def _submitted_body(parameters: dict[str, str]) -> dict[str, object]:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json={"code": 0, "data": {"id": "task-1"}})

    provider = provider_with(handler)
    item = generation(1, GenerationStatus.INTENT_PERSISTED)
    object.__setattr__(item, "parameters", parameters)
    provider.submit_grid_sheet(item)
    return captured[0]


def test_grsai_submit_uses_documented_aspect_ratio_pixels_and_quality() -> None:
    body = _submitted_body({"aspect_ratio": "9:16"})
    assert body["aspectRatio"] == "941x1672"
    assert body["quality"] == "high"
    assert "size" not in body


def test_grsai_submit_maps_landscape_to_official_1k_pixels() -> None:
    body = _submitted_body({"aspect_ratio": "16:9"})
    assert body["aspectRatio"] == "1672x941"


def test_grsai_submit_honours_configured_quality() -> None:
    body = _submitted_body({"aspect_ratio": "16:9", "quality": "medium"})
    assert body["quality"] == "medium"


def test_grsai_submit_rejects_unknown_aspect_ratio() -> None:
    provider = provider_with(lambda request: httpx.Response(500))
    item = generation(1, GenerationStatus.INTENT_PERSISTED)
    object.__setattr__(item, "parameters", {"aspect_ratio": "1:1"})
    with pytest.raises(ValueError, match="unsupported aspect ratio"):
        provider.submit_grid_sheet(item)


def test_grsai_connect_error_is_classified_as_not_delivered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    provider = provider_with(handler)
    with pytest.raises(ProviderSubmitNotDeliveredError):
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))


def test_grsai_insufficient_balance_is_classified_as_channel_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 1001, "msg": "insufficient balance"})

    provider = provider_with(handler)
    with pytest.raises(ProviderChannelUnavailableError) as captured:
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))
    assert captured.value.failure_code == "provider_quota_exhausted"


def test_grsai_poll_maps_statuses_and_caches_download(tmp_path: Path) -> None:
    image = png_bytes()
    digest = hashlib.sha256(image).hexdigest()
    states = iter(("running", "failed", "succeeded"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/draw/result":
            state = next(states)
            data: dict[str, object] = {"id": "task-1", "status": state, "progress": 42}
            if state == "failed":
                data["failure_reason"] = "content_blocked"
            if state == "succeeded":
                data["results"] = [{"url": "https://cdn.test/grid.png"}]
            return httpx.Response(200, json={"code": 0, "data": data})
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=image)
        raise AssertionError(f"unexpected request: {request.url}")

    provider = provider_with(handler)
    running = provider.poll("task-1")
    assert running.status is GenerationStatus.RUNNING
    failed = provider.poll("task-1")
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_code == "content_blocked"
    succeeded = provider.poll("task-1")
    assert succeeded.status is GenerationStatus.SUCCEEDED
    assert succeeded.download_token == "https://cdn.test/grid.png"
    assert succeeded.expected_sha256 == digest
    destination = tmp_path / "grid.image"
    provider.download("https://cdn.test/grid.png", destination)
    assert destination.read_bytes() == image


@pytest.mark.parametrize("model", ["nano-banana-2", "nano-banana-pro"])
def test_grsai_nano_models_use_native_endpoint_and_2k_parameters(model: str) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/draw/nano-banana"
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json={"code": 0, "data": {"id": "nano-task"}})

    item = replace(
        generation(1, GenerationStatus.INTENT_PERSISTED),
        model=model,
        parameters={"aspect_ratio": "16:9", "imageSize": "2K"},
    )
    submission = provider_with(handler).submit_grid_sheet(item)
    assert submission.task_id == "nano-task"
    body = captured[0]
    assert body["model"] == model
    assert body["aspectRatio"] == "16:9"
    assert body["imageSize"] == "2K"
    assert "quality" not in body
    assert body["webHook"] == "-1"


def test_grsai_poll_preserves_the_specific_provider_error() -> None:
    provider = provider_with(
        lambda request: httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": "task-1",
                    "status": "failed",
                    "error": "get image url failed",
                    "failure_reason": "error",
                },
            },
        )
    )
    failed = provider.poll("task-1")
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_code == "get image url failed"


@pytest.mark.parametrize("status", [401, 402, 403])
def test_grsai_auth_and_balance_errors_disable_the_channel(status: int) -> None:
    provider = provider_with(lambda request: httpx.Response(status))
    with pytest.raises(ProviderChannelUnavailableError):
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))


def test_grsai_5xx_with_task_id_returns_recoverable_submission() -> None:
    provider = provider_with(
        lambda request: httpx.Response(503, json={"code": 503, "data": {"id": "existing"}})
    )
    with pytest.raises(ProviderSubmitServerError) as captured:
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))
    assert captured.value.submission is not None
    assert captured.value.submission.task_id == "existing"


def test_grsai_429_with_task_id_preserves_original_submission() -> None:
    provider = provider_with(
        lambda request: httpx.Response(429, json={"code": 429, "data": {"id": "existing-429"}})
    )
    with pytest.raises(ProviderSubmitThrottledError) as captured:
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))
    assert captured.value.submission is not None
    assert captured.value.submission.task_id == "existing-429"


def test_grsai_business_error_with_task_id_returns_original_submission() -> None:
    provider = provider_with(
        lambda request: httpx.Response(200, json={"code": 500, "data": {"id": "existing-500"}})
    )
    submission = provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))
    assert submission.task_id == "existing-500"


def test_grsai_business_moderation_is_not_a_channel_failure() -> None:
    provider = provider_with(
        lambda request: httpx.Response(200, json={"code": 403, "msg": "output_moderation"})
    )
    with pytest.raises(ProviderSubmitRejectedError) as captured:
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))
    assert captured.value.failure_code == "output_moderation"


def test_grsai_nested_moderation_type_precedes_numeric_error_code() -> None:
    provider = provider_with(
        lambda request: httpx.Response(
            403, json={"error": {"code": 403, "type": "output_moderation"}}
        )
    )
    with pytest.raises(ProviderSubmitRejectedError) as captured:
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))
    assert captured.value.failure_code == "output_moderation"


def test_grsai_nested_poll_moderation_type_is_preserved() -> None:
    provider = provider_with(
        lambda request: httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": "task-1",
                    "status": "failed",
                    "error": {"code": 403, "type": "output_moderation"},
                },
            },
        )
    )
    assert provider.poll("task-1").failure_code == "output_moderation"


def test_grsai_missing_status_code_with_task_id_returns_original_submission() -> None:
    provider = provider_with(lambda request: httpx.Response(200, json={"data": {"id": "no-code"}})
    )
    submission = provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))
    assert submission.task_id == "no-code"


def test_grsai_non_object_submit_response_is_uncertain() -> None:
    provider = provider_with(lambda request: httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="non-object"):
        provider.submit_grid_sheet(generation(1, GenerationStatus.INTENT_PERSISTED))


def test_grsai_poll_redacts_signed_urls_from_failure_text() -> None:
    provider = provider_with(
        lambda request: httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": "task-1",
                    "status": "failed",
                    "error": "https://cdn.test/result.png?signature=DO_NOT_EXPOSE",
                },
            },
        )
    )
    failure = provider.poll("task-1").failure_code
    assert failure is not None and "DO_NOT_EXPOSE" not in failure
    assert "<redacted-url>" in failure


def test_grsai_reconcile_is_unsupported_without_http() -> None:
    provider = provider_with(
        lambda request: (_ for _ in ()).throw(AssertionError("unexpected HTTP"))
    )
    with pytest.raises(ProviderReconcileUnsupportedError):
        provider.reconcile("any-key")
