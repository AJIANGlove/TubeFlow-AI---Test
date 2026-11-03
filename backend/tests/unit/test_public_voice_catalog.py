from __future__ import annotations

import hashlib
import io
import json
import struct
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from PIL import Image

from tubeflow_test.adapters.persistence.identity_project.voice_catalog import (
    persist_public_voice_catalog,
)
from tubeflow_test.adapters.public_voice_catalog import import_public_voice_catalog
from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.adapters import MemoryRepository
from tubeflow_test.identity_project.ports import StagedObjectClaim
from tubeflow_test.identity_project.runtime import IdentityProjectRuntime


def _wav(duration_ms: int) -> bytes:
    output = io.BytesIO()
    rate = 8000
    samples = duration_ms * rate // 1000
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<h", 1000) * samples)
    return output.getvalue()


def _reference_zip() -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        entry = ZipInfo("reference.wav", date_time=(2026, 1, 1, 0, 0, 0))
        entry.compress_type = ZIP_DEFLATED
        archive.writestr(entry, _wav(100))
    return output.getvalue()


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (80, 120, 160)).save(output, format="PNG")
    return output.getvalue()


def _catalog(
    path: Path,
    *,
    extra: tuple[str | ZipInfo, bytes] | None = None,
    display_name: str = "Voice One",
    languages: tuple[str, ...] = ("en", "ja"),
    preview: bytes | None = None,
    avatar: bytes | None = None,
) -> None:
    manifest = {
        "version": 1,
        "count": 1,
        "voices": [
            {
                "id": "voice-1",
                "name": "voice_one",
                "version": 1,
                "display_name_zh": display_name,
                "gender": "female",
                "age_band": "young",
                "accent": "us_en",
                "accent_zh": "美式英语",
                "tags": ["叙事"],
                "supported_languages": list(languages),
                "language_full_zh": "英语、日语",
                "description": "真实合同测试音色",
                "avatar_rel": "avatars/voice-1.png",
                "preview_rel": "previews/voice-1.wav",
                "ref_zip_rel": "refs/voice-1.zip",
                "created_at": "2026-05-23T06:52:18Z",
                "author": "test-author",
                "_source": {"site": "example.invalid/voice/1"},
            }
        ],
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("public_voice_library/index.json", json.dumps(manifest))
        archive.writestr("public_voice_library/refs/voice-1.zip", _reference_zip())
        archive.writestr(
            "public_voice_library/previews/voice-1.wav",
            _wav(100) if preview is None else preview,
        )
        archive.writestr(
            "public_voice_library/avatars/voice-1.png",
            _png() if avatar is None else avatar,
        )
        if extra:
            archive.writestr(*extra)


def test_safe_catalog_import_records_content_hashes(tmp_path: Path) -> None:
    source = tmp_path / "catalog.zip"
    _catalog(source)
    catalog = import_public_voice_catalog(source)
    assert catalog.version == 1
    assert len(catalog.source_sha256) == 64
    assert len(catalog.voices[0].reference_zip.sha256) == 64
    assert catalog.voices[0].raw_language_codes == ("en", "ja")
    assert catalog.voices[0].supported_languages == ("en-US", "ja-JP")


def test_fixed_catalog_startup_rejects_unapproved_or_missing_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "catalog.zip"
    _catalog(source)
    runtime = SimpleNamespace(settings=Settings(public_voice_catalog_path=source))

    with pytest.raises(DomainError, match="approved fixed catalog"):
        IdentityProjectRuntime.initialize_fixed_voice_catalog(runtime)  # type: ignore[arg-type]

    runtime.settings = Settings(public_voice_catalog_path=None)
    with pytest.raises(DomainError, match="must be configured"):
        IdentityProjectRuntime.initialize_fixed_voice_catalog(runtime)  # type: ignore[arg-type]


def test_catalog_preserves_legal_raw_languages_and_projects_only_business_locales(
    tmp_path: Path,
) -> None:
    source = tmp_path / "catalog.zip"
    languages = ("en", "pt", "pt-BR", "es-419", "zh-Hans", "de")
    _catalog(source, languages=languages)

    voice = import_public_voice_catalog(source).voices[0]

    assert voice.raw_language_codes == languages
    assert voice.supported_languages == ("en-US", "pt-BR")


def test_catalog_rejects_invalid_language_codes_and_undecodable_media(tmp_path: Path) -> None:
    invalid_language = tmp_path / "invalid-language.zip"
    _catalog(invalid_language, languages=("en", "../pt"))
    with pytest.raises(DomainError, match="languages"):
        import_public_voice_catalog(invalid_language)

    invalid_preview = tmp_path / "invalid-preview.zip"
    _catalog(invalid_preview, preview=b"not-a-wave")
    with pytest.raises(DomainError, match="WAV audio"):
        import_public_voice_catalog(invalid_preview)

    invalid_avatar = tmp_path / "invalid-avatar.zip"
    _catalog(invalid_avatar, avatar=b"not-an-image")
    with pytest.raises(DomainError, match="avatar"):
        import_public_voice_catalog(invalid_avatar)


def test_catalog_bridge_persists_media_facts_without_exposing_keys(tmp_path: Path) -> None:
    source = tmp_path / "catalog.zip"
    _catalog(source)
    repo = MemoryRepository()

    class Storage:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def put(self, body: bytes, media_type: str) -> object:
            del media_type
            key = f"key-{len(self.values)}"
            self.values[key] = body
            return type("Stored", (), {"key": key, "created": True})()

        def put_at_key(
            self, key: str, body: bytes, media_type: str, expected_sha256: str
        ) -> object:
            del media_type
            assert hashlib.sha256(body).hexdigest() == expected_sha256
            existing = self.values.get(key)
            if existing is not None:
                assert existing == body
                return type(
                    "Stored",
                    (),
                    {
                        "key": key,
                        "created": False,
                        "claim": StagedObjectClaim(key, 1, uuid4()),
                    },
                )()
            self.values[key] = body
            return type(
                "Stored",
                (),
                {
                    "key": key,
                    "created": True,
                    "claim": StagedObjectClaim(key, 1, uuid4()),
                },
            )()

        def get(self, key: str) -> bytes:
            return self.values[key]

        def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
            body = self.values[key]
            assert hashlib.sha256(body).hexdigest() == expected_sha256
            assert len(body) == expected_size
            return body

        def delete_exact(self, key: str) -> None:
            self.values.pop(key, None)

    storage = Storage()
    created = persist_public_voice_catalog(source, repo, storage)  # type: ignore[arg-type]
    assert len(created) == 1
    voice = repo.voice_version(created[0])
    assert voice is not None and voice.reference_zip is not None
    assert repo.voice_media(created[0], "preview") is not None
    assert set(storage.values) == {
        "voice-catalog/v1/voice-1/reference.zip",
        "voice-catalog/v1/voice-1/preview.wav",
        "voice-catalog/v1/voice-1/avatar.png",
    }
    source_hash = import_public_voice_catalog(source).source_sha256
    assert persist_public_voice_catalog(source, repo, storage) == ()  # type: ignore[arg-type]
    assert repo.catalog_imports[source_hash] == (1, "completed")
    published = repo.voices[created[0]]
    repo.voices[created[0]] = replace(published, enabled=False)
    with pytest.raises(DomainError, match="internally inconsistent"):
        persist_public_voice_catalog(source, repo, storage)  # type: ignore[arg-type]
    repo.voices[created[0]] = published
    preview_facts = repo.voice_media_items[(created[0], "preview")]
    canonical_preview_key = "voice-catalog/v1/voice-1/preview.wav"
    rogue_preview_key = "voice-catalog/v1/voice-1/rogue.wav"
    storage.values[rogue_preview_key] = storage.values.pop(canonical_preview_key)
    repo.voice_media_items[(created[0], "preview")] = (
        rogue_preview_key,
        *preview_facts[1:],
    )
    with pytest.raises(DomainError, match="invalid or reused media facts"):
        persist_public_voice_catalog(source, repo, storage)  # type: ignore[arg-type]
    safe_generation_key = f"{canonical_preview_key}.generation-2-{'a' * 32}"
    storage.values[safe_generation_key] = storage.values.pop(rogue_preview_key)
    repo.voice_media_items[(created[0], "preview")] = (
        safe_generation_key,
        *preview_facts[1:],
    )
    assert persist_public_voice_catalog(source, repo, storage) == ()  # type: ignore[arg-type]
    storage.values[canonical_preview_key] = storage.values.pop(safe_generation_key)
    repo.voice_media_items[(created[0], "preview")] = preview_facts
    preview = storage.values.pop("voice-catalog/v1/voice-1/preview.wav")
    with pytest.raises(DomainError, match="missing or corrupt media"):
        persist_public_voice_catalog(source, repo, storage)  # type: ignore[arg-type]
    storage.values["voice-catalog/v1/voice-1/preview.wav"] = preview
    conflict = tmp_path / "conflict.zip"
    _catalog(conflict, display_name="Different Content")
    with pytest.raises(DomainError, match="different content"):
        persist_public_voice_catalog(conflict, repo, storage)  # type: ignore[arg-type]
    assert len(storage.values) == 3


def test_catalog_import_failure_leaves_staged_bytes_for_cleaner(tmp_path: Path) -> None:
    source = tmp_path / "catalog-failure.zip"
    _catalog(source)

    class FailingRepository(MemoryRepository):
        def save_catalog_import(self, *args: object, **kwargs: object) -> None:
            if self.voices:
                raise RuntimeError("injected catalog progress failure")
            super().save_catalog_import(*args, **kwargs)  # type: ignore[arg-type]

    class Storage:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}
            self.deleted: list[str] = []

        def put_at_key(
            self, key: str, body: bytes, media_type: str, expected_sha256: str
        ) -> object:
            del media_type
            assert hashlib.sha256(body).hexdigest() == expected_sha256
            self.values.setdefault(key, body)
            return type(
                "Stored",
                (),
                {
                    "key": key,
                    "created": True,
                    "claim": StagedObjectClaim(key, 1, uuid4()),
                },
            )()

        def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
            body = self.values[key]
            assert hashlib.sha256(body).hexdigest() == expected_sha256
            assert len(body) == expected_size
            return body

        def delete_exact(self, key: str) -> None:
            self.deleted.append(key)
            self.values.pop(key, None)

    storage = Storage()
    with pytest.raises(RuntimeError, match="progress failure"):
        repository = FailingRepository()
        persist_public_voice_catalog(source, repository, storage)  # type: ignore[arg-type]

    assert len(storage.values) == 3
    assert storage.deleted == []
    assert repository.list_voice_versions() == ()


@pytest.mark.parametrize("name", ["../escape", "/absolute"])
def test_catalog_rejects_unsafe_or_unknown_files(tmp_path: Path, name: str) -> None:
    source = tmp_path / "catalog.zip"
    _catalog(source, extra=(name, b"bad"))
    with pytest.raises(DomainError):
        import_public_voice_catalog(source)


def test_catalog_rejects_duplicate_and_symlink(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.zip"
    _catalog(duplicate, extra=("public_voice_library/avatars/voice-1.png", b"again"))
    with pytest.raises(DomainError):
        import_public_voice_catalog(duplicate)
    linked = ZipInfo("linked")
    linked.create_system = 3
    linked.external_attr = 0o120777 << 16
    symlink = tmp_path / "symlink.zip"
    _catalog(symlink, extra=(linked, b"target"))
    with pytest.raises(DomainError):
        import_public_voice_catalog(symlink)


def test_catalog_rejects_unreferenced_manifest_file(tmp_path: Path) -> None:
    source = tmp_path / "unreferenced.zip"
    _catalog(source, extra=("public_voice_library/avatars/unused.png", _png()))
    with pytest.raises(DomainError, match="not declared exactly once"):
        import_public_voice_catalog(source)
