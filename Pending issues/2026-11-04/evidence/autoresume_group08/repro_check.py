"""独立复算第 8 组方案的非相邻重复，用来核对 domain/storyboards.py 的判定。

用法（仓库根目录）：
    python "Pending issues/2026-11-04/evidence/autoresume_group08/repro_check.py"

规则来源（部署镜像内实读）：
    tubeflow_test/domain/storyboards.py:338-358
        composition_keys = (normalized(scene), normalized(shot_size), normalized(camera_viewpoint))
        event_keys       = controlled_semantic_key() + (normalized(subject_action_outcome),
                                                        normalized(narrative_function))
        repeats_non_adjacent(values): 同一键在 index 与 previous 出现且 index - previous > 1 -> True
    任一为 True 即抛 "director plan repeats an event or composition across the group"。

注意：本脚本只是按上述规则复算数据，不导入产品代码，也不写任何数据。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def normalized(value: str) -> str:
    # 与 domain 层的 _normalized 同义：去空白、转小写，便于比较
    return re.sub(r"\s+", "", value).strip().lower()


def repeat_positions(values: list[tuple[str, ...]]) -> list[tuple[int, int, tuple[str, ...]]]:
    """返回 (previous_index, index, key)，index - previous > 1 即视为非相邻重复。"""
    hits: list[tuple[int, int, tuple[str, ...]]] = []
    positions: dict[tuple[str, ...], int] = {}
    for index, value in enumerate(values):
        previous = positions.get(value)
        if previous is not None and index - previous > 1:
            hits.append((previous, index, value))
        positions[value] = index
    return hits


def main() -> None:
    candidate = json.loads((HERE / "group08_candidate.json").read_text(encoding="utf-8"))
    shots = candidate["shots_json"]

    composition = [
        (
            normalized(shot["scene"]),
            normalized(shot["shot_size"]),
            normalized(shot["camera_viewpoint"]),
        )
        for shot in shots
    ]
    event = [
        (
            normalized(shot["subject_focus"]),
            normalized(shot["semantic_event"]),
            normalized(shot["time_relation"]),
            normalized(shot["location_relation"]),
            normalized(shot["subject_action_outcome"]),
            normalized(shot["narrative_function"]),
        )
        for shot in shots
    ]

    print(f"group_sequence = {candidate['group_sequence']}")
    print(f"review_state   = {candidate['review_state']}")
    print(f"final_revision = {candidate['final_revision_id']}")
    print(f"shots          = {len(shots)}")
    print()

    for shot, key in zip(shots, composition, strict=True):
        print(f"  #{shot['order']} scene={shot['scene']!r} size={shot['shot_size']} vp={shot['camera_viewpoint']}")

    print()
    comp_hits = repeat_positions(composition)
    event_hits = repeat_positions(event)
    print(f"composition 非相邻重复: {comp_hits}")
    print(f"event       非相邻重复: {event_hits}")
    print()
    verdict = bool(comp_hits or event_hits)
    print(f"domain 校验结论: {'抛 ValueError（与线上一致）' if verdict else '通过'}")


if __name__ == "__main__":
    main()
