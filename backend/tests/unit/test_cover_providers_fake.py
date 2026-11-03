from __future__ import annotations

import pytest

from tubeflow_test.adapters.in_memory_cover_providers import (
    FakeCoverImageProvider,
    FakeCoverPromptProvider,
    FakeCoverRefAnalysisProvider,
)
from tubeflow_test.application.cover_providers import (
    CoverImageProvider,
    CoverImageResult,
    CoverLlmResult,
    CoverPromptProvider,
    CoverRefAnalysisProvider,
)
from tubeflow_test.domain.errors import DomainError

MESSAGES = [{"role": "user", "content": "hello"}]


def _llm_result(model: str = "gpt-5.5") -> CoverLlmResult:
    return CoverLlmResult({"ok": True}, "receipt-1", model, "0" * 64)


def _image_result() -> CoverImageResult:
    return CoverImageResult(b"image-bytes", "1" * 64, "receipt-2", "b64_json")


def _run_analysis(provider: CoverRefAnalysisProvider) -> CoverLlmResult:
    return provider.analyze(MESSAGES)


def _run_prompt(provider: CoverPromptProvider) -> CoverLlmResult:
    return provider.complete(MESSAGES, temperature=0.9)


def _run_image(provider: CoverImageProvider) -> CoverImageResult:
    return provider.generate(prompt="prompt")


def test_fake_analysis_provider_matches_protocol_and_records_calls() -> None:
    fake = FakeCoverRefAnalysisProvider([_llm_result()])
    result = _run_analysis(fake)  # 以 Protocol 类型调用：结构一致
    assert result.payload == {"ok": True}
    assert fake.calls == [MESSAGES]
    fake.close()
    assert fake.closed is True


def test_fake_prompt_provider_records_temperature() -> None:
    fake = FakeCoverPromptProvider([_llm_result("deepseek-v4-flash")])
    result = _run_prompt(fake)
    assert result.model == "deepseek-v4-flash"
    assert fake.calls == [(MESSAGES, 0.9)]


def test_fake_image_provider_records_prompts() -> None:
    fake = FakeCoverImageProvider([_image_result()])
    result = _run_image(fake)
    assert result.sha256 == "1" * 64
    assert fake.calls == ["prompt"]


def test_fakes_raise_scripted_exceptions_in_order() -> None:
    error = DomainError("cover_llm_provider_timeout", "封面 LLM 请求超时。")
    fake = FakeCoverPromptProvider([error, _llm_result()])
    with pytest.raises(DomainError) as captured:
        fake.complete(MESSAGES, temperature=0.8)
    assert captured.value.code == "cover_llm_provider_timeout"
    assert fake.complete(MESSAGES, temperature=0.8).payload == {"ok": True}
    assert len(fake.calls) == 2


def test_fakes_fail_fast_on_unscripted_call() -> None:
    with pytest.raises(AssertionError, match="unscripted call"):
        FakeCoverRefAnalysisProvider().analyze(MESSAGES)
    with pytest.raises(AssertionError, match="unscripted call"):
        FakeCoverImageProvider().generate(prompt="p")


def test_fake_queue_appends_outcomes() -> None:
    fake = FakeCoverImageProvider()
    fake.queue(_image_result())
    assert fake.generate(prompt="p").content == b"image-bytes"
