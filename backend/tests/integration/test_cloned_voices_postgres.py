"""§23.4/§23.5 克隆音色与配音源的 PostgreSQL 契约测试。

覆盖：`0035` 迁移从空库升级后的表/列/约束形状；克隆音色行与账号映射往返；
删除保护第三类引用（频道默认）在 SQL 上的真实计数；项目配音源与快照事实往返。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import migration_head
from sqlalchemy import inspect, text

from tubeflow_test.adapters.persistence.identity_project.models import (
    ClonedVoiceAccountRow,
    ClonedVoiceRow,
    ProjectRow,
)
from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.identity_project.domain import (
    Channel,
    ClonedVoice,
    ClonedVoiceAccount,
    TtsProvider,
)

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


@pytest.mark.integration
def test_0035_schema_shape_matches_models(postgres_database) -> None:
    inspector = inspect(postgres_database.engine)
    tables = set(inspector.get_table_names())
    assert {"cloned_voices", "cloned_voice_accounts"}.issubset(tables)

    for model in (ClonedVoiceRow, ClonedVoiceAccountRow):
        columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
        assert columns == set(model.__table__.columns.keys())

    channel_columns = {column["name"] for column in inspector.get_columns("channels")}
    assert {"default_tts_provider", "default_cloned_voice_id"}.issubset(channel_columns)
    project_columns = {
        column["name"]: column for column in inspector.get_columns("projects")
    }
    assert {
        "tts_provider",
        "cloned_voice_id",
        "snapshot_tts_provider",
        "snapshot_cloned_voice_id",
        "snapshot_cloned_voice_name",
        "snapshot_cloned_voice_source_sha256",
    }.issubset(project_columns)
    # EL 项目没有公共音色：projects.voice_version_id 必须可空。
    assert project_columns["voice_version_id"]["nullable"] is True

    attempt_columns = {
        column["name"] for column in inspector.get_columns("narration_attempt_records")
    }
    assert "provider_request_id" in attempt_columns

    foreign_keys = inspector.get_foreign_keys("cloned_voices")
    assert any(
        item["constrained_columns"] == ["channel_id"]
        and item["referred_table"] == "channels"
        and item["options"].get("ondelete") == "RESTRICT"
        for item in foreign_keys
    )
    account_foreign_keys = inspector.get_foreign_keys("cloned_voice_accounts")
    assert any(
        item["constrained_columns"] == ["pool_key_id"]
        and item["referred_table"] == "elevenlabs_keys"
        and item["options"].get("ondelete") == "RESTRICT"
        for item in account_foreign_keys
    )
    # 刻意不建 FK：channels ↔ cloned_voices 会形成建表环（指针列先例）。
    assert not any(
        item["constrained_columns"] == ["default_cloned_voice_id"]
        for item in inspector.get_foreign_keys("channels")
    )


@pytest.mark.integration
def test_cloned_voice_round_trip_mapping_and_usage_counts(
    postgres_database, production_parent_chain
) -> None:
    from tubeflow_test.adapters.persistence.elevenlabs import SqlElevenlabsKeyPool

    ids = production_parent_chain
    pool = SqlElevenlabsKeyPool(postgres_database.sessions)
    key = pool.add_key(api_key="sk-clone", label="主号", now=NOW)
    voice_id = uuid4()
    with postgres_database.sessions.begin() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        repository.add_cloned_voice(
            ClonedVoice(
                id=voice_id,
                channel_id=ids.channel_id,
                owner_id=ids.owner_id,
                name="旁白克隆",
                remark="测试",
                source_object_key=f"cloned-voices/{ids.channel_id}/{voice_id}/source.wav",
                source_sha256="a" * 64,
                source_size_bytes=128,
                source_media_type="audio/wav",
                created_at=NOW,
            )
        )
        repository.save_cloned_voice_account(
            ClonedVoiceAccount(voice_id, key.id, "el-voice-1", NOW)
        )

    with postgres_database.sessions() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        loaded = repository.cloned_voice_by_id(voice_id)
        assert loaded is not None and loaded.name == "旁白克隆"
        assert repository.cloned_voice(ids.channel_id, voice_id) is not None
        mapping = repository.cloned_voice_account(voice_id, key.id)
        assert mapping is not None and mapping.el_voice_id == "el-voice-1"
        accounts = repository.list_cloned_voice_accounts(voice_id)
        assert [item.cloned_voice_id for item in accounts] == [voice_id]
        # 未被任何引用：可删除（第三类引用计数全为 0）。
        assert repository.cloned_voice_usage(voice_id).protected is False

    # 设为频道默认 → 第三类引用生效，手动删除被守卫拒绝。
    with postgres_database.sessions.begin() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        channel = repository.channel(ids.owner_id, ids.channel_id)
        assert channel is not None
        repository.save_channel(
            Channel(
                id=channel.id,
                owner_id=channel.owner_id,
                url=channel.url,
                default_voice_version_id=channel.default_voice_version_id,
                default_character_image_ids=channel.default_character_image_ids,
                name=channel.name,
                default_tts_provider=TtsProvider.ELEVENLABS.value,
                default_cloned_voice_id=voice_id,
            )
        )

    with postgres_database.sessions() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        usage = repository.cloned_voice_usage(voice_id)
        assert usage.channel_default_references == 1
        assert usage.reason_code == "cloned_voice_channel_default"
        channel = repository.channel(ids.owner_id, ids.channel_id)
        assert channel is not None
        assert channel.default_tts_provider == TtsProvider.ELEVENLABS.value
        assert channel.default_cloned_voice_id == voice_id

    with postgres_database.sessions.begin() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        assert repository.forget_cloned_voice_account(voice_id, key.id) is None
        assert repository.cloned_voice_account(voice_id, key.id) is None
        assert repository.delete_cloned_voice(ids.channel_id, voice_id) is True


@pytest.mark.integration
def test_elevenlabs_project_and_snapshot_facts_round_trip(
    postgres_database, production_parent_chain
) -> None:
    ids = production_parent_chain
    cloned_voice_id = uuid4()
    with postgres_database.sessions.begin() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        repository.add_cloned_voice(
            ClonedVoice(
                id=cloned_voice_id,
                channel_id=ids.channel_id,
                owner_id=ids.owner_id,
                name="EL 旁白",
                remark="",
                source_object_key=f"cloned-voices/{ids.channel_id}/{cloned_voice_id}/source.mp3",
                source_sha256="b" * 64,
                source_size_bytes=64,
                source_media_type="audio/mpeg",
                created_at=NOW,
            )
        )

    with postgres_database.sessions.begin() as session:
        session.add(
            ProjectRow(
                id=uuid4(),
                owner_id=ids.owner_id,
                channel_id=ids.channel_id,
                title="EL 项目",
                script="A complete ElevenLabs fixture.",
                character_image_ids=[ids.character_id],
                visual_style="documentary",
                tts_provider=TtsProvider.ELEVENLABS.value,
                voice_version_id=None,
                cloned_voice_id=cloned_voice_id,
                locale="en-US",
                aspect_ratio="16:9",
                draft_revision=1,
                production_state="ACTIVE",
                production_revision=1,
            )
        )

    with postgres_database.sessions() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        projects = repository.list_projects(ids.owner_id)
        elevenlabs = [
            item for item in projects if item.tts_provider == TtsProvider.ELEVENLABS.value
        ]
        assert len(elevenlabs) == 1
        assert elevenlabs[0].voice_version_id is None
        assert elevenlabs[0].cloned_voice_id == cloned_voice_id

    # 快照事实：EL 分支只写逻辑音色三列，三件媒体事实保持 NULL。
    with postgres_database.engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT tts_provider, voice_version_id, cloned_voice_id FROM projects "
                "WHERE cloned_voice_id = :voice"
            ),
            {"voice": cloned_voice_id},
        ).one()
        assert row.tts_provider == TtsProvider.ELEVENLABS.value
        assert row.voice_version_id is None
        assert row.cloned_voice_id == cloned_voice_id

    inspection = inspect(postgres_database.engine)
    columns = {column["name"]: column for column in inspection.get_columns("projects")}
    assert columns["snapshot_cloned_voice_source_sha256"]["nullable"] is True
    assert columns["snapshot_voice_reference_asset_id"]["nullable"] is True

    # 频道行默认值与既有项目不回填：既有（CosyVoice）项目仍持有公共音色。
    with postgres_database.sessions() as session:
        repository = SqlAlchemyIdentityProjectRepository(session)
        cosyvoice = [
            item
            for item in repository.list_projects(ids.owner_id)
            if item.tts_provider == TtsProvider.COSYVOICE.value
        ]
        assert cosyvoice and cosyvoice[0].voice_version_id is not None

    with postgres_database.engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == migration_head()
        )
        default_provider = connection.scalar(
            text("SELECT default_tts_provider FROM channels WHERE id = :id"),
            {"id": ids.channel_id},
        )
        assert default_provider == TtsProvider.COSYVOICE.value
