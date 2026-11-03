from __future__ import annotations

import hashlib
import io
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.cover.models import CoverImageGenerationRow
from tubeflow_test.adapters.persistence.media_repository import SqlAlchemyDeletionRepository
from tubeflow_test.adapters.persistence.models import MediaAssetRow, StagedObjectRow
from tubeflow_test.api import media_runtime as media_runtime_module
from tubeflow_test.api.deletion_receipt import DeletionReceiptCodec
from tubeflow_test.api.media_router import AuthenticatedOwner
from tubeflow_test.api.media_runtime import (
    ObjectExportFactory,
    SqlDeletionStatus,
    _load_export_assets,
    create_media_runtime,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.errors import DomainError


class Authority:
    def load(self, project_id, owner_id):
        return None


class InactiveGate:
    def __init__(self, name: str) -> None:
        self.name = name

    def has_active(self, session, project_id):
        return False


def test_media_runtime_factory_is_constructible_and_runs_startup_cleanup(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        minio_endpoint="127.0.0.1:9000",
        temporal_target="127.0.0.1:7233",
    )
    StagedObjectRow.__table__.create(create_engine(settings.database_url))
    runtime = create_media_runtime(
        settings,
        lambda: AuthenticatedOwner(uuid4()),
        Authority(),
        tuple(InactiveGate(name) for name in ("narration", "storyboard", "h3", "composition")),
        private_root=tmp_path / "private",
    )
    assert isinstance(runtime.router, APIRouter)
    app = FastAPI()
    app.include_router(runtime.router)
    assert "/projects/{project_id}/composition" in app.openapi()["paths"]
    runtime.startup()


def test_manual_deletion_retry_reenqueues_persistent_outbox() -> None:
    class Repository:
        called = None

        def reenqueue(self, project_id, owner_id):
            self.called = (project_id, owner_id)

    repository = Repository()
    project, owner = uuid4(), uuid4()
    SqlDeletionStatus(
        repository,  # type: ignore[arg-type]
        DeletionReceiptCodec("test-deletion-receipt-secret-is-at-least-32-bytes"),
    ).retry(project, owner, None)
    assert repository.called == (project, owner)


def test_failed_deletion_retry_returns_job_to_deleting_state() -> None:
    job = SimpleNamespace(
        state="DELETE_FAILED",
        workflow_generation=3,
        execution_token=uuid4(),
        lease_until=object(),
        dispatch_error_code="internal-detail",
    )
    outbox = SimpleNamespace(
        workflow_generation=3,
        dispatched_at=object(),
        lease_until=object(),
        last_error_code="internal-detail",
    )

    class FakeSession:
        def __init__(self) -> None:
            self.rows = iter((job, outbox))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def begin(self):
            return self

        def scalar(self, _statement):
            return next(self.rows)

        def add(self, _row) -> None:
            raise AssertionError("existing retry outbox must be reused")

    session = FakeSession()
    repository = SqlAlchemyDeletionRepository(
        lambda: session,  # type: ignore[arg-type]
        tuple(InactiveGate(name) for name in ("narration", "storyboard", "h3", "composition")),
    )

    repository.reenqueue(uuid4(), uuid4())

    assert job.state == "DELETING"
    assert job.workflow_generation == 4
    assert job.execution_token is None
    assert job.lease_until is None
    assert job.dispatch_error_code is None
    assert outbox.workflow_generation == 4
    assert outbox.dispatched_at is None
    assert outbox.lease_until is None
    assert outbox.last_error_code is None


def _export_authority():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MediaAssetRow.__table__.create(engine)
    session = Session(engine)
    owner_id, project_id, video_id, srt_id = (uuid4() for _ in range(4))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "current_cover_image_generation_id CHAR(32))"
            )
        )
        connection.execute(
            text("INSERT INTO projects VALUES (:id, :owner, NULL)"),
            {"id": project_id.hex, "owner": owner_id.hex},
        )
    CoverImageGenerationRow.__table__.create(engine)
    video_bytes, srt_bytes = b"final-video", b"1\n00:00:00,000 --> 00:00:01,000\ntext\n"
    video = MediaAssetRow(
        id=video_id, owner_id=owner_id, project_id=project_id, kind="COMPOSITION",
        object_key="composition/final.mp4", sha256=hashlib.sha256(video_bytes).hexdigest(),
        media_type="video/mp4", size_bytes=len(video_bytes), metadata_json={}, purging=False,
        deletion_state="ACTIVE",
    )
    srt = MediaAssetRow(
        id=srt_id, owner_id=owner_id, project_id=project_id, kind="SRT",
        object_key="composition/subtitles.srt", sha256=hashlib.sha256(srt_bytes).hexdigest(),
        media_type="application/x-subrip", size_bytes=len(srt_bytes), metadata_json={},
        purging=False, deletion_state="ACTIVE",
    )
    version = SimpleNamespace(
        owner_id=owner_id, project_id=project_id, video_asset_id=video_id,
        srt_asset_id=srt_id,
        manifest_json={"project_id": str(project_id), "srt_sha256": srt.sha256},
    )
    session.add_all((video, srt))
    session.commit()
    return session, version, video, srt, video_bytes, srt_bytes, owner_id, project_id


@pytest.mark.parametrize(
    "invalid", ("cross_owner", "wrong_project", "deleting", "wrong_kind", "bad_hash")
)
def test_export_authority_rejects_polluted_assets(invalid: str) -> None:
    session, version, video, srt, _video_bytes, _srt_bytes, owner_id, project_id = (
        _export_authority()
    )
    with session:
        if invalid == "cross_owner":
            video.owner_id = uuid4()
        elif invalid == "wrong_project":
            srt.project_id = uuid4()
        elif invalid == "deleting":
            video.deletion_state = "DELETING"
        elif invalid == "wrong_kind":
            srt.kind = "COMPOSITION"
        else:
            version.manifest_json["srt_sha256"] = "0" * 64
        session.commit()
        with pytest.raises(DomainError, match="composition assets"):
            _load_export_assets(session, version, project_id, owner_id)


def test_export_authority_allows_valid_zip(tmp_path) -> None:
    session, version, video, srt, video_bytes, srt_bytes, owner_id, project_id = (
        _export_authority()
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identity):
            return None

        def scalar(self, statement):
            if "composition_versions" in str(statement):
                return version
            return session.scalar(statement)

    class Storage:
        def get(self, key: str) -> bytes:
            return {video.object_key: video_bytes, srt.object_key: srt_bytes}[key]

    payload = b"".join(
        ObjectExportFactory(lambda: FakeSession(), Storage(), tmp_path)(project_id, owner_id)  # type: ignore[arg-type]
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.read("final.mp4") == video_bytes
        assert archive.read("subtitles.srt") == srt_bytes
    session.close()


def test_export_preserves_stream_and_cleanup_failures(tmp_path, monkeypatch) -> None:
    session, version, video, srt, video_bytes, srt_bytes, owner_id, project_id = (
        _export_authority()
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identity):
            return None

        def scalar(self, statement):
            if "composition_versions" in str(statement):
                return version
            return session.scalar(statement)

    class Storage:
        def get(self, key: str) -> bytes:
            return {video.object_key: video_bytes, srt.object_key: srt_bytes}[key]

    class BrokenStream:
        def __init__(self, *_args, **_kwargs):
            pass

        def __iter__(self):
            raise OSError("stream failed")

    monkeypatch.setattr(media_runtime_module, "StreamingExport", BrokenStream)
    monkeypatch.setattr(
        media_runtime_module.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    factory = ObjectExportFactory(lambda: FakeSession(), Storage(), tmp_path)  # type: ignore[arg-type]
    with pytest.raises(BaseExceptionGroup) as raised:
        list(factory(project_id, owner_id))
    rendered = repr(raised.value.exceptions)
    assert "stream failed" in rendered
    assert "cleanup failed" in rendered
    session.close()


def _cover_export_setup(session, engine, project_id, owner_id, cover_bytes):  # type: ignore[no-untyped-def]
    generation_id = uuid4()
    digest = hashlib.sha256(cover_bytes).hexdigest()
    object_key = f"cover-images/{owner_id.hex}/{generation_id.hex}"
    session.add(
        CoverImageGenerationRow(
            id=generation_id,
            owner_id=owner_id,
            project_id=project_id,
            prompt_generation_id=uuid4(),
            version=1,
            prompt="final prompt",
            model="gpt-image-2",
            size="2560x1440",
            quality="high",
            parameters={},
            idempotency_key=f"image-{generation_id.hex}",
            provider_idempotency_key="d" * 64,
            canonical_request_sha256="c" * 64,
            status="SUCCEEDED",
            provider_receipt="receipt-1",
            expected_sha256=digest,
            downloaded_sha256=digest,
            downloaded_size=len(cover_bytes),
            qc_width=2560,
            qc_height=1440,
            qc_mime="image/png",
            object_key=object_key,
            object_sha256=digest,
        )
    )
    session.commit()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE projects SET current_cover_image_generation_id = :generation "
                "WHERE id = :id"
            ),
            {"generation": generation_id.hex, "id": project_id.hex},
        )
    return generation_id, object_key, digest


def test_export_appends_current_cover_original_bytes_when_present(tmp_path) -> None:
    session, version, video, srt, video_bytes, srt_bytes, owner_id, project_id = (
        _export_authority()
    )
    engine = session.get_bind()
    cover_bytes = b"current-cover-image-bytes"
    _generation_id, object_key, _digest = _cover_export_setup(
        session, engine, project_id, owner_id, cover_bytes
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identity):
            return None

        def scalar(self, statement):
            if "composition_versions" in str(statement):
                return version
            return session.scalar(statement)

    class Storage:
        def get(self, key: str) -> bytes:
            return {
                video.object_key: video_bytes,
                srt.object_key: srt_bytes,
                object_key: cover_bytes,
            }[key]

    payload = b"".join(
        ObjectExportFactory(lambda: FakeSession(), Storage(), tmp_path)(project_id, owner_id)  # type: ignore[arg-type]
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["final.mp4", "subtitles.srt", "cover.png"]
        assert archive.read("final.mp4") == video_bytes
        assert archive.read("subtitles.srt") == srt_bytes
        assert archive.read("cover.png") == cover_bytes
    session.close()


def test_export_cover_pointer_inconsistency_fails_closed(tmp_path) -> None:
    session, version, video, srt, video_bytes, srt_bytes, owner_id, project_id = (
        _export_authority()
    )
    engine = session.get_bind()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE projects SET current_cover_image_generation_id = :generation "
                "WHERE id = :id"
            ),
            {"generation": uuid4().hex, "id": project_id.hex},
        )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identity):
            return None

        def scalar(self, statement):
            if "composition_versions" in str(statement):
                return version
            return session.scalar(statement)

    class Storage:
        def get(self, key: str) -> bytes:
            return {video.object_key: video_bytes, srt.object_key: srt_bytes}[key]

    factory = ObjectExportFactory(lambda: FakeSession(), Storage(), tmp_path)  # type: ignore[arg-type]
    with pytest.raises(DomainError, match="cover asset"):
        b"".join(factory(project_id, owner_id))
    session.close()


def test_export_without_cover_is_byte_identical_to_two_entry_contract(
    tmp_path, monkeypatch
) -> None:
    session, version, video, srt, video_bytes, srt_bytes, owner_id, project_id = (
        _export_authority()
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identity):
            return None

        def scalar(self, statement):
            if "composition_versions" in str(statement):
                return version
            return session.scalar(statement)

    class Storage:
        def get(self, key: str) -> bytes:
            return {video.object_key: video_bytes, srt.object_key: srt_bytes}[key]

    reference = {}
    real_export = media_runtime_module.StreamingExport

    class SpyExport:
        """捕获工厂实际调用，并在迭代窗口内用旧两条目逻辑对同一源文件构建参考字节。

        参考 ZIP 与工厂读取的是同一批源文件（相同 mtime），zipfile 输出确定，
        因此逐字节比较成立。
        """

        def __init__(self, video_path, srt_path, *, private_root, chunk_size=65536, cover=None):
            reference["cover"] = cover
            self._video, self._subtitles = video_path, srt_path
            self._delegate = real_export(
                video_path, srt_path, private_root=private_root, chunk_size=chunk_size,
                cover=cover,
            )

        def __iter__(self):
            if reference["cover"] is None:
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.write(self._video, "final.mp4")
                    archive.write(self._subtitles, "subtitles.srt")
                reference["payload"] = buffer.getvalue()
            return iter(self._delegate)

        def close(self):
            return self._delegate.close()

    monkeypatch.setattr(media_runtime_module, "StreamingExport", SpyExport)
    factory = ObjectExportFactory(lambda: FakeSession(), Storage(), tmp_path)  # type: ignore[arg-type]
    payload = b"".join(factory(project_id, owner_id))

    assert reference["cover"] is None
    assert payload == reference["payload"]
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["final.mp4", "subtitles.srt"]
    session.close()
