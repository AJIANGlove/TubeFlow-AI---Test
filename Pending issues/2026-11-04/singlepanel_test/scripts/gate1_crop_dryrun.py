"""Gate 1 offline crop dry run for PR #4 (read-only).

Runs inside the PR #4 image with --env-file pointing at the deploy .env.
Reads baseline.json (fixed denominator), fetches original storyboard bytes
from MinIO, and calls derive_shot_panels with layout_version="grid-v1" and
derivation={"version": "crop-grid-v2"}.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

from minio import Minio

from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.panel_images import derive_shot_panels


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    rows = baseline["rows"]
    if args.limit:
        rows = rows[: args.limit]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    done.add(json.loads(line)["generation_id"])
                except Exception:
                    pass

    client = Minio(
        os.environ["TUBEFLOW_MINIO_ENDPOINT"],
        access_key=os.environ["TUBEFLOW_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_MINIO_SECRET_KEY"],
        secure=False,
    )
    bucket = os.environ["TUBEFLOW_MINIO_BUCKET"]

    processed = 0
    for index, row in enumerate(rows, 1):
        generation_id = str(row["generation_id"])
        if generation_id in done:
            continue
        started = time.perf_counter()
        record: dict[str, object] = {
            "generation_id": generation_id,
            "project_id": row.get("project_id"),
            "project_title": row.get("project_title"),
            "group_id": row.get("group_id"),
            "asset_id": row.get("asset_id"),
            "object_key": row.get("object_key"),
            "expected_sha256": row.get("sha256"),
            "shots": row.get("shots"),
            "status": "failed",
            "failure_code": None,
            "panels_returned": 0,
            "panels": [],
        }
        try:
            shots = int(row["shots"]) if row.get("shots") is not None else 0
            if shots < 1:
                raise ValueError("shots_not_positive")
            response = client.get_object(
                bucket, str(row["object_key"])
            )
            try:
                content = response.read()
            finally:
                response.close()
                response.release_conn()
            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha != row.get("sha256"):
                record["status"] = "failed"
                record["failure_code"] = "source_bytes_sha_mismatch"
                record["actual_sha256"] = actual_sha
            else:
                panels = derive_shot_panels(
                    content,
                    source_sha256=actual_sha,
                    panel_count=shots,
                    aspect_ratio="16:9",
                    derivation={"version": "crop-grid-v2"},
                    layout_version="grid-v1",
                )
                record["status"] = "passed"
                record["panels_returned"] = len(panels)
                record["panels"] = [
                    {
                        "shot_order": panel.shot_order,
                        "crop_xyxy": list(panel.crop_xyxy),
                        "dimensions": list(panel.dimensions),
                        "derivation": panel.derivation,
                        "sha256": hashlib.sha256(panel.content).hexdigest(),
                        "bytes": len(panel.content),
                    }
                    for panel in panels
                ]
        except VideoRuleViolation as exc:
            record["status"] = "failed"
            record["failure_code"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - record the exact failure
            record["status"] = "failed"
            record["failure_code"] = f"{type(exc).__name__}: {exc}"
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        processed += 1
        if processed % 25 == 0:
            print(f"progress {processed}/{len(rows)}", flush=True)

    records = []
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))

    by_project: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    by_failure: Counter[str] = Counter()
    by_shots: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    for record in records:
        project = str(record.get("project_id"))
        status = str(record.get("status"))
        by_project[project]["total"] += 1
        by_project[project][status] += 1
        by_shots[str(record.get("shots"))]["total"] += 1
        by_shots[str(record.get("shots"))][status] += 1
        if status != "passed":
            by_failure[str(record.get("failure_code"))] += 1

    passed = sum(1 for r in records if r.get("status") == "passed")
    total = len(records)
    summary = {
        "baseline_total": baseline.get("total"),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 6) if total else 0.0,
        "by_project": dict(by_project),
        "by_failure_code": dict(by_failure),
        "by_shots": dict(by_shots),
    }
    Path(str(out_path) + ".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
