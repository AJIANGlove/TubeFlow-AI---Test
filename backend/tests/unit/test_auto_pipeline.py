"""WP3：AUTO 一键生产编排器单元测试（全部使用内存/Stub 端口，无真实 Provider 调用）。"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from tubeflow_test.adapters.persistence.narration import InMemoryNarrationRepository
from tubeflow_test.application.narration import ProjectNarrationService, SubmitSentenceNarration
from tubeflow_test.application.narration_ports import NarrationJob, VoiceReference
from tubeflow_test.application.storyboard_batch import (
    StoryboardBatchGenerateResult,
    StoryboardBatchGroupOverview,
    StoryboardBatchSeedResult,
)
from tubeflow_test.application.video_batch import (
    VideoBatchItemView,
    VideoBatchJobView,
)
from tubeflow_test.auto_pipeline.domain import (
    AutoPipelineRun,
    AutoPipelineState,
    new_auto_pipeline_run,
)
from tubeflow_test.auto_pipeline.ports import (
    AutoPipelineFacts,
    OversizedGroupFact,
    StoryboardGroupFact,
    VideoGroupFact,
)
from tubeflow_test.auto_pipeline.service import AutoPipelineService
from tubeflow_test.auto_pipeline.timing import phase_timings
from tubeflow_test.domain.common import AspectRatio, SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import (
    NarrationAttempt,
    NarrationAttemptState,
    narration_idempotency_key,
    segment_pasted_script,
)
from tubeflow_test.identity_project.adapters import MemoryRepository
from tubeflow_test.identity_project.domain import (
    Account,
    AccountRole,
    Project,
    ProjectSnapshot,
    SnapshotCharacter,
    VoiceMediaFact,
)
from tubeflow_test.identity_project.ports import VideoDefectGroupFact
from tubeflow_test.identity_project.service import IdentityProjectService

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
OWNER, PROJECT = uuid4(), uuid4()
VOICE = uuid4()
GROUP_A, GROUP_B = uuid4(), uuid4()


# ---------------------------------------------------------------------------
# 内存/Stub 端口
# ---------------------------------------------------------------------------


class FakeRunLock:
    def __init__(self, repo: FakeRunRepository, run: AutoPipelineRun | None) -> None:
        self._repo = repo
        self._run = run

    @property
    def run(self) -> AutoPipelineRun | None:
        return self._run

    def save(self, run: AutoPipelineRun) -> None:
        assert self._run is not None
        self._repo.saves += 1
        self._repo.runs[run.run_id] = run


class FakeRunRepository:
    def __init__(self) -> None:
        self.runs: dict[UUID, AutoPipelineRun] = {}
        self.project_rounds: dict[UUID, int] = {}
        self.saves = 0

    def _active(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None:
        return next(
            (
                run
                for run in self.runs.values()
                if run.owner_id == owner_id
                and run.project_id == project_id
                and run.state
                in {AutoPipelineState.RUNNING, AutoPipelineState.PAUSED}
            ),
            None,
        )

    def create_active(self, run: AutoPipelineRun) -> AutoPipelineRun:
        existing = self._active(run.owner_id, run.project_id)
        if existing is not None:
            return existing
        self.runs[run.run_id] = run
        self.project_rounds.setdefault(run.project_id, 1)
        return run

    def active_run(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None:
        return self._active(owner_id, project_id)

    def latest_run(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None:
        found = [
            run
            for run in self.runs.values()
            if run.owner_id == owner_id and run.project_id == project_id
        ]
        return max(found, key=lambda run: run.created_at) if found else None

    def list_running(self) -> tuple[AutoPipelineRun, ...]:
        return tuple(
            run for run in self.runs.values() if run.state is AutoPipelineState.RUNNING
        )

    @contextmanager
    def lock_active(self, owner_id: UUID, project_id: UUID):
        yield FakeRunLock(self, self._active(owner_id, project_id))

    def resume_paused(self, owner_id: UUID, project_id: UUID, *, now: datetime) -> AutoPipelineRun:
        run = self._active(owner_id, project_id)
        if run is None:
            raise DomainError("not_found", "Resource not found")
        if run.state is not AutoPipelineState.PAUSED:
            raise DomainError(
                "auto_pipeline_not_paused", "Only a paused auto pipeline can be resumed"
            )
        new_round = self.project_rounds.get(project_id, 1) + 1
        try:
            resumed = run.resumed(new_round, now)
        except ValueError as exc:
            raise DomainError(
                "auto_pipeline_not_paused", "Only a paused auto pipeline can be resumed"
            ) from exc
        self.project_rounds[project_id] = new_round
        self.runs[resumed.run_id] = resumed
        return resumed

    def resume_paused_for_reason(
        self, owner_id: UUID, project_id: UUID, *, reason_code: str, now: datetime
    ) -> AutoPipelineRun | None:
        run = self._active(owner_id, project_id)
        if (
            run is None
            or run.state is not AutoPipelineState.PAUSED
            or run.reason_code != reason_code
        ):
            return None
        new_round = self.project_rounds.get(project_id, 1) + 1
        resumed = run.resumed(new_round, now)
        self.project_rounds[project_id] = new_round
        self.runs[resumed.run_id] = resumed
        return resumed


class FakeFacts:
    def __init__(self, facts: AutoPipelineFacts) -> None:
        self.facts = facts

    def load(self, owner_id: UUID, project_id: UUID) -> AutoPipelineFacts:
        return self.facts


class StubNarration:
    def __init__(self) -> None:
        self.calls = 0
        self.remaining: dict[int, str] = {}

    def resume_auto_narration(self, owner_id: UUID, project_id: UUID) -> dict[int, str]:
        self.calls += 1
        return dict(self.remaining)


class StubDirector:
    def __init__(self) -> None:
        self.rounds: list[int] = []
        self.materialize_calls = 0

    def create_recovery_run(self, owner_id: UUID, project_id: UUID, *, round: int) -> None:
        self.rounds.append(round)

    def materialize_if_ready(self, owner_id: UUID, project_id: UUID) -> None:
        self.materialize_calls += 1

class RejectingStubDirector(StubDirector):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def create_recovery_run(self, owner_id: UUID, project_id: UUID, *, round: int) -> None:
        raise DomainError(self.code, "recovery run rejected")


class StubStoryboards:
    def __init__(self, items: tuple[StoryboardBatchGroupOverview, ...] = ()) -> None:
        self.items = items
        self.seed_calls = 0
        self.seed_auto_approve: list[bool] = []
        self.seed_results: tuple[StoryboardBatchSeedResult, ...] | None = None
        self.repair_calls = 0
        self.repair_fixes = False
        self.repair_results: tuple[StoryboardBatchSeedResult, ...] = ()
        self.generate_calls: list[tuple[tuple[UUID, ...], bool]] = []
        self.generate_results: tuple[StoryboardBatchGenerateResult, ...] = ()

    def overview(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardBatchGroupOverview, ...]:
        return self.items

    def seed_drafts(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        auto_approve_ambiguous: bool = False,
    ) -> tuple[StoryboardBatchSeedResult, ...]:
        self.seed_calls += 1
        self.seed_auto_approve.append(auto_approve_ambiguous)
        if self.seed_results is not None:
            return self.seed_results
        self.items = tuple(
            dataclasses.replace(item, has_draft=True, can_generate=True)
            if not item.has_draft and not item.has_applied
            else item
            for item in self.items
        )
        return tuple(
            StoryboardBatchSeedResult(item.group_id, item.group_sequence, "seeded")
            for item in self.items
        )

    def repair_ambiguous_drafts(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[StoryboardBatchSeedResult, ...]:
        self.repair_calls += 1
        if self.repair_fixes:
            self.items = tuple(
                dataclasses.replace(item, draft_review_state="APPROVED", can_generate=True)
                if item.has_draft and item.draft_review_state == "REQUIRED" and not item.has_applied
                else item
                for item in self.items
            )
        return self.repair_results

    def generate(
        self, owner_id: UUID, project_id: UUID, group_ids: tuple[UUID, ...], *, force: bool
    ) -> tuple[StoryboardBatchGenerateResult, ...]:
        self.generate_calls.append((group_ids, force))
        if self.generate_results:
            return self.generate_results
        return tuple(
            StoryboardBatchGenerateResult(group_id, 0, "queued", generation_id=uuid4())
            for group_id in group_ids
        )


class StubVideos:
    def __init__(self) -> None:
        self.jobs: dict[UUID, VideoBatchJobView] = {}
        self.start_calls = 0
        self.process_calls: list[UUID] = []

    def reopen_failed_items(self, job_id: UUID) -> int:
        job = self.jobs.get(job_id)
        if job is None:
            return 0
        reopened = 0
        items = []
        for item in job.items:
            if item.status in {
                "error",
                "not_found",
                "no_current_storyboard",
                "submission_budget_exhausted",
            }:
                reopened += 1
                items.append(VideoBatchItemView(item.group_id, item.sequence, "PENDING", None, None))
            else:
                items.append(item)
        self.jobs[job_id] = VideoBatchJobView(
            job.job_id, job.owner_id, job.project_id, job.force, job.total,
            job.created_at, job.completed_at, tuple(items),
        )
        return reopened

    def unfinished_jobs(self) -> tuple[UUID, ...]:
        return tuple(job_id for job_id, job in self.jobs.items() if job.completed_at is None)

    def get_job(self, owner_id: UUID, job_id: UUID) -> VideoBatchJobView | None:
        job = self.jobs.get(job_id)
        return job if job is not None and job.owner_id == owner_id else None

    def start(
        self, owner_id: UUID, project_id: UUID, group_ids: tuple[UUID, ...], *, force: bool
    ) -> VideoBatchJobView:
        self.start_calls += 1
        job_id = uuid4()
        self.jobs[job_id] = VideoBatchJobView(
            job_id=job_id,
            owner_id=owner_id,
            project_id=project_id,
            force=force,
            total=len(group_ids),
            created_at=NOW,
            completed_at=None,
            items=tuple(
                VideoBatchItemView(group_id, index + 1, "PENDING", None, None)
                for index, group_id in enumerate(group_ids)
            ),
        )
        return self.jobs[job_id]

    def process_all(self, job_id: UUID, *, respect_capacity_queue: bool = False) -> None:
        self.process_calls.append(job_id)
        job = self.jobs[job_id]
        self.jobs[job_id] = VideoBatchJobView(
            job.job_id,
            job.owner_id,
            job.project_id,
            job.force,
            job.total,
            job.created_at,
            NOW,
            tuple(
                VideoBatchItemView(item.group_id, item.sequence, "queued", uuid4(), None)
                for item in job.items
            ),
        )


class HalfDoneVideos(StubVideos):
    """process_all 记录但不完成 job（模拟开始与推进之间的重入窗口）。"""

    def process_all(self, job_id: UUID, *, respect_capacity_queue: bool = False) -> None:
        self.process_calls.append(job_id)


class SlowFailingVideos(StubVideos):
    """process_all 耗时 20 分钟并以一个 error 项结束（模拟 2026-10-18 生产）。"""

    def __init__(self, clock: list[datetime]) -> None:
        super().__init__()
        self._clock = clock

    def process_all(self, job_id: UUID, *, respect_capacity_queue: bool = False) -> None:
        self.process_calls.append(job_id)
        job = self.jobs[job_id]
        finished = self._clock[0] + timedelta(minutes=20)
        self._clock[0] = finished
        self.jobs[job_id] = VideoBatchJobView(
            job.job_id,
            job.owner_id,
            job.project_id,
            job.force,
            job.total,
            job.created_at,
            finished,
            tuple(
                VideoBatchItemView(
                    item.group_id,
                    item.sequence,
                    "error" if index == 0 else "queued",
                    None,
                    "video prompt invalid" if index == 0 else None,
                )
                for index, item in enumerate(job.items)
            ),
        )


class StubCompositions:
    def __init__(self) -> None:
        self.start_calls: list[tuple[UUID, UUID]] = []
        self.close_calls: list[tuple[UUID, UUID]] = []

    def start(self, owner_id: UUID, project_id: UUID) -> None:
        self.start_calls.append((owner_id, project_id))

    def close_superseded(self, owner_id: UUID, project_id: UUID) -> None:
        self.close_calls.append((owner_id, project_id))


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_facts(**overrides: object) -> AutoPipelineFacts:
    values: dict[str, object] = {
        "production_mode": "AUTO",
        "budget_round": 1,
        "narration_ready": True,
        "narration_blocking": None,
        "oversized_groups": (),
        "director_preplan_done": True,
        "director_latest_run_id": None,
        "director_latest_status": "SUCCEEDED",
        "director_latest_failure_code": None,
        "materialization_ready": True,
        "storyboards_complete": True,
        "storyboard_groups": (),
        "videos_complete": True,
        "video_groups": (),
        "composition_current_exists": True,
        "composition_in_flight": False,
        "composition_failed_code": None,
    }
    values.update(overrides)
    return AutoPipelineFacts(**values)  # type: ignore[arg-type]


def make_service(
    runs: FakeRunRepository,
    facts: FakeFacts,
    *,
    narration: StubNarration | None = None,
    director: StubDirector | None = None,
    storyboards: StubStoryboards | None = None,
    videos: StubVideos | None = None,
    compositions: StubCompositions | None = None,
) -> AutoPipelineService:
    return AutoPipelineService(
        runs,
        facts,
        narration=narration,
        director=director,
        storyboards=storyboards,
        videos=videos,
        compositions=compositions,
        clock=lambda: NOW,
    )


def start_run(runs: FakeRunRepository) -> AutoPipelineRun:
    return runs.create_active(new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW))


def overview_item(
    group_id: UUID,
    sequence: int,
    *,
    has_draft: bool = True,
    draft_review_state: str | None = "NOT_REQUIRED",
    has_applied: bool = False,
    storyboard_status: str | None = None,
    can_generate: bool = True,
) -> StoryboardBatchGroupOverview:
    return StoryboardBatchGroupOverview(
        group_id=group_id,
        group_sequence=sequence,
        has_draft=has_draft,
        draft_review_state=draft_review_state,
        has_applied=has_applied,
        storyboard_status=storyboard_status,
        storyboard_version=None,
        current_generation_id=None,
        can_generate=can_generate,
    )


def video_group(
    group_id: UUID,
    sequence: int,
    *,
    has_current: bool = False,
    latest_stage: str | None = None,
    latest_archive_stage: str | None = None,
    latest_error_code: str | None = None,
    budget_round: int = 1,
    submissions: int = 0,
) -> VideoGroupFact:
    return VideoGroupFact(
        group_id=group_id,
        group_sequence=sequence,
        has_current_video=has_current,
        latest_stage=latest_stage,
        latest_archive_stage=latest_archive_stage,
        latest_error_code=latest_error_code,
        latest_budget_round=budget_round,
        submissions=submissions,
    )


# ---------------------------------------------------------------------------
# 领域状态机
# ---------------------------------------------------------------------------


def test_domain_state_machine_legal_and_illegal_transitions() -> None:
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    assert run.state is AutoPipelineState.RUNNING and run.authorization_round == 1

    paused = run.paused("oversized_sentence", NOW, phase="video")
    assert paused.state is AutoPipelineState.PAUSED
    assert paused.reason_code == "oversized_sentence"
    assert paused.payload["phase"] == "video"

    resumed = paused.resumed(2, NOW)
    assert resumed.state is AutoPipelineState.RUNNING
    assert resumed.reason_code is None
    assert resumed.authorization_round == 2

    completed = resumed.completed(NOW)
    assert completed.state is AutoPipelineState.COMPLETED
    assert completed.payload["phase"] == "completed"

    with pytest.raises(ValueError):
        run.resumed(2, NOW)  # RUNNING 不能 resume
    with pytest.raises(ValueError):
        run.completed(NOW).paused("x", NOW, phase="video")  # COMPLETED 是终态
    with pytest.raises(ValueError):
        paused.resumed(1, NOW)  # 轮次必须递增
    with pytest.raises(ValueError):
        AutoPipelineRun(uuid4(), OWNER, PROJECT, AutoPipelineState.PAUSED, None, 1, {}, NOW, NOW)
    with pytest.raises(ValueError):
        AutoPipelineRun(
            uuid4(), OWNER, PROJECT, AutoPipelineState.RUNNING, "stray", 1, {}, NOW, NOW
        )
    with pytest.raises(ValueError):
        run.touched(NOW, phase="bogus")


# ---------------------------------------------------------------------------
# advance 全阶段推进与各类 PAUSED
# ---------------------------------------------------------------------------


def test_advance_narration_in_progress_is_noop() -> None:
    runs, facts = FakeRunRepository(), FakeFacts(make_facts(narration_ready=False))
    narration = StubNarration()
    service = make_service(runs, facts, narration=narration)
    start_run(runs)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.RUNNING
    assert narration.calls == 0
    assert run.payload["phase"] == "narration"


def test_advance_narration_blocking_pauses_with_stable_code() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(
            narration_ready=False,
            narration_blocking={3: "narration_submission_budget_exhausted"},
        )
    )
    narration = StubNarration()
    narration.remaining = {3: "narration_submission_budget_exhausted"}
    service = make_service(runs, facts, narration=narration)
    start_run(runs)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "narration_submission_budget_exhausted"
    assert run.payload["sequences"] == {"3": "narration_submission_budget_exhausted"}


def test_advance_oversized_sentence_pauses_with_sentence_facts() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(
            oversized_groups=(
                OversizedGroupFact(GROUP_A, 2, ((5, 16200),)),
            ),
        )
    )
    service = make_service(runs, facts, videos=StubVideos())
    start_run(runs)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "oversized_sentence"
    assert run.payload["groups"] == [
        {"group_sequence": 2, "sentences": [{"sequence": 5, "measured_duration_ms": 16200}]}
    ]


def test_advance_director_failed_pauses_then_resume_creates_recovery_run() -> None:
    runs = FakeRunRepository()
    run_id = uuid4()
    facts = FakeFacts(
        make_facts(
            director_preplan_done=False,
            director_latest_run_id=run_id,
            director_latest_status="FAILED",
            director_latest_failure_code="remote_state_unknown_exhausted",
        )
    )
    director = StubDirector()
    service = make_service(runs, facts, director=director)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "director_preplan_failed"
    assert run.payload["failure_code"] == "remote_state_unknown_exhausted"
    assert director.rounds == []

    # 重复 advance（未恢复）：保持 PAUSED，不创建新 run。
    service.advance(OWNER, PROJECT)
    assert director.rounds == []

    # 恢复 = 新一轮授权：经前缀复用机制创建新 run（确定性幂等键）。
    service.resume(OWNER, PROJECT)
    assert runs.project_rounds[PROJECT] == 2
    assert director.rounds == [2]
    active = runs.active_run(OWNER, PROJECT)
    assert active is not None and active.state is AutoPipelineState.RUNNING


def test_advance_director_failed_pauses_immediately_while_narration_in_flight() -> None:
    runs = FakeRunRepository()
    run_id = uuid4()
    facts = FakeFacts(
        make_facts(
            narration_ready=False,
            director_preplan_done=False,
            director_latest_run_id=run_id,
            director_latest_status="FAILED",
            director_latest_failure_code="director_output_chapter_count_invalid",
        )
    )
    director = StubDirector()
    service = make_service(runs, facts, director=director)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    # 配音在途但导演已终态失败：立即暂停在导演阶段，不等配音完成。
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "director_preplan_failed"
    assert run.payload["phase"] == "director_preplan"
    assert director.rounds == []

    # 恢复（新授权轮次）后立即创建导演恢复 run，与配音并行推进。
    service.resume(OWNER, PROJECT)
    assert director.rounds == [2]
    active = runs.active_run(OWNER, PROJECT)
    assert active is not None and active.state is AutoPipelineState.RUNNING


def test_advance_materialization_phase_kicks_idempotent_remap() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(materialization_ready=False))
    director = StubDirector()
    service = make_service(runs, facts, director=director)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    # 物化未就绪：编排器幂等补踢确定性重映射（钩子失败的恢复路径），不重复提交。
    assert run is not None and run.payload["phase"] == "director_materialization"
    assert director.materialize_calls == 1
    service.advance(OWNER, PROJECT)
    assert director.materialize_calls == 2


def test_advance_director_recovery_rejection_pauses_without_hot_loop() -> None:
    runs = FakeRunRepository()
    run_id = uuid4()
    facts = FakeFacts(
        make_facts(
            director_preplan_done=False,
            director_latest_run_id=run_id,
            director_latest_status="FAILED",
            director_latest_failure_code="director_output_chapter_count_invalid",
        )
    )
    director = RejectingStubDirector("director_snapshot_integrity_violation")
    service = make_service(runs, facts, director=director)
    start_run(runs)

    service.advance(OWNER, PROJECT)  # 首次失败暂停并记录 handled 标记
    service.resume(OWNER, PROJECT)  # 新授权轮次的 kick 内重试被拒
    run = runs.active_run(OWNER, PROJECT)
    # 恢复 run 被拒绝：稳定暂停、如实原因码，不在巡检中热循环重试。
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "director_preplan_failed"
    assert run.payload["failure_code"] == "director_snapshot_integrity_violation"
    assert director.rounds == []
    service.advance(OWNER, PROJECT)
    assert director.rounds == []


def test_advance_materialization_pending_is_noop() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(materialization_ready=False))
    storyboards = StubStoryboards((overview_item(GROUP_A, 1),))
    service = make_service(runs, facts, storyboards=storyboards)
    start_run(runs)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.RUNNING
    assert storyboards.seed_calls == 0
    assert run.payload["phase"] == "director_materialization"


def test_advance_seeds_drafts_once_then_generates_missing_storyboards() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(storyboards_complete=False))
    storyboards = StubStoryboards(
        (overview_item(GROUP_A, 1, has_draft=False, can_generate=False),)
    )
    service = make_service(runs, facts, storyboards=storyboards)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert storyboards.seed_calls == 1
    assert storyboards.seed_auto_approve == [True]  # AUTO 播种附加系统审批（§20）
    assert storyboards.generate_calls == []  # 恰好一个动作
    assert run is not None and run.payload["phase"] == "storyboard_drafts"

    # 播种完成（外部钩子推进后）：草稿就绪 → 生成全部缺失组。
    storyboards.items = (overview_item(GROUP_A, 1),)
    run = service.advance(OWNER, PROJECT)
    assert storyboards.generate_calls == [((GROUP_A,), False)]
    assert run is not None and run.state is AutoPipelineState.RUNNING


def test_advance_seed_needs_review_pauses_director_validation() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(storyboards_complete=False))
    storyboards = StubStoryboards(
        (
            overview_item(
                GROUP_A, 1, has_draft=True, draft_review_state="REQUIRED", can_generate=False
            ),
        )
    )
    service = make_service(runs, facts, storyboards=storyboards)
    start_run(runs)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "director_validation_failed"
    assert run.payload["group_sequences"] == [1]
    assert storyboards.seed_calls == 0
    # 先尝试系统审批修复；修复无果（结构性问题）才暂停。
    assert storyboards.repair_calls == 1
    assert storyboards.generate_calls == []
    assert "failure_messages" not in run.payload  # 修复无错误消息时详情不含该键


def test_advance_seed_failure_messages_surface_in_pause_details() -> None:
    """§22：播种失败的逐组原因随暂停详情落库（不再只剩组号）。"""
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(storyboards_complete=False))
    storyboards = StubStoryboards(
        (overview_item(GROUP_A, 1, has_draft=False, can_generate=False),)
    )
    storyboards.seed_results = (
        StoryboardBatchSeedResult(
            GROUP_A, 1, "error", message="Real narration gate has not passed"
        ),
    )
    service = make_service(runs, facts, storyboards=storyboards)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)

    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "director_validation_failed"
    assert run.payload["group_sequences"] == [1]
    assert run.payload["failure_messages"] == {"1": "Real narration gate has not passed"}


def test_advance_repairs_required_drafts_then_continues() -> None:
    """§20：既有 REQUIRED 草稿经系统审批修复后不再暂停，下一跳继续生成。"""
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(storyboards_complete=False))
    storyboards = StubStoryboards(
        (
            overview_item(
                GROUP_A, 1, has_draft=True, draft_review_state="REQUIRED", can_generate=False
            ),
        )
    )
    storyboards.repair_fixes = True
    storyboards.repair_results = (
        StoryboardBatchSeedResult(GROUP_A, 1, "repaired", review_state="APPROVED"),
    )
    service = make_service(runs, facts, storyboards=storyboards)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert storyboards.repair_calls == 1 and storyboards.seed_calls == 0
    assert run is not None and run.state is AutoPipelineState.RUNNING
    assert run.payload["phase"] == "storyboard_drafts"
    assert storyboards.generate_calls == []  # 本阶段已写入草稿变更：恰好一个动作

    run = service.advance(OWNER, PROJECT)  # 下一跳：草稿已就绪 → 生成缺失组
    assert run is not None and run.state is AutoPipelineState.RUNNING
    assert storyboards.generate_calls == [((GROUP_A,), False)]


@pytest.mark.parametrize(
    "failure_code", ["submission_budget_exhausted", "remote_state_unknown_exhausted"]
)
def test_advance_storyboard_terminal_failure_pauses_only_in_current_round(
    failure_code: str,
) -> None:
    runs = FakeRunRepository()
    failed = StoryboardGroupFact(GROUP_A, 1, "FAILED", failure_code, 1)
    facts = FakeFacts(
        make_facts(storyboards_complete=False, storyboard_groups=(failed,))
    )
    storyboards = StubStoryboards((overview_item(GROUP_A, 1),))
    service = make_service(runs, facts, storyboards=storyboards)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == f"storyboard_failed:{failure_code}"
    assert storyboards.generate_calls == []

    # 恢复后旧轮失败不再占用预算：按缺失组重新生成（新链 auto_attempt 重新计 3 次）。
    facts.facts = make_facts(
        storyboards_complete=False, storyboard_groups=(failed,), budget_round=2
    )
    service.resume(OWNER, PROJECT)
    assert storyboards.generate_calls == [((GROUP_A,), False)]
    assert runs.active_run(OWNER, PROJECT).state is AutoPipelineState.RUNNING  # type: ignore[union-attr]


def test_pause_for_storyboard_recovery_is_fail_closed() -> None:
    runs = FakeRunRepository()
    service = make_service(runs, FakeFacts(make_facts()))
    start_run(runs)
    run = service.pause_for_storyboard_recovery(OWNER, PROJECT, 1)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "storyboard_recovery_unavailable"
    assert run.payload["phase"] == "storyboard"
    assert run.payload["failure_code"] == "storyboard_recovery_unavailable"
    assert run.payload["group_sequences"] == [1]


def test_advance_storyboard_in_flight_skips_generate() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(
            storyboards_complete=False,
            storyboard_groups=(StoryboardGroupFact(GROUP_A, 1, "RUNNING", None, 1),),
        )
    )
    storyboards = StubStoryboards(
        (overview_item(GROUP_A, 1, storyboard_status="RUNNING"),)
    )
    service = make_service(runs, facts, storyboards=storyboards)
    start_run(runs)
    service.advance(OWNER, PROJECT)
    assert storyboards.generate_calls == []


def test_advance_video_starts_job_and_reentry_reuses_bookmark() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(
            videos_complete=False,
            video_groups=(video_group(GROUP_A, 1), video_group(GROUP_B, 2)),
        )
    )
    videos = HalfDoneVideos()
    service = make_service(runs, facts, videos=videos)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert videos.start_calls == 1
    job_id = next(iter(videos.jobs))
    assert videos.process_calls == [job_id]
    assert run is not None and run.payload["video_batch_job_id"] == str(job_id)

    # 重入：job 未完成 → 续跑同一 job，不新建第二个。
    run2 = service.advance(OWNER, PROJECT)
    assert videos.start_calls == 1
    assert videos.process_calls == [job_id, job_id]
    assert run2 is not None and run2.payload["video_batch_job_id"] == str(job_id)


def test_advance_video_terminal_failure_pauses_and_resume_gets_new_budget() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(
            videos_complete=False,
            video_groups=(
                video_group(
                    GROUP_A,
                    1,
                    latest_stage="FAILED",
                    latest_error_code="submission_budget_exhausted",
                    budget_round=1,
                ),
            ),
        )
    )
    videos = StubVideos()
    service = make_service(runs, facts, videos=videos)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "video_failed:submission_budget_exhausted"
    assert videos.start_calls == 0

    # 恢复：历史轮次失败保留可审计，新一轮重新提交。
    facts.facts = make_facts(
        videos_complete=False,
        budget_round=2,
        video_groups=(
            video_group(
                GROUP_A,
                1,
                latest_stage="FAILED",
                latest_error_code="submission_budget_exhausted",
                budget_round=1,
            ),
        ),
    )
    service.resume(OWNER, PROJECT)
    assert videos.start_calls == 1
    assert runs.project_rounds[PROJECT] == 2


def test_advance_video_intent_rejection_pauses() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )

    class FailingVideos(StubVideos):
        def process_all(self, job_id: UUID, *, respect_capacity_queue: bool = False) -> None:
            super().process_all(job_id)
            job = self.jobs[job_id]
            self.jobs[job_id] = VideoBatchJobView(
                job.job_id,
                job.owner_id,
                job.project_id,
                job.force,
                job.total,
                job.created_at,
                job.completed_at,
                (VideoBatchItemView(GROUP_A, 1, "error", None, "gate:video_input"),),
            )

    videos = FailingVideos()
    service = make_service(runs, facts, videos=videos)
    start_run(runs)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "video_failed:intent_rejected"


def test_advance_composition_start_then_completed() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(composition_current_exists=False))
    compositions = StubCompositions()
    service = make_service(runs, facts, compositions=compositions)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert compositions.start_calls == [(OWNER, PROJECT)]
    assert run is not None and run.state is AutoPipelineState.RUNNING

    # 幂等：在途时不重复入队。
    facts.facts = make_facts(composition_current_exists=False, composition_in_flight=True)
    service.advance(OWNER, PROJECT)
    assert len(compositions.start_calls) == 1

    # 当前版本就绪 → COMPLETED 终态；完成即收口非终态合成 outbox 行（§17.3）。
    facts.facts = make_facts(composition_current_exists=True)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.COMPLETED
    assert compositions.close_calls == [(OWNER, PROJECT)]


def test_advance_composition_terminal_failure_pauses_in_current_round() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(composition_current_exists=False, composition_failed_code="media_command_failed")
    )
    compositions = StubCompositions()
    service = make_service(runs, facts, compositions=compositions)
    start_run(runs)
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "composition_failed"
    assert run.payload["error_code"] == "media_command_failed"
    assert compositions.start_calls == []

    # 恢复后新一轮：失败码按轮清空，重新幂等入队。
    facts.facts = make_facts(composition_current_exists=False, budget_round=2)
    service.resume(OWNER, PROJECT)
    assert compositions.start_calls == [(OWNER, PROJECT)]


def test_advance_full_happy_path_one_action_per_call() -> None:
    """顺利路径自动串联：每次 advance 恰好一个动作，直至 COMPLETED。"""
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(narration_ready=False))
    narration = StubNarration()
    storyboards = StubStoryboards((overview_item(GROUP_A, 1, has_draft=False),))
    videos = StubVideos()
    compositions = StubCompositions()
    service = make_service(
        runs,
        facts,
        narration=narration,
        storyboards=storyboards,
        videos=videos,
        compositions=compositions,
    )
    start_run(runs)

    service.advance(OWNER, PROJECT)  # 配音在途：不动
    assert narration.calls == storyboards.seed_calls == 0

    facts.facts = make_facts(director_preplan_done=False, director_latest_status="RUNNING")
    service.advance(OWNER, PROJECT)  # 前置导演在途：不动
    assert director_not_called(storyboards)

    facts.facts = make_facts(materialization_ready=False)
    service.advance(OWNER, PROJECT)  # 物化未就绪：不动
    assert storyboards.seed_calls == 0

    facts.facts = make_facts(storyboards_complete=False)
    service.advance(OWNER, PROJECT)  # 动作 1：播种
    assert storyboards.seed_calls == 1 and storyboards.generate_calls == []

    storyboards.items = (overview_item(GROUP_A, 1),)
    service.advance(OWNER, PROJECT)  # 动作 2：批量生成故事板
    assert storyboards.generate_calls == [((GROUP_A,), False)]

    facts.facts = make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    service.advance(OWNER, PROJECT)  # 动作 3：启动并推进视频批量 job
    assert videos.start_calls == 1

    facts.facts = make_facts(composition_current_exists=False)
    service.advance(OWNER, PROJECT)  # 动作 4：入队合成
    assert compositions.start_calls == [(OWNER, PROJECT)]

    facts.facts = make_facts()
    run = service.advance(OWNER, PROJECT)  # 完成
    assert run is not None and run.state is AutoPipelineState.COMPLETED


def director_not_called(storyboards: StubStoryboards) -> bool:
    return storyboards.seed_calls == 0 and storyboards.generate_calls == []


def test_advance_defensive_noop_for_manual_and_missing_run() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(production_mode="MANUAL"))
    storyboards = StubStoryboards()
    service = make_service(runs, facts, storyboards=storyboards)
    assert service.advance(OWNER, PROJECT) is None  # 无运行：不动

    start_run(runs)  # 防御：运行存在但项目不是 AUTO
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.RUNNING
    assert storyboards.seed_calls == 0


def test_advance_paused_run_is_not_advanced() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(composition_current_exists=False))
    compositions = StubCompositions()
    service = make_service(runs, facts, compositions=compositions)
    run = start_run(runs)
    runs.runs[run.run_id] = run.paused("composition_failed", NOW, phase="composition")
    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED
    assert compositions.start_calls == []


# ---------------------------------------------------------------------------
# resume 语义
# ---------------------------------------------------------------------------


def test_resume_requires_paused_and_bumps_round() -> None:
    runs = FakeRunRepository()
    # 合成在途：恢复后推进一步仍为 RUNNING（resume 现返回推进后的真实状态，§48.1）。
    facts = FakeFacts(make_facts(composition_current_exists=False, composition_in_flight=True))
    service = make_service(runs, facts)
    run = start_run(runs)

    with pytest.raises(DomainError) as captured:
        service.resume(OWNER, PROJECT)
    assert captured.value.code == "auto_pipeline_not_paused"
    with pytest.raises(DomainError) as captured:
        service.resume(OWNER, uuid4())
    assert captured.value.code == "not_found"

    runs.runs[run.run_id] = run.paused("composition_failed", NOW, phase="composition")
    resumed = service.resume(OWNER, PROJECT)
    assert resumed.state is AutoPipelineState.RUNNING
    assert resumed.reason_code is None
    assert resumed.authorization_round == 2
    assert runs.project_rounds[PROJECT] == 2
    stored = runs.runs[run.run_id]
    assert stored.state is AutoPipelineState.RUNNING
    assert stored.reason_code is None
    assert stored.authorization_round == 2


def test_resume_reports_the_pause_when_the_problem_is_not_fixed() -> None:
    """§48.1：问题未修复时恢复在同一请求内再次暂停，返回 PAUSED 与真实原因码。"""
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(composition_current_exists=False, composition_failed_code="media_command_failed")
    )
    service = make_service(runs, facts)
    run = start_run(runs)
    runs.runs[run.run_id] = run.paused("composition_failed", NOW, phase="composition")

    resumed = service.resume(OWNER, PROJECT)

    assert resumed.state is AutoPipelineState.PAUSED
    assert resumed.reason_code == "composition_failed"
    assert resumed.authorization_round == 2
    assert runs.runs[run.run_id].state is AutoPipelineState.PAUSED
    assert runs.project_rounds[PROJECT] == 2


def test_resume_returns_the_resumed_run_when_advance_fails() -> None:
    class FailingFacts:
        def load(self, owner_id: UUID, project_id: UUID) -> AutoPipelineFacts:
            raise RuntimeError("facts unavailable")

    runs = FakeRunRepository()
    service = make_service(runs, FailingFacts())  # type: ignore[arg-type]
    run = start_run(runs)
    runs.runs[run.run_id] = run.paused("composition_failed", NOW, phase="composition")

    resumed = service.resume(OWNER, PROJECT)

    assert resumed.state is AutoPipelineState.RUNNING
    assert resumed.authorization_round == 2


def test_pause_details_do_not_carry_over_to_the_next_pause() -> None:
    """§48.2：新暂停前清除旧暂停详情；书签与 director_failed_run_id 保留。"""
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    first = run.paused(
        "director_preplan_failed",
        NOW,
        phase="director_preplan",
        details={
            "director_failed_run_id": "run-1",
            "failure_code": "director_output_adjacent_diversity_insufficient",
            "video_batch_job_id": "job-1",
        },
    )
    second = first.resumed(2, NOW).paused(
        "director_validation_failed",
        NOW,
        phase="storyboard_drafts",
        details={"group_sequences": [8], "failure_messages": {"8": "repeats"}},
    )

    assert "failure_code" not in second.payload
    assert second.payload["group_sequences"] == [8]
    assert second.payload["failure_messages"] == {"8": "repeats"}
    assert second.payload["director_failed_run_id"] == "run-1"
    assert second.payload["video_batch_job_id"] == "job-1"


def test_resume_if_paused_for_reason_guard() -> None:
    """§20 保存即续跑：仅原因码匹配的暂停才自动恢复；其余情形不动预算轮次。"""
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts(composition_current_exists=False))
    compositions = StubCompositions()
    service = make_service(runs, facts, compositions=compositions)
    run = start_run(runs)

    # 未暂停：不动
    assert (
        service.resume_if_paused_for(OWNER, PROJECT, reason_code="composition_failed") is False
    )
    assert runs.project_rounds[PROJECT] == 1

    # 原因码不匹配：不动
    runs.runs[run.run_id] = run.paused("composition_failed", NOW, phase="composition")
    assert (
        service.resume_if_paused_for(OWNER, PROJECT, reason_code="director_validation_failed")
        is False
    )
    paused = runs.active_run(OWNER, PROJECT)
    assert paused is not None and paused.state is AutoPipelineState.PAUSED
    assert runs.project_rounds[PROJECT] == 1

    # 匹配：授权轮次 +1、置 RUNNING 并在 kick 内推进
    assert service.resume_if_paused_for(OWNER, PROJECT, reason_code="composition_failed") is True
    resumed = runs.active_run(OWNER, PROJECT)
    assert resumed is not None and resumed.state is AutoPipelineState.RUNNING
    assert resumed.authorization_round == 2
    assert compositions.start_calls == [(OWNER, PROJECT)]


def test_resume_narration_round_resets_budget_and_clears_blocking() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(
            narration_ready=False,
            narration_blocking={2: "narration_submission_budget_exhausted"},
        )
    )
    narration = StubNarration()
    narration.remaining = {2: "narration_submission_budget_exhausted"}
    service = make_service(runs, facts, narration=narration)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert run is not None and run.state is AutoPipelineState.PAUSED

    # 恢复：第 2 轮预算内重投成功入队，阻断清除，运行回到 RUNNING。
    narration.remaining = {}
    resumed = service.resume(OWNER, PROJECT)
    assert resumed.state is AutoPipelineState.RUNNING
    assert narration.calls >= 2


# ---------------------------------------------------------------------------
# 踢点与巡检
# ---------------------------------------------------------------------------


def test_kick_swallows_failures_and_sweep_covers_others() -> None:
    runs = FakeRunRepository()

    class ExplodingFacts:
        def load(self, owner_id: UUID, project_id: UUID) -> AutoPipelineFacts:
            raise RuntimeError("database is down")

    service = make_service(runs, ExplodingFacts())  # type: ignore[arg-type]
    start_run(runs)
    service.kick(OWNER, PROJECT)  # 不抛出：踢失败只记日志

    other_project = uuid4()
    runs.create_active(new_auto_pipeline_run(uuid4(), OWNER, other_project, NOW))
    runs.create_active(new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW))
    good_facts = FakeFacts(make_facts())
    good = make_service(runs, good_facts)
    swept = good.sweep_once()
    assert swept == 2
    assert runs.latest_run(OWNER, other_project).state is AutoPipelineState.COMPLETED  # type: ignore[union-attr]




# ---------------------------------------------------------------------------
# 配音：resume 按轮重置预算（真实服务 + 内存仓储）
# ---------------------------------------------------------------------------


class _NarrationProjects:
    def __init__(self, text: str, voice: UUID) -> None:
        self.value = (text, SupportedLocale.EN_US, voice)

    def get_draft_script(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        return self.value

    def get_frozen_narration(self, owner_id: UUID, project_id: UUID) -> tuple[object, ...]:
        return self.value


class _NarrationVoices:
    def get_reference(
        self, owner_id: UUID, project_id: UUID, locale: SupportedLocale
    ) -> VoiceReference:
        del owner_id, project_id, locale
        payload = b"reference-zip"
        return VoiceReference(VOICE, payload, hashlib.sha256(payload).hexdigest())


class _NarrationGuard:
    def assert_writable(self, owner_id: UUID, project_id: UUID) -> None:
        return None


class _NarrationStarter:
    async def start(self, **_kwargs: object) -> str:
        return "job"


def test_narration_resume_round_resets_budget_and_keeps_history() -> None:
    """恢复后该句获得新的 3 次预算且历史尝试保留（§15.9）。"""
    voice = uuid4()
    repo = InMemoryNarrationRepository()
    repo.production_mode = "AUTO"
    script = segment_pasted_script("Hello world. Another one.", SupportedLocale.EN_US)
    repo.save_script(OWNER, PROJECT, script)
    sentence = next(item for item in script.sentences if item.sequence == 1)
    base = narration_idempotency_key(str(PROJECT), sentence, str(voice))
    for ordinal in (1, 2, 3):
        key = base if ordinal == 1 else f"{base}:attempt:{ordinal}"
        repo.save_attempt(
            NarrationAttempt(
                PROJECT,
                OWNER,
                1,
                key,
                "a" * 64,
                NarrationAttemptState.SAFE_TO_RETRY,
                f"call-{ordinal}",
            )
        )
    repo.save_job(NarrationJob("job-1", OWNER, PROJECT, (1, 2), "PENDING"))
    service = ProjectNarrationService(
        _NarrationProjects("Hello world. Another one.", voice),  # type: ignore[arg-type]
        _NarrationVoices(),  # type: ignore[arg-type]
        repo,
        SubmitSentenceNarration(None, repo, None, None),  # type: ignore[arg-type]
        _NarrationStarter(),  # type: ignore[arg-type]
        _NarrationGuard(),  # type: ignore[arg-type]
    )

    # 第 1 轮：句 1 已 3 次尝试 → 预算耗尽阻断；句 2 排期自动重投。
    service.finish_job(OWNER, PROJECT, "job-1", (1,))
    persisted = repo.blocking_reason(OWNER, PROJECT)
    assert persisted is not None
    assert persisted["sequences"] == {"1": "narration_submission_budget_exhausted"}  # type: ignore[index]

    # 恢复到第 2 轮：句 1 重新获得预算并入队，历史 3 次尝试保留。
    repo.auto_budget_round = 2
    blocked = service.resume_auto_narration(OWNER, PROJECT)
    assert blocked == {}
    assert repo.blocking_reason(OWNER, PROJECT) is None
    # 句 1（第 2 轮新预算）与句 2（第 1 轮）各有排队任务；排队恢复任务在当前任务终结后执行。
    current = repo.get_job(OWNER, PROJECT)
    queued = repo._pending_recovery_jobs.get((OWNER, PROJECT), [])  # noqa: SLF001
    scheduled = ([current] if current is not None else []) + list(queued)
    assert len(scheduled) == 2
    assert len({job.job_id for job in scheduled}) == 2
    assert all(job.state == "PENDING" for job in scheduled)
    assert len(repo.list_attempts(OWNER, PROJECT)) == 3
    assert all(item.budget_round == 1 for item in repo.list_attempts(OWNER, PROJECT))

    # 重入幂等：同一确定性 job_id 不重复入队。
    assert service.resume_auto_narration(OWNER, PROJECT) == {}
    queued_after = repo._pending_recovery_jobs.get((OWNER, PROJECT), [])  # noqa: SLF001
    assert len(queued_after) == 1


# ---------------------------------------------------------------------------
# 运行记录仓储（sqlite 级）
# ---------------------------------------------------------------------------


def test_sql_run_repository_idempotent_create_lock_and_resume(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from tubeflow_test.adapters.persistence.auto_pipeline import SqlAutoPipelineRunRepository
    from tubeflow_test.adapters.persistence.identity_project.models import AutoPipelineRunRow

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'auto_pipeline.sqlite3'}")
    AutoPipelineRunRow.__table__.create(engine)
    owner_id, project_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "auto_budget_round INTEGER DEFAULT 1)"
            )
        )
        connection.execute(
            text("INSERT INTO projects (id, owner_id, auto_budget_round) VALUES (:id, :owner, 1)"),
            {"id": project_id.hex, "owner": owner_id.hex},
        )
    repo = SqlAutoPipelineRunRepository(sessionmaker(engine, expire_on_commit=False))

    run = new_auto_pipeline_run(uuid4(), owner_id, project_id, NOW)
    created = repo.create_active(run)
    again = repo.create_active(new_auto_pipeline_run(uuid4(), owner_id, project_id, NOW))
    assert again.run_id == created.run_id  # 重入幂等：同项目一条活跃运行
    active = repo.active_run(owner_id, project_id)
    assert active is not None and active.run_id == created.run_id
    assert active.payload["run_started_ms"] == _ms(NOW)
    assert active.payload["phase_started_ms"] == _ms(NOW)
    assert active.payload["timing_from_phase"] == "narration"
    assert [run.run_id for run in repo.list_running()] == [created.run_id]

    with pytest.raises(DomainError) as captured:
        repo.resume_paused(owner_id, project_id, now=NOW)
    assert captured.value.code == "auto_pipeline_not_paused"

    with repo.lock_active(owner_id, project_id) as lock:
        locked = lock.run
        assert locked is not None
        lock.save(locked.paused("composition_failed", NOW, phase="composition"))
    paused = repo.active_run(owner_id, project_id)
    assert paused is not None and paused.state is AutoPipelineState.PAUSED

    resumed = repo.resume_paused(owner_id, project_id, now=NOW)
    assert resumed.state is AutoPipelineState.RUNNING
    assert resumed.reason_code is None
    assert resumed.authorization_round == 2
    assert resumed.payload["phase"] == "composition"
    # 计时字段经 SQLite JSON 往返后仍是 int/dict；暂停与恢复同一时刻，扣除 0。
    assert resumed.payload["phase_elapsed_ms"] == {"narration": 0}
    assert resumed.payload["run_paused_ms"] == 0
    with engine.begin() as connection:
        round_value = connection.execute(
            text("SELECT auto_budget_round FROM projects WHERE id=:id"),
            {"id": project_id.hex},
        ).scalar_one()
    assert round_value == 2

    with pytest.raises(DomainError) as captured:
        repo.resume_paused(owner_id, project_id, now=NOW)
    assert captured.value.code == "auto_pipeline_not_paused"
    engine.dispose()


def test_auto_pipeline_migrations_chain_and_columns() -> None:
    import importlib.util
    from pathlib import Path as _Path

    def load(name: str) -> object:
        path = _Path(__file__).parents[2] / "migrations" / "versions" / name
        spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    m26 = load("0026_auto_pipeline_runs.py")
    assert m26.revision == "0026" and m26.down_revision == "0025"  # type: ignore[attr-defined]
    added: list[tuple[str, object]] = []
    created_tables: list[str] = []
    created_indexes: list[str] = []
    m26.op.add_column = lambda table, column, *a, **k: added.append((table, column))  # type: ignore[attr-defined]
    m26.op.create_table = lambda name, *a, **k: created_tables.append(name)  # type: ignore[attr-defined]
    m26.op.create_index = lambda name, *a, **k: created_indexes.append(name)  # type: ignore[attr-defined]
    m26.upgrade()  # type: ignore[attr-defined]
    assert [(table, column.name) for table, column in added] == [  # type: ignore[attr-defined]
        ("projects", "auto_budget_round")
    ]
    assert created_tables == ["auto_pipeline_runs"]
    assert "uq_auto_pipeline_runs_active_project" in created_indexes

    m27 = load("0027_budget_round_columns.py")
    assert m27.revision == "0027" and m27.down_revision == "0026"  # type: ignore[attr-defined]
    added27: list[tuple[str, object]] = []
    m27.op.add_column = lambda table, column, *a, **k: added27.append((table, column))  # type: ignore[attr-defined]
    m27.upgrade()  # type: ignore[attr-defined]
    assert [(table, column.name) for table, column in added27] == [  # type: ignore[attr-defined]
        ("video_generation_attempts", "budget_round"),
        ("composition_outbox", "budget_round"),
    ]


# ---------------------------------------------------------------------------
# workflow_summary / 项目列表 flow 集成
# ---------------------------------------------------------------------------


class _PipelineIdentityRepo(MemoryRepository):
    def __init__(
        self,
        counts: dict[str, int],
        attempt_states: dict[str, str | None] | None = None,
    ) -> None:
        super().__init__()
        self.counts = counts
        self.attempt_states = attempt_states or {
            "project_director": None,
            "director": None,
            "storyboard": None,
            "video": None,
            "composition": None,
        }
        self.pipeline_run: tuple[str, str | None, dict[str, object]] | None = None
        self.pipeline_completed_at: datetime | None = None
        self.composition_dispatched_at: datetime | None = None
        self.defect_groups: tuple[VideoDefectGroupFact, ...] = ()

    def workflow_counts(self, owner_id: UUID, project_id: UUID) -> dict[str, int]:
        return dict(self.counts)

    def workflow_attempt_states(
        self, owner_id: UUID, project_id: UUID
    ) -> dict[str, str | None]:
        return dict(self.attempt_states)

    def auto_pipeline_run(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, str | None, dict[str, object]] | None:
        return self.pipeline_run

    def auto_pipeline_completed_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        return self.pipeline_completed_at

    def latest_composition_dispatched_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        return self.composition_dispatched_at

    def video_defect_groups(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[VideoDefectGroupFact, ...]:
        return self.defect_groups


class _Passwords:
    def hash(self, password: str) -> str:
        return password

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == password


def _summary_counts(**overrides: int) -> dict[str, int]:
    values = {
        "sentences": 1,
        "ready_sentences": 1,
        "grouped_sentences": 1,
        "groups": 1,
        "blocked": 0,
        "director_preplan_total_units": 1,
        "director_preplan_completed_units": 1,
        "director_preplan_revisions": 1,
        "director_materializations": 1,
        "directed": 1,
        "director_ready": 1,
        "storyboards": 1,
        "videos": 0,
        "compositions": 0,
    }
    values.update(overrides)
    return values


def _summary_service(
    mode: str,
    counts: dict[str, int],
    run: tuple[str, str | None, dict[str, object]] | None = None,
    attempt_states: dict[str, str | None] | None = None,
    completed_at: datetime | None = None,
    dispatched_at: datetime | None = None,
    defect_groups: tuple[VideoDefectGroupFact, ...] = (),
) -> tuple[IdentityProjectService, Account, Project]:
    owner = Account(uuid4(), "owner", "hash", AccountRole.USER)
    image_id = uuid4()
    project = Project(
        id=uuid4(),
        owner_id=owner.id,
        channel_id=uuid4(),
        script="A real saved script.",
        character_image_ids=(image_id,),
        visual_style="cinematic",
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
        voice_version_id=uuid4(),
        production_mode=mode,
    )
    media = VoiceMediaFact(uuid4(), "a" * 64, "audio/wav", 10)
    project = dataclasses.replace(
        project,
        snapshot=ProjectSnapshot(
            script=project.script,
            characters=(SnapshotCharacter(0, image_id, uuid4(), "b" * 64, "image/png", 20),),
            visual_style=project.visual_style,
            locale=project.locale,
            aspect_ratio=project.aspect_ratio,
            channel_url="https://example.test/channel",
            expected_duration_seconds=None,
            confirmed_at=NOW,
            voice_version_id=project.voice_version_id,
            voice_metadata_sha256="c" * 64,
            voice_catalog_source_sha256="d" * 64,
            voice_reference_zip=media,
            voice_preview=media,
            voice_avatar=media,
        ),
    )
    repo = _PipelineIdentityRepo(counts, attempt_states)
    repo.pipeline_run = run
    repo.pipeline_completed_at = completed_at
    repo.composition_dispatched_at = dispatched_at
    repo.defect_groups = defect_groups
    repo.add_project(project)
    return IdentityProjectService(repo, _Passwords()), owner, project  # type: ignore[arg-type]


def test_summary_auto_running_strips_manual_stage_actions() -> None:
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(),
        ("RUNNING", None, {"phase": "video"}),
    )
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "VIDEO"
    assert summary["blocking_reason"] == "videos_incomplete"
    assert summary["auto_pipeline"] == {
        "state": "RUNNING",
        "reason_code": None,
        "phase": "video",
        "defect_groups": [],
    }
    assert summary["allowed_actions"] == []
    stages = summary["stages"]
    assert isinstance(stages, list)
    video_stage = stages[7]
    assert "generate_group_video" not in video_stage["allowed_actions"]
    assert "generate_group_videos_batch" not in video_stage["allowed_actions"]


def test_summary_auto_paused_overrides_reason_and_offers_resume() -> None:
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(),
        ("PAUSED", "video_failed:submission_budget_exhausted", {"phase": "video"}),
    )
    summary = service.workflow_summary(owner, project.id)
    assert summary["blocking_reason"] == "video_failed:submission_budget_exhausted"
    actions = summary["allowed_actions"]
    assert "RESUME_AUTO_PIPELINE" in actions
    # PAUSED 保留底层阶段动作（恢复后人工仍可处理）。
    assert "generate_group_video" in actions
    assert summary["auto_pipeline"]["state"] == "PAUSED"  # type: ignore[index]
    # 无白名单细节键时不暴露 details。
    assert "details" not in summary["auto_pipeline"]  # type: ignore[operator]

    flow = service.project_flow(owner, project)
    assert flow["blocking_reason"] == "video_failed:submission_budget_exhausted"
    assert flow["attention"] is True


def test_summary_auto_paused_exposes_pause_details_whitelist() -> None:
    """§20 暂停自描述：PAUSED 暴露可执行细节白名单，内部书签不暴露。"""
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(),
        (
            "PAUSED",
            "director_validation_failed",
            {
                "phase": "storyboard_drafts",
                "group_sequences": [3, 7],
                "failure_messages": {"3": "Real narration gate has not passed"},
                "video_batch_job_id": str(uuid4()),
            },
        ),
    )
    summary = service.workflow_summary(owner, project.id)
    pipeline = summary["auto_pipeline"]
    assert pipeline["details"] == {  # type: ignore[index]
        "group_sequences": [3, 7],
        "failure_messages": {"3": "Real narration gate has not passed"},
    }
    assert summary["blocking_reason"] == "director_validation_failed"
    actions = summary["allowed_actions"]
    assert "RESUME_AUTO_PIPELINE" in actions


def test_summary_auto_completed_restores_normal_actions() -> None:
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(videos=1, compositions=1),
        ("COMPLETED", None, {"phase": "completed"}),
    )
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "VIDEO_READY"
    assert "compose" in summary["allowed_actions"]
    assert "export" in summary["allowed_actions"]
    assert summary["auto_pipeline"]["state"] == "COMPLETED"  # type: ignore[index]


def test_summary_auto_exposes_defect_groups_and_empty_list_when_clean() -> None:
    # 带病交付缺陷透出（2026-10-16 决策）：键始终存在，无缺陷时为空数组。
    defects: tuple[VideoDefectGroupFact, ...] = (
        {
            "group_sequence": 2,
            "error_code": "video_content_black_region",
            "findings": [{"start_second": 0.0, "end_second": 4.0}],
        },
    )
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(videos=1, compositions=1),
        ("COMPLETED", None, {"phase": "completed"}),
        defect_groups=defects,
    )
    summary = service.workflow_summary(owner, project.id)
    assert summary["auto_pipeline"]["defect_groups"] == [  # type: ignore[index]
        {
            "group_sequence": 2,
            "error_code": "video_content_black_region",
            "findings": [{"start_second": 0.0, "end_second": 4.0}],
        }
    ]
    # 缺陷透出不影响阶段判定、动作与暂停语义。
    assert summary["stage"] == "VIDEO_READY"
    assert summary["blocking_reason"] is None

    clean_service, clean_owner, clean_project = _summary_service(
        "AUTO",
        _summary_counts(videos=1, compositions=1),
        ("COMPLETED", None, {"phase": "completed"}),
    )
    clean = clean_service.workflow_summary(clean_owner, clean_project.id)
    assert clean["auto_pipeline"]["defect_groups"] == []  # type: ignore[index]

    # MANUAL 项目不输出 auto_pipeline（含 defect_groups）。
    manual_service, manual_owner, manual_project = _summary_service(
        "MANUAL",
        _summary_counts(videos=1),
        defect_groups=defects,
    )
    manual = manual_service.workflow_summary(manual_owner, manual_project.id)
    assert "auto_pipeline" not in manual


def _composition_failed_states() -> dict[str, str | None]:
    return {
        "project_director": None,
        "director": None,
        "storyboard": None,
        "video": None,
        "composition": "FAILED",
    }


def test_summary_auto_completed_suppresses_residual_composition_failure() -> None:
    # §17.2：COMPLETED 为权威终态，完成前派发（dispatched_at 早于完成时间）的
    # 残留合成失败不再驱动「需要恢复」徽章与阻塞原因展示。
    completed = NOW + timedelta(days=2)
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(videos=1, compositions=1),
        ("COMPLETED", None, {"phase": "completed"}),
        attempt_states=_composition_failed_states(),
        completed_at=completed,
        dispatched_at=completed - timedelta(seconds=24),
    )
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "VIDEO_READY"
    assert summary["blocking_reason"] is None
    stages = summary["stages"]
    assert isinstance(stages, list)
    composition_stage = next(s for s in stages if s["key"] == "composition_export")
    assert composition_stage["status"] == "complete"
    assert composition_stage["blocking_reason"] is None
    # 手动入口保留：完成后的显式重试与导出仍可用。
    assert "RETRY_COMPOSITION" in composition_stage["allowed_actions"]
    assert "export" in composition_stage["allowed_actions"]

    flow = service.project_flow(owner, project)
    assert flow["stage"] == "VIDEO_READY"
    assert flow["blocking_reason"] is None
    assert flow["attention"] is False


def test_summary_auto_completed_keeps_post_completion_composition_failure() -> None:
    # §17.2 精确版豁免：完成后新派发（dispatched_at 晚于完成时间）的合成失败仍如实提示。
    completed = NOW + timedelta(days=2)
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(videos=1, compositions=1),
        ("COMPLETED", None, {"phase": "completed"}),
        attempt_states=_composition_failed_states(),
        completed_at=completed,
        dispatched_at=completed + timedelta(minutes=5),
    )
    summary = service.workflow_summary(owner, project.id)
    assert summary["blocking_reason"] == "composition_attempt_failed"
    stages = summary["stages"]
    assert isinstance(stages, list)
    composition_stage = next(s for s in stages if s["key"] == "composition_export")
    assert composition_stage["blocking_reason"] == "composition_attempt_failed"

    flow = service.project_flow(owner, project)
    assert flow["attention"] is True
    assert flow["blocking_reason"] == "composition_attempt_failed"


def test_summary_manual_is_byte_identical_without_auto_key() -> None:
    service, owner, project = _summary_service("MANUAL", _summary_counts())
    summary = service.workflow_summary(owner, project.id)
    assert "auto_pipeline" not in summary
    assert summary["stage"] == "VIDEO"
    assert summary["blocking_reason"] == "videos_incomplete"
    assert summary["allowed_actions"] == ["generate_group_video", "generate_group_videos_batch"]
    flow = service.project_flow(owner, project)
    assert flow["blocking_reason"] == "videos_incomplete"
    assert flow["attention"] is False


def test_sweep_only_advances_running_runs() -> None:
    runs = FakeRunRepository()
    facts = FakeFacts(make_facts())
    service = make_service(runs, facts)
    paused = start_run(runs)
    runs.runs[paused.run_id] = paused.paused("composition_failed", NOW, phase="composition")
    assert service.sweep_once() == 0


# ---------------------------------------------------------------------------
# 阶段用时（2026-10-17）
# ---------------------------------------------------------------------------


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def test_domain_phase_timing_settles_previous_and_excludes_pause() -> None:
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    assert run.payload["run_started_ms"] == _ms(NOW)
    assert run.payload["timing_from_phase"] == "narration"
    assert run.payload["phase_started_ms"] == _ms(NOW)

    run = run.touched(NOW + timedelta(seconds=30), phase="director_preplan")
    assert run.payload["phase_elapsed_ms"] == {"narration": 30_000}

    run = run.paused(
        "director_preplan_failed",
        NOW + timedelta(seconds=40),
        phase="director_preplan",
    )
    assert run.payload["paused_at_ms"] == _ms(NOW + timedelta(seconds=40))

    run = run.resumed(2, NOW + timedelta(seconds=100))
    assert "paused_at_ms" not in run.payload
    assert run.payload["run_paused_ms"] == 60_000
    assert run.payload["phase_paused_ms"] == 60_000

    run = run.touched(NOW + timedelta(seconds=120), phase="storyboard")
    # preplan：暂停前 10 秒 + 恢复后 20 秒；60 秒暂停被扣除。
    assert run.payload["phase_elapsed_ms"] == {
        "narration": 30_000,
        "director_preplan": 30_000,
    }


def test_domain_pause_that_changes_phase_settles_previous() -> None:
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    run = run.touched(NOW + timedelta(seconds=25), phase="narration")
    assert run.payload["phase_started_ms"] == _ms(NOW)  # 同阶段不重置起点

    run = run.paused(
        "oversized_sentence",
        NOW + timedelta(seconds=30),
        phase="video",
        details={"groups": []},
    )
    assert run.payload["phase"] == "video"
    assert run.payload["phase_elapsed_ms"] == {"narration": 30_000}
    assert run.payload["phase_started_ms"] == _ms(NOW + timedelta(seconds=30))
    assert run.payload["phase_paused_ms"] == 0
    assert run.payload["paused_at_ms"] == _ms(NOW + timedelta(seconds=30))


def test_domain_completed_finalizes_and_total_matches_phase_sum() -> None:
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    run = run.touched(NOW + timedelta(seconds=10), phase="composition")
    done = run.completed(NOW + timedelta(seconds=30))
    assert done.payload["phase"] == "completed"
    assert done.payload["phase_started_ms"] is None
    assert done.payload["completed_ms"] == _ms(NOW + timedelta(seconds=30))
    assert done.payload["phase_elapsed_ms"] == {
        "narration": 10_000,
        "composition": 20_000,
    }
    timing = phase_timings(done.payload, "COMPLETED", NOW + timedelta(hours=2))
    assert timing is not None
    assert timing["total_elapsed_ms"] == 30_000
    assert timing["phases"]["composition"] == {"elapsed_ms": 20_000, "running": False}


def test_domain_legacy_payload_starts_timing_on_next_phase() -> None:
    legacy = AutoPipelineRun(
        uuid4(),
        OWNER,
        PROJECT,
        AutoPipelineState.RUNNING,
        None,
        1,
        {"phase": "video"},
        NOW,
        NOW,
    )
    advanced = legacy.touched(NOW + timedelta(seconds=15), phase="composition")
    assert advanced.payload["timing_from_phase"] == "composition"
    assert advanced.payload["phase_started_ms"] == _ms(NOW + timedelta(seconds=15))
    # 历史阶段不补算。
    assert "phase_elapsed_ms" not in advanced.payload
    timing = phase_timings(advanced.payload, "RUNNING", NOW + timedelta(seconds=45))
    assert timing is not None
    assert timing["from_phase"] == "composition"
    assert timing["total_elapsed_ms"] is None
    assert timing["phases"] == {"composition": {"elapsed_ms": 30_000, "running": True}}


def test_domain_timing_payload_is_json_safe() -> None:
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    run = run.touched(NOW + timedelta(seconds=30), phase="video")
    run = run.paused("video_failed:x", NOW + timedelta(seconds=40), phase="video")
    run = run.resumed(2, NOW + timedelta(seconds=100))
    run = run.completed(NOW + timedelta(seconds=120))
    # datetime 会在这里抛 TypeError；阶段计时必须是 JSON 安全类型。
    json.dumps(dict(run.payload))


def test_advance_same_phase_does_not_rewrite_timing_payload() -> None:
    clock = [NOW]
    runs = FakeRunRepository()
    start_run(runs)
    service = AutoPipelineService(
        runs,
        FakeFacts(make_facts(narration_ready=False)),
        clock=lambda: clock[0],
    )
    baseline = runs.saves
    service.advance(OWNER, PROJECT)
    assert runs.saves == baseline
    clock[0] = NOW + timedelta(seconds=20)
    service.advance(OWNER, PROJECT)
    assert runs.saves == baseline


def test_phase_timings_running_includes_partial_and_total() -> None:
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    run = run.touched(NOW + timedelta(seconds=40), phase="video")
    timing = phase_timings(run.payload, "RUNNING", NOW + timedelta(seconds=70))
    assert timing is not None
    assert timing["total_elapsed_ms"] == 70_000
    assert timing["phases"]["narration"] == {"elapsed_ms": 40_000, "running": False}
    assert timing["phases"]["video"] == {"elapsed_ms": 30_000, "running": True}


def test_phase_timings_paused_freezes_current_and_total() -> None:
    run = new_auto_pipeline_run(uuid4(), OWNER, PROJECT, NOW)
    run = run.touched(NOW + timedelta(seconds=40), phase="video")
    paused = run.paused("video_failed:x", NOW + timedelta(seconds=40), phase="video")
    timing = phase_timings(paused.payload, "PAUSED", NOW + timedelta(seconds=100))
    assert timing is not None
    assert timing["phases"]["video"] == {"elapsed_ms": 0, "running": False}
    assert timing["total_elapsed_ms"] == 40_000
    assert timing["paused_ms"] == 60_000


def test_phase_timings_returns_none_without_timing() -> None:
    assert phase_timings({}, "RUNNING", NOW) is None
    assert phase_timings({"phase": "video"}, "RUNNING", NOW) is None


def test_phase_timings_ignores_malformed_values() -> None:
    payload = {
        "timing_from_phase": "narration",
        "run_started_ms": "bad",
        "phase": "storyboard",
        "phase_started_ms": "bad",
        "phase_elapsed_ms": {
            "narration": "bad",
            "video": -1,
            "storyboard": 5000,
            "unknown": 10,
        },
        "phase_paused_ms": "bad",
        "run_paused_ms": -5,
    }
    timing = phase_timings(payload, "RUNNING", NOW)
    assert timing is not None
    assert timing["phases"] == {"storyboard": {"elapsed_ms": 5000, "running": False}}
    assert timing["total_elapsed_ms"] is None
    assert timing["paused_ms"] == 0


def test_summary_exposes_timing_only_when_payload_has_it() -> None:
    payload = {
        "phase": "video",
        "run_started_ms": _ms(NOW),
        "timing_from_phase": "narration",
        "phase_started_ms": _ms(NOW + timedelta(seconds=30)),
        "phase_elapsed_ms": {"narration": 30_000},
        "phase_paused_ms": 0,
        "run_paused_ms": 0,
    }
    service, owner, project = _summary_service(
        "AUTO",
        _summary_counts(),
        ("RUNNING", None, payload),
    )
    summary = service.workflow_summary(
        owner, project.id, now=NOW + timedelta(seconds=40)
    )
    timing = summary["auto_pipeline"]["timing"]  # type: ignore[index]
    assert timing["phases"]["narration"] == {"elapsed_ms": 30_000, "running": False}
    assert timing["phases"]["video"] == {"elapsed_ms": 10_000, "running": True}
    assert timing["total_elapsed_ms"] == 40_000


def test_phase_timings_never_grows_without_terminal_timestamp() -> None:
    completed = {
        "phase": "completed",
        "timing_from_phase": "narration",
        "run_started_ms": _ms(NOW),
        "phase_started_ms": None,
        "phase_elapsed_ms": {},
    }
    timing = phase_timings(completed, "COMPLETED", NOW + timedelta(hours=1))
    assert timing is not None
    assert timing["total_elapsed_ms"] is None

    paused = {
        "phase": "video",
        "timing_from_phase": "narration",
        "run_started_ms": _ms(NOW),
        "phase_started_ms": _ms(NOW),
    }
    timing = phase_timings(paused, "PAUSED", NOW + timedelta(hours=1))
    assert timing is not None
    assert timing["total_elapsed_ms"] is None
    assert "video" not in timing["phases"]


def test_video_phase_starts_when_batch_dispatch_starts() -> None:
    clock = [NOW]
    runs = FakeRunRepository()
    run = AutoPipelineRun(
        uuid4(),
        OWNER,
        PROJECT,
        AutoPipelineState.RUNNING,
        None,
        1,
        {
            "phase": "storyboard",
            "run_started_ms": _ms(NOW - timedelta(minutes=10)),
            "timing_from_phase": "narration",
            "phase_started_ms": _ms(NOW - timedelta(minutes=5)),
            "phase_elapsed_ms": {"narration": 60_000},
            "phase_paused_ms": 0,
            "run_paused_ms": 0,
        },
        NOW,
        NOW,
    )
    runs.runs[run.run_id] = run
    videos = SlowFailingVideos(clock)
    service = AutoPipelineService(
        runs,
        FakeFacts(
            make_facts(
                videos_complete=False,
                video_groups=(video_group(GROUP_A, 1),),
            )
        ),
        videos=videos,
        clock=lambda: clock[0],
    )
    updated = service.advance(OWNER, PROJECT)
    assert updated is not None
    payload = updated.payload
    assert payload["phase"] == "video"
    # 视频起点 = 派发开始，而不是 process_all 结束/暂停时刻。
    assert payload["phase_started_ms"] == _ms(NOW)
    assert payload["paused_at_ms"] == _ms(NOW + timedelta(minutes=20))
    # storyboard 只结算到派发开始（5 分钟），派发 20 分钟归 video。
    assert payload["phase_elapsed_ms"]["storyboard"] == 300_000
    timing = phase_timings(payload, "PAUSED", NOW + timedelta(minutes=20))
    assert timing is not None
    assert timing["phases"]["storyboard"] == {"elapsed_ms": 300_000, "running": False}
    assert timing["phases"]["video"] == {"elapsed_ms": 1_200_000, "running": False}


# ---------------------------------------------------------------------------
# C3（2026-10-21）：意图阶段失败纳入每组 6 次提交预算
# ---------------------------------------------------------------------------


class ScriptedIntentVideos(StubVideos):
    """按脚本让意图创建先失败若干次，再成功（用于验证 C3 重试与预算）。"""

    def __init__(self, *, fail_times: int) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.failures = 0
        self.mark_calls: list[tuple[UUID, str]] = []

    def process_all(self, job_id: UUID, *, respect_capacity_queue: bool = False) -> None:
        self.process_calls.append(job_id)
        job = self.jobs[job_id]
        if self.failures < self.fail_times:
            self.failures += 1
            status, attempt_id, message = (
                "RETRY_WAIT",
                None,
                "video_prompt_compilation_failed",
            )
        else:
            status, attempt_id, message = "queued", uuid4(), None
        self.jobs[job_id] = VideoBatchJobView(
            job.job_id,
            job.owner_id,
            job.project_id,
            job.force,
            job.total,
            job.created_at,
            job.completed_at,
            tuple(
                VideoBatchItemView(GROUP_A, 1, status, attempt_id, message) for _ in job.items
            ),
        )

    def mark_item(
        self, job_id: UUID, group_id: UUID, status: str, *, message: str | None = None
    ) -> None:
        self.mark_calls.append((group_id, status))
        job = self.jobs[job_id]
        self.jobs[job_id] = VideoBatchJobView(
            job.job_id,
            job.owner_id,
            job.project_id,
            job.force,
            job.total,
            job.created_at,
            job.completed_at,
            tuple(
                VideoBatchItemView(
                    item.group_id, item.sequence, status, item.attempt_id, message
                )
                for item in job.items
            ),
        )


def _c3_service(
    runs: FakeRunRepository, facts: FakeFacts, videos: StubVideos, clock: list[datetime]
) -> AutoPipelineService:
    return AutoPipelineService(runs, facts, videos=videos, clock=lambda: clock[0])


def test_video_intent_retry_scheduled_then_succeeds() -> None:
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )
    videos = ScriptedIntentVideos(fail_times=1)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)

    first = service.advance(OWNER, PROJECT)
    assert first is not None and first.state is AutoPipelineState.RUNNING
    state = first.payload["video_intent_retry"]
    assert state["failures"] == {str(GROUP_A): 1}
    assert state["next_ms"][str(GROUP_A)] == _ms(NOW) + 30_000
    # 首次失败不立即重试：本 tick 不再 process_all（只有派发那一次）。
    assert len(videos.process_calls) == 1

    clock[0] = NOW + timedelta(seconds=31)
    second = service.advance(OWNER, PROJECT)
    assert second is not None and second.state is AutoPipelineState.RUNNING
    assert videos.mark_calls == [(GROUP_A, "PENDING")]
    assert len(videos.process_calls) == 2
    assert second.payload["video_intent_retry"]["failures"] == {str(GROUP_A): 1}


def test_video_intent_retry_exhausts_budget_and_pauses() -> None:
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )
    videos = ScriptedIntentVideos(fail_times=99)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    for _ in range(5):
        assert run is not None and run.state is AutoPipelineState.RUNNING
        clock[0] = clock[0] + timedelta(seconds=200)
        run = service.advance(OWNER, PROJECT)

    assert run is not None
    assert run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "video_failed:submission_budget_exhausted"
    assert (GROUP_A, "submission_budget_exhausted") in videos.mark_calls
    assert len(videos.process_calls) == 6


def test_video_intent_retry_counts_existing_submissions_against_budget() -> None:
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(
            videos_complete=False,
            video_groups=(video_group(GROUP_A, 1, submissions=5),),
        )
    )
    videos = ScriptedIntentVideos(fail_times=99)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    assert run is not None
    assert run.state is AutoPipelineState.PAUSED
    assert run.reason_code == "video_failed:submission_budget_exhausted"


def test_video_intent_retry_counter_resets_with_new_budget_round() -> None:
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )
    videos = ScriptedIntentVideos(fail_times=2)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)
    first = service.advance(OWNER, PROJECT)
    assert first is not None
    assert first.payload["video_intent_retry"]["failures"] == {str(GROUP_A): 1}

    # 新一轮授权（budget_round=2）：旧轮次计数失效，从 1 重新开始。
    facts.facts = make_facts(
        videos_complete=False,
        budget_round=2,
        video_groups=(video_group(GROUP_A, 1, budget_round=2),),
    )
    run = service.advance(OWNER, PROJECT)
    assert run is not None
    state = run.payload["video_intent_retry"]
    assert state["round"] == 2
    assert state["failures"] == {str(GROUP_A): 1}


class ScriptedCapacityVideos(ScriptedIntentVideos):
    """前 N 次推进都返回 WAITING_CAPACITY（模拟 H3 无空闲槽位），之后成功。"""

    def __init__(self, *, wait_times: int) -> None:
        super().__init__(fail_times=0)
        self.wait_times = wait_times
        self.waits = 0

    def process_all(self, job_id: UUID, *, respect_capacity_queue: bool = False) -> None:
        self.process_calls.append(job_id)
        job = self.jobs[job_id]
        if self.waits < self.wait_times:
            self.waits += 1
            status, attempt_id, message = (
                "WAITING_CAPACITY",
                None,
                "video_capacity_unavailable",
            )
        else:
            status, attempt_id, message = "queued", uuid4(), None
        self.jobs[job_id] = VideoBatchJobView(
            job.job_id,
            job.owner_id,
            job.project_id,
            job.force,
            job.total,
            job.created_at,
            job.completed_at,
            tuple(
                VideoBatchItemView(item.group_id, item.sequence, status, attempt_id, message)
                for item in job.items
            ),
        )


def test_video_capacity_wait_keeps_running_and_retries() -> None:
    """2026-10-31：无 H3 容量不再暂停，按 20s 退避排队重试。"""
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )
    videos = ScriptedCapacityVideos(wait_times=1)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)

    first = service.advance(OWNER, PROJECT)
    assert first is not None and first.state is AutoPipelineState.RUNNING
    state = first.payload["video_capacity_wait"]
    assert state["since_ms"] == _ms(NOW)
    assert state["next_ms"][str(GROUP_A)] == _ms(NOW) + 20_000
    assert len(videos.process_calls) == 1

    clock[0] = NOW + timedelta(seconds=21)
    second = service.advance(OWNER, PROJECT)
    assert second is not None and second.state is AutoPipelineState.RUNNING
    assert (GROUP_A, "PENDING") in videos.mark_calls
    assert len(videos.process_calls) == 2
    # 已拿到槽位：等待状态清空
    assert second.payload.get("video_capacity_wait") is None


def test_video_capacity_wait_does_not_consume_intent_budget() -> None:
    """容量等待不计入 6 次提交预算：等 8 次仍 RUNNING，不出现预算耗尽暂停。"""
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )
    videos = ScriptedCapacityVideos(wait_times=99)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)

    run = service.advance(OWNER, PROJECT)
    for _ in range(8):
        assert run is not None and run.state is AutoPipelineState.RUNNING
        clock[0] = clock[0] + timedelta(seconds=61)
        run = service.advance(OWNER, PROJECT)

    assert run is not None and run.state is AutoPipelineState.RUNNING
    assert (GROUP_A, "submission_budget_exhausted") not in videos.mark_calls


def test_video_capacity_wait_timeout_pauses_with_stable_reason() -> None:
    """等待超过 6 小时才暂停，原因码 video_capacity_timeout。"""
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )
    videos = ScriptedCapacityVideos(wait_times=999)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)

    first = service.advance(OWNER, PROJECT)
    assert first is not None and first.state is AutoPipelineState.RUNNING

    clock[0] = NOW + timedelta(hours=6, seconds=5)
    second = service.advance(OWNER, PROJECT)
    assert second is not None
    assert second.state is AutoPipelineState.PAUSED
    assert second.reason_code == "video_capacity_timeout"
    assert second.payload["group_sequences"] == [1]


class ScriptedErrorVideos(ScriptedCapacityVideos):
    """首次推进把项落成 error 终态（模拟上一轮既定失败），之后成功。"""

    def process_all(self, job_id: UUID, *, respect_capacity_queue: bool = False) -> None:
        self.process_calls.append(job_id)
        job = self.jobs[job_id]
        if self.waits < 1:
            self.waits += 1
            status, attempt_id, message = "error", None, "capacity_error_history"
        else:
            status, attempt_id, message = "queued", uuid4(), None
        self.jobs[job_id] = VideoBatchJobView(
            job.job_id,
            job.owner_id,
            job.project_id,
            job.force,
            job.total,
            job.created_at,
            job.completed_at,
            tuple(
                VideoBatchItemView(item.group_id, item.sequence, status, attempt_id, message)
                for item in job.items
            ),
        )


def test_video_new_budget_round_reopens_previous_failed_items() -> None:
    """2026-10-31：恢复=新轮次，必须把上一轮 error 项翻回 PENDING，否则立刻再暂停。"""
    clock = [NOW]
    runs = FakeRunRepository()
    facts = FakeFacts(
        make_facts(videos_complete=False, video_groups=(video_group(GROUP_A, 1),))
    )
    videos = ScriptedErrorVideos(wait_times=1)
    service = _c3_service(runs, facts, videos, clock)
    start_run(runs)

    first = service.advance(OWNER, PROJECT)
    assert first is not None and first.state is AutoPipelineState.PAUSED
    job_id = next(iter(videos.jobs))
    assert videos.jobs[job_id].items[0].status == "error"

    # 用户恢复：授权轮次 +1
    facts.facts = make_facts(
        videos_complete=False,
        budget_round=2,
        video_groups=(video_group(GROUP_A, 1, budget_round=2),),
    )
    service.resume(OWNER, PROJECT)
    second = service.advance(OWNER, PROJECT)
    assert second is not None and second.state is AutoPipelineState.RUNNING
    assert second.payload["video_batch_job_round"] == 2
    assert videos.jobs[job_id].items[0].status == "queued"
