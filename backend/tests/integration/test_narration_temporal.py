from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

from tubeflow_test.adapters.persistence.identity_project.models import VoiceVersionRow
from tubeflow_test.adapters.persistence.narration import SqlNarrationRepository
from tubeflow_test.domain.narration import NarrationAttempt, NarrationAttemptState
from tubeflow_test.workflows.narration import TemporalNarrationStarter


@workflow.defn(sandboxed=False)
class LeaseExpiryWorkflow:
    @workflow.run
    async def run(self, data: tuple[str, str, str]) -> str:
        return await workflow.execute_activity(
            "lease_expiry_claim",
            args=list(data),
            start_to_close_timeout=timedelta(seconds=2),
            schedule_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=200),
                maximum_interval=timedelta(milliseconds=200),
                maximum_attempts=30,
            ),
        )


class LeaseExpiryActivities:
    def __init__(self, repository: SqlNarrationRepository) -> None:
        self.repository = repository

    @activity.defn(name="lease_expiry_claim")
    async def claim(self, owner_id: str, project_id: str, key: str) -> str:
        attempt = self.repository.attempt(UUID(owner_id), key)
        assert attempt is not None and attempt.project_id == UUID(project_id)
        claimed = self.repository.claim_submission(
            attempt, str(uuid4()), datetime.now(UTC) + timedelta(seconds=1)
        )
        if claimed is not None:
            raise RuntimeError("simulated worker crash after durable claim")
        current = self.repository.attempt(UUID(owner_id), key)
        assert current is not None
        if current.state is NarrationAttemptState.SUBMISSION_STARTED:
            raise RuntimeError("lease still active")
        return current.state.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporal_duplicate_and_starter_restart_reuse_stable_workflow() -> None:
    target = os.environ.get("TUBEFLOW_TEST_TEMPORAL_TARGET")
    if target is None:
        pytest.skip("isolated Temporal target is not configured")
    client = await Client.connect(target)
    job_id = f"narration-integration-{uuid4()}"
    first = TemporalNarrationStarter(client, task_queue="narration-integration-no-worker")
    await first.start(
        owner_id=UUID(int=1),
        project_id=UUID(int=2),
        sentence_sequences=(1, 2),
        job_id=job_id,
    )
    restarted_client = await Client.connect(target)
    restarted = TemporalNarrationStarter(
        restarted_client, task_queue="narration-integration-no-worker"
    )
    assert (
        await restarted.start(
            owner_id=UUID(int=1),
            project_id=UUID(int=2),
            sentence_sequences=(1, 2),
            job_id=job_id,
        )
        == job_id
    )
    handle = restarted_client.get_workflow_handle(job_id)
    description = await handle.describe()
    assert description.id == job_id
    await handle.terminate("integration test cleanup")
    closed_run_id = description.run_id
    assert (
        await restarted.start(
            owner_id=UUID(int=1),
            project_id=UUID(int=2),
            sentence_sequences=(1, 2),
            job_id=job_id,
        )
        == job_id
    )
    assert (await restarted_client.get_workflow_handle(job_id).describe()).run_id == closed_run_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_temporal_retry_observes_postgres_claim_lease_expiry() -> None:
    target = os.environ.get("TUBEFLOW_TEST_TEMPORAL_TARGET")
    database_url = os.environ.get("TUBEFLOW_TEST_POSTGRES_URL")
    if target is None or database_url is None:
        pytest.skip("isolated Temporal and PostgreSQL are not configured")
    engine = create_engine(database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, project, channel, voice = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO accounts(id,username,password_hash,role,active,session_generation) "
                "VALUES (:id,:username,'hash','USER',true,1)"
            ),
            {"id": owner, "username": f"lease-{owner}"},
        )
        connection.execute(
            text("INSERT INTO channels(id,owner_id,url) VALUES (:id,:owner,'http://lease')"),
            {"id": channel, "owner": owner},
        )
        connection.execute(
            VoiceVersionRow.__table__.insert().values(
                id=voice,
                voice_id=f"lease-{voice}",
                version=1,
                display_name="Lease Voice",
                reference_zip_asset_id=uuid4(),
                reference_zip_object_key=f"voice/{voice}/reference.zip",
                reference_zip_media_type="application/zip",
                reference_zip_size_bytes=1,
                preview_asset_id=uuid4(),
                preview_object_key=f"voice/{voice}/preview.wav",
                preview_media_type="audio/wav",
                preview_size_bytes=1,
                avatar_asset_id=uuid4(),
                avatar_object_key=f"voice/{voice}/avatar.png",
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
                "INSERT INTO projects(id,owner_id,channel_id,title,script,character_image_ids,"
                "visual_style,voice_version_id,locale,aspect_ratio) "
                "VALUES (:id,:owner,:channel,'Lease test','Lease sentence.',"
                "ARRAY[]::uuid[],'style',:voice,'en-US','16:9')"
            ),
            {"id": project, "owner": owner, "channel": channel, "voice": voice},
        )
    repository = SqlNarrationRepository(sessions)
    key = "lease_" + "b" * 64
    repository.save_attempt(
        NarrationAttempt(
            project,
            owner,
            1,
            key,
            "c" * 64,
            NarrationAttemptState.INTENT_RECORDED,
            "lease-call",
        )
    )
    client = await Client.connect(target)
    activities = LeaseExpiryActivities(repository)
    async with Worker(
        client,
        task_queue="narration-lease-integration",
        workflows=[LeaseExpiryWorkflow],
        activities=[activities.claim],
    ):
        result = await client.execute_workflow(
            LeaseExpiryWorkflow.run,
            (str(owner), str(project), key),
            id=f"lease-expiry-{uuid4()}",
            task_queue="narration-lease-integration",
        )
    assert result == NarrationAttemptState.REMOTE_STATE_UNKNOWN.value
