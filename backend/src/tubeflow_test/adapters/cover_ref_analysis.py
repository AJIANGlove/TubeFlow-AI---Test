"""封面参考图视觉解析适配器（grsai，OpenAI 兼容 chat/completions，json_object）。

POST `{base_url}/chat/completions`，body `{model, temperature:0.1, messages,
response_format:{type:"json_object"}}`；消息由
`domain.cover.build_ref_analysis_messages` 构造，本适配器只负责传输与响应
解析。渠道故障按共享传输语义换 fallback_model 一次（同 baseURL/凭据仅换
model）；401/403 既定不重试（计划 §5/§6）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from tubeflow_test.adapters.cover_llm_transport import OpenAiCompatibleCoverLlmTransport
from tubeflow_test.application.cover_providers import CoverLlmResult

_ANALYSIS_TEMPERATURE = 0.1


class GrsaiCoverRefAnalysisProvider:
    """参考图视觉解析通道：temperature 固定 0.1（计划 §5/§1.7）。"""

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

    def analyze(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> CoverLlmResult:
        return self._transport.chat_json(
            model=self._model,
            temperature=_ANALYSIS_TEMPERATURE,
            messages=messages,
            fallback_model=self._fallback_model,
        )
