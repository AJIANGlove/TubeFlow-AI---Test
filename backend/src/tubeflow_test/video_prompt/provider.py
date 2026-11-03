"""视频提示词编译 Provider 端口与错误语义。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VideoPromptProviderResult:
    text: str
    receipt: str
    raw_response_sha256: str


class VideoPromptProvider(Protocol):
    def compile(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        model: str,
        idempotency_key: str,
    ) -> VideoPromptProviderResult: ...

    def close(self) -> None: ...


class PermanentVideoPromptProviderError(Exception):
    """确定性失败（4xx/合同拒绝）：重试同一请求没有意义。"""


class RetryableVideoPromptProviderError(Exception):
    """网络/限流/5xx 等不确定失败：用户可稍后手动重试。"""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class VideoPromptProviderResponseError(Exception):
    """Provider 响应解析/合同错误：可携带原因码进入一次修复重试。

    diagnostics 只放可安全落库的标量（解析阶段、正文字节数、finish_reason、
    响应体 sha256 前 12 位、失败键集合），**不包含**响应正文。
    """

    def __init__(
        self,
        code: str,
        diagnostics: dict[str, str | int | bool] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics or {}
