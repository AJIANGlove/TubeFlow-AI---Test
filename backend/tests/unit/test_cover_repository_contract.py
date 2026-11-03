"""封面仓储合同测试：SQL（SQLite）与内存实现对每个协议操作行为一致。

照 `test_storyboard_sql_repository.py` 的 SQLite 模式：五张 cover 表按模型
元数据建表，channels/projects 用裸 SQL 桩表（只含仓储触及的指针/状态列）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.persistence.cover.models import (
    CoverCharacterCardRow,
    CoverImageGenerationRow,
    CoverOutboxRow,
    CoverPromptGenerationRow,
    CoverRefImageRow,
    CoverStyleDraftRow,
    CoverStyleProfileRow,
)
from tubeflow_test.adapters.persistence.cover.repository import SqlAlchemyCoverRepository
from tubeflow_test.adapters.persistence.identity_project.models import CharacterImageRow
from tubeflow_test.application.cover_ports import (
    COVER_IMAGE_SOURCE_UPLOAD,
    COVER_SCOPE_CHANNEL,
    COVER_SCOPE_PROJECT,
    COVER_STATUS_FAILED,
    COVER_STATUS_INTENT_PERSISTED,
    COVER_STATUS_SUCCEEDED,
    CoverCharacterCard,
    CoverCharacterImageRef,
    CoverImageGeneration,
    CoverPromptGeneration,
    CoverRefImage,
    CoverRepository,
    CoverStyleDraft,
    CoverStyleProfile,
)
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


@dataclass(slots=True)
class _Backend:
    repo: CoverRepository
    seed_channel: Callable[[UUID, UUID], None]
    seed_project: Callable[[UUID, UUID], None]
    purge_project: Callable[[UUID, UUID], None]
    seed_character_image: Callable[[UUID, UUID, UUID], None]
    uow_factory: Callable[[], AbstractContextManager[Session]] | None = None


def _sql_backend(path: Path) -> _Backend:
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )
    for table in (
        CoverRefImageRow.__table__,
        CoverStyleDraftRow.__table__,
        CoverStyleProfileRow.__table__,
        CoverPromptGenerationRow.__table__,
        CoverImageGenerationRow.__table__,
        CoverOutboxRow.__table__,
        CoverCharacterCardRow.__table__,
        CharacterImageRow.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE channels (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "current_cover_style_profile_id CHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "production_state VARCHAR(16), current_cover_prompt_generation_id CHAR(32), "
                "current_cover_image_generation_id CHAR(32))"
            )
        )
    sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def uow() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def seed_channel(owner_id: UUID, channel_id: UUID) -> None:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO channels (id, owner_id) VALUES (:id, :owner)"),
                {"id": channel_id.hex, "owner": owner_id.hex},
            )

    def seed_project(owner_id: UUID, project_id: UUID) -> None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (id, owner_id, production_state) "
                    "VALUES (:id, :owner, 'ACTIVE')"
                ),
                {"id": project_id.hex, "owner": owner_id.hex},
            )

    def purge_project(owner_id: UUID, project_id: UUID) -> None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE projects SET production_state = 'PURGING' "
                    "WHERE id = :id AND owner_id = :owner"
                ),
                {"id": project_id.hex, "owner": owner_id.hex},
            )

    def seed_character_image(owner_id: UUID, channel_id: UUID, image_id: UUID) -> None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO character_images (id, owner_id, channel_id, asset_id, "
                    "object_key, sha256, media_type, size_bytes, width, height) "
                    "VALUES (:id, :owner, :channel, :asset, :key, :sha, 'image/png', "
                    "128, 640, 360)"
                ),
                {
                    "id": image_id.hex,
                    "owner": owner_id.hex,
                    "channel": channel_id.hex,
                    "asset": uuid4().hex,
                    "key": f"character-images/{owner_id.hex}/{image_id.hex}",
                    "sha": hashlib.sha256(image_id.bytes).hexdigest(),
                },
            )

    return _Backend(
        SqlAlchemyCoverRepository(uow),
        seed_channel,
        seed_project,
        purge_project,
        seed_character_image,
        uow,
    )


@pytest.fixture(params=("memory", "sql"), ids=("memory", "sql"))
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    if request.param == "memory":
        repo = InMemoryCoverRepository()
        return _Backend(
            repo,
            repo.add_channel,
            lambda owner_id, project_id: repo.set_project_state(owner_id, project_id, "ACTIVE"),
            lambda owner_id, project_id: repo.set_project_state(owner_id, project_id, "PURGING"),
            lambda owner_id, channel_id, image_id: repo.add_character_image(
                CoverCharacterImageRef(
                    image_id,
                    owner_id,
                    channel_id,
                    f"character-images/{owner_id.hex}/{image_id.hex}",
                    hashlib.sha256(image_id.bytes).hexdigest(),
                    128,
                    "image/png",
                )
            ),
        )
    return _sql_backend(tmp_path / "cover.sqlite")


def _ref_image(
    owner_id: UUID,
    *,
    scope: str = COVER_SCOPE_CHANNEL,
    channel_id: UUID | None = None,
    project_id: UUID | None = None,
    sha256: str = "a" * 64,
    object_key: str | None = None,
) -> CoverRefImage:
    return CoverRefImage(
        id=uuid4(),
        owner_id=owner_id,
        scope=scope,
        channel_id=channel_id,
        project_id=project_id,
        sha256=sha256,
        object_key=object_key or f"cover-ref/{uuid4().hex}",
        byte_size=128,
        width=640,
        height=360,
        image_format="png",
        created_at=NOW,
    )


def _draft(
    owner_id: UUID,
    *,
    scope: str = COVER_SCOPE_CHANNEL,
    channel_id: UUID | None = None,
    project_id: UUID | None = None,
    ref_image_id: UUID | None = None,
    edited_text: str = "style text",
    updated_at: datetime = NOW,
) -> CoverStyleDraft:
    return CoverStyleDraft(
        id=uuid4(),
        owner_id=owner_id,
        scope=scope,
        channel_id=channel_id,
        project_id=project_id,
        ref_image_id=ref_image_id,
        dims={"subject": ["外貌"]},
        analysis={"subject": {"外貌": "冷峻"}},
        analysis_dims={"subject": ["外貌"]},
        ref_image_sha256="a" * 64,
        edited_text=edited_text,
        updated_at=updated_at,
    )


def _profile(
    owner_id: UUID,
    channel_id: UUID,
    *,
    ref_image_id: UUID | None = None,
    edited_text: str = "style text",
) -> CoverStyleProfile:
    return CoverStyleProfile(
        id=uuid4(),
        owner_id=owner_id,
        channel_id=channel_id,
        version=0,
        ref_image_id=ref_image_id,
        dims={"subject": ["外貌"]},
        analysis_dims={"subject": ["外貌"]},
        edited_text=edited_text,
        created_at=NOW,
        supersedes_id=None,
    )


def _prompt_generation(
    owner_id: UUID,
    project_id: UUID,
    *,
    idempotency_key: str = "prompt-key-1",
    brief: str = "brief",
    style_dims_used: dict[str, Any] | None = None,
) -> CoverPromptGeneration:
    return CoverPromptGeneration(
        id=uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        version=0,
        style_source="channel_profile",
        channel_style_profile_id=None,
        style_text_used="style text",
        style_dims_used=style_dims_used if style_dims_used is not None else {"subject": ["外貌"]},
        style_override_text=None,
        brief=brief,
        cover_text_candidates=[],
        cover_text_default="",
        cover_text_used="",
        prompt_body="",
        prompt_system="",
        prompt_current="",
        title_used="title",
        script_sha256="b" * 64,
        warnings=[],
        judge=None,
        model="deepseek-v4-flash",
        parameters={"temperature": 0.9},
        idempotency_key=idempotency_key,
        status=COVER_STATUS_INTENT_PERSISTED,
        failure_stage=None,
        failure_code=None,
        created_at=NOW,
    )


def _completed_prompt(value: CoverPromptGeneration) -> CoverPromptGeneration:
    return replace(
        value,
        status=COVER_STATUS_SUCCEEDED,
        cover_text_candidates=[{"text": "到此为止", "pattern": "宣告式"}],
        cover_text_default="到此为止",
        cover_text_used="到此为止",
        prompt_body="prompt body",
        prompt_system="system prompt",
        prompt_current="system prompt",
        warnings=["cover_text_truncated"],
        judge={"best": 0},
    )


def _image_generation(
    owner_id: UUID,
    project_id: UUID,
    prompt_generation_id: UUID,
    *,
    idempotency_key: str = "image-key-1",
    canonical_request_sha256: str = "c" * 64,
) -> CoverImageGeneration:
    return CoverImageGeneration(
        id=uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        prompt_generation_id=prompt_generation_id,
        version=0,
        prompt="final prompt",
        model="gpt-image-2",
        size="2560x1440",
        quality="high",
        parameters={"n": 1},
        idempotency_key=idempotency_key,
        provider_idempotency_key="d" * 64,
        canonical_request_sha256=canonical_request_sha256,
        status=COVER_STATUS_INTENT_PERSISTED,
        provider_receipt=None,
        expected_sha256=None,
        downloaded_sha256=None,
        downloaded_size=None,
        qc_width=None,
        qc_height=None,
        qc_mime=None,
        object_key=None,
        object_sha256=None,
        failure_stage=None,
        failure_code=None,
        created_at=NOW,
    )


def _completed_image(value: CoverImageGeneration) -> CoverImageGeneration:
    digest = "e" * 64
    return replace(
        value,
        status=COVER_STATUS_SUCCEEDED,
        provider_receipt="receipt-1",
        expected_sha256=digest,
        downloaded_sha256=digest,
        downloaded_size=1024,
        qc_width=2560,
        qc_height=1440,
        qc_mime="image/png",
        object_key=f"cover-images/{value.id.hex}.png",
        object_sha256=digest,
    )


def test_ref_image_add_dedupes_within_scope(backend: _Backend) -> None:
    repo = backend.repo
    owner, channel, other_channel, project = uuid4(), uuid4(), uuid4(), uuid4()
    backend.seed_channel(owner, channel)
    backend.seed_channel(owner, other_channel)
    backend.seed_project(owner, project)
    first = _ref_image(owner, channel_id=channel)
    assert repo.add_ref_image(first) is first
    hit = repo.add_ref_image(_ref_image(owner, channel_id=channel, object_key="other/key"))
    assert hit.id == first.id and hit.object_key == first.object_key
    assert [item.id for item in repo.list_ref_images_for_channel(owner, channel)] == [first.id]
    # 同 sha256 在其他频道与项目作用域各自独立
    other_channel_image = _ref_image(owner, channel_id=other_channel)
    assert repo.add_ref_image(other_channel_image).id == other_channel_image.id
    project_image = _ref_image(owner, scope=COVER_SCOPE_PROJECT, project_id=project)
    assert repo.add_ref_image(project_image).id == project_image.id
    assert [item.id for item in repo.list_ref_images_for_project(owner, project)] == [
        project_image.id
    ]
    # owner 隔离
    assert repo.get_ref_image(uuid4(), first.id) is None
    assert repo.list_ref_images_for_channel(uuid4(), channel) == ()


def test_style_draft_upsert_round_trip(backend: _Backend) -> None:
    repo = backend.repo
    owner, channel, project = uuid4(), uuid4(), uuid4()
    backend.seed_channel(owner, channel)
    backend.seed_project(owner, project)
    assert repo.get_style_draft_for_channel(owner, channel) is None
    first_draft = _draft(owner, channel_id=channel, edited_text="v1 text")
    assert repo.upsert_style_draft(first_draft) is first_draft
    update = replace(
        first_draft,
        id=uuid4(),
        edited_text="v2 text",
        dims={"camera": ["焦距"]},
        analysis={"camera": {"焦距": "35mm"}},
        analysis_dims={"camera": ["焦距"]},
        ref_image_sha256="f" * 64,
        ref_image_id=uuid4(),
        updated_at=LATER,
    )
    updated = repo.upsert_style_draft(update)
    assert updated.id == first_draft.id
    assert updated.scope == COVER_SCOPE_CHANNEL
    assert updated.channel_id == channel
    assert updated.edited_text == "v2 text"
    assert updated.dims == {"camera": ["焦距"]}
    assert updated.analysis == {"camera": {"焦距": "35mm"}}
    assert updated.analysis_dims == {"camera": ["焦距"]}
    assert updated.ref_image_sha256 == "f" * 64
    assert updated.ref_image_id == update.ref_image_id
    assert updated.updated_at.replace(tzinfo=None) == LATER.replace(tzinfo=None)
    fetched = repo.get_style_draft_for_channel(owner, channel)
    assert fetched is not None and fetched.id == first_draft.id
    assert fetched.edited_text == "v2 text"
    # 项目作用域草稿独立
    assert repo.get_style_draft_for_project(owner, project) is None
    project_draft = _draft(owner, scope=COVER_SCOPE_PROJECT, project_id=project)
    assert repo.upsert_style_draft(project_draft) is project_draft
    fetched_project = repo.get_style_draft_for_project(owner, project)
    assert fetched_project is not None and fetched_project.id == project_draft.id
    fetched_channel = repo.get_style_draft_for_channel(owner, channel)
    assert fetched_channel is not None and fetched_channel.edited_text == "v2 text"
    # owner 隔离
    assert repo.get_style_draft_for_channel(uuid4(), channel) is None


def test_style_profile_versions_supersedes_chain_and_current_pointer(backend: _Backend) -> None:
    repo = backend.repo
    owner, channel, other_channel = uuid4(), uuid4(), uuid4()
    backend.seed_channel(owner, channel)
    backend.seed_channel(owner, other_channel)
    first = repo.create_style_profile(_profile(owner, channel, edited_text="style v1"))
    assert first.version == 1 and first.supersedes_id is None
    second = repo.create_style_profile(_profile(owner, channel, edited_text="style v2"))
    assert second.version == 2 and second.supersedes_id == first.id
    history = repo.list_style_profiles_for_channel(owner, channel)
    assert [item.version for item in history] == [2, 1]
    fetched = repo.get_style_profile(owner, first.id)
    assert fetched is not None and fetched.edited_text == "style v1"
    assert repo.get_style_profile(uuid4(), first.id) is None
    # 其他频道版本序列独立
    foreign = repo.create_style_profile(_profile(owner, other_channel))
    assert foreign.version == 1 and foreign.supersedes_id is None
    # 当前指针切换
    assert repo.current_style_profile_for_channel(owner, channel) is None
    assert repo.set_channel_current_style_profile(owner, channel, first.id).id == first.id
    current = repo.current_style_profile_for_channel(owner, channel)
    assert current is not None and current.id == first.id
    repo.set_channel_current_style_profile(owner, channel, second.id)
    current = repo.current_style_profile_for_channel(owner, channel)
    assert current is not None and current.id == second.id
    with pytest.raises(DomainError) as caught:
        repo.set_channel_current_style_profile(owner, channel, uuid4())
    assert caught.value.code == "invalid_cover_candidate"
    with pytest.raises(DomainError) as caught:
        repo.set_channel_current_style_profile(owner, other_channel, first.id)
    assert caught.value.code == "invalid_cover_candidate"
    with pytest.raises(DomainError) as caught:
        repo.create_style_profile(_profile(owner, uuid4()))
    assert caught.value.code == "not_found"


def test_prompt_generation_create_idempotency_conflict_branches(backend: _Backend) -> None:
    repo = backend.repo
    owner, project, other_project = uuid4(), uuid4(), uuid4()
    backend.seed_project(owner, project)
    backend.seed_project(owner, other_project)
    value = _prompt_generation(owner, project)
    created = repo.create_prompt_generation(value)
    assert created.version == 1
    # 同请求重放返回既有记录
    replay = repo.create_prompt_generation(replace(value, id=uuid4()))
    assert replay.id == created.id and replay.version == 1
    assert len(repo.list_prompt_generations_for_project(owner, project)) == 1
    # 键绑不同请求指纹 → 幂等冲突
    with pytest.raises(DomainError) as caught:
        repo.create_prompt_generation(replace(value, id=uuid4(), brief="different brief"))
    assert caught.value.code == "idempotency_conflict"
    with pytest.raises(DomainError) as caught:
        repo.create_prompt_generation(
            replace(value, id=uuid4(), style_dims_used={"camera": ["焦距"]})
        )
    assert caught.value.code == "idempotency_conflict"
    # 不同键递增版本；不同项目序列独立
    second = repo.create_prompt_generation(
        _prompt_generation(owner, project, idempotency_key="prompt-key-2")
    )
    assert second.version == 2
    foreign = repo.create_prompt_generation(
        _prompt_generation(owner, other_project, idempotency_key="prompt-key-other")
    )
    assert foreign.version == 1
    history = repo.list_prompt_generations_for_project(owner, project)
    assert [item.version for item in history] == [2, 1]
    assert repo.get_prompt_generation(uuid4(), created.id) is None


def test_prompt_generation_complete_panel_edit_current_and_failure(backend: _Backend) -> None:
    repo = backend.repo
    owner, project = uuid4(), uuid4()
    backend.seed_project(owner, project)
    created = repo.create_prompt_generation(_prompt_generation(owner, project))
    assert repo.current_prompt_generation_for_project(owner, project) is None
    with pytest.raises(DomainError) as caught:
        repo.update_prompt_current(owner, created.id, "edited")
    assert caught.value.code == "revision_conflict"
    with pytest.raises(DomainError) as caught:
        repo.set_current_prompt_generation(owner, project, created.id)
    assert caught.value.code == "invalid_cover_candidate"
    done = repo.complete_prompt_generation(_completed_prompt(created))
    assert done.status == COVER_STATUS_SUCCEEDED
    assert done.prompt_system == "system prompt"
    assert done.prompt_body == "prompt body"
    assert done.cover_text_candidates == [{"text": "到此为止", "pattern": "宣告式"}]
    assert done.cover_text_default == "到此为止" == done.cover_text_used
    assert done.warnings == ["cover_text_truncated"]
    assert done.judge == {"best": 0}
    # 终态重放返回既有记录
    replay = repo.complete_prompt_generation(replace(done, prompt_system="changed"))
    assert replay.prompt_system == "system prompt"
    # 切当前 + 面板编辑（只动 prompt_current，不动 system）
    repo.set_current_prompt_generation(owner, project, created.id)
    current = repo.current_prompt_generation_for_project(owner, project)
    assert current is not None and current.id == created.id
    edited = repo.update_prompt_current(owner, created.id, "edited prompt")
    assert edited.prompt_current == "edited prompt"
    assert edited.prompt_system == "system prompt"
    fetched = repo.get_prompt_generation(owner, created.id)
    assert fetched is not None and fetched.prompt_current == "edited prompt"
    assert fetched.prompt_system == "system prompt"
    # SUCCEEDED 后 mark_failed 为重放（不改变既有终态）
    still = repo.mark_prompt_generation_failed(owner, created.id, "provider", "http_500")
    assert still.status == COVER_STATUS_SUCCEEDED
    # 另一条版本：失败终态与失败码稳定、重放不变
    second = repo.create_prompt_generation(
        _prompt_generation(owner, project, idempotency_key="prompt-key-2")
    )
    failed = repo.mark_prompt_generation_failed(owner, second.id, "provider", "http_500")
    assert failed.status == COVER_STATUS_FAILED
    assert failed.failure_stage == "provider" and failed.failure_code == "http_500"
    terminal = repo.mark_prompt_generation_failed(owner, second.id, "other", "other_code")
    assert terminal.failure_code == "http_500"
    with pytest.raises(DomainError) as caught:
        repo.update_prompt_current(owner, second.id, "nope")
    assert caught.value.code == "revision_conflict"
    with pytest.raises(DomainError) as caught:
        repo.set_current_prompt_generation(owner, project, second.id)
    assert caught.value.code == "invalid_cover_candidate"
    with pytest.raises(DomainError) as caught:
        repo.mark_prompt_generation_failed(owner, uuid4(), "provider", "http_500")
    assert caught.value.code == "not_found"


def test_image_generation_complete_switch_current_and_failure(backend: _Backend) -> None:
    repo = backend.repo
    owner, project, prompt_id = uuid4(), uuid4(), uuid4()
    backend.seed_project(owner, project)
    first = repo.create_image_generation(_image_generation(owner, project, prompt_id))
    assert first.version == 1
    replay = repo.create_image_generation(replace(first, id=uuid4()))
    assert replay.id == first.id
    with pytest.raises(DomainError) as caught:
        repo.create_image_generation(replace(first, id=uuid4(), canonical_request_sha256="f" * 64))
    assert caught.value.code == "idempotency_conflict"
    assert repo.current_image_generation_for_project(owner, project) is None
    done = repo.complete_image_generation_and_switch_current(
        _completed_image(first), switch_current=True
    )
    assert done.status == COVER_STATUS_SUCCEEDED
    assert done.qc_width == 2560 and done.qc_height == 1440
    assert done.qc_mime == "image/png"
    assert done.object_key == f"cover-images/{first.id.hex}.png"
    assert done.object_sha256 == "e" * 64
    assert done.downloaded_sha256 == "e" * 64
    assert done.downloaded_size == 1024
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == first.id
    # 第二个成功不自动切当前（首个成功自动当前由应用层决定调用）
    second = repo.create_image_generation(
        _image_generation(
            owner,
            project,
            prompt_id,
            idempotency_key="image-key-2",
            canonical_request_sha256="1" * 64,
        )
    )
    assert second.version == 2
    repo.complete_image_generation_and_switch_current(
        _completed_image(second), switch_current=False
    )
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == first.id
    # 用户明确切换
    repo.set_current_image_generation(owner, project, second.id)
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == second.id
    # 重放旧 complete 不回切指针
    repo.complete_image_generation_and_switch_current(_completed_image(first), switch_current=True)
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == second.id
    # 失败版本不可切换
    third = repo.create_image_generation(
        _image_generation(
            owner,
            project,
            prompt_id,
            idempotency_key="image-key-3",
            canonical_request_sha256="2" * 64,
        )
    )
    failed = repo.mark_image_generation_failed(owner, third.id, "download", "hash_mismatch")
    assert failed.status == COVER_STATUS_FAILED
    assert failed.failure_stage == "download" and failed.failure_code == "hash_mismatch"
    with pytest.raises(DomainError) as caught:
        repo.set_current_image_generation(owner, project, third.id)
    assert caught.value.code == "invalid_cover_candidate"
    fetched = repo.get_image_generation(owner, third.id)
    assert fetched is not None and fetched.status == COVER_STATUS_FAILED
    assert repo.get_image_generation(uuid4(), third.id) is None
    history = repo.list_image_generations_for_project(owner, project)
    assert [item.version for item in history] == [3, 2, 1]


def _uploaded_image(
    owner_id: UUID,
    project_id: UUID,
    *,
    sha256: str = "f" * 64,
) -> CoverImageGeneration:
    return CoverImageGeneration(
        id=uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        prompt_generation_id=None,
        version=0,
        prompt="",
        model=None,
        size=None,
        quality=None,
        parameters={},
        idempotency_key=f"upload:{project_id}:{sha256}",
        provider_idempotency_key="",
        canonical_request_sha256=sha256,
        status=COVER_STATUS_SUCCEEDED,
        provider_receipt=None,
        expected_sha256=None,
        downloaded_sha256=None,
        downloaded_size=2048,
        qc_width=1280,
        qc_height=720,
        qc_mime="image/png",
        object_key=f"cover-images/uploads/{owner_id}/{project_id}/{sha256}",
        object_sha256=sha256,
        failure_stage=None,
        failure_code=None,
        created_at=NOW,
        source=COVER_IMAGE_SOURCE_UPLOAD,
    )


def test_uploaded_image_generation_dedupes_and_switches_current(backend: _Backend) -> None:
    repo = backend.repo
    owner, project = uuid4(), uuid4()
    backend.seed_project(owner, project)

    first, reused = repo.create_uploaded_image_generation(
        _uploaded_image(owner, project, sha256="1" * 64)
    )
    assert reused is False
    assert first.version == 1 and first.source == COVER_IMAGE_SOURCE_UPLOAD
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == first.id

    second, reused = repo.create_uploaded_image_generation(
        _uploaded_image(owner, project, sha256="2" * 64)
    )
    assert reused is False and second.version == 2
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == second.id

    repo.set_current_image_generation(owner, project, first.id)
    again, reused = repo.create_uploaded_image_generation(
        _uploaded_image(owner, project, sha256="1" * 64)
    )
    assert reused is True and again.id == first.id
    assert len(repo.list_image_generations_for_project(owner, project)) == 2
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == first.id


def test_uploaded_image_generation_is_scoped_by_project(backend: _Backend) -> None:
    repo = backend.repo
    owner, first_project, second_project = uuid4(), uuid4(), uuid4()
    backend.seed_project(owner, first_project)
    backend.seed_project(owner, second_project)

    first, _ = repo.create_uploaded_image_generation(
        _uploaded_image(owner, first_project, sha256="3" * 64)
    )
    second, _ = repo.create_uploaded_image_generation(
        _uploaded_image(owner, second_project, sha256="3" * 64)
    )

    assert first.id != second.id
    assert first.version == 1 and second.version == 1
    current = repo.current_image_generation_for_project(owner, first_project)
    assert current is not None and current.id == first.id
    current = repo.current_image_generation_for_project(owner, second_project)
    assert current is not None and current.id == second.id


def test_uploaded_image_generation_versions_after_generated(backend: _Backend) -> None:
    repo = backend.repo
    owner, project = uuid4(), uuid4()
    backend.seed_project(owner, project)
    prompt = repo.create_prompt_generation(_prompt_generation(owner, project))
    generated = repo.create_image_generation(_image_generation(owner, project, prompt.id))

    uploaded, reused = repo.create_uploaded_image_generation(
        _uploaded_image(owner, project, sha256="4" * 64)
    )

    assert reused is False
    assert generated.version == 1
    assert uploaded.version == 2
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.id == uploaded.id


def test_uploaded_image_generation_requires_active_project(backend: _Backend) -> None:
    repo = backend.repo
    owner = uuid4()

    with pytest.raises(DomainError) as caught:
        repo.create_uploaded_image_generation(_uploaded_image(owner, uuid4()))

    assert caught.value.code == "not_found"


def test_delete_ref_images_for_project_scopes_cleanup(backend: _Backend) -> None:
    repo = backend.repo
    owner, channel, project, other_project = uuid4(), uuid4(), uuid4(), uuid4()
    backend.seed_channel(owner, channel)
    backend.seed_project(owner, project)
    backend.seed_project(owner, other_project)
    channel_image = repo.add_ref_image(_ref_image(owner, channel_id=channel, sha256="a" * 64))
    first = repo.add_ref_image(
        _ref_image(owner, scope=COVER_SCOPE_PROJECT, project_id=project, sha256="b" * 64)
    )
    second = repo.add_ref_image(
        _ref_image(owner, scope=COVER_SCOPE_PROJECT, project_id=project, sha256="c" * 64)
    )
    foreign = repo.add_ref_image(
        _ref_image(owner, scope=COVER_SCOPE_PROJECT, project_id=other_project, sha256="d" * 64)
    )
    repo.upsert_style_draft(_draft(owner, channel_id=channel, ref_image_id=channel_image.id))
    repo.upsert_style_draft(
        _draft(owner, scope=COVER_SCOPE_PROJECT, project_id=project, ref_image_id=first.id)
    )
    profile = repo.create_style_profile(_profile(owner, channel, ref_image_id=channel_image.id))
    removed = repo.delete_ref_images_for_project(owner, project)
    assert {item.id for item in removed} == {first.id, second.id}
    assert {item.object_key for item in removed} == {first.object_key, second.object_key}
    assert repo.list_ref_images_for_project(owner, project) == ()
    assert repo.get_ref_image(owner, first.id) is None
    # 频道数据与其他项目数据不受影响
    assert [item.id for item in repo.list_ref_images_for_channel(owner, channel)] == [
        channel_image.id
    ]
    assert [item.id for item in repo.list_ref_images_for_project(owner, other_project)] == [
        foreign.id
    ]
    # 引用被置空（照 FK SET NULL 语义），草稿与频道风格本体保留
    project_draft = repo.get_style_draft_for_project(owner, project)
    assert project_draft is not None and project_draft.ref_image_id is None
    channel_draft = repo.get_style_draft_for_channel(owner, channel)
    assert channel_draft is not None and channel_draft.ref_image_id == channel_image.id
    stored_profile = repo.get_style_profile(owner, profile.id)
    assert stored_profile is not None and stored_profile.ref_image_id == channel_image.id
    # 幂等：再删为空
    assert repo.delete_ref_images_for_project(owner, project) == ()


def test_project_scope_mutations_require_active_project(backend: _Backend) -> None:
    repo = backend.repo
    owner, project = uuid4(), uuid4()
    with pytest.raises(DomainError) as caught:
        repo.create_prompt_generation(_prompt_generation(owner, project))
    assert caught.value.code == "not_found"
    backend.seed_project(owner, project)
    backend.purge_project(owner, project)
    with pytest.raises(DomainError) as caught:
        repo.create_prompt_generation(_prompt_generation(owner, project))
    assert caught.value.code == "project_purging"
    with pytest.raises(DomainError) as caught:
        repo.add_ref_image(_ref_image(owner, scope=COVER_SCOPE_PROJECT, project_id=project))
    assert caught.value.code == "project_purging"
    with pytest.raises(DomainError) as caught:
        repo.upsert_style_draft(_draft(owner, scope=COVER_SCOPE_PROJECT, project_id=project))
    assert caught.value.code == "project_purging"


def test_invalid_scope_is_rejected(backend: _Backend) -> None:
    repo = backend.repo
    owner = uuid4()
    with pytest.raises(DomainError) as caught:
        repo.add_ref_image(_ref_image(owner, scope="bogus"))
    assert caught.value.code == "invalid_cover_scope"
    with pytest.raises(DomainError) as caught:
        repo.upsert_style_draft(_draft(owner, scope="bogus"))
    assert caught.value.code == "invalid_cover_scope"


def test_channel_scope_exists_checks_ownership(backend: _Backend) -> None:
    repo = backend.repo
    owner, other_owner, channel = uuid4(), uuid4(), uuid4()
    assert repo.channel_scope_exists(owner, channel) is False
    backend.seed_channel(owner, channel)
    assert repo.channel_scope_exists(owner, channel) is True
    assert repo.channel_scope_exists(other_owner, channel) is False
    assert repo.channel_scope_exists(owner, uuid4()) is False


def test_two_sql_repositories_concurrently_create_one_idempotent_prompt(tmp_path: Path) -> None:
    backend = _sql_backend(tmp_path / "race.sqlite")
    assert backend.uow_factory is not None
    first, second = backend.repo, SqlAlchemyCoverRepository(backend.uow_factory)
    owner, project = uuid4(), uuid4()
    backend.seed_project(owner, project)
    value = _prompt_generation(owner, project)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda repo: repo.create_prompt_generation(value), (first, second))
        )
    assert results[0].id == results[1].id
    assert len(first.list_prompt_generations_for_project(owner, project)) == 1


# ---------- v5：角色卡 CRUD / label / character_cards_used 血缘 ----------


def _character_card(
    owner_id: UUID,
    channel_id: UUID,
    character_image_id: UUID,
    *,
    identity_text: str = "小艾",
    appearance_text: str = "二头身",
    source: str = "manual",
    created_at: datetime = NOW,
    updated_at: datetime = NOW,
) -> CoverCharacterCard:
    return CoverCharacterCard(
        id=uuid4(),
        owner_id=owner_id,
        channel_id=channel_id,
        character_image_id=character_image_id,
        identity_text=identity_text,
        appearance_text=appearance_text,
        source=source,
        created_at=created_at,
        updated_at=updated_at,
    )


def test_character_card_upsert_per_image_and_listing(backend: _Backend) -> None:
    repo = backend.repo
    owner, channel, other_channel = uuid4(), uuid4(), uuid4()
    image_a, image_b = uuid4(), uuid4()
    backend.seed_channel(owner, channel)
    backend.seed_channel(owner, other_channel)
    backend.seed_character_image(owner, channel, image_a)
    backend.seed_character_image(owner, channel, image_b)

    # 角色图只读投影：属主匹配与隔离。
    ref = repo.get_character_image(owner, image_a)
    assert ref is not None and ref.channel_id == channel and ref.sha256
    assert repo.get_character_image(uuid4(), image_a) is None

    # 建卡 → 同角色图再 upsert = 改卡（id 与 created_at 保留）。
    first = _character_card(owner, channel, image_a, identity_text="v1")
    assert repo.upsert_character_card(first) is first
    update = _character_card(
        owner,
        channel,
        image_a,
        identity_text="v2",
        appearance_text="",
        source="analysis",
        created_at=NOW,
        updated_at=LATER,
    )
    updated = repo.upsert_character_card(update)
    assert updated.id == first.id
    assert updated.identity_text == "v2" and updated.appearance_text == ""
    assert updated.source == "analysis"
    assert updated.created_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)
    assert updated.updated_at.replace(tzinfo=None) == LATER.replace(tzinfo=None)
    assert len(repo.list_character_cards_for_channel(owner, channel)) == 1

    # 第二角色图第二卡；列表按 (created_at, id) 升序；频道隔离。
    second = _character_card(owner, channel, image_b, created_at=LATER, updated_at=LATER)
    repo.upsert_character_card(second)
    assert [item.id for item in repo.list_character_cards_for_channel(owner, channel)] == [
        first.id,
        second.id,
    ]
    assert repo.list_character_cards_for_channel(owner, other_channel) == ()

    # get / by_ids：保序、跳过缺失与跨属主；owner 隔离。
    fetched = repo.get_character_card(owner, first.id)
    assert fetched is not None and fetched.character_image_id == image_a
    assert repo.get_character_card(uuid4(), first.id) is None
    ordered = repo.get_character_cards_by_ids(owner, (second.id, uuid4(), first.id))
    assert [item.id for item in ordered] == [second.id, first.id]

    # 频道不存在 → not_found。
    with pytest.raises(DomainError) as caught:
        repo.upsert_character_card(_character_card(owner, uuid4(), image_a))
    assert caught.value.code == "not_found"


def test_style_profile_label_round_trip(backend: _Backend) -> None:
    repo = backend.repo
    owner, channel = uuid4(), uuid4()
    backend.seed_channel(owner, channel)
    labeled = repo.create_style_profile(
        replace(_profile(owner, channel, edited_text="style v1"), label="首发风格")
    )
    assert labeled.label == "首发风格"
    fetched = repo.get_style_profile(owner, labeled.id)
    assert fetched is not None and fetched.label == "首发风格"
    unlabeled = repo.create_style_profile(_profile(owner, channel, edited_text="style v2"))
    assert unlabeled.label == ""
    history = repo.list_style_profiles_for_channel(owner, channel)
    assert [item.label for item in history] == ["", "首发风格"]
    current = repo.set_channel_current_style_profile(owner, channel, labeled.id)
    assert current.label == "首发风格"


def test_prompt_generation_character_cards_used_lineage(backend: _Backend) -> None:
    repo = backend.repo
    owner, project = uuid4(), uuid4()
    backend.seed_project(owner, project)
    lineage = [
        {
            "card_id": str(uuid4()),
            "character_image_id": str(uuid4()),
            "identity_text": "小艾",
            "appearance_text": "二头身",
        }
    ]
    value = replace(_prompt_generation(owner, project), character_cards_used=lineage)
    created = repo.create_prompt_generation(value)
    assert created.character_cards_used == lineage
    fetched = repo.get_prompt_generation(owner, created.id)
    assert fetched is not None and fetched.character_cards_used == lineage
    current = repo.list_prompt_generations_for_project(owner, project)
    assert current[0].character_cards_used == lineage
    # 同键重放返回既有记录；血缘不同 → 幂等冲突（血缘属请求事实）。
    replay = repo.create_prompt_generation(replace(value, id=uuid4()))
    assert replay.id == created.id
    with pytest.raises(DomainError) as caught:
        repo.create_prompt_generation(replace(value, id=uuid4(), character_cards_used=[]))
    assert caught.value.code == "idempotency_conflict"


def test_image_generation_visual_review_roundtrip(backend: _Backend) -> None:
    repo = backend.repo
    owner, project, prompt_id = uuid4(), uuid4(), uuid4()
    backend.seed_project(owner, project)
    created = repo.create_image_generation(_image_generation(owner, project, prompt_id))
    assert created.visual_review is None
    review = {
        "status": "succeeded",
        "ok": False,
        "issues": [
            {
                "kind": "style_mismatch",
                "severity": "blocker",
                "description": "与风格描述矛盾",
                "suggested_edit": "平涂色块",
            }
        ],
        "model": "gpt-5.5",
        "receipt": "receipt-r1",
    }
    done = repo.complete_image_generation_and_switch_current(
        replace(_completed_image(created), visual_review=review), switch_current=True
    )
    assert done.visual_review == review
    fetched = repo.get_image_generation(owner, created.id)
    assert fetched is not None and fetched.visual_review == review
    current = repo.current_image_generation_for_project(owner, project)
    assert current is not None and current.visual_review == review
    history = repo.list_image_generations_for_project(owner, project)
    assert history[0].visual_review == review
    failed_review = {"status": "failed", "code": "cover_image_review_failed"}
    second = repo.create_image_generation(
        _image_generation(
            owner,
            project,
            prompt_id,
            idempotency_key="image-key-review-2",
            canonical_request_sha256="9" * 64,
        )
    )
    done_second = repo.complete_image_generation_and_switch_current(
        replace(_completed_image(second), visual_review=failed_review), switch_current=False
    )
    assert done_second.visual_review == failed_review
