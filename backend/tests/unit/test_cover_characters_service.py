"""封面角色卡服务测试：建/改校验、显式解析 202 意图与解析编排（全部内存 fake）。

覆盖：频道/角色图归属校验、字段上限稳定码、每图一卡 upsert、解析意图落
outbox 与在途去重、run_card_analysis 成功覆盖（source=analysis、消息合同）、
identity 空稳定失败码（卡不被破坏）、存储完整性失败。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.in_memory_cover_providers import FakeCoverRefAnalysisProvider
from tubeflow_test.application.cover_characters import CoverCharacterService
from tubeflow_test.application.cover_outbox import (
    COVER_AGGREGATE_CHARACTER_CARD,
    COVER_CHARACTER_CARD_ANALYSIS_REQUESTED,
)
from tubeflow_test.application.cover_ports import CoverCharacterImageRef
from tubeflow_test.application.cover_providers import CoverLlmResult
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.objects[key]
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("integrity mismatch")
        return body


def png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def harness():
    repo = InMemoryCoverRepository()
    storage = FakeStorage()
    analysis = FakeCoverRefAnalysisProvider()
    service = CoverCharacterService(repo, repo, storage, analysis, Ids(), Clock())
    return repo, storage, analysis, service


def _seed(
    repo: InMemoryCoverRepository,
    storage: FakeStorage,
    owner_id: UUID,
    channel_id: UUID,
    *,
    content: bytes | None = None,
) -> UUID:
    repo.add_channel(owner_id, channel_id)
    payload = content if content is not None else png_bytes()
    image_id = uuid4()
    object_key = f"character-images/{owner_id}/{image_id}"
    storage.objects[object_key] = payload
    repo.add_character_image(
        CoverCharacterImageRef(
            image_id,
            owner_id,
            channel_id,
            object_key,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "image/png",
        )
    )
    return image_id


def test_upsert_card_validates_scope_ownership_and_lengths(harness) -> None:
    repo, _storage, _analysis, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_id = _seed(repo, _storage, owner_id, channel_id)

    card = service.upsert_card(
        owner_id,
        channel_id,
        character_image_id=image_id,
        identity_text=" 小艾 ",
        appearance_text=" 二头身 ",
    )
    assert card.identity_text == "小艾" and card.appearance_text == "二头身"
    assert card.source == "manual"

    with pytest.raises(DomainError) as missing_channel:
        service.upsert_card(owner_id, uuid4(), character_image_id=image_id)
    assert missing_channel.value.code == "not_found"
    with pytest.raises(DomainError) as missing_image:
        service.upsert_card(owner_id, channel_id, character_image_id=uuid4())
    assert missing_image.value.code == "not_found"
    with pytest.raises(DomainError) as foreign_owner:
        service.upsert_card(uuid4(), channel_id, character_image_id=image_id)
    assert foreign_owner.value.code == "not_found"
    # 角色图属其他频道。
    other_channel = uuid4()
    repo.add_channel(owner_id, other_channel)
    with pytest.raises(DomainError) as wrong_channel:
        service.upsert_card(owner_id, other_channel, character_image_id=image_id)
    assert wrong_channel.value.code == "not_found"
    with pytest.raises(DomainError) as identity_long:
        service.upsert_card(
            owner_id, channel_id, character_image_id=image_id, identity_text="长" * 101
        )
    assert identity_long.value.code == "cover_character_card_identity_too_long"
    with pytest.raises(DomainError) as appearance_long:
        service.upsert_card(
            owner_id, channel_id, character_image_id=image_id, appearance_text="长" * 501
        )
    assert appearance_long.value.code == "cover_character_card_appearance_too_long"


def test_upsert_card_is_per_image_and_editable_to_blank(harness) -> None:
    repo, _storage, _analysis, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_id = _seed(repo, _storage, owner_id, channel_id)
    first = service.upsert_card(
        owner_id, channel_id, character_image_id=image_id, identity_text="v1"
    )
    # 同图再提交 = 改卡（§9.6 可编辑清空文本，不做 DELETE）。
    second = service.upsert_card(owner_id, channel_id, character_image_id=image_id)
    assert second.id == first.id
    assert second.identity_text == "" and second.source == "manual"
    assert [item.id for item in service.list_cards(owner_id, channel_id)] == [first.id]
    with pytest.raises(DomainError) as caught:
        service.list_cards(owner_id, uuid4())
    assert caught.value.code == "not_found"


def test_analysis_intent_enqueues_and_dedupes_in_flight(harness) -> None:
    repo, _storage, _analysis, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_id = _seed(repo, _storage, owner_id, channel_id)
    card = service.upsert_card(owner_id, channel_id, character_image_id=image_id)

    intent = service.create_card_analysis_intent(owner_id, channel_id, card.id)
    assert intent.enqueued is True
    assert intent.card.id == card.id
    assert intent.workflow_id.startswith(f"cover-character-card-analysis-{owner_id}-{card.id}-")
    (event,) = tuple(repo.outbox.values())
    assert event.event_type == COVER_CHARACTER_CARD_ANALYSIS_REQUESTED
    assert event.aggregate_type == COVER_AGGREGATE_CHARACTER_CARD
    assert event.aggregate_id == card.id
    assert event.project_id is None

    again = service.create_card_analysis_intent(owner_id, channel_id, card.id)
    assert again.enqueued is False
    assert len(repo.outbox) == 1

    with pytest.raises(DomainError) as missing_card:
        service.create_card_analysis_intent(owner_id, channel_id, uuid4())
    assert missing_card.value.code == "not_found"
    with pytest.raises(DomainError) as wrong_channel:
        service.create_card_analysis_intent(owner_id, uuid4(), card.id)
    assert wrong_channel.value.code == "not_found"
    with pytest.raises(DomainError) as foreign_owner:
        service.create_card_analysis_intent(uuid4(), channel_id, card.id)
    assert foreign_owner.value.code == "not_found"


def test_run_card_analysis_overwrites_card_with_validated_result(harness) -> None:
    repo, _storage, analysis, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_id = _seed(repo, _storage, owner_id, channel_id)
    card = service.upsert_card(
        owner_id, channel_id, character_image_id=image_id, identity_text="手写身份"
    )
    analysis.queue(
        CoverLlmResult(
            {"identity": " 原创角色小艾 ", "appearance": " 二次元二头身 "}, "r1", "m", "a" * 64
        )
    )

    assert service.run_card_analysis(owner_id, card.id) == "SUCCEEDED"

    stored = repo.get_character_card(owner_id, card.id)
    assert stored is not None
    assert stored.identity_text == "原创角色小艾"
    assert stored.appearance_text == "二次元二头身"
    assert stored.source == "analysis"
    # 解析消息：角色卡任务单 + 图片 high detail。
    (messages,) = analysis.calls
    assert messages[0]["role"] == "system"
    assert "IP 角色的核心特征提取员" in str(messages[0]["content"])
    user_content = messages[1]["content"]
    assert user_content[0]["type"] == "text"
    assert user_content[1]["image_url"]["detail"] == "high"
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_run_card_analysis_empty_identity_fails_without_touching_card(harness) -> None:
    repo, _storage, analysis, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_id = _seed(repo, _storage, owner_id, channel_id)
    card = service.upsert_card(
        owner_id, channel_id, character_image_id=image_id, identity_text="保留我"
    )
    analysis.queue(
        CoverLlmResult(
            {"identity": "  ", "appearance": "只有外貌"}, "r1", "m", "a" * 64
        )
    )

    with pytest.raises(DomainError) as caught:
        service.run_card_analysis(owner_id, card.id)
    assert caught.value.code == "cover_character_card_empty"
    stored = repo.get_character_card(owner_id, card.id)
    assert stored is not None and stored.identity_text == "保留我"
    assert stored.source == "manual"


def test_run_card_analysis_storage_integrity_failure(harness) -> None:
    repo, storage, _analysis, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_id = _seed(repo, storage, owner_id, channel_id)
    card = service.upsert_card(owner_id, channel_id, character_image_id=image_id)
    # 破坏存储内容使哈希校验失败。
    key = repo.character_images[image_id].object_key
    storage.objects[key] = b"corrupted"

    with pytest.raises(DomainError) as caught:
        service.run_card_analysis(owner_id, card.id)
    assert caught.value.code == "storage_integrity_error"

    with pytest.raises(DomainError) as missing:
        service.run_card_analysis(owner_id, uuid4())
    assert missing.value.code == "not_found"
