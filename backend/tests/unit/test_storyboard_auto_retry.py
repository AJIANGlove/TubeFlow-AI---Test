from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.grsai_grid_sheet import GrsaiGridSheetImageProvider
from tubeflow_test.adapters.in_memory_storyboards import InMemoryStoryboardRepository
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorPointerRow,
    StoryboardCurrentRow,
    StoryboardGenerationRow,
    StoryboardMediaRow,
    StoryboardOutboxRow,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
    _generation_row,
)
from tubeflow_test.adapters.storyboard_image_qc import PillowStoryboardTechnicalQc
from tubeflow_test.api.storyboards import create_storyboard_router
from tubeflow_test.application.storyboard_failover import DEFAULT_STORYBOARD_FAILOVER_POLICY
from tubeflow_test.application.storyboards import (
    DirectorPlanService,
    GridSheetReference,
    ProviderChannelUnavailableError,
    ProviderPoll,
    ProviderReconcileUnsupportedError,
    ProviderSubmission,
    ProviderSubmitNotDeliveredError,
    ProviderSubmitRejectedError,
    ProviderSubmitServerError,
    ProviderSubmitThrottledError,
    StoryboardContext,
    StoryboardGenerationService,
)
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.director_generation import DirectorSentenceSnapshot
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    STORYBOARD_AUTO_MAX_SUBMISSIONS,
    CharacterAssetSnapshot,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    GenerationStatus,
    ReviewState,
    ShotSentenceLink,
    StoryboardAssetKind,
    StoryboardGeneration,
    storyboard_auto_retry_delay_seconds,
    storyboard_auto_submission_budget_exhausted,
)
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.workflows.storyboards import (
    StoryboardActivities,
    StoryboardRecoveryResult,
    StoryboardWorkflowInput,
    _recovery_status_delay,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class ScriptedProvider:
    """Stub provider with scripted submit outcomes; records every real submission."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.submissions = 0
        self.reconciled: ProviderSubmission | None = None
        self.reconcile_error: Exception | None = None
        self.reconcile_calls = 0
        self.polls: list[ProviderPoll] = []

    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[object, ...] = ()
    ) -> ProviderSubmission:
        del references
        self.submissions += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, ProviderSubmission)
        return outcome

    def reconcile(self, idempotency_key: str) -> ProviderSubmission | None:
        del idempotency_key
        self.reconcile_calls += 1
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconciled

    def poll(self, task_id: str) -> ProviderPoll:
        del task_id
        outcome = self.polls.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def download(self, download_token: str, destination: Path) -> None:
        del download_token
        Image.new("RGB", (64, 36), "white").save(destination, "PNG")


class Storage:
    def put_verified(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        source: Path,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef:
        del owner_id, project_id
        payload = source.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        return MediaAssetRef(uuid4(), expected_sha256, media_type, len(payload))

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        del asset
        return b"character"

    def delete_exact(self, asset: MediaAssetRef) -> None:
        raise NotImplementedError


def image_digest() -> str:
    stream = BytesIO()
    Image.new("RGB", (64, 36), "white").save(stream, "PNG")
    return hashlib.sha256(stream.getvalue()).hexdigest()


def setup(
    tmp_path: Path, outcomes: list[object], mode: str = "AUTO"
) -> tuple[
    InMemoryStoryboardRepository,
    StoryboardGenerationService,
    ScriptedProvider,
    UUID,
    UUID,
    DirectorPlanRevision,
]:
    owner, project, group, sentence = uuid4(), uuid4(), uuid4(), uuid4()
    repo = InMemoryStoryboardRepository()
    character_payload = b"character"
    character = CharacterAssetSnapshot(
        1,
        uuid4(),
        uuid4(),
        hashlib.sha256(character_payload).hexdigest(),
        "image/png",
        len(character_payload),
    )
    repo.add_context(
        StoryboardContext(
            owner,
            project,
            group,
            (sentence,),
            True,
            (character,),
            "ink",
            sentences=(DirectorSentenceSnapshot(sentence, 1, "line", "2" * 64, 0, 3000),),
        )
    )
    repo.set_production_mode(owner, project, mode)
    shot = DirectorShot(uuid4(), 1, "station", "wide", "eye", "arrives", "establish", "Arrival")
    revision = DirectorPlanRevision(
        uuid4(),
        owner,
        project,
        group,
        1,
        DirectorRevisionKind.APPLIED,
        (shot,),
        (ShotSentenceLink(shot.shot_id, sentence),),
        (sentence,),
        ReviewState.NOT_REQUIRED,
        (),
        NOW,
    )
    repo.save_revision(revision)
    provider = ScriptedProvider(outcomes)
    service = StoryboardGenerationService(
        repo,
        provider,
        Storage(),
        PillowStoryboardTechnicalQc(),
        Ids(),
        Clock(),
        tmp_path,
        DEFAULT_STORYBOARD_FAILOVER_POLICY,
    )
    return repo, service, provider, owner, group, revision


def create(
    service: StoryboardGenerationService, owner: UUID, group: UUID, revision: UUID, key: str = "key"
) -> StoryboardGeneration:
    return service.create(
        group_id=group, owner_id=owner, expected_applied_revision_id=revision, idempotency_key=key
    )


def minimal_generation(**overrides: object) -> StoryboardGeneration:
    shot = DirectorShot(uuid4(), 1, "scene", "wide", "eye", "arrive", "establish", "desc")
    character = CharacterAssetSnapshot(1, uuid4(), uuid4(), "a" * 64, "image/png", 10)
    values: dict[str, object] = {
        "generation_id": uuid4(),
        "owner_id": uuid4(),
        "project_id": uuid4(),
        "group_id": uuid4(),
        "applied_revision_id": uuid4(),
        "shots_snapshot": (shot,),
        "sentence_links_snapshot": (ShotSentenceLink(shot.shot_id, uuid4()),),
        "version": 1,
        "kind": StoryboardAssetKind.GRID_SHEET,
        "prompt": "grid",
        "character_assets_snapshot": (character,),
        "style_snapshot": "ink",
        "model": "fake",
        "parameters": {},
        "idempotency_key": "key",
        "provider_idempotency_key": "b" * 64,
        "canonical_request_sha256": "c" * 64,
        "status": GenerationStatus.INTENT_PERSISTED,
        "created_at": NOW,
    }
    values.update(overrides)
    return StoryboardGeneration(**values)  # type: ignore[arg-type]


def successor_of(failed: StoryboardGeneration) -> StoryboardGeneration:
    key = f"auto-retry-{failed.generation_id}"
    return replace(
        failed,
        generation_id=uuid4(),
        version=1,
        idempotency_key=key,
        provider_idempotency_key=hashlib.sha256(f"{failed.owner_id}:{key}".encode()).hexdigest(),
        status=GenerationStatus.INTENT_PERSISTED,
        created_at=NOW,
        state_revision=0,
        provider_task_id=None,
        provider_receipt=None,
        failure_stage=None,
        failure_code=None,
        auto_attempt=failed.auto_attempt + 1,
        supersedes_generation_id=failed.generation_id,
        failover_model_attempt=(
            failed.failover_model_attempt + 1 if failed.failover_model_attempt is not None else None
        ),
        failover_total_attempt=(
            failed.failover_total_attempt + 1 if failed.failover_total_attempt is not None else None
        ),
    )


def test_auto_retry_delay_seconds_boundaries() -> None:
    assert STORYBOARD_AUTO_MAX_SUBMISSIONS == 14
    assert storyboard_auto_retry_delay_seconds(1) == 5.0
    assert storyboard_auto_retry_delay_seconds(2) == 15.0
    assert storyboard_auto_retry_delay_seconds(3) == 30.0
    assert storyboard_auto_retry_delay_seconds(0) == 5.0
    assert storyboard_auto_retry_delay_seconds(99) == 30.0
    assert storyboard_auto_retry_delay_seconds(1, 120.0) == 120.0
    assert storyboard_auto_retry_delay_seconds(2, 1.0) == 15.0
    assert storyboard_auto_submission_budget_exhausted(14)
    assert storyboard_auto_submission_budget_exhausted(15)
    assert not storyboard_auto_submission_budget_exhausted(1)
    assert not storyboard_auto_submission_budget_exhausted(13)


def test_generation_lineage_defaults_and_validation() -> None:
    value = minimal_generation()
    assert value.auto_attempt == 1
    assert value.supersedes_generation_id is None
    with pytest.raises(ValueError, match="auto attempt"):
        minimal_generation(auto_attempt=0)


def test_auto_connect_failure_schedules_successor_with_same_input(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [ProviderSubmitNotDeliveredError("connect-stage failure"), ProviderSubmission("r-2", "ok")],
    )
    first = create(service, owner, group, revision.revision_id)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "submit"
    assert failed.failure_code == "ProviderSubmitNotDeliveredError"
    assert provider.submissions == 1
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert successor.generation_id != first.generation_id
    assert successor.status is GenerationStatus.INTENT_PERSISTED
    assert successor.auto_attempt == 2
    assert successor.supersedes_generation_id == first.generation_id
    assert successor.canonical_request_sha256 == first.canonical_request_sha256
    assert successor.prompt == first.prompt
    assert successor.version == 2
    assert repo.retry_schedules[successor.generation_id] == NOW + timedelta(seconds=5)
    # 工作流重放/重复调用已终结尝试：不产生新记录
    assert service.resume(first.generation_id, owner).status is GenerationStatus.FAILED
    assert len(repo.list_generation_history(owner, group)) == 2
    submitted = service.resume(successor.generation_id, owner)
    assert submitted.status is GenerationStatus.SUBMITTED
    assert provider.submissions == 2


def test_auto_throttled_submit_respects_retry_after_then_backoff(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [
            ProviderSubmitThrottledError("provider_http_429", retry_after_seconds=120.0),
            ProviderSubmitThrottledError("provider_http_429"),
            ProviderSubmission("r-3", "ok"),
        ],
    )
    first = create(service, owner, group, revision.revision_id)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_code == "provider_http_429"
    second = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert second is not None
    # 429 尊重 Retry-After（大于 5s 基准退避）
    assert repo.retry_schedules[second.generation_id] == NOW + timedelta(seconds=120)
    failed_second = service.resume(second.generation_id, owner)
    assert failed_second.failure_code == "provider_http_429"
    third = repo.find_generation_by_key(owner, f"auto-retry-{second.generation_id}")
    assert third is not None
    # 无 Retry-After 时走 5s/15s/30s 固定退避第二档
    assert repo.retry_schedules[third.generation_id] == NOW + timedelta(seconds=15)
    assert third.auto_attempt == 3
    assert third.supersedes_generation_id == second.generation_id
    assert service.resume(third.generation_id, owner).status is GenerationStatus.SUBMITTED
    assert provider.submissions == 3


def test_auto_submission_budget_exhausted_after_sixteen_connect_failures(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmitNotDeliveredError("connect")] * 14
    )
    attempt = create(service, owner, group, revision.revision_id)
    for expected_total in range(1, 15):
        failed = service.resume(attempt.generation_id, owner)
        assert failed.status is GenerationStatus.FAILED
        assert failed.failover_total_attempt == expected_total
        if expected_total < 14:
            successor = repo.find_generation_by_key(owner, f"auto-retry-{attempt.generation_id}")
            assert successor is not None
            assert successor.failover_total_attempt == expected_total + 1
            attempt = successor
    assert failed.failure_stage == "submit"
    assert failed.failure_code == "failover_budget_exhausted"
    assert provider.submissions == 14
    assert repo.find_generation_by_key(owner, f"auto-retry-{attempt.generation_id}") is None
    history = repo.list_generation_history(owner, group)
    assert [item.version for item in history] == list(range(14, 0, -1))
    assert [item.auto_attempt for item in history] == list(range(14, 0, -1))
    assert [item.failover_route_index for item in history] == [
        6,
        6,
        5,
        5,
        4,
        4,
        3,
        3,
        2,
        2,
        1,
        1,
        0,
        0,
    ]
    assert [item.failover_model_attempt for item in history] == [2, 1] * 7


def test_auto_rejected_submit_is_terminal_without_retry(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmitRejectedError("definitive rejection")]
    )
    first = create(service, owner, group, revision.revision_id)
    failed = service.resume(first.generation_id, owner)
    # §15.3：既定失败（4xx 等）不自动重试，直接终态
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "submit"
    assert failed.failure_code == "ProviderSubmitRejectedError"
    assert provider.submissions == 1
    assert repo.retry_schedules == {}
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None


def test_auto_unknown_submit_requests_recovery_and_reconciles_existing_task(
    tmp_path: Path,
) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path, [TimeoutError("read")])
    first = create(service, owner, group, revision.revision_id)
    unknown = service.resume(first.generation_id, owner)
    assert unknown.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert unknown.failure_stage == "submit"
    assert provider.submissions == 1
    # §15.4：落未知态的同事务登记自动对账（STORYBOARD_RECOVERY_REQUESTED 等价物）
    assert [generation_id for generation_id, _ in repo.recovery_events] == [first.generation_id]
    # 对账发现远端任务存在：照常续推归档，不重提
    provider.reconciled = ProviderSubmission("remote-lost", "recovered")
    recovered = service.resume(first.generation_id, owner)
    assert recovered.status is GenerationStatus.SUBMITTED
    assert recovered.provider_task_id == "remote-lost"
    assert provider.submissions == 1
    # 与活动包装器一致：每次 resume 结束后释放处理租约
    service.release(recovered)
    provider.polls = [ProviderPoll(GenerationStatus.SUCCEEDED, "ok", "token", image_digest())]
    completed = service.resume(first.generation_id, owner)
    assert completed.status is GenerationStatus.SUCCEEDED
    current = repo.get_current_generation(owner, group)
    assert current is not None and current.generation_id == first.generation_id
    assert provider.submissions == 1
    assert len(repo.list_generation_history(owner, group)) == 1


def test_auto_unknown_reconcile_empty_resubmits_after_third_failure(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [TimeoutError("lost"), ProviderSubmission("r-2", "ok")]
    )
    first = create(service, owner, group, revision.revision_id)
    unknown = service.resume(first.generation_id, owner)
    assert unknown.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert unknown.unknown_origin_stage == "submit"
    assert unknown.unknown_origin_code == "TimeoutError"
    for expected_count in (1, 2):
        unresolved = service.resume(first.generation_id, owner)
        assert unresolved.status is GenerationStatus.REMOTE_STATE_UNKNOWN
        assert unresolved.failure_stage == "reconcile"
        assert unresolved.failure_code == "reconcile_no_remote_task"
        assert unresolved.recovery_failure_count == expected_count
        assert provider.submissions == 1
        assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
        service.release(unresolved)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.recovery_failure_count == 3
    assert failed.unknown_origin_stage == "submit"
    assert failed.unknown_origin_code == "TimeoutError"
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert successor.failover_total_attempt == 2
    assert successor.recovery_failure_count == 0
    assert successor.unknown_origin_stage is None
    assert repo.retry_schedules[successor.generation_id] == NOW + timedelta(seconds=30)
    assert provider.reconcile_calls == 3
    assert provider.submissions == 1
    assert len(repo.recovery_events) == 1
    assert service.resume(successor.generation_id, owner).status is GenerationStatus.SUBMITTED
    assert provider.submissions == 2


def test_recovery_activity_returns_backoff_and_terminal_after_replacement(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path, [TimeoutError("lost")])
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    activities = StoryboardActivities(
        director=None,  # type: ignore[arg-type]
        storyboards=service,
        project_director=None,  # type: ignore[arg-type]
    )
    value = StoryboardWorkflowInput(
        str(owner), str(first.generation_id), first.canonical_request_sha256
    )
    first_result = activities.reconcile_storyboard(value)
    assert first_result.status == "REMOTE_STATE_UNKNOWN"
    assert first_result.retry_after_seconds == 5.0
    second_result = activities.reconcile_storyboard(value)
    assert second_result.status == "REMOTE_STATE_UNKNOWN"
    assert second_result.retry_after_seconds == 15.0
    third_result = activities.reconcile_storyboard(value)
    assert third_result.status == "FAILED"
    assert provider.reconcile_calls == 3
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is not None


def test_recovery_result_normalizes_legacy_dataclass_and_temporal_dict() -> None:
    assert _recovery_status_delay("FAILED") == ("FAILED", 5.0)
    assert _recovery_status_delay(StoryboardRecoveryResult("SUCCEEDED", 30.0)) == (
        "SUCCEEDED",
        30.0,
    )
    assert _recovery_status_delay(
        {"status": "REMOTE_STATE_UNKNOWN", "retry_after_seconds": 15.0}
    ) == ("REMOTE_STATE_UNKNOWN", 15.0)


def test_auto_unknown_reconcile_recovers_before_third_and_resets_failures(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path, [TimeoutError("lost")])
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    unresolved = service.resume(first.generation_id, owner)
    assert unresolved.recovery_failure_count == 1
    service.release(unresolved)
    provider.reconciled = ProviderSubmission("remote-lost", "recovered")
    recovered = service.resume(first.generation_id, owner)
    assert recovered.status is GenerationStatus.SUBMITTED
    assert recovered.provider_task_id == "remote-lost"
    assert recovered.recovery_failure_count == 0
    assert provider.submissions == 1
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None


def test_auto_unknown_without_id_resubmits_immediately_when_reconcile_unsupported(
    tmp_path: Path,
) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [TimeoutError("lost"), ProviderSubmission("r-2", "ok")]
    )
    provider.reconcile_error = ProviderReconcileUnsupportedError("unsupported")
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_code == "ProviderReconcileUnsupportedError"
    assert failed.recovery_failure_count == 0
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert repo.retry_schedules[successor.generation_id] == NOW + timedelta(seconds=5)
    assert provider.reconcile_calls == 1
    assert provider.submissions == 1
    assert service.resume(successor.generation_id, owner).status is GenerationStatus.SUBMITTED
    assert provider.submissions == 2


def test_auto_unknown_without_id_stops_after_fourteen_attempts(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path, [TimeoutError("lost")] * 14)
    provider.reconcile_error = ProviderReconcileUnsupportedError("unsupported")
    attempt = create(service, owner, group, revision.revision_id)
    for expected_total in range(1, 15):
        unknown = service.resume(attempt.generation_id, owner)
        assert unknown.status is GenerationStatus.REMOTE_STATE_UNKNOWN
        failed = service.resume(attempt.generation_id, owner)
        assert failed.status is GenerationStatus.FAILED
        assert failed.failover_total_attempt == expected_total
        if expected_total < 14:
            successor = repo.find_generation_by_key(owner, f"auto-retry-{attempt.generation_id}")
            assert successor is not None
            attempt = successor
    assert failed.failure_code == "remote_state_unknown_exhausted"
    assert provider.submissions == 14
    assert repo.find_generation_by_key(owner, f"auto-retry-{attempt.generation_id}") is None
    assert len(repo.list_generation_history(owner, group)) == 14


def test_supersede_replay_returns_terminal_without_duplicate(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmitNotDeliveredError("connect")]
    )
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    # 模拟 worker 重启/outbox redrive 后持旧快照重放同一仓储调用
    stale_failed = replace(first, status=GenerationStatus.SUBMITTING, state_revision=1)
    replayed = repo.supersede_with_auto_retry(
        stale_failed,
        failure_stage="submit",
        failure_code="ProviderSubmitNotDeliveredError",
        successor=successor_of(first),
        next_attempt_at=NOW + timedelta(seconds=5),
    )
    assert replayed.status is GenerationStatus.FAILED
    assert replayed.failure_code == "ProviderSubmitNotDeliveredError"
    assert len(repo.list_generation_history(owner, group)) == 2
    assert list(repo.retry_schedules) == [successor.generation_id]
    assert provider.submissions == 1


def test_manual_mode_uses_same_failover_chain(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [ProviderSubmitNotDeliveredError("connect"), ProviderSubmission("r-2", "ok")],
        mode="MANUAL",
    )
    first = create(service, owner, group, revision.revision_id)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "submit"
    assert failed.failure_code == "ProviderSubmitNotDeliveredError"
    assert repo.recovery_events == []
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert successor.auto_attempt == 2
    assert successor.failover_total_attempt == 2
    assert successor.supersedes_generation_id == first.generation_id
    assert repo.retry_schedules[successor.generation_id] == NOW + timedelta(seconds=5)
    assert service.resume(successor.generation_id, owner).status is GenerationStatus.SUBMITTED
    assert provider.submissions == 2


def test_manual_mode_429_uses_failover_retry_after(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [ProviderSubmitThrottledError("provider_http_429", retry_after_seconds=30.0)],
        mode="MANUAL",
    )
    first = create(service, owner, group, revision.revision_id)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "submit"
    assert failed.failure_code == "provider_http_429"
    assert provider.submissions == 1
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert repo.retry_schedules[successor.generation_id] == NOW + timedelta(seconds=30)


def test_manual_unknown_stays_manual_and_reconcile_entry_unchanged(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [TimeoutError("lost")], mode="MANUAL"
    )
    first = create(service, owner, group, revision.revision_id)
    stuck = service.resume(first.generation_id, owner)
    assert stuck.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert repo.recovery_events == []
    again = service.resume(first.generation_id, owner)
    assert again.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert again.failure_stage == "reconcile"
    assert provider.submissions == 1
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
    # 既有人工对账入口（RECONCILE 动作对应的授权接口）不变
    recovered = service.request_recovery(owner, first.generation_id)
    assert recovered.status is GenerationStatus.REMOTE_STATE_UNKNOWN


def test_manual_unknown_does_not_resubmit_when_reconcile_unsupported(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [TimeoutError("lost")], mode="MANUAL"
    )
    provider.reconcile_error = ProviderReconcileUnsupportedError("unsupported")
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    unresolved = service.resume(first.generation_id, owner)
    assert unresolved.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert unresolved.failure_code == "ProviderReconcileUnsupportedError"
    assert provider.submissions == 1
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None


def test_missing_project_is_treated_as_manual(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path, [TimeoutError("lost")])
    repo.production_modes.clear()
    first = create(service, owner, group, revision.revision_id)
    stuck = service.resume(first.generation_id, owner)
    assert stuck.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert repo.recovery_events == []


def test_public_contract_reflects_scheduled_retry_and_exhaustion(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmitNotDeliveredError("connect")] * 14
    )
    account = Account(owner, "user", "hash", AccountRole.USER)
    app = FastAPI()
    director = DirectorPlanService(repo, Ids(), Clock())
    app.include_router(create_storyboard_router(director, service, lambda: account))
    client = TestClient(app)
    attempt = create(service, owner, group, revision.revision_id)
    service.resume(attempt.generation_id, owner)
    second = repo.find_generation_by_key(owner, f"auto-retry-{attempt.generation_id}")
    assert second is not None
    queued = client.get(f"/storyboard-groups/generations/{second.generation_id}")
    assert queued.status_code == 200
    assert queued.json()["public_status"] == "QUEUED"
    assert queued.json()["provider"] == "grsai"
    assert queued.json()["failover"]["total_attempt"] == 2
    attempt = second
    for _ in range(2, 15):
        service.resume(attempt.generation_id, owner)
        if attempt.failover_total_attempt < 14:
            successor = repo.find_generation_by_key(owner, f"auto-retry-{attempt.generation_id}")
            assert successor is not None
            attempt = successor
    exhausted = client.get(f"/storyboard-groups/generations/{attempt.generation_id}")
    assert exhausted.json()["public_status"] == "FAILED"
    history = client.get(f"/storyboard-groups/{group}/history").json()
    assert history[0]["failure"] == {"stage": "submit", "code": "failover_budget_exhausted"}


def test_public_contract_keeps_manual_reconcile_action(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [TimeoutError("lost")], mode="MANUAL"
    )
    account = Account(owner, "user", "hash", AccountRole.USER)
    app = FastAPI()
    director = DirectorPlanService(repo, Ids(), Clock())
    app.include_router(create_storyboard_router(director, service, lambda: account))
    client = TestClient(app)
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    body = client.get(f"/storyboard-groups/generations/{first.generation_id}").json()
    assert body["public_status"] == "RECOVERY_REQUIRED"
    assert "RECONCILE" in body["allowed_actions"]


def test_provider_failed_poll_retries_same_model_then_moves_to_next(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmission("r-1", "ok"), ProviderSubmission("r-2", "ok")]
    )
    first = create(service, owner, group, revision.revision_id)
    assert service.resume(first.generation_id, owner).status is GenerationStatus.SUBMITTED
    provider.polls = [ProviderPoll(GenerationStatus.FAILED, "failed", failure_code="error")]
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    second = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert second is not None
    assert (second.failover_route_index, second.failover_model_attempt) == (0, 2)
    assert service.resume(second.generation_id, owner).status is GenerationStatus.SUBMITTED
    provider.polls = [
        ProviderPoll(GenerationStatus.FAILED, "failed", failure_code="generate image failed")
    ]
    assert service.resume(second.generation_id, owner).status is GenerationStatus.FAILED
    third = repo.find_generation_by_key(owner, f"auto-retry-{second.generation_id}")
    assert third is not None
    assert third.model == "gpt-image-2"
    assert third.failover_route_index == 1
    assert third.failover_model_attempt == 1
    assert third.failover_total_attempt == 3
    assert third.canonical_request_sha256 != second.canonical_request_sha256
    assert provider.submissions == 2


def test_provider_moderation_failure_never_fails_over(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmission("r-1", "ok")]
    )
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    provider.polls = [
        ProviderPoll(GenerationStatus.FAILED, "rejected", failure_code="output_moderation")
    ]
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_code == "output_moderation"
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
    assert provider.submissions == 1


def test_channel_auth_failure_skips_all_remaining_grsai_models(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [
            ProviderChannelUnavailableError("401", failure_code="provider_auth_failed"),
            ProviderSubmission("pix-1", "ok"),
        ],
    )
    first = create(service, owner, group, revision.revision_id)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert "storyboard_provider_channel_unavailable" in caplog.text
    assert "provider_auth_failed" in caplog.text
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert successor.provider == "pix"
    assert successor.model == "tt-image-2.5"
    assert successor.failover_route_index == 4
    assert successor.failover_model_attempt == 1
    assert successor.failover_total_attempt == 2
    assert service.resume(successor.generation_id, owner).status is GenerationStatus.SUBMITTED
    assert provider.submissions == 2


def test_submit_5xx_with_task_id_only_recovers_original_task(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [
            ProviderSubmitServerError(
                "503", submission=ProviderSubmission("existing-task", "receipt")
            )
        ],
    )
    first = create(service, owner, group, revision.revision_id)
    submitted = service.resume(first.generation_id, owner)
    assert submitted.status is GenerationStatus.SUBMITTED
    assert submitted.provider_task_id == "existing-task"
    assert submitted.submission_receipt == "receipt"
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
    assert provider.submissions == 1


def test_poll_query_failures_resubmit_original_task_after_third_failure(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmission("r-1", "ok"), ProviderSubmission("r-2", "ok")]
    )
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    for expected_count in (1, 2):
        provider.polls = [TimeoutError("query timeout")]
        unresolved = service.resume(first.generation_id, owner)
        assert unresolved.status is GenerationStatus.REMOTE_STATE_UNKNOWN
        assert unresolved.failure_stage == "poll"
        assert unresolved.recovery_failure_count == expected_count
        assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
        service.release(unresolved)
    provider.polls = [TimeoutError("query timeout")]
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.recovery_failure_count == 3
    assert failed.unknown_origin_stage == "poll"
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert repo.retry_schedules[successor.generation_id] == NOW + timedelta(seconds=30)
    assert provider.submissions == 1
    assert service.resume(successor.generation_id, owner).status is GenerationStatus.SUBMITTED
    assert provider.submissions == 2


def test_poll_query_recovery_resets_consecutive_failures_without_resubmit(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmission("r-1", "ok")]
    )
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    provider.polls = [TimeoutError("query timeout")]
    unresolved = service.resume(first.generation_id, owner)
    assert unresolved.recovery_failure_count == 1
    service.release(unresolved)
    provider.polls = [ProviderPoll(GenerationStatus.RUNNING, "running")]
    running = service.resume(first.generation_id, owner)
    assert running.status is GenerationStatus.RUNNING
    assert running.recovery_failure_count == 0
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
    service.release(running)
    provider.polls = [TimeoutError("query timeout")]
    unresolved = service.resume(first.generation_id, owner)
    assert unresolved.recovery_failure_count == 1
    assert provider.submissions == 1


def test_new_root_chain_is_blocked_while_any_successor_is_in_flight(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [ProviderSubmitNotDeliveredError("connect"), ProviderSubmitNotDeliveredError("connect")],
    )
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    second = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert second is not None
    service.resume(second.generation_id, owner)
    third = repo.find_generation_by_key(owner, f"auto-retry-{second.generation_id}")
    assert third is not None and third.model == "gpt-image-2"
    with pytest.raises(DomainError) as blocked:
        create(service, owner, group, revision.revision_id, "new-root")
    assert blocked.value.code == "remote_state_unresolved"
    assert provider.submissions == 2
    assert len(repo.list_generation_history(owner, group)) == 3
    with pytest.raises(DomainError) as changed_revision:
        repo.create_generation(
            replace(
                third,
                generation_id=uuid4(),
                applied_revision_id=uuid4(),
                idempotency_key="new-revision-root",
            )
        )
    assert changed_revision.value.code == "remote_state_unresolved"
    assert len(repo.list_generation_history(owner, group)) == 3


def test_submit_429_with_task_id_recovers_original_without_successor(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path,
        [
            ProviderSubmitThrottledError(
                "429", submission=ProviderSubmission("existing-429", "receipt")
            )
        ],
    )
    first = create(service, owner, group, revision.revision_id)
    submitted = service.resume(first.generation_id, owner)
    assert submitted.status is GenerationStatus.SUBMITTED
    assert submitted.provider_task_id == "existing-429"
    assert submitted.submission_receipt == "receipt"
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
    assert provider.submissions == 1


def test_legacy_portrait_intent_is_finalized_without_new_submission(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path, [])
    first = create(service, owner, group, revision.revision_id)
    context = repo.context(owner, group)
    assert context is not None
    repo.add_context(replace(context, aspect_ratio="9:16"))
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_code == "aspect_ratio_not_supported"
    assert provider.submissions == 0


def test_reference_read_failure_is_local_failed_not_unknown(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path, [])

    class FailingStorage(Storage):
        def read_verified(self, asset: MediaAssetRef) -> bytes:
            raise OSError("local object storage unavailable")

    service._storage = FailingStorage()  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id)
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "reference_read"
    assert failed.failure_code == "OSError"
    assert provider.submissions == 0
    assert repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}") is None
    assert repo.recovery_events == []


def test_public_failure_code_redacts_legacy_signed_url(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path, [])
    account = Account(owner, "user", "hash", AccountRole.USER)
    app = FastAPI()
    director = DirectorPlanService(repo, Ids(), Clock())
    app.include_router(create_storyboard_router(director, service, lambda: account))
    client = TestClient(app)
    first = create(service, owner, group, revision.revision_id)
    repo.generations[first.generation_id] = replace(
        first,
        status=GenerationStatus.FAILED,
        failure_stage="provider",
        failure_code="https://cdn.test/result.png?signature=DO_NOT_EXPOSE",
    )
    body = client.get(f"/storyboard-groups/generations/{first.generation_id}").json()
    assert "DO_NOT_EXPOSE" not in body["failure"]["code"]
    assert body["failure"]["code"] == "<redacted-url>"


# --- grsai 适配器 429 分类（MockTransport，不真实外发） ---


def grsai_provider(handler) -> GrsaiGridSheetImageProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GrsaiGridSheetImageProvider("https://grsai.test", "sk-test", client=client)


def test_grsai_submit_429_is_throttled_and_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/draw/completions"
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"code": 429})

    provider = grsai_provider(handler)
    with pytest.raises(ProviderSubmitThrottledError) as captured:
        provider.submit_grid_sheet(minimal_generation())
    assert captured.value.retry_after_seconds == 7.0


def test_grsai_submit_429_without_retry_after_header() -> None:
    provider = grsai_provider(lambda request: httpx.Response(429, json={"code": 429}))
    with pytest.raises(ProviderSubmitThrottledError) as captured:
        provider.submit_grid_sheet(minimal_generation())
    assert captured.value.retry_after_seconds is None


def test_grsai_submit_4xx_remains_definitive_rejection() -> None:
    provider = grsai_provider(lambda request: httpx.Response(400, json={"code": 400}))
    with pytest.raises(ProviderSubmitRejectedError, match="HTTP 400"):
        provider.submit_grid_sheet(minimal_generation())


def test_grsai_submit_5xx_is_definite_retryable_failure() -> None:
    provider = grsai_provider(lambda request: httpx.Response(503))
    with pytest.raises(ProviderSubmitServerError):
        provider.submit_grid_sheet(minimal_generation())


def test_grsai_reference_upload_429_is_throttled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/client/resource/newUploadTokenZH"
        return httpx.Response(429, headers={"Retry-After": "3"})

    provider = grsai_provider(handler)
    reference = GridSheetReference(1, "image/png", "a" * 64, b"bytes")
    with pytest.raises(ProviderSubmitThrottledError) as captured:
        provider.submit_grid_sheet(minimal_generation(), (reference,))
    assert captured.value.retry_after_seconds == 3.0


# --- SQL 仓储：原子性、重放安全与权威计数 ---


def sql_repositories(
    path: Path,
) -> tuple[SqlAlchemyStoryboardRepository, SqlAlchemyStoryboardRepository, sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )
    for table in (
        StoryboardGenerationRow.__table__,
        DirectorPointerRow.__table__,
        StoryboardCurrentRow.__table__,
        StoryboardMediaRow.__table__,
        StoryboardOutboxRow.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "production_state VARCHAR(16), production_mode VARCHAR(16))"
            )
        )
    sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def uow() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    return SqlAlchemyStoryboardRepository(uow), SqlAlchemyStoryboardRepository(uow), sessions


def insert_project(sessions: sessionmaker[Session], value: StoryboardGeneration, mode: str) -> None:
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state, production_mode) "
                "VALUES (:id, :owner, 'ACTIVE', :mode)"
            ),
            {"id": value.project_id.hex, "owner": value.owner_id.hex, "mode": mode},
        )
        session.add(_generation_row(value))


def test_sql_project_production_mode(tmp_path: Path) -> None:
    repo, _, sessions = sql_repositories(tmp_path / "mode.sqlite")
    value = minimal_generation(status=GenerationStatus.SUBMITTING, state_revision=1)
    insert_project(sessions, value, "AUTO")
    assert repo.project_production_mode(value.owner_id, value.project_id) == "AUTO"
    other = minimal_generation()
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state, production_mode) "
                "VALUES (:id, :owner, 'ACTIVE', 'MANUAL')"
            ),
            {"id": other.project_id.hex, "owner": other.owner_id.hex},
        )
    assert repo.project_production_mode(other.owner_id, other.project_id) == "MANUAL"
    assert repo.project_production_mode(uuid4(), uuid4()) is None


def test_sql_supersede_is_atomic_replay_safe_and_schedules_retry(tmp_path: Path) -> None:
    repo, _, sessions = sql_repositories(tmp_path / "supersede.sqlite")
    value = minimal_generation(status=GenerationStatus.SUBMITTING, state_revision=1)
    insert_project(sessions, value, "AUTO")
    successor = successor_of(value)
    next_at = NOW + timedelta(seconds=5)
    terminal = repo.supersede_with_auto_retry(
        value,
        failure_stage="submit",
        failure_code="ConnectError",
        successor=successor,
        next_attempt_at=next_at,
    )
    assert terminal.status is GenerationStatus.FAILED
    stored = repo.get_generation(value.owner_id, value.generation_id)
    assert stored is not None
    assert stored.status is GenerationStatus.FAILED
    assert stored.failure_stage == "submit"
    assert stored.failure_code == "ConnectError"
    stored_successor = repo.find_generation_by_key(value.owner_id, successor.idempotency_key)
    assert stored_successor is not None
    assert stored_successor.status is GenerationStatus.INTENT_PERSISTED
    assert stored_successor.version == 2
    # 血缘经既有 JSON 列持久化并读回（无需迁移）
    assert stored_successor.auto_attempt == 2
    assert stored_successor.supersedes_generation_id == value.generation_id
    with sessions() as session:
        event = session.scalar(
            select(StoryboardOutboxRow).where(
                StoryboardOutboxRow.aggregate_id == stored_successor.generation_id
            )
        )
        assert event is not None
        assert event.event_type == "STORYBOARD_GENERATION_REQUESTED"
        assert event.next_attempt_at.replace(tzinfo=UTC) == next_at
        current = session.get(StoryboardCurrentRow, (value.owner_id, value.group_id))
        assert current is not None and current.generation_id is None
    # 重放（陈旧 state_revision）：直接返回当前行，不产生第二条后继
    replayed = repo.supersede_with_auto_retry(
        value,
        failure_stage="submit",
        failure_code="ConnectError",
        successor=successor_of(value),
        next_attempt_at=next_at,
    )
    assert replayed.status is GenerationStatus.FAILED
    with sessions() as session:
        assert len(session.scalars(select(StoryboardGenerationRow)).all()) == 2
        assert len(session.scalars(select(StoryboardOutboxRow)).all()) == 1


def test_sql_failover_chain_fields_and_budget_are_persisted_atomically(tmp_path: Path) -> None:
    repo, _, sessions = sql_repositories(tmp_path / "failover-chain.sqlite")
    value = minimal_generation(
        status=GenerationStatus.SUBMITTING,
        state_revision=1,
        provider="grsai",
        model="gpt-image-2.5",
    )
    value = replace(
        value,
        failover_chain_id=value.generation_id,
        failover_policy_version="storyboard-failover-v1",
        failover_route_index=0,
        failover_model_attempt=1,
        failover_total_attempt=1,
        recovery_failure_count=2,
        unknown_origin_stage="submit",
        unknown_origin_code="TimeoutError",
    )
    insert_project(sessions, value, "AUTO")
    successor = successor_of(value)
    terminal = repo.supersede_with_auto_retry(
        value,
        failure_stage="provider",
        failure_code="error",
        successor=successor,
        next_attempt_at=NOW + timedelta(seconds=5),
    )
    assert terminal.status is GenerationStatus.FAILED
    stored_terminal = repo.get_generation(value.owner_id, value.generation_id)
    assert stored_terminal is not None
    assert stored_terminal.recovery_failure_count == 2
    assert stored_terminal.unknown_origin_stage == "submit"
    assert stored_terminal.unknown_origin_code == "TimeoutError"
    stored = repo.find_generation_by_key(value.owner_id, successor.idempotency_key)
    assert stored is not None
    assert stored.provider == "grsai"
    assert stored.failover_chain_id == value.generation_id
    assert stored.failover_policy_version == "storyboard-failover-v1"
    assert stored.failover_route_index == 0
    assert stored.failover_model_attempt == 2
    assert stored.failover_total_attempt == 2
    assert stored.auto_attempt == 2


def test_sql_concurrent_failover_supersede_creates_exactly_one_successor(tmp_path: Path) -> None:
    first_repo, second_repo, sessions = sql_repositories(tmp_path / "failover-race.sqlite")
    value = minimal_generation(
        status=GenerationStatus.SUBMITTING,
        state_revision=1,
        provider="grsai",
        model="gpt-image-2.5",
    )
    value = replace(
        value,
        failover_chain_id=value.generation_id,
        failover_policy_version="storyboard-failover-v1",
        failover_route_index=0,
        failover_model_attempt=1,
        failover_total_attempt=1,
    )
    insert_project(sessions, value, "AUTO")

    def supersede(repo: SqlAlchemyStoryboardRepository) -> StoryboardGeneration:
        return repo.supersede_with_auto_retry(
            value,
            failure_stage="provider",
            failure_code="error",
            successor=successor_of(value),
            next_attempt_at=NOW + timedelta(seconds=5),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(supersede, (first_repo, second_repo)))
    assert all(item.status is GenerationStatus.FAILED for item in results)
    with sessions() as session:
        rows = session.scalars(select(StoryboardGenerationRow)).all()
        assert len(rows) == 2
        assert len({row.failover_chain_id for row in rows}) == 1
        assert max(int(row.failover_total_attempt or 0) for row in rows) == 2


def test_sql_concurrent_supersede_creates_exactly_one_successor(tmp_path: Path) -> None:
    first_repo, second_repo, sessions = sql_repositories(tmp_path / "race.sqlite")
    value = minimal_generation(status=GenerationStatus.SUBMITTING, state_revision=1)
    insert_project(sessions, value, "AUTO")

    def supersede(repo: SqlAlchemyStoryboardRepository) -> StoryboardGeneration:
        return repo.supersede_with_auto_retry(
            value,
            failure_stage="submit",
            failure_code="ConnectError",
            successor=successor_of(value),
            next_attempt_at=NOW + timedelta(seconds=5),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(supersede, (first_repo, second_repo)))
    assert all(item.status is GenerationStatus.FAILED for item in results)
    with sessions() as session:
        rows = session.scalars(select(StoryboardGenerationRow)).all()
        assert len(rows) == 2
        successors = [row for row in rows if row.id != value.generation_id]
        assert len(successors) == 1
        assert len(session.scalars(select(StoryboardOutboxRow)).all()) == 1


def test_sql_save_unknown_requests_recovery_once_per_fresh_transition(tmp_path: Path) -> None:
    repo, _, sessions = sql_repositories(tmp_path / "recovery.sqlite")
    value = minimal_generation(status=GenerationStatus.RUNNING, state_revision=3)
    insert_project(sessions, value, "AUTO")
    unknown = replace(
        value,
        status=GenerationStatus.REMOTE_STATE_UNKNOWN,
        failure_stage="poll",
        failure_code="ReadTimeout",
    )
    saved = repo.save_generation_and_request_recovery(unknown, uuid4(), NOW)
    assert saved.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert saved.state_revision == 4
    with sessions() as session:
        events = session.scalars(select(StoryboardOutboxRow)).all()
        assert len(events) == 1
        assert events[0].event_type == "STORYBOARD_RECOVERY_REQUESTED"
        assert events[0].workflow_id == (
            f"storyboard-grid-recovery-{value.owner_id}-{value.generation_id}"
        )
    # 恢复轮询中再次落未知态（前行已是未知）：不重复登记
    saved_again = repo.save_generation_and_request_recovery(
        replace(saved, failure_stage="reconcile"), uuid4(), NOW
    )
    assert saved_again.state_revision == 5
    # 陈旧快照重放：不转换、不登记
    replayed = repo.save_generation_and_request_recovery(unknown, uuid4(), NOW)
    assert replayed.state_revision == 5
    with sessions() as session:
        assert len(session.scalars(select(StoryboardOutboxRow)).all()) == 1


def test_sql_unknown_transition_requires_active_project(tmp_path: Path) -> None:
    repo, _, sessions = sql_repositories(tmp_path / "purging.sqlite")
    value = minimal_generation(status=GenerationStatus.RUNNING, state_revision=3)
    insert_project(sessions, value, "AUTO")
    with sessions.begin() as session:
        session.execute(
            text("UPDATE projects SET production_state = 'PURGING' WHERE id = :id"),
            {"id": value.project_id.hex},
        )
    with pytest.raises(DomainError, match="purged"):
        repo.save_generation_and_request_recovery(
            replace(value, status=GenerationStatus.REMOTE_STATE_UNKNOWN), uuid4(), NOW
        )
