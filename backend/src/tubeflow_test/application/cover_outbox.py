"""封面 outbox 端口与事件类型（照 `application/storyboard_outbox.py`）。

事件类型与聚合：
- ``COVER_REF_ANALYSIS_REQUESTED``：参考图解析显式动作；aggregate_type 为
  ``COVER_STYLE_CHANNEL``/``COVER_STYLE_PROJECT``，aggregate_id 为频道/项目 id，
  project_id 仅项目作用域事件非空；
- ``COVER_PROMPT_GENERATION_REQUESTED``：提示词生成意图；aggregate 为
  cover_prompt_generations.id，由 ``create_prompt_generation`` 同事务写入；
- ``COVER_IMAGE_GENERATION_REQUESTED``：出图意图；aggregate 为
  cover_image_generations.id，由 ``create_image_generation`` 同事务写入；
- ``COVER_CHARACTER_CARD_ANALYSIS_REQUESTED``：角色卡解析显式动作（v5 §1.2）；
  aggregate 为 cover_character_cards.id，project_id 恒为 None。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

COVER_REF_ANALYSIS_REQUESTED = "COVER_REF_ANALYSIS_REQUESTED"
COVER_PROMPT_GENERATION_REQUESTED = "COVER_PROMPT_GENERATION_REQUESTED"
COVER_IMAGE_GENERATION_REQUESTED = "COVER_IMAGE_GENERATION_REQUESTED"
COVER_CHARACTER_CARD_ANALYSIS_REQUESTED = "COVER_CHARACTER_CARD_ANALYSIS_REQUESTED"

COVER_AGGREGATE_STYLE_CHANNEL = "COVER_STYLE_CHANNEL"
COVER_AGGREGATE_STYLE_PROJECT = "COVER_STYLE_PROJECT"
COVER_AGGREGATE_PROMPT_GENERATION = "COVER_PROMPT_GENERATION"
COVER_AGGREGATE_IMAGE_GENERATION = "COVER_IMAGE_GENERATION"
COVER_AGGREGATE_CHARACTER_CARD = "COVER_CHARACTER_CARD"


@dataclass(frozen=True, slots=True)
class CoverOutboxEvent:
    event_id: UUID
    owner_id: UUID
    project_id: UUID | None
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    request_sha256: str
    workflow_id: str
    lease_token: UUID
    attempt_count: int


class CoverOutboxRepository(Protocol):
    """封面 outbox 的派发侧仓储合同（SQL 与内存实现行为一致）。"""

    def request_ref_analysis(
        self,
        *,
        event_id: UUID,
        owner_id: UUID,
        project_id: UUID | None,
        aggregate_type: str,
        aggregate_id: UUID,
        request_sha256: str,
        workflow_id: str,
        now: datetime,
    ) -> bool:
        """写入解析事件；同一 workflow_id 的在途事件已存在时返回 False（去重）。"""
        ...

    def request_character_card_analysis(
        self,
        *,
        event_id: UUID,
        owner_id: UUID,
        aggregate_id: UUID,
        request_sha256: str,
        workflow_id: str,
        now: datetime,
    ) -> bool:
        """写入角色卡解析事件（aggregate 为卡 id，无 project_id）；在途同 workflow_id
        去重返回 False。"""
        ...

    def claim_outbox(
        self, now: datetime, lease_until: datetime, token: UUID
    ) -> CoverOutboxEvent | None:
        """认领一条到期事件：项目作用域事件要求项目 ACTIVE；租约 CAS。"""
        ...

    def acknowledge_outbox(self, event_id: UUID, token: UUID, now: datetime) -> bool: ...

    def retry_outbox(self, event_id: UUID, token: UUID, next_at: datetime, error: str) -> bool: ...


class CoverWorkflowStarter(Protocol):
    async def start(self, event: CoverOutboxEvent) -> None: ...
