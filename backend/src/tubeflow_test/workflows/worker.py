from __future__ import annotations

import asyncio
import inspect
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from tubeflow_test.adapters.persistence.media_repository import production_active_job_gates
from tubeflow_test.adapters.persistence.narration import SqlStoryboardNarrationAuthority
from tubeflow_test.adapters.persistence.production_authority import (
    SqlCanonicalCompositionInputs,
    canonical_video_authorities,
    single_panel_policy_settings,
)
from tubeflow_test.adapters.persistence.shot_panel_assets import ShotPanelAssetService
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
)
from tubeflow_test.adapters.persistence.video_prompt_diagnostics import (
    SqlVideoPromptDiagnostics,
)
from tubeflow_test.api.cover_runtime import create_cover_supervised_runner
from tubeflow_test.api.media_runtime import MediaRuntime, create_media_runtime
from tubeflow_test.api.narration_runtime import (
    NarrationRuntime,
    SqlProjectWriteGuard,
    create_narration_runtime,
)
from tubeflow_test.api.storyboard_runtime import create_storyboard_supervised_runner
from tubeflow_test.application.composition import (
    COMPOSITION_AUTO_MAX_ATTEMPTS,
    COMPOSITION_AUTO_RETRY_BACKOFF_SECONDS,
)
from tubeflow_test.application.composition_outbox import (
    CompositionOutboxRelay,
)
from tubeflow_test.application.purge_outbox import PurgeOutboxRelay
from tubeflow_test.application.storyboards import StoryboardRecoveryCandidate
from tubeflow_test.application.video_batch import GroupVideoBatchService
from tubeflow_test.auto_pipeline.factory import build_auto_pipeline_service
from tubeflow_test.auto_pipeline.service import AutoPipelineService
from tubeflow_test.config import Settings
from tubeflow_test.identity_project.runtime import create_identity_project_runtime
from tubeflow_test.production_lifecycle import raise_lifecycle_errors
from tubeflow_test.video_generation.adapters import build_h3_archive_adapter
from tubeflow_test.video_generation.node_health import refresh_h3_node_health
from tubeflow_test.video_generation.persistence import (
    ManualH3IntentWriter,
    SqlVideoBatchJobRepository,
    SqlVideoBatchRepository,
)
from tubeflow_test.video_generation.runtime import build_production_video_runtime
from tubeflow_test.video_generation.worker import (
    create_video_worker,
    dispatch_video_outbox_once,
    redrive_stale_video_outbox,
)
from tubeflow_test.video_prompt.compiler import build_video_prompt_compiler
from tubeflow_test.workflows.composition import (
    ManualCompositionWorkflow,
    TemporalCompositionDispatcher,
    create_composition_activity,
)
from tubeflow_test.workflows.narration import NarrationActivities, NarrationWorkflow
from tubeflow_test.workflows.project_purge import (
    PURGE_TASK_QUEUE,
    ProjectPurgeWorkflow,
    TemporalPurgeDispatcher,
    create_purge_activity,
)
from tubeflow_test.workflows.supervision import SupervisedWorkerGroup, WorkerComponent

logger = logging.getLogger(__name__)


BASE_COMPONENT_NAMES = (
    "narration-temporal-worker",
    "narration-outbox-relay",
    "storyboard-temporal-worker",
    "storyboard-outbox-relay",
    "cover-temporal-worker",
    "cover-outbox-relay",
    "purge-composition-temporal-worker",
    "purge-outbox-relay",
    "composition-outbox-relay",
    "auto-pipeline-sweeper",
)
H3_COMPONENT_NAMES = (
    "h3-temporal-worker",
    "h3-outbox-relay",
    "h3-node-health",
    "h3-slot-lease-sweeper",
)


def _supervised_component(
    name: str,
    target: Callable[[], Awaitable[None]],
    ready: dict[str, bool],
    started: Callable[[], bool],
    preflight: Callable[[], Awaitable[object]] | None = None,
) -> WorkerComponent:
    async def run_component() -> None:
        primary: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        if preflight is not None:
            await preflight()
        target_task: asyncio.Future[None] = asyncio.ensure_future(target())
        try:
            while not started():
                if target_task.done():
                    await target_task
                    raise RuntimeError(f"{name} exited during startup")
                await asyncio.sleep(0)
            if target_task.done():
                await target_task
                raise RuntimeError(f"{name} exited during startup")
            ready[name] = True
            await target_task
        except BaseException as error:
            primary = error
        finally:
            ready[name] = False
            if not target_task.done():
                target_task.cancel()
                result = (await asyncio.gather(target_task, return_exceptions=True))[0]
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    cleanup_errors.append(result)
        raise_lifecycle_errors(f"{name} lifecycle failed", primary, cleanup_errors)

    return WorkerComponent(name, run_component, lambda: ready[name])


@dataclass(slots=True)
class ProductionWorkerAssembly:
    components: tuple[WorkerComponent, ...]
    narration: NarrationRuntime
    closers: tuple[Callable[[], None], ...] = ()

    async def close(self) -> None:
        errors: list[BaseException] = []
        try:
            await self.narration.http_client.aclose()
        except BaseException as error:
            errors.append(error)
        for close_resource in reversed(self.closers):
            try:
                close_resource()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("production worker shutdown failed", errors)


def _media_runtime(settings: Settings, narration: NarrationRuntime) -> MediaRuntime:
    sessions = sessionmaker(narration.database.engine, expire_on_commit=False)
    return create_media_runtime(
        settings,
        narration.current_media_owner,
        SqlCanonicalCompositionInputs(sessions),
        production_active_job_gates(),
        private_root=Path("var/media"),
        database=narration.database,
        storage=narration.storage,
    )


async def build_production_worker_assembly(
    settings: Settings, *, temporal_client: Client | None = None
) -> ProductionWorkerAssembly:
    activity_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tubeflow-activity")
    rollback: list[Callable[[], object]] = [activity_executor.shutdown]
    try:
        assembly = await _build_production_worker_assembly_with_executor(
            settings,
            temporal_client=temporal_client,
            activity_executor=activity_executor,
            rollback=rollback,
        )
        rollback.clear()
        return assembly
    except BaseException as primary:
        errors: list[BaseException] = []
        for close_resource in reversed(rollback):
            try:
                result = close_resource()
                if inspect.isawaitable(result):
                    await result
            except BaseException as error:
                errors.append(error)
        raise_lifecycle_errors("production worker assembly failed", primary, errors)
        raise AssertionError("unreachable") from primary


async def _build_production_worker_assembly_with_executor(
    settings: Settings,
    *,
    temporal_client: Client | None,
    activity_executor: ThreadPoolExecutor,
    rollback: list[Callable[[], object]],
) -> ProductionWorkerAssembly:
    client = temporal_client or await Client.connect(settings.temporal_target)
    identity = create_identity_project_runtime(settings)
    rollback.append(identity.database.engine.dispose)
    identity.ensure_private_bucket()
    with identity.database.session() as session:
        session.execute(text("SELECT 1"))
    narration = create_narration_runtime(
        settings,
        temporal_client=client,
        database=identity.database,
        storage=identity.storage,
        tokens=identity.tokens,
    )
    rollback.append(narration.http_client.aclose)
    # §15.9：AUTO 编排器的踢点晚绑定（装配完成后注入服务实例），踢点失败仅记日志。
    auto_pipeline_holder: dict[str, AutoPipelineService] = {}

    def auto_pipeline_kick(owner_id: UUID, project_id: UUID) -> None:
        pipeline = auto_pipeline_holder.get("service")
        if pipeline is not None:
            pipeline.kick(owner_id, project_id)

    def auto_pipeline_recovery_inconclusive(candidate: StoryboardRecoveryCandidate) -> None:
        pipeline = auto_pipeline_holder.get("service")
        if pipeline is not None:
            pipeline.pause_for_storyboard_recovery(
                candidate.generation.owner_id,
                candidate.generation.project_id,
                candidate.group_sequence,
            )

    authority = SqlStoryboardNarrationAuthority(identity.database.session, identity.storage)
    storyboard = await create_storyboard_supervised_runner(
        settings,
        work_dir=Path("var/storyboards"),
        narration_authority=authority,
        database=identity.database,
        temporal_client=client,
        minio_client=identity.storage.client,
        activity_executor=activity_executor,
        auto_kick=auto_pipeline_kick,
        recovery_inconclusive=auto_pipeline_recovery_inconclusive,
    )
    rollback.append(storyboard.close)
    cover = await create_cover_supervised_runner(
        settings,
        work_dir=Path("var/covers"),
        database=identity.database,
        temporal_client=client,
        minio_client=identity.storage.client,
        activity_executor=activity_executor,
    )
    rollback.append(cover.close)
    media = _media_runtime(settings, narration)
    activities = NarrationActivities(narration.service, auto_kick=auto_pipeline_kick)
    narration_worker = Worker(
        client,
        task_queue=settings.narration_task_queue,
        workflows=[NarrationWorkflow],
        activities=[
            activities.synthesize_batch,
            activities.finalize_timeline,
            activities.finish_job,
        ],
    )
    media_worker = Worker(
        client,
        task_queue=PURGE_TASK_QUEUE,
        workflows=[ProjectPurgeWorkflow, ManualCompositionWorkflow],
        activities=[
            create_purge_activity(media.deletions),
            create_composition_activity(media.compositions, auto_kick=auto_pipeline_kick),
        ],
        activity_executor=activity_executor,
    )
    purge_relay = PurgeOutboxRelay(media.outbox, TemporalPurgeDispatcher(client))
    composition_relay = CompositionOutboxRelay(
        media.composition_outbox, TemporalCompositionDispatcher(client)
    )

    async def relay_forever(
        relay: PurgeOutboxRelay | CompositionOutboxRelay, started: asyncio.Event
    ) -> None:
        reconcile_at = 0.0
        while True:
            while await relay.relay_one():
                pass
            started.set()
            if isinstance(relay, CompositionOutboxRelay):
                now = asyncio.get_running_loop().time()
                if now >= reconcile_at:
                    reconcile_at = now + 60.0
                    # 失联工作流对账（容器崩溃心跳超时死亡）：AUTO 自动重试排程。
                    try:
                        media.composition_outbox.reconcile_lost_dispatches(
                            older_than=datetime.now(UTC)
                            - timedelta(seconds=settings.composition_stale_reconcile_seconds),
                            max_attempts=COMPOSITION_AUTO_MAX_ATTEMPTS,
                            backoff_seconds=COMPOSITION_AUTO_RETRY_BACKOFF_SECONDS,
                        )
                    except Exception:
                        logger.exception("composition lost-dispatch reconcile failed")
            await asyncio.sleep(1)

    storyboard_relay_started = asyncio.Event()

    async def storyboard_relay() -> None:
        while True:
            dispatched = await storyboard.dispatcher.dispatch_one()
            storyboard_relay_started.set()
            if not dispatched:
                await asyncio.sleep(0.5)

    cover_relay_started = asyncio.Event()

    async def cover_relay() -> None:
        while True:
            dispatched = await cover.dispatcher.dispatch_one()
            cover_relay_started.set()
            if not dispatched:
                await asyncio.sleep(0.5)

    narration_relay_started = asyncio.Event()

    async def narration_relay() -> None:
        while True:
            await narration.service.dispatch_pending_jobs()
            narration_relay_started.set()
            await asyncio.sleep(2)

    purge_relay_started = asyncio.Event()
    composition_relay_started = asyncio.Event()

    component_names = BASE_COMPONENT_NAMES + (H3_COMPONENT_NAMES if settings.h3_configured else ())
    ready: dict[str, bool] = {name: False for name in component_names}

    async def database_preflight() -> None:
        def probe() -> None:
            with identity.database.session() as session:
                session.execute(text("SELECT 1"))

        await asyncio.to_thread(probe)

    async def temporal_preflight() -> None:
        if not await client.service_client.check_health():
            raise RuntimeError("Temporal workflow service is unhealthy")
        await database_preflight()

    def supervised(
        name: str,
        target: Callable[[], Awaitable[None]],
        preflight: Callable[[], Awaitable[object]] | None = None,
        runtime_started: Callable[[], bool] | None = None,
    ) -> WorkerComponent:
        if runtime_started is None:
            raise ValueError(f"{name} requires an explicit started signal")

        async def checked_preflight() -> None:
            if preflight is not None:
                await preflight()

        return _supervised_component(
            name,
            target,
            ready,
            runtime_started,
            checked_preflight,
        )

    components: tuple[WorkerComponent, ...] = (
        supervised(
            "narration-temporal-worker",
            narration_worker.run,
            temporal_preflight,
            lambda: narration_worker.is_running,
        ),
        supervised(
            "narration-outbox-relay",
            narration_relay,
            narration.service.dispatch_pending_jobs,
            narration_relay_started.is_set,
        ),
        supervised(
            "storyboard-temporal-worker",
            storyboard.worker.run,
            temporal_preflight,
            lambda: storyboard.worker.is_running,
        ),
        supervised(
            "storyboard-outbox-relay",
            storyboard_relay,
            storyboard.dispatcher.dispatch_one,
            storyboard_relay_started.is_set,
        ),
        supervised(
            "cover-temporal-worker",
            cover.worker.run,
            temporal_preflight,
            lambda: cover.worker.is_running,
        ),
        supervised(
            "cover-outbox-relay",
            cover_relay,
            cover.dispatcher.dispatch_one,
            cover_relay_started.is_set,
        ),
        supervised(
            "purge-composition-temporal-worker",
            media_worker.run,
            temporal_preflight,
            lambda: media_worker.is_running,
        ),
        supervised(
            "purge-outbox-relay",
            lambda: relay_forever(purge_relay, purge_relay_started),
            purge_relay.relay_one,
            purge_relay_started.is_set,
        ),
        supervised(
            "composition-outbox-relay",
            lambda: relay_forever(composition_relay, composition_relay_started),
            composition_relay.relay_one,
            composition_relay_started.is_set,
        ),
    )
    closers: list[Callable[[], None]] = [
        activity_executor.shutdown,
        identity.database.engine.dispose,
        storyboard.close,
        cover.close,
    ]
    video_batch_service: GroupVideoBatchService | None = None
    if settings.h3_configured:
        video_sessions = sessionmaker(identity.database.engine, expire_on_commit=False)
        video_asset_store = build_h3_archive_adapter(video_sessions, identity.storage)
        requested_policy, policy_allowed = single_panel_policy_settings(settings)
        narration_video_authority, storyboard_video_authority, _loader = (
            canonical_video_authorities(
                settings,
                reference_policy=requested_policy,
                single_panel_allowed=policy_allowed,
                panel_derivatives=ShotPanelAssetService(
                    video_sessions, identity.storage, video_asset_store
                ),
            )
        )
        feature = build_production_video_runtime(
            settings,
            narration_authority=narration_video_authority,
            storyboard_authority=storyboard_video_authority,
            authenticated_owner=narration.current_owner_id,
            write_guard=SqlProjectWriteGuard(identity.database.session).assert_writable,
            database_engine=identity.database.engine,
            minio=identity.storage.client,
            asset_store=video_asset_store,
            refresh_health=False,
            reference_policy_authority=_loader,
        )
        rollback.append(feature.close)
        closers.append(feature.close)
        # AUTO 编排器的视频批量端口：与 API 侧同一批量服务（持久化 job 可续跑）。
        video_prompt_compiler = build_video_prompt_compiler(
            settings,
            SqlVideoPromptDiagnostics(video_sessions).record,
        )
        if video_prompt_compiler is not None:
            closers.append(video_prompt_compiler.close)
        video_batch_service = GroupVideoBatchService(
            SqlVideoBatchRepository(video_sessions),
            ManualH3IntentWriter(
                identity.database.unit_of_work,
                SqlAlchemyStoryboardRepository(identity.database.unit_of_work, authority),
                _loader,
                prompt_compiler=video_prompt_compiler,
                intent_group_loader=_loader,
            ),
            uuid4,
            SqlVideoBatchJobRepository(video_sessions),
        )
        token_by_ref = {
            f"configured-h3-node:{node_id}": token
            for pool in settings.h3_node_pools
            for node_id, token in zip(pool.node_ids, pool.node_tokens, strict=True)
        }

        async def h3_health() -> None:
            while True:
                await asyncio.to_thread(
                    refresh_h3_node_health,
                    video_sessions,
                    feature.engine,
                    token_by_ref.__getitem__,
                )
                h3_health_started.set()
                await asyncio.sleep(10)

        async def h3_sweeper() -> None:
            while True:
                await asyncio.to_thread(feature.service.sweep_expired_slots)
                h3_sweeper_started.set()
                await asyncio.sleep(10)

        h3_health_started = asyncio.Event()
        h3_sweeper_started = asyncio.Event()
        h3_relay_started = asyncio.Event()

        async def h3_relay() -> None:
            while True:
                await asyncio.to_thread(redrive_stale_video_outbox, video_sessions)
                dispatched = await dispatch_video_outbox_once(
                    client,
                    video_sessions,
                    task_queue=settings.h3_task_queue,
                    stalled_poll_seconds=settings.h3_stalled_poll_seconds,
                )
                h3_relay_started.set()
                if not dispatched:
                    await asyncio.sleep(0.5)

        async def h3_relay_preflight() -> None:
            await asyncio.to_thread(redrive_stale_video_outbox, video_sessions)
            await dispatch_video_outbox_once(
                client,
                video_sessions,
                task_queue=settings.h3_task_queue,
                stalled_poll_seconds=settings.h3_stalled_poll_seconds,
            )

        async def h3_worker_preflight() -> None:
            if not await client.service_client.check_health():
                raise RuntimeError("Temporal workflow service is unhealthy")
            await asyncio.to_thread(
                refresh_h3_node_health,
                video_sessions,
                feature.engine,
                token_by_ref.__getitem__,
            )

        h3_worker = create_video_worker(
            client, feature.service, task_queue=settings.h3_task_queue, auto_kick=auto_pipeline_kick
        )
        components += (
            supervised(
                "h3-temporal-worker",
                h3_worker.run,
                h3_worker_preflight,
                lambda: h3_worker.is_running,
            ),
            supervised(
                "h3-outbox-relay",
                h3_relay,
                h3_relay_preflight,
                h3_relay_started.is_set,
            ),
            supervised(
                "h3-node-health",
                h3_health,
                lambda: asyncio.to_thread(
                    refresh_h3_node_health,
                    video_sessions,
                    feature.engine,
                    token_by_ref.__getitem__,
                ),
                h3_health_started.is_set,
            ),
            supervised(
                "h3-slot-lease-sweeper",
                h3_sweeper,
                lambda: asyncio.to_thread(feature.service.sweep_expired_slots),
                h3_sweeper_started.is_set,
            ),
        )
    auto_pipeline_holder["service"] = build_auto_pipeline_service(
        database=identity.database,
        narration_service=narration.service,
        project_director=narration.project_director,
        storyboards=storyboard.batch,
        videos=video_batch_service,
        composition_repository=media.composition_repository,
        composition_outbox=media.composition_outbox,
    )
    auto_sweep_started = asyncio.Event()

    async def auto_pipeline_sweep() -> None:
        # 重启安全网：扫描活跃 RUNNING 运行逐条 advance（kick 内部吞错记日志）。
        while True:
            await asyncio.to_thread(auto_pipeline_holder["service"].sweep_once)
            auto_sweep_started.set()
            await asyncio.sleep(settings.auto_pipeline_sweep_interval_seconds)

    components += (
        supervised(
            "auto-pipeline-sweeper",
            auto_pipeline_sweep,
            lambda: asyncio.to_thread(auto_pipeline_holder["service"].sweep_once),
            auto_sweep_started.is_set,
        ),
    )
    return ProductionWorkerAssembly(components, narration, tuple(closers))


async def run_worker() -> None:
    settings = Settings()
    ready_path = Path(settings.worker_ready_file)
    ready_path.unlink(missing_ok=True)
    assembly: ProductionWorkerAssembly | None = None
    readiness: asyncio.Task[None] | None = None
    workers: asyncio.Task[None] | None = None
    primary: BaseException | None = None
    cleanup_errors: list[BaseException] = []

    async def publish_readiness() -> None:
        assert assembly is not None
        while not all(component.ready() for component in assembly.components):
            await asyncio.sleep(0.05)
        _publish_ready_file(ready_path)
        await asyncio.Event().wait()

    try:
        assembly = await build_production_worker_assembly(settings)
        readiness = asyncio.create_task(publish_readiness(), name="worker-ready-file")
        workers = asyncio.create_task(
            SupervisedWorkerGroup(assembly.components).run(), name="worker-supervision"
        )
        done, _pending = await asyncio.wait(
            (workers, readiness), return_when=asyncio.FIRST_COMPLETED
        )
        done_errors: list[BaseException] = []
        for task in done:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                done_errors.append(error)
        if len(done_errors) == 1:
            raise done_errors[0]
        if done_errors:
            raise BaseExceptionGroup("production worker tasks failed", done_errors)
    except BaseException as error:
        primary = error
    finally:
        running_tasks = tuple(task for task in (workers, readiness) if task is not None)
        pending_tasks = tuple(task for task in running_tasks if not task.done())
        for task in pending_tasks:
            task.cancel()
        pending_results = await asyncio.gather(*pending_tasks, return_exceptions=True)
        for result in pending_results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                cleanup_errors.append(result)
        for task in running_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running_tasks, return_exceptions=True)
        try:
            ready_path.unlink(missing_ok=True)
        except BaseException as error:
            cleanup_errors.append(error)
        if assembly is not None:
            try:
                await assembly.close()
            except BaseException as error:
                cleanup_errors.append(error)
    raise_lifecycle_errors("production worker lifecycle failed", primary, cleanup_errors)


def _publish_ready_file(ready_path: Path) -> None:
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{ready_path.name}.", dir=ready_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write("ready\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ready_path)
    finally:
        temporary.unlink(missing_ok=True)


def run() -> None:
    asyncio.run(run_worker())
