"""Audit Temporal history JSON for the three stalled AUTO projects (read-only, offline).

Usage:
    python temporal_audit.py <history_dir> [--out <summary.json>] [--markdown <table.md>]

For every `advance_project_director` activity it pairs Scheduled/Started/Completed/TimedOut by
`scheduledEventId` and reports:
  * per-attempt Started→Completed durations (p50/p90/max, count),
  * Started→TimedOut durations and timeoutType counts,
  * retries per scheduledEventId (attempts - 1),
  * whether 1201 / 1232 / 1274 seconds appear (+-1 s) in any of the three interval kinds.

No network, no database, no writes besides the two optional output files.
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

TARGET_ACTIVITY = "advance_project_director"
INTERESTING = (1201.0, 1232.0, 1274.0)
KEYS = ("scheduledEventId", "startedEventId", "attempt", "timeoutType", "retryState")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _attrs(event: dict[str, Any]) -> dict[str, Any]:
    kind = event["eventType"]
    suffix = "Attributes"
    for key, value in event.items():
        if key.endswith(suffix) and isinstance(value, dict):
            return value
    return {}


def _activity_name(event: dict[str, Any]) -> str | None:
    attrs = _attrs(event)
    activity_type = attrs.get("activityType") or {}
    return activity_type.get("name")


def audit(path: Path) -> dict[str, Any]:
    history = json.loads(path.read_text(encoding="utf-8"))
    events = history.get("events") or history.get("history", {}).get("events") or []
    scheduled: dict[str, dict[str, Any]] = {}
    starts: dict[str, list[dict[str, Any]]] = {}
    completions: dict[str, dict[str, Any]] = {}
    timeouts: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("eventType", "")
        attrs = _attrs(event)
        if kind == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            name = _activity_name(event)
            if name == TARGET_ACTIVITY:
                scheduled[str(event["eventId"])] = {
                    "name": name,
                    "time": event["eventTime"],
                }
        elif kind == "EVENT_TYPE_ACTIVITY_TASK_STARTED":
            parent = str(attrs.get("scheduledEventId"))
            if parent in scheduled:
                starts.setdefault(parent, []).append(
                    {"time": event["eventTime"], "attempt": attrs.get("attempt")}
                )
        elif kind == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED":
            parent = str(attrs.get("scheduledEventId"))
            if parent in scheduled:
                completions[parent] = {"time": event["eventTime"]}
        elif kind == "EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT":
            parent = str(attrs.get("scheduledEventId"))
            if parent in scheduled:
                timeouts.append(
                    {
                        "scheduled_event_id": parent,
                        "time": event["eventTime"],
                        "timeout_type": attrs.get("timeoutType"),
                        "retry_state": attrs.get("retryState"),
                    }
                )

    attempts: list[dict[str, Any]] = []
    for scheduled_id, info in scheduled.items():
        started_events = sorted(starts.get(scheduled_id, []), key=lambda item: item["time"])
        for index, started in enumerate(started_events):
            record: dict[str, Any] = {
                "scheduled_event_id": scheduled_id,
                "attempt": started["attempt"] or index + 1,
                "scheduled_to_started_s": round(
                    (_ts(started["time"]) - _ts(info["time"])).total_seconds(), 3
                ),
            }
            completion = completions.get(scheduled_id)
            same_attempt_timeout = next(
                (
                    item
                    for item in timeouts
                    if item["scheduled_event_id"] == scheduled_id
                    and _ts(item["time"]) >= _ts(started["time"])
                ),
                None,
            )
            if completion and (
                same_attempt_timeout is None
                or _ts(completion["time"]) <= _ts(same_attempt_timeout["time"])
            ):
                record["started_to_completed_s"] = round(
                    (_ts(completion["time"]) - _ts(started["time"])).total_seconds(), 3
                )
            elif same_attempt_timeout is not None:
                record["started_to_timedout_s"] = round(
                    (_ts(same_attempt_timeout["time"]) - _ts(started["time"])).total_seconds(), 3
                )
                record["timeout_type"] = same_attempt_timeout["timeout_type"]
            attempts.append(record)

    completed = [a["started_to_completed_s"] for a in attempts if "started_to_completed_s" in a]
    timed_out = [a["started_to_timedout_s"] for a in attempts if "started_to_timedout_s" in a]
    scheduled_waits = [a["scheduled_to_started_s"] for a in attempts]

    def pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
        return ordered[index]

    hits = {}
    for label, values in (
        ("started_to_completed_s", completed),
        ("started_to_timedout_s", timed_out),
        ("scheduled_to_started_s", scheduled_waits),
    ):
        for target in INTERESTING:
            matched = [v for v in values if abs(v - target) <= 1.0]
            if matched:
                hits.setdefault(f"{label}~{target:.0f}s", []).extend(matched)

    return {
        "run_id": path.stem,
        "events": len(events),
        "scheduled": len(scheduled),
        "attempts": len(attempts),
        "retries": sum(max(0, len(starts.get(sid, [])) - 1) for sid in scheduled),
        "completed": len(completed),
        "timed_out": len(timed_out),
        "timeout_types": sorted({a.get("timeout_type") for a in attempts if a.get("timeout_type")}),
        "completed_s": {
            "count": len(completed),
            "p50": pct(completed, 0.5),
            "p90": pct(completed, 0.9),
            "max": max(completed) if completed else None,
            "mean": round(statistics.fmean(completed), 3) if completed else None,
        },
        "timedout_s": {
            "count": len(timed_out),
            "max": max(timed_out) if timed_out else None,
        },
        "scheduled_to_started_s": {
            "count": len(scheduled_waits),
            "p50": pct(scheduled_waits, 0.5),
            "p90": pct(scheduled_waits, 0.9),
            "max": max(scheduled_waits) if scheduled_waits else None,
        },
        "interesting_hits": hits,
        "slowest": sorted(attempts, key=lambda a: -(a.get("started_to_completed_s") or 0))[:5],
        "timeout_detail": timeouts,
    }


def main() -> None:
    history_dir = Path(sys.argv[1])
    out_path = None
    markdown_path = None
    for flag, attr in (("--out", "out"), ("--markdown", "md")):
        if flag in sys.argv:
            value = sys.argv[sys.argv.index(flag) + 1]
            if attr == "out":
                out_path = Path(value)
            else:
                markdown_path = Path(value)
    results = [audit(path) for path in sorted(history_dir.glob("*.json"))]
    summary = {
        "history_files": len(results),
        "runs": results,
        "totals": {
            "attempts": sum(r["attempts"] for r in results),
            "completed": sum(r["completed"] for r in results),
            "timed_out": sum(r["timed_out"] for r in results),
            "retries": sum(r["retries"] for r in results),
        },
        "interesting_summary": {
            f"~{target:.0f}s": sum(
                1 for r in results for key in r["interesting_hits"] if key.endswith(f"~{target:.0f}s")
            )
            for target in INTERESTING
        },
    }
    if out_path:
        out_path.write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    if markdown_path:
        lines = [
            "| run | attempts | completed | timedOut | retries | p50(s) | p90(s) | max(s) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in results:
            lines.append(
                "| {run} | {a} | {c} | {t} | {r} | {p50} | {p90} | {mx} |".format(
                    run=row["run_id"][:8],
                    a=row["attempts"],
                    c=row["completed"],
                    t=row["timed_out"],
                    r=row["retries"],
                    p50=row["completed_s"]["p50"],
                    p90=row["completed_s"]["p90"],
                    mx=row["completed_s"]["max"],
                )
            )
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary["totals"], indent=1))
    print("interesting:", json.dumps(summary["interesting_summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
