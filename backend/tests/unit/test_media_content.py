from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from minio.error import S3Error
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tubeflow_test.adapters.persistence.models import CompositionVersionRow, MediaAssetRow
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationSentenceRecord
from tubeflow_test.api import app as app_module
from tubeflow_test.api.media_content import (
    AcquiredMediaStream,
    MediaContent,
    MinioMediaStream,
    SqlCurrentVideoAuthority,
)
from tubeflow_test.api.media_router import AuthenticatedOwner, create_media_router
from tubeflow_test.application.composition import CompositionVersion, ManualCompositionService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.http_range import RangeNotSatisfiable, parse_single_range
from tubeflow_test.video_generation.persistence import (
    GroupCurrentVideoRecord,
    VideoAttemptRecord,
)


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.position = 0
        self.closed = False
        self.released = False

    def read(self, size: int) -> bytes:
        result = self.body[self.position : self.position + size]
        self.position += len(result)
        return result

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _Minio:
    def __init__(
        self, body: bytes, *, media_type: str = "video/mp4", on_get: object | None = None
    ) -> None:
        self.body = body
        self.media_type = media_type
        self.on_get = on_get
        self.last_response: _Response | None = None
        self.get_calls = 0

    def stat_object(self, bucket: str, key: str) -> SimpleNamespace:
        return SimpleNamespace(
            size=len(self.body),
            content_type=self.media_type,
            metadata={"x-amz-meta-sha256": hashlib.sha256(self.body).hexdigest()},
        )

    def get_object(self, bucket: str, key: str, *, offset: int, length: int) -> _Response:
        self.get_calls += 1
        if callable(self.on_get):
            self.on_get()
        self.last_response = _Response(self.body[offset : offset + length])
        return self.last_response


class _MissingMinio(_Minio):
    def stat_object(self, bucket: str, key: str) -> SimpleNamespace:
        raise S3Error(None, "NoSuchKey", "missing", key, "request", "host")  # type: ignore[arg-type]


class _SecretStatMinio(_Minio):
    def stat_object(self, bucket: str, key: str) -> SimpleNamespace:
        raise RuntimeError(f"provider receipt SECRET for {key}")


class _GetMissingMinio(_Minio):
    def get_object(self, bucket: str, key: str, *, offset: int, length: int) -> _Response:
        raise S3Error(None, "AccessDenied", "secret detail", key, "request", "host")  # type: ignore[arg-type]


class _Storage:
    def __init__(self, client: _Minio) -> None:
        self.client = client
        self.bucket = "private"


class _Noop:
    def current(self, project_id: UUID, owner_id: UUID) -> None:
        return None

    async def start(self, project_id: UUID, owner_id: UUID) -> dict[str, str]:
        return {}

    def request(self, *args: object, **kwargs: object) -> None:
        return None

    def status(self, *args: object) -> dict[str, object]:
        return {}

    def retry(self, *args: object) -> None:
        return None


class _Authority:
    def __init__(self, content: MediaContent) -> None:
        self.content = content
        self.active = True

    def resolve(self, project_id: UUID, asset_id: UUID, owner_id: UUID) -> MediaContent:
        if not self.active:
            raise DomainError("not_found", "project or media was not found")
        return self.content

    def revalidate(
        self, project_id: UUID, asset_id: UUID, owner_id: UUID, expected: MediaContent
    ) -> None:
        assert expected == self.content
        if not self.active:
            raise DomainError("not_found", "project or media was not found")


def _router_client(authority: _Authority, minio: _Minio) -> TestClient:
    owner = AuthenticatedOwner(uuid4())
    noop = _Noop()
    router = create_media_router(
        current_owner=lambda: owner,
        compositions=noop,  # type: ignore[arg-type]
        composition_commands=noop,
        deletions=noop,  # type: ignore[arg-type]
        deletion_status=noop,
        export_factory=lambda _project, _owner: iter(()),
        content_authority=authority,  # type: ignore[arg-type]
        content_stream=MinioMediaStream(_Storage(minio)),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _client(body: bytes) -> tuple[TestClient, _Minio]:
    asset = MediaContent(
        "videos/current.mp4", "video/mp4", len(body), hashlib.sha256(body).hexdigest()
    )
    minio = _Minio(body)
    return _router_client(_Authority(asset), minio), minio


def test_composition_versions_expose_stable_public_selection_contract() -> None:
    project_id = uuid4()
    current = CompositionVersion(uuid4(), "current-video", "current-srt", {}, True)
    historical = CompositionVersion(uuid4(), "old-video", "old-srt", {}, False)

    class Compositions:
        def current(self, _project_id: UUID, _owner_id: UUID) -> CompositionVersion:
            return current

        def history(
            self, _project_id: UUID, _owner_id: UUID
        ) -> tuple[CompositionVersion, ...]:
            return (current, historical)

        def select_current(
            self, _project_id: UUID, _owner_id: UUID, version_id: UUID
        ) -> CompositionVersion:
            assert version_id == historical.id
            return CompositionVersion(
                historical.id,
                historical.video_asset_id,
                historical.srt_asset_id,
                historical.manifest,
                True,
            )

    owner = AuthenticatedOwner(uuid4())
    noop = _Noop()
    content = MediaContent("unused", "video/mp4", 1, hashlib.sha256(b"x").hexdigest())
    router = create_media_router(
        current_owner=lambda: owner,
        compositions=Compositions(),  # type: ignore[arg-type]
        composition_commands=noop,
        deletions=noop,  # type: ignore[arg-type]
        deletion_status=noop,
        export_factory=lambda _project, _owner: iter(()),
        content_authority=_Authority(content),  # type: ignore[arg-type]
        content_stream=MinioMediaStream(_Storage(_Minio(b"x"))),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    current_payload = client.get(f"/projects/{project_id}/composition/current").json()
    assert current_payload["id"] == current_payload["version_id"] == str(current.id)
    assert current_payload["public_status"] == "SUCCEEDED"
    assert current_payload["allowed_actions"] == ["VIEW_MEDIA"]

    history = client.get(f"/projects/{project_id}/composition/history").json()["items"]
    assert history[0]["current"] is True
    assert history[0]["allowed_actions"] == ["VIEW_MEDIA"]
    assert history[1]["id"] == history[1]["version_id"] == str(historical.id)
    assert history[1]["allowed_actions"] == ["VIEW_MEDIA", "SELECT_CURRENT"]

    selected = client.post(
        f"/projects/{project_id}/composition/current",
        json={"version_id": str(historical.id)},
    ).json()
    assert selected["version_id"] == str(historical.id)
    assert selected["current"] is True
    assert selected["allowed_actions"] == ["VIEW_MEDIA"]


def test_current_composition_empty_state_maps_to_not_found_404(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    owner = AuthenticatedOwner(uuid4())
    noop = _Noop()
    content = MediaContent("unused", "video/mp4", 1, hashlib.sha256(b"x").hexdigest())
    router = create_media_router(
        current_owner=lambda: owner,
        compositions=ManualCompositionService(noop, noop),  # type: ignore[arg-type]
        composition_commands=noop,
        deletions=noop,  # type: ignore[arg-type]
        deletion_status=noop,
        export_factory=lambda _project, _owner: iter(()),
        content_authority=_Authority(content),  # type: ignore[arg-type]
        content_stream=MinioMediaStream(_Storage(_Minio(b"x"))),  # type: ignore[arg-type]
    )
    identity = type("IdentityRuntime", (), {"router": APIRouter(prefix="/identity")})()
    monkeypatch.setattr(
        app_module, "create_identity_project_runtime", lambda _settings: identity
    )
    narration = SimpleNamespace(
        service=noop,
        current_owner=lambda: owner,
        http_client=httpx.AsyncClient(),
    )
    media = SimpleNamespace(router=router, startup=lambda: None)
    with TestClient(
        app_module.create_app(media_runtime=media, narration_runtime=narration)  # type: ignore[arg-type]
    ) as client:
        response = client.get(f"/projects/{uuid4()}/composition/current")
    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "not_found",
        "message": "project or current composition was not found",
    }


def test_full_and_single_range_reads_stream_and_close_minio_response() -> None:
    body = b"0123456789"
    client, minio = _client(body)
    project, asset = uuid4(), uuid4()

    response = client.get(f"/projects/{project}/media/{asset}/content")
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "10"
    assert response.headers["content-type"] == "video/mp4"
    assert minio.last_response is not None
    assert minio.last_response.closed and minio.last_response.released

    response = client.get(
        f"/projects/{project}/media/{asset}/content", headers={"Range": "bytes=2-5"}
    )
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"


def test_matching_etag_returns_304_after_object_and_authority_validation() -> None:
    body = b"0123456789"
    client, minio = _client(body)
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    response = client.get(
        f"/projects/{uuid4()}/media/{uuid4()}/content",
        headers={"If-None-Match": etag},
    )
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert minio.get_calls == 0


@pytest.mark.parametrize("value", ("bytes=0-1,3-4", "items=0-1", "bytes=20-", "bytes=-0"))
def test_invalid_or_multiple_range_returns_416(value: str) -> None:
    client, _ = _client(b"0123456789")
    response = client.get(f"/projects/{uuid4()}/media/{uuid4()}/content", headers={"Range": value})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize(
    "value",
    (
        "bytes= 0-1",
        "bytes=0 -1",
        "bytes=0- 1",
        "bytes=0-1 ",
        "bytes=+0-1",
        "bytes=0-+1",
    ),
)
def test_range_rejects_whitespace_and_signed_numbers(value: str) -> None:
    client, _ = _client(b"0123456789")
    response = client.get(f"/projects/{uuid4()}/media/{uuid4()}/content", headers={"Range": value})
    assert response.status_code == 416


@pytest.mark.parametrize("value", ("bytes=٠-1", "bytes=０-1"))
def test_range_parser_rejects_non_ascii_digits(value: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range(value, 10)


def test_suffix_and_open_ended_ranges() -> None:
    assert parse_single_range("bytes=-3", 10) == parse_single_range("bytes=7-9", 10)
    assert parse_single_range("bytes=8-", 10) == parse_single_range("bytes=8-9", 10)
    with pytest.raises(RangeNotSatisfiable):
        parse_single_range("bytes=0-1,2-3", 10)


def test_delete_first_rejects_before_object_acquire() -> None:
    body = b"0123456789"
    content = MediaContent(
        "videos/current.mp4", "video/mp4", len(body), hashlib.sha256(body).hexdigest()
    )
    authority = _Authority(content)
    authority.active = False
    minio = _Minio(body)
    client = _router_client(authority, minio)
    with pytest.raises(DomainError):
        client.get(f"/projects/{uuid4()}/media/{uuid4()}/content")
    assert minio.get_calls == 0


def test_delete_after_stat_wins_before_db_revalidation_and_closes_response() -> None:
    body = b"0123456789"
    content = MediaContent(
        "videos/current.mp4", "video/mp4", len(body), hashlib.sha256(body).hexdigest()
    )
    authority = _Authority(content)
    minio = _Minio(body, on_get=lambda: setattr(authority, "active", False))
    client = _router_client(authority, minio)
    with pytest.raises(DomainError):
        client.get(f"/projects/{uuid4()}/media/{uuid4()}/content")
    assert minio.last_response is not None
    assert minio.last_response.closed and minio.last_response.released


class _FailingResponse(_Response):
    def __init__(self, *, fail_read: bool, fail_close: bool, fail_release: bool) -> None:
        super().__init__(b"x")
        self.fail_read = fail_read
        self.fail_close = fail_close
        self.fail_release = fail_release

    def read(self, size: int) -> bytes:
        if self.fail_read:
            raise RuntimeError("SECRET read provider receipt")
        return super().read(size)

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("SECRET close object key")

    def release_conn(self) -> None:
        self.released = True
        if self.fail_release:
            raise RuntimeError("SECRET release credential")


def test_close_failure_still_releases_connection() -> None:
    response = _FailingResponse(fail_read=False, fail_close=True, fail_release=False)
    stream = AcquiredMediaStream(response, 1, 1)
    with pytest.raises(DomainError) as raised:
        list(stream)
    assert raised.value.code == "media_close_failed"
    assert raised.value.__cause__ is None
    assert response.released


def test_read_close_and_release_failures_are_preserved_together() -> None:
    response = _FailingResponse(fail_read=True, fail_close=True, fail_release=True)
    stream = AcquiredMediaStream(response, 1, 1)
    with pytest.raises(BaseExceptionGroup) as raised:
        list(stream)
    assert [error.code for error in raised.value.exceptions] == [  # type: ignore[union-attr]
        "media_read_failed",
        "media_close_failed",
        "media_release_failed",
    ]
    assert "SECRET" not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def _authority_fixture() -> tuple[SqlCurrentVideoAuthority, Session, dict[str, UUID]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "production_state VARCHAR(16), channel_id CHAR(32), script TEXT, "
                "character_image_ids JSON, visual_style TEXT, voice_version_id CHAR(32), "
                "locale VARCHAR(8), aspect_ratio VARCHAR(5), expected_duration_seconds INTEGER, "
                "draft_revision INTEGER, production_revision INTEGER)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE composition_versions (id CHAR(32) PRIMARY KEY, "
                "project_id CHAR(32), owner_id CHAR(32), video_asset_id CHAR(32), "
                "srt_asset_id CHAR(32), manifest_json JSON, intent_sequence INTEGER, "
                "current BOOLEAN)"
            )
        )
    for table in (
        MediaAssetRow.__table__,
        VideoAttemptRecord.__table__,
        GroupCurrentVideoRecord.__table__,
        AuthoritativeNarrationSentenceRecord.__table__,
    ):
        table.create(engine)
    ids = {name: uuid4() for name in ("owner", "other", "project", "wrong_project", "asset", "old")}
    session = Session(engine)
    for project, owner in ((ids["project"], ids["owner"]), (ids["wrong_project"], ids["owner"])):
        session.execute(
            text(
                "INSERT INTO projects VALUES "
                "(:id,:owner,'ACTIVE',:channel,'script','[]','style',:voice,'en-US','16:9',NULL,1,1)"
            ),
            {"id": project.hex, "owner": owner.hex, "channel": uuid4().hex, "voice": uuid4().hex},
        )
    for asset_id, kind in ((ids["asset"], "COMPOSITION"), (ids["old"], "COMPOSITION")):
        session.add(
            MediaAssetRow(
                id=asset_id,
                owner_id=ids["owner"],
                project_id=ids["project"],
                kind=kind,
                object_key=f"video/{asset_id}",
                sha256="a" * 64,
                media_type="video/mp4",
                size_bytes=10,
                metadata_json={},
                shared=False,
                purging=False,
                deletion_state="ACTIVE",
            )
        )
    session.add(
        CompositionVersionRow(
            id=uuid4(),
            project_id=ids["project"],
            owner_id=ids["owner"],
            video_asset_id=ids["asset"],
            srt_asset_id=uuid4(),
            manifest_json={},
            current=True,
        )
    )
    session.commit()
    return SqlCurrentVideoAuthority(sessionmaker(engine, expire_on_commit=False)), session, ids


@pytest.mark.parametrize(
    "case",
    (
        "cross_account",
        "wrong_project",
        "old",
        "purging",
        "non_active",
        "wrong_kind",
        "asset_deleting",
    ),
)
def test_authority_rejects_unauthorized_noncurrent_or_invalid_asset(case: str) -> None:
    authority, session, ids = _authority_fixture()
    owner, project, asset = ids["owner"], ids["project"], ids["asset"]
    if case == "cross_account":
        owner = ids["other"]
    elif case == "wrong_project":
        project = ids["wrong_project"]
    elif case == "old":
        asset = ids["old"]
    elif case == "purging":
        session.execute(
            text("UPDATE projects SET production_state='PURGING' WHERE id=:id"), {"id": project.hex}
        )
    elif case == "non_active":
        session.execute(
            text("UPDATE projects SET production_state='PAUSED' WHERE id=:id"), {"id": project.hex}
        )
    elif case == "wrong_kind":
        session.get(MediaAssetRow, asset).kind = "SRT"  # type: ignore[union-attr]
    else:
        session.get(MediaAssetRow, asset).deletion_state = "DELETING"  # type: ignore[union-attr]
    session.commit()
    with pytest.raises(DomainError):
        authority.resolve(project, asset, owner)


def test_object_metadata_mismatch_is_rejected_without_leaking_object_key() -> None:
    content = MediaContent("secret/internal/key", "video/mp4", 9, "a" * 64)
    stream = MinioMediaStream(_Storage(_Minio(b"wrong")))  # type: ignore[arg-type]
    with pytest.raises(DomainError) as raised:
        stream.validate(content)
    assert "secret/internal/key" not in str(raised.value)


def test_missing_object_is_reported_without_leaking_object_key() -> None:
    content = MediaContent("secret/missing/key", "video/mp4", 9, "a" * 64)
    stream = MinioMediaStream(_Storage(_MissingMinio(b"")))  # type: ignore[arg-type]
    with pytest.raises(DomainError) as raised:
        stream.validate(content)
    assert raised.value.code == "media_unavailable"
    assert "secret/missing/key" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_non_s3_stat_error_is_sanitized_without_context_chain() -> None:
    content = MediaContent("secret/stat/key", "video/mp4", 9, "a" * 64)
    stream = MinioMediaStream(_Storage(_SecretStatMinio(b"")))  # type: ignore[arg-type]
    with pytest.raises(DomainError) as raised:
        stream.validate(content)
    assert raised.value.code == "media_unavailable"
    assert "SECRET" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_open_s3_error_is_normalized_without_leaking_object_key() -> None:
    content = MediaContent("secret/open/key", "video/mp4", 9, "a" * 64)
    stream = MinioMediaStream(_Storage(_GetMissingMinio(b"")))  # type: ignore[arg-type]
    with pytest.raises(DomainError) as raised:
        stream.acquire(content, None)
    assert raised.value.code == "media_unavailable"
    assert "secret/open/key" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_historical_h3_requires_matching_terminal_attempt_but_not_current_pointer() -> None:
    authority, session, ids = _authority_fixture()
    asset_id, attempt_id, group_id, generation_id = (uuid4() for _ in range(4))
    asset = MediaAssetRow(
        id=asset_id,
        owner_id=ids["owner"],
        project_id=ids["project"],
        kind="H3_ORIGINAL_MP4",
        object_key=f"h3/{asset_id}",
        sha256="b" * 64,
        media_type="video/mp4",
        size_bytes=20,
        metadata_json={},
        shared=False,
        purging=False,
        deletion_state="ACTIVE",
    )
    attempt = VideoAttemptRecord(
        attempt_id=attempt_id,
        owner_id=ids["owner"],
        project_id=ids["project"],
        group_id=group_id,
        generation_id=generation_id,
        idempotency_key="current-h3",
        generation_sha256="c" * 64,
        request_sha256="d" * 64,
        request_json={},
        stage="ARCHIVED",
        staged_ids=[],
        cleaned_staged_ids=[],
        archive_stage="COMPLETE",
        output_id="output",
        output_sha256=asset.sha256,
        output_size_bytes=asset.size_bytes,
        output_media_type=asset.media_type,
        archived_asset_json={
            "asset_id": str(asset.id),
            "sha256": asset.sha256,
            "media_type": asset.media_type,
            "size_bytes": asset.size_bytes,
        },
        retry_evidence=[],
        intent_sequence=3,
        slot_claimed=False,
        updated_at=datetime.now(UTC),
    )
    pointer = GroupCurrentVideoRecord(
        group_id=group_id,
        attempt_id=attempt_id,
        generation_id=generation_id,
        intent_sequence=3,
        updated_at=datetime.now(UTC),
    )
    session.add_all((asset, attempt, pointer))
    session.commit()
    assert authority.resolve(ids["project"], asset_id, ids["owner"]).object_key == asset.object_key

    pointer.intent_sequence = 2
    session.commit()
    assert authority.resolve(ids["project"], asset_id, ids["owner"]).object_key == asset.object_key

    pointer.intent_sequence = 3
    pointer.group_id = uuid4()
    session.commit()
    assert authority.resolve(ids["project"], asset_id, ids["owner"]).object_key == asset.object_key


def _narration_authority_fixture() -> tuple[SqlCurrentVideoAuthority, Session, dict[str, UUID]]:
    authority, session, ids = _authority_fixture()
    raw_id, normalized_id, unbound_id = uuid4(), uuid4(), uuid4()
    for asset_id, kind in (
        (raw_id, "narration_raw"),
        (normalized_id, "narration_normalized"),
        (unbound_id, "narration_normalized"),
    ):
        session.add(
            MediaAssetRow(
                id=asset_id,
                owner_id=ids["owner"],
                project_id=ids["project"],
                kind=kind,
                object_key=f"narration/{asset_id}",
                sha256="c" * 64,
                media_type="audio/wav",
                size_bytes=8,
                metadata_json={},
                shared=False,
                purging=False,
                deletion_state="ACTIVE",
            )
        )
    session.add(
        AuthoritativeNarrationSentenceRecord(
            id=uuid4(),
            project_id=ids["project"],
            owner_id=ids["owner"],
            snapshot_sha256="d" * 64,
            snapshot_revision=1,
            sequence=1,
            text="The sky cracks open.",
            text_sha256="e" * 64,
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
    session.commit()
    ids.update({"raw": raw_id, "normalized": normalized_id, "unbound": unbound_id})
    return authority, session, ids


def test_authority_resolves_and_revalidates_authoritative_narration_audio() -> None:
    authority, _, ids = _narration_authority_fixture()
    for key in ("raw", "normalized"):
        content = authority.resolve(ids["project"], ids[key], ids["owner"])
        assert content.object_key == f"narration/{ids[key]}"
        assert content.media_type == "audio/wav"
        authority.revalidate(ids["project"], ids[key], ids["owner"], content)


@pytest.mark.parametrize(
    "case",
    ("unbound", "cross_account", "wrong_project", "stale_revision", "wrong_media_type"),
)
def test_authority_rejects_narration_audio_without_valid_binding(case: str) -> None:
    authority, session, ids = _narration_authority_fixture()
    owner, project, asset = ids["owner"], ids["project"], ids["normalized"]
    if case == "unbound":
        asset = ids["unbound"]
    elif case == "cross_account":
        owner = ids["other"]
    elif case == "wrong_project":
        project = ids["wrong_project"]
    elif case == "stale_revision":
        session.query(AuthoritativeNarrationSentenceRecord).update(
            {"snapshot_revision": 2}, synchronize_session=False
        )
    else:
        row = session.get(MediaAssetRow, asset)
        assert row is not None
        row.media_type = "audio/mpeg"
    session.commit()
    with pytest.raises(DomainError):
        authority.resolve(project, asset, owner)


def test_http_streams_authoritative_narration_audio_with_range_and_etag() -> None:
    authority, session, ids = _narration_authority_fixture()
    body = b"wav-audio-bytes"
    digest = hashlib.sha256(body).hexdigest()
    row = session.get(MediaAssetRow, ids["normalized"])
    assert row is not None
    row.size_bytes = len(body)
    row.sha256 = digest
    session.commit()
    owner = AuthenticatedOwner(ids["owner"])
    noop = _Noop()
    minio = _Minio(body, media_type="audio/wav")
    router = create_media_router(
        current_owner=lambda: owner,
        compositions=noop,  # type: ignore[arg-type]
        composition_commands=noop,
        deletions=noop,  # type: ignore[arg-type]
        deletion_status=noop,
        export_factory=lambda _project, _owner: iter(()),
        content_authority=authority,
        content_stream=MinioMediaStream(_Storage(minio)),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    path = f"/projects/{ids['project']}/media/{ids['normalized']}/content"
    full = client.get(path)
    assert full.status_code == 200
    assert full.content == body
    assert full.headers["content-type"] == "audio/wav"
    assert full.headers["etag"] == f'"{digest}"'
    partial = client.get(path, headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206
    assert partial.content == body[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(body)}"
    cached = client.get(path, headers={"If-None-Match": f'"{digest}"'})
    assert cached.status_code == 304
    assert minio.get_calls == 2
