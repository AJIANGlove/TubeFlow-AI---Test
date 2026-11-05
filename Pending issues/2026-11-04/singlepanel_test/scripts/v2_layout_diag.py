"""Read-only diagnosis of v2 storyboards rejected as storyboard_panel_layout_unreadable.

For one project: per-group rejection counts, the project's aspect ratio and style, and for the most
recent rejected storyboards (whose downloaded image is still in the worker's work dir) a step-by-step
crop-grid-v2 trace drawn on a half-size preview, plus contact sheets. No writes to DB/MinIO, no
Provider calls. Trace colours: cyan expected grid lines, green gutter bands found, yellow dark tail,
red where it failed, white final boxes.

    docker compose exec -T -e PROJECT_ID=<uuid> -e OUT=/tmp/v2diag [-e LIMIT=24] \
        worker python - < v2_layout_diag.py
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import text
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.config import Settings
from tubeflow_test.domain.storyboards import grid_layout_for
from tubeflow_test.video_generation import panel_images as pi
from tubeflow_test.video_generation.domain import VideoRuleViolation

WORK_DIR = Path("var/storyboards")  # same as worker.py / app.py storyboard runtime work_dir
LAYOUT_FAILURE = "storyboard_panel_layout_unreadable"


def trace_one(content: bytes, shots: int, layout_version: str = "grid-v2") -> dict:
    """Run the real crop with recording wrappers around its search steps (§47 algorithm)."""
    trace: dict = {"expected": [], "bands": [], "dark_tails": [], "boxes": [], "failure": None}
    with Image.open(BytesIO(content)) as opened:
        image = opened.convert("RGB")
    trace["size"] = list(image.size)
    layout = grid_layout_for(shots, "16:9", layout_version)
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
                {"axis": context["axis"], "start": found[0], "end": found[1], "offset": context["offset"], "kind": kind}
            )

    def separator(means, expected, radius, lo, hi, pixels):  # type: ignore[no-untyped-def]
        found = originals[1](means, expected, radius, lo, hi, pixels)
        record("strict", expected, radius, found)
        return found

    def wide(region, axis, means, expected, window, lo, hi, pixels):  # type: ignore[no-untyped-def]
        found = originals[2](region, axis, means, expected, window, lo, hi, pixels)
        record("wide", expected, window, found)
        return found

    def tail(means, expected, radius, lo, hi, cell, pixels):  # type: ignore[no-untyped-def]
        start = originals[3](means, expected, radius, lo, hi, cell, pixels)
        trace["dark_tails"].append({"axis": context["axis"], "start": start, "end": hi, "offset": context["offset"]})
        return start

    pi._v2_spans, pi._v2_separator, pi._v2_wide_separator, pi._v2_dark_tail = (spans, separator, wide, tail)
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
    project = os.environ["PROJECT_ID"]
    out = Path(os.environ.get("OUT", "/tmp/v2diag"))
    limit = int(os.environ.get("LIMIT", "24"))
    for sub in ("previews", "sheets", "originals"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    settings = Settings()
    database = Database(settings.database_url)

    def rows(sql: str, **params: object) -> list[dict]:
        with database.session() as session:
            return [dict(row) for row in session.execute(text(sql), params).mappings().all()]

    project_row = rows("select * from projects where id = :p", p=project)
    keep = ("id", "title", "aspect_ratio", "locale", "visual_style", "channel_id", "production_mode", "created_at")
    summary: dict = {"project": {k: v for k, v in (project_row[0] if project_row else {}).items() if k in keep}}
    generations = rows(
        "select id, owner_id, group_id, status, input_json, checkpoint_json, created_at "
        "from storyboard_generations where project_id = :p order by created_at",
        p=project,
    )

    def obj(value: object) -> dict:
        return json.loads(value) if isinstance(value, str) else (value or {})  # type: ignore[return-value]

    per_group: dict[str, Counter] = {}
    rejected = []
    for row in generations:
        inputs, checkpoint = obj(row["input_json"]), obj(row["checkpoint_json"])
        counter = per_group.setdefault(str(row["group_id"]), Counter())
        counter[str(row["status"])] += 1
        code = checkpoint.get("failure_code")
        if code:
            counter[str(code)] += 1
        if code == LAYOUT_FAILURE:
            rejected.append((row, inputs, checkpoint))
    summary["generations"] = len(generations)
    summary["layout_versions"] = dict(
        Counter(str(obj(r["input_json"]).get("grid_layout_version")) for r in generations)
    )
    summary["rejected_total"] = len(rejected)
    summary["shots_of_rejected"] = dict(Counter(len(i.get("shots") or []) for _r, i, _c in rejected))
    summary["per_group"] = {g: dict(c) for g, c in per_group.items() if c.get(LAYOUT_FAILURE)}
    summary["checkpoint_sample"] = [{k: v for k, v in c.items() if k not in ("prompt",)} for _r, _i, c in rejected[-3:]]

    items: list[tuple[str, Image.Image]] = []
    traces = []
    for row, inputs, _checkpoint in rejected[-limit:]:
        path = WORK_DIR / str(row["owner_id"]) / str(row["id"]) / "grid.image"
        record: dict = {"generation_id": str(row["id"]), "group_id": str(row["group_id"]), "path_exists": path.exists()}
        if path.exists():
            content = path.read_bytes()
            shots = len(inputs.get("shots") or [])
            trace = trace_one(content, shots, str(inputs.get("grid_layout_version") or "grid-v1"))
            record.update(trace)
            image = preview(content, trace)
            image.save(out / "previews" / f"{row['id']}.jpg", quality=85)
            step = (trace["failure"] or {}).get("step", "")
            items.append((f"{str(row['id'])[:8]} shots={shots} {trace['status']} {step}", image))
            if len(list((out / "originals").iterdir())) < 8:
                (out / "originals" / f"{row['id']}.png").write_bytes(content)
        traces.append(record)
    if items:
        contact_sheets(items, out / "sheets", "rejected")
    summary["traced"] = sum(1 for t in traces if t.get("path_exists"))
    summary["trace_status"] = dict(Counter(str(t.get("status")) for t in traces if t.get("path_exists")))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    with (out / "trace.jsonl").open("w", encoding="utf-8") as handle:
        for record in traces:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    print(
        json.dumps(
            {k: summary[k] for k in ("generations", "layout_versions", "rejected_total", "traced", "trace_status")},
            default=str,
        )
    )
    database.engine.dispose()


if __name__ == "__main__":
    main()
