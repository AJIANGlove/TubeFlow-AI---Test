from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from tubeflow_test.domain.storyboards import (
    SHOT_TIMING_PROJECTION_V1,
    ContinuousObservationApproval,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    ReviewState,
    ShotSentenceLink,
    ShotTimingProjectionError,
    StoryboardAssetKind,
    projected_shot_time_ranges_ms,
    shot_time_ranges_ms,
)


def shot(
    order: int, sentence: UUID, *, same: bool = False
) -> tuple[DirectorShot, ShotSentenceLink]:
    item = DirectorShot(
        uuid4(),
        order,
        "room" if same else f"scene {order}",
        "wide",
        "eye",
        "same outcome" if same else f"outcome {order}",
        "same function" if same else f"function {order}",
        f"description {order}",
    )
    return item, ShotSentenceLink(item.shot_id, sentence)


def plan(
    count: int, *, same: bool = False, review: ReviewState = ReviewState.NOT_REQUIRED
) -> DirectorPlanRevision:
    sentence = uuid4()
    values = [shot(index, sentence, same=same) for index in range(1, count + 1)]
    approvals = (
        (ContinuousObservationApproval(values[0][0].shot_id, values[1][0].shot_id, "observe"),)
        if review is ReviewState.APPROVED and count > 1
        else ()
    )
    return DirectorPlanRevision(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        DirectorRevisionKind.DRAFT,
        tuple(item[0] for item in values),
        tuple(item[1] for item in values),
        (sentence,),
        review,
        approvals,
        datetime.now(UTC),
    )


@pytest.mark.parametrize("count", [1, 5, 6])
def test_plan_accepts_boundaries_and_many_to_many(count: int) -> None:
    assert len(plan(count).shots) == count


def test_plan_rejects_seven_shots_and_uncovered_sentence() -> None:
    with pytest.raises(ValueError, match="1 to 6"):
        plan(7)
    value = plan(1)
    with pytest.raises(ValueError, match="every sentence"):
        DirectorPlanRevision(
            value.revision_id,
            value.owner_id,
            value.project_id,
            value.group_id,
            1,
            value.kind,
            value.shots,
            value.sentence_links,
            (value.group_sentence_ids[0], uuid4()),
            value.review_state,
            (),
            value.created_at,
        )


def test_ambiguous_adjacent_shots_require_explicit_human_gate() -> None:
    required = plan(2, same=True, review=ReviewState.REQUIRED)
    assert not required.may_apply
    assert plan(2, same=True, review=ReviewState.APPROVED).may_apply


def test_single_panel_is_only_a_reserved_enum() -> None:
    assert StoryboardAssetKind.SINGLE_PANEL.value == "SINGLE_PANEL"


def test_plan_rejects_non_adjacent_a_b_a_event_or_composition() -> None:
    value = plan(3)
    repeated = (
        value.shots[0],
        value.shots[1],
        DirectorShot(
            value.shots[2].shot_id,
            3,
            value.shots[0].scene,
            value.shots[0].shot_size,
            value.shots[0].camera_viewpoint,
            value.shots[0].subject_action_outcome,
            value.shots[0].narrative_function,
            "returns to A",
        ),
    )
    with pytest.raises(ValueError, match="repeats"):
        DirectorPlanRevision(
            value.revision_id,
            value.owner_id,
            value.project_id,
            value.group_id,
            value.revision_number,
            value.kind,
            repeated,
            value.sentence_links,
            value.group_sentence_ids,
            value.review_state,
            (),
            value.created_at,
        )


def _semantic_shot(
    order: int,
    sentence: UUID,
    *,
    action: str,
    function: str,
    key: tuple[str, str, str, str],
) -> tuple[DirectorShot, ShotSentenceLink]:
    item = DirectorShot(
        uuid4(),
        order,
        f"scene {order}",
        "wide",
        "eye",
        action,
        function,
        f"description {order}",
        *key,
    )
    return item, ShotSentenceLink(item.shot_id, sentence)


def _semantic_plan(
    shots: tuple[tuple[DirectorShot, ShotSentenceLink], ...],
) -> DirectorPlanRevision:
    sentence = shots[0][1].sentence_id
    return DirectorPlanRevision(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        DirectorRevisionKind.DRAFT,
        tuple(item[0] for item in shots),
        tuple(item[1] for item in shots),
        (sentence,),
        ReviewState.NOT_REQUIRED,
        (),
        datetime.now(UTC),
    )


_EVENT_A = ("PERFORM_ACTION", "MULTIPLE_SUBJECTS", "SAME_MOMENT", "SAME_LOCATION")
_EVENT_B = ("SHOW_CONSEQUENCE", "PRIMARY_SUBJECT", "LATER", "NON_SPATIAL")


def test_non_adjacent_repeat_requires_same_semantics_and_same_text() -> None:
    """§22 精度修正（Vikings 第 88 组现场）：同枚举分类但动作文本不同的非相邻
    镜头表达不同事件，不算重复；枚举与文本都相同的才是真重复。"""
    sentence = uuid4()
    distinct_texts = _semantic_plan(
        (
            _semantic_shot(
                1, sentence, action="players move carved pieces", function="play", key=_EVENT_A
            ),
            _semantic_shot(2, sentence, action="a lyre begins", function="music", key=_EVENT_B),
            _semantic_shot(
                3, sentence, action="the hall joins the song", function="sing along", key=_EVENT_A
            ),
        )
    )
    assert distinct_texts.may_apply

    # 枚举相同 + 动作/功能文本也相同（大小写归一）的非相邻镜头：真重复，拦截
    with pytest.raises(ValueError, match="repeats"):
        _semantic_plan(
            (
                _semantic_shot(
                    1, sentence, action="players move carved pieces", function="play", key=_EVENT_A
                ),
                _semantic_shot(2, sentence, action="a lyre begins", function="music", key=_EVENT_B),
                _semantic_shot(
                    3, sentence, action="Players move carved pieces", function="play", key=_EVENT_A
                ),
            )
        )


def _projection_shots(count: int) -> tuple[DirectorShot, ...]:
    return tuple(
        DirectorShot(
            uuid4(),
            order,
            f"scene {order}",
            "wide",
            "eye",
            f"outcome {order}",
            f"function {order}",
            f"description {order}",
        )
        for order in range(1, count + 1)
    )


def _sequential_sentences(
    spans_ms: tuple[int, ...],
) -> tuple[tuple[UUID, ...], dict[UUID, tuple[int, int]]]:
    """相邻无间隙的实测时间轴：第 i 句 = (spans_ms[i-1], spans_ms[i])，首句从 0 起。"""
    sentence_ids = tuple(uuid4() for _ in spans_ms)
    ranges: dict[UUID, tuple[int, int]] = {}
    start = 0
    for sentence_id, duration in zip(sentence_ids, spans_ms, strict=True):
        ranges[sentence_id] = (start, start + duration)
        start += duration
    return sentence_ids, ranges


def _ranges_text(values: tuple[object, ...]) -> list[tuple[int, int, int]]:
    return [(value.shot_order, value.start_ms, value.end_ms) for value in values]  # type: ignore[attr-defined]


def test_projection_matches_legacy_on_continuous_assignment() -> None:
    """顺序独占链接：投影结果与 legacy min/max 完全一致。"""
    shots = _projection_shots(2)
    sentence_ids, ranges = _sequential_sentences((3000, 2000, 4000))
    links = (
        ShotSentenceLink(shots[0].shot_id, sentence_ids[0]),
        ShotSentenceLink(shots[1].shot_id, sentence_ids[1]),
        ShotSentenceLink(shots[1].shot_id, sentence_ids[2]),
    )
    projected = projected_shot_time_ranges_ms(shots, links, ranges)
    assert projected == shot_time_ranges_ms(shots, links, ranges)
    assert _ranges_text(projected) == [(1, 0, 3000), (2, 3000, 9000)]


def test_projection_rejects_group0_nested_pattern() -> None:
    """生产实证形态：镜头 1 链接第 1、2、4 句、镜头 2 链接第 3 句。

    legacy min/max 产生嵌套区间（SHOT 2 落在 SHOT 1 内）；投影判无解，
    由调用方分流（自动相邻合并或人工修复）。
    """
    shots = _projection_shots(2)
    sentence_ids, ranges = _sequential_sentences((2800, 2800, 2800, 3000))
    links = (
        ShotSentenceLink(shots[0].shot_id, sentence_ids[0]),
        ShotSentenceLink(shots[0].shot_id, sentence_ids[1]),
        ShotSentenceLink(shots[1].shot_id, sentence_ids[2]),
        ShotSentenceLink(shots[0].shot_id, sentence_ids[3]),
    )
    legacy = shot_time_ranges_ms(shots, links, ranges)
    assert _ranges_text(legacy) == [(1, 0, 11400), (2, 5600, 8400)]  # 嵌套缺陷实录
    with pytest.raises(ShotTimingProjectionError) as excinfo:
        projected_shot_time_ranges_ms(shots, links, ranges)
    assert excinfo.value.code == "shot_timing_unresolvable"
    assert isinstance(excinfo.value, ValueError)  # 兼容性：仍是 ValueError 子类


def test_projection_shared_sentence_has_unique_monotone_solution() -> None:
    """共享句（镜头 1={1,2}、镜头 2={2,3}）：字典序最小解把第 2 句分给镜头 1，
    区间有序共享边界、互不重叠。"""
    shots = _projection_shots(2)
    sentence_ids, ranges = _sequential_sentences((3000, 3000, 3000))
    links = (
        ShotSentenceLink(shots[0].shot_id, sentence_ids[0]),
        ShotSentenceLink(shots[0].shot_id, sentence_ids[1]),
        ShotSentenceLink(shots[1].shot_id, sentence_ids[1]),
        ShotSentenceLink(shots[1].shot_id, sentence_ids[2]),
    )
    projected = projected_shot_time_ranges_ms(shots, links, ranges)
    assert _ranges_text(projected) == [(1, 0, 6000), (2, 6000, 9000)]
    legacy = shot_time_ranges_ms(shots, links, ranges)
    assert _ranges_text(legacy) == [(1, 0, 6000), (2, 3000, 9000)]  # legacy 区间重叠


def test_projection_requires_every_shot_to_own_a_sentence() -> None:
    """每镜至少分到一句：两镜只链接同一句时无法满足，判无解。"""
    shots = _projection_shots(2)
    sentence_ids, ranges = _sequential_sentences((3000,))
    links = tuple(ShotSentenceLink(shot.shot_id, sentence_ids[0]) for shot in shots)
    with pytest.raises(ShotTimingProjectionError, match="shot_timing_unresolvable"):
        projected_shot_time_ranges_ms(shots, links, ranges)


def test_projection_picks_lexicographically_smallest_assignment() -> None:
    """多个可行分配（(1,1,2) 与 (1,2,2)）时取字典序最小者，结果唯一确定。"""
    shots = _projection_shots(2)
    sentence_ids, ranges = _sequential_sentences((2000, 2000, 2000))
    links = (
        ShotSentenceLink(shots[0].shot_id, sentence_ids[0]),
        ShotSentenceLink(shots[0].shot_id, sentence_ids[1]),
        ShotSentenceLink(shots[1].shot_id, sentence_ids[1]),
        ShotSentenceLink(shots[0].shot_id, sentence_ids[2]),
        ShotSentenceLink(shots[1].shot_id, sentence_ids[2]),
    )
    projected = projected_shot_time_ranges_ms(shots, links, ranges)
    # (1,1,2)：镜头 1 得第 1、2 句，镜头 2 得第 3 句；而非 (1,2,2)。
    assert _ranges_text(projected) == [(1, 0, 4000), (2, 4000, 6000)]


def test_projection_boundaries_single_shot_and_six_shots() -> None:
    one = _projection_shots(1)
    sentence_ids, ranges = _sequential_sentences((2100,))
    values = projected_shot_time_ranges_ms(
        one, (ShotSentenceLink(one[0].shot_id, sentence_ids[0]),), ranges
    )
    assert _ranges_text(values) == [(1, 0, 2100)]

    six = _projection_shots(6)
    six_ids, six_ranges = _sequential_sentences((1000, 1000, 1000, 1000, 1000, 1000))
    six_links = tuple(
        ShotSentenceLink(shot.shot_id, sentence_id)
        for shot, sentence_id in zip(six, six_ids, strict=True)
    )
    projected = projected_shot_time_ranges_ms(six, six_links, six_ranges)
    assert _ranges_text(projected) == [
        (order, (order - 1) * 1000, order * 1000) for order in range(1, 7)
    ]
    assert projected == shot_time_ranges_ms(six, six_links, six_ranges)


def test_projection_orders_sentences_by_measured_time_not_input_order() -> None:
    """句序按 (start_ms, end_ms, sentence_id) 排序，与链接/字典给出顺序无关。"""
    shots = _projection_shots(2)
    sentence_ids, ranges = _sequential_sentences((3000, 3000))
    # 链接顺序刻意颠倒；句的计时归属仍按实测时间。
    links = (
        ShotSentenceLink(shots[1].shot_id, sentence_ids[1]),
        ShotSentenceLink(shots[0].shot_id, sentence_ids[0]),
    )
    projected = projected_shot_time_ranges_ms(shots, links, ranges)
    assert _ranges_text(projected) == [(1, 0, 3000), (2, 3000, 6000)]


def test_projection_keeps_legacy_input_validation() -> None:
    shots = _projection_shots(1)
    sentence_ids, ranges = _sequential_sentences((3000,))
    with pytest.raises(ValueError, match="at least one sentence"):
        projected_shot_time_ranges_ms(shots, (), ranges)
    with pytest.raises(ValueError, match="missing a measured time range"):
        projected_shot_time_ranges_ms(
            shots, (ShotSentenceLink(shots[0].shot_id, uuid4()),), ranges
        )
    with pytest.raises(ValueError, match="sentence time range is invalid"):
        projected_shot_time_ranges_ms(
            shots,
            (ShotSentenceLink(shots[0].shot_id, sentence_ids[0]),),
            {sentence_ids[0]: (500, 500)},
        )


def test_projection_algorithm_version_is_stable() -> None:
    assert SHOT_TIMING_PROJECTION_V1 == "monotone-projection-v1"
