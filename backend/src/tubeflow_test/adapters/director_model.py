from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from tubeflow_test.application.director_generation import (
    DirectorResponseDiagnostics,
    DirectorStructuredResponseError,
    PermanentDirectorModelError,
    RetryableDirectorModelError,
)
from tubeflow_test.domain.director_generation import DirectorModelResult
from tubeflow_test.domain.project_director import ProjectDirectorUnit, ProjectDirectorUnitKind
from tubeflow_test.domain.storyboards import (
    DirectorCameraViewpoint,
    DirectorLocationRelation,
    DirectorSemanticEvent,
    DirectorShotSize,
    DirectorSubjectFocus,
    DirectorTimeRelation,
)


class DirectorModelAttempt(Protocol):
    @property
    def provider_idempotency_key(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def parameters(self) -> Mapping[str, str | int | float | bool]: ...

    @property
    def prompt_content(self) -> str: ...


class OpenAiCompatibleDirectorModel:
    @property
    def supports_reconciliation(self) -> bool:
        return False

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {token}"
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def generate(self, attempt: DirectorModelAttempt) -> DirectorModelResult:
        schema_name, schema = _response_contract(attempt)
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": self._authorization,
                "Idempotency-Key": attempt.provider_idempotency_key,
            },
            json={
                "model": attempt.model,
                "stream": False,
                **attempt.parameters,
                "messages": [{"role": "system", "content": attempt.prompt_content}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
            },
        )
        raw = response.content
        if response.status_code in {401, 403}:
            raise PermanentDirectorModelError(f"provider_http_{response.status_code}")
        if response.status_code in {400, 404, 409, 410, 422}:
            # §15.14：LLM 平台偶发虚假 4xx（实测同请求重放即成功），预算内退避重试。
            raise RetryableDirectorModelError(f"provider_http_{response.status_code}")
            raise PermanentDirectorModelError(f"provider_http_{response.status_code}")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableDirectorModelError(
                f"provider_http_{response.status_code}",
                retry_after_seconds=_retry_after_seconds(response),
            )
        response.raise_for_status()
        payload, receipt = _parse_structured_response(response, raw)
        return DirectorModelResult(payload, receipt, hashlib.sha256(raw).hexdigest())

    def reconcile(self, attempt: DirectorModelAttempt) -> DirectorModelResult | None:
        del attempt
        return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_structured_response(
    response: httpx.Response,
    raw: bytes,
) -> tuple[dict[str, object], str]:
    try:
        decoded: Any = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise _structured_response_error(
            response,
            raw,
            code="director_response_json_invalid",
            parse_stage="response_json",
            exc=exc,
        ) from exc
    if not isinstance(decoded, dict):
        type_error = TypeError("director response envelope must be an object")
        raise _structured_response_error(
            response,
            raw,
            code="director_response_envelope_invalid",
            parse_stage="envelope_type",
            exc=type_error,
        ) from type_error
    envelope = decoded
    try:
        choices = envelope["choices"]
        if not isinstance(choices, list) or not choices:
            raise TypeError("director response choices must be a non-empty array")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise TypeError("director response choice must be an object")
    except (KeyError, TypeError) as exc:
        raise _structured_response_error(
            response,
            raw,
            code="director_response_choices_invalid",
            parse_stage="choices",
            exc=exc,
            envelope=envelope,
        ) from exc
    try:
        message = choice["message"]
        if not isinstance(message, dict):
            raise TypeError("director response message must be an object")
    except (KeyError, TypeError) as exc:
        raise _structured_response_error(
            response,
            raw,
            code="director_response_message_invalid",
            parse_stage="message",
            exc=exc,
            envelope=envelope,
            choice=choice,
        ) from exc
    try:
        content = message["content"]
    except KeyError as exc:
        raise _structured_response_error(
            response,
            raw,
            code="director_response_content_missing",
            parse_stage="content",
            exc=exc,
            envelope=envelope,
            choice=choice,
        ) from exc
    if not isinstance(content, str):
        content_type_error = TypeError("director response content must be a string")
        raise _structured_response_error(
            response,
            raw,
            code="director_response_content_type_invalid",
            parse_stage="content_type",
            exc=content_type_error,
            envelope=envelope,
            choice=choice,
        ) from content_type_error
    text = content
    if text.lstrip().startswith("<think>") and "</think>" in text:
        # 推理型模型在 JSON 前输出思维链前言；剥离后按原合同解析。
        text = text.split("</think>", 1)[1]
    try:
        payload: Any = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _structured_response_error(
            response,
            raw,
            code="director_response_content_json_invalid",
            parse_stage="content_json",
            exc=exc,
            envelope=envelope,
            choice=choice,
        ) from exc
    if not isinstance(payload, dict):
        payload_type_error = TypeError("director response payload must be an object")
        raise _structured_response_error(
            response,
            raw,
            code="director_response_payload_invalid",
            parse_stage="payload_type",
            exc=payload_type_error,
            envelope=envelope,
            choice=choice,
        ) from payload_type_error
    receipt, _source = _provider_receipt(response, envelope)
    return payload, receipt or ""


def _structured_response_error(
    response: httpx.Response,
    raw: bytes,
    *,
    code: str,
    parse_stage: str,
    exc: Exception,
    envelope: dict[str, object] | None = None,
    choice: dict[str, object] | None = None,
) -> DirectorStructuredResponseError:
    receipt, request_id_source = _provider_receipt(response, envelope)
    raw_response_sha256 = hashlib.sha256(raw).hexdigest()
    content_type = response.headers.get("content-type", "").strip()
    content_type = content_type.replace("\r", "").replace("\n", "")[:256]
    finish_reason, prompt_tokens, completion_tokens = _safe_response_metadata(envelope, choice)
    diagnostics = DirectorResponseDiagnostics(
        http_status=response.status_code,
        content_type=content_type,
        request_id_present=receipt is not None,
        request_id_source=request_id_source,
        raw_size_bytes=len(raw),
        raw_response_sha256=raw_response_sha256,
        parse_stage=parse_stage,
        exception_class=type(exc).__name__,
        finish_reason=finish_reason,
        usage_prompt_tokens=prompt_tokens,
        usage_completion_tokens=completion_tokens,
    )
    return DirectorStructuredResponseError(
        code,
        provider_receipt=receipt,
        diagnostics=diagnostics,
    )


def _safe_response_metadata(
    envelope: dict[str, object] | None,
    choice: dict[str, object] | None,
) -> tuple[str | None, int | None, int | None]:
    finish_reason: str | None = None
    if choice is not None:
        candidate = choice.get("finish_reason")
        if isinstance(candidate, str) and candidate.strip():
            finish_reason = candidate
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    if envelope is not None:
        usage = envelope.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0:
                prompt_tokens = prompt
            if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0:
                completion_tokens = completion
    return finish_reason, prompt_tokens, completion_tokens


def _provider_receipt(
    response: httpx.Response,
    envelope: dict[str, object] | None,
) -> tuple[str | None, str]:
    header_receipt = response.headers.get("x-request-id", "").strip()
    if header_receipt:
        return header_receipt[:512], "header"
    if envelope is not None:
        envelope_receipt = envelope.get("id")
        if isinstance(envelope_receipt, str) and envelope_receipt.strip():
            return envelope_receipt.strip()[:512], "envelope"
    return None, "absent"


_TEXT = {"type": "string", "minLength": 1}
_DIRECTOR_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["shots"],
    "properties": {
        "shots": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "order",
                    "scene",
                    "shot_size",
                    "camera_viewpoint",
                    "action",
                    "outcome",
                    "narrative_function",
                    "description",
                    "sentence_ids",
                ],
                "properties": {
                    "order": {"type": "integer", "minimum": 1, "maximum": 5},
                    "scene": _TEXT,
                    "shot_size": _TEXT,
                    "camera_viewpoint": _TEXT,
                    "action": _TEXT,
                    "outcome": _TEXT,
                    "narrative_function": _TEXT,
                    "description": _TEXT,
                    "sentence_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "format": "uuid"},
                    },
                },
            },
        }
    },
}


def _response_contract(attempt: DirectorModelAttempt) -> tuple[str, dict[str, object]]:
    if isinstance(attempt, ProjectDirectorUnit):
        if attempt.kind is ProjectDirectorUnitKind.GLOBAL_PLAN:
            return "project_director_global_plan_v4", _global_plan_schema(attempt)
        return "project_director_unit_v4", _project_director_schema(attempt)
    return "director_plan_v1", _DIRECTOR_SCHEMA


def _global_plan_schema(attempt: ProjectDirectorUnit) -> dict[str, object]:
    allowed_sentence_ids = [str(item.sentence_id) for item in attempt.context_before]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["output_locale", "chapters"],
        "properties": {
            "output_locale": {"type": "string", "const": attempt.required_locale},
            "chapters": {
                "type": "array",
                "minItems": 1,
                "maxItems": 24,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["order", "sentence_ids", "visual_beat", "setting"],
                    "properties": {
                        "order": {"type": "integer", "minimum": 1},
                        "sentence_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "format": "uuid",
                                "enum": allowed_sentence_ids,
                            },
                        },
                        "visual_beat": _TEXT,
                        "setting": _TEXT,
                    },
                },
            },
        },
    }


def _project_director_schema(attempt: ProjectDirectorUnit) -> dict[str, object]:
    allowed_sentence_ids = [str(item.sentence_id) for item in attempt.sentences]
    shot_cap = min(2 * len(attempt.sentences), 8)
    shot_required = [
        "order",
        "scene",
        "shot_size",
        "camera_viewpoint",
        "action",
        "outcome",
        "narrative_function",
        "description",
        "semantic_event",
        "subject_focus",
        "time_relation",
        "location_relation",
        "sentence_ids",
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["output_locale", "shots"],
        "properties": {
            "output_locale": {"type": "string", "const": attempt.required_locale},
            "shots": {
                "type": "array",
                "minItems": 1,
                "maxItems": shot_cap,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": shot_required,
                    "properties": {
                        "order": {"type": "integer", "minimum": 1, "maximum": shot_cap},
                        "scene": _TEXT,
                        "shot_size": {
                            "type": "string",
                            "enum": [item.value for item in DirectorShotSize],
                        },
                        "camera_viewpoint": {
                            "type": "string",
                            "enum": [item.value for item in DirectorCameraViewpoint],
                        },
                        "action": _TEXT,
                        "outcome": _TEXT,
                        "narrative_function": _TEXT,
                        "description": _TEXT,
                        "semantic_event": {
                            "type": "string",
                            "enum": [
                                item.value
                                for item in DirectorSemanticEvent
                                if item is not DirectorSemanticEvent.UNSPECIFIED
                            ],
                        },
                        "subject_focus": {
                            "type": "string",
                            "enum": [
                                item.value
                                for item in DirectorSubjectFocus
                                if item is not DirectorSubjectFocus.UNSPECIFIED
                            ],
                        },
                        "time_relation": {
                            "type": "string",
                            "enum": [
                                item.value
                                for item in DirectorTimeRelation
                                if item is not DirectorTimeRelation.UNSPECIFIED
                            ],
                        },
                        "location_relation": {
                            "type": "string",
                            "enum": [
                                item.value
                                for item in DirectorLocationRelation
                                if item is not DirectorLocationRelation.UNSPECIFIED
                            ],
                        },
                        "sentence_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": len(allowed_sentence_ids),
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "format": "uuid",
                                "enum": allowed_sentence_ids,
                            },
                        },
                    },
                },
            },
        },
    }
