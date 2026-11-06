from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from database_guard import refuse_application_database
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker

from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    CharacterImageRow,
    ProjectRow,
    VoiceCatalogImportRow,
    VoiceVersionRow,
)

V3_UNIT_COLUMNS = (
    "id, run_id, owner_id, project_id, ordinal, sentence_id, sentence_sequence, "
    "sentence_text, sentence_sha256, context_before_json, context_after_json, "
    "provider_idempotency_key, canonical_request_sha256, prompt_ref, prompt_sha256, "
    "prompt_content, required_locale, model, parameters_json, status, state_revision, "
    "lease_token, lease_until, provider_receipt, raw_response_sha256, "
    "response_diagnostics_json, shots_json, links_json, failure_code, "
    "reused_from_unit_id, created_at"
)


def _schema_url(database_url: str, schema: str) -> str:
    parsed: URL = make_url(database_url)
    query = dict(parsed.query)
    query["options"] = f"-csearch_path={schema}"
    return parsed.set(query=query).render_as_string(hide_password=False)


def _alembic_config(schema_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("prepend_sys_path", str(backend_root / "src"))
    config.attributes["database_url"] = schema_url
    return config


def _add_parent_chain(session: object, ids: dict[str, UUID]) -> None:
    session.add(
        AccountRow(
            id=ids["owner_id"],
            username=f"pg-owner-{ids['owner_id'].hex}",
            password_hash="test-password-hash",
            role="USER",
            active=True,
            session_generation=1,
        )
    )
    session.flush()
    session.execute(
        text(
            "INSERT INTO channels (id, owner_id, url) "
            "VALUES (CAST(:id AS UUID), CAST(:owner_id AS UUID), :url)"
        ),
        {
            "id": ids["channel_id"],
            "owner_id": ids["owner_id"],
            "url": "https://x.test",
        },
    )
    session.add(
        CharacterImageRow(
            id=ids["character_id"],
            owner_id=ids["owner_id"],
            channel_id=ids["channel_id"],
            asset_id=uuid4(),
            object_key=f"characters/{ids['character_id']}.png",
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
            id=ids["voice_id"],
            voice_id=f"voice-{ids['voice_id'].hex}",
            version=1,
            display_name="Migration contract voice",
            reference_zip_asset_id=uuid4(),
            reference_zip_object_key=f"voices/{ids['voice_id']}/reference.zip",
            reference_zip_media_type="application/zip",
            reference_zip_size_bytes=128,
            preview_asset_id=uuid4(),
            preview_object_key=f"voices/{ids['voice_id']}/preview.wav",
            preview_media_type="audio/wav",
            preview_size_bytes=128,
            avatar_asset_id=uuid4(),
            avatar_object_key=f"voices/{ids['voice_id']}/avatar.png",
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
        ProjectRow.__table__.insert().values(
            id=ids["project_id"],
            owner_id=ids["owner_id"],
            channel_id=ids["channel_id"],
            title="Migration backfill project",
            script="Sentence one.",
            character_image_ids=[ids["character_id"]],
            visual_style="documentary",
            voice_version_id=ids["voice_id"],
            locale="en-US",
            aspect_ratio="16:9",
            draft_revision=1,
            production_state="ACTIVE",
            production_revision=1,
        )
    )


@pytest.mark.integration
def test_0018_backfills_v3_units_into_global_plan_unit_shape() -> None:
    database_url = os.environ.get("TUBEFLOW_TEST_POSTGRES_URL")
    if not database_url:
        pytest.fail(
            "TUBEFLOW_TEST_POSTGRES_URL must name an explicitly isolated PostgreSQL database"
        )
    schema = f"contract_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    refuse_application_database(admin_engine)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    schema_url = _schema_url(database_url, schema)
    engine = create_engine(schema_url, pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    ids = {name: uuid4() for name in ("owner_id", "channel_id", "character_id", "voice_id")}
    ids["project_id"] = uuid4()
    run_id = uuid4()
    unit_ids = [uuid4() for _ in range(4)]
    sentence_ids = [uuid4() for _ in range(4)]
    created_at = datetime(2026, 8, 31, tzinfo=UTC)
    try:
        command.upgrade(_alembic_config(schema_url), "0017")
        with sessions.begin() as session:
            _add_parent_chain(session, ids)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO project_director_runs ("
                    "id, owner_id, project_id, trigger, idempotency_key, "
                    "canonical_input_sha256, input_json, snapshot_sha256, snapshot_revision, "
                    "model, parameters_json, status, next_unit_ordinal, total_units, "
                    "state_revision, revision_id, failure_code, retry_blocked, "
                    "recovery_source_run_id, created_at"
                    ") VALUES ("
                    ":id, :owner_id, :project_id, 'NARRATION_CONFIRMATION', 'v3-run', "
                    ":canonical, '{}'::json, :snapshot, 1, "
                    "'gpt-5.6-sol', '{}'::json, 'FAILED', 3, 4, "
                    "4, NULL, 'director_response_content_json_invalid', false, "
                    "NULL, :created_at)"
                ),
                {
                    "id": run_id,
                    "owner_id": ids["owner_id"],
                    "project_id": ids["project_id"],
                    "canonical": "a" * 64,
                    "snapshot": "b" * 64,
                    "created_at": created_at,
                },
            )
            units = [
                # Succeeded semantic unit with a v3 context link in links_json.
                (
                    unit_ids[0],
                    1,
                    sentence_ids[0],
                    1,
                    "Sentence one.",
                    "1" * 64,
                    "SUCCEEDED",
                    "receipt-1",
                    "1" * 64,
                    [
                        {
                            "shot_id": str(sentence_ids[0]),
                            "order": 1,
                            "scene": "scene-1",
                            "shot_size": "WIDE",
                            "camera_viewpoint": "EYE_LEVEL",
                            "subject_action_outcome": "event-1",
                            "narrative_function": "function-1",
                            "description": "description-1",
                        }
                    ],
                    # Dead v3 payload: links a sentence outside the unit itself.
                    [
                        {"shot_id": str(sentence_ids[0]), "sentence_id": str(sentence_ids[0])},
                        {"shot_id": str(sentence_ids[0]), "sentence_id": str(sentence_ids[1])},
                    ],
                    None,
                ),
                # Succeeded markdown separator unit.
                (
                    unit_ids[1],
                    2,
                    sentence_ids[1],
                    2,
                    "---",
                    "2" * 64,
                    "SUCCEEDED",
                    "internal:semantic-section-context-v3:nonvisual-separator",
                    "2" * 64,
                    [],
                    [],
                    None,
                ),
                # Failed semantic unit keeps its failure code as last_failure_code.
                (
                    unit_ids[2],
                    3,
                    sentence_ids[2],
                    3,
                    "Sentence three.",
                    "3" * 64,
                    "FAILED",
                    "provider-failed-receipt",
                    "d" * 64,
                    [],
                    [],
                    "director_response_content_json_invalid",
                ),
                # Unsent unit never attempted the provider.
                (
                    unit_ids[3],
                    4,
                    sentence_ids[3],
                    4,
                    "Sentence four.",
                    "4" * 64,
                    "INTENT_PERSISTED",
                    None,
                    None,
                    [],
                    [],
                    None,
                ),
            ]
            for (
                unit_id,
                ordinal,
                sentence_id,
                sequence,
                unit_text,
                text_sha,
                status,
                receipt,
                raw_sha,
                shots,
                links,
                failure_code,
            ) in units:
                connection.execute(
                    text(
                        f"INSERT INTO project_director_units ({V3_UNIT_COLUMNS}) VALUES ("
                        ":id, :run_id, :owner_id, :project_id, :ordinal, :sentence_id, "
                        ":sequence, :text, :text_sha, '[]'::json, '[]'::json, "
                        ":provider_key, :canonical, 'project-director-semantic-section-v3', "
                        ":prompt_sha, 'prompt', 'en-US', 'gpt-5.6-sol', '{}'::json, "
                        ":status, 1, NULL, NULL, :receipt, :raw_sha, '{}'::json, "
                        "CAST(:shots AS json), CAST(:links AS json), "
                        ":failure_code, NULL, :created_at)"
                    ),
                    {
                        "id": unit_id,
                        "run_id": run_id,
                        "owner_id": ids["owner_id"],
                        "project_id": ids["project_id"],
                        "ordinal": ordinal,
                        "sentence_id": sentence_id,
                        "sequence": sequence,
                        "text": unit_text,
                        "text_sha": text_sha,
                        "provider_key": f"{ordinal}" * 64,
                        "canonical": f"{ordinal}" * 64,
                        "prompt_sha": f"{ordinal}" * 64,
                        "status": status,
                        "receipt": receipt,
                        "raw_sha": raw_sha,
                        "shots": json.dumps(shots),
                        "links": json.dumps(links),
                        "failure_code": failure_code,
                        "created_at": created_at,
                    },
                )

        command.upgrade(_alembic_config(schema_url), "head")

        with engine.begin() as connection:
            run_row = connection.execute(
                text(
                    "SELECT auto_retries_used, failure_code FROM project_director_runs "
                    "WHERE id = :id"
                ),
                {"id": run_id},
            ).one()
            unit_rows = {
                row.ordinal: row
                for row in connection.execute(
                    text(
                        "SELECT ordinal, kind, sentences_json, attempt_count, "
                        "last_failure_code, next_attempt_at, plan_document, "
                        "plan_document_sha256, links_json "
                        "FROM project_director_units WHERE run_id = :run_id"
                    ),
                    {"run_id": run_id},
                )
            }
            dropped = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name IN ('project_director_runs', 'project_director_units') "
                    "AND column_name IN ("
                    "'next_unit_ordinal', 'sentence_id', 'sentence_sequence', "
                    "'sentence_text', 'sentence_sha256')"
                )
            ).all()
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()

        assert version == ScriptDirectory.from_config(
            _alembic_config(schema_url)
        ).get_current_head()
        assert dropped == []
        assert run_row.auto_retries_used == 0
        assert run_row.failure_code == "director_response_content_json_invalid"
        first = unit_rows[1]
        assert first.kind == "SHOT_PLAN"
        assert first.sentences_json == [
            {
                "sentence_id": str(sentence_ids[0]),
                "sequence": 1,
                "text": "Sentence one.",
                "text_sha256": "1" * 64,
            }
        ]
        assert first.attempt_count == 1
        assert first.last_failure_code is None
        # Immutable dead v3 payload is preserved, never rewritten.
        assert first.links_json == [
            {"shot_id": str(sentence_ids[0]), "sentence_id": str(sentence_ids[0])},
            {"shot_id": str(sentence_ids[0]), "sentence_id": str(sentence_ids[1])},
        ]
        second = unit_rows[2]
        assert second.kind == "NONVISUAL_SEPARATOR"
        assert second.sentences_json[0]["text"] == "---"
        assert second.attempt_count == 1
        third = unit_rows[3]
        assert third.kind == "SHOT_PLAN"
        assert third.attempt_count == 1
        assert third.last_failure_code == "director_response_content_json_invalid"
        fourth = unit_rows[4]
        assert fourth.kind == "SHOT_PLAN"
        assert fourth.attempt_count == 0
        assert fourth.last_failure_code is None
        for row in unit_rows.values():
            assert row.next_attempt_at is None
            assert row.plan_document is None
            assert row.plan_document_sha256 is None
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
