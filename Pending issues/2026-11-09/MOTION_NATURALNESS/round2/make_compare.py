#!/usr/bin/env python3
"""第 2 轮对比视频（只用 ffmpeg）：每组 6 格并排（3 列 × 2 行，格位 1–6 从左到右、从上到下），
顺序按 spec.json 的 tile_order 写定；缺失的尝试用黑格占位。
另把每个输出视频转成 960 宽副本，便于上传。

画面上不印任何文字（避免依赖字体）；格位与臂的对应关系写在 KEY.json，供用户先盲看再对答案。

用法：python make_compare.py spec.json <videos 目录> <输出目录>
产出：compare_g116.mp4、compare_g119.mp4、KEY.json、videos_960/<标签>.mp4
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TILE_W, TILE_H = 640, 360
LAYOUT = "0_0|640_0|1280_0|0_360|640_360|1280_360"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main(argv: list[str]) -> int:
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    videos, out = Path(argv[1]), Path(argv[2])
    (out / "videos_960").mkdir(parents=True, exist_ok=True)
    key: dict[str, dict[str, str]] = {}
    for video in sorted(videos.glob("*.mp4")):
        run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-an", "-vf", "scale=960:-2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             str(out / "videos_960" / video.name)])
    for group, order in spec["tile_order"].items():
        inputs: list[str] = []
        key[group] = {}
        for position, cell in enumerate(order, start=1):
            arm, seed = cell.split("@")
            label = f"g{group}_{arm}_{seed}"
            path = videos / f"{label}.mp4"
            key[group][str(position)] = label if path.exists() else f"{label} (missing: black tile)"
            if path.exists():
                inputs += ["-i", str(path)]
            else:
                inputs += ["-f", "lavfi", "-i", f"color=c=black:s={TILE_W}x{TILE_H}:r=24:d=16"]
        chains = [
            f"[{i}:v]scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,"
            f"pad={TILE_W}:{TILE_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24[v{i}]"
            for i in range(len(order))
        ]
        stack = "".join(f"[v{i}]" for i in range(len(order)))
        xstack = f"xstack=inputs={len(order)}:layout={LAYOUT}:shortest=1[out]"
        graph = ";".join(chains) + f";{stack}{xstack}"
        run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[out]",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             str(out / f"compare_g{group}.mp4")])
    (out / "KEY.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(key, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
