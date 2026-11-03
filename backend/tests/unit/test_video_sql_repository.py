from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from test_video_orchestration import Probe, setup

from tubeflow_test.video_generation.engine import NodeHealth
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    DurableGroupVideoOrchestrator,
)
from tubeflow_test.video_generation.persistence import (
    VIDEO_GENERATION_METADATA,
    VIDEO_GENERATION_TABLES,
    H3NodeRecord,
    VideoAttemptRecord,
)
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_prompt.fake_compiler import (
    FakeVideoPromptCompiler as StubVideoPromptCompiler,
)


def test_feature_metadata_create_all_round_trip_and_restart_recovery(tmp_path: Path) -> None:
    source, memory, fake_engine, assets, _service = setup()
    workflow = source.workflow
    engine = create_engine(f"sqlite:///{tmp_path / 'video.db'}")
    VIDEO_GENERATION_METADATA.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for item in memory.nodes.values():
            session.add(
                H3NodeRecord(
                    node_id=item.node_id,
                    base_url=item.base_url,
                    token_secret_ref=f"secret:{item.node_id}",
                    capacity=item.max_concurrent,
                    claimed_slots=0,
                    enabled=True,
                    health=NodeHealth.READY.value,
                    capability="video.reference_images_to_video",
                    model_key=workflow.model_key,
                    model_version=workflow.model_version,
                    workflow_id=workflow.workflow_id,
                    workflow_version=workflow.workflow_version,
                    manifest_sha256=workflow.workflow_manifest_sha256,
                )
            )

    def load_group(session: Session, owner_id: UUID, group_id: UUID):  # type: ignore[no-untyped-def]
        assert owner_id == source.owner_id and group_id == source.group_id
        return source

    first_repo = SqlAlchemyVideoRepository(sessions, load_group, lambda ref: ref)
    first_service = DurableGroupVideoOrchestrator(
        first_repo, fake_engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    attempt = first_service.trigger(source.owner_id, source.group_id, "restart-key")
    attempt = first_service.advance(source.owner_id, attempt.attempt_id)
    del first_service, first_repo

    second_repo = SqlAlchemyVideoRepository(sessions, load_group, lambda ref: ref)
    second_service = DurableGroupVideoOrchestrator(
        second_repo, fake_engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    recovered = second_service.resume(source.owner_id, attempt.attempt_id)
    assert recovered.attempt_id == attempt.attempt_id
    assert recovered.node_id == attempt.node_id
    assert sum(call == "submit" for _, call in fake_engine.calls) == 1
    with sessions() as session:
        row = session.scalar(
            select(VideoAttemptRecord).where(VideoAttemptRecord.attempt_id == attempt.attempt_id)
        )
        assert row is not None
        assert row.request_sha256 == recovered.request_sha256
        assert row.input_snapshot_json is not None


def test_sqlite_partial_unique_allows_new_attempt_after_terminal(tmp_path: Path) -> None:
    source, _memory, _engine, assets, _service = setup()
    database = create_engine(f"sqlite:///{tmp_path / 'terminal.db'}")
    VIDEO_GENERATION_METADATA.create_all(database, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(database, expire_on_commit=False)

    def load_group(session: Session, owner_id: UUID, group_id: UUID):  # type: ignore[no-untyped-def]
        return source

    repository = SqlAlchemyVideoRepository(sessions, load_group, lambda ref: ref)
    service = DurableGroupVideoOrchestrator(
        repository, FakeEngine(), assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    first = service.trigger(source.owner_id, source.group_id, "terminal-first")
    with sessions.begin() as session:
        row = session.get(VideoAttemptRecord, first.attempt_id)
        assert row is not None
        row.stage = AttemptStage.FAILED.value
    second = service.trigger(source.owner_id, source.group_id, "terminal-second")
    assert second.attempt_id != first.attempt_id


def test_downloaded_checkpoint_recovers_with_new_repository_and_engine(tmp_path: Path) -> None:
    source, memory, first_engine, assets, _service = setup()
    workflow = source.workflow
    database = create_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    VIDEO_GENERATION_METADATA.create_all(database, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(database, expire_on_commit=False)
    with sessions.begin() as session:
        for item in memory.nodes.values():
            session.add(
                H3NodeRecord(
                    node_id=item.node_id,
                    base_url=item.base_url,
                    token_secret_ref=item.node_id,
                    capacity=item.max_concurrent,
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

    def load_group(session: Session, owner_id: UUID, group_id: UUID):  # type: ignore[no-untyped-def]
        assert owner_id == source.owner_id and group_id == source.group_id
        return source

    first_repo = SqlAlchemyVideoRepository(sessions, load_group, lambda ref: ref)
    attempt = DurableGroupVideoOrchestrator(
        first_repo, first_engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    ).trigger(
        source.owner_id, source.group_id, "download-crash"
    )
    attempt = DurableGroupVideoOrchestrator(
        first_repo, first_engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    ).advance(
        source.owner_id, attempt.attempt_id
    )
    with sessions.begin() as session:
        row = session.get(VideoAttemptRecord, attempt.attempt_id)
        assert row is not None
        row.stage = AttemptStage.SUCCEEDED.value
        row.archive_stage = ArchiveStage.DOWNLOADED.value
        row.output_id = "output-1"
        row.output_sha256 = hashlib.sha256(first_engine.output).hexdigest()
        row.output_size_bytes = len(first_engine.output)
        row.output_media_type = "video/mp4"

    del first_repo, first_engine
    restarted_engine = FakeEngine()
    restarted_repo = SqlAlchemyVideoRepository(sessions, load_group, lambda ref: ref)
    archived = DurableGroupVideoOrchestrator(
        restarted_repo, restarted_engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    ).archive(source.owner_id, attempt.attempt_id)
    assert archived.archive_stage is ArchiveStage.COMPLETE
    assert archived.output_size_bytes == len(restarted_engine.output)
