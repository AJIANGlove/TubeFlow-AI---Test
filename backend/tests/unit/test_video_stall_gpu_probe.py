from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from temporalio.testing import WorkflowEnvironment
from test_video_auto_mode import Probe, drive_replacement_to_success, submit_calls
from test_video_auto_mode import setup as auto_setup

from tubeflow_test.config import Settings
from tubeflow_test.video_generation.engine import EngineNode
from tubeflow_test.video_generation.gpu_probe import (
    FakeGpuStatusProbe,
    GpuProbeObservation,
    GpuProbeOutcome,
    HttpGpuStatusProbe,
)
from tubeflow_test.video_generation.orchestration import (
    SUBMISSION_BUDGET_EXHAUSTED,
    ArchiveStage,
    AttemptStage,
    DurableGroupVideoOrchestrator,
    StallPolicy,
)
from tubeflow_test.video_generation.temporal import (
    GroupVideoRecoveryWorkflow,
    RecoverVideoCommand,
)
from tubeflow_test.video_generation.worker import create_video_worker
from tubeflow_test.video_prompt.fake_compiler import (
    FakeVideoPromptCompiler as StubVideoPromptCompiler,
)

_STALL_NOW = StallPolicy(accepted_seconds=0.0, running_seconds=0.0, poll_seconds=1.0)


def _setup_with_probe(production_mode: str, gpu_probe: FakeGpuStatusProbe):
    source, repository, engine, assets, _service = auto_setup(production_mode=production_mode)
    service = DurableGroupVideoOrchestrator(
        repository,
        engine,
        assets,
        Probe(),
        prompt_compiler=StubVideoPromptCompiler(),
        stall_policy=_STALL_NOW,
        gpu_probe=gpu_probe,
    )
    return source, repository, engine, service


def _busy() -> GpuProbeObservation:
    return GpuProbeObservation(
        GpuProbeOutcome.BUSY,
        comfyui_reachable=True,
        queue_running=1,
        queue_pending=3,
        detail="queue_busy",
    )


def _idle() -> GpuProbeObservation:
    return GpuProbeObservation(
        GpuProbeOutcome.IDLE,
        comfyui_reachable=True,
        queue_running=0,
        queue_pending=0,
        detail="queue_idle",
    )


def _cancel_calls(engine) -> int:
    return sum(call == "cancel" for _, call in engine.calls)


def _gpu_events(repository) -> list[str]:
    return [
        event.event_type
        for event in repository.provider_events
        if event.event_type.startswith("GPU_PROBE")
    ]


def test_auto_stalled_busy_node_keeps_waiting_without_resubmit() -> None:
    probe = FakeGpuStatusProbe(_busy())
    source, repository, engine, service = _setup_with_probe("AUTO", probe)
    engine.inspect_status = "ACCEPTED"
    intent = service.trigger(source.owner_id, source.group_id, "stall-busy")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED

    stalled = service.advance(source.owner_id, intent.attempt_id)

    assert stalled.stage is AttemptStage.SUBMITTED
    assert stalled.stalled_at is not None
    assert probe.calls == [stalled.node_id]
    assert _cancel_calls(engine) == 0
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}
    assert _gpu_events(repository) == ["GPU_PROBE_BUSY"]

    again = service.advance(source.owner_id, intent.attempt_id)
    assert again.stage is AttemptStage.SUBMITTED
    assert again.stalled_at == stalled.stalled_at
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert probe.calls == [stalled.node_id, stalled.node_id]
    assert _gpu_events(repository) == ["GPU_PROBE_BUSY", "GPU_PROBE_BUSY"]


def test_auto_stalled_idle_node_cancels_and_resubmits_within_budget() -> None:
    probe = FakeGpuStatusProbe(_idle())
    source, repository, engine, service = _setup_with_probe("AUTO", probe)
    engine.inspect_status = "ACCEPTED"
    intent = service.trigger(source.owner_id, source.group_id, "stall-idle")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED

    resolved = service.advance(source.owner_id, intent.attempt_id)

    assert resolved.stage is AttemptStage.CANCELLED
    assert resolved.stalled_at is not None
    assert "auto_cancel:stalled_node_idle" in resolved.retry_evidence
    assert "auto_resubmit:stalled_node_idle" in resolved.retry_evidence
    assert probe.calls == [submitted.node_id]
    assert _cancel_calls(engine) == 1
    assert submit_calls(engine) == 1
    replacement = repository.find_attempt(source.owner_id, f"auto-resubmit:{intent.attempt_id}")
    assert replacement is not None
    assert replacement.superseded_attempt_id == intent.attempt_id
    assert repository.scheduled_delays[replacement.attempt_id] == 5.0
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    event_types = [event.event_type for event in repository.provider_events]
    assert "GPU_PROBE_IDLE" in event_types
    assert "RESUBMIT_STALLED_IDLE" in event_types

    # 重放旧尝试：清理暂存后落 CANCELLED 终态，不产生第二次替代提交。
    cleaned = service.advance(source.owner_id, intent.attempt_id)
    assert cleaned.stage is AttemptStage.CANCELLED
    replayed = service.advance(source.owner_id, intent.attempt_id)
    assert replayed.stage is AttemptStage.CANCELLED
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    assert submit_calls(engine) == 1

    # 替代提交复用持久化输入与已编译提示词，可以走通完整链路。
    archived = drive_replacement_to_success(
        source, repository, engine, service, replacement.attempt_id
    )
    assert archived.stage is AttemptStage.ARCHIVED
    assert submit_calls(engine) == 2


def test_auto_stalled_dead_comfyui_process_cancels_and_resubmits() -> None:
    probe = FakeGpuStatusProbe(
        GpuProbeObservation(
            GpuProbeOutcome.IDLE,
            comfyui_reachable=False,
            detail="comfyui_unreachable",
        )
    )
    source, repository, engine, service = _setup_with_probe("AUTO", probe)
    engine.inspect_status = "ACCEPTED"
    intent = service.trigger(source.owner_id, source.group_id, "stall-dead")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED

    resolved = service.advance(source.owner_id, intent.attempt_id)

    assert resolved.stage is AttemptStage.CANCELLED
    assert "auto_cancel:stalled_node_idle" in resolved.retry_evidence
    assert "auto_resubmit:stalled_node_idle" in resolved.retry_evidence
    assert _cancel_calls(engine) == 1
    replacement = repository.find_attempt(source.owner_id, f"auto-resubmit:{intent.attempt_id}")
    assert replacement is not None
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    assert "GPU_PROBE_IDLE" in _gpu_events(repository)


def test_auto_stalled_probe_failure_keeps_waiting_and_fallback_still_applies() -> None:
    source, repository, engine, service = _setup_with_probe("AUTO", FakeGpuStatusProbe())
    engine.inspect_status = "ACCEPTED"
    intent = service.trigger(source.owner_id, source.group_id, "stall-probe-failed")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED

    stalled = service.advance(source.owner_id, intent.attempt_id)

    # 探测失败：不取消、不重提，维持降频等待。
    assert stalled.stage is AttemptStage.SUBMITTED
    assert stalled.stalled_at is not None
    assert _cancel_calls(engine) == 0
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}
    assert _gpu_events(repository) == ["GPU_PROBE_INCONCLUSIVE"]

    # 既有兜底不受探测失败影响：远端持续报告未知时对账连续 3 轮无果自动重提。
    engine.remote_acceptance = "UNKNOWN"
    first = service.advance(source.owner_id, intent.attempt_id)
    assert first.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    second = service.advance(source.owner_id, intent.attempt_id)
    assert second.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1

    third = service.advance(source.owner_id, intent.attempt_id)

    assert third.stage is AttemptStage.FAILED
    assert "auto_resubmit:remote_state_unverifiable" in third.retry_evidence
    replacement = repository.find_attempt(source.owner_id, f"auto-resubmit:{intent.attempt_id}")
    assert replacement is not None
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    # 未知态对账轮不再触发 GPU 探测。
    assert _gpu_events(repository) == ["GPU_PROBE_INCONCLUSIVE"]


def test_auto_stalled_without_configured_probe_defaults_to_conservative_wait() -> None:
    source, repository, engine, assets, _service = auto_setup(production_mode="AUTO")
    service = DurableGroupVideoOrchestrator(
        repository,
        engine,
        assets,
        Probe(),
        prompt_compiler=StubVideoPromptCompiler(),
        stall_policy=_STALL_NOW,
    )
    engine.inspect_status = "ACCEPTED"
    intent = service.trigger(source.owner_id, source.group_id, "stall-unconfigured")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED

    stalled = service.advance(source.owner_id, intent.attempt_id)

    # 未配置探测端点：编排器回退到始终探测失败的保守实现，维持降频等待。
    assert stalled.stage is AttemptStage.SUBMITTED
    assert stalled.stalled_at is not None
    assert _cancel_calls(engine) == 0
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}
    assert _gpu_events(repository) == ["GPU_PROBE_INCONCLUSIVE"]


def test_auto_stalled_idle_resubmit_exhausts_budget_into_stable_terminal() -> None:
    source, repository, engine, service = _setup_with_probe("AUTO", FakeGpuStatusProbe(_idle()))
    engine.inspect_status = "ACCEPTED"
    attempt = service.trigger(source.owner_id, source.group_id, "stall-budget")
    submitted = service.advance(source.owner_id, attempt.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED

    # 2026-10-16 决策：每组每预算轮提交预算 6 次。
    for _round in range(6):
        resolved = service.advance(source.owner_id, attempt.attempt_id)
        assert resolved.stage is AttemptStage.CANCELLED
        replacement = repository.find_attempt(
            source.owner_id, f"auto-resubmit:{attempt.attempt_id}"
        )
        if replacement is None:
            break
        # fake 引擎在取消后报告 CANCELLED，复位远端状态供下一轮提交。
        engine.inspect_status = "ACCEPTED"
        submitted = service.advance(source.owner_id, replacement.attempt_id)
        assert submitted.stage is AttemptStage.SUBMITTED
        attempt = replacement

    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    assert submit_calls(engine) == 6
    assert _cancel_calls(engine) == 6
    parked = repository.get_attempt(attempt.attempt_id)
    assert parked is not None
    # 预算耗尽：保留 CANCELLED 稳定终态，稳定原因码进入血缘证据与审计事件。
    assert parked.stage is AttemptStage.CANCELLED
    assert f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}" in parked.retry_evidence
    assert any(
        event.error_code == SUBMISSION_BUDGET_EXHAUSTED for event in repository.provider_events
    )

    # 预算耗尽后重放旧尝试不再产生任何新提交或新替代。
    service.advance(source.owner_id, attempt.attempt_id)
    service.advance(source.owner_id, attempt.attempt_id)
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    assert submit_calls(engine) == 6


def test_manual_stalled_attempt_never_probes_or_acts() -> None:
    probe = FakeGpuStatusProbe(_idle())
    source, repository, engine, service = _setup_with_probe("MANUAL", probe)
    engine.inspect_status = "ACCEPTED"
    intent = service.trigger(source.owner_id, source.group_id, "stall-manual")
    submitted = service.advance(source.owner_id, intent.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED

    stalled = service.advance(source.owner_id, intent.attempt_id)

    assert stalled.stage is AttemptStage.SUBMITTED
    assert stalled.stalled_at is not None
    assert probe.calls == []
    assert _cancel_calls(engine) == 0
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}
    assert not [
        event
        for event in repository.provider_events
        if event.event_type.startswith(("GPU_PROBE", "RESUBMIT"))
    ]


@pytest.mark.asyncio
async def test_recovery_workflow_ends_for_terminal_attempt_with_stall_marker() -> None:
    class Service:
        calls = 0

        def advance(self, _owner_id, _attempt_id) -> object:
            self.calls += 1
            return type(
                "Attempt",
                (),
                {
                    "archive_stage": ArchiveStage.NOT_STARTED,
                    "stage": AttemptStage.CANCELLED,
                    "error_code": None,
                    "stalled_at": "2026-09-15T00:00:00+00:00",
                },
            )()

    service = Service()
    task_queue = f"h3-stalled-terminal-{uuid4()}"
    command = RecoverVideoCommand(uuid4(), uuid4(), stalled_poll_seconds=1.0)
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        create_video_worker(environment.client, service, task_queue=task_queue),  # type: ignore[arg-type]
    ):
        result = await environment.client.execute_workflow(
            GroupVideoRecoveryWorkflow.run,
            command,
            id=f"h3-stalled-terminal-{uuid4()}",
            task_queue=task_queue,
        )
    assert result == command
    assert service.calls == 1


def _h3_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "h3_node_urls": ("http://a", "http://b", "http://c"),
        "h3_node_ids": ("node-a", "node-b", "node-c"),
        "h3_node_instance_ids": ("instance-a", "instance-b", "instance-c"),
        "h3_node_tokens": ("ta", "tb", "tc"),
        "h3_node_capacities": (1, 1, 1),
        "h3_workflow_manifest_sha256": "a" * 64,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_gpu_probe_config_accepts_aligned_urls_with_empty_entries() -> None:
    settings = _h3_settings(
        h3_node_gpu_status_urls=("http://gpu-a:8399/gpu-status", "", "http://gpu-c:8399/gpu-status")
    )
    assert not settings.h3_gpu_probe_configured

    configured = _h3_settings(
        h3_node_gpu_status_urls=("http://gpu-a:8399/gpu-status", "", "http://gpu-c:8399/gpu-status"),
        h3_gpu_agent_token="shared-secret",
    )
    assert configured.h3_gpu_probe_configured


def test_gpu_probe_config_requires_alignment_with_nodes() -> None:
    with pytest.raises(ValueError, match="GPU status URLs must align"):
        _h3_settings(h3_node_gpu_status_urls=("http://gpu-a", "http://gpu-b"))
    with pytest.raises(ValueError, match="GPU status URLs must align"):
        Settings(_env_file=None, h3_node_gpu_status_urls=("http://gpu-a",))


def test_gpu_probe_config_rejects_non_absolute_urls() -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        _h3_settings(h3_node_gpu_status_urls=("gpu-a:8399/gpu-status", "", ""))


def _http_probe(
    handler,
    *,
    urls: dict[str, str] | None = None,
    token: str = "shared-secret",
) -> HttpGpuStatusProbe:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpGpuStatusProbe(
        dict(urls) if urls is not None else {"node-0": "http://gpu-0:8399/gpu-status"},
        token,
        client=client,
    )


_NODE = EngineNode("node-0", "http://node-0", "node-secret", 1)


def test_http_probe_maps_busy_and_idle_queue_states() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "comfyui_reachable": True,
                "queue_running": 2,
                "queue_pending": 5,
                "devices": [{"name": "gpu", "vram_total": 1, "vram_used": 1, "vram_free": 0}],
            },
        )

    probe = _http_probe(handler)
    busy = probe.probe(_NODE)
    assert busy.outcome is GpuProbeOutcome.BUSY
    assert busy.queue_running == 2
    assert busy.queue_pending == 5
    assert seen[0].headers["Authorization"] == "Bearer shared-secret"

    def idle_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"comfyui_reachable": True, "queue_running": 0, "queue_pending": 4, "devices": []},
        )

    idle = _http_probe(idle_handler).probe(_NODE)
    assert idle.outcome is GpuProbeOutcome.IDLE
    assert idle.comfyui_reachable is True
    assert idle.queue_running == 0


def test_http_probe_maps_unreachable_comfyui_to_idle() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"comfyui_reachable": False, "queue_running": None, "queue_pending": None},
        )

    observation = _http_probe(handler).probe(_NODE)
    assert observation.outcome is GpuProbeOutcome.IDLE
    assert observation.comfyui_reachable is False
    assert observation.detail == "comfyui_unreachable"


@pytest.mark.parametrize("status_code", [401, 403, 404, 500, 503])
def test_http_probe_fails_closed_on_http_errors(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "boom"})

    observation = _http_probe(handler).probe(_NODE)
    assert observation.outcome is GpuProbeOutcome.PROBE_FAILED
    assert observation.detail == f"http_status_{status_code}"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not-a-dict", id="non_dict_json"),
        pytest.param({"queue_running": 0}, id="missing_reachable"),
        pytest.param({"comfyui_reachable": True, "queue_running": "1"}, id="invalid_running"),
        pytest.param({"comfyui_reachable": True, "queue_running": True}, id="bool_running"),
        pytest.param({"comfyui_reachable": True, "queue_running": -1}, id="negative_running"),
    ],
)
def test_http_probe_fails_closed_on_invalid_payloads(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    assert _http_probe(handler).probe(_NODE).outcome is GpuProbeOutcome.PROBE_FAILED


def test_http_probe_fails_closed_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    assert _http_probe(handler).probe(_NODE).outcome is GpuProbeOutcome.PROBE_FAILED


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(httpx.ConnectError("down"), id="connect_error"),
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
    ],
)
def test_http_probe_fails_closed_on_transport_errors(error: httpx.HTTPError) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    assert _http_probe(handler).probe(_NODE).outcome is GpuProbeOutcome.PROBE_FAILED


def test_http_probe_fails_closed_without_url_or_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue any HTTP request")

    assert _http_probe(handler, urls={}).probe(_NODE).outcome is GpuProbeOutcome.PROBE_FAILED
    assert (
        _http_probe(handler, urls={"node-0": ""}).probe(_NODE).outcome
        is GpuProbeOutcome.PROBE_FAILED
    )
    assert _http_probe(handler, token="").probe(_NODE).outcome is GpuProbeOutcome.PROBE_FAILED
