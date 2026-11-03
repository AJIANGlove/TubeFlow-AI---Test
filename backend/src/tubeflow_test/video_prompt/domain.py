"""H3 视频原生提示词编译的领域事实与纯校验。

只含值对象与纯函数：不依赖 Web 框架、数据库、Temporal、HTTP 客户端或任何
Provider SDK。DeepSeek 报文与提示词模板文本分别位于适配器层与模板层。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from tubeflow_test.domain.storyboards import (
    DirectorShot,
    ShotTimeRange,
    format_shot_time_label,
    grid_layout_for,
    panel_row_column,
    shot_cut_point_text,
)

MAX_COMPILED_PROMPT_CHARS = 20000

SECTION_ORDER = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)

_DASHES = "-–—"


class VideoPromptStrategy(StrEnum):
    KINESTACK_V6_PORT = "kinestack_v6_port"
    TUBEFLOW_ENHANCED_V1 = "tubeflow_enhanced_v1"
    TUBEFLOW_STORYBOARD_V2 = "tubeflow_storyboard_v2"
    TUBEFLOW_STORYBOARD_V3 = "tubeflow_storyboard_v3"
    TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1 = (
        "tubeflow_storyboard_v3_single_panel_opt_in_v1"
    )
    # v4：输出合同与 v3 完全一致（同一出口校验、绑定行与格位），只改写系统提示词中的
    # 运镜与画面自然度规则（运镜白名单、默认稳定机位、真实速度、禁止画内文字）。
    TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION = "tubeflow_storyboard_v4_natural_motion"


# 共享 v3 输出合同（出口校验、绑定行、格位注入）的策略集合。
V3_CONTRACT_STRATEGIES = frozenset(
    {
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3,
        VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION,
    }
)


class VideoPromptViolation(ValueError):
    """稳定的提示词合同违例（str 为可公开的原因码）。"""


@dataclass(frozen=True, slots=True)
class VideoPromptCompilerInput:
    """编译一次组级视频提示词所需的全部确定性事实。"""

    character_labels: tuple[str, ...]
    storyboard_label: str
    shots: tuple[DirectorShot, ...]
    shot_ranges: tuple[ShotTimeRange, ...]
    duration_seconds: int
    aspect_ratio: str
    visual_style: str
    strategy: VideoPromptStrategy
    idempotency_key: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.character_labels) <= 8:
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if len(set(self.character_labels)) != len(self.character_labels):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not self.storyboard_label.strip() or self.storyboard_label in self.character_labels:
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not self.shots or not self.shot_ranges:
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if [shot.order for shot in self.shots] != list(range(1, len(self.shots) + 1)):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if tuple(value.shot_order for value in self.shot_ranges) != tuple(
            shot.order for shot in self.shots
        ):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not 3 <= self.duration_seconds <= 15:
            raise VideoPromptViolation("video_prompt_duration_out_of_range")
        if not self.aspect_ratio.strip() or not self.visual_style.strip():
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not self.idempotency_key.strip():
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")

    @property
    def reference_labels(self) -> tuple[str, ...]:
        return (*self.character_labels, self.storyboard_label)

    @property
    def fingerprint(self) -> str:
        """编译输入决定性事实的 SHA-256，用于装配点二次一致性校验。"""
        return hashlib.sha256(
            json.dumps(
                {
                    "character_labels": list(self.character_labels),
                    "storyboard_label": self.storyboard_label,
                    "shots": [
                        {
                            "order": shot.order,
                            "scene": shot.scene,
                            "shot_size": shot.shot_size,
                            "camera_viewpoint": shot.camera_viewpoint,
                            "subject_action_outcome": shot.subject_action_outcome,
                            "narrative_function": shot.narrative_function,
                            "description": shot.description,
                        }
                        for shot in self.shots
                    ],
                    "shot_ranges": [
                        [value.shot_order, value.start_ms, value.end_ms]
                        for value in self.shot_ranges
                    ],
                    "duration_seconds": self.duration_seconds,
                    "aspect_ratio": self.aspect_ratio,
                    "visual_style": self.visual_style,
                    "strategy": self.strategy.value,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SinglePanelReference:
    label: str
    shot_order: int
    source_storyboard_sha256: str
    derivative_sha256: str
    derivation: str

    def __post_init__(self) -> None:
        if self.shot_order < 1 or not self.label.strip() or not self.derivation.strip():
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_storyboard_sha256):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", self.derivative_sha256):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")


@dataclass(frozen=True, slots=True)
class SinglePanelPromptCompilerInput:
    """Independent opt-in input; composite v3 fields and fingerprint are untouched."""

    character_labels: tuple[str, ...]
    panels: tuple[SinglePanelReference, ...]
    shots: tuple[DirectorShot, ...]
    shot_ranges: tuple[ShotTimeRange, ...]
    duration_seconds: int
    aspect_ratio: str
    visual_style: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.character_labels) <= 8:
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not self.panels or not self.shots or not self.shot_ranges:
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        labels = (*self.character_labels, *(panel.label for panel in self.panels))
        if len(set(labels)) != len(labels) or any(not label.strip() for label in labels):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        orders = [shot.order for shot in self.shots]
        if orders != list(range(1, len(self.shots) + 1)):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if [panel.shot_order for panel in self.panels] != orders:
            raise VideoPromptViolation("h3_prompt_single_panel_mapping_missing")
        if tuple(value.shot_order for value in self.shot_ranges) != tuple(orders):
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if len(self.character_labels) + len(self.panels) > 9:
            raise VideoPromptViolation("video reference image limit exceeded")
        if not 3 <= self.duration_seconds <= 15:
            raise VideoPromptViolation("video_prompt_duration_out_of_range")
        if not self.aspect_ratio.strip() or not self.visual_style.strip():
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")
        if not self.idempotency_key.strip():
            raise VideoPromptViolation("video_prompt_compiler_input_incomplete")

    @property
    def strategy(self) -> VideoPromptStrategy:
        return VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "character_labels": list(self.character_labels),
                    "panels": [
                        {
                            "label": panel.label,
                            "shot_order": panel.shot_order,
                            "source_storyboard_sha256": panel.source_storyboard_sha256,
                            "derivative_sha256": panel.derivative_sha256,
                            "derivation": panel.derivation,
                        }
                        for panel in self.panels
                    ],
                    "shots": [
                        {
                            "order": shot.order,
                            "scene": shot.scene,
                            "shot_size": shot.shot_size,
                            "camera_viewpoint": shot.camera_viewpoint,
                            "subject_action_outcome": shot.subject_action_outcome,
                            "narrative_function": shot.narrative_function,
                            "description": shot.description,
                        }
                        for shot in self.shots
                    ],
                    "shot_ranges": [
                        [value.shot_order, value.start_ms, value.end_ms]
                        for value in self.shot_ranges
                    ],
                    "duration_seconds": self.duration_seconds,
                    "aspect_ratio": self.aspect_ratio,
                    "visual_style": self.visual_style,
                    "strategy": self.strategy.value,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class CompiledVideoPrompt:
    text: str
    strategy: VideoPromptStrategy
    model: str
    provider_receipt: str
    repaired: bool
    input_fingerprint: str


def validate_compiled_prompt(text: str, *, expected: VideoPromptCompilerInput) -> None:
    """按策略版本校验提示词全文（对最终文本调用，含已插入的绑定行）。"""
    if not text.strip():
        raise VideoPromptViolation("h3_prompt_structure_invalid")
    if len(text) > MAX_COMPILED_PROMPT_CHARS:
        raise VideoPromptViolation("h3_prompt_too_long")
    positions = _section_positions(text)
    bodies = _section_bodies(text, positions)
    v3_contract = expected.strategy in V3_CONTRACT_STRATEGIES
    if expected.strategy is not VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2 and not v3_contract:
        _validate_timeline_header(bodies["detailed_description"], expected.duration_seconds)
    if expected.strategy is VideoPromptStrategy.TUBEFLOW_ENHANCED_V1:
        _validate_shot_labels(bodies["detailed_description"], expected.shot_ranges)
    if expected.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2:
        _validate_storyboard_v2(bodies, expected)
    if v3_contract:
        _validate_storyboard_v3(bodies, expected)
    if expected.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION:
        _validate_v4_pronouns(bodies["detailed_description"])
    storyboard_ordinal = len(expected.reference_labels)
    for ordinal in range(1, storyboard_ordinal + 1):
        # v3 主体定义禁止图片来源指代：角色图用途由确定性绑定行表达，
        # 正文中只强制故事板 Picture N 出现（retention 条目另行强制其格式）。
        if v3_contract and ordinal != storyboard_ordinal:
            continue
        if f"Picture {ordinal}" not in text:
            raise VideoPromptViolation("h3_prompt_reference_missing")
    if "<d>" in text or "</d>" in text:
        raise VideoPromptViolation("h3_prompt_dialogue_forbidden")
    if bodies["non_diegetic_music"].strip() != "N/A":
        raise VideoPromptViolation("h3_prompt_music_forbidden")


def render_binding_line(value: VideoPromptCompilerInput) -> str:
    if value.strategy in V3_CONTRACT_STRATEGIES:
        # v3 绑定行表达用途：角色图仅身份参考；故事板按同号格映射到镜头。
        bindings = [
            f"Character reference {index} = {label} (identity reference only)"
            for index, label in enumerate(value.character_labels, 1)
        ]
        bindings.append(
            f"Multi-panel storyboard shot plan = {value.storyboard_label} "
            "(each shot renders the content of its same-numbered panel only)"
        )
    else:
        bindings = [
            f"Character reference {index} = {label}"
            for index, label in enumerate(value.character_labels, 1)
        ]
        bindings.append(f"Multi-panel storyboard shot plan = {value.storyboard_label}")
    return "visual_asset_bindings: " + "; ".join(bindings) + "."


def insert_binding_line(text: str, value: VideoPromptCompilerInput) -> str:
    """把确定性绑定行插到 subject_definitions 段首行之后（复刻 KineStack 渲染口径）。"""
    match = re.search(r"(?m)^\s*subject_definitions\s*:.*$", text)
    if match is None:
        raise VideoPromptViolation("h3_prompt_structure_invalid")
    return text[: match.end()] + "\n" + render_binding_line(value) + text[match.end() :]


def render_single_panel_binding_line(value: SinglePanelPromptCompilerInput) -> str:
    bindings = [
        f"Character reference {index} = <{label}> (identity reference only)"
        for index, label in enumerate(value.character_labels, 1)
    ]
    bindings.extend(
        f"Shot {panel.shot_order} panel reference = <{panel.label}>"
        for panel in value.panels
    )
    return (
        "visual_asset_bindings: "
        + "; ".join(bindings)
        + " (each shot renders only the content of its corresponding single-panel reference)."
    )


def insert_single_panel_binding_line(
    text: str, value: SinglePanelPromptCompilerInput
) -> str:
    if "visual_asset_bindings:" in text:
        raise VideoPromptViolation("h3_prompt_binding_line_unexpected")
    match = re.search(r"(?m)^\s*subject_definitions\s*:.*$", text)
    if match is None:
        raise VideoPromptViolation("h3_prompt_structure_invalid")
    return (
        text[: match.end()]
        + "\n"
        + render_single_panel_binding_line(value)
        + text[match.end() :]
    )


def validate_single_panel_compiled_prompt(
    text: str,
    *,
    expected: SinglePanelPromptCompilerInput,
    binding_inserted: bool,
) -> None:
    """Validate the opt-in output before and after deterministic binding insertion."""
    if not text.strip():
        raise VideoPromptViolation("h3_prompt_structure_invalid")
    if len(text) > MAX_COMPILED_PROMPT_CHARS:
        raise VideoPromptViolation("h3_prompt_too_long")
    bodies = _section_bodies(text, _section_positions(text))
    if text.count("visual_asset_bindings:") != (1 if binding_inserted else 0):
        raise VideoPromptViolation("h3_prompt_binding_line_invalid")
    if binding_inserted and render_single_panel_binding_line(expected) not in text:
        raise VideoPromptViolation("h3_prompt_binding_line_invalid")
    for forbidden in ("Multi-panel storyboard shot plan", "The complete multi-panel storyboard"):
        if forbidden in text:
            raise VideoPromptViolation("h3_prompt_composite_reference_forbidden")
    subject = bodies["subject_definitions"]
    for line in subject.splitlines():
        if re.match(r"\s*-?\s*\(S\d+\)", line) and re.search(
            r"Picture\s*\d+|@图[一二三四五六七八九]", line
        ):
            raise VideoPromptViolation("h3_prompt_image_provenance_forbidden")
    retention = bodies["retention_analysis"]
    detailed = bodies["detailed_description"]
    for panel in expected.panels:
        order = panel.shot_order
        if (
            f"- {panel.label}: The single-panel storyboard reference for Shot {order}"
            not in subject
        ):
            raise VideoPromptViolation("h3_prompt_single_panel_definition_missing")
        if (
            f"- {panel.label} — shot_plan_reference_only: "
            f"retain Shot {order} composition and style;"
            not in retention
        ):
            raise VideoPromptViolation("h3_prompt_single_panel_retention_missing")
        if re.search(
            rf"-\s*{re.escape(panel.label)}\s*—\s*fully_preserved", retention
        ):
            raise VideoPromptViolation("h3_prompt_single_panel_retention_missing")
        shot_range = expected.shot_ranges[order - 1]
        if order == 1:
            prefix = f"[Shot 1] <{panel.label}>:"
        else:
            cut_at = shot_cut_point_text(shot_range.start_ms)
            prefix = f"[Shot {order}] At {cut_at}, cut to <{panel.label}>:"
        if prefix not in detailed:
            raise VideoPromptViolation("h3_prompt_single_panel_mapping_missing: " + prefix)
    blocks = list(re.finditer(r"\[Shot (\d+)\]", detailed))
    orders = [int(match.group(1)) for match in blocks]
    if orders != [shot.order for shot in expected.shots]:
        raise VideoPromptViolation("h3_prompt_shot_blocks_invalid")
    for index, match in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(detailed)
        block = detailed[match.end() : end]
        order = int(match.group(1))
        for other in expected.panels:
            if other.shot_order != order and other.label in block:
                raise VideoPromptViolation("h3_prompt_cross_shot_reference_forbidden")
    if "single-panel references" not in bodies["summary"] or "shot order" not in bodies["summary"]:
        raise VideoPromptViolation("h3_prompt_single_panel_summary_missing")
    if "Maintain " not in detailed or "No storyboard grid" not in detailed:
        raise VideoPromptViolation("h3_prompt_negative_rules_missing")
    if "split screen" not in detailed or "text" not in detailed or "watermarks" not in detailed:
        raise VideoPromptViolation("h3_prompt_negative_rules_missing")
    if "<d>" in text or "</d>" in text:
        raise VideoPromptViolation("h3_prompt_dialogue_forbidden")
    if bodies["non_diegetic_music"].strip() != "N/A":
        raise VideoPromptViolation("h3_prompt_music_forbidden")
    if not bodies["overall_soundscape"].strip():
        raise VideoPromptViolation("h3_prompt_audio_constraints_invalid")


def _section_positions(text: str) -> dict[str, re.Match[str]]:
    positions: dict[str, re.Match[str]] = {}
    for name in SECTION_ORDER:
        matches = list(re.finditer(rf"(?m)^\s*{name}\s*:", text))
        if len(matches) != 1:
            raise VideoPromptViolation("h3_prompt_structure_invalid")
        positions[name] = matches[0]
    ordered = [positions[name].start() for name in SECTION_ORDER]
    if ordered != sorted(ordered):
        raise VideoPromptViolation("h3_prompt_structure_invalid")
    return positions


def _section_bodies(text: str, positions: dict[str, re.Match[str]]) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for index, name in enumerate(SECTION_ORDER):
        end = (
            positions[SECTION_ORDER[index + 1]].start()
            if index + 1 < len(SECTION_ORDER)
            else len(text)
        )
        bodies[name] = text[positions[name].end() : end]
    return bodies


def _validate_timeline_header(detailed_body: str, duration_seconds: int) -> None:
    pattern = rf"^\s*\[0(\.0+)?s\s*[{_DASHES}]\s*{duration_seconds}(\.0+)?s\]"
    if re.search(pattern, detailed_body) is None:
        raise VideoPromptViolation("h3_prompt_timeline_incomplete")


def _validate_shot_labels(
    detailed_body: str, shot_ranges: tuple[ShotTimeRange, ...]
) -> None:
    for value in shot_ranges:
        pattern = (
            rf"SHOT\s+{value.shot_order}\s*·\s*"
            rf"{re.escape(value.start_text)}s\s*[{_DASHES}]\s*{re.escape(value.end_text)}s"
        )
        if re.search(pattern, detailed_body) is None:
            raise VideoPromptViolation("h3_prompt_shot_range_missing")


def _validate_storyboard_v2(
    bodies: dict[str, str], expected: VideoPromptCompilerInput
) -> None:
    """v2 合同：故事板正面定义、逐镜 [Shot n] 块、绝对切镜点、一致性与负规则收尾。"""
    if "Treat each panel as a separate chronological shot beat" not in (
        bodies["subject_definitions"]
    ):
        raise VideoPromptViolation("h3_prompt_storyboard_framing_missing")
    detailed = bodies["detailed_description"]
    blocks = list(re.finditer(r"\[Shot (\d+)\]", detailed))
    actual_orders = [int(match.group(1)) for match in blocks]
    expected_orders = [shot.order for shot in expected.shots]
    if actual_orders != expected_orders:
        # 修复重试必须把真实差异交回模型，否则只能收到笼统错误并重复同类输出。
        raise VideoPromptViolation(
            "h3_prompt_shot_blocks_invalid: "
            f"expected={expected_orders}, actual={actual_orders}"
        )
    for value in expected.shot_ranges[1:]:
        cut_point = f"At {shot_cut_point_text(value.start_ms)}, cut to"
        if cut_point not in detailed:
            raise VideoPromptViolation("h3_prompt_cut_point_missing")
    if "Maintain " not in detailed:
        raise VideoPromptViolation("h3_prompt_consistency_checklist_missing")
    if "No storyboard grid" not in detailed or "split screen" not in detailed:
        raise VideoPromptViolation("h3_prompt_negative_rules_missing")


_GENDERED_PRONOUN = re.compile(r"\b(?:he|she|him|her|his|hers)\b", re.IGNORECASE)


def _validate_v4_pronouns(detailed_body: str) -> None:
    """v4 角色只按 (Sx) 或 the person 指代（2026-11-03 用户决策 5）。

    性别代词会让看不到图片的 H3 画错人（attempt ed495263 用 he 而参考图是女性）。
    只作用于 v4；该错误码即编译修复原因（compiler 按错误码重试一次）。
    """
    match = _GENDERED_PRONOUN.search(detailed_body)
    if match:
        raise VideoPromptViolation(
            f"h3_prompt_gendered_pronoun_forbidden: {match.group(0).lower()}"
        )


def _validate_storyboard_v3(
    bodies: dict[str, str], expected: VideoPromptCompilerInput
) -> None:
    """v3 合同：复用 v2 全部检查，新增镜头↔格子映射、来源指代禁令与故事板留存条目格式。

    绑定行插入前后各跑一次：两次都必须成立（绑定行在来源指代检查中剔除）。
    """
    _validate_storyboard_v2(bodies, expected)
    detailed = bodies["detailed_description"]
    layout = grid_layout_for(len(expected.shots), expected.aspect_ratio)
    expected_panels: list[str] = []
    for shot in expected.shots:
        row, column = panel_row_column(shot.order, layout.cols)
        expected_panels.append(f"Panel {shot.order} (row {row}, column {column})")
    actual_panels = re.findall(r"Panel \d+ \(row \d+, column \d+\)", detailed)
    if actual_panels != expected_panels:
        raise VideoPromptViolation(
            "h3_prompt_panel_mapping_missing: "
            f"expected={expected_panels}, actual={actual_panels}"
        )
    storyboard_ordinal = len(expected.reference_labels)
    subject_lines = [
        line
        for line in bodies["subject_definitions"].splitlines()
        if re.match(r"\s*visual_asset_bindings\s*:", line) is None
        and re.match(rf"\s*-?\s*Picture\s*{storyboard_ordinal}\s*[:：]", line) is None
    ]
    if re.search(r"Picture\s*\d+", "\n".join(subject_lines)):
        raise VideoPromptViolation("h3_prompt_image_provenance_forbidden")
    retention = bodies["retention_analysis"]
    if f"- Picture {storyboard_ordinal} — shot_plan_reference_only:" not in retention:
        raise VideoPromptViolation("h3_prompt_storyboard_retention_invalid")
    if re.search(rf"-\s*Picture\s*\d+\s*[{_DASHES}]\s*fully_preserved", retention):
        raise VideoPromptViolation("h3_prompt_storyboard_retention_invalid")


def assert_shot_labels_present(text: str, shot_ranges: tuple[ShotTimeRange, ...]) -> None:
    """供测试与调用方使用的精确标签断言（逐字一致，含 en dash）。"""
    for value in shot_ranges:
        if format_shot_time_label(value) not in text:
            raise VideoPromptViolation("h3_prompt_shot_range_missing")
