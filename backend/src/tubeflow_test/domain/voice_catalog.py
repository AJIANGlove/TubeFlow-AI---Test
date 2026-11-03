from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CatalogFile:
    member_name: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PublicVoiceVersion:
    voice_id: str
    version: int
    display_name: str
    supported_languages: tuple[str, ...]
    reference_zip: CatalogFile
    preview_audio: CatalogFile
    avatar_image: CatalogFile
    metadata_sha256: str = ""
    enabled: bool = True
    raw_language_codes: tuple[str, ...] = ()
    internal_name: str = ""
    gender: str = ""
    age_band: str = ""
    accent: str = ""
    accent_display_name: str = ""
    tags: tuple[str, ...] = ()
    language_display_name: str = ""
    description: str = ""
    created_at: str = ""
    author: str = ""
    source_site: str = ""


@dataclass(frozen=True, slots=True)
class PublicVoiceCatalog:
    version: int
    source_sha256: str
    voices: tuple[PublicVoiceVersion, ...]
