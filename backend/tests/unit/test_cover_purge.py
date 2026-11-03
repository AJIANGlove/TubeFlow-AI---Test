"""项目删除清理枚举的封面触点：项目作用域封面裸对象键进入清理清单并按键删除。

行清理由 projects 删除时的 FK CASCADE/SET NULL 完成（迁移 0030：
cover_ref_images/cover_style_drafts/cover_prompt_generations/cover_image_generations/
cover_outbox 的 project_id 均为 ondelete CASCADE；草稿/频道风格版本的
ref_image_id 为 ondelete SET NULL）；SQLite 不执行 FK，故行级存活断言只覆盖
「频道作用域封面数据不受项目删除影响」。

SQLite 模式照 test_cover_repository_contract：ORM 无法建表的主表（projects 含
PG ARRAY 列、purge 表含裸 JSONB 列）用裸 SQL 桩表，仅含仓储触及的列。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tubeflow_test.adapters.persistence import media_repository as media_repository_module
from tubeflow_test.adapters.persistence.cover.models import (
    CoverImageGenerationRow,
    CoverRefImageRow,
)
from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyDeletionRepository,
    production_active_job_gates,
)
from tubeflow_test.adapters.persistence.models import (
    DeletionTombstoneRow,
    MediaAssetRow,
    ProjectAssetReferenceRow,
    StagedObjectRow,
)
from tubeflow_test.application.deletion import ProjectDeletionService
from tubeflow_test.video_generation.persistence import VideoAttemptRecord

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class NaiveUtcNow(datetime):
    """SQLite 的 DATETIME 列丢时区（PG timestamptz 无此问题）：

    _claim_matches 用 datetime.now(UTC) 与库内 lease_until 比较；SQLite 读回的
    值是 naive，比较会 TypeError。测试内把该模块的时钟替换为 naive UTC now，
    两侧同口径比较（生产 PG 上两侧均为 aware，语义一致）。
    """

    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def]
        value = datetime.now(tz)
        return value.replace(tzinfo=None) if tz is not None else value


_STUB_TABLES = (
    "CREATE TABLE projects ("
    "id CHAR(32) PRIMARY KEY, owner_id CHAR(32), channel_id CHAR(32), "
    "title TEXT, script TEXT, character_image_ids TEXT, visual_style TEXT, "
    "voice_version_id CHAR(32), tts_provider TEXT, cloned_voice_id CHAR(32), "
    "locale VARCHAR(8), aspect_ratio VARCHAR(5), "
    "expected_duration_seconds INTEGER, created_at DATETIME, published_at DATETIME, "
    "draft_revision INTEGER, "
    "production_state VARCHAR(16), production_mode VARCHAR(8), auto_budget_round INTEGER, "
    "production_revision INTEGER, snapshot_script TEXT, snapshot_character_image_ids TEXT, "
    "snapshot_visual_style TEXT, snapshot_voice_version_id CHAR(32), "
    "snapshot_locale VARCHAR(8), snapshot_aspect_ratio VARCHAR(5), "
    "snapshot_channel_url TEXT, snapshot_voice_metadata_sha256 CHAR(64), "
    "snapshot_voice_catalog_source_sha256 CHAR(64), "
    "snapshot_voice_reference_asset_id CHAR(32), snapshot_voice_reference_sha256 CHAR(64), "
    "snapshot_voice_reference_media_type VARCHAR(64), "
    "snapshot_voice_reference_size_bytes INTEGER, "
    "snapshot_voice_preview_asset_id CHAR(32), snapshot_voice_preview_sha256 CHAR(64), "
    "snapshot_voice_preview_media_type VARCHAR(64), "
    "snapshot_voice_preview_size_bytes INTEGER, "
    "snapshot_voice_avatar_asset_id CHAR(32), snapshot_voice_avatar_sha256 CHAR(64), "
    "snapshot_voice_avatar_media_type VARCHAR(64), "
    "snapshot_voice_avatar_size_bytes INTEGER, "
    "snapshot_tts_provider TEXT, snapshot_cloned_voice_id CHAR(32), "
    "snapshot_cloned_voice_name TEXT, snapshot_cloned_voice_source_sha256 CHAR(64), "
    "snapshot_expected_duration_seconds INTEGER, snapshot_confirmed_at DATETIME, "
    "current_cover_prompt_generation_id CHAR(32), "
    "current_cover_image_generation_id CHAR(32))",
    "CREATE TABLE project_purge_jobs ("
    "project_id CHAR(32) PRIMARY KEY, owner_id CHAR(32), state VARCHAR(16), "
    "dispatch_error_code VARCHAR(64), object_results TEXT, workflow_generation INTEGER, "
    "execution_token CHAR(32), lease_until DATETIME)",
    "CREATE TABLE project_purge_objects ("
    "project_id CHAR(32), object_key TEXT, asset_id CHAR(32), staged_object_id CHAR(32), "
    "object_kind VARCHAR(16), sha256 CHAR(64), owner_id CHAR(32), "
    "external_reference_count INTEGER, deletion_token CHAR(32), "
    "deletion_generation INTEGER, PRIMARY KEY (project_id, object_key))",
    "CREATE TABLE purge_outbox ("
    "id CHAR(32) PRIMARY KEY, project_id CHAR(32), owner_id CHAR(32), "
    "dispatched_at DATETIME, lease_until DATETIME, attempt INTEGER, "
    "last_error_code VARCHAR(64), workflow_generation INTEGER, lease_token CHAR(32))",
    "CREATE TABLE narration_project_records ("
    "project_id CHAR(32) PRIMARY KEY, owner_id CHAR(32), payload TEXT)",
    "CREATE TABLE narration_attempt_records ("
    "id CHAR(32) PRIMARY KEY, owner_id CHAR(32), project_id CHAR(32), payload TEXT)",
    "CREATE TABLE storyboard_generations ("
    "id CHAR(32) PRIMARY KEY, project_id CHAR(32), status VARCHAR(32))",
    "CREATE TABLE director_generation_attempts ("
    "id CHAR(32) PRIMARY KEY, group_id CHAR(32), status VARCHAR(32))",
    "CREATE TABLE authoritative_narration_groups (id CHAR(32) PRIMARY KEY, project_id CHAR(32))",
    "CREATE TABLE project_director_runs ("
    "id CHAR(32) PRIMARY KEY, project_id CHAR(32), status VARCHAR(32))",
    "CREATE TABLE project_director_current (project_id CHAR(32) PRIMARY KEY, revision_id CHAR(32))",
    "CREATE TABLE project_director_materializations ("
    "id CHAR(32) PRIMARY KEY, project_id CHAR(32), preplan_revision_id CHAR(32))",
    "CREATE TABLE storyboard_outbox ("
    "id CHAR(32) PRIMARY KEY, project_id CHAR(32), acknowledged_at DATETIME)",
    "CREATE TABLE composition_jobs ("
    "id CHAR(32) PRIMARY KEY, project_id CHAR(32), state VARCHAR(24), "
    "idempotency_key VARCHAR(256))",
    "CREATE TABLE composition_outbox ("
    "id CHAR(32) PRIMARY KEY, project_id CHAR(32), owner_id CHAR(32), "
    "revision_id CHAR(32), inputs_sha256 CHAR(64), workflow_id VARCHAR(256), "
    "state VARCHAR(24), attempt INTEGER, workflow_generation INTEGER, "
    "lease_token CHAR(32), lease_until DATETIME, next_attempt_at DATETIME, "
    "dispatched_at DATETIME, last_error_code VARCHAR(64), budget_round INTEGER)",
)


class FakeDeleter:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_exact(self, object_key: str) -> None:
        self.deleted.append(object_key)


def _engine(path: Path):  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite:///{path}")
    for table in (
        MediaAssetRow.__table__,
        ProjectAssetReferenceRow.__table__,
        StagedObjectRow.__table__,
        VideoAttemptRecord.__table__,
        CoverRefImageRow.__table__,
        CoverImageGenerationRow.__table__,
        DeletionTombstoneRow.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        for statement in _STUB_TABLES:
            connection.execute(text(statement))
    return engine


def _seed_project(engine, owner_id: UUID, project_id: UUID) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id, owner_id, channel_id, title, script, "
                "production_state, draft_revision, production_revision, production_mode, "
                "auto_budget_round) VALUES "
                "(:id, :owner, :channel, '标题', '文案', 'ACTIVE', 1, 1, 'MANUAL', 1)"
            ),
            {"id": project_id.hex, "owner": owner_id.hex, "channel": uuid4().hex},
        )


def _ref_image(
    owner_id: UUID, *, scope: str, project_id: UUID | None, sha256: str, object_key: str
) -> CoverRefImageRow:
    return CoverRefImageRow(
        id=uuid4(),
        owner_id=owner_id,
        scope=scope,
        channel_id=uuid4() if scope == "channel" else None,
        project_id=project_id,
        sha256=sha256,
        object_key=object_key,
        byte_size=128,
        width=640,
        height=360,
        image_format="PNG",
        created_at=NOW,
    )


def _image_generation(
    owner_id: UUID, project_id: UUID, *, status: str, object_key: str | None, version: int
) -> CoverImageGenerationRow:
    return CoverImageGenerationRow(
        id=uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        prompt_generation_id=uuid4(),
        version=version,
        prompt="final prompt",
        model="gpt-image-2",
        size="2560x1440",
        quality="high",
        parameters={},
        idempotency_key=f"image-{uuid4().hex}",
        provider_idempotency_key="d" * 64,
        canonical_request_sha256="c" * 64,
        status=status,
        provider_receipt=None,
        expected_sha256=None,
        downloaded_sha256=None,
        downloaded_size=None,
        qc_width=None,
        qc_height=None,
        qc_mime=None,
        object_key=object_key,
        object_sha256=None,
        created_at=NOW,
    )


def test_purge_enumerates_project_scope_cover_object_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_repository_module, "datetime", NaiveUtcNow)
    engine = _engine(tmp_path / "purge.sqlite")
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner_id, project_id = uuid4(), uuid4()
    _seed_project(engine, owner_id, project_id)
    ref_key = f"cover-ref-images/project/{project_id}/{'a' * 64}"
    image_key = f"cover-images/{owner_id}/{uuid4()}"
    channel_ref = _ref_image(
        owner_id, scope="channel", project_id=None, sha256="c" * 64, object_key="channel/key"
    )
    with sessions() as session:
        session.add(
            _ref_image(
                owner_id,
                scope="project",
                project_id=project_id,
                sha256="a" * 64,
                object_key=ref_key,
            )
        )
        session.add(
            _image_generation(
                owner_id, project_id, status="SUCCEEDED", object_key=image_key, version=1
            )
        )
        session.add(
            _image_generation(
                owner_id, project_id, status="INTENT_PERSISTED", object_key=None, version=2
            )
        )
        session.add(channel_ref)
        session.commit()

    repository = SqlAlchemyDeletionRepository(sessions, production_active_job_gates())
    assert repository.begin_with_lock_and_outbox(project_id, owner_id) is True

    record = repository.get_for_owner(project_id, owner_id)
    assert record is not None
    keys = {item.object_key for item in record.objects}
    assert keys == {ref_key, image_key}
    assert all(item.scope == "PROJECT" and not item.shared_candidate for item in record.objects)

    deleter = FakeDeleter()
    ProjectDeletionService(repository, deleter).clean(  # type: ignore[arg-type]
        project_id, owner_id, workflow_generation=1
    )
    assert set(deleter.deleted) == {ref_key, image_key}
    tombstone = repository.tombstone(project_id)
    assert tombstone is not None and tombstone.object_count == 2

    with sessions() as session:
        # 频道作用域封面数据不受项目删除影响（行与键均未进入清理清单）。
        assert session.get(CoverRefImageRow, channel_ref.id) is not None
    engine.dispose()
