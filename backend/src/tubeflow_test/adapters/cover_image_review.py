"""封面图视觉复审适配器（grsai，与参考图解析同通道同配置）。

消息由 `domain.cover.build_cover_image_review_messages` 构造（含原图与
168×94 缩略图），本适配器只负责传输与响应解析；错误语义与
`adapters/cover_ref_analysis.py` 完全一致（渠道故障可换备用模型一次）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from tubeflow_test.adapters.cover_llm_transport import OpenAiCompatibleCoverLlmTransport
from tubeflow_test.application.cover_providers import CoverLlmResult

_REVIEW_TEMPERATURE = 0.1


class GrsaiCoverImageReviewProvider:
    """出图后视觉复审通道：temperature 固定 0.1（与解析通道一致）。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        model: str,
        fallback_model: str = "",
        timeout_seconds: float = 120.0,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._model = model
        self._fallback_model = fallback_model
        self._transport = OpenAiCompatibleCoverLlmTransport(
            base_url, token, timeout_seconds, client=client, sleeper=sleeper, rng=rng
        )

    def close(self) -> None:
        self._transport.close()

    def review(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> CoverLlmResult:
        return self._transport.chat_json(
            model=self._model,
            temperature=_REVIEW_TEMPERATURE,
            messages=messages,
            fallback_model=self._fallback_model,
        )
