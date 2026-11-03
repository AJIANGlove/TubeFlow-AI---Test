from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from tubeflow_test.domain.common import MediaAssetRef

_SPACE = re.compile(r"\s+")


def _normalized(value: str, field: str) -> str:
    result = _SPACE.sub(" ", value.strip()).casefold()
    if not result:
        raise ValueError(f"{field} must not be empty")
    return result


# §24（2026-10-14，由 5 修订为 6）：组级导演镜头数上限——每个最终配音组 1 至 N 个镜头，
# 与故事板生图网格容量一致。单一权威定义：域校验、确定性收敛、API 边界与前端均由此派生；
# 修改必须与生图网格声明（本模块 grid_layout_for）同版发布。
DIRECTOR_GROUP_SHOT_LIMIT = 6

_DEFAULT_GRID_ASPECT_RATIO = "16:9"
# §24（2026-10-14）：6 格起用 2×3（9:16 为 3×2）整格全用、不设留黑；
# 5 格维持 2×3（9:16 为 3×2）并以第 6 格纯黑留空（empty_panels 自动计算）。
_LANDSCAPE_GRIDS = {1: (1, 1), 2: (1, 2), 3: (1, 3), 4: (2, 2), 5: (2, 3), 6: (2, 3)}
_PORTRAIT_GRIDS = {1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (2, 2), 5: (3, 2), 6: (3, 2)}


@dataclass(frozen=True, slots=True)
class GridLayout:
    rows: int
    cols: int
    empty_panels: tuple[int, ...] = ()


def grid_layout_for(panel_count: int, aspect_ratio: str) -> GridLayout:
    ratio = aspect_ratio or _DEFAULT_GRID_ASPECT_RATIO
    if ratio == "16:9":
        table = _LANDSCAPE_GRIDS
    elif ratio == "9:16":
        table = _PORTRAIT_GRIDS
    else:
        raise ValueError(f"unsupported aspect ratio for grid layout: {aspect_ratio!r}")
    if panel_count not in table:
        raise ValueError(f"unsupported panel count for grid layout: {panel_count!r}")
    rows, cols = table[panel_count]
    capacity = rows * cols
    return GridLayout(rows, cols, tuple(range(panel_count + 1, capacity + 1)))


def panel_row_column(order: int, cols: int) -> tuple[int, int]:
    index = order - 1
    return index // cols + 1, index % cols + 1


# 2026-10-15 故事板双渠道故障转移：vip 不上线，剩余 7 模型共用每组每轮 14 次、
# 每模型 2 次的真实提交上限；退避 5s/15s/30s，429 尊重 Retry-After。
STORYBOARD_AUTO_MAX_SUBMISSIONS = 14
STORYBOARD_FAILOVER_MAX_MODEL_SUBMISSIONS = 2
STORYBOARD_FAILOVER_ROUTE_LENGTH = 7
STORYBOARD_AUTO_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 30.0)


def storyboard_auto_retry_delay_seconds(
    failed_attempt: int, retry_after_seconds: float | None = None
) -> float:
    """AUTO 项目第 n 次提交尝试失败后的退避秒数；429 时尊重 Retry-After（取较大者）。"""
    index = min(max(failed_attempt - 1, 0), len(STORYBOARD_AUTO_RETRY_BACKOFF_SECONDS) - 1)
    return max(STORYBOARD_AUTO_RETRY_BACKOFF_SECONDS[index], retry_after_seconds or 0.0)


def storyboard_auto_submission_budget_exhausted(attempt: int) -> bool:
    """该组当前授权链的提交预算（含自动重提）是否已用尽。"""
    return attempt >= STORYBOARD_AUTO_MAX_SUBMISSIONS


class DirectorRevisionKind(StrEnum):
    DRAFT = "DRAFT"
    APPLIED = "APPLIED"


class ReviewState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUIRED = "REQUIRED"
    APPROVED = "APPROVED"


class StoryboardAssetKind(StrEnum):
    GRID_SHEET = "GRID_SHEET"
    SINGLE_PANEL = "SINGLE_PANEL"


class GenerationStatus(StrEnum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    DOWNLOADED = "DOWNLOADED"
    QC_PASSED = "QC_PASSED"
    OBJECT_STORED = "OBJECT_STORED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DirectorSemanticEvent(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    ESTABLISH_CONTEXT = "ESTABLISH_CONTEXT"
    PERFORM_ACTION = "PERFORM_ACTION"
    SHOW_CONSEQUENCE = "SHOW_CONSEQUENCE"
    REVEAL_EVIDENCE = "REVEAL_EVIDENCE"
    EXPLAIN_CAUSE = "EXPLAIN_CAUSE"
    COMPARE = "COMPARE"
    COUNTERFACTUAL = "COUNTERFACTUAL"
    EXPRESS_UNCERTAINTY = "EXPRESS_UNCERTAINTY"
    TRANSITION = "TRANSITION"


class DirectorSubjectFocus(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    ENVIRONMENT = "ENVIRONMENT"
    PRIMARY_SUBJECT = "PRIMARY_SUBJECT"
    MULTIPLE_SUBJECTS = "MULTIPLE_SUBJECTS"
    OBJECT_OR_EVIDENCE = "OBJECT_OR_EVIDENCE"
    PROCESS = "PROCESS"
    ABSTRACT_CONCEPT = "ABSTRACT_CONCEPT"


class DirectorTimeRelation(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    SAME_MOMENT = "SAME_MOMENT"
    EARLIER = "EARLIER"
    LATER = "LATER"
    TIMELESS = "TIMELESS"


class DirectorLocationRelation(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    SAME_LOCATION = "SAME_LOCATION"
    SCRIPT_SUPPORTED_NEW_LOCATION = "SCRIPT_SUPPORTED_NEW_LOCATION"
    NON_SPATIAL = "NON_SPATIAL"


class DirectorShotSize(StrEnum):
    EXTREME_WIDE = "EXTREME_WIDE"
    WIDE = "WIDE"
    MEDIUM = "MEDIUM"
    CLOSE_UP = "CLOSE_UP"
    EXTREME_CLOSE_UP = "EXTREME_CLOSE_UP"
    NON_SPATIAL = "NON_SPATIAL"


class DirectorCameraViewpoint(StrEnum):
    EYE_LEVEL = "EYE_LEVEL"
    HIGH_ANGLE = "HIGH_ANGLE"
    LOW_ANGLE = "LOW_ANGLE"
    OVERHEAD = "OVERHEAD"
    OVER_SHOULDER = "OVER_SHOULDER"
    FIRST_PERSON = "FIRST_PERSON"
    OBJECTIVE = "OBJECTIVE"
    NON_SPATIAL = "NON_SPATIAL"


@dataclass(frozen=True, slots=True)
class ShotSentenceLink:
    shot_id: UUID
    sentence_id: UUID


@dataclass(frozen=True, slots=True)
class CharacterAssetSnapshot:
    ordinal: int
    character_image_id: UUID
    asset_id: UUID
    sha256: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if self.ordinal < 1 or len(self.sha256) != 64 or self.size_bytes <= 0:
            raise ValueError("character asset snapshot is invalid")
        if not self.media_type.startswith("image/"):
            raise ValueError("character asset snapshot must be an image")


@dataclass(frozen=True, slots=True)
class ContinuousObservationApproval:
    previous_shot_id: UUID
    next_shot_id: UUID
    reason: str

    def __post_init__(self) -> None:
        _normalized(self.reason, "continuous observation reason")
        if self.previous_shot_id == self.next_shot_id:
            raise ValueError("continuous observation approval must bind two shots")


@dataclass(frozen=True, slots=True)
class DirectorShot:
    shot_id: UUID
    order: int
    scene: str
    shot_size: str
    camera_viewpoint: str
    subject_action_outcome: str
    narrative_function: str
    description: str
    semantic_event: DirectorSemanticEvent = DirectorSemanticEvent.UNSPECIFIED
    subject_focus: DirectorSubjectFocus = DirectorSubjectFocus.UNSPECIFIED
    time_relation: DirectorTimeRelation = DirectorTimeRelation.UNSPECIFIED
    location_relation: DirectorLocationRelation = DirectorLocationRelation.UNSPECIFIED

    def __post_init__(self) -> None:
        if self.order < 1:
            raise ValueError("shot order must be positive")
        for field in (
            "scene",
            "shot_size",
            "camera_viewpoint",
            "subject_action_outcome",
            "narrative_function",
            "description",
        ):
            _normalized(getattr(self, field), field.replace("_", " "))
        for field, enum_type in (
            ("semantic_event", DirectorSemanticEvent),
            ("subject_focus", DirectorSubjectFocus),
            ("time_relation", DirectorTimeRelation),
            ("location_relation", DirectorLocationRelation),
        ):
            value = getattr(self, field)
            if not isinstance(value, enum_type):
                object.__setattr__(self, field, enum_type(value))

    @property
    def has_controlled_semantics(self) -> bool:
        return all(
            value.value != "UNSPECIFIED"
            for value in (
                self.semantic_event,
                self.subject_focus,
                self.time_relation,
                self.location_relation,
            )
        )

    def controlled_semantic_key(self) -> tuple[str, str, str, str] | None:
        if not self.has_controlled_semantics:
            return None
        return (
            self.semantic_event.value,
            self.subject_focus.value,
            self.time_relation.value,
            self.location_relation.value,
        )

    def event_repeat_key(self) -> tuple[str, ...]:
        """非相邻重复判定键（§22 精度修正）：受控语义四元组 + 归一化动作/功能文本。

        枚举词表粒度粗（PERFORM_ACTION 可涵盖多个不同活动），仅当分类相同
        且动作/叙事文本也相同时才构成真重复；无受控语义时保持纯文本对。
        """
        text_pair = (
            _normalized(self.subject_action_outcome, "action"),
            _normalized(self.narrative_function, "function"),
        )
        controlled = self.controlled_semantic_key()
        if controlled is not None:
            return (*controlled, *text_pair)
        return text_pair

    def changes_from(self, previous: DirectorShot) -> int:
        current_key = self.controlled_semantic_key()
        previous_key = previous.controlled_semantic_key()
        if current_key is not None and previous_key is not None:
            return sum(
                current != prior for current, prior in zip(current_key, previous_key, strict=True)
            )
        fields = (
            "scene",
            "shot_size",
            "camera_viewpoint",
            "subject_action_outcome",
            "narrative_function",
        )
        return sum(
            _normalized(getattr(self, field), field) != _normalized(getattr(previous, field), field)
            for field in fields
        )


@dataclass(frozen=True, slots=True)
class DirectorPlanRevision:
    revision_id: UUID
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    revision_number: int
    kind: DirectorRevisionKind
    shots: tuple[DirectorShot, ...]
    sentence_links: tuple[ShotSentenceLink, ...]
    group_sentence_ids: tuple[UUID, ...]
    review_state: ReviewState
    continuous_observation_approvals: tuple[ContinuousObservationApproval, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.revision_number < 1 or not 1 <= len(self.shots) <= DIRECTOR_GROUP_SHOT_LIMIT:
            raise ValueError(
                "a director plan must contain 1 to "
                f"{DIRECTOR_GROUP_SHOT_LIMIT} shots and a positive revision"
            )
        if [shot.order for shot in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("shot order must be stable, unique, and contiguous")
        shot_ids = {shot.shot_id for shot in self.shots}
        if len(shot_ids) != len(self.shots):
            raise ValueError("shot ids must be unique")
        if not self.group_sentence_ids or len(set(self.group_sentence_ids)) != len(
            self.group_sentence_ids
        ):
            raise ValueError("group sentence ids must be non-empty and unique")
        pairs = {(link.shot_id, link.sentence_id) for link in self.sentence_links}
        if len(pairs) != len(self.sentence_links):
            raise ValueError("sentence links must be unique")
        if {link.shot_id for link in self.sentence_links} != shot_ids:
            raise ValueError("every shot must link to at least one sentence")
        linked_sentences = {link.sentence_id for link in self.sentence_links}
        if not linked_sentences.issubset(set(self.group_sentence_ids)):
            raise ValueError("sentence link is outside this narration group")
        if linked_sentences != set(self.group_sentence_ids):
            raise ValueError("every sentence in the group must be covered")
        event_keys = [shot.event_repeat_key() for shot in self.shots]
        composition_keys = [
            (
                _normalized(shot.scene, "scene"),
                _normalized(shot.shot_size, "size"),
                _normalized(shot.camera_viewpoint, "viewpoint"),
            )
            for shot in self.shots
        ]

        def repeats_non_adjacent(values: Sequence[tuple[str, ...]]) -> bool:
            positions: dict[tuple[str, ...], int] = {}
            for index, value in enumerate(values):
                previous = positions.get(value)
                if previous is not None and index - previous > 1:
                    return True
                positions[value] = index
            return False

        if repeats_non_adjacent(event_keys) or repeats_non_adjacent(composition_keys):
            raise ValueError("director plan repeats an event or composition across the group")
        approved_pairs = {
            (item.previous_shot_id, item.next_shot_id)
            for item in self.continuous_observation_approvals
        }
        ambiguous_pairs = {
            (previous.shot_id, shot.shot_id)
            for previous, shot in zip(self.shots, self.shots[1:], strict=False)
            if shot.changes_from(previous) < 2
        }
        valid_adjacent_pairs = {
            (previous.shot_id, shot.shot_id)
            for previous, shot in zip(self.shots, self.shots[1:], strict=False)
        }
        if not approved_pairs.issubset(valid_adjacent_pairs):
            raise ValueError("continuous observation approval must bind an adjacent shot pair")
        ambiguous = bool(ambiguous_pairs - approved_pairs)
        if ambiguous and self.review_state is not ReviewState.REQUIRED:
            raise ValueError("adjacent shots require human diversity approval")
        if not ambiguous and self.review_state is ReviewState.REQUIRED:
            raise ValueError("human approval cannot remain required for an unambiguous plan")

    @property
    def may_apply(self) -> bool:
        return self.review_state is not ReviewState.REQUIRED


@dataclass(frozen=True, slots=True)
class ShotTimeRange:
    """组内相对实测配音区间（毫秒）；故事板印字与视频提示词节拍共用同一事实。"""

    shot_order: int
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.shot_order < 1 or self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("shot time range is invalid")

    @property
    def start_text(self) -> str:
        return _tenth_seconds_text(self.start_ms)

    @property
    def end_text(self) -> str:
        return _tenth_seconds_text(self.end_ms)


def shot_time_ranges_ms(
    shots: tuple[DirectorShot, ...],
    links: tuple[ShotSentenceLink, ...],
    sentence_ranges_ms: Mapping[UUID, tuple[int, int]],
) -> tuple[ShotTimeRange, ...]:
    """从句级实测时间轴推导每个分镜的组内相对区间（两侧必须使用同一函数）。

    legacy min/max 口径：每镜区间取其全部链接句的最早开始与最晚结束；镜头↔句子
    多对多且句序与镜序不一致时会产生嵌套/重叠区间。保留仅供历史记录自洽重算；
    新数据一律使用 projected_shot_time_ranges_ms（monotone-projection-v1）。
    """
    linked: dict[UUID, list[UUID]] = {}
    for link in links:
        linked.setdefault(link.shot_id, []).append(link.sentence_id)
    absolute: list[tuple[int, int]] = []
    for shot in shots:
        sentence_ids = linked.get(shot.shot_id)
        if not sentence_ids:
            raise ValueError("every shot must link to at least one sentence")
        spans: list[tuple[int, int]] = []
        for sentence_id in sentence_ids:
            span = sentence_ranges_ms.get(sentence_id)
            if span is None:
                raise ValueError("shot sentence is missing a measured time range")
            start_ms, end_ms = span
            if start_ms < 0 or end_ms <= start_ms:
                raise ValueError("sentence time range is invalid")
            spans.append((start_ms, end_ms))
        absolute.append((min(start for start, _ in spans), max(end for _, end in spans)))
    group_start_ms = min(start for start, _ in absolute)
    return tuple(
        ShotTimeRange(shot.order, start - group_start_ms, end - group_start_ms)
        for shot, (start, end) in zip(shots, absolute, strict=True)
    )


# 2026-10-16 用户决策：镜头↔句子多对多语义不变，新增确定性单调计时投影
# （算法版本标识随故事板生成输入持久化；历史记录无此键，按 legacy 口径自洽）。
SHOT_TIMING_PROJECTION_V1 = "monotone-projection-v1"


class ShotTimingProjectionError(ValueError):
    """单调计时投影无解的稳定信号（monotone-projection-v1）。

    与输入非法的 ValueError 区分：链接与量程本身合法，但不存在满足
    「每句恰好一个计时镜头、计时镜头序号按句序非降、每镜至少一句」的分配。
    调用方据此分流——重映射物化阶段自动相邻合并兜底；MANUAL 草稿阻止生成、
    转人工修复。继承 ValueError 以便既有粗略捕获不崩溃，分流时必须优先捕获本类。
    """

    def __init__(self) -> None:
        super().__init__("shot_timing_unresolvable")
        self.code = "shot_timing_unresolvable"


def projected_shot_time_ranges_ms(
    shots: tuple[DirectorShot, ...],
    links: tuple[ShotSentenceLink, ...],
    sentence_ranges_ms: Mapping[UUID, tuple[int, int]],
) -> tuple[ShotTimeRange, ...]:
    """单调计时投影：先为每句指定唯一计时镜头，再推导每镜组内相对区间。

    句序按 (start_ms, end_ms, sentence_id 字符串) 确定性排序；每句的候选计时镜头
    为链接该句的镜头；可行分配要求计时镜头位置按句序非降且每镜至少分到一句，
    取字典序最小可行分配保证唯一确定。投影后每镜句集在句序上连续、区间有序
    不重叠（相邻允许共享边界）。覆盖与量程校验沿用 legacy 语义；无解时抛
    ShotTimingProjectionError。
    """
    linked: dict[UUID, list[UUID]] = {}
    for link in links:
        linked.setdefault(link.shot_id, []).append(link.sentence_id)
    spans: dict[UUID, tuple[int, int]] = {}
    candidates: dict[UUID, list[int]] = {}
    for position, shot in enumerate(shots, start=1):
        sentence_ids = linked.get(shot.shot_id)
        if not sentence_ids:
            raise ValueError("every shot must link to at least one sentence")
        for sentence_id in sentence_ids:
            span = sentence_ranges_ms.get(sentence_id)
            if span is None:
                raise ValueError("shot sentence is missing a measured time range")
            start_ms, end_ms = span
            if start_ms < 0 or end_ms <= start_ms:
                raise ValueError("sentence time range is invalid")
            spans.setdefault(sentence_id, (start_ms, end_ms))
            positions = candidates.setdefault(sentence_id, [])
            if position not in positions:
                positions.append(position)
    ordered = tuple(sorted(spans, key=lambda value: (*spans[value], str(value))))
    assignment = _monotone_timing_assignment(
        tuple(tuple(sorted(candidates[value])) for value in ordered), len(shots)
    )
    if assignment is None:
        raise ShotTimingProjectionError()
    absolute: list[tuple[int, int]] = []
    index = 0
    for position in range(1, len(shots) + 1):
        block: list[tuple[int, int]] = []
        while index < len(ordered) and assignment[index] == position:
            block.append(spans[ordered[index]])
            index += 1
        absolute.append((min(start for start, _ in block), max(end for _, end in block)))
    if any(
        current[0] < previous[1] for previous, current in zip(absolute, absolute[1:], strict=False)
    ):
        # 句级区间自身重叠时投影区间可能交叉；实测时间轴不会，防御按无解分流。
        raise ShotTimingProjectionError()
    group_start_ms = min(start for start, _ in absolute)
    return tuple(
        ShotTimeRange(shot.order, start - group_start_ms, end - group_start_ms)
        for shot, (start, end) in zip(shots, absolute, strict=True)
    )


def _monotone_timing_assignment(
    candidates: tuple[tuple[int, ...], ...], shot_count: int
) -> tuple[int, ...] | None:
    """字典序最小的可行计时分配；无解返回 None。

    candidates[i] 为第 i 句（句序）可取的镜头位置升序元组；分配序列须非降且
    恰好覆盖 1..shot_count（每镜至少一句）。按候选升序深度优先，首个完整解即
    字典序最小；失败状态按 (句序号, 当前位置, 已覆盖位掩码) 记忆化。
    """
    full = (1 << shot_count) - 1
    failure: set[tuple[int, int, int]] = set()

    def search(index: int, previous: int, covered: int) -> tuple[int, ...] | None:
        if index == len(candidates):
            return () if covered == full else None
        state = (index, previous, covered)
        if state in failure:
            return None
        for position in candidates[index]:
            if position < previous:
                continue
            rest = search(index + 1, position, covered | (1 << (position - 1)))
            if rest is not None:
                return (position, *rest)
        failure.add(state)
        return None

    return search(0, 1, 0)


def shot_time_text(value: ShotTimeRange) -> str:
    """0.1 秒精度的区间文本，如 "0.0s–3.0s"；相邻镜头共享同一边界值。"""
    return f"{value.start_text}s–{value.end_text}s"


def format_shot_time_label(value: ShotTimeRange) -> str:
    """故事板格上印字与视频提示词节拍共用的镜头标签，如 "SHOT 1 · 0.0s–3.0s"。"""
    return f"SHOT {value.shot_order} · {shot_time_text(value)}"


def _tenth_seconds_text(milliseconds: int) -> str:
    tenths = (milliseconds + 50) // 100
    return f"{tenths // 10}.{tenths % 10}"


def shot_cut_point_text(milliseconds: int) -> str:
    """mm:ss.mmm 绝对切镜时刻文本（v2 提示词的 `[Shot n] At …, cut to` 使用），如 "00:08.700"。"""
    if milliseconds < 0:
        raise ValueError("cut point must not be negative")
    minutes, remainder = divmod(milliseconds, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


@dataclass(frozen=True, slots=True)
class StoryboardAsset:
    storyboard_asset_id: UUID
    generation_id: UUID
    kind: StoryboardAssetKind
    media: MediaAssetRef
    width: int
    height: int
    image_format: str

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or not self.image_format.strip():
            raise ValueError("storyboard image metadata is invalid")


@dataclass(frozen=True, slots=True)
class StoryboardGeneration:
    generation_id: UUID
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    applied_revision_id: UUID
    shots_snapshot: tuple[DirectorShot, ...]
    sentence_links_snapshot: tuple[ShotSentenceLink, ...]
    version: int
    kind: StoryboardAssetKind
    prompt: str
    character_assets_snapshot: tuple[CharacterAssetSnapshot, ...]
    style_snapshot: str
    model: str
    parameters: Mapping[str, str | int | float | bool]
    idempotency_key: str
    provider_idempotency_key: str
    canonical_request_sha256: str
    status: GenerationStatus
    created_at: datetime
    state_revision: int = 0
    provider_task_id: str | None = None
    provider_receipt: str | None = None
    download_token: str | None = None
    expected_sha256: str | None = None
    downloaded_sha256: str | None = None
    downloaded_size: int | None = None
    qc_width: int | None = None
    qc_height: int | None = None
    qc_mime: str | None = None
    object_asset: MediaAssetRef | None = None
    failure_stage: str | None = None
    failure_code: str | None = None
    asset: StoryboardAsset | None = None
    execution_token: UUID | None = None
    lease_until: datetime | None = None
    shot_time_ranges_snapshot: tuple[ShotTimeRange, ...] = ()
    # 区间计时算法版本键（随生成输入 JSON 持久化）：monotone-projection-v1 = 单调计时
    # 投影；空 = 历史记录（legacy min/max，无此键）。门禁按该键选择重算口径。
    shot_time_ranges_algorithm: str = ""
    # §15（仅 AUTO）：当前授权链内的提交尝试序号（首次生成为 1）与上一尝试血缘。
    auto_attempt: int = 1
    supersedes_generation_id: UUID | None = None
    # §15.9：提交预算按项目预算轮次计数；恢复后新授权轮次重新计 3 次，历史轮次保留审计。
    budget_round: int = 1
    # 提交前安全替换的命中规则 ID（storyboard-prompt-safety-v1，按序去重留档；
    # 空元组 = 未命中）。记录的是「识别与规避」的审计事实，不改变镜头创意快照。
    prompt_safety: tuple[str, ...] = ()
    # 2026-10-14：实际 Provider 与故障转移链绑定。空值仅表示 0036 前的历史记录。
    provider: str = ""
    failover_chain_id: UUID | None = None
    failover_policy_version: str | None = None
    failover_route_index: int | None = None
    failover_model_attempt: int | None = None
    failover_total_attempt: int | None = None
    # 初次提交回执单独保留，后续 poll 回执不得覆盖提交证据。
    submission_receipt: str | None = None
    recovery_failure_count: int = 0
    unknown_origin_stage: str | None = None
    unknown_origin_code: str | None = None

    def __post_init__(self) -> None:
        if self.version < 1 or not 1 <= len(self.shots_snapshot) <= DIRECTOR_GROUP_SHOT_LIMIT:
            raise ValueError("storyboard version or shot snapshot is invalid")
        if self.state_revision < 0:
            raise ValueError("generation state revision must not be negative")
        if self.recovery_failure_count < 0:
            raise ValueError("storyboard recovery failure count must not be negative")
        if self.auto_attempt < 1:
            raise ValueError("storyboard auto attempt must be positive")
        if self.budget_round < 1:
            raise ValueError("storyboard budget round must be positive")
        if self.provider:
            _normalized(self.provider, "provider")
        failover_values = (
            self.failover_chain_id,
            self.failover_policy_version,
            self.failover_route_index,
            self.failover_model_attempt,
            self.failover_total_attempt,
        )
        if any(value is not None for value in failover_values):
            if any(value is None for value in failover_values) or not self.provider:
                raise ValueError("storyboard failover lineage must be complete")
            route_index = self.failover_route_index
            model_attempt = self.failover_model_attempt
            total_attempt = self.failover_total_attempt
            if route_index is None or model_attempt is None or total_attempt is None:
                raise ValueError("storyboard failover lineage must be complete")
            if not 0 <= route_index < STORYBOARD_FAILOVER_ROUTE_LENGTH:
                raise ValueError("storyboard failover route index is invalid")
            if not 1 <= model_attempt <= STORYBOARD_FAILOVER_MAX_MODEL_SUBMISSIONS:
                raise ValueError("storyboard failover model attempt is invalid")
            if not 1 <= total_attempt <= STORYBOARD_AUTO_MAX_SUBMISSIONS:
                raise ValueError("storyboard failover total attempt is invalid")
            if self.auto_attempt != self.failover_total_attempt:
                raise ValueError("storyboard failover total attempt must match auto attempt")
            if not str(self.failover_policy_version).strip():
                raise ValueError("storyboard failover policy version must not be empty")
        if self.kind is not StoryboardAssetKind.GRID_SHEET:
            raise ValueError("SINGLE_PANEL generation is not available in the first release")
        for value, field in (
            (self.prompt, "storyboard prompt"),
            (self.style_snapshot, "style snapshot"),
            (self.model, "model"),
            (self.idempotency_key, "idempotency key"),
        ):
            _normalized(value, field)
        if len(self.canonical_request_sha256) != 64:
            raise ValueError("canonical request hash must be SHA-256")
        if not 1 <= len(self.character_assets_snapshot) <= 8 or [
            item.ordinal for item in self.character_assets_snapshot
        ] != list(range(1, len(self.character_assets_snapshot) + 1)):
            raise ValueError("a storyboard requires 1 to 8 unique character assets")
        if len({item.character_image_id for item in self.character_assets_snapshot}) != len(
            self.character_assets_snapshot
        ):
            raise ValueError("character asset ids must be unique")
        if len(self.provider_idempotency_key) != 64:
            raise ValueError("provider idempotency key must be an owner-scoped SHA-256")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        if self.status is GenerationStatus.SUCCEEDED and self.asset is None:
            raise ValueError("a successful generation must have an asset")
        if self.shot_time_ranges_snapshot and tuple(
            value.shot_order for value in self.shot_time_ranges_snapshot
        ) != tuple(shot.order for shot in self.shots_snapshot):
            raise ValueError("shot time ranges must align with the shot snapshot")
        if self.shot_time_ranges_algorithm and not self.shot_time_ranges_snapshot:
            raise ValueError("shot time ranges algorithm requires a ranges snapshot")

    @property
    def character_asset_ids(self) -> tuple[UUID, ...]:
        return tuple(item.character_image_id for item in self.character_assets_snapshot)


@dataclass(frozen=True, slots=True)
class VideoStoryboardBinding:
    video_id: UUID
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    storyboard_generation_id: UUID
    applied_revision_id: UUID
    storyboard_asset_sha256: str

    def __post_init__(self) -> None:
        if len(self.storyboard_asset_sha256) != 64:
            raise ValueError("video storyboard binding requires an asset SHA-256")
