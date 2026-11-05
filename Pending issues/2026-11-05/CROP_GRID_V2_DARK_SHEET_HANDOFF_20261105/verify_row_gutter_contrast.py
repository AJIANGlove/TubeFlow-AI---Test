#!/usr/bin/env python3
"""crop-grid-v2 行分隔对比度核验（只读，不改业务代码、不写文件）。

对每张故事板：
1. 原样调用业务函数 check_storyboard_panel_layout，打印 PASS / REJECT；
2. 按业务同款口径（行分隔用"每个出镜行都有镜头的列"所组成的条带求行均值）
   找到第一条行分隔附近最暗的连续带，量出：
   - 分隔带行号、宽度、带内均值；
   - ±邻域（业务 neighbourhood）内、**剔除分隔带后**的中位数 = 最理想的基线；
   - 最理想对比度 = 上述中位数 − 带内均值（业务阈值 _V2_DELTA）；
   - 分隔带两侧 7–26 px 对照条均值（放宽搜索 _v2_is_gutter_band 的口径）。
   只有一行出镜（如 2 镜 2×2）时没有行分隔，改为打印暗尾起点（_v2_dark_tail 口径）。

用法（仓库根目录）：
  python "Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_HANDOFF_20261105/verify_row_gutter_contrast.py" \
      IMAGE:PANELS [IMAGE:PANELS ...] [--src backend/src]
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("items", nargs="+", help="图片路径:镜数，例如 a.jpg:6")
    parser.add_argument("--src", default=None, help="后端源码目录，默认 <repo>/backend/src")
    args = parser.parse_args(argv)
    src = Path(args.src) if args.src else Path(__file__).resolve().parents[3] / "backend" / "src"
    sys.path.insert(0, str(src.resolve()))

    from PIL import Image

    from tubeflow_test.domain.storyboards import GRID_LAYOUT_V2, grid_layout_for
    from tubeflow_test.video_generation import panel_images as P

    print(f"_V2_DELTA={P._V2_DELTA}")
    for item in args.items:
        raw_path, _, raw_count = item.rpartition(":")
        path, count = Path(raw_path), int(raw_count)
        try:
            P.check_storyboard_panel_layout(
                path, panel_count=count, aspect_ratio="16:9", layout_version=GRID_LAYOUT_V2
            )
            verdict = "PASS"
        except P.VideoRuleViolation as exc:
            verdict = f"REJECT {exc}"
        image = Image.open(path).convert("RGB")
        layout = grid_layout_for(count, "16:9", GRID_LAYOUT_V2)
        pixels = P._V2Pixels.for_image(image)
        lum = image.convert("L").convert("F")
        x0, x1 = P._v2_outer(P._v2_means(lum, "x"), pixels)
        y0, y1 = P._v2_outer(P._v2_means(lum, "y"), pixels)
        shot_rows = math.ceil(count / layout.cols)
        print(f"\n{path.name} panels={count} layout={layout.rows}x{layout.cols} -> {verdict}")
        if shot_rows < 2:
            # 只有一行出镜：行方向走 _v2_dark_tail（means ≤ _V2_DARK_MAX 的暗尾起点须在期望线附近）
            strip = lum.crop((x0, 0, x1, lum.height))
            means = P._v2_means(strip, "y")
            cell = (y1 - y0) / layout.rows
            radius = max(pixels.radius_min, math.ceil(cell * P._V2_RADIUS_FRACTION))
            expected = y0 + cell * shot_rows
            start = y1
            while start > y0 and means[start - 1] <= P._V2_DARK_MAX:
                start -= 1
            print(
                f"  只有一行出镜，无行分隔；暗尾（行均值 ≤ {P._V2_DARK_MAX}）起点第 {start} 行，"
                f"期望 {expected:.0f}，偏离 {start - expected:+.0f}（允许 ±{radius + pixels.band_max}）"
            )
            continue
        last_row_cells = count - (shot_rows - 1) * layout.cols
        full = min(layout.cols, last_row_cells)
        strip = lum.crop((x0, 0, round(x0 + (x1 - x0) / layout.cols * full), lum.height))
        means = P._v2_means(strip, "y")
        expected = y0 + (y1 - y0) / layout.rows
        wide = max(
            max(pixels.radius_min, math.ceil((y1 - y0) / layout.rows * P._V2_RADIUS_FRACTION)),
            math.ceil((y1 - y0) / layout.rows * P._V2_WIDE_FRACTION),
        )
        lo, hi = math.floor(expected - wide), math.ceil(expected + wide)
        darkest = min(range(lo, hi + 1), key=lambda i: means[i])
        limit = means[darkest] + 8.0
        a = b = darkest
        while a - 1 >= lo and means[a - 1] <= limit:
            a -= 1
        while b + 1 <= hi and means[b + 1] <= limit:
            b += 1
        centre, n = (a + b) // 2, pixels.neighbourhood
        window = range(max(0, centre - n), min(len(means), centre + n + 1))
        baseline = statistics.median(means[i] for i in window if not a <= i <= b)
        gutter = sum(means[a : b + 1]) / (b - a + 1)
        near, far = pixels.side
        before = sum(means[a - far : a - near]) / (far - near)
        after = sum(means[b + 1 + near : b + 1 + far]) / (far - near)
        marked = sum(P._v2_extreme(means, lo, hi + 1, pixels)[lo : hi + 1])
        print(
            f"  行分隔 rows {a}-{b} ({b - a + 1}px, 期望 {expected:.0f}) 带内均值 {gutter:.1f}\n"
            f"  剔除分隔带后 ±{n}px 中位数 {baseline:.1f} -> 最理想对比度 {baseline - gutter:.1f}"
            f" (阈值 {P._V2_DELTA})\n"
            f"  两侧 {near}-{far}px 对照条 {before:.1f} / {after:.1f}；"
            f"±{wide}px 内被 _v2_extreme 标记的行数 {marked}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
