from __future__ import annotations

import json

import httpx
import pytest
from test_director_generation import build

from tubeflow_test.adapters.director_model import (
    DirectorStructuredResponseError,
    OpenAiCompatibleDirectorModel,
)
from tubeflow_test.application.director_generation import (
    PermanentDirectorModelError,
    RetryableDirectorModelError,
)


def test_openai_compatible_director_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        content = json.dumps({"shots": []})
        return httpx.Response(
            200,
            headers={"x-request-id": "receipt"},
            json={"id": "response", "choices": [{"message": {"content": content}}]},
        )

    service, _, owner, group = build()
    attempt = service.create_intent(
        owner_id=owner, group_id=group, expected_draft_revision_id=None, idempotency_key="key"
    )
    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        "token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = adapter.generate(attempt)
    assert seen[0].headers["Idempotency-Key"] == attempt.provider_idempotency_key
    assert seen[0].url.path == "/v1/chat/completions"
    request_body = json.loads(seen[0].content)
    schema = request_body["response_format"]
    assert schema["type"] == "json_schema"
    assert schema["json_schema"]["strict"] is True
    assert result.receipt == "receipt"
    assert len(result.raw_response_sha256) == 64


def test_director_adapter_classifies_4xx_per_decision_15_14() -> None:
    service, _, owner, group = build()
    attempt = service.create_intent(
        owner_id=owner, group_id=group, expected_draft_revision_id=None, idempotency_key="key"
    )
    # §15.14：401/403（鉴权/权限）既定不重试；其余 4xx 为平台偶发抖动，可重试。
    for status in (401, 403):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request, code=status: httpx.Response(code, request=request)
            )
        )
        adapter = OpenAiCompatibleDirectorModel("https://director.test/v1", "token", client=client)
        with pytest.raises(PermanentDirectorModelError, match=f"provider_http_{status}"):
            adapter.generate(attempt)
    for status in (400, 404, 409, 410, 422):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request, code=status: httpx.Response(code, request=request)
            )
        )
        adapter = OpenAiCompatibleDirectorModel("https://director.test/v1", "token", client=client)
        with pytest.raises(RetryableDirectorModelError, match=f"provider_http_{status}"):
            adapter.generate(attempt)


def test_director_adapter_strips_think_preamble_before_json_parse() -> None:
    service, _, owner, group = build()
    attempt = service.create_intent(
        owner_id=owner,
        group_id=group,
        expected_draft_revision_id=None,
        idempotency_key="key",
    )
    content = "<think>planning notes with {braces}</think>" + json.dumps({"shots": []})
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": content}}]}
            )
        )
    )
    adapter = OpenAiCompatibleDirectorModel("https://director.test/v1", "token", client=client)
    result = adapter.generate(attempt)
    assert result.payload == {"shots": []}


def test_director_adapter_still_rejects_genuinely_invalid_content() -> None:
    service, _, owner, group = build()
    attempt = service.create_intent(
        owner_id=owner, group_id=group, expected_draft_revision_id=None, idempotency_key="key"
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": "definitely not json"}}]}
            )
        )
    )
    adapter = OpenAiCompatibleDirectorModel("https://director.test/v1", "token", client=client)
    with pytest.raises(DirectorStructuredResponseError) as captured:
        adapter.generate(attempt)
    assert captured.value.code == "director_response_content_json_invalid"
