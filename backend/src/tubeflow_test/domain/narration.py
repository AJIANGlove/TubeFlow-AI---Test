from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from uuid import NAMESPACE_URL, UUID, uuid5

from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError

MAX_GROUP_DURATION_MS = 15_000
SEGMENTATION_RULE_VERSION = "punctuation-v3"
LANGUAGE_GATE_VERSION = "script-profile-v3"
Language = SupportedLocale

# §15.2（仅 AUTO 项目）：每句配音提交尝试（含自动重提）最多 3 次，退避 5s/15s/30s，
# 429 尊重 Retry-After；无整轮总上限。MANUAL 项目不适用。
NARRATION_AUTO_MAX_SUBMISSIONS = 3
NARRATION_AUTO_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 30.0)

# 估算仅用于冻结前预检提示，绝不是时间事实；生产计算只读实测时长。
ESTIMATED_RISK_THRESHOLD_MS = 13_000
_MAX_LATIN_WORDS = 35
_MAX_LATIN_CHARS = 180
_MAX_JA_CHARS = 90
_MAX_KO_CHARS = 75

_MARKDOWN_SEPARATOR = re.compile(r"[-*_~=—–…`]+")
_CLAUSE_BOUNDARIES = ("—–", ";；", ":：", "…", ",，")
_CLOSING_QUOTES = "\"'”’)]}»」』"


def is_nonvisual_script_separator(text: str) -> bool:
    compact = "".join(text.split())
    if not compact:
        return False
    # 2026-10-31：只含格式/控制字符（Cf/Cc，如 U+200B 零宽空格）的文本没有任何
    # 可发音内容，必须等同分隔符，否则会被当成真实配音句提交给 TTS 而永久失败。
    if all(unicodedata.category(character).startswith("C") for character in compact):
        return True
    return bool(_MARKDOWN_SEPARATOR.fullmatch(compact)) or all(
        unicodedata.category(character).startswith("P") for character in compact
    )


def estimate_narration_duration_ms(text: str, language: SupportedLocale) -> int:
    if language is SupportedLocale.JA_JP:
        seconds = len(text) / 7.0
    elif language is SupportedLocale.KO_KR:
        seconds = len(text) / 5.5
    else:
        seconds = max(len(text.split()) / 2.5, len(text) / 15.0)
    return math.ceil(seconds * 1000)


def authoritative_sentence_id(
    project_id: UUID, source_sha256: str, sequence: int, *, snapshot_revision: int = 1
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        (
            f"tubeflow:narration:{project_id}:{source_sha256}:"
            f"{snapshot_revision}:sentence:{sequence}"
        ),
    )


def authoritative_group_id(
    project_id: UUID, source_sha256: str, sequence: int, *, snapshot_revision: int = 1
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        (
            f"tubeflow:narration:{project_id}:{source_sha256}:"
            f"{snapshot_revision}:group:{sequence}"
        ),
    )


class NarrationAttemptState(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    SUBMISSION_STARTED = "SUBMISSION_STARTED"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    ARCHIVE_FAILED = "ARCHIVE_FAILED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class ScriptSentence:
    sequence: int
    text: str
    text_sha256: str


@dataclass(frozen=True, slots=True)
class ScriptVersion:
    language: SupportedLocale
    rule_version: str
    source_sha256: str
    sentences: tuple[ScriptSentence, ...]


@dataclass(frozen=True, slots=True)
class AudioAsset:
    asset_id: str
    sha256: str
    media_type: str
    size_bytes: int = 1


@dataclass(frozen=True, slots=True)
class AudioQcResult:
    passed: bool
    measured_duration_ms: int | None
    codec: str | None
    reason: str | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None
    sample_width_bytes: int | None = None
    frame_count: int | None = None
    rms: float | None = None
    peak: int | None = None
    active_sample_ratio: float | None = None


@dataclass(frozen=True, slots=True)
class NarrationAttempt:
    project_id: UUID
    owner_id: UUID
    sentence_sequence: int
    idempotency_key: str
    request_sha256: str
    state: NarrationAttemptState
    provider_call_id: str
    remote_id: str | None = None
    safe_error_code: str | None = None
    claim_token: str | None = None
    lease_until: str | None = None
    parent_attempt_id: str | None = None
    original_complete_manifest: tuple[str, ...] = ()
    original_manifest_sha256: str | None = None
    original_zip_asset_id: str | None = None
    retry_subset_manifest: tuple[str, ...] = ()
    retry_subset_manifest_sha256: str | None = None
    # AUTO 自动重投的持久化退避时间戳（ISO）；MANUAL 项目恒为 None。
    retry_not_before: str | None = None
    # §15.9 预算轮次：恢复后新一轮授权内重新计数；历史尝试保留原轮次供审计。
    budget_round: int = 1


@dataclass(frozen=True, slots=True)
class NarratedSentence:
    project_id: UUID
    owner_id: UUID
    # §23.1：CosyVoice 公共音色 id；EL 项目没有公共音色，恒为 None（配音源见项目快照）。
    voice_version_id: UUID | None
    locale: SupportedLocale
    provider_call_id: str
    reference_package_sha256: str
    sentence: ScriptSentence
    idempotency_key: str
    raw_asset: AudioAsset
    normalized_asset: AudioAsset
    raw_qc: AudioQcResult
    normalized_qc: AudioQcResult
    normalization_manifest: dict[str, object]
    # §23.7：Provider 响应头 request-id 的对账凭证（EL 逐句回填；CosyVoice 恒为 None）。
    provider_request_id: str | None = None

    @property
    def qc(self) -> AudioQcResult:
        return self.normalized_qc


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    sentence_sequence: int
    start_ms: int
    end_ms: int
    audio_asset_id: str
    start_frame: int
    end_frame: int
    sample_rate_hz: int


@dataclass(frozen=True, slots=True)
class NarrationGroup:
    sequence: int
    sentence_sequences: tuple[int, ...]
    start_ms: int
    end_ms: int
    target_duration_ms: int
    video_blocked: bool
    block_reason: str | None = None


_LATIN_HINTS = {
    SupportedLocale.EN_US: {
        "the",
        "and",
        "is",
        "are",
        "this",
        "with",
        "hello",
        "world",
        "cats",
        "sleep",
        "birds",
        "fly",
        "people",
        "make",
        "video",
        "today",
        "another",
        "sentence",
    },
    SupportedLocale.ES_ES: {
        "el",
        "es",
        "la",
        "los",
        "las",
        "que",
        "con",
        "una",
        "hola",
        "mundo",
        "gatos",
        "duermen",
        "pájaros",
        "vuelan",
    },
    SupportedLocale.FR_FR: {
        "le",
        "ceci",
        "est",
        "la",
        "les",
        "des",
        "avec",
        "une",
        "bonjour",
        "monde",
        "chats",
        "dorment",
        "oiseaux",
        "volent",
    },
    SupportedLocale.PT_BR: {
        "o",
        "a",
        "os",
        "as",
        "que",
        "com",
        "uma",
        "nao",
        "olá",
        "é",
        "mundo",
        "gatos",
        "dormem",
        "pássaros",
        "voam",
    },
}


def segment_pasted_script(text: str, language: SupportedLocale) -> ScriptVersion:
    normalized = unicodedata.normalize("NFC", text).strip()
    if not normalized:
        raise DomainError("script_empty", "Pasted script must not be empty.")
    validate_script_language(normalized, language)
    terminators = (
        "。！？.!?" if language in {SupportedLocale.JA_JP, SupportedLocale.KO_KR} else ".!?"
    )
    pieces: list[str] = []
    for line in normalized.splitlines():
        line = line.strip()
        if not line:
            continue
        matches = re.findall(
            rf"[^{re.escape(terminators)}]+[{re.escape(terminators)}]+"
            rf"[{re.escape(_CLOSING_QUOTES)}]*|[^{re.escape(terminators)}]+$",
            line,
        )
        for part in matches:
            part = part.strip()
            if part:
                pieces.extend(_split_overlong(part, language))
    if not pieces or all(is_nonvisual_script_separator(piece) for piece in pieces):
        raise DomainError("script_empty", "Pasted script contains no sentences.")
    if _without_space("".join(pieces)) != _without_space(normalized):
        raise DomainError("segmentation_loss", "Sentence segmentation did not preserve the script.")
    return ScriptVersion(
        language,
        SEGMENTATION_RULE_VERSION,
        frozen_script_sha256(text),
        tuple(
            ScriptSentence(i, value, _sha256(value.encode())) for i, value in enumerate(pieces, 1)
        ),
    )


def _within_budget(piece: str, language: SupportedLocale) -> bool:
    if language is SupportedLocale.JA_JP:
        return len(piece) <= _MAX_JA_CHARS
    if language is SupportedLocale.KO_KR:
        return len(piece) <= _MAX_KO_CHARS
    return len(piece.split()) <= _MAX_LATIN_WORDS and len(piece) <= _MAX_LATIN_CHARS


def _split_overlong(piece: str, language: SupportedLocale) -> list[str]:
    if _within_budget(piece, language) or is_nonvisual_script_separator(piece):
        return [piece]
    midpoint = len(piece) / 2
    for boundary in _CLAUSE_BOUNDARIES:
        candidates = [
            index
            for index, character in enumerate(piece)
            if character in boundary
            and piece[: index + 1].strip()
            and piece[index + 1 :].strip()
        ]
        if not candidates:
            continue
        cut = min(candidates, key=lambda index: (abs(index - midpoint), index))
        left, right = piece[: cut + 1].strip(), piece[cut + 1 :].strip()
        return _split_overlong(left, language) + _split_overlong(right, language)
    return [piece]


def validate_script_language(text: str, language: SupportedLocale) -> None:
    kana = bool(re.search(r"[\u3040-\u30ff]", text))
    hangul = bool(re.search(r"[\uac00-\ud7af]", text))
    han = bool(re.search(r"[\u4e00-\u9fff]", text))
    words = set(re.findall(r"[^\W\d_]+", text.casefold()))
    if language is SupportedLocale.JA_JP:
        invalid = (
            hangul or not (kana or han) or len(words & _LATIN_HINTS[SupportedLocale.EN_US]) >= 2
        )
    elif language is SupportedLocale.KO_KR:
        invalid = kana or not hangul or bool(words & _LATIN_HINTS[SupportedLocale.EN_US])
    else:
        scores = {locale: len(words & hints) for locale, hints in _LATIN_HINTS.items()}
        strongest_other = max(
            (score for locale, score in scores.items() if locale is not language), default=0
        )
        invalid = (
            kana
            or hangul
            or han
            or (strongest_other >= 2 and scores[language] == 0)
            or _obvious_latin_mix(words)
        )
    if invalid:
        raise DomainError(
            "script_language_mismatch", "Script is unsupported or mixed for the selected language."
        )


def narration_auto_retry_delay_seconds(
    failed_submissions: int, retry_after_seconds: float | None = None
) -> float:
    """AUTO 项目第 n 次提交失败后的退避秒数；429 时尊重 Retry-After（取较大者）。"""
    index = min(
        max(failed_submissions - 1, 0), len(NARRATION_AUTO_RETRY_BACKOFF_SECONDS) - 1
    )
    return max(NARRATION_AUTO_RETRY_BACKOFF_SECONDS[index], retry_after_seconds or 0.0)


def narration_auto_retry_exhausted(submission_count: int) -> bool:
    """该句已用完全部提交预算（含自动重提）。"""
    return submission_count >= NARRATION_AUTO_MAX_SUBMISSIONS


def narration_auto_retryable(state: NarrationAttemptState) -> bool:
    """AUTO 项目允许自动重投的失败态；PROVIDER_REJECTED 等既定失败不在其列。"""
    return state in {
        NarrationAttemptState.SAFE_TO_RETRY,
        NarrationAttemptState.REMOTE_STATE_UNKNOWN,
        NarrationAttemptState.OUTPUT_INVALID,
    }


# §23.6：Provider 账号级阻断——号池无可用 key（额度耗尽）或 key 确认失效。
# 这些是账号/Provider 级事实，不消耗对象提交预算，也不得自动重投；
# 只能由用户充值/换 key 后在页面点「恢复」（构成新一轮授权，§15.9）。
NARRATION_PROVIDER_BLOCKING_CODES: dict[str, str] = {
    "provider_quota_exhausted": "narration_provider_quota_exhausted",
    "provider_auth_failed": "narration_provider_auth_failed",
}


def narration_provider_blocking_code(safe_error_code: str | None) -> str | None:
    """失败句若属 Provider 账号级阻断，返回项目级阻断原因码；否则 None。"""
    return NARRATION_PROVIDER_BLOCKING_CODES.get((safe_error_code or "").strip())


def narration_idempotency_key(
    project_id: str, sentence: ScriptSentence, voice_version_id: str
) -> str:
    """幂等键 = 项目 + 句序 + 句文本哈希 + 音色身份。

    音色身份取配音源冻结的音色标识（§23.1）：CosyVoice 为 voice_version_id、
    EL 为 cloned_voice_id；同一项目内配音源唯一，故键仍然稳定可复算。
    """
    return "tts_" + _sha256(
        f"{project_id}:{sentence.sequence}:{sentence.text_sha256}:{voice_version_id}".encode()
    )


def build_measured_timeline(
    script: ScriptVersion, narrated: tuple[NarratedSentence, ...]
) -> tuple[TimelineEntry, ...]:
    by_sequence = {item.sentence.sequence: item for item in narrated}
    # 非视觉分隔句保留在冻结文案中供前置导演切段，但不参与配音与时间轴。
    expected = tuple(
        sentence
        for sentence in script.sentences
        if not is_nonvisual_script_separator(sentence.text)
    )
    if len(by_sequence) != len(narrated) or len(narrated) != len(expected):
        raise DomainError(
            "narration_incomplete", "Every sentence must have exactly one real narration."
        )
    cursor = Fraction(0)
    entries: list[TimelineEntry] = []
    for sentence in expected:
        item = by_sequence.get(sentence.sequence)
        if item is None or item.sentence.text_sha256 != sentence.text_sha256:
            raise DomainError(
                "narration_hash_mismatch",
                "Narration text hash does not match the immutable script.",
            )
        qc = item.normalized_qc
        duration = qc.measured_duration_ms
        if (
            not item.raw_qc.passed
            or not item.normalized_qc.passed
            or duration is None
            or duration <= 0
            or qc.frame_count is None
            or qc.frame_count <= 0
            or qc.sample_rate_hz is None
            or qc.sample_rate_hz <= 0
        ):
            raise DomainError(
                "narration_qc_incomplete", "Archived raw and normalized QC are required."
            )
        if (
            not item.raw_asset.sha256
            or not item.normalized_asset.sha256
            or item.raw_asset.asset_id == item.normalized_asset.asset_id
        ):
            raise DomainError(
                "narration_asset_incomplete", "Distinct raw and normalized assets are required."
            )
        end = cursor + Fraction(qc.frame_count, qc.sample_rate_hz)
        start_ms = round(cursor * 1000)
        end_ms = round(end * 1000)
        entries.append(
            TimelineEntry(
                sentence.sequence,
                start_ms,
                end_ms,
                item.normalized_asset.asset_id,
                round(cursor * qc.sample_rate_hz),
                round(end * qc.sample_rate_hz),
                qc.sample_rate_hz,
            )
        )
        cursor = end
    return tuple(entries)


def group_timeline(timeline: tuple[TimelineEntry, ...]) -> tuple[NarrationGroup, ...]:
    groups: list[NarrationGroup] = []
    pending: list[TimelineEntry] = []

    def flush() -> None:
        if pending:
            start, end = pending[0].start_ms, pending[-1].end_ms
            groups.append(
                NarrationGroup(
                    len(groups) + 1,
                    tuple(x.sentence_sequence for x in pending),
                    start,
                    end,
                    end - start,
                    False,
                )
            )
            pending.clear()

    for entry in timeline:
        duration = Fraction(entry.end_frame - entry.start_frame, entry.sample_rate_hz)
        if duration > 15:
            flush()
            groups.append(
                NarrationGroup(
                    len(groups) + 1,
                    (entry.sentence_sequence,),
                    entry.start_ms,
                    entry.end_ms,
                    entry.end_ms - entry.start_ms,
                    True,
                    "single_sentence_exceeds_15000ms",
                )
            )
        elif pending and _span_seconds(pending[0], entry) > 15:
            flush()
            pending.append(entry)
        else:
            pending.append(entry)
    flush()
    return tuple(groups)


def assert_video_generation_allowed(groups: tuple[NarrationGroup, ...]) -> None:
    if not groups:
        raise DomainError("narration_not_ready", "Measured narration groups are not ready.")
    # 单句超 15 秒的分组在装组时已标记 video_blocked，按已确认规则跳过该组，
    # 不再阻止项目内其余正常分组的视频生成；此处仅兜底未标记的异常分组。
    if any(
        not group.video_blocked and group.target_duration_ms > MAX_GROUP_DURATION_MS
        for group in groups
    ):
        raise DomainError("video_blocked", "A narrated sentence exceeds 15000ms.")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def frozen_script_sha256(text: str) -> str:
    """冻结文案的权威快照哈希基线（NFC 归一化 + 去首尾空白）。

    与 segment_pasted_script 的 source_sha256 同基线；权威句子/分组表、
    各阶段就绪门禁统一以此比对，严禁旁路使用原始文本哈希。
    """
    return _sha256(unicodedata.normalize("NFC", text).strip().encode())


def _without_space(value: str) -> str:
    return "".join(character for character in value if not character.isspace())



def _span_seconds(first: TimelineEntry, last: TimelineEntry) -> Fraction:
    return Fraction(last.end_frame, last.sample_rate_hz) - Fraction(
        first.start_frame, first.sample_rate_hz
    )


def _obvious_latin_mix(words: set[str]) -> bool:
    signatures = (
        {"hello", "world"},
        {"hola", "mundo"},
        {"bonjour", "monde"},
        {"olá", "mundo"},
    )
    return sum(signature <= words for signature in signatures) > 1
