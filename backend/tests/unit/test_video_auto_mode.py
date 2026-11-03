from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink
from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    VideoRuleViolation,
    canonical_sha256,
)
from tubeflow_test.video_generation.engine import EngineNode
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.orchestration import (
    SUBMISSION_BUDGET_EXHAUSTED,
    ArchiveStage,
    AttemptStage,
    AuthoritativeGroupInput,
    DurableGroupVideoOrchestrator,
    PersistentAttempt,
    VideoMediaEvidence,
)
from tubeflow_test.video_generation.persistence import (
    VIDEO_GENERATION_TABLES,
    VideoAttemptRecord,
    VideoGenerationBase,
    VideoOutboxRecord,
)
from tubeflow_test.video_generation.provider_audit import ProviderAuditEvent
from tubeflow_test.video_generation.provider_audit import canonical_hash as audit_hash
from tubeflow_test.video_generation.repository import InMemoryVideoRepository
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_prompt.fake_compiler import (
    FakeVideoPromptCompiler as StubVideoPromptCompiler,
)


class Assets:
    def __init__(self) -> None:
        self.values: dict[UUID, bytes] = {}

    def add(self, value: bytes, media_type: str = "image/png") -> MediaAssetRef:
        result = MediaAssetRef(uuid4(), hashlib.sha256(value).hexdigest(), media_type, len(value))
        self.values[result.asset_id] = value
        return result

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        return self.values[asset.asset_id]

    def archive_immutable(
        self,
        *,
        attempt_id: UUID,
        generation_id: UUID,
        output_id: str,
        content: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef:
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        return self.add(content, media_type)


class Probe:
    def __init__(self, evidence: VideoMediaEvidence | None = None) -> None:
        self.evidence = evidence or VideoMediaEvidence(
            "video/mp4", 1056, 608, 24.0, 3100, 1, 1, ("mp4",), 3100, 74, 0, 3100, 3100, "aac"
        )

    def inspect(self, content: bytes) -> VideoMediaEvidence:
        return self.evidence


def setup(
    production_mode: str = "AUTO", probe: Probe | None = None
) -> tuple[
    AuthoritativeGroupInput,
    InMemoryVideoRepository,
    FakeEngine,
    Assets,
    DurableGroupVideoOrchestrator,
]:
    assets = Assets()
    owner, project, group, generation, revision = (uuid4() for _ in range(5))
    manifest_bytes = b'{"parameters": {"resolution_preset": "preview"}}\n'
    manifest = {
        "workflow_id": "h3.community.reference-images-to-video",
        "workflow_version": "1.1.0-mvp",
        "parameters": {"resolution_preset": "preview"},
    }
    workflow = ImmutableWorkflowSnapshot(
        "minimax-h3",
        "1",
        "h3.community.reference-images-to-video",
        "1.1.0-mvp",
        hashlib.sha256(manifest_bytes).hexdigest(),
        "preview",
        manifest,
    )
    script = "hello"
    source = AuthoritativeGroupInput(
        owner,
        project,
        group,
        generation,
        True,
        hashlib.sha256(script.encode()).hexdigest(),
        ("1" * 64,),
        ("1" * 64,),
        (3000,),
        (True,),
        (assets.add(b"narration", "audio/wav"),),
        ((0, 3000),),
        (0,),
        0,
        3000,
        3000,
        (assets.add(b"character"),),
        assets.add(b"grid"),
        "GRID_SHEET",
        True,
        revision,
        revision,
        "group prompt",
        AspectRatio.LANDSCAPE,
        2**53 - 1,
        workflow,
        script,
        (0,),
        1,
        (assets.add(b"raw-narration", "audio/wav"),),
        generation,
        None,
    )
    source = replace(source, storyboard_asset_sha256=source.storyboard_image.sha256)
    source = replace(
        source,
        sentence_sample_counts=(144000,),
        sentence_sample_rates=(48000,),
        timeline_ranges_frames=((0, 144000),),
        group_start_frame=0,
        group_end_frame=144000,
        group_sample_rate=48000,
        authority_snapshot={"fixture": "canonical-v1"},
        production_mode=production_mode,
    )
    shot = DirectorShot(
        uuid4(), 1, "scene", "WIDE", "OBJECTIVE", "action", "function", "description"
    )
    sentence_id = uuid4()
    source = replace(
        source,
        director_shots=(shot,),
        shot_sentence_links=(ShotSentenceLink(shot.shot_id, sentence_id),),
        group_sentence_ids=(sentence_id,),
        visual_style="fixture style",
    )
    nodes = tuple(
        EngineNode(
            f"node-{index}",
            f"http://node-{index}",
            f"secret-{index}",
            1,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.workflow_version,
            manifest_sha256=workflow.workflow_manifest_sha256,
        )
        for index in range(3)
    )
    repository = InMemoryVideoRepository((source,), nodes)
    engine = FakeEngine()
    service = DurableGroupVideoOrchestrator(
        repository, engine, assets, probe or Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    return source, repository, engine, assets, service


def submit_calls(engine: FakeEngine) -> int:
    return sum(call == "submit" for _, call in engine.calls)


def drive_replacement_to_success(
    source: AuthoritativeGroupInput,
    repository: InMemoryVideoRepository,
    engine: FakeEngine,
    service: DurableGroupVideoOrchestrator,
    attempt_id: UUID,
) -> PersistentAttempt:
    submitted = service.advance(source.owner_id, attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    engine.inspect_status = "SUCCEEDED"
    succeeded = service.advance(source.owner_id, attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED
    archived = service.advance(source.owner_id, attempt_id)
    assert archived.stage is AttemptStage.ARCHIVED
    assert archived.archive_stage is ArchiveStage.COMPLETE
    return archived


def test_auto_rejected_submit_creates_budgeted_replacement_with_lineage() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "auto-reject-429")
    engine.reject_submit_status = 429
    engine.reject_submit_retry_after = 12.0

    rejected = service.advance(source.owner_id, intent.attempt_id)

    assert rejected.stage is AttemptStage.FAILED
    assert rejected.error_code == "EngineRejected:429"
    assert rejected.archive_stage is ArchiveStage.CLEANUP_PENDING
    assert "auto_resubmit:engine_rejected:429" in rejected.retry_evidence
    assert submit_calls(engine) == 1
    replacement = repository.find_attempt(
        source.owner_id, f"auto-resubmit:{intent.attempt_id}"
    )
    assert replacement is not None
    assert replacement.superseded_attempt_id == intent.attempt_id
    assert replacement.stage is AttemptStage.CREATED
    assert replacement.intent_sequence == intent.intent_sequence + 1
    assert repository.scheduled_delays[replacement.attempt_id] == 12.0
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2

    # 重放与重入：旧尝试已终态，重复推进不再产生第二次替代提交。
    cleaned = service.advance(source.owner_id, intent.attempt_id)
    assert cleaned.stage is AttemptStage.FAILED
    again = service.advance(source.owner_id, intent.attempt_id)
    assert again.stage is AttemptStage.FAILED
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    assert submit_calls(engine) == 1

    # 替代提交复用同一已编译提示词与不可变输入，成功后成为该组当前视频。
    engine.reject_submit_status = None
    archived = drive_replacement_to_success(
        source, repository, engine, service, replacement.attempt_id
    )
    assert submit_calls(engine) == 2
    # 既定拒绝未被引擎受理，fake 不留提交记录；提示词与旧尝试的持久化请求逐字一致。
    persisted_original = repository.get_attempt(intent.attempt_id)
    assert persisted_original is not None
    replacement_body = engine._submissions[replacement.attempt_id]
    assert replacement_body["prompt"] == persisted_original.request["prompt"]
    current = repository.get_current(source.owner_id, source.group_id)
    assert current is not None and current.attempt_id == archived.attempt_id


def test_auto_rejected_submit_without_retry_after_uses_backoff_ladder() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "auto-reject-500")
    engine.reject_submit_status = 500

    rejected = service.advance(source.owner_id, intent.attempt_id)

    assert rejected.error_code == "EngineRejected:500"
    replacement = repository.find_attempt(
        source.owner_id, f"auto-resubmit:{intent.attempt_id}"
    )
    assert replacement is not None
    assert repository.scheduled_delays[replacement.attempt_id] == 5.0


def test_auto_retry_after_below_ladder_keeps_ladder_floor() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "auto-reject-429-short")
    engine.reject_submit_status = 429
    engine.reject_submit_retry_after = 2.0

    service.advance(source.owner_id, intent.attempt_id)

    replacement = repository.find_attempt(
        source.owner_id, f"auto-resubmit:{intent.attempt_id}"
    )
    assert replacement is not None
    assert repository.scheduled_delays[replacement.attempt_id] == 5.0


def test_auto_submit_rejection_exhausts_budget_into_stable_terminal() -> None:
    source, repository, engine, _assets, service = setup()
    engine.reject_submit_status = 500
    attempt = service.trigger(source.owner_id, source.group_id, "auto-budget")
    delays: list[float] = []
    # 2026-10-16 决策：每组每预算轮提交预算 6 次（含全部 AUTO 重提原因）。
    for _round in range(6):
        outcome = service.advance(source.owner_id, attempt.attempt_id)
        assert outcome.stage is AttemptStage.FAILED
        replacement = repository.find_attempt(
            source.owner_id, f"auto-resubmit:{attempt.attempt_id}"
        )
        if replacement is None:
            break
        # 被拒尝试落 FAILED 后由 worker 兜底推进清理暂存并释放槽位（3 节点 × 1 容量）。
        cleaned = service.advance(source.owner_id, attempt.attempt_id)
        assert cleaned.stage is AttemptStage.FAILED
        delays.append(repository.scheduled_delays[replacement.attempt_id])
        attempt = replacement

    assert delays == [5.0, 15.0, 30.0, 30.0, 30.0]
    assert submit_calls(engine) == 6
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    exhausted = repository.get_attempt(attempt.attempt_id)
    assert exhausted is not None
    assert exhausted.stage is AttemptStage.FAILED
    assert exhausted.error_code == SUBMISSION_BUDGET_EXHAUSTED
    assert f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}" in exhausted.retry_evidence
    # 预算耗尽后重放旧尝试仍不产生任何新提交。
    service.advance(source.owner_id, exhausted.attempt_id)
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    assert submit_calls(engine) == 6


def test_auto_unknown_reconcile_404_creates_replacement_with_lineage() -> None:
    source, repository, engine, _assets, service = setup()
    engine.lose_submit_response = True
    intent = service.trigger(source.owner_id, source.group_id, "auto-unknown-404")
    unknown = service.advance(source.owner_id, intent.attempt_id)
    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    engine.inspect_not_found = True

    superseded = service.advance(source.owner_id, intent.attempt_id)

    assert superseded.stage is AttemptStage.FAILED
    assert superseded.error_code == "EngineAttemptNotFound"
    assert "auto_resubmit:remote_not_visible" in superseded.retry_evidence
    replacement = repository.find_attempt(
        source.owner_id, f"auto-resubmit:{intent.attempt_id}"
    )
    assert replacement is not None
    assert replacement.superseded_attempt_id == intent.attempt_id
    assert repository.scheduled_delays[replacement.attempt_id] == 5.0
    assert submit_calls(engine) == 1
    # 重入旧尝试的对账/推进都不会创建第二个替代提交。
    service.resume(source.owner_id, intent.attempt_id)
    service.advance(source.owner_id, intent.attempt_id)
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    assert submit_calls(engine) == 1
    # 替代尝试可以走通完整链路并完成归档。
    engine.lose_submit_response = False
    drive_replacement_to_success(source, repository, engine, service, replacement.attempt_id)
    assert submit_calls(engine) == 2


def test_auto_unverifiable_remote_state_resubmits_after_bounded_reconciles() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "auto-unverifiable")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    engine.remote_acceptance = "UNKNOWN"

    first = service.advance(source.owner_id, intent.attempt_id)
    assert first.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    second = service.advance(source.owner_id, intent.attempt_id)
    assert second.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1

    third = service.advance(source.owner_id, intent.attempt_id)

    assert third.stage is AttemptStage.FAILED
    assert third.error_code == "remote_state_unverifiable"
    assert "auto_resubmit:remote_state_unverifiable" in third.retry_evidence
    replacement = repository.find_attempt(
        source.owner_id, f"auto-resubmit:{intent.attempt_id}"
    )
    assert replacement is not None
    assert replacement.superseded_attempt_id == intent.attempt_id
    assert submit_calls(engine) == 1
    assert sum(call == "inspect" for _, call in engine.calls) == 3


def test_auto_qc_failed_regenerates_new_version_and_old_is_preserved() -> None:
    bad = replace(Probe().evidence, audio_tracks=0)
    source, repository, engine, assets, service = setup(probe=Probe(bad))
    intent = service.trigger(source.owner_id, source.group_id, "auto-qc")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    engine.inspect_status = "SUCCEEDED"
    succeeded = service.advance(source.owner_id, intent.attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED

    failed = service.advance(source.owner_id, intent.attempt_id)

    assert failed.stage is AttemptStage.SUCCEEDED
    assert failed.archive_stage is ArchiveStage.QC_FAILED
    assert failed.error_code == "VideoRuleViolation"
    assert failed.archived_asset is not None
    assert "auto_resubmit:qc_failed" in failed.retry_evidence
    replacement = repository.find_attempt(
        source.owner_id, f"auto-resubmit:{intent.attempt_id}"
    )
    assert replacement is not None
    assert replacement.superseded_attempt_id == intent.attempt_id
    assert repository.scheduled_delays[replacement.attempt_id] == 5.0
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    assert submit_calls(engine) == 1

    # 重放归档旧版本：幂等返回既有替代尝试，不产生重复提交或重复事件。
    events_before = len(repository.provider_events)
    replayed = service.archive(source.owner_id, intent.attempt_id)
    assert replayed.archive_stage is ArchiveStage.QC_FAILED
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    assert submit_calls(engine) == 1
    assert len(repository.provider_events) == events_before

    # 新版本用合格探针走通后成为当前视频；旧版本保持不可变历史。
    healthy = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    archived = drive_replacement_to_success(
        source, repository, engine, healthy, replacement.attempt_id
    )
    current = repository.get_current(source.owner_id, source.group_id)
    assert current is not None and current.attempt_id == archived.attempt_id
    persisted_old = repository.get_attempt(intent.attempt_id)
    assert persisted_old is not None
    assert persisted_old.stage is AttemptStage.SUCCEEDED
    assert persisted_old.archive_stage is ArchiveStage.QC_FAILED
    assert persisted_old.archived_asset is not None


def test_auto_qc_regeneration_exhausts_budget_and_parks_terminal() -> None:
    bad = replace(Probe().evidence, audio_tracks=0)
    source, repository, engine, assets, service = setup(probe=Probe(bad))
    attempt = service.trigger(source.owner_id, source.group_id, "auto-qc-budget")
    # 2026-10-16 决策：每组每预算轮提交预算 6 次；技术规格 QC 失败耗尽后维持 park。
    for _round in range(6):
        submitted = service.advance(source.owner_id, attempt.attempt_id)
        assert submitted.stage is AttemptStage.SUBMITTED
        engine.inspect_status = "SUCCEEDED"
        succeeded = service.advance(source.owner_id, attempt.attempt_id)
        assert succeeded.stage is AttemptStage.SUCCEEDED
        failed = service.advance(source.owner_id, attempt.attempt_id)
        assert failed.archive_stage is ArchiveStage.QC_FAILED
        replacement = repository.find_attempt(
            source.owner_id, f"auto-resubmit:{attempt.attempt_id}"
        )
        if replacement is None:
            break
        attempt = replacement

    assert submit_calls(engine) == 6
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    parked = repository.get_attempt(attempt.attempt_id)
    assert parked is not None
    # QC 路径预算耗尽：旧版本保持 SUCCEEDED+QC_FAILED 终态等人工，原因码稳定可查。
    assert parked.stage is AttemptStage.SUCCEEDED
    assert parked.archive_stage is ArchiveStage.QC_FAILED
    assert parked.error_code == "VideoRuleViolation"
    assert f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}" in parked.retry_evidence
    events_before = len(repository.provider_events)
    service.archive(source.owner_id, attempt.attempt_id)
    assert len(repository.provider_events) == events_before
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    assert submit_calls(engine) == 6


def test_manual_rejected_submit_stays_unknown_for_human_reconcile() -> None:
    source, repository, engine, _assets, service = setup(production_mode="MANUAL")
    intent = service.trigger(source.owner_id, source.group_id, "manual-reject-429")
    engine.reject_submit_status = 429
    engine.reject_submit_retry_after = 12.0

    outcome = service.advance(source.owner_id, intent.attempt_id)

    assert outcome.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert outcome.remote_acceptance == "UNKNOWN"
    # 与旧 EngineUncertain 路径一致：尝试本身不写 error_code，拒绝状态码记入审计事件。
    assert outcome.error_code is None
    events = [event for event in repository.provider_events if event.event_type == "SUBMIT_UNKNOWN"]
    assert len(events) == 1
    assert events[0].error_code == "EngineRejected:429"
    assert "submit_rejected:429" in outcome.retry_evidence
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}
    assert submit_calls(engine) == 1
    # 人工对账只恢复同一尝试，绝不自动重提。
    engine.reject_submit_status = None
    recovered = service.advance(source.owner_id, intent.attempt_id)
    assert recovered.stage is AttemptStage.RUNNING
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert submit_calls(engine) == 1


def test_manual_unknown_404_and_qc_failure_never_create_replacement() -> None:
    bad = replace(Probe().evidence, audio_tracks=0)
    source, repository, engine, _assets, service = setup(
        production_mode="MANUAL", probe=Probe(bad)
    )
    engine.lose_submit_response = True
    intent = service.trigger(source.owner_id, source.group_id, "manual-unknown-404")
    unknown = service.advance(source.owner_id, intent.attempt_id)
    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    engine.inspect_not_found = True

    reconciled = service.advance(source.owner_id, intent.attempt_id)

    assert reconciled.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert reconciled.error_code == "EngineAttemptNotFound"
    assert "reconcile:not_visible" in reconciled.retry_evidence
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}

    # MANUAL 的 QC_FAILED 是等待用户决策的终态，不自动重生成。
    engine.inspect_status = "SUCCEEDED"
    engine.remote_task_id = None
    engine.lose_submit_response = False
    persisted = repository.get_attempt(intent.attempt_id)
    assert persisted is not None
    persisted = replace(persisted, stage=AttemptStage.SUCCEEDED, remote_acceptance=None)
    repository.save_attempt(persisted)
    failed = service.archive(source.owner_id, intent.attempt_id)
    assert failed.archive_stage is ArchiveStage.QC_FAILED
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}
    assert submit_calls(engine) == 1


def _audit_event(attempt: PersistentAttempt, event_type: str) -> ProviderAuditEvent:
    safe: dict[str, object] = {}
    return ProviderAuditEvent(
        attempt_id=attempt.attempt_id,
        owner_id=attempt.owner_id,
        project_id=attempt.project_id,
        group_id=attempt.group_id,
        provider="kinestack",
        node_id="node-0",
        schema_version="kinestack.h3.v1",
        workflow_id="h3-r2v-images",
        workflow_version="1",
        model_key="minimax-h3",
        model_version="1",
        local_idempotency_key=attempt.idempotency_key,
        provider_task_key=None,
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        request_sha256=None,
        receipt=safe,
        receipt_sha256=audit_hash(safe),
        error_code="EngineRejected:500",
    )


def _replacement_for(superseded: PersistentAttempt) -> PersistentAttempt:
    request = dict(superseded.request)
    snapshot = dict(superseded.input_snapshot or {})
    return PersistentAttempt(
        uuid4(),
        superseded.owner_id,
        superseded.project_id,
        superseded.group_id,
        superseded.generation_id,
        f"auto-resubmit:{superseded.attempt_id}",
        superseded.generation_sha256,
        canonical_sha256(request),
        request,
        input_snapshot=snapshot,
        superseded_attempt_id=superseded.attempt_id,
    )


def test_sql_supersede_attempt_is_atomic_idempotent_and_budgeted() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    VideoGenerationBase.metadata.create_all(engine, tables=VIDEO_GENERATION_TABLES)
    sessions = sessionmaker(engine, expire_on_commit=False)
    repository = SqlAlchemyVideoRepository(
        sessions, lambda *_args: (_ for _ in ()).throw(AssertionError()), lambda value: value
    )
    first = repository.create_attempt(
        PersistentAttempt(
            uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "manual", "a" * 64, "b" * 64, {}
        )
    )
    loaded = repository.get_attempt(first.attempt_id)
    assert loaded is not None
    superseded = replace(
        loaded,
        stage=AttemptStage.FAILED,
        error_code="EngineRejected:500",
        retry_evidence=loaded.retry_evidence + ("auto_resubmit:engine_rejected:500",),
    )
    replacement = _replacement_for(superseded)

    saved, created = repository.supersede_attempt(
        superseded,
        replacement,
        _audit_event(superseded, "SUBMIT_REJECTED"),
        delay_seconds=5.0,
        max_submissions=3,
    )

    assert saved.stage is AttemptStage.FAILED
    assert created.superseded_attempt_id == first.attempt_id
    assert created.intent_sequence == first.intent_sequence + 1
    with sessions() as session:
        rows = session.scalars(select(VideoAttemptRecord)).all()
        assert len(rows) == 2
        persisted_replacement = session.get(VideoAttemptRecord, created.attempt_id)
        assert persisted_replacement is not None
        assert persisted_replacement.superseded_attempt_id == first.attempt_id
        outbox = session.get(VideoOutboxRecord, created.attempt_id)
        assert outbox is not None and outbox.dispatched_at is None
        assert (outbox.next_attempt_at - outbox.created_at).total_seconds() == 5.0
        events = session.scalars(select(VideoAttemptRecord)).all()
        assert len(events) == 2

    # 同一替代幂等键重放：不产生第二行，也不改写既有替代尝试。
    replayed, existing = repository.supersede_attempt(
        superseded,
        _replacement_for(superseded),
        _audit_event(superseded, "SUBMIT_REJECTED"),
        delay_seconds=5.0,
        max_submissions=3,
    )
    assert existing.attempt_id == created.attempt_id
    assert replayed.stage is AttemptStage.FAILED
    with sessions() as session:
        assert len(session.scalars(select(VideoAttemptRecord)).all()) == 2
        assert len(session.scalars(select(VideoOutboxRecord)).all()) == 2

    # 预算在库内复核：达到上限的替代提交整体回滚，旧尝试保持原样。
    second = repository.get_attempt(created.attempt_id)
    assert second is not None
    second_superseded = replace(second, stage=AttemptStage.FAILED, error_code="EngineRejected:500")
    with pytest.raises(VideoRuleViolation, match=SUBMISSION_BUDGET_EXHAUSTED):
        repository.supersede_attempt(
            second_superseded,
            _replacement_for(second_superseded),
            _audit_event(second_superseded, "SUBMIT_REJECTED"),
            delay_seconds=15.0,
            max_submissions=2,
        )
    reloaded = repository.get_attempt(created.attempt_id)
    assert reloaded is not None and reloaded.stage is AttemptStage.CREATED
    with sessions() as session:
        assert len(session.scalars(select(VideoAttemptRecord)).all()) == 2
    engine.dispose()

def test_auto_trigger_records_current_budget_round() -> None:
    # §15.9：AUTO 意图记录当前预算轮次；恢复后新轮次新意图按轮隔离计数。
    source1, repo1, _e1, _a1, service1 = setup()
    first = service1.trigger(source1.owner_id, source1.group_id, "round-tag-1")
    assert first.budget_round == 1

    source2, repo2, _e2, _a2, service2 = setup()
    repo2.budget_rounds[(source2.owner_id, source2.project_id)] = 2
    second = service2.trigger(source2.owner_id, source2.group_id, "round-tag-2")
    assert second.budget_round == 2
    assert repo2.count_group_attempts(source2.owner_id, source2.group_id, budget_round=2) == 1
    assert repo2.count_group_attempts(source2.owner_id, source2.group_id, budget_round=1) == 0



def test_intent_compile_failure_is_retryable_for_auto_only() -> None:
    """C3：AUTO 项目的意图编译失败转可重试标记；MANUAL 维持直接失败。"""
    from tubeflow_test.domain.errors import DomainError
    from tubeflow_test.video_generation.domain import RetryableVideoIntentFailure
    from tubeflow_test.video_prompt.domain import VideoPromptStrategy

    class FailingCompiler:
        strategy = VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2

        def compile(self, value, *, max_repairs: int = 1, **_: object):  # noqa: ANN001
            del value, max_repairs
            raise DomainError("video_prompt_compilation_failed", "boom")

        def close(self) -> None:
            return None

    source, repository, engine, assets, _service = setup(production_mode="AUTO")
    auto = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=FailingCompiler()
    )
    with pytest.raises(RetryableVideoIntentFailure) as auto_exc:
        auto.trigger(source.owner_id, source.group_id, "auto-compile")
    assert auto_exc.value.code == "video_prompt_compilation_failed"

    source, repository, engine, assets, _service = setup(production_mode="MANUAL")
    manual = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=FailingCompiler()
    )
    with pytest.raises(DomainError) as manual_exc:
        manual.trigger(source.owner_id, source.group_id, "manual-compile")
    assert not isinstance(manual_exc.value, RetryableVideoIntentFailure)
    assert manual_exc.value.code == "video_prompt_compilation_failed"
