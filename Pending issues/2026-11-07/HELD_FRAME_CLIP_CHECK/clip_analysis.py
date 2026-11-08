"""Held-frame check on a clip cut from final_93d717fa.mp4 (0:13-0:37), 2026-11-07.

1. Whole-clip and per-shot held share / HF with the product's own algorithm
   (backend/src/tubeflow_test/video_generation/motion_metrics.py, loaded by file path) and the
   same ffmpeg/OpenCV extraction as adapters/video_motion_metrics.py.
2. Per-frame movement (OpenCV phase correlation, pixels at the clip's 1280x720) inside fixed
   regions of four shots, split into even and odd frame transitions.

Usage: python -I clip_analysis.py <clip.mp4> <out_dir>
Needs ffmpeg on PATH, numpy and opencv-python-headless. No network, no database.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
METRICS = REPO / "backend/src/tubeflow_test/video_generation/motion_metrics.py"
YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
WIDTH, HEIGHT = 1280, 720

# (label, first frame, last frame, centre y, centre x, crop h, crop w); clip time = frame / 24,
# final-video time = clip time + 13 s.
REGIONS = (
    ("group 3 shot 1, woman's face", 290, 314, 240, 700, 320, 420),
    ("group 3 shot 1, left background", 300, 316, 240, 150, 320, 300),
    ("group 3 shot 2, arm placing coins", 432, 456, 260, 1000, 320, 420),
    ("group 2 shot 3, coins", 130, 154, 500, 990, 320, 420),
)


def _load_metrics():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("motion_metrics_local", METRICS)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["motion_metrics_local"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _grey_frames(path: Path, width: int, height: int) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={width}:{height},format=gray",
         "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width)


def _differences(path: Path) -> list[float]:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf",
         "scale=264:152,format=gray,tblend=all_mode=difference,signalstats,"
         "metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [float(value) for value in YAVG.findall(out)]


def _shifts(frames: np.ndarray) -> list[tuple[float, float]]:
    height, width = frames.shape[1:]
    window = cv2.createHanningWindow((width, height), cv2.CV_32F)
    result = []
    for previous, current in zip(frames[:-1], frames[1:], strict=True):
        (dx, dy), _ = cv2.phaseCorrelate(
            previous.astype(np.float32), current.astype(np.float32), window
        )
        result.append((float(dx), float(dy)))
    return result


def _region_steps(frames: np.ndarray, region: tuple) -> dict[str, object]:  # type: ignore[type-arg]
    label, first, last, cy, cx, h, w = region
    y0 = max(0, min(HEIGHT - h, cy - h // 2))
    x0 = max(0, min(WIDTH - w, cx - w // 2))
    window = cv2.createHanningWindow((w, h), cv2.CV_32F)
    steps = []
    for index in range(first, last):
        a = frames[index, y0 : y0 + h, x0 : x0 + w].astype(np.float32)
        b = frames[index + 1, y0 : y0 + h, x0 : x0 + w].astype(np.float32)
        (dx, dy), _ = cv2.phaseCorrelate(a, b, window)
        steps.append(round(float(np.hypot(dx, dy)), 2))
    return {
        "region": label,
        "frames": [first, last],
        "final_video_seconds": [round(13 + first / 24, 2), round(13 + last / 24, 2)],
        "steps_px": steps,
        "even_transition_mean_px": round(float(np.mean(steps[0::2])), 2),
        "odd_transition_mean_px": round(float(np.mean(steps[1::2])), 2),
    }


def main() -> None:
    clip, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = _load_metrics()
    signals = metrics.MotionSignals(
        tuple(_differences(clip)), tuple(_shifts(_grey_frames(clip, 528, 304)))
    )
    evidence = metrics.motion_metrics_evidence(signals, fps=24.0)
    full = _grey_frames(clip, WIDTH, HEIGHT)
    result = {
        "motion_metrics": evidence,
        "regions": [_region_steps(full, region) for region in REGIONS],
    }
    (out_dir / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
