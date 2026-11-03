from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from PIL import Image
from test_storyboard_sql_repository import generation

from tubeflow_test.adapters.pix_grid_sheet import PixGridSheetImageProvider
from tubeflow_test.application.storyboards import (
    GridSheetReference,
    ProviderChannelUnavailableError,
    ProviderReconcileUnsupportedError,
    ProviderSubmitRejectedError,
    ProviderSubmitServerError,
    ProviderSubmitThrottledError,
)
from tubeflow_test.domain.storyboards import GenerationStatus


def png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 36), "white").save(stream, "PNG")
    return stream.getvalue()


def provider_with(handler, token: str = "sk-test") -> PixGridSheetImageProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PixGridSheetImageProvider("https://api.lk888.ai", token, client=client)


@pytest.mark.parametrize(
    ("model", "parameters", "expected"),
    [
        (
            "tt-image-2.5",
            {
                "version": "flare",
                "aspect_ratio": "16:9",
                "resolution": "2K",
                "quality": "high",
                "background": "opaque",
            },
            {
                "version": "flare",
                "aspect_ratio": "16:9",
                "resolution": "2K",
                "quality": "high",
                "background": "opaque",
            },
        ),
        (
            "tt-image-2",
            {"size": "1920x1088", "quality": "high"},
            {"size": "1920x1088", "quality": "high", "n": 1},
        ),
        (
            "banana-2.1",
            {"aspectRatio": "16:9", "imageSize": "2K", "thinkingLevel": "minimal"},
            {"aspectRatio": "16:9", "imageSize": "2K", "thinkingLevel": "minimal", "n": 1},
        ),
    ],
)
def test_pix_submit_uses_each_models_documented_protocol(model, parameters, expected) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/media/generate"
        assert request.headers["Authorization"] == "Bearer sk-test"
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json={"code": 200, "data": {"task_id": 123456}})

    item = replace(
        generation(1, GenerationStatus.INTENT_PERSISTED), model=model, parameters=parameters
    )
    submission = provider_with(handler).submit_grid_sheet(item)
    assert submission.task_id == "123456"
    assert captured[0]["model"] == model
    assert captured[0]["params"] == expected
    if model == "banana-2.1":
        assert "web_search" not in captured[0]["params"]


def test_pix_submit_inlines_ordered_reference_images() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(200, json={"code": 200, "data": {"task_id": "pix-1"}})

    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    references = (
        GridSheetReference(1, "image/png", "a" * 64, b"first"),
        GridSheetReference(2, "image/jpeg", "b" * 64, b"second"),
    )
    provider_with(handler).submit_grid_sheet(item, references)
    images = captured[0]["params"]["images"]
    assert images == [
        "data:image/png;base64," + base64.b64encode(b"first").decode("ascii"),
        "data:image/jpeg;base64," + base64.b64encode(b"second").decode("ascii"),
    ]


def test_pix_poll_requires_final_success_and_downloads_same_result(tmp_path: Path) -> None:
    payload = png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/media/status":
            assert request.url.params["task_id"] == "pix-1"
            return httpx.Response(
                200,
                json={
                    "task_id": "pix-1",
                    "state": "success",
                    "is_final": True,
                    "result_url": "https://cdn.test/result.png",
                    "error": "",
                },
            )
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=payload)
        raise AssertionError(f"unexpected request: {request.url}")

    provider = provider_with(handler)
    poll = provider.poll("pix-1")
    assert poll.status is GenerationStatus.SUCCEEDED
    assert poll.download_token == "https://cdn.test/result.png"
    provider.download(poll.download_token or "", tmp_path / "result.png")
    assert (tmp_path / "result.png").read_bytes() == payload


def test_pix_poll_running_failed_and_invalid_final_combinations() -> None:
    responses = iter(
        [
            {"state": "running", "is_final": False, "result_url": ""},
            {"state": "failed", "is_final": True, "error": "generate image failed"},
        ]
    )
    provider = provider_with(lambda request: httpx.Response(200, json=next(responses)))
    assert provider.poll("pix-1").status is GenerationStatus.RUNNING
    failed = provider.poll("pix-1")
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_code == "generate image failed"
    invalid = provider_with(
        lambda request: httpx.Response(200, json={"state": "success", "is_final": False})
    )
    assert invalid.poll("pix-1").status is GenerationStatus.RUNNING
    contradictory = provider_with(
        lambda request: httpx.Response(200, json={"state": "running", "is_final": True})
    )
    with pytest.raises(ValueError, match="non-terminal"):
        contradictory.poll("pix-1")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(401, json={"error": {"type": "invalid_api_key"}}),
            ProviderChannelUnavailableError,
        ),
        (
            httpx.Response(402, json={"error": {"type": "insufficient_balance"}}),
            ProviderChannelUnavailableError,
        ),
        (httpx.Response(429, headers={"Retry-After": "7"}), ProviderSubmitThrottledError),
        (httpx.Response(503), ProviderSubmitServerError),
        (
            httpx.Response(400, json={"error": {"type": "invalid_request_error"}}),
            ProviderSubmitRejectedError,
        ),
    ],
)
def test_pix_submit_error_classification(response, expected) -> None:
    provider = provider_with(lambda request: response)
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    with pytest.raises(expected):
        provider.submit_grid_sheet(item)


def test_pix_5xx_with_task_id_is_recovered_not_resubmitted() -> None:
    response = httpx.Response(503, json={"code": 503, "data": {"task_id": "existing-task"}})
    provider = provider_with(lambda request: response)
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    with pytest.raises(ProviderSubmitServerError) as captured:
        provider.submit_grid_sheet(item)
    assert captured.value.submission is not None
    assert captured.value.submission.task_id == "existing-task"


def test_pix_429_with_task_id_preserves_original_submission() -> None:
    provider = provider_with(
        lambda request: httpx.Response(429, json={"code": 429, "data": {"task_id": "existing-429"}})
    )
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    with pytest.raises(ProviderSubmitThrottledError) as captured:
        provider.submit_grid_sheet(item)
    assert captured.value.submission is not None
    assert captured.value.submission.task_id == "existing-429"


def test_pix_business_error_with_task_id_returns_original_submission() -> None:
    provider = provider_with(
        lambda request: httpx.Response(200, json={"code": 500, "data": {"task_id": "existing-500"}})
    )
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    submission = provider.submit_grid_sheet(item)
    assert submission.task_id == "existing-500"


def test_pix_business_moderation_is_not_a_channel_failure() -> None:
    provider = provider_with(
        lambda request: httpx.Response(200, json={"code": 403, "msg": "output_moderation"})
    )
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    with pytest.raises(ProviderSubmitRejectedError) as captured:
        provider.submit_grid_sheet(item)
    assert captured.value.failure_code == "output_moderation"


def test_pix_nested_moderation_type_precedes_numeric_error_code() -> None:
    provider = provider_with(
        lambda request: httpx.Response(
            403, json={"error": {"code": 403, "type": "output_moderation"}}
        )
    )
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    with pytest.raises(ProviderSubmitRejectedError) as captured:
        provider.submit_grid_sheet(item)
    assert captured.value.failure_code == "output_moderation"


def test_pix_missing_status_code_with_task_id_returns_original_submission() -> None:
    provider = provider_with(
        lambda request: httpx.Response(200, json={"data": {"task_id": "no-code"}})
    )
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    submission = provider.submit_grid_sheet(item)
    assert submission.task_id == "no-code"


def test_pix_non_object_submit_response_is_uncertain() -> None:
    provider = provider_with(lambda request: httpx.Response(200, json=[]))
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    with pytest.raises(ValueError, match="non-object"):
        provider.submit_grid_sheet(item)


def test_pix_poll_redacts_signed_urls_from_failure_text() -> None:
    provider = provider_with(
        lambda request: httpx.Response(
            200,
            json={
                "state": "failed",
                "is_final": True,
                "error": "https://cdn.test/result.png?signature=DO_NOT_EXPOSE",
            },
        )
    )
    failure = provider.poll("pix-1").failure_code
    assert failure is not None and "DO_NOT_EXPOSE" not in failure
    assert "<redacted-url>" in failure


def test_pix_missing_task_id_is_uncertain_and_empty_token_stops_before_http() -> None:
    provider = provider_with(lambda request: httpx.Response(200, json={"code": 200, "data": {}}))
    item = replace(generation(1, GenerationStatus.INTENT_PERSISTED), model="tt-image-2")
    with pytest.raises(ValueError, match="no task id"):
        provider.submit_grid_sheet(item)
    no_token = provider_with(
        lambda request: (_ for _ in ()).throw(AssertionError("unexpected HTTP")), token=""
    )
    with pytest.raises(ProviderChannelUnavailableError, match="not configured"):
        no_token.submit_grid_sheet(item)


def test_pix_reconcile_is_unsupported_without_http() -> None:
    provider = provider_with(
        lambda request: (_ for _ in ()).throw(AssertionError("unexpected HTTP"))
    )
    with pytest.raises(ProviderReconcileUnsupportedError):
        provider.reconcile("any-key")
