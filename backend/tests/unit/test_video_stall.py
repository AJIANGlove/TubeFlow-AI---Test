from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from temporalio.testing import WorkflowEnvironment
from test_video_orchestration import Probe, setup

from tubeflow_test.video_generation.api import create_group_video_router
from tubeflow_test.video_generation.domain import (
    VideoCapacityUnavailable,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.engine import NodeHealth
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    DurableGroupVideoOrchestrator,
    StallPolicy,
)
from tubeflow_test.video_generation.persistence import (
    VIDEO_GENERATION_TABLES,
    H3NodeRecord,
    VideoAttemptRecord,
    VideoGenerationBase,
    VideoOutboxRecord,
)
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_generation.temporal import (
    GroupVideoRecoveryWorkflow,
    RecoverVideoCommand,
)
from tubeflow_test.video_generation.worker import (
    create_video_worker,
    redrive_stale_video_outbox,
)
from tubeflow_test.video_prompt.fake_compiler import (
    FakeVideoPromptCompiler as StubVideoPromptCompiler,
)


def _stalled_service(policy: StallPolicy):
    source, repository, engine, assets, _service = setup()
    service = DurableGroupVideoOrchestrator(
        repository,
        engine,
        assets,
        Probe(),
        prompt_compiler=StubVideoPromptCompiler(),
        stall_policy=policy,
    )
    return source, repository, engine, service


def _extra_group(source, repository) -> None:
    group_id = uuid4()
    repository.groups[group_id] = replace(source, group_id=group_id)
    return group_id


def test_accepted_phase_stalls_after_threshold_and_clears_on_phase_change() -> None:
    source, repository, engine, service = _stalled_service(
        StallPolicy(accepted_seconds=0.0, running_seconds=3600.0, poll_seconds=1.0)
    )
    engine.inspect_status = "ACCEPTED"
    attempt = service.trigger(source.owner_id, source.group_id, "stall-accepted")
    attempt = service.advance(source.owner_id, attempt.attempt_id)
    assert attempt.stage is AttemptStage.SUBMITTED

    polled = service.advance(source.owner_id, attempt.attempt_id)
    assert polled.stage is AttemptStage.SUBMITTED
    assert polled.stalled_at is not None
    assert polled.remote_phase_changed_at is not None

    engine.inspect_status = "RUNNING"
    moved = service.advance(source.owner_id, attempt.attempt_id)
    assert moved.stage is AttemptStage.RUNNING
    assert moved.stalled_at is None
    assert moved.remote_phase_changed_at >= polled.remote_phase_changed_at

    again = service.advance(source.owner_id, attempt.attempt_id)
    assert again.stalled_at is None


def test_running_phase_stalls_only_after_running_threshold() -> None:
    source, repository, engine, service = _stalled_service(
        StallPolicy(accepted_seconds=9999.0, running_seconds=0.0, poll_seconds=1.0)
    )
    engine.inspect_status = "ACCEPTED"
    attempt = service.trigger(source.owner_id, source.group_id, "stall-running")
    service.advance(source.owner_id, attempt.attempt_id)
    accepted = service.advance(source.owner_id, attempt.attempt_id)
    assert accepted.stalled_at is None

    engine.inspect_status = "RUNNING"
    service.advance(source.owner_id, attempt.attempt_id)
    running = service.advance(source.owner_id, attempt.attempt_id)
    assert running.stage is AttemptStage.RUNNING
    assert running.stalled_at is not None

    kept = service.advance(source.owner_id, attempt.attempt_id)
    assert kept.stalled_at == running.stalled_at


def test_terminal_and_unknown_states_clear_stall() -> None:
    source, repository, engine, service = _stalled_service(
        StallPolicy(accepted_seconds=0.0, running_seconds=0.0, poll_seconds=1.0)
    )
    engine.inspect_status = "ACCEPTED"
    attempt = service.trigger(source.owner_id, source.group_id, "stall-terminal")
    service.advance(source.owner_id, attempt.attempt_id)
    stalled = service.advance(source.owner_id, attempt.attempt_id)
    assert stalled.stalled_at is not None
    node_id = stalled.node_id

    engine.inspect_status = "SUCCEEDED"
    succeeded = service.advance(source.owner_id, attempt.attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED
    assert succeeded.stalled_at is None
    assert repository.nodes[node_id].active_executions == 0


def test_remote_state_unknown_never_marks_stall() -> None:
    source, repository, engine, service = _stalled_service(
        StallPolicy(accepted_seconds=0.0, running_seconds=0.0, poll_seconds=1.0)
    )
    engine.remote_acceptance = "UNKNOWN"
    engine.inspect_status = "RUNNING"
    attempt = service.trigger(source.owner_id, source.group_id, "stall-unknown")
    service.advance(source.owner_id, attempt.attempt_id)
    for _ in range(2):
        polled = service.advance(source.owner_id, attempt.attempt_id)
    assert polled.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert polled.stalled_at is None


def test_full_capacity_without_stall_blocks_new_attempts() -> None:
    source, repository, engine, service = _stalled_service(StallPolicy())
    engine.inspect_status = "ACCEPTED"
    group_ids = [source.group_id] + [_extra_group(source, repository) for _ in range(2)]
    for index, group_id in enumerate(group_ids):
        attempt = service.trigger(source.owner_id, group_id, f"full-{index}")
        assert service.advance(source.owner_id, attempt.attempt_id).stage is AttemptStage.SUBMITTED
    assert sum(node.active_executions for node in repository.nodes.values()) == 3

    extra = service.trigger(source.owner_id, _extra_group(source, repository), "full-extra")
    with pytest.raises(VideoRuleViolation, match="no compatible H3 node has capacity"):
        service.advance(source.owner_id, extra.attempt_id)


def test_stalled_slots_on_all_nodes_do_not_block_new_attempts() -> None:
    source, repository, engine, service = _stalled_service(
        StallPolicy(accepted_seconds=0.0, running_seconds=0.0, poll_seconds=1.0)
    )
    engine.inspect_status = "ACCEPTED"
    group_ids = [source.group_id] + [_extra_group(source, repository) for _ in range(3)]
    for index, group_id in enumerate(group_ids[:3]):
        attempt = service.trigger(source.owner_id, group_id, f"stall-fill-{index}")
        service.advance(source.owner_id, attempt.attempt_id)
        polled = service.advance(source.owner_id, attempt.attempt_id)
        assert polled.stalled_at is not None
    assert sum(node.active_executions for node in repository.nodes.values()) == 3

    late = service.trigger(source.owner_id, group_ids[3], "stall-late")
    late = service.advance(source.owner_id, late.attempt_id)
    assert late.stage is AttemptStage.SUBMITTED


def test_public_view_exposes_versioned_remote_wait_without_node_details() -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, "stall-public")
    changed = (datetime.now(UTC) - timedelta(seconds=100)).isoformat()
    repository.attempts[attempt.attempt_id] = replace(
        attempt,
        stage=AttemptStage.RUNNING,
        node_id="stall-node-secret",
        remote_phase="RUNNING",
        remote_phase_changed_at=changed,
    )
    app = FastAPI()
    app.include_router(create_group_video_router(service, lambda: source.owner_id))
    client = TestClient(app)

    payload = client.get(f"/group-videos/attempts/{attempt.attempt_id}").json()
    assert payload["remote_wait_status"] == "waiting_remote"
    assert 95 <= payload["remote_wait_seconds"] <= 130
    assert "CANCEL" in payload["allowed_actions"]
    assert "stall-node-secret" not in str(payload)

    repository.attempts[attempt.attempt_id] = replace(
        repository.attempts[attempt.attempt_id],
        stalled_at=datetime.now(UTC).isoformat(),
    )
    stalled = client.get(f"/group-videos/attempts/{attempt.attempt_id}").json()
    assert stalled["remote_wait_status"] == "remote_stalled"

    repository.attempts[attempt.attempt_id] = replace(
        repository.attempts[attempt.attempt_id],
        stage=AttemptStage.SUCCEEDED,
    )
    succeeded = client.get(f"/group-videos/attempts/{attempt.attempt_id}").json()
    assert succeeded["remote_wait_status"] is None
    assert succeeded["remote_wait_seconds"] is None
    assert "CANCEL" not in succeeded["allowed_actions"]

    repository.attempts[attempt.attempt_id] = replace(
        repository.attempts[attempt.attempt_id],
        stage=AttemptStage.CREATED,
        stalled_at=None,
    )
    queued = client.get(f"/group-videos/attempts/{attempt.attempt_id}").json()
    assert queued["remote_wait_status"] is None
    assert "CANCEL" in queued["allowed_actions"]


def _sql_repository(tmp_path, nodes, workflow, attempts=()):
    engine = create_engine(f"sqlite:///{tmp_path / 'stall.db'}")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for item in nodes:
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
        for attempt in attempts:
            session.add(attempt)
    repository = SqlAlchemyVideoRepository(
        sessions,
        lambda *_args: (_ for _ in ()).throw(AssertionError()),
        lambda value: value,
    )
    return engine, sessions, repository


def _attempt_row(**overrides):
    values = {
        "attempt_id": uuid4(),
        "owner_id": uuid4(),
        "project_id": uuid4(),
        "group_id": uuid4(),
        "generation_id": uuid4(),
        "idempotency_key": uuid4().hex,
        "generation_sha256": "a" * 64,
        "request_sha256": "b" * 64,
        "request_json": {},
        "stage": AttemptStage.CREATED.value,
        "archive_stage": ArchiveStage.NOT_STARTED.value,
        "updated_at": datetime.now(UTC),
    }
    values.update(overrides)
    return VideoAttemptRecord(**values)


def test_sql_claim_excludes_stalled_slots(tmp_path) -> None:
    source, memory, _engine, _assets, _service = setup()
    workflow = source.workflow
    node_a, node_b = tuple(memory.nodes.values())[:2]
    stalled_holder = _attempt_row(
        node_id=node_a.node_id,
        stage=AttemptStage.RUNNING.value,
        slot_claimed=True,
        stalled_at=datetime.now(UTC),
    )
    engine, sessions, repository = _sql_repository(
        tmp_path, (node_a, node_b), workflow, attempts=(stalled_holder,)
    )
    with sessions.begin() as session:
        session.get(H3NodeRecord, node_a.node_id).claimed_slots = 1

    fresh = _attempt_row()
    with sessions.begin() as session:
        session.add(fresh)
    claimed = repository.claim_compatible_node(fresh.attempt_id, workflow)
    assert claimed.node_id == node_a.node_id
    engine.dispose()


def test_node_claim_tiebreak_and_rotation_match_sql(tmp_path) -> None:
    source, memory, fake_engine, _assets, service = setup()
    database, sessions, sql = _sql_repository(
        tmp_path, tuple(memory.nodes.values()), source.workflow
    )
    expected_order = sorted(memory.nodes) * 3
    try:
        for index, expected_node_id in enumerate(expected_order):
            group_id = source.group_id if index == 0 else _extra_group(source, memory)
            attempt = service.trigger(source.owner_id, group_id, f"rotation-{index}")
            with sessions.begin() as session:
                session.add(_attempt_row(attempt_id=attempt.attempt_id))

            memory_node = memory.claim_compatible_node(attempt.attempt_id, source.workflow)
            sql_node = sql.claim_compatible_node(attempt.attempt_id, source.workflow)
            assert memory_node.node_id == sql_node.node_id == expected_node_id

            memory_attempt = memory.attempts[attempt.attempt_id]
            memory.attempts[attempt.attempt_id] = replace(
                memory_attempt, stage=AttemptStage.FAILED
            )
            with sessions.begin() as session:
                row = session.get(VideoAttemptRecord, attempt.attempt_id)
                row.stage = AttemptStage.FAILED.value
                sql_lease_token = row.slot_lease_token
            memory.release_node(attempt.attempt_id, memory_attempt.slot_lease_token)
            sql.release_node(attempt.attempt_id, sql_lease_token)
        assert all(node.active_executions == 0 for node in memory.nodes.values())
        assert fake_engine.calls == []
    finally:
        database.dispose()


@pytest.mark.parametrize("preassigned", [False, True])
@pytest.mark.parametrize(
    "claimed_slots, stalled_slots, can_claim",
    [(1, 1, True), (2, 1, False), (3, 1, False), (2, 2, True)],
)
def test_stalled_capacity_boundaries_match_sql(
    tmp_path, preassigned, claimed_slots, stalled_slots, can_claim
) -> None:
    source, memory, fake_engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, "stalled-capacity")
    node_id = min(memory.nodes)
    for key, node in memory.nodes.items():
        memory.nodes[key] = replace(
            node,
            health=NodeHealth.READY if key == node_id else NodeHealth.OFFLINE,
            active_executions=claimed_slots if key == node_id else 0,
        )
    assigned_node_id = node_id if preassigned else None
    memory.attempts[attempt.attempt_id] = replace(attempt, node_id=assigned_node_id)
    now = datetime.now(UTC)
    sql_holders = []
    for index in range(claimed_slots):
        holder_id = uuid4()
        stalled_at = now if index < stalled_slots else None
        memory.attempts[holder_id] = replace(
            attempt,
            attempt_id=holder_id,
            group_id=uuid4(),
            idempotency_key=f"stalled-holder-{index}",
            node_id=node_id,
            stage=AttemptStage.SUBMITTED,
            slot_claimed=True,
            stalled_at=stalled_at.isoformat() if stalled_at else None,
        )
        sql_holders.append(
            _attempt_row(
                attempt_id=holder_id,
                node_id=node_id,
                stage=AttemptStage.SUBMITTED.value,
                slot_claimed=True,
                stalled_at=stalled_at,
            )
        )
    database, sessions, sql = _sql_repository(
        tmp_path, (memory.nodes[node_id],), source.workflow, attempts=tuple(sql_holders)
    )
    try:
        with sessions.begin() as session:
            session.get(H3NodeRecord, node_id).claimed_slots = claimed_slots
            session.add(
                _attempt_row(attempt_id=attempt.attempt_id, node_id=assigned_node_id)
            )
        if can_claim:
            memory_node = memory.claim_compatible_node(attempt.attempt_id, source.workflow)
            sql_node = sql.claim_compatible_node(attempt.attempt_id, source.workflow)
            assert memory_node.node_id == sql_node.node_id == node_id
        else:
            # 已绑定节点的槽位耗尽仍是规则错误；无任何可用节点才是有容量等待语义的新异常。
            expected_error = VideoRuleViolation if preassigned else VideoCapacityUnavailable
            for repository in (memory, sql):
                with pytest.raises(expected_error, match="no capacity|has capacity"):
                    repository.claim_compatible_node(attempt.attempt_id, source.workflow)
        expected_claimed = claimed_slots + int(can_claim)
        assert memory.nodes[node_id].active_executions == expected_claimed
        assert memory.attempts[attempt.attempt_id].slot_claimed == can_claim
        with sessions() as session:
            assert session.get(H3NodeRecord, node_id).claimed_slots == expected_claimed
            assert session.get(VideoAttemptRecord, attempt.attempt_id).slot_claimed == can_claim
        assert fake_engine.calls == []
    finally:
        database.dispose()


def test_sql_claim_tiebreak_prefers_least_recently_completed_node(tmp_path) -> None:
    source, memory, _engine, _assets, _service = setup()
    workflow = source.workflow
    node_a, node_b = tuple(memory.nodes.values())[:2]
    now = datetime.now(UTC)
    completed_a = _attempt_row(
        node_id=node_a.node_id,
        stage=AttemptStage.ARCHIVED.value,
        archive_stage=ArchiveStage.COMPLETE.value,
        updated_at=now - timedelta(hours=1),
    )
    engine, sessions, repository = _sql_repository(
        tmp_path, (node_a, node_b), workflow, attempts=(completed_a,)
    )

    first = _attempt_row()
    with sessions.begin() as session:
        session.add(first)
    claimed = repository.claim_compatible_node(first.attempt_id, workflow)
    assert claimed.node_id == node_b.node_id

    with sessions.begin() as session:
        row = session.get(VideoAttemptRecord, first.attempt_id)
        row.stage = AttemptStage.ARCHIVED.value
        row.archive_stage = ArchiveStage.COMPLETE.value
        row.slot_claimed = False
        row.updated_at = now
        session.get(H3NodeRecord, node_b.node_id).claimed_slots = 0

    second = _attempt_row()
    with sessions.begin() as session:
        session.add(second)
    claimed_second = repository.claim_compatible_node(second.attempt_id, workflow)
    assert claimed_second.node_id == node_a.node_id
    engine.dispose()


def test_redrive_leaves_qc_failed_attempts_untouched(tmp_path) -> None:
    source, memory, _engine, _assets, _service = setup()
    workflow = source.workflow
    del workflow
    engine = create_engine(f"sqlite:///{tmp_path / 'redrive.db'}")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    qc_failed = _attempt_row(
        stage=AttemptStage.SUCCEEDED.value, archive_stage=ArchiveStage.QC_FAILED.value
    )
    archived = _attempt_row(
        stage=AttemptStage.ARCHIVED.value, archive_stage=ArchiveStage.COMPLETE.value
    )
    with sessions.begin() as session:
        session.add(qc_failed)
        session.add(archived)
        for attempt in (qc_failed, archived):
            session.add(
                VideoOutboxRecord(
                    attempt_id=attempt.attempt_id,
                    owner_id=attempt.owner_id,
                    event_type="VIDEO_GENERATION_REQUESTED",
                    created_at=datetime.now(UTC) - timedelta(minutes=15),
                    dispatched_at=datetime.now(UTC) - timedelta(minutes=10),
                    next_attempt_at=datetime.now(UTC) - timedelta(minutes=10),
                )
            )
    assert redrive_stale_video_outbox(sessions) == 0
    with sessions() as session:
        for attempt in (qc_failed, archived):
            row = session.get(VideoOutboxRecord, attempt.attempt_id)
            assert row.dispatched_at is not None
    engine.dispose()


def test_redrive_still_resets_unfinished_remote_attempts(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'redrive2.db'}")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    running = _attempt_row(
        stage=AttemptStage.RUNNING.value,
        archive_stage=ArchiveStage.NOT_STARTED.value,
        # S6：workflow 丢失场景下 attempt 自身也不再更新，才允许 redrive。
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    with sessions.begin() as session:
        session.add(running)
        session.add(
            VideoOutboxRecord(
                attempt_id=running.attempt_id,
                owner_id=running.owner_id,
                event_type="VIDEO_GENERATION_REQUESTED",
                created_at=datetime.now(UTC) - timedelta(minutes=15),
                dispatched_at=datetime.now(UTC) - timedelta(minutes=10),
                next_attempt_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
    assert redrive_stale_video_outbox(sessions) == 1
    with sessions() as session:
        row = session.get(VideoOutboxRecord, running.attempt_id)
        assert row.dispatched_at is None
    engine.dispose()


@pytest.mark.asyncio
async def test_recovery_workflow_slows_down_while_stalled() -> None:
    class Service:
        calls = 0

        def advance(self, _owner_id, _attempt_id) -> object:
            self.calls += 1
            stalled = self.calls < 3
            return type(
                "Attempt",
                (),
                {
                    "archive_stage": ArchiveStage.NOT_STARTED,
                    "stage": AttemptStage.RUNNING if stalled else AttemptStage.ARCHIVED,
                    "error_code": None,
                    "stalled_at": "2026-09-03T00:00:00+00:00" if stalled else None,
                },
            )()

    service = Service()
    task_queue = f"h3-stalled-poll-{uuid4()}"
    command = RecoverVideoCommand(uuid4(), uuid4(), stalled_poll_seconds=1.0)
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        create_video_worker(environment.client, service, task_queue=task_queue),  # type: ignore[arg-type]
    ):
        result = await environment.client.execute_workflow(
            GroupVideoRecoveryWorkflow.run,
            command,
            id=f"h3-stalled-poll-{uuid4()}",
            task_queue=task_queue,
        )
    assert result == command
    assert service.calls == 3


@pytest.mark.asyncio
async def test_recovery_workflow_stops_at_qc_failed_terminal_state() -> None:
    class Service:
        calls = 0

        def advance(self, _owner_id, _attempt_id) -> object:
            self.calls += 1
            return type(
                "Attempt",
                (),
                {
                    "archive_stage": ArchiveStage.QC_FAILED,
                    "stage": AttemptStage.SUCCEEDED,
                    "error_code": None,
                    "stalled_at": None,
                },
            )()

    service = Service()
    task_queue = f"h3-qc-failed-stop-{uuid4()}"
    command = RecoverVideoCommand(uuid4(), uuid4())
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        create_video_worker(environment.client, service, task_queue=task_queue),  # type: ignore[arg-type]
    ):
        result = await environment.client.execute_workflow(
            GroupVideoRecoveryWorkflow.run,
            command,
            id=f"h3-qc-failed-stop-{uuid4()}",
            task_queue=task_queue,
        )
    assert result == command
    assert service.calls == 1


def test_redrive_skips_attempt_still_advancing(tmp_path) -> None:
    """S6：attempt 仍在推进时不得重置 outbox。

    否则 relay 会把同一个 workflow 反复重置成「未派发」，制造 300+ 僵尸 workflow。
    """
    source, memory, _engine, _assets, _service = setup()
    node = tuple(memory.nodes.values())[0]
    active = _attempt_row(
        node_id=node.node_id,
        stage=AttemptStage.RUNNING.value,
        slot_claimed=True,
        # updated_at 保持 fresh：模拟 activity 仍在推进
    )
    engine, sessions, _repository = _sql_repository(
        tmp_path, (node,), source.workflow, attempts=(active,)
    )
    with sessions.begin() as session:
        session.add(
            VideoOutboxRecord(
                attempt_id=active.attempt_id,
                owner_id=active.owner_id,
                event_type="VIDEO_GENERATION_REQUESTED",
                created_at=datetime.now(UTC) - timedelta(minutes=15),
                dispatched_at=datetime.now(UTC) - timedelta(minutes=10),
                next_attempt_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
    assert redrive_stale_video_outbox(sessions) == 0
    with sessions() as session:
        row = session.get(VideoOutboxRecord, active.attempt_id)
        assert row is not None and row.dispatched_at is not None
    engine.dispose()


def test_sweeper_reclaims_expired_unsubmitted_slot_after_grace(tmp_path) -> None:
    """S6：卡在 STAGING 的过期租约超过宽限期后必须归还槽位，否则槽位会被占死。"""
    source, memory, _engine, _assets, _service = setup()
    node = tuple(memory.nodes.values())[0]
    staging = _attempt_row(
        node_id=node.node_id,
        stage=AttemptStage.STAGING.value,
        slot_claimed=True,
        slot_lease_token="expired-token",
        slot_lease_expires_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    engine, sessions, repository = _sql_repository(
        tmp_path, (node,), source.workflow, attempts=(staging,)
    )
    with sessions.begin() as session:
        session.get(H3NodeRecord, node.node_id).claimed_slots = 1
    assert repository.sweep_expired_slots() == 1
    with sessions() as session:
        row = session.get(VideoAttemptRecord, staging.attempt_id)
        assert row is not None and row.slot_claimed is False
        assert session.get(H3NodeRecord, node.node_id).claimed_slots == 0
    engine.dispose()


def test_sweeper_keeps_expired_running_slot(tmp_path) -> None:
    """S6：RUNNING 的远端可能仍在生成，回收槽位会导致重复提交，必须保持不动。"""
    source, memory, _engine, _assets, _service = setup()
    node = tuple(memory.nodes.values())[0]
    running = _attempt_row(
        node_id=node.node_id,
        stage=AttemptStage.RUNNING.value,
        slot_claimed=True,
        slot_lease_token="expired-token",
        slot_lease_expires_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    engine, sessions, repository = _sql_repository(
        tmp_path, (node,), source.workflow, attempts=(running,)
    )
    with sessions.begin() as session:
        session.get(H3NodeRecord, node.node_id).claimed_slots = 1
    assert repository.sweep_expired_slots() == 0
    with sessions() as session:
        row = session.get(VideoAttemptRecord, running.attempt_id)
        assert row is not None and row.slot_claimed is True
    engine.dispose()
