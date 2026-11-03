from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter
from minio import Minio
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity

from tubeflow_test.adapters.ffmpeg import FFmpegComposer, SafeSubprocessRunner
from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.cover.models import CoverImageGenerationRow
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.media_repository import (
    ActiveJobGate,
    SessionFactory,
    SqlAlchemyCompositionOutbox,
    SqlAlchemyCompositionRepository,
    SqlAlchemyDeletionRepository,
    SqlAlchemyPurgeOutbox,
    composition_key_scope,
    reconcile_staged_objects,
)
from tubeflow_test.adapters.persistence.models import (
    CompositionVersionRow,
    MediaAssetRow,
    ProjectPurgeJobRow,
)
from tubeflow_test.api.deletion_receipt import DeletionReceiptCodec
from tubeflow_test.api.media_content import MinioMediaStream, SqlCurrentVideoAuthority
from tubeflow_test.api.media_router import (
    AuthenticatedOwner,
    DeletionStatusReader,
    create_media_router,
)
from tubeflow_test.api.public_status import deletion_public_state
from tubeflow_test.application.composition import ManualCompositionService
from tubeflow_test.application.deletion import ProjectDeletionService
from tubeflow_test.application.export import StreamingExport, cleanup_orphan_exports
from tubeflow_test.application.media_authority import AuthoritativeCompositionInputPort
from tubeflow_test.config import Settings
from tubeflow_test.domain.composition import CompositionRequest
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.production_lifecycle import raise_lifecycle_errors
from tubeflow_test.workflows.composition import TemporalCompositionCommands


class ObjectAssetPathResolver:
    def __init__(
        self, sessions: SessionFactory, storage: PrivateObjectStorage, private_root: Path
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._root = private_root
        self._root.mkdir(parents=True, exist_ok=True)

    def resolve_read(self, asset_id: str) -> Path:
        try:
            parsed = UUID(asset_id)
        except ValueError as exc:
            raise DomainError(
                "invalid_asset_reference", "media asset reference is invalid"
            ) from exc
        with self._sessions() as session:
            row = session.get(MediaAssetRow, parsed)
            if row is None or row.purging:
                raise DomainError("media_not_found", "media asset was not found")
            payload = self._storage.get(row.object_key)
            if __import__("hashlib").sha256(payload).hexdigest() != row.sha256:
                raise DomainError("media_hash_mismatch", "media asset integrity check failed")
        with tempfile.NamedTemporaryFile(
            prefix="asset-", dir=self._root, delete=False
        ) as temporary:
            path = Path(temporary.name)
        path.write_bytes(payload)
        return path

    def unique_candidate(self, project_id: str) -> Path:
        return self._root / f"candidate-{project_id}-{uuid4()}.mp4"


class CompositionMediaAdapter:
    def __init__(
        self,
        sessions: SessionFactory,
        storage: PrivateObjectStorage,
        root: Path,
        timeout_seconds: float = 600,
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._timeout_seconds = timeout_seconds
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def compose(self, request: CompositionRequest) -> tuple[Path, dict[str, object]]:
        work = Path(tempfile.mkdtemp(prefix="compose-", dir=self._root))
        try:
            resolver = ObjectAssetPathResolver(self._sessions, self._storage, work)
            path, manifest = FFmpegComposer(
                SafeSubprocessRunner(
                    timeout_seconds=self._timeout_seconds,
                    cancel=TemporalCancellationFlag(),
                ), resolver
            ).compose(request)
            durable_candidate = self._root / f"candidate-{request.project_id}-{uuid4()}.mp4"
            path.replace(durable_candidate)
            return durable_candidate, asdict(manifest)
        finally:
            shutil.rmtree(work, ignore_errors=False)


class TemporalCancellationFlag:
    def is_set(self) -> bool:
        try:
            return activity.is_cancelled()
        except RuntimeError:
            return False


class SqlDeletionStatus(DeletionStatusReader):
    def __init__(
        self, repository: SqlAlchemyDeletionRepository, receipts: DeletionReceiptCodec
    ) -> None:
        self._repository = repository
        self._receipts = receipts

    def receipt(self, project_id: UUID, owner_id: UUID) -> dict[str, object]:
        record = self._repository.get_for_owner(project_id, owner_id)
        if record is None or record.status == "ACTIVE":
            raise DomainError("not_found", "Resource not found")
        return self._receipts.issue(project_id, owner_id).as_dict()

    def status(
        self, project_id: UUID, owner_id: UUID, receipt: str | None
    ) -> dict[str, object]:
        record = self._repository.get_for_owner(project_id, owner_id)
        if record is None:
            return self._deleted_status(project_id, owner_id, receipt)
        if receipt is not None:
            self._receipts.verify(receipt, project_id=project_id, owner_id=owner_id)
        latest_results = {result.object_key: result for result in record.results}
        public_errors: list[str] = []
        if any(not result.succeeded for result in latest_results.values()):
            public_errors.append("PROJECT_OBJECT_DELETE_FAILED")
        if record.dispatch_error_code is not None:
            public_errors.append("PROJECT_DELETE_DISPATCH_FAILED")
        return {
            "project_id": record.project_id,
            "status": record.status,
            "object_count": len(record.objects),
            "succeeded_count": sum(result.succeeded for result in latest_results.values()),
            "failed_count": sum(not result.succeeded for result in latest_results.values()),
            "errors": public_errors,
            **self._receipts.issue(project_id, owner_id).as_dict(),
            **deletion_public_state(record.status).as_dict(),
        }

    def retry(
        self, project_id: UUID, owner_id: UUID, receipt: str | None
    ) -> dict[str, object]:
        if receipt is not None:
            self._receipts.verify(receipt, project_id=project_id, owner_id=owner_id)
        self._repository.reenqueue(project_id, owner_id)
        return self._receipts.issue(project_id, owner_id).as_dict()

    def _deleted_status(
        self, project_id: UUID, owner_id: UUID, receipt: str | None
    ) -> dict[str, object]:
        if receipt is None:
            raise DomainError("not_found", "Resource not found")
        claims = self._receipts.verify(receipt, project_id=project_id, owner_id=owner_id)
        tombstone = self._repository.tombstone(project_id)
        if tombstone is None:
            raise DomainError("not_found", "Resource not found")
        return {
            "project_id": project_id,
            "status": tombstone.status,
            "object_count": tombstone.object_count,
            "succeeded_count": tombstone.object_count,
            "failed_count": 0,
            "errors": [],
            "deletion_receipt": receipt,
            "receipt_expires_at": claims.expires_at.isoformat().replace("+00:00", "Z"),
            **deletion_public_state(tombstone.status).as_dict(),
        }


class ObjectExportFactory:
    def __init__(
        self, sessions: SessionFactory, storage: PrivateObjectStorage, private_root: Path
    ) -> None:
        self._sessions = sessions
        self._storage = storage
        self._root = private_root
        self._root.mkdir(parents=True, exist_ok=True)

    def __call__(self, project_id: UUID, owner_id: UUID) -> Iterator[bytes]:
        source_root = Path(tempfile.mkdtemp(prefix="export-source-", dir=self._root))
        primary: BaseException | None = None
        try:
            with self._sessions() as session:
                if session.get(ProjectPurgeJobRow, project_id) is not None:
                    raise DomainError("project_purging", "project is being deleted")
                version = session.scalar(
                    select(CompositionVersionRow).where(
                        CompositionVersionRow.project_id == project_id,
                        CompositionVersionRow.owner_id == owner_id,
                        CompositionVersionRow.current.is_(True),
                    )
                )
                if version is None:
                    raise DomainError("project_not_found", "project or composition was not found")
                video, srt = _load_export_assets(session, version, project_id, owner_id)
                video_path, srt_path = source_root / "final.mp4", source_root / "subtitles.srt"
                video_bytes = self._storage.get(video.object_key)
                srt_bytes = self._storage.get(srt.object_key)
                if (
                    len(video_bytes) != video.size_bytes
                    or hashlib.sha256(video_bytes).hexdigest() != video.sha256
                ):
                    raise DomainError("media_hash_mismatch", "composition integrity failed")
                if (
                    len(srt_bytes) != srt.size_bytes
                    or hashlib.sha256(srt_bytes).hexdigest() != srt.sha256
                ):
                    raise DomainError("media_hash_mismatch", "subtitle integrity failed")
                video_path.write_bytes(video_bytes)
                srt_path.write_bytes(srt_bytes)
                cover_entry = _load_export_cover(
                    session, self._storage, project_id, owner_id, source_root
                )
            yield from StreamingExport(
                video_path, srt_path, private_root=self._root, cover=cover_entry
            )
        except BaseException as error:
            primary = error
        finally:
            cleanup_errors: list[BaseException] = []
            try:
                shutil.rmtree(source_root, ignore_errors=False)
            except BaseException as error:
                cleanup_errors.append(error)
            raise_lifecycle_errors("production export lifecycle failed", primary, cleanup_errors)


_COVER_EXPORT_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _load_export_cover(
    session: Session,
    storage: PrivateObjectStorage,
    project_id: UUID,
    owner_id: UUID,
    source_root: Path,
) -> tuple[Path, str] | None:
    """当前封面图版本（无则 None）：指针/归属/状态校验 → 字节完整性复核 → 原始字节落盘。

    指针悬垂或不一致（非 SUCCEEDED/缺对象字段/未知格式）属于权限事实损坏，如实失败
    （cover_asset_unavailable），不静默降级为无封面导出。
    """
    generation_id = session.scalar(
        select(ProjectRow.current_cover_image_generation_id).where(
            ProjectRow.id == project_id,
            ProjectRow.owner_id == owner_id,
        )
    )
    if generation_id is None:
        return None
    cover = session.scalar(
        select(CoverImageGenerationRow).where(
            CoverImageGenerationRow.id == generation_id,
            CoverImageGenerationRow.owner_id == owner_id,
            CoverImageGenerationRow.project_id == project_id,
            CoverImageGenerationRow.status == "SUCCEEDED",
        )
    )
    extension = _COVER_EXPORT_EXTENSIONS.get(cover.qc_mime or "") if cover is not None else None
    if (
        cover is None
        or cover.object_key is None
        or cover.object_sha256 is None
        or cover.downloaded_size is None
        or extension is None
    ):
        raise DomainError("cover_asset_unavailable", "current cover asset is unavailable")
    payload = storage.get(cover.object_key)
    if (
        len(payload) != cover.downloaded_size
        or hashlib.sha256(payload).hexdigest() != cover.object_sha256
    ):
        raise DomainError("media_hash_mismatch", "cover integrity failed")
    arcname = f"cover.{extension}"
    path = source_root / arcname
    path.write_bytes(payload)
    return path, arcname


def _load_export_assets(
    session: Session,
    version: CompositionVersionRow,
    project_id: UUID,
    owner_id: UUID,
) -> tuple[MediaAssetRow, MediaAssetRow]:
    video = session.scalar(
        select(MediaAssetRow).where(
            MediaAssetRow.id == version.video_asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.project_id == project_id,
            MediaAssetRow.kind == "COMPOSITION",
            MediaAssetRow.media_type == "video/mp4",
            MediaAssetRow.purging.is_(False),
            MediaAssetRow.deletion_state == "ACTIVE",
        )
    )
    srt = session.scalar(
        select(MediaAssetRow).where(
            MediaAssetRow.id == version.srt_asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.project_id == project_id,
            MediaAssetRow.kind == "SRT",
            MediaAssetRow.media_type == "application/x-subrip",
            MediaAssetRow.purging.is_(False),
            MediaAssetRow.deletion_state == "ACTIVE",
        )
    )
    manifest = version.manifest_json
    if (
        version.owner_id != owner_id
        or version.project_id != project_id
        or video is None
        or srt is None
        or manifest.get("project_id") != str(project_id)
        or manifest.get("srt_sha256") != srt.sha256
        or len(video.sha256) != 64
        or len(srt.sha256) != 64
        or video.size_bytes <= 0
        or srt.size_bytes <= 0
        or not video.object_key
        or not srt.object_key
    ):
        raise DomainError("composition_state_corrupt", "composition assets are unavailable")
    return video, srt


@dataclass(frozen=True, slots=True)
class MediaRuntime:
    router: APIRouter
    compositions: ManualCompositionService
    composition_repository: SqlAlchemyCompositionRepository
    deletions: ProjectDeletionService
    outbox: SqlAlchemyPurgeOutbox
    composition_outbox: SqlAlchemyCompositionOutbox
    startup: Callable[[], None]


def create_media_runtime(
    settings: Settings,
    current_owner: Callable[..., AuthenticatedOwner],
    authority: AuthoritativeCompositionInputPort,
    active_job_gates: tuple[ActiveJobGate, ...],
    *,
    private_root: Path,
    database: Database | None = None,
    storage: PrivateObjectStorage | None = None,
    media_owner: Callable[..., AuthenticatedOwner] | None = None,
) -> MediaRuntime:
    database = database or Database(settings.database_url)
    sessions = sessionmaker(database.engine, expire_on_commit=False)
    storage = storage or PrivateObjectStorage(
        Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        ),
        settings.minio_bucket,
    )
    composition_repository = SqlAlchemyCompositionRepository(sessions, storage, authority)
    compositions = ManualCompositionService(
        composition_repository,
        key_scope=composition_key_scope(sessions),
        composer=CompositionMediaAdapter(
                sessions,
                storage,
                private_root / "composition",
                timeout_seconds=settings.media_command_timeout_seconds,
            ),
    )
    deletion_repository = SqlAlchemyDeletionRepository(sessions, active_job_gates)
    deletion_receipts = DeletionReceiptCodec(settings.session_secret)
    deletions = ProjectDeletionService(deletion_repository, storage)
    export_factory = ObjectExportFactory(sessions, storage, private_root / "exports")
    router = create_media_router(
        current_owner=current_owner,
        compositions=compositions,
        composition_commands=TemporalCompositionCommands(composition_repository),
        deletions=deletions,
        deletion_status=SqlDeletionStatus(deletion_repository, deletion_receipts),
        export_factory=export_factory,
        content_authority=SqlCurrentVideoAuthority(sessions),
        content_stream=MinioMediaStream(storage),
        media_owner=media_owner,
    )

    def startup() -> None:
        cleanup_orphan_exports(private_root / "exports", older_than_seconds=3600)
        reconcile_staged_objects(sessions, storage, older_than_seconds=3600)

    return MediaRuntime(
        router,
        compositions,
        composition_repository,
        deletions,
        SqlAlchemyPurgeOutbox(sessions),
        SqlAlchemyCompositionOutbox(sessions),
        startup,
    )
