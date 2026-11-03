"""拆句 agent 应用服务：v3 确定性分句 → 估算风险句 → 并发 LLM 二级拆分 → 无损校验。

§15.7 / 验收 §18：只对风险句调用；每句最多 3 次尝试，429/5xx/超时按 5s/15s/30s
退避（429 尊重 Retry-After）；任一环节最终失败则整个提交失败，不落任何快照/job。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import ScriptSentence, ScriptVersion
from tubeflow_test.sentence_split.domain import (
    SentenceSplit,
    SentenceSplitPlan,
    estimate_split_risk_sentences,
    validate_split_parts,
)
from tubeflow_test.sentence_split.provider import (
    PermanentSentenceSplitProviderError,
    RetryableSentenceSplitProviderError,
    SentenceSplitProvider,
    SentenceSplitResponseError,
)

SENTENCE_SPLIT_SYSTEM_PROMPT = (
    "You split one over-long narration sentence into 2 to 8 shorter consecutive parts. "
    "Never change the wording: the concatenation of the parts must equal the original "
    "sentence character by character, preserving every word, space and punctuation mark "
    "in order. Do not translate, summarize, correct or normalize. "
    'Respond with JSON only: {"parts": ["<part 1>", "<part 2>", ...]}.'
    " Preserve whitespace exactly, especially at split boundaries and around "
    "punctuation such as dashes; never drop or add spaces."
)


class SentenceSplitService:
    def __init__(
        self,
        provider: SentenceSplitProvider | None,
        *,
        model: str,
        threshold_seconds: float = 12.0,
        max_attempts: int = 3,
        backoff_seconds: tuple[float, ...] = (5.0, 15.0, 30.0),
        max_concurrency: int = 4,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if threshold_seconds <= 0 or threshold_seconds > 15:
            raise ValueError("Sentence split threshold must be within (0, 15] seconds")
        if max_attempts < 1 or not backoff_seconds:
            raise ValueError("Sentence split attempts and backoff must be configured")
        self._provider = provider
        self._model = model
        self._threshold_seconds = threshold_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sleeper = sleeper

    async def plan_splits(self, script: ScriptVersion) -> SentenceSplitPlan:
        risk = estimate_split_risk_sentences(script, self._threshold_seconds)
        if not risk:
            return SentenceSplitPlan(())
        if self._provider is None:
            raise DomainError(
                "sentence_split_provider_not_configured",
                "Sentence split Provider is required for over-long sentences.",
            )
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._split_one(script, sentence)) for sentence in risk]
        except BaseExceptionGroup as grouped:
            # 保持提交失败的稳定错误码不被 ExceptionGroup 包裹。
            domain_error = _first_domain_error(grouped)
            if domain_error is not None:
                raise domain_error from grouped
            raise
        return SentenceSplitPlan(tuple(task.result() for task in tasks))

    async def _split_one(self, script: ScriptVersion, sentence: ScriptSentence) -> SentenceSplit:
        idempotency_key = (
            f"sentence-split:{script.source_sha256}:{sentence.sequence}:"
            f"{sentence.text_sha256[:12]}"
        )
        repair_reason: str | None = None
        attempt_key = idempotency_key
        async with self._semaphore:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    assert self._provider is not None
                    result = await self._provider.split(
                        system_prompt=SENTENCE_SPLIT_SYSTEM_PROMPT,
                        sentence_text=sentence.text,
                        locale=script.language.value,
                        model=self._model,
                        idempotency_key=attempt_key,
                        repair_reason=repair_reason,
                    )
                except RetryableSentenceSplitProviderError as error:
                    if attempt >= self._max_attempts:
                        raise DomainError(
                            "sentence_split_unavailable",
                            "Sentence split Provider stayed unavailable within the retry budget.",
                        ) from error
                    await self._sleeper(self._backoff(attempt, error.retry_after_seconds))
                    continue
                except PermanentSentenceSplitProviderError as error:
                    raise DomainError(
                        "sentence_split_provider_rejected",
                        "Sentence split Provider rejected the request.",
                    ) from error
                except SentenceSplitResponseError as error:
                    # 输出合同不合法（2xx 但非约定 JSON）：输出不确定性，预算内修复重试。
                    if attempt >= self._max_attempts:
                        raise DomainError(
                            "sentence_split_response_invalid",
                            "Sentence split Provider response is invalid.",
                        ) from error
                    await self._sleeper(self._backoff(attempt))
                    repair_reason = (
                        "Previous response was not valid. Respond with JSON only: "
                        '{"parts": ["<part 1>", "<part 2>", ...]} with 2 to 8 exact '
                        "consecutive substrings of the original sentence."
                    )
                    attempt_key = f"{idempotency_key}:a{attempt}"
                    continue
                try:
                    parts = validate_split_parts(sentence.text, result.parts)
                except DomainError as error:
                    # 有损拆分（无法按序精确命中原句）：输出不确定性，预算内修复重试。
                    if error.code != "sentence_split_lossy":
                        raise
                    if attempt >= self._max_attempts:
                        raise
                    await self._sleeper(self._backoff(attempt))
                    repair_reason = (
                        "Previous split changed characters or lost whitespace. Return exact "
                        "consecutive substrings of the original sentence, preserving every "
                        "space and punctuation mark at the split boundaries."
                    )
                    attempt_key = f"{idempotency_key}:a{attempt}"
                    continue
                return SentenceSplit(
                    sentence.sequence,
                    sentence.text_sha256,
                    parts,
                    result.receipt,
                    result.raw_response_sha256,
                )
        raise AssertionError("sentence split retry loop must return or raise")

    def _backoff(self, attempt: int, retry_after_seconds: float | None = None) -> float:
        base = self._backoff_seconds[min(attempt - 1, len(self._backoff_seconds) - 1)]
        return max(base, retry_after_seconds or 0.0)


def _first_domain_error(grouped: BaseExceptionGroup[BaseException]) -> DomainError | None:
    for error in grouped.exceptions:
        if isinstance(error, DomainError):
            return error
        if isinstance(error, BaseExceptionGroup):
            nested = _first_domain_error(error)
            if nested is not None:
                return nested
    return None
