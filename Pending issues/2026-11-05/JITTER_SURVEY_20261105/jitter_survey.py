"""How common is the held-frame ("on twos") stutter? Read-only survey of current group videos.

Read-only: SELECTs and MinIO GETs only; no writes, no Provider calls. Needs ffmpeg, the
product Settings (database, MinIO) and `held_frames.py` next to this file, so run it inside
the web container from a copied directory:

    docker compose cp "Pending issues/2026-11-05/JITTER_SURVEY_20261105" web:/tmp/jitter_survey
    docker compose exec -T -e OUT=/tmp/jitter_out [-e PROJECTS=<uuid>,<uuid>] \
        [-e SINCE=2026-10-01] [-e LIMIT=400] web python /tmp/jitter_survey/jitter_survey.py
    docker compose cp web:/tmp/jitter_out "Pending issues/2026-11-05/JITTER_SURVEY_20261105/results"

Selection: the current video of every group (group_current_videos) whose attempt was updated on
or after SINCE (default 2026-10-01), optionally limited to PROJECTS. Videos are streamed to a
temporary file one at a time and deleted after measuring.

Outputs in OUT: survey.jsonl (one line per group video) and summary.json (per project and
overall: videos, held_share mean/max, videos with held_share >= 0.2).
"""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from minio import Minio
from sqlalchemy import text
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.config import Settings

from held_frames import frame_differences, held_share

NOTABLE = 0.2


def _obj(value: object) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})  # type: ignore[arg-type]


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    shares = [row["held_share"] for row in rows if row.get("held_share") is not None]
    return {
        "videos": len(rows),
        "measured": len(shares),
        "held_share_mean": round(statistics.fmean(shares), 3) if shares else None,
        "held_share_max": max(shares) if shares else None,
        f"videos_held_share_ge_{NOTABLE}": sum(1 for value in shares if value >= NOTABLE),
    }


def main() -> None:
    out = Path(os.environ.get("OUT", "/tmp/jitter_out"))
    out.mkdir(parents=True, exist_ok=True)
    since = date.fromisoformat(os.environ.get("SINCE", "2026-10-01"))
    limit = int(os.environ.get("LIMIT", "400"))
    projects = [item.strip() for item in os.environ.get("PROJECTS", "").split(",") if item.strip()]
    settings = Settings()
    database = Database(settings.database_url)
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    where = "a.updated_at >= :since"
    params: dict[str, Any] = {"since": since, "limit": limit}
    if projects:
        where += " and g.project_id::text = any(:projects)"
        params["projects"] = projects
    with database.session() as session:
        videos = [
            dict(row)
            for row in session.execute(
                text(
                    "select g.project_id, g.sequence, a.attempt_id, a.archived_asset_json, "
                    "a.updated_at from group_current_videos c "
                    "join video_generation_attempts a on a.attempt_id = c.attempt_id "
                    "join authoritative_narration_groups g on g.id = c.group_id "
                    f"where {where} order by a.updated_at desc limit :limit"
                ),
                params,
            ).mappings()
        ]
    results: list[dict[str, Any]] = []
    for video in videos:
        record: dict[str, Any] = {
            "project_id": str(video["project_id"]),
            "sequence": video["sequence"],
            "attempt_id": str(video["attempt_id"]),
            "updated_at": video["updated_at"].isoformat(),
        }
        asset_id = _obj(video["archived_asset_json"]).get("asset_id")
        if not asset_id:
            record["error"] = "no_archived_asset"
            results.append(record)
            continue
        with database.session() as session:
            key = session.execute(
                text("select object_key from media_assets where id = :i"), {"i": asset_id}
            ).scalar_one()
        response = client.get_object(settings.minio_bucket, key)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
            try:
                for chunk in response.stream(1 << 20):
                    handle.write(chunk)
            finally:
                response.close()
                response.release_conn()
            handle.flush()
            try:
                record.update(held_share(frame_differences(handle.name)))
            except Exception as exc:  # measurement failures are recorded, not hidden
                record["error"] = f"{type(exc).__name__}: {exc}"[:200]
        results.append(record)
    with (out / "survey.jsonl").open("w", encoding="utf-8") as handle:
        for record in results:
            handle.write(json.dumps(record) + "\n")
    by_project: dict[str, list[dict[str, Any]]] = {}
    for record in results:
        by_project.setdefault(record["project_id"], []).append(record)
    summary = {
        "metric": "held-frame share (held_frames.py, same algorithm as cadence.py)",
        "since": since.isoformat(),
        "projects_filter": projects,
        "overall": _summary(results),
        "errors": sum(1 for record in results if "error" in record),
        "projects": {key: _summary(value) for key, value in sorted(by_project.items())},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary["overall"], indent=1))
    database.engine.dispose()


if __name__ == "__main__":
    main()
