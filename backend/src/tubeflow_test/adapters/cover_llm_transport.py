"""封面 LLM 共享传输：OpenAI 兼容 chat/completions + 重试 + 一次备用模型。

两个封面 LLM 适配器（参考图解析 `cover_ref_analysis.py`、封面文本
`cover_prompt.py`）共用同一传输语义（移植自参考实现 `src/ai/llm.js`，见
COVER_GENERATION_PLAN §5 与交接文档 §12.4）：

- 408/425/429/5xx 与超时/网络错误最多重试 2 次，退避 1s/3s + 0–250ms 抖动；
  数字 Retry-After 封顶 60s 时替代退避基数，再加抖动；
- HTTP 200 但外层/内容不可解析为 JSON 对象时，同模型最多重发一轮；内容按
  原文 → Markdown 围栏剥离 → 花括号区间顺序确定性解析；
- 渠道故障（重试耗尽的 408/425/429/5xx、超时/网络错误、两轮仍不可解析、
  200 错误体/空内容）且配置了不同备用模型时，同 baseURL/凭据仅换 model
  重发一次；
- 401/403 与其余 4xx 属请求本身问题，既定不重试、不换模型（计划 §6）。

错误统一映射为 DomainError 稳定码；日志/异常不含 token、提示词或响应原文。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from tubeflow_test.application.cover_providers import (
    CoverLlmChannelFailureError,
    CoverLlmResult,
)
from tubeflow_test.domain.errors import DomainError

_RETRYABLE_STATUSES = frozenset({408, 425, 429})
_MAX_RETRIES = 2
_BACKOFF_SECONDS = (1.0, 3.0)
_RETRY_AFTER_CAP_SECONDS = 60.0
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?([\s\S]*?)\s*```$", re.IGNORECASE)


def parse_json_object_content(content: object) -> dict[str, Any] | None:
    """宽松解析模型内容为 JSON 对象：原文 → Markdown 围栏剥离 → 花括号区间。

    三种形态都失败返回 None；顶层必须是对象（数组不做花括号切片，否则
    [{...}] 会被悄悄抠成对象，改变契约语义）。
    """
    if not isinstance(content, str) or not content.strip():
        return None
    trimmed = content.strip()
    candidates = [trimmed]
    fenced = _FENCE_RE.match(trimmed)
    if fenced:
        candidates.append(fenced.group(1).strip())
    if not trimmed.startswith("["):
        first = trimmed.find("{")
        last = trimmed.rfind("}")
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


class OpenAiCompatibleCoverLlmTransport:
    """封面 LLM 共享传输：单模型一轮 = HTTP 重试 ≤2 + 非 JSON 同模型重发一轮。

    sleeper/rng 注入便于测试确定性；生产用 time.sleep 与 random.random。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {token}"
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._sleeper = sleeper
        self._rng = rng

    def close(self) -> None:
        self._client.close()

    def chat_json(
        self,
        *,
        model: str,
        temperature: float,
        messages: Sequence[Mapping[str, Any]],
        fallback_model: str = "",
    ) -> CoverLlmResult:
        """以主模型调用；渠道故障且配置了不同备用模型时仅换 model 重发一次。"""
        try:
            return self._chat_with_model(model, temperature, messages)
        except CoverLlmChannelFailureError:
            fallback = fallback_model.strip()
            if not fallback or fallback == model:
                raise
            return self._chat_with_model(fallback, temperature, messages)

    def _chat_with_model(
        self, model: str, temperature: float, messages: Sequence[Mapping[str, Any]]
    ) -> CoverLlmResult:
        last_invalid: DomainError | None = None
        # HTTP 层错误不在此重发（_post 的传输重试已覆盖瞬时故障）；仅 HTTP 200
        # 但不可解析的病态响应同模型最多重发一轮。
        for _round in range(2):
            response = self._post(model, temperature, messages)
            raw = response.content
            status = response.status_code
            if status >= 400:
                raise DomainError(
                    f"cover_llm_provider_http_{status}",
                    f"封面 LLM 请求被拒绝（HTTP {status}）。",
                )
            try:
                envelope: Any = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                last_invalid = CoverLlmChannelFailureError(
                    "cover_llm_response_invalid", "封面 LLM 响应外层不是合法 JSON。"
                )
                continue
            if not isinstance(envelope, dict):
                raise CoverLlmChannelFailureError(
                    "cover_llm_response_invalid", "封面 LLM 响应外层不是 JSON 对象。"
                )
            if isinstance(envelope.get("error"), Mapping):
                # 网关退化响应：HTTP 200 但 body 是错误体 → 渠道故障。
                raise CoverLlmChannelFailureError(
                    "cover_llm_response_invalid", "封面 LLM 网关在 HTTP 200 中返回了错误体。"
                )
            content = _extract_content(envelope)
            if content is None:
                raise CoverLlmChannelFailureError(
                    "cover_llm_response_invalid", "封面 LLM 返回了空响应（HTTP 200 无有效内容）。"
                )
            payload = parse_json_object_content(content)
            if payload is not None:
                return CoverLlmResult(
                    payload, _receipt(response, envelope), model, hashlib.sha256(raw).hexdigest()
                )
            last_invalid = CoverLlmChannelFailureError(
                "cover_llm_response_invalid", "封面 LLM 返回内容不符合 JSON 对象契约。"
            )
        assert last_invalid is not None
        raise last_invalid

    def _post(
        self, model: str, temperature: float, messages: Sequence[Mapping[str, Any]]
    ) -> httpx.Response:
        body = {
            "model": model,
            "temperature": temperature,
            "messages": list(messages),
            "response_format": {"type": "json_object"},
        }
        last_error: DomainError | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": self._authorization},
                    json=body,
                )
            except httpx.TimeoutException:
                last_error = CoverLlmChannelFailureError(
                    "cover_llm_provider_timeout", "封面 LLM 请求超时。"
                )
            except httpx.TransportError:
                last_error = CoverLlmChannelFailureError(
                    "cover_llm_provider_network", "无法连接封面 LLM 服务。"
                )
            else:
                status = response.status_code
                if status not in _RETRYABLE_STATUSES and status < 500:
                    return response
                last_error = CoverLlmChannelFailureError(
                    f"cover_llm_provider_http_{status}", f"封面 LLM 请求失败（HTTP {status}）。"
                )
                if attempt < _MAX_RETRIES:
                    self._sleep_before_status_retry(attempt, response)
                    continue
            if attempt < _MAX_RETRIES:
                self._sleeper(self._backoff_seconds(attempt) + self._jitter_seconds())
        assert last_error is not None
        raise last_error

    def _sleep_before_status_retry(self, attempt: int, response: httpx.Response) -> None:
        retry_after = _retry_after_seconds(response)
        base = self._backoff_seconds(attempt)
        delay = (min(retry_after, _RETRY_AFTER_CAP_SECONDS) if retry_after else base)
        self._sleeper(delay + self._jitter_seconds())

    def _backoff_seconds(self, attempt: int) -> float:
        return _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]

    def _jitter_seconds(self) -> float:
        return int(self._rng() * 251) / 1000.0


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
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _receipt(response: httpx.Response, envelope: Mapping[str, Any]) -> str:
    header_receipt = str(response.headers.get("x-request-id", "")).strip()
    if header_receipt:
        return header_receipt[:512]
    envelope_id = envelope.get("id")
    if isinstance(envelope_id, str) and envelope_id.strip():
        return envelope_id.strip()[:512]
    return ""


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None
