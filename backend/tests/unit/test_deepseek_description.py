from __future__ import annotations

import json

import httpx
import pytest

from tubeflow_test.adapters.deepseek_description import DeepSeekDescriptionProvider
from tubeflow_test.domain.errors import DomainError


def _provider(handler) -> DeepSeekDescriptionProvider:
    return DeepSeekDescriptionProvider(
        "https://api.deepseek.com/v1",
        "token",
        model="deepseek-v4-flash",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_description_provider_sends_json_chat_contract_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"x-request-id": "desc-req-1"},
            json={
                "choices": [
                    {"message": {"content": json.dumps({"hook": "钩子"}, ensure_ascii=False)}}
                ]
            },
            request=request,
        )

    provider = _provider(handler)
    result = provider.complete_json(
        [{"role": "user", "content": "生成简介"}], temperature=0.4
    )
    assert calls == 1
    assert result.payload == {"hook": "钩子"}
    assert result.receipt == "desc-req-1"
    assert result.model == "deepseek-v4-flash"
    provider.close()


def test_description_provider_never_retries_transport_or_5xx() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    provider = _provider(handler)
    with pytest.raises(DomainError) as raised:
        provider.complete_json([{"role": "user", "content": "x"}], temperature=0.4)
    assert raised.value.code == "description_provider_unavailable"
    assert calls == 1
    provider.close()


def test_description_provider_marks_bad_json_as_repairable_output_error() -> None:
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json"}}]},
            request=request,
        )
    )
    with pytest.raises(DomainError) as raised:
        provider.complete_json([{"role": "user", "content": "x"}], temperature=0.4)
    assert raised.value.code == "description_output_invalid"
    provider.close()
