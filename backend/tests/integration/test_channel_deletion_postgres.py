"""频道删除的 PostgreSQL 集成测试（真实迁移 + 真实外键约束）。

证明三件事：
1. 七张引用表的 RESTRICT/NO ACTION 外键按既定顺序被满足，事务不失败；
2. 只删目标频道，另一个频道与其资产完好；
3. 角色图/克隆音色源音频/封面参考图的对象键都进入可被既有对账任务回收的清理状态。

需要显式隔离库：`TUBEFLOW_TEST_POSTGRES_URL`（fixture 会在其中建独立 schema）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from tubeflow_test.adapters.persistence.channel_deletion import (
    SqlAlchemyChannelDeletionRepository,
)
from tubeflow_test.adapters.persistence.cover.models import (
    CoverCharacterCardRow,
    CoverOutboxRow,
    CoverRefImageRow,
    CoverStyleDraftRow,
    CoverStyleProfileRow,
)
from tubeflow_test.adapters.persistence.elevenlabs import ElevenlabsKeyRow
from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    ChannelRow,
    CharacterImageRow,
    ClonedVoiceAccountRow,
    ClonedVoiceRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.media_repository import reconcile_staged_objects
from tubeflow_test.application.cover_outbox import (
    COVER_AGGREGATE_CHARACTER_CARD,
    COVER_AGGREGATE_STYLE_CHANNEL,
    COVER_CHARACTER_CARD_ANALYSIS_REQUESTED,
    COVER_REF_ANALYSIS_REQUESTED,
)
from tubeflow_test.cover_templates.models import (
    ChannelCoverTemplateItemRow,
    ChannelCoverTemplateRow,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.ports import ChannelDeletionSummary
from tubeflow_test.video_descriptions.models import (
    ChannelDescriptionTemplateCounterRow,
    ChannelDescriptionTemplateRow,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
URL = "https://www.youtube.com/@channel-delete"


class _RecordingStorage:
    """reconcile_staged_objects 所需的最小存储端口。"""

    def __init__(self, keys: set[str]) -> None:
        self.keys = keys
        self.deleted: list[str] = []

    def delete_exact(self, key: str) -> None:
        self.deleted.append(key)
        self.keys.discard(key)

    def exists(self, key: str) -> bool:
        return key in self.keys

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        raise AssertionError("verified read must not be needed for channel deletion tombstones")


class _Seed:
    def __init__(self) -> None:
        self.owner_id = uuid4()
        self.channel_id = uuid4()
        self.other_channel_id = uuid4()
        self.image_key = str(uuid4())
        self.cloned_key = f"cloned-voices/{self.channel_id}/{uuid4()}/source.mp3"
        self.cover_key = f"cover-ref-images/channel/{self.channel_id}/{'a' * 64}"
        self.image_id = uuid4()
        self.voice_id = uuid4()
        self.pool_key_id = uuid4()
        self.card_id = uuid4()
        self.profile_ids = (uuid4(), uuid4())


def _seed(postgres_database: object, seed: _Seed) -> None:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        session.add(
            AccountRow(
                id=seed.owner_id,
                username=f"owner-{seed.owner_id.hex[:8]}",
                password_hash="hashed",
                role="user",
                active=True,
                session_generation=1,
            )
        )
        session.add_all(
            [
                ChannelRow(id=seed.channel_id, owner_id=seed.owner_id, url=URL, name="待删频道"),
                ChannelRow(
                    id=seed.other_channel_id,
                    owner_id=seed.owner_id,
                    url="https://www.youtube.com/@keep-me",
                    name="保留频道",
                ),
            ]
        )
        session.flush()
        session.add(
            CharacterImageRow(
                id=seed.image_id,
                owner_id=seed.owner_id,
                channel_id=seed.channel_id,
                asset_id=uuid4(),
                object_key=seed.image_key,
                sha256="b" * 64,
                media_type="image/png",
                size_bytes=10,
                width=4,
                height=4,
            )
        )
        session.add(
            StagedObjectRow(
                id=uuid4(),
                object_key=seed.image_key,
                logical_object_key=seed.image_key,
                owner_id=seed.owner_id,
                ownership_scope="OWNER",
                created_at=NOW,
                state="FINALIZED",
                sha256="b" * 64,
                size_bytes=10,
                media_type="image/png",
                generation=1,
                reference_type="character_image",
            )
        )
        session.add(
            ElevenlabsKeyRow(
                id=seed.pool_key_id,
                api_key=f"el-{seed.pool_key_id.hex[:8]}",
                label="pool",
                status="active",
                status_at=NOW,
                added_at=NOW,
            )
        )
        session.add(
            ClonedVoiceRow(
                id=seed.voice_id,
                channel_id=seed.channel_id,
                owner_id=seed.owner_id,
                name="克隆音色",
                remark="",
                source_object_key=seed.cloned_key,
                source_sha256="c" * 64,
                source_size_bytes=12,
                source_media_type="audio/mpeg",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ClonedVoiceAccountRow(
                cloned_voice_id=seed.voice_id,
                pool_key_id=seed.pool_key_id,
                el_voice_id="el-voice-1",
                created_at=NOW,
            )
        )
        session.add(
            CoverRefImageRow(
                id=uuid4(),
                owner_id=seed.owner_id,
                scope="channel",
                channel_id=seed.channel_id,
                sha256="a" * 64,
                object_key=seed.cover_key,
                byte_size=11,
                width=4,
                height=4,
                image_format="png",
                created_at=NOW,
            )
        )
        session.add(
            CoverStyleDraftRow(
                id=uuid4(),
                owner_id=seed.owner_id,
                scope="channel",
                channel_id=seed.channel_id,
                ref_image_sha256="a" * 64,
                edited_text="主体：站立男子",
                updated_at=NOW,
            )
        )
        session.add_all(
            [
                CoverStyleProfileRow(
                    id=seed.profile_ids[0],
                    owner_id=seed.owner_id,
                    channel_id=seed.channel_id,
                    version=1,
                    edited_text="第一版",
                    label="",
                ),
                CoverStyleProfileRow(
                    id=seed.profile_ids[1],
                    owner_id=seed.owner_id,
                    channel_id=seed.channel_id,
                    version=2,
                    edited_text="第二版",
                    supersedes_id=seed.profile_ids[0],
                    label="",
                ),
            ]
        )
        session.add_all(
            [
                ChannelCoverTemplateRow(
                    channel_id=seed.channel_id,
                    prompt_a="A",
                    prompt_b="B",
                    prompt_c="C",
                    updated_at=NOW,
                ),
                ChannelCoverTemplateRow(
                    channel_id=seed.other_channel_id,
                    prompt_a="keep",
                    prompt_b="",
                    prompt_c="",
                    updated_at=NOW,
                ),
                ChannelCoverTemplateItemRow(
                    id=uuid4(),
                    channel_id=seed.channel_id,
                    name="模板 1",
                    position=0,
                    prompt="A",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                ChannelCoverTemplateItemRow(
                    id=uuid4(),
                    channel_id=seed.other_channel_id,
                    name="模板 1",
                    position=0,
                    prompt="keep",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                ChannelDescriptionTemplateCounterRow(
                    channel_id=seed.channel_id, next_version=2
                ),
                ChannelDescriptionTemplateRow(
                    channel_id=seed.channel_id,
                    version=1,
                    prompt="description",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.add(
            CoverCharacterCardRow(
                id=seed.card_id,
                owner_id=seed.owner_id,
                channel_id=seed.channel_id,
                character_image_id=seed.image_id,
                identity_text="小蓝",
                appearance_text="Q 版",
                source="manual",
            )
        )
        session.add_all(
            [
                CoverOutboxRow(
                    id=uuid4(),
                    owner_id=seed.owner_id,
                    aggregate_type=COVER_AGGREGATE_STYLE_CHANNEL,
                    aggregate_id=seed.channel_id,
                    event_type=COVER_REF_ANALYSIS_REQUESTED,
                    request_sha256="d" * 64,
                    workflow_id=f"cover-ref-analysis-{seed.channel_id}",
                    state="PENDING",
                    attempt_count=0,
                    next_attempt_at=NOW,
                ),
                CoverOutboxRow(
                    id=uuid4(),
                    owner_id=seed.owner_id,
                    aggregate_type=COVER_AGGREGATE_CHARACTER_CARD,
                    aggregate_id=seed.card_id,
                    event_type=COVER_CHARACTER_CARD_ANALYSIS_REQUESTED,
                    request_sha256="e" * 64,
                    workflow_id=f"cover-card-analysis-{seed.card_id}",
                    state="PENDING",
                    attempt_count=0,
                    next_attempt_at=NOW,
                ),
            ]
        )


def _delete(postgres_database: object, seed: _Seed, confirmation: str) -> ChannelDeletionSummary:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    return SqlAlchemyChannelDeletionRepository(sessions.begin).delete_channel(
        seed.owner_id, seed.channel_id, confirmation
    )


def test_channel_deletion_satisfies_restrict_foreign_keys(
    postgres_database: object,
) -> None:
    seed = _Seed()
    _seed(postgres_database, seed)

    summary = _delete(postgres_database, seed, "待删频道")

    assert summary.character_images == 1
    assert summary.cloned_voices == 1
    assert summary.cover_ref_images == 1
    assert summary.cover_character_cards == 1
    assert summary.cover_templates == 1
    assert summary.description_templates == 1
    assert set(summary.object_keys) == {seed.image_key, seed.cloned_key, seed.cover_key}
    assert summary.remote_copies[0].el_voice_id == "el-voice-1"

    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions() as session:
        assert session.get(ChannelRow, seed.channel_id) is None
        assert session.get(ChannelRow, seed.other_channel_id) is not None
        assert session.scalar(
            select(CharacterImageRow.id).where(CharacterImageRow.channel_id == seed.channel_id)
        ) is None
        assert session.scalar(
            select(ClonedVoiceRow.id).where(ClonedVoiceRow.channel_id == seed.channel_id)
        ) is None
        assert session.scalar(
            select(ClonedVoiceAccountRow.cloned_voice_id).where(
                ClonedVoiceAccountRow.cloned_voice_id == seed.voice_id
            )
        ) is None
        assert session.scalar(
            select(CoverRefImageRow.id).where(CoverRefImageRow.channel_id == seed.channel_id)
        ) is None
        assert session.scalar(
            select(CoverStyleDraftRow.id).where(CoverStyleDraftRow.channel_id == seed.channel_id)
        ) is None
        assert session.scalar(
            select(CoverStyleProfileRow.id).where(
                CoverStyleProfileRow.channel_id == seed.channel_id
            )
        ) is None
        assert session.scalar(
            select(CoverCharacterCardRow.id).where(
                CoverCharacterCardRow.channel_id == seed.channel_id
            )
        ) is None
        assert session.scalar(
            select(CoverOutboxRow.id).where(CoverOutboxRow.aggregate_id == seed.channel_id)
        ) is None
        assert session.scalar(
            select(CoverOutboxRow.id).where(CoverOutboxRow.aggregate_id == seed.card_id)
        ) is None
        assert session.get(ChannelCoverTemplateRow, seed.channel_id) is None
        assert session.scalar(
            select(ChannelCoverTemplateItemRow.id).where(
                ChannelCoverTemplateItemRow.channel_id == seed.channel_id
            )
        ) is None
        assert session.get(ChannelDescriptionTemplateCounterRow, seed.channel_id) is None
        assert session.scalars(
            select(ChannelDescriptionTemplateRow).where(
                ChannelDescriptionTemplateRow.channel_id == seed.channel_id
            )
        ).all() == []
        kept = session.get(ChannelCoverTemplateRow, seed.other_channel_id)
        assert kept is not None
        assert kept.prompt_a == "keep"
        kept_item = session.scalar(
            select(ChannelCoverTemplateItemRow).where(
                ChannelCoverTemplateItemRow.channel_id == seed.other_channel_id
            )
        )
        assert kept_item is not None
        assert kept_item.prompt == "keep"


def test_channel_deletion_tombstones_object_bytes_for_reconciler(
    postgres_database: object,
) -> None:
    seed = _Seed()
    _seed(postgres_database, seed)
    _delete(postgres_database, seed, "待删频道")

    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions() as session:
        character_row = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == seed.image_key)
        )
        assert character_row is not None
        assert character_row.state == "CLEANUP_PENDING"

    storage = _RecordingStorage({seed.image_key})
    removed = reconcile_staged_objects(sessions, storage, older_than_seconds=-1)  # type: ignore[arg-type]
    assert removed == 1
    assert storage.deleted == [seed.image_key]


def test_channel_deletion_rejects_wrong_confirmation_and_other_owner(
    postgres_database: object,
) -> None:
    seed = _Seed()
    _seed(postgres_database, seed)

    with pytest.raises(DomainError) as wrong:
        _delete(postgres_database, seed, "错误名称")
    assert wrong.value.code == "channel_deletion_confirmation_required"

    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with pytest.raises(DomainError) as stranger:
        SqlAlchemyChannelDeletionRepository(sessions.begin).delete_channel(
            uuid4(), seed.channel_id, "待删频道"
        )
    assert stranger.value.code == "not_found"

    with sessions() as session:
        assert session.get(ChannelRow, seed.channel_id) is not None
        assert session.get(CharacterImageRow, seed.image_id) is not None
        assert session.get(CoverCharacterCardRow, seed.card_id) is not None


def test_channel_deletion_uses_url_for_unnamed_channel(
    postgres_database: object,
) -> None:
    seed = _Seed()
    _seed(postgres_database, seed)
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        row = session.get(ChannelRow, seed.channel_id)
        assert row is not None
        row.name = None

    _delete(postgres_database, seed, URL)

    with sessions() as session:
        assert session.get(ChannelRow, seed.channel_id) is None


def test_channel_deletion_second_call_is_not_found(postgres_database: object) -> None:
    seed = _Seed()
    _seed(postgres_database, seed)
    _delete(postgres_database, seed, "待删频道")
    with pytest.raises(DomainError) as raised:
        _delete(postgres_database, seed, "待删频道")
    assert raised.value.code == "not_found"
