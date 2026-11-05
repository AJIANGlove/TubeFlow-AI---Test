"""Count activity-level timeouts per workflow type inside a time window (read-only).

Runs inside the worker container (needs temporalio + the compose network):

    docker exec -i tubeflowai-test-worker-1 python - --start 2026-11-05T12:50:00Z \
        --end 2026-11-05T13:50:00Z --out /tmp/w1.json < temporal_window_audit.py

It enumerates workflows whose StartTime falls in the window, plus those still running and those
closed inside it (union, de-duplicated), keeps only the workflow types that carry storyboard /
composition / cover / director activities, then reads each history and counts
`EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT` events whose `eventTime` is inside the window, grouped by
(workflowType, activityType, timeoutType). Fetch failures are reported, never counted as zero.

Read-only: ListWorkflows + GetWorkflowHistory only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime

from temporalio.api.enums.v1 import EventType, TimeoutType
from temporalio.client import Client

INTERESTING = ("storyboard", "grid", "cover", "director", "composition")


def _enum_name(value: object, enum_cls: type) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    try:
        return enum_cls(int(value)).name  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"UNKNOWN({value!r})"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _wanted(workflow_type: str) -> bool:
    lowered = workflow_type.lower()
    return any(token in lowered for token in INTERESTING)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--lookback-minutes", type=int, default=30)
    parser.add_argument("--address", default="tubeflowai-test-temporal-1:7233")
    parser.add_argument("--namespace", default="default")
    args = parser.parse_args()
    start, end = _parse(args.start), _parse(args.end)
    from datetime import timedelta
    args.lookback_start = (start - timedelta(minutes=args.lookback_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    client = await Client.connect(args.address, namespace=args.namespace)

    queries = [
        f'StartTime >= "{args.lookback_start}" and StartTime < "{args.end}"',
    ]
    seen: dict[tuple[str, str], str] = {}
    type_histogram: Counter[str] = Counter()
    for query in queries:
        count = 0
        async for workflow in client.list_workflows(query=query):
            key = (workflow.id, workflow.run_id or "")
            seen.setdefault(key, workflow.workflow_type or "")
            type_histogram[workflow.workflow_type or "?"] += 1
            count += 1
            if count >= 800:
                break

    targets = {key: wtype for key, wtype in seen.items() if _wanted(wtype)}
    timeouts: Counter[tuple[str, str, str]] = Counter()
    all_timeouts: Counter[tuple[str, str, str]] = Counter()
    per_workflow: list[dict] = []
    failures: list[dict] = []
    for (workflow_id, run_id), workflow_type in targets.items():
        try:
            handle = client.get_workflow_handle(workflow_id, run_id=run_id or None)
            history = await handle.fetch_history()
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
            failures.append(
                {"workflow_id": workflow_id, "run_id": run_id, "type": workflow_type,
                 "error": f"{type(exc).__name__}: {exc}"[:200]}
            )
            continue
        local = Counter()
        for event in history.events:
            if _enum_name(event.event_type, EventType) != "EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT":
                continue
            attributes = event.activity_task_timed_out_event_attributes
            epoch = event.event_time.timestamp()
            if not (start.timestamp() <= epoch < end.timestamp()):
                continue
            name = "?"
            try:
                scheduled_id = attributes.scheduled_event_id
                for candidate in history.events:
                    if candidate.event_id == scheduled_id:
                        name = candidate.activity_task_scheduled_event_attributes.activity_type.name
                        break
            except Exception:  # noqa: BLE001
                pass
            timeout_type = _enum_name(attributes.timeout_type, TimeoutType)
            key = (workflow_type, name, timeout_type)
            local[key] += 1
            all_timeouts[key] += 1
        for key, value in local.items():
            timeouts[key] += value
        if local:
            per_workflow.append(
                {"workflow_id": workflow_id, "type": workflow_type,
                 "timeouts": {f"{k[1]}|{k[2]}": v for k, v in local.items()}}
            )

    report = {
        "window": {"start": args.start, "end": args.end},
        "enumeration": f"StartTime in [{args.lookback_start}, {args.end})",
        "workflows_listed": len(seen),
        "type_histogram": dict(sorted(type_histogram.items(), key=lambda item: -item[1])),
        "targets_scanned": len(targets),
        "fetch_failures": failures,
        "fetch_failure_count": len(failures),
        "timeouts_in_window": {
            f"{wtype} | {name} | {timeout_type}": count
            for (wtype, name, timeout_type), count in sorted(
                timeouts.items(), key=lambda item: -item[1]
            )
        },
        "timeout_total": sum(timeouts.values()),
        "workflows_with_timeouts": per_workflow,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, ensure_ascii=False)
    print(json.dumps({k: report[k] for k in ("workflows_listed", "targets_scanned",
                                             "fetch_failure_count", "timeout_total")}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
