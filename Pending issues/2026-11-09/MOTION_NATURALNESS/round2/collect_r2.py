#!/usr/bin/env python3
"""第 2 轮只读收集（web 容器内运行）：实验尝试的状态、指标与输出视频原文件。

只读：数据库查询都在 READ ONLY 事务里；对象存储只 GET；不调用任何 Provider。
实验尝试按 experiment.name = prompt-edit-ab-v1 且幂等键以 motion-r2- 开头自动找出；
另收两组生产来源尝试（spec.json 的 source_attempt_id）作对照。

用法：OUT=/tmp/motion_r2 python collect_r2.py /tmp/spec.json
产出：$OUT/attempts.json、$OUT/videos/<标签>.mp4（输出视频原字节，sha256 记入 attempts.json）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

URL = re.compile(r"https?://", re.I)


def _obj(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str) and URL.search(value):
        return "<url removed>"
    return value


def main(argv: list[str]) -> int:
    from minio import Minio
    from sqlalchemy import text

    from tubeflow_test.adapters.persistence.database import Database
    from tubeflow_test.config import Settings

    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    out = Path(os.environ.get("OUT", "/tmp/motion_r2"))
    (out / "videos").mkdir(parents=True, exist_ok=True)
    settings = Settings()
    database = Database(settings.database_url)
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    second = int(spec["second_seed"])
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with database.session() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        for seq, group in spec["groups"].items():
            rows = (
                connection.execute(
                    text(
                        "select attempt_id::text as attempt_id, idempotency_key, node_id, stage, "
                        "archive_stage, error_code, updated_at, input_snapshot_json, "
                        "request_json, probe_evidence_json, archived_asset_json, output_sha256 "
                        "from video_generation_attempts where group_id = cast(:g as uuid) and ("
                        "attempt_id = cast(:s as uuid) or (idempotency_key like 'motion-r2-%' and "
                        "input_snapshot_json -> 'experiment' ->> 'name' = 'prompt-edit-ab-v1')) "
                        "order by updated_at"
                    ),
                    {"g": group["group_id"], "s": group["source_attempt_id"]},
                )
                .mappings()
                .all()
            )
            for row in rows:
                snapshot = _obj(row["input_snapshot_json"]) or {}
                request = _obj(row["request_json"]) or {}
                probe = _obj(row["probe_evidence_json"]) or {}
                experiment = snapshot.get("experiment")
                seed = (request.get("parameters") or {}).get("seed")
                if row["attempt_id"] == group["source_attempt_id"]:
                    label = f"g{seq}_production"
                else:
                    variant = (experiment or {}).get("variant_label", "unknown")
                    which = (
                        "source"
                        if seed == group["source_seed"]
                        else ("second" if seed == second else f"seed{seed}")
                    )
                    label = f"g{seq}_{variant}_{which}"
                prompt = request.get("prompt")
                record: dict[str, Any] = {
                    "label": label,
                    "group": seq,
                    "attempt_id": row["attempt_id"],
                    "idempotency_key": row["idempotency_key"],
                    "node_id": row["node_id"],
                    "stage": row["stage"],
                    "archive_stage": row["archive_stage"],
                    "error_code": row["error_code"],
                    # 表上没有 created_at 列（2026-11-10 实测）；用 updated_at 作时间戳，
                    # 提交与终态时刻以 submit_v2 日志为准。
                    "created_at": None,
                    "updated_at": str(row["updated_at"]),
                    "seed": seed,
                    "strategy": (snapshot.get("video_prompt") or {}).get("strategy"),
                    "experiment": experiment,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                    if isinstance(prompt, str)
                    else None,
                    "output_sha256": row["output_sha256"],
                    "motion_metrics": probe.get("motion_metrics"),
                    "content_qc": _scrub(probe.get("content_qc")),
                    "probe_error_code": probe.get("error_code"),
                }
                archived = _obj(row["archived_asset_json"]) or {}
                asset_id = archived.get("asset_id")
                if asset_id:
                    asset = (
                        connection.execute(
                            text(
                                "select object_key, sha256 from media_assets "
                                "where id = cast(:i as uuid)"
                            ),
                            {"i": str(asset_id)},
                        )
                        .mappings()
                        .first()
                    )
                    if asset is None:
                        errors.append({"label": label, "error": "media_asset_not_found"})
                    else:
                        response = client.get_object(settings.minio_bucket, asset["object_key"])
                        try:
                            data = response.read()
                        finally:
                            response.close()
                            response.release_conn()
                        path = out / "videos" / f"{label}.mp4"
                        path.write_bytes(data)
                        record["video_file"] = path.name
                        record["video_sha256"] = hashlib.sha256(data).hexdigest()
                        record["video_bytes"] = len(data)
                        record["video_sha256_matches_asset"] = (
                            record["video_sha256"] == asset["sha256"]
                        )
                records.append(record)
        connection.rollback()
    labels = [r["label"] for r in records]
    duplicates = sorted({x for x in labels if labels.count(x) > 1})
    if duplicates:
        errors.append({"error": "duplicate_labels", "labels": duplicates})
    (out / "attempts.json").write_text(
        json.dumps(
            {"records": records, "errors": errors}, ensure_ascii=False, indent=1, default=str
        ),
        encoding="utf-8",
    )
    print(json.dumps({"records": len(records), "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
