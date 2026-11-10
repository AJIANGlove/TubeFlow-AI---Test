"""Round 1 (read-only): export what the video model was told for each group, plus the images.

Run inside the web container on the deployment machine (same pattern as the 3a script):

    docker cp motion_round1_export.py tubeflowai-test-web-1:/tmp/
    docker exec -e OUT=/tmp/motion_round1 tubeflowai-test-web-1 python /tmp/motion_round1_export.py
    docker cp tubeflowai-test-web-1:/tmp/motion_round1 ./results

Every database query runs in a READ ONLY transaction; object storage is only read (GET).
No Provider is called, nothing is written to the database or the bucket.

Writes:
  OUT/prompts.json          one record per group of each project (current video attempt):
                            seed, node, strategy, compiled H3 prompt, compiler input
                            (director shots, shot time ranges, visual style), motion metrics.
  OUT/focus/<p8>_g<seq>/     for the focus groups: character, per-shot panel and storyboard
                            images (JPEG copies, long side <= 1600 px; originals' sha256 and
                            size recorded in images.json) and the storyboard request with any
                            URL removed (storyboard_input.json).
  OUT/manifest.json          counts, missing items and errors (never silently skipped).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import re
from typing import Any

from minio import Minio
from PIL import Image
from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.config import Settings

PROJECTS = (
    "2280f7c6-f0c7-439a-8d82-71c52b588cbf",  # Maya, 535 sentences, contains the boat shot
    "93d717fa-e23c-4ee6-86dc-f5558088003b",  # E2E ko-KR project, different visual style
)
# Groups whose shots were measured on 2026-11-09 (boat, lakeshore, reservoir, city, shoreline,
# ruins) plus the two neighbours of the boat group.
FOCUS = {PROJECTS[0]: (115, 116, 117, 119, 125, 127, 128), PROJECTS[1]: (3,)}
MAX_SIDE = 1600
URL = re.compile(r"https?://", re.IGNORECASE)


def _obj(value: object) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _scrub(value: Any) -> Any:
    """Drop URLs (they may carry signatures) and raw base64 blobs from exported JSON."""
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        if URL.search(value):
            return "<url removed>"
        if value.startswith("data:") or (len(value) > 2000 and " " not in value):
            return f"<blob removed, {len(value)} chars>"
    return value


def _read_only(session) -> None:  # type: ignore[no-untyped-def]
    session.execute(text("SET TRANSACTION READ ONLY"))


def _attempt_record(session, group: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    row = session.execute(
        text(
            "select attempt_id, generation_id, node_id, stage, archive_stage, error_code, "
            "input_snapshot_json, request_json, probe_evidence_json, output_sha256 "
            "from video_generation_attempts where attempt_id = :a"
        ),
        {"a": group["attempt_id"]},
    ).mappings().one()
    snapshot = _obj(row["input_snapshot_json"]) or {}
    request = _obj(row["request_json"]) or {}
    probe = _obj(row["probe_evidence_json"]) or {}
    prompt_block = snapshot.get("video_prompt") or {}
    compiler_input = snapshot.get("compiler_input") or {}
    prompt = str(request.get("prompt") or "")
    return {
        "sequence": group["sequence"],
        "group_id": str(group["id"]),
        "start_ms": group["start_ms"],
        "end_ms": group["end_ms"],
        "attempt_id": str(row["attempt_id"]),
        "storyboard_generation_id": str(row["generation_id"]),
        "node_id": row["node_id"],
        "stage": row["stage"],
        "archive_stage": row["archive_stage"],
        "error_code": row["error_code"],
        "output_sha256": row["output_sha256"],
        "seed": (request.get("parameters") or {}).get("seed"),
        "strategy": prompt_block.get("strategy"),
        "compile_model": prompt_block.get("model"),
        "repaired": prompt_block.get("repaired"),
        "experiment": snapshot.get("experiment"),
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "compiler_input": _scrub(compiler_input),
        "reference_policy": _scrub(snapshot.get("reference_policy")),
        "motion_metrics": probe.get("motion_metrics"),
        "_snapshot_assets": {
            "character": [item.get("id") for item in snapshot.get("character_assets") or []],
            "panels": [
                {"id": item.get("id"), "shot_order": item.get("shot_order")}
                for item in snapshot.get("shot_panel_assets") or []
            ],
            "storyboard": (snapshot.get("storyboard") or {}).get("id"),
        },
    }


def _save_image(session, client, settings, asset_id: str, path: pathlib.Path) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    row = session.execute(
        text("select object_key, media_type, sha256 from media_assets where id = cast(:i as uuid)"),
        {"i": asset_id},
    ).mappings().one_or_none()
    if row is None:
        return {"asset_id": asset_id, "error": "media_asset_not_found"}
    response = client.get_object(settings.minio_bucket, row["object_key"])
    try:
        data = response.read()
    finally:
        response.close()
        response.release_conn()
    image = Image.open(io.BytesIO(data)).convert("RGB")
    original = image.size
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    image.save(path, "JPEG", quality=90)
    digest = hashlib.sha256(data).hexdigest()
    return {
        "asset_id": asset_id,
        "file": path.name,
        "media_type": row["media_type"],
        "original_size": list(original),
        "original_bytes": len(data),
        "original_sha256": digest,
        "sha256_matches_db": digest == row["sha256"],
    }


def main() -> None:
    out = pathlib.Path(os.environ.get("OUT", "/tmp/motion_round1"))
    (out / "focus").mkdir(parents=True, exist_ok=True)
    settings = Settings()
    database = Database(settings.database_url)
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    manifest: dict[str, Any] = {"projects": {}, "errors": []}
    export: dict[str, Any] = {}
    try:
        for project_id in PROJECTS:
            with database.session() as session:
                _read_only(session)
                title = session.execute(
                    text("select title from projects where id = cast(:p as uuid)"),
                    {"p": project_id},
                ).scalar_one_or_none()
                if title is None:
                    manifest["errors"].append({"project_id": project_id, "error": "project_not_found"})
                    continue
                groups = session.execute(
                    text(
                        "select g.id, g.sequence, g.start_ms, g.end_ms, c.attempt_id "
                        "from authoritative_narration_groups g "
                        "left join group_current_videos c on c.group_id = g.id "
                        "where g.project_id = cast(:p as uuid) order by g.sequence"
                    ),
                    {"p": project_id},
                ).mappings().all()
                records = []
                missing = []
                for group in groups:
                    if group["attempt_id"] is None:
                        missing.append(group["sequence"])
                        continue
                    records.append(_attempt_record(session, dict(group)))
            styles = {
                str((record["compiler_input"] or {}).get("visual_style") or "") for record in records
            }
            export[project_id] = {
                "title": title,
                "visual_styles": sorted(styles),
                "groups": [
                    {key: value for key, value in record.items() if not key.startswith("_")}
                    for record in records
                ],
            }
            manifest["projects"][project_id] = {
                "title": title,
                "groups": len(groups),
                "groups_with_current_video": len(records),
                "groups_without_current_video": missing,
                "strategies": sorted({str(record["strategy"]) for record in records}),
                "distinct_visual_styles": len(styles),
            }
            for record in records:
                if record["sequence"] not in FOCUS.get(project_id, ()):
                    continue
                folder = out / "focus" / f"{project_id[:8]}_g{record['sequence']:03d}"
                folder.mkdir(parents=True, exist_ok=True)
                assets = record["_snapshot_assets"]
                images = []
                with database.session() as session:
                    _read_only(session)
                    for index, asset_id in enumerate(assets["character"], start=1):
                        images.append(
                            {"role": f"character_{index}", **_save_image(
                                session, client, settings, asset_id, folder / f"character_{index}.jpg"
                            )}
                        )
                    for panel in assets["panels"]:
                        name = f"panel_shot{panel['shot_order']}.jpg"
                        images.append(
                            {"role": f"panel_shot{panel['shot_order']}", **_save_image(
                                session, client, settings, panel["id"], folder / name
                            )}
                        )
                    if assets["storyboard"]:
                        images.append(
                            {"role": "storyboard", **_save_image(
                                session, client, settings, assets["storyboard"], folder / "storyboard.jpg"
                            )}
                        )
                    storyboard = session.execute(
                        text(
                            "select kind, version, provider, input_json from storyboard_generations "
                            "where id = cast(:g as uuid)"
                        ),
                        {"g": record["storyboard_generation_id"]},
                    ).mappings().one_or_none()
                (folder / "images.json").write_text(
                    json.dumps(images, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                (folder / "storyboard_input.json").write_text(
                    json.dumps(
                        _scrub(
                            {
                                "kind": storyboard["kind"],
                                "version": storyboard["version"],
                                "provider": storyboard["provider"],
                                "input": _obj(storyboard["input_json"]),
                            }
                        )
                        if storyboard
                        else {"error": "storyboard_generation_not_found"},
                        ensure_ascii=False,
                        indent=1,
                    ),
                    encoding="utf-8",
                )
                (folder / "prompt.txt").write_text(record["prompt"], encoding="utf-8")
                manifest["errors"].extend(
                    {"focus": folder.name, **item} for item in images if "error" in item
                )
        (out / "prompts.json").write_text(
            json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=1))
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()
