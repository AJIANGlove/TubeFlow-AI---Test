from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from uuid import UUID

import pytest

from tubeflow_test.api import narration_runtime
from tubeflow_test.application.narration_ports import VoiceReference
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import TtsProvider

OWNER_ID = UUID("00000000-0000-0000-0000-000000000001")
VOICE_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
CLONED_VOICE_ID = UUID("00000000-0000-0000-0000-000000000004")
REFERENCE_KEY = "voice-catalog/v1/voice/reference.zip"
REFERENCE_SHA256 = "a" * 64
REFERENCE_BYTES = b"verified-reference-zip"
REFERENCE_MEDIA = (
    REFERENCE_KEY,
    "application/zip",
    REFERENCE_SHA256,
    len(REFERENCE_BYTES),
)
SOURCE_SHA256 = "b" * 64


@dataclass(frozen=True)
class _Voice:
    id: UUID = VOICE_ID
    enabled: bool = True
    supported_locales: tuple[SupportedLocale, ...] = (SupportedLocale.EN_US,)


@dataclass(frozen=True)
class _Project:
    tts_provider: str = TtsProvider.COSYVOICE.value
    voice_version_id: UUID | None = VOICE_ID
    cloned_voice_id: UUID | None = None
    snapshot: object | None = None


@dataclass(frozen=True)
class _ClonedVoice:
    id: UUID = CLONED_VOICE_ID
    name: str = "旁白克隆"
    source_sha256: str = SOURCE_SHA256


def _patch_repository(
    monkeypatch: pytest.MonkeyPatch,
    *,
    voice: _Voice,
    project: _Project | None = None,
    cloned: _ClonedVoice | None = None,
    media: tuple[str, str, str, int] = REFERENCE_MEDIA,
    forbid_media_lookup: bool = False,
) -> object:
    session = object()

    class Repository:
        def __init__(self, actual_session: object) -> None:
            assert actual_session is session

        def project(self, owner_id: UUID, project_id: UUID) -> _Project | None:
            assert owner_id == OWNER_ID and project_id == PROJECT_ID
            return project if project is not None else _Project()

        def voice_version(self, voice_version_id: UUID) -> _Voice:
            assert voice_version_id == VOICE_ID
            return voice

        def voice_media(self, voice_version_id: UUID, kind: str) -> tuple[str, str, str, int]:
            assert not forbid_media_lookup
            assert (voice_version_id, kind) == (VOICE_ID, "reference")
            return media

        def cloned_voice_by_id(self, cloned_voice_id: UUID) -> _ClonedVoice | None:
            assert cloned_voice_id == CLONED_VOICE_ID
            return cloned

    monkeypatch.setattr(narration_runtime, "SqlAlchemyIdentityProjectRepository", Repository)
    return session


def test_voice_reference_uses_voice_repository_and_verified_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patch_repository(monkeypatch, voice=_Voice())
    verified_calls: list[tuple[str, str, int]] = []

    class Storage:
        def get_verified(self, key: str, sha256: str, size_bytes: int) -> bytes:
            verified_calls.append((key, sha256, size_bytes))
            return REFERENCE_BYTES

    adapter = narration_runtime.SqlVoiceReferenceAdapter(
        lambda: nullcontext(session),  # type: ignore[arg-type, return-value]
        Storage(),  # type: ignore[arg-type]
    )

    result = adapter.get_reference(OWNER_ID, PROJECT_ID, SupportedLocale.EN_US)

    assert result == VoiceReference(VOICE_ID, REFERENCE_BYTES, REFERENCE_SHA256)
    assert verified_calls == [(REFERENCE_KEY, REFERENCE_SHA256, len(REFERENCE_BYTES))]


def test_voice_reference_rejects_locale_before_media_or_storage_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patch_repository(
        monkeypatch,
        voice=_Voice(),
        forbid_media_lookup=True,
    )

    class Storage:
        def get_verified(self, key: str, sha256: str, size_bytes: int) -> bytes:
            del key, sha256, size_bytes
            raise AssertionError("storage must not be read for an incompatible locale")

    adapter = narration_runtime.SqlVoiceReferenceAdapter(
        lambda: nullcontext(session),  # type: ignore[arg-type, return-value]
        Storage(),  # type: ignore[arg-type]
    )

    with pytest.raises(DomainError) as captured:
        adapter.get_reference(OWNER_ID, PROJECT_ID, SupportedLocale.JA_JP)

    assert captured.value.code == "voice_not_available"


def test_voice_reference_maps_verified_storage_integrity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patch_repository(monkeypatch, voice=_Voice())

    class Storage:
        def get_verified(self, key: str, sha256: str, size_bytes: int) -> bytes:
            assert (key, sha256, size_bytes) == (
                REFERENCE_KEY,
                REFERENCE_SHA256,
                len(REFERENCE_BYTES),
            )
            raise ValueError("object SHA-256 integrity mismatch")

    adapter = narration_runtime.SqlVoiceReferenceAdapter(
        lambda: nullcontext(session),  # type: ignore[arg-type, return-value]
        Storage(),  # type: ignore[arg-type]
    )

    with pytest.raises(DomainError) as captured:
        adapter.get_reference(OWNER_ID, PROJECT_ID, SupportedLocale.EN_US)

    assert captured.value.code == "voice_reference_hash_mismatch"
    assert isinstance(captured.value.__cause__, ValueError)


def test_elevenlabs_reference_uses_logical_cloned_voice_and_skips_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§23.1：EL 分支只冻结逻辑克隆音色事实，不加载 reference ZIP。"""
    session = _patch_repository(
        monkeypatch,
        voice=_Voice(),
        project=_Project(
            tts_provider=TtsProvider.ELEVENLABS.value,
            voice_version_id=None,
            cloned_voice_id=CLONED_VOICE_ID,
        ),
        cloned=_ClonedVoice(),
        forbid_media_lookup=True,
    )

    class Storage:
        def get_verified(self, key: str, sha256: str, size_bytes: int) -> bytes:
            del key, sha256, size_bytes
            raise AssertionError("EL narration must not load a CosyVoice reference package")

    adapter = narration_runtime.SqlVoiceReferenceAdapter(
        lambda: nullcontext(session),  # type: ignore[arg-type, return-value]
        Storage(),  # type: ignore[arg-type]
    )

    result = adapter.get_reference(OWNER_ID, PROJECT_ID, SupportedLocale.EN_US)

    assert result.provider == TtsProvider.ELEVENLABS.value
    assert result.cloned_voice_id == CLONED_VOICE_ID
    assert result.cloned_voice_name == "旁白克隆"
    assert result.reference_sha256 == SOURCE_SHA256
    assert result.reference_zip == b""
    assert result.voice_version_id is None


def test_elevenlabs_reference_requires_a_known_cloned_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _patch_repository(
        monkeypatch,
        voice=_Voice(),
        project=_Project(
            tts_provider=TtsProvider.ELEVENLABS.value,
            voice_version_id=None,
            cloned_voice_id=CLONED_VOICE_ID,
        ),
        cloned=None,
        forbid_media_lookup=True,
    )

    class Storage:
        def get_verified(self, key: str, sha256: str, size_bytes: int) -> bytes:
            del key, sha256, size_bytes
            raise AssertionError("storage must not be read")

    adapter = narration_runtime.SqlVoiceReferenceAdapter(
        lambda: nullcontext(session),  # type: ignore[arg-type, return-value]
        Storage(),  # type: ignore[arg-type]
    )

    with pytest.raises(DomainError) as captured:
        adapter.get_reference(OWNER_ID, PROJECT_ID, SupportedLocale.EN_US)

    assert captured.value.code == "cloned_voice_unavailable"
