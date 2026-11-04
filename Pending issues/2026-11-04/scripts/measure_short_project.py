"""Per-group measurement for the short test project (read-only except local /tmp).

For each group: fetch current storyboard image + current video, run HF jitter
(tmp/diag/am4.py algorithm) on the video, and write a contact sheet for both.
Prints JSON lines.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from minio import Minio
from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.config import Settings

PROJECT = "fa3a0b30-9222-4ca4-a475-3bfd6cc57220"
OUT = Path("/tmp/shortproj")
OUT.mkdir(exist_ok=True)

settings = Settings()
database = Database(settings.database_url)
client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_access_key,
    secret_key=settings.minio_secret_key,
    secure=False,
)


def fetch(asset_id: str, path: Path) -> None:
    with database.session() as session:
        key = session.execute(
            text("select object_key from media_assets where id = :i"), {"i": asset_id}
        ).scalar_one()
    client.fget_object(settings.minio_bucket, key, str(path))


def am4(path: Path) -> dict:
    out = subprocess.run(
        [sys.executable, "/tmp/am4.py", str(path)], capture_output=True, text=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


with database.session() as session:
    rows = session.execute(
        text(
            "select g.sequence, c.generation_id, v.attempt_id "
            "from authoritative_narration_groups g "
            "left join storyboard_current c on c.group_id = g.id "
            "left join group_current_videos v on v.group_id = g.id "
            "where g.project_id = :p order by g.sequence"
        ),
        {"p": PROJECT},
    ).all()

for sequence, generation_id, attempt_id in rows:
    record: dict[str, object] = {"sequence": sequence}
    with database.session() as session:
        sb_asset = session.execute(
            text("select asset_id from storyboard_media where generation_id = :g"),
            {"g": generation_id},
        ).scalar_one()
        attempt = session.execute(
            text(
                "select archived_asset_json->>'asset_id', "
                "archived_asset_json->>'sha256', stage, "
                "input_snapshot_json->'video_prompt'->>'strategy', "
                "input_snapshot_json->>'seed' "
                "from video_generation_attempts where attempt_id = :a"
            ),
            {"a": attempt_id},
        ).one()
    sb_path = OUT / f"sb_{sequence}.png"
    v_path = OUT / f"v_{sequence}.mp4"
    sheet_sb = OUT / f"sheet_sb_{sequence}.png"
    sheet_v = OUT / f"sheet_v_{sequence}.png"
    fetch(sb_asset, sb_path)
    fetch(attempt[0], v_path)
    record.update(
        storyboard_generation_id=str(generation_id),
        attempt_id=str(attempt_id),
        stage=attempt[2],
        strategy=attempt[3],
        seed=attempt[4],
        video_sha256=attempt[1],
        video_jitter=am4(v_path),
    )
    # storyboard is a single image; video sheet via ffmpeg
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(v_path),
            "-vf",
            "fps=2,scale=352:-1,tile=5x6",
            "-frames:v",
            "1",
            str(sheet_v),
        ],
        check=True,
    )
    record["paths"] = [str(sb_path), str(sheet_v)]
    print(json.dumps(record, ensure_ascii=False), flush=True)

database.engine.dispose()
