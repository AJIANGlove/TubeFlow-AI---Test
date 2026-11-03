"""DeepSeek 视频提示词编译适配器（OpenAI 兼容 chat/completions，json_object 模式）。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx

from tubeflow_test.video_prompt.provider import (
    PermanentVideoPromptProviderError,
    RetryableVideoPromptProviderError,
    VideoPromptProviderResponseError,
    VideoPromptProviderResult,
)


class DeepSeekVideoPromptProvider:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 45.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {token}"
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def compile(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        model: str,
        idempotency_key: str,
    ) -> VideoPromptProviderResult:
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": self._authorization,
                "Idempotency-Key": idempotency_key,
            },
            json={
                "model": model,
                "stream": False,
                "max_tokens": 4096,
                "thinking": {"type": "disabled"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(user_payload, ensure_ascii=False),
                    },
                ],
                "response_format": {"type": "json_object"},
            },
        )
        raw = response.content
        if response.status_code in {401, 403}:
            raise PermanentVideoPromptProviderError(f"provider_http_{response.status_code}")
        if response.status_code in {400, 404, 409, 410, 422}:
            # §15.14：LLM 平台偶发虚假 4xx，预算内退避重试。
            raise RetryableVideoPromptProviderError(f"provider_http_{response.status_code}")
            raise PermanentVideoPromptProviderError(f"provider_http_{response.status_code}")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableVideoPromptProviderError(
                f"provider_http_{response.status_code}",
                retry_after_seconds=_retry_after_seconds(response),
            )
        response.raise_for_status()
        text, receipt = _parse_response(response, raw)
        return VideoPromptProviderResult(text, receipt, hashlib.sha256(raw).hexdigest())


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_response(response: httpx.Response, raw: bytes) -> tuple[str, str]:
    try:
        envelope: Any = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise VideoPromptProviderResponseError(
            "video_prompt_response_json_invalid",
            _diagnostics(raw, parse_stage="envelope", envelope=None),
        ) from exc
    if not isinstance(envelope, dict):
        raise VideoPromptProviderResponseError("video_prompt_response_envelope_invalid")
    receipt = _receipt(response, envelope)
    try:
        choice = envelope["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VideoPromptProviderResponseError(
            "video_prompt_response_choices_invalid",
            _diagnostics(raw, parse_stage="choices", envelope=envelope),
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise VideoPromptProviderResponseError(
            "video_prompt_response_content_invalid",
            _diagnostics(raw, parse_stage="content", envelope=envelope),
        )
    try:
        payload: Any = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VideoPromptProviderResponseError(
            "video_prompt_response_payload_invalid",
            _diagnostics(
                raw,
                parse_stage="content_json",
                envelope=envelope,
                choice=choice,
                content=content,
            ),
        ) from exc
    if not isinstance(payload, dict) or set(payload.keys()) != {"prompt"}:
        diagnostics = _diagnostics(
            raw,
            parse_stage="payload_shape",
            envelope=envelope,
            choice=choice,
            content=content,
        )
        if isinstance(payload, dict):
            diagnostics["payload_keys"] = ",".join(sorted(str(key) for key in payload))[:120]
        raise VideoPromptProviderResponseError(
            "video_prompt_response_payload_invalid", diagnostics
        )
    text = payload["prompt"]
    if not isinstance(text, str) or not text.strip():
        raise VideoPromptProviderResponseError(
            "video_prompt_response_payload_invalid",
            _diagnostics(
                raw,
                parse_stage="payload_text",
                envelope=envelope,
                choice=choice,
                content=content,
            ),
        )
    return text.strip(), receipt


def _diagnostics(
    raw: bytes,
    *,
    parse_stage: str,
    envelope: dict[str, object] | None,
    choice: dict[str, object] | None = None,
    content: str | None = None,
) -> dict[str, str | int | bool]:
    """只记标量：不落响应正文，仅记阶段/长度/finish_reason/sha256 前缀。"""
    diagnostics: dict[str, str | int | bool] = {
        "parse_stage": parse_stage,
        "raw_size_bytes": len(raw),
        "raw_sha256_prefix": hashlib.sha256(raw).hexdigest()[:12],
    }
    if content is not None:
        diagnostics["content_length"] = len(content)
    finish_reason = None
    if choice is not None and isinstance(choice, dict):
        candidate = choice.get("finish_reason")
        if isinstance(candidate, str) and candidate.strip():
            finish_reason = candidate.strip()[:32]
    elif isinstance(envelope, dict):
        choices = envelope.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            candidate = choices[0].get("finish_reason")
            if isinstance(candidate, str) and candidate.strip():
                finish_reason = candidate.strip()[:32]
    if finish_reason:
        diagnostics["finish_reason"] = finish_reason
    return diagnostics


def _receipt(response: httpx.Response, envelope: dict[str, object]) -> str:
    header_receipt = str(response.headers.get("x-request-id", "")).strip()
    if header_receipt:
        return header_receipt[:512]
    envelope_id = envelope.get("id")
    if isinstance(envelope_id, str) and envelope_id.strip():
        return envelope_id.strip()[:512]
    return ""
