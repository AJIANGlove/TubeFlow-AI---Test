from __future__ import annotations

import io
import json
import os
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from alembic import command
from alembic.config import Config
from minio import Minio
from PIL import Image
from sqlalchemy import create_engine, text

from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.runtime import create_identity_project_runtime

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _wav(frames: int, value: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(value.to_bytes(2, "little", signed=True) * frames)
    return output.getvalue()


def _catalog(path: Path, preview_value: int) -> None:
    reference = io.BytesIO()
    with ZipFile(reference, "w", ZIP_DEFLATED) as nested:
        nested.writestr("reference.wav", _wav(100, preview_value))
    manifest = {
        "version": 1,
        "count": 1,
        "voices": [
            {
                "id": "same-voice",
                "name": "same_voice",
                "version": 1,
                "display_name_zh": f"Voice {preview_value}",
                "gender": "female",
                "age_band": "young",
                "accent": "us_en",
                "accent_zh": "US English",
                "tags": ["narration"],
                "supported_languages": ["en"],
                "language_full_zh": "English",
                "description": "Concurrency fixture",
                "avatar_rel": "avatars/same.png",
                "preview_rel": "previews/same.wav",
                "ref_zip_rel": "refs/same.zip",
                "created_at": "2026-08-28T00:00:00Z",
                "author": "test",
                "_source": {"site": "test.invalid"},
            }
        ],
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        avatar = io.BytesIO()
        Image.new("RGB", (2, 2), (80, 120, 160)).save(avatar, format="PNG")
        archive.writestr("public_voice_library/index.json", json.dumps(manifest))
        archive.writestr("public_voice_library/refs/same.zip", reference.getvalue())
        archive.writestr("public_voice_library/previews/same.wav", _wav(100, preview_value))
        archive.writestr("public_voice_library/avatars/same.png", avatar.getvalue())


@pytest.mark.integration
def test_concurrent_conflicting_import_cannot_overwrite_winner(tmp_path: Path) -> None:
    postgres_url = os.environ.get("TUBEFLOW_TEST_POSTGRES_URL")
    minio_endpoint = os.environ.get("TUBEFLOW_TEST_MINIO_ENDPOINT")
    if not postgres_url or not minio_endpoint:
        pytest.skip("PostgreSQL and MinIO integration endpoints are required")
    minio_access_key = os.environ.get("TUBEFLOW_TEST_MINIO_ACCESS_KEY")
    minio_secret_key = os.environ.get("TUBEFLOW_TEST_MINIO_SECRET_KEY")
    if not minio_access_key or not minio_secret_key:
        pytest.fail("real MinIO tests require explicit access and secret key environment variables")
    schema = f"voice_race_{uuid4().hex}"
    bucket = f"voice-race-{uuid4().hex}"
    admin_engine = create_engine(postgres_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    database_url = f"{postgres_url}?options=-csearch_path%3D{schema}"
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(BACKEND_ROOT / "src"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    runtime = create_identity_project_runtime(
        Settings(
            database_url=database_url,
            identity_setup_username="admin",
            identity_setup_password="strong-password",
            minio_endpoint=minio_endpoint,
            minio_access_key=minio_access_key,
            minio_secret_key=minio_secret_key,
            minio_bucket=bucket,
        )
    )
    runtime.ensure_private_bucket()
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    _catalog(first, 1)
    _catalog(second, 2)

    def run(path: Path) -> str:
        try:
            runtime.import_voice_catalog(path)
            return "success"
        except DomainError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(run, (first, first, second)))
        assert set(results) == {"success", "voice_version_conflict"}
        assert results.count("success") in {1, 2}
        with runtime.database.unit_of_work() as session:
            voices = SqlAlchemyIdentityProjectRepository(session).list_voice_versions()
            assert len(voices) == 1
            voice = voices[0]
        assert voice.reference_zip is not None
        assert voice.preview is not None
        assert voice.avatar is not None
        reference = runtime.storage.get_verified(
            "voice-catalog/v1/same-voice/reference.zip",
            voice.reference_zip_sha256,
            voice.reference_zip.size_bytes,
        )
        preview = runtime.storage.get_verified(
            "voice-catalog/v1/same-voice/preview.wav",
            voice.preview_sha256,
            voice.preview.size_bytes,
        )
        avatar = runtime.storage.get_verified(
            "voice-catalog/v1/same-voice/avatar.png",
            voice.avatar_sha256,
            voice.avatar.size_bytes,
        )
        assert preview in {_wav(100, 1), _wav(100, 2)}
        with ZipFile(io.BytesIO(reference)) as archive:
            assert archive.read("reference.wav") == preview
        with Image.open(io.BytesIO(avatar)) as image:
            assert image.size == (2, 2)
    finally:
        client = Minio(
            minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            secure=False,
        )
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
        runtime.database.engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
