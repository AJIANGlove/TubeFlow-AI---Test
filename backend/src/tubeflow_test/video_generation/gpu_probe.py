from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeGuard

import httpx

from tubeflow_test.video_generation.engine import EngineNode

logger = logging.getLogger(__name__)

GPU_PROBE_RECEIPT_SCHEMA = "tubeflow.h3-gpu-probe.v1"


class GpuProbeOutcome(StrEnum):
    """停滞处置的节点探测结论；PROBE_FAILED 按未知态处理，绝不当作节点空闲。"""

    PROBE_FAILED = "PROBE_FAILED"
    BUSY = "BUSY"
    IDLE = "IDLE"


@dataclass(frozen=True, slots=True)
class GpuProbeObservation:
    """一次节点探测的结构化结论；GPU 明细只进入服务端日志与审计。"""

    outcome: GpuProbeOutcome
    comfyui_reachable: bool | None = None
    queue_running: int | None = None
    queue_pending: int | None = None
    detail: str = ""


class GpuStatusProbe(Protocol):
    """H3 节点只读 GPU/队列状态探测端口；任何失败都 fail-closed 为 PROBE_FAILED。"""

    def probe(self, node: EngineNode) -> GpuProbeObservation: ...


class HttpGpuStatusProbe:
    """节点侧 gpu-status agent 的 HTTP 适配器（短超时，默认 5 秒）。

    401/4xx/5xx、超时、连接错误与非法 JSON 一律判为探测失败，绝不把失败当空闲；
    comfyui_reachable=false 或 queue_running==0 判为节点空闲/进程死。
    """

    def __init__(
        self,
        urls_by_node_id: dict[str, str],
        token: str,
        *,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._urls = dict(urls_by_node_id)
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.Client()

    def close(self) -> None:
        self._client.close()

    def probe(self, node: EngineNode) -> GpuProbeObservation:
        url = self._urls.get(node.node_id, "").strip()
        if not url or not self._token.strip():
            return _failed("gpu_status_probe_unconfigured")
        try:
            response = self._client.get(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "h3 gpu status probe failed for node %s: %s", node.node_id, type(exc).__name__
            )
            return _failed(type(exc).__name__)
        if response.status_code != 200:
            return _failed(f"http_status_{response.status_code}")
        try:
            payload: object = response.json()
        except ValueError:
            return _failed("invalid_json")
        if not isinstance(payload, dict):
            return _failed("invalid_json")
        reachable = payload.get("comfyui_reachable")
        if reachable is False:
            # agent 以 200 + comfyui_reachable=false 上报 ComfyUI 进程不可达。
            return GpuProbeObservation(
                GpuProbeOutcome.IDLE,
                comfyui_reachable=False,
                queue_running=_optional_count(payload.get("queue_running")),
                queue_pending=_optional_count(payload.get("queue_pending")),
                detail="comfyui_unreachable",
            )
        if reachable is not True:
            return _failed("missing_comfyui_reachable")
        queue_running = payload.get("queue_running")
        if not _is_count(queue_running):
            return _failed("invalid_queue_running")
        queue_pending = _optional_count(payload.get("queue_pending"))
        if queue_running > 0:
            return GpuProbeObservation(
                GpuProbeOutcome.BUSY,
                comfyui_reachable=True,
                queue_running=queue_running,
                queue_pending=queue_pending,
                detail="queue_busy",
            )
        return GpuProbeObservation(
            GpuProbeOutcome.IDLE,
            comfyui_reachable=True,
            queue_running=0,
            queue_pending=queue_pending,
            detail="queue_idle",
        )


class UnavailableGpuStatusProbe:
    """未配置探测端点或共享 token 时的保守实现：永远探测失败，维持降频等待与既有兜底。"""

    def probe(self, node: EngineNode) -> GpuProbeObservation:
        return _failed("gpu_status_probe_unconfigured")


class FakeGpuStatusProbe:
    """测试替身：按预设结论返回并记录探测过的节点。"""

    def __init__(self, observation: GpuProbeObservation | None = None) -> None:
        self.observation = observation or GpuProbeObservation(
            GpuProbeOutcome.PROBE_FAILED, detail="fake_probe_failed"
        )
        self.calls: list[str] = []

    def probe(self, node: EngineNode) -> GpuProbeObservation:
        self.calls.append(node.node_id)
        return self.observation


def _failed(detail: str) -> GpuProbeObservation:
    return GpuProbeObservation(GpuProbeOutcome.PROBE_FAILED, detail=detail)


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _optional_count(value: object) -> int | None:
    return value if _is_count(value) else None
