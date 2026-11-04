"""Read-only consistency checks for the Gate 1 diagnostics output.

Compares diag/trace.jsonl against gate1/gate1_results.jsonl and reports:
  - record / status counts
  - per-generation comparison of Gate 1 verdict vs traced verdict
  - any source_bytes_sha_mismatch (records carrying an "error" key)
  - file coverage of previews/, sheets/, originals/

Usage:  python gate1_diag_check.py --results gate1_results.jsonl --diag diag --out diag/checks.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--diag", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    results = [json.loads(line) for line in Path(args.results).read_text().splitlines() if line.strip()]
    diag = Path(args.diag)
    traces = [json.loads(line) for line in (diag / "trace.jsonl").read_text().splitlines() if line.strip()]

    by_gid_results = {r["generation_id"]: r for r in results}
    failed_results = {r["generation_id"] for r in results if r.get("status") == "failed"}
    passed_results = {r["generation_id"] for r in results if r.get("status") == "passed"}

    report: dict = {}
    report["gate1_rows"] = len(results)
    report["gate1_status_counts"] = dict(Counter(r.get("status") for r in results))
    report["gate1_failure_counts"] = dict(Counter(r.get("failure_code") for r in results if r.get("status") == "failed"))
    report["trace_rows"] = len(traces)
    report["trace_status_counts"] = dict(Counter(t.get("status") for t in traces))

    # --- error records (source_bytes_sha_mismatch and friends) ---
    error_records = [t for t in traces if "error" in t]
    report["trace_error_records"] = len(error_records)
    report["trace_error_codes"] = dict(Counter(t.get("error") for t in error_records))
    report["source_bytes_sha_mismatch_count"] = sum(1 for t in error_records if t.get("error") == "source_bytes_sha_mismatch")
    report["source_bytes_sha_mismatch_ids"] = [t.get("generation_id") for t in error_records if t.get("error") == "source_bytes_sha_mismatch"]
    report["gate1_rows_with_expected_sha256"] = sum(1 for r in results if r.get("expected_sha256"))

    # --- coverage: every Gate 1 failed row must be traced, passed only a sample ---
    traced_gids = [t.get("generation_id") for t in traces]
    traced_set = set(traced_gids)
    report["trace_duplicate_generation_ids"] = [g for g, n in Counter(traced_gids).items() if n > 1]
    report["trace_unknown_generation_ids"] = sorted(traced_set - set(by_gid_results))
    report["failed_results_missing_from_trace"] = sorted(failed_results - traced_set)

    traced_failed = {t["generation_id"] for t in traces if t.get("status") == "failed"}
    traced_passed = {t["generation_id"] for t in traces if t.get("status") == "passed"}
    report["traced_failed"] = len(traced_failed)
    report["traced_passed"] = len(traced_passed)
    report["traced_failed_but_gate1_passed"] = sorted(traced_failed & passed_results)
    report["traced_passed_but_gate1_failed"] = sorted(traced_passed & failed_results)
    report["passed_sample_size"] = len(traced_passed)

    # --- per-record verdict / code agreement ---
    report["gate1_status_vs_trace_status"] = dict(
        Counter(f'{t.get("gate1_status")}->{t.get("status")}' for t in traces)
    )
    report["gate1_code_vs_trace_code"] = dict(
        Counter(f'{t.get("gate1_failure_code")}->{(t.get("failure") or {}).get("code")}' for t in traces)
    )

    status_mismatch: list[dict] = []
    code_mismatch: list[dict] = []
    cross_check_mismatch: list[dict] = []
    for t in traces:
        gid = t.get("generation_id")
        row = by_gid_results.get(gid, {})
        if t.get("status") != t.get("gate1_status"):
            status_mismatch.append({"generation_id": gid, "gate1_status": t.get("gate1_status"), "trace_status": t.get("status")})
        if t.get("status") == "failed":
            trace_code = (t.get("failure") or {}).get("code")
            if trace_code != t.get("gate1_failure_code"):
                code_mismatch.append({"generation_id": gid, "gate1_failure_code": t.get("gate1_failure_code"), "trace_failure_code": trace_code, "step": (t.get("failure") or {}).get("step")})
        # independent cross-check against the original Gate 1 row
        row_code = row.get("failure_code")
        if t.get("status") == "failed" and row_code != (t.get("failure") or {}).get("code"):
            cross_check_mismatch.append({"generation_id": gid, "gate1_row_failure_code": row_code, "trace_failure_code": (t.get("failure") or {}).get("code")})

    report["status_mismatch_count"] = len(status_mismatch)
    report["status_mismatch"] = status_mismatch
    report["failure_code_mismatch_count"] = len(code_mismatch)
    report["failure_code_mismatch"] = code_mismatch
    report["cross_check_against_gate1_row_mismatch_count"] = len(cross_check_mismatch)
    report["cross_check_against_gate1_row_mismatch"] = cross_check_mismatch

    # failed traces that do not carry a failure object at all
    report["failed_without_failure_object"] = sorted(
        t["generation_id"] for t in traces if t.get("status") == "failed" and not t.get("failure")
    )

    # --- artifact coverage ---
    def listing(sub: str) -> set[str]:
        d = diag / sub
        if not d.is_dir():
            return set()
        return {p.stem for p in d.iterdir() if p.is_file()}

    previews = listing("previews")
    originals = listing("originals")
    sheets = sorted(p.name for p in (diag / "sheets").iterdir() if p.is_file()) if (diag / "sheets").is_dir() else []
    report["files"] = {
        "previews": len(previews),
        "sheets": len(sheets),
        "originals": len(originals),
        "trace_jsonl": 1 if (diag / "trace.jsonl").is_file() else 0,
    }
    report["previews_missing_for_traced_ids"] = sorted(traced_set - previews)
    report["previews_without_traced_id"] = sorted(previews - traced_set)
    report["originals_not_traced"] = sorted(originals - traced_set)
    report["originals_not_failed"] = sorted(originals - traced_failed)
    report["sheet_pages"] = sheets

    # --- sizes ---
    def total_size(sub: str) -> dict:
        d = diag / sub
        if not d.is_dir():
            return {"files": 0, "bytes": 0}
        files = [p for p in d.iterdir() if p.is_file()]
        return {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}

    sizes = {sub: total_size(sub) for sub in ("previews", "sheets", "originals")}
    sizes["trace.jsonl"] = {"files": 1, "bytes": (diag / "trace.jsonl").stat().st_size}
    sizes["diag_total"] = {"files": sum(v["files"] for v in sizes.values()), "bytes": sum(v["bytes"] for v in sizes.values())}
    report["sizes_bytes"] = sizes

    # --- shots distribution for failed traces (context, factual) ---
    report["failed_shots_distribution"] = dict(Counter(t.get("shots") for t in traces if t.get("status") == "failed"))
    report["passed_shots_distribution"] = dict(Counter(t.get("shots") for t in traces if t.get("status") == "passed"))
    report["failed_layout_distribution"] = dict(Counter(str(t.get("layout")) for t in traces if t.get("status") == "failed"))
    report["failure_step_counts"] = dict(Counter((t.get("failure") or {}).get("step") for t in traces if t.get("status") == "failed"))

    # --- which projects the failed traces belong to (bucket distribution) ---
    proj_counter: dict[str, Counter] = defaultdict(Counter)
    for t in traces:
        proj_counter[str(t.get("gate1_failure_code"))][str(t.get("project_id"))] += 1
    report["failed_projects_per_code"] = {k: dict(v) for k, v in proj_counter.items()}

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({k: report[k] for k in (
        "trace_rows", "trace_status_counts", "source_bytes_sha_mismatch_count",
        "status_mismatch_count", "failure_code_mismatch_count",
        "cross_check_against_gate1_row_mismatch_count", "files",
    )}, ensure_ascii=False))


if __name__ == "__main__":
    main()
