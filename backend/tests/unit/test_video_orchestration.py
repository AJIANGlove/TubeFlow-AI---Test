from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink, ShotTimeRange
from tubeflow_test.video_generation.api import create_group_video_router
from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    RetryableVideoIntentFailure,
    VideoAssetIntegrityError,
    VideoCapacityUnavailable,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.engine import EngineNode, EngineUncertain
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    AuthoritativeGroupInput,
    DurableGroupVideoOrchestrator,
    PersistentAttempt,
    RequestedAction,
    VideoMediaEvidence,
    _authorize,
    _derived_seed,
    _generation_from_snapshot,
    _group_shot_ranges,
    _input_snapshot,
    canonical_sha256,
    compile_video_prompt,
    validate_reference_intent,
)
from tubeflow_test.video_generation.repository import InMemoryVideoRepository
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
    *,
    workflow_id: str = "h3.community.reference-images-to-video",
    workflow_version: str = "1.1.0-mvp",
    probe: "Probe | None" = None,
    seed: int | str | None = 2**53 - 1,
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
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "parameters": {"resolution_preset": "preview"},
    }
    workflow = ImmutableWorkflowSnapshot(
        "minimax-h3",
        "1",
        workflow_id,
        workflow_version,
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
        seed,
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


def test_fractional_sample_endpoints_are_gapless_and_drive_provider_ceil() -> None:
    source, repository, _engine, _assets, service = setup()
    sample_count = 48024
    count = 4
    endpoints = tuple(index * sample_count for index in range(count + 1))
    ranges_ms = tuple(
        (
            round(endpoints[index] * 1000 / 48000),
            round(endpoints[index + 1] * 1000 / 48000),
        )
        for index in range(count)
    )
    sentence_ids = tuple(uuid4() for _ in range(count))
    exact = replace(
        source,
        group_sentence_ids=sentence_ids,
        shot_sentence_links=tuple(
            ShotSentenceLink(source.director_shots[0].shot_id, sentence_id)
            for sentence_id in sentence_ids
        ),
        sentence_text_sha256=("1" * 64,) * count,
        narrated_text_sha256=("1" * 64,) * count,
        sentence_durations_ms=(round(sample_count * 1000 / 48000),) * count,
        narration_qc_passed=(True,) * count,
        normalized_audio_assets=(source.normalized_audio_assets[0],) * count,
        raw_audio_assets=(source.raw_audio_assets[0],) * count,
        timeline_ranges_ms=ranges_ms,
        group_sentence_indexes=tuple(range(count)),
        authoritative_group_sentence_indexes=tuple(range(count)),
        group_start_ms=0,
        group_end_ms=ranges_ms[-1][1],
        group_target_duration_ms=round(endpoints[-1] * 1000 / 48000),
        sentence_sample_counts=(sample_count,) * count,
        sentence_sample_rates=(48000,) * count,
        timeline_ranges_frames=tuple(zip(endpoints[:-1], endpoints[1:], strict=True)),
        group_start_frame=0,
        group_end_frame=endpoints[-1],
        group_sample_rate=48000,
    )
    repository.groups[source.group_id] = exact
    attempt = service.trigger(source.owner_id, source.group_id, "fractional")
    assert attempt.request["parameters"]["duration_seconds"] == 5  # type: ignore[index]
    assert all(ranges_ms[index][1] == ranges_ms[index + 1][0] for index in range(count - 1))


def test_24013_sample_sentences_use_qc_duration_and_cumulative_endpoints() -> None:
    source, repository, engine, _assets, service = setup()
    sample_count = 24013
    sentence_ids = (uuid4(), uuid4())
    exact = replace(
        source,
        group_sentence_ids=sentence_ids,
        shot_sentence_links=tuple(
            ShotSentenceLink(source.director_shots[0].shot_id, sentence_id)
            for sentence_id in sentence_ids
        ),
        sentence_text_sha256=("1" * 64, "1" * 64),
        narrated_text_sha256=("1" * 64, "1" * 64),
        sentence_durations_ms=(1001, 1001),
        narration_qc_passed=(True, True),
        normalized_audio_assets=(source.normalized_audio_assets[0],) * 2,
        raw_audio_assets=(source.raw_audio_assets[0],) * 2,
        timeline_ranges_ms=((0, 1001), (1001, 2001)),
        group_sentence_indexes=(0, 1),
        authoritative_group_sentence_indexes=(0, 1),
        group_start_ms=0,
        group_end_ms=2001,
        group_target_duration_ms=2001,
        sentence_sample_counts=(sample_count, sample_count),
        sentence_sample_rates=(24000, 24000),
        timeline_ranges_frames=((0, sample_count), (sample_count, sample_count * 2)),
        group_start_frame=0,
        group_end_frame=sample_count * 2,
        group_sample_rate=24000,
    )
    repository.groups[source.group_id] = exact
    attempt = service.trigger(source.owner_id, source.group_id, "fractional-24013")
    submitted = service.advance(source.owner_id, attempt.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    assert sum(call == "submit" for _, call in engine.calls) == 1


def test_persisted_validation_marker_never_authorizes_drifted_submit() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "marker-is-evidence")
    repository.attempts[intent.attempt_id] = replace(
        intent, narration_validation={"version": "tts-bytes-v1"}
    )
    repository.groups[source.group_id] = replace(
        source, authority_snapshot={"fixture": "drifted"}
    )
    failed = service.advance(source.owner_id, intent.attempt_id)
    assert failed.stage is AttemptStage.FAILED
    assert failed.error_code == "immutable_video_input_changed"
    assert sum(call == "submit" for _, call in engine.calls) == 0


def test_authoritative_gate_blocks_before_h3_io() -> None:
    source, repository, engine, _assets, service = setup()
    repository.groups[source.group_id] = replace(source, narration_qc_passed=(False,))
    with pytest.raises(VideoRuleViolation, match="narration_qc"):
        service.trigger(source.owner_id, source.group_id, "click-1")
    assert engine.calls == []


def test_submit_verifies_audio_bytes_for_sentences_outside_current_group() -> None:
    source, repository, engine, assets, service = setup()
    raw_second = assets.add(b"raw-second", "audio/wav")
    normalized_second = assets.add(b"normalized-second", "audio/wav")
    repository.groups[source.group_id] = replace(
        source,
        sentence_text_sha256=("1" * 64, "2" * 64),
        narrated_text_sha256=("1" * 64, "2" * 64),
        sentence_durations_ms=(3000, 1000),
        narration_qc_passed=(True, True),
        normalized_audio_assets=(source.normalized_audio_assets[0], normalized_second),
        raw_audio_assets=(source.raw_audio_assets[0], raw_second),
        timeline_ranges_ms=((0, 3000), (3000, 4000)),
        sentence_sample_counts=(144000, 48000),
        sentence_sample_rates=(48000, 48000),
        timeline_ranges_frames=((0, 144000), (144000, 192000)),
        group_end_frame=144000,
    )
    assets.values[raw_second.asset_id] = b"tampered"
    intent = service.trigger(source.owner_id, source.group_id, "all-audio")
    failed = service.advance(source.owner_id, intent.attempt_id)
    assert failed.stage is AttemptStage.FAILED
    assert failed.error_code == "narration_audio_bytes_changed"
    assert "gate:narration_audio_bytes_changed" in failed.retry_evidence
    assert not failed.slot_claimed and not failed.staged_ids
    assert engine.calls == []

    replacement = service.trigger(source.owner_id, source.group_id, "after-byte-failure")
    assert replacement.attempt_id != failed.attempt_id


def test_persisted_request_hash_failure_is_terminal_and_not_active() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "hash-gate")
    repository.attempts[intent.attempt_id] = replace(
        intent, request={**intent.request, "tampered": True}
    )

    failed = service.advance(source.owner_id, intent.attempt_id)
    assert failed.stage is AttemptStage.FAILED
    assert failed.error_code == "persisted_provider_request_hash_mismatch"
    assert not failed.slot_claimed
    assert engine.calls == []
    replacement = service.trigger(source.owner_id, source.group_id, "hash-gate-retry")
    assert replacement.attempt_id != failed.attempt_id


def test_reference_asset_integrity_failure_is_terminal_before_submit() -> None:
    source, repository, engine, assets, service = setup()
    original_read = assets.read_verified

    def fail_image(asset: MediaAssetRef) -> bytes:
        if asset.media_type.startswith("image/"):
            raise VideoAssetIntegrityError("missing reference object")
        return original_read(asset)

    assets.read_verified = fail_image  # type: ignore[method-assign]
    intent = service.trigger(source.owner_id, source.group_id, "missing-reference")
    failed = service.advance(source.owner_id, intent.attempt_id)
    assert failed.stage is AttemptStage.FAILED
    assert failed.error_code == "video_input_asset_integrity_failed"
    assert failed.archive_stage is ArchiveStage.NOT_STARTED
    assert not failed.slot_claimed and not failed.staged_ids
    assert sum(call == "submit" for _node, call in engine.calls) == 0
    assert repository.find_unresolved_group(source.owner_id, source.group_id) is None


def test_persisted_validation_marker_never_authorizes_submit_after_staging_drift() -> None:
    source, repository, engine, _assets, service = setup()
    original_stage = engine.stage_asset

    def drift_after_stage(*args, **kwargs):  # type: ignore[no-untyped-def]
        receipt = original_stage(*args, **kwargs)
        repository.groups[source.group_id] = replace(
            repository.groups[source.group_id],
            authority_snapshot={"fixture": "changed-after-stage"},
        )
        return receipt

    engine.stage_asset = drift_after_stage  # type: ignore[method-assign]
    intent = service.trigger(source.owner_id, source.group_id, "stage-authority-drift")
    repository.attempts[intent.attempt_id] = replace(
        intent, narration_validation={"version": "stale-marker"}
    )

    failed = service.advance(source.owner_id, intent.attempt_id)

    assert failed.stage is AttemptStage.FAILED
    assert failed.error_code == "immutable_video_input_changed"
    assert sum(call == "submit" for _node, call in engine.calls) == 0


def test_post_submit_reconcile_ignores_unneeded_local_request_hash_drift() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "accepted-drift")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.attempts[submitted.attempt_id] = replace(
        submitted, request={**submitted.request, "tampered": True}
    )
    engine.inspect_status = "RUNNING"
    reconciled = service.advance(source.owner_id, submitted.attempt_id)
    assert reconciled.stage is AttemptStage.RUNNING
    assert reconciled.remote_acceptance == submitted.remote_acceptance
    assert reconciled.remote_id == submitted.remote_id
    assert reconciled.node_id == submitted.node_id
    assert reconciled.slot_claimed
    assert reconciled.staged_ids == submitted.staged_ids
    assert not any(call.startswith("delete:") for _node, call in engine.calls)


def test_archive_local_integrity_block_preserves_known_remote_success() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "archive-local-block")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    succeeded = replace(
        submitted,
        stage=AttemptStage.SUCCEEDED,
        remote_phase="SUCCEEDED",
        remote_acceptance="ACCEPTED",
        raw_receipt={"attempt_id": str(intent.attempt_id), "status": "SUCCEEDED"},
        request={**submitted.request, "tampered": True},
    )
    repository.attempts[submitted.attempt_id] = succeeded

    blocked = service.advance(source.owner_id, submitted.attempt_id)
    assert blocked.stage is AttemptStage.SUCCEEDED
    assert blocked.error_code == (
        "local_integrity_blocked:persisted_provider_request_hash_mismatch"
    )
    assert blocked.remote_phase == "SUCCEEDED"
    assert blocked.remote_acceptance == "ACCEPTED"
    assert blocked.raw_receipt == succeeded.raw_receipt
    assert blocked.node_id == succeeded.node_id
    assert blocked.slot_claimed == succeeded.slot_claimed
    assert blocked.staged_ids == succeeded.staged_ids
    assert not any(call.startswith("delete:") for _node, call in engine.calls)


def test_real_schema_and_duplicate_click_are_idempotent() -> None:
    source, _repository, engine, _assets, service = setup()
    first = service.trigger(source.owner_id, source.group_id, "click-1")
    second = service.trigger(source.owner_id, source.group_id, "click-1")
    assert first.attempt_id == second.attempt_id
    assert engine.calls == []
    first = service.advance(source.owner_id, first.attempt_id)
    assert sum(call == "submit" for _, call in engine.calls) == 1
    body = engine._submissions[first.attempt_id]
    assert body["parameters"]["resolution_preset"] == "preview"
    assert "staged_assets" in body and "references" not in body
    assert not any(
        forbidden in repr(body).lower()
        for forbidden in ("first_frame", "last_frame", "reference_video", "reference_audio")
    )


def test_http_commands_only_persist_intent_without_external_io() -> None:
    source, _repository, engine, _assets, service = setup()
    app = FastAPI()
    app.include_router(create_group_video_router(service, lambda: source.owner_id))
    client = TestClient(app)
    response = client.post(f"/group-videos/groups/{source.group_id}/attempts")
    assert response.status_code == 404
    attempt_id = service.trigger(source.owner_id, source.group_id, "http-intent").attempt_id
    for command in ("reconcile", "cancel", "archive"):
        result = client.post(f"/group-videos/attempts/{attempt_id}/{command}")
        assert result.status_code == 202
    assert engine.calls == []


def test_http_cancel_rejects_successful_attempt_without_persisting_action() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "http-success-cancel")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(replace(submitted, stage=AttemptStage.SUCCEEDED))
    app = FastAPI()
    app.include_router(create_group_video_router(service, lambda: source.owner_id))

    response = TestClient(app).post(f"/group-videos/attempts/{intent.attempt_id}/cancel")

    assert response.status_code == 409
    persisted = repository.get_attempt(intent.attempt_id)
    assert persisted is not None and persisted.requested_action is None
    assert sum(call == "cancel" for _, call in engine.calls) == 0


def test_changed_authoritative_request_conflicts_with_same_key() -> None:
    source, repository, _engine, _assets, service = setup()
    service.trigger(source.owner_id, source.group_id, "click-1")
    repository.groups[source.group_id] = replace(source, prompt="different")
    with pytest.raises(VideoRuleViolation, match="idempotency_conflict"):
        service.trigger(source.owner_id, source.group_id, "click-1")


def test_uncertain_submit_only_recovers_same_attempt_and_node() -> None:
    source, repository, engine, assets, service = setup()
    engine.lose_submit_response = True
    intent = service.trigger(source.owner_id, source.group_id, "click-1")
    attempt = service.advance(source.owner_id, intent.attempt_id)
    assert attempt.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    recovered = restarted.resume(source.owner_id, attempt.attempt_id)
    assert recovered.node_id == attempt.node_id
    assert recovered.stage is AttemptStage.RUNNING
    assert sum(call == "submit" for _, call in engine.calls) == 1


def test_submit_receipt_audit_is_bound_to_validated_remote_id() -> None:
    source, repository, _engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "receipt-audit")

    submitted = service.advance(source.owner_id, intent.attempt_id)

    events = [event for event in repository.provider_events if event.event_type == "SUBMIT_RECEIPT"]
    assert len(events) == 1
    assert submitted.remote_id is not None
    assert events[0].provider_task_key == submitted.remote_id


def test_terminal_resume_does_not_reclaim_capacity() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "click-1")
    attempt = service.advance(source.owner_id, intent.attempt_id)
    engine.inspect_status = "SUCCEEDED"
    done = service.resume(source.owner_id, attempt.attempt_id)
    assert done.stage is AttemptStage.SUCCEEDED and not done.slot_claimed
    before = tuple(node.active_executions for node in repository.nodes.values())
    assert service.resume(source.owner_id, attempt.attempt_id) == done
    assert tuple(node.active_executions for node in repository.nodes.values()) == before


def test_successful_remote_attempt_cannot_be_overwritten_by_cancel() -> None:
    source, _repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "click-1")
    attempt = service.advance(source.owner_id, intent.attempt_id)
    engine.inspect_status = "SUCCEEDED"
    result = service.cancel(source.owner_id, attempt.attempt_id)
    assert result.stage is AttemptStage.SUCCEEDED


def test_cancel_response_loss_reconciles_terminal_without_second_cancel() -> None:
    source, repository, engine, assets, service = setup()
    engine.remote_task_id = "opaque-cancel-task"
    intent = service.trigger(source.owner_id, source.group_id, "cancel-loss")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    engine.lose_cancel_response_once = True
    engine.cancel_status_after_loss = "RUNNING"
    with pytest.raises(EngineUncertain):
        service.cancel(source.owner_id, submitted.attempt_id)
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for _ in range(3):
        pending = restarted.cancel(source.owner_id, submitted.attempt_id)
        assert pending.cancel_dispatched
        assert pending.cancel_idempotency_key == f"{intent.attempt_id}:cancel"
        assert len(engine.cancel_effects) == 1
    engine.inspect_not_found = True
    hidden = restarted.cancel(source.owner_id, submitted.attempt_id)
    assert hidden.stage is AttemptStage.CANCELLED
    assert len(engine.cancel_effects) == 1
    engine.inspect_status = "CANCELLED"
    cancelled = restarted.cancel(source.owner_id, submitted.attempt_id)
    assert cancelled.stage is AttemptStage.CANCELLED
    assert engine.last_task_key == "opaque-cancel-task"
    assert len(engine.cancel_effects) == 1


def test_cancel_checkpoint_crash_redrives_same_key_after_bounded_inspect() -> None:
    source, repository, engine, assets, service = setup()
    engine.remote_task_id = "known-cancel-task"
    intent = service.trigger(source.owner_id, source.group_id, "cancel-pre-call-crash")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    original_save = repository.save_attempt
    crashed = False

    def checkpoint_then_crash(value):  # type: ignore[no-untyped-def]
        nonlocal crashed
        saved = original_save(value)
        if not crashed and saved.cancel_dispatched:
            crashed = True
            raise RuntimeError("crash before cancel POST")
        return saved

    repository.save_attempt = checkpoint_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash before cancel POST"):
        service.cancel(source.owner_id, submitted.attempt_id)
    repository.save_attempt = original_save  # type: ignore[method-assign]
    assert sum(call == "cancel" for _, call in engine.calls) == 0
    engine.inspect_status = "RUNNING"
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for _ in range(2):
        pending = restarted.cancel(source.owner_id, submitted.attempt_id)
        assert pending.stage is AttemptStage.RUNNING
        assert sum(call == "cancel" for _, call in engine.calls) == 0
    cancelled = restarted.cancel(source.owner_id, submitted.attempt_id)
    assert cancelled.stage is AttemptStage.CANCELLED
    assert len(engine.cancel_effects) == 1


def test_requested_cancel_survives_checkpoint_crash_until_bounded_same_key_redrive() -> None:
    source, repository, engine, assets, service = setup()
    engine.remote_task_id = "known-requested-cancel-task"
    intent = service.trigger(source.owner_id, source.group_id, "requested-cancel-crash")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    service.request_action(source.owner_id, submitted.attempt_id, RequestedAction.CANCEL)
    original_save = repository.save_attempt
    crashed = False

    def checkpoint_then_crash(value):  # type: ignore[no-untyped-def]
        nonlocal crashed
        saved = original_save(value)
        if not crashed and saved.cancel_dispatched:
            crashed = True
            raise RuntimeError("crash after cancel checkpoint")
        return saved

    repository.save_attempt = checkpoint_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash after cancel checkpoint"):
        service.advance(source.owner_id, submitted.attempt_id)
    repository.save_attempt = original_save  # type: ignore[method-assign]
    engine.inspect_status = "RUNNING"
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for _ in range(2):
        pending = restarted.advance(source.owner_id, submitted.attempt_id)
        assert pending.requested_action is RequestedAction.CANCEL
        assert sum(call == "cancel" for _, call in engine.calls) == 0
    cancelled = restarted.advance(source.owner_id, submitted.attempt_id)
    assert cancelled.stage is AttemptStage.CANCELLED
    assert cancelled.requested_action is None
    assert len(engine.cancel_effects) == 1


def test_cancel_remote_unknown_receipts_count_toward_same_key_redrive() -> None:
    source, repository, engine, assets, service = setup()
    engine.remote_task_id = "known-unknown-cancel-task"
    intent = service.trigger(source.owner_id, source.group_id, "cancel-unknown-redrive")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(
        replace(
            submitted,
            requested_action=RequestedAction.CANCEL,
            cancel_dispatched=True,
            cancel_idempotency_key=f"{intent.attempt_id}:cancel",
        )
    )
    engine.inspect_status = "RUNNING"
    engine.remote_acceptance = "UNKNOWN"
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for expected_count in (1, 2):
        pending = restarted.advance(source.owner_id, intent.attempt_id)
        assert pending.stage is AttemptStage.REMOTE_STATE_UNKNOWN
        assert pending.requested_action is RequestedAction.CANCEL
        assert pending.cancel_inspect_count == expected_count
        assert len(engine.cancel_effects) == 0
    cancelled = restarted.advance(source.owner_id, intent.attempt_id)
    assert cancelled.stage is AttemptStage.CANCELLED
    assert cancelled.requested_action is None
    assert len(engine.cancel_effects) == 1


def test_same_idempotency_key_is_scoped_by_owner() -> None:
    source, repository, engine, assets, service = setup()
    other = uuid4()
    other_group = uuid4()
    repository.groups[other_group] = replace(
        source, owner_id=other, group_id=other_group, generation_id=uuid4()
    )
    first = service.trigger(source.owner_id, source.group_id, "same-key")
    second = service.trigger(other, other_group, "same-key")
    assert first.attempt_id != second.attempt_id
    assert sum(call == "submit" for _, call in engine.calls) == 0


def test_different_key_cannot_create_second_active_group_attempt() -> None:
    source, _repository, engine, _assets, service = setup()
    first = service.trigger(source.owner_id, source.group_id, "first-key")
    with pytest.raises(VideoRuleViolation, match="active_group_video_attempt_exists"):
        service.trigger(source.owner_id, source.group_id, "second-key")
    assert first.stage is AttemptStage.CREATED
    assert engine.calls == []


def test_late_older_archive_cannot_replace_newer_group_current() -> None:
    source, repository, _engine, _assets, service = setup()
    older = service.trigger(source.owner_id, source.group_id, "older")
    older = replace(older, stage=AttemptStage.ARCHIVED, archive_stage=ArchiveStage.COMPLETE)
    repository.attempts[older.attempt_id] = older
    newer = service.trigger(source.owner_id, source.group_id, "newer")
    newer = replace(newer, stage=AttemptStage.ARCHIVED, archive_stage=ArchiveStage.COMPLETE)
    repository.attempts[newer.attempt_id] = newer
    repository.mark_current(newer)
    repository.mark_current(older)
    current = repository.get_current(source.owner_id, source.group_id)
    assert current is not None and current.attempt_id == newer.attempt_id
    assert newer.intent_sequence > older.intent_sequence


def test_not_submitted_can_retry_same_attempt() -> None:
    source, _repository, engine, _assets, service = setup()
    engine.fail_stage_at = 2
    with pytest.raises(RuntimeError, match="upload failure"):
        intent = service.trigger(source.owner_id, source.group_id, "click-1")
        service.advance(source.owner_id, intent.attempt_id)
    engine.fail_stage_at = None
    retried = service.advance(source.owner_id, intent.attempt_id)
    assert retried.stage is AttemptStage.SUBMITTED


def test_real_accepted_receipt_is_nonterminal_and_preserved() -> None:
    source, _repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "accepted")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    engine.inspect_status = "ACCEPTED"
    accepted = service.resume(source.owner_id, submitted.attempt_id)
    assert accepted.stage is AttemptStage.SUBMITTED
    assert accepted.remote_id == str(intent.attempt_id)
    assert accepted.raw_receipt == {
        "attempt_id": str(intent.attempt_id),
        "status": "ACCEPTED",
        "schema": "kinestack.execution.v1",
    }


def test_remote_task_id_is_persisted_and_used_for_every_follow_up() -> None:
    source, _repository, engine, _assets, service = setup()
    engine.remote_task_id = "remote-task-42"
    intent = service.trigger(source.owner_id, source.group_id, "remote-task")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.remote_id == "remote-task-42"
    service.resume(source.owner_id, submitted.attempt_id)
    assert engine.last_task_key == "remote-task-42"


def test_cancel_uses_validated_provider_remote_task_id() -> None:
    source, _repository, engine, _assets, service = setup()
    engine.remote_task_id = "remote-cancel-42"
    intent = service.trigger(source.owner_id, source.group_id, "cancel-remote-id")
    submitted = service.advance(source.owner_id, intent.attempt_id)

    service.cancel(source.owner_id, submitted.attempt_id)

    assert engine.last_task_key == "remote-cancel-42"


def test_failed_with_unknown_remote_acceptance_never_releases_or_retries() -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "unknown-acceptance")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    engine.inspect_status = "FAILED"
    engine.remote_acceptance = "UNKNOWN"
    unknown = service.resume(source.owner_id, submitted.attempt_id)
    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert unknown.slot_claimed
    assert unknown.remote_acceptance == "UNKNOWN"
    assert unknown.staged_ids and not unknown.cleaned_staged_ids
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    with pytest.raises(VideoRuleViolation, match="remote_acceptance_unknown"):
        restarted.trigger(source.owner_id, source.group_id, "different-key")
    requested = restarted.request_action(
        source.owner_id, unknown.attempt_id, RequestedAction.CANCEL
    )
    still_unknown = restarted.advance(source.owner_id, requested.attempt_id)
    assert still_unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert still_unknown.slot_claimed and not still_unknown.cleaned_staged_ids
    with pytest.raises(VideoRuleViolation, match="remote_acceptance_unknown"):
        restarted.trigger(source.owner_id, source.group_id, "after-cancel")


def test_submit_response_loss_reconciles_same_attempt_without_resubmit() -> None:
    source, repository, engine, assets, service = setup()
    engine.lose_submit_response = True
    intent = service.trigger(source.owner_id, source.group_id, "response-loss")
    unknown = service.advance(source.owner_id, intent.attempt_id)
    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert unknown.remote_acceptance == "UNKNOWN"
    assert sum(call == "submit" for _, call in engine.calls) == 1

    engine.lose_submit_response = False
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for _ in range(5):
        engine.inspect_not_found = True
        still_unknown = restarted.advance(source.owner_id, intent.attempt_id)
        assert still_unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
        assert still_unknown.attempt_id == intent.attempt_id
        assert sum(call == "submit" for _, call in engine.calls) == 1

    engine.inspect_status = "RUNNING"
    visible = restarted.advance(source.owner_id, intent.attempt_id)
    assert visible.stage is AttemptStage.RUNNING
    assert visible.remote_id == str(intent.attempt_id)
    assert sum(call == "submit" for _, call in engine.calls) == 1


def test_submitting_recovery_404_never_posts_again() -> None:
    source, repository, engine, assets, service = setup()
    original_submit = engine.submit
    submit_calls = 0

    def submit(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal submit_calls
        submit_calls += 1
        raise SystemExit("worker crashed after persisting SUBMITTING")

    engine.submit = submit  # type: ignore[method-assign]
    intent = service.trigger(source.owner_id, source.group_id, "deterministic-recovery")
    with pytest.raises(SystemExit):
        service.advance(source.owner_id, intent.attempt_id)
    engine.inspect_not_found = True
    unknown = service.advance(source.owner_id, intent.attempt_id)
    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert unknown.remote_acceptance == "UNKNOWN"
    for _ in range(3):
        engine.inspect_not_found = True
        unknown = service.advance(source.owner_id, intent.attempt_id)
        assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert submit_calls == 1
    engine.submit = original_submit  # type: ignore[method-assign]


def test_cancel_uses_remote_task_identity_and_rejects_cross_task_receipt() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "remote-cancel")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    remote_id = "remote-task-42"
    submitted = replace(submitted, remote_id=remote_id)
    repository.attempts[submitted.attempt_id] = submitted
    seen: list[str] = []

    def inspect(_node, task_id):  # type: ignore[no-untyped-def]
        seen.append(str(task_id))
        return {
            "attempt_id": str(task_id),
            "status": "RUNNING",
            "schema": "kinestack.execution.v1",
        }

    def cancel(_node, task_id, *, idempotency_key):  # type: ignore[no-untyped-def]
        seen.append(str(task_id))
        assert idempotency_key == f"{submitted.attempt_id}:cancel"
        return {
            "attempt_id": str(task_id),
            "status": "CANCELLED",
            "schema": "kinestack.execution.v1",
        }

    engine.inspect = inspect  # type: ignore[method-assign]
    engine.cancel = cancel  # type: ignore[method-assign]
    cancelled = service.cancel(source.owner_id, submitted.attempt_id)
    assert cancelled.cleanup_terminal_stage is AttemptStage.CANCELLED
    assert seen == [str(remote_id), str(remote_id)]

    repository.attempts[submitted.attempt_id] = submitted

    def cross_task(_node, _task_id):  # type: ignore[no-untyped-def]
        return {
            "attempt_id": str(uuid4()),
            "status": "RUNNING",
            "schema": "kinestack.execution.v1",
        }

    engine.inspect = cross_task  # type: ignore[method-assign]
    with pytest.raises(VideoRuleViolation, match="inspect receipt task mismatch"):
        service.cancel(source.owner_id, submitted.attempt_id)
    assert repository.attempts[submitted.attempt_id].stage is AttemptStage.SUBMITTED
@pytest.mark.parametrize("uncertain", [False, True])
def test_submit_and_audit_atomic_failure_redrives_by_inspect_only(uncertain: bool) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"atomic-audit-{uncertain}")
    engine.lose_submit_response = uncertain
    original = repository.save_attempt_with_event
    failed = False

    def fail_submit_event(value, event):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed and event.event_type in {"SUBMIT_RECEIPT", "SUBMIT_UNKNOWN"}:
            failed = True
            raise RuntimeError("audit insert failed")
        return original(value, event)

    repository.save_attempt_with_event = fail_submit_event  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="audit insert failed"):
        service.advance(source.owner_id, intent.attempt_id)
    persisted = repository.get_attempt(intent.attempt_id)
    assert persisted is not None and persisted.stage is AttemptStage.SUBMITTING
    assert sum(call == "submit" for _, call in engine.calls) == 1

    repository.save_attempt_with_event = original  # type: ignore[method-assign]
    engine.lose_submit_response = False
    engine.inspect_not_found = True
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for _ in range(5):
        engine.inspect_not_found = True
        hidden = restarted.advance(source.owner_id, intent.attempt_id)
        assert hidden.stage is AttemptStage.REMOTE_STATE_UNKNOWN
        assert hidden.remote_acceptance == "UNKNOWN"
        assert hidden.remote_id is None and hidden.raw_receipt is None
        assert sum(call == "submit" for _, call in engine.calls) == 1
    engine.inspect_not_found = False
    recovered = restarted.advance(source.owner_id, intent.attempt_id)
    assert recovered.stage is AttemptStage.RUNNING
    assert sum(call == "submit" for _, call in engine.calls) == 1


def test_cancel_cannot_treat_uncertain_submit_404_as_not_accepted() -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "cancel-uncertain-submit")
    original = repository.save_attempt_with_event
    failed = False

    def lose_accepted_receipt(value, event):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed and event.event_type == "SUBMIT_RECEIPT":
            failed = True
            raise RuntimeError("accepted receipt transaction lost")
        return original(value, event)

    repository.save_attempt_with_event = lose_accepted_receipt  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="accepted receipt transaction lost"):
        service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt_with_event = original  # type: ignore[method-assign]
    persisted = repository.get_attempt(intent.attempt_id)
    assert persisted is not None and persisted.stage is AttemptStage.SUBMITTING
    staged_ids = persisted.staged_ids

    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for _ in range(5):
        engine.inspect_not_found = True
        unknown = restarted.cancel(source.owner_id, intent.attempt_id)
        assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
        assert unknown.remote_acceptance == "UNKNOWN"
        assert unknown.staged_ids == staged_ids and not unknown.cleaned_staged_ids
        assert unknown.slot_claimed
        assert sum(call == "submit" for _, call in engine.calls) == 1
        assert sum(call == "cancel" for _, call in engine.calls) == 0
        with pytest.raises(VideoRuleViolation, match="remote_acceptance_unknown"):
            restarted.trigger(source.owner_id, source.group_id, f"blocked-{_}")

    engine.inspect_status = "RUNNING"
    cancelled = restarted.cancel(source.owner_id, intent.attempt_id)
    assert cancelled.stage is AttemptStage.CANCELLED
    assert sum(call == "submit" for _, call in engine.calls) == 1
    assert sum(call == "cancel" for _, call in engine.calls) == 1


def test_atomic_audit_rejects_cross_project_identity() -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, "audit-identity")
    event = service._event(  # noqa: SLF001
        attempt,
        repository.nodes["node-0"],
        "TEST",
        {},
    )
    with pytest.raises(VideoRuleViolation, match="provider_audit_identity_mismatch"):
        repository.save_attempt_with_event(
            attempt,
            replace(event, project_id=uuid4()),
        )


def test_audit_event_schema_is_bounded_scalar_after_receipt_redaction() -> None:
    source, repository, _engine, _assets, service = setup()
    attempt = service.trigger(source.owner_id, source.group_id, "audit-schema-boundary")
    list_schema = service._event(  # noqa: SLF001
        attempt, repository.nodes["node-0"], "TEST", {"schema": ["not", "scalar"]}
    )
    long_schema = service._event(  # noqa: SLF001
        attempt, repository.nodes["node-0"], "TEST", {"schema": "s" * 700}
    )
    assert list_schema.schema_version == "kinestack.h3.v1"
    assert long_schema.schema_version == "s" * 512


@pytest.mark.parametrize("event_type", ["STAGE_RECEIPT", "INSPECT_RECEIPT"])
def test_stage_and_inspect_audit_failure_leave_safe_redrive_state(event_type: str) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"audit-fail-{event_type}")
    if event_type == "INSPECT_RECEIPT":
        before = service.advance(source.owner_id, intent.attempt_id)
    else:
        before = intent
    original = repository.save_attempt_with_event
    failed = False

    def fail_event(value, event):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed and event.event_type == event_type:
            failed = True
            raise RuntimeError("audit insert failed")
        return original(value, event)

    repository.save_attempt_with_event = fail_event  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="audit insert failed"):
        service.advance(source.owner_id, intent.attempt_id)
    persisted = repository.get_attempt(intent.attempt_id)
    expected = AttemptStage.NOT_SUBMITTED if event_type == "STAGE_RECEIPT" else before.stage
    assert persisted is not None and persisted.stage is expected
    repository.save_attempt_with_event = original  # type: ignore[method-assign]
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    recovered = restarted.advance(source.owner_id, intent.attempt_id)
    assert recovered.stage in {AttemptStage.SUBMITTED, AttemptStage.RUNNING}
    assert sum(call == "submit" for _, call in engine.calls) == 1


def test_unknown_attempt_remains_unknown_across_restart_and_unbounded_404s() -> None:
    source, repository, engine, assets, service = setup()
    engine.lose_submit_response = True
    intent = service.trigger(source.owner_id, source.group_id, "unbounded-404")
    unknown = service.advance(source.owner_id, intent.attempt_id)
    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN

    engine.lose_submit_response = False
    for _ in range(12):
        engine.inspect_not_found = True
        restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
        persisted = restarted.advance(source.owner_id, intent.attempt_id)
        assert persisted.stage is AttemptStage.REMOTE_STATE_UNKNOWN
        assert persisted.remote_acceptance == "UNKNOWN"
    assert sum(call == "submit" for _, call in engine.calls) == 1


def test_native_unknown_execution_status_remains_reconcilable_without_resubmit() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "native-unknown-status")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    engine.inspect_status = "UNKNOWN"
    engine.remote_acceptance = "UNKNOWN"

    unknown = service.resume(source.owner_id, submitted.attempt_id)

    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert unknown.remote_acceptance == "UNKNOWN"
    assert sum(call == "submit" for _, call in engine.calls) == 1


@pytest.mark.parametrize("remote_status", ["FAILED", "CANCELLED"])
def test_remote_terminal_cleanup_completes_before_slot_release(remote_status: str) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"terminal-{remote_status}")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    staged = submitted.staged_ids
    engine.inspect_status = remote_status
    terminal = service.resume(source.owner_id, submitted.attempt_id)
    assert terminal.archive_stage is ArchiveStage.CLEANUP_PENDING

    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    completed = restarted.advance(source.owner_id, terminal.attempt_id)
    assert completed.stage is AttemptStage(remote_status)
    assert completed.archive_stage is ArchiveStage.NOT_STARTED
    assert not completed.slot_claimed and not completed.staged_ids
    assert {call for _, call in engine.calls if call.startswith("delete:")} == {
        f"delete:{staged_id}" for staged_id in staged
    }


def test_submitting_with_receipt_404_never_resubmits() -> None:
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "before-send")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.attempts[submitted.attempt_id] = replace(submitted, stage=AttemptStage.SUBMITTING)
    engine.inspect_not_found = True
    recovered = service.advance(source.owner_id, intent.attempt_id)
    assert recovered.attempt_id == intent.attempt_id
    assert recovered.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert recovered.remote_id == submitted.remote_id
    assert sum(call == "submit" for _, call in engine.calls) == 1

    restarted = DurableGroupVideoOrchestrator(
        repository, engine, _assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    reconciled = restarted.advance(source.owner_id, intent.attempt_id)
    assert reconciled.stage is AttemptStage.RUNNING
    assert reconciled.remote_id == submitted.remote_id
    assert sum(call == "submit" for _, call in engine.calls) == 1


def test_staged_cleanup_failure_resumes_from_persisted_checkpoint() -> None:
    source, repository, engine, assets, service = setup()
    engine.fail_stage_at = 2
    with pytest.raises(RuntimeError, match="upload failure"):
        intent = service.trigger(source.owner_id, source.group_id, "cleanup")
        service.advance(source.owner_id, intent.attempt_id)
    engine.fail_stage_at = None
    engine.fail_delete_once = True
    with pytest.raises(RuntimeError, match="cleanup failure"):
        service.advance(source.owner_id, intent.attempt_id)
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    recovered = restarted.advance(source.owner_id, intent.attempt_id)
    assert recovered.stage is AttemptStage.SUBMITTED
    assert not recovered.cleaned_staged_ids
    assert set(recovered.staged_ids) <= engine._staged


@pytest.mark.parametrize("remote_terminal", ["FAILED", "CANCELLED"])
def test_remote_terminal_cleanup_resumes_before_terminal_early_return(
    remote_terminal: str,
) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"remote-{remote_terminal}")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    staged_ids = submitted.staged_ids
    assert staged_ids and submitted.slot_claimed

    engine.inspect_status = remote_terminal
    pending = service.resume(source.owner_id, submitted.attempt_id)
    assert pending.archive_stage is ArchiveStage.CLEANUP_PENDING
    assert pending.slot_claimed

    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    cleaned = restarted.advance(source.owner_id, submitted.attempt_id)
    assert cleaned.stage is AttemptStage(remote_terminal)
    assert cleaned.archive_stage is ArchiveStage.NOT_STARTED
    assert not cleaned.staged_ids and not cleaned.cleaned_staged_ids
    assert not cleaned.slot_claimed
    deleted = {
        call.removeprefix("delete:") for _node, call in engine.calls if call.startswith("delete:")
    }
    assert deleted == set(staged_ids)


@pytest.mark.parametrize("operation", ["resume", "cancel"])
def test_terminal_and_cleanup_checkpoint_are_atomic_before_crash(operation: str) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"atomic-{operation}")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    staged_ids = submitted.staged_ids
    original_save = repository.save_attempt
    injected = False

    def save_then_crash(value):  # type: ignore[no-untyped-def]
        nonlocal injected
        saved = original_save(value)
        if not injected and saved.stage in {AttemptStage.FAILED, AttemptStage.CANCELLED}:
            injected = True
            raise RuntimeError("crash after terminal save")
        return saved

    repository.save_attempt = save_then_crash  # type: ignore[method-assign]
    if operation == "resume":
        engine.inspect_status = "FAILED"
        action = service.resume
    else:
        action = service.cancel
    with pytest.raises(RuntimeError, match="crash after terminal save"):
        action(source.owner_id, submitted.attempt_id)

    persisted = repository.get_attempt(submitted.attempt_id)
    assert persisted is not None
    assert persisted.archive_stage is ArchiveStage.CLEANUP_PENDING
    assert persisted.cleanup_terminal_stage is persisted.stage
    repository.save_attempt = original_save  # type: ignore[method-assign]
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    cleaned = restarted.advance(source.owner_id, submitted.attempt_id)
    assert cleaned.stage in {AttemptStage.FAILED, AttemptStage.CANCELLED}
    assert not cleaned.staged_ids and not cleaned.slot_claimed
    deleted = {
        call.removeprefix("delete:") for _node, call in engine.calls if call.startswith("delete:")
    }
    assert deleted == set(staged_ids)


def test_not_submitted_attempt_blocks_second_idempotency_key_after_restart() -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "first")
    repository.attempts[intent.attempt_id] = replace(
        intent,
        stage=AttemptStage.NOT_SUBMITTED,
        node_id="node-0",
    )
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    with pytest.raises(VideoRuleViolation, match="active_group_video_attempt_exists"):
        restarted.trigger(source.owner_id, source.group_id, "second")


@pytest.mark.parametrize(
    "evidence, message",
    [
        (VideoMediaEvidence("video/mp4", 1056, 608, 24.0, 3100, 1, 0, ("mp4",)), "audio"),
        (
            VideoMediaEvidence(
                "video/mp4", 1056, 608, 25.0, 3100, 1, 1, ("mp4",), 3100, 78, 0, 3100, 3100, "aac"
            ),
            "24fps",
        ),
        (
            VideoMediaEvidence(
                "video/mp4", 1280, 720, 24.0, 3100, 1, 1, ("mp4",), 3100, 74, 0, 3100, 3100, "aac"
            ),
            "resolution",
        ),
        (
            VideoMediaEvidence(
                "video/mp4", 1056, 608, 24.0, 2999, 1, 1, ("mp4",), 2999, 72, 0, 2999, 2999, "aac"
            ),
            "shorter",
        ),
    ],
)
def test_archive_media_qc_fails_closed(evidence: VideoMediaEvidence, message: str) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "archive")
    attempt = service.advance(source.owner_id, intent.attempt_id)
    succeeded = replace(attempt, stage=AttemptStage.SUCCEEDED)
    repository.save_attempt(succeeded)
    blocked = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(evidence), prompt_compiler=StubVideoPromptCompiler()
    )
    archived = blocked.archive(source.owner_id, attempt.attempt_id)
    preserved = repository.get_attempt(attempt.attempt_id)
    assert preserved is not None
    assert archived.archive_stage is ArchiveStage.QC_FAILED
    assert archived.stage is AttemptStage.SUCCEEDED
    assert preserved.archived_asset is not None
    expected_evidence: dict[str, object] = {
        "qc_passed": False,
        "error_code": "VideoRuleViolation",
        "message": next(
            item
            for item in (
                "H3 native audio track evidence is required",
                "H3 output must be 24fps",
                "H3 output resolution does not match project aspect ratio",
                "H3 output is shorter than measured narration; regenerate this group",
            )
            if message in item
        ),
    }
    if message == "resolution":
        # 尺寸失配额外记录期望/实际与工作流身份（归因用）。
        expected_evidence.update(
            {
                "rule_code": "H3 output resolution does not match project aspect ratio",
                "expected_width": 1056,
                "expected_height": 608,
                "actual_width": evidence.width,
                "actual_height": evidence.height,
                "workflow_id": "h3.community.reference-images-to-video",
                "workflow_version": "1.1.0-mvp",
            }
        )
    assert preserved.probe_evidence == expected_evidence
    assert preserved.slot_claimed
    assert not repository.is_current(source.group_id, attempt.attempt_id)
    assert not any(call == "archive" for _, call in engine.calls)
    assert not preserved.cleaned_staged_ids


@pytest.mark.parametrize("failure", ["response_loss", "audit_save"])
def test_archive_confirmation_failure_reconciles_without_second_post(failure: str) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"archive-{failure}")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(replace(submitted, stage=AttemptStage.SUCCEEDED))
    original = repository.save_attempt_with_event
    if failure == "response_loss":
        engine.lose_archive_response_once = True
        pending = service.archive(source.owner_id, intent.attempt_id)
        assert pending.archive_stage is ArchiveStage.CONFIRMING
    else:
        failed = False

        def fail_confirmation(value, event):  # type: ignore[no-untyped-def]
            nonlocal failed
            if not failed and event.event_type == "ARCHIVE_CONFIRMED":
                failed = True
                raise RuntimeError("archive audit failed")
            return original(value, event)

        repository.save_attempt_with_event = fail_confirmation  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="archive audit failed"):
            service.archive(source.owner_id, intent.attempt_id)
        pending = repository.get_attempt(intent.attempt_id)
        assert pending is not None and pending.archive_stage is ArchiveStage.CONFIRMING
        repository.save_attempt_with_event = original  # type: ignore[method-assign]

    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    archived = restarted.archive(source.owner_id, intent.attempt_id)
    assert archived.archive_stage is ArchiveStage.COMPLETE
    assert sum(call == "archive" for _, call in engine.calls) == 1


@pytest.mark.parametrize("failure", ["not_found", "network"])
def test_archive_confirm_inconclusive_inspect_redrives_same_key(failure: str) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"archive-inspect-{failure}")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(replace(submitted, stage=AttemptStage.SUCCEEDED))
    engine.lose_archive_response_once = True
    pending = service.archive(source.owner_id, intent.attempt_id)
    assert pending.archive_stage is ArchiveStage.CONFIRMING
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for _ in range(3):
        if failure == "not_found":
            engine.inspect_not_found = True
        else:
            engine.inspect_error_once = True
        pending = restarted.archive(source.owner_id, intent.attempt_id)
    assert pending.archive_stage is ArchiveStage.CONFIRMED
    assert len(engine.archive_confirmation_effects) == 1


def test_archive_confirm_checkpoint_crash_redrives_same_operation_key_after_bounded_inspect() -> (
    None
):
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, "archive-pre-post-crash")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(replace(submitted, stage=AttemptStage.SUCCEEDED))
    original_save = repository.save_attempt
    crashed = False

    def save_then_crash(value):  # type: ignore[no-untyped-def]
        nonlocal crashed
        saved = original_save(value)
        if not crashed and saved.archive_stage is ArchiveStage.CONFIRMING:
            crashed = True
            raise RuntimeError("crash before archive POST")
        return saved

    repository.save_attempt = save_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash before archive POST"):
        service.archive(source.owner_id, intent.attempt_id)
    repository.save_attempt = original_save  # type: ignore[method-assign]
    engine.inspect_status = "SUCCEEDED"
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    for expected_count in (1, 2):
        pending = restarted.archive(source.owner_id, intent.attempt_id)
        assert pending.archive_stage is ArchiveStage.CONFIRMING
        assert pending.archive_confirm_inspect_count == expected_count
        assert sum(call == "archive" for _, call in engine.calls) == 0
    confirmed = restarted.archive(source.owner_id, intent.attempt_id)
    assert confirmed.archive_stage is ArchiveStage.CONFIRMED
    assert confirmed.archive_confirm_idempotency_key == f"{intent.attempt_id}:archive-confirm"
    assert len(engine.archive_confirmation_effects) == 1
    assert sum(call == "archive" for _, call in engine.calls) == 1


@pytest.mark.parametrize("qc_passes", [False, True])
def test_qc_transition_and_audit_are_atomic_and_event_is_exactly_once(qc_passes: bool) -> None:
    source, repository, engine, assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"qc-atomic-{qc_passes}")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(replace(submitted, stage=AttemptStage.SUCCEEDED))
    evidence = Probe().evidence
    if not qc_passes:
        evidence = replace(evidence, audio_tracks=0)
    orchestrator = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(evidence), prompt_compiler=StubVideoPromptCompiler()
    )
    original = repository.save_attempt_with_event
    failed = False

    def fail_qc_event(value, event):  # type: ignore[no-untyped-def]
        nonlocal failed
        if not failed and event.event_type in {"QC_PASSED", "QC_FAILED"}:
            failed = True
            raise RuntimeError("qc audit insert failed")
        return original(value, event)

    repository.save_attempt_with_event = fail_qc_event  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="qc audit insert failed"):
        orchestrator.archive(source.owner_id, intent.attempt_id)
    persisted = repository.get_attempt(intent.attempt_id)
    assert persisted is not None and persisted.archive_stage is ArchiveStage.STORED
    repository.save_attempt_with_event = original  # type: ignore[method-assign]
    recovered = orchestrator.archive(source.owner_id, intent.attempt_id)
    expected_event = "QC_PASSED" if qc_passes else "QC_FAILED"
    assert (
        len([event for event in repository.provider_events if event.event_type == expected_event])
        == 1
    )
    assert recovered.archive_stage in {ArchiveStage.QC_FAILED, ArchiveStage.COMPLETE}


def test_staged_delete_response_loss_retries_to_not_found_receipt() -> None:
    source, repository, engine, assets, service = setup()
    engine.fail_stage_at = 2
    with pytest.raises(RuntimeError, match="upload failure"):
        intent = service.trigger(source.owner_id, source.group_id, "delete-loss")
        service.advance(source.owner_id, intent.attempt_id)
    engine.fail_stage_at = None
    engine.lose_delete_response_once = True
    with pytest.raises(EngineUncertain):
        service.advance(source.owner_id, intent.attempt_id)
    restarted = DurableGroupVideoOrchestrator(
        repository, engine, assets, Probe(), prompt_compiler=StubVideoPromptCompiler()
    )
    recovered = restarted.advance(source.owner_id, intent.attempt_id)
    assert recovered.stage is AttemptStage.SUBMITTED
    delete_events = [
        event for event in repository.provider_events if event.event_type == "STAGED_DELETE_RECEIPT"
    ]
    assert delete_events and delete_events[-1].receipt["status"] == "NOT_FOUND"


@pytest.mark.parametrize("terminal", [AttemptStage.FAILED, AttemptStage.CANCELLED])
def test_terminal_attempt_with_leftover_staged_assets_runs_cleanup_exit(
    terminal: AttemptStage,
) -> None:
    """人工终态覆盖（2026-10-13 应急）留下的孤儿状态：终态仍持有暂存资产。

    兜底清理出口应补齐清理意图并删除遗留暂存资产，使 outbox 重驱动收敛。
    """
    source, repository, engine, _assets, service = setup()
    intent = service.trigger(source.owner_id, source.group_id, f"leftover-{terminal.value}")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    assert submitted.staged_ids  # 提交前参考图已暂存到引擎节点
    # 模拟人工终态覆盖：直接置终态，未同步清理意图（cleanup_terminal_stage 保持空）。
    overridden = replace(
        submitted,
        stage=terminal,
        archive_stage=ArchiveStage.NOT_STARTED,
        cleanup_terminal_stage=None,
    )
    repository.save_attempt(overridden)
    staged_before = overridden.staged_ids

    healed = service.advance(source.owner_id, intent.attempt_id)

    assert healed.stage is terminal
    assert healed.archive_stage is ArchiveStage.NOT_STARTED
    assert healed.staged_ids == ()
    assert healed.cleaned_staged_ids == ()
    assert healed.cleanup_terminal_stage is None
    assert healed.slot_claimed is False
    assert healed.node_id == submitted.node_id
    deletes = [call for call in engine.calls if call[1].startswith("delete:")]
    assert deletes == [(submitted.node_id, f"delete:{staged_id}") for staged_id in staged_before]
    events = [
        event
        for event in repository.provider_events
        if event.event_type == "TERMINAL_LEFTOVER_CLEANUP"
    ]
    assert len(events) == 1
    assert events[0].receipt["staged_count"] == len(staged_before)
    # 清理完成后再次推进是稳定空操作：不再产生删除调用或清理事件（重驱动已收敛）。
    again = service.advance(source.owner_id, intent.attempt_id)
    assert again.stage is terminal and again.staged_ids == ()
    assert [call for call in engine.calls if call[1].startswith("delete:")] == deletes
    assert (
        len(
            [
                event
                for event in repository.provider_events
                if event.event_type == "TERMINAL_LEFTOVER_CLEANUP"
            ]
        )
        == 1
    )


def test_raw_manifest_sha_and_business_parameter_sha_are_distinct() -> None:
    source, _repository, _engine, _assets, _service = setup()
    assert (
        source.workflow.workflow_manifest_sha256
        == hashlib.sha256(b'{"parameters": {"resolution_preset": "preview"}}\n').hexdigest()
    )
    assert source.workflow.workflow_manifest_sha256 != source.workflow.manifest_parameters_sha256


def test_first_archive_auto_current_and_later_archive_keeps_manual_choice() -> None:
    source, repository, _engine, _assets, service = setup()
    first = service.trigger(source.owner_id, source.group_id, "first")
    first = replace(first, stage=AttemptStage.ARCHIVED, archive_stage=ArchiveStage.COMPLETE)
    repository.attempts[first.attempt_id] = first

    assert repository.mark_current_if_absent(first) is True
    current = repository.get_current(source.owner_id, source.group_id)
    assert current is not None and current.attempt_id == first.attempt_id

    second = service.trigger(source.owner_id, source.group_id, "second")
    second = replace(second, stage=AttemptStage.ARCHIVED, archive_stage=ArchiveStage.COMPLETE)
    repository.attempts[second.attempt_id] = second

    assert repository.mark_current_if_absent(second) is False
    current = repository.get_current(source.owner_id, source.group_id)
    assert current is not None and current.attempt_id == first.attempt_id


def _shared_sentence_timing_source(
    source: AuthoritativeGroupInput, assets: Assets
) -> AuthoritativeGroupInput:
    """共享句链接的 3 句组权威输入：镜头 1={句1,句2}、镜头 2={句2,句3}。

    legacy min/max 对该形态产出重叠区间（镜头 2 从 3.0s 起）；投影 v1 把句 2
    唯一分给镜头 1，区间 (0,6000)/(6000,9000) 有序共享边界。
    """
    shots = (
        DirectorShot(uuid4(), 1, "scene 1", "WIDE", "OBJECTIVE", "action 1", "fn 1", "desc 1"),
        DirectorShot(uuid4(), 2, "scene 2", "CLOSE_UP", "HIGH_ANGLE", "action 2", "fn 2", "desc 2"),
    )
    sentences = (uuid4(), uuid4(), uuid4())
    return replace(
        source,
        sentence_text_sha256=("1" * 64, "2" * 64, "3" * 64),
        narrated_text_sha256=("1" * 64, "2" * 64, "3" * 64),
        sentence_durations_ms=(3000, 3000, 3000),
        narration_qc_passed=(True, True, True),
        normalized_audio_assets=tuple(
            assets.add(f"narration-{index}".encode(), "audio/wav") for index in range(3)
        ),
        raw_audio_assets=tuple(
            assets.add(f"raw-{index}".encode(), "audio/wav") for index in range(3)
        ),
        timeline_ranges_ms=((0, 3000), (3000, 6000), (6000, 9000)),
        sentence_sample_counts=(144000, 144000, 144000),
        sentence_sample_rates=(48000, 48000, 48000),
        timeline_ranges_frames=((0, 144000), (144000, 288000), (288000, 432000)),
        group_sentence_indexes=(0, 1, 2),
        authoritative_group_sentence_indexes=(0, 1, 2),
        group_start_ms=0,
        group_end_ms=9000,
        group_target_duration_ms=9000,
        group_start_frame=0,
        group_end_frame=432000,
        group_sample_rate=48000,
        director_shots=shots,
        shot_sentence_links=(
            ShotSentenceLink(shots[0].shot_id, sentences[0]),
            ShotSentenceLink(shots[0].shot_id, sentences[1]),
            ShotSentenceLink(shots[1].shot_id, sentences[1]),
            ShotSentenceLink(shots[1].shot_id, sentences[2]),
        ),
        group_sentence_ids=sentences,
    )


def test_compiler_input_uses_persisted_projection_ranges_for_v1_records() -> None:
    """2026-10-16：v1 记录编译侧直接使用故事板持久化投影区间（与角标同源同一份），
    不再按 legacy min/max 重算。"""
    source, _repository, _engine, assets, _service = setup()
    source = _shared_sentence_timing_source(source, assets)
    persisted = (ShotTimeRange(1, 0, 6000), ShotTimeRange(2, 6000, 9000))
    source = replace(
        source,
        storyboard_shot_ranges=persisted,
        shot_time_ranges_algorithm="monotone-projection-v1",
    )

    compiler = StubVideoPromptCompiler()
    compile_video_prompt(source, compiler, attempt_id=uuid4())

    assert compiler.inputs[0].shot_ranges == persisted
    # 对照：同一链接形态 legacy 口径会产生重叠区间（镜头 2 自 3.0s 起）。
    legacy = _group_shot_ranges(replace(source, shot_time_ranges_algorithm=""))
    assert [(item.shot_order, item.start_ms, item.end_ms) for item in legacy] == [
        (1, 0, 6000),
        (2, 3000, 9000),
    ]


def test_compiler_input_legacy_records_recompute_unchanged() -> None:
    """历史记录（无算法键）行为不变：按 legacy min/max 重算，忽略持久化字段。"""
    source, _repository, _engine, assets, _service = setup()
    source = _shared_sentence_timing_source(source, assets)
    source = replace(
        source,
        storyboard_shot_ranges=(ShotTimeRange(1, 0, 6000), ShotTimeRange(2, 6000, 9000)),
    )

    compiler = StubVideoPromptCompiler()
    compile_video_prompt(source, compiler, attempt_id=uuid4())

    assert [
        (item.shot_order, item.start_ms, item.end_ms)
        for item in compiler.inputs[0].shot_ranges
    ] == [(1, 0, 6000), (2, 3000, 9000)]


def test_compiler_input_rejects_v1_record_with_inconsistent_persisted_ranges() -> None:
    """v1 记录的持久化区间与镜头快照不齐/序号不符时失败关闭。"""
    source, _repository, _engine, assets, _service = setup()
    source = _shared_sentence_timing_source(source, assets)
    missing = replace(source, shot_time_ranges_algorithm="monotone-projection-v1")
    with pytest.raises(VideoRuleViolation, match="video_prompt_compiler_input_incomplete"):
        _group_shot_ranges(missing)
    wrong_order = replace(
        source,
        storyboard_shot_ranges=(ShotTimeRange(1, 0, 6000), ShotTimeRange(3, 6000, 9000)),
        shot_time_ranges_algorithm="monotone-projection-v1",
    )
    with pytest.raises(VideoRuleViolation, match="video_prompt_compiler_input_incomplete"):
        _group_shot_ranges(wrong_order)


def test_hd_workflow_accepts_native_1600x896_and_rejects_legacy_size() -> None:
    """2026-10-21 事故回归：HD 工作流原生 1600x896 必须通过质检。"""
    hd_evidence = VideoMediaEvidence(
        "video/mp4", 1600, 896, 24.0, 3100, 1, 1, ("mp4",), 3100, 74, 0, 3100, 3100, "aac"
    )
    source, repository, engine, assets, service = setup(
        workflow_id="h3.tubeflow.storyboard-to-video-hd",
        workflow_version="1.0.1",
        probe=Probe(hd_evidence),
    )
    intent = service.trigger(source.owner_id, source.group_id, "hd-native")
    attempt = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(replace(attempt, stage=AttemptStage.SUCCEEDED))
    archived = service.archive(source.owner_id, attempt.attempt_id)
    # 归档确认在 fake engine 上会一路推进到 COMPLETE；关键断言是质检通过。
    assert archived.archive_stage is not ArchiveStage.QC_FAILED
    assert archived.error_code is None
    assert archived.probe_evidence["qc_passed"] is True
    assert archived.probe_evidence["width"] == 1600
    assert archived.probe_evidence["height"] == 896

    legacy_evidence = VideoMediaEvidence(
        "video/mp4", 1056, 608, 24.0, 3100, 1, 1, ("mp4",), 3100, 74, 0, 3100, 3100, "aac"
    )
    source, repository, engine, assets, service = setup(
        workflow_id="h3.tubeflow.storyboard-to-video-hd",
        workflow_version="1.0.1",
        probe=Probe(legacy_evidence),
    )
    intent = service.trigger(source.owner_id, source.group_id, "hd-legacy-size")
    attempt = service.advance(source.owner_id, intent.attempt_id)
    repository.save_attempt(replace(attempt, stage=AttemptStage.SUCCEEDED))
    archived = service.archive(source.owner_id, attempt.attempt_id)
    assert archived.archive_stage is ArchiveStage.QC_FAILED
    evidence = repository.get_attempt(attempt.attempt_id)
    assert evidence is not None
    assert evidence.probe_evidence is not None
    assert evidence.probe_evidence["expected_width"] == 1600
    assert evidence.probe_evidence["expected_height"] == 896
    assert evidence.probe_evidence["actual_width"] == 1056


def test_trigger_maps_capacity_shortage_by_production_mode(monkeypatch) -> None:
    """2026-10-31：无 H3 槽位——AUTO 转可排队重试，MANUAL 维持同步规则错误。"""
    source, repository, _engine, _assets, orchestrator = setup()
    repository.groups[source.group_id] = replace(source, production_mode="AUTO")

    def capacity_then_raise(_attempt: object) -> None:
        raise VideoCapacityUnavailable()

    monkeypatch.setattr(repository, "create_attempt", capacity_then_raise)

    with pytest.raises(RetryableVideoIntentFailure) as captured:
        orchestrator.trigger(source.owner_id, source.group_id, "capacity-auto")
    assert captured.value.code == "video_capacity_unavailable"

    repository.groups[source.group_id] = replace(source, production_mode="MANUAL")
    with pytest.raises(VideoRuleViolation, match="no compatible H3 node has capacity"):
        orchestrator.trigger(source.owner_id, source.group_id, "capacity-manual")


def test_derived_seed_is_deterministic_bounded_and_key_scoped() -> None:
    # 2026-11-03 AB_REVIEW_AND_FOLLOWUPS §3：seed 由 (group_id, idempotency_key) 确定性派生。
    group_id = uuid4()
    first = _derived_seed(group_id, "click-seed")
    assert first == _derived_seed(group_id, "click-seed")
    assert 0 <= first < 2**53
    assert first != _derived_seed(group_id, "click-other")
    assert first != _derived_seed(uuid4(), "click-seed")


def test_unseeded_trigger_records_derived_seed_and_reuses_it_on_retry() -> None:
    source, _repository, _engine, _assets, service = setup(seed=None)
    first = service.trigger(source.owner_id, source.group_id, "click-seed")
    snapshot = first.input_snapshot or {}
    seed = snapshot["seed"]
    assert seed == _derived_seed(source.group_id, "click-seed")
    assert first.request["parameters"]["seed"] == seed
    # 同一幂等键重试：不冲突、同一 attempt、同一 seed（不得因 attempt_id 变化而漂移）。
    second = service.trigger(source.owner_id, source.group_id, "click-seed")
    assert second.attempt_id == first.attempt_id
    assert (second.input_snapshot or {})["seed"] == seed


def test_worker_rebuild_submits_the_snapshot_seed() -> None:
    source, _repository, _engine, _assets, service = setup(seed=None)
    attempt = service.trigger(source.owner_id, source.group_id, "click-rebuild")
    generation = _generation_from_snapshot(attempt.input_snapshot or {})
    assert generation.seed == (attempt.input_snapshot or {})["seed"]
    assert generation.seed == _derived_seed(source.group_id, "click-rebuild")


def test_explicit_seed_is_never_overridden_by_derivation() -> None:
    source, _repository, _engine, _assets, service = setup(seed=123456789)
    attempt = service.trigger(source.owner_id, source.group_id, "click-explicit")
    assert (attempt.input_snapshot or {})["seed"] == 123456789


def test_validate_reference_intent_keeps_derived_seed_hashes_consistent() -> None:
    source, _repository, _engine, _assets, service = setup(seed=None)
    attempt = service.trigger(source.owner_id, source.group_id, "click-validate")
    validate_reference_intent(source, attempt)


def test_legacy_attempt_without_seed_remains_idempotent_for_same_key() -> None:
    # 2026-11-03 用户决策 3：seed 记录上线前创建的键，快照 seed 为 null；重试不得报 idempotency_conflict。
    source, repository, _engine, _assets, service = setup(seed=None)
    attempt_id = uuid4()
    legacy = _authorize(source)  # 不传幂等键 → 不派生 seed，等价于旧版快照
    snapshot = _input_snapshot(source, legacy, compiled=None)
    repository.create_attempt(
        PersistentAttempt(
            attempt_id,
            source.owner_id,
            source.project_id,
            source.group_id,
            source.generation_id,
            "legacy-click",
            canonical_sha256(snapshot),
            canonical_sha256({}),
            {},
            input_snapshot=snapshot,
        )
    )
    result = service.trigger(source.owner_id, source.group_id, "legacy-click")
    assert result.attempt_id == attempt_id
    assert (result.input_snapshot or {})["seed"] is None
