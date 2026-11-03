"""测试与本地契约用的确定性视频提示词编译器（不触网，镜像 fake_engine 约定）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tubeflow_test.video_prompt.domain import (
    CompiledVideoPrompt,
    VideoPromptCompilerInput,
    VideoPromptStrategy,
)


class FakeVideoPromptCompiler:
    def __init__(self, strategy: VideoPromptStrategy = VideoPromptStrategy.TUBEFLOW_ENHANCED_V1):
        self._strategy = strategy
        self.inputs: list[VideoPromptCompilerInput] = []
        self.contexts: list[Mapping[str, Any]] = []
        self.closed = False

    @property
    def strategy(self) -> VideoPromptStrategy:
        return self._strategy

    def compile(
        self,
        value: VideoPromptCompilerInput,
        *,
        max_repairs: int = 1,
        repair_reason: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CompiledVideoPrompt:
        del max_repairs, repair_reason
        self.inputs.append(value)
        if context is not None:
            self.contexts.append(context)
        return CompiledVideoPrompt(
            f"compiled video prompt for {value.idempotency_key}",
            value.strategy,
            "fake-model",
            "fake-receipt",
            False,
            value.fingerprint,
        )

    def close(self) -> None:
        self.closed = True
