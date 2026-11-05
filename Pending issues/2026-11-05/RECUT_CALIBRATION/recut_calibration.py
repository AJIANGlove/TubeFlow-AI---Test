"""Offline calibration for the recut similarity threshold (USER_DECISIONS §48.4/§48.5).

Read-only: SELECTs from the database and GETs from MinIO only, no writes, no Provider calls.
Must run on an image that contains PR #10 (it imports recut-similarity-v1 from the product
code, so the numbers are exactly what the storyboard check would compute).

For every SUCCEEDED grid-v2 storyboard it crops the shot panels with crop-grid-v2 and
measures recut-similarity-v1 (1 - Pearson correlation of 64x36 grayscale thumbnails) for
every pair of panels in the same sheet:

  different       - the two shots have different compositions (baseline: the threshold
                    must sit clearly below this whole distribution)
  adjacent_same   - adjacent shots with the same scene/size/viewpoint (continuous
                    observation); the closest real-world analogue of a recut
  nonadjacent_same- should be empty (the old rule rejected these); listed if present

    docker compose cp recut_calibration.py worker:/tmp/recut_calibration.py
    docker compose exec -T -e OUT=/tmp/recut_cal [-e PROJECTS=<uuid>,<uuid>] [-e LIMIT=600] \
        worker python /tmp/recut_calibration.py
    docker compose cp worker:/tmp/recut_cal ./recut_cal

Outputs in OUT: summary.json (counts and percentiles per category, crop failures),
pairs.jsonl (one line per pair), closest_different_NN.jpg (the 36 closest different-shot
pairs side by side), same_composition_NN.jpg (same-composition pairs, closest first).
The threshold is NOT chosen here: the user picks it after looking at these sheets.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from collections.abc import Callable
from io import BytesIO
from itertools import combinations
from pathlib import Path
from typing import Any

from minio import Minio
from PIL import Image, ImageDraw
from sqlalchemy import text
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.storyboards.repository import _shot
from tubeflow_test.config import Settings
from tubeflow_test.domain.storyboards import (
    GRID_LAYOUT_V2,
    RECUT_SIMILARITY_V1,
    composition_key,
    grid_layout_for,
)
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.panel_images import grid_v2_panel_boxes, panel_distance

PERCENTILES = (0, 0.5, 1, 2, 5, 10, 25, 50)


def _obj(value: object) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})  # type: ignore[arg-type]


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    result = {}
    for p in PERCENTILES:
        index = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
        result[f"p{p}"] = round(ordered[index], 4)
    result["mean"] = round(statistics.fmean(ordered), 4)
    return result


def _pages(items: list[dict[str, Any]], crops: dict[tuple[str, int], Image.Image], out: Path, name: str) -> None:
    per_page, cell_w, cell_h, caption = 6, 2 * 480 + 12, 270 + 30, 26
    for page in range(0, len(items), per_page):
        chunk = items[page : page + per_page]
        sheet = Image.new("RGB", (cell_w, len(chunk) * cell_h), (255, 255, 255))
        draw = ImageDraw.Draw(sheet)
        for row, item in enumerate(chunk):
            y = row * cell_h
            for col, order in enumerate((item["earlier"], item["later"])):
                crop = crops[(item["generation_id"], order)]
                sheet.paste(crop.resize((480, 270)), (col * (480 + 12), y + caption))
            draw.text(
                (4, y + 6),
                f"{item['project_id'][:8]} g={item['group_id'][:8]} sb={item['generation_id'][:8]} "
                f"panels {item['earlier']}-{item['later']}  distance={item['distance']:.4f}  {item['category']}",
                fill=(0, 0, 0),
            )
        sheet.save(out / f"{name}_{page // per_page + 1:02d}.jpg", quality=85)


def measure(
    sheets: list[dict[str, Any]], fetch: Callable[[str], bytes]
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], Image.Image], dict[str, int]]:
    """Pairs of every measured sheet, panel thumbnails for the contact sheets, crop failures."""
    pairs: list[dict[str, Any]] = []
    crops: dict[tuple[str, int], Image.Image] = {}
    failures: dict[str, int] = {}
    for sheet in sheets:
        inputs = _obj(sheet["input_json"])
        shots = tuple(_shot(item) for item in inputs.get("shots") or [])
        if len(shots) < 2:
            continue
        content = fetch(sheet["object_key"])
        with Image.open(BytesIO(content)) as opened:
            image = opened.convert("RGB")
        try:
            layout = grid_layout_for(len(shots), "16:9", GRID_LAYOUT_V2)
            boxes = grid_v2_panel_boxes(image, layout, len(shots))
        except (VideoRuleViolation, ValueError) as exc:
            failures[str(exc)] = failures.get(str(exc), 0) + 1
            continue
        generation_id = str(sheet["id"])
        panel_crops = {shot.order: image.crop(boxes[shot.order - 1]) for shot in shots}
        for first, second in combinations(shots, 2):
            same = composition_key(first) == composition_key(second)
            category = (
                "different"
                if not same
                else "adjacent_same"
                if second.order - first.order == 1
                else "nonadjacent_same"
            )
            pairs.append(
                {
                    "project_id": str(sheet["project_id"]),
                    "group_id": str(sheet["group_id"]),
                    "generation_id": generation_id,
                    "earlier": first.order,
                    "later": second.order,
                    "gap": second.order - first.order,
                    "category": category,
                    "distance": panel_distance(panel_crops[first.order], panel_crops[second.order]),
                }
            )
        for order, crop in panel_crops.items():
            small = crop.copy()
            small.thumbnail((480, 270))
            crops[(generation_id, order)] = small
    return pairs, crops, failures


def main() -> None:
    out = Path(os.environ.get("OUT", "/tmp/recut_cal"))
    out.mkdir(parents=True, exist_ok=True)
    limit = int(os.environ.get("LIMIT", "600"))
    projects = [item.strip() for item in os.environ.get("PROJECTS", "").split(",") if item.strip()]
    settings = Settings()
    database = Database(settings.database_url)
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )

    def rows(sql: str, **params: Any) -> list[dict[str, Any]]:
        with database.session() as session:
            return [dict(row) for row in session.execute(text(sql), params).mappings().all()]

    where = "g.status = 'SUCCEEDED' and (g.input_json::jsonb ->> 'grid_layout_version') = :layout"
    params: dict[str, Any] = {"layout": GRID_LAYOUT_V2, "limit": limit}
    if projects:
        where += " and g.project_id::text = any(:projects)"
        params["projects"] = projects
    sheets = rows(
        "select g.id, g.project_id, g.group_id, g.input_json, m.asset_id, a.object_key "
        "from storyboard_generations g "
        "join storyboard_media m on m.generation_id = g.id "
        "join media_assets a on a.id = m.asset_id "
        f"where {where} order by g.created_at desc limit :limit",
        **params,
    )
    def fetch(object_key: str) -> bytes:
        response = client.get_object(settings.minio_bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    pairs, crops, failures = measure(sheets, fetch)
    by_category: dict[str, list[float]] = {}
    for item in pairs:
        by_category.setdefault(item["category"], []).append(item["distance"])
    summary = {
        "algorithm": RECUT_SIMILARITY_V1,
        "sheets_selected": len(sheets),
        "sheets_measured": len({item["generation_id"] for item in pairs}),
        "projects": sorted({item["project_id"] for item in pairs}),
        "crop_failures": failures,
        "categories": {
            name: {"pairs": len(values), **_percentiles(values)}
            for name, values in sorted(by_category.items())
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    with (out / "pairs.jsonl").open("w", encoding="utf-8") as handle:
        for item in pairs:
            handle.write(json.dumps(item) + "\n")
    ordered = sorted(pairs, key=lambda item: item["distance"])
    _pages([p for p in ordered if p["category"] == "different"][:36], crops, out, "closest_different")
    _pages([p for p in ordered if p["category"] != "different"][:36], crops, out, "same_composition")
    print(json.dumps(summary, indent=1))
    database.engine.dispose()


if __name__ == "__main__":
    main()
