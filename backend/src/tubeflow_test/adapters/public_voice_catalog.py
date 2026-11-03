from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import subprocess
import warnings
import wave
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from zipfile import BadZipFile, ZipFile, ZipInfo

from PIL import Image, UnidentifiedImageError

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.voice_catalog import CatalogFile, PublicVoiceCatalog, PublicVoiceVersion

ROOT = "public_voice_library"
INDEX = f"{ROOT}/index.json"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_INDEX_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_RATIO = 100
FIXED_CATALOG_SOURCE_SHA256 = "b4da63a5ecc06ecd0a6a238f21da198c8316e393a110c9d495f706da2d7655be"
FIXED_CATALOG_VOICE_COUNT = 402
PROJECT_LANGUAGES = {
    "en": "en-US",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "es": "es-ES",
    "fr": "fr-FR",
    "pt": "pt-BR",
    "pt-BR": "pt-BR",
}
LANGUAGE_CODE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
MEDIA = {
    ".zip": "application/zip",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def import_public_voice_catalog(
    path: Path, *, ffmpeg_executable: str = "ffmpeg"
) -> PublicVoiceCatalog:
    try:
        return _import_public_voice_catalog(path, ffmpeg_executable=ffmpeg_executable)
    except DomainError:
        raise
    except (
        BadZipFile,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as error:
        raise DomainError("voice_catalog_invalid", "Voice catalog archive is malformed.") from error


def _import_public_voice_catalog(path: Path, *, ffmpeg_executable: str) -> PublicVoiceCatalog:
    if path.is_symlink():
        _invalid("Voice catalog source is not a safe regular archive.")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not 0 < resolved.stat().st_size <= MAX_ARCHIVE_BYTES:
        _invalid("Voice catalog source is not a safe regular archive.")
    source_sha256 = _file_sha256(resolved)
    try:
        with ZipFile(resolved) as archive:
            members = _members(archive)
            index = members[INDEX]
            if index.file_size > MAX_INDEX_BYTES:
                _invalid("Voice catalog index is too large.")
            raw = json.loads(
                archive.read(index).decode("utf-8-sig"), object_pairs_hook=_unique_object
            )
            if not isinstance(raw, dict):
                _invalid("Voice catalog index must be an object.")
            version = _positive(raw.get("version"))
            voices_raw = raw.get("voices")
            if not isinstance(voices_raw, list) or _positive(raw.get("count")) != len(voices_raw):
                _invalid("Voice catalog count is invalid.")
            used: set[str] = set()
            voices = tuple(
                _voice(
                    archive,
                    members,
                    item,
                    version,
                    used,
                    ffmpeg_executable=ffmpeg_executable,
                )
                for item in voices_raw
            )
            if len({item.voice_id for item in voices}) != len(voices):
                _invalid("Voice catalog source identifiers must be unique.")
            expected_files = {INDEX, *used}
            actual_files = {name for name, info in members.items() if not info.is_dir()}
            if actual_files != expected_files:
                _invalid("Voice catalog contains files that are not declared exactly once.")
            return PublicVoiceCatalog(version, source_sha256, voices)
    except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        raise DomainError("voice_catalog_invalid", "Voice catalog archive is malformed.") from error


def _members(archive: ZipFile) -> dict[str, ZipInfo]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_MEMBERS:
        _invalid("Voice catalog member count is unsafe.")
    result: dict[str, ZipInfo] = {}
    total = 0
    for info in infos:
        name = _safe_name(info.filename)
        if name in result or _symlink(info) or info.flag_bits & 1:
            _invalid("Voice catalog has duplicate, linked, or encrypted members.")
        if not info.is_dir():
            _validate_file(info)
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                _invalid("Voice catalog expands beyond the safety limit.")
        result[name] = info
    if INDEX not in result:
        _invalid("Voice catalog index is missing.")
    return result


def _voice(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    raw: Any,
    version: int,
    used: set[str],
    *,
    ffmpeg_executable: str,
) -> PublicVoiceVersion:
    if not isinstance(raw, dict) or _positive(raw.get("version")) != version:
        _invalid("Voice entry fields or version are invalid.")
    voice_id = _text(raw.get("id"), 120)
    if (
        voice_id in {".", ".."}
        or any(char in voice_id for char in ("/", "\\", "\x00", "\r", "\n", "\t"))
        or any(ord(char) < 32 or ord(char) == 127 for char in voice_id)
    ):
        _invalid("Voice identifier must be one safe path segment.")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", voice_id) is None:
        _invalid("Voice identifier is unsafe.")
    raw_languages = _texts(raw.get("supported_languages"), 20)
    if any(LANGUAGE_CODE.fullmatch(value) is None for value in raw_languages):
        _invalid("Voice languages are invalid.")
    reference = _file(archive, members, raw.get("ref_zip_rel"), "refs", {".zip"}, used)
    preview = _file(archive, members, raw.get("preview_rel"), "previews", {".wav", ".mp3"}, used)
    avatar = _file(
        archive, members, raw.get("avatar_rel"), "avatars", {".png", ".jpg", ".jpeg", ".webp"}, used
    )
    _validate_reference(
        archive.read(members[reference.member_name]), ffmpeg_executable=ffmpeg_executable
    )
    _validate_audio(
        archive.read(members[preview.member_name]),
        PurePosixPath(preview.member_name).suffix.lower(),
        ffmpeg_executable=ffmpeg_executable,
    )
    _validate_avatar(archive.read(members[avatar.member_name]), avatar.media_type)
    source = raw.get("_source")
    source_site = source.get("site") if isinstance(source, dict) else None
    created_at = _text(raw.get("created_at"), 80)
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        _invalid("Voice creation time is invalid.")
    if parsed.tzinfo is None:
        _invalid("Voice creation time must include a timezone.")
    language_display = raw.get("language_full_zh") or raw.get("language_display_zh")
    return PublicVoiceVersion(
        voice_id=voice_id,
        version=version,
        display_name=_text(raw.get("display_name_zh"), 240),
        supported_languages=tuple(
            dict.fromkeys(
                PROJECT_LANGUAGES[value] for value in raw_languages if value in PROJECT_LANGUAGES
            )
        ),
        reference_zip=reference,
        preview_audio=preview,
        avatar_image=avatar,
        metadata_sha256=_sha(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ),
        enabled=True,
        raw_language_codes=raw_languages,
        internal_name=_text(raw.get("name"), 200),
        gender=_choice(raw.get("gender"), {"female", "male"}),
        age_band=_choice(raw.get("age_band"), {"young", "middle", "old", "child"}),
        accent=_text(raw.get("accent"), 80),
        accent_display_name=_text(raw.get("accent_zh"), 200),
        tags=_texts(raw.get("tags"), 200),
        language_display_name=_text(language_display, 2000),
        description=_text(raw.get("description"), 5000),
        created_at=created_at,
        author=_text(raw.get("author"), 200),
        source_site=_text(source_site, 2000),
    )


def _file(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    raw: Any,
    group: str,
    suffixes: set[str],
    used: set[str],
) -> CatalogFile:
    relative = _safe_name(_text(raw, 300))
    path = PurePosixPath(relative)
    name = f"{ROOT}/{relative}"
    if (
        len(path.parts) != 2
        or path.parts[0] != group
        or path.suffix.lower() not in suffixes
        or name not in members
        or name in used
    ):
        _invalid("Voice media is missing, duplicated, or outside its required directory.")
    used.add(name)
    payload = archive.read(members[name])
    return CatalogFile(name, MEDIA[path.suffix.lower()], len(payload), _sha(payload))


def _validate_reference(payload: bytes, *, ffmpeg_executable: str) -> None:
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) != 1:
                _invalid("Reference ZIP must contain exactly one audio file.")
            info = infos[0]
            path = PurePosixPath(_safe_name(info.filename))
            suffix = path.suffix.lower()
            if (
                info.is_dir()
                or _symlink(info)
                or info.flag_bits & 1
                or len(path.parts) != 1
                or suffix not in {".wav", ".mp3"}
            ):
                _invalid("Reference ZIP member is unsafe or unsupported.")
            _validate_file(info)
            _validate_audio(archive.read(info), suffix, ffmpeg_executable=ffmpeg_executable)
    except BadZipFile as error:
        raise DomainError("voice_catalog_invalid", "Reference ZIP is malformed.") from error


def _validate_audio(payload: bytes, suffix: str, *, ffmpeg_executable: str) -> None:
    if not payload:
        _invalid("Voice audio is empty or cannot be decoded.")
    if suffix == ".wav":
        try:
            with wave.open(io.BytesIO(payload), "rb") as audio:
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                sample_rate = audio.getframerate()
                frame_count = audio.getnframes()
                compression = audio.getcomptype()
                frames = audio.readframes(frame_count + 1)
        except (EOFError, OSError, ValueError, wave.Error) as error:
            raise DomainError(
                "voice_catalog_invalid", "Voice WAV audio cannot be decoded."
            ) from error
        expected_bytes = frame_count * channels * sample_width
        if (
            compression != "NONE"
            or channels not in {1, 2}
            or sample_width not in {1, 2, 3, 4}
            or sample_rate <= 0
            or frame_count <= 0
            or len(frames) != expected_bytes
        ):
            _invalid("Voice WAV audio is empty, truncated, or unsupported.")
        return
    try:
        result = subprocess.run(
            [
                ffmpeg_executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DomainError(
            "voice_catalog_invalid", "Voice MP3 decoder is unavailable or timed out."
        ) from error
    if result.returncode != 0:
        _invalid("Voice MP3 audio cannot be decoded.")


def _validate_avatar(payload: bytes, expected_media_type: str) -> None:
    formats = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    try:
        with Image.open(io.BytesIO(payload)) as image:
            media_type = formats.get(image.format or "")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 40_000_000:
                _invalid("Voice avatar dimensions are invalid.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image.load()
    except DomainError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise DomainError("voice_catalog_invalid", "Voice avatar cannot be decoded.") from error
    if media_type != expected_media_type:
        _invalid("Voice avatar content does not match its declared file type.")


def _validate_file(info: ZipInfo) -> None:
    if info.is_dir() or info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
        _invalid("Voice catalog member size is invalid.")
    if info.file_size and info.compress_size <= 0:
        _invalid("Voice catalog member compression metadata is invalid.")
    if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
        _invalid("Voice catalog compression ratio is unsafe.")


def _safe_name(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        _invalid("Archive member path is unsafe.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _invalid("Archive member path is unsafe.")
    return path.as_posix()


def _symlink(info: ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        _invalid("Voice catalog text is invalid.")
    return value.strip()


def _texts(value: Any, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        _invalid("Voice catalog list is invalid.")
    values = tuple(_text(item, maximum) for item in value)
    if not values or len(values) != len(set(values)):
        _invalid("Voice catalog list is invalid.")
    return values


def _choice(value: Any, choices: set[str]) -> str:
    result = _text(value, 30)
    if result not in choices:
        _invalid("Voice catalog choice is unsupported.")
    return result


def _positive(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _invalid("Voice catalog version is invalid.")
    try:
        result = int(value)
    except ValueError as error:
        raise DomainError("voice_catalog_invalid", "Voice catalog version is invalid.") from error
    if result < 1:
        _invalid("Voice catalog version is invalid.")
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _invalid(message: str) -> NoReturn:
    raise DomainError("voice_catalog_invalid", message)
