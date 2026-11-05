"""AUTO 编排器应用服务：幂等推进（advance）、恢复（resume）与安全踢点。

每次 advance 按权威事实判定当前阶段并执行恰好一个动作；暂停/完成写稳定
reason_code/终态；踢点与巡检共用 kick/sweep，任何内部异常只记日志，绝不
破坏主流程。阶段服务自身幂等与跳过规则兜底重入与并发。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from tubeflow_test.application.storyboard_batch import (
    StoryboardBatchGroupOverview,
    StoryboardBatchSeedResult,
)
from tubeflow_test.application.video_batch import (
    PENDING_STATUS,
    RETRY_WAIT_STATUS,
    SUBMISSION_BUDGET_EXHAUSTED_STATUS,
    WAITING_CAPACITY_STATUS,
    VideoBatchItemView,
    VideoBatchJobView,
)
from tubeflow_test.domain.errors import DomainError

from .domain import (
    REASON_COMPOSITION_FAILED,
    REASON_DIRECTOR_PREPLAN_FAILED,
    REASON_DIRECTOR_VALIDATION_FAILED,
    REASON_OVERSIZED_SENTENCE,
    REASON_VIDEO_CAPACITY_TIMEOUT,
    AutoPipelineRun,
    AutoPipelineState,
    storyboard_failed_reason,
    to_epoch_ms,
    video_failed_reason,
)
from .ports import (
    AutoPipelineFacts,
    AutoPipelineFactsPort,
    AutoPipelineRunRepository,
    CompositionAutoPort,
    DirectorAutoResumePort,
    NarrationAutoResumePort,
    StoryboardAutoPort,
    VideoAutoPort,
)

logger = logging.getLogger(__name__)

# 2026-11-01：单项目的一次 advance 可能阻塞在故事板/视频派发（分钟级）；
# 巡检按项目提交到独立线程并只等待短暂时间，避免一个项目饿死其他项目。
_SWEEP_WAIT_SECONDS = 0.5
_SWEEP_MAX_WORKERS = 4

_DIRECTOR_HANDLED_KEY = "director_failed_run_id"
_VIDEO_JOB_KEY = "video_batch_job_id"
# 该 job 对应的授权轮次；轮次变化时把上一轮终态失败项翻回 PENDING。
_VIDEO_JOB_ROUND_KEY = "video_batch_job_round"
# C3：意图阶段失败计数与退避（按授权轮次失效）。
_VIDEO_INTENT_RETRY_KEY = "video_intent_retry"
_VIDEO_INTENT_BUDGET = 6
_VIDEO_INTENT_RETRY_BACKOFF_SECONDS = (30.0, 60.0, 120.0)
# 2026-10-31：容量不足改为「排队等待」，不计入上面的 6 次预算。
_VIDEO_CAPACITY_KEY = "video_capacity_wait"
_VIDEO_CAPACITY_RETRY_BACKOFF_SECONDS = (20.0, 40.0, 60.0)
_VIDEO_CAPACITY_WAIT_LIMIT_SECONDS = 6 * 60 * 60


def _video_intent_retry_delay_seconds(attempt: int) -> float:
    """第 n 次意图失败后的退避秒数（30/60/120，之后固定 120）。"""
    index = min(max(attempt - 1, 0), len(_VIDEO_INTENT_RETRY_BACKOFF_SECONDS) - 1)
    return _VIDEO_INTENT_RETRY_BACKOFF_SECONDS[index]


def _video_capacity_retry_delay_seconds(attempt: int) -> float:
    """第 n 次容量等待后的重试间隔（20/40/60，之后固定 60）。"""
    index = min(max(attempt - 1, 0), len(_VIDEO_CAPACITY_RETRY_BACKOFF_SECONDS) - 1)
    return _VIDEO_CAPACITY_RETRY_BACKOFF_SECONDS[index]


# 批量 job 逐项状态中的既定失败（不可重试 / 预算耗尽：整项目暂停并展示真实原因）。
_VIDEO_ITEM_FAILURE_STATUSES = frozenset(
    {"error", "not_found", "no_current_storyboard", SUBMISSION_BUDGET_EXHAUSTED_STATUS}
)


def _error_messages(results: tuple[StoryboardBatchSeedResult, ...]) -> dict[str, str]:
    """播种/修复失败组的逐组原因（组号 → 消息），供暂停详情如实暴露给页面。"""
    return {
        str(item.group_sequence): item.message
        for item in results
        if item.outcome == "error" and item.message
    }


class AutoPipelineService:
    """仅 AUTO 项目的一键生产编排；MANUAL 项目永远没有运行记录。"""

    def __init__(
        self,
        runs: AutoPipelineRunRepository,
        facts: AutoPipelineFactsPort,
        *,
        narration: NarrationAutoResumePort | None = None,
        director: DirectorAutoResumePort | None = None,
        storyboards: StoryboardAutoPort | None = None,
        videos: VideoAutoPort | None = None,
        compositions: CompositionAutoPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runs = runs
        self._facts = facts
        self._narration = narration
        self._director = director
        self._storyboards = storyboards
        self._videos = videos
        self._compositions = compositions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sweep_pool = ThreadPoolExecutor(
            max_workers=_SWEEP_MAX_WORKERS, thread_name_prefix="auto-sweep"
        )
        self._sweep_inflight: set[UUID] = set()
        self._sweep_guard = threading.Lock()

    def advance(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun | None:
        """持锁推进恰好一个动作；无活动运行或非 RUNNING 时不做任何事。"""
        with self._runs.lock_active(owner_id, project_id) as lock:
            run = lock.run
            if run is None or run.state is not AutoPipelineState.RUNNING:
                return run
            facts = self._facts.load(owner_id, project_id)
            if facts.production_mode != "AUTO":
                # 防御：AUTO 运行只可能属于 AUTO 项目；异常时不做动作。
                logger.error(
                    "auto pipeline run %s found for non-AUTO project %s",
                    run.run_id,
                    project_id,
                )
                return run
            updated = self._advance_locked(run, facts)
            if updated is not run:
                lock.save(updated)
            return updated

    def resume(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun:
        """用户恢复 = 新一轮授权：预算轮次 +1、清原因、置 RUNNING 并立即推进一步。

        返回推进之后的真实运行（§48.1）：问题未修复时同一请求内会再次暂停，调用方必须
        看到 PAUSED 与原因码，而不是推进前的 RUNNING。推进异常与 kick 一样只记日志。
        """
        run = self._runs.resume_paused(owner_id, project_id, now=self._clock())
        try:
            after = self.advance(owner_id, project_id)
        except Exception:
            logger.exception("auto pipeline kick failed for project %s", project_id)
            return run
        return after or run

    def resume_if_paused_for(self, owner_id: UUID, project_id: UUID, *, reason_code: str) -> bool:
        """修正保存即续跑（§20）：仅当暂停原因码匹配时才恢复并踢 advance。

        不 PAUSED、无活动运行或原因码不匹配时返回 False，不抛错、不动预算轮次。
        """
        resumed = self._runs.resume_paused_for_reason(
            owner_id, project_id, reason_code=reason_code, now=self._clock()
        )
        if resumed is None:
            return False
        self.kick(owner_id, project_id)
        return True

    def pause_for_storyboard_recovery(
        self, owner_id: UUID, project_id: UUID, group_sequence: int
    ) -> AutoPipelineRun | None:
        with self._runs.lock_active(owner_id, project_id) as lock:
            run = lock.run
            if run is None or run.state is not AutoPipelineState.RUNNING:
                return run
            details: dict[str, object] = {"failure_code": "storyboard_recovery_unavailable"}
            if group_sequence > 0:
                details["group_sequences"] = [group_sequence]
            updated = self._pause(
                run,
                "storyboard_recovery_unavailable",
                phase="storyboard",
                details=details,
            )
            lock.save(updated)
            return updated

    def kick(self, owner_id: UUID, project_id: UUID) -> None:
        """终态回调处的安全踢点；失败只记日志，绝不破坏主流程。"""
        try:
            self.advance(owner_id, project_id)
        except Exception:
            logger.exception("auto pipeline kick failed for project %s", project_id)

    def _sweep_project(self, owner_id: UUID, project_id: UUID) -> None:
        try:
            self.kick(owner_id, project_id)
        finally:
            with self._sweep_guard:
                self._sweep_inflight.discard(project_id)

    def sweep_once(self) -> int:
        """重启安全网：每个 RUNNING 运行独立线程推进，单项目阻塞不饿死其他项目。

        同一项目上一轮仍未结束时跳过，避免堆积重复 advance；提交后最多等待
        `_SWEEP_WAIT_SECONDS`，超时的线程继续在后台完成（持项目锁，天然互斥）。
        """
        futures = []
        for run in self._runs.list_running():
            with self._sweep_guard:
                if run.project_id in self._sweep_inflight:
                    continue
                self._sweep_inflight.add(run.project_id)
            futures.append(
                self._sweep_pool.submit(self._sweep_project, run.owner_id, run.project_id)
            )
        if futures:
            wait(futures, timeout=_SWEEP_WAIT_SECONDS)
        return len(futures)

    # ------------------------------------------------------------------
    # 决策树（每一层最多一个外部动作；事实由端口一次性读取）
    # ------------------------------------------------------------------

    def _advance_locked(self, run: AutoPipelineRun, facts: AutoPipelineFacts) -> AutoPipelineRun:
        owner_id, project_id = run.owner_id, run.project_id

        if not facts.narration_ready:
            if facts.narration_blocking and self._narration is not None:
                remaining = self._narration.resume_auto_narration(owner_id, project_id)
                if remaining:
                    sequence = sorted(remaining)[0]
                    return self._pause(
                        run,
                        remaining[sequence],
                        phase="narration",
                        details={
                            "sequences": {str(key): remaining[key] for key in sorted(remaining)}
                        },
                    )
            # 配音在途期间，并行的导演 run 终态失败立即处理，不等配音完成。
            if not facts.director_preplan_done and facts.director_latest_status == "FAILED":
                return self._handle_director_failure(run, facts)
            return self._touch(run, phase="narration")

        # §15.7：分组后发现任何实测超长句所在组即整项目暂停（替代 §5.10 跳过规则）。
        if facts.oversized_groups:
            return self._pause(
                run,
                REASON_OVERSIZED_SENTENCE,
                phase="video",
                details={
                    "groups": [
                        {
                            "group_sequence": group.group_sequence,
                            "sentences": [
                                {"sequence": sequence, "measured_duration_ms": duration_ms}
                                for sequence, duration_ms in group.oversized_sentences
                            ],
                        }
                        for group in facts.oversized_groups
                    ]
                },
            )

        if not facts.director_preplan_done:
            if facts.director_latest_status == "FAILED":
                return self._handle_director_failure(run, facts)
            return self._touch(run, phase="director_preplan")

        if not facts.materialization_ready:
            # 物化由既有钩子（finish_job / 导演 run 终态）触发；此处幂等补踢，
            # 覆盖钩子失败或历史不完整写入的恢复（canonical 幂等，无外部 AI 调用）。
            if self._director is not None:
                self._director.materialize_if_ready(owner_id, project_id)
            return self._touch(run, phase="director_materialization")

        if not facts.storyboards_complete:
            if self._storyboards is None:
                return self._touch(run, phase="storyboard_drafts")
            overview = self._storyboards.overview(owner_id, project_id)

            def invalid_groups(
                items: tuple[StoryboardBatchGroupOverview, ...],
            ) -> list[StoryboardBatchGroupOverview]:
                return [
                    group
                    for group in items
                    if not group.has_applied
                    and (not group.has_draft or group.draft_review_state == "REQUIRED")
                ]

            # 本阶段动作均为本地确定性写入（无外部 AI）：缺失草稿播种 + 既有
            # REQUIRED 草稿修复；AUTO 按 §20 决策为歧义相邻对附加系统审批。
            acted = False
            failure_messages: dict[str, str] = {}
            if any(not group.has_draft and not group.has_applied for group in overview):
                seed_results = self._storyboards.seed_drafts(
                    owner_id, project_id, auto_approve_ambiguous=True
                )
                failure_messages.update(_error_messages(seed_results))
                overview = self._storyboards.overview(owner_id, project_id)
                acted = True
            invalid = invalid_groups(overview)
            if any(
                group.has_draft and group.draft_review_state == "REQUIRED" for group in invalid
            ):
                repair_results = self._storyboards.repair_ambiguous_drafts(owner_id, project_id)
                failure_messages.update(_error_messages(repair_results))
                if repair_results:
                    overview = self._storyboards.overview(owner_id, project_id)
                    invalid = invalid_groups(overview)
                    acted = True
            if invalid:
                details: dict[str, object] = {
                    "group_sequences": [group.group_sequence for group in invalid]
                }
                if failure_messages:
                    details["failure_messages"] = failure_messages
                return self._pause(
                    run,
                    REASON_DIRECTOR_VALIDATION_FAILED,
                    phase="storyboard_drafts",
                    details=details,
                )
            if acted:
                # 本阶段已写入草稿变更；故事板生成留待下一跳。
                return self._touch(run, phase="storyboard_drafts")

            failures = facts.storyboard_terminal_failures()
            if failures:
                return self._pause(
                    run,
                    storyboard_failed_reason(failures[0].latest_failure_code),
                    phase="storyboard",
                    details={
                        "group_sequences": [group.group_sequence for group in failures],
                        "failure_code": failures[0].latest_failure_code,
                    },
                )
            missing = [
                group.group_id for group in overview if group.storyboard_status != "SUCCEEDED"
            ]
            generatable = tuple(group.group_id for group in overview if group.can_generate)
            # 在途组由批量服务逐组跳过（already_running/unresolved），不阻塞其余缺失组。
            in_flight_ids = facts.storyboard_in_flight_group_ids()
            targets = tuple(
                group_id
                for group_id in missing
                if group_id in set(generatable) and group_id not in in_flight_ids
            )
            if targets:
                results = self._storyboards.generate(
                    owner_id, project_id, targets, force=False
                )  # 本阶段唯一动作
                needs_review = [item for item in results if item.outcome == "needs_review"]
                if needs_review:
                    return self._pause(
                        run,
                        REASON_DIRECTOR_VALIDATION_FAILED,
                        phase="storyboard",
                        details={
                            "group_sequences": [item.group_sequence for item in needs_review]
                        },
                    )
            return self._touch(run, phase="storyboard")

        if not facts.videos_complete:
            video_failures = facts.video_terminal_failures()
            if video_failures:
                return self._pause(
                    run,
                    video_failed_reason(video_failures[0].latest_error_code),
                    phase="video",
                    details={
                        "group_sequences": [
                            group.group_sequence for group in video_failures
                        ],
                        "error_code": video_failures[0].latest_error_code,
                    },
                )
            needs = facts.video_needs_submission()
            if needs:
                if self._videos is None:
                    return self._touch(run, phase="video")
                # 先进入 video 阶段（仅内存，本分支末尾统一落库），再执行可能长时间
                # 阻塞的批量派发。process_all 逐组创建意图/提示词，生产实测可达 20 分钟；
                # 若放在 _touch 之前，整段派发时长会被结算进上一阶段（2026-10-18 实锤）。
                run = self._touch(run, phase="video")
                run, job = self._resolve_video_job(run, facts)
                # C3（2026-10-21）：意图阶段失败不再一次就暂停——到期的重试先翻回
                # PENDING，再统一推进一次；失败计数与退避状态存 payload（随授权轮次重置）。
                run, retry_state = self._flip_due_intent_retries(run, job, facts)
                # 2026-10-31：容量等待项同样按退避翻回 PENDING；等待不计入 6 次预算。
                run, capacity_state = self._flip_due_capacity_waits(run, job, facts)
                self._videos.process_all(job.job_id, respect_capacity_queue=True)
                view = self._videos.get_job(run.owner_id, job.job_id) or job
                run = self._schedule_intent_retries(run, job.job_id, retry_state, view, facts)
                view = self._videos.get_job(run.owner_id, job.job_id) or job
                needed_ids = {group.group_id for group in needs}
                waiting_items = [
                    item
                    for item in view.items
                    if item.group_id in needed_ids
                    and item.status == WAITING_CAPACITY_STATUS
                ]
                if waiting_items:
                    run = self._schedule_capacity_waits(
                        run, job.job_id, capacity_state, waiting_items
                    )
                    waited_ms = to_epoch_ms(self._clock()) - int(capacity_state["since_ms"])
                    if waited_ms >= _VIDEO_CAPACITY_WAIT_LIMIT_SECONDS * 1000:
                        return self._pause(
                            run,
                            REASON_VIDEO_CAPACITY_TIMEOUT,
                            phase="video",
                            details={
                                "group_sequences": [
                                    item.sequence for item in waiting_items
                                ],
                                "waited_seconds": waited_ms // 1000,
                                "error_code": "video_capacity_unavailable",
                            },
                        )
                    # 排队等待：保持 RUNNING，不写暂停原因。
                    return run
                if run.payload.get(_VIDEO_CAPACITY_KEY) is not None:
                    run = self._touch(
                        run, phase="video", bookmarks={_VIDEO_CAPACITY_KEY: None}
                    )
                # 既定失败（不可重试 / 预算耗尽）：逐项如实落暂停原因。
                failed_items = [
                    item
                    for item in view.items
                    if item.group_id in needed_ids
                    and item.status in _VIDEO_ITEM_FAILURE_STATUSES
                ]
                if failed_items:
                    code = failed_items[0].status
                    return self._pause(
                        run,
                        video_failed_reason(
                            "intent_rejected" if code == "error" else code
                        ),
                        phase="video",
                        details={
                            "group_sequences": [item.sequence for item in failed_items],
                            "error_code": code,
                        },
                    )
                return self._touch(
                    run,
                    phase="video",
                    bookmarks={_VIDEO_JOB_KEY: str(job.job_id)},
                )
            return self._touch(run, phase="video")

        if facts.composition_current_exists:
            # §17.3：完成即收口该项目全部非终态合成 outbox 行，防止完成后再派发/重排。
            if self._compositions is not None:
                self._compositions.close_superseded(owner_id, project_id)
            return run.completed(self._clock())
        if facts.composition_failed_code is not None:
            return self._pause(
                run,
                REASON_COMPOSITION_FAILED,
                phase="composition",
                details={"error_code": facts.composition_failed_code},
            )
        if facts.composition_in_flight:
            return self._touch(run, phase="composition")
        if self._compositions is not None:
            self._compositions.start(owner_id, project_id)  # 本阶段唯一动作（幂等）
        return self._touch(run, phase="composition")

    # ------------------------------------------------------------------

    def _resume_or_start_video_job(
        self, run: AutoPipelineRun, facts: AutoPipelineFacts
    ) -> VideoBatchJobView:
        """续跑书签/既有未完成 job，否则创建新批量 job 并立即推进（每组至多一个意图）。"""
        _, job = self._resolve_video_job(run, facts)
        assert self._videos is not None
        self._videos.process_all(job.job_id)
        return job

    def _resolve_video_job(
        self, run: AutoPipelineRun, facts: AutoPipelineFacts
    ) -> tuple[AutoPipelineRun, VideoBatchJobView]:
        """只解析（书签 / 未完成 job / 新建）批量任务，不推进逐组意图。

        2026-10-31：新一轮授权（budget_round 变化）时把上一轮遗留的终态失败项翻回
        PENDING，否则旧 job 的 error 项会让每次「恢复」立刻再次暂停。
        """
        assert self._videos is not None
        owner_id, project_id = run.owner_id, run.project_id
        bookmark = run.payload.get(_VIDEO_JOB_KEY)
        job = None
        if isinstance(bookmark, str):
            try:
                job = self._videos.get_job(owner_id, UUID(bookmark))
            except ValueError:
                job = None
        if job is not None and job.completed_at is not None:
            job = None
        if job is None:
            for candidate_id in self._videos.unfinished_jobs():
                candidate = self._videos.get_job(owner_id, candidate_id)
                if candidate is not None and candidate.project_id == project_id:
                    job = candidate
                    break
        if job is None:
            job = self._videos.start(
                owner_id,
                project_id,
                tuple(group.group_id for group in facts.video_groups),
                force=False,
            )
        elif run.payload.get(_VIDEO_JOB_ROUND_KEY) != facts.budget_round:
            reopened = self._videos.reopen_failed_items(job.job_id)
            if reopened:
                logger.info(
                    "video round %s reopened %s failed items of job %s",
                    facts.budget_round,
                    reopened,
                    job.job_id,
                )
            run = run.touched(
                self._clock(),
                phase="video",
                bookmarks={
                    _VIDEO_JOB_KEY: str(job.job_id),
                    _VIDEO_JOB_ROUND_KEY: facts.budget_round,
                },
            )
            job = self._videos.get_job(owner_id, job.job_id) or job
        return run, job

    # ------------------------------------------------------------------
    # C3：意图阶段失败纳入每组 6 次提交预算（计数与退避状态存 payload，
    # 随 auto_budget_round 失效，不新增列/表）。

    def _intent_retry_state(
        self, run: AutoPipelineRun, facts: AutoPipelineFacts, job_id: UUID
    ) -> dict[str, object]:
        raw = run.payload.get(_VIDEO_INTENT_RETRY_KEY)
        fresh = {
            "round": facts.budget_round,
            "job": str(job_id),
            "failures": {},
            "next_ms": {},
        }
        if not isinstance(raw, dict):
            return fresh
        if raw.get("round") != facts.budget_round or raw.get("job") != str(job_id):
            # 新一轮授权或换了批次 job：预算与退避状态一并清零，避免翻错宿主。
            return fresh
        failures = raw.get("failures")
        next_ms = raw.get("next_ms")
        return {
            "round": facts.budget_round,
            "job": str(job_id),
            "failures": dict(failures) if isinstance(failures, dict) else {},
            "next_ms": dict(next_ms) if isinstance(next_ms, dict) else {},
        }

    def _flip_due_intent_retries(
        self, run: AutoPipelineRun, job: VideoBatchJobView, facts: AutoPipelineFacts
    ) -> tuple[AutoPipelineRun, dict[str, object]]:
        """把到期的 RETRY_WAIT 项翻回 PENDING；返回更新后的 run 与重试状态。"""
        assert self._videos is not None
        state = self._intent_retry_state(run, facts, job.job_id)
        next_ms = cast(dict[str, int], state["next_ms"])
        now_ms = to_epoch_ms(self._clock())
        due = [key for key, at in next_ms.items() if at <= now_ms]
        if not due:
            return run, state
        for key in due:
            next_ms.pop(key, None)
            self._videos.mark_item(job.job_id, UUID(key), PENDING_STATUS)
        updated = run.touched(
            self._clock(),
            phase="video",
            bookmarks={_VIDEO_INTENT_RETRY_KEY: state},
        )
        return updated, state

    def _schedule_intent_retries(
        self,
        run: AutoPipelineRun,
        job_id: UUID,
        state: dict[str, object],
        view: VideoBatchJobView,
        facts: AutoPipelineFacts,
    ) -> AutoPipelineRun:
        """首次观察到 RETRY_WAIT 时计数：预算内排下一次，耗尽则落稳定终态。"""
        assert self._videos is not None
        failures = cast(dict[str, int], state["failures"])
        next_ms = cast(dict[str, int], state["next_ms"])
        submissions = {group.group_id: group.submissions for group in facts.video_groups}
        changed = False
        now_ms = to_epoch_ms(self._clock())
        for item in view.items:
            if item.status != RETRY_WAIT_STATUS:
                continue
            key = str(item.group_id)
            if key in next_ms:
                continue
            count = int(failures.get(key, 0)) + 1
            failures[key] = count
            changed = True
            if submissions.get(item.group_id, 0) + count >= _VIDEO_INTENT_BUDGET:
                self._videos.mark_item(
                    job_id,
                    item.group_id,
                    SUBMISSION_BUDGET_EXHAUSTED_STATUS,
                    message=item.message,
                )
                continue
            next_ms[key] = now_ms + int(
                _video_intent_retry_delay_seconds(count) * 1000
            )
        if not changed:
            return run
        return run.touched(
            self._clock(),
            phase="video",
            bookmarks={_VIDEO_INTENT_RETRY_KEY: state},
        )

    def _capacity_wait_state(
        self, run: AutoPipelineRun, facts: AutoPipelineFacts, job_id: UUID
    ) -> dict[str, object]:
        """读取/初始化容量等待状态（按授权轮次与 job 失效）。"""
        raw = run.payload.get(_VIDEO_CAPACITY_KEY)
        now_ms = to_epoch_ms(self._clock())
        fresh: dict[str, object] = {
            "round": facts.budget_round,
            "job": str(job_id),
            "since_ms": now_ms,
            "attempts": {},
            "next_ms": {},
        }
        if not isinstance(raw, dict):
            return fresh
        if raw.get("round") != facts.budget_round or raw.get("job") != str(job_id):
            return fresh
        attempts = raw.get("attempts")
        next_ms = raw.get("next_ms")
        since_ms = raw.get("since_ms")
        fresh["since_ms"] = since_ms if isinstance(since_ms, int) else now_ms
        fresh["attempts"] = dict(attempts) if isinstance(attempts, dict) else {}
        fresh["next_ms"] = dict(next_ms) if isinstance(next_ms, dict) else {}
        return fresh

    def _flip_due_capacity_waits(
        self, run: AutoPipelineRun, job: VideoBatchJobView, facts: AutoPipelineFacts
    ) -> tuple[AutoPipelineRun, dict[str, object]]:
        """把到期的 WAITING_CAPACITY 翻回 PENDING；返回更新后的 run 与等待状态。"""
        assert self._videos is not None
        state = self._capacity_wait_state(run, facts, job.job_id)
        next_ms = cast(dict[str, int], state["next_ms"])
        now_ms = to_epoch_ms(self._clock())
        due = [key for key, at in next_ms.items() if int(at) <= now_ms]
        if not due:
            return run, state
        for key in due:
            next_ms.pop(key, None)
            self._videos.mark_item(job.job_id, UUID(key), PENDING_STATUS)
        updated = run.touched(
            self._clock(), phase="video", bookmarks={_VIDEO_CAPACITY_KEY: state}
        )
        return updated, state

    def _schedule_capacity_waits(
        self,
        run: AutoPipelineRun,
        job_id: UUID,
        state: dict[str, object],
        waiting_items: list[VideoBatchItemView],
    ) -> AutoPipelineRun:
        """为每个等待项排下一次尝试（20/40/60s）；不计入 6 次提交预算。"""
        attempts = cast(dict[str, int], state["attempts"])
        next_ms = cast(dict[str, int], state["next_ms"])
        now_ms = to_epoch_ms(self._clock())
        for item in waiting_items:
            key = str(item.group_id)
            if key in next_ms:
                continue
            count = int(attempts.get(key, 0)) + 1
            attempts[key] = count
            next_ms[key] = now_ms + int(_video_capacity_retry_delay_seconds(count) * 1000)
        touched = run.touched(
            self._clock(),
            phase="video",
            bookmarks={_VIDEO_CAPACITY_KEY: state, _VIDEO_JOB_KEY: str(job_id)},
        )
        return touched

    def _handle_director_failure(
        self, run: AutoPipelineRun, facts: AutoPipelineFacts
    ) -> AutoPipelineRun:
        handled = run.payload.get(_DIRECTOR_HANDLED_KEY)
        if handled is not None and handled == str(facts.director_latest_run_id):
            # 该失败 run 已暂停过且用户已恢复：经前缀复用机制创建新一轮 run。
            if self._director is not None:
                try:
                    self._director.create_recovery_run(
                        run.owner_id, run.project_id, round=run.authorization_round
                    )
                except DomainError as error:
                    # 恢复 run 被拒绝（如快照完整性 fail-closed）：稳定暂停，不热循环。
                    return self._pause(
                        run,
                        REASON_DIRECTOR_PREPLAN_FAILED,
                        phase="director_preplan",
                        details={
                            _DIRECTOR_HANDLED_KEY: str(facts.director_latest_run_id),
                            "failure_code": error.code,
                        },
                    )
            return self._touch(run, phase="director_preplan")
        return self._pause(
            run,
            REASON_DIRECTOR_PREPLAN_FAILED,
            phase="director_preplan",
            details={
                _DIRECTOR_HANDLED_KEY: str(facts.director_latest_run_id),
                "failure_code": facts.director_latest_failure_code,
            },
        )

    def _pause(
        self,
        run: AutoPipelineRun,
        reason_code: str,
        *,
        phase: str,
        details: dict[str, object],
    ) -> AutoPipelineRun:
        paused = run.paused(reason_code, self._clock(), phase=phase, details=details)
        logger.warning(
            "auto pipeline paused: project=%s reason=%s phase=%s",
            run.project_id,
            reason_code,
            phase,
        )
        return paused

    def _touch(
        self,
        run: AutoPipelineRun,
        *,
        phase: str,
        bookmarks: dict[str, object] | None = None,
    ) -> AutoPipelineRun:
        touched = run.touched(self._clock(), phase=phase, bookmarks=bookmarks)
        # 事实无变化时不落库（巡检每 20s 一次，避免空写）。
        if touched.payload == run.payload:
            return run
        return touched
