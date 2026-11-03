from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.object_storage import StoredObject
from tubeflow_test.adapters.persistence.identity_project.write_gate import SqlProjectWriteGate
from tubeflow_test.adapters.persistence.models import (
    MediaAssetRow,
    ProjectAssetReferenceRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.staged_claims import lock_staged_claim_identity
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.video_generation.domain import (
    ReferencePolicy,
    ShotPanelImage,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import AuthoritativeGroupInput
from tubeflow_test.video_generation.panel_images import DerivedPanelImage, derive_shot_panels

logger = logging.getLogger(__name__)
_TRACKER_KEY = "tubeflow.shot-panel-uploads"


class VerifiedAssetReader(Protocol):
    def read_verified(self, asset: MediaAssetRef) -> bytes: ...


class ShotPanelObjectStorage(Protocol):
    def put_at_key(
        self, key: str, body: bytes, media_type: str, expected_sha256: str
    ) -> StoredObject: ...

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete_exact(self, key: str) -> None: ...


class ShotPanelAssetService:
    """Preview in memory; archive immutable PNGs with the caller's intent transaction."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        objects: ShotPanelObjectStorage,
        reader: VerifiedAssetReader,
    ) -> None:
        self._sessions = sessions
        self._objects = objects
        self._reader = reader

    def derive(
        self,
        session: Session,
        source: AuthoritativeGroupInput,
        policy: ReferencePolicy,
        *,
        for_intent: bool,
    ) -> tuple[ShotPanelImage, ...]:
        content = self._reader.read_verified(source.storyboard_image)
        panels = derive_shot_panels(
            content,
            source_sha256=source.storyboard_image.sha256,
            panel_count=len(source.director_shots),
            aspect_ratio=source.aspect_ratio.value,
            derivation=policy.derivation or {},
        )
        if len(source.character_images) + len(panels) > 9:
            raise VideoRuleViolation("video reference image limit exceeded")
        result: list[ShotPanelImage] = []
        for panel in panels:
            digest = hashlib.sha256(panel.content).hexdigest()
            key = shot_panel_object_key(
                source.project_id,
                source.storyboard_image.sha256,
                panel.shot_order,
                panel.derivation,
                digest,
            )
            if for_intent:
                result.append(self._archive(session, source, panel, key, digest))
            else:
                result.append(
                    ShotPanelImage(
                        MediaAssetRef(
                            uuid5(NAMESPACE_URL, f"h3-shot-panel:{key}"),
                            digest,
                            "image/png",
                            len(panel.content),
                        ),
                        panel.shot_order,
                        source.storyboard_image.sha256,
                        panel.derivation,
                    )
                )
        return tuple(result)

    def _archive(
        self,
        session: Session,
        source: AuthoritativeGroupInput,
        panel: DerivedPanelImage,
        key: str,
        digest: str,
    ) -> ShotPanelImage:
        SqlProjectWriteGate(session).lock_writable(source.owner_id, source.project_id)
        lock_staged_claim_identity(
            session,
            owner_id=source.owner_id,
            ownership_scope="PROJECT",
            project_id=source.project_id,
            logical_object_key=key,
        )
        existing = self._attach_existing(session, source, panel, key, digest)
        if existing is not None:
            return existing
        journal = session.scalar(
            select(StagedObjectRow)
            .where(StagedObjectRow.logical_object_key == key)
            .order_by(
                StagedObjectRow.generation.desc(),
                StagedObjectRow.created_at.desc(),
                StagedObjectRow.id.desc(),
            )
            .with_for_update()
            .limit(1)
        )
        asset_id = uuid5(NAMESPACE_URL, f"h3-shot-panel:{key}")
        actual_key = key
        if journal is not None and journal.state == "FINALIZED":
            raise VideoRuleViolation("shot_panel_media_lineage_mismatch")
        if journal is not None and journal.state == "CLEANUP_PENDING":
            actual_key = f"{key}.generation-{journal.generation + 1}-{uuid4().hex}"
            asset_id = uuid5(NAMESPACE_URL, f"h3-shot-panel:{actual_key}")
            journal = None
        now = datetime.now(UTC)
        if journal is None:
            journal = StagedObjectRow(
                object_key=actual_key,
                logical_object_key=key,
                owner_id=source.owner_id,
                project_id=source.project_id,
                ownership_scope="PROJECT",
                purpose="H3_SHOT_PANEL",
                kind="H3_SHOT_PANEL",
                state="PENDING_UPLOAD",
                sha256=digest,
                size_bytes=len(panel.content),
                media_type="image/png",
                asset_id=asset_id,
                generation=1,
                execution_token=uuid4(),
                created_at=now,
                lease_until=now + timedelta(minutes=5),
            )
            session.add(journal)
            session.flush()
        self._validate_journal(journal, source, digest, len(panel.content))
        tracker = self._upload_tracker(session)
        upload = (actual_key, source.owner_id, source.project_id, digest, len(panel.content))
        if not self._objects.exists(actual_key):
            tracker[actual_key] = upload
        stored = self._objects.put_at_key(actual_key, panel.content, "image/png", digest)
        if not stored.created:
            tracker.pop(actual_key, None)
        journal.state = "STAGED"
        journal.lease_until = None
        row = session.scalar(select(MediaAssetRow).where(MediaAssetRow.object_key == actual_key))
        if row is None:
            row = MediaAssetRow(
                id=asset_id,
                owner_id=source.owner_id,
                project_id=source.project_id,
                ownership_scope="PROJECT",
                kind="H3_SHOT_PANEL",
                object_key=actual_key,
                sha256=digest,
                media_type="image/png",
                size_bytes=len(panel.content),
                metadata_json=_lineage(source, panel),
                shared=False,
            )
            session.add(row)
            session.flush()
        self._validate_media(row, source, panel, digest)
        if session.get(ProjectAssetReferenceRow, (source.project_id, row.id)) is None:
            session.add(
                ProjectAssetReferenceRow(
                    project_id=source.project_id,
                    asset_id=row.id,
                    owner_id=source.owner_id,
                )
            )
        journal.state = "FINALIZED"
        journal.asset_id = row.id
        journal.reference_type = "h3_shot_panel"
        journal.execution_token = None
        journal.lease_until = None
        session.flush()
        return ShotPanelImage(
            MediaAssetRef(row.id, row.sha256, row.media_type, row.size_bytes),
            panel.shot_order,
            source.storyboard_image.sha256,
            panel.derivation,
        )

    def _attach_existing(
        self,
        session: Session,
        source: AuthoritativeGroupInput,
        panel: DerivedPanelImage,
        key: str,
        digest: str,
    ) -> ShotPanelImage | None:
        row = session.scalar(select(MediaAssetRow).where(MediaAssetRow.object_key == key))
        if row is None:
            return None
        self._validate_media(row, source, panel, digest)
        self._objects.get_verified(key, digest, len(panel.content))
        locked = session.get(MediaAssetRow, row.id, with_for_update=True)
        if locked is None:
            raise VideoRuleViolation("shot_panel_media_lineage_mismatch")
        self._validate_media(locked, source, panel, digest)
        if session.get(ProjectAssetReferenceRow, (source.project_id, locked.id)) is None:
            session.add(
                ProjectAssetReferenceRow(
                    project_id=source.project_id,
                    asset_id=locked.id,
                    owner_id=source.owner_id,
                )
            )
        return ShotPanelImage(
            MediaAssetRef(locked.id, locked.sha256, locked.media_type, locked.size_bytes),
            panel.shot_order,
            source.storyboard_image.sha256,
            panel.derivation,
        )

    def _upload_tracker(
        self, session: Session
    ) -> dict[str, tuple[str, UUID, UUID, str, int]]:
        existing = session.info.get(_TRACKER_KEY)
        if existing is not None:
            return cast(dict[str, tuple[str, UUID, UUID, str, int]], existing)
        tracker: dict[str, tuple[str, UUID, UUID, str, int]] = {}
        session.info[_TRACKER_KEY] = tracker
        event.listen(session, "after_commit", lambda _session: tracker.clear())
        event.listen(session, "after_rollback", lambda _session: self._cleanup_uploads(tracker))
        return tracker

    def _cleanup_uploads(self, tracker: dict[str, tuple[str, UUID, UUID, str, int]]) -> None:
        pending = tuple(tracker.values())
        tracker.clear()
        for key, owner_id, project_id, digest, size in pending:
            with self._sessions() as session:
                referenced = session.scalar(
                    select(MediaAssetRow.id).where(MediaAssetRow.object_key == key)
                )
            if referenced is not None:
                continue
            try:
                self._objects.delete_exact(key)
                continue
            except Exception:
                logger.warning("shot panel rollback delete needs reconciliation: %s", key)
            now = datetime.now(UTC)
            try:
                with self._sessions() as session, session.begin():
                    row = session.scalar(
                        select(StagedObjectRow).where(StagedObjectRow.object_key == key)
                    )
                    if row is None:
                        row = StagedObjectRow(
                            object_key=key,
                            logical_object_key=key,
                            owner_id=owner_id,
                            project_id=project_id,
                            ownership_scope="PROJECT",
                            purpose="H3_SHOT_PANEL",
                            kind="H3_SHOT_PANEL",
                            state="CLEANUP_PENDING",
                            sha256=digest,
                            size_bytes=size,
                            media_type="image/png",
                            created_at=now,
                            cleanup_after=now,
                            cleanup_attempt=0,
                        )
                        session.add(row)
                    else:
                        row.state = "CLEANUP_PENDING"
                        row.cleanup_after = now
                        row.execution_token = None
                    session.flush()
            except IntegrityError:
                logger.warning("shot panel cleanup journal already exists: %s", key)

    @staticmethod
    def _validate_journal(
        row: StagedObjectRow,
        source: AuthoritativeGroupInput,
        digest: str,
        size: int,
    ) -> None:
        if (
            row.owner_id != source.owner_id
            or row.project_id != source.project_id
            or row.sha256 != digest
            or row.size_bytes != size
            or row.kind != "H3_SHOT_PANEL"
            or row.media_type != "image/png"
        ):
            raise VideoRuleViolation("shot_panel_media_lineage_mismatch")

    @staticmethod
    def _validate_media(
        row: MediaAssetRow,
        source: AuthoritativeGroupInput,
        panel: DerivedPanelImage,
        digest: str,
    ) -> None:
        if (
            row.owner_id != source.owner_id
            or row.project_id != source.project_id
            or row.kind != "H3_SHOT_PANEL"
            or row.sha256 != digest
            or row.size_bytes != len(panel.content)
            or row.media_type != "image/png"
            or row.deletion_state != "ACTIVE"
            or row.purging
            or row.metadata_json != _lineage(source, panel)
        ):
            raise VideoRuleViolation("shot_panel_media_lineage_mismatch")


def shot_panel_object_key(
    project_id: UUID,
    source_storyboard_sha256: str,
    shot_order: int,
    derivation: str,
    content_sha256: str,
) -> str:
    version = re.sub(r"[^a-zA-Z0-9._-]+", "-", derivation).strip("-")
    if not version:
        raise VideoRuleViolation("shot_panel_derivation_unsupported")
    return (
        f"h3-shot-panels/{project_id}/{source_storyboard_sha256}/shot-{shot_order}/"
        f"{version}/{content_sha256}.png"
    )


def _lineage(
    source: AuthoritativeGroupInput, panel: DerivedPanelImage
) -> dict[str, object]:
    return {
        "source_asset_id": str(source.storyboard_image.asset_id),
        "source_storyboard_sha256": source.storyboard_image.sha256,
        "shot_order": panel.shot_order,
        "derivation": panel.derivation,
        "crop_xyxy": list(panel.crop_xyxy),
        "dimensions": list(panel.dimensions),
    }
