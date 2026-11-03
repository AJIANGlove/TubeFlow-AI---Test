"""频道删除编排（2026-10-20 用户确认：仅空频道可删）。

职责：调用跨聚合仓储在单事务内删除频道与其频道级资产，提交后清理对象字节与
ElevenLabs 远端克隆副本。副作用端口：`ChannelDeletionRepository`、
`ExactObjectDeleter`、`ClonedVoiceRemoteCleaner`。

失败语义：数据库事务失败即整体失败（频道保留）；对象字节与远端副本的清理失败
只计数并如实 `warning` 留痕，不回滚已提交的删除，也不在响应里伪装成功。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .domain import Account
from .ports import (
    ChannelDeletionRepository,
    ChannelDeletionSummary,
    ClonedVoiceRemoteCleaner,
    ClonedVoiceRemoteFailure,
)

logger = logging.getLogger(__name__)


class ExactObjectDeleter(Protocol):
    def delete_exact(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ChannelDeletionResult:
    summary: ChannelDeletionSummary
    object_cleanup_failures: tuple[str, ...]
    remote_voice_failures: tuple[ClonedVoiceRemoteFailure, ...]


class ChannelDeletionService:
    def __init__(
        self,
        repository: ChannelDeletionRepository,
        objects: ExactObjectDeleter,
        remote_voices: ClonedVoiceRemoteCleaner,
    ) -> None:
        self._repository = repository
        self._objects = objects
        self._remote_voices = remote_voices

    async def delete(
        self, owner: Account, channel_id: UUID, confirmation: str
    ) -> ChannelDeletionResult:
        summary = self._repository.delete_channel(owner.id, channel_id, confirmation)
        object_failures = self._delete_objects(summary)
        remote_failures = await self._delete_remote_copies(summary)
        return ChannelDeletionResult(summary, object_failures, remote_failures)

    def _delete_objects(self, summary: ChannelDeletionSummary) -> tuple[str, ...]:
        failures: list[str] = []
        for key in dict.fromkeys(summary.object_keys):
            try:
                self._objects.delete_exact(key)
            except Exception:  # 已提交的删除不可回滚；失败必须留痕而不是静默
                logger.warning(
                    "channel deletion left object bytes behind: channel=%s key=%s",
                    summary.channel_id,
                    key,
                    exc_info=True,
                )
                failures.append(key)
        return tuple(failures)

    async def _delete_remote_copies(
        self, summary: ChannelDeletionSummary
    ) -> tuple[ClonedVoiceRemoteFailure, ...]:
        if not summary.remote_copies:
            return ()
        try:
            return await self._remote_voices.delete_remote_copies(summary.remote_copies)
        except Exception:  # 远端清理不影响本地删除结果，但要如实计数与留痕
            logger.warning(
                "channel deletion failed to clean cloned voice copies: channel=%s copies=%d",
                summary.channel_id,
                len(summary.remote_copies),
                exc_info=True,
            )
            return tuple(
                ClonedVoiceRemoteFailure(copy.cloned_voice_id, copy.pool_key_id)
                for copy in summary.remote_copies
            )
