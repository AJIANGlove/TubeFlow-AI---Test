from __future__ import annotations

from uuid import UUID, uuid4

from tubeflow_test.application.video_batch import (
    GroupVideoBatchService,
    VideoBatchGroupState,
    VideoBatchItemView,
    VideoBatchJobView,
)
from tubeflow_test.domain.errors import DomainError


class FakeRepository:
    def __init__(self, states: list[VideoBatchGroupState]) -> None:
        self.states = states

    def list_group_states(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[VideoBatchGroupState, ...]:
        return tuple(self.states)


class FakeJobs:
    def __init__(self) -> None:
        self.jobs: dict[UUID, dict[str, object]] = {}
        self.items: dict[tuple[UUID, UUID], VideoBatchItemView] = {}
        self.oldest_waiting: UUID | None = None

    def create_job(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        force: bool,
        items: tuple[tuple[UUID, int, str], ...],
    ) -> UUID:
        from datetime import UTC, datetime

        job_id = uuid4()
        self.jobs[job_id] = {
            "owner_id": owner_id,
            "project_id": project_id,
            "force": force,
            "total": len(items),
            "created_at": datetime.now(UTC),
            "completed_at": None,
        }
        for group_id, sequence, status in items:
            self.items[(job_id, group_id)] = VideoBatchItemView(
                group_id, sequence, status, None, None
            )
        return job_id

    def get_job(self, job_id: UUID) -> VideoBatchJobView | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        items = tuple(
            sorted(
                (item for (jid, _), item in self.items.items() if jid == job_id),
                key=lambda item: item.sequence,
            )
        )
        return VideoBatchJobView(
            job_id=job_id,
            owner_id=job["owner_id"],  # type: ignore[arg-type]
            project_id=job["project_id"],  # type: ignore[arg-type]
            force=bool(job["force"]),
            total=int(job["total"]),
            created_at=job["created_at"],  # type: ignore[arg-type]
            completed_at=job["completed_at"],  # type: ignore[arg-type]
            items=items,
        )

    def next_pending_group(self, job_id: UUID) -> tuple[UUID, int] | None:
        pending = [
            item
            for (jid, _), item in self.items.items()
            if jid == job_id and item.status == "PENDING"
        ]
        if not pending:
            return None
        first = sorted(pending, key=lambda item: item.sequence)[0]
        return first.group_id, first.sequence

    def mark_item(
        self,
        job_id: UUID,
        group_id: UUID,
        status: str,
        *,
        attempt_id: UUID | None = None,
        message: str | None = None,
    ) -> None:
        item = self.items[(job_id, group_id)]
        self.items[(job_id, group_id)] = VideoBatchItemView(
            group_id, item.sequence, status, attempt_id, message
        )

    def complete_job(self, job_id: UUID) -> None:
        from datetime import UTC, datetime

        self.jobs[job_id]["completed_at"] = datetime.now(UTC)

    def list_unfinished_job_ids(self) -> tuple[UUID, ...]:
        return tuple(
            job_id for job_id, job in self.jobs.items() if job["completed_at"] is None
        )

    def oldest_waiting_job_id(self) -> UUID | None:
        return self.oldest_waiting

    def reopen_failed_items(self, job_id: UUID) -> int:
        reopened = 0
        for (jid, group_id), item in list(self.items.items()):
            if jid != job_id:
                continue
            if item.status in {
                "error",
                "not_found",
                "no_current_storyboard",
                "submission_budget_exhausted",
            }:
                self.items[(jid, group_id)] = VideoBatchItemView(
                    group_id, item.sequence, "PENDING", None, None
                )
                reopened += 1
        return reopened


class FakeWriter:
    def __init__(self, fail_for: set[UUID] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_for = fail_for or set()

    def create(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        expected_storyboard_generation_id: UUID,
        idempotency_key: str,
    ) -> UUID:
        self.calls.append(
            {
                "group_id": group_id,
                "generation_id": expected_storyboard_generation_id,
                "key": idempotency_key,
            }
        )
        if group_id in self.fail_for:
            raise DomainError("h3_inputs_not_ready", "Canonical H3 inputs are not ready")
        return uuid4()


def _state(sequence: int, **overrides: object) -> VideoBatchGroupState:
    values = {
        "group_id": uuid4(),
        "sequence": sequence,
        "video_blocked": False,
        "block_reason": None,
        "storyboard_generation_id": uuid4(),
        "has_current_video": False,
        "latest_attempt_stage": None,
    }
    values.update(overrides)
    return VideoBatchGroupState(**values)  # type: ignore[arg-type]


def _service(
    states: list[VideoBatchGroupState], writer: FakeWriter, jobs: FakeJobs
) -> GroupVideoBatchService:
    return GroupVideoBatchService(FakeRepository(states), writer, uuid4, jobs)


def test_overview_carries_latest_attempt_stalled_flag() -> None:
    stalled = _state(1, latest_attempt_stage="RUNNING", latest_attempt_stalled=True)
    items = _service([stalled], FakeWriter(), FakeJobs()).overview(uuid4(), uuid4())

    assert items[0].latest_attempt_stalled is True
    assert items[0].eligible is False and items[0].reason == "already_running"


def test_overview_marks_each_group_with_stable_reasons() -> None:
    blocked = _state(1, video_blocked=True, block_reason="single_sentence_exceeds_15000ms")
    no_storyboard = _state(2, storyboard_generation_id=None)
    has_video = _state(3, has_current_video=True)
    running = _state(4, latest_attempt_stage="RUNNING")
    unknown = _state(5, latest_attempt_stage="REMOTE_STATE_UNKNOWN")
    ready = _state(6, latest_attempt_stage="FAILED")
    items = _service(
        [blocked, no_storyboard, has_video, running, unknown, ready], FakeWriter(), FakeJobs()
    ).overview(uuid4(), uuid4())

    assert [(item.sequence, item.eligible, item.reason) for item in items] == [
        (1, False, "single_sentence_exceeds_15000ms"),
        (2, False, "no_current_storyboard"),
        (3, False, "has_current_video"),
        (4, False, "already_running"),
        (5, False, "remote_acceptance_unknown"),
        (6, True, None),
    ]


def test_overview_force_opens_groups_with_current_video_but_never_blocked() -> None:
    has_video = _state(1, has_current_video=True)
    blocked = _state(2, video_blocked=True, block_reason="single_sentence_exceeds_15000ms")
    items = _service([has_video, blocked], FakeWriter(), FakeJobs()).overview(
        uuid4(), uuid4(), force=True
    )

    assert [(item.sequence, item.eligible) for item in items] == [(1, True), (2, False)]


def test_start_registers_job_with_eligibility_snapshot_and_returns_immediately() -> None:
    ready = _state(1)
    skipped = _state(2, has_current_video=True)
    blocked = _state(3, video_blocked=True, block_reason="single_sentence_exceeds_15000ms")
    jobs = FakeJobs()
    writer = FakeWriter()
    view = _service([ready, skipped, blocked], writer, jobs).start(
        uuid4(), uuid4(), (ready.group_id, skipped.group_id, blocked.group_id, uuid4()), force=False
    )

    assert sorted(item.status for item in view.items) == [
        "PENDING",
        "has_current_video",
        "not_found",
        "single_sentence_exceeds_15000ms",
    ]
    assert view.total == 4 and view.completed_at is None
    assert writer.calls == []


def test_process_all_creates_one_intent_per_pending_group_with_unique_keys() -> None:
    ready_one = _state(1, latest_attempt_stage="ARCHIVED")
    ready_two = _state(2)
    skipped = _state(3, has_current_video=True)
    jobs = FakeJobs()
    writer = FakeWriter()
    service = _service([ready_one, ready_two, skipped], writer, jobs)
    owner, project = uuid4(), uuid4()
    view = service.start(
        owner, project, (ready_one.group_id, ready_two.group_id, skipped.group_id), force=False
    )

    service.process_all(view.job_id)

    assert len(writer.calls) == 2
    keys = [call["key"] for call in writer.calls]
    assert len(set(keys)) == 2
    assert all(str(key).startswith(f"batch-{view.job_id}-") for key in keys)
    final = service.get_job(owner, view.job_id)
    assert final is not None and final.completed_at is not None
    assert [item.status for item in final.items] == [
        "queued",
        "queued",
        "has_current_video",
    ]
    assert jobs.list_unfinished_job_ids() == ()


def test_process_isolates_single_group_failure_and_continues() -> None:
    failing = _state(1)
    healthy = _state(2)
    jobs = FakeJobs()
    writer = FakeWriter(fail_for={failing.group_id})
    service = _service([failing, healthy], writer, jobs)
    owner, project = uuid4(), uuid4()
    view = service.start(owner, project, (failing.group_id, healthy.group_id), force=False)

    service.process_all(view.job_id)

    final = service.get_job(owner, view.job_id)
    assert final is not None
    assert [item.status for item in final.items] == ["error", "queued"]
    assert final.items[0].message == "Canonical H3 inputs are not ready"
    assert final.items[1].attempt_id is not None


def test_process_next_rechecks_current_state_at_processing_time() -> None:
    ready = _state(1)
    jobs = FakeJobs()
    writer = FakeWriter()
    states = [ready]
    service = _service(states, writer, jobs)
    owner, project = uuid4(), uuid4()
    view = service.start(owner, project, (ready.group_id,), force=False)

    states[0] = _state(
        1,
        group_id=ready.group_id,
        storyboard_generation_id=ready.storyboard_generation_id,
        has_current_video=True,
    )
    service.process_all(view.job_id)

    assert writer.calls == []
    final = service.get_job(owner, view.job_id)
    assert final is not None and final.items[0].status == "has_current_video"


def test_unfinished_jobs_support_resume_after_restart() -> None:
    ready = _state(1)
    jobs = FakeJobs()
    service = _service([ready], FakeWriter(), jobs)
    owner, project = uuid4(), uuid4()
    view = service.start(owner, project, (ready.group_id,), force=False)

    assert jobs.list_unfinished_job_ids() == (view.job_id,)
    service.process_all(view.job_id)
    assert jobs.list_unfinished_job_ids() == ()


def test_process_all_stops_at_in_flight_limit() -> None:
    """S6：批量提交必须受在途上限约束。

    否则 140 组会在几秒内全部推成 queued，等价于同时起 140 个常驻
    Temporal workflow，把单线程 workflow executor 压死。
    """
    from dataclasses import replace

    states = [_state(i) for i in range(1, 16)]  # 15 组
    repository = FakeRepository(states)
    jobs = FakeJobs()
    created: list[UUID] = []

    class AdvancingWriter:
        def create(
            self,
            *,
            owner_id: UUID,
            group_id: UUID,
            expected_storyboard_generation_id: UUID,
            idempotency_key: str,
        ) -> UUID:
            created.append(group_id)
            repository.states = [
                replace(state, latest_attempt_stage="CREATED")
                if state.group_id == group_id
                else state
                for state in repository.states
            ]
            return uuid4()

    service = GroupVideoBatchService(
        repository, AdvancingWriter(), uuid4, jobs, in_flight_limit=5
    )
    owner_id, project_id = uuid4(), uuid4()
    view = service.start(
        owner_id, project_id, tuple(state.group_id for state in states), force=False
    )
    service.process_all(view.job_id)

    assert len(created) == 5
    current = service.get_job(owner_id, view.job_id)
    assert current is not None
    assert sum(1 for item in current.items if item.status == "PENDING") == 10


def test_process_all_without_limit_drains_every_pending_group() -> None:
    """limit=0 时保持旧行为，用于显式放开的场景。"""
    from dataclasses import replace

    states = [_state(i) for i in range(1, 9)]
    repository = FakeRepository(states)
    jobs = FakeJobs()
    created: list[UUID] = []

    class AdvancingWriter:
        def create(
            self,
            *,
            owner_id: UUID,
            group_id: UUID,
            expected_storyboard_generation_id: UUID,
            idempotency_key: str,
        ) -> UUID:
            created.append(group_id)
            repository.states = [
                replace(state, latest_attempt_stage="CREATED")
                if state.group_id == group_id
                else state
                for state in repository.states
            ]
            return uuid4()

    service = GroupVideoBatchService(
        repository, AdvancingWriter(), uuid4, jobs, in_flight_limit=0
    )
    owner_id, project_id = uuid4(), uuid4()
    view = service.start(
        owner_id, project_id, tuple(state.group_id for state in states), force=False
    )
    service.process_all(view.job_id)

    assert len(created) == 8


class FakeCapacityWriter(FakeWriter):
    """容量不足：抛可重试意图失败（稳定码 video_capacity_unavailable）。"""

    def create(
        self,
        *,
        owner_id: UUID,
        group_id: UUID,
        expected_storyboard_generation_id: UUID,
        idempotency_key: str,
    ) -> UUID:
        from tubeflow_test.video_generation.domain import RetryableVideoIntentFailure

        raise RetryableVideoIntentFailure(
            "video_capacity_unavailable", "no compatible H3 node has capacity"
        )


def test_capacity_unavailable_marks_waiting_and_keeps_job_open() -> None:
    """2026-10-31：无 H3 槽位落 WAITING_CAPACITY，不落 error、不收口 job。"""
    group = _state(1)
    jobs = FakeJobs()
    service = GroupVideoBatchService(
        FakeRepository([group]), FakeCapacityWriter(), uuid4, jobs
    )
    job = service.start(uuid4(), uuid4(), (group.group_id,), force=False)
    service.process_all(job.job_id)

    view = jobs.get_job(job.job_id)
    assert view is not None
    assert view.items[0].status == "WAITING_CAPACITY"
    assert view.items[0].message == "video_capacity_unavailable"
    assert view.completed_at is None

    service.process_all(job.job_id)
    assert jobs.get_job(job.job_id).completed_at is None  # 仍等待，不收口


def test_process_all_yields_to_older_waiting_job() -> None:
    """多项目 FIFO：更早开始等待的 job 优先认领槽位。"""
    group = _state(1)
    jobs = FakeJobs()
    writer = FakeWriter()
    service = GroupVideoBatchService(FakeRepository([group]), writer, uuid4, jobs)
    job = service.start(uuid4(), uuid4(), (group.group_id,), force=False)
    jobs.oldest_waiting = uuid4()  # 另一个更早的等待项目

    service.process_all(job.job_id, respect_capacity_queue=True)

    assert writer.calls == []
    assert jobs.get_job(job.job_id).items[0].status == "PENDING"

    jobs.oldest_waiting = job.job_id
    service.process_all(job.job_id, respect_capacity_queue=True)
    assert len(writer.calls) == 1
