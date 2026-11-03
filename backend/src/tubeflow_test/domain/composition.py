from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest

from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.errors import DomainError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_subtitle_text(text: str) -> str:
    """SRT 字幕文本规范化：统一换行、折叠连续空行、去首尾空白。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]*\n+", "\n", normalized).strip()


@dataclass(frozen=True, slots=True)
class SentenceTiming:
    id: str
    order: int
    text: str
    text_sha256: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        normalized = normalize_subtitle_text(self.text)
        object.__setattr__(self, "text", normalized)
        if (
            not self.id
            or self.order < 0
            or not self.text.strip()
            or not _SHA256.fullmatch(self.text_sha256)
        ):
            raise DomainError("invalid_subtitle", "subtitle identity, text and hash must be valid")
        actual_hash = sha256(self.text.encode("utf-8")).hexdigest()
        if not compare_digest(self.text_sha256, actual_hash):
            raise DomainError("subtitle_text_hash_mismatch", "subtitle text provenance is invalid")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise DomainError("invalid_subtitle_timing", "subtitle timing must be positive")


@dataclass(frozen=True, slots=True)
class NarrationSegmentInput:
    sentence_id: str
    audio_asset_id: str
    duration_ms: int
    sample_count: int
    sample_rate_hz: int

    def __post_init__(self) -> None:
        if (
            not self.sentence_id
            or not self.audio_asset_id
            or self.duration_ms <= 0
            or self.sample_count <= 0
            or self.sample_rate_hz <= 0
        ):
            raise DomainError("invalid_narration_segment", "narration segment is incomplete")

    @property
    def measured_sample_count(self) -> int:
        return self.sample_count


@dataclass(frozen=True, slots=True)
class GroupInput:
    id: str
    order: int
    video_asset_id: str
    narration_segments: tuple[NarrationSegmentInput, ...]
    target_duration_ms: int

    def __post_init__(self) -> None:
        if self.order < 0 or not self.id or not self.video_asset_id or not self.narration_segments:
            raise DomainError("invalid_group", "group input is incomplete")
        if self.target_duration_ms <= 0:
            raise DomainError("invalid_group_duration", "target duration must be positive")
        rates = {item.sample_rate_hz for item in self.narration_segments}
        measured_ms = _round_half_up(
            sum(item.measured_sample_count for item in self.narration_segments) * 1000,
            self.narration_segments[0].sample_rate_hz,
        )
        if len(rates) != 1 or abs(measured_ms - self.target_duration_ms) > 1:
            raise DomainError(
                "group_narration_duration_mismatch",
                "group narration segments must cover the group timeline",
            )


@dataclass(frozen=True, slots=True)
class CompositionRequest:
    project_id: str
    snapshot_sha256: str
    groups: tuple[GroupInput, ...]
    sentences: tuple[SentenceTiming, ...]
    aspect_ratio: AspectRatio
    width: int
    height: int
    fps: int = 24
    group_width: int | None = None
    group_height: int | None = None
    crf: int = 20
    sharpen: float = 0.0

    def __post_init__(self) -> None:
        group_width = self.width if self.group_width is None else self.group_width
        group_height = self.height if self.group_height is None else self.group_height
        object.__setattr__(self, "group_width", group_width)
        object.__setattr__(self, "group_height", group_height)
        if not self.project_id or not _SHA256.fullmatch(self.snapshot_sha256):
            raise DomainError("invalid_composition", "project snapshot is required")
        if (
            not self.groups
            or self.fps != 24
            or self.width <= 0
            or self.height <= 0
            or group_width <= 0
            or group_height <= 0
        ):
            raise DomainError(
                "invalid_composition", "composition requires groups and native 24fps output"
            )
        if not 0 <= self.crf <= 51 or not 0.0 <= self.sharpen <= 2.0:
            raise DomainError("invalid_composition", "encoding parameters are out of range")
        if (self.height > self.width) != (self.aspect_ratio is AspectRatio.PORTRAIT):
            raise DomainError(
                "invalid_composition", "native resolution does not match aspect ratio"
            )
        _require_contiguous_orders(tuple(group.order for group in self.groups), "group")
        _require_contiguous_orders(tuple(item.order for item in self.sentences), "subtitle")
        if not self.sentences:
            raise DomainError("empty_subtitles", "a scripted composition requires subtitles")
        previous_end = 0
        for sentence in self.sentences:
            if sentence.start_ms != previous_end:
                raise DomainError(
                    "non_contiguous_subtitles", "subtitle timeline must be contiguous"
                )
            previous_end = sentence.end_ms
        if previous_end != self.target_duration_ms:
            raise DomainError(
                "subtitle_duration_mismatch", "subtitle timeline must cover narration"
            )

    @property
    def target_duration_ms(self) -> int:
        return sum(group.target_duration_ms for group in self.groups)

    @property
    def total_frames(self) -> int:
        samples, rate = self._sample_timeline()
        return _round_half_up(samples * self.fps, rate)

    def group_frame_counts(self) -> tuple[int, ...]:
        result: list[int] = []
        cumulative_samples = 0
        prior_boundary = 0
        _, sample_rate = self._sample_timeline()
        for group in self.groups:
            cumulative_samples += sum(
                item.measured_sample_count for item in group.narration_segments
            )
            boundary = _round_half_up(cumulative_samples * self.fps, sample_rate)
            result.append(boundary - prior_boundary)
            prior_boundary = boundary
        if self.total_frames < len(self.groups) or any(frames < 1 for frames in result):
            raise DomainError(
                "insufficient_composition_frames",
                "positive-duration groups require at least one frame each",
            )
        return tuple(result)

    def _sample_timeline(self) -> tuple[int, int]:
        rates = {
            segment.sample_rate_hz
            for group in self.groups
            for segment in group.narration_segments
        }
        if len(rates) != 1:
            raise DomainError(
                "composition_sample_rate_mismatch",
                "composition narration must use one measured sample rate",
            )
        rate = next(iter(rates))
        samples = sum(
            segment.measured_sample_count
            for group in self.groups
            for segment in group.narration_segments
        )
        return samples, rate

    def output_sample_count(self, output_rate_hz: int = 48_000) -> int:
        samples, rate = self._sample_timeline()
        numerator = samples * output_rate_hz
        if numerator % rate:
            raise DomainError(
                "composition_sample_rate_mismatch",
                "measured narration cannot be represented at the output sample rate",
            )
        return numerator // rate


def _round_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def _require_contiguous_orders(orders: tuple[int, ...], label: str) -> None:
    if orders != tuple(range(len(orders))):
        raise DomainError(f"invalid_{label}_order", f"{label} order must be contiguous")


def render_srt(sentences: tuple[SentenceTiming, ...], *, total_duration_ms: int) -> str:
    if not sentences:
        raise DomainError("empty_subtitles", "a scripted composition requires subtitles")
    _require_contiguous_orders(tuple(item.order for item in sentences), "subtitle")
    previous_end = 0
    blocks: list[str] = []
    for index, sentence in enumerate(sentences, start=1):
        if sentence.start_ms != previous_end:
            raise DomainError("non_contiguous_subtitles", "subtitle timeline must be contiguous")
        safe_text = sentence.text
        blocks.append(
            f"{index}\n{_srt_time(sentence.start_ms)} --> {_srt_time(sentence.end_ms)}\n{safe_text}"
        )
        previous_end = sentence.end_ms
    if previous_end != total_duration_ms:
        raise DomainError("subtitle_duration_mismatch", "subtitles must end with narration")
    return "\n\n".join(blocks) + "\n"


def subtitle_bindings(sentences: tuple[SentenceTiming, ...]) -> tuple[dict[str, str], ...]:
    return tuple({"sentence_id": item.id, "text_sha256": item.text_sha256} for item in sentences)


def _srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"
