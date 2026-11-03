"""v5 角色绑定领域规则测试（计划 §1.2/§1.3 与 §6 测试矩阵领域行）。

覆盖：validate_character_card（正常/空/超长/非 str）、角色卡解析任务单全文、
绑定块注入有无、屏蔽规则（绑定时参考 subject 不拼接、非人物子项照常）、
多角色块、评委块注入、不绑定零回归（与 v4 逐字一致）。
"""

from __future__ import annotations

import pytest

from tubeflow_test.domain.cover import (
    CHARACTER_CARD_APPEARANCE_MAX,
    CHARACTER_CARD_IDENTITY_MAX,
    CharacterCardBinding,
    bind_character_cards,
    build_character_binding_system_block,
    build_character_binding_user_block,
    build_character_card_messages,
    build_cover_prompt_messages,
    build_judge_messages,
    validate_character_card,
)
from tubeflow_test.domain.errors import DomainError

CARD_A = CharacterCardBinding("原创角色小艾", "二次元画风，二头身，大眼睛，呆毛")
CARD_B = CharacterCardBinding("配角阿黄", "同画风柴犬，卷尾")

_PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgo="


def _png_image() -> dict[str, str]:
    return {"image_base64": "iVBORw0KGgo="}


# ---------- validate_character_card ----------


def test_validate_character_card_trims_normal_payload() -> None:
    result = validate_character_card({"identity": " 小艾 ", "appearance": " 二头身 "})
    assert result == {"identity": "小艾", "appearance": "二头身", "warnings": []}


@pytest.mark.parametrize("raw", [{}, {"identity": ""}, {"identity": "   "}, {"identity": 7}, "x"])
def test_validate_character_card_empty_identity_fails(raw: object) -> None:
    with pytest.raises(DomainError) as caught:
        validate_character_card(raw)
    assert caught.value.code == "cover_character_card_empty"


def test_validate_character_card_truncates_with_warnings() -> None:
    result = validate_character_card(
        {
            "identity": "身" * (CHARACTER_CARD_IDENTITY_MAX + 20),
            "appearance": "貌" * (CHARACTER_CARD_APPEARANCE_MAX + 20),
        }
    )
    assert len(result["identity"]) == CHARACTER_CARD_IDENTITY_MAX
    assert len(result["appearance"]) == CHARACTER_CARD_APPEARANCE_MAX
    assert result["warnings"] == [
        "cover_character_card_identity_truncated",
        "cover_character_card_appearance_truncated",
    ]


def test_validate_character_card_non_str_appearance_becomes_empty() -> None:
    result = validate_character_card({"identity": "小艾", "appearance": ["不是字符串"]})
    assert result == {"identity": "小艾", "appearance": "", "warnings": []}


# ---------- 角色卡解析任务单（§1.2 全文） ----------


def test_build_character_card_messages_verbatim_and_image_high_detail() -> None:
    messages = build_character_card_messages(_png_image())
    assert messages[0] == {
        "role": "system",
        "content": (
            "你是 IP 角色的核心特征提取员。只提取该角色跨场景不变的识别特征"
            "（身份、头身比例、面部特征、标志性元素），不描述服装、姿势、道具、场景等"
            "情境属性，不提取画风/渲染风格（渲染风格由频道风格档案统一决定），"
            "不评价、不虚构。输出严格 JSON，无解释、无 Markdown 围栏。"
        ),
    }
    content = messages[1]["content"]
    assert content[0] == {
        "type": "text",
        "text": (
            "提取这张 IP 角色图片的核心识别特征，输出 JSON：\n"
            '{ "identity": "<一句话：角色是谁/是什么，≤50 字>", '
            '"appearance": "<一段：头身比例、面部特征、标志性元素等跨场景不变的外形特征，'
            '不写画风/渲染风格，不写服装/姿势/道具/场景，≤300 字>" }'
        ),
    }
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": _PNG_DATA_URL, "detail": "high"},
    }


# ---------- 绑定块（§1.3 全文） ----------


def test_binding_blocks_empty_cards_inject_nothing() -> None:
    assert build_character_binding_system_block(()) == ""
    assert build_character_binding_user_block(()) == ""
    assert build_character_binding_system_block(None) == ""  # type: ignore[arg-type]


def test_binding_system_block_verbatim() -> None:
    block = build_character_binding_system_block((CARD_A,))
    assert block == (
        "【角色绑定】画面中的人物角色一律为本频道 IP 角色，其人物身份与外貌子项由系统逐字提供"
        '（你留空 ""）；参考图既定描述中出现的任何人物外形特征一律忽略，'
        "仅可参考其动作、表情、站位与构图；角色的服装、姿势与道具由你按标题/文案/封面文字的情境"
        "重新设计，与画面协调；角色的头身比例、面部特征与标志性元素必须保持不变；"
        "角色的渲染风格服从风格维度的既定描述，角色卡外貌文本中的画风表述不单独决定画面美术风格。"
    )


def test_binding_user_block_single_and_multi_cards() -> None:
    single = build_character_binding_user_block((CARD_A,))
    assert single == (
        "【出场角色（身份与外貌由系统逐字采用；服装/姿势/动作/表情由你按情境设计）】\n"
        "主角：原创角色小艾；二次元画风，二头身，大眼睛，呆毛"
    )
    multi = build_character_binding_user_block((CARD_A, CARD_B))
    assert multi.splitlines() == [
        "【出场角色（身份与外貌由系统逐字采用；服装/姿势/动作/表情由你按情境设计）】",
        "主角：原创角色小艾；二次元画风，二头身，大眼睛，呆毛",
        "配角：配角阿黄；同画风柴犬，卷尾",
    ]
    assert multi.splitlines()[2] == f"配角：{CARD_B.identity_text}；{CARD_B.appearance_text}"


# ---------- 屏蔽规则与拼接输入 ----------


def test_bind_character_cards_masks_subject_and_injects_first_card() -> None:
    dims, by_child = bind_character_cards(
        ["subject", "composition::留白"],
        {
            "subject::人物身份": "参考人物",
            "subject::外貌": "参考外貌",
            "subject::动作": "跑步",
            "composition::留白": "顶部留白",
        },
        (CARD_A, CARD_B),
    )
    # 参考 subject:: 全 5 子项不进入拼接；非人物子项不变；首卡注入两子项。
    assert dims == ["composition::留白", "subject::人物身份", "subject::外貌"]
    assert by_child == {
        "composition::留白": "顶部留白",
        "subject::人物身份": CARD_A.identity_text,
        "subject::外貌": CARD_A.appearance_text,
    }


def test_bind_character_cards_no_cards_returns_inputs() -> None:
    dims, by_child = bind_character_cards(["subject", "style"], {"subject::动作": "跑步"}, ())
    assert dims == ["subject", "style"]
    assert by_child == {"subject::动作": "跑步"}


# ---------- 画面任务单：绑定期行为与零回归 ----------

_REF_TEXT = "主体：人物身份：参考人物；外貌：参考外貌；动作：参考动作\n风格：艺术流派：写实摄影"
_ARGS = ("标题", "封面文字", "文案全文")
_REF_KW: dict[str, object] = {
    "ref_text": _REF_TEXT,
    "ref_dims": ["subject", "style"],
    "provided": ["subject::人物身份", "subject::外貌", "subject::动作", "style::艺术流派"],
}


def test_prompt_messages_unbound_is_v4_verbatim() -> None:
    default_call = build_cover_prompt_messages(*_ARGS, **_REF_KW)  # type: ignore[arg-type]
    explicit_empty = build_cover_prompt_messages(*_ARGS, character_cards=(), **_REF_KW)  # type: ignore[arg-type]
    assert default_call == explicit_empty
    system, user = default_call[0]["content"], default_call[1]["content"]
    assert "【角色绑定】" not in system
    assert "【出场角色" not in user
    # v4 豁免段照旧列出参考 subject 子项。
    assert "subject::人物身份" in system and "subject::动作" in system


def test_prompt_messages_bound_injects_blocks_and_masks_ref_subject() -> None:
    messages = build_cover_prompt_messages(*_ARGS, character_cards=(CARD_A, CARD_B), **_REF_KW)  # type: ignore[arg-type]
    system, user = messages[0]["content"], messages[1]["content"]
    # system：豁免段 +【角色绑定】段；参考 subject 子项只剩卡片两子项。
    assert "【参考图豁免】" in system
    assert "【角色绑定】" in system
    assert system.index("【参考图豁免】") < system.index("【角色绑定】")
    exempt = system.split("【参考图豁免】")[1].split("【角色绑定】")[0]
    assert "subject::人物身份" in exempt and "subject::外貌" in exempt
    assert "subject::动作" not in exempt and "subject::表情" not in exempt
    assert "subject::服装" not in exempt
    assert "style::艺术流派" in exempt  # 非人物子项照常
    # user：【出场角色】在【封面文字】之后、参考块之前；含配角行。
    i_cover = user.index("【封面文字")
    i_bound = user.index("【出场角色")
    i_ref = user.index("【参考图既定描述")
    assert i_cover < i_bound < i_ref
    assert f"主角：{CARD_A.identity_text}；{CARD_A.appearance_text}" in user
    assert f"配角：{CARD_B.identity_text}；{CARD_B.appearance_text}" in user
    # 参考文本仍整段注入 user 作上下文（屏蔽仅限拼接）。
    assert _REF_TEXT in user
    # dims 模板：卡片两子项 "" 占位，其余 subject 子项由 LLM 撰写。
    assert '"人物身份": ""' in user
    assert '"外貌": ""' in user
    assert '"动作": "<一句话，具体可执行>"' in user
    assert '"艺术流派": ""' in user


def test_prompt_messages_bound_without_ref_still_provides_card_children() -> None:
    messages = build_cover_prompt_messages(
        "标题", "封面文字", "文案全文", character_cards=(CARD_A,)
    )
    system, user = messages[0]["content"], messages[1]["content"]
    # 无参考文本时绑定仍生效：豁免段只列卡片两子项。
    assert "【参考图豁免】" in system
    assert "subject::人物身份" in system and "subject::外貌" in system
    assert "【角色绑定】" in system
    assert "【出场角色" in user
    assert "【参考图既定描述" not in user
    assert '"人物身份": ""' in user and '"外貌": ""' in user


# ---------- 评委任务单：绑定块注入与零回归 ----------


def _versions() -> list[dict[str, object]]:
    return [
        {"version": 1, "covers": [{"rank": 1, "forTitle": "标题", "prompt": "版本一"}]},
        {"version": 2, "covers": [{"rank": 1, "forTitle": "标题", "prompt": "版本二"}]},
    ]


def test_judge_messages_unbound_is_v4_verbatim() -> None:
    default_call = build_judge_messages(_versions(), "标题")
    explicit_empty = build_judge_messages(_versions(), "标题", character_cards=())
    assert default_call == explicit_empty
    user = default_call[1]["content"]
    assert user.startswith("【任务】评审 2 版")
    assert "\n\n【各版内容】\n" in user
    assert "\n\n输出 JSON：\n" in user
    assert "【出场角色" not in user


def test_judge_messages_bound_appends_cast_block() -> None:
    messages = build_judge_messages(_versions(), "标题", character_cards=(CARD_A, CARD_B))
    user = messages[1]["content"]
    assert "【出场角色（身份与外貌由系统逐字采用" in user
    assert "配角：" in user
    # 块位于各版内容之后、输出契约之前；rubric（system）不变。
    assert user.index("【各版内容】") < user.index("【出场角色") < user.index("输出 JSON")
    unbound_system = build_judge_messages(_versions(), "标题")[0]["content"]
    assert messages[0]["content"] == unbound_system
