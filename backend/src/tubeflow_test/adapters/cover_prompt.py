"""封面文本适配器（DeepSeek，OpenAI 兼容 chat/completions，json_object）。

同一传输形态供文字生成（0.9）/画面两版（0.8、1.0）/整改（0.8）/评委（0.2）
四处复用：温度与消息由调用方传入（消息由 `domain/cover.py` 构造）。渠道
故障按共享传输语义换 fallback_model 一次（同 baseURL/凭据仅换 model）；
401/403 既定不重试（计划 §5/§6）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from tubeflow_test.adapters.cover_llm_transport import OpenAiCompatibleCoverLlmTransport
from tubeflow_test.application.cover_providers import CoverLlmResult


class DeepSeekCoverPromptProvider:
    """封面文本通道：DeepSeek 端点，fallback deepseek-v4-pro 一次。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        model: str,
        fallback_model: str = "",
        timeout_seconds: float = 300.0,
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

    def complete(
        self, messages: Sequence[Mapping[str, Any]], *, temperature: float
    ) -> CoverLlmResult:
        return self._transport.chat_json(
            model=self._model,
            temperature=temperature,
            messages=messages,
            fallback_model=self._fallback_model,
        )
