"""视频提示词编译诊断落库（2026-10-27）。

编译失败发生在组级 attempt 创建之前，既有链路没有可写的位置；这里用一张
只增不改的诊断表记录每次尝试的策略、原因码与模型原始输出，供事后归因。
诊断写入永远不会影响生成主流程：任何异常只记日志。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 20_000

_INSERT = text(
    """
    INSERT INTO video_prompt_compilations (
        id, owner_id, project_id, group_id, attempt_id, strategy, model,
        payload_sha256, attempt_index, reason, output_sha256, output_text, created_at
    ) VALUES (
        :id, :owner_id, :project_id, :group_id, :attempt_id, :strategy, :model,
        :payload_sha256, :attempt_index, :reason, :output_sha256, :output_text, :created_at
    )
    """
)


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


class SqlVideoPromptDiagnostics:
    """`record(payload)` 接口的诊断写入器；按每次尝试一行落库。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def record(self, payload: Mapping[str, Any]) -> None:
        owner_id = _as_uuid(payload.get("owner_id"))
        project_id = _as_uuid(payload.get("project_id"))
        if owner_id is None or project_id is None:
            logger.warning("video prompt diagnostics skipped: missing owner/project")
            return
        try:
            with self._sessions() as session, session.begin():
                session.execute(
                    _INSERT,
                    {
                        "id": uuid4(),
                        "owner_id": owner_id,
                        "project_id": project_id,
                        "group_id": _as_uuid(payload.get("group_id")),
                        "attempt_id": _as_uuid(payload.get("attempt_id")),
                        "strategy": str(payload.get("strategy") or "")[:64],
                        "model": str(payload.get("model") or "")[:128],
                        "payload_sha256": (str(payload.get("payload_sha256") or "") or None),
                        "attempt_index": int(payload.get("attempt_index") or 0),
                        "reason": str(payload.get("reason") or "")[:200],
                        "output_sha256": (str(payload.get("output_sha256") or "") or None),
                        "output_text": (
                            str(payload.get("output_text") or "")[:_MAX_OUTPUT_CHARS] or None
                        ),
                        "created_at": datetime.now(UTC),
                    },
                )
        except Exception:  # 诊断绝不影响主流程
            logger.exception("video prompt diagnostics write failed")


DiagnosticsSink = Callable[[Mapping[str, Any]], None]
