from __future__ import annotations

import base64
import json

import httpx
import pytest

from tubeflow_test.adapters.cover_ref_analysis import GrsaiCoverRefAnalysisProvider
from tubeflow_test.domain.cover import build_ref_analysis_messages
from tubeflow_test.domain.errors import DomainError

TOKEN = "cover-analysis-secret-token"
PNG_BASE64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
JPEG_BASE64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 32).decode()
WEBP_BASE64 = base64.b64encode(b"RIFF" + b"0" * 32).decode()


def _messages(image_base64: str = PNG_BASE64) -> list[dict[str, object]]:
    return build_ref_analysis_messages(
        {"image_base64": image_base64, "image_url": ""}, ["subject", "lighting::光源"]
    )


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "req-analysis"},
        json={
            "id": "chatcmpl-analysis",
            "choices": [
                {"message": {"content": json.dumps({"subject": {"服装": "红色外套"}})}}
            ],
        },
        request=request,
    )


def _provider(
    handler, *, fallback_model: str = "gemini-3.1-pro"
) -> GrsaiCoverRefAnalysisProvider:
    return GrsaiCoverRefAnalysisProvider(
        "https://grsai.dakka.com.cn/v1",
        TOKEN,
        model="gpt-5.5",
        fallback_model=fallback_model,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
        rng=lambda: 0.0,
    )


def test_analysis_request_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response(request)

    result = _provider(handler).analyze(_messages())

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-5.5"
    assert body["temperature"] == 0.1
    assert body["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    user_content = body["messages"][1]["content"]
    assert user_content[0]["type"] == "text"
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["detail"] == "high"
    assert result.payload == {"subject": {"服装": "红色外套"}}
    assert result.model == "gpt-5.5"
    assert result.receipt == "req-analysis"


@pytest.mark.parametrize(
    ("image_base64", "expected_mime"),
    (
        (JPEG_BASE64, "image/jpeg"),
        (WEBP_BASE64, "image/webp"),
        (PNG_BASE64, "image/png"),
    ),
)
def test_analysis_request_image_mime_from_magic_bytes(
    image_base64: str, expected_mime: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response(request)

    _provider(handler).analyze(_messages(image_base64))

    body = json.loads(seen[0].content)
    data_url = body["messages"][1]["content"][1]["image_url"]["url"]
    assert data_url.startswith(f"data:{expected_mime};base64,")


def test_analysis_channel_failure_switches_to_fallback_model_once() -> None:
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "gpt-5.5":
            return httpx.Response(503, request=request)
        return _ok_response(request)

    result = _provider(handler).analyze(_messages())

    assert seen_models == ["gpt-5.5", "gpt-5.5", "gpt-5.5", "gemini-3.1-pro"]
    assert result.model == "gemini-3.1-pro"
    assert result.payload == {"subject": {"服装": "红色外套"}}


def test_analysis_auth_error_no_retry_no_fallback() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).analyze(_messages())
    assert captured.value.code == "cover_llm_provider_http_401"
    assert len(seen) == 1
    assert TOKEN not in str(captured.value)
