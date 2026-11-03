"""拆句 agent 领域规则：风险句识别、无损校验、拆分计划的确定性应用与持久化 serde。

对应 USER_DECISIONS.md §15.7：只对确定性估算超长的风险句做语义二级拆分，
只拆句、绝不改动原文文字；拆分结果须通过无损拼接校验，校验失败即拒绝。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import (
    ScriptSentence,
    ScriptVersion,
    estimate_narration_duration_ms,
    is_nonvisual_script_separator,
)

SENTENCE_SPLIT_RULE_VERSION = "sentence-split-v1"
# 拆分计划非空时，冻结分句规则版本升级为 "punctuation-v3+sentence-split-v1"。
SPLIT_RULE_SUFFIX = "+sentence-split-v1"
MAX_SPLIT_PARTS = 8


@dataclass(frozen=True, slots=True)
class SentenceSplit:
    """一条原句的拆分血缘：只记录拆分结果与安全回执，不保存 Provider 响应正文。"""

    source_sequence: int
    source_text_sha256: str
    parts: tuple[str, ...]
    provider_receipt: str = ""
    raw_response_sha256: str = ""


@dataclass(frozen=True, slots=True)
class SentenceSplitPlan:
    splits: tuple[SentenceSplit, ...] = ()


def estimate_split_risk_sentences(
    script: ScriptVersion, threshold_seconds: float
) -> tuple[ScriptSentence, ...]:
    """确定性估算可能超长的风险句（> 阈值）；分隔句不参与配音，直接排除。"""
    threshold_ms = threshold_seconds * 1000
    return tuple(
        sentence
        for sentence in script.sentences
        if not is_nonvisual_script_separator(sentence.text)
        and estimate_narration_duration_ms(sentence.text, script.language) > threshold_ms
    )


def validate_split_parts(original: str, parts: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """无损校验并返回按原句重切的各部分。

    规则：2..8 个非空白字符串部分。模型返回的各部分只作有序锚点——每段（去整体
    首尾空白后）必须能在原句中按顺序精确命中；最终返回以各段命中位置切出的原句
    片段，无损由构造保证（边界空白以原句为准，模型在切分点丢/加的空白自动复原）。
    任何一段无法按序命中（改词、丢字、段内空白变化、加词）一律拒绝为 sentence_split_lossy。
    """
    if not 2 <= len(parts) <= MAX_SPLIT_PARTS:
        raise DomainError(
            "sentence_split_contract_invalid", "A sentence split must contain 2 to 8 parts."
        )
    for part in parts:
        if not isinstance(part, str):
            raise DomainError(
                "sentence_split_contract_invalid", "Sentence split parts must be strings."
            )
        if not part.strip():
            raise DomainError(
                "sentence_split_contract_invalid", "Sentence split parts must not be blank."
            )
    return _realign_lossless(original, parts)


def _realign_lossless(original: str, parts: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    starts: list[int] = []
    cursor = 0
    for part in parts:
        needle = part.strip()
        index = original.find(needle, cursor)
        if index < 0:
            raise DomainError(
                "sentence_split_lossy",
                "Split parts must concatenate to the original sentence unchanged.",
            )
        starts.append(index)
        cursor = index + len(needle)
    boundaries = [0, *starts[1:], len(original)]
    return tuple(original[boundaries[i] : boundaries[i + 1]] for i in range(len(parts)))


def apply_sentence_split_plan(script: ScriptVersion, plan: SentenceSplitPlan) -> ScriptVersion:
    """把拆分计划确定性应用到 v3 分句结果上：被拆原句由其各部分原位替代并重新编号。

    无损校验在应用时逐句重放；任何不一致（序不存在、哈希不符、分隔句、拼接有损）
    都拒绝。源文案不变，因此 source_sha256 不变；计划非空时规则版本升级。
    """
    if not plan.splits:
        return script
    by_sequence: dict[int, SentenceSplit] = {}
    for split in plan.splits:
        if split.source_sequence in by_sequence:
            raise DomainError(
                "sentence_split_plan_invalid", "Duplicate sentence split source sequence."
            )
        by_sequence[split.source_sequence] = split
    texts: list[str] = []
    consumed: set[int] = set()
    for sentence in script.sentences:
        split_item = by_sequence.get(sentence.sequence)
        if split_item is None:
            texts.append(sentence.text)
            continue
        consumed.add(sentence.sequence)
        if split_item.source_text_sha256 != sentence.text_sha256:
            raise DomainError(
                "sentence_split_plan_invalid", "Sentence split source hash mismatches the script."
            )
        if is_nonvisual_script_separator(sentence.text):
            raise DomainError(
                "sentence_split_plan_invalid", "Non-visual separator sentences cannot be split."
            )
        texts.extend(validate_split_parts(sentence.text, split_item.parts))
    if consumed != set(by_sequence):
        raise DomainError(
            "sentence_split_plan_invalid", "Sentence split source sequence is not in the script."
        )
    if _without_space("".join(texts)) != _without_space(
        "".join(item.text for item in script.sentences)
    ):
        raise DomainError(
            "sentence_split_lossy", "Sentence split application must preserve the script."
        )
    return ScriptVersion(
        script.language,
        script.rule_version + SPLIT_RULE_SUFFIX,
        script.source_sha256,
        tuple(
            ScriptSentence(index, text, hashlib.sha256(text.encode()).hexdigest())
            for index, text in enumerate(texts, 1)
        ),
    )


def sentence_split_plan_to_json(plan: SentenceSplitPlan) -> dict[str, object]:
    return {
        "version": SENTENCE_SPLIT_RULE_VERSION,
        "items": [
            {
                "source_sequence": split.source_sequence,
                "source_text_sha256": split.source_text_sha256,
                "parts": list(split.parts),
                "provider_receipt": split.provider_receipt,
                "raw_response_sha256": split.raw_response_sha256,
            }
            for split in plan.splits
        ],
    }


def sentence_split_plan_from_json(value: object) -> SentenceSplitPlan | None:
    """从持久化 payload 还原拆分计划；缺失返回 None，结构不合法 fail-closed。"""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != SENTENCE_SPLIT_RULE_VERSION:
        raise DomainError(
            "sentence_split_lineage_invalid", "Persisted sentence split lineage is invalid."
        )
    items = value.get("items")
    if not isinstance(items, list):
        raise DomainError(
            "sentence_split_lineage_invalid", "Persisted sentence split lineage is invalid."
        )
    splits: list[SentenceSplit] = []
    for item in items:
        parts = item.get("parts") if isinstance(item, dict) else None
        source_sequence = item.get("source_sequence") if isinstance(item, dict) else None
        source_hash = item.get("source_text_sha256") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(source_sequence, int)
            or not isinstance(source_hash, str)
            or len(source_hash) != 64
            or not isinstance(parts, list)
            or not all(isinstance(part, str) for part in parts)
        ):
            raise DomainError(
                "sentence_split_lineage_invalid", "Persisted sentence split lineage is invalid."
            )
        splits.append(
            SentenceSplit(
                source_sequence,
                source_hash,
                tuple(parts),
                str(item.get("provider_receipt") or ""),
                str(item.get("raw_response_sha256") or ""),
            )
        )
    return SentenceSplitPlan(tuple(splits))


def _without_space(value: str) -> str:
    return "".join(character for character in value if not character.isspace())
