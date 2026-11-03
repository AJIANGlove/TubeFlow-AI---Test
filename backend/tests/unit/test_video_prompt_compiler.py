from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import uuid4

import pytest

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    DirectorShot,
    ShotSentenceLink,
    shot_time_ranges_ms,
)
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import VideoPromptCompilerInput, VideoPromptStrategy
from tubeflow_test.video_prompt.provider import (
    PermanentVideoPromptProviderError,
    RetryableVideoPromptProviderError,
    VideoPromptProviderResponseError,
    VideoPromptProviderResult,
)


class StubProvider:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[Mapping[str, object]] = []
        self.closed = False

    def compile(
        self,
        *,
        system_prompt: str,
        user_payload: Mapping[str, object],
        model: str,
        idempotency_key: str,
    ) -> VideoPromptProviderResult:
        self.calls.append(user_payload)
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return VideoPromptProviderResult(str(outcome), "receipt", "0" * 64)

    def close(self) -> None:
        self.closed = True


def _input() -> VideoPromptCompilerInput:
    shot = DirectorShot(uuid4(), 1, "cave", "WIDE", "OBJECTIVE", "enters", "turn", "he enters")
    sentence_id = uuid4()
    ranges = shot_time_ranges_ms(
        (shot,), (ShotSentenceLink(shot.shot_id, sentence_id),), {sentence_id: (0, 5000)}
    )
    return VideoPromptCompilerInput(
        ("@图一",),
        "@图二",
        (shot,),
        ranges,
        5,
        "16:9",
        "style",
        VideoPromptStrategy.TUBEFLOW_ENHANCED_V1,
        "key",
    )


def _valid(value: VideoPromptCompilerInput) -> str:
    label = f"SHOT 1 · {value.shot_ranges[0].start_text}s–{value.shot_ranges[0].end_text}s"
    return (
        "subject_definitions:\n(S1) is the hero from Picture 1; the plan is Picture 2.\n"
        "summary: One line.\n"
        "retention_analysis: One line.\n"
        f"detailed_description: [0.0s–5.0s] {label}: he enters.\n"
        "overall_soundscape: wind.\n"
        "non_diegetic_music: N/A"
    )


def test_compile_success_inserts_binding_line_without_repair() -> None:
    value = _input()
    provider = StubProvider([_valid(value)])
    compiler = VideoPromptCompiler(provider, model="deepseek-v4-flash", strategy=value.strategy)
    compiled = compiler.compile(value)
    assert compiled.repaired is False
    assert compiled.model == "deepseek-v4-flash"
    assert compiled.input_fingerprint == value.fingerprint
    lines = compiled.text.splitlines()
    assert lines[0] == "subject_definitions:"
    assert lines[1].startswith("visual_asset_bindings: ")
    assert "@图一" in lines[1] and "@图二" in lines[1]
    assert len(provider.calls) == 1
    compiler.close()
    assert provider.closed is True


def test_compile_repairs_once_with_reason() -> None:
    value = _input()
    broken = _valid(value).replace("non_diegetic_music: N/A", "non_diegetic_music: piano")
    provider = StubProvider([broken, _valid(value)])
    compiler = VideoPromptCompiler(provider, model="m", strategy=value.strategy)
    compiled = compiler.compile(value)
    assert compiled.repaired is True
    assert len(provider.calls) == 2
    assert provider.calls[1].get("repair_reason") == "h3_prompt_music_forbidden"


def test_compile_shot_block_repair_receives_expected_and_actual_orders() -> None:
    value = replace(_input(), strategy=VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2)
    valid = (
        "subject_definitions:\n(S1) is the hero from Picture 1; Picture 2 is the storyboard. "
        "Treat each panel as a separate chronological shot beat, not as one composite image.\n"
        "summary: Follow panels from left to right and top to bottom.\n"
        "retention_analysis: One line.\n"
        "detailed_description:\n[Shot 1] The hero enters.\n"
        "Maintain identity and style.\n"
        "No storyboard grid, panel borders, split screen, text, or watermarks.\n"
        "overall_soundscape: wind.\n"
        "non_diegetic_music: N/A"
    )
    provider = StubProvider([valid.replace("[Shot 1]", "[Shot 2]"), valid])
    compiler = VideoPromptCompiler(provider, model="m", strategy=value.strategy)
    compiled = compiler.compile(value)
    assert compiled.repaired is True
    reason = str(provider.calls[1].get("repair_reason"))
    assert "h3_prompt_shot_blocks_invalid" in reason
    assert "expected=[1], actual=[2]" in reason


def test_compile_fails_without_fallback_after_failed_repair() -> None:
    value = _input()
    broken = _valid(value).replace("non_diegetic_music: N/A", "non_diegetic_music: piano")
    provider = StubProvider([broken, broken])
    compiler = VideoPromptCompiler(provider, model="m", strategy=value.strategy)
    with pytest.raises(DomainError) as captured:
        compiler.compile(value)
    assert captured.value.code == "video_prompt_compilation_failed"
    assert "h3_prompt_music_forbidden" in captured.value.message


def test_compile_repairs_unparseable_provider_response() -> None:
    value = _input()
    provider = StubProvider(
        [VideoPromptProviderResponseError("video_prompt_response_payload_invalid"), _valid(value)]
    )
    compiler = VideoPromptCompiler(provider, model="m", strategy=value.strategy)
    compiled = compiler.compile(value)
    assert compiled.repaired is True
    assert provider.calls[1].get("repair_reason") == "video_prompt_response_payload_invalid"


def test_compile_maps_permanent_and_retryable_provider_errors() -> None:
    value = _input()
    compiler = VideoPromptCompiler(
        StubProvider([PermanentVideoPromptProviderError("provider_http_401")]),
        model="m",
        strategy=value.strategy,
    )
    with pytest.raises(DomainError) as captured:
        compiler.compile(value)
    assert captured.value.code == "video_prompt_compilation_failed"

    # §15.14：可重试错误（429/5xx/偶发 4xx）在共享 3 次预算内退避重试。
    delays: list[float] = []
    provider = StubProvider([RetryableVideoPromptProviderError("provider_http_400"), _valid(value)])
    compiler = VideoPromptCompiler(
        provider, model="m", strategy=value.strategy, sleeper=delays.append
    )
    compiler.compile(value)
    assert len(provider.calls) == 2
    assert delays == [5.0]

    delays = []
    provider = StubProvider(
        [RetryableVideoPromptProviderError("provider_http_429") for _ in range(3)]
    )
    compiler = VideoPromptCompiler(
        provider, model="m", strategy=value.strategy, sleeper=delays.append
    )
    with pytest.raises(DomainError) as captured:
        compiler.compile(value)
    assert captured.value.code == "video_prompt_compilation_unavailable"
    assert len(provider.calls) == 3
    assert delays == [5.0, 15.0]

def test_compile_auto_mode_repairs_twice_within_shared_budget() -> None:
    # AUTO（max_repairs=2）：校验失败可两次修复重试；全程共享 3 次调用预算。
    value = _input()
    provider = StubProvider(["bad-1", "bad-2", _valid(value)])
    compiler = VideoPromptCompiler(
        provider, model="m", strategy=value.strategy, sleeper=lambda s: None
    )
    compiled = compiler.compile(value, max_repairs=2)
    assert compiled.repaired is True
    assert len(provider.calls) == 3
    assert provider.calls[1].get("repair_reason")
    assert provider.calls[2].get("repair_reason")


def test_compile_auto_mode_budget_exhaustion_fails() -> None:
    value = _input()
    provider = StubProvider(["bad", "bad", "bad"])
    compiler = VideoPromptCompiler(
        provider, model="m", strategy=value.strategy, sleeper=lambda s: None
    )
    with pytest.raises(DomainError) as captured:
        compiler.compile(value, max_repairs=2)
    assert captured.value.code == "video_prompt_compilation_failed"
    assert len(provider.calls) == 3


def test_compile_default_single_repair_unchanged() -> None:
    # MANUAL 默认仍是一次修复重试。
    value = _input()
    provider = StubProvider(["bad", "bad"])
    compiler = VideoPromptCompiler(
        provider, model="m", strategy=value.strategy, sleeper=lambda s: None
    )
    with pytest.raises(DomainError):
        compiler.compile(value)
    assert len(provider.calls) == 2

def test_compile_auto_mode_retries_transient_network_errors() -> None:
    # AUTO：网络层临时故障（ConnectError 等）进入共享预算退避；MANUAL 维持即时失败。
    import httpx as _httpx

    value = _input()
    provider = StubProvider([_httpx.ConnectError("down"), _valid(value)])
    delays: list[float] = []
    compiler = VideoPromptCompiler(
        provider, model="m", strategy=value.strategy, sleeper=delays.append
    )
    compiled = compiler.compile(value, max_repairs=2)
    assert compiled.repaired is False
    assert len(provider.calls) == 2
    assert delays == [5.0]

    provider = StubProvider([_httpx.ConnectError("down")])
    compiler = VideoPromptCompiler(provider, model="m", strategy=value.strategy)
    with pytest.raises(DomainError) as captured:
        compiler.compile(value)
    assert captured.value.code == "video_prompt_compilation_unavailable"
    assert len(provider.calls) == 1


def test_failed_compilation_records_each_attempt_diagnostic() -> None:
    value = _input()
    broken = _valid(value).replace("non_diegetic_music: N/A", "non_diegetic_music: piano")
    provider = StubProvider([broken, broken, broken])
    records: list[Mapping[str, object]] = []
    compiler = VideoPromptCompiler(
        provider,
        model="deepseek-v4-flash",
        strategy=value.strategy,
        diagnostics=records.append,
    )

    with pytest.raises(DomainError, match="stayed invalid"):
        compiler.compile(
            value,
            max_repairs=2,
            context={
                "owner_id": str(uuid4()),
                "project_id": str(uuid4()),
                "group_id": str(uuid4()),
                "attempt_id": str(uuid4()),
            },
        )

    assert len(records) == 3
    assert [item["attempt_index"] for item in records] == [0, 1, 2]
    assert all(str(item["reason"]).startswith("h3_prompt_") for item in records)
    assert all(item["output_text"] == broken for item in records)
    assert all(len(str(item["output_sha256"])) == 64 for item in records)
    assert records[0]["strategy"] == str(value.strategy)
    assert records[0]["project_id"] and records[0]["group_id"]
    compiler.close()


def test_provider_response_error_is_recorded_without_output() -> None:
    value = _input()
    provider = StubProvider(
        [VideoPromptProviderResponseError("provider_bad_json"), _valid(value)]
    )
    records: list[Mapping[str, object]] = []
    compiler = VideoPromptCompiler(
        provider,
        model="deepseek-v4-flash",
        strategy=value.strategy,
        diagnostics=records.append,
    )

    compiled = compiler.compile(value, max_repairs=1)

    assert compiled.repaired is True
    assert len(records) == 1
    assert records[0]["reason"] == "provider_bad_json"
    assert records[0]["output_text"] is None
    compiler.close()
