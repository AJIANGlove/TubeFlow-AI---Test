from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.models import CharacterImageRow, ProjectRow
from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.models import StagedObjectRow as MediaUploadJournalRow
from tubeflow_test.adapters.persistence.staged_claims import lock_staged_claim_identity
from tubeflow_test.application.ports import AssetLocation, UploadReservation
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.errors import DomainError


class SqlAlchemyAssetCatalog:
    def __init__(self, unit_of_work: Callable[[], AbstractContextManager[Session]]) -> None:
        self._uow = unit_of_work

    def resolve(self, asset: MediaAssetRef) -> AssetLocation:
        with self._uow() as session:
            media = session.get(MediaAssetRow, asset.asset_id)
            if media is not None:
                location = AssetLocation(
                    media.object_key,
                    MediaAssetRef(media.id, media.sha256, media.media_type, media.size_bytes),
                )
            else:
                character = session.scalar(
                    select(CharacterImageRow).where(CharacterImageRow.asset_id == asset.asset_id)
                )
                if character is None:
                    raise DomainError("not_found", "Media asset was not found")
                location = AssetLocation(
                    character.object_key,
                    MediaAssetRef(
                        character.asset_id,
                        character.sha256,
                        character.media_type,
                        character.size_bytes,
                    ),
                )
            if location.asset != asset:
                raise DomainError(
                    "storage_integrity_error", "Asset catalog facts do not match snapshot"
                )
            return location

    def register_storyboard(
        self, owner_id: UUID, project_id: UUID, object_key: str, asset: MediaAssetRef
    ) -> MediaAssetRef:
        with self._uow() as session:
            state = session.scalar(
                select(ProjectRow.production_state)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .with_for_update()
            )
            if state is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if state != "ACTIVE":
                raise DomainError("project_purging", "Project is being purged")
            lock_staged_claim_identity(
                session,
                owner_id=owner_id,
                ownership_scope="OWNER",
                project_id=None,
                logical_object_key=object_key,
            )
            existing = session.scalar(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.owner_id == owner_id,
                    MediaAssetRow.sha256 == asset.sha256,
                    MediaAssetRow.media_type == asset.media_type,
                    MediaAssetRow.shared.is_(True),
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.deletion_state != "ACTIVE":
                    raise DomainError("asset_deleting", "Storyboard asset is being deleted")
                if existing.size_bytes != asset.size_bytes:
                    raise DomainError("storage_integrity_error", "Existing media facts conflict")
                registered = MediaAssetRef(
                    existing.id, existing.sha256, existing.media_type, existing.size_bytes
                )
            else:
                session.add(
                    MediaAssetRow(
                        id=asset.asset_id,
                        owner_id=owner_id,
                        project_id=None,
                        ownership_scope="OWNER",
                        kind="STORYBOARD_GRID",
                        object_key=object_key,
                        sha256=asset.sha256,
                        media_type=asset.media_type,
                        size_bytes=asset.size_bytes,
                        metadata_json={"sha256": asset.sha256},
                        shared=True,
                    )
                )
                registered = asset
            reference = session.get(ProjectAssetReferenceRow, (project_id, registered.asset_id))
            if reference is None:
                session.add(
                    ProjectAssetReferenceRow(
                        project_id=project_id,
                        asset_id=registered.asset_id,
                        owner_id=owner_id,
                    )
                )
            return registered

    def prepare_storyboard_upload(
        self, owner_id: UUID, project_id: UUID, object_key: str, asset: MediaAssetRef
    ) -> UploadReservation:
        journal_id = uuid5(NAMESPACE_URL, f"storyboard-upload:{project_id}:{asset.asset_id}")
        execution_token = uuid4()
        with self._uow() as session:
            state = session.scalar(
                select(ProjectRow.production_state)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .with_for_update()
            )
            if state is None:
                raise DomainError("not_found", "Storyboard resource was not found")
            if state != "ACTIVE":
                raise DomainError("project_purging", "Project is being purged")
            lock_staged_claim_identity(
                session,
                owner_id=owner_id,
                ownership_scope="OWNER",
                project_id=None,
                logical_object_key=object_key,
            )
            media = session.scalar(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.owner_id == owner_id,
                    MediaAssetRow.sha256 == asset.sha256,
                    MediaAssetRow.media_type == asset.media_type,
                )
                .with_for_update()
            )
            if media is None:
                reserved_asset = asset
            elif (
                media.size_bytes != asset.size_bytes
                or media.deletion_state != "ACTIVE"
            ):
                raise DomainError("storage_integrity_error", "Staged media facts conflict")
            else:
                finalized = session.scalar(
                    select(MediaUploadJournalRow)
                    .where(
                        MediaUploadJournalRow.logical_object_key == object_key,
                        MediaUploadJournalRow.object_key == media.object_key,
                        MediaUploadJournalRow.owner_id == owner_id,
                        MediaUploadJournalRow.ownership_scope == "OWNER",
                        MediaUploadJournalRow.state == "FINALIZED",
                        MediaUploadJournalRow.asset_id == media.id,
                        MediaUploadJournalRow.sha256 == media.sha256,
                        MediaUploadJournalRow.media_type == media.media_type,
                        MediaUploadJournalRow.size_bytes == media.size_bytes,
                    )
                    .order_by(
                        MediaUploadJournalRow.generation.desc(),
                        MediaUploadJournalRow.created_at.desc(),
                        MediaUploadJournalRow.id.desc(),
                    )
                    .limit(1)
                )
                if finalized is None:
                    raise DomainError(
                        "storage_integrity_error", "Storyboard winner journal is invalid"
                    )
                reserved_asset = MediaAssetRef(
                    media.id, media.sha256, media.media_type, media.size_bytes
                )
                # The finalized journal belongs to the project that originally
                # created the shared object. Do not mutate or duplicate it: after
                # byte verification, finalize adds only this project's reference.
                return UploadReservation(
                    journal_id,
                    media.object_key,
                    reserved_asset,
                    0,
                    execution_token,
                    reused=True,
                )
            journal = session.scalar(
                select(MediaUploadJournalRow)
                .where(MediaUploadJournalRow.logical_object_key == object_key)
                .order_by(
                    MediaUploadJournalRow.generation.desc(),
                    MediaUploadJournalRow.created_at.desc(),
                    MediaUploadJournalRow.id.desc(),
                )
                .with_for_update()
                .limit(1)
            )
            actual_key = object_key
            if journal is None:
                journal = MediaUploadJournalRow(
                    id=journal_id,
                    owner_id=owner_id,
                    project_id=None,
                    asset_id=reserved_asset.asset_id,
                    object_key=object_key,
                    logical_object_key=object_key,
                    sha256=reserved_asset.sha256,
                    media_type=reserved_asset.media_type,
                    size_bytes=reserved_asset.size_bytes,
                    purpose="storyboard",
                    kind="STORYBOARD_GRID",
                    ownership_scope="OWNER",
                    state="PENDING_UPLOAD",
                    created_at=datetime.now(UTC),
                    generation=1,
                    execution_token=execution_token,
                    lease_until=datetime.now(UTC) + timedelta(minutes=5),
                )
                session.add(journal)
                generation = 1
            elif (
                journal.owner_id != owner_id
                or journal.ownership_scope != "OWNER"
                or journal.sha256 != reserved_asset.sha256
                or journal.size_bytes != reserved_asset.size_bytes
                or journal.media_type != reserved_asset.media_type
            ):
                raise DomainError("upload_claim_lost", "Storyboard upload owner or facts conflict")
            elif journal.state == "FINALIZED":
                raise DomainError("upload_claim_lost", "Storyboard upload already has a winner")
            elif journal.state == "CLEANUP_PENDING" and journal.cleanup_checked_at is not None:
                generation = journal.generation + 1
                actual_key = f"{object_key}.generation-{generation}-{uuid4().hex}"
                journal = MediaUploadJournalRow(
                    id=uuid4(),
                    owner_id=owner_id,
                    project_id=None,
                    asset_id=reserved_asset.asset_id,
                    object_key=actual_key,
                    logical_object_key=object_key,
                    sha256=reserved_asset.sha256,
                    media_type=reserved_asset.media_type,
                    size_bytes=reserved_asset.size_bytes,
                    purpose="storyboard",
                    kind="STORYBOARD_GRID",
                    ownership_scope="OWNER",
                    state="PENDING_UPLOAD",
                    created_at=datetime.now(UTC),
                    generation=generation,
                    execution_token=execution_token,
                    lease_until=datetime.now(UTC) + timedelta(minutes=5),
                )
                session.add(journal)
            else:
                raise DomainError("upload_claim_lost", "Storyboard upload is already claimed")
            return UploadReservation(
                journal.id,
                actual_key,
                reserved_asset,
                generation,
                execution_token,
            )

    def mark_storyboard_uploaded(
        self, owner_id: UUID, project_id: UUID, reservation: UploadReservation
    ) -> None:
        with self._uow() as session:
            state = session.scalar(
                select(ProjectRow.production_state)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .with_for_update()
            )
            if reservation.reused:
                media = session.scalar(
                    select(MediaAssetRow)
                    .where(MediaAssetRow.id == reservation.asset.asset_id)
                    .with_for_update()
                )
                if (
                    state != "ACTIVE"
                    or media is None
                    or media.deletion_state != "ACTIVE"
                    or media.deletion_token is not None
                ):
                    raise DomainError("upload_claim_lost", "Shared asset is unavailable")
                return
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(MediaUploadJournalRow)
                    .where(
                        MediaUploadJournalRow.id == reservation.journal_id,
                        MediaUploadJournalRow.owner_id == owner_id,
                        MediaUploadJournalRow.ownership_scope == "OWNER",
                        MediaUploadJournalRow.asset_id == reservation.asset.asset_id,
                        MediaUploadJournalRow.state == "PENDING_UPLOAD",
                        MediaUploadJournalRow.generation == reservation.generation,
                        MediaUploadJournalRow.execution_token == reservation.execution_token,
                    )
                    .values(state="STAGED", lease_until=None)
                ),
            )
            if state != "ACTIVE" or not result.rowcount:
                raise DomainError("upload_claim_lost", "Storyboard upload claim was lost")

    def finalize_storyboard_upload(
        self, owner_id: UUID, project_id: UUID, reservation: UploadReservation
    ) -> MediaAssetRef:
        with self._uow() as session:
            state = session.scalar(
                select(ProjectRow.production_state)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .with_for_update()
            )
            if reservation.reused:
                media = session.scalar(
                    select(MediaAssetRow)
                    .where(MediaAssetRow.id == reservation.asset.asset_id)
                    .with_for_update()
                )
                if (
                    state != "ACTIVE"
                    or media is None
                    or media.deletion_state != "ACTIVE"
                    or media.deletion_token is not None
                    or media.object_key != reservation.object_key
                    or media.sha256 != reservation.asset.sha256
                    or media.media_type != reservation.asset.media_type
                    or media.size_bytes != reservation.asset.size_bytes
                ):
                    raise DomainError("asset_deleting", "Shared storyboard asset is unavailable")
                reference = session.get(
                    ProjectAssetReferenceRow, (project_id, reservation.asset.asset_id)
                )
                if reference is None:
                    session.add(
                        ProjectAssetReferenceRow(
                            project_id=project_id,
                            asset_id=reservation.asset.asset_id,
                            owner_id=owner_id,
                        )
                    )
                return reservation.asset
            journal = session.scalar(
                select(MediaUploadJournalRow)
                .where(
                    MediaUploadJournalRow.id == reservation.journal_id,
                    MediaUploadJournalRow.owner_id == owner_id,
                    MediaUploadJournalRow.ownership_scope == "OWNER",
                    MediaUploadJournalRow.state == "STAGED",
                    MediaUploadJournalRow.generation == reservation.generation,
                    MediaUploadJournalRow.execution_token == reservation.execution_token,
                )
                .with_for_update()
            )
            if state != "ACTIVE" or journal is None:
                raise DomainError("project_purging", "Project is being purged")
            if (
                journal.object_key != reservation.object_key
                or journal.sha256 != reservation.asset.sha256
                or journal.media_type != reservation.asset.media_type
                or journal.size_bytes != reservation.asset.size_bytes
            ):
                raise DomainError("storage_integrity_error", "Upload journal facts conflict")
            media = session.scalar(
                select(MediaAssetRow)
                .where(MediaAssetRow.id == reservation.asset.asset_id)
                .with_for_update()
            )
            if media is None:
                media = MediaAssetRow(
                    id=reservation.asset.asset_id,
                    owner_id=owner_id,
                    project_id=None,
                    ownership_scope="OWNER",
                    kind="STORYBOARD_GRID",
                    object_key=reservation.object_key,
                    sha256=reservation.asset.sha256,
                    media_type=reservation.asset.media_type,
                    size_bytes=reservation.asset.size_bytes,
                    metadata_json={"sha256": reservation.asset.sha256},
                    shared=True,
                )
                session.add(media)
                session.flush()
            if (
                media.deletion_state != "ACTIVE"
                or media.deletion_token is not None
                or media.sha256 != reservation.asset.sha256
            ):
                raise DomainError("asset_deleting", "Storyboard asset is being deleted")
            reference = session.get(
                ProjectAssetReferenceRow, (project_id, reservation.asset.asset_id)
            )
            if reference is None:
                session.add(
                    ProjectAssetReferenceRow(
                        project_id=project_id,
                        asset_id=reservation.asset.asset_id,
                        owner_id=owner_id,
                    )
                )
            journal.state = "ATTACHED"
            session.flush()
            journal.state = "FINALIZED"
            journal.execution_token = None
            journal.lease_until = None
            media.metadata_json = {**media.metadata_json, "upload_state": "FINALIZED"}
            return reservation.asset
