"""频道删除的跨聚合写适配器（2026-10-20）。

频道被 `projects`、`character_images`、`cloned_voices` 与四张 `cover_*` 表以
`RESTRICT`/`NO ACTION` 外键引用，数据库不会级联；因此删除必须在一个事务内按
「子表 → 父表」的确定顺序显式执行。确认值校验与项目门禁也在同一事务、同一行锁
下完成，避免检查后被并发写入。

对象字节不在此删除：事务提交后由应用服务直接删（角色图的 staged 行同时转
`CLEANUP_PENDING`，作为直接删除失败时的兜底重试记录）。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from tubeflow_test.application.cover_outbox import (
    COVER_AGGREGATE_CHARACTER_CARD,
    COVER_AGGREGATE_STYLE_CHANNEL,
)
from tubeflow_test.cover_templates.models import (
    ChannelCoverTemplateItemRow,
    ChannelCoverTemplateRow,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Channel, channel_deletion_confirmation
from tubeflow_test.identity_project.ports import ChannelDeletionSummary, ClonedVoiceRemoteCopy
from tubeflow_test.video_descriptions.models import (
    ChannelDescriptionTemplateCounterRow,
    ChannelDescriptionTemplateRow,
)

from .cover.models import (
    CoverCharacterCardRow,
    CoverOutboxRow,
    CoverRefImageRow,
    CoverStyleDraftRow,
    CoverStyleProfileRow,
)
from .identity_project.models import (
    ChannelRow,
    CharacterImageRow,
    ClonedVoiceAccountRow,
    ClonedVoiceRow,
    ProjectRow,
    StagedObjectRow,
)


class SqlAlchemyChannelDeletionRepository:
    """One unit of work per channel deletion; any failure rolls the whole delete back."""

    def __init__(self, unit_of_work: Callable[[], AbstractContextManager[Session]]) -> None:
        self._uow = unit_of_work

    def delete_channel(
        self, owner_id: UUID, channel_id: UUID, confirmation: str
    ) -> ChannelDeletionSummary:
        with self._uow() as session:
            channel_row = session.scalar(
                select(ChannelRow)
                .where(ChannelRow.id == channel_id, ChannelRow.owner_id == owner_id)
                .with_for_update()
            )
            if channel_row is None:
                raise DomainError("not_found", "Resource not found")
            project_count = (
                session.scalar(
                    select(func.count())
                    .select_from(ProjectRow)
                    .where(ProjectRow.channel_id == channel_id)
                )
                or 0
            )
            if project_count:
                raise DomainError(
                    "channel_has_projects",
                    f"该频道下还有 {project_count} 个项目，请先删除项目后再删除频道。",
                )
            expected = channel_deletion_confirmation(
                Channel(
                    id=channel_row.id,
                    owner_id=channel_row.owner_id,
                    url=channel_row.url,
                    name=channel_row.name,
                )
            )
            if confirmation != expected:
                raise DomainError(
                    "channel_deletion_confirmation_required",
                    "请输入完整的频道名称或频道 URL 以确认删除。",
                )

            character_keys = tuple(
                session.scalars(
                    select(CharacterImageRow.object_key).where(
                        CharacterImageRow.channel_id == channel_id
                    )
                )
            )
            cloned_voices = tuple(
                session.scalars(
                    select(ClonedVoiceRow).where(ClonedVoiceRow.channel_id == channel_id)
                )
            )
            cloned_voice_ids = tuple(voice.id for voice in cloned_voices)
            cover_keys = tuple(
                session.scalars(
                    select(CoverRefImageRow.object_key).where(
                        CoverRefImageRow.channel_id == channel_id,
                        CoverRefImageRow.scope == "channel",
                    )
                )
            )
            card_ids = tuple(
                session.scalars(
                    select(CoverCharacterCardRow.id).where(
                        CoverCharacterCardRow.channel_id == channel_id
                    )
                )
            )
            remote_copies = (
                tuple(
                    ClonedVoiceRemoteCopy(voice_id, pool_key_id, el_voice_id)
                    for voice_id, pool_key_id, el_voice_id in session.execute(
                        select(
                            ClonedVoiceAccountRow.cloned_voice_id,
                            ClonedVoiceAccountRow.pool_key_id,
                            ClonedVoiceAccountRow.el_voice_id,
                        ).where(ClonedVoiceAccountRow.cloned_voice_id.in_(cloned_voice_ids))
                    )
                )
                if cloned_voice_ids
                else ()
            )

            # 频道级封面解析事件先删，避免 dispatcher 继续派发即将消失的草稿/角色卡。
            session.execute(
                delete(CoverOutboxRow).where(
                    CoverOutboxRow.aggregate_type == COVER_AGGREGATE_STYLE_CHANNEL,
                    CoverOutboxRow.aggregate_id == channel_id,
                )
            )
            if card_ids:
                session.execute(
                    delete(CoverOutboxRow).where(
                        CoverOutboxRow.aggregate_type == COVER_AGGREGATE_CHARACTER_CARD,
                        CoverOutboxRow.aggregate_id.in_(card_ids),
                    )
                )
            # 角色卡持有 character_image_id（无 ondelete），必须先于角色图删除。
            session.execute(
                delete(CoverCharacterCardRow).where(
                    CoverCharacterCardRow.channel_id == channel_id
                )
            )
            session.execute(
                delete(CoverStyleDraftRow).where(CoverStyleDraftRow.channel_id == channel_id)
            )
            session.execute(
                delete(CoverStyleProfileRow).where(CoverStyleProfileRow.channel_id == channel_id)
            )
            session.execute(
                delete(CoverRefImageRow).where(
                    CoverRefImageRow.channel_id == channel_id,
                    CoverRefImageRow.scope == "channel",
                )
            )
            # 临时功能（2026-10-21 / 2026-10-27）：模板行的图片存在 BYTEA 内，删行即无残留。
            # 新结构可能有多条；旧表仅留档，一并删除。
            template_count = (
                session.scalar(
                    select(func.count())
                    .select_from(ChannelCoverTemplateItemRow)
                    .where(ChannelCoverTemplateItemRow.channel_id == channel_id)
                )
                or 0
            )
            session.execute(
                delete(ChannelCoverTemplateItemRow).where(
                    ChannelCoverTemplateItemRow.channel_id == channel_id
                )
            )
            session.execute(
                delete(ChannelCoverTemplateRow).where(
                    ChannelCoverTemplateRow.channel_id == channel_id
                )
            )
            description_template_count = (
                session.scalar(
                    select(func.count())
                    .select_from(ChannelDescriptionTemplateRow)
                    .where(ChannelDescriptionTemplateRow.channel_id == channel_id)
                )
                or 0
            )
            session.execute(
                delete(ChannelDescriptionTemplateRow).where(
                    ChannelDescriptionTemplateRow.channel_id == channel_id
                )
            )
            session.execute(
                delete(ChannelDescriptionTemplateCounterRow).where(
                    ChannelDescriptionTemplateCounterRow.channel_id == channel_id
                )
            )
            if cloned_voice_ids:
                session.execute(
                    delete(ClonedVoiceAccountRow).where(
                        ClonedVoiceAccountRow.cloned_voice_id.in_(cloned_voice_ids)
                    )
                )
            session.execute(delete(ClonedVoiceRow).where(ClonedVoiceRow.channel_id == channel_id))
            session.execute(
                delete(CharacterImageRow).where(CharacterImageRow.channel_id == channel_id)
            )
            if character_keys:
                # 兜底重试网：直接删字节失败时，媒体运行时启动对账会按引用保护再删一次。
                session.execute(
                    update(StagedObjectRow)
                    .where(StagedObjectRow.object_key.in_(character_keys))
                    .values(
                        state="CLEANUP_PENDING",
                        generation=StagedObjectRow.generation + 1,
                        execution_token=uuid4(),
                        lease_until=None,
                        cleanup_after=datetime.now(UTC),
                        cleanup_checked_at=None,
                        cleanup_attempt=0,
                    )
                )
            session.execute(
                delete(ChannelRow).where(
                    ChannelRow.id == channel_id, ChannelRow.owner_id == owner_id
                )
            )
            return ChannelDeletionSummary(
                channel_id=channel_id,
                character_images=len(character_keys),
                cloned_voices=len(cloned_voices),
                cover_ref_images=len(cover_keys),
                cover_character_cards=len(card_ids),
                object_keys=character_keys + cover_keys
                + tuple(voice.source_object_key for voice in cloned_voices),
                remote_copies=remote_copies,
                cover_templates=template_count,
                description_templates=description_template_count,
            )
