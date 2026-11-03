from __future__ import annotations

import hashlib
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from minio.error import S3Error

from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.video_generation.adapters import (
    ImmutableVideoObjectStorage,
    SqlObjectVideoAssetStore,
)
from tubeflow_test.video_generation.domain import VideoAssetIntegrityError


class Session:
    def __init__(self, row: object | None, *, missing_get: bool = False) -> None:
        self._row = row
        self._missing_get = missing_get
        self.active = False

    def __enter__(self) -> Session:
        self.active = True
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True
        return None

    closed = False

    def get(self, _model: object, _asset_id: object) -> object | None:
        return None if self._missing_get else self._row

    def begin(self):  # type: ignore[no-untyped-def]
        return nullcontext()

    def scalar(self, _statement: object) -> object | None:
        return self._row


class Sessions:
    def __init__(self, row: object | None, *, missing_get: bool = False) -> None:
        self._row = row
        self._missing_get = missing_get
        self.created: list[Session] = []

    def __call__(self) -> Session:
        session = Session(self._row, missing_get=self._missing_get)
        self.created.append(session)
        return session


def _asset_and_row() -> tuple[MediaAssetRef, object]:
    content = b"real-audio"
    asset = MediaAssetRef(uuid4(), hashlib.sha256(content).hexdigest(), "audio/wav", len(content))
    row = SimpleNamespace(
        id=asset.asset_id,
        owner_id=uuid4(),
        project_id=uuid4(),
        kind="narration_normalized",
        object_key="audio/key",
        sha256=asset.sha256,
        media_type=asset.media_type,
        size_bytes=asset.size_bytes,
        deletion_state="ACTIVE",
        purging=False,
    )
    return asset, row


@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_production_asset_store_classifies_deterministic_object_failure(
    failure: str,
) -> None:
    asset, row = _asset_and_row()

    class Objects:
        def get(self, _key: str) -> bytes:
            if failure == "missing":
                raise S3Error(None, "NoSuchKey", "missing", None, None, None)
            return b"tampered"

    store = SqlObjectVideoAssetStore(Sessions(row), Objects())  # type: ignore[arg-type]
    with pytest.raises(VideoAssetIntegrityError):
        store.read_verified(asset)


def test_production_asset_store_closes_first_db_session_before_object_io() -> None:
    asset, row = _asset_and_row()
    sessions = Sessions(row)

    class Objects:
        def get(self, _key: str) -> bytes:
            assert sessions.created and sessions.created[0].closed
            return b"real-audio"

    store = SqlObjectVideoAssetStore(sessions, Objects())  # type: ignore[arg-type]
    assert store.read_verified(asset) == b"real-audio"
    assert len(sessions.created) == 2


def _character_asset_and_row() -> tuple[MediaAssetRef, object]:
    content = b"real-character-image"
    asset = MediaAssetRef(uuid4(), hashlib.sha256(content).hexdigest(), "image/png", len(content))
    row = SimpleNamespace(
        asset_id=asset.asset_id,
        owner_id=uuid4(),
        object_key="characters/key",
        sha256=asset.sha256,
        media_type=asset.media_type,
        size_bytes=asset.size_bytes,
    )
    return asset, row


def test_character_reference_image_reads_via_character_catalog() -> None:
    asset, character_row = _character_asset_and_row()

    class Objects:
        def get(self, key: str) -> bytes:
            assert key == "characters/key"
            return b"real-character-image"

    store = SqlObjectVideoAssetStore(  # type: ignore[arg-type]
        Sessions(character_row, missing_get=True), Objects()
    )
    assert store.read_verified(asset) == b"real-character-image"


@pytest.mark.parametrize("failure", ["tampered", "bad_facts", "uncatalogued"])
def test_character_reference_image_integrity_failures(failure: str) -> None:
    asset, character_row = _character_asset_and_row()
    if failure == "bad_facts":
        character_row.sha256 = "0" * 64

    class Objects:
        def get(self, _key: str) -> bytes:
            return b"tampered" if failure == "tampered" else b"real-character-image"

    row = None if failure == "uncatalogued" else character_row
    store = SqlObjectVideoAssetStore(  # type: ignore[arg-type]
        Sessions(row, missing_get=True), Objects()
    )
    with pytest.raises(VideoAssetIntegrityError):
        store.read_verified(asset)


def test_h3_object_storage_uses_canonical_conditional_create() -> None:
    payload = b"winner"

    class Response:
        def __init__(self, body: bytes = b"") -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def close(self) -> None:
            pass

        def release_conn(self) -> None:
            pass

    class Client:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def _execute(self, _method: str, _bucket: str, _key: str, **kwargs: object) -> Response:
            self.headers = dict(kwargs["headers"])  # type: ignore[arg-type]
            return Response()

        def get_object(self, _bucket: str, _key: str) -> Response:
            return Response(payload)

    client = Client()
    storage = ImmutableVideoObjectStorage(client, "archive")  # type: ignore[arg-type]
    digest = hashlib.sha256(payload).hexdigest()
    stored = storage.put_at_key("h3/key", payload, "video/mp4", digest)
    assert stored.created
    assert client.headers["If-None-Match"] == "*"
