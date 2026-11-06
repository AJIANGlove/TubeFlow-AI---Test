"""B: 只读导出 6 条已归档样片（DB SELECT + MinIO GET），在 web 容器内运行。

    docker exec -e ATTEMPTS=<id,id,...> -e OUT=/tmp/held_frame tubeflowai-test-web-1 \
        python /tmp/held_frame_export.py

不写库、不调 Provider；只按 media_assets.object_key 取对象。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib

from minio import Minio
from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.config import Settings

settings = Settings()
database = Database(settings.database_url)
out = pathlib.Path(os.environ.get("OUT", "/tmp/held_frame"))
out.mkdir(parents=True, exist_ok=True)
attempts = [x for x in os.environ["ATTEMPTS"].split(",") if x]

client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_access_key,
    secret_key=settings.minio_secret_key,
    secure=False,
)

records = []
with database.session() as session:
    for attempt_id in attempts:
        row = session.execute(
            text(
                "select a.attempt_id, a.project_id, g.sequence, a.output_sha256, "
                "a.archived_asset_json, a.output_size_bytes "
                "from video_generation_attempts a "
                "join authoritative_narration_groups g on g.id = a.group_id "
                "where a.attempt_id = cast(:a as uuid)"
            ),
            {"a": attempt_id},
        ).mappings().one()
        asset = row["archived_asset_json"]
        if isinstance(asset, str):
            asset = json.loads(asset)
        asset_id = (asset or {}).get("asset_id")
        key = session.execute(
            text("select object_key from media_assets where id = :i"), {"i": asset_id}
        ).scalar_one()
        path = out / f"{row['sequence']:02d}_{attempt_id[:8]}.mp4"
        client.fget_object(settings.minio_bucket, key, str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(
            {
                "attempt_id": str(row["attempt_id"]),
                "project_id": str(row["project_id"]),
                "sequence": row["sequence"],
                "object_key": key,
                "bytes": path.stat().st_size,
                "sha256": digest,
                "sha256_matches_db": digest == row["output_sha256"],
                "file": str(path),
            }
        )

print(json.dumps(records, ensure_ascii=False, indent=2))
database.engine.dispose()
