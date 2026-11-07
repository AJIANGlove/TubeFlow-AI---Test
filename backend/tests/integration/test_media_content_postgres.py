from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from minio import Minio
from object_store_guard import refuse_application_object_store
from sqlalchemy import event, text, update

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow, SessionRow
from tubeflow_test.adapters.persistence.models import CompositionVersionRow, MediaAssetRow
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationSentenceRecord
from tubeflow_test.api.media_content import (
    AcquiredMediaStream,
    MinioMediaStream,
    SqlCurrentVideoAuthority,
)
from tubeflow_test.api.media_router import create_media_router
from tubeflow_test.api.narration_runtime import NarrationRuntime
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.adapters import OpaqueSessionTokens
from tubeflow_test.video_generation.persistence import (
    GroupCurrentVideoRecord,
    VideoAttemptRecord,
)


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False
        self.released = False

    def read(self, size: int) -> bytes:
        result, self.body = self.body[:size], self.body[size:]
        return result

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _Noop:
    def current(self, *_args: object) -> None:
        return None

    async def start(self, *_args: object) -> dict[str, str]:
        return {}

    def request(self, *_args: object, **_kwargs: object) -> None:
        return None

    def status(self, *_args: object) -> dict[str, object]:
        return {}

    def retry(self, *_args: object) -> None:
        return None


class _TrackingMinio(Minio):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.closed = 0
        self.released = 0
        self.after_acquire: Any | None = None

    def get_object(self, *args: object, **kwargs: object) -> Any:
        response = super().get_object(*args, **kwargs)
        original_close = response.close
        original_release = response.release_conn

        def close() -> None:
            self.closed += 1
            original_close()

        def release() -> None:
            self.released += 1
            original_release()

        response.close = close
        response.release_conn = release
        if self.after_acquire is not None:
            callback, self.after_acquire = self.after_acquire, None
            callback()
        return response


@pytest.mark.parametrize("winning_change", ("delete", "current_switch"))
def test_postgres_revalidation_has_one_snapshot_linearization_barrier(
    postgres_database, production_parent_chain, winning_change: str
) -> None:
    ids = production_parent_chain
    video_id, srt_id = uuid4(), uuid4()
    with postgres_database.sessions.begin() as session:
        session.add_all(
            (
                MediaAssetRow(
                    id=video_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    kind="COMPOSITION",
                    object_key=f"composition/{video_id}.mp4",
                    sha256="a" * 64,
                    media_type="video/mp4",
                    size_bytes=10,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=srt_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    kind="SRT",
                    object_key=f"composition/{srt_id}.srt",
                    sha256="b" * 64,
                    media_type="application/x-subrip",
                    size_bytes=5,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
            )
        )
        session.flush()
        session.add(
            CompositionVersionRow(
                id=uuid4(),
                project_id=ids.project_id,
                owner_id=ids.owner_id,
                video_asset_id=video_id,
                srt_asset_id=srt_id,
                manifest_json={},
                current=True,
            )
        )

    authority = SqlCurrentVideoAuthority(postgres_database.sessions)
    content = authority.resolve(ids.project_id, video_id, ids.owner_id)
    statement_started = threading.Event()
    allow_statement = threading.Event()
    select_count = 0
    armed = True

    def barrier(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal select_count, armed
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1
            if armed:
                armed = False
                statement_started.set()
                assert allow_statement.wait(10)

    event.listen(postgres_database.engine, "before_cursor_execute", barrier)
    failures: list[BaseException] = []

    def revalidate() -> None:
        try:
            authority.revalidate(ids.project_id, video_id, ids.owner_id, content)
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=revalidate)
    try:
        worker.start()
        assert statement_started.wait(10)
        with postgres_database.sessions.begin() as session:
            if winning_change == "delete":
                session.execute(
                    update(ProjectRow)
                    .where(ProjectRow.id == ids.project_id)
                    .values(production_state="PURGING")
                )
            else:
                session.execute(
                    update(CompositionVersionRow)
                    .where(CompositionVersionRow.project_id == ids.project_id)
                    .values(current=False)
                )
        allow_statement.set()
        worker.join(10)
        assert not worker.is_alive()
    finally:
        allow_statement.set()
        worker.join(10)
        event.remove(postgres_database.engine, "before_cursor_execute", barrier)

    assert select_count == 1
    if winning_change == "delete":
        assert len(failures) == 1
        assert isinstance(failures[0], DomainError)
    else:
        assert failures == []

    with postgres_database.sessions.begin() as session:
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == ids.project_id)
            .values(production_state="ACTIVE")
        )
        session.execute(
            update(CompositionVersionRow)
            .where(CompositionVersionRow.project_id == ids.project_id)
            .values(current=True)
        )
    authority.revalidate(ids.project_id, video_id, ids.owner_id, content)
    response = _Response(b"0123456789")
    acquired = AcquiredMediaStream(response, 10, 3)
    with postgres_database.sessions.begin() as session:
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == ids.project_id)
            .values(production_state="PURGING")
        )
    assert b"".join(acquired) == b"0123456789"
    assert response.closed and response.released


def test_postgres_narration_audio_requires_authoritative_sentence_binding(
    postgres_database, production_parent_chain
) -> None:
    ids = production_parent_chain
    raw_id, normalized_id, unbound_id = uuid4(), uuid4(), uuid4()
    digest = hashlib.sha256(b"wav").hexdigest()
    with postgres_database.sessions.begin() as session:
        for asset_id, kind in (
            (raw_id, "narration_raw"),
            (normalized_id, "narration_normalized"),
            (unbound_id, "narration_normalized"),
        ):
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    ownership_scope="PROJECT",
                    kind=kind,
                    object_key=f"narration/{asset_id}.wav",
                    sha256=digest,
                    media_type="audio/wav",
                    size_bytes=3,
                    metadata_json={},
                    shared=False,
                )
            )
        session.add(
            AuthoritativeNarrationSentenceRecord(
                id=uuid4(),
                project_id=ids.project_id,
                owner_id=ids.owner_id,
                snapshot_sha256="9" * 64,
                snapshot_revision=1,
                sequence=1,
                text="The sky cracks open.",
                text_sha256="a" * 64,
                raw_asset_id=raw_id,
                normalized_asset_id=normalized_id,
                raw_qc_passed=True,
                normalized_qc_passed=True,
                measured_frame_count=48_000,
                sample_rate_hz=48_000,
                measured_duration_ms=1_000,
                start_frame=0,
                end_frame=48_000,
                start_ms=0,
                end_ms=1_000,
            )
        )

    authority = SqlCurrentVideoAuthority(postgres_database.sessions)
    for asset_id in (raw_id, normalized_id):
        content = authority.resolve(ids.project_id, asset_id, ids.owner_id)
        assert content.object_key == f"narration/{asset_id}.wav"
        assert content.media_type == "audio/wav"
        authority.revalidate(ids.project_id, asset_id, ids.owner_id, content)
    with pytest.raises(DomainError):
        authority.resolve(ids.project_id, unbound_id, ids.owner_id)
    with pytest.raises(DomainError):
        authority.resolve(ids.project_id, normalized_id, uuid4())


@pytest.mark.integration
def test_real_minio_authenticated_http_serves_current_and_historical_video_assets(
    postgres_database, production_parent_chain, storyboard_parent_chain
) -> None:
    endpoint = os.environ.get("TUBEFLOW_TEST_MINIO_ENDPOINT")
    access_key = os.environ.get("TUBEFLOW_TEST_MINIO_ACCESS_KEY")
    secret_key = os.environ.get("TUBEFLOW_TEST_MINIO_SECRET_KEY")
    if not endpoint:
        pytest.skip("TUBEFLOW_TEST_MINIO_ENDPOINT is not configured")
    if not access_key or not secret_key:
        pytest.fail("real MinIO HTTP test requires explicit access and secret credentials")

    ids = production_parent_chain
    storyboard_generation_id = storyboard_parent_chain[1]
    bucket = f"media-http-{uuid4().hex}"
    client = _TrackingMinio(
        endpoint, access_key=access_key, secret_key=secret_key, secure=False
    )
    refuse_application_object_store(client)
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    composition_body = b"composition-current-mp4"
    h3_body = b"h3-current-mp4"
    composition_id, h3_id, srt_id, attempt_id = (uuid4() for _ in range(4))
    composition_key = f"composition/{composition_id}.mp4"
    h3_key = f"h3/{attempt_id}/{h3_id}.mp4"
    srt_key = f"composition/{srt_id}.srt"
    raw_token, token_hash = OpaqueSessionTokens().issue()
    other_token, other_hash = OpaqueSessionTokens().issue()
    other_owner = uuid4()
    try:
        storage.put_at_key(
            composition_key,
            composition_body,
            "video/mp4",
            hashlib.sha256(composition_body).hexdigest(),
        )
        storage.put_at_key(
            h3_key, h3_body, "video/mp4", hashlib.sha256(h3_body).hexdigest()
        )
        storage.put_at_key(
            srt_key,
            b"subtitle",
            "application/x-subrip",
            hashlib.sha256(b"subtitle").hexdigest(),
        )
        with postgres_database.sessions.begin() as session:
            session.execute(
                text("UPDATE accounts SET role='user' WHERE id=:id"),
                {"id": ids.owner_id},
            )
            session.execute(
                text(
                    "INSERT INTO accounts "
                    "(id,username,password_hash,role,active,session_generation) "
                    "VALUES (:id,:username,'test','user',true,1)"
                ),
                {"id": other_owner, "username": f"other-{other_owner.hex}"},
            )
            session.add_all(
                (
                    SessionRow(token_sha256=token_hash, account_id=ids.owner_id, generation=1),
                    SessionRow(token_sha256=other_hash, account_id=other_owner, generation=1),
                    MediaAssetRow(
                        id=composition_id,
                        owner_id=ids.owner_id,
                        project_id=ids.project_id,
                        ownership_scope="PROJECT",
                        kind="COMPOSITION",
                        object_key=composition_key,
                        sha256=hashlib.sha256(composition_body).hexdigest(),
                        media_type="video/mp4",
                        size_bytes=len(composition_body),
                        metadata_json={},
                        shared=False,
                    ),
                    MediaAssetRow(
                        id=h3_id,
                        owner_id=ids.owner_id,
                        project_id=ids.project_id,
                        ownership_scope="PROJECT",
                        kind="H3_ORIGINAL_MP4",
                        object_key=h3_key,
                        sha256=hashlib.sha256(h3_body).hexdigest(),
                        media_type="video/mp4",
                        size_bytes=len(h3_body),
                        metadata_json={},
                        shared=False,
                    ),
                    MediaAssetRow(
                        id=srt_id,
                        owner_id=ids.owner_id,
                        project_id=ids.project_id,
                        ownership_scope="PROJECT",
                        kind="SRT",
                        object_key=srt_key,
                        sha256=hashlib.sha256(b"subtitle").hexdigest(),
                        media_type="application/x-subrip",
                        size_bytes=len(b"subtitle"),
                        metadata_json={},
                        shared=False,
                    ),
                )
            )
            session.flush()
            session.add(
                CompositionVersionRow(
                    id=uuid4(),
                    project_id=ids.project_id,
                    owner_id=ids.owner_id,
                    video_asset_id=composition_id,
                    srt_asset_id=srt_id,
                    manifest_json={},
                    current=True,
                )
            )
            session.add(
                VideoAttemptRecord(
                    attempt_id=attempt_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    group_id=ids.group_id,
                    generation_id=storyboard_generation_id,
                    idempotency_key=f"media-{attempt_id.hex}",
                    generation_sha256="a" * 64,
                    request_sha256="b" * 64,
                    request_json={},
                    stage="ARCHIVED",
                    staged_ids=[],
                    cleaned_staged_ids=[],
                    archive_stage="COMPLETE",
                    archived_asset_json={
                        "asset_id": str(h3_id),
                        "sha256": hashlib.sha256(h3_body).hexdigest(),
                        "media_type": "video/mp4",
                        "size_bytes": len(h3_body),
                    },
                    retry_evidence=[],
                    slot_claimed=False,
                    intent_sequence=1,
                    updated_at=datetime.now(UTC),
                )
            )
            session.flush()
            session.add(
                GroupCurrentVideoRecord(
                    group_id=ids.group_id,
                    attempt_id=attempt_id,
                    generation_id=storyboard_generation_id,
                    intent_sequence=1,
                    updated_at=datetime.now(UTC),
                )
            )

        database = Database(postgres_database.url)
        runtime = NarrationRuntime(
            service=cast(Any, None),
            database=database,
            http_client=cast(Any, None),
            tokens=OpaqueSessionTokens(),
            storage=storage,
            dispatcher=cast(Any, None),
        )
        noop = _Noop()
        app = FastAPI()

        @app.exception_handler(DomainError)
        async def domain_error(_request: Request, error: DomainError) -> JSONResponse:
            return JSONResponse(status_code=404, content={"error": error.code})

        app.include_router(
            create_media_router(
                current_owner=runtime.current_media_owner,
                compositions=cast(Any, noop),
                composition_commands=cast(Any, noop),
                deletions=cast(Any, noop),
                deletion_status=cast(Any, noop),
                export_factory=lambda *_args: iter(()),
                content_authority=SqlCurrentVideoAuthority(postgres_database.sessions),
                content_stream=MinioMediaStream(storage, chunk_size=3),
            )
        )
        headers = {"Authorization": f"Bearer {raw_token}"}
        with TestClient(app) as http:
            full = http.get(
                f"/projects/{ids.project_id}/media/{composition_id}/content",
                headers=headers,
            )
            assert full.status_code == 200 and full.content == composition_body
            assert full.headers["content-length"] == str(len(composition_body))
            etag = f'"{hashlib.sha256(composition_body).hexdigest()}"'
            assert full.headers["etag"] == etag
            cached = http.get(
                f"/projects/{ids.project_id}/media/{composition_id}/content",
                headers={**headers, "If-None-Match": etag},
            )
            assert cached.status_code == 304 and cached.content == b""
            assert cached.headers["etag"] == etag
            partial = http.get(
                f"/projects/{ids.project_id}/media/{h3_id}/content",
                headers={**headers, "Range": "bytes=3-8"},
            )
            assert partial.status_code == 206 and partial.content == h3_body[3:9]
            assert partial.headers["content-range"] == f"bytes 3-8/{len(h3_body)}"
            invalid = http.get(
                f"/projects/{ids.project_id}/media/{h3_id}/content",
                headers={**headers, "Range": "bytes=999-1000"},
            )
            assert invalid.status_code == 416
            denied = http.get(
                f"/projects/{ids.project_id}/media/{composition_id}/content",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert denied.status_code == 404
            assert composition_key not in denied.text and h3_key not in denied.text
            with postgres_database.sessions.begin() as session:
                session.execute(
                    update(CompositionVersionRow)
                    .where(CompositionVersionRow.project_id == ids.project_id)
                    .values(current=False)
                )
            old = http.get(
                f"/projects/{ids.project_id}/media/{composition_id}/content",
                headers=headers,
            )
            assert old.status_code == 200 and old.content == composition_body
            with postgres_database.sessions.begin() as session:
                session.execute(
                    update(CompositionVersionRow)
                    .where(CompositionVersionRow.project_id == ids.project_id)
                    .values(current=True)
                )

            def freeze_after_minio_acquire() -> None:
                with postgres_database.sessions.begin() as session:
                    session.execute(
                        update(ProjectRow)
                        .where(ProjectRow.id == ids.project_id)
                        .values(production_state="PURGING")
                    )

            client.after_acquire = freeze_after_minio_acquire
            raced = http.get(
                f"/projects/{ids.project_id}/media/{h3_id}/content",
                headers=headers,
            )
            assert raced.status_code == 404
            assert composition_key not in raced.text and h3_key not in raced.text
            inactive = http.get(
                f"/projects/{ids.project_id}/media/{composition_id}/content",
                headers=headers,
            )
            assert inactive.status_code == 404
        assert client.closed >= 3 and client.released >= 3
        database.engine.dispose()
    finally:
        for key in (composition_key, h3_key, srt_key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)
