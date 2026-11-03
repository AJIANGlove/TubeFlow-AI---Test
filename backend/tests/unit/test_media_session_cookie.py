from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tubeflow_test.api.media_content import MediaContent, MinioMediaStream
from tubeflow_test.api.media_router import create_media_router
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.adapters import MemoryRepository
from tubeflow_test.identity_project.domain import Account, CharacterImage
from tubeflow_test.identity_project.media_session import MEDIA_SESSION_COOKIE
from tubeflow_test.identity_project.router import create_dependencies, create_router
from tubeflow_test.identity_project.service import IdentityProjectService


class _Passwords:
    def hash(self, password: str) -> str:
        return f"hashed:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


class _ImageStorage:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        assert key == "images/character.png"
        assert hashlib.sha256(self.body).hexdigest() == expected_sha256
        assert len(self.body) == expected_size
        return self.body


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.position = 0

    def read(self, size: int) -> bytes:
        chunk = self.body[self.position : self.position + size]
        self.position += len(chunk)
        return chunk

    def close(self) -> None:
        return None

    def release_conn(self) -> None:
        return None


class _Minio:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.get_calls = 0

    def stat_object(self, bucket: str, key: str) -> SimpleNamespace:
        return SimpleNamespace(
            size=len(self.body),
            content_type="video/mp4",
            metadata={"x-amz-meta-sha256": hashlib.sha256(self.body).hexdigest()},
        )

    def get_object(self, bucket: str, key: str, *, offset: int, length: int) -> _Response:
        self.get_calls += 1
        return _Response(self.body[offset : offset + length])


class _Storage:
    def __init__(self, client: _Minio) -> None:
        self.client = client
        self.bucket = "private"


class _Noop:
    def current(self, *args: object) -> None:
        return None

    async def start(self, *args: object) -> dict[str, object]:
        return {}

    async def retry(self, *args: object) -> dict[str, object]:
        return {}

    def request(self, *args: object, **kwargs: object) -> None:
        return None

    def status(self, *args: object) -> dict[str, object]:
        return {}


class _Authority:
    def __init__(self, content: MediaContent, expected_owner: UUID) -> None:
        self.content = content
        self.expected_owner = expected_owner

    def resolve(self, project_id: UUID, asset_id: UUID, owner_id: UUID) -> MediaContent:
        if owner_id != self.expected_owner:
            raise DomainError("not_found", "Resource not found")
        return self.content

    def revalidate(
        self, project_id: UUID, asset_id: UUID, owner_id: UUID, expected: MediaContent
    ) -> None:
        if owner_id != self.expected_owner or expected != self.content:
            raise DomainError("not_found", "Resource not found")


def _identity() -> tuple[IdentityProjectService, MemoryRepository, Account]:
    repository = MemoryRepository()
    service = IdentityProjectService(
        repository,
        _Passwords(),
        setup_username="admin",
        setup_password="password1",
    )
    admin = service.initialize_admin("admin", "password1")
    return service, repository, admin


def _identity_client(service: IdentityProjectService) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(service))
    return TestClient(app)


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/identity/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def test_login_issues_scoped_browser_session_cookie_and_bearer_still_works() -> None:
    service, _, _ = _identity()
    client = _identity_client(service)

    token = _login(client, "admin", "password1")
    cookie = client.cookies.get(MEDIA_SESSION_COOKIE)
    set_cookie = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).headers["set-cookie"]

    assert cookie
    assert f"{MEDIA_SESSION_COOKIE}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Secure" not in set_cookie
    assert "Max-Age" not in set_cookie
    assert client.get("/identity/me").status_code == 401
    assert (
        client.get("/identity/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )


def test_media_cookie_handles_image_head_cross_account_and_invalid_session() -> None:
    body = b"verified-image"
    service, repository, admin = _identity()
    service.image_storage = _ImageStorage(body)  # type: ignore[assignment]
    channel = service.create_channel(admin, "https://example.test/channel")
    image_id = uuid4()
    repository.add_image(
        CharacterImage(
            image_id,
            admin.id,
            MediaAssetRef(
                uuid4(), hashlib.sha256(body).hexdigest(), "image/png", len(body)
            ),
            1,
            1,
            "images/character.png",
            channel.id,
        )
    )
    user = service.create_account(admin, "other", "password2")
    client = _identity_client(service)
    _login(client, "admin", "password1")
    path = f"/identity/channels/{channel.id}/character-images/{image_id}/content"

    get_response = client.get(path)
    before_head_cookie = client.cookies.get(MEDIA_SESSION_COOKIE)
    head_response = client.head(path)
    assert get_response.status_code == 200 and get_response.content == body
    assert head_response.status_code == 200 and head_response.content == b""
    assert head_response.headers["content-length"] == str(len(body))
    assert client.cookies.get(MEDIA_SESSION_COOKIE) == before_head_cookie

    _login(client, "other", "password2")
    cross_account = client.get(path)
    assert cross_account.status_code == 404
    assert "verified-image" not in cross_account.text

    service.disable_account(admin, user.id)
    invalid = client.get(path)
    assert invalid.status_code == 401
    assert f"{MEDIA_SESSION_COOKIE}=" in invalid.headers["set-cookie"]
    assert "Max-Age=0" in invalid.headers["set-cookie"]
    assert client.cookies.get(MEDIA_SESSION_COOKIE) is None


def test_logout_revokes_session_and_clears_media_cookie() -> None:
    service, _, _ = _identity()
    client = _identity_client(service)
    token = _login(client, "admin", "password1")

    response = client.post(
        "/identity/logout", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.cookies.get(MEDIA_SESSION_COOKIE) is None
    assert (
        client.get("/identity/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_cookie_media_auth_supports_range_and_head_but_not_general_media_api() -> None:
    service, _, admin = _identity()
    dependencies = create_dependencies(service)

    body = b"0123456789"
    content = MediaContent(
        "video/current.mp4", "video/mp4", len(body), hashlib.sha256(body).hexdigest()
    )
    minio = _Minio(body)
    noop = _Noop()
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": error.code, "message": "Resource not found"}},
        )

    app.include_router(create_router(service, dependencies))
    app.include_router(
        create_media_router(
            current_owner=dependencies.current_account,  # type: ignore[arg-type]
            media_owner=dependencies.media_account,  # type: ignore[arg-type]
            compositions=noop,  # type: ignore[arg-type]
            composition_commands=noop,
            deletions=noop,  # type: ignore[arg-type]
            deletion_status=noop,  # type: ignore[arg-type]
            export_factory=lambda _project, _owner: iter(()),
            content_authority=_Authority(content, admin.id),  # type: ignore[arg-type]
            content_stream=MinioMediaStream(_Storage(minio)),  # type: ignore[arg-type]
        )
    )
    client = TestClient(app)
    token = _login(client, "admin", "password1")
    path = f"/projects/{uuid4()}/media/{uuid4()}/content"

    ranged = client.get(path, headers={"Range": "bytes=2-5"})
    calls_after_get = minio.get_calls
    head = client.head(path)
    general = client.get(f"/projects/{uuid4()}/composition/current")
    client.cookies.clear()
    missing = client.get(path)
    bearer = client.get(path, headers={"Authorization": f"Bearer {token}"})

    assert ranged.status_code == 206 and ranged.content == b"2345"
    assert ranged.headers["content-range"] == "bytes 2-5/10"
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == "10"
    assert minio.get_calls == calls_after_get + 1  # the final Bearer GET only
    assert general.status_code == 401
    assert missing.status_code == 401
    assert bearer.status_code == 200 and bearer.content == body
