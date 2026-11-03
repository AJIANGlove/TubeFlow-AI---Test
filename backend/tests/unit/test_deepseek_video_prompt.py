from __future__ import annotations

import json

import httpx
import pytest

from tubeflow_test.adapters.deepseek_video_prompt import DeepSeekVideoPromptProvider
from tubeflow_test.video_prompt.provider import (
    PermanentVideoPromptProviderError,
    RetryableVideoPromptProviderError,
    VideoPromptProviderResponseError,
)


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "req-1"},
        json={
            "id": "chatcmpl-1",
            "choices": [
                {"message": {"content": json.dumps({"prompt": "subject_definitions:\n..."})}}
            ],
        },
        request=request,
    )


def test_deepseek_provider_request_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response(request)

    provider = DeepSeekVideoPromptProvider(
        "https://api.deepseek.com/v1",
        "token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.compile(
        system_prompt="system",
        user_payload={"json_task": "compile_minimax_h3_video_prompt"},
        model="deepseek-v4-flash",
        idempotency_key="key-1",
    )
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer token"
    assert request.headers["Idempotency-Key"] == "key-1"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 4096
    assert body["thinking"] == {"type": "disabled"}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert json.loads(body["messages"][1]["content"])["json_task"] == (
        "compile_minimax_h3_video_prompt"
    )
    assert result.text.startswith("subject_definitions:")
    assert result.receipt == "req-1"
    assert len(result.raw_response_sha256) == 64
    provider.close()


def test_deepseek_provider_classifies_http_errors() -> None:
    # §15.14：401/403 既定不重试；400/404/409/410/422 为平台偶发抖动，可重试。
    for status in (401, 403):
        provider = DeepSeekVideoPromptProvider(
            "https://api.deepseek.com/v1",
            "token",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, code=status: httpx.Response(code, request=request)
                )
            ),
        )
        with pytest.raises(PermanentVideoPromptProviderError, match=f"provider_http_{status}"):
            provider.compile(
                system_prompt="s", user_payload={}, model="m", idempotency_key="k"
            )
    for status in (400, 404, 409, 410, 422):
        provider = DeepSeekVideoPromptProvider(
            "https://api.deepseek.com/v1",
            "token",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, code=status: httpx.Response(code, request=request)
                )
            ),
        )
        with pytest.raises(RetryableVideoPromptProviderError, match=f"provider_http_{status}"):
            provider.compile(
                system_prompt="s", user_payload={}, model="m", idempotency_key="k"
            )
    provider = DeepSeekVideoPromptProvider(
        "https://api.deepseek.com/v1",
        "token",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429, headers={"retry-after": "2"}, request=request
                )
            )
        ),
    )
    with pytest.raises(RetryableVideoPromptProviderError) as captured:
        provider.compile(system_prompt="s", user_payload={}, model="m", idempotency_key="k")
    assert captured.value.retry_after_seconds == 2.0


def test_deepseek_provider_rejects_bad_payloads() -> None:
    cases = [
        httpx.Response(200, content=b"not-json", request=httpx.Request("POST", "http://x")),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json"}}]},
            request=httpx.Request("POST", "http://x"),
        ),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"wrong": 1})}}]},
            request=httpx.Request("POST", "http://x"),
        ),
    ]
    for response in cases:
        provider = DeepSeekVideoPromptProvider(
            "https://api.deepseek.com/v1",
            "token",
            client=httpx.Client(
                transport=httpx.MockTransport(lambda request, r=response: r)
            ),
        )
        with pytest.raises(VideoPromptProviderResponseError):
            provider.compile(system_prompt="s", user_payload={}, model="m", idempotency_key="k")
