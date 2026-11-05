"""Gates 3-5 collection for the §46 switch-on test project (read-only; no Provider calls).

Run inside the deployed web container (Settings, DB, MinIO and ffmpeg available):

    docker compose exec -T -e PROJECT_ID=<uuid> -e OUT=/tmp/gate3_5 \
        [-e EXPORT=1] [-e AM4=/tmp/am4.py] web python - < gate3_5_collect.py

Writes to OUT:
  groups.json   per group: shots, v2 layout, every storyboard attempt (status, grid version,
                failure code), empty-cell luminance of the current storyboard, every video
                attempt with its Gate 3-4 checks, current attempt, HF (if AM4 given)
  checks.json   Gate 3-4 summary (one boolean per FINAL_ACCEPTANCE §42 criterion + counts)
  evidence/     only with EXPORT=1:
                sb_<N>.png          current storyboard original
                panels_<N>.png      the shot panels actually sent to H3 (current attempt), in order
                v_<N>.mp4           current group video (do not commit; keep on the deploy host)
                sheet_v_<N>.png     1 fps, 640 px wide, 4x4 tile (16 s)
                sheet2_v_<N>.png    fa3a0b30 format: 2 fps, 352 px wide, 5x6 tile
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageStat
from tubeflow_test.domain.storyboards import GRID_LAYOUT_V2, grid_layout_for, panel_row_column

EXPECTED_POLICY = "single_panel_v1"
EXPECTED_DERIVATION = "crop-grid-v2"
EXPECTED_STRATEGY = "tubeflow_storyboard_v4_single_panel"
LADDER_ERROR = "single_panel_automatic_resubmit_not_supported"
LAYOUT_FAILURE = "storyboard_panel_layout_unreadable"
DARK_MAX = 64.0


def _obj(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value if value is not None else {}


def empty_cells(image: Image.Image, shots: int) -> list[dict[str, Any]]:
    """Mean luminance of each v2 empty cell (central 80% of the equal-division cell)."""
    layout = grid_layout_for(shots, "16:9", GRID_LAYOUT_V2)
    gray = image.convert("L")
    width, height = gray.size
    cell_w, cell_h = width / layout.cols, height / layout.rows
    result = []
    for order in layout.empty_panels:
        row, col = panel_row_column(order, layout.cols)  # 1-based
        left, top = (col - 1) * cell_w, (row - 1) * cell_h
        box = (
            round(left + 0.1 * cell_w),
            round(top + 0.1 * cell_h),
            round(left + 0.9 * cell_w),
            round(top + 0.9 * cell_h),
        )
        mean = ImageStat.Stat(gray.crop(box)).mean[0]
        result.append({"cell": order, "mean": round(mean, 1), "dark": mean <= DARK_MAX})
    return result


def check_attempt(attempt: dict[str, Any], shots: int | None) -> dict[str, Any]:
    """Gate 3-4 checks for one video attempt (snapshot + request)."""
    snapshot = _obj(attempt.get("input_snapshot_json"))
    request = _obj(attempt.get("request_json"))
    if not snapshot:
        return {"has_snapshot": False}
    policy = snapshot.get("reference_policy") or {}
    derivation = policy.get("derivation") or {}
    panels = snapshot.get("shot_panel_assets") or []
    characters = snapshot.get("character_assets") or []
    storyboard_sha = (snapshot.get("storyboard") or {}).get("sha256")
    # Staging pops request_json.references and replaces it with staged_assets (ids only, no sha):
    # orchestration.py "execution_request['staged_assets']". The logical request kept in the
    # snapshot carries the same references with their sha256, so read it when references are gone.
    if "references" in request:
        references, reference_source = request.get("references") or [], "request_json"
    else:
        references = (snapshot.get("logical_request") or {}).get("references") or []
        reference_source = "logical_request"
    ref_shas = [str(ref.get("asset_sha256")) for ref in references]
    staged = request.get("staged_assets")
    panel_shas = [str(panel.get("sha256")) for panel in panels]
    orders = [panel.get("shot_order") for panel in panels]
    strategy = (snapshot.get("video_prompt") or {}).get("strategy")
    return {
        "has_snapshot": True,
        "policy_ok": policy.get("strategy") == EXPECTED_POLICY and derivation.get("version") == EXPECTED_DERIVATION,
        "strategy": strategy,
        "strategy_ok": strategy == EXPECTED_STRATEGY,
        "panels": len(panels),
        "panels_match_shots": shots is not None and len(panels) == shots,
        "shot_orders_ok": orders == list(range(1, len(panels) + 1)),
        "storyboard_not_sent": storyboard_sha is not None and storyboard_sha not in ref_shas,
        "each_panel_sent_once": all(ref_shas.count(sha) == 1 for sha in panel_shas),
        "reference_count_ok": bool(ref_shas)
        and len(ref_shas) == len(characters) + len(panels)
        and (staged is None or len(staged) == len(ref_shas)),
        "references": len(ref_shas),
        "reference_source": reference_source,
        "staged": None if staged is None else len(staged),
        "characters": len(characters),
    }


def attempt_ok(check: dict[str, Any]) -> bool:
    keys = (
        "policy_ok",
        "strategy_ok",
        "panels_match_shots",
        "shot_orders_ok",
        "storyboard_not_sent",
        "each_panel_sent_once",
        "reference_count_ok",
    )
    return bool(check.get("has_snapshot")) and all(check.get(key) for key in keys)


def panel_strip(images: list[Image.Image], height: int = 360) -> Image.Image:
    scaled = [image.resize((max(1, round(image.width * height / image.height)), height)) for image in images]
    gap, label = 8, 22
    strip = Image.new("RGB", (sum(i.width for i in scaled) + gap * (len(scaled) - 1), height + label), "white")
    draw = ImageDraw.Draw(strip)
    x = 0
    for index, image in enumerate(scaled, 1):
        strip.paste(image.convert("RGB"), (x, label))
        draw.text((x + 4, 4), f"Shot {index}  {images[index - 1].width}x{images[index - 1].height}", fill="black")
        x += image.width + gap
    return strip


def main() -> None:  # pragma: no cover - needs the deployed DB and MinIO
    from minio import Minio
    from sqlalchemy import text
    from tubeflow_test.adapters.persistence.database import Database
    from tubeflow_test.config import Settings

    project = os.environ["PROJECT_ID"]
    out = Path(os.environ.get("OUT", "/tmp/gate3_5"))
    export = os.environ.get("EXPORT") == "1"
    am4 = os.environ.get("AM4")
    (out / "evidence").mkdir(parents=True, exist_ok=True)
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

    def fetch_asset(asset_id: str) -> bytes:
        key = rows("select object_key from media_assets where id = :i", i=asset_id)[0]["object_key"]
        response = client.get_object(settings.minio_bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    groups_out: list[dict[str, Any]] = []
    all_checks: list[dict[str, Any]] = []
    for group in rows(
        "select id, sequence from authoritative_narration_groups where project_id = :p order by sequence",
        p=project,
    ):
        gid, seq = str(group["id"]), int(group["sequence"])
        storyboards = rows(
            "select id, version, status, input_json, checkpoint_json, provider, failover_total_attempt, "
            "created_at from storyboard_generations where group_id = :g order by version",
            g=gid,
        )
        shots_by_generation: dict[str, int] = {}
        sb_items = []
        for sb in storyboards:
            inputs, checkpoint = _obj(sb["input_json"]), _obj(sb["checkpoint_json"])
            shots = len(inputs.get("shots") or [])
            shots_by_generation[str(sb["id"])] = shots
            sb_items.append(
                {
                    "id": str(sb["id"]),
                    "version": sb["version"],
                    "status": sb["status"],
                    "grid_layout_version": inputs.get("grid_layout_version"),
                    "shots": shots,
                    "provider": sb["provider"],
                    "failover_total_attempt": sb["failover_total_attempt"],
                    "failure_code": checkpoint.get("failure_code"),
                    "created_at": str(sb["created_at"]),
                }
            )
        current_sb = rows("select generation_id from storyboard_current where group_id = :g", g=gid)
        current_sb_id = str(current_sb[0]["generation_id"]) if current_sb and current_sb[0]["generation_id"] else None
        shots = shots_by_generation.get(current_sb_id or "")
        record: dict[str, Any] = {
            "sequence": seq,
            "group_id": gid,
            "shots": shots,
            "layout_v2": None,
            "storyboard_attempts": len(sb_items),
            "storyboard_failure_codes": dict(Counter(i["failure_code"] for i in sb_items if i["failure_code"])),
            "layout_unreadable": sum(1 for i in sb_items if i["failure_code"] == LAYOUT_FAILURE),
            "all_storyboards_v2": bool(sb_items) and all(i["grid_layout_version"] == GRID_LAYOUT_V2 for i in sb_items),
            "storyboards": sb_items,
            "current_storyboard": current_sb_id,
        }
        if shots:
            layout = grid_layout_for(shots, "16:9", GRID_LAYOUT_V2)
            record["layout_v2"] = f"{layout.rows}x{layout.cols}"
        sb_image = None
        if current_sb_id:
            media = rows("select asset_id from storyboard_media where generation_id = :g", g=current_sb_id)
            if media:
                content = fetch_asset(str(media[0]["asset_id"]))
                sb_image = Image.open(BytesIO(content))
                record["storyboard_size"] = list(sb_image.size)
                if shots:
                    record["empty_cells"] = empty_cells(sb_image, shots)
                if export:
                    (out / "evidence" / f"sb_{seq}.png").write_bytes(content)
        attempts = rows(
            "select attempt_id, generation_id, superseded_attempt_id, stage, archive_stage, error_code, "
            "request_json, input_snapshot_json, archived_asset_json, updated_at "
            "from video_generation_attempts where group_id = :g order by updated_at",
            g=gid,
        )
        attempt_items = []
        for attempt in attempts:
            check = check_attempt(attempt, shots_by_generation.get(str(attempt["generation_id"])))
            item = {
                "attempt_id": str(attempt["attempt_id"]),
                "generation_id": str(attempt["generation_id"]),
                "superseded_attempt_id": str(attempt["superseded_attempt_id"])
                if attempt["superseded_attempt_id"]
                else None,
                "stage": attempt["stage"],
                "archive_stage": attempt["archive_stage"],
                "error_code": attempt["error_code"],
                "ok": attempt_ok(check),
                "check": check,
            }
            attempt_items.append(item)
            all_checks.append(item)
        record["video_attempts"] = attempt_items
        current_video = rows("select attempt_id from group_current_videos where group_id = :g", g=gid)
        current_attempt_id = str(current_video[0]["attempt_id"]) if current_video else None
        record["current_attempt"] = current_attempt_id
        current = next((a for a in attempts if str(a["attempt_id"]) == current_attempt_id), None)
        if export and current is not None:
            snapshot = _obj(current["input_snapshot_json"])
            panels = [Image.open(BytesIO(fetch_asset(str(p["id"])))) for p in snapshot.get("shot_panel_assets") or []]
            if panels:
                record["panel_sizes"] = [list(p.size) for p in panels]
                panel_strip(panels).save(out / "evidence" / f"panels_{seq}.png")
            archived = _obj(current["archived_asset_json"])
            if archived.get("asset_id"):
                video = out / "evidence" / f"v_{seq}.mp4"
                video.write_bytes(fetch_asset(str(archived["asset_id"])))
                record["video_sha256"] = archived.get("sha256")
                for name, vf in (
                    (f"sheet_v_{seq}.png", "fps=1,scale=640:-1,tile=4x4"),
                    (f"sheet2_v_{seq}.png", "fps=2,scale=352:-1,tile=5x6"),
                ):
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-y",
                            "-i",
                            str(video),
                            "-vf",
                            vf,
                            "-frames:v",
                            "1",
                            str(out / "evidence" / name),
                        ],
                        check=True,
                    )
                if am4:
                    result = subprocess.run([sys.executable, am4, str(video)], capture_output=True, text=True)
                    record["hf"] = json.loads(result.stdout.strip().splitlines()[-1])
        groups_out.append(record)
        print(json.dumps({"sequence": seq, "shots": shots, "attempts": len(attempt_items)}), flush=True)

    auto = rows(
        "select state, authorization_round, reason_code, payload from auto_pipeline_runs "
        "where project_id = :p order by updated_at desc",
        p=project,
    )
    auto_text = json.dumps(auto, default=str)
    checks = {
        "groups": len(groups_out),
        "all_groups_v2": all(g["all_storyboards_v2"] for g in groups_out),
        "every_attempt_single_panel": bool(all_checks)
        and all(a["ok"] for a in all_checks if a["check"].get("has_snapshot")),
        "attempts_total": len(all_checks),
        "attempts_without_snapshot": sum(1 for a in all_checks if not a["check"].get("has_snapshot")),
        "attempts_failing_checks": [
            a["attempt_id"] for a in all_checks if a["check"].get("has_snapshot") and not a["ok"]
        ],
        "strategies": dict(Counter(str(a["check"].get("strategy")) for a in all_checks)),
        "ladder_error_count": sum(1 for a in all_checks if LADDER_ERROR in str(a["error_code"]))
        + auto_text.count(LADDER_ERROR),
        "layout_unreadable_by_group": {g["sequence"]: g["layout_unreadable"] for g in groups_out},
        "empty_cells_by_group": {g["sequence"]: g.get("empty_cells") for g in groups_out if g.get("empty_cells")},
        "auto": auto,
    }
    (out / "groups.json").write_text(
        json.dumps(groups_out, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    (out / "checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in checks.items() if k != "auto"}, ensure_ascii=False, default=str))
    database.engine.dispose()


if __name__ == "__main__":
    main()
