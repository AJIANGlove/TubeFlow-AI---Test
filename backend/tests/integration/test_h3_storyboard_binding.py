from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from conftest import PostgresDatabase, ProductionParentChain
from sqlalchemy import func, select, text, update

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.storyboards.models import (
    StoryboardCurrentRow,
    StoryboardGenerationRow,
    StoryboardMediaRow,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
)
from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink
from tubeflow_test.video_generation.domain import ImmutableWorkflowSnapshot, VideoRuleViolation
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.orchestration import (
    AttemptStage,
    AuthoritativeGroupInput,
    DurableGroupVideoOrchestrator,
    _derived_seed,
)
from tubeflow_test.video_generation.persistence import (
    H3NodeRecord,
    ManualH3IntentWriter,
    SqlVideoBatchJobRepository,
    SqlVideoBatchRepository,
    VideoAttemptRecord,
    VideoOutboxRecord,
)
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_prompt.fake_compiler import FakeVideoPromptCompiler


class Probe:
    def inspect(self, content: bytes):  # type: ignore[no-untyped-def]
        raise AssertionError("archive probe is outside this submit-path test")


def _writer(
    database: PostgresDatabase, *, seed: int | None = 1
) -> ManualH3IntentWriter:
    repository = SqlAlchemyStoryboardRepository(lambda: pytest.fail("caller transaction only"))

    def load(session, owner_id: UUID, group_id: UUID) -> AuthoritativeGroupInput:  # type: ignore[no-untyped-def]
        current = session.get(StoryboardCurrentRow, (owner_id, group_id))
        assert current is not None and current.generation_id is not None
        generation = session.get(StoryboardGenerationRow, current.generation_id)
        link = session.get(StoryboardMediaRow, current.generation_id)
        assert generation is not None and link is not None
        media = session.get(MediaAssetRow, link.asset_id)
        assert media is not None
        asset = MediaAssetRef(media.id, media.sha256, media.media_type, media.size_bytes)
        script = "fixture sentence"
        manifest_bytes = b'{"parameters":{"resolution_preset":"preview"}}\n'
        workflow = ImmutableWorkflowSnapshot(
            "h3", "1", "workflow", "1", hashlib.sha256(manifest_bytes).hexdigest(),
            "preview", {"parameters": {"resolution_preset": "preview"}},
        )
        shot = DirectorShot(
            uuid4(), 1, "scene", "WIDE", "OBJECTIVE", "action", "function", "description"
        )
        sentence_ids = (uuid4(), uuid4())
        return AuthoritativeGroupInput(
            owner_id, generation.project_id, group_id, generation.id, True,
            hashlib.sha256(script.encode()).hexdigest(), ("1" * 64,) * 2, ("1" * 64,) * 2,
            (1001, 1001), (True,) * 2, (asset,) * 2,
            ((0, 1001), (1001, 2001)), (0, 1), 0, 2001, 2001,
            (asset,), asset, "GRID_SHEET", True, generation.applied_revision_id,
            generation.applied_revision_id, "fixture prompt", AspectRatio.LANDSCAPE, seed,
            workflow, script, (0, 1), generation.version, (asset,) * 2, generation.id,
            asset.sha256, (24013,) * 2, (24000,) * 2,
            ((0, 24013), (24013, 48026)), 0, 48026, 24000,
            {
                "project_id": str(generation.project_id),
                "group_id": str(group_id),
                "storyboard_generation_id": str(generation.id),
            },
            director_shots=(shot,),
            shot_sentence_links=tuple(
                ShotSentenceLink(shot.shot_id, sentence_id) for sentence_id in sentence_ids
            ),
            group_sentence_ids=sentence_ids,
            visual_style="fixture style",
        )

    return ManualH3IntentWriter(
        database.sessions.begin,
        repository,
        load,
        prompt_compiler=FakeVideoPromptCompiler(),
    )


def _seed_current_media(
    database: PostgresDatabase,
    parents: ProductionParentChain,
    generation_id: UUID,
    *,
    make_current: bool = True,
) -> None:
    asset_id = uuid4()
    with database.sessions.begin() as session:
        session.add(
            MediaAssetRow(
                id=asset_id,
                owner_id=parents.owner_id,
                project_id=None,
                ownership_scope="OWNER",
                kind="STORYBOARD_GRID",
                object_key=f"storyboards/{generation_id}.png",
                sha256=hashlib.sha256(b"asset").hexdigest(),
                media_type="image/png",
                size_bytes=5,
                metadata_json={},
                shared=True,
            )
        )
        session.add(
            ProjectAssetReferenceRow(
                project_id=parents.project_id,
                asset_id=asset_id,
                owner_id=parents.owner_id,
            )
        )
        session.add(
            StoryboardMediaRow(
                generation_id=generation_id,
                storyboard_asset_id=uuid4(),
                asset_id=asset_id,
                width=1024,
                height=1024,
                image_format="PNG",
            )
        )
        if make_current:
            session.add(
                StoryboardCurrentRow(
                    owner_id=parents.owner_id,
                    group_id=parents.group_id,
                    generation_id=generation_id,
                    version=1,
                )
            )


@pytest.mark.integration
def test_h3_binding_fails_closed_when_project_is_purging(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    with postgres_database.sessions.begin() as session:
        session.add(
            StoryboardCurrentRow(
                owner_id=parents.owner_id,
                group_id=parents.group_id,
                generation_id=generation_id,
                version=1,
            )
        )
        session.execute(
            update(ProjectRow)
            .where(ProjectRow.id == parents.project_id)
            .values(production_state="PURGING")
        )

    writer = _writer(postgres_database)

    with pytest.raises(DomainError, match="purged"):
        writer.create(
            owner_id=parents.owner_id,
            group_id=parents.group_id,
            expected_storyboard_generation_id=generation_id,
            idempotency_key="h3-manual-click",
            request={"manual": True},
        )


@pytest.mark.integration
def test_stale_expected_generation_after_barrier_creates_no_attempt(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_a = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_a)
    with postgres_database.sessions.begin() as session:
        original = session.get(StoryboardGenerationRow, generation_a)
        assert original is not None
        generation_b = uuid4()
        session.add(
            StoryboardGenerationRow(
                id=generation_b,
                owner_id=parents.owner_id,
                project_id=parents.project_id,
                group_id=parents.group_id,
                applied_revision_id=original.applied_revision_id,
                version=2,
                kind="GRID",
                input_json={},
                idempotency_key=f"storyboard-{generation_b}",
                provider_idempotency_key=f"provider-{generation_b}",
                canonical_request_sha256="a" * 64,
                status="SUCCEEDED",
                state_revision=1,
                created_at=datetime.now(UTC),
                checkpoint_json={},
            )
        )
    validated = Barrier(2)
    switched = Barrier(2)

    def switch_current() -> None:
        validated.wait()
        with postgres_database.sessions.begin() as session:
            project = session.scalar(
                select(ProjectRow)
                .where(ProjectRow.id == parents.project_id)
                .with_for_update()
            )
            assert project is not None
            current = session.get(
                StoryboardCurrentRow, (parents.owner_id, parents.group_id)
            )
            assert current is not None
            current.generation_id = generation_b
            current.version = 2
        switched.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(switch_current)
        validated.wait()
        switched.wait()
        with pytest.raises(DomainError, match="current storyboard"):
            _writer(postgres_database).create(
                owner_id=parents.owner_id,
                group_id=parents.group_id,
                expected_storyboard_generation_id=generation_a,
                idempotency_key="stale-generation",
            )
        future.result()
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(VideoAttemptRecord)) == 0
        assert session.scalar(select(func.count()).select_from(VideoOutboxRecord)) == 0


@pytest.mark.integration
def test_project_purging_wins_barrier_and_creates_no_attempt_or_outbox(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    ready = Barrier(2)
    purged = Barrier(2)

    def begin_purge() -> None:
        ready.wait()
        with postgres_database.sessions.begin() as session:
            project = session.scalar(
                select(ProjectRow)
                .where(ProjectRow.id == parents.project_id)
                .with_for_update()
            )
            assert project is not None
            project.production_state = "PURGING"
        purged.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(begin_purge)
        ready.wait()
        purged.wait()
        with pytest.raises(DomainError, match="purged"):
            _writer(postgres_database).create(
                owner_id=parents.owner_id,
                group_id=parents.group_id,
                expected_storyboard_generation_id=generation_id,
                idempotency_key="purge-window",
            )
        future.result()
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(VideoAttemptRecord)) == 0
        assert session.scalar(select(func.count()).select_from(VideoOutboxRecord)) == 0


@pytest.mark.integration
def test_concurrent_same_manual_key_returns_one_attempt_and_outbox(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    barrier = Barrier(2)

    writer = _writer(postgres_database)

    def create() -> UUID:
        barrier.wait()
        return writer.create(
            owner_id=parents.owner_id,
            group_id=parents.group_id,
            expected_storyboard_generation_id=generation_id,
            idempotency_key="same-manual-key",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempt_ids = tuple(pool.map(lambda _index: create(), range(2)))
    assert attempt_ids[0] == attempt_ids[1]
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(VideoAttemptRecord)) == 1
        assert session.scalar(select(func.count()).select_from(VideoOutboxRecord)) == 1
    actual_repository = SqlAlchemyVideoRepository(
        postgres_database.sessions, writer._group_loader, lambda value: value  # type: ignore[attr-defined]
    )
    restored = actual_repository.get_attempt(attempt_ids[0])
    assert restored is not None and restored.input_snapshot is not None
    assert restored.input_snapshot["target_sample_count"] == 48026
    assert [
        item["frame_range"] for item in restored.input_snapshot["sentences"]  # type: ignore[union-attr]
    ] == [[0, 24013], [24013, 48026]]
    assert restored.request["parameters"]["duration_seconds"] == 3  # type: ignore[index]
    with postgres_database.sessions() as session:
        workflow = writer._group_loader(  # type: ignore[attr-defined]
            session, parents.owner_id, parents.group_id
        ).workflow
    with postgres_database.sessions.begin() as session:
        for index in range(3):
            session.add(
                H3NodeRecord(
                    node_id=f"e2e-node-{index}",
                    base_url=f"http://e2e-node-{index}",
                    token_secret_ref=f"e2e-token-{index}",
                    token_sha256="a" * 64,
                    capacity=1,
                    claimed_slots=0,
                    enabled=True,
                    health="READY",
                    capability="video.reference_images_to_video",
                    model_key=workflow.model_key,
                    model_version=workflow.model_version,
                    workflow_id=workflow.workflow_id,
                    workflow_version=workflow.workflow_version,
                    manifest_sha256=workflow.workflow_manifest_sha256,
                )
            )

    class Assets:
        def read_verified(self, asset: MediaAssetRef) -> bytes:
            assert asset.sha256 == hashlib.sha256(b"asset").hexdigest()
            return b"asset"

    engine = FakeEngine()
    service = DurableGroupVideoOrchestrator(actual_repository, engine, Assets(), Probe())  # type: ignore[arg-type]
    submitted = service.advance(parents.owner_id, attempt_ids[0])
    assert submitted.stage is AttemptStage.SUBMITTED, (
        submitted.error_code,
        submitted.retry_evidence,
        submitted.archive_stage,
    )
    assert sum(call == "submit" for _, call in engine.calls) == 1


@pytest.mark.integration
def test_concurrent_different_keys_create_only_one_active_attempt(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    barrier = Barrier(2)

    def create(key: str) -> UUID | DomainError:
        barrier.wait()
        try:
            return _writer(postgres_database).create(
                owner_id=parents.owner_id,
                group_id=parents.group_id,
                expected_storyboard_generation_id=generation_id,
                idempotency_key=key,
            )
        except DomainError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(create, ("click-a", "click-b")))
    assert sum(isinstance(item, UUID) for item in results) == 1
    assert sum(isinstance(item, DomainError) for item in results) == 1
    with postgres_database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(VideoAttemptRecord)) == 1
        assert session.scalar(select(func.count()).select_from(VideoOutboxRecord)) == 1


@pytest.mark.integration
def test_committed_key_retries_after_current_change_and_purge(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    writer = _writer(postgres_database)
    first = writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_id,
        idempotency_key="response-lost",
        request={"manual": True},
    )
    with postgres_database.sessions.begin() as session:
        project = session.get(ProjectRow, parents.project_id)
        current = session.get(StoryboardCurrentRow, (parents.owner_id, parents.group_id))
        assert project is not None and current is not None
        project.production_state = "PURGING"
        current.generation_id = None
    assert writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_id,
        idempotency_key="response-lost",
        request={"manual": True},
    ) == first
    with pytest.raises(DomainError, match="another request"):
        writer.create(
            owner_id=parents.owner_id,
            group_id=parents.group_id,
            expected_storyboard_generation_id=generation_id,
            idempotency_key="response-lost",
            request={"manual": False},
        )


def _submit_service(
    database: PostgresDatabase,
    writer: ManualH3IntentWriter,
    engine: FakeEngine,
    repository: SqlAlchemyVideoRepository | None = None,
) -> DurableGroupVideoOrchestrator:
    repository = repository or SqlAlchemyVideoRepository(
        database.sessions, writer._group_loader, lambda value: value  # type: ignore[attr-defined]
    )

    class Assets:
        def read_verified(self, asset: MediaAssetRef) -> bytes:
            del asset
            return b"asset"

    return DurableGroupVideoOrchestrator(repository, engine, Assets(), Probe())  # type: ignore[arg-type]


def _seed_submit_node(database: PostgresDatabase) -> None:
    manifest = b'{"parameters":{"resolution_preset":"preview"}}\n'
    with database.sessions.begin() as session:
        session.add(
            H3NodeRecord(
                node_id="barrier-node",
                base_url="http://barrier-node",
                token_secret_ref="barrier-token",
                token_sha256="a" * 64,
                capacity=1,
                claimed_slots=0,
                enabled=True,
                health="READY",
                capability="video.reference_images_to_video",
                model_key="h3",
                model_version="1",
                workflow_id="workflow",
                workflow_version="1",
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            )
        )


@pytest.mark.integration
def test_storyboard_switch_wins_group_fence_and_h3_never_posts(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_a = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_a)
    writer = _writer(postgres_database)
    _seed_submit_node(postgres_database)
    attempt_id = writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_a,
        idempotency_key="storyboard-wins-final-submit",
    )
    generation_b = uuid4()
    with postgres_database.sessions.begin() as session:
        original = session.get(StoryboardGenerationRow, generation_a)
        assert original is not None
        session.add(
            StoryboardGenerationRow(
                id=generation_b,
                owner_id=parents.owner_id,
                project_id=parents.project_id,
                group_id=parents.group_id,
                applied_revision_id=original.applied_revision_id,
                version=2,
                kind="GRID",
                input_json={},
                idempotency_key=f"storyboard-{generation_b}",
                provider_idempotency_key=f"provider-{generation_b}",
                canonical_request_sha256="b" * 64,
                status="SUCCEEDED",
                state_revision=1,
                created_at=datetime.now(UTC),
                checkpoint_json={},
            )
        )
        session.flush()
    _seed_current_media(
        postgres_database, parents, generation_b, make_current=False
    )
    commit_ready = Barrier(2)
    switch_committed = Barrier(2)

    class CommitBarrierRepository(SqlAlchemyVideoRepository):
        def commit_submitting(self, attempt):  # type: ignore[no-untyped-def]
            commit_ready.wait(timeout=10)
            switch_committed.wait(timeout=10)
            return super().commit_submitting(attempt)

    def switch_current() -> None:
        commit_ready.wait(timeout=10)
        with postgres_database.sessions.begin() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                {"scope": f"{parents.owner_id}:{parents.group_id}"},
            )
            current = session.get(StoryboardCurrentRow, (parents.owner_id, parents.group_id))
            assert current is not None
            current.generation_id = generation_b
            current.version += 1
        switch_committed.wait(timeout=10)

    engine = FakeEngine()
    barrier_repository = CommitBarrierRepository(
        postgres_database.sessions,
        writer._group_loader,  # type: ignore[attr-defined]
        lambda value: value,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        switch = pool.submit(switch_current)
        blocked = _submit_service(
            postgres_database, writer, engine, barrier_repository
        ).advance(parents.owner_id, attempt_id)
        switch.result(timeout=10)
    assert blocked.error_code == "immutable_video_input_changed"
    assert sum(call == "submit" for _, call in engine.calls) == 0
    with postgres_database.sessions() as session:
        attempt = session.get(VideoAttemptRecord, attempt_id)
        assert attempt is not None and attempt.stage != AttemptStage.SUBMITTING.value


@pytest.mark.integration
def test_h3_group_fence_wins_switch_rolls_back_and_post_runs_after_commit(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    writer = _writer(postgres_database)
    _seed_submit_node(postgres_database)
    attempt_id = writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_id,
        idempotency_key="h3-wins-final-submit",
    )

    provider_after_commit = Barrier(2)
    switch_finished = Barrier(2)

    class LockProbeEngine(FakeEngine):
        def submit(self, node, *, idempotency_key, body):  # type: ignore[no-untyped-def]
            with postgres_database.sessions.begin() as session:
                acquired = session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtext(:scope))"),
                    {"scope": f"{parents.owner_id}:{parents.group_id}"},
                )
                assert acquired is True, "provider POST ran before the H3 lock transaction released"
            provider_after_commit.wait(timeout=10)
            switch_finished.wait(timeout=10)
            return super().submit(node, idempotency_key=idempotency_key, body=body)

    engine = LockProbeEngine()
    service = _submit_service(postgres_database, writer, engine)

    def rejected_switch() -> None:
        provider_after_commit.wait(timeout=10)
        try:
            with postgres_database.sessions.begin() as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                    {"scope": f"{parents.owner_id}:{parents.group_id}"},
                )
                active = session.scalar(
                    select(VideoAttemptRecord.attempt_id).where(
                        VideoAttemptRecord.owner_id == parents.owner_id,
                        VideoAttemptRecord.group_id == parents.group_id,
                        VideoAttemptRecord.stage.in_(("SUBMITTING", "SUBMITTED", "RUNNING")),
                    )
                )
                if active is not None:
                    raise DomainError(
                        "active_group_video_attempt_exists",
                        "Current storyboard cannot change while H3 is active",
                    )
                pytest.fail("H3 attempt was not visible to the storyboard transaction")
        finally:
            switch_finished.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        switch = pool.submit(rejected_switch)
        submitted = service.advance(parents.owner_id, attempt_id)
        with pytest.raises(DomainError, match="cannot change while H3 is active"):
            switch.result(timeout=10)
    assert submitted.stage is AttemptStage.SUBMITTED
    assert sum(call == "submit" for _, call in engine.calls) == 1
    with postgres_database.sessions() as session:
        current = session.get(StoryboardCurrentRow, (parents.owner_id, parents.group_id))
        assert current is not None and current.generation_id == generation_id


@pytest.mark.integration
def test_missing_shared_asset_reference_fails_with_stable_code(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    asset_id = uuid4()
    with postgres_database.sessions.begin() as session:
        session.add(
            MediaAssetRow(
                id=asset_id,
                owner_id=parents.owner_id,
                project_id=None,
                ownership_scope="OWNER",
                kind="STORYBOARD_GRID",
                object_key=f"storyboards/{generation_id}-unreferenced.png",
                sha256=hashlib.sha256(b"asset").hexdigest(),
                media_type="image/png",
                size_bytes=5,
                metadata_json={},
                shared=True,
            )
        )
        session.add(
            StoryboardMediaRow(
                generation_id=generation_id,
                storyboard_asset_id=uuid4(),
                asset_id=asset_id,
                width=1024,
                height=1024,
                image_format="PNG",
            )
        )
        session.add(
            StoryboardCurrentRow(
                owner_id=parents.owner_id,
                group_id=parents.group_id,
                generation_id=generation_id,
                version=1,
            )
        )

    writer = _writer(postgres_database)

    with pytest.raises(DomainError, match="unavailable") as captured:
        writer.create(
            owner_id=parents.owner_id,
            group_id=parents.group_id,
            expected_storyboard_generation_id=generation_id,
            idempotency_key="missing-reference",
        )
    assert captured.value.code == "h3_inputs_not_ready"


@pytest.mark.integration
def test_video_batch_repository_reports_authoritative_group_states(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    repository = SqlVideoBatchRepository(postgres_database.sessions)

    (state,) = repository.list_group_states(parents.owner_id, parents.project_id)
    assert state.group_id == parents.group_id
    assert state.sequence == 1
    assert state.video_blocked is False
    assert state.block_reason is None
    assert state.storyboard_generation_id == generation_id
    assert state.has_current_video is False
    assert state.latest_attempt_stage is None

    writer = _writer(postgres_database)
    writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_id,
        idempotency_key="batch-repository-check",
    )
    (state,) = repository.list_group_states(parents.owner_id, parents.project_id)
    assert state.latest_attempt_stage == "CREATED"
    assert state.latest_attempt_stalled is False
    assert state.has_current_video is False

    with postgres_database.sessions.begin() as session:
        row = session.scalar(
            select(VideoAttemptRecord).where(
                VideoAttemptRecord.owner_id == parents.owner_id,
                VideoAttemptRecord.group_id == parents.group_id,
            )
        )
        assert row is not None
        row.stage = "RUNNING"
        row.stalled_at = datetime.now(UTC)
    (state,) = repository.list_group_states(parents.owner_id, parents.project_id)
    assert state.latest_attempt_stage == "RUNNING"
    assert state.latest_attempt_stalled is True

    assert repository.list_group_states(uuid4(), parents.project_id) == ()


@pytest.mark.integration
def test_video_batch_job_repository_crud_and_resume_semantics(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    parents = production_parent_chain
    repository = SqlVideoBatchJobRepository(postgres_database.sessions)

    job_id = repository.create_job(
        parents.owner_id,
        parents.project_id,
        force=False,
        items=(
            (parents.group_id, 1, "PENDING"),
            (uuid4(), 2, "has_current_video"),
        ),
    )
    view = repository.get_job(job_id)
    assert view is not None
    assert view.owner_id == parents.owner_id and view.total == 2
    assert [item.status for item in view.items] == ["PENDING", "has_current_video"]
    assert repository.list_unfinished_job_ids() == (job_id,)

    assert repository.next_pending_group(job_id) == (parents.group_id, 1)
    repository.mark_item(job_id, parents.group_id, "queued", attempt_id=uuid4())
    assert repository.next_pending_group(job_id) is None

    repository.complete_job(job_id)
    assert repository.list_unfinished_job_ids() == ()
    view = repository.get_job(job_id)
    assert view is not None and view.completed_at is not None
    assert view.items[0].status == "queued" and view.items[0].attempt_id is not None

    assert repository.get_job(uuid4()) is None


@pytest.mark.integration
def test_manual_intent_records_derived_seed_when_none_configured(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    # 2026-11-03 AB_REVIEW_AND_FOLLOWUPS §3：未配置 seed 时按 (group_id, idempotency_key) 派生并落库，
    # 快照与 provider request 必须一致；不同幂等键得到不同 seed。
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    writer = _writer(postgres_database, seed=None)
    key = "seed-derivation-manual"
    attempt_id = writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_id,
        idempotency_key=key,
    )
    expected = _derived_seed(parents.group_id, key)
    with postgres_database.sessions.begin() as session:
        row = session.get(VideoAttemptRecord, attempt_id)
        assert row is not None
        assert row.input_snapshot_json["seed"] == expected
        assert row.request_json["parameters"]["seed"] == expected


@pytest.mark.integration
def test_manual_intent_explicit_seed_overrides_and_conflicts_on_change(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    # 2026-11-03 用户决策 4：显式 seed 覆盖派生值并落库；同键换 seed 视为另一请求。
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    writer = _writer(postgres_database, seed=None)
    explicit = 798160553724158
    attempt_id = writer.create(
        owner_id=parents.owner_id,
        group_id=parents.group_id,
        expected_storyboard_generation_id=generation_id,
        idempotency_key="seed-explicit-manual",
        seed=explicit,
    )
    with postgres_database.sessions.begin() as session:
        row = session.get(VideoAttemptRecord, attempt_id)
        assert row is not None
        assert row.input_snapshot_json["seed"] == explicit
        assert row.request_json["parameters"]["seed"] == explicit

    with pytest.raises(DomainError) as conflict:
        writer.create(
            owner_id=parents.owner_id,
            group_id=parents.group_id,
            expected_storyboard_generation_id=generation_id,
            idempotency_key="seed-explicit-manual",
            seed=explicit + 1,
        )
    assert conflict.value.code == "idempotency_conflict"


@pytest.mark.integration
def test_manual_intent_experiment_arm_is_refused_on_a_composite_group(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    # §51（2026-11-05）：抖动 A/B 的实验臂只用于 v4 单格分组；整图分组在编译前即拒绝，不落库。
    parents, generation_id = storyboard_parent_chain
    _seed_current_media(postgres_database, parents, generation_id)
    writer = _writer(postgres_database, seed=None)
    with pytest.raises(VideoRuleViolation, match="experiment_requires_v4_single_panel"):
        writer.create(
            owner_id=parents.owner_id,
            group_id=parents.group_id,
            expected_storyboard_generation_id=generation_id,
            idempotency_key="experiment-composite",
            seed=798160553724158,
            experiment_arm="baseline",
        )
    with postgres_database.sessions.begin() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(VideoAttemptRecord)
                .where(VideoAttemptRecord.idempotency_key == "experiment-composite")
            )
            == 0
        )
