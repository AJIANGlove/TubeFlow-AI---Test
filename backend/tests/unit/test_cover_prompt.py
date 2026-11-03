from __future__ import annotations

import json

import httpx
import pytest

from tubeflow_test.adapters.cover_prompt import DeepSeekCoverPromptProvider
from tubeflow_test.domain.errors import DomainError

TOKEN = "cover-prompt-secret-token"
MESSAGES = [
    {"role": "system", "content": "system"},
    {"role": "user", "content": "user"},
]


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-prompt",
            "choices": [{"message": {"content": json.dumps({"candidates": []})}}],
        },
        request=request,
    )


def _provider(handler) -> DeepSeekCoverPromptProvider:
    return DeepSeekCoverPromptProvider(
        "https://api.deepseek.com/v1",
        TOKEN,
        model="deepseek-v4-flash",
        fallback_model="deepseek-v4-pro",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
        rng=lambda: 0.0,
    )


@pytest.mark.parametrize("temperature", (0.9, 0.8, 1.0, 0.2))
def test_prompt_request_contract_with_caller_temperature(temperature: float) -> None:
    # 文字生成 0.9 / 画面两版 0.8、1.0 / 整改 0.8 / 评委 0.2 四处复用同一传输。
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response(request)

    result = _provider(handler).complete(MESSAGES, temperature=temperature)

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4-flash"
    assert body["temperature"] == temperature
    assert body["messages"] == MESSAGES
    assert body["response_format"] == {"type": "json_object"}
    assert result.payload == {"candidates": []}
    assert result.model == "deepseek-v4-flash"
    assert result.receipt == "chatcmpl-prompt"


def test_prompt_channel_failure_switches_to_fallback_model_once() -> None:
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "deepseek-v4-flash":
            return httpx.Response(429, request=request)
        return _ok_response(request)

    result = _provider(handler).complete(MESSAGES, temperature=0.9)

    assert seen_models == ["deepseek-v4-flash"] * 3 + ["deepseek-v4-pro"]
    assert result.model == "deepseek-v4-pro"


def test_prompt_auth_error_no_retry_no_fallback() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403, request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).complete(MESSAGES, temperature=0.2)
    assert captured.value.code == "cover_llm_provider_http_403"
    assert len(seen) == 1
    assert TOKEN not in str(captured.value)
