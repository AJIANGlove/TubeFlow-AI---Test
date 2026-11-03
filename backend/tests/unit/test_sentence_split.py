from __future__ import annotations

import hashlib
import json
from uuid import UUID

import httpx
import pytest

from tubeflow_test.adapters.deepseek_sentence_split import DeepSeekSentenceSplitProvider
from tubeflow_test.application.narration import ProjectNarrationService
from tubeflow_test.application.narration_ports import NarrationJob
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import ScriptSentence, segment_pasted_script
from tubeflow_test.sentence_split.domain import (
    SentenceSplit,
    SentenceSplitPlan,
    apply_sentence_split_plan,
    estimate_split_risk_sentences,
    sentence_split_plan_from_json,
    sentence_split_plan_to_json,
    validate_split_parts,
)
from tubeflow_test.sentence_split.provider import (
    PermanentSentenceSplitProviderError,
    RetryableSentenceSplitProviderError,
    SentenceSplitProviderResult,
    SentenceSplitResponseError,
)
from tubeflow_test.sentence_split.service import SentenceSplitService

OWNER = UUID("00000000-0000-0000-0000-000000000101")
PROJECT = UUID("00000000-0000-0000-0000-000000000110")
VOICE = UUID("00000000-0000-0000-0000-000000000120")

# --- 估算器边界 -------------------------------------------------------------


def _english(words: int) -> str:
    return " ".join(["word"] * words) + "."


@pytest.mark.parametrize(
    ("text", "locale", "risky"),
    [
        # 拉丁语系：max(词数/2.5, 字符数/15) 秒；12.0s 整不标风险，超出才标。
        (_english(30), SupportedLocale.EN_US, False),
        (_english(31), SupportedLocale.EN_US, True),
        ("x" * 179 + ".", SupportedLocale.ES_ES, False),
        ("x" * 180 + ".", SupportedLocale.ES_ES, True),
        # 日文：字符数/7.0；84 字符 = 12.0s 整。
        ("あ" * 83 + "。", SupportedLocale.JA_JP, False),
        ("あ" * 84 + "。", SupportedLocale.JA_JP, True),
        # 韩文：字符数/5.5；66 字符 = 12.0s 整。
        ("한" * 65 + ".", SupportedLocale.KO_KR, False),
        ("한" * 66 + ".", SupportedLocale.KO_KR, True),
    ],
)
def test_estimate_split_risk_threshold_boundaries(
    text: str, locale: SupportedLocale, risky: bool
) -> None:
    script = segment_pasted_script(text, locale)
    risk = estimate_split_risk_sentences(script, 12.0)
    assert bool(risk) is risky


def test_estimate_split_risk_custom_threshold_and_separator_excluded() -> None:
    script = segment_pasted_script(_english(26), SupportedLocale.EN_US)
    assert estimate_split_risk_sentences(script, 10.0)  # 26 词 = 10.4s > 10s
    assert not estimate_split_risk_sentences(script, 12.0)
    long_separator = ScriptSentence(1, "-" * 200, hashlib.sha256(b"-" * 200).hexdigest())
    separators = type(script)(
        script.language, script.rule_version, script.source_sha256, (long_separator,)
    )
    assert not estimate_split_risk_sentences(separators, 0.1)


# --- 无损校验 ---------------------------------------------------------------


def test_validate_split_parts_accepts_exact_and_outer_whitespace() -> None:
    original = "Hello world, foo bar."
    assert validate_split_parts(original, ["Hello world, ", "foo bar."]) == (
        "Hello world, ",
        "foo bar.",
    )
    # 整体首尾空白差异按原句归一：返回原句的精确切片。
    assert validate_split_parts(original, ["Hello world, ", "foo bar.\n"]) == (
        "Hello world, ",
        "foo bar.",
    )
    assert validate_split_parts(original, ["\nHello world, ", "foo bar."]) == (
        "Hello world, ",
        "foo bar.",
    )


def test_validate_split_parts_realigns_boundary_whitespace() -> None:
    original = "Hello world, foo bar."
    # 模型在切分点丢/加边界空白：以原句为准复原，拼接逐字符等于原句。
    assert validate_split_parts(original, ["Hello world,", "foo bar."]) == (
        "Hello world, ",
        "foo bar.",
    )
    assert validate_split_parts(original, ["Hello world, ", "foo bar"]) == (
        "Hello world, ",
        "foo bar.",
    )
    assert validate_split_parts(original, ["Hello world,  ", "foo bar."]) == (
        "Hello world, ",
        "foo bar.",
    )
    # 生产实例（2026-09 真实验收复现）：em-dash 切分点空格丢失。
    sentence = (
        "The instinct to seek cover \u2014 to read the sky, feel the pressure drop, "
        "and move toward stone \u2014 that behavior was already running in bodies "
        "that weren't even fully human."
    )
    parts = validate_split_parts(
        sentence,
        [
            (
                "The instinct to seek cover \u2014 to read the sky, feel the pressure drop, "
                "and move toward stone \u2014"
            ),
            "that behavior was already running",
            "in bodies that weren't even fully human.",
        ],
    )
    assert "".join(parts) == sentence
    assert parts[0].endswith("\u2014 ")
    assert parts[1] == "that behavior was already running "


@pytest.mark.parametrize(
    "parts",
    [
        ["Hello world, ", "bar foo."],  # 改词
        ["Hello world, ", "foo  bar."],  # 段内多空白
        ["Hello world, ", "foobar."],  # 段内丢字
        ["Hello world, foo bar.", "extra."],  # 加词
    ],
)
def test_validate_split_parts_rejects_lossy(parts: list[str]) -> None:
    with pytest.raises(DomainError) as failure:
        validate_split_parts("Hello world, foo bar.", parts)
    assert failure.value.code == "sentence_split_lossy"


@pytest.mark.parametrize(
    ("parts", "code"),
    [
        (["Hello world, foo bar."], "sentence_split_contract_invalid"),  # 少于 2 段
        (["Hello world, ", " ", "foo bar."], "sentence_split_contract_invalid"),  # 空白段
        (["a"] * 9, "sentence_split_contract_invalid"),  # 超过 8 段
        (["Hello world, ", 3], "sentence_split_contract_invalid"),  # 非字符串
    ],
)
def test_validate_split_parts_rejects_contract_violations(
    parts: list[object], code: str
) -> None:
    with pytest.raises(DomainError) as failure:
        validate_split_parts("Hello world, foo bar.", parts)  # type: ignore[arg-type]
    assert failure.value.code == code


# --- 计划应用与血缘 serde ----------------------------------------------------


def _split_for(script_text: str, sequence: int, parts: tuple[str, ...]) -> SentenceSplitPlan:
    script = segment_pasted_script(script_text, SupportedLocale.EN_US)
    sentence = next(item for item in script.sentences if item.sequence == sequence)
    return SentenceSplitPlan(
        (SentenceSplit(sequence, sentence.text_sha256, parts, "req-1", "ab" * 32),)
    ), script


def test_apply_split_plan_resequences_and_bumps_rule_version() -> None:
    plan, script = _split_for(
        "First sentence. Hello world, foo bar. Last one.", 2, ("Hello world, ", "foo bar.")
    )
    applied = apply_sentence_split_plan(script, plan)
    assert applied.rule_version == "punctuation-v3+sentence-split-v1"
    assert applied.source_sha256 == script.source_sha256
    assert [item.text for item in applied.sentences] == [
        "First sentence.",
        "Hello world, ",  # parts 原样保留（含边界空白）
        "foo bar.",
        "Last one.",
    ]
    assert [item.sequence for item in applied.sentences] == [1, 2, 3, 4]
    for item in applied.sentences:
        assert item.text_sha256 == hashlib.sha256(item.text.encode()).hexdigest()
    # 无损：忽略空白后与 v3 结果一致。
    assert "".join("".join(item.text.split()) for item in applied.sentences) == "".join(
        "".join(item.text.split()) for item in script.sentences
    )


def test_apply_empty_plan_is_identity() -> None:
    script = segment_pasted_script("First sentence.", SupportedLocale.EN_US)
    applied = apply_sentence_split_plan(script, SentenceSplitPlan(()))
    assert applied is script


def test_apply_split_plan_rejects_unknown_duplicate_hash_and_separator() -> None:
    script = segment_pasted_script("First sentence. Second one.", SupportedLocale.EN_US)
    sentence = script.sentences[0]
    with pytest.raises(DomainError) as failure:
        apply_sentence_split_plan(
            script, SentenceSplitPlan((SentenceSplit(99, "a" * 64, ("a", "b")),))
        )
    assert failure.value.code == "sentence_split_plan_invalid"
    with pytest.raises(DomainError) as failure:
        apply_sentence_split_plan(
            script,
            SentenceSplitPlan(
                (
                    SentenceSplit(1, sentence.text_sha256, ("First ", "sentence.")),
                    SentenceSplit(1, sentence.text_sha256, ("First ", "sentence.")),
                )
            ),
        )
    assert failure.value.code == "sentence_split_plan_invalid"
    with pytest.raises(DomainError) as failure:
        apply_sentence_split_plan(
            script, SentenceSplitPlan((SentenceSplit(1, "b" * 64, ("First ", "sentence.")),))
        )
    assert failure.value.code == "sentence_split_plan_invalid"
    separator_script = segment_pasted_script("Real sentence.\n---", SupportedLocale.EN_US)
    separator = next(item for item in separator_script.sentences if item.text == "---")
    with pytest.raises(DomainError) as failure:
        apply_sentence_split_plan(
            separator_script,
            SentenceSplitPlan(
                (SentenceSplit(separator.sequence, separator.text_sha256, ("--", "-")),)
            ),
        )
    assert failure.value.code == "sentence_split_plan_invalid"


def test_apply_split_plan_replays_lossless_validation() -> None:
    plan, script = _split_for("Hello world, foo bar.", 1, ("Hello world, ", "foo baz."))
    with pytest.raises(DomainError) as failure:
        apply_sentence_split_plan(script, plan)
    assert failure.value.code == "sentence_split_lossy"


def test_split_plan_serde_round_trip_and_fail_closed() -> None:
    plan, _script = _split_for("Hello world, foo bar.", 1, ("Hello world, ", "foo bar."))
    payload = sentence_split_plan_to_json(plan)
    assert sentence_split_plan_from_json(payload) == plan
    assert sentence_split_plan_from_json(None) is None
    for malformed in (
        {"version": "other", "items": []},
        {"version": "sentence-split-v1", "items": "nope"},
        {"version": "sentence-split-v1", "items": [{"parts": "nope"}]},
        {"version": "sentence-split-v1", "items": [{"source_sequence": 1, "parts": ["a"]}]},
    ):
        with pytest.raises(DomainError) as failure:
            sentence_split_plan_from_json(malformed)
        assert failure.value.code == "sentence_split_lineage_invalid"


# --- 应用服务（Stub Provider）-----------------------------------------------


class _StubProvider:
    def __init__(
        self,
        parts: tuple[str, ...] | None = None,
        errors: list[Exception] | None = None,
        parts_sequence: list[tuple[str, ...]] | None = None,
    ) -> None:
        self.parts = parts
        self.errors = list(errors or [])
        self.parts_sequence = list(parts_sequence or [])
        self.calls: list[dict[str, str]] = []

    async def split(self, **kwargs: object) -> SentenceSplitProviderResult:
        self.calls.append({key: str(value) for key, value in kwargs.items()})
        if self.errors:
            raise self.errors.pop(0)
        if self.parts_sequence:
            return SentenceSplitProviderResult(self.parts_sequence.pop(0), "receipt-1", "cd" * 32)
        assert self.parts is not None
        return SentenceSplitProviderResult(self.parts, "receipt-1", "cd" * 32)


class _FakeSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _long_script() -> tuple[object, str]:
    long_sentence = _english(40)
    script = segment_pasted_script(f"Intro line. {long_sentence}", SupportedLocale.EN_US)
    return script, long_sentence


def _service(provider: _StubProvider | None, sleeper: _FakeSleeper) -> SentenceSplitService:
    return SentenceSplitService(
        provider,  # type: ignore[arg-type]
        model="deepseek-v4-flash",
        sleeper=sleeper,
    )


@pytest.mark.asyncio
async def test_service_skips_provider_when_no_risk_sentence() -> None:
    provider = _StubProvider(parts=("a", "b"))
    script = segment_pasted_script("Short one.", SupportedLocale.EN_US)
    plan = await _service(provider, _FakeSleeper()).plan_splits(script)
    assert plan.splits == ()
    assert provider.calls == []


@pytest.mark.asyncio
async def test_service_splits_only_risk_sentences_with_lineage() -> None:
    script, long_sentence = _long_script()
    cut = long_sentence.index(" ", len(long_sentence) // 2)
    parts = (long_sentence[: cut + 1], long_sentence[cut + 1 :])
    provider = _StubProvider(parts=parts)
    plan = await _service(provider, _FakeSleeper()).plan_splits(script)
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["sentence_text"] == long_sentence
    assert call["model"] == "deepseek-v4-flash"
    assert call["idempotency_key"].startswith("sentence-split:")
    assert len(plan.splits) == 1
    split = plan.splits[0]
    assert split.source_sequence == 2
    assert split.parts == parts
    assert split.provider_receipt == "receipt-1"
    applied = apply_sentence_split_plan(script, plan)  # type: ignore[arg-type]
    assert len(applied.sentences) == 3


@pytest.mark.asyncio
async def test_service_retries_retryable_errors_with_backoff_and_retry_after() -> None:
    script, long_sentence = _long_script()
    cut = long_sentence.index(" ", len(long_sentence) // 2)
    parts = (long_sentence[: cut + 1], long_sentence[cut + 1 :])
    provider = _StubProvider(
        parts=parts,
        errors=[
            RetryableSentenceSplitProviderError("provider_http_500"),
            RetryableSentenceSplitProviderError("provider_http_429", retry_after_seconds=20.0),
        ],
    )
    sleeper = _FakeSleeper()
    plan = await _service(provider, sleeper).plan_splits(script)  # type: ignore[arg-type]
    assert len(plan.splits) == 1
    assert len(provider.calls) == 3
    assert sleeper.delays == [5.0, 20.0]  # 5s 固定退避；429 尊重更长的 Retry-After


@pytest.mark.asyncio
async def test_service_retry_budget_exhaustion_fails_submission() -> None:
    script, _long_sentence = _long_script()
    provider = _StubProvider(
        errors=[RetryableSentenceSplitProviderError("provider_timeout")] * 3
    )
    sleeper = _FakeSleeper()
    with pytest.raises(DomainError) as failure:
        await _service(provider, sleeper).plan_splits(script)  # type: ignore[arg-type]
    assert failure.value.code == "sentence_split_unavailable"
    assert len(provider.calls) == 3
    assert sleeper.delays == [5.0, 15.0]


@pytest.mark.asyncio
async def test_service_permanent_errors_are_not_retried() -> None:
    script, _long_sentence = _long_script()
    provider = _StubProvider(errors=[PermanentSentenceSplitProviderError("provider_http_400")])
    with pytest.raises(DomainError) as failure:
        await _service(provider, _FakeSleeper()).plan_splits(script)  # type: ignore[arg-type]
    assert failure.value.code == "sentence_split_provider_rejected"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_service_contract_errors_are_retried_within_budget() -> None:
    script, _long_sentence = _long_script()
    provider = _StubProvider(
        errors=[SentenceSplitResponseError("sentence_split_response_payload_invalid")] * 3
    )
    sleeper = _FakeSleeper()
    with pytest.raises(DomainError) as failure:
        await _service(provider, sleeper).plan_splits(script)  # type: ignore[arg-type]
    assert failure.value.code == "sentence_split_response_invalid"
    assert len(provider.calls) == 3
    assert sleeper.delays == [5.0, 15.0]
    # 内容无效的重试携带修复提示并更换幂等键，避开 Provider 幂等缓存。
    assert provider.calls[0]["repair_reason"] == "None"
    assert provider.calls[1]["repair_reason"] != "None"
    assert provider.calls[1]["idempotency_key"].endswith(":a1")


@pytest.mark.asyncio
async def test_service_lossy_split_is_retried_within_budget() -> None:
    script, _long_sentence = _long_script()
    provider = _StubProvider(parts=("nonexistent-anchor-1", "nonexistent-anchor-2"))
    sleeper = _FakeSleeper()
    with pytest.raises(DomainError) as failure:
        await _service(provider, sleeper).plan_splits(script)  # type: ignore[arg-type]
    assert failure.value.code == "sentence_split_lossy"
    assert len(provider.calls) == 3
    assert sleeper.delays == [5.0, 15.0]


@pytest.mark.asyncio
async def test_service_lossy_split_recovers_with_repair_reason() -> None:
    script, long_sentence = _long_script()
    cut = long_sentence.index(" ", len(long_sentence) // 2)
    good = (long_sentence[: cut + 1], long_sentence[cut + 1 :])
    provider = _StubProvider(
        parts_sequence=[("nonexistent-anchor-1", "nonexistent-anchor-2"), good]
    )
    sleeper = _FakeSleeper()
    plan = await _service(provider, sleeper).plan_splits(script)  # type: ignore[arg-type]
    assert len(provider.calls) == 2
    assert sleeper.delays == [5.0]
    assert provider.calls[1]["repair_reason"] != "None"
    assert provider.calls[1]["idempotency_key"].endswith(":a1")
    assert plan.splits[0].parts == good


@pytest.mark.asyncio
async def test_service_requires_provider_for_risk_sentences() -> None:
    script, _long_sentence = _long_script()
    with pytest.raises(DomainError) as failure:
        await _service(None, _FakeSleeper()).plan_splits(script)  # type: ignore[arg-type]
    assert failure.value.code == "sentence_split_provider_not_configured"


@pytest.mark.asyncio
async def test_service_splits_risk_sentences_concurrently_and_fails_as_whole() -> None:
    first = _english(40)
    second = _english(50)
    script = segment_pasted_script(f"{first} {second}", SupportedLocale.EN_US)
    assert len(script.sentences) == 2
    provider = _StubProvider(errors=[PermanentSentenceSplitProviderError("provider_http_422")])
    with pytest.raises(DomainError) as failure:
        await _service(provider, _FakeSleeper()).plan_splits(script)  # type: ignore[arg-type]
    assert failure.value.code == "sentence_split_provider_rejected"


# --- 配置组校验 -------------------------------------------------------------


def test_sentence_split_settings_defaults_and_validation() -> None:
    from pydantic import SecretStr, ValidationError

    from tubeflow_test.config import Settings

    settings = Settings()
    assert settings.sentence_split_provider_url == "https://api.deepseek.com/v1"
    assert settings.sentence_split_provider_model == "deepseek-v4-flash"
    assert settings.sentence_split_estimated_seconds_threshold == 12.0
    assert not settings.sentence_split_provider_configured

    configured = Settings(sentence_split_provider_token=SecretStr("token"))
    assert configured.sentence_split_provider_configured

    with pytest.raises(ValidationError, match="Sentence split Provider URL"):
        Settings(
            sentence_split_provider_token=SecretStr("token"),
            sentence_split_provider_url="not-a-url",
        )
    with pytest.raises(ValidationError, match="Sentence split Provider model"):
        Settings(
            sentence_split_provider_token=SecretStr("token"),
            sentence_split_provider_model=" ",
        )
    with pytest.raises(ValidationError):
        Settings(sentence_split_estimated_seconds_threshold=0)
    with pytest.raises(ValidationError):
        Settings(sentence_split_estimated_seconds_threshold=15.1)


# --- DeepSeek 适配器（MockTransport，无真实调用）---------------------------


def _split_response(request: httpx.Request, parts: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "req-split-1"},
        json={
            "id": "chatcmpl-split",
            "choices": [{"message": {"content": json.dumps({"parts": parts})}}],
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_deepseek_sentence_split_request_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _split_response(request, ["Hello world, ", "foo bar."])

    provider = DeepSeekSentenceSplitProvider(
        "https://api.deepseek.com/v1",
        "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await provider.split(
        system_prompt="system",
        sentence_text="Hello world, foo bar.",
        locale="en-US",
        model="deepseek-v4-flash",
        idempotency_key="key-1",
    )
    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer token"
    assert request.headers["Idempotency-Key"] == "key-1"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    user = json.loads(body["messages"][1]["content"])
    assert user == {
        "json_task": "split_overlong_sentence",
        "language": "en-US",
        "sentence": "Hello world, foo bar.",
    }
    assert result.parts == ("Hello world, ", "foo bar.")
    assert result.receipt == "req-split-1"  # 安全回执：不含响应正文
    assert len(result.raw_response_sha256) == 64


@pytest.mark.asyncio
async def test_deepseek_sentence_split_classifies_http_errors() -> None:
    for status in (401, 403):
        provider = DeepSeekSentenceSplitProvider(
            "https://api.deepseek.com/v1",
            "token",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, code=status: httpx.Response(code, request=request)
                )
            ),
        )
        with pytest.raises(PermanentSentenceSplitProviderError, match=f"provider_http_{status}"):
            await provider.split(
                system_prompt="s", sentence_text="t", locale="en-US", model="m",
                idempotency_key="k",
            )
    # §15.14：其余 4xx 为平台偶发抖动，归入可重试。
    for status in (400, 404, 409, 410, 422):
        provider = DeepSeekSentenceSplitProvider(
            "https://api.deepseek.com/v1",
            "token",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, code=status: httpx.Response(code, request=request)
                )
            ),
        )
        with pytest.raises(RetryableSentenceSplitProviderError, match=f"provider_http_{status}"):
            await provider.split(
                system_prompt="s", sentence_text="t", locale="en-US", model="m",
                idempotency_key="k",
            )
    provider = DeepSeekSentenceSplitProvider(
        "https://api.deepseek.com/v1",
        "token",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, headers={"retry-after": "7"}, request=request)
            )
        ),
    )
    with pytest.raises(RetryableSentenceSplitProviderError) as captured:
        await provider.split(
            system_prompt="s", sentence_text="t", locale="en-US", model="m", idempotency_key="k"
        )
    assert captured.value.retry_after_seconds == 7.0
    provider = DeepSeekSentenceSplitProvider(
        "https://api.deepseek.com/v1",
        "token",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, request=request)
            )
        ),
    )
    with pytest.raises(RetryableSentenceSplitProviderError, match="provider_http_500"):
        await provider.split(
            system_prompt="s", sentence_text="t", locale="en-US", model="m", idempotency_key="k"
        )


@pytest.mark.asyncio
async def test_deepseek_sentence_split_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    provider = DeepSeekSentenceSplitProvider(
        "https://api.deepseek.com/v1",
        "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetryableSentenceSplitProviderError, match="provider_timeout"):
        await provider.split(
            system_prompt="s", sentence_text="t", locale="en-US", model="m", idempotency_key="k"
        )


@pytest.mark.asyncio
async def test_deepseek_sentence_split_rejects_bad_payloads() -> None:
    cases = [
        httpx.Response(200, content=b"not-json", request=httpx.Request("POST", "http://x")),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not-json"}}]},
            request=httpx.Request("POST", "http://x"),
        ),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"wrong": 1})}}]},
            request=httpx.Request("POST", "http://x"),
        ),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"parts": "nope"})}}]},
            request=httpx.Request("POST", "http://x"),
        ),
    ]
    for response in cases:
        provider = DeepSeekSentenceSplitProvider(
            "https://api.deepseek.com/v1",
            "token",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request, r=response: r)
            ),
        )
        with pytest.raises(SentenceSplitResponseError):
            await provider.split(
                system_prompt="s", sentence_text="t", locale="en-US", model="m",
                idempotency_key="k",
            )


# --- 确认配音路径（AUTO 冻结前介入 / MANUAL 不变 / 幂等重入）----------------


class _ConfirmProjectPort:
    """按 SqlProjectSnapshotAdapter 的合同模拟冻结：应用拆分计划并记录。"""

    def __init__(self, text: str, locale: SupportedLocale = SupportedLocale.EN_US) -> None:
        self.value = (text, locale, VOICE)
        self.frozen = False
        self.enqueue_calls = 0
        self.captured_plan: SentenceSplitPlan | None = None
        self.frozen_script = None

    def get_draft_script(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        assert (owner_id, project_id) == (OWNER, PROJECT)
        return self.value

    def get_frozen_narration(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        if not self.frozen:
            raise DomainError("not_found", "Resource not found.")
        return self.value

    def confirm_and_enqueue(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        sentence_splits: SentenceSplitPlan | None = None,
    ) -> tuple[object, ...]:
        self.enqueue_calls += 1
        self.captured_plan = sentence_splits
        script = segment_pasted_script(self.value[0], self.value[1])
        if sentence_splits is not None and sentence_splits.splits:
            script = apply_sentence_split_plan(script, sentence_splits)
        self.frozen = True
        self.frozen_script = script
        job = NarrationJob(
            "job-1",
            owner_id,
            project_id,
            tuple(item.sequence for item in script.sentences),
            "PENDING",
        )
        return (*self.value, job)


class _NoJobs:
    async def start(self, **_kwargs: object) -> str:
        raise AssertionError("job dispatch is not part of confirmation")


class _Writable:
    def assert_writable(self, owner_id: UUID, project_id: UUID) -> None:
        assert (owner_id, project_id) == (OWNER, PROJECT)


def _confirm_service(
    repo: object,
    projects: _ConfirmProjectPort,
    splitter: SentenceSplitService | None,
) -> ProjectNarrationService:
    from tubeflow_test.adapters.persistence.narration import InMemoryNarrationRepository

    assert isinstance(repo, InMemoryNarrationRepository)
    return ProjectNarrationService(
        projects,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repo,
        None,  # type: ignore[arg-type]
        _NoJobs(),  # type: ignore[arg-type]
        _Writable(),  # type: ignore[arg-type]
        sentence_splitter=splitter,
    )


@pytest.mark.asyncio
async def test_auto_confirm_splits_risk_sentence_before_freezing() -> None:
    from tubeflow_test.adapters.persistence.narration import InMemoryNarrationRepository

    long_sentence = _english(40)
    cut = long_sentence.index(" ", len(long_sentence) // 2)
    provider = _StubProvider(parts=(long_sentence[: cut + 1], long_sentence[cut + 1 :]))
    splitter = SentenceSplitService(provider, model="deepseek-v4-flash", sleeper=_FakeSleeper())  # type: ignore[arg-type]
    repo = InMemoryNarrationRepository()
    repo.production_mode = "AUTO"
    projects = _ConfirmProjectPort(f"Intro line. {long_sentence}")
    service = _confirm_service(repo, projects, splitter)

    result = await service.confirm_and_start(OWNER, PROJECT)

    assert result["job_id"] == "job-1"
    assert len(provider.calls) == 1
    assert projects.captured_plan is not None and len(projects.captured_plan.splits) == 1
    frozen = projects.frozen_script
    assert frozen is not None
    assert frozen.rule_version == "punctuation-v3+sentence-split-v1"
    assert [item.text for item in frozen.sentences][:1] == ["Intro line."]
    assert len(frozen.sentences) == 3


@pytest.mark.asyncio
async def test_auto_confirm_replay_does_not_call_provider_again() -> None:
    from tubeflow_test.adapters.persistence.narration import InMemoryNarrationRepository

    provider = _StubProvider(parts=("a", "b"))
    splitter = SentenceSplitService(provider, model="m", sleeper=_FakeSleeper())  # type: ignore[arg-type]
    repo = InMemoryNarrationRepository()
    repo.production_mode = "AUTO"
    projects = _ConfirmProjectPort("Short one.")
    service = _confirm_service(repo, projects, splitter)

    await service.confirm_and_start(OWNER, PROJECT)
    assert provider.calls == []  # 无风险句，不调用
    assert projects.captured_plan is not None and projects.captured_plan.splits == ()
    # 幂等重入：已冻结项目不再触发 agent。
    await service.confirm_and_start(OWNER, PROJECT)
    assert provider.calls == []
    assert projects.enqueue_calls == 2


@pytest.mark.asyncio
async def test_auto_confirm_agent_failure_persists_nothing() -> None:
    from tubeflow_test.adapters.persistence.narration import InMemoryNarrationRepository

    provider = _StubProvider(errors=[PermanentSentenceSplitProviderError("provider_http_400")])
    splitter = SentenceSplitService(provider, model="m", sleeper=_FakeSleeper())  # type: ignore[arg-type]
    repo = InMemoryNarrationRepository()
    repo.production_mode = "AUTO"
    projects = _ConfirmProjectPort(f"Intro line. {_english(40)}")
    service = _confirm_service(repo, projects, splitter)

    with pytest.raises(DomainError) as failure:
        await service.confirm_and_start(OWNER, PROJECT)
    assert failure.value.code == "sentence_split_provider_rejected"
    assert projects.enqueue_calls == 0  # 零落库：不落快照、不落 job
    assert not projects.frozen


@pytest.mark.asyncio
async def test_auto_confirm_without_splitter_fails_closed() -> None:
    from tubeflow_test.adapters.persistence.narration import InMemoryNarrationRepository

    repo = InMemoryNarrationRepository()
    repo.production_mode = "AUTO"
    projects = _ConfirmProjectPort("Short one.")
    service = _confirm_service(repo, projects, None)
    with pytest.raises(DomainError) as failure:
        await service.confirm_and_start(OWNER, PROJECT)
    assert failure.value.code == "sentence_split_not_configured"
    assert projects.enqueue_calls == 0


@pytest.mark.asyncio
async def test_manual_confirm_never_touches_the_split_agent() -> None:
    from tubeflow_test.adapters.persistence.narration import InMemoryNarrationRepository

    class _ForbiddenProvider:
        async def split(self, **_kwargs: object) -> object:
            raise AssertionError("MANUAL projects must not call the split provider")

    splitter = SentenceSplitService(_ForbiddenProvider(), model="m", sleeper=_FakeSleeper())  # type: ignore[arg-type]
    repo = InMemoryNarrationRepository()  # 默认 MANUAL
    projects = _ConfirmProjectPort(f"Intro line. {_english(40)}")
    service = _confirm_service(repo, projects, splitter)

    result = await service.confirm_and_start(OWNER, PROJECT)

    assert result["job_id"] == "job-1"
    assert projects.captured_plan is None  # MANUAL：纯 v3，不传拆分计划
    frozen = projects.frozen_script
    assert frozen is not None
    assert frozen.rule_version == "punctuation-v3"
    assert len(frozen.sentences) == 2
