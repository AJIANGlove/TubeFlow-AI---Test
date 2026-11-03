from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol, TypeVar
from uuid import UUID

from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.composition import (
    CompositionRequest,
    GroupInput,
    NarrationSegmentInput,
    SentenceTiming,
    normalize_subtitle_text,
)
from tubeflow_test.domain.errors import DomainError


class AuthoritativeCompositionInputPort(Protocol):
    """Maps the latest narration timeline and current H3 group versions."""

    def load(self, project_id: UUID, owner_id: UUID) -> CompositionRequest | None: ...


@dataclass(frozen=True, slots=True)
class LatestNarrationSentence:
    id: UUID
    order: int
    text: str
    text_sha256: str
    start_frame: int
    end_frame: int
    sample_rate: int
    audio_asset_id: UUID

    @property
    def start_ms(self) -> int:
        return round(Fraction(self.start_frame * 1000, self.sample_rate))

    @property
    def end_ms(self) -> int:
        return round(Fraction(self.end_frame * 1000, self.sample_rate))


@dataclass(frozen=True, slots=True)
class LatestH3Group:
    id: UUID
    order: int
    video_asset_id: UUID
    sentence_ids: tuple[UUID, ...]
    start_ms: int
    end_ms: int
    width: int
    height: int


OUTPUT_RESOLUTION: dict[AspectRatio, tuple[int, int]] = {
    AspectRatio.LANDSCAPE: (1920, 1080),
    AspectRatio.PORTRAIT: (1080, 1920),
}
COMPOSITION_CRF = 20
COMPOSITION_SHARPEN = 0.4


@dataclass(frozen=True, slots=True)
class CompositionProjectSnapshot:
    sha256: str
    aspect_ratio: AspectRatio
    width: int
    height: int
    group_width: int | None = None
    group_height: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "group_width", self.width if self.group_width is None else self.group_width
        )
        object.__setattr__(
            self, "group_height", self.height if self.group_height is None else self.group_height
        )


class NarrationTimelinePort(Protocol):
    def timeline(self, project_id: UUID, owner_id: UUID) -> tuple[LatestNarrationSentence, ...]: ...


class CurrentH3GroupsPort(Protocol):
    def current_groups(self, project_id: UUID, owner_id: UUID) -> tuple[LatestH3Group, ...]: ...


class CompositionSnapshotPort(Protocol):
    def snapshot(self, project_id: UUID, owner_id: UUID) -> CompositionProjectSnapshot | None: ...


class LatestFeatureCompositionInputs:
    """Maps only latest narration/H3 contracts; it has no legacy-table fallback."""

    def __init__(
        self,
        narration: NarrationTimelinePort,
        h3: CurrentH3GroupsPort,
        snapshots: CompositionSnapshotPort,
    ) -> None:
        self._narration = narration
        self._h3 = h3
        self._snapshots = snapshots

    def load(self, project_id: UUID, owner_id: UUID) -> CompositionRequest | None:
        snapshot = self._snapshots.snapshot(project_id, owner_id)
        sentences = self._narration.timeline(project_id, owner_id)
        groups = self._h3.current_groups(project_id, owner_id)
        if snapshot is None or not sentences or not groups:
            return None
        sentence_by_id = {item.id: item for item in sentences}
        expected_sentence_ids = tuple(item.id for item in sentences)
        actual_sentence_ids: list[UUID] = []
        cursor = 0
        for group_index, group in enumerate(groups, start=1):
            group_sentences = tuple(sentence_by_id.get(item) for item in group.sentence_ids)
            if (
                group.order != group_index
                or not group.sentence_ids
                or any(item is None for item in group_sentences)
                or group.start_ms != cursor
                or group.end_ms <= group.start_ms
                or (group.width, group.height) != (snapshot.group_width, snapshot.group_height)
            ):
                raise DomainError(
                    "authoritative_group_timeline_invalid",
                    "current H3 groups do not match the authoritative timeline",
                )
            authoritative_group = tuple(item for item in group_sentences if item is not None)
            if (
                authoritative_group[0].start_ms != group.start_ms
                or authoritative_group[-1].end_ms != group.end_ms
                or any(
                    sentence.end_ms <= sentence.start_ms
                    or (index > 0 and sentence.start_ms != authoritative_group[index - 1].end_ms)
                    for index, sentence in enumerate(authoritative_group)
                )
            ):
                raise DomainError(
                    "authoritative_group_timeline_invalid",
                    "current H3 groups do not match the authoritative timeline",
                )
            actual_sentence_ids.extend(group.sentence_ids)
            cursor = group.end_ms
        if (
            tuple(actual_sentence_ids) != expected_sentence_ids
            or cursor != sentences[-1].end_ms
        ):
            raise DomainError(
                "composition_authority_invalid",
                "H3 groups must exactly cover the authoritative narration timeline",
            )
        return CompositionRequest(
            str(project_id),
            snapshot.sha256,
            tuple(
                GroupInput(
                    str(item.id),
                    item.order - 1,
                    str(item.video_asset_id),
                    tuple(
                        NarrationSegmentInput(
                            str(sentence_by_id[sentence_id].id),
                            str(sentence_by_id[sentence_id].audio_asset_id),
                            sentence_by_id[sentence_id].end_ms
                            - sentence_by_id[sentence_id].start_ms,
                            sentence_by_id[sentence_id].end_frame
                            - sentence_by_id[sentence_id].start_frame,
                            sentence_by_id[sentence_id].sample_rate,
                        )
                        for sentence_id in item.sentence_ids
                    ),
                    item.end_ms - item.start_ms,
                )
                for item in groups
            ),
            tuple(
                # 权威句文本可能携带拆句边界空白等原始形态；字幕输入先按 SRT 口径
                # 规范化并配套其哈希，保证 SentenceTiming 的来源校验一致。
                SentenceTiming(
                    str(item.id),
                    item.order - 1,
                    (normalized := normalize_subtitle_text(item.text)),
                    hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    item.start_ms,
                    item.end_ms,
                )
                for item in sentences
            ),
            snapshot.aspect_ratio,
            snapshot.width,
            snapshot.height,
            24,
            snapshot.group_width,
            snapshot.group_height,
            COMPOSITION_CRF,
            COMPOSITION_SHARPEN,
        )


SessionT = TypeVar("SessionT", contravariant=True)


class RelationalNarrationReader(Protocol[SessionT]):
    def timeline_in_session(
        self, session: SessionT, project_id: UUID, owner_id: UUID
    ) -> tuple[LatestNarrationSentence, ...]: ...


class RelationalCurrentH3Reader(Protocol[SessionT]):
    def current_archived_groups_in_session(
        self, session: SessionT, project_id: UUID, owner_id: UUID
    ) -> tuple[LatestH3Group, ...]: ...


class RelationalSnapshotReader(Protocol[SessionT]):
    def snapshot_in_session(
        self, session: SessionT, project_id: UUID, owner_id: UUID
    ) -> CompositionProjectSnapshot | None: ...
