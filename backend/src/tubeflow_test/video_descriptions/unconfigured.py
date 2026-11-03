"""未配置简介 Provider 时的显式失败实现；绝不静默降级。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_descriptions.ports import DescriptionProviderResult


class UnconfiguredDescriptionProvider:
    def complete_json(
        self, messages: Sequence[Mapping[str, Any]], *, temperature: float
    ) -> DescriptionProviderResult:
        del messages, temperature
        raise DomainError(
            "description_provider_not_configured", "简介文本服务未配置。"
        )

    def close(self) -> None:
        return None
