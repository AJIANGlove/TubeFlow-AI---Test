from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tubeflow_test.domain.common import MediaAssetRef


@dataclass(frozen=True, slots=True)
class StoryboardProjectAssets:
    project_id: UUID
    assets: tuple[MediaAssetRef, ...]


@dataclass(frozen=True, slots=True)
class StoryboardDeletionObject:
    asset: MediaAssetRef
    object_key: str
    project_exclusive: bool
    deletion_token: UUID | None = None


class StoryboardProjectLifecyclePort(Protocol):
    def activate_storyboards(self, owner_id: UUID, project_id: UUID) -> None: ...
    def begin_storyboard_purge(self, owner_id: UUID, project_id: UUID) -> None: ...
    def has_active_storyboard_jobs(self, owner_id: UUID, project_id: UUID) -> bool: ...
    def enumerate_storyboard_assets(
        self, owner_id: UUID, project_id: UUID
    ) -> StoryboardProjectAssets: ...
    def detach_assets_for_purge_in_transaction(
        self, transaction: object, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardDeletionObject, ...]: ...
    def finalize_asset_deletion(self, asset_id: UUID, deletion_token: UUID) -> bool: ...
