"""DeepSeek 拆句 agent 适配器（OpenAI 兼容 chat/completions，json_object 模式）。

错误语义对齐 adapters/deepseek_video_prompt.py：4xx 既定、429/5xx 可退避重试
（尊重 Retry-After）、响应合同校验、安全回执不保存响应正文。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from tubeflow_test.sentence_split.provider import (
    PermanentSentenceSplitProviderError,
    RetryableSentenceSplitProviderError,
    SentenceSplitProviderResult,
    SentenceSplitResponseError,
)


class DeepSeekSentenceSplitProvider:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 45.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Sentence split Provider base URL must be HTTP or HTTPS.")
        self._base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {token}"
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def split(
        self,
        *,
        system_prompt: str,
        sentence_text: str,
        locale: str,
        model: str,
        idempotency_key: str,
        repair_reason: str | None = None,
    ) -> SentenceSplitProviderResult:
        try:
            response = await self._client.post(
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
                            "content": _user_payload(sentence_text, locale, repair_reason),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                },
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise RetryableSentenceSplitProviderError("provider_timeout") from error
        except httpx.RequestError as error:
            raise RetryableSentenceSplitProviderError("provider_unavailable") from error
        raw = response.content
        if response.status_code in {401, 403}:
            raise PermanentSentenceSplitProviderError(f"provider_http_{response.status_code}")
        if response.status_code in {400, 404, 409, 410, 422}:
            # §15.14：LLM 平台偶发虚假 4xx，预算内退避重试。
            raise RetryableSentenceSplitProviderError(f"provider_http_{response.status_code}")
            raise PermanentSentenceSplitProviderError(f"provider_http_{response.status_code}")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableSentenceSplitProviderError(
                f"provider_http_{response.status_code}",
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code != 200:
            raise PermanentSentenceSplitProviderError(f"provider_http_{response.status_code}")
        parts, receipt = _parse_response(response, raw)
        return SentenceSplitProviderResult(parts, receipt, hashlib.sha256(raw).hexdigest())


def _user_payload(sentence_text: str, locale: str, repair_reason: str | None) -> str:
    payload: dict[str, str] = {
        "json_task": "split_overlong_sentence",
        "language": locale,
        "sentence": sentence_text,
    }
    if repair_reason:
        payload["repair_reason"] = repair_reason
    return json.dumps(payload, ensure_ascii=False)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_response(response: httpx.Response, raw: bytes) -> tuple[tuple[str, ...], str]:
    try:
        envelope: Any = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise SentenceSplitResponseError("sentence_split_response_json_invalid") from exc
    if not isinstance(envelope, dict):
        raise SentenceSplitResponseError("sentence_split_response_envelope_invalid")
    receipt = _receipt(response, envelope)
    try:
        choice = envelope["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SentenceSplitResponseError("sentence_split_response_choices_invalid") from exc
    if not isinstance(content, str) or not content.strip():
        raise SentenceSplitResponseError("sentence_split_response_content_invalid")
    try:
        payload: Any = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SentenceSplitResponseError("sentence_split_response_payload_invalid") from exc
    if not isinstance(payload, dict) or set(payload.keys()) != {"parts"}:
        raise SentenceSplitResponseError("sentence_split_response_payload_invalid")
    parts = payload["parts"]
    if not isinstance(parts, list) or not all(isinstance(part, str) for part in parts):
        raise SentenceSplitResponseError("sentence_split_response_payload_invalid")
    return tuple(parts), receipt


def _receipt(response: httpx.Response, envelope: dict[str, object]) -> str:
    header_receipt = str(response.headers.get("x-request-id", "")).strip()
    if header_receipt:
        return header_receipt[:512]
    envelope_id = envelope.get("id")
    if isinstance(envelope_id, str) and envelope_id.strip():
        return envelope_id.strip()[:512]
    return ""
