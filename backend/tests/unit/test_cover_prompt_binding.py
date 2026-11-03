"""v5 提示词服务测试：角色绑定解析与编排、按风格版本生成、提示词版本切换。

覆盖（计划 §6 服务行）：默认绑定（项目快照角色图∩有卡、按快照顺序）、显式
[] 不绑定（零回归）、显式选择（顺序/归属/空卡过滤）、character_cards_used 血
缘、绑定期拼接优先级（卡 > AI > 参考）与屏蔽（参考 subject 零残留、非人物子
项照常）、绑定块/出场角色块注入任务单与评委、style_profile_id 归属校验与使
用、set_current 合法/非法候选。全部内存 fake，零网络。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.in_memory_cover_providers import FakeCoverPromptProvider
from tubeflow_test.application.cover_ports import (
    COVER_STATUS_SUCCEEDED,
    CoverCharacterCard,
    CoverStyleProfile,
)
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_prompts import CoverPromptService
from tubeflow_test.application.cover_providers import CoverLlmResult
from tubeflow_test.domain.cover import REF_DIMENSIONS
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
TITLE = "测试标题"
SCRIPT = "冻结文案全文，一字不截断。"


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeProjects:
    def __init__(self) -> None:
        self.contexts: dict[tuple[UUID, UUID], CoverProjectContext] = {}

    def add(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        channel_id: UUID | None = None,
        title: str = TITLE,
        script: str | None = SCRIPT,
        locale: str = "zh-CN",
        snapshot_character_image_ids: tuple[UUID, ...] = (),
    ) -> None:
        self.contexts[(owner_id, project_id)] = CoverProjectContext(
            project_id,
            channel_id or uuid4(),
            title,
            script,
            locale,
            snapshot_character_image_ids,
        )

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


def llm(payload: dict[str, Any]) -> CoverLlmResult:
    return CoverLlmResult(payload, f"receipt-{uuid4()}", "cover-model", "r" * 64)


def text_payload() -> dict[str, Any]:
    return {
        "candidates": [{"text": "钩子", "pattern": "句式"}],
        "recommended": 1,
    }


def full_dims(**overrides: str) -> dict[str, Any]:
    dims: dict[str, Any] = {}
    for dim in REF_DIMENSIONS:
        dims[dim.key] = {
            child: overrides.get(f"{dim.key}::{child}", f"{dim.label}{child}描述")
            for child in dim.children
        }
    return dims


def cover_payload(dims: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ratio": "16:9",
        "covers": [
            {"rank": 1, "forTitle": TITLE, "dims": dims if dims is not None else full_dims()}
        ],
    }


def judge_payload(best: int = 1) -> dict[str, Any]:
    scores = {key: 18 for key, _desc in (
        ("visual_impact", ""), ("thumbnail_clarity", ""), ("title_match", ""),
        ("text_visual_match", ""), ("script_fidelity", ""), ("style_consistency", ""),
        ("technical_quality", ""),
    )}
    return {
        "verdicts": [
            {"version": 1, "scores": scores, "comment": "好"},
            {"version": 2, "scores": scores, "comment": "也好"},
        ],
        "best": best,
        "advice": "",
    }


@pytest.fixture
def harness():
    repo = InMemoryCoverRepository()
    projects = FakeProjects()
    provider = FakeCoverPromptProvider()
    service = CoverPromptService(repo, projects, provider, Ids(), Clock(), model="cover-model")
    return repo, projects, provider, service


def _project(
    repo,
    projects: FakeProjects,
    owner_id: UUID,
    *,
    channel_id: UUID | None = None,
    snapshot_character_image_ids: tuple[UUID, ...] = (),
) -> UUID:
    project_id = uuid4()
    channel = channel_id or uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    repo.add_channel(owner_id, channel)
    projects.add(
        owner_id,
        project_id,
        channel_id=channel,
        snapshot_character_image_ids=snapshot_character_image_ids,
    )
    return project_id


def _card(
    repo,
    owner_id: UUID,
    channel_id: UUID,
    character_image_id: UUID,
    *,
    identity_text: str = "小艾",
    appearance_text: str = "二头身",
) -> CoverCharacterCard:
    return repo.upsert_character_card(
        CoverCharacterCard(
            uuid4(),
            owner_id,
            channel_id,
            character_image_id,
            identity_text,
            appearance_text,
            "manual",
            NOW,
            NOW,
        )
    )


def _seed_channel_profile(
    repo, owner_id: UUID, channel_id: UUID, *, dims: dict, edited_text: str, label: str = ""
) -> CoverStyleProfile:
    profile = repo.create_style_profile(
        CoverStyleProfile(
            uuid4(), owner_id, channel_id, 0, None, dims, {}, edited_text, NOW, None, label=label
        )
    )
    return repo.set_channel_current_style_profile(owner_id, channel_id, profile.id)


def _run(coro):
    return asyncio.run(coro)


def _queue_full_round(provider) -> None:
    provider.queue(llm(text_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload()))


# ---------- 默认绑定 / 显式选择 / 血缘 ----------


def test_create_default_binding_snapshot_order_and_lineage(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_a, image_b, image_c = uuid4(), uuid4(), uuid4()
    project_id = _project(
        repo,
        projects,
        owner_id,
        channel_id=channel_id,
        snapshot_character_image_ids=(image_b, image_c, image_a),
    )
    card_b = _card(repo, owner_id, channel_id, image_b, identity_text="角色B")
    card_a = _card(repo, owner_id, channel_id, image_a, identity_text="角色A")
    # image_c 无卡 → 跳过；顺序按快照 (B, C, A) → [B, A]。
    generation = service.create(owner_id, project_id, idempotency_key="k1")
    assert [item["card_id"] for item in generation.character_cards_used] == [
        str(card_b.id),
        str(card_a.id),
    ]
    assert [item["identity_text"] for item in generation.character_cards_used] == ["角色B", "角色A"]
    assert generation.character_cards_used[0]["character_image_id"] == str(image_b)

    # 无快照角色图 → 默认不绑定。
    plain_project = _project(repo, projects, owner_id, channel_id=channel_id)
    plain = service.create(owner_id, plain_project, idempotency_key="k2")
    assert plain.character_cards_used == []


def test_create_explicit_empty_list_disables_binding(harness) -> None:
    repo, projects, provider, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_id = uuid4()
    project_id = _project(
        repo, projects, owner_id, channel_id=channel_id,
        snapshot_character_image_ids=(image_id,),
    )
    _card(repo, owner_id, channel_id, image_id)
    generation = service.create(
        owner_id, project_id, character_card_ids=[], idempotency_key="k1"
    )
    assert generation.character_cards_used == []

    # 零回归：生成消息无绑定块（与 v4 行为一致）。
    _queue_full_round(provider)
    completed = _run(service.generate(owner_id, generation.id))
    assert completed.status == COVER_STATUS_SUCCEEDED
    for messages, _temperature in provider.calls:
        assert "【角色绑定】" not in str(messages[0].get("content"))
        assert "【出场角色" not in str(messages[-1].get("content"))


def test_create_explicit_selection_order_and_validation(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_a, image_b = uuid4(), uuid4()
    project_id = _project(repo, projects, owner_id, channel_id=channel_id)
    card_a = _card(repo, owner_id, channel_id, image_a, identity_text="角色A")
    card_b = _card(repo, owner_id, channel_id, image_b, identity_text="角色B")

    generation = service.create(
        owner_id,
        project_id,
        character_card_ids=[card_b.id, card_a.id],
        idempotency_key="k1",
    )
    assert [item["identity_text"] for item in generation.character_cards_used] == [
        "角色B",
        "角色A",
    ]

    with pytest.raises(DomainError) as missing:
        service.create(
            owner_id, project_id, character_card_ids=[uuid4()], idempotency_key="k2"
        )
    assert missing.value.code == "not_found"

    # 跨频道卡 → not_found。
    other_channel = uuid4()
    repo.add_channel(owner_id, other_channel)
    foreign = _card(repo, owner_id, other_channel, uuid4())
    with pytest.raises(DomainError) as wrong_channel:
        service.create(
            owner_id, project_id, character_card_ids=[foreign.id], idempotency_key="k3"
        )
    assert wrong_channel.value.code == "not_found"

    # 空 identity 卡视为无卡（照「快照角色无卡时不绑定」语义）。
    blank = _card(repo, owner_id, channel_id, uuid4(), identity_text="  ")
    blanked = service.create(
        owner_id, project_id, character_card_ids=[blank.id], idempotency_key="k4"
    )
    assert blanked.character_cards_used == []


# ---------- 绑定期编排：优先级 / 屏蔽 / 块注入 ----------


def test_generate_bound_splices_card_verbatim_and_masks_ref_subject(harness) -> None:
    repo, projects, provider, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    image_main, image_side = uuid4(), uuid4()
    project_id = _project(repo, projects, owner_id, channel_id=channel_id)
    main = _card(
        repo, owner_id, channel_id, image_main,
        identity_text="原创角色小艾", appearance_text="二次元二头身大眼睛",
    )
    side = _card(
        repo, owner_id, channel_id, image_side,
        identity_text="配角阿黄", appearance_text="同画风柴犬",
    )
    # 频道风格：subject（含人物身份/外貌/动作 provided）+ style。
    _seed_channel_profile(
        repo,
        owner_id,
        channel_id,
        dims={"selected": ["subject", "style"]},
        edited_text=(
            "主体：人物身份：参考人物；外貌：参考外貌；动作：参考动作；表情：参考表情；"
            "服装：参考服装\n风格：艺术流派：写实摄影"
        ),
    )
    generation = service.create(
        owner_id,
        project_id,
        character_card_ids=[main.id, side.id],
        idempotency_key="k1",
    )
    provider.queue(llm(text_payload()))
    # 模型撰写的人物身份/外貌必须被忽略（系统提供子项）。
    provider.queue(llm(cover_payload(full_dims(**{
        "subject::人物身份": "模型人物",
        "subject::外貌": "模型外貌",
        "subject::服装": "情境西装",
    }))))
    provider.queue(llm(cover_payload(full_dims(**{
        "subject::人物身份": "模型人物",
        "subject::外貌": "模型外貌",
        "subject::服装": "情境西装",
    }))))
    provider.queue(llm(judge_payload(best=1)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    body = completed.prompt_body
    # 优先级：卡 > AI > 参考——两子项逐字=首卡内容。
    assert "人物身份：原创角色小艾" in body
    assert "外貌：二次元二头身大眼睛" in body
    assert "模型人物" not in body and "模型外貌" not in body
    # 屏蔽：参考 subject 全 5 子项零残留。
    assert "参考人物" not in body and "参考外貌" not in body
    assert "参考动作" not in body and "参考表情" not in body and "参考服装" not in body
    # 非人物子项（style::艺术流派 provided）照常逐字拼接。
    assert "艺术流派：写实摄影" in body
    # 服装/动作/表情由 AI 按情境撰写（采用模型值）。
    assert "服装：情境西装" in body
    # 绑定块注入画面任务单（system + user）。
    variant_messages = [
        messages for messages, temperature in provider.calls if temperature in (0.8, 1.0)
    ]
    assert variant_messages
    for messages in variant_messages:
        assert "【角色绑定】" in str(messages[0].get("content"))
        user = str(messages[-1].get("content"))
        assert "【出场角色（身份与外貌由系统逐字采用" in user
        assert "主角：原创角色小艾；二次元二头身大眼睛" in user
        assert "配角：配角阿黄；同画风柴犬" in user
    # 评委 user 附【出场角色】块。
    judge_messages = [messages for messages, temperature in provider.calls if temperature == 0.2]
    assert judge_messages
    assert "【出场角色" in str(judge_messages[0][-1].get("content"))


# ---------- style_profile_id 按版本生成 ----------


def test_create_with_style_profile_id_uses_selected_version_text(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id, channel_id = uuid4(), uuid4()
    project_id = _project(repo, projects, owner_id, channel_id=channel_id)
    current = _seed_channel_profile(
        repo, owner_id, channel_id,
        dims={"selected": ["style"]}, edited_text="风格：艺术流派：写实摄影", label="当前",
    )
    # 旧版本（非当前指针）。
    older = repo.create_style_profile(
        CoverStyleProfile(
            uuid4(), owner_id, channel_id, 0, None,
            {"selected": ["style"]}, {}, "风格：艺术流派：水彩", NOW, None, label="旧版",
        )
    )
    assert older.id != current.id

    selected = service.create(
        owner_id, project_id, style_profile_id=older.id, idempotency_key="k1"
    )
    assert selected.style_source == "channel_profile"
    assert selected.channel_style_profile_id == older.id
    assert selected.style_text_used == "风格：艺术流派：水彩"

    # style_source=channel 显式 + 版本同样有效。
    explicit = service.create(
        owner_id, project_id,
        style_source="channel", style_profile_id=older.id, idempotency_key="k2",
    )
    assert explicit.style_text_used == "风格：艺术流派：水彩"

    # 组合非法：project/none 来源带版本。
    for source in ("project", "none"):
        with pytest.raises(DomainError) as invalid:
            service.create(
                owner_id, project_id,
                style_source=source, style_profile_id=older.id, idempotency_key=f"k-{source}",
            )
        assert invalid.value.code == "cover_style_source_invalid"
    # 跨频道/不存在版本 → invalid_cover_candidate。
    other_channel = uuid4()
    repo.add_channel(owner_id, other_channel)
    foreign = _seed_channel_profile(
        repo, owner_id, other_channel,
        dims={"selected": ["style"]}, edited_text="风格：艺术流派：油画",
    )
    for candidate in (uuid4(), foreign.id):
        with pytest.raises(DomainError) as unavailable:
            service.create(
                owner_id, project_id, style_profile_id=candidate,
                idempotency_key=f"k-{candidate}",
            )
        assert unavailable.value.code == "invalid_cover_candidate"


# ---------- set_current 版本切换 ----------


def test_set_current_switches_only_succeeded_project_candidates(harness) -> None:
    repo, projects, provider, service = harness
    owner_id, _channel_id = uuid4(), uuid4()
    project_id = _project(repo, projects, owner_id)
    first = service.create(owner_id, project_id, idempotency_key="k1")
    _queue_full_round(provider)
    completed = _run(service.generate(owner_id, first.id))
    assert completed.status == COVER_STATUS_SUCCEEDED

    second = service.create(owner_id, project_id, idempotency_key="k2")
    _queue_full_round(provider)
    completed_second = _run(service.generate(owner_id, second.id))
    current = repo.current_prompt_generation_for_project(owner_id, project_id)
    assert current is not None and current.id == completed_second.id

    switched = service.set_current(owner_id, project_id, completed.id)
    assert switched.id == completed.id
    current = repo.current_prompt_generation_for_project(owner_id, project_id)
    assert current is not None and current.id == completed.id

    # 未成功/不存在/其他项目候选 → invalid_cover_candidate。
    pending = service.create(owner_id, project_id, idempotency_key="k3")
    with pytest.raises(DomainError) as not_succeeded:
        service.set_current(owner_id, project_id, pending.id)
    assert not_succeeded.value.code == "invalid_cover_candidate"
    with pytest.raises(DomainError) as missing:
        service.set_current(owner_id, project_id, uuid4())
    assert missing.value.code == "invalid_cover_candidate"
    other_project = _project(repo, projects, owner_id)
    with pytest.raises(DomainError) as foreign:
        service.set_current(owner_id, other_project, completed.id)
    assert foreign.value.code == "invalid_cover_candidate"
    with pytest.raises(DomainError) as missing_project:
        service.set_current(owner_id, uuid4(), completed.id)
    assert missing_project.value.code == "not_found"
