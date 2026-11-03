"""封面 Provider 内存 fake：供应用层与工作流测试注入（零真实网络）。

三个 fake 分别实现 `application/cover_providers.py` 的三个 Protocol：按注入
顺序弹出结果或异常，记录全部调用参数；未编排结果的调用以 AssertionError
快速失败，避免测试误通过。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tubeflow_test.application.cover_providers import CoverImageResult, CoverLlmResult

Outcome = CoverLlmResult | CoverImageResult | Exception


def _pop(outcomes: deque[Outcome], label: str) -> Outcome:
    if not outcomes:
        raise AssertionError(f"{label} received an unscripted call")
    outcome = outcomes.popleft()
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


class FakeCoverRefAnalysisProvider:
    """参考图解析 fake：记录每次调用的消息列表。"""

    def __init__(self, outcomes: Iterable[CoverLlmResult | Exception] = ()) -> None:
        self._outcomes: deque[Outcome] = deque(outcomes)
        self.calls: list[list[Mapping[str, Any]]] = []
        self.closed = False

    def queue(self, outcome: CoverLlmResult | Exception) -> None:
        self._outcomes.append(outcome)

    def analyze(self, messages: Sequence[Mapping[str, Any]]) -> CoverLlmResult:
        self.calls.append(list(messages))
        outcome = _pop(self._outcomes, "FakeCoverRefAnalysisProvider")
        assert isinstance(outcome, CoverLlmResult)
        return outcome

    def close(self) -> None:
        self.closed = True


class FakeCoverPromptProvider:
    """封面文本 fake：记录每次调用的消息列表与温度。"""

    def __init__(self, outcomes: Iterable[CoverLlmResult | Exception] = ()) -> None:
        self._outcomes: deque[Outcome] = deque(outcomes)
        self.calls: list[tuple[list[Mapping[str, Any]], float]] = []
        self.closed = False

    def queue(self, outcome: CoverLlmResult | Exception) -> None:
        self._outcomes.append(outcome)

    def complete(
        self, messages: Sequence[Mapping[str, Any]], *, temperature: float
    ) -> CoverLlmResult:
        self.calls.append((list(messages), temperature))
        outcome = _pop(self._outcomes, "FakeCoverPromptProvider")
        assert isinstance(outcome, CoverLlmResult)
        return outcome

    def close(self) -> None:
        self.closed = True


class FakeCoverImageProvider:
    """封面出图 fake：记录每次调用的提示词。"""

    def __init__(self, outcomes: Iterable[CoverImageResult | Exception] = ()) -> None:
        self._outcomes: deque[Outcome] = deque(outcomes)
        self.calls: list[str] = []
        self.closed = False

    def queue(self, outcome: CoverImageResult | Exception) -> None:
        self._outcomes.append(outcome)

    def generate(self, *, prompt: str) -> CoverImageResult:
        self.calls.append(prompt)
        outcome = _pop(self._outcomes, "FakeCoverImageProvider")
        assert isinstance(outcome, CoverImageResult)
        return outcome

    def close(self) -> None:
        self.closed = True


class FakeCoverImageReviewProvider:
    """出图后视觉复审 fake：记录每次调用的消息列表。"""

    def __init__(self, outcomes: Iterable[CoverLlmResult | Exception] = ()) -> None:
        self._outcomes: deque[Outcome] = deque(outcomes)
        self.calls: list[list[Mapping[str, Any]]] = []
        self.closed = False

    def queue(self, outcome: CoverLlmResult | Exception) -> None:
        self._outcomes.append(outcome)

    def review(self, messages: Sequence[Mapping[str, Any]]) -> CoverLlmResult:
        self.calls.append(list(messages))
        outcome = _pop(self._outcomes, "FakeCoverImageReviewProvider")
        assert isinstance(outcome, CoverLlmResult)
        return outcome

    def close(self) -> None:
        self.closed = True


class FakeCoverAnchorImageProvider:
    """封面锚图出图 fake：记录每次调用的提示词与参考图字节数。"""

    def __init__(self, outcomes: Iterable[CoverImageResult | Exception] = ()) -> None:
        self._outcomes: deque[Outcome] = deque(outcomes)
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def queue(self, outcome: CoverImageResult | Exception) -> None:
        self._outcomes.append(outcome)

    def generate(self, *, prompt: str, references: tuple[bytes, ...]) -> CoverImageResult:
        self.calls.append((prompt, len(references)))
        outcome = _pop(self._outcomes, "FakeCoverAnchorImageProvider")
        assert isinstance(outcome, CoverImageResult)
        return outcome

    def close(self) -> None:
        self.closed = True
