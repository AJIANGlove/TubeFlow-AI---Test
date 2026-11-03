from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DomainError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


class NarrationIntentConflictError(DomainError):
    """同一幂等键承载不同句序意图：确定性冲突，不可重试。"""


@dataclass(slots=True)
class ProviderRateLimitedError(DomainError):
    """Provider 返回 429：明确拒绝但可按退避重试；携带 Retry-After 秒数（如有）。"""

    retry_after_seconds: float | None = None


@dataclass(slots=True)
class ProviderHttpStatusError(DomainError):
    """Provider 以非 2xx 状态码明确拒绝：携带状态码，供号池失败判定矩阵使用。

    §23.6：只有拿到真实状态码才能区分「账号病」（402/复核确认的 401）与请求级瞬态。
    """

    status_code: int | None = None
