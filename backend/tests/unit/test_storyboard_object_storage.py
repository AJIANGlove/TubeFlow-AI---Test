from __future__ import annotations

from typing import cast
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from tubeflow_test.adapters.storyboard_object_storage import MinioStoryboardObjectStorage
from tubeflow_test.application.ports import AssetCatalogPort, AssetLocation, UploadReservation
from tubeflow_test.domain.common import MediaAssetRef


class Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class Client:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.keys: list[str] = []

    def get_object(self, bucket: str, key: str) -> Response:
        del bucket
        self.keys.append(key)
        return Response(self.payload)


class Catalog:
    def __init__(self, location: AssetLocation) -> None:
        self.location = location

    def resolve(self, asset: MediaAssetRef) -> AssetLocation:
        assert asset == self.location.asset
        return self.location

    def register_storyboard(
        self, owner_id: object, project_id: object, object_key: str, asset: MediaAssetRef
    ) -> MediaAssetRef:
        del owner_id, project_id, object_key
        return asset

    def prepare_storyboard_upload(
        self, owner_id: object, project_id: object, object_key: str, asset: MediaAssetRef
    ) -> UploadReservation:
        del owner_id, project_id
        return UploadReservation(uuid4(), object_key, asset, 1, uuid4())

    def finalize_storyboard_upload(
        self, owner_id: object, project_id: object, reservation: UploadReservation
    ) -> MediaAssetRef:
        del owner_id, project_id
        return reservation.asset

    def mark_storyboard_uploaded(
        self, owner_id: object, project_id: object, reservation: UploadReservation
    ) -> None:
        del owner_id, project_id, reservation


def test_private_character_read_uses_catalog_object_key_not_asset_uuid() -> None:
    payload = b"private-character"
    import hashlib

    asset = MediaAssetRef(uuid4(), hashlib.sha256(payload).hexdigest(), "image/png", len(payload))
    location = AssetLocation("real-private-minio-key", asset)
    client = Client(payload)
    storage = MinioStoryboardObjectStorage(
        cast(Minio, client), "private", cast(AssetCatalogPort, Catalog(location))
    )
    assert storage.read_verified(asset) == payload
    assert client.keys == ["real-private-minio-key"]


def test_existing_storyboard_object_is_rehashed_before_catalog_attach(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import hashlib
    expected = b"expected-grid"
    corrupt = b"corrupt-grid!"
    source = tmp_path / "grid.png"
    source.write_bytes(expected)
    asset = MediaAssetRef(
        uuid4(), hashlib.sha256(expected).hexdigest(), "image/png", len(expected)
    )
    location = AssetLocation("unused", asset)

    class Existing(Client):
        def _execute(  # type: ignore[no-untyped-def]
            self, method, bucket, key, *, body, headers, no_body_trace
        ):
            del method, bucket, key, body, headers
            assert no_body_trace is True
            raise S3Error(None, "PreconditionFailed", "exists", None, None, None)

        def get_object(self, bucket: str, key: str) -> Response:
            del bucket, key
            return Response(corrupt)

    storage = MinioStoryboardObjectStorage(
        cast(Minio, Existing(corrupt)), "private", cast(AssetCatalogPort, Catalog(location))
    )
    import pytest

    with pytest.raises(ValueError, match="integrity mismatch"):
        storage.put_verified(
            owner_id=uuid4(),
            project_id=uuid4(),
            source=source,
            media_type="image/png",
            expected_sha256=hashlib.sha256(expected).hexdigest(),
        )


def test_creator_claim_loss_never_deletes_concurrently_attached_reuse(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import hashlib

    payload = b"shared-storyboard-grid"
    source = tmp_path / "grid.png"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    class ConditionalClient:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.removed: list[str] = []

        def _execute(  # type: ignore[no-untyped-def]
            self, method, bucket, key, *, body, headers, no_body_trace
        ):
            del method, bucket, headers
            assert no_body_trace is True
            if key in self.objects:
                raise S3Error(None, "PreconditionFailed", "exists", None, None, None)
            self.objects[key] = body
            return Response(b"")

        def get_object(self, bucket: str, key: str) -> Response:
            del bucket
            return Response(self.objects[key])

        def remove_object(self, bucket: str, key: str) -> None:
            del bucket
            self.removed.append(key)
            self.objects.pop(key, None)

    client = ConditionalClient()
    owner_id, project_a, project_b = uuid4(), uuid4(), uuid4()
    location = AssetLocation(
        "unused", MediaAssetRef(uuid4(), digest, "image/png", len(payload))
    )
    catalog_b = Catalog(location)
    storage_b = MinioStoryboardObjectStorage(
        cast(Minio, client), "private", cast(AssetCatalogPort, catalog_b)
    )

    class LosingCatalog(Catalog):
        def mark_storyboard_uploaded(self, owner_id, project_id, reservation) -> None:  # type: ignore[no-untyped-def]
            storage_b.put_verified(
                owner_id=owner_id,
                project_id=project_b,
                source=source,
                media_type="image/png",
                expected_sha256=digest,
            )
            raise RuntimeError("creator lost its journal claim")

    storage_a = MinioStoryboardObjectStorage(
        cast(Minio, client), "private", cast(AssetCatalogPort, LosingCatalog(location))
    )
    import pytest

    with pytest.raises(RuntimeError, match="lost its journal claim"):
        storage_a.put_verified(
            owner_id=owner_id,
            project_id=project_a,
            source=source,
            media_type="image/png",
            expected_sha256=digest,
        )

    assert len(client.objects) == 1
    assert next(iter(client.objects.values())) == payload
    assert client.removed == []
