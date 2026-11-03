from __future__ import annotations

import base64
import json

import pytest

from tubeflow_test.domain.cover import (
    AUDIT_RULE_KEYS,
    BANNED_DARK_WORDS,
    BANNED_ERA_WORDS,
    BANNED_FRAMING_WORDS,
    BANNED_STYLE_WORDS,
    COVER_RUBRIC,
    COVER_TEXT_FIELD_TEMPLATE,
    FRAMING_ANCHOR,
    KNEEL_WORDS,
    POLICY_BLOCK,
    QUALITY_ANCHOR,
    REF_CHILD_RULE_MAP,
    REF_DIMENSIONS,
    assemble_final_prompt,
    assemble_prompt_body,
    audit_cover_prompts,
    build_cover_prompt_messages,
    build_cover_text_messages,
    build_judge_messages,
    build_ref_analysis_messages,
    clean_banned_words,
    find_dark_words,
    image_data_url,
    normalize_cover_text,
    normalize_ref_dims,
    normalize_ref_selection,
    parse_ref_text_by_child,
    provided_child_ids,
    ref_rule_suppressions,
    render_ref_analysis_text,
    splice_ref_children,
    validate_cover_text_candidates,
    validate_covers,
    validate_ref_analysis,
    validate_verdict,
)
from tubeflow_test.domain.errors import DomainError

ALL_DIM_KEYS = [dim.key for dim in REF_DIMENSIONS]


def full_ai_dims() -> dict[str, dict[str, str]]:
    return {
        dim.key: {child: f"{dim.key}-{child}" for child in dim.children} for dim in REF_DIMENSIONS
    }


# ---------------------------------------------------------------------------
# 常量数据完整性（逐字移植防转录偏差）
# ---------------------------------------------------------------------------


def test_constants_catalog_integrity():
    assert [dim.key for dim in REF_DIMENSIONS] == [
        "subject",
        "spatial",
        "composition",
        "camera",
        "lighting",
        "color",
        "material",
        "style",
    ]
    assert [dim.label for dim in REF_DIMENSIONS] == [
        "主体",
        "空间",
        "构图",
        "镜头",
        "光影",
        "色彩",
        "材质",
        "风格",
    ]
    assert sum(len(dim.children) for dim in REF_DIMENSIONS) == 36
    assert len(REF_CHILD_RULE_MAP) == 36
    for dim in REF_DIMENSIONS:
        for child in dim.children:
            assert f"{dim.key}::{child}" in REF_CHILD_RULE_MAP
    assert "framing_anchor" in REF_CHILD_RULE_MAP["composition::人物占比"]
    assert REF_CHILD_RULE_MAP["composition::留白"] == ("no_text",)
    assert REF_CHILD_RULE_MAP["subject::表情"] == ()


def test_constants_word_lists_and_policy():
    assert len(BANNED_STYLE_WORDS) == 21
    assert len(BANNED_ERA_WORDS) == 30
    assert len(BANNED_FRAMING_WORDS) == 11
    assert len(BANNED_DARK_WORDS) == 46
    assert len(KNEEL_WORDS) == 12
    assert POLICY_BLOCK.startswith("【平台内容政策】")
    assert "【边界限定】" in POLICY_BLOCK
    assert POLICY_BLOCK.endswith("包括以 no/none/without 等否定形式出现。")
    assert FRAMING_ANCHOR == "画面主体完整入画，关键元素不被画面任何边缘裁切"
    assert QUALITY_ANCHOR == "主体突出，轮廓与边缘清晰，缩略图小尺寸下主体仍可一眼辨认"
    assert COVER_TEXT_FIELD_TEMPLATE.startswith("画面中加入标题文字「{cover_text}」：")
    assert [key for key, _ in COVER_RUBRIC] == [
        "visual_impact",
        "thumbnail_clarity",
        "title_match",
        "text_visual_match",
        "script_fidelity",
        "style_consistency",
        "technical_quality",
    ]


# ---------------------------------------------------------------------------
# 勾选规范化
# ---------------------------------------------------------------------------


def test_normalize_ref_dims_rejects_non_list():
    assert normalize_ref_dims(None) == []
    assert normalize_ref_dims("subject") == []
    assert normalize_ref_dims(123) == []
    assert normalize_ref_dims({"subject": True}) == []


def test_normalize_ref_dims_filters_unknown_and_dedupes():
    dims = [
        " subject::动作 ",
        "subject::动作",
        "unknown",
        "subject::未知子项",
        "subject::动作::多余",
        "",
        None,
        "color::主色",
    ]
    assert normalize_ref_dims(dims) == ["subject::动作", "color::主色"]


def test_normalize_ref_dims_whole_class_ignores_children():
    assert normalize_ref_dims(["subject::动作", "subject", "subject::服装"]) == ["subject"]


def test_normalize_ref_dims_catalog_order_and_no_collapse():
    every_child = [f"{dim.key}::{child}" for dim in REF_DIMENSIONS for child in dim.children]
    # 全部 36 子项勾选也不折叠为大类 key，且按分类表顺序输出
    assert normalize_ref_dims(list(reversed(every_child))) == every_child
    assert normalize_ref_dims(["style::时代感", "subject::表情", "camera::对焦"]) == [
        "subject::表情",
        "camera::对焦",
        "style::时代感",
    ]


def test_normalize_ref_selection_whole_and_partial():
    assert normalize_ref_selection(["subject", "color::主色"]) == [
        {"key": "subject", "label": "主体", "children": []},
        {"key": "color", "label": "色彩", "children": ["主色"]},
    ]
    assert normalize_ref_selection([]) == []


# ---------------------------------------------------------------------------
# 参考图解析：载荷、消息、校验、渲染、回读
# ---------------------------------------------------------------------------


def test_image_data_url_magic_bytes():
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0xxxx").decode()
    webp = base64.b64encode(b"RIFFxxxxWEBP").decode()
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    assert image_data_url(jpeg).startswith("data:image/jpeg;base64,")
    assert image_data_url(webp).startswith("data:image/webp;base64,")
    assert image_data_url(png).startswith("data:image/png;base64,")
    assert image_data_url("data:image/gif;base64,AA") == "data:image/gif;base64,AA"
    assert image_data_url("not base64 !!!") == "data:image/png;base64,not base64 !!!"
    assert image_data_url(jpeg, image_url="https://img.local/a.png") == "https://img.local/a.png"
    with pytest.raises(DomainError) as excinfo:
        image_data_url("")
    assert excinfo.value.code == "cover_ref_image_missing"


def test_build_ref_analysis_messages_full_selection():
    messages = build_ref_analysis_messages(
        {"image_base64": base64.b64encode(b"\xff\xd8\xff\x00").decode()}, ALL_DIM_KEYS
    )
    assert messages[0] == {
        "role": "system",
        "content": "你是视频封面参考图解析员。只客观描述图片的画面形态"
        "（构图、站位、光影、色彩、材质、风格等），不评价好坏、不虚构内容、"
        "不猜测人物真实身份与关系、不得输出内容解读。输出严格 JSON，无解释、无 Markdown 围栏。",
    }
    user = messages[1]
    assert user["role"] == "user"
    text_part, image_part = user["content"]
    assert text_part["type"] == "text"
    expected_url = image_data_url(base64.b64encode(b"\xff\xd8\xff\x00").decode())
    assert image_part == {
        "type": "image_url",
        "image_url": {"url": expected_url, "detail": "high"},
    }
    text = text_part["text"]
    assert text.startswith("解析这张视频封面参考图，仅按下列勾选维度输出中文描述")
    for dim in REF_DIMENSIONS:
        assert f'- "{dim.key}"（{dim.label}）：{"、".join(dim.children)}' in text
    assert "若画面含有显著标题文字，请在构图/风格的相关子项中描述其样式、颜色、描边与位置" in text
    expected_tail = '输出 JSON：{ "<维度key>": { "<子项>": "<描述>" } }，只包含上述维度与子项。'
    assert text.endswith(expected_tail)


def test_build_ref_analysis_messages_partial_selection():
    messages = build_ref_analysis_messages(
        {"image_url": "https://img.local/a.png"}, ["subject::动作", "color"]
    )
    text = messages[1]["content"][0]["text"]
    assert '- "subject"（主体）：动作' in text
    assert '- "color"（色彩）：主色、辅色、饱和度、色彩情绪' in text
    assert "spatial" not in text
    assert messages[1]["content"][1]["image_url"]["url"] == "https://img.local/a.png"


def test_validate_ref_analysis_drops_noise():
    raw = {
        "subject": {
            "动作": "  奔跑  ",
            "表情": "不明显",
            "服装": "x" * 250,
            "未知子项": "值",
        },
        "spatial": {"前景": "   "},
        "unknown_dim": {"a": "b"},
        "color": "not-a-dict",
    }
    result = validate_ref_analysis(raw, ["subject", "spatial::前景", "color"])
    assert result == {"subject": {"动作": "奔跑", "服装": "x" * 200}}


def test_validate_ref_analysis_empty_means_no_constraint():
    assert validate_ref_analysis({}, ALL_DIM_KEYS) == {}
    assert validate_ref_analysis("garbage", ALL_DIM_KEYS) == {}
    assert validate_ref_analysis({"subject": {"动作": "跑"}}, []) == {}


def test_render_and_parse_roundtrip():
    analysis = {
        "subject": {"动作": "奔跑", "表情": "惊讶"},
        "style": {"艺术流派": "写实"},
    }
    text = render_ref_analysis_text(analysis)
    assert text == "主体：动作：奔跑；表情：惊讶\n风格：艺术流派：写实"
    parsed = parse_ref_text_by_child(text, ["subject", "style"])
    assert parsed == {
        "ok": True,
        "by_child": {"subject::动作": "奔跑", "subject::表情": "惊讶", "style::艺术流派": "写实"},
    }


def test_render_ref_analysis_text_caps_at_2000():
    analysis = {"subject": {"动作": "长" * 3000}}
    text = render_ref_analysis_text(analysis)
    assert len(text) == 2000
    assert render_ref_analysis_text({}) == ""
    assert render_ref_analysis_text(None) == ""


def test_parse_ref_text_by_child_dim_line_invalid():
    # 勾选维度整行缺失 → 拒绝
    result = parse_ref_text_by_child("主体：动作：奔跑", ["subject", "style"])
    assert result == {"ok": False, "reason": "dim_line_invalid", "dim": "style"}
    # 勾选维度行内零有效子项 → 拒绝
    result = parse_ref_text_by_child("主体：不存在子项：值", ["subject"])
    assert result == {"ok": False, "reason": "dim_line_invalid", "dim": "subject"}
    result = parse_ref_text_by_child("主体：", ["subject"])
    assert result == {"ok": False, "reason": "dim_line_invalid", "dim": "subject"}
    # 行内个别勾选子项缺失不算损坏
    result = parse_ref_text_by_child("主体：动作：奔跑", ["subject::动作", "subject::服装"])
    assert result == {"ok": True, "by_child": {"subject::动作": "奔跑"}}


def test_parse_ref_text_by_child_notes_ignored_and_duplicates_overwrite():
    text = (
        "这是一句没有冒号的备注\n"
        "未知标签：这行维度不存在，忽略\n"
        "这是一个超过六字符的标签：忽略\n"
        "主体：动作：跑；动作：跳；未知子项：值；表情：\n"
        "风格：艺术流派：写实：带冒号的值"
    )
    parsed = parse_ref_text_by_child(text)
    assert parsed == {
        "ok": True,
        "by_child": {
            "subject::动作": "跳",
            "style::艺术流派": "写实：带冒号的值",
        },
    }


def test_parse_ref_text_by_child_empty_and_value_cap():
    assert parse_ref_text_by_child("") == {"ok": False, "reason": "empty"}
    assert parse_ref_text_by_child("   ", None) == {"ok": False, "reason": "empty"}
    parsed = parse_ref_text_by_child(f"主体：动作：{'值' * 300}")
    assert parsed["by_child"]["subject::动作"] == "值" * 200


# ---------------------------------------------------------------------------
# provided / splice / 让步并集
# ---------------------------------------------------------------------------


def test_provided_child_ids_intersection_and_expansion():
    by_child = {
        "subject::服装": "红裙",
        "subject::动作": "   ",
        "lighting::光源": "强光",
        "lighting::方向": "侧光",
        "style::时代感": "现代",
    }
    result = provided_child_ids(["subject", "lighting::光源", "material"], by_child)
    assert result == ["subject::服装", "lighting::光源"]


def test_ref_rule_suppressions_union():
    assert ref_rule_suppressions(["composition::留白"]) == {"no_text"}
    composition_rules = ref_rule_suppressions(["composition"])
    expected_rules = {"shot_diversity", "scene_dup", "framing_anchor", "framing_words", "no_text"}
    assert expected_rules <= composition_rules
    assert "framing_anchor" in ref_rule_suppressions(["camera::焦距"])
    assert ref_rule_suppressions(["unknown", "subject::不存在", None, ""]) == set()


def test_splice_ref_children_verbatim_overlay():
    dims = {
        "subject": {"动作": "AI动作", "服装": "AI服装"},
        "unknown_dim": {"x": 1},
    }
    by_child = {
        "subject::服装": "夜晚动漫（逐字不清洗）",
        "subject::外貌": "参考外貌",
        "style::时代感": "不在勾选范围",
    }
    out = splice_ref_children(dims, by_child, ["subject::服装", "subject::外貌"])
    assert out["subject"] == {
        "动作": "AI动作",
        "服装": "夜晚动漫（逐字不清洗）",
        "外貌": "参考外貌",
    }
    assert out["unknown_dim"] == {"x": 1}
    assert "style" not in out
    # 输入不被修改
    assert dims["subject"] == {"动作": "AI动作", "服装": "AI服装"}


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def test_assemble_prompt_body_appends_anchors_without_truncation():
    dims = {"subject": {"动作": "跑" * 3000}}
    body = assemble_prompt_body(dims)
    dim_text = render_ref_analysis_text(dims)
    assert len(dim_text) == 2000  # render 自身的 2000 码点上限
    # 装配不再截断：锚点完整追加在维度文本之后
    assert body == f"{dim_text}\n{FRAMING_ANCHOR}，{QUALITY_ANCHOR}"
    assert len(body) > 2000


def test_assemble_prompt_body_anchor_detection_and_skip():
    dims = {"subject": {"动作": "人物完整入画，主体一眼辨认"}}
    assert assemble_prompt_body(dims) == "主体：动作：人物完整入画，主体一眼辨认"
    dims = {"subject": {"动作": "关键元素不被任何边缘裁切"}}
    body = assemble_prompt_body(dims)
    assert FRAMING_ANCHOR not in body
    assert body.endswith(QUALITY_ANCHOR)
    dims = {"subject": {"动作": "普通描述"}}
    body = assemble_prompt_body(dims, skip={"framing_anchor"})
    assert FRAMING_ANCHOR not in body
    assert body.endswith(QUALITY_ANCHOR)
    # skip 不影响恒定质量锚点
    body = assemble_prompt_body(dims, skip={"framing_anchor", "quality"})
    assert body.endswith(QUALITY_ANCHOR)


def test_assemble_prompt_body_empty_dims():
    assert assemble_prompt_body({}) == f"{FRAMING_ANCHOR}，{QUALITY_ANCHOR}"


def test_assemble_final_prompt_embeds_cover_text_verbatim():
    body = assemble_prompt_body({"subject": {"动作": "奔跑"}})
    final = assemble_final_prompt(body, "震惊全场")
    assert final.startswith(body + "\n")
    assert "画面中加入标题文字「震惊全场」：大号醒目字体" in final
    assert final.endswith("除该文字外，画面中不得出现其他任何文字、字母、数字、招牌或水印")
    assert assemble_final_prompt("", "震惊全场").startswith("画面中加入标题文字「震惊全场」")


def test_reassemble_identity_on_candidate_switch():
    body = assemble_prompt_body({"subject": {"动作": "奔跑"}})
    first = assemble_final_prompt(body, "甲方案")
    second = assemble_final_prompt(body, "乙方案")
    expected_first = body + "\n" + COVER_TEXT_FIELD_TEMPLATE.replace("{cover_text}", "甲方案")
    expected_second = body + "\n" + COVER_TEXT_FIELD_TEMPLATE.replace("{cover_text}", "乙方案")
    assert first == expected_first
    assert second == expected_second
    # 换候选只改绘字段，prompt_body 前缀逐字节一致
    shared_prefix = body + "\n画面中加入标题文字「"
    assert first.startswith(shared_prefix)
    assert second.startswith(shared_prefix)


# ---------------------------------------------------------------------------
# 封面文字：候选校验与规整
# ---------------------------------------------------------------------------


def test_validate_cover_text_candidates_normal():
    raw = {
        "candidates": [
            {"text": "  震惊全场  ", "pattern": "宣告式"},
            {"text": "别再错了", "pattern": "否定式"},
            {"text": "震惊全场", "pattern": "重复"},
            {"text": "「就这？」\n", "pattern": "提问式"},
            {"text": "   ", "pattern": "空"},
        ],
        "recommended": 3,
    }
    result = validate_cover_text_candidates(raw, "标题")
    assert result == {
        "candidates": ["震惊全场", "别再错了", "就这？"],
        "recommended": 3,
        "warnings": [],
    }


def test_validate_cover_text_candidates_truncate_and_clamp():
    raw = {
        "candidates": [{"text": "长" * 35}, {"text": "正常"}],
        "recommended": 99,
    }
    result = validate_cover_text_candidates(raw, "标题")
    assert result["candidates"] == ["长" * 30, "正常"]
    assert result["recommended"] == 1
    assert result["warnings"] == ["cover_text_truncated"]
    raw["recommended"] = "2"
    assert validate_cover_text_candidates(raw, "标题")["recommended"] == 2
    raw["recommended"] = 0
    assert validate_cover_text_candidates(raw, "标题")["recommended"] == 1


def test_validate_cover_text_candidates_fallback_title():
    result = validate_cover_text_candidates({"candidates": []}, "项目标题")
    assert result == {
        "candidates": ["项目标题"],
        "recommended": 1,
        "warnings": ["cover_text_fallback_title"],
    }
    result = validate_cover_text_candidates(None, "长" * 40)
    assert result["candidates"] == ["长" * 30]
    assert result["warnings"] == ["cover_text_fallback_title"]
    result = validate_cover_text_candidates({"candidates": [{"text": "「」\n"}]}, "标题")
    assert result["candidates"] == ["标题"]


def test_normalize_cover_text():
    assert normalize_cover_text("  「震惊」\n全场  ", "标题") == ("震惊全场", [])
    assert normalize_cover_text("", "项目标题") == ("项目标题", ["cover_text_fallback_title"])
    assert normalize_cover_text("   ", "长" * 40) == ("长" * 30, ["cover_text_fallback_title"])
    assert normalize_cover_text("字" * 31, "标题") == ("字" * 30, ["cover_text_truncated"])


def test_build_cover_text_messages_conditional_blocks():
    messages = build_cover_text_messages(
        "我的标题", "文案全文内容", brief="要突出反差", language="zh-CN"
    )
    assert messages[0]["role"] == "system"
    assert "YouTube 爆款封面文案专家" in messages[0]["content"]
    assert POLICY_BLOCK in messages[0]["content"]
    user = messages[1]["content"]
    assert "【标题】\n我的标题" in user
    assert "【用户补充需求】\n要突出反差" in user
    assert "【文案全文（事实来源）】\n文案全文内容" in user
    assert "【项目语言】\nzh-CN" in user
    assert "务必正好 5 条，句式模式尽量互不相同。" in user

    messages = build_cover_text_messages("我的标题", "文案全文内容")
    user = messages[1]["content"]
    assert "【用户补充需求】" not in user
    assert "【项目语言】" in user


# ---------------------------------------------------------------------------
# 画面生成任务单与契约校验
# ---------------------------------------------------------------------------


def test_build_cover_prompt_messages_without_ref():
    messages = build_cover_prompt_messages(
        "标题", "震惊全场", "文案全文", extra_directive="  改成更亮  "
    )
    system, user = messages[0]["content"], messages[1]["content"]
    assert "【参考图豁免】" not in system
    assert "【参考图既定描述" not in user
    assert '"服装": ""' not in user
    assert '"服装": "<一句话，具体可执行>"' in user
    assert "标注为" not in user
    assert "【用户补充需求】" not in user
    assert "请为这个标题写 1 条封面描述" in user
    assert "你要为给定的标题、封面文字与文案写 1 条封面提示词。" in system
    assert POLICY_BLOCK in system
    cover_text_block = (
        "【封面文字（系统将要求图像模型逐字绘制，你不得撰写或改动；画面必须与其呼应）】\n震惊全场"
    )
    assert cover_text_block in user
    assert user.endswith("【定向整改要求】改成更亮")


def test_build_cover_prompt_messages_with_ref_and_brief():
    messages = build_cover_prompt_messages(
        "标题",
        "震惊全场",
        "文案全文",
        brief="突出夜景",
        ref_text="主体：服装：红裙",
        ref_dims=["subject::服装"],
        provided=["subject::服装"],
    )
    system, user = messages[0]["content"], messages[1]["content"]
    assert "【参考图豁免】" in system
    assert "- 主体·服装（subject::服装）" in system
    assert "【参考图既定描述（系统逐字采用）】\n主体：服装：红裙" in user
    assert "【用户补充需求】\n突出夜景" in user
    expected_line = (
        '      "subject": { "人物身份": "<一句话，具体可执行>", "外貌": "<一句话，具体可执行>", '
        '"动作": "<一句话，具体可执行>", "表情": "<一句话，具体可执行>", "服装": "" }'
    )
    assert expected_line in user
    expected_tail = (
        '务必正好输出 1 条封面，含完整 dims 八个维度。标注为 "" 的子项由系统提供，原样留空即可。'
    )
    assert user.endswith(expected_tail)


def test_build_cover_prompt_messages_ref_without_provided():
    messages = build_cover_prompt_messages("标题", "文字", "文案", ref_text="主体：服装：红裙")
    system, user = messages[0]["content"], messages[1]["content"]
    assert "【参考图豁免】" not in system
    assert "【参考图既定描述（系统逐字采用）】" in user
    assert '"服装": ""' not in user
    assert "标注为" not in user


def test_validate_covers_normal_and_cap():
    parsed = {
        "ratio": "9:16",
        "covers": [
            {"rank": 5, "forTitle": "  标题  ", "dims": full_ai_dims()},
            {"rank": 1, "forTitle": "多余", "dims": full_ai_dims()},
        ],
    }
    result = validate_covers(parsed, ratio="16:9")
    assert result["ratio"] == "9:16"
    assert len(result["covers"]) == 1
    assert result["covers"][0]["rank"] == 1
    assert result["covers"][0]["forTitle"] == "标题"
    assert result["covers"][0]["dims"] == full_ai_dims()
    assert result["needsRetry"] is False
    assert result["dimIssues"] == []


def test_validate_covers_ratio_fallback_and_for_title():
    result = validate_covers({"ratio": "4:3", "covers": [{"dims": full_ai_dims()}]}, ratio="1:1")
    assert result["ratio"] == "1:1"
    result = validate_covers({"covers": [{"dims": full_ai_dims()}]}, ratio="bad")
    assert result["ratio"] == "16:9"
    result = validate_covers({"covers": [{"forTitle": 123, "dims": full_ai_dims()}]})
    assert result["covers"][0]["forTitle"] == ""


def test_validate_covers_provided_ignores_model_values():
    dims = full_ai_dims()
    dims["subject"]["服装"] = "模型乱写的服装"
    parsed = {"covers": [{"dims": dims}]}
    result = validate_covers(parsed, provided_children={"subject::服装"})
    assert result["covers"][0]["dims"]["subject"] == {
        child: f"subject-{child}" for child in ("人物身份", "外貌", "动作", "表情")
    }
    assert result["needsRetry"] is False


def test_validate_covers_missing_dims_needs_retry():
    dims = full_ai_dims()
    del dims["camera"]
    del dims["style"]
    result = validate_covers({"covers": [{"dims": dims}]})
    assert result["needsRetry"] is True
    assert result["dimIssues"] == [{"rank": 1, "kind": "dims_missing", "dims": ["camera", "style"]}]
    # 缺失维度有 provided 子项 → 不算缺失
    result = validate_covers(
        {"covers": [{"dims": dims}]}, provided_children={"camera::焦距", "style::时代感"}
    )
    assert result["needsRetry"] is False
    assert result["dimIssues"] == []


def test_validate_covers_drop_empty_cover():
    result = validate_covers({"covers": [{"dims": {}}, {"dims": full_ai_dims()}]})
    assert len(result["covers"]) == 1
    assert result["covers"][0]["rank"] == 1
    assert result["needsRetry"] is False
    # 全部丢弃 → needsRetry
    result = validate_covers({"covers": [{"dims": {}}]})
    assert result["covers"] == []
    assert result["needsRetry"] is True
    # 无 AI 内容但 provided 非空 → 保留（dims 可为空 dict）
    result = validate_covers({"covers": [{"dims": {}}]}, provided_children={"subject::服装"})
    assert len(result["covers"]) == 1
    assert result["covers"][0]["dims"] == {}


# ---------------------------------------------------------------------------
# 评委
# ---------------------------------------------------------------------------


def two_versions() -> list[dict[str, object]]:
    return [
        {"version": 1, "covers": [{"rank": 1, "forTitle": "标题", "prompt": "提示词甲"}]},
        {"version": 2, "covers": [{"rank": 1, "forTitle": "标题", "prompt": "提示词乙"}]},
    ]


def test_build_judge_messages():
    messages = build_judge_messages(two_versions(), "标题")
    system, user = messages[0]["content"], messages[1]["content"]
    assert "你是视频封面（YouTube 缩略图）的严苛评审（评委）" in system
    for key, _desc in COVER_RUBRIC:
        assert f"- {key}（0-20 分）：" in system
    task_line = (
        "【任务】评审 2 版视频封面文生图提示词（每版 1 条，须兑现标题「标题」的钩子且与其信息互补）"
    )
    assert task_line in user
    payload_line = user.split("【各版内容】\n", 1)[1].split("\n", 1)[0]
    assert json.loads(payload_line) == two_versions()
    assert '"text_visual_match": <int 0-20>' in user
    assert user.endswith('"advice": "<对最优版仍存在的问题给出整改意见，中文；无问题则空串>"\n}')


def test_validate_verdict_clamp_total_and_best_fallback():
    raw = {
        "verdicts": [
            {
                "version": 1,
                "scores": {
                    "visual_impact": 25,
                    "thumbnail_clarity": -3,
                    "title_match": 15.7,
                    "text_visual_match": "abc",
                    "script_fidelity": 10,
                    "style_consistency": 10,
                    "technical_quality": 10,
                },
                "comment": "一般",
            },
            {"version": 2, "scores": {key: 20 for key, _ in COVER_RUBRIC}, "comment": "好"},
            {"version": 2, "scores": {key: 1 for key, _ in COVER_RUBRIC}, "comment": "重复忽略"},
            {"version": 9, "scores": {key: 20 for key, _ in COVER_RUBRIC}},
            "not-a-dict",
        ],
        "best": 99,
        "advice": 123,
    }
    result = validate_verdict(raw, 2)
    assert len(result["verdicts"]) == 2
    first = result["verdicts"][0]
    assert first["scores"]["visual_impact"] == 20
    assert first["scores"]["thumbnail_clarity"] == 0
    assert first["scores"]["title_match"] == 15
    assert first["scores"]["text_visual_match"] == 0
    assert first["total"] == 20 + 0 + 15 + 0 + 10 + 10 + 10
    assert result["verdicts"][1]["total"] == 140
    # best 非法 → 本地最高总分版（version 2 → 0 基 1）
    assert result["best"] == 1
    assert result["advice"] == ""


def test_validate_verdict_best_passthrough_and_tiebreak():
    raw = {
        "verdicts": [
            {"version": 1, "scores": {key: 10 for key, _ in COVER_RUBRIC}},
            {"version": 2, "scores": {key: 10 for key, _ in COVER_RUBRIC}},
        ],
        "best": 1,
        "advice": "继续改",
    }
    result = validate_verdict(raw, 2)
    assert result["best"] == 0
    assert result["advice"] == "继续改"
    # best 缺失且总分并列 → 取先出现版
    del raw["best"]
    assert validate_verdict(raw, 2)["best"] == 0
    assert validate_verdict({}, 2) == {"best": 0, "verdicts": [], "advice": ""}


# ---------------------------------------------------------------------------
# 审计与清理（激活矩阵）
# ---------------------------------------------------------------------------


def test_audit_cover_prompts_default_inactive():
    covers = [{"rank": 1, "prompt": "动漫风格，皇宫夜景，特写，下跪求饶"}]
    assert audit_cover_prompts(covers) == {"issues": [], "ok": True}
    expected_keys = {"style_words", "era_words", "dark_words", "framing_words", "kneel_limit"}
    assert set(AUDIT_RULE_KEYS) == expected_keys


def test_audit_cover_prompts_active_rules():
    covers = [
        {"rank": 1, "prompt": "日系 Anime 画风，皇宫大殿，特写镜头"},
        {"rank": 2, "prompt": "bright day, nightmare alley"},
    ]
    result = audit_cover_prompts(covers, active_rules={"style_words", "era_words", "framing_words"})
    kinds = {(issue["rank"], issue["kind"], issue["word"]) for issue in result["issues"]}
    assert (1, "banned_word", "anime") in kinds
    assert (1, "era_word", "皇宫") in kinds
    assert (1, "framing_word", "特写") in kinds
    assert result["ok"] is False
    # 夜暗未激活 → night 不报
    assert not any(issue["kind"] == "dark_word" for issue in result["issues"])


def test_audit_cover_prompts_dark_words_word_boundary():
    assert find_dark_words("nightmare on elm street") == []
    assert find_dark_words("a dark Night falls") == ["night", "dark night"]
    assert find_dark_words("夜幕降临，雨夜独行") == ["雨夜", "夜幕降临"]
    covers = [{"rank": 3, "prompt": "night 降临"}]
    result = audit_cover_prompts(covers, active_rules={"dark_words"})
    assert result["issues"] == [{"rank": 3, "kind": "dark_word", "word": "night"}]


def test_audit_cover_prompts_kneel_count():
    covers = [
        {"rank": 1, "prompt": "她跪地求饶"},
        {"rank": 2, "prompt": "他 on his knees"},
        {"rank": 3, "prompt": "正常对峙"},
    ]
    result = audit_cover_prompts(covers, active_rules={"kneel_limit"})
    assert result["issues"] == [{"rank": 0, "kind": "kneel_excess", "count": 2}]
    single = audit_cover_prompts(covers[:1], active_rules={"kneel_limit"})
    assert single == {"issues": [], "ok": True}


def test_clean_banned_words():
    assert clean_banned_words("日系 ANIME 风格，写实") == "日系 风格，写实"
    assert clean_banned_words("皇宫夜景", era_words=["皇宫"], dark_words=["夜景"]) == ""
    assert clean_banned_words("nightmare night", dark_words=["night"]) == "nightmare"
    assert clean_banned_words("特写，，  画面 ，", framing_words=["特写"]) == "画面"


def test_cover_text_latin_lint_word_budget_and_title_overlap():
    raw = {
        "candidates": [
            {"text": "Nine Months Zero Tools Now"},
            {"text": "NO PADS?"},
            {"text": "Ancient Women Pregnancy"},
            {"text": "How Did Survive"},
            {"text": "没有卫生巾"},
        ]
    }
    result = validate_cover_text_candidates(
        raw, "How Did Ancient Women Deal With Pregnancy and Childbirth?"
    )
    assert result["candidates"] == [
        "Nine Months Zero Tools Now",
        "NO PADS?",
        "Ancient Women Pregnancy",
        "How Did Survive",
        "没有卫生巾",
    ]
    assert result["warnings"] == ["cover_text_over_word_budget", "cover_text_title_overlap"]


def test_cover_text_lint_english_fallback_has_no_overlap_warning():
    result = validate_cover_text_candidates(
        {"candidates": []}, "How Did Ancient Women Deal With Pregnancy and Childbirth?"
    )
    assert result["warnings"] == ["cover_text_fallback_title"]


def test_build_ref_analysis_messages_pure_style_abstraction_block():
    messages = build_ref_analysis_messages(
        {"image_url": "https://img.local/a.png"}, ["style", "composition"]
    )
    text = messages[1]["content"][0]["text"]
    assert "【纯风格维度抽象化】" in text
    assert "禁止复述图中的具体场景元素" in text
    expected_tail = '输出 JSON：{ "<维度key>": { "<子项>": "<描述>" } }，只包含上述维度与子项。'
    assert text.endswith(expected_tail)


def test_build_ref_analysis_messages_no_abstraction_block_without_pure_style():
    messages = build_ref_analysis_messages(
        {"image_url": "https://img.local/a.png"}, ["subject", "color"]
    )
    text = messages[1]["content"][0]["text"]
    assert "【纯风格维度抽象化】" not in text
