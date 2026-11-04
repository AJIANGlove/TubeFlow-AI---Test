"""故事板整图提示词构建：确定性网格布局声明 + 逐格视觉字段。

本模块是故事板生图提示词的唯一归属地。模板为英文脚手架（gpt-image 对英文
指令遵循更稳），镜头字段值保持项目语言。2026-11-03 起（USER_DECISIONS §44）
不再要求图像模型绘制 SHOT 角标，并禁止图内一切可读文字；镜头时间节拍只由
视频提示词表达。提示词只包含视觉字段；句子 ID、叙事功能等非视觉信息
不得进入图像提示词。

另含提交前的「违禁/高风险表达识别与规避」（2026-10-14）：对拼装完成的
提示词做确定性、可审计的最小替换，降低服务商内容审核拒绝率。规则与替换
证据见 runtime/operations/content_moderation_records.md §5。
"""

from __future__ import annotations

import re

from tubeflow_test.domain.storyboards import (
    GRID_LAYOUT_V1,
    GRID_LAYOUT_V2,
    REFERENCE_PRESERVATION_CLAUSE,
    DirectorShot,
    ShotTimeRange,
    grid_layout_for,
    panel_row_column,
)

# 提交前安全替换规则（storyboard-prompt-safety-v1）。
# 规则来源：服务商实际审核拒绝记录的复盘（content_moderation_records.md §5）：
# ① 未成年年龄标记（"the seventeen-year-old bride"）；② 身体准备/沐浴类措辞；
# ③ 惊悚/病理渲染词（disturbing / graphic / gory …）。
# 仅做确定性、可审计的最小替换；不改写镜头创意字段本身（数据库中的镜头字段
# 保持原样，只有生图提示词文本被处理），替换记录随生成输入持久化。
# v2（2026-11-03）：新增 style_reference_preservation_conflict，移除画面风格中
# "保留原图……仅转换……风格"子句（见 domain.storyboards.REFERENCE_PRESERVATION_CLAUSE）。
_PROMPT_SAFETY_RULE_VERSION = "storyboard-prompt-safety-v2"
_SAFETY_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("age_marker_hyphenated", re.compile(r"\b(?:\d+|[A-Za-z]+)-year-old\b\s?", re.I), ""),
    ("age_marker_plain", re.compile(r"\b(?:\d+|[A-Za-z]+)\s+year[- ]old\b\s?", re.I), ""),
    ("age_marker_zh", re.compile(r"\d{1,3}\s*岁的?"), ""),
    ("age_marker_zh_numeral", re.compile(r"[一二三四五六七八九十]{1,3}\s*岁的?"), ""),
    (
        "sensational_wording",
        re.compile(r"\b(?:disturbing|graphic|gory|grotesque|gruesome)\s+", re.I),
        "",
    ),
    (
        "body_ritual_wording",
        re.compile(r"\bbeing bathed\b", re.I),
        "receiving ceremonial cleansing",
    ),
    ("body_ritual_wording", re.compile(r"\bbathed\b", re.I), "ceremonially cleansed"),
    ("body_ritual_wording", re.compile(r"\bbathing\b", re.I), "ceremonial cleansing"),
    ("style_reference_preservation_conflict", REFERENCE_PRESERVATION_CLAUSE, ""),
)


def apply_prompt_safety(prompt: str) -> tuple[str, tuple[str, ...]]:
    """违禁/高风险表达识别与规避；返回（处理后提示词，命中的规则 ID）。

    确定性：同一输入恒得同一输出（幂等；重复应用不再命中）。规则只作用于
    生图提示词的文本，不改变镜头创意快照；命中记录随生成输入留档供审计。
    """
    applied: list[str] = []
    value = prompt
    for rule_id, pattern, replacement in _SAFETY_RULES:
        value, count = pattern.subn(replacement, value)
        if count:
            applied.append(rule_id)
    return value, tuple(sorted(set(applied)))


def build_grid_sheet_prompt(
    *,
    style: str,
    aspect_ratio: str,
    shots: tuple[DirectorShot, ...],
    ranges: tuple[ShotTimeRange, ...],
    character_count: int,
    layout_version: str = GRID_LAYOUT_V1,
) -> str:
    layout = grid_layout_for(len(shots), aspect_ratio, layout_version)
    parts: list[str] = []
    if layout.rows * layout.cols == 1:
        parts.append(
            "Generate exactly one storyboard image as a single full-frame panel with no grid lines."
        )
    elif layout_version == GRID_LAYOUT_V2:
        # §46：v2 每格会被逐格裁切为独立参考图；要求纯黑等宽分隔、无白框装饰线，
        # 以便确定性裁切（fa3a0b30 实测：手绘墨线、白黑相间边框会使旧裁切失败）。
        rows_text = _count(layout.rows, "row")
        cols_text = _count(layout.cols, "column")
        parts.append(
            f"Generate exactly one storyboard image laid out as a strict grid of "
            f"{rows_text} and {cols_text} of equal-sized panels. The panels are read "
            "left-to-right, top-to-bottom, and are separated by straight solid black "
            "gutters about 12 pixels wide. Do not draw white frames, borders, outlines "
            "or decorative lines around or inside the gutters; each panel's picture "
            "runs right up to the black gutter."
        )
        parts.extend(
            f"Panel {panel} must stay completely black and empty." for panel in layout.empty_panels
        )
    else:
        rows_text = _count(layout.rows, "row")
        cols_text = _count(layout.cols, "column")
        parts.append(
            f"Generate exactly one storyboard image laid out as a strict grid of "
            f"{rows_text} and {cols_text}. The panels are read left-to-right, "
            "top-to-bottom, and are separated by thin uniform black gutters."
        )
        parts.extend(
            f"Panel {panel} must stay completely black and empty." for panel in layout.empty_panels
        )
    parts.append(
        f"Art style for every panel: {style}. Keep the same rendering medium, "
        "palette, lighting and texture consistent across all panels."
    )
    if character_count > 0:
        parts.append(
            f"Reference images 1 to {character_count} are the story characters. "
            "Whenever a character appears in a panel, render the same person as "
            "in the reference images with an identical face, hairstyle and "
            "clothing in every panel."
        )
        # Pending issues/2026-11-03/STORYBOARD_REFERENCE_POSE_ROOT_CAUSE（用户已接受第 1 项）：
        # 参考图只提供身份；姿势、手势、表情、构图、机位、布局与物品位置由各格分镜决定。
        parts.append(
            "Use the reference images only for identity: face, hairstyle, body "
            "type and signature clothing. Do not copy their pose, gesture, facial "
            "expression, framing, camera angle, background, scene layout or object "
            "positions. Design each panel's pose, expression and composition anew "
            "from its Action and Visual description; those panel instructions take "
            "priority over every non-identity feature of the reference images. A "
            "reference image that shows one character from several angles is still "
            "one person; never draw more copies of a character than the panel's "
            "action calls for."
        )
    # ranges 仍参与长度校验（与镜头一一对应），但不再写入图像提示词。
    for shot, _shot_range in zip(shots, ranges, strict=True):
        row, column = panel_row_column(shot.order, layout.cols)
        parts.append(
            f"Panel {shot.order} (row {row}, column {column}): {shot.shot_size}, "
            f"{shot.camera_viewpoint}. Scene: {shot.scene}. Action: "
            f"{shot.subject_action_outcome}. Visual description: {shot.description}."
        )
    # 2026-11-03（USER_DECISIONS §44）：不再要求图像模型绘制 SHOT 角标，并禁止一切可读文字。
    # A/B 证据：故事板文字（含错误语言的金额）会被 H3 复制成乱码文字；角标为多格泄漏素材。
    # 镜头时间节拍仍由视频提示词的 cut_at 表达，不依赖图内角标。
    parts.append(
        "Do not draw any readable text anywhere in the image: no captions, panel "
        "numbers, labels, titles, words, letters, numbers, prices, currency signs, "
        "charts or tables with labels or figures, signboards with lettering, logos, "
        "watermarks or signatures. Show amounts, costs and comparisons through "
        "objects, gestures and expressions instead, such as a tall or short stack "
        "of coins, a thick or thin pile of bills, or a busy or empty shop."
    )
    return " ".join(parts)


def _count(value: int, noun: str) -> str:
    suffix = "" if value == 1 else "s"
    return f"{value} {noun}{suffix}"
