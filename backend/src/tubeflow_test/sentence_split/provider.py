"""拆句 agent Provider 端口与错误语义（对齐 video_prompt/provider.py）。

- PermanentSentenceSplitProviderError：4xx 既定失败，重试同一请求没有意义。
- RetryableSentenceSplitProviderError：超时/连接错误/429/5xx，可退避重试，
  429 携带 Retry-After 秒数（如有）。
- SentenceSplitResponseError：2xx 但响应合同不合法，属既定失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SentenceSplitProviderResult:
    parts: tuple[str, ...]
    receipt: str
    raw_response_sha256: str


class SentenceSplitProvider(Protocol):
    async def split(
        self,
        *,
        system_prompt: str,
        sentence_text: str,
        locale: str,
        model: str,
        idempotency_key: str,
        repair_reason: str | None = None,
    ) -> SentenceSplitProviderResult: ...


class PermanentSentenceSplitProviderError(Exception):
    """确定性失败（4xx）：重试同一请求没有意义。"""


class RetryableSentenceSplitProviderError(Exception):
    """超时/连接错误/限流/5xx 等临时故障：可按预算退避重试。"""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class SentenceSplitResponseError(Exception):
    """Provider 响应解析/合同错误：输出不确定性导致，可在预算内修复重试。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
