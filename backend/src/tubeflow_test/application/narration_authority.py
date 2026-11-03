from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class AuthoritativeNarrationSentence:
    sentence_id: UUID
    ordinal: int
    text: str
    text_sha256: str
    raw_asset_id: UUID
    normalized_asset_id: UUID
    raw_sha256: str
    normalized_sha256: str
    qc_passed: bool
    sample_count: int
    sample_rate: int
    start_ms: int
    end_ms: int
    start_frame: int = 0
    end_frame: int = 0
    raw_size_bytes: int = 0
    normalized_size_bytes: int = 0


@dataclass(frozen=True)
class AuthoritativeNarrationGroup:
    group_id: UUID
    owner_id: UUID
    project_id: UUID
    snapshot_sha256: str
    snapshot_revision: int
    start_ms: int
    end_ms: int
    video_blocked: bool
    sentences: tuple[AuthoritativeNarrationSentence, ...]


class NarrationAuthorityPort(Protocol):
    """Read-only boundary owned by narration; storyboard never maps its tables."""

    def current_group(
        self, owner_id: UUID, group_id: UUID
    ) -> AuthoritativeNarrationGroup | None: ...
