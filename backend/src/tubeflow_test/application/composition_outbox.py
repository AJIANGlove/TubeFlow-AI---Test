from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CompositionEnvelope:
    id: UUID
    project_id: UUID
    owner_id: UUID
    revision_id: UUID
    inputs_sha256: str
    workflow_id: str
    attempt: int
    workflow_generation: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class CompositionDispatchReceipt:
    revision_id: UUID
    workflow_id: str
    state: str
    workflow_generation: int


class CompositionOutboxPort(Protocol):
    def claim(self, *, now: datetime | None = None) -> CompositionEnvelope | None: ...
    def succeeded(self, envelope: CompositionEnvelope) -> None: ...
    def failed(
        self, envelope: CompositionEnvelope, error_code: str, *, now: datetime | None = None
    ) -> None: ...
    def reconcile_lost_dispatches(
        self,
        *,
        older_than: datetime,
        max_attempts: int,
        backoff_seconds: tuple[float, ...],
    ) -> int:
        """对账超时失联的 DISPATCHED 工作流（容器/Worker 崩溃心跳超时死亡）：

        将仍 RUNNING 的任务标记为 FAILED（composition_workflow_lost）；
        AUTO 项目随即按既有自动重试机制排程新代际，MANUAL 留待人工重试。
        """
        ...

    def close_superseded(self, owner_id: UUID, project_id: UUID) -> int:
        """项目已产出当前成片后，收口全部非终态（PENDING/DISPATCHED）行：

        收口为 SUPERSEDED 后 relay 不再派发、reconcile 不再重排；
        已派发的远端工作流不被取消，其终态仍如实落 job 簿记。
        无当前成片时不动任何行。返回收口行数。
        """
        ...


class DurableCompositionDispatcher(Protocol):
    async def dispatch(self, envelope: CompositionEnvelope) -> None: ...


class CompositionOutboxRelay:
    def __init__(
        self, outbox: CompositionOutboxPort, dispatcher: DurableCompositionDispatcher
    ) -> None:
        self._outbox = outbox
        self._dispatcher = dispatcher

    async def relay_one(self) -> bool:
        envelope = self._outbox.claim()
        if envelope is None:
            return False
        try:
            await self._dispatcher.dispatch(envelope)
        except Exception:
            self._outbox.failed(envelope, "composition_dispatch_failed")
            return True
        self._outbox.succeeded(envelope)
        return True
