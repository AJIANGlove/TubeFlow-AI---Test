from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, or_, select
from sqlalchemy.sql import Select

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.media_repository import SessionFactory
from tubeflow_test.adapters.persistence.models import CompositionVersionRow, MediaAssetRow
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationSentenceRecord
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.http_range import ByteRange
from tubeflow_test.video_generation.persistence import (
    VideoAttemptRecord,
)


@dataclass(frozen=True, slots=True)
class MediaContent:
    object_key: str
    media_type: str
    size_bytes: int
    sha256: str


class SqlCurrentVideoAuthority:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def resolve(self, project_id: UUID, asset_id: UUID, owner_id: UUID) -> MediaContent:
        with self._sessions() as session:
            row = session.execute(self._statement(project_id, asset_id, owner_id)).one_or_none()
            if row is None:
                raise DomainError("not_found", "project or media was not found")
            return MediaContent(row.object_key, row.media_type, row.size_bytes, row.sha256)

    def revalidate(
        self, project_id: UUID, asset_id: UUID, owner_id: UUID, expected: MediaContent
    ) -> None:
        with self._sessions() as session:
            if (
                session.execute(
                    self._statement(project_id, asset_id, owner_id, expected)
                ).one_or_none()
                is None
            ):
                raise DomainError("not_found", "project or media was not found")

    @staticmethod
    def _statement(
        project_id: UUID,
        asset_id: UUID,
        owner_id: UUID,
        expected: MediaContent | None = None,
    ) -> Select[tuple[str, str, int, str]]:
        composition_video_binding = exists(
            select(1).where(
                CompositionVersionRow.project_id == project_id,
                CompositionVersionRow.owner_id == owner_id,
                CompositionVersionRow.video_asset_id == asset_id,
            )
        )
        composition_srt_binding = exists(
            select(1).where(
                CompositionVersionRow.project_id == project_id,
                CompositionVersionRow.owner_id == owner_id,
                CompositionVersionRow.srt_asset_id == asset_id,
            )
        )
        h3_binding = exists(
            select(1)
            .select_from(VideoAttemptRecord)
            .where(
                VideoAttemptRecord.project_id == project_id,
                VideoAttemptRecord.owner_id == owner_id,
                VideoAttemptRecord.stage == "ARCHIVED",
                VideoAttemptRecord.archive_stage == "COMPLETE",
                VideoAttemptRecord.archived_asset_json["asset_id"].as_string() == str(asset_id),
                VideoAttemptRecord.archived_asset_json["sha256"].as_string()
                == MediaAssetRow.sha256,
                VideoAttemptRecord.archived_asset_json["media_type"].as_string()
                == MediaAssetRow.media_type,
                cast(
                    VideoAttemptRecord.archived_asset_json["size_bytes"].as_string(),
                    String,
                )
                == cast(MediaAssetRow.size_bytes, String),
            )
        )
        narration_audio_binding = exists(
            select(1).where(
                AuthoritativeNarrationSentenceRecord.project_id == project_id,
                AuthoritativeNarrationSentenceRecord.owner_id == owner_id,
                AuthoritativeNarrationSentenceRecord.snapshot_revision == 1,
                or_(
                    AuthoritativeNarrationSentenceRecord.raw_asset_id == asset_id,
                    AuthoritativeNarrationSentenceRecord.normalized_asset_id == asset_id,
                ),
            )
        )
        conditions = [
            MediaAssetRow.id == asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.project_id == project_id,
            MediaAssetRow.deletion_state == "ACTIVE",
            MediaAssetRow.purging.is_(False),
            MediaAssetRow.size_bytes > 0,
            MediaAssetRow.sha256.regexp_match(r"^[0-9a-f]{64}$"),
            or_(
                and_(
                    MediaAssetRow.kind == "COMPOSITION",
                    MediaAssetRow.media_type == "video/mp4",
                    composition_video_binding,
                ),
                and_(
                    MediaAssetRow.kind == "SRT",
                    MediaAssetRow.media_type == "application/x-subrip",
                    composition_srt_binding,
                ),
                and_(
                    MediaAssetRow.kind == "H3_ORIGINAL_MP4",
                    MediaAssetRow.media_type == "video/mp4",
                    h3_binding,
                ),
                and_(
                    MediaAssetRow.kind.in_(("narration_raw", "narration_normalized")),
                    MediaAssetRow.media_type == "audio/wav",
                    narration_audio_binding,
                ),
            ),
        ]
        if expected is not None:
            conditions.extend(
                (
                    MediaAssetRow.object_key == expected.object_key,
                    MediaAssetRow.media_type == expected.media_type,
                    MediaAssetRow.size_bytes == expected.size_bytes,
                    MediaAssetRow.sha256 == expected.sha256,
                )
            )
        return (
            select(
                MediaAssetRow.object_key,
                MediaAssetRow.media_type,
                MediaAssetRow.size_bytes,
                MediaAssetRow.sha256,
            )
            .join(
                ProjectRow,
                and_(
                    ProjectRow.id == MediaAssetRow.project_id,
                    ProjectRow.owner_id == MediaAssetRow.owner_id,
                ),
            )
            .where(
                ProjectRow.id == project_id,
                ProjectRow.owner_id == owner_id,
                ProjectRow.production_state == "ACTIVE",
                *conditions,
            )
        )


class AcquiredMediaStream:
    def __init__(self, response: Any, length: int, chunk_size: int) -> None:
        self._response = response
        self._remaining = length
        self._chunk_size = chunk_size
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        errors: list[BaseException] = []
        try:
            while self._remaining:
                chunk = self._response.read(min(self._chunk_size, self._remaining))
                if not chunk:
                    raise DomainError("media_integrity_failed", "media object ended unexpectedly")
                self._remaining -= len(chunk)
                yield chunk
        except BaseException:
            errors.append(DomainError("media_read_failed", "media stream read failed"))
        finally:
            self._collect_cleanup_errors(errors)
        if errors:
            self._raise_errors(errors)

    def abort(self, _primary: BaseException) -> NoReturn:
        errors: list[BaseException] = [
            DomainError("media_authorization_changed", "media authorization changed")
        ]
        self._collect_cleanup_errors(errors)
        self._raise_errors(errors)

    def _collect_cleanup_errors(self, errors: list[BaseException]) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        except BaseException:
            errors.append(DomainError("media_close_failed", "media stream close failed"))
        finally:
            try:
                self._response.release_conn()
            except BaseException:
                errors.append(
                    DomainError("media_release_failed", "media connection release failed")
                )

    @staticmethod
    def _raise_errors(errors: list[BaseException]) -> NoReturn:
        if len(errors) == 1:
            raise errors[0]
        raise BaseExceptionGroup("media stream and cleanup failed", errors)


class MinioMediaStream:
    def __init__(self, storage: PrivateObjectStorage, chunk_size: int = 1024 * 1024) -> None:
        self._storage = storage
        self._chunk_size = chunk_size

    def validate(self, content: MediaContent) -> None:
        stat: Any | None = None
        with suppress(BaseException):
            stat = self._storage.client.stat_object(self._storage.bucket, content.object_key)
        if stat is None:
            raise DomainError("media_unavailable", "media object is unavailable")
        valid = False
        with suppress(BaseException):
            metadata = stat.metadata or {}
            stored_hash = metadata.get("x-amz-meta-sha256") or metadata.get("sha256")
            valid = (
                stat.size == content.size_bytes
                and stat.content_type == content.media_type
                and stored_hash == content.sha256
            )
        if not valid:
            raise DomainError("media_integrity_failed", "media object integrity check failed")

    def acquire(self, content: MediaContent, selected: ByteRange | None) -> AcquiredMediaStream:
        offset = selected.start if selected else 0
        length = selected.length if selected else content.size_bytes
        response: Any | None = None
        with suppress(BaseException):
            response = self._storage.client.get_object(
                self._storage.bucket, content.object_key, offset=offset, length=length
            )
        if response is None:
            raise DomainError("media_unavailable", "media object is unavailable")
        return AcquiredMediaStream(response, length, self._chunk_size)
