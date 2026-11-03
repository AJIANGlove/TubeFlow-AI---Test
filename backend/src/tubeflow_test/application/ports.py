from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from tubeflow_test.domain.common import MediaAssetRef


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdentifierFactory(Protocol):
    def new(self) -> UUID: ...


class ObjectStoragePort(Protocol):
    def put_verified(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        source: Path,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef: ...

    def read_verified(self, asset: MediaAssetRef) -> bytes: ...

    def delete_exact(self, asset: MediaAssetRef) -> None: ...


@dataclass(frozen=True, slots=True)
class AssetLocation:
    object_key: str
    asset: MediaAssetRef


@dataclass(frozen=True, slots=True)
class UploadReservation:
    journal_id: UUID
    object_key: str
    asset: MediaAssetRef
    generation: int
    execution_token: UUID
    reused: bool = False


class AssetCatalogPort(Protocol):
    def resolve(self, asset: MediaAssetRef) -> AssetLocation: ...

    def register_storyboard(
        self, owner_id: UUID, project_id: UUID, object_key: str, asset: MediaAssetRef
    ) -> MediaAssetRef: ...
    def prepare_storyboard_upload(
        self, owner_id: UUID, project_id: UUID, object_key: str, asset: MediaAssetRef
    ) -> UploadReservation: ...
    def finalize_storyboard_upload(
        self, owner_id: UUID, project_id: UUID, reservation: UploadReservation
    ) -> MediaAssetRef: ...
    def mark_storyboard_uploaded(
        self, owner_id: UUID, project_id: UUID, reservation: UploadReservation
    ) -> None: ...
