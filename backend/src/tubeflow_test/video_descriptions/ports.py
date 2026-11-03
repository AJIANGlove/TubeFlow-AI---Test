"""简介应用层端口：仓储与文本模型 Provider。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class DescriptionProviderResult:
    payload: dict[str, Any]
    receipt: str
    model: str
    raw_response_sha256: str


class DescriptionProvider(Protocol):
    def complete_json(
        self, messages: Sequence[Mapping[str, Any]], *, temperature: float
    ) -> DescriptionProviderResult: ...

    def close(self) -> None: ...
