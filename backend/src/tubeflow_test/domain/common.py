from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AspectRatio(StrEnum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"


class SupportedLocale(StrEnum):
    EN_US = "en-US"
    JA_JP = "ja-JP"
    KO_KR = "ko-KR"
    ES_ES = "es-ES"
    FR_FR = "fr-FR"
    PT_BR = "pt-BR"


@dataclass(frozen=True, slots=True)
class MediaAssetRef:
    asset_id: UUID
    sha256: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase hexadecimal digest")
        if not self.media_type or "/" not in self.media_type:
            raise ValueError("media_type must be a valid MIME type")
        if self.size_bytes <= 0:
            raise ValueError("size_bytes must be positive")
