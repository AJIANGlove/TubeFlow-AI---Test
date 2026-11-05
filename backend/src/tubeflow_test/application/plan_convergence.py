"""组级镜头数确定性收敛（§24，规则版本 adjacent-merge-v1）。

确定性重映射产出的组切片镜头数超过组级上限时，本模块按固定规则逐次合并相邻
镜头对，直至不超过上限。模块为纯函数：不调用外部 AI、不读写存储、不使用时间
与随机量；相同输入恒产生相同输出（含镜头顺序、字段与血缘）。

审计契约（并入候选存档，见 USER_DECISIONS §24）：
- 合并保留前镜身份；后镜身份退场，其全部来源镜头血缘重定向到保留镜头
  （同一候选由此可保留多个来源镜头，即多父血缘）；
- 句子链接取并集，(shot_id, sentence_id) 对唯一，并保持稳定序；
- 合并镜头按前镜取值：场景/景别/机位/叙事功能/受控语义四元组；动作结果与
  画面描述为「前镜文本 + 单空格 + 后镜文本」；
- 合并完成后镜头序号重排为 1..n 连续。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from tubeflow_test.domain.project_director import ProjectDirectorSourceShotLink
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink

PLAN_CONVERGENCE_VERSION = "adjacent-merge-v1"

# 合并文本分隔符：单空格（语言中立、确定性强，见 §24）。
_MERGE_TEXT_SEPARATOR = " "


def converge_group_shots(
    shots: Sequence[DirectorShot],
    links: Sequence[ShotSentenceLink],
    source_shot_links: Sequence[ProjectDirectorSourceShotLink],
    *,
    limit: int,
) -> tuple[
    tuple[DirectorShot, ...],
    tuple[ShotSentenceLink, ...],
    tuple[ProjectDirectorSourceShotLink, ...],
]:
    """把组切片收敛为不超过 limit 个镜头；未超限时恒等返回。"""
    if limit < 1:
        raise ValueError("convergence limit must be at least one shot")
    if len(shots) <= limit:
        return tuple(shots), tuple(links), tuple(source_shot_links)
    converged_shots = list(shots)
    converged_links = list(links)
    converged_sources = list(source_shot_links)
    while len(converged_shots) > limit:
        index = _select_merge_index(converged_shots)
        front = converged_shots[index]
        back = converged_shots[index + 1]
        merged = _merge_shot(front, back)
        converged_shots[index : index + 2] = [merged]
        converged_links = [
            ShotSentenceLink(merged.shot_id, item.sentence_id)
            if item.shot_id == back.shot_id
            else item
            for item in converged_links
        ]
        converged_sources = [
            ProjectDirectorSourceShotLink(item.source_shot_id, merged.shot_id)
            if item.candidate_shot_id == back.shot_id
            else item
            for item in converged_sources
        ]
    renumbered = tuple(
        replace(shot, order=position) for position, shot in enumerate(converged_shots, start=1)
    )
    return (
        renumbered,
        tuple(dict.fromkeys(converged_links)),
        tuple(dict.fromkeys(converged_sources)),
    )


def _select_merge_index(shots: list[DirectorShot]) -> int:
    """选择待合并的相邻对：动作结果＋画面描述合计字符最短；平局取序号较大者。"""
    best_index = 0
    best_key: tuple[int, int] | None = None
    for index in range(len(shots) - 1):
        key = (
            _merge_cost(shots[index], shots[index + 1]),
            -shots[index].order,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_index = index
    return best_index


def _merge_cost(front: DirectorShot, back: DirectorShot) -> int:
    return (
        len(front.subject_action_outcome)
        + len(front.description)
        + len(back.subject_action_outcome)
        + len(back.description)
    )


def _merge_shot(front: DirectorShot, back: DirectorShot) -> DirectorShot:
    """保身份合并：前镜身份 + 前镜取值的结构字段 + 拼接的动作结果与画面描述。"""
    return replace(
        front,
        subject_action_outcome=_joined_text(
            front.subject_action_outcome, back.subject_action_outcome
        ),
        description=_joined_text(front.description, back.description),
    )


def _joined_text(front: str, back: str) -> str:
    return f"{front}{_MERGE_TEXT_SEPARATOR}{back}"


# §50（2026-11-05 用户「全部按推荐执行」）：前置导演单元第 3 次（末次）尝试仍只因相邻
# 多样性不足被拒时，对违规相邻对做确定性合并。配方与 adjacent-merge-v1 不同：保留前镜
# 全部字段（不拼接后镜文本），只把后镜的句子链接并入前镜，避免一格写入两个时刻（§46.11）。
ADJACENT_DIVERSITY_MERGE_VERSION = "adjacent-diversity-merge-v1"
ADJACENT_DIVERSITY_MIN_CHANGES = 2


def merge_insufficiently_different_shots(
    shots: Sequence[DirectorShot],
    links: Sequence[ShotSentenceLink],
) -> tuple[
    tuple[DirectorShot, ...],
    tuple[ShotSentenceLink, ...],
    tuple[tuple[int, int], ...],
]:
    """逐次合并首个受控语义差异不足 2 维的相邻对，直至全部相邻对合格或只剩 1 镜。

    返回（重排序号后的镜头、去重并保持稳定序的句子链接、按原序号记录的合并对）。
    纯函数：不调用外部 AI、不读写存储；相同输入恒得相同输出。无违规时恒等返回。
    """
    current = list(shots)
    current_links = list(links)
    merged: list[tuple[int, int]] = []
    while True:
        index = next(
            (
                position
                for position in range(len(current) - 1)
                if current[position + 1].changes_from(current[position])
                < ADJACENT_DIVERSITY_MIN_CHANGES
            ),
            None,
        )
        if index is None:
            break
        front, back = current[index], current[index + 1]
        merged.append((front.order, back.order))
        del current[index + 1]
        current_links = [
            ShotSentenceLink(front.shot_id, item.sentence_id)
            if item.shot_id == back.shot_id
            else item
            for item in current_links
        ]
    if not merged:
        return tuple(shots), tuple(links), ()
    renumbered = tuple(
        replace(shot, order=position) for position, shot in enumerate(current, start=1)
    )
    return renumbered, tuple(dict.fromkeys(current_links)), tuple(merged)
