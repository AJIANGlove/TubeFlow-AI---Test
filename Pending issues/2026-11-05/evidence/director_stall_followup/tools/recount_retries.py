"""Recount activity RETRIES (attempt > 1) instead of only ActivityTaskTimedOut events.

The 2026-11-05 audit counted `EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT`, which Temporal only writes when
retries are exhausted. A retry that eventually succeeds leaves no TimedOut event, so "0 timed out"
was mis-read as "0 retries". This script counts `ActivityTaskStarted.attempt > 1` and attributes each
retry through `lastFailure` (timeout vs application vs unknown).

Modes
  json-dir  parse already-saved `workflow show --output json` files (no temporalio import)
  fetch     enumerate a window inside the worker container, fetch histories, parse (temporalio)
  pending   describe currently running workflows and list pending activities with attempt > 1
  compare   diff W0 vs W1 and test timing compatibility with a deploy restart

All modes are read-only: ListWorkflows / GetWorkflowHistory / DescribeWorkflowExecution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TIMEOUT_NAMES = {
    1: "TIMEOUT_TYPE_START_TO_CLOSE",
    2: "TIMEOUT_TYPE_SCHEDULE_TO_START",
    3: "TIMEOUT_TYPE_SCHEDULE_TO_CLOSE",
    4: "TIMEOUT_TYPE_HEARTBEAT",
}


# ----------------------------------------------------------------------------- time helpers
def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value)
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # 9-digit nanos -> 6
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def timeout_name(value: Any) -> str:
    if isinstance(value, str) and value.startswith("TIMEOUT_TYPE_"):
        return value
    if isinstance(value, int) and value:
        return TIMEOUT_NAMES.get(value, f"UNKNOWN({value})")
    return "UNSPECIFIED"


# ----------------------------------------------------------------------------- event helpers
def attrs_of(event: dict[str, Any]) -> dict[str, Any]:
    for key, value in event.items():
        if key.endswith("Attributes") and isinstance(value, dict):
            return value
    return {}


def failure_kind(last_failure: dict[str, Any] | None) -> tuple[str, str | None, str | None]:
    """Return (kind, timeout_type, message)."""
    if not last_failure:
        return "unknown", None, None
    message = last_failure.get("message")
    if last_failure.get("timeoutFailureInfo") is not None:
        info = last_failure.get("timeoutFailureInfo") or {}
        return "timeout", timeout_name(info.get("timeoutType")), message
    if last_failure.get("applicationFailureInfo") is not None:
        return "application", None, message
    return "other", None, message


def scan_history(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Index scheduled/started and return attempts with attempt > 1."""
    scheduled: dict[str, dict[str, Any]] = {}
    started: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        kind = event.get("eventType")
        if kind == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            attrs = attrs_of(event)
            scheduled[str(event.get("eventId"))] = {
                "activity_type": (attrs.get("activityType") or {}).get("name"),
                "activity_id": attrs.get("activityId"),
                "time": parse_time(event.get("eventTime")),
            }
        elif kind == "EVENT_TYPE_ACTIVITY_TASK_STARTED":
            attrs = attrs_of(event)
            parent = str(attrs.get("scheduledEventId"))
            started.setdefault(parent, []).append(
                {
                    "event_id": str(event.get("eventId")),
                    "time": parse_time(event.get("eventTime")),
                    "attempt": attrs.get("attempt") or 1,
                    "identity": attrs.get("identity"),
                    "last_failure": attrs.get("lastFailure"),
                }
            )
    for items in started.values():
        items.sort(key=lambda item: item["time"] or datetime.min.replace(tzinfo=UTC))

    retries: list[dict[str, Any]] = []
    unresolved = 0
    for scheduled_id, starts in started.items():
        info = scheduled.get(scheduled_id)
        if info is None:
            unresolved += 1
        for index, start in enumerate(starts):
            if (start["attempt"] or 1) <= 1:
                continue
            kind, timeout_type, message = failure_kind(start["last_failure"])
            prior = [s for s in starts[:index] if (s["attempt"] or 1) < (start["attempt"] or 1)]
            retries.append(
                {
                    "scheduled_event_id": scheduled_id,
                    "started_event_id": start["event_id"],
                    "activity_type": (info or {}).get("activity_type"),
                    "activity_id": (info or {}).get("activity_id"),
                    "scheduled_time": iso((info or {}).get("time")),
                    "started_time": iso(start["time"]),
                    "schedule_to_start_s": (
                        round((start["time"] - info["time"]).total_seconds(), 3)
                        if info and info.get("time") and start["time"]
                        else None
                    ),
                    "attempt": start["attempt"],
                    "identity": start["identity"],
                    "has_prior_started": bool(prior),
                    "prior_attempt_duration_s": (
                        round((prior[-1]["time"] - info["time"]).total_seconds(), 3)
                        if prior and info and info.get("time")
                        else None
                    ),
                    "kind": kind,
                    "timeout_type": timeout_type,
                    "message": message,
                }
            )
    return {"retries": retries, "unresolved_scheduled_event": unresolved,
            "scheduled": len(scheduled), "started": sum(len(v) for v in started.values())}


def in_window(value: str | None, start: datetime, end: datetime) -> bool:
    parsed = parse_time(value)
    return bool(parsed and start <= parsed < end)


def summarize(retries: list[dict[str, Any]], start: datetime, end: datetime,
              window_name: str) -> dict[str, Any]:
    timeouts, applications, others = [], [], []
    for item in retries:
        item = dict(item)
        item["in_window"] = in_window(item["started_time"], start, end)
        target = timeouts if item["kind"] == "timeout" else (
            applications if item["kind"] == "application" else others
        )
        target.append(item)

    def grouped(items: list[dict[str, Any]], key_timeout: bool) -> list[dict[str, Any]]:
        buckets: dict[tuple, list[dict[str, Any]]] = {}
        for item in items:
            key = (item.get("activity_type"), item.get("timeout_type") if key_timeout else None,
                   item.get("message"))
            buckets.setdefault(key, []).append(item)
        rows = []
        for (activity, timeout_type, message), values in buckets.items():
            rows.append(
                {
                    "activity_type": activity,
                    "timeout_type": timeout_type,
                    "message": message,
                    "count": len(values),
                    "count_in_window": sum(1 for v in values if v["in_window"]),
                    "started_times": [v["started_time"] for v in values],
                }
            )
        rows.sort(key=lambda row: (-row["count"], str(row["activity_type"])))
        return rows

    return {
        "window": window_name,
        "range": {"start": iso(start), "end": iso(end)},
        "timeout_retries": [r for r in timeouts if r["in_window"]],
        "application_retries": [r for r in applications if r["in_window"]],
        "other_retries": [r for r in others if r["in_window"]],
        "out_of_window_timeout_retries": [r for r in timeouts if not r["in_window"]],
        "grouped_timeout": grouped([r for r in timeouts if r["in_window"]], True),
        "grouped_application": grouped([r for r in applications if r["in_window"]], False),
        "totals": {
            "timeout_retries": sum(1 for r in timeouts if r["in_window"]),
            "application_retries": sum(1 for r in applications if r["in_window"]),
            "other_retries": sum(1 for r in others if r["in_window"]),
        },
    }


# ----------------------------------------------------------------------------- modes
def mode_json_dir(args: argparse.Namespace) -> None:
    directory = Path(args.dir)
    reports, all_retries = [], []
    for path in sorted(directory.glob("*.json")):
        history = json.loads(path.read_text(encoding="utf-8"))
        events = history.get("events") or history.get("history", {}).get("events") or []
        scan = scan_history(events)
        for item in scan["retries"]:
            item["run_id"] = path.stem
        all_retries.extend(scan["retries"])
        reports.append(
            {
                "run_id": path.stem,
                "events": len(events),
                "scheduled": scan["scheduled"],
                "started": scan["started"],
                "retries": len(scan["retries"]),
                "unresolved_scheduled_event": scan["unresolved_scheduled_event"],
            }
        )
    payload = {
        "mode": "json-dir",
        "directory": str(directory),
        "histories": len(reports),
        "per_run": reports,
        "retries": all_retries,
        "totals": {
            "attempt_gt_1": len(all_retries),
            "timeout_retries": sum(1 for r in all_retries if r["kind"] == "timeout"),
            "application_retries": sum(1 for r in all_retries if r["kind"] == "application"),
            "other_retries": sum(1 for r in all_retries if r["kind"] == "other"),
        },
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload["totals"], indent=1))
    for item in all_retries:
        print(f"  {item['run_id'][:8]} sched={item['scheduled_event_id']} attempt={item['attempt']} "
              f"type={item['timeout_type']} started={item['started_time']} prior={item['has_prior_started']}")


async def _fetch_window(args: argparse.Namespace) -> None:
    from google.protobuf.json_format import MessageToDict
    from temporalio.client import Client

    start, end = parse_time(args.start), parse_time(args.end)
    lookback = start - timedelta(minutes=args.lookback_minutes)
    client = await Client.connect(args.address, namespace=args.namespace)

    listings: dict[str, int] = {}
    strata: dict[tuple[str, str], set[str]] = {}
    types: dict[tuple[str, str], str] = {}
    queries = {
        "primary": f'StartTime >= "{iso(lookback)}" and StartTime < "{iso(end)}"',
        "supplement": (f'StartTime < "{iso(lookback)}" and CloseTime >= "{iso(start)}" '
                       f'and CloseTime < "{iso(end)}"'),
        "running": 'ExecutionStatus = "Running"',
    }
    supplement_error = None
    for name, query in queries.items():
        count = 0
        try:
            async for workflow in client.list_workflows(query=query):
                key = (workflow.id, workflow.run_id or "")
                strata.setdefault(key, set()).add(name)
                types[key] = workflow.workflow_type or "?"
                count += 1
                if count >= args.max_workflows:
                    break
        except Exception as exc:  # noqa: BLE001
            if name == "supplement":
                supplement_error = f"{type(exc).__name__}: {exc}"[:300]
                listings[name] = -1
                continue
            raise
        listings[name] = count

    semaphore = asyncio.Semaphore(args.concurrency)
    failures: list[dict[str, Any]] = []
    scanned: list[dict[str, Any]] = []
    retries: list[dict[str, Any]] = []

    async def one(key: tuple[str, str]) -> None:
        workflow_id, run_id = key
        workflow_type = types[key]
        async with semaphore:
            history = None
            for attempt in (1, 2):
                try:
                    handle = client.get_workflow_handle(workflow_id, run_id=run_id or None)
                    history = await asyncio.wait_for(handle.fetch_history(), timeout=args.timeout)
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        failures.append({"workflow_id": workflow_id, "run_id": run_id,
                                         "workflow_type": workflow_type,
                                         "error": f"{type(exc).__name__}: {exc}"[:300]})
        if history is None:
            return
        events = [MessageToDict(event, preserving_proto_field_name=False)
                  for event in history.events]
        scan = scan_history(events)
        for item in scan["retries"]:
            item["workflow_id"] = workflow_id
            item["run_id"] = run_id
            item["workflow_type"] = workflow_type
            item["strata"] = sorted(strata.get(key, []))
        retries.extend(scan["retries"])
        scanned.append(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "workflow_type": workflow_type,
                "events": len(events),
                "started": scan["started"],
                "attempt_gt_1": len(scan["retries"]),
                "unresolved_scheduled_event": scan["unresolved_scheduled_event"],
                "strata": sorted(strata.get(key, [])),
            }
        )

    await asyncio.gather(*(one(key) for key in strata))

    summary = summarize(retries, start, end, args.window)
    payload = {
        "mode": "fetch",
        "generated_at": iso(datetime.now(UTC)),
        "temporal": {"address": args.address, "namespace": args.namespace},
        "window": {"name": args.window, "start": iso(start), "end": iso(end),
                   "lookback_start": iso(lookback)},
        "enumeration": {
            "queries": queries,
            "listings": listings,
            "unique_workflow_runs": len(strata),
            "type_histogram": {
                t: sum(1 for v in types.values() if v == t) for t in sorted(set(types.values()))
            },
            "max_workflows": args.max_workflows,
            "truncated": any(v >= args.max_workflows for v in listings.values() if v > 0),
            "supplement_unavailable": supplement_error,
        },
        "fetch": {"attempted": len(strata), "ok": len(scanned),
                  "failure_count": len(failures), "failures": failures},
        "scan": {
            "histories_scanned": len(scanned),
            "started_events": sum(r["started"] for r in scanned),
            "attempt_gt_1_total": len(retries),
            "in_window": summary["totals"]["timeout_retries"] + summary["totals"]["application_retries"]
            + summary["totals"]["other_retries"],
        },
        **{k: summary[k] for k in ("timeout_retries", "application_retries", "other_retries",
                                   "out_of_window_timeout_retries", "grouped_timeout",
                                   "grouped_application", "totals", "range")},
        "history_index": sorted(scanned, key=lambda r: (r["workflow_type"], r["workflow_id"])),
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"window": args.window, **payload["enumeration"]["listings"],
                      "unique": len(strata), "fetched": len(scanned),
                      "failures": len(failures), **payload["totals"]}, indent=1))


async def _pending(args: argparse.Namespace) -> None:
    from google.protobuf.json_format import MessageToDict
    from temporalio.client import Client

    client = await Client.connect(args.address, namespace=args.namespace)
    workflows = []
    async for workflow in client.list_workflows(query='ExecutionStatus = "Running"'):
        handle = client.get_workflow_handle(workflow.id, run_id=workflow.run_id or None)
        try:
            description = await asyncio.wait_for(handle.describe(), timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            workflows.append({"workflow_id": workflow.id, "run_id": workflow.run_id,
                              "workflow_type": workflow.workflow_type,
                              "describe_error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        raw = MessageToDict(description.raw_description, preserving_proto_field_name=False)
        pending = []
        for item in raw.get("pendingActivities", []) or []:
            info = {
                "activity_type": (item.get("activityType") or {}).get("name"),
                "activity_id": item.get("activityId"),
                "state": item.get("state"),
                "attempt": item.get("attempt"),
                "maximum_attempts": item.get("maximumAttempts"),
                "scheduled_time": item.get("scheduledTime"),
                "last_started_time": item.get("lastStartedTime"),
                "last_failure": item.get("lastFailure"),
            }
            kind, timeout_type, message = failure_kind(item.get("lastFailure"))
            info.update({"kind": kind, "timeout_type": timeout_type, "message": message})
            pending.append(info)
        workflows.append(
            {
                "workflow_id": workflow.id,
                "run_id": workflow.run_id,
                "workflow_type": workflow.workflow_type,
                "pending_total": len(pending),
                "pending_attempt_gt_1": [p for p in pending if (p.get("attempt") or 0) > 1],
                "pending_all": pending,
            }
        )
    payload = {
        "mode": "pending",
        "captured_at": iso(datetime.now(UTC)),
        "temporal": {"address": args.address, "namespace": args.namespace},
        "running": len(workflows),
        "workflows": workflows,
        "summary": {
            "with_pending": sum(1 for w in workflows if w.get("pending_total")),
            "pending_total": sum(w.get("pending_total") or 0 for w in workflows),
            "pending_attempt_gt_1": sum(len(w.get("pending_attempt_gt_1") or []) for w in workflows),
        },
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=1))


def mode_compare(args: argparse.Namespace) -> None:
    w0 = json.loads(Path(args.w0).read_text(encoding="utf-8"))
    w1 = json.loads(Path(args.w1).read_text(encoding="utf-8"))
    t0, t1 = parse_time(args.t_dep_start), parse_time(args.t_dep_end)
    n0 = w0["totals"]["timeout_retries"]
    n1 = w1["totals"]["timeout_retries"]
    if n0 == 0 and n1 == 0:
        verdict = "两窗均无"
    elif n0 == 0 < n1:
        verdict = "W1 有、W0 无"
    elif n1 == 0 < n0:
        verdict = "W0 有、W1 无"
    else:
        verdict = f"两窗均有（W0={n0} / W1={n1}）"

    rows = []
    for item in w1["timeout_retries"]:
        scheduled = parse_time(item["scheduled_time"])
        started = parse_time(item["started_time"])
        compatible = bool(
            scheduled and started and scheduled < t0 and started >= t1
            and item["schedule_to_start_s"] is not None
            and abs(item["schedule_to_start_s"] - 1200) <= 5
        )
        rows.append(
            {
                "workflow": item.get("workflow_id"),
                "activity": item.get("activity_type"),
                "scheduled_time": item["scheduled_time"],
                "started_time": item["started_time"],
                "schedule_to_start_s": item["schedule_to_start_s"],
                "scheduled_before_restart": bool(scheduled and scheduled < t0),
                "started_after_restart": bool(started and started >= t1),
                "restart_timing_compatible": compatible,
            }
        )
    payload = {
        "mode": "compare",
        "windows": {"W0": w0["window"], "W1": w1["window"]},
        "totals": {
            "W0_timeout_retries": n0,
            "W1_timeout_retries": n1,
            "W0_application_retries": w0["totals"]["application_retries"],
            "W1_application_retries": w1["totals"]["application_retries"],
            "W0_scanned": w0["fetch"]["ok"],
            "W1_scanned": w1["fetch"]["ok"],
            "W0_fetch_failures": w0["fetch"]["failure_count"],
            "W1_fetch_failures": w1["fetch"]["failure_count"],
        },
        "verdict": verdict,
        "restart": {"start": iso(t0), "end": iso(t1)},
        "w1_retry_timing": rows,
        "blind_spot": (
            "旧审计只统计 EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT；Temporal 仅在该活动的重试耗尽时才写"
            "该事件，重试后成功的尝试不留 TimedOut，因此「0 TimedOut」不等于「0 重试」。"
        ),
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"verdict": verdict, **payload["totals"]}, indent=1))
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True,
                        choices=("json-dir", "fetch", "pending", "compare"))
    parser.add_argument("--out")
    parser.add_argument("--dir")
    parser.add_argument("--w0")
    parser.add_argument("--w1")
    parser.add_argument("--window", default="W?")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--lookback-minutes", type=int, default=30)
    parser.add_argument("--max-workflows", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--address", default="tubeflowai-test-temporal-1:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--t-dep-start")
    parser.add_argument("--t-dep-end")
    args = parser.parse_args(argv)

    if args.mode == "json-dir":
        mode_json_dir(args)
    elif args.mode == "fetch":
        asyncio.run(_fetch_window(args))
    elif args.mode == "pending":
        asyncio.run(_pending(args))
    else:
        mode_compare(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
