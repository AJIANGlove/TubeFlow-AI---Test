"""DeepSeek 简介文本适配器（OpenAI 兼容 chat/completions，单次请求无传输重试）。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_descriptions.ports import DescriptionProviderResult

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?([\s\S]*?)\s*```$", re.IGNORECASE)


class DeepSeekDescriptionProvider:
    """简介生成复用既有 video_prompt_provider_* DeepSeek 配置；网络/超时/429/5xx 不重试。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        model: str,
        timeout_seconds: float = 45.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {token}"
        self._model = model
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def complete_json(
        self, messages: Sequence[Mapping[str, Any]], *, temperature: float
    ) -> DescriptionProviderResult:
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": self._authorization},
                json={
                    "model": self._model,
                    "temperature": temperature,
                    "messages": list(messages),
                    "response_format": {"type": "json_object"},
                },
            )
        except httpx.TimeoutException as exc:
            raise DomainError("description_provider_timeout", "简介文本服务请求超时。") from exc
        except httpx.TransportError as exc:
            raise DomainError("description_provider_network", "无法连接简介文本服务。") from exc
        raw = response.content
        if response.status_code == 429 or response.status_code >= 500:
            raise DomainError(
                "description_provider_unavailable",
                f"简介文本服务暂不可用（HTTP {response.status_code}）。",
            )
        if response.status_code >= 400:
            raise DomainError(
                "description_provider_rejected",
                f"简介文本服务拒绝了请求（HTTP {response.status_code}）。",
            )
        try:
            envelope: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DomainError(
                "description_output_invalid", "简介文本服务响应外层不是合法 JSON。"
            ) from exc
        if not isinstance(envelope, dict):
            raise DomainError(
                "description_output_invalid", "简介文本服务响应外层不是 JSON 对象。"
            )
        content = _extract_content(envelope)
        payload = _parse_json_object(content) if content is not None else None
        if payload is None:
            raise DomainError(
                "description_output_invalid", "简介文本服务返回内容不符合 JSON 对象契约。"
            )
        return DescriptionProviderResult(
            payload,
            _receipt(response, envelope),
            self._model,
            hashlib.sha256(raw).hexdigest(),
        )


def _extract_content(envelope: Mapping[str, Any]) -> str | None:
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    message = choice.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) and content.strip() else None


def _parse_json_object(content: str) -> dict[str, Any] | None:
    trimmed = content.strip()
    candidates = [trimmed]
    fenced = _FENCE_RE.match(trimmed)
    if fenced:
        candidates.append(fenced.group(1).strip())
    if not trimmed.startswith("["):
        first, last = trimmed.find("{"), trimmed.rfind("}")
        if first >= 0 and last > first:
            candidates.append(trimmed[first : last + 1])
    for candidate in candidates:
        try:
            parsed: Any = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _receipt(response: httpx.Response, envelope: Mapping[str, Any]) -> str:
    header = str(response.headers.get("x-request-id", "")).strip()
    if header:
        return header[:512]
    value = envelope.get("id")
    return value.strip()[:512] if isinstance(value, str) and value.strip() else ""
