"""Export the jitter A/B attempts (read-only): metadata + archived MP4s.

Read-only: SELECTs and MinIO GETs only; no writes, no Provider calls. Run inside the web
container from a copied directory (same pattern as ../jitter_survey.py):

    docker compose cp "Pending issues/2026-11-05/JITTER_SURVEY_20261105" web:/tmp/jitter_survey
    docker compose exec -T -e OUT=/tmp/jitter_ab_out -e ATTEMPTS=<uuid>,<uuid>,... \
        web python /tmp/jitter_survey/ab/ab_export.py
    docker compose exec -T web python /tmp/jitter_survey/ab/ab_measure.py /tmp/jitter_ab_out frames
    docker compose cp web:/tmp/jitter_ab_out "Pending issues/2026-11-05/JITTER_SURVEY_20261105/ab/results"
    # then, where numpy + cv2 are installed (no ffmpeg needed):
    python ab_measure.py "Pending issues/2026-11-05/JITTER_SURVEY_20261105/ab/results" hf

Writes OUT/attempts.json (one object per attempt, in the ATTEMPTS order) and
OUT/<attempt_id>.mp4 for every attempt that has an archived asset. Attempts that failed or
have no archive are exported with their stage and error code; they are never skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from minio import Minio
from sqlalchemy import text
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.config import Settings


def _obj(value: object) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})  # type: ignore[arg-type]


def main() -> None:
    out = Path(os.environ.get("OUT", "/tmp/jitter_ab_out"))
    out.mkdir(parents=True, exist_ok=True)
    attempts = [item.strip() for item in os.environ.get("ATTEMPTS", "").split(",") if item.strip()]
    if not attempts:
        raise SystemExit("ATTEMPTS must list the A/B attempt ids")
    settings = Settings()
    database = Database(settings.database_url)
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    records: list[dict[str, Any]] = []
    for attempt_id in attempts:
        with database.session() as session:
            row = session.execute(
                text(
                    "select a.attempt_id, a.project_id, g.sequence, a.group_id, a.generation_id, "
                    "a.idempotency_key, a.stage, a.archive_stage, a.error_code, a.node_id, "
                    "a.output_sha256, a.archived_asset_json, a.probe_evidence_json, "
                    "a.input_snapshot_json, a.request_json, a.updated_at "
                    "from video_generation_attempts a "
                    "join authoritative_narration_groups g on g.id = a.group_id "
                    "where a.attempt_id = cast(:a as uuid)"
                ),
                {"a": attempt_id},
            ).mappings().one_or_none()
        if row is None:
            records.append({"attempt_id": attempt_id, "error": "attempt_not_found"})
            continue
        snapshot = _obj(row["input_snapshot_json"])
        request = _obj(row["request_json"])
        prompt = str(request.get("prompt") or "")
        block = _obj(snapshot.get("video_prompt"))
        record: dict[str, Any] = {
            "attempt_id": str(row["attempt_id"]),
            "project_id": str(row["project_id"]),
            "group_sequence": row["sequence"],
            "group_id": str(row["group_id"]),
            "storyboard_generation_id": str(row["generation_id"]),
            "idempotency_key": row["idempotency_key"],
            "stage": row["stage"],
            "archive_stage": row["archive_stage"],
            "error_code": row["error_code"],
            "node_id": row["node_id"],
            "output_sha256": row["output_sha256"],
            "seed": _obj(request.get("parameters")).get("seed"),
            "strategy": block.get("strategy"),
            "experiment": snapshot.get("experiment"),
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "probe_evidence": _obj(row["probe_evidence_json"]),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        asset_id = _obj(row["archived_asset_json"]).get("asset_id")
        if asset_id:
            with database.session() as session:
                key = session.execute(
                    text("select object_key from media_assets where id = :i"), {"i": asset_id}
                ).scalar_one()
            path = out / f"{record['attempt_id']}.mp4"
            client.fget_object(settings.minio_bucket, key, str(path))
            record["video_file"] = path.name
            record["video_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(record)
    (out / "attempts.json").write_text(
        json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps([{k: r.get(k) for k in ("attempt_id", "stage", "video_file")} for r in records], indent=1))
    database.engine.dispose()


if __name__ == "__main__":
    main()
