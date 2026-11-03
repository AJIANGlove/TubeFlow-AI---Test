from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    ProjectRow,
    VoiceVersionRow,
)
from tubeflow_test.adapters.persistence.schema import canonical_metadata


@pytest.mark.integration
def test_fresh_schema_matches_canonical_metadata(postgres_database) -> None:
    with postgres_database.engine.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(connection), canonical_metadata()
        )
    assert differences == []


@pytest.mark.integration
@pytest.mark.parametrize("postgres_database", ["0006"], indirect=True)
def test_0007_upgrades_legacy_owner_shared_storyboard_scope(postgres_database) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    config.attributes["database_url"] = postgres_database.url
    owner_id = uuid4()
    with postgres_database.sessions.begin() as session:
        session.add(AccountRow(
            id=owner_id,
            username=f"migration-owner-{owner_id.hex}",
            password_hash="test",
            role="USER",
            active=True,
            session_generation=1,
        ))

    asset_id = uuid4()
    values = {
        "id": asset_id,
        "owner": owner_id,
        "key": f"legacy-owner-shared/{asset_id}.png",
    }
    with postgres_database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO media_assets "
                "(id,owner_id,project_id,ownership_scope,kind,object_key,sha256,media_type,"
                "size_bytes,metadata_json,shared,purging,deletion_state,deletion_generation) "
                "VALUES (:id,:owner,NULL,'PROJECT','STORYBOARD_GRID',:key,:sha,'image/png',"
                "1,'{}'::jsonb,TRUE,FALSE,'ACTIVE',0)"
            ),
            {**values, "sha": "d" * 64},
        )

    command.upgrade(config, "head")
    with postgres_database.engine.begin() as connection:
        assert connection.scalar(
            text("SELECT ownership_scope FROM media_assets WHERE id = :id"), {"id": asset_id}
        ) == "OWNER"
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO media_assets "
                    "(id,owner_id,project_id,ownership_scope,kind,object_key,sha256,media_type,"
                    "size_bytes,metadata_json,shared,purging,deletion_state,deletion_generation) "
                    "VALUES (:id,:owner,NULL,'PROJECT','STORYBOARD_GRID',:key,:sha,'image/png',"
                    "1,'{}'::jsonb,TRUE,FALSE,'ACTIVE',0)"
                ),
                {
                    "id": uuid4(),
                    "owner": owner_id,
                    "key": f"invalid-owner-shared/{uuid4()}.png",
                    "sha": "e" * 64,
                },
            )


@pytest.mark.integration
@pytest.mark.parametrize("postgres_database", ["0007"], indirect=True)
def test_0007_round_trip_discards_nullable_staged_purge_manifest(postgres_database) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    config.attributes["database_url"] = postgres_database.url
    owner_id, channel_id, voice_id, project_id = (uuid4() for _ in range(4))
    with postgres_database.engine.begin() as connection:
        connection.execute(AccountRow.__table__.insert().values(
            id=owner_id,
            username=f"migration-owner-{owner_id.hex}",
            password_hash="test",
            role="USER",
            active=True,
            session_generation=1,
        ))
        connection.execute(text(
            "INSERT INTO channels (id, owner_id, url) "
            "VALUES (CAST(:id AS UUID), CAST(:owner_id AS UUID), :url)"
        ).bindparams(id=channel_id, owner_id=owner_id, url="https://migration.test"))
        connection.execute(VoiceVersionRow.__table__.insert().values(
            id=voice_id,
            voice_id=f"migration-{voice_id.hex}",
            version=1,
            display_name="Migration voice",
            reference_zip_asset_id=uuid4(),
            reference_zip_object_key=f"voices/{voice_id}/reference.zip",
            reference_zip_media_type="application/zip",
            reference_zip_size_bytes=1,
            preview_asset_id=uuid4(),
            preview_object_key=f"voices/{voice_id}/preview.wav",
            preview_media_type="audio/wav",
            preview_size_bytes=1,
            avatar_asset_id=uuid4(),
            avatar_object_key=f"voices/{voice_id}/avatar.png",
            avatar_media_type="image/png",
            avatar_size_bytes=1,
            metadata_sha256="1" * 64,
            catalog_source_sha256="2" * 64,
            reference_zip_sha256="3" * 64,
            preview_sha256="4" * 64,
            avatar_sha256="5" * 64,
            supported_locales=["en-US"],
            raw_language_codes=["en"],
            catalog_metadata_json="{}",
            enabled=True,
        ))
        connection.execute(ProjectRow.__table__.insert().values(
            id=project_id,
            owner_id=owner_id,
            channel_id=channel_id,
            script="Migration project.",
            character_image_ids=[],
            visual_style="documentary",
            voice_version_id=voice_id,
            locale="en-US",
            aspect_ratio="16:9",
        ))
        connection.execute(
            text(
                "INSERT INTO project_purge_jobs "
                "(project_id,owner_id,state,object_results,workflow_generation) "
                "VALUES (:project,:owner,'DELETING','[]'::jsonb,1)"
            ),
            {"project": project_id, "owner": owner_id},
        )
        connection.execute(
            text(
                "INSERT INTO project_purge_objects "
                "(project_id,object_key,asset_id,staged_object_id,object_kind,sha256,owner_id,"
                "external_reference_count,deletion_generation) "
                "VALUES (:project,:key,NULL,:staged,'STAGED',NULL,:owner,0,1)"
            ),
            {
                "project": project_id,
                "owner": owner_id,
                "staged": uuid4(),
                "key": f"staged-downgrade/{uuid4()}",
            },
        )

    command.downgrade(config, "0006")
    with postgres_database.engine.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM project_purge_objects")) == 0
    command.upgrade(config, "0007")
    with postgres_database.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={
            "include_object": lambda _obj, name, kind, _reflected, _compare_to: (
                kind != "table"
                or name in {"project_purge_jobs", "project_purge_objects", "purge_outbox"}
            ),
        })
        differences = compare_metadata(context, canonical_metadata())
    assert differences == []
