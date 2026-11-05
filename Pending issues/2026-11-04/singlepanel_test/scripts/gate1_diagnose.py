"""Gate 1 diagnostics for crop-grid-v2 (read-only, no provider calls).

For every failed generation in gate1_results.jsonl (and a fixed sample of passed ones),
fetch the original storyboard bytes from MinIO (or read them from --local-dir), trace
crop-grid-v2 step by step, and write:

  <out>/previews/<generation_id>.jpg   half-size preview with the trace drawn on it
      cyan   = expected grid lines           green  = gutter bands found
      yellow = dark tail (black empty cells)  red    = where it failed
      white  = final crop boxes (passed only)
  <out>/sheets/<category>_<n>.jpg       contact sheets, 12 previews per page
  <out>/trace.jsonl                     one JSON record per generation
  <out>/originals/<generation_id>.png   full-size originals of a small failed sample

Usage (inside the deployed image, same env as gate1_crop_dryrun.py):
  python gate1_diagnose.py --results gate1_results.jsonl --out diag \
      --passed-sample 36 --originals 15
Local test mode (no MinIO): add --local-dir DIR with files named <generation_id>.png.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
from collections import defaultdict
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from tubeflow_test.domain.storyboards import grid_layout_for
from tubeflow_test.video_generation import panel_images as pi
from tubeflow_test.video_generation.domain import VideoRuleViolation


def _fetch(row: dict, local_dir: Path | None) -> bytes:
    if local_dir is not None:
        return (local_dir / f"{row['generation_id']}.png").read_bytes()
    from minio import Minio

    client = Minio(
        os.environ["TUBEFLOW_MINIO_ENDPOINT"],
        access_key=os.environ["TUBEFLOW_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_MINIO_SECRET_KEY"],
        secure=False,
    )
    response = client.get_object(os.environ["TUBEFLOW_MINIO_BUCKET"], str(row["object_key"]))
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def trace_one(content: bytes, shots: int) -> dict:
    """Run the real crop with recording wrappers around its search steps (§47 algorithm)."""
    trace: dict = {"expected": [], "bands": [], "dark_tails": [], "boxes": [], "failure": None}
    with Image.open(BytesIO(content)) as opened:
        image = opened.convert("RGB")
    trace["size"] = list(image.size)
    layout = grid_layout_for(shots, "16:9", "grid-v1")
    trace["layout"] = [layout.rows, layout.cols]
    originals = (pi._v2_spans, pi._v2_separator, pi._v2_wide_separator, pi._v2_dark_tail)
    context = {"axis": "y", "offset": 0, "row_tops": [], "last": None}

    def spans(region, axis, lo, hi, total, used, pixels):  # type: ignore[no-untyped-def]
        context["axis"] = axis
        context["offset"] = context["row_tops"].pop(0) if axis == "x" and context["row_tops"] else 0
        result = originals[0](region, axis, lo, hi, total, used, pixels)
        if axis == "y":
            context["row_tops"] = [top for top, _bottom in result]
        return result

    def record(kind, expected, window, found):  # type: ignore[no-untyped-def]
        item = {"axis": context["axis"], "at": expected, "offset": context["offset"], "kind": kind}
        trace["expected"].append(item)
        context["last"] = {**item, "radius": window}
        if found is not None:
            trace["bands"].append(
                {"axis": context["axis"], "start": found[0], "end": found[1],
                 "offset": context["offset"], "kind": kind}
            )

    def separator(means, expected, radius, lo, hi, pixels, **kwargs):  # type: ignore[no-untyped-def]
        found = originals[1](means, expected, radius, lo, hi, pixels, **kwargs)
        record("strict", expected, radius, found)
        return found

    def wide(region, axis, means, expected, window, lo, hi, pixels, **kwargs):  # type: ignore[no-untyped-def]
        found = originals[2](region, axis, means, expected, window, lo, hi, pixels, **kwargs)
        record("wide", expected, window, found)
        return found

    def tail(means, expected, radius, lo, hi, cell, pixels):  # type: ignore[no-untyped-def]
        start = originals[3](means, expected, radius, lo, hi, cell, pixels)
        trace["dark_tails"].append(
            {"axis": context["axis"], "start": start, "end": hi, "offset": context["offset"]}
        )
        return start

    pi._v2_spans, pi._v2_separator, pi._v2_wide_separator, pi._v2_dark_tail = (
        spans, separator, wide, tail
    )
    try:
        trace["boxes"] = [list(box) for box in pi.grid_v2_panel_boxes(image, layout, shots)]
    except VideoRuleViolation as exc:
        trace["failure"] = {"code": str(exc), **(context["last"] or {})}
    finally:
        pi._v2_spans, pi._v2_separator, pi._v2_wide_separator, pi._v2_dark_tail = originals
    trace["status"] = "failed" if trace["failure"] else "passed"
    if trace["failure"]:
        trace["failure"]["step"] = f"{trace['failure'].get('axis', '?')} {trace['failure'].get('kind', '')}"
    return trace


def preview(content: bytes, trace: dict) -> Image.Image:
    with Image.open(BytesIO(content)) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for item in trace["expected"]:
        at = round(item["at"])
        if item["axis"] == "x":
            draw.line((at, item["offset"], at, height), fill=(0, 255, 255), width=2)
        else:
            draw.line((0, at, width, at), fill=(0, 255, 255), width=2)
    for item in trace["bands"]:
        if item["axis"] == "x":
            draw.rectangle((item["start"], item["offset"], item["end"], height), outline=(0, 255, 0), width=3)
        else:
            draw.rectangle((0, item["start"], width, item["end"]), outline=(0, 255, 0), width=3)
    for item in trace["dark_tails"]:
        if item["axis"] == "x":
            draw.rectangle((item["start"], item["offset"], item["end"], height), outline=(255, 255, 0), width=4)
        else:
            draw.rectangle((0, item["start"], width, item["end"]), outline=(255, 255, 0), width=4)
    failure = trace["failure"]
    if failure:
        if "box" in failure:
            box = failure["box"]
            draw.rectangle(box, outline=(255, 0, 0), width=6)
            if failure["axis"] == "x":
                draw.rectangle((box[0] + failure["start"], box[1], box[0] + failure["end"], box[3]), fill=(255, 0, 0))
            else:
                draw.rectangle((box[0], box[1] + failure["start"], box[2], box[1] + failure["end"]), fill=(255, 0, 0))
        elif "at" in failure:
            at, radius = failure["at"], failure["radius"]
            if failure["axis"] == "x":
                draw.rectangle((at - radius, failure["offset"], at + radius, height), outline=(255, 0, 0), width=6)
            else:
                draw.rectangle((0, at - radius, width, at + radius), outline=(255, 0, 0), width=6)
    else:
        for box in trace["boxes"]:
            draw.rectangle(box, outline=(255, 255, 255), width=4)
    return image.resize((width // 2, height // 2), Image.Resampling.LANCZOS)


def contact_sheets(items: list[tuple[str, Image.Image]], out: Path, category: str) -> None:
    per_page, cols = 12, 3
    for page in range(0, len(items), per_page):
        chunk = items[page : page + per_page]
        cell_w, cell_h = 836, 470 + 28
        rows = math.ceil(len(chunk) / cols)
        sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (255, 255, 255))
        draw = ImageDraw.Draw(sheet)
        for index, (caption, image) in enumerate(chunk):
            x, y = (index % cols) * cell_w, (index // cols) * cell_h
            sheet.paste(image.resize((cell_w - 6, 470 - 6)), (x + 3, y + 25))
            draw.text((x + 6, y + 6), caption, fill=(0, 0, 0))
        sheet.save(out / f"{category}_{page // per_page + 1:02d}.jpg", quality=85)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--passed-sample", type=int, default=36)
    parser.add_argument("--originals", type=int, default=15)
    parser.add_argument("--local-dir", default=None)
    args = parser.parse_args()
    out = Path(args.out)
    for sub in ("previews", "sheets", "originals"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    local_dir = Path(args.local_dir) if args.local_dir else None

    rows = [json.loads(line) for line in Path(args.results).read_text(encoding="utf-8").splitlines() if line.strip()]
    failed = [row for row in rows if row["status"] != "passed"]
    passed = [row for row in rows if row["status"] == "passed"]
    sample = random.Random(46).sample(passed, min(args.passed_sample, len(passed)))

    # Original sample: spread across failure codes and projects (deterministic).
    by_bucket: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in failed:
        by_bucket[(str(row["failure_code"]), str(row["project_id"]))].append(row)
    original_ids: list[str] = []
    while len(original_ids) < args.originals and any(by_bucket.values()):
        for key in sorted(by_bucket):
            if by_bucket[key] and len(original_ids) < args.originals:
                original_ids.append(str(by_bucket[key].pop(0)["generation_id"]))

    categories: dict[str, list[tuple[str, Image.Image]]] = defaultdict(list)
    with (out / "trace.jsonl").open("w", encoding="utf-8") as handle:
        for row in failed + sample:
            content = _fetch(row, local_dir)
            if hashlib.sha256(content).hexdigest() != row["expected_sha256"]:
                record = {"generation_id": row["generation_id"], "error": "source_bytes_sha_mismatch"}
                handle.write(json.dumps(record) + "\n")
                continue
            trace = trace_one(content, int(row["shots"]))
            record = {
                "generation_id": row["generation_id"],
                "project_id": row["project_id"],
                "shots": row["shots"],
                "gate1_status": row["status"],
                "gate1_failure_code": row.get("failure_code"),
                **trace,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            image = preview(content, trace)
            image.save(out / "previews" / f"{row['generation_id']}.jpg", quality=85)
            if row["status"] == "passed":
                category = "passed_sample"
            else:
                category = f"failed_{row['failure_code']}"
            step = (trace["failure"] or {}).get("step", "")
            caption = f"{str(row['generation_id'])[:8]} {str(row['project_id'])[:8]} shots={row['shots']} {step}"
            categories[category].append((caption, image))
            if str(row["generation_id"]) in original_ids:
                (out / "originals" / f"{row['generation_id']}.png").write_bytes(content)
    for category, items in categories.items():
        contact_sheets(items, out / "sheets", category)
    print(json.dumps({key: len(value) for key, value in categories.items()}, indent=1))


if __name__ == "__main__":
    main()
