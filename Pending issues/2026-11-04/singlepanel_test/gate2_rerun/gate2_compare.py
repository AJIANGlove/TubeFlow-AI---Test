"""Gate 2 comparison: per-group and global deltas across the observation window.

Reads two CSV snapshots produced by gate2_baseline.sql and a SQL window file, and
reports facts only:

  * groups that already had a current video at T0 and whose storyboard/attempt
    counters changed (the Gate 2 failure condition)
  * groups that had a current video at T0 and kept it
  * every group whose counters changed, by has_current
  * global v2 key count in both snapshots

Usage:
  python gate2_compare.py --before baseline_groups.csv --after groups_after30.csv \
      --window window.json --out gate2_diff.json
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

COUNTERS = ("sb_rows", "sb_version_max", "attempt_rows", "intent_max")


def load(path: Path) -> dict[str, dict[str, int]]:
    rows: dict[str, dict[str, int]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows[row["group_id"]] = {
                "project_id": row["project_id"],
                **{name: int(row[name]) for name in COUNTERS},
                "has_current": int(row["has_current"]),
            }
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--window", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    before = load(Path(args.before))
    after = load(Path(args.after))

    completed = {gid for gid, row in before.items() if row["has_current"] == 1}
    changed: list[dict] = []
    for gid, row in before.items():
        new = after.get(gid)
        if new is None:
            changed.append({"group_id": gid, "project_id": row["project_id"], "missing_after": True})
            continue
        diff = {name: [row[name], new[name]] for name in COUNTERS if row[name] != new[name]}
        if diff:
            changed.append(
                {
                    "group_id": gid,
                    "project_id": row["project_id"],
                    "has_current_before": row["has_current"],
                    "has_current_after": new["has_current"],
                    "changed": diff,
                }
            )

    completed_changed = [c for c in changed if c.get("has_current_before") == 1 or c["group_id"] in completed]
    new_groups = sorted(set(after) - set(before))
    current_lost = sorted(gid for gid in completed if after.get(gid, {}).get("has_current", 1) == 0)

    report = {
        "groups_before": len(before),
        "groups_after": len(after),
        "completed_groups_before": len(completed),
        "completed_groups_changed": completed_changed,
        "completed_groups_changed_count": len(completed_changed),
        "completed_groups_losing_current_video": current_lost,
        "groups_new_after_deploy": new_groups,
        "all_changed_groups": changed,
        "all_changed_groups_count": len(changed),
        "changed_split": {
            "was_complete": sum(1 for c in changed if c.get("has_current_before") == 1),
            "was_incomplete": sum(1 for c in changed if c.get("has_current_before") == 0),
        },
    }
    if args.window:
        report["window"] = json.loads(Path(args.window).read_text(encoding="utf-8"))
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "groups_before": report["groups_before"],
                "groups_after": report["groups_after"],
                "completed_groups_before": report["completed_groups_before"],
                "completed_groups_changed_count": report["completed_groups_changed_count"],
                "completed_groups_losing_current_video": len(current_lost),
                "groups_new_after_deploy": len(new_groups),
                "all_changed_groups_count": report["all_changed_groups_count"],
                "changed_split": report["changed_split"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
