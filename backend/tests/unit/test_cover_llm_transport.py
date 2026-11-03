from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tubeflow_test.adapters.cover_llm_transport import (
    OpenAiCompatibleCoverLlmTransport,
    parse_json_object_content,
)
from tubeflow_test.application.cover_providers import CoverLlmChannelFailureError, CoverLlmResult
from tubeflow_test.domain.errors import DomainError

TOKEN = "cover-llm-secret-token"
MESSAGES = [{"role": "user", "content": "hello"}]


def _ok_response(
    request: httpx.Request, payload: dict[str, object] | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "req-1"},
        json={
            "id": "chatcmpl-1",
            "choices": [{"message": {"content": json.dumps(payload or {"ok": True})}}],
        },
        request=request,
    )


def _transport(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleeps: list[float] | None = None,
) -> OpenAiCompatibleCoverLlmTransport:
    return OpenAiCompatibleCoverLlmTransport(
        "https://grsai.dakka.com.cn/v1",
        TOKEN,
        120.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=(sleeps.append if sleeps is not None else lambda _seconds: None),
        rng=lambda: 0.5,  # 抖动固定为 0.125s
    )


def _call(transport: OpenAiCompatibleCoverLlmTransport, **overrides: Any) -> CoverLlmResult:
    kwargs: dict[str, Any] = {"model": "gpt-5.5", "temperature": 0.1, "messages": MESSAGES}
    kwargs.update(overrides)
    return transport.chat_json(**kwargs)


def test_transport_request_contract() -> None:
    seen: list[httpx.Request] = []
    response_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response = _ok_response(request, {"a": 1})
        response_bodies.append(response.content)
        return response

    transport = _transport(handler)
    result = transport.chat_json(model="gpt-5.5", temperature=0.1, messages=MESSAGES)

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-5.5"
    assert body["temperature"] == 0.1
    assert body["messages"] == MESSAGES
    assert body["response_format"] == {"type": "json_object"}
    assert result.payload == {"a": 1}
    assert result.model == "gpt-5.5"
    assert result.receipt == "req-1"
    assert result.raw_response_sha256 == hashlib.sha256(response_bodies[0]).hexdigest()
    transport.close()


def test_transport_receipt_falls_back_to_envelope_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-envelope",
                "choices": [{"message": {"content": '{"ok": true}'}}],
            },
            request=request,
        )

    result = _call(_transport(handler))
    assert result.receipt == "chatcmpl-envelope"


@pytest.mark.parametrize("status", (401, 403))
def test_transport_auth_errors_permanent_no_retry_no_fallback(status: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, request=request)

    transport = _transport(handler)
    with pytest.raises(DomainError) as captured:
        transport.chat_json(
            model="gpt-5.5",
            temperature=0.1,
            messages=MESSAGES,
            fallback_model="gemini-3.1-pro",
        )
    assert captured.value.code == f"cover_llm_provider_http_{status}"
    assert not isinstance(captured.value, CoverLlmChannelFailureError)
    assert len(seen) == 1
    assert TOKEN not in str(captured.value)


@pytest.mark.parametrize("status", (400, 404, 409, 410, 422))
def test_transport_other_4xx_permanent_no_retry_no_fallback(status: int) -> None:
    # 计划 §6：首期手动模式 400/404/409/410/422 一律如实报错，不换模型。
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, request=request)

    transport = _transport(handler)
    with pytest.raises(DomainError) as captured:
        transport.chat_json(
            model="gpt-5.5",
            temperature=0.1,
            messages=MESSAGES,
            fallback_model="gemini-3.1-pro",
        )
    assert captured.value.code == f"cover_llm_provider_http_{status}"
    assert not isinstance(captured.value, CoverLlmChannelFailureError)
    assert len(seen) == 1


def test_transport_retries_transient_statuses_within_budget() -> None:
    seen: list[httpx.Request] = []
    sleeps: list[float] = []
    statuses = [429, 500]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if statuses:
            return httpx.Response(statuses.pop(0), request=request)
        return _ok_response(request)

    result = _call(_transport(handler, sleeps=sleeps))

    assert len(seen) == 3
    assert sleeps == [1.125, 3.125]  # 退避 1s/3s + 固定抖动 0.125s
    assert result.payload == {"ok": True}


@pytest.mark.parametrize("status", (408, 425, 429, 500, 503))
def test_transport_transient_status_budget_exhausted(status: int) -> None:
    seen: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, request=request)

    with pytest.raises(CoverLlmChannelFailureError) as captured:
        _call(_transport(handler, sleeps=sleeps))
    assert captured.value.code == f"cover_llm_provider_http_{status}"
    assert len(seen) == 3  # 1 + 最多 2 次重试
    assert sleeps == [1.125, 3.125]


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    (("2", 2.125), ("120", 60.125), ("not-a-number", 1.125), ("-5", 1.125)),
)
def test_transport_retry_after_honored_capped_and_sanitized(
    retry_after: str, expected_delay: float
) -> None:
    sleeps: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": retry_after}, request=request)
        return _ok_response(request)

    _call(_transport(handler, sleeps=sleeps))
    assert sleeps == [expected_delay]


def test_transport_timeout_retried_then_channel_failure() -> None:
    seen: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(CoverLlmChannelFailureError) as captured:
        _call(_transport(handler, sleeps=sleeps))
    assert captured.value.code == "cover_llm_provider_timeout"
    assert len(seen) == 3
    assert TOKEN not in str(captured.value)


def test_transport_network_error_retried_then_channel_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(CoverLlmChannelFailureError) as captured:
        _call(_transport(handler))
    assert captured.value.code == "cover_llm_provider_network"
    assert calls == 3


def test_transport_non_json_content_resent_once_then_invalid() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json at all"}}]},
            request=request,
        )

    with pytest.raises(CoverLlmChannelFailureError) as captured:
        _call(_transport(handler))
    assert captured.value.code == "cover_llm_response_invalid"
    assert len(seen) == 2  # 同模型最多重发一轮


def test_transport_non_json_envelope_resent_once_then_success() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, content=b"<html>bad gateway page</html>", request=request)
        return _ok_response(request, {"recovered": True})

    result = _call(_transport(handler))
    assert calls == 2
    assert result.payload == {"recovered": True}


@pytest.mark.parametrize(
    "content",
    (
        '```json\n{"fenced": 1}\n```',
        '```\n{"fenced": 1}\n```',
        '前言说明 {"fenced": 1} 后记',
        '  {"fenced": 1}  ',
    ),
)
def test_transport_deterministic_content_parsing(content: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    result = _call(_transport(handler))
    assert result.payload == {"fenced": 1}


def test_transport_top_level_array_content_rejected() -> None:
    # 顶层数组不做花括号切片：[{...}] 不得被抠成对象。
    assert parse_json_object_content('[{"a": 1}]') is None
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '[{"a": 1}]'}}]},
            request=request,
        )

    with pytest.raises(CoverLlmChannelFailureError) as captured:
        _call(_transport(handler))

    assert captured.value.code == "cover_llm_response_invalid"
    assert calls == 2


def test_transport_http200_error_body_is_channel_failure_without_resend() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"error": {"message": "openai api error, please try again"}},
            request=request,
        )

    with pytest.raises(CoverLlmChannelFailureError) as captured:
        _call(_transport(handler))

    assert captured.value.code == "cover_llm_response_invalid"
    assert calls == 1


@pytest.mark.parametrize(
    "envelope",
    (
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": []},
        {"choices": [{"message": {}}]},
    ),
)
def test_transport_empty_content_is_channel_failure_without_resend(
    envelope: dict[str, object],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=envelope, request=request)

    with pytest.raises(CoverLlmChannelFailureError) as captured:
        _call(_transport(handler))

    assert captured.value.code == "cover_llm_response_invalid"
    assert calls == 1


def test_transport_fallback_switches_model_once_same_channel() -> None:
    seen_models: list[str] = []
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "gpt-5.5":
            return httpx.Response(503, request=request)
        return _ok_response(request, {"via": "fallback"})

    result = OpenAiCompatibleCoverLlmTransport(
        "https://grsai.dakka.com.cn/v1",
        TOKEN,
        120.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
        rng=lambda: 0.0,
    ).chat_json(
        model="gpt-5.5",
        temperature=0.1,
        messages=MESSAGES,
        fallback_model="gemini-3.1-pro",
    )

    assert seen_models == ["gpt-5.5", "gpt-5.5", "gpt-5.5", "gemini-3.1-pro"]
    assert all(r.url.path == "/v1/chat/completions" for r in seen_requests)
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in seen_requests)
    fallback_body = json.loads(seen_requests[-1].content)
    assert fallback_body["temperature"] == 0.1
    assert fallback_body["messages"] == MESSAGES
    assert result.model == "gemini-3.1-pro"
    assert result.payload == {"via": "fallback"}


def test_transport_fallback_only_once() -> None:
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return httpx.Response(500, request=request)

    transport = OpenAiCompatibleCoverLlmTransport(
        "https://grsai.dakka.com.cn/v1",
        TOKEN,
        120.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(CoverLlmChannelFailureError) as captured:
        transport.chat_json(
            model="gpt-5.5",
            temperature=0.1,
            messages=MESSAGES,
            fallback_model="gemini-3.1-pro",
        )
    assert captured.value.code == "cover_llm_provider_http_500"
    assert seen_models == ["gpt-5.5"] * 3 + ["gemini-3.1-pro"] * 3


@pytest.mark.parametrize("fallback", ("", "gpt-5.5", "   "))
def test_transport_no_fallback_when_missing_or_same_model(fallback: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    transport = OpenAiCompatibleCoverLlmTransport(
        "https://grsai.dakka.com.cn/v1",
        TOKEN,
        120.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(CoverLlmChannelFailureError):
        transport.chat_json(
            model="gpt-5.5", temperature=0.1, messages=MESSAGES, fallback_model=fallback
        )
    assert calls == 3


def test_transport_fallback_engaged_for_invalid_output() -> None:
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        content = "not json" if model == "gpt-5.5" else '{"fixed": true}'
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}, request=request
        )

    transport = OpenAiCompatibleCoverLlmTransport(
        "https://grsai.dakka.com.cn/v1",
        TOKEN,
        120.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _seconds: None,
    )
    result = transport.chat_json(
        model="gpt-5.5",
        temperature=0.1,
        messages=MESSAGES,
        fallback_model="gemini-3.1-pro",
    )
    assert seen_models == ["gpt-5.5", "gpt-5.5", "gemini-3.1-pro"]
    assert result.payload == {"fixed": True}
    assert result.model == "gemini-3.1-pro"


def test_parse_json_object_content_forms() -> None:
    assert parse_json_object_content('{"a": 1}') == {"a": 1}
    assert parse_json_object_content('```JSON\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object_content('noise {"a": 1} tail') == {"a": 1}
    assert parse_json_object_content("") is None
    assert parse_json_object_content("   ") is None
    assert parse_json_object_content(None) is None
    assert parse_json_object_content("not json") is None
    assert parse_json_object_content("[1, 2]") is None
    assert parse_json_object_content('{"a": ') is None
