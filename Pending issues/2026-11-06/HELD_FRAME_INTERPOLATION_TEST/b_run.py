"""B：隔帧停顿离线修补 —— 标定 / 三臂处理 / 测量。在一次性容器内运行（--network none）。

用法：
    python b_run.py calibrate
    python b_run.py process <mpdecimate参数>
    python b_run.py measure <mpdecimate参数>
    python b_run.py sidebyside
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

import cv2
import numpy as np

WORK = pathlib.Path("/work")
SAMPLES = WORK / "samples"
OUT = WORK / "out"
OUT.mkdir(parents=True, exist_ok=True)

SAMPLES_LIST = [
    ("12_3ca4653d", "high"),
    ("07_7460885c", "high"),
    ("03_90c4a4e7", "high"),
    ("07_fc6e97b8", "high"),
    ("12_0cc7c89c", "low"),
    ("129_8f749d4e", "low"),
]

MD_DEFAULT = "mpdecimate"
INTERP_MCI = "minterpolate=fps=24:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
INTERP_BLEND = "minterpolate=fps=24:mi_mode=blend"
ENC = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "24"]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, check=False, **kw)


def probe(path: pathlib.Path) -> dict:
    r = run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames,r_frame_rate,width,height",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ]
    )
    d = json.loads(r.stdout or b"{}")
    s = (d.get("streams") or [{}])[0]
    return {
        "frames": int(s.get("nb_read_frames") or 0),
        "fps": s.get("r_frame_rate"),
        "size": f"{s.get('width')}x{s.get('height')}",
        "duration_s": round(float(d.get("format", {}).get("duration") or 0), 3),
    }


def count_after_mpdecimate(path: pathlib.Path, md: str) -> int:
    """mpdecimate 后保留的帧数：解码成 gray raw 再按帧大小计数。"""
    meta = probe(path)
    w, h = (int(x) for x in meta["size"].split("x"))
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", md, "-vsync", "0",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    )
    return len(r.stdout) // (w * h)


def laplacian_variance(path: pathlib.Path) -> float:
    cap = cv2.VideoCapture(str(path))
    values = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        values.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
    cap.release()
    return round(float(np.mean(values)), 3) if values else 0.0


def motion(path: pathlib.Path) -> dict:
    from tubeflow_test.adapters.video_motion_metrics import FfmpegOpenCvMotionExtractor
    from tubeflow_test.video_generation.motion_metrics import motion_metrics_evidence

    content = path.read_bytes()
    extractor = FfmpegOpenCvMotionExtractor(timeout_seconds=120.0)
    t0 = time.time()
    ev = motion_metrics_evidence(extractor.extract(content), fps=24.0)
    ev["elapsed_s"] = round(time.time() - t0, 2)
    return ev


def calibrate() -> None:
    candidates = [
        ("default(hi=768,lo=320,frac=0.33)", "mpdecimate"),
        ("hi=1344,lo=320,frac=0.33", "mpdecimate=hi=1344:lo=320:frac=0.33"),
        ("hi=768,lo=320,frac=0.5", "mpdecimate=hi=768:lo=320:frac=0.5"),
        ("hi=1024,lo=256,frac=0.33", "mpdecimate=hi=1024:lo=256:frac=0.33"),
    ]
    report = {}
    for name in ("03_90c4a4e7", "12_0cc7c89c", "07_7460885c"):
        path = SAMPLES / f"{name}.mp4"
        base = probe(path)
        m = motion(path)
        rows = []
        for label, md in candidates:
            kept = count_after_mpdecimate(path, md)
            rows.append(
                {
                    "params": md,
                    "label": label,
                    "kept_frames": kept,
                    "dropped_share": round(1 - kept / base["frames"], 3) if base["frames"] else None,
                }
            )
        report[name] = {
            "source": base,
            "measured_held_share": m.get("held_share"),
            "candidates": rows,
        }
        for row in rows:
            print(name, row["label"], "kept", row["kept_frames"], "/", base["frames"],
                  "dropped", row["dropped_share"], "| held", m.get("held_share"))
    (WORK / "calibrate.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def process(md: str) -> None:
    report = {}
    for name, kind in SAMPLES_LIST:
        src = SAMPLES / f"{name}.mp4"
        base = probe(src)
        arms = {}
        for arm, chain in (
            ("B0", None),
            ("B1", f"{md},{INTERP_MCI}"),
            ("B2", f"{md},{INTERP_BLEND}"),
        ):
            dst = OUT / f"{name}_{arm}.mp4"
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)]
            if chain:
                cmd += ["-vf", chain]
            cmd += ENC + [str(dst)]
            t0 = time.time()
            r = run(cmd)
            if r.returncode != 0:
                arms[arm] = {"error": r.stderr.decode()[-500:]}
                continue
            meta = probe(dst)
            arms[arm] = {
                "file": str(dst),
                "frames": meta["frames"],
                "duration_s": meta["duration_s"],
                "frames_match_source": meta["frames"] == base["frames"],
                "elapsed_s": round(time.time() - t0, 2),
                "bytes": dst.stat().st_size,
            }
        report[name] = {"kind": kind, "source": base, "arms": arms}
        print(name, json.dumps(arms, ensure_ascii=False)[:300])
    (WORK / "process.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def measure(md: str) -> None:
    src = json.loads((WORK / "process.json").read_text(encoding="utf-8"))
    result = {}
    for name, kind in SAMPLES_LIST:
        entry = {"kind": kind, "source": src[name]["source"], "src_metrics": motion(SAMPLES / f"{name}.mp4"),
                 "src_lap": laplacian_variance(SAMPLES / f"{name}.mp4"), "arms": {}}
        for arm in ("B0", "B1", "B2"):
            path = OUT / f"{name}_{arm}.mp4"
            if not path.exists():
                entry["arms"][arm] = {"error": "missing"}
                continue
            m = motion(path)
            entry["arms"][arm] = {
                "frames": src[name]["arms"][arm]["frames"],
                "frames_match_source": src[name]["arms"][arm]["frames_match_source"],
                "held_share": m.get("held_share"),
                "hf": m.get("hf"),
                "jitter_shots": m.get("jitter_shots"),
                "moving_windows": m.get("moving_windows"),
                "held_windows": m.get("held_windows"),
                "lap_var": laplacian_variance(path),
                "elapsed_s": m.get("elapsed_s"),
            }
        result[name] = entry
        print(name, json.dumps(entry["arms"], ensure_ascii=False))
    (WORK / "measure.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def sidebyside() -> None:
    for name, _kind in SAMPLES_LIST:
        inputs = [OUT / f"{name}_{arm}.mp4" for arm in ("B0", "B1", "B2")]
        if not all(p.exists() for p in inputs):
            print("skip", name)
            continue
        dst = OUT / f"{name}_sbs.mp4"
        cmd = ["ffmpeg", "-v", "error", "-y"]
        for p in inputs:
            cmd += ["-i", str(p)]
        filt = (
            "[0:v]scale=704:-2[a];[1:v]scale=704:-2[b];[2:v]scale=704:-2[c];"
            "[a][b][c]hstack=inputs=3[v]"
        )
        cmd += ["-filter_complex", filt, "-map", "[v]"] + ENC + [str(dst)]
        r = run(cmd)
        print(name, "sbs", "ok" if r.returncode == 0 else r.stderr.decode()[-300:])
        if name in ("12_3ca4653d", "07_fc6e97b8") and dst.exists():
            slow = OUT / f"{name}_sbs_slow.mp4"
            r2 = run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(dst), "-vf", "setpts=2*PTS",
                 "-r", "24"] + ENC + [str(slow)]
            )
            print(name, "slow", "ok" if r2.returncode == 0 else r2.stderr.decode()[-300:])


def calibrate_custom() -> None:
    import os

    candidates = json.loads(os.environ["MD_CANDIDATES"])
    report = {}
    for name in json.loads(os.environ.get("MD_SAMPLES", '["03_90c4a4e7","12_0cc7c89c","07_7460885c"]')):
        path = SAMPLES / f"{name}.mp4"
        base = probe(path)
        m = motion(path)
        rows = []
        for label, md in candidates:
            kept = count_after_mpdecimate(path, md)
            rows.append(
                {
                    "params": md,
                    "label": label,
                    "kept_frames": kept,
                    "dropped_share": round(1 - kept / base["frames"], 3) if base["frames"] else None,
                }
            )
        report[name] = {"source": base, "measured_held_share": m.get("held_share"), "candidates": rows}
        for row in rows:
            print(name, row["label"], "kept", row["kept_frames"], "/", base["frames"],
                  "dropped", row["dropped_share"], "| held", m.get("held_share"))
    out = WORK / (os.environ.get("MD_OUT", "calibrate2.json"))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "calibrate":
        calibrate()
    elif action == "calibrate2":
        calibrate_custom()
    elif action == "process":
        process(sys.argv[2])
    elif action == "measure":
        measure(sys.argv[2])
    elif action == "sidebyside":
        sidebyside()
    else:
        raise SystemExit(f"unknown action {action}")
