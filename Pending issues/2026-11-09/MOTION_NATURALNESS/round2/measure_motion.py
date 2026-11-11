#!/usr/bin/env python3
"""第 2 轮预先写定的区域运动测量（spec.json）。需要 numpy、opencv-python、ffmpeg。

对一个 H3 输出视频：
  1) 目标时间段：spec 给了 window_s（第 3 轮起）就用它，并做相关性保护——段内每帧与段中点帧的
     相关系数须 ≥ 0.7，不满足（叠化、换镜）就收缩到中点两侧连续满足的部分并标记 window_truncated；
     没给 window_s（第 2 轮原口径）时：逐帧（160 宽灰度）相邻帧平均绝对差 > max(3, 12×中位数)
     记为切点（6 帧内只记第一个），目标镜头 = 含该镜计划时间中点的检测段（两端各去 6 帧）。
     注意：原口径只认硬切，2026-11-10 发现多数新输出以叠化换镜，会把下一镜算进来，已弃用；
  3) 每隔 6 帧（0.25 s）取一对，480 宽 Farneback 光流；用 RANSAC 仿射拟合全局镜头运动并扣除；
  4) 每个区域：残差速度 = 区域内 |残差光流| 的均值，换算成「960 宽画面上的像素/秒」
     （×(960/480)×(fps/6)）；另给区域内残差 > 0.5 px 的像素占比，以及区域内残差光流的平均水平
     分量（带正负号，正 = 向右，同单位），用于检查风向是否一致。

用法：python measure_motion.py --spec spec.json --group 116 --label g116_m1_source video.mp4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import cv2
import numpy as np


def probe(path: str) -> tuple[int, int, float]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-of",
            "json",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    num, den = stream["r_frame_rate"].split("/")
    return int(stream["width"]), int(stream["height"]), float(num) / float(den)


def frames(path: str, width: int, height: int) -> np.ndarray:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            f"scale={width}:{height}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width)


def cuts(small: np.ndarray) -> list[int]:
    diff = np.abs(small[1:].astype(np.int16) - small[:-1].astype(np.int16)).mean(axis=(1, 2))
    threshold = max(3.0, 12.0 * float(np.median(diff)))
    found: list[int] = []
    for index in np.nonzero(diff > threshold)[0]:
        frame = int(index) + 1
        if not found or frame - found[-1] > 6:
            found.append(frame)
    return found


def corr(a: np.ndarray, b: np.ndarray) -> float:
    x = a.astype(np.float32) - float(a.mean())
    y = b.astype(np.float32) - float(b.mean())
    return float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9))


def guarded_window(small: np.ndarray, a: int, b: int) -> tuple[int, int, bool]:
    anchor = (a + b) // 2
    ok = [corr(small[anchor], small[i]) >= 0.7 for i in range(a, b)]
    lo, hi = anchor - a, anchor - a
    while lo > 0 and ok[lo - 1]:
        lo -= 1
    while hi < len(ok) - 1 and ok[hi + 1]:
        hi += 1
    return a + lo, a + hi + 1, (lo, hi + 1) != (0, len(ok))


def box(region: list[float], width: int, height: int) -> tuple[slice, slice]:
    x0, y0, x1, y1 = region
    return slice(int(y0 * height), int(y1 * height)), slice(int(x0 * width), int(x1 * width))


def measure(path: str, spec: dict, group: str) -> dict:
    width, height, fps = probe(path)
    flow_w = int(spec["flow_width"])
    flow_h = int(round(flow_w * height / width / 2) * 2)
    step = int(spec["pair_step_frames"])
    threshold = float(spec["residual_threshold_px"])
    scale = (960 / flow_w) * (fps / step)  # → 960 宽画面上的像素/秒
    gray = frames(path, flow_w, flow_h)
    small = np.stack([cv2.resize(f, (160, int(160 * flow_h / flow_w))) for f in gray])
    cut_frames = cuts(small)
    bounds = [0, *cut_frames, len(gray)]
    group_spec = spec["groups"][group]
    planned = {str(order): (start, end) for order, start, end in group_spec["planned_shots_ms"]}
    ys, xs = np.mgrid[8 : flow_h - 8 : 8, 8 : flow_w - 8 : 8]
    points = np.stack([xs.ravel(), ys.ravel()], 1).astype(np.float32)
    gy, gx = np.mgrid[0:flow_h, 0:flow_w].astype(np.float32)
    result: dict = {
        "video": path.rsplit("/", 1)[-1],
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "frames": int(len(gray)),
        "cuts_s": [round(c / fps, 2) for c in cut_frames],
        "targets": {},
    }
    for shot, regions in group_spec["targets"].items():
        truncated = None
        if "window_s" in regions:
            w0, w1 = regions["window_s"]
            a, b, truncated = guarded_window(small, int(w0 * fps), min(int(w1 * fps), len(gray)))
            segment = [round(a / fps, 2), round(b / fps, 2)]
        else:
            start_ms, end_ms = planned[shot]
            mid = (start_ms + end_ms) / 2000 * fps
            k = max(i for i in range(len(bounds) - 1) if bounds[i] <= mid)
            a, b = bounds[k] + 6, bounds[k + 1] - 6
            segment = [round(bounds[k] / fps, 2), round(bounds[k + 1] / fps, 2)]
        names = {
            **{f"env:{n}": r for n, r in regions["env"].items()},
            **{f"control:{n}": r for n, r in regions["control"].items()},
        }
        speeds: dict[str, list[float]] = {n: [] for n in names}
        shares: dict[str, list[float]] = {n: [] for n in names}
        drift: dict[str, list[float]] = {n: [] for n in names}
        camera: list[float] = []
        for i in range(a, b - step, step):
            flow = cv2.calcOpticalFlowFarneback(
                gray[i], gray[i + step], None, 0.5, 3, 21, 3, 5, 1.1, 0
            )
            vectors = flow[points[:, 1].astype(int), points[:, 0].astype(int)]
            matrix, _ = cv2.estimateAffine2D(
                points, points + vectors, method=cv2.RANSAC, ransacReprojThreshold=1.0
            )
            if matrix is None:
                continue
            cam = np.stack(
                [
                    matrix[0, 0] * gx + matrix[0, 1] * gy + matrix[0, 2] - gx,
                    matrix[1, 0] * gx + matrix[1, 1] * gy + matrix[1, 2] - gy,
                ],
                -1,
            )
            residual_flow = flow - cam
            residual = np.linalg.norm(residual_flow, axis=-1)
            camera.append(float(np.linalg.norm(cam, axis=-1).mean()) * scale)
            for name, region in names.items():
                part = residual[box(region, flow_w, flow_h)]
                speeds[name].append(float(part.mean()) * scale)
                shares[name].append(float((part > threshold).mean()))
                drift[name].append(
                    float(residual_flow[box(region, flow_w, flow_h)][..., 0].mean()) * scale
                )
        env = [float(np.mean(v)) for n, v in speeds.items() if n.startswith("env:") and v]
        result["targets"][shot] = {
            "segment_s": segment,
            "window_truncated": truncated,
            "pairs": len(camera),
            "camera_px_s": round(float(np.mean(camera)), 2) if camera else None,
            "regions_px_s": {n: round(float(np.mean(v)), 2) for n, v in speeds.items() if v},
            "regions_moving_share": {
                n: round(float(np.mean(v)), 3) for n, v in shares.items() if v
            },
            "regions_dx_px_s": {n: round(float(np.mean(v)), 2) for n, v in drift.items() if v},
            "env_score": round(float(np.median(env)), 2) if env else None,
        }
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("video")
    args = parser.parse_args(argv)
    with open(args.spec, encoding="utf-8") as handle:
        spec = json.load(handle)
    row = measure(args.video, spec, args.group)
    row["label"] = args.label
    row["group"] = args.group
    print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
