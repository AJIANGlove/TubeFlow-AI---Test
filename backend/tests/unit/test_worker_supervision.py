from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tubeflow_test.workflows import supervision as supervision_module
from tubeflow_test.workflows import worker as worker_module
from tubeflow_test.workflows.supervision import SupervisedWorkerGroup, WorkerComponent
from tubeflow_test.workflows.worker import (
    BASE_COMPONENT_NAMES,
    H3_COMPONENT_NAMES,
    ProductionWorkerAssembly,
    _supervised_component,
)


def test_production_worker_manifest_lists_every_required_worker_and_relay() -> None:
    assert BASE_COMPONENT_NAMES == (
        "narration-temporal-worker",
        "narration-outbox-relay",
        "storyboard-temporal-worker",
        "storyboard-outbox-relay",
        "cover-temporal-worker",
        "cover-outbox-relay",
        "purge-composition-temporal-worker",
        "purge-outbox-relay",
        "composition-outbox-relay",
        # §15.9：AUTO 编排器巡检循环（重启安全网）。
        "auto-pipeline-sweeper",
    )
    assert H3_COMPONENT_NAMES == (
        "h3-temporal-worker",
        "h3-outbox-relay",
        "h3-node-health",
        "h3-slot-lease-sweeper",
    )


@pytest.mark.asyncio
async def test_group_becomes_ready_only_after_every_component_is_ready() -> None:
    stop = asyncio.Event()
    ready = {"worker": False, "relay": False}

    async def run_forever() -> None:
        await stop.wait()

    group = SupervisedWorkerGroup(
        [
            WorkerComponent("worker", run_forever, lambda: ready["worker"]),
            WorkerComponent("relay", run_forever, lambda: ready["relay"]),
        ],
        readiness_poll_seconds=0.001,
    )
    task = asyncio.create_task(group.run())
    await asyncio.sleep(0.01)
    assert not group.state.healthy
    ready["worker"] = True
    await asyncio.sleep(0.01)
    assert not group.state.healthy
    ready["relay"] = True
    await asyncio.sleep(0.01)
    assert group.state.healthy
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not group.state.healthy


@pytest.mark.asyncio
async def test_normal_component_exit_fails_group_and_cancels_sibling() -> None:
    sibling_cancelled = asyncio.Event()

    async def exits() -> None:
        return

    async def sibling() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    group = SupervisedWorkerGroup(
        [WorkerComponent("exits", exits), WorkerComponent("sibling", sibling)]
    )
    with pytest.raises(RuntimeError, match="exited unexpectedly: exits"):
        await group.run()
    assert sibling_cancelled.is_set()
    assert not group.state.healthy


@pytest.mark.asyncio
async def test_component_error_is_visible_and_propagated() -> None:
    expected = OSError("database unavailable")

    async def fails() -> None:
        raise expected

    group = SupervisedWorkerGroup([WorkerComponent("relay", fails)])
    with pytest.raises(OSError, match="database unavailable"):
        await group.run()
    assert group.state.last_error is expected
    assert not group.state.healthy


@pytest.mark.asyncio
async def test_group_aggregates_two_components_that_fail_together() -> None:
    release = asyncio.Event()

    async def first() -> None:
        await release.wait()
        raise OSError("first failed")

    async def second() -> None:
        await release.wait()
        raise RuntimeError("second failed")

    group = SupervisedWorkerGroup(
        [WorkerComponent("first", first), WorkerComponent("second", second)]
    )
    task = asyncio.create_task(group.run())
    release.set()
    with pytest.raises(BaseExceptionGroup) as raised:
        await task
    rendered = repr(raised.value.exceptions)
    assert "first failed" in rendered
    assert "second failed" in rendered


@pytest.mark.asyncio
async def test_group_preserves_failure_raised_while_cancelling_pending_component() -> None:
    sibling_started = asyncio.Event()

    async def primary() -> None:
        await sibling_started.wait()
        raise OSError("primary failed")

    async def sibling() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            raise RuntimeError("cancel cleanup failed") from error

    group = SupervisedWorkerGroup(
        [WorkerComponent("primary", primary), WorkerComponent("sibling", sibling)]
    )
    with pytest.raises(BaseExceptionGroup) as raised:
        await group.run()
    rendered = repr(raised.value.exceptions)
    assert "primary failed" in rendered
    assert "cancel cleanup failed" in rendered


@pytest.mark.asyncio
async def test_group_drains_cleanup_results_after_repeated_external_cancellation() -> None:
    sibling_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def primary() -> None:
        await sibling_started.wait()
        raise OSError("primary failed")

    async def sibling() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cleanup_started.set()
            await release_cleanup.wait()
            raise RuntimeError("late cleanup failed") from error

    group = SupervisedWorkerGroup(
        [WorkerComponent("primary", primary), WorkerComponent("sibling", sibling)]
    )
    task = asyncio.create_task(group.run())
    await cleanup_started.wait()
    task.cancel()
    task.cancel()
    release_cleanup.set()
    with pytest.raises(BaseExceptionGroup) as raised:
        await task
    rendered = repr(raised.value.exceptions)
    assert "primary failed" in rendered
    assert "late cleanup failed" in rendered


@pytest.mark.asyncio
async def test_pending_normal_exit_in_wait_cancel_race_is_unexpected(monkeypatch) -> None:
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    real_wait = supervision_module.asyncio.wait

    async def delayed_wait(tasks, *, return_when):
        done, pending = await real_wait(tasks, return_when=return_when)
        second_release.set()
        await asyncio.sleep(0)
        return done, pending

    async def first() -> None:
        await first_release.wait()

    async def second() -> None:
        await second_release.wait()

    monkeypatch.setattr(supervision_module.asyncio, "wait", delayed_wait)
    group = SupervisedWorkerGroup(
        [WorkerComponent("first", first), WorkerComponent("second", second)]
    )
    task = asyncio.create_task(group.run())
    first_release.set()
    with pytest.raises(BaseExceptionGroup) as raised:
        await task
    rendered = repr(raised.value.exceptions)
    assert "exited unexpectedly: first" in rendered
    assert "exited unexpectedly: second" in rendered


@pytest.mark.asyncio
async def test_failed_preflight_never_marks_component_ready() -> None:
    ready = {"worker": False}

    async def preflight() -> None:
        raise OSError("database unavailable")

    async def target() -> None:
        raise AssertionError("target must not start")

    component = _supervised_component("worker", target, ready, lambda: False, preflight)
    with pytest.raises(OSError, match="database unavailable"):
        await component.run()
    assert not component.ready()


@pytest.mark.asyncio
async def test_target_immediate_failure_never_marks_component_ready() -> None:
    ready = {"worker": False}

    async def target() -> None:
        raise OSError("poll startup failed")

    component = _supervised_component("worker", target, ready, lambda: False)
    with pytest.raises(OSError, match="poll startup failed"):
        await component.run()
    assert not component.ready()


@pytest.mark.asyncio
async def test_started_signal_then_immediate_failure_never_marks_ready() -> None:
    ready = {"relay": False}
    started = asyncio.Event()

    async def preflight() -> None:
        return None

    async def target() -> None:
        started.set()
        raise OSError("relay loop failed")

    component = _supervised_component(
        "relay", target, ready, started.is_set, preflight
    )
    with pytest.raises(OSError, match="relay loop failed"):
        await component.run()
    assert not component.ready()


@pytest.mark.asyncio
async def test_pending_preflight_never_marks_component_ready() -> None:
    ready = {"worker": False}
    blocked = asyncio.Event()

    async def preflight() -> None:
        await blocked.wait()

    async def target() -> None:
        await asyncio.Event().wait()

    component = _supervised_component("worker", target, ready, lambda: False, preflight)
    task = asyncio.create_task(component.run())
    await asyncio.sleep(0)
    assert not component.ready()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_target_blocked_on_first_await_never_marks_component_ready() -> None:
    ready = {"worker": False}
    validation = asyncio.Event()
    running = False

    async def target() -> None:
        nonlocal running
        await validation.wait()
        running = True
        await asyncio.Event().wait()

    component = _supervised_component("worker", target, ready, lambda: running)
    task = asyncio.create_task(component.run())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not component.ready()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_temporal_validate_gate_must_complete_before_ready() -> None:
    ready = {"temporal": False}
    validate = asyncio.Event()
    worker = SimpleNamespace(is_running=False)

    async def worker_run() -> None:
        await validate.wait()
        worker.is_running = True
        await asyncio.Event().wait()

    component = _supervised_component(
        "temporal", worker_run, ready, lambda: worker.is_running
    )
    task = asyncio.create_task(component.run())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not component.ready()
    validate.set()
    while not component.ready():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_close_is_best_effort_and_reports_all_failures() -> None:
    closed: list[str] = []

    class Client:
        async def aclose(self) -> None:
            closed.append("http")
            raise OSError("http close failed")

    def first() -> None:
        closed.append("first")

    def second() -> None:
        closed.append("second")
        raise RuntimeError("second close failed")

    narration = type("Narration", (), {"http_client": Client()})()
    assembly = ProductionWorkerAssembly((), narration, (first, second))  # type: ignore[arg-type]
    with pytest.raises(BaseExceptionGroup) as raised:
        await assembly.close()
    assert closed == ["http", "second", "first"]
    assert len(raised.value.exceptions) == 2


@pytest.mark.asyncio
async def test_worker_close_shuts_down_activity_executor() -> None:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-activity")
    executor.submit(lambda: None).result()

    class Client:
        async def aclose(self) -> None:
            return None

    narration = type("Narration", (), {"http_client": Client()})()
    assembly = ProductionWorkerAssembly((), narration, (executor.shutdown,))  # type: ignore[arg-type]
    await assembly.close()
    with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
        executor.submit(lambda: None)


@pytest.mark.asyncio
async def test_worker_close_releases_runtime_resources_before_executor() -> None:
    closed: list[str] = []

    class Client:
        async def aclose(self) -> None:
            closed.append("narration-http")

    narration = type("Narration", (), {"http_client": Client()})()
    assembly = ProductionWorkerAssembly(
        (),
        narration,  # type: ignore[arg-type]
        (
            lambda: closed.append("executor"),
            lambda: closed.append("database"),
            lambda: closed.append("storyboard"),
            lambda: closed.append("h3-runtime"),
        ),
    )
    await assembly.close()
    assert closed == [
        "narration-http",
        "h3-runtime",
        "storyboard",
        "database",
        "executor",
    ]


@pytest.mark.asyncio
async def test_worker_assembly_failure_shuts_down_activity_executor(monkeypatch) -> None:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="failed-assembly")
    executor.submit(lambda: None).result()

    monkeypatch.setattr(worker_module, "ThreadPoolExecutor", lambda **_kwargs: executor)

    async def fail(*_args, **kwargs):
        kwargs["rollback"].append(lambda: None)
        raise OSError("assembly failed")

    monkeypatch.setattr(worker_module, "_build_production_worker_assembly_with_executor", fail)
    with pytest.raises(OSError, match="assembly failed"):
        await worker_module.build_production_worker_assembly(SimpleNamespace())
    with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
        executor.submit(lambda: None)


@pytest.mark.asyncio
async def test_worker_assembly_failure_rolls_back_resources_in_reverse_order(
    monkeypatch,
) -> None:
    closed: list[str] = []

    class Executor:
        def shutdown(self) -> None:
            closed.append("executor")

    monkeypatch.setattr(worker_module, "ThreadPoolExecutor", lambda **_kwargs: Executor())

    async def fail(*_args, **kwargs):
        rollback = kwargs["rollback"]
        rollback.append(lambda: closed.append("database"))

        async def close_http() -> None:
            closed.append("narration-http")

        rollback.append(close_http)
        rollback.append(lambda: closed.append("storyboard"))
        raise OSError("late assembly failure")

    monkeypatch.setattr(worker_module, "_build_production_worker_assembly_with_executor", fail)
    with pytest.raises(OSError, match="late assembly failure"):
        await worker_module.build_production_worker_assembly(SimpleNamespace())
    assert closed == ["storyboard", "narration-http", "database", "executor"]


@pytest.mark.asyncio
async def test_run_worker_stops_components_before_assembly_resources(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    started = asyncio.Event()
    ready_path = tmp_path / "worker.ready"

    async def component_run() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("worker-stopped")

    component = WorkerComponent("worker", component_run, lambda: started.is_set())

    class Assembly:
        components = (component,)

        async def close(self) -> None:
            assert events == ["worker-stopped"]
            events.append("executor-closed")

    monkeypatch.setattr(
        worker_module, "Settings", lambda: SimpleNamespace(worker_ready_file=str(ready_path))
    )
    monkeypatch.setattr(
        worker_module,
        "build_production_worker_assembly",
        lambda _settings: _async_value(Assembly()),
    )
    task = asyncio.create_task(worker_module.run_worker())
    while not ready_path.exists():
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["worker-stopped", "executor-closed"]


@pytest.mark.asyncio
async def test_run_worker_clears_stale_readiness_before_assembly_failure(
    tmp_path, monkeypatch
) -> None:
    ready_path = tmp_path / "worker.ready"
    ready_path.write_text("stale", encoding="ascii")
    monkeypatch.setattr(
        worker_module, "Settings", lambda: SimpleNamespace(worker_ready_file=str(ready_path))
    )

    async def build(_settings):
        assert not ready_path.exists()
        raise OSError("assembly failed")

    monkeypatch.setattr(worker_module, "build_production_worker_assembly", build)
    with pytest.raises(OSError, match="assembly failed"):
        await worker_module.run_worker()
    assert not ready_path.exists()


@pytest.mark.asyncio
async def test_run_worker_publishes_atomically_and_removes_readiness(
    tmp_path, monkeypatch
) -> None:
    ready_path = tmp_path / "worker.ready"
    replaced: list[tuple[object, object]] = []
    real_replace = worker_module.os.replace
    component = WorkerComponent("ready", lambda: asyncio.Event().wait(), lambda: True)
    class Assembly:
        components = (component,)

        async def close(self) -> None:
            return None

    class Group:
        def __init__(self, _components):
            pass

        async def run(self) -> None:
            while not ready_path.exists():
                await asyncio.sleep(0)

    def replace(source, destination):
        replaced.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(
        worker_module, "Settings", lambda: SimpleNamespace(worker_ready_file=str(ready_path))
    )
    monkeypatch.setattr(
        worker_module,
        "build_production_worker_assembly",
        lambda _settings: _async_value(Assembly()),
    )
    monkeypatch.setattr(worker_module, "SupervisedWorkerGroup", Group)
    monkeypatch.setattr(worker_module.os, "replace", replace)
    await worker_module.run_worker()
    assert len(replaced) == 1
    assert replaced[0][1] == ready_path
    assert not ready_path.exists()


async def _async_value(value):  # type: ignore[no-untyped-def]
    return value


@pytest.mark.asyncio
async def test_run_worker_preserves_primary_and_cleanup_failures(tmp_path, monkeypatch) -> None:
    ready_path = tmp_path / "worker.ready"

    class Assembly:
        components = ()

        async def close(self) -> None:
            raise BaseExceptionGroup(
                "close failures", [RuntimeError("close one"), OSError("close two")]
            )

    class Group:
        def __init__(self, _components):
            pass

        async def run(self) -> None:
            raise LookupError("component failed")

    monkeypatch.setattr(
        worker_module, "Settings", lambda: SimpleNamespace(worker_ready_file=str(ready_path))
    )
    monkeypatch.setattr(
        worker_module,
        "build_production_worker_assembly",
        lambda _settings: _async_value(Assembly()),
    )
    monkeypatch.setattr(worker_module, "SupervisedWorkerGroup", Group)
    with pytest.raises(BaseExceptionGroup) as raised:
        await worker_module.run_worker()
    rendered = repr(raised.value.exceptions)
    assert "component failed" in rendered
    assert "close one" in rendered
    assert "close two" in rendered


@pytest.mark.asyncio
async def test_run_worker_aggregates_simultaneous_task_failures(tmp_path, monkeypatch) -> None:
    ready_path = tmp_path / "worker.ready"

    class Assembly:
        components = ()

        async def close(self) -> None:
            return None

    class Group:
        def __init__(self, _components):
            pass

        async def run(self) -> None:
            raise OSError("worker failed")

    monkeypatch.setattr(
        worker_module, "Settings", lambda: SimpleNamespace(worker_ready_file=str(ready_path))
    )
    monkeypatch.setattr(
        worker_module,
        "build_production_worker_assembly",
        lambda _settings: _async_value(Assembly()),
    )
    monkeypatch.setattr(worker_module, "SupervisedWorkerGroup", Group)
    monkeypatch.setattr(
        worker_module,
        "_publish_ready_file",
        lambda _path: (_ for _ in ()).throw(RuntimeError("ready failed")),
    )
    with pytest.raises(BaseExceptionGroup) as raised:
        await worker_module.run_worker()
    rendered = repr(raised.value.exceptions)
    assert "worker failed" in rendered
    assert "ready failed" in rendered
