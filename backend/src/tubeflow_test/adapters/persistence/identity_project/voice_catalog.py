from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4
from zipfile import ZipFile

from tubeflow_test.adapters.public_voice_catalog import import_public_voice_catalog
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.voice_catalog import PublicVoiceCatalog, PublicVoiceVersion
from tubeflow_test.identity_project.domain import VoiceMediaFact, VoiceVersion
from tubeflow_test.identity_project.ports import IdentityProjectRepository, PrivateImageStorage


def _catalog_metadata_json(item: PublicVoiceVersion) -> str:
    return json.dumps(
        {
            "name": item.internal_name,
            "gender": item.gender,
            "age_band": item.age_band,
            "accent": item.accent,
            "accent_zh": item.accent_display_name,
            "tags": item.tags,
            "supported_languages": item.raw_language_codes,
            "language_display_zh": item.language_display_name,
            "description": item.description,
            "created_at": item.created_at,
            "author": item.author,
            "source_site": item.source_site,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _verify_completed_catalog(
    catalog: PublicVoiceCatalog,
    repository: IdentityProjectRepository,
    storage: PrivateImageStorage,
) -> None:
    repository.publish_voice_catalog(
        catalog.source_sha256,
        catalog.version,
        len(catalog.voices),
        datetime.now(UTC),
    )
    published = tuple(
        voice
        for voice in repository.list_voice_versions()
        if voice.catalog_source_sha256 == catalog.source_sha256
    )
    by_identity = {(voice.voice_id, voice.version): voice for voice in published}
    if len(published) != len(catalog.voices) or len(by_identity) != len(catalog.voices):
        raise DomainError(
            "voice_catalog_integrity_failed",
            "The completed public voice catalog is incomplete.",
        )
    verified_object_keys: set[str] = set()
    for item in catalog.voices:
        voice = by_identity.get((item.voice_id, item.version))
        expected_locales = tuple(
            SupportedLocale(value) for value in item.supported_languages
        )
        if (
            voice is None
            or not voice.enabled
            or voice.metadata_sha256 != item.metadata_sha256
            or voice.display_name != item.display_name
            or voice.supported_locales != expected_locales
            or voice.raw_language_codes != item.raw_language_codes
            or voice.reference_zip_sha256 != item.reference_zip.sha256
            or voice.preview_sha256 != item.preview_audio.sha256
            or voice.avatar_sha256 != item.avatar_image.sha256
            or voice.catalog_metadata_json != _catalog_metadata_json(item)
        ):
            raise DomainError(
                "voice_catalog_integrity_failed",
                "The completed public voice catalog does not match the approved manifest.",
            )
        for kind, descriptor in (
            ("reference", item.reference_zip),
            ("preview", item.preview_audio),
            ("avatar", item.avatar_image),
        ):
            media = repository.voice_media(voice.id, kind)
            suffix = PurePosixPath(descriptor.member_name).suffix.lower()
            canonical_key = (
                f"voice-catalog/v{catalog.version}/{item.voice_id}/{kind}{suffix}"
            )
            safe_generation_key = re.fullmatch(
                rf"{re.escape(canonical_key)}\.generation-(?:[2-9]|[1-9][0-9]+)-[0-9a-f]{{32}}",
                media[0] if media is not None else "",
            )
            if (
                media is None
                or (media[0] != canonical_key and safe_generation_key is None)
                or media[0] in verified_object_keys
                or media[1:]
                != (
                descriptor.media_type,
                descriptor.sha256,
                descriptor.size_bytes,
                )
            ):
                raise DomainError(
                    "voice_catalog_integrity_failed",
                    "The completed public voice catalog has invalid or reused media facts.",
                )
            verified_object_keys.add(media[0])
            try:
                storage.get_verified(media[0], descriptor.sha256, descriptor.size_bytes)
            except Exception as error:
                raise DomainError(
                    "voice_catalog_integrity_failed",
                    "The completed public voice catalog has missing or corrupt media.",
                ) from error
    if len(verified_object_keys) != len(catalog.voices) * 3:
        raise DomainError(
            "voice_catalog_integrity_failed",
            "The completed public voice catalog does not have distinct media objects.",
        )


def persist_public_voice_catalog(
    path: Path,
    repository: IdentityProjectRepository,
    storage: PrivateImageStorage,
    *,
    catalog: PublicVoiceCatalog | None = None,
    max_new_voices: int | None = None,
) -> tuple[UUID, ...]:
    """Validate and atomically stage immutable catalog media for the caller's UoW."""
    catalog = catalog or import_public_voice_catalog(path)
    repository.lock_voice_catalog_import(catalog.source_sha256)
    progress = repository.catalog_import_progress(catalog.source_sha256)
    if progress is not None and progress[1] == "completed":
        _verify_completed_catalog(catalog, repository, storage)
        return ()
    imported_count = progress[0] if progress else 0
    repository.save_catalog_import(
        catalog.source_sha256,
        catalog.version,
        len(catalog.voices),
        imported_count,
        "importing",
        datetime.now(UTC),
    )
    created: list[UUID] = []
    processed = 0
    last_ordinal = imported_count
    try:
        with ZipFile(path) as archive:
            for ordinal, item in enumerate(catalog.voices, start=1):
                if ordinal <= imported_count:
                    continue
                repository.lock_voice_version_import(item.voice_id, item.version)
                payloads = {
                    "reference": (item.reference_zip, archive.read(item.reference_zip.member_name)),
                    "preview": (item.preview_audio, archive.read(item.preview_audio.member_name)),
                    "avatar": (item.avatar_image, archive.read(item.avatar_image.member_name)),
                }
                media: dict[str, tuple[UUID, str, str, int]] = {}
                claims = {}
                for kind, (descriptor, payload) in payloads.items():
                    suffix = PurePosixPath(descriptor.member_name).suffix.lower()
                    object_key = f"voice-catalog/v{catalog.version}/{item.voice_id}/{kind}{suffix}"
                    try:
                        stored = storage.put_at_key(
                            object_key, payload, descriptor.media_type, descriptor.sha256
                        )
                    except DomainError as error:
                        if error.code != "staged_object_claim_lost":
                            raise
                        raise DomainError(
                            "voice_version_conflict",
                            "Voice version import is already in progress with different content",
                        ) from error
                    except ValueError as error:
                        raise DomainError(
                            "voice_version_conflict",
                            "Voice version already exists with different content",
                        ) from error
                    storage.get_verified(stored.key, descriptor.sha256, descriptor.size_bytes)
                    media[kind] = (
                        uuid4(),
                        stored.key,
                        descriptor.media_type,
                        descriptor.size_bytes,
                    )
                    claims[kind] = stored.claim
                voice = VoiceVersion(
                    id=uuid4(),
                    metadata_sha256=item.metadata_sha256,
                    supported_locales=tuple(
                        SupportedLocale(value) for value in item.supported_languages
                    ),
                    # A fixed catalog is published as one unit. Individual rows
                    # remain invisible until the final transaction verifies and
                    # enables the complete catalog.
                    enabled=False,
                    reference_zip_sha256=item.reference_zip.sha256,
                    preview_sha256=item.preview_audio.sha256,
                    avatar_sha256=item.avatar_image.sha256,
                    voice_id=item.voice_id,
                    version=item.version,
                    display_name=item.display_name,
                    catalog_source_sha256=catalog.source_sha256,
                    reference_zip=VoiceMediaFact(
                        media["reference"][0],
                        item.reference_zip.sha256,
                        item.reference_zip.media_type,
                        item.reference_zip.size_bytes,
                    ),
                    preview=VoiceMediaFact(
                        media["preview"][0],
                        item.preview_audio.sha256,
                        item.preview_audio.media_type,
                        item.preview_audio.size_bytes,
                    ),
                    avatar=VoiceMediaFact(
                        media["avatar"][0],
                        item.avatar_image.sha256,
                        item.avatar_image.media_type,
                        item.avatar_image.size_bytes,
                    ),
                    raw_language_codes=item.raw_language_codes,
                    catalog_metadata_json=_catalog_metadata_json(item),
                )
                if repository.add_voice_version(voice, media, claims):
                    created.append(voice.id)
                repository.save_catalog_import(
                    catalog.source_sha256,
                    catalog.version,
                    len(catalog.voices),
                    ordinal,
                    "importing",
                    datetime.now(UTC),
                )
                last_ordinal = ordinal
                processed += 1
                if max_new_voices is not None and processed >= max_new_voices:
                    break
        if last_ordinal == len(catalog.voices):
            repository.publish_voice_catalog(
                catalog.source_sha256,
                catalog.version,
                len(catalog.voices),
                datetime.now(UTC),
            )
        return tuple(created)
    except BaseException:
        # Staged rows and bytes remain durable for the canonical cleaner after
        # the caller's business transaction rolls back. Deleting here can
        # deadlock on that same transaction's row locks or remove a winner.
        raise
