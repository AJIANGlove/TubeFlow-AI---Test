from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from minio import Minio
from minio.error import S3Error
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.identity_project.models import (
    CharacterImageRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.identity_project.write_gate import SqlProjectWriteGate
from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.staged_claims import lock_staged_claim_identity
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.video_generation.domain import VideoAssetIntegrityError
from tubeflow_test.video_generation.orchestration import VideoMediaEvidence
from tubeflow_test.video_generation.persistence import VideoAttemptRecord


class SqlObjectVideoAssetStore:
    """Exact-key PostgreSQL catalog plus private object-storage bytes adapter."""

    def __init__(self, sessions: sessionmaker[Session], objects: PrivateObjectStorage) -> None:
        self._sessions = sessions
        self._objects = objects

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        with self._sessions() as session:
            row = session.get(MediaAssetRow, asset.asset_id)
            # 角色 IP 图只持久化在 character_images（频道共享库），与
            # SqlAlchemyAssetCatalog.resolve 同一口径回退；该行无 deletion/purging 概念。
            character = (
                None
                if row is not None
                else session.scalar(
                    select(CharacterImageRow).where(CharacterImageRow.asset_id == asset.asset_id)
                )
            )
            if row is not None:
                object_key = row.object_key
                snapshot: tuple[object, ...] = (
                    row.id,
                    row.owner_id,
                    row.project_id,
                    row.kind,
                    row.object_key,
                    row.sha256,
                    row.media_type,
                    row.size_bytes,
                    row.deletion_state,
                    row.purging,
                )
                facts_match = (
                    row.sha256 == asset.sha256
                    and row.media_type == asset.media_type
                    and row.size_bytes == asset.size_bytes
                    and row.deletion_state == "ACTIVE"
                    and not row.purging
                )
            elif character is not None:
                object_key = character.object_key
                snapshot = (
                    character.asset_id,
                    character.owner_id,
                    character.object_key,
                    character.sha256,
                    character.media_type,
                    character.size_bytes,
                )
                facts_match = (
                    character.sha256 == asset.sha256
                    and character.media_type == asset.media_type
                    and character.size_bytes == asset.size_bytes
                )
            else:
                raise VideoAssetIntegrityError("video input asset is not catalogued")
        try:
            content = self._objects.get(object_key)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject"}:
                raise
            raise VideoAssetIntegrityError("video input object is missing") from exc
        if (
            not facts_match
            or hashlib.sha256(content).hexdigest() != asset.sha256
            or len(content) != asset.size_bytes
        ):
            raise VideoAssetIntegrityError("video input asset integrity mismatch")
        with self._sessions() as session, session.begin():
            if row is not None:
                current = session.scalar(
                    select(MediaAssetRow)
                    .where(MediaAssetRow.id == asset.asset_id)
                    .with_for_update()
                )
                persisted: tuple[object, ...] | None = (
                    (
                        current.id,
                        current.owner_id,
                        current.project_id,
                        current.kind,
                        current.object_key,
                        current.sha256,
                        current.media_type,
                        current.size_bytes,
                        current.deletion_state,
                        current.purging,
                    )
                    if current is not None
                    else None
                )
            else:
                current_character = session.scalar(
                    select(CharacterImageRow)
                    .where(CharacterImageRow.asset_id == asset.asset_id)
                    .with_for_update()
                )
                persisted = (
                    (
                        current_character.asset_id,
                        current_character.owner_id,
                        current_character.object_key,
                        current_character.sha256,
                        current_character.media_type,
                        current_character.size_bytes,
                    )
                    if current_character is not None
                    else None
                )
            if persisted != snapshot:
                raise VideoAssetIntegrityError("video input asset authority changed during read")
        return content

    def archive_immutable(
        self,
        *,
        attempt_id: UUID,
        generation_id: UUID,
        output_id: str,
        content: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef:
        digest = hashlib.sha256(content).hexdigest()
        if media_type != "video/mp4" or digest != expected_sha256:
            raise ValueError("original H3 archive integrity mismatch")
        with self._sessions() as session, session.begin():
            attempt = self._lock_archive_authority(
                session,
                attempt_id,
                generation_id,
                output_id,
                expected_sha256,
                len(content),
                media_type,
            )
            owner_id, project_id = attempt.owner_id, attempt.project_id
            object_key = hashlib.sha256(
                f"{attempt.attempt_id}:{attempt.output_id}:{attempt.output_sha256}".encode()
            ).hexdigest()
            logical_key = object_key
        self._assert_writable(owner_id, project_id)
        authority = (attempt_id, generation_id, output_id, digest, len(content), media_type)
        existing = self._attach_finalized_logical(
            owner_id, project_id, logical_key, digest, len(content), authority
        )
        if existing is not None:
            return existing
        try:
            actual_key, token, generation = self._claim_stage(
                owner_id, project_id, object_key, digest, len(content), media_type
            )
        except ValueError:
            existing = self._attach_finalized_logical(
                owner_id, project_id, logical_key, digest, len(content), authority
            )
            if existing is not None:
                return existing
            raise
        self._objects.put_at_key(actual_key, content, media_type, digest)
        if not self._mark_staged(actual_key, token, generation):
            existing = self._attach_finalized_logical(
                owner_id, project_id, logical_key, digest, len(content), authority
            )
            if existing is not None:
                return existing
            raise ValueError("H3 archive staged-object claim was lost")
        with self._sessions() as session, session.begin():
            self._lock_archive_authority(session, *authority)
            SqlProjectWriteGate(session).lock_writable(owner_id, project_id)
            stage = session.scalar(
                select(StagedObjectRow)
                .where(StagedObjectRow.object_key == actual_key)
                .with_for_update()
            )
            if stage is not None and stage.state == "FINALIZED" and stage.asset_id is not None:
                winner = session.get(MediaAssetRow, stage.asset_id)
                if winner is None:
                    raise ValueError("finalized H3 archive has no canonical asset")
                self._validate_winner(winner, owner_id, project_id, digest, len(content))
                if session.get(ProjectAssetReferenceRow, (project_id, winner.id)) is None:
                    session.add(
                        ProjectAssetReferenceRow(
                            project_id=project_id, asset_id=winner.id, owner_id=owner_id
                        )
                    )
                return MediaAssetRef(winner.id, winner.sha256, winner.media_type, winner.size_bytes)
            if (
                stage is None
                or stage.state != "STAGED"
                or stage.execution_token != token
                or stage.generation != generation
            ):
                raise ValueError("H3 archive staged-object claim was lost")
            row = session.scalar(
                select(MediaAssetRow).where(MediaAssetRow.object_key == actual_key)
            )
            if row is None:
                row = MediaAssetRow(
                    id=uuid4(),
                    owner_id=owner_id,
                    project_id=project_id,
                    ownership_scope="PROJECT",
                    kind="H3_ORIGINAL_MP4",
                    object_key=actual_key,
                    sha256=digest,
                    media_type=media_type,
                    size_bytes=len(content),
                    metadata_json={
                        "attempt_id": str(attempt_id),
                        "generation_id": str(generation_id),
                        "output_id": output_id,
                        "output_sha256": digest,
                    },
                    shared=False,
                )
                session.add(row)
                session.flush()
            self._validate_winner(row, owner_id, project_id, digest, len(content))
            if session.get(ProjectAssetReferenceRow, (project_id, row.id)) is None:
                session.add(
                    ProjectAssetReferenceRow(
                        project_id=project_id, asset_id=row.id, owner_id=owner_id
                    )
                )
            stage.state = "FINALIZED"
            stage.asset_id = row.id
            stage.reference_type = "h3_original_mp4"
            stage.execution_token = None
            session.flush()
            return MediaAssetRef(row.id, row.sha256, row.media_type, row.size_bytes)

    def _claim_stage(
        self, owner_id: UUID, project_id: UUID, key: str, digest: str, size: int, media_type: str
    ) -> tuple[str, UUID, int]:
        for _ in range(2):
            try:
                with self._sessions() as session, session.begin():
                    SqlProjectWriteGate(session).lock_writable(owner_id, project_id)
                    lock_staged_claim_identity(
                        session,
                        owner_id=owner_id,
                        ownership_scope="PROJECT",
                        project_id=project_id,
                        logical_object_key=key,
                    )
                    row = session.scalar(
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
                    if row is None:
                        row = StagedObjectRow(
                            object_key=key,
                            logical_object_key=key,
                            owner_id=owner_id,
                            project_id=project_id,
                            ownership_scope="PROJECT",
                            purpose="H3_ARCHIVE",
                            kind="H3_ORIGINAL_MP4",
                            state="PENDING_UPLOAD",
                            sha256=digest,
                            size_bytes=size,
                            media_type=media_type,
                            generation=1,
                            execution_token=uuid4(),
                            created_at=datetime.now(UTC),
                            lease_until=datetime.now(UTC) + timedelta(minutes=5),
                        )
                        session.add(row)
                        session.flush()
                    self._validate_stage(row, owner_id, project_id, digest, size)
                    if row.state == "CLEANUP_PENDING":
                        generation = row.generation + 1
                        row = StagedObjectRow(
                            object_key=f"{key}.generation-{generation}-{uuid4().hex}",
                            logical_object_key=key,
                            owner_id=owner_id,
                            project_id=project_id,
                            ownership_scope="PROJECT",
                            purpose="H3_ARCHIVE",
                            kind="H3_ORIGINAL_MP4",
                            state="PENDING_UPLOAD",
                            sha256=digest,
                            size_bytes=size,
                            media_type=media_type,
                            generation=generation,
                            execution_token=uuid4(),
                            created_at=datetime.now(UTC),
                            lease_until=datetime.now(UTC) + timedelta(minutes=5),
                        )
                        session.add(row)
                        session.flush()
                    if row.state == "FINALIZED" or row.execution_token is None:
                        raise ValueError("H3 archive staged-object claim was lost")
                    return row.object_key, row.execution_token, row.generation
            except IntegrityError:
                continue
        raise RuntimeError("H3 archive staged-object claim could not be reconciled")

    def _mark_staged(self, key: str, token: UUID, generation: int) -> bool:
        with self._sessions() as session, session.begin():
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StagedObjectRow)
                    .where(
                        StagedObjectRow.object_key == key,
                        StagedObjectRow.state.in_(("PENDING_UPLOAD", "STAGED")),
                        StagedObjectRow.execution_token == token,
                        StagedObjectRow.generation == generation,
                    )
                    .values(state="STAGED", lease_until=None)
                ),
            )
            return bool(result.rowcount)

    def _attach_finalized_logical(
        self,
        owner_id: UUID,
        project_id: UUID,
        logical_key: str,
        digest: str,
        size: int,
        authority: tuple[UUID, UUID, str, str, int, str],
    ) -> MediaAssetRef | None:
        with self._sessions() as session:
            journal = session.scalar(
                select(StagedObjectRow)
                .where(
                    StagedObjectRow.logical_object_key == logical_key,
                    StagedObjectRow.state == "FINALIZED",
                )
                .order_by(
                    StagedObjectRow.generation.desc(),
                    StagedObjectRow.created_at.desc(),
                    StagedObjectRow.id.desc(),
                )
                .limit(1)
            )
            if journal is None or journal.asset_id is None:
                return None
            facts = session.get(MediaAssetRow, journal.asset_id)
            if facts is None:
                raise ValueError("finalized H3 archive has no canonical asset")
            self._validate_winner(facts, owner_id, project_id, digest, size)
            self._validate_winner_attempt(facts, authority[0], authority[1], authority[2])
            facts_snapshot = (
                journal.id,
                journal.generation,
                journal.asset_id,
                facts.id,
                facts.object_key,
                facts.owner_id,
                facts.project_id,
                facts.kind,
                facts.sha256,
                facts.size_bytes,
                facts.media_type,
                facts.deletion_state,
                facts.purging,
            )
        self._objects.get_verified(facts_snapshot[4], digest, size)
        with self._sessions() as session, session.begin():
            self._lock_archive_authority(session, *authority)
            SqlProjectWriteGate(session).lock_writable(owner_id, project_id)
            journal = session.get(StagedObjectRow, facts_snapshot[0], with_for_update=True)
            row = session.get(MediaAssetRow, facts_snapshot[2], with_for_update=True)
            if journal is None or row is None:
                raise ValueError("finalized H3 archive changed during verification")
            self._validate_winner(row, owner_id, project_id, digest, size)
            self._validate_winner_attempt(row, authority[0], authority[1], authority[2])
            locked_snapshot = (
                journal.id,
                journal.generation,
                journal.asset_id,
                row.id,
                row.object_key,
                row.owner_id,
                row.project_id,
                row.kind,
                row.sha256,
                row.size_bytes,
                row.media_type,
                row.deletion_state,
                row.purging,
            )
            if locked_snapshot != facts_snapshot:
                raise ValueError("immutable H3 object winner changed during verification")
            if session.get(ProjectAssetReferenceRow, (project_id, row.id)) is None:
                session.add(
                    ProjectAssetReferenceRow(
                        project_id=project_id, asset_id=row.id, owner_id=owner_id
                    )
                )
            return MediaAssetRef(row.id, row.sha256, row.media_type, row.size_bytes)

    @staticmethod
    def _validate_winner_attempt(
        row: MediaAssetRow, attempt_id: UUID, generation_id: UUID, output_id: str
    ) -> None:
        if (
            row.metadata_json.get("attempt_id") != str(attempt_id)
            or row.metadata_json.get("generation_id") != str(generation_id)
            or row.metadata_json.get("output_id") != output_id
            or row.metadata_json.get("output_sha256") != row.sha256
        ):
            raise ValueError("immutable H3 object winner attempt mismatch")

    @staticmethod
    def _lock_archive_authority(
        session: Session,
        attempt_id: UUID,
        generation_id: UUID,
        output_id: str,
        digest: str,
        size: int,
        media_type: str,
    ) -> VideoAttemptRecord:
        attempt = session.scalar(
            select(VideoAttemptRecord)
            .where(VideoAttemptRecord.attempt_id == attempt_id)
            .with_for_update()
        )
        if (
            attempt is None
            or attempt.generation_id != generation_id
            or attempt.stage != "SUCCEEDED"
            or attempt.archive_stage != "DOWNLOADED"
            or attempt.output_id != output_id
            or attempt.output_sha256 != digest
            or attempt.output_size_bytes != size
            or attempt.output_media_type != media_type
        ):
            raise ValueError("generation attempt is not ready for archive")
        return attempt

    def _assert_writable(self, owner_id: UUID, project_id: UUID) -> None:
        with self._sessions() as session, session.begin():
            SqlProjectWriteGate(session).lock_writable(owner_id, project_id)

    @staticmethod
    def _validate_stage(
        row: StagedObjectRow, owner_id: UUID, project_id: UUID, digest: str, size: int
    ) -> None:
        if (
            row.owner_id != owner_id
            or row.project_id != project_id
            or row.sha256 != digest
            or row.size_bytes != size
            or row.kind != "H3_ORIGINAL_MP4"
        ):
            raise ValueError("immutable H3 object key collision or ownership mismatch")

    @staticmethod
    def _validate_winner(
        row: MediaAssetRow, owner_id: UUID, project_id: UUID, digest: str, size: int
    ) -> None:
        if (
            row.owner_id != owner_id
            or row.project_id != project_id
            or row.sha256 != digest
            or row.size_bytes != size
            or row.kind != "H3_ORIGINAL_MP4"
            or row.deletion_state != "ACTIVE"
            or row.purging
        ):
            raise ValueError("immutable H3 object winner mismatch")


class ImmutableVideoObjectStorage(PrivateObjectStorage):
    """Compatibility constructor backed by canonical conditional object storage."""

    def __init__(self, client: Minio, bucket: str) -> None:
        super().__init__(client, bucket)


def build_h3_archive_adapter(
    sessions: sessionmaker[Session], storage: PrivateObjectStorage
) -> SqlObjectVideoAssetStore:
    """Production binding seam for the canonical storage and staged-object ledger."""
    return SqlObjectVideoAssetStore(sessions, storage)


class FfprobeVideoProbe:
    def __init__(self, executable: str = "ffprobe", timeout_seconds: float = 30.0) -> None:
        self._executable = executable
        self._timeout = timeout_seconds

    def inspect(self, content: bytes) -> VideoMediaEvidence:
        if not content:
            raise ValueError("empty H3 output")
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as stream:
                stream.write(content)
                path = Path(stream.name)
            result = subprocess.run(
                [
                    self._executable,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            payload = json.loads(result.stdout)
            streams = payload.get("streams", [])
            videos = [item for item in streams if item.get("codec_type") == "video"]
            audios = [item for item in streams if item.get("codec_type") == "audio"]
            if len(videos) != 1:
                raise ValueError("H3 output must contain exactly one video track")
            video = videos[0]
            numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/", 1)
            fps = int(numerator) / int(denominator)
            frame_count = int(video["nb_frames"]) if video.get("nb_frames") else None
            video_duration_ms = (
                round(float(video["duration"]) * 1000)
                if video.get("duration") is not None
                else round(frame_count / fps * 1000)
                if frame_count is not None and fps > 0
                else None
            )
            if video_duration_ms is None:
                raise ValueError("ffprobe video stream duration evidence is required")
            if len(audios) != 1:
                raise ValueError("H3 output must contain exactly one native audio track")
            audio = audios[0]
            if audio.get("start_time") is None or audio.get("duration") is None:
                raise ValueError("ffprobe native audio timing evidence is required")
            audio_start_ms = round(float(audio["start_time"]) * 1000)
            audio_duration_ms = round(float(audio["duration"]) * 1000)
            return VideoMediaEvidence(
                "video/mp4",
                int(video["width"]),
                int(video["height"]),
                fps,
                round(float(payload["format"]["duration"]) * 1000),
                len(videos),
                len(audios),
                tuple(str(payload["format"].get("format_name", "")).split(",")),
                video_duration_ms,
                frame_count,
                audio_start_ms,
                audio_duration_ms,
                audio_start_ms + audio_duration_ms,
                str(audio.get("codec_name", "")) or None,
            )
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
