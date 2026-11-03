"""封面角色卡服务：频道级角色卡的建/改与显式解析编排（v5 计划 §1）。

照 `application/cover_style.py` 的意图/幂等/终态模式：

- 建/改卡（手填 identity ≤100 / appearance ≤500 码点）是纯本地确定性操作，
  零 Provider 调用；角色图属主与频道归属校验失败一律 not_found；
- 「解析角色卡」是显式动作（202）：写 outbox 事件（同一 workflow_id 在途去重），
  由 CoverCharacterCardAnalysisWorkflow 的活动执行 `run_card_analysis`；
  解析复用参考图视觉解析通道（CoverRefAnalysisProvider）与
  `domain.cover.build_character_card_messages`/`validate_character_card`；
- 解析结果 identity 为空 → 稳定失败码 cover_character_card_empty（不覆盖卡，
  回退手写）；成功覆盖卡文本并置 source=analysis，用户可再编辑（source=manual）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from uuid import UUID

from tubeflow_test.application.cover_outbox import CoverOutboxRepository
from tubeflow_test.application.cover_ports import (
    CoverCharacterCard,
    CoverRepository,
)
from tubeflow_test.application.cover_providers import CoverRefAnalysisProvider
from tubeflow_test.application.cover_style import (
    COVER_ANALYSIS_SUCCEEDED,
    CoverObjectStoragePort,
)
from tubeflow_test.application.ports import Clock, IdentifierFactory
from tubeflow_test.domain.cover import (
    CHARACTER_CARD_APPEARANCE_MAX,
    CHARACTER_CARD_IDENTITY_MAX,
    build_character_card_messages,
    validate_character_card,
)
from tubeflow_test.domain.errors import DomainError

COVER_CHARACTER_CARD_SOURCE_MANUAL = "manual"
COVER_CHARACTER_CARD_SOURCE_ANALYSIS = "analysis"


def _not_found() -> DomainError:
    return DomainError("not_found", "Cover resource was not found")


@dataclass(frozen=True, slots=True)
class CoverCharacterCardAnalysisIntent:
    """角色卡解析动作意图结果（照 CoverRefAnalysisIntent，无缓存复用分支）。"""

    card: CoverCharacterCard
    enqueued: bool
    workflow_id: str


class CoverCharacterService:
    def __init__(
        self,
        repo: CoverRepository,
        outbox: CoverOutboxRepository,
        storage: CoverObjectStoragePort,
        analysis: CoverRefAnalysisProvider,
        ids: IdentifierFactory,
        clock: Clock,
    ) -> None:
        self._repo, self._outbox, self._storage = repo, outbox, storage
        self._analysis, self._ids, self._clock = analysis, ids, clock

    def upsert_card(
        self,
        owner_id: UUID,
        channel_id: UUID,
        *,
        character_image_id: UUID,
        identity_text: str = "",
        appearance_text: str = "",
    ) -> CoverCharacterCard:
        """建/改卡（手填）：trim；identity ≤100、appearance ≤500 码点（超限拒绝）；
        角色图须存在、属本账号且属本频道；每角色图一张卡（uq 语义由仓储保证）。"""
        if not self._repo.channel_scope_exists(owner_id, channel_id):
            raise _not_found()
        image = self._repo.get_character_image(owner_id, character_image_id)
        if image is None or image.channel_id != channel_id:
            raise _not_found()
        identity = ("" if identity_text is None else str(identity_text)).strip()
        if len(identity) > CHARACTER_CARD_IDENTITY_MAX:
            raise DomainError(
                "cover_character_card_identity_too_long",
                "Cover character card identity exceeds the 100 code point limit",
            )
        appearance = ("" if appearance_text is None else str(appearance_text)).strip()
        if len(appearance) > CHARACTER_CARD_APPEARANCE_MAX:
            raise DomainError(
                "cover_character_card_appearance_too_long",
                "Cover character card appearance exceeds the 500 code point limit",
            )
        now = self._clock.now()
        return self._repo.upsert_character_card(
            CoverCharacterCard(
                self._ids.new(),
                owner_id,
                channel_id,
                character_image_id,
                identity,
                appearance,
                COVER_CHARACTER_CARD_SOURCE_MANUAL,
                now,
                now,
            )
        )

    def list_cards(self, owner_id: UUID, channel_id: UUID) -> tuple[CoverCharacterCard, ...]:
        if not self._repo.channel_scope_exists(owner_id, channel_id):
            raise _not_found()
        return self._repo.list_character_cards_for_channel(owner_id, channel_id)

    def create_card_analysis_intent(
        self, owner_id: UUID, channel_id: UUID, card_id: UUID
    ) -> CoverCharacterCardAnalysisIntent:
        """「解析角色卡」显式动作：写 outbox 事件（在途同 workflow_id 去重）。"""
        card = self._repo.get_character_card(owner_id, card_id)
        if card is None or card.channel_id != channel_id:
            raise _not_found()
        image = self._repo.get_character_image(owner_id, card.character_image_id)
        if image is None:
            raise _not_found()
        request_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "card_id": str(card.id),
                    "character_image_sha256": image.sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        workflow_id = f"cover-character-card-analysis-{owner_id}-{card.id}-{request_sha256}"
        enqueued = self._outbox.request_character_card_analysis(
            event_id=self._ids.new(),
            owner_id=owner_id,
            aggregate_id=card.id,
            request_sha256=request_sha256,
            workflow_id=workflow_id,
            now=self._clock.now(),
        )
        return CoverCharacterCardAnalysisIntent(card, enqueued, workflow_id)

    def run_card_analysis(self, owner_id: UUID, card_id: UUID) -> str:
        """角色卡解析编排（工作流活动内执行）：取字节 → Provider → 校验 → 覆盖卡。

        validate_character_card 的 DomainError（identity 空 →
        cover_character_card_empty）直接上抛，由活动转 FAILED:<code>；卡内容不被
        破坏。成功覆盖 identity/appearance 并置 source=analysis。
        """
        card = self._repo.get_character_card(owner_id, card_id)
        if card is None:
            raise _not_found()
        image = self._repo.get_character_image(owner_id, card.character_image_id)
        if image is None:
            raise _not_found()
        try:
            content = self._storage.get_verified(image.object_key, image.sha256, image.size_bytes)
        except ValueError as exc:
            raise DomainError(
                "storage_integrity_error", "Cover character image bytes failed integrity check"
            ) from exc
        messages = build_character_card_messages(
            {"image_base64": base64.b64encode(content).decode("ascii")}
        )
        result = self._analysis.analyze(messages)
        validated = validate_character_card(result.payload)
        self._repo.upsert_character_card(
            replace(
                card,
                identity_text=validated["identity"],
                appearance_text=validated["appearance"],
                source=COVER_CHARACTER_CARD_SOURCE_ANALYSIS,
                updated_at=self._clock.now(),
            )
        )
        return COVER_ANALYSIS_SUCCEEDED
