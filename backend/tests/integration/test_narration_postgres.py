from __future__ import annotations

import asyncio
import hashlib
import io
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from database_guard import refuse_application_database
from fastapi.testclient import TestClient
from minio import Minio
from object_store_guard import refuse_application_object_store
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from tubeflow_test.adapters.object_storage import PrivateObjectStorage, StoredObject
from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    CharacterImageRow,
    ProjectSnapshotCharacterRow,
    VoiceVersionRow,
)
from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.adapters.persistence.models import (
    MediaAssetRow,
    ProjectAssetReferenceRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    PrivateNarrationAudioArchive,
    SqlNarrationRepository,
    SqlNarrationVideoGate,
)
from tubeflow_test.adapters.persistence.production_authority import canonical_video_authorities
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorRevisionRow,
    StoryboardCurrentRow,
    StoryboardGenerationRow,
    StoryboardMediaRow,
)
from tubeflow_test.api.app import create_app
from tubeflow_test.api.narration_runtime import SqlProjectSnapshotAdapter
from tubeflow_test.application.storyboards import (
    canonical_character_payload,
    canonical_storyboard_request_sha256,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import (
    AudioAsset,
    AudioQcResult,
    NarratedSentence,
    NarrationAttempt,
    NarrationAttemptState,
    build_measured_timeline,
    group_timeline,
    segment_pasted_script,
)
from tubeflow_test.identity_project.adapters import Argon2PasswordHasher
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.orchestration import AttemptStage, DurableGroupVideoOrchestrator
from tubeflow_test.video_generation.persistence import H3NodeRecord, VideoAttemptRecord
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_generation.worker import dispatch_video_outbox_once
from tubeflow_test.video_prompt.domain import VideoPromptStrategy
from tubeflow_test.video_prompt.fake_compiler import FakeVideoPromptCompiler


@pytest.fixture
def narration_database_url() -> str:
    postgres_url = os.environ.get("TUBEFLOW_TEST_POSTGRES_URL")
    if postgres_url is None:
        pytest.skip("isolated PostgreSQL is not configured")
    schema = f"narration_authority_{uuid4().hex}"
    admin_engine = create_engine(postgres_url)
    refuse_application_database(admin_engine)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    separator = "&" if "?" in postgres_url else "?"
    database_url = f"{postgres_url}{separator}options=-csearch_path%3D{schema}"
    try:
        yield database_url
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _remove_bucket(client: Minio, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        return
    for item in client.list_objects(bucket, recursive=True):
        client.remove_object(bucket, item.object_name)
    client.remove_bucket(bucket)


@pytest.mark.integration
def test_empty_postgres_authority_round_trip_after_migration(
    narration_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    database_url = narration_database_url
    minio_endpoint = os.environ.get("TUBEFLOW_TEST_MINIO_ENDPOINT")
    if minio_endpoint is None:
        pytest.skip("isolated PostgreSQL and MinIO are not configured")
    minio_access_key = os.environ.get("TUBEFLOW_TEST_MINIO_ACCESS_KEY")
    minio_secret_key = os.environ.get("TUBEFLOW_TEST_MINIO_SECRET_KEY")
    if not minio_access_key or not minio_secret_key:
        pytest.fail("real MinIO tests require explicit access and secret key environment variables")
    alembic = Config("alembic.ini")
    alembic.attributes["database_url"] = database_url
    command.upgrade(alembic, "head")
    engine = create_engine(database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    client = Minio(
        minio_endpoint,
        access_key=minio_access_key,
        secret_key=minio_secret_key,
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"narration-check-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_bucket()
    # 2026-11-06：此前从不删桶（通过时也留下一个 narration-check-*）；失败时同样要清理。
    request.addfinalizer(lambda: _remove_bucket(client, bucket))
    raw_bytes = b"raw-authority-bytes"
    normalized_bytes = b"normalized-authority-bytes"
    raw_object = storage.put(raw_bytes, "audio/wav")
    normalized_object = storage.put(normalized_bytes, "audio/wav")
    owner, project, channel, voice, character = (uuid4() for _ in range(5))
    raw_asset_id, normalized_asset_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id,username,password_hash,role,active,session_generation) "
                "VALUES (:id,:username,'hash','USER',true,1)"
            ),
            {"id": owner, "username": f"pg-owner-{owner}"},
        )
        connection.execute(
            text("INSERT INTO channels (id,owner_id,url) VALUES (:id,:owner,'http://channel')"),
            {"id": channel, "owner": owner},
        )
        connection.execute(
            text(
                "INSERT INTO character_images "
                "(id,owner_id,channel_id,asset_id,object_key,sha256,media_type,"
                "size_bytes,width,height) "
                "VALUES (:id,:owner,:channel,:asset,'character-key',:hash,"
                "'image/png',10,10,10)"
            ),
            {
                "id": character,
                "owner": owner,
                "channel": channel,
                "asset": uuid4(),
                "hash": "9" * 64,
            },
        )
        connection.execute(
            VoiceVersionRow.__table__.insert().values(
                id=voice,
                voice_id=f"integration-voice-{voice}",
                version=1,
                display_name="Integration Voice",
                reference_zip_asset_id=uuid4(),
                reference_zip_object_key="voice/reference.zip",
                reference_zip_media_type="application/zip",
                reference_zip_size_bytes=1,
                preview_asset_id=uuid4(),
                preview_object_key="voice/preview.wav",
                preview_media_type="audio/wav",
                preview_size_bytes=1,
                avatar_asset_id=uuid4(),
                avatar_object_key="voice/avatar.png",
                avatar_media_type="image/png",
                avatar_size_bytes=1,
                metadata_sha256="a" * 64,
                catalog_source_sha256="b" * 64,
                reference_zip_sha256="c" * 64,
                preview_sha256="d" * 64,
                avatar_sha256="e" * 64,
                supported_locales=["en-US"],
                raw_language_codes=["en"],
                catalog_metadata_json="{}",
                enabled=True,
            )
        )
        connection.execute(
            text(
                "INSERT INTO voice_catalog_imports "
                "(source_sha256,catalog_version,expected_count,imported_count,status,"
                "updated_at) "
                "VALUES (:source,1,1,1,'completed',:now)"
            ),
            {"source": "b" * 64, "now": datetime.now(UTC)},
        )
        connection.execute(
            text(
                "INSERT INTO projects "
                "(id,owner_id,channel_id,title,script,character_image_ids,visual_style,"
                "voice_version_id,locale,aspect_ratio) "
                "VALUES (:id,:owner,:channel,'It','One sentence.',ARRAY[:character]::uuid[],"
                "'style',:voice,'en-US','16:9')"
            ),
            {
                "id": project,
                "owner": owner,
                "channel": channel,
                "voice": voice,
                "character": character,
            },
        )
        for asset_id, kind, stored in (
            (raw_asset_id, "narration_raw", raw_object),
            (normalized_asset_id, "narration_normalized", normalized_object),
        ):
            connection.execute(
                text(
                    "INSERT INTO media_assets "
                    "(id,owner_id,project_id,kind,object_key,sha256,media_type,"
                    "size_bytes,metadata_json,shared) "
                    "VALUES (:id,:owner,:project,:kind,:key,:hash,'audio/wav',"
                    ":size,'{}'::jsonb,false)"
                ),
                {
                    "id": asset_id,
                    "owner": owner,
                    "project": project,
                    "kind": kind,
                    "key": stored.key,
                    "hash": stored.sha256,
                    "size": stored.size_bytes,
                },
            )

    def confirm() -> tuple[str, SupportedLocale, UUID, object]:
        return SqlProjectSnapshotAdapter(sessions).confirm_and_enqueue(owner, project)

    def concurrent_edit() -> None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE projects SET script='Concurrent sentence.', "
                    "draft_revision=draft_revision+1 "
                    "WHERE id=:project AND snapshot_script IS NULL"
                ),
                {"project": project},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        confirmed_future = executor.submit(confirm)
        edited_future = executor.submit(concurrent_edit)
        confirmed_text, confirmed_locale, confirmed_voice, _job = confirmed_future.result()
        edited_future.result()
    with engine.connect() as connection:
        snapshot_script, payload_script = connection.execute(
            text(
                "SELECT p.snapshot_script, n.payload->'script'->>'source_sha256' "
                "FROM projects p JOIN narration_project_records n ON n.project_id=p.id "
                "WHERE p.id=:project"
            ),
            {"project": project},
        ).one()
    script = segment_pasted_script(confirmed_text, confirmed_locale)
    assert snapshot_script == confirmed_text
    assert payload_script == script.source_sha256
    assert confirmed_voice == voice

    stale_project_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects "
                "(id,owner_id,channel_id,title,script,character_image_ids,visual_style,"
                "voice_version_id,locale,aspect_ratio) "
                "VALUES (:id,:owner,:channel,'Draft','Original draft.',ARRAY[:character]::uuid[],"
                "'style',:voice,'en-US','16:9')"
            ),
            {
                "id": stale_project_id,
                "owner": owner,
                "channel": channel,
                "voice": voice,
                "character": character,
            },
        )
    session_a = sessions()
    session_b = sessions()
    try:
        repository_a = SqlAlchemyIdentityProjectRepository(session_a)
        repository_b = SqlAlchemyIdentityProjectRepository(session_b)
        stale_draft = repository_a.project(owner, stale_project_id)
        current_draft = repository_b.project(owner, stale_project_id)
        assert stale_draft is not None and current_draft is not None
        winner = current_draft.edited(script="Committed winner.")
        assert repository_b.save_project(winner, current_draft.draft_revision)
        session_b.commit()
        stale_edit = stale_draft.edited(script="Stale overwrite.")
        assert not repository_a.save_project(stale_edit, stale_draft.draft_revision)
        session_a.rollback()
    finally:
        session_a.close()
        session_b.close()
    with engine.connect() as connection:
        editable_script, snapshot_script, draft_revision = connection.execute(
            text("SELECT script,snapshot_script,draft_revision FROM projects WHERE id=:project"),
            {"project": stale_project_id},
        ).one()
    assert editable_script == "Committed winner."
    assert snapshot_script is None
    assert draft_revision == 2

    repository = SqlNarrationRepository(sessions)
    sentence = script.sentences[0]
    key = "tts_" + "b" * 64
    request_hash = hashlib.sha256(b"request").hexdigest()
    attempt = NarrationAttempt(
        project,
        owner,
        1,
        key,
        request_hash,
        NarrationAttemptState.INTENT_RECORDED,
        "call-1",
    )
    repository.save_attempt(attempt)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda token: SqlNarrationRepository(sessions).claim_submission(
                    attempt, token, datetime.now(UTC) + timedelta(minutes=5)
                ),
                ("claim-a", "claim-b"),
            )
        )
    assert sum(value is not None for value in claims) == 1
    checkpoint_evidence = AudioAsset(str(uuid4()), "f" * 64, "application/zip", 123)
    started_attempt = repository.attempt(owner, key)
    assert started_attempt is not None
    repository.set_checkpoint_ledger(
        started_attempt,
        "ATTACHED",
        checkpoint_evidence,
    )
    qc = AudioQcResult(True, 1000, "pcm_wav", None, 48_000, 1, 2, 48_000)
    narrated = NarratedSentence(
        project,
        owner,
        voice,
        SupportedLocale.EN_US,
        "call-1",
        "c" * 64,
        sentence,
        key,
        AudioAsset(str(raw_asset_id), raw_object.sha256, "audio/wav", raw_object.size_bytes),
        AudioAsset(
            str(normalized_asset_id),
            normalized_object.sha256,
            "audio/wav",
            normalized_object.size_bytes,
        ),
        qc,
        qc,
        {"normalizer": "integration"},
    )
    repository.save_completed(
        replace(started_attempt, state=NarrationAttemptState.COMPLETED),
        narrated,
    )
    assert repository.provider_checkpoint(owner, key) == checkpoint_evidence
    timeline = build_measured_timeline(script, (narrated,))
    groups = group_timeline(timeline)
    repository.save_timeline(owner, project, timeline, groups)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "select count(*) from authoritative_narration_sentences "
                    "where project_id=:project"
                ),
                {"project": project},
            ).scalar_one()
            == 1
        )
        assert connection.execute(
            text(
                "select array_agg(m.ordinal order by m.ordinal) "
                "from authoritative_narration_memberships m "
                "join authoritative_narration_groups g on g.id=m.group_id "
                "where g.project_id=:project"
            ),
            {"project": project},
        ).scalar_one() == [1]
        constraints = {
            row[0]
            for row in connection.execute(
                text(
                    "select conname from pg_constraint where conrelid = "
                    "'authoritative_narration_memberships'::regclass and contype = 'u'"
                )
            )
        }
        assert "uq_authoritative_narration_memberships_sentence_id" in constraints
        assert "uq_authoritative_narration_memberships_group_ordinal" in constraints
        assert (
            connection.execute(
                text(
                    "select count(*) from authoritative_narration_groups where project_id=:project"
                ),
                {"project": project},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    "select count(*) from authoritative_narration_memberships m "
                    "join authoritative_narration_groups g on g.id=m.group_id "
                    "where g.project_id=:project"
                ),
                {"project": project},
            ).scalar_one()
            == 1
        )
    # Complete the same frozen project through the default production composition root.
    storyboard_bytes = b"contract-storyboard-image"
    character_bytes = b"contract-character-image"
    storyboard_object = storage.put(storyboard_bytes, "image/png")
    character_object = storage.put(character_bytes, "image/png")
    storyboard_asset_id, storyboard_generation_id, director_revision_id = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    with sessions.begin() as session:
        account = session.get_one(AccountRow, owner)
        account.password_hash = Argon2PasswordHasher().hash("production-e2e-password")
        account.role = "user"
        snapshot_character = session.scalar(
            select(ProjectSnapshotCharacterRow).where(
                ProjectSnapshotCharacterRow.project_id == project
            )
        )
        assert snapshot_character is not None
        group_id = session.scalar(
            select(AuthoritativeNarrationGroupRecord.id).where(
                AuthoritativeNarrationGroupRecord.project_id == project,
                AuthoritativeNarrationGroupRecord.owner_id == owner,
            )
        )
        assert group_id is not None
        character_source = session.get_one(CharacterImageRow, character)
        character_source.object_key = character_object.key
        character_source.sha256 = character_object.sha256
        character_source.media_type = "image/png"
        character_source.size_bytes = character_object.size_bytes
        character_row = session.get(MediaAssetRow, snapshot_character.asset_id)
        if character_row is None:
            character_row = MediaAssetRow(
                id=snapshot_character.asset_id,
                owner_id=owner,
                project_id=project,
                kind="character_image",
                object_key=character_object.key,
                sha256=character_object.sha256,
                media_type="image/png",
                size_bytes=character_object.size_bytes,
                metadata_json={},
            )
            session.add(character_row)
            session.add(
                ProjectAssetReferenceRow(
                    project_id=project,
                    asset_id=snapshot_character.asset_id,
                    owner_id=owner,
                )
            )
        else:
            character_row.object_key = character_object.key
            character_row.sha256 = character_object.sha256
            character_row.media_type = "image/png"
            character_row.size_bytes = character_object.size_bytes
        snapshot_character.sha256 = character_object.sha256
        snapshot_character.media_type = "image/png"
        snapshot_character.size_bytes = character_object.size_bytes
        member_ids = tuple(
            session.scalars(
                select(text("sentence_id"))
                .select_from(text("authoritative_narration_memberships"))
                .order_by(text("ordinal"))
            )
        )
        assert member_ids
        shot_id = uuid4()
        shots = [{
            "shot_id": str(shot_id),
            "order": 1,
            "scene": "A documentary scene",
            "shot_size": "WIDE",
            "camera_viewpoint": "EYE_LEVEL",
            "subject_action_outcome": "The subject completes a distinct event",
            "narrative_function": "Establish the event",
            "description": "A distinct canonical event",
        }]
        links = [
            {"shot_id": str(shot_id), "sentence_id": str(sentence_id)}
            for sentence_id in member_ids
        ]
        characters = [
            canonical_character_payload(
                ordinal=snapshot_character.ordinal + 1,
                character_image_id=snapshot_character.character_image_id,
                asset_id=snapshot_character.asset_id,
                sha256=snapshot_character.sha256,
                media_type=snapshot_character.media_type,
                size_bytes=snapshot_character.size_bytes,
            )
        ]
        prompt = (
            "Character reference one; storyboard reference two; "
            "ambient sound only, no dialogue or music."
        )
        parameters = {"seed": 17}
        canonical_hash = canonical_storyboard_request_sha256(
            owner_id=owner,
            group_id=group_id,
            revision_id=director_revision_id,
            prompt=prompt,
            characters=characters,
            style="style",
            model="grid-sheet-v1",
            parameters=parameters,
        )
        session.add(
            DirectorRevisionRow(
                id=director_revision_id,
                owner_id=owner,
                project_id=project,
                group_id=group_id,
                revision_number=1,
                kind="APPLIED",
                shots_json=shots,
                links_json=links,
                group_sentence_ids_json=[str(value) for value in member_ids],
                review_state="NOT_REQUIRED",
                continuous_observation_approvals_json=[],
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            MediaAssetRow(
                id=storyboard_asset_id,
                owner_id=owner,
                project_id=project,
                kind="STORYBOARD_GRID",
                object_key=storyboard_object.key,
                sha256=storyboard_object.sha256,
                media_type="image/png",
                size_bytes=storyboard_object.size_bytes,
                metadata_json={},
            )
        )
        session.add(
            ProjectAssetReferenceRow(
                project_id=project,
                asset_id=storyboard_asset_id,
                owner_id=owner,
            )
        )
        session.add(
            StoryboardGenerationRow(
                id=storyboard_generation_id,
                owner_id=owner,
                project_id=project,
                group_id=group_id,
                applied_revision_id=director_revision_id,
                version=1,
                kind="GRID_SHEET",
                input_json={
                    "shots": shots,
                    "links": links,
                    "character_assets": characters,
                    "style": "style",
                    "prompt": prompt,
                    "model": "grid-sheet-v1",
                    "parameters": parameters,
                },
                idempotency_key="storyboard-production-e2e",
                provider_idempotency_key="a" * 64,
                canonical_request_sha256=canonical_hash,
                status="SUCCEEDED",
                state_revision=1,
                created_at=datetime.now(UTC),
                asset_id=storyboard_asset_id,
                checkpoint_json={
                    "object_asset": {
                        "asset_id": str(storyboard_asset_id),
                        "sha256": storyboard_object.sha256,
                        "media_type": "image/png",
                        "size_bytes": storyboard_object.size_bytes,
                    },
                    "expected_sha256": storyboard_object.sha256,
                    "downloaded_sha256": storyboard_object.sha256,
                    "downloaded_size": storyboard_object.size_bytes,
                    "qc_mime": "image/png",
                },
            )
        )
        session.flush()
        session.add(
            StoryboardMediaRow(
                generation_id=storyboard_generation_id,
                storyboard_asset_id=uuid4(),
                asset_id=storyboard_asset_id,
                width=1024,
                height=1024,
                image_format="PNG",
            )
        )
        session.add(
            StoryboardCurrentRow(
                owner_id=owner,
                group_id=group_id,
                generation_id=storyboard_generation_id,
                version=1,
            )
        )

    workflow_manifest = b"production-e2e-workflow"
    settings = Settings(
        database_url=database_url,
        identity_setup_username="unused-e2e-bootstrap",
        identity_setup_password="unused-e2e-bootstrap-password",
        minio_endpoint=minio_endpoint,
        minio_access_key=minio_access_key,
        minio_secret_key=minio_secret_key,
        minio_bucket=bucket,
        h3_node_urls=("http://fake-1", "http://fake-2", "http://fake-3"),
        h3_node_ids=("e2e-1", "e2e-2", "e2e-3"),
        h3_node_instance_ids=("instance-1", "instance-2", "instance-3"),
        h3_node_tokens=("token-1", "token-2", "token-3"),
        h3_node_capacities=(1, 1, 1),
        h3_workflow_manifest_sha256=hashlib.sha256(workflow_manifest).hexdigest(),
    )
    compiler = FakeVideoPromptCompiler(VideoPromptStrategy(settings.video_prompt_strategy))
    monkeypatch.setattr(
        # app.py 按位置传 (settings, diagnostics)；替身须接受任意参数。
        "tubeflow_test.api.app.build_video_prompt_compiler", lambda *_args: compiler
    )
    app = create_app(settings)
    http = TestClient(app)
    login = http.post(
        "/identity/login",
        json={"username": f"pg-owner-{owner}", "password": "production-e2e-password"},
    )
    assert login.status_code == 200
    authorization = {"Authorization": f"Bearer {login.json()['token']}"}
    with sessions.begin() as session:
        for node in session.scalars(select(H3NodeRecord)):
            node.health = "READY"
            node.last_heartbeat_at = datetime.now(UTC)
    created = http.post(
        f"/group-videos/groups/{group_id}/generations",
        headers={**authorization, "Idempotency-Key": "manual-production-e2e"},
        json={"expected_storyboard_generation_id": str(storyboard_generation_id)},
    )
    assert created.status_code == 202, created.text
    assert len(compiler.inputs) == 1
    attempt_id = UUID(created.json()["attempt_id"])

    class TemporalContract:
        calls = 0

        async def start_workflow(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1

    temporal = TemporalContract()
    assert asyncio.run(
        dispatch_video_outbox_once(temporal, sessions, task_queue="contract-h3")  # type: ignore[arg-type]
    ) == 1
    assert temporal.calls == 1
    with sessions.begin() as session:
        for node in session.scalars(select(H3NodeRecord)):
            node.health = "READY"

    class ContractAssets:
        def read_verified(self, asset):  # type: ignore[no-untyped-def]
            values = {
                raw_object.sha256: raw_bytes,
                normalized_object.sha256: normalized_bytes,
                storyboard_object.sha256: storyboard_bytes,
                character_object.sha256: character_bytes,
            }
            return values[asset.sha256]

    class Probe:
        def inspect(self, _content: bytes):  # type: ignore[no-untyped-def]
            raise AssertionError("probe is outside submit-only production E2E")

    _, _, loader = canonical_video_authorities(settings)
    video_repository = SqlAlchemyVideoRepository(sessions, loader, lambda value: value)
    fake_engine = FakeEngine()
    submitted = DurableGroupVideoOrchestrator(
        video_repository, fake_engine, ContractAssets(), Probe()  # type: ignore[arg-type]
    ).advance(owner, attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    assert sum(call == "submit" for _, call in fake_engine.calls) == 1
    round_trip = video_repository.get_attempt(attempt_id)
    assert round_trip is not None and round_trip.input_snapshot is not None
    assert round_trip.input_snapshot["storyboard_generation_id"] == str(
        storyboard_generation_id
    )
    assert round_trip.input_snapshot["authority"]["group"]["memberships"] == [  # type: ignore[index]
        {"ordinal": 1, "sentence_id": str(member_ids[0])}
    ]
    with sessions() as session:
        persisted = session.get_one(VideoAttemptRecord, attempt_id)
        assert persisted.owner_id == owner and persisted.project_id == project

    gate = SqlNarrationVideoGate(sessions, storage)
    gate.assert_project_video_allowed(project)
    client.put_object(
        bucket,
        normalized_object.key,
        io.BytesIO(b"tampered-normalized"),
        len(b"tampered-normalized"),
        content_type="audio/wav",
    )
    with pytest.raises(DomainError, match="integrity"):
        gate.assert_project_video_allowed(project)
    storage.delete_exact(normalized_object.key)
    with pytest.raises(DomainError, match="missing"):
        gate.assert_project_video_allowed(project)

    if minio_endpoint is not None:
        archive = PrivateNarrationAudioArchive(storage, sessions)
        checkpoint_metadata = {
            "provider_request_key": key,
            "manifest": [f"0001-{sentence.text_sha256[:12]}.wav"],
        }
        checkpoint = archive.save(
            owner_id=owner,
            project_id=project,
            kind="narration_provider_zip",
            payload=b"provider-zip-evidence",
            metadata=checkpoint_metadata,
        )
        assert (
            archive.save(
                owner_id=owner,
                project_id=project,
                kind="narration_provider_zip",
                payload=b"provider-zip-evidence",
                metadata=checkpoint_metadata,
            )
            == checkpoint
        )

        commit_loss_payload = b"commit-response-loss"
        commit_loss_metadata = {
            "provider_request_key": f"{key}:commit-loss",
            "manifest": [f"0001-{sentence.text_sha256[:12]}.wav"],
        }
        class CountingStorage:
            def __init__(self, wrapped: PrivateObjectStorage) -> None:
                self.wrapped = wrapped
                self.put_count = 0
                self.verified_count = 0

            def put_at_key(
                self, key: str, body: bytes, media_type: str, expected_sha256: str
            ) -> StoredObject:
                self.put_count += 1
                return self.wrapped.put_at_key(key, body, media_type, expected_sha256)

            def get_verified(
                self, key: str, expected_sha256: str, expected_size: int
            ) -> bytes:
                self.verified_count += 1
                return self.wrapped.get_verified(key, expected_sha256, expected_size)

            def __getattr__(self, name: str) -> object:
                return getattr(self.wrapped, name)

        counted_storage = CountingStorage(storage)

        def lose_final_archive_commit() -> None:
            raise RuntimeError("database commit response lost")

        commit_loss_archive = PrivateNarrationAudioArchive(
            counted_storage,  # type: ignore[arg-type]
            sessions,
            after_final_commit=lose_final_archive_commit,
        )
        with pytest.raises(RuntimeError, match="response lost"):
            commit_loss_archive.save(
                owner_id=owner,
                project_id=project,
                kind="narration_provider_zip",
                payload=commit_loss_payload,
                metadata=commit_loss_metadata,
            )
        commit_loss_sha256 = hashlib.sha256(commit_loss_payload).hexdigest()
        with sessions() as session:
            committed_assets = tuple(
                session.scalars(
                    select(MediaAssetRow).where(
                        MediaAssetRow.owner_id == owner,
                        MediaAssetRow.project_id == project,
                        MediaAssetRow.kind == "narration_provider_zip",
                        MediaAssetRow.sha256 == commit_loss_sha256,
                    )
                )
            )
            assert len(committed_assets) == 1
            committed_stage = session.get(
                StagedObjectRow, UUID(committed_assets[0].metadata_json["staging_journal_id"])
            )
            assert committed_stage is not None
            assert committed_stage.state == "FINALIZED"
        assert counted_storage.put_count == 1
        recovered = commit_loss_archive.save(
            owner_id=owner,
            project_id=project,
            kind="narration_provider_zip",
            payload=commit_loss_payload,
            metadata=commit_loss_metadata,
        )
        assert recovered.asset_id == str(committed_assets[0].id)
        assert counted_storage.put_count == 1
        assert counted_storage.verified_count == 1

        raw_metadata = {
            "idempotency_key": f"{key}:partial",
            "sentence_sequence": sentence.sequence,
        }
        raw = archive.save(
            owner_id=owner,
            project_id=project,
            kind="narration_raw",
            payload=raw_bytes,
            metadata=raw_metadata,
        )
        with pytest.raises(ValueError, match="empty"):
            archive.save(
                owner_id=owner,
                project_id=project,
                kind="narration_normalized",
                payload=b"",
                metadata=raw_metadata,
            )
        assert (
            archive.save(
                owner_id=owner,
                project_id=project,
                kind="narration_raw",
                payload=raw_bytes,
                metadata=raw_metadata,
            )
            == raw
        )
        normalized = archive.save(
            owner_id=owner,
            project_id=project,
            kind="narration_normalized",
            payload=normalized_bytes,
            metadata=raw_metadata,
        )

        other_owner, other_project, other_channel = uuid4(), uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO accounts "
                    "(id,username,password_hash,role,active,session_generation) "
                    "VALUES (:id,:username,'hash','USER',true,1)"
                ),
                {"id": other_owner, "username": f"archive-owner-{other_owner}"},
            )
            connection.execute(
                text("INSERT INTO channels (id,owner_id,url) VALUES (:id,:owner,'http://other')"),
                {"id": other_channel, "owner": other_owner},
            )
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id,owner_id,channel_id,title,script,character_image_ids,visual_style,"
                    "voice_version_id,locale,aspect_ratio) "
                    "VALUES (:id,:owner,:channel,'Other','One sentence.',"
                    "ARRAY[]::uuid[],'style',:voice,'en-US','16:9')"
                ),
                {
                    "id": other_project,
                    "owner": other_owner,
                    "channel": other_channel,
                    "voice": voice,
                },
            )
        isolated = archive.save(
            owner_id=other_owner,
            project_id=other_project,
            kind="narration_raw",
            payload=raw_bytes,
            metadata=raw_metadata,
        )
        assert isolated.asset_id != raw.asset_id

        concurrent_metadata = {
            "idempotency_key": f"{key}:concurrent",
            "sentence_sequence": sentence.sequence,
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent = tuple(
                executor.map(
                    lambda _index: PrivateNarrationAudioArchive(storage, sessions).save(
                        owner_id=owner,
                        project_id=project,
                        kind="narration_raw",
                        payload=b"concurrent-raw",
                        metadata=concurrent_metadata,
                    ),
                    range(2),
                )
            )
        assert concurrent[0] == concurrent[1]
        with sessions() as session_scope:
            ids = {
                UUID(item.asset_id)
                for item in (recovered, raw, normalized, concurrent[0], isolated)
            }
            assert (
                session_scope.scalar(
                    select(func.count()).select_from(MediaAssetRow).where(MediaAssetRow.id.in_(ids))
                )
                == 5
            )
            assert (
                session_scope.scalar(
                    select(func.count())
                    .select_from(MediaAssetRow)
                    .where(MediaAssetRow.id == UUID(raw.asset_id))
                )
                == 1
            )
        assert (
            archive.load(owner_id=owner, project_id=project, asset_id=checkpoint.asset_id)
            == b"provider-zip-evidence"
        )
        with sessions() as session:
            row = session.get(MediaAssetRow, UUID(checkpoint.asset_id))
            assert row is not None
            object_key = row.object_key
        client.put_object(
            bucket,
            object_key,
            io.BytesIO(b"tampered"),
            len(b"tampered"),
            content_type="application/zip",
        )
        with pytest.raises(DomainError, match="checkpoint"):
            archive.load(owner_id=owner, project_id=project, asset_id=checkpoint.asset_id)

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE projects SET production_state='PURGING', "
                "production_revision=production_revision+1 WHERE id=:id"
            ),
            {"id": project},
        )
    purging_attempt = NarrationAttempt(
        project,
        owner,
        1,
        "tts_" + "f" * 64,
        hashlib.sha256(b"purging").hexdigest(),
        NarrationAttemptState.INTENT_RECORDED,
        "call-purging",
    )
    with pytest.raises(DomainError, match="deleted"):
        repository.save_attempt(purging_attempt)
