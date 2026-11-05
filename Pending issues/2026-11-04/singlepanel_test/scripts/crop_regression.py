"""crop-grid-v2 regression over the Gate 1 baseline (548 storyboards; read-only, no Provider calls).

Crop every sheet once with the OLD code and once with the NEW code, then compare:

    # old = the running worker; new = a one-off container of the new image on the same
    # compose network with the worker's environment (MinIO variables)
    docker compose cp gate1_rerun/baseline.json worker:/tmp/baseline.json
    docker compose exec -T worker python - run < crop_regression.py > crop_old.jsonl
    docker run --rm -i --network <compose network> --env-file .env \
        -v "$PWD/gate1_rerun/baseline.json:/tmp/baseline.json:ro" <new image> \
        python - run < crop_regression.py > crop_new.jsonl
    python crop_regression.py compare crop_old.jsonl crop_new.jsonl

`run` reads the baseline from BASELINE (default /tmp/baseline.json), checks each sheet's
sha256 and crops it with the Gate 1 layout (grid-v1), printing one JSON line per sheet.
`compare` prints the boxes changed among sheets that passed before (must be empty), sheets
that newly fail (must be empty), and sheets that newly pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from io import BytesIO


def run() -> None:
    from minio import Minio
    from PIL import Image
    from tubeflow_test.domain.storyboards import grid_layout_for
    from tubeflow_test.video_generation.domain import VideoRuleViolation
    from tubeflow_test.video_generation.panel_images import grid_v2_panel_boxes

    rows = json.load(open(os.environ.get("BASELINE", "/tmp/baseline.json"), encoding="utf-8"))["rows"]
    client = Minio(
        os.environ["TUBEFLOW_MINIO_ENDPOINT"],
        access_key=os.environ["TUBEFLOW_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_MINIO_SECRET_KEY"],
        secure=False,
    )
    bucket = os.environ["TUBEFLOW_MINIO_BUCKET"]
    for row in rows:
        response = client.get_object(bucket, row["object_key"])
        try:
            content = response.read()
        finally:
            response.close()
            response.release_conn()
        record = {"generation_id": row["generation_id"], "shots": row["shots"]}
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            record["status"] = "source_bytes_sha_mismatch"
        else:
            with Image.open(BytesIO(content)) as opened:
                image = opened.convert("RGB")
            layout = grid_layout_for(int(row["shots"]), "16:9", "grid-v1")
            try:
                record["boxes"] = [list(box) for box in grid_v2_panel_boxes(image, layout, int(row["shots"]))]
                record["status"] = "passed"
            except VideoRuleViolation as error:
                record["status"] = str(error)
        print(json.dumps(record), flush=True)


def compare(old_path: str, new_path: str) -> None:
    def load(path: str) -> dict[str, dict]:
        with open(path, encoding="utf-8") as handle:
            return {r["generation_id"]: r for r in (json.loads(line) for line in handle if line.strip())}

    old, new = load(old_path), load(new_path)
    passed_before = [key for key, row in old.items() if row["status"] == "passed"]
    summary = {
        "sheets": len(old),
        "same_ids": sorted(old) == sorted(new),
        "passed_before": len(passed_before),
        "passed_after": sum(1 for row in new.values() if row["status"] == "passed"),
        "boxes_changed_among_passed_before": [
            key for key in passed_before if new.get(key, {}).get("boxes") != old[key]["boxes"]
        ],
        "newly_failing": [
            key for key in passed_before if new.get(key, {}).get("status") != "passed"
        ],
        "newly_passing": [
            key
            for key, row in old.items()
            if row["status"] != "passed" and new.get(key, {}).get("status") == "passed"
        ],
        "still_failing": {
            key: row["status"] for key, row in new.items() if row["status"] != "passed"
        },
    }
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "compare":
        compare(sys.argv[2], sys.argv[3])
    else:
        run()
