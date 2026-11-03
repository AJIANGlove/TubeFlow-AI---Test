from __future__ import annotations

from uuid import UUID

import pytest

from tubeflow_test.application.plan_convergence import (
    PLAN_CONVERGENCE_VERSION,
    converge_group_shots,
)
from tubeflow_test.domain.project_director import ProjectDirectorSourceShotLink
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink


def identifier(value: int) -> UUID:
    return UUID(int=value)


def shot(order: int, shot_id: int, action: str, description: str) -> DirectorShot:
    return DirectorShot(
        identifier(shot_id),
        order,
        "scene",
        "MEDIUM",
        "EYE_LEVEL",
        action,
        "function",
        description,
    )


def sources(pair_count: int, *, first: int = 2001) -> tuple[ProjectDirectorSourceShotLink, ...]:
    """第 n 对 = (first + n - 1 → 100 + n)，与镜头身份 101..107 对齐。"""
    return tuple(
        ProjectDirectorSourceShotLink(identifier(first + index - 1), identifier(100 + index))
        for index in range(1, pair_count + 1)
    )


def test_convergence_version_is_stable() -> None:
    assert PLAN_CONVERGENCE_VERSION == "adjacent-merge-v1"


def test_seven_shots_merge_shortest_adjacent_pair_and_keep_ordering() -> None:
    shots = (
        shot(1, 101, "a" * 10, "b" * 10),
        shot(2, 102, "c", "d"),
        shot(3, 103, "e", "f"),
        shot(4, 104, "g" * 10, "h" * 10),
        shot(5, 105, "ii", "jj"),
        shot(6, 106, "kkk", "lll"),
        shot(7, 107, "m", "n"),
    )
    sentence_links = (
        ShotSentenceLink(identifier(101), identifier(9001)),
        ShotSentenceLink(identifier(102), identifier(9002)),
        # 同一句子同时链接前镜与后镜：合并后应去重为单条。
        ShotSentenceLink(identifier(102), identifier(9003)),
        ShotSentenceLink(identifier(103), identifier(9003)),
        ShotSentenceLink(identifier(103), identifier(9004)),
        ShotSentenceLink(identifier(104), identifier(9005)),
        ShotSentenceLink(identifier(105), identifier(9006)),
        ShotSentenceLink(identifier(106), identifier(9007)),
        ShotSentenceLink(identifier(107), identifier(9008)),
    )

    converged, converged_links, converged_sources = converge_group_shots(
        shots, sentence_links, sources(7), limit=6
    )

    # 最短相邻对 (102, 103) 被合并；前镜身份保留、序号重排连续。
    assert [item.shot_id for item in converged] == [
        identifier(101),
        identifier(102),
        identifier(104),
        identifier(105),
        identifier(106),
        identifier(107),
    ]
    assert [item.order for item in converged] == [1, 2, 3, 4, 5, 6]
    merged = converged[1]
    # 字段配方：结构字段取前镜，动作结果与画面描述为前镜文本 + 单空格 + 后镜文本。
    assert merged.subject_action_outcome == "c e"
    assert merged.description == "d f"
    assert merged.scene == "scene"
    assert merged.shot_size == "MEDIUM"
    # 多父血缘：退场镜头的来源重定向到保留镜头；(source, candidate) 对唯一。
    parents = [(item.source_shot_id, item.candidate_shot_id) for item in converged_sources]
    assert (identifier(2002), identifier(102)) in parents
    assert (identifier(2003), identifier(102)) in parents
    assert len(parents) == len(set(parents)) == 7
    # 句子链接并集去重、保持稳定序。
    assert len(converged_links) == 8
    assert sum(1 for item in converged_links if item.sentence_id == identifier(9003)) == 1
    assert all(item.shot_id != identifier(103) for item in converged_links)


def test_eight_and_nine_shots_converge_deterministically() -> None:
    shots8 = tuple(
        shot(index, 300 + index, "x" * (index * 2), "y" * (index * 2)) for index in range(1, 9)
    )
    links8 = tuple(
        ShotSentenceLink(identifier(300 + index), identifier(5000 + index)) for index in range(1, 9)
    )
    first = converge_group_shots(shots8, links8, sources(8, first=7001), limit=6)
    replay = converge_group_shots(shots8, links8, sources(8, first=7001), limit=6)
    assert first == replay, "same input must produce the same output"
    assert len(first[0]) == 6
    assert [item.order for item in first[0]] == [1, 2, 3, 4, 5, 6]
    assert len(first[1]) == 8
    assert len(first[2]) == 8, "lineage must not be lost"

    shots9 = tuple(shot(index, 600 + index, "p" * index, "q" * index) for index in range(1, 10))
    links9 = tuple(
        ShotSentenceLink(identifier(600 + index), identifier(6100 + index))
        for index in range(1, 10)
    )
    wide = converge_group_shots(shots9, links9, sources(9, first=6201), limit=6)
    assert len(wide[0]) == 6
    assert [item.order for item in wide[0]] == [1, 2, 3, 4, 5, 6]
    assert len(wide[2]) == 9


def test_input_at_or_below_limit_is_returned_unchanged() -> None:
    shots = tuple(shot(index, 800 + index, "a", "b") for index in range(1, 6))
    sentence_links = tuple(
        ShotSentenceLink(identifier(800 + index), identifier(8500 + index)) for index in range(1, 6)
    )
    lineage = sources(5, first=8801)

    same = converge_group_shots(shots, sentence_links, lineage, limit=6)

    assert same[0] == shots
    assert same[0][0] is shots[0]
    assert same[1] == sentence_links
    assert same[2] == lineage


def test_tie_breaks_toward_the_larger_order() -> None:
    shots = (
        shot(1, 401, "aa", "bb"),
        shot(2, 402, "cc", "dd"),
        shot(3, 403, "ee", "ff"),
        shot(4, 404, "gg", "hh"),
    )
    sentence_links = tuple(
        ShotSentenceLink(identifier(400 + index), identifier(8000 + index)) for index in range(1, 5)
    )

    converged, _, _ = converge_group_shots(shots, sentence_links, sources(4, first=9001), limit=3)

    # 全部相邻对合计字符相等（4）→ 合并序号较大者 (403, 404)，保留 403。
    assert [item.shot_id for item in converged] == [
        identifier(401),
        identifier(402),
        identifier(403),
    ]
    assert converged[2].subject_action_outcome == "ee gg"


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        converge_group_shots((), (), (), limit=0)
