from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid5

from minio import Minio

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.application.ports import AssetCatalogPort
from tubeflow_test.domain.common import MediaAssetRef

_NAMESPACE = UUID("d646736d-7d8c-4f4b-8d98-3c5e2796edab")


class MinioStoryboardObjectStorage:
    """Restart-safe adapter whose exact object key is the deterministic asset UUID."""

    def __init__(self, client: Minio, bucket: str, catalog: AssetCatalogPort) -> None:
        self._storage = PrivateObjectStorage(client, bucket)
        self._catalog = catalog

    def put_verified(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        source: Path,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef:
        payload = source.read_bytes()
        import hashlib

        digest = hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            raise ValueError("object input hash mismatch")
        asset_id = uuid5(_NAMESPACE, f"{owner_id}:{digest}:{media_type}")
        key = f"storyboard-{owner_id.hex}-{digest}-{media_type.replace('/', '-')}"
        reservation = self._catalog.prepare_storyboard_upload(
            owner_id,
            project_id,
            key,
            MediaAssetRef(asset_id, digest, media_type, len(payload)),
        )
        self._storage.put_at_key(
            reservation.object_key,
            payload,
            media_type,
            reservation.asset.sha256,
        )
        try:
            self._catalog.mark_storyboard_uploaded(owner_id, project_id, reservation)
        except BaseException:
            # A concurrent project may have reused and attached the exact key
            # after this request created it. Cleanup requires the journal's CAS
            # claim plus an authoritative no-reference check.
            raise
        return self._catalog.finalize_storyboard_upload(
            owner_id, project_id, reservation
        )

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        location = self._catalog.resolve(asset)
        return self._storage.get_verified(
            location.object_key, asset.sha256, asset.size_bytes
        )

    def delete_exact(self, asset: MediaAssetRef) -> None:
        self._storage.delete_exact(self._catalog.resolve(asset).object_key)
