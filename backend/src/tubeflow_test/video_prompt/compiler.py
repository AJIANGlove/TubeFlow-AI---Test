"""视频提示词编译应用服务：模板渲染、Provider 调用、合同校验与一次修复重试。

失败语义遵循项目边界：编译失败即显式失败（不降级回旧提示词），原因码随
DomainError 透出到 API 层，用户可手动重试。
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_prompt.domain import (
    NON_COMPILABLE_STRATEGIES,
    CompiledVideoPrompt,
    SinglePanelPromptCompilerInput,
    VideoPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
    insert_binding_line,
    insert_cadence_sentence,
    insert_single_panel_binding_line,
    validate_compiled_prompt,
    validate_single_panel_compiled_prompt,
)
from tubeflow_test.video_prompt.provider import (
    PermanentVideoPromptProviderError,
    RetryableVideoPromptProviderError,
    VideoPromptProvider,
    VideoPromptProviderResponseError,
    VideoPromptProviderResult,
)
from tubeflow_test.video_prompt.templates import build_system_prompt, build_user_payload

# §15.14：编译的临时故障（含 LLM 平台偶发虚假 4xx）按预算退避重试；
# 每对象（含修复重试）共享 3 次 Provider 调用预算。
_VIDEO_PROMPT_MAX_ATTEMPTS = 3
_VIDEO_PROMPT_RETRY_BACKOFF_SECONDS = (5.0, 15.0)

# 诊断：每次尝试一行（策略 / 原因码 / 模型原始输出），只用于事后归因。
DiagnosticsSink = Callable[[Mapping[str, Any]], None]
PromptCompilerInput = VideoPromptCompilerInput | SinglePanelPromptCompilerInput


class VideoPromptCompiler:
    def __init__(
        self,
        provider: VideoPromptProvider,
        *,
        model: str,
        strategy: VideoPromptStrategy,
        sleeper: Callable[[float], None] = time.sleep,
        diagnostics: DiagnosticsSink | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("video prompt compiler model must be non-empty")
        self._provider = provider
        self._model = model
        self._strategy = strategy
        self._sleeper = sleeper
        self._diagnostics = diagnostics

    @property
    def strategy(self) -> VideoPromptStrategy:
        return self._strategy

    def close(self) -> None:
        self._provider.close()

    def compile(
        self,
        value: PromptCompilerInput,
        *,
        max_repairs: int = 1,
        repair_reason: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CompiledVideoPrompt:
        if value.strategy in NON_COMPILABLE_STRATEGIES:
            # §54：已编辑提示词只能由实验臂整段提交，不得调用 Provider 生成。
            raise VideoPromptViolation("h3_prompt_strategy_not_compilable")
        system_prompt = build_system_prompt(
            value.strategy, duration_seconds=value.duration_seconds
        )
        result, repaired = self._generate(
            value,
            system_prompt,
            max_repairs=max_repairs,
            initial_reason=repair_reason,
            context=context,
        )
        if isinstance(value, SinglePanelPromptCompilerInput):
            final_text = insert_single_panel_binding_line(result.text, value)
            if value.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_SINGLE_PANEL_CADENCE_V1:
                # §51：变体句与绑定行一样是确定性插入，插入后再全量校验。
                final_text = insert_cadence_sentence(final_text)
            validate_single_panel_compiled_prompt(
                final_text, expected=value, binding_inserted=True
            )
        else:
            final_text = insert_binding_line(result.text, value)
            # 绑定行插入后再做一次全量校验，确保出口文本必然满足合同。
            validate_compiled_prompt(final_text, expected=value)
        return CompiledVideoPrompt(
            final_text,
            value.strategy,
            self._model,
            result.receipt,
            repaired,
            value.fingerprint,
        )

    def _record(
        self,
        value: PromptCompilerInput,
        *,
        attempt_index: int,
        reason: str,
        output_text: str | None,
        context: Mapping[str, Any] | None,
    ) -> None:
        sink = self._diagnostics
        if sink is None:
            return
        with contextlib.suppress(Exception):  # 诊断绝不影响生成主流程
            sink(
                {
                    **(dict(context or {})),
                    "strategy": str(value.strategy),
                    "model": self._model,
                    "payload_sha256": value.fingerprint,
                    "attempt_index": attempt_index,
                    "reason": reason,
                    "output_sha256": (
                        hashlib.sha256(output_text.encode()).hexdigest()
                        if output_text
                        else None
                    ),
                    "output_text": output_text,
                }
            )

    def _generate(
        self,
        value: PromptCompilerInput,
        system_prompt: str,
        *,
        max_repairs: int,
        initial_reason: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[VideoPromptProviderResult, bool]:
        # AUTO 项目 max_repairs=2：输出不确定性校验失败可多次修复重试；
        # 与临时故障退避共享每对象 3 次 Provider 调用预算（attempts 贯穿全程）。
        # initial_reason：QC 梯子等外部失败原因，首轮载荷即携带（templates 规则 15）。
        attempts: list[int] = []
        reason: str | None = initial_reason
        diagnostics: dict[str, str | int | bool] = {}
        for repair_index in range(max_repairs + 1):
            payload = build_user_payload(value, repair_reason=reason)
            try:
                result = self._call(
                    value,
                    system_prompt,
                    payload,
                    repair_index=repair_index,
                    attempts=attempts,
                    allow_transient_retry=max_repairs >= 2,
                )
            except VideoPromptProviderResponseError as exc:
                reason = exc.code
                diagnostics = dict(exc.diagnostics)
                self._record(
                    value,
                    attempt_index=repair_index,
                    reason=reason,
                    output_text=None,
                    context=context,
                )
                continue
            try:
                if isinstance(value, SinglePanelPromptCompilerInput):
                    validate_single_panel_compiled_prompt(
                        result.text, expected=value, binding_inserted=False
                    )
                else:
                    validate_compiled_prompt(result.text, expected=value)
            except VideoPromptViolation as exc:
                reason = str(exc)
                self._record(
                    value,
                    attempt_index=repair_index,
                    reason=reason,
                    output_text=result.text,
                    context=context,
                )
                continue
            return result, repair_index > 0
        raise DomainError(
            "video_prompt_compilation_failed",
            f"Video prompt stayed invalid after {max_repairs} repairs: {reason}"
            f"{_diagnostics_suffix(diagnostics)}",
        )


    def _call(
        self,
        value: PromptCompilerInput,
        system_prompt: str,
        payload: dict[str, object],
        *,
        repair_index: int,
        attempts: list[int],
        allow_transient_retry: bool,
    ) -> VideoPromptProviderResult:
        base_key = f"{value.idempotency_key}:video-prompt"
        if repair_index:
            base_key += f":repair-{repair_index}"
        while True:
            idempotency_key = base_key if not attempts else f"{base_key}:retry-{len(attempts)}"
            try:
                return self._provider.compile(
                    system_prompt=system_prompt,
                    user_payload=payload,
                    model=self._model,
                    idempotency_key=idempotency_key,
                )
            except PermanentVideoPromptProviderError as exc:
                raise DomainError(
                    "video_prompt_compilation_failed", f"Video prompt provider rejected: {exc}"
                ) from exc
            except RetryableVideoPromptProviderError as exc:
                attempts.append(1)
                if len(attempts) >= _VIDEO_PROMPT_MAX_ATTEMPTS:
                    raise DomainError(
                        "video_prompt_compilation_unavailable",
                        f"Video prompt provider stayed unavailable within the retry budget: {exc}",
                    ) from exc
                delay = _VIDEO_PROMPT_RETRY_BACKOFF_SECONDS[
                    min(len(attempts) - 1, len(_VIDEO_PROMPT_RETRY_BACKOFF_SECONDS) - 1)
                ]
                self._sleeper(max(delay, exc.retry_after_seconds or 0.0))
            except httpx.HTTPError as exc:
                if not allow_transient_retry:
                    raise DomainError(
                        "video_prompt_compilation_unavailable",
                        f"Video prompt provider call failed: {type(exc).__name__}",
                    ) from exc
                attempts.append(1)
                if len(attempts) >= _VIDEO_PROMPT_MAX_ATTEMPTS:
                    raise DomainError(
                        "video_prompt_compilation_unavailable",
                        f"Video prompt provider call failed within the retry budget: "
                        f"{type(exc).__name__}",
                    ) from exc
                delay = _VIDEO_PROMPT_RETRY_BACKOFF_SECONDS[
                    min(len(attempts) - 1, len(_VIDEO_PROMPT_RETRY_BACKOFF_SECONDS) - 1)
                ]
                self._sleeper(delay)


def _diagnostics_suffix(diagnostics: dict[str, str | int | bool]) -> str:
    """把解析诊断压成一行后缀（不含正文，最多 6 个键），随稳定码一起透出。"""
    if not diagnostics:
        return ""
    parts = [f"{key}={diagnostics[key]}" for key in sorted(diagnostics)[:6]]
    return " (" + ", ".join(parts) + ")"


def build_video_prompt_compiler(
    settings: Settings, diagnostics: DiagnosticsSink | None = None
) -> VideoPromptCompiler | None:
    """按配置构建编译器；未配置 token 时返回 None，由调用方在触发时显式失败。"""
    from tubeflow_test.adapters.deepseek_video_prompt import DeepSeekVideoPromptProvider

    if not settings.video_prompt_provider_configured:
        return None
    provider = DeepSeekVideoPromptProvider(
        settings.video_prompt_provider_url,
        settings.video_prompt_provider_token.get_secret_value(),
        timeout_seconds=settings.video_prompt_provider_timeout_seconds,
    )
    return VideoPromptCompiler(
        provider,
        model=settings.video_prompt_provider_model,
        strategy=VideoPromptStrategy(settings.video_prompt_strategy),
        diagnostics=diagnostics,
    )
