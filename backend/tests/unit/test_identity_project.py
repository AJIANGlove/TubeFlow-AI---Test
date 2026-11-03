from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    ChannelRow,
    ProjectRow,
    SystemInitializationRow,
)
from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.common import AspectRatio, MediaAssetRef, SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.adapters import MemoryRepository
from tubeflow_test.identity_project.domain import (
    Account,
    AccountRole,
    Channel,
    CharacterImage,
    VoiceMediaFact,
    VoiceVersion,
)
from tubeflow_test.identity_project.images import verify_character_image
from tubeflow_test.identity_project.router import (
    ChannelPatch,
    ProjectInput,
    ProjectPatch,
    create_router,
)
from tubeflow_test.identity_project.runtime import create_identity_project_runtime
from tubeflow_test.identity_project.service import IdentityProjectService


class PlainPasswords:
    def hash(self, password: str) -> str:
        return f"hashed:{password}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)


@pytest.fixture
def setup() -> tuple[IdentityProjectService, MemoryRepository]:
    repo = MemoryRepository()
    return (
        IdentityProjectService(
            repo,
            PlainPasswords(),
            setup_username="admin",
            setup_password="password1",
        ),
        repo,
    )


def assert_code(code: str, operation: object) -> None:
    with pytest.raises(DomainError, match=".") as raised:
        operation()  # type: ignore[operator]
    assert raised.value.code == code


def test_admin_initialization_is_one_time(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, _ = setup
    service.initialize_admin("admin", "password1")
    assert_code(
        "admin_already_initialized",
        lambda: service.initialize_admin("admin", "password1"),
    )


@pytest.mark.parametrize("action", ["disable", "reset"])
def test_disable_and_password_reset_invalidate_sessions(
    setup: tuple[IdentityProjectService, MemoryRepository], action: str
) -> None:
    service, _ = setup
    admin = service.initialize_admin("admin", "password1")
    user = service.create_account(admin, "user", "password2")
    token = service.login("user", "password2")
    if action == "disable":
        service.disable_account(admin, user.id)
    else:
        service.reset_password(admin, user.id, "password3")
    assert_code("invalid_session", lambda: service.authenticate(token))


def _project_fixture(
    service: IdentityProjectService, repo: MemoryRepository
) -> tuple[object, object]:
    owner = service.initialize_admin("admin", "password1")
    channel = service.create_channel(owner, "https://example.test/channel")
    image_ids = []
    for index in range(8):
        image_id = uuid4()
        repo.add_image(
            CharacterImage(
                image_id,
                owner.id,
                MediaAssetRef(uuid4(), f"{index:064x}", "image/png", 10),
                1,
                1,
            )
        )
        image_ids.append(image_id)
    voice_id = uuid4()
    fact = VoiceMediaFact(uuid4(), "d" * 64, "audio/wav", 10)
    catalog_source_sha256 = "a" * 64
    repo.voices[voice_id] = VoiceVersion(
        voice_id,
        "c" * 64,
        tuple(SupportedLocale),
        True,
        "d" * 64,
        "d" * 64,
        "d" * 64,
        reference_zip=fact,
        preview=fact,
        avatar=fact,
        catalog_source_sha256=catalog_source_sha256,
    )
    repo.catalog_imports[catalog_source_sha256] = (1, "completed")
    channel = service.update_channel(owner, channel.id, default_voice_version_id=voice_id)
    return owner, (channel, tuple(image_ids), voice_id)


def test_channel_default_character_images_set_clear_and_boundaries(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, _voice_id = value
    assert channel.default_character_image_ids == ()

    single = service.update_channel(owner, channel.id, default_character_image_ids=[image_ids[0]])
    assert single.default_character_image_ids == (image_ids[0],)

    ordered = (image_ids[2], image_ids[0], image_ids[1])
    updated = service.update_channel(owner, channel.id, default_character_image_ids=list(ordered))
    assert updated.default_character_image_ids == ordered
    assert service.get_channel(owner, channel.id).default_character_image_ids == ordered
    # 与默认音色同语义：只落在频道上，不写入既有或新建项目。
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    assert project.character_image_ids == image_ids[:1]

    cleared = service.update_channel(owner, channel.id, default_character_image_ids=[])
    assert cleared.default_character_image_ids == ()
    assert service.get_channel(owner, channel.id).default_character_image_ids == ()

    assert_code(
        "invalid_channel_default_character_images",
        lambda: service.update_channel(
            owner, channel.id, default_character_image_ids=list(image_ids[:4])
        ),
    )
    assert_code(
        "invalid_channel_default_character_images",
        lambda: service.update_channel(
            owner, channel.id, default_character_image_ids=[image_ids[0], image_ids[0]]
        ),
    )
    assert_code(
        "invalid_channel_default_character_images",
        lambda: service.update_channel(owner, channel.id, default_character_image_ids=[uuid4()]),
    )
    other = service.create_channel(owner, "https://example.test/other")
    foreign_id = uuid4()
    repo.add_image(
        CharacterImage(
            foreign_id,
            owner.id,
            MediaAssetRef(uuid4(), "9" * 64, "image/png", 10),
            1,
            1,
            channel_id=other.id,
        )
    )
    assert_code(
        "invalid_channel_default_character_images",
        lambda: service.update_channel(owner, channel.id, default_character_image_ids=[foreign_id]),
    )


@pytest.mark.parametrize("count", [1, 8])
def test_project_accepts_one_and_eight_images(
    setup: tuple[IdentityProjectService, MemoryRepository], count: int
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:count],
        visual_style="ink",
        voice_version_id=voice_id,
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    assert len(project.character_image_ids) == count


def test_project_requires_channel_default_even_with_explicit_voice(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    _configured_channel, image_ids, voice_id = value
    channel = service.create_channel(owner, "https://example.test/unconfigured")

    assert_code(
        "channel_default_voice_required",
        lambda: service.create_project(
            owner,
            channel_id=channel.id,
            script="Hello.",
            image_ids=image_ids[:1],
            visual_style="ink",
            voice_version_id=voice_id,
            locale=SupportedLocale.EN_US,
            aspect_ratio=AspectRatio.LANDSCAPE,
        ),
    )
    outsider = service.create_account(owner, "channel-outsider", "password2")
    assert_code(
        "not_found",
        lambda: service.update_channel(
            outsider,
            channel.id,
            default_voice_version_id=voice_id,
        ),
    )


def test_channel_default_is_copied_and_later_changes_only_affect_new_projects(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, first_voice_id = value
    second_voice_id = uuid4()
    first_voice = repo.voices[first_voice_id]
    repo.voices[second_voice_id] = replace(
        first_voice,
        id=second_voice_id,
        voice_id="second-fixed-voice",
        metadata_sha256="e" * 64,
        catalog_source_sha256="f" * 64,
    )
    repo.catalog_imports["f" * 64] = (1, "completed")

    draft = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    frozen_source = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    frozen = service.confirm_narration(
        owner,
        frozen_source.id,
        frozen_source.draft_revision,
        datetime(2026, 8, 30, tzinfo=UTC),
    )

    updated_channel = service.update_channel(
        owner, channel.id, default_voice_version_id=second_voice_id
    )
    later = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    explicit_override = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        voice_version_id=first_voice_id,
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )

    assert updated_channel.default_voice_version_id == second_voice_id
    assert service.get_project(owner, draft.id).voice_version_id == first_voice_id
    assert frozen.snapshot is not None
    assert frozen.snapshot.voice_version_id == first_voice_id
    assert later.voice_version_id == second_voice_id
    assert explicit_override.voice_version_id == first_voice_id


def test_channel_default_and_project_voice_must_be_published_and_locale_compatible(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, english_voice_id = value
    english_voice = replace(
        repo.voices[english_voice_id],
        supported_locales=(SupportedLocale.EN_US,),
    )
    repo.voices[english_voice_id] = english_voice
    disabled_voice_id = uuid4()
    repo.voices[disabled_voice_id] = replace(
        english_voice,
        id=disabled_voice_id,
        voice_id="unpublished-voice",
        enabled=False,
        catalog_source_sha256="b" * 64,
    )
    repo.catalog_imports["b" * 64] = (1, "completed")
    incomplete_voice_id = uuid4()
    repo.voices[incomplete_voice_id] = replace(
        english_voice,
        id=incomplete_voice_id,
        voice_id="incomplete-enabled-voice",
        enabled=True,
        catalog_source_sha256="9" * 64,
    )
    repo.catalog_imports["9" * 64] = (1, "importing")
    repo.voice_media_items[(incomplete_voice_id, "preview")] = (
        "voice-catalog/incomplete/preview.wav",
        "audio/wav",
        "9" * 64,
        1,
    )

    assert_code(
        "invalid_channel_default_voice",
        lambda: service.update_channel(
            owner, channel.id, default_voice_version_id=disabled_voice_id
        ),
    )
    assert_code(
        "invalid_channel_default_voice",
        lambda: service.update_channel(
            owner, channel.id, default_voice_version_id=incomplete_voice_id
        ),
    )
    assert incomplete_voice_id not in {voice.id for voice in service.list_voice_versions(owner)}
    assert repo.voice_media(incomplete_voice_id, "preview") is None
    assert_code(
        "invalid_voice_version",
        lambda: service.create_project(
            owner,
            channel_id=channel.id,
            script="こんにちは。",
            image_ids=image_ids[:1],
            visual_style="ink",
            locale=SupportedLocale.JA_JP,
            aspect_ratio=AspectRatio.LANDSCAPE,
        ),
    )

    japanese_voice_id = uuid4()
    repo.voices[japanese_voice_id] = replace(
        english_voice,
        id=japanese_voice_id,
        voice_id="japanese-fixed-voice",
        supported_locales=(SupportedLocale.JA_JP,),
        catalog_source_sha256="c" * 64,
    )
    repo.catalog_imports["c" * 64] = (1, "completed")
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="こんにちは。",
        image_ids=image_ids[:1],
        visual_style="ink",
        voice_version_id=japanese_voice_id,
        locale=SupportedLocale.JA_JP,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    assert project.voice_version_id == japanese_voice_id


def test_project_rejects_nine_images(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    ninth = uuid4()
    repo.add_image(
        CharacterImage(ninth, owner.id, MediaAssetRef(uuid4(), "b" * 64, "image/png", 10), 1, 1)
    )
    assert_code(
        "invalid_character_image_count",
        lambda: service.create_project(
            owner,
            channel_id=channel.id,
            script="Hello.",
            image_ids=(*image_ids, ninth),
            visual_style="ink",
            voice_version_id=voice_id,
            locale=SupportedLocale.EN_US,
            aspect_ratio=AspectRatio.LANDSCAPE,
        ),
    )


def test_locale_and_aspect_ratio_boundaries() -> None:
    assert {item.value for item in SupportedLocale} == {
        "en-US",
        "ja-JP",
        "ko-KR",
        "es-ES",
        "fr-FR",
        "pt-BR",
    }
    assert {item.value for item in AspectRatio} == {"16:9", "9:16"}
    with pytest.raises(ValueError):
        SupportedLocale("zh-CN")
    with pytest.raises(ValueError):
        AspectRatio("1:1")


def test_snapshot_is_immutable_and_captures_channel_url(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        voice_version_id=voice_id,
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
        expected_duration_seconds=30,
    )
    confirmed = service.confirm_narration(
        owner, project.id, project.draft_revision, datetime(2026, 8, 28, tzinfo=UTC)
    )
    service.update_channel(owner, channel.id, "https://example.test/new")
    assert confirmed.snapshot is not None
    assert confirmed.snapshot.channel_url == "https://example.test/channel"
    assert_code("project_snapshot_locked", lambda: confirmed.edited(script="Changed"))
    assert_code(
        "project_snapshot_locked",
        lambda: confirmed.edited(expected_duration_seconds=60),
    )
    for field, value in (
        ("owner_id", uuid4()),
        ("id", uuid4()),
        ("snapshot", None),
        ("channel_id", uuid4()),
    ):
        assert_code(
            "project_field_not_editable",
            lambda field=field, value=value: confirmed.edited(**{field: value}),
        )


def test_cross_account_project_access_is_not_found(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    outsider = service.create_account(owner, "outsider", "password2")
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        voice_version_id=voice_id,
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    assert_code("not_found", lambda: service.get_project(outsider, project.id))
    assert_code(
        "aspect_ratio_not_supported",
        lambda: service.create_project(
            owner,
            channel_id=channel.id,
            script="Portrait is not accepted.",
            image_ids=image_ids[:1],
            visual_style="ink",
            voice_version_id=voice_id,
            locale=SupportedLocale.EN_US,
            aspect_ratio=AspectRatio.PORTRAIT,
        ),
    )


@pytest.mark.parametrize("format_name", ["JPEG", "PNG", "WEBP"])
def test_image_validation_decodes_supported_formats(format_name: str) -> None:
    stream = BytesIO()
    Image.new("RGB", (2, 3)).save(stream, format=format_name)
    verified = verify_character_image(stream.getvalue())
    assert (verified.width, verified.height) == (2, 3)
    assert len(verified.sha256) == 64


def test_image_validation_rejects_fake_and_pixel_bomb() -> None:
    assert_code("invalid_image", lambda: verify_character_image(b"not an image"))
    stream = BytesIO()
    Image.new("RGB", (11, 10)).save(stream, format="PNG")
    assert_code(
        "invalid_image_dimensions",
        lambda: verify_character_image(stream.getvalue(), max_pixels=100),
    )


def test_session_is_opaque_and_repository_stores_only_digest(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    service.initialize_admin("admin", "password1")
    token = service.login("admin", "password1")
    assert token not in repo.sessions
    assert hashlib.sha256(token.encode()).hexdigest() in repo.sessions
    assert service.authenticate(token).username == "admin"
    for malformed in ("", " ", "x" * 257, token + "!"):
        assert_code("invalid_session", lambda malformed=malformed: service.authenticate(malformed))


def test_old_logout_cannot_undo_password_reset(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    admin = service.initialize_admin("admin", "password1")
    user = service.create_account(admin, "user", "password2")
    old_token = service.login("user", "password2")
    old_actor = service.authenticate(old_token)
    service.reset_password(admin, user.id, "password3")
    service.logout(old_actor, old_token)
    assert repo.account(user.id).session_generation == 2  # type: ignore[union-attr]
    assert_code("invalid_session", lambda: service.authenticate(old_token))


def test_atomic_initialization_allows_only_one_concurrent_account() -> None:
    repo = MemoryRepository()
    accounts = [
        Account(uuid4(), name, "hash", AccountRole.ADMIN) for name in ("admin-a", "admin-b")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda account: repo.initialize_admin(account, datetime.now(UTC)), accounts)
        )
    assert sorted(results) == [False, True]
    assert len(repo.accounts) == 1


def test_identity_http_setup_login_me_and_rejects_client_owner(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, _ = setup
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    assert client.get("/identity/setup/status").json() == {
        "initialized": False,
        "initialization_available": True,
    }
    initialized = client.post(
        "/identity/setup/initialize",
        json={"username": "admin", "password": "password1"},
    )
    assert initialized.status_code == 201
    login = client.post("/identity/login", json={"username": "admin", "password": "password1"})
    token = login.json()["token"]
    assert login.json()["expires_in_seconds"] == 43200
    assert login.json()["account"]["role"] == "admin"
    me = client.get("/identity/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["username"] == "admin"
    rejected = client.post(
        "/identity/channels",
        headers={"Authorization": f"Bearer {token}"},
        json={"url": "https://example.test/channel", "owner_id": str(uuid4())},
    )
    assert rejected.status_code == 422


def test_channel_default_voice_http_contract(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    _owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/identity/channels/{channel.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["default_voice_version_id"] == str(voice_id)
    patched = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_voice_version_id": str(voice_id)},
    )
    assert patched.status_code == 200
    assert patched.json()["default_voice_version_id"] == str(voice_id)

    created = client.post(
        "/identity/projects",
        headers=headers,
        json={
            "title": "Default voice project",
            "channel_id": str(channel.id),
            "script": "Hello.",
            "character_image_ids": [str(image_ids[0])],
            "visual_style": "ink",
            "locale": "en-US",
            "aspect_ratio": "16:9",
        },
    )
    assert created.status_code == 201
    assert created.json()["voice_version_id"] == str(voice_id)

    unconfigured = client.post(
        "/identity/channels",
        headers=headers,
        json={"url": "https://example.test/unconfigured-http"},
    )
    assert unconfigured.status_code == 201
    assert unconfigured.json()["default_voice_version_id"] is None
    blocked = client.post(
        "/identity/projects",
        headers=headers,
        json={
            "title": "Blocked project",
            "channel_id": unconfigured.json()["id"],
            "script": "Hello.",
            "character_image_ids": [str(image_ids[0])],
            "visual_style": "ink",
            "voice_version_id": str(voice_id),
            "locale": "en-US",
            "aspect_ratio": "16:9",
        },
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "channel_default_voice_required"


def test_channel_default_character_images_http_contract(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    _owner, value = _project_fixture(service, repo)
    channel, image_ids, _voice_id = value
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/identity/channels/{channel.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["default_character_image_ids"] == []

    ordered = [str(image_ids[2]), str(image_ids[0])]
    patched = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_character_image_ids": ordered},
    )
    assert patched.status_code == 200
    assert patched.json()["default_character_image_ids"] == ordered
    listed = client.get("/identity/channels", headers=headers)
    by_id = {item["id"]: item for item in listed.json()}
    assert by_id[str(channel.id)]["default_character_image_ids"] == ordered

    cleared = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_character_image_ids": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["default_character_image_ids"] == []

    too_many = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_character_image_ids": [str(item) for item in image_ids[:4]]},
    )
    assert too_many.status_code == 422
    duplicated = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_character_image_ids": [str(image_ids[0]), str(image_ids[0])]},
    )
    assert duplicated.status_code == 422
    assert duplicated.json()["detail"]["code"] == "invalid_channel_default_character_images"
    missing = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_character_image_ids": [str(uuid4())]},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "invalid_channel_default_character_images"
    nulled = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_character_image_ids": None},
    )
    assert nulled.status_code == 422


def test_channel_default_visual_style_set_clear_and_boundaries(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, _image_ids, _voice_id = value
    assert channel.default_visual_style == ""

    styled = service.update_channel(owner, channel.id, default_visual_style="  暖色胶片、浅景深  ")
    assert styled.default_visual_style == "暖色胶片、浅景深"
    assert service.get_channel(owner, channel.id).default_visual_style == "暖色胶片、浅景深"

    longest = "风" * 500
    assert (
        service.update_channel(owner, channel.id, default_visual_style=longest).default_visual_style
        == longest
    )
    assert_code(
        "invalid_channel_default_visual_style",
        lambda: service.update_channel(owner, channel.id, default_visual_style="风" * 501),
    )
    # 被拒绝的更新不改变已保存值。
    assert service.get_channel(owner, channel.id).default_visual_style == longest

    cleared = service.update_channel(owner, channel.id, default_visual_style="")
    assert cleared.default_visual_style == ""
    assert service.get_channel(owner, channel.id).default_visual_style == ""


def test_channel_default_visual_style_http_contract(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, _image_ids, _voice_id = value
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/identity/channels/{channel.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["default_visual_style"] == ""

    patched = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_visual_style": " 暖色胶片 "},
    )
    assert patched.status_code == 200
    assert patched.json()["default_visual_style"] == "暖色胶片"
    listed = client.get("/identity/channels", headers=headers)
    by_id = {item["id"]: item for item in listed.json()}
    assert by_id[str(channel.id)]["default_visual_style"] == "暖色胶片"

    cleared = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_visual_style": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["default_visual_style"] == ""

    too_long = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_visual_style": "风" * 501},
    )
    assert too_long.status_code == 422
    nulled = client.patch(
        f"/identity/channels/{channel.id}",
        headers=headers,
        json={"default_visual_style": None},
    )
    assert nulled.status_code == 422


def test_channel_default_visual_style_copied_to_new_project_only(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, _voice_id = value
    project_fields = {
        "channel_id": channel.id,
        "script": "Hello.",
        "image_ids": image_ids[:1],
        "locale": SupportedLocale.EN_US,
        "aspect_ratio": AspectRatio.LANDSCAPE,
    }

    service.update_channel(owner, channel.id, default_visual_style="first style")
    first = service.create_project(owner, **project_fields)
    assert first.visual_style == "first style"

    frozen = service.confirm_narration(
        owner, first.id, first.draft_revision, datetime(2026, 8, 30, tzinfo=UTC)
    )
    assert frozen.snapshot is not None
    assert frozen.snapshot.visual_style == "first style"

    service.update_channel(owner, channel.id, default_visual_style="second style")
    assert service.get_project(owner, first.id).visual_style == "first style"
    assert service.create_project(owner, **project_fields).visual_style == "second style"
    explicit = service.create_project(owner, visual_style="third style", **project_fields)
    assert explicit.visual_style == "third style"


def test_project_visual_style_falls_back_and_rejects_blank(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, _voice_id = value
    project_fields = {
        "channel_id": channel.id,
        "script": "Hello.",
        "image_ids": image_ids[:1],
        "locale": SupportedLocale.EN_US,
        "aspect_ratio": AspectRatio.LANDSCAPE,
    }

    # 频道未设默认：不传与空白都拒绝，前端 required 之外再兜一层。
    assert_code("visual_style_required", lambda: service.create_project(owner, **project_fields))
    assert_code(
        "visual_style_required",
        lambda: service.create_project(owner, visual_style="   ", **project_fields),
    )

    service.update_channel(owner, channel.id, default_visual_style="channel style")
    assert service.create_project(owner, **project_fields).visual_style == "channel style"
    assert (
        service.create_project(owner, visual_style="  ", **project_fields).visual_style
        == "channel style"
    )

    project = service.create_project(owner, visual_style="explicit", **project_fields)
    assert project.visual_style == "explicit"
    assert_code(
        "visual_style_required",
        lambda: service.edit_project(
            owner, project.id, project.draft_revision, {"visual_style": "   "}
        ),
    )
    edited = service.edit_project(
        owner, project.id, project.draft_revision, {"visual_style": "next style"}
    )
    assert edited.visual_style == "next style"


def test_channel_and_project_serialize_cover_pointer_booleans(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/identity/projects",
        headers=headers,
        json={
            "title": "Cover flag project",
            "channel_id": str(channel.id),
            "script": "Hello.",
            "character_image_ids": [str(image_ids[0])],
            "visual_style": "ink",
            "locale": "en-US",
            "aspect_ratio": "16:9",
        },
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    assert created.json()["has_current_cover"] is False
    assert created.json()["current_cover_image_generation_id"] is None
    listed = client.get("/identity/channels", headers=headers)
    assert listed.json()[0]["has_cover_style"] is False
    fetched = client.get(f"/identity/projects/{project_id}", headers=headers)
    assert fetched.json()["has_current_cover"] is False

    # 封面仓储直写指针列后的读取面：序列化布尔翻转，不改变其他字段。
    repo.channels[channel.id] = replace(
        repo.channels[channel.id], current_cover_style_profile_id=uuid4()
    )
    cover_image_generation_id = uuid4()
    repo.projects[UUID(project_id)] = replace(
        repo.projects[UUID(project_id)],
        current_cover_image_generation_id=cover_image_generation_id,
    )
    assert client.get("/identity/channels", headers=headers).json()[0][
        "has_cover_style"
    ] is True
    assert client.get(f"/identity/channels/{channel.id}", headers=headers).json()[
        "has_cover_style"
    ] is True
    detail = client.get(f"/identity/projects/{project_id}", headers=headers)
    assert detail.json()["has_current_cover"] is True
    assert detail.json()["current_cover_image_generation_id"] == str(cover_image_generation_id)
    listed_projects = client.get("/identity/projects", headers=headers)
    assert listed_projects.json()[0]["has_current_cover"] is True
    assert listed_projects.json()[0]["current_cover_image_generation_id"] == str(
        cover_image_generation_id
    )


def test_admin_cannot_disable_self(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, _ = setup
    admin = service.initialize_admin("admin", "password1")
    assert_code("admin_lockout_forbidden", lambda: service.disable_account(admin, admin.id))


def test_project_patch_null_contract() -> None:
    assert (
        ProjectPatch(expected_revision=1, expected_duration_seconds=None).expected_duration_seconds
        is None
    )
    with pytest.raises(ValidationError):
        ProjectPatch(expected_revision=1, script=None)


def test_channel_patch_and_optional_project_voice_contract() -> None:
    voice_id = uuid4()
    assert ChannelPatch(default_voice_version_id=voice_id).default_voice_version_id == voice_id
    for payload in ({}, {"default_voice_version_id": None}, {"url": None}):
        with pytest.raises(ValidationError):
            ChannelPatch.model_validate(payload)
    project = ProjectInput(
        title="Project",
        channel_id=uuid4(),
        script="Hello.",
        character_image_ids=(uuid4(),),
        visual_style="ink",
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    assert project.voice_version_id is None


def test_project_edit_uses_revision_cas(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="This is a sentence.",
        image_ids=image_ids[:1],
        visual_style="ink",
        voice_version_id=voice_id,
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    updated = service.edit_project(owner, project.id, 1, {"visual_style": "paint"})
    assert updated.draft_revision == 2
    assert_code(
        "project_snapshot_locked",
        lambda: service.edit_project(owner, project.id, 1, {"visual_style": "stale"}),
    )


def test_purging_project_rejects_edit_confirm_and_channel_write(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="Hello.",
        image_ids=image_ids[:1],
        visual_style="ink",
        voice_version_id=voice_id,
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    repo.projects[project.id] = replace(project, production_state="PURGING", production_revision=2)

    with pytest.raises(DomainError, match="permanently deleted"):
        service.edit_project(owner, project.id, project.draft_revision, {"script": "new"})
    with pytest.raises(DomainError, match="permanently deleted"):
        service.confirm_narration(owner, project.id, project.draft_revision, datetime.now(UTC))
    with pytest.raises(DomainError, match="permanently deleted"):
        service.update_channel(owner, project.channel_id, "https://example.com/other")


def _publication_fixture(
    service: IdentityProjectService, repo: MemoryRepository
) -> tuple[object, object]:
    owner, value = _project_fixture(service, repo)
    channel, image_ids, voice_id = value
    project = service.create_project(
        owner,
        channel_id=channel.id,
        script="This is a sentence.",
        image_ids=image_ids[:1],
        visual_style="ink",
        voice_version_id=voice_id,
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
    )
    return owner, project


def test_project_publication_requires_current_composition(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, project = _publication_fixture(service, repo)
    now = datetime.now(UTC)

    assert_code(
        "project_composition_required",
        lambda: service.set_project_publication(owner, project.id, True, now),
    )
    assert repo.projects[project.id].published_at is None
    assert repo.publication_events == []


def test_project_publication_is_idempotent_and_audited(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, project = _publication_fixture(service, repo)
    repo.current_compositions.add(project.id)
    first = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    second = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)

    published = service.set_project_publication(owner, project.id, True, first)
    assert published.published_at == first
    again = service.set_project_publication(owner, project.id, True, second)
    assert again.published_at == first
    assert len(repo.publication_events) == 1
    assert repo.publication_events[0]["action"] == "PUBLISHED"
    assert repo.publication_events[0]["actor_account_id"] == owner.id

    unpublished = service.set_project_publication(owner, project.id, False, second)
    assert unpublished.published_at is None
    service.set_project_publication(owner, project.id, False, second)
    assert len(repo.publication_events) == 2
    assert repo.publication_events[1]["action"] == "UNPUBLISHED"


def test_project_publication_rejects_purging_and_other_owner(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, project = _publication_fixture(service, repo)
    repo.current_compositions.add(project.id)
    other = service.create_account(owner, "other", "password2")

    assert_code(
        "not_found",
        lambda: service.set_project_publication(
            other, project.id, True, datetime.now(UTC)
        ),
    )
    repo.projects[project.id] = replace(project, production_state="PURGING")
    assert_code(
        "project_purging",
        lambda: service.set_project_publication(
            owner, project.id, True, datetime.now(UTC)
        ),
    )


def test_publication_does_not_modify_revision_snapshot_or_editing(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    owner, project = _publication_fixture(service, repo)
    repo.current_compositions.add(project.id)
    published = service.set_project_publication(
        owner, project.id, True, datetime.now(UTC)
    )

    assert published.draft_revision == project.draft_revision
    assert published.production_revision == project.production_revision
    assert published.snapshot is None
    edited = service.edit_project(
        owner, project.id, project.draft_revision, {"visual_style": "paint"}
    )
    assert edited.published_at == published.published_at
    assert repo.projects[project.id].published_at == published.published_at


def test_http_project_publication_contract(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    _owner, project = _publication_fixture(service, repo)
    repo.current_compositions.add(project.id)
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/identity/projects/{project.id}/publication"

    first = client.post(path, headers=headers)
    assert first.status_code == 200
    published_at = first.json()["published_at"]
    assert published_at is not None
    repeat = client.post(path, headers=headers)
    assert repeat.status_code == 200
    assert repeat.json()["published_at"] == published_at
    assert len(repo.publication_events) == 1

    listed = client.get("/identity/projects", headers=headers)
    assert listed.status_code == 200
    (item,) = [entry for entry in listed.json() if entry["id"] == str(project.id)]
    assert item["published_at"] == published_at
    detail = client.get(f"/identity/projects/{project.id}", headers=headers)
    assert detail.json()["published_at"] == published_at

    removed = client.delete(path, headers=headers)
    assert removed.status_code == 200
    assert removed.json()["published_at"] is None
    assert client.delete(path, headers=headers).status_code == 200
    assert [event["action"] for event in repo.publication_events] == [
        "PUBLISHED",
        "UNPUBLISHED",
    ]
    paths = app.openapi()["paths"]["/identity/projects/{project_id}/publication"]
    assert "post" in paths and "delete" in paths


def test_runtime_factory_fails_closed_without_setup_credentials() -> None:
    with pytest.raises(ValueError, match="explicitly configured"):
        create_identity_project_runtime(
            Settings(identity_setup_username=None, identity_setup_password=None)
        )


def test_runtime_rejects_public_placeholder_credentials() -> None:
    with pytest.raises(ValueError, match="explicitly configured"):
        create_identity_project_runtime(
            Settings(
                database_url="sqlite+pysqlite:///:memory:",
                identity_setup_username="replace-with-one-time-admin-username",
                identity_setup_password="replace-with-a-strong-one-time-password",
            )
        )


def test_runtime_allows_removed_setup_secret_after_initialization(tmp_path: Path) -> None:
    database_path = tmp_path / "identity.db"
    url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    database = Database(url)
    AccountRow.__table__.create(database.engine)
    SystemInitializationRow.__table__.create(database.engine)
    account_id = uuid4()
    with Session(database.engine) as session, session.begin():
        session.add(
            AccountRow(
                id=account_id,
                username="admin",
                password_hash="hash",
                role="admin",
                active=True,
                session_generation=1,
            )
        )
        session.add(
            SystemInitializationRow(
                singleton=1,
                admin_account_id=account_id,
                completed_at=datetime.now(UTC),
            )
        )
    runtime = create_identity_project_runtime(
        Settings(database_url=url, identity_setup_username=None, identity_setup_password=None)
    )
    runtime.database.engine.dispose()


def test_runtime_factory_constructs_router_and_dependency_bundle() -> None:
    runtime = create_identity_project_runtime(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            identity_setup_username="admin",
            identity_setup_password="password1",
        )
    )
    try:
        assert runtime.dependencies.current_session
        assert runtime.dependencies.current_account
        assert runtime.dependencies.admin
        paths = {route.path for route in runtime.router.routes}
        assert {
            "/identity/setup/status",
            "/identity/login",
            "/identity/projects",
        } <= paths
        assert "/identity/admin/voice-catalog/import" not in paths
    finally:
        runtime.database.engine.dispose()


def test_public_voice_preview_is_cross_account_authorized_but_reference_is_worker_only(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, repo = setup
    admin, value = _project_fixture(service, repo)
    _, _, voice_id = value
    user = service.create_account(admin, "viewer", "password2")

    class Storage:
        def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
            assert key == "voice-catalog/v1/voice/preview.wav"
            assert expected_sha256 == "p" * 64
            assert expected_size == 7
            return b"preview"

    service.image_storage = Storage()  # type: ignore[assignment]
    repo.voice_media_items[(voice_id, "preview")] = (
        "voice-catalog/v1/voice/preview.wav",
        "audio/wav",
        "p" * 64,
        7,
    )
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    for username, password in (("admin", "password1"), (user.username, "password2")):
        token = client.post(
            "/identity/login", json={"username": username, "password": password}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        path = f"/identity/voices/{voice_id}/preview"
        full = client.get(path, headers=headers)
        assert full.content == b"preview"
        assert full.headers["accept-ranges"] == "bytes"
        assert full.headers["cache-control"] == "private, no-cache"
        ranged = client.get(path, headers={**headers, "Range": "bytes=1-3"})
        assert ranged.status_code == 206
        assert ranged.content == b"rev"
        assert ranged.headers["content-range"] == "bytes 1-3/7"
        assert ranged.headers["content-length"] == "3"
        head = client.head(path, headers=headers)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == "7"
        not_modified = client.get(
            path,
            headers={**headers, "If-None-Match": full.headers["etag"]},
        )
        assert not_modified.status_code == 304
        assert not_modified.headers["cache-control"] == "private, no-cache"
        unsatisfied = client.get(path, headers={**headers, "Range": "bytes=99-100"})
        assert unsatisfied.status_code == 416
        assert unsatisfied.headers["content-range"] == "bytes */7"
        assert (
            client.get(f"/identity/voices/{voice_id}/reference", headers=headers).status_code == 404
        )


def test_channel_rename_set_strip_clear_and_boundaries(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, _ = setup
    owner = service.initialize_admin("admin", "password1")
    channel = service.create_channel(owner, "https://example.test/channel")
    assert channel.name is None

    renamed = service.update_channel(owner, channel.id, name="  主频道  ")
    assert renamed.name == "主频道"
    assert service.get_channel(owner, channel.id).name == "主频道"

    cleared = service.update_channel(owner, channel.id, name="   ")
    assert cleared.name is None
    assert service.get_channel(owner, channel.id).name is None

    for invalid in ("x" * 31, "  " + "x" * 31):
        assert_code(
            "invalid_channel_name",
            lambda invalid=invalid: service.update_channel(owner, channel.id, name=invalid),
        )
    boundary = service.update_channel(owner, channel.id, name=" " + "x" * 30 + " ")
    assert boundary.name == "x" * 30


def test_channel_rename_allows_duplicates_and_keeps_other_fields(
    setup: tuple[IdentityProjectService, MemoryRepository],
) -> None:
    service, _ = setup
    owner = service.initialize_admin("admin", "password1")
    first = service.create_channel(owner, "https://example.test/first")
    second = service.create_channel(owner, "https://example.test/second")
    service.update_channel(owner, first.id, name="Same name")
    renamed_second = service.update_channel(owner, second.id, name="Same name")
    assert renamed_second.name == "Same name"
    assert service.get_channel(owner, first.id).name == "Same name"

    updated = service.update_channel(owner, first.id, "https://example.test/renamed")
    assert updated.name == "Same name"
    assert updated.url == "https://example.test/renamed"

    outsider = service.create_account(owner, "outsider", "password2")
    assert_code(
        "not_found",
        lambda: service.update_channel(outsider, first.id, name="Hijacked"),
    )


def test_channel_patch_name_contract() -> None:
    assert ChannelPatch(name="abc").name == "abc"
    for payload in ({"name": None}, {"name": "x" * 31}):
        with pytest.raises(ValidationError):
            ChannelPatch.model_validate(payload)


def test_channel_patch_default_character_images_contract() -> None:
    image_id = uuid4()
    patch = ChannelPatch(default_character_image_ids=[image_id])
    assert patch.default_character_image_ids == [image_id]
    # 空数组是合法的“清空”值，不触发 null 拒绝。
    assert ChannelPatch(default_character_image_ids=[]).default_character_image_ids == []
    for payload in (
        {"default_character_image_ids": None},
        {"default_character_image_ids": [str(uuid4()) for _ in range(4)]},
        {"default_character_image_ids": ["not-a-uuid"]},
    ):
        with pytest.raises(ValidationError):
            ChannelPatch.model_validate(payload)


def test_sql_repository_maps_channel_name_and_project_created_at() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ChannelRow.__table__.create(engine)
    owner_id, channel_id = uuid4(), uuid4()
    default_image_ids = (uuid4(), uuid4())
    with Session(engine) as session:
        repo = SqlAlchemyIdentityProjectRepository(session)
        repo.add_channel(
            Channel(
                id=channel_id,
                owner_id=owner_id,
                url="https://example.test/channel",
                name="主频道",
                default_character_image_ids=default_image_ids,
                default_visual_style="暖色胶片",
            )
        )
        loaded = repo.channel(owner_id, channel_id)
        assert loaded is not None
        assert loaded.name == "主频道"
        assert loaded.current_cover_style_profile_id is None
        assert loaded.default_character_image_ids == default_image_ids
        assert loaded.default_visual_style == "暖色胶片"
        repo.save_channel(replace(loaded, name=None))
        assert repo.list_channels(owner_id)[0].name is None
        assert repo.list_channels(owner_id)[0].default_character_image_ids == default_image_ids
        # 封面指针列由封面仓储直写；身份仓储读取映射为领域事实。
        profile_id = uuid4()
        session.execute(
            ChannelRow.__table__.update()
            .where(ChannelRow.id == channel_id)
            .values(current_cover_style_profile_id=profile_id)
        )
        styled = repo.channel(owner_id, channel_id)
        assert styled is not None
        assert styled.current_cover_style_profile_id == profile_id
        assert repo.list_channels(owner_id)[0].current_cover_style_profile_id == profile_id
        # save_channel 不写封面指针列（频道编辑不得破坏指针）。
        repo.save_channel(replace(styled, url="https://example.test/renamed"))
        restyled = repo.channel(owner_id, channel_id)
        assert restyled is not None
        assert restyled.current_cover_style_profile_id == profile_id
        assert restyled.default_character_image_ids == default_image_ids
        # save_channel 同步写默认角色 IP 列（清空也落库）。
        repo.save_channel(replace(restyled, default_character_image_ids=()))
        cleared = repo.channel(owner_id, channel_id)
        assert cleared is not None
        assert cleared.default_character_image_ids == ()
        # 默认画面风格同样经 save_channel 落库，清空写入空串。
        repo.save_channel(replace(cleared, default_visual_style=""))
        assert repo.channel(owner_id, channel_id).default_visual_style == ""

    created_at = datetime(2026, 9, 4, 8, 30, tzinfo=UTC)
    published_at = datetime(2026, 10, 20, 1, 30, tzinfo=UTC)
    cover_image_generation_id = uuid4()
    row = ProjectRow(
        id=uuid4(),
        owner_id=uuid4(),
        channel_id=uuid4(),
        title="Listed",
        script="Hello.",
        character_image_ids=[uuid4()],
        visual_style="ink",
        voice_version_id=uuid4(),
        locale="en-US",
        aspect_ratio="16:9",
        expected_duration_seconds=None,
        created_at=created_at,
        published_at=published_at,
        current_cover_image_generation_id=cover_image_generation_id,
    )
    with Session() as session:
        repo = SqlAlchemyIdentityProjectRepository(session)
        project = repo._project(row)
        assert repo._project_row(project).published_at is None
    assert project.created_at == created_at
    assert project.published_at == published_at
    assert project.current_cover_image_generation_id == cover_image_generation_id
    engine.dispose()


def test_channel_name_and_project_list_flow_http_contract(
    setup: tuple[IdentityProjectService, MemoryRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repo = setup
    _owner, value = _project_fixture(service, repo)
    channel, image_ids, _voice_id = value
    app = FastAPI()
    app.include_router(create_router(service))
    client = TestClient(app)
    token = client.post(
        "/identity/login", json={"username": "admin", "password": "password1"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    unnamed = client.post(
        "/identity/channels", headers=headers, json={"url": "https://example.test/unnamed"}
    )
    assert unnamed.status_code == 201
    assert unnamed.json()["name"] is None

    patched = client.patch(
        f"/identity/channels/{channel.id}", headers=headers, json={"name": "  主频道  "}
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "主频道"
    channels = client.get("/identity/channels", headers=headers)
    by_id = {item["id"]: item for item in channels.json()}
    assert by_id[str(channel.id)]["name"] == "主频道"

    cleared = client.patch(
        f"/identity/channels/{channel.id}", headers=headers, json={"name": " "}
    )
    assert cleared.status_code == 200
    assert cleared.json()["name"] is None

    for payload in ({"name": "x" * 31}, {"name": None}):
        rejected = client.patch(
            f"/identity/channels/{channel.id}", headers=headers, json=payload
        )
        assert rejected.status_code == 422

    created = client.post(
        "/identity/projects",
        headers=headers,
        json={
            "title": "Listed project",
            "channel_id": str(channel.id),
            "script": "Hello.",
            "character_image_ids": [str(image_ids[0])],
            "visual_style": "ink",
            "locale": "en-US",
            "aspect_ratio": "16:9",
        },
    )
    assert created.status_code == 201
    assert created.json()["created_at"] is None
    project_id = UUID(created.json()["id"])
    created_at = datetime(2026, 9, 4, 8, 30, tzinfo=UTC)
    repo.projects[project_id] = replace(repo.projects[project_id], created_at=created_at)

    projects = client.get("/identity/projects", headers=headers)
    assert projects.status_code == 200
    (item,) = [entry for entry in projects.json() if entry["id"] == str(project_id)]
    assert item["created_at"] == created_at.isoformat()
    assert item["flow"] == {
        "stage": "DRAFT",
        "blocking_reason": None,
        "attention": False,
        "sentences": 0,
        "ready_sentences": 0,
        "groups": 0,
        "videos": 0,
        "compositions": 0,
    }

    service.confirm_narration(
        _owner, project_id, repo.projects[project_id].draft_revision, datetime.now(UTC)
    )
    counts = repo.workflow_counts(_owner.id, project_id)
    counts.update(
        sentences=1, ready_sentences=1, grouped_sentences=1,
        groups=1, directed=1, storyboards=1,
    )
    monkeypatch.setattr(repo, "workflow_counts", lambda *_args: counts)
    attempt_key = (_owner.id, project_id, "video", uuid4())
    for video_state, expected_reason in (
        (None, "videos_incomplete"),
        ("CREATED", "video_attempt_running"),
        ("RUNNING", "video_attempt_running"),
    ):
        if video_state is not None:
            repo.workflow_group_attempt_states[attempt_key] = video_state
        response = client.get("/identity/projects", headers=headers)
        assert response.status_code == 200
        (item,) = [entry for entry in response.json() if entry["id"] == str(project_id)]
        assert item["flow"]["stage"] == "VIDEO"
        assert item["flow"]["blocking_reason"] == expected_reason
        assert item["flow"]["videos"] == 0
