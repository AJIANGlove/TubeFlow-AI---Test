from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    CharacterImageRow,
    VoiceCatalogImportRow,
    VoiceVersionRow,
)
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord


@dataclass(frozen=True)
class PostgresDatabase:
    engine: Engine
    sessions: sessionmaker[Session]
    url: str
    schema: str


@dataclass(frozen=True)
class ProductionParentChain:
    owner_id: UUID
    channel_id: UUID
    character_id: UUID
    voice_id: UUID
    project_id: UUID
    group_id: UUID


def _schema_url(database_url: str, schema: str) -> str:
    parsed: URL = make_url(database_url)
    query = dict(parsed.query)
    query["options"] = f"-csearch_path={schema}"
    return parsed.set(query=query).render_as_string(hide_password=False)


@pytest.fixture
def postgres_database(request: pytest.FixtureRequest) -> Iterator[PostgresDatabase]:
    database_url = os.environ.get("TUBEFLOW_TEST_POSTGRES_URL")
    if not database_url:
        pytest.fail(
            "TUBEFLOW_TEST_POSTGRES_URL must name an explicitly isolated PostgreSQL database; "
            "the PostgreSQL integration suite never reports an unconfigured environment as green"
        )
    schema = f"contract_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine: Engine | None = None
    # 2026-11-06：迁移失败或中断时也要删掉临时 schema（此前 upgrade 在 try 之外，留下过空 schema）。
    try:
        schema_url = _schema_url(database_url, schema)
        backend_root = Path(__file__).resolve().parents[2]
        config = Config(str(backend_root / "alembic.ini"))
        config.set_main_option("script_location", str(backend_root / "migrations"))
        config.set_main_option("prepend_sys_path", str(backend_root / "src"))
        config.attributes["database_url"] = schema_url
        command.upgrade(config, getattr(request, "param", "head"))
        engine = create_engine(schema_url, pool_pre_ping=True)
        yield PostgresDatabase(
            engine=engine,
            sessions=sessionmaker(engine, expire_on_commit=False),
            url=schema_url,
            schema=schema,
        )
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture
def production_parent_chain(postgres_database: PostgresDatabase) -> ProductionParentChain:
    ids = ProductionParentChain(*(uuid4() for _ in range(6)))
    with postgres_database.sessions.begin() as session:
        session.add(
            AccountRow(
                id=ids.owner_id,
                username=f"pg-owner-{ids.owner_id.hex}",
                password_hash="test-password-hash",
                role="USER",
                active=True,
                session_generation=1,
            )
        )
        session.flush()
        # 钉版迁移测试（0007/0018/0028 等）的模式里 channels 只有 0001 的基础列，
        # ORM 模型的 Python 端默认值会把后引入列（如 default_character_image_ids）
        # 带进 INSERT 必然失败；改用基础列 Core 写入，后续列由各版 server_default 承担。
        session.execute(
            text(
                "INSERT INTO channels (id, owner_id, url) "
                "VALUES (CAST(:id AS UUID), CAST(:owner_id AS UUID), :url)"
            ),
            {"id": ids.channel_id, "owner_id": ids.owner_id, "url": "https://example.test"},
        )
        session.add(
            CharacterImageRow(
                id=ids.character_id,
                owner_id=ids.owner_id,
                channel_id=ids.channel_id,
                asset_id=uuid4(),
                object_key=f"characters/{ids.character_id}.png",
                sha256="1" * 64,
                media_type="image/png",
                size_bytes=128,
                width=64,
                height=64,
            )
        )
        session.add(
            VoiceCatalogImportRow(
                source_sha256="3" * 64,
                catalog_version=1,
                expected_count=1,
                imported_count=1,
                status="completed",
                updated_at=datetime.now(UTC),
            )
        )
        session.add(
            VoiceVersionRow(
                id=ids.voice_id,
                voice_id=f"voice-{ids.voice_id.hex}",
                version=1,
                display_name="PostgreSQL contract voice",
                reference_zip_asset_id=uuid4(),
                reference_zip_object_key=f"voices/{ids.voice_id}/reference.zip",
                reference_zip_media_type="application/zip",
                reference_zip_size_bytes=128,
                preview_asset_id=uuid4(),
                preview_object_key=f"voices/{ids.voice_id}/preview.wav",
                preview_media_type="audio/wav",
                preview_size_bytes=128,
                avatar_asset_id=uuid4(),
                avatar_object_key=f"voices/{ids.voice_id}/avatar.png",
                avatar_media_type="image/png",
                avatar_size_bytes=128,
                metadata_sha256="2" * 64,
                catalog_source_sha256="3" * 64,
                reference_zip_sha256="4" * 64,
                preview_sha256="5" * 64,
                avatar_sha256="6" * 64,
                supported_locales=["en-US"],
                raw_language_codes=["en"],
                catalog_metadata_json="{}",
                enabled=True,
            )
        )
        session.flush()
        session.execute(
            text(
                "UPDATE channels SET default_voice_version_id = CAST(:voice AS UUID) "
                "WHERE id = CAST(:id AS UUID)"
            ),
            {"voice": ids.voice_id, "id": ids.channel_id},
        )
        # 钉版迁移测试的 projects 模式随版本增减列（如 title、cloned_voice_id、
        # 封面指针均在历史迁移中陆续加入）；按当前测试模式实际存在的列过滤写入，
        # 基础列自 0001/0007 起稳定存在。
        project_values: dict[str, object] = {
            "id": ids.project_id,
            "owner_id": ids.owner_id,
            "channel_id": ids.channel_id,
            "title": "PostgreSQL fixture project",
            "script": "A complete PostgreSQL fixture.",
            "character_image_ids": [ids.character_id],
            "visual_style": "documentary",
            "voice_version_id": ids.voice_id,
            "locale": "en-US",
            "aspect_ratio": "16:9",
            "draft_revision": 1,
            "production_state": "ACTIVE",
            "production_revision": 1,
        }
        existing_columns = {
            row[0]
            for row in session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'projects' AND table_schema = current_schema()"
                )
            )
        }
        filtered = {
            key: value for key, value in project_values.items() if key in existing_columns
        }
        session.execute(
            text(
                f"INSERT INTO projects ({', '.join(filtered)}) "
                f"VALUES ({', '.join(f':{key}' for key in filtered)})"
            ),
            filtered,
        )
        session.flush()
        session.add(
            AuthoritativeNarrationGroupRecord(
                id=ids.group_id,
                project_id=ids.project_id,
                owner_id=ids.owner_id,
                snapshot_sha256="7" * 64,
                snapshot_revision=1,
                sequence=1,
                start_frame=0,
                end_frame=48_000,
                sample_rate_hz=48_000,
                start_ms=0,
                end_ms=1_000,
                video_blocked=False,
                block_reason=None,
            )
        )
    return ids


@pytest.fixture
def storyboard_parent_chain(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> tuple[ProductionParentChain, UUID]:
    from tubeflow_test.adapters.persistence.storyboards.models import (
        DirectorRevisionRow,
        StoryboardGenerationRow,
    )

    revision_id, generation_id = uuid4(), uuid4()
    ids = production_parent_chain
    with postgres_database.sessions.begin() as session:
        session.add(
            DirectorRevisionRow(
                id=revision_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                group_id=ids.group_id,
                revision_number=1,
                kind="APPLIED",
                shots_json=[{"ordinal": 1, "description": "A distinct event"}],
                links_json=[],
                group_sentence_ids_json=[],
                review_state="APPROVED",
                continuous_observation_approvals_json=[],
                created_at=datetime.now(UTC),
            )
        )
        session.flush()
        session.add(
            StoryboardGenerationRow(
                id=generation_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                group_id=ids.group_id,
                applied_revision_id=revision_id,
                version=1,
                kind="GRID",
                input_json={},
                idempotency_key=f"storyboard-{generation_id.hex}",
                provider_idempotency_key=f"provider-{generation_id.hex}",
                canonical_request_sha256="8" * 64,
                status="SUCCEEDED",
                state_revision=1,
                created_at=datetime.now(UTC),
                checkpoint_json={},
            )
        )
    return ids, generation_id
