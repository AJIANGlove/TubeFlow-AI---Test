"""Stratify the held-frame survey by video-prompt strategy and project visual style (read-only).

Inputs (all already on disk):
  db_export.tsv   attempt_id, strategy, visual_style, project_id, sequence  (read-only export)
  survey.jsonl    one line per current group video, with held_share / moving_windows / updated_at

Outputs into --out: joined.csv, strategy_date.{csv,md}, strategy_style.{csv,md},
visual_style_legend.csv, coverage.json.

Rules
  * date bucket uses the attempt's `updated_at` from survey.jsonl (UTC); 2026-11-03T00:00:00Z and
    later is "2026-11-03 and later", earlier is "before 2026-11-03";
  * held_share = null (moving_windows == 0) counts as unmeasured, never as 0;
  * rows with an `error` field are excluded from statistics and counted separately;
  * a cell with fewer than 10 videos is marked as a small sample.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

SPLIT = datetime(2026, 11, 3, tzinfo=UTC)
BUCKETS = ("before 2026-11-03", "2026-11-03 and later")
SMALL = 10


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def bucket_of(value: str | None) -> str:
    stamp = parse_ts(value)
    if stamp is None:
        return "unknown"
    return BUCKETS[0] if stamp < SPLIT else BUCKETS[1]


def read_export(path: Path) -> tuple[list[dict], int]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    seen, unique, duplicates = set(), [], 0
    for row in rows:
        key = row["attempt_id"]
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(row)
    return unique, duplicates


def read_survey(path: Path) -> tuple[dict[str, dict], int, int]:
    records: dict[str, dict] = {}
    duplicates = 0
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        total += 1
        item = json.loads(line)
        key = item.get("attempt_id")
        if key in records:
            duplicates += 1
            continue
        records[key] = item
    return records, total, duplicates


def cell(items: list[dict]) -> dict:
    measured = [i for i in items if i["held_share"] is not None and not i.get("error")]
    values = [i["held_share"] for i in measured]
    ge = sum(1 for v in values if v >= 0.2)
    return {
        "n_videos": len(items),
        "n_measured": len(values),
        "n_unmeasured": sum(1 for i in items if i["held_share"] is None and not i.get("error")),
        "n_error": sum(1 for i in items if i.get("error")),
        "n_missing_survey": sum(1 for i in items if not i["in_survey"]),
        "mean_held_share": round(statistics.fmean(values), 3) if values else None,
        "n_ge_0.2": ge,
        "share_ge_0.2": round(ge / len(values), 3) if values else None,
    }


def sample_flag(c: dict) -> str:
    if c["n_videos"] < SMALL:
        return "样本少"
    if c["n_measured"] < SMALL:
        return "样本少‡"
    return ""


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def fmt_mean(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--survey", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    db_rows, db_dupes = read_export(Path(args.db))
    survey, survey_total, survey_dupes = read_survey(Path(args.survey))

    joined: list[dict] = []
    for row in db_rows:
        item = survey.get(row["attempt_id"])
        joined.append(
            {
                "attempt_id": row["attempt_id"],
                "project_id": row["project_id"],
                "sequence": row["sequence"],
                "strategy": row["strategy"] or "(null)",
                "visual_style": row["visual_style"] or "(empty)",
                "in_survey": item is not None,
                "updated_at": (item or {}).get("updated_at"),
                "date_bucket": bucket_of((item or {}).get("updated_at")),
                "moving_windows": (item or {}).get("moving_windows"),
                "held_share": (item or {}).get("held_share"),
                "error": (item or {}).get("error"),
            }
        )

    db_ids = {row["attempt_id"] for row in db_rows}
    survey_only = [key for key in survey if key not in db_ids]

    # style labels by frequency (tie-break by sha1 of the original text for stability)
    counts: dict[str, int] = {}
    for row in joined:
        counts[row["visual_style"]] = counts.get(row["visual_style"], 0) + 1
    ordered = sorted(counts, key=lambda s: (-counts[s], hashlib.sha1(s.encode()).hexdigest()))
    labels = {style: f"S{i}" for i, style in enumerate(ordered, 1)}
    for row in joined:
        row["style_label"] = labels[row["visual_style"]]

    with (out / "visual_style_legend.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["label", "visual_style", "n_videos", "sha1_12"])
        for style in ordered:
            writer.writerow([labels[style], style, counts[style],
                             hashlib.sha1(style.encode()).hexdigest()[:12]])

    with (out / "joined.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["attempt_id", "project_id", "sequence", "strategy", "style_label",
                  "date_bucket", "updated_at", "in_survey", "moving_windows", "held_share", "error"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(joined)

    strategies = sorted({row["strategy"] for row in joined},
                        key=lambda s: -sum(1 for r in joined if r["strategy"] == s))

    # table 1: strategy x date bucket (+ ALL)
    rows1: list[dict] = []
    for strategy in strategies:
        subset = [r for r in joined if r["strategy"] == strategy]
        for bucket in BUCKETS:
            rows1.append({"strategy": strategy, "bucket": bucket,
                          **cell([r for r in subset if r["date_bucket"] == bucket])})
        extra = [r for r in subset if r["date_bucket"] not in BUCKETS]
        if extra:
            rows1.append({"strategy": strategy, "bucket": "unknown", **cell(extra)})
        rows1.append({"strategy": strategy, "bucket": "ALL", **cell(subset)})

    # table 2: strategy x style label (all combinations kept)
    rows2: list[dict] = []
    for strategy in strategies:
        subset = [r for r in joined if r["strategy"] == strategy]
        for style in ordered:
            rows2.append({"strategy": strategy, "style": labels[style],
                          **cell([r for r in subset if r["visual_style"] == style])})
        rows2.append({"strategy": strategy, "style": "ALL", **cell(subset)})

    def write_table(path: Path, rows: list[dict], key: str, header_key: str,
                    pivot: bool = False) -> None:
        fields = [header_key, key, "n_videos", "n_measured", "n_unmeasured", "n_error",
                  "n_missing_survey", "mean_held_share", "n_ge_0.2", "share_ge_0.2", "sample"]
        for row in rows:
            row["sample"] = sample_flag(row)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        lines = ["| " + " | ".join(fields) + " |", "|" + "---|" * len(fields)]
        for row in rows:
            lines.append("| " + " | ".join(
                [str(row[header_key]), str(row[key]), str(row["n_videos"]), str(row["n_measured"]),
                 str(row["n_unmeasured"]), str(row["n_error"]), str(row["n_missing_survey"]),
                 fmt_mean(row["mean_held_share"]), str(row["n_ge_0.2"]),
                 fmt_pct(row["share_ge_0.2"]), row["sample"] or "—"]
            ) + " |")
        if pivot:
            lines.append("")
            lines.append("均值透视（`·` = 该格没有测到值）：")
            lines.append("")
            lines.append("| strategy | " + " | ".join(ordered and [labels[s] for s in ordered] or []) + " | ALL |")
            lines.append("|---|" + "---|" * (len(ordered) + 1))
            for strategy in strategies:
                cells = []
                for style in ordered:
                    match = [r for r in rows2
                             if r["strategy"] == strategy and r["style"] == labels[style]]
                    value = match[0]["mean_held_share"] if match else None
                    cells.append("·" if value is None else f"{value:.3f}")
                all_row = [r for r in rows2 if r["strategy"] == strategy and r["style"] == "ALL"][0]
                cells.append(fmt_mean(all_row["mean_held_share"]))
                lines.append(f"| `{strategy}` | " + " | ".join(cells) + " |")
        path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    write_table(out / "strategy_date.csv", rows1, "bucket", "strategy")
    write_table(out / "strategy_style.csv", rows2, "style", "strategy", pivot=True)

    coverage = {
        "db_rows": len(db_rows),
        "db_duplicate_attempt_ids": db_dupes,
        "survey_rows": survey_total,
        "survey_duplicate_attempt_ids": survey_dupes,
        "matched": sum(1 for r in joined if r["in_survey"]),
        "db_only": sum(1 for r in joined if not r["in_survey"]),
        "survey_only": len(survey_only),
        "survey_only_sample": survey_only[:20],
        "n_unmeasured_total": sum(1 for r in joined if r["held_share"] is None and not r.get("error")),
        "n_error_total": sum(1 for r in joined if r.get("error")),
        "sum_check_rows1": sum(r["n_videos"] for r in rows1 if r["bucket"] == "ALL"),
        "sum_check_rows2": sum(r["n_videos"] for r in rows2 if r["style"] == "ALL"),
        "strategies": strategies,
        "style_labels": {labels[s]: {"style": s, "n_videos": counts[s]} for s in ordered},
        "split": "2026-11-03T00:00:00Z",
    }
    (out / "coverage.json").write_text(json.dumps(coverage, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    print(json.dumps({k: coverage[k] for k in ("db_rows", "survey_rows", "matched", "db_only",
                                               "survey_only", "n_unmeasured_total",
                                               "n_error_total", "sum_check_rows1",
                                               "sum_check_rows2")}, indent=1))


if __name__ == "__main__":
    main()
