from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx
import pytest
from test_director_generation import build
from test_project_director import context, service

from tubeflow_test.adapters.director_model import OpenAiCompatibleDirectorModel
from tubeflow_test.application.director_generation import (
    DirectorResponseDiagnostics,
    DirectorStructuredResponseError,
    PermanentDirectorModelError,
    RetryableDirectorModelError,
)
from tubeflow_test.domain.project_director import ProjectDirectorTrigger

DUMMY_TOKEN = "unit-test-token"


@dataclass(frozen=True, slots=True)
class ProjectDirectorUnitAttempt:
    provider_idempotency_key: str
    model: str
    parameters: dict[str, str | int | float | bool]
    prompt_content: str


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def _valid_response_bytes(payload: dict[str, object], *, response_id: str = "response-id") -> bytes:
    return json.dumps(
        {
            "id": response_id,
            "choices": [{"message": {"content": json.dumps(payload)}}],
        },
        separators=(",", ":"),
    ).encode()


def test_group_attempt_sends_openai_compatible_structured_request() -> None:
    service, _, owner_id, group_id = build()
    attempt = service.create_intent(
        owner_id=owner_id,
        group_id=group_id,
        expected_draft_revision_id=None,
        idempotency_key="group-request",
    )
    payload = {"shots": []}
    raw_response = _valid_response_bytes(payload)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-receipt"},
            content=raw_response,
            request=request,
        )

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1/",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        result = adapter.generate(attempt)
    finally:
        adapter.close()

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://director.test/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {DUMMY_TOKEN}"
    assert request.headers["Idempotency-Key"] == attempt.provider_idempotency_key

    body = json.loads(request.content)
    assert body["model"] == attempt.model
    assert body["stream"] is False
    assert {key: body[key] for key in attempt.parameters} == attempt.parameters
    assert body["messages"] == [{"role": "system", "content": attempt.prompt_content}]
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "director_plan_v1"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["shots"]
    assert schema["properties"]["shots"]["minItems"] == 1
    assert schema["properties"]["shots"]["maxItems"] == 5
    assert schema["properties"]["shots"]["items"]["properties"]["sentence_ids"]["items"] == {
        "type": "string",
        "format": "uuid",
    }

    assert result.payload == payload
    assert result.receipt == "provider-receipt"
    assert result.raw_response_sha256 == hashlib.sha256(raw_response).hexdigest()


def test_project_unit_attempt_uses_locale_semantics_and_dynamic_sentence_contract() -> None:
    value = context(3)
    director, _repository, _model, _clock = service(value)
    _run, units = director.prepare_run(
        context=value,
        idempotency_key="provider-schema",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
    )
    attempt = units[1]
    raw_response = _valid_response_bytes({"shots": []}, response_id="fallback-receipt")
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, content=raw_response, request=request)

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        result = adapter.generate(attempt)
    finally:
        adapter.close()

    assert seen_body["model"] == attempt.model
    assert seen_body["stream"] is False
    assert seen_body["temperature"] == 0.2
    assert seen_body["messages"] == [{"role": "system", "content": attempt.prompt_content}]
    response_format = seen_body["response_format"]
    assert isinstance(response_format, dict)
    contract = response_format["json_schema"]
    assert isinstance(contract, dict)
    assert contract["name"] == "project_director_unit_v4"
    schema = contract["schema"]
    assert isinstance(schema, dict)
    assert schema["required"] == ["output_locale", "shots"]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["output_locale"] == {"type": "string", "const": "en-US"}
    shots = properties["shots"]
    assert isinstance(shots, dict)
    assert shots["minItems"] == 1
    assert shots["maxItems"] == 6
    shot_items = shots["items"]
    assert isinstance(shot_items, dict)
    shot_properties = shot_items["properties"]
    assert isinstance(shot_properties, dict)
    assert "UNSPECIFIED" not in shot_properties["semantic_event"]["enum"]
    assert shot_properties["shot_size"]["enum"] == [
        "EXTREME_WIDE",
        "WIDE",
        "MEDIUM",
        "CLOSE_UP",
        "EXTREME_CLOSE_UP",
        "NON_SPATIAL",
    ]
    sentence_items = shot_properties["sentence_ids"]["items"]
    assert sentence_items["enum"] == [str(item.sentence_id) for item in attempt.sentences]
    assert result.receipt == "fallback-receipt"


def test_global_plan_attempt_uses_chapter_partition_contract() -> None:
    value = context(4)
    director, _repository, _model, _clock = service(value)
    _run, units = director.prepare_run(
        context=value,
        idempotency_key="plan-schema",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
    )
    attempt = units[0]
    payload = {"output_locale": "en-US", "chapters": []}
    raw_response = _valid_response_bytes(payload)
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, content=raw_response, request=request)

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        result = adapter.generate(attempt)
    finally:
        adapter.close()

    response_format = seen_body["response_format"]
    assert isinstance(response_format, dict)
    contract = response_format["json_schema"]
    assert isinstance(contract, dict)
    assert contract["name"] == "project_director_global_plan_v4"
    schema = contract["schema"]
    assert isinstance(schema, dict)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["output_locale", "chapters"]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["output_locale"] == {"type": "string", "const": "en-US"}
    chapters = properties["chapters"]
    assert isinstance(chapters, dict)
    assert chapters["minItems"] == 1
    assert chapters["maxItems"] == 24
    chapter_items = chapters["items"]
    assert isinstance(chapter_items, dict)
    assert chapter_items["required"] == ["order", "sentence_ids", "visual_beat", "setting"]
    chapter_properties = chapter_items["properties"]
    assert isinstance(chapter_properties, dict)
    sentence_items = chapter_properties["sentence_ids"]["items"]
    assert sentence_items["enum"] == [str(item.sentence_id) for item in value.sentences]
    assert result.payload == payload


@pytest.mark.parametrize(
    ("raw_response", "expected_code", "expected_stage", "expected_exception"),
    [
        (b"not-json", "director_response_json_invalid", "response_json", "JSONDecodeError"),
        (b"[]", "director_response_envelope_invalid", "envelope_type", "TypeError"),
        (
            json.dumps({"id": "response"}).encode(),
            "director_response_choices_invalid",
            "choices",
            "KeyError",
        ),
        (
            json.dumps({"id": "response", "choices": [{}]}).encode(),
            "director_response_message_invalid",
            "message",
            "KeyError",
        ),
        (
            json.dumps({"id": "response", "choices": [{"message": {}}]}).encode(),
            "director_response_content_missing",
            "content",
            "KeyError",
        ),
        (
            json.dumps({"id": "response", "choices": [{"message": {"content": []}}]}).encode(),
            "director_response_content_type_invalid",
            "content_type",
            "TypeError",
        ),
        (
            json.dumps(
                {"id": "response", "choices": [{"message": {"content": "{not-json"}}]}
            ).encode(),
            "director_response_content_json_invalid",
            "content_json",
            "JSONDecodeError",
        ),
        (
            json.dumps({"id": "response", "choices": [{"message": {"content": "[]"}}]}).encode(),
            "director_response_payload_invalid",
            "payload_type",
            "TypeError",
        ),
    ],
)
def test_200_invalid_json_or_envelope_is_rejected_with_safe_diagnostics(
    raw_response: bytes,
    expected_code: str,
    expected_stage: str,
    expected_exception: str,
) -> None:
    attempt = ProjectDirectorUnitAttempt("unit-key", "model", {}, "prompt")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json; charset=utf-8",
                "x-request-id": "safe-provider-request-id",
            },
            content=raw_response,
            request=request,
        )

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(DirectorStructuredResponseError) as raised:
            adapter.generate(attempt)
    finally:
        adapter.close()

    error = raised.value
    assert error.code == expected_code
    assert str(error) == expected_code
    assert error.provider_receipt == "safe-provider-request-id"
    assert error.raw_response_sha256 == hashlib.sha256(raw_response).hexdigest()
    assert error.diagnostics.as_json() == {
        "http_status": 200,
        "content_type": "application/json; charset=utf-8",
        "request_id_present": True,
        "request_id_source": "header",
        "raw_size_bytes": len(raw_response),
        "raw_response_sha256": hashlib.sha256(raw_response).hexdigest(),
        "parse_stage": expected_stage,
        "exception_class": expected_exception,
    }
    assert "not-json" not in json.dumps(error.diagnostics.as_json())


def test_invalid_content_uses_safe_envelope_receipt_when_header_is_absent() -> None:
    attempt = ProjectDirectorUnitAttempt("unit-key", "model", {}, "prompt")
    raw_response = json.dumps(
        {"id": "envelope-receipt", "choices": [{"message": {"content": "not-json"}}]}
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw_response, request=request)

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(DirectorStructuredResponseError) as raised:
            adapter.generate(attempt)
    finally:
        adapter.close()

    assert raised.value.provider_receipt == "envelope-receipt"
    assert raised.value.diagnostics.request_id_present is True
    assert raised.value.diagnostics.request_id_source == "envelope"


def test_400_is_a_bounded_retryable_provider_failure() -> None:
    # §15.14：LLM 平台偶发虚假 400 实测可重试成功，归入临时故障（预算内重试）；
    # 401/403 鉴权类仍为既定失败。
    attempt = ProjectDirectorUnitAttempt("unit-key", "model", {}, "prompt")
    for status, error_type in (
        (400, RetryableDirectorModelError),
        (401, PermanentDirectorModelError),
        (403, PermanentDirectorModelError),
        (422, RetryableDirectorModelError),
    ):
        adapter = OpenAiCompatibleDirectorModel(
            "https://director.test/v1",
            DUMMY_TOKEN,
            client=_client(
                httpx.MockTransport(
                    lambda request, code=status: httpx.Response(code, request=request)
                )
            ),
        )
        try:
            with pytest.raises(error_type, match=f"^provider_http_{status}$"):
                adapter.generate(attempt)
        finally:
            adapter.close()


@pytest.mark.parametrize("status_code", (429, 500, 503))
def test_throttling_and_5xx_are_retryable_provider_failures(status_code: int) -> None:
    attempt = ProjectDirectorUnitAttempt("unit-key", "model", {}, "prompt")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(RetryableDirectorModelError) as raised:
            adapter.generate(attempt)
    finally:
        adapter.close()

    assert raised.value.code == f"provider_http_{status_code}"
    assert raised.value.retry_after_seconds is None


def test_retry_after_header_is_parsed_for_throttled_responses() -> None:
    attempt = ProjectDirectorUnitAttempt("unit-key", "model", {}, "prompt")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "20"}, request=request)

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(RetryableDirectorModelError) as raised:
            adapter.generate(attempt)
    finally:
        adapter.close()

    assert raised.value.code == "provider_http_429"
    assert raised.value.retry_after_seconds == 20.0


def test_retry_after_date_format_is_not_honored() -> None:
    attempt = ProjectDirectorUnitAttempt("unit-key", "model", {}, "prompt")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
            request=request,
        )

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(RetryableDirectorModelError) as raised:
            adapter.generate(attempt)
    finally:
        adapter.close()

    assert raised.value.retry_after_seconds is None


def test_diagnostics_capture_finish_reason_and_usage_when_present() -> None:
    attempt = ProjectDirectorUnitAttempt("unit-key", "model", {}, "prompt")
    raw_response = json.dumps(
        {
            "id": "response",
            "choices": [{"finish_reason": "stop", "message": {}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 0},
        }
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw_response, request=request)

    adapter = OpenAiCompatibleDirectorModel(
        "https://director.test/v1",
        DUMMY_TOKEN,
        client=_client(httpx.MockTransport(handler)),
    )
    try:
        with pytest.raises(DirectorStructuredResponseError) as raised:
            adapter.generate(attempt)
    finally:
        adapter.close()

    error = raised.value
    assert error.code == "director_response_content_missing"
    assert error.diagnostics.finish_reason == "stop"
    assert error.diagnostics.usage_prompt_tokens == 12
    assert error.diagnostics.usage_completion_tokens == 0
    rendered = error.diagnostics.as_json()
    assert rendered["finish_reason"] == "stop"
    assert rendered["usage_prompt_tokens"] == 12
    assert rendered["usage_completion_tokens"] == 0


def test_diagnostics_reject_empty_finish_reason_and_negative_usage() -> None:
    base = {
        "http_status": 200,
        "content_type": "application/json",
        "request_id_present": False,
        "request_id_source": "absent",
        "raw_size_bytes": 1,
        "raw_response_sha256": "a" * 64,
        "parse_stage": "content",
        "exception_class": "KeyError",
    }
    with pytest.raises(ValueError, match="finish reason"):
        DirectorResponseDiagnostics(**base, finish_reason=" ")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage counters"):
        DirectorResponseDiagnostics(**base, usage_prompt_tokens=-1)  # type: ignore[arg-type]
