"""复算 7460885c 的归档视频指标，并定位第 1 镜最大的 5 处位移残差（只读，无 Provider）。

在部署机的 worker 容器内运行一次：

    docker cp recompute_and_shot1.py tubeflowai-test-worker-1:/tmp/
    docker exec tubeflowai-test-worker-1 python /tmp/recompute_and_shot1.py /tmp/mm_out

产物：
    /tmp/mm_out/recompute.json     复算结果 + 与 measure.json 的对照
    /tmp/mm_out/shot1.json         第 1 镜残差排序前 5 帧与导出帧清单
    /tmp/mm_out/shot1/frame_<K>.png  原分辨率帧（K-2..K+2）

只读：SELECT、MinIO GET、本地临时文件。不写库、不调用任何 Provider。
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from minio import Minio
from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.video_motion_metrics import FfmpegOpenCvMotionExtractor
from tubeflow_test.config import Settings
from tubeflow_test.video_generation.motion_metrics import (
    JITTER_SHOT_HF,
    MotionSignals,
    _shot_residuals,
    motion_metrics_evidence,
)

ATTEMPT = "7460885c-3aab-4e0e-ae73-e9c3fba5413b"
FPS = 24.0
SHOT1_START, SHOT1_END = 0, 92  # measure.json：第 1 镜 frames 0–92（t 0–3.83 s）
TOP_N = 5

# measure.json 里的对照值（原样抄录，不由本脚本推导）
BASELINE = {
    "frames": 328,
    "cuts": 2,
    "moving_windows": 45,
    "held_windows": 22,
    "held_share": 0.49,
    "hf": 0.874,
    "jitter_shots": 2,
    "shots": [
        {"t0": 0.0, "t1": 3.83, "moving_windows": 14, "held_windows": 1, "held_share": 0.07, "hf": 1.578},
        {"t0": 3.83, "t1": 8.0, "moving_windows": 15, "held_windows": 15, "held_share": 1.0, "hf": 0.424},
        {"t0": 8.0, "t1": 13.67, "moving_windows": 17, "held_windows": 7, "held_share": 0.41, "hf": 0.167},
    ],
}


def fetch(out_dir: Path) -> tuple[dict[str, object], bytes, Path]:
    settings = Settings()
    database = Database(settings.database_url)
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )
    with database.session() as session:
        row = session.execute(
            text(
                "select archived_asset_json, output_sha256, output_size_bytes, stage, "
                "probe_evidence_json "
                "from video_generation_attempts where attempt_id = cast(:a as uuid)"
            ),
            {"a": ATTEMPT},
        ).mappings().one()
        asset = row["archived_asset_json"] or {}
        asset_id = asset.get("asset_id")
        if not asset_id:
            raise SystemExit("no archived asset on this attempt")
        key = session.execute(
            text("select object_key from media_assets where id = :i"), {"i": asset_id}
        ).scalar_one()
    local = out_dir / "source.mp4"
    client.fget_object(settings.minio_bucket, key, str(local))
    content = local.read_bytes()
    meta = {
        "attempt_id": ATTEMPT,
        "bucket": settings.minio_bucket,
        "object_key": key,
        "mp4_bytes": len(content),
        "mp4_sha256": hashlib.sha256(content).hexdigest(),
        "db_output_sha256": row["output_sha256"],
        "db_output_size_bytes": row["output_size_bytes"],
        "stage": row["stage"],
        "probe_evidence_motion_metrics": (row["probe_evidence_json"] or {}).get(
            "motion_metrics"
        ),
    }
    database.engine.dispose()
    return meta, content, local


def compare(evidence: dict[str, object]) -> dict[str, object]:
    rows: list[dict[str, object]] = []

    def add(name: str, got: object, want: object, tol: float | None) -> None:
        if tol is None:
            ok = got == want
        elif isinstance(got, (int, float)) and isinstance(want, (int, float)):
            ok = abs(float(got) - float(want)) <= tol
        else:
            ok = False
        rows.append({"field": name, "recomputed": got, "measure_json": want, "ok": ok})

    for field in ("frames", "cuts", "moving_windows", "held_windows", "held_share", "jitter_shots"):
        add(field, evidence.get(field), BASELINE[field], None)
    add("hf(whole)", evidence.get("hf"), BASELINE["hf"], 0.005)

    shots = evidence.get("shots") or []
    for index, want in enumerate(BASELINE["shots"], start=1):
        got = shots[index - 1] if index - 1 < len(shots) else {}
        add(f"shot{index}.t0", got.get("t0"), want["t0"], None)
        add(f"shot{index}.t1", got.get("t1"), want["t1"], None)
        add(f"shot{index}.moving_windows", got.get("moving_windows"), want["moving_windows"], None)
        add(f"shot{index}.held_windows", got.get("held_windows"), want["held_windows"], None)
        add(f"shot{index}.held_share", got.get("held_share"), want["held_share"], None)
        add(f"shot{index}.hf", got.get("hf"), want["hf"], 0.005)

    return {"rows": rows, "all_ok": all(bool(row["ok"]) for row in rows), "shot_count": len(shots)}


def shot1_residuals(signals: MotionSignals, out_dir: Path, video: Path) -> dict[str, object]:
    segment = signals.shifts[SHOT1_START : SHOT1_END - 1]
    residuals = _shot_residuals(segment)
    ranked = sorted(range(len(residuals)), key=lambda i: residuals[i], reverse=True)[:TOP_N]
    hf = round((statistics.fmean(residuals)) ** 0.5, 3)
    frames_dir = out_dir / "shot1"
    frames_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, object]] = []
    exported: list[str] = []
    for rank, index in enumerate(ranked, start=1):
        jump_into = index + 1  # residual[i] = 第 i -> i+1 帧的位移偏离
        dx, dy = segment[index]
        window = [frame for frame in range(jump_into - 2, jump_into + 3) if frame >= 0]
        names: list[str] = []
        for frame in window:
            target = frames_dir / f"frame_{frame:03d}.png"
            if not target.exists():
                subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-y", "-i", str(video),
                        "-vf", f"select=eq(n\\,{frame})", "-vsync", "0", "-frames:v", "1",
                        str(target),
                    ],
                    check=True,
                    capture_output=True,
                )
            if target.exists():
                names.append(target.name)
                exported.append(target.name)
        items.append(
            {
                "rank": rank,
                "residual": round(residuals[index], 3),
                "transition": f"{index}->{jump_into}",
                "jump_into_frame": jump_into,
                "t_seconds": round(jump_into / FPS, 3),
                "shift_dx": round(dx, 2),
                "shift_dy": round(dy, 2),
                "window_frames": window,
                "files": names,
            }
        )
    return {
        "shot1": {"start_frame": SHOT1_START, "end_frame": SHOT1_END,
                  "t0": 0.0, "t1": round(SHOT1_END / FPS, 2)},
        "segment_len": len(segment),
        "shot1_hf": hf,
        "jitter_line": JITTER_SHOT_HF,
        "top": items,
        "exported_files": sorted(set(exported)),
    }


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/mm_out")
    out_dir.mkdir(parents=True, exist_ok=True)

    meta, content, video = fetch(out_dir)
    extractor = FfmpegOpenCvMotionExtractor(timeout_seconds=60.0)
    started = time.perf_counter()
    signals = extractor.extract(content)
    extract_seconds = time.perf_counter() - started
    started = time.perf_counter()
    evidence = motion_metrics_evidence(signals, fps=FPS)
    metrics_seconds = time.perf_counter() - started

    recompute = {
        "meta": meta,
        "signals": {"diffs": len(signals.diffs), "shifts": len(signals.shifts)},
        "timing": {
            "extract_seconds": round(extract_seconds, 3),
            "metrics_seconds": round(metrics_seconds, 3),
        },
        "evidence": evidence,
        "comparison": compare(evidence),
    }
    (out_dir / "recompute.json").write_text(
        json.dumps(recompute, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    shot1 = shot1_residuals(signals, out_dir, video)
    (out_dir / "shot1.json").write_text(
        json.dumps(shot1, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    print(json.dumps(
        {
            "mp4_sha256": meta["mp4_sha256"],
            "mp4_bytes": meta["mp4_bytes"],
            "diffs": len(signals.diffs),
            "timing": recompute["timing"],
            "held_share": evidence.get("held_share"),
            "moving_windows": evidence.get("moving_windows"),
            "held_windows": evidence.get("held_windows"),
            "hf": evidence.get("hf"),
            "jitter_shots": evidence.get("jitter_shots"),
            "all_ok": recompute["comparison"]["all_ok"],
            "shot1_top": [(i["jump_into_frame"], i["residual"]) for i in shot1["top"]],
        },
        indent=1,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
