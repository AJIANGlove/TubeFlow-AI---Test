#!/usr/bin/env python3
"""crop-grid-v2 逐格裁切拒绝的离线复现脚本（只读，不改业务代码）。

用法（在仓库根目录执行，或用 --src 指定后端源码目录）：

  # 1) 直接用业务代码跑一次裁切演练（等价于故事板阶段的校验）
  python repro_panel_layout.py check \
      --image "Pending issues/2026-11-05/CROP_GRID_V2_DARK_SHEET_REJECTION_20261105/data/failed/e459d954_6panels_25931.jpg" \
      --panels 6

  # 2) 量出每条分隔带的最大偏差，看是否达到 _V2_DELTA 阈值
  python repro_panel_layout.py profile --image ... --cols 3 --rows 2

  # 3) 扫邻域半径，观察同一批图能否被切开（用于验证"不是几何问题"）
  python repro_panel_layout.py sweep --image ... --panels 6 --neighbourhoods 65,120,300

脚本只依赖 Pillow 与仓库后端源码，不写任何文件。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path


def _bootstrap(src: str | None) -> None:
    if src:
        root = Path(src).resolve()
    else:
        root = Path(__file__).resolve().parents[4] / "backend" / "src"
    sys.path.insert(0, str(root))


def _load():
    from tubeflow_test.domain.storyboards import GRID_LAYOUT_V2, grid_layout_for
    from tubeflow_test.video_generation import panel_images

    return panel_images, grid_layout_for, GRID_LAYOUT_V2


def _override_neighbourhood(panel_images, radius: int | None):
    """临时把邻域半径换成指定值；返回还原函数。"""
    if radius is None:
        return lambda: None
    original = panel_images._V2Pixels.for_image.__func__

    def patched(cls, image):
        base = original(cls, image)
        return cls(
            radius, base.gap, base.band_max, base.framed_max, base.radius_min, base.inset, base.side
        )

    panel_images._V2Pixels.for_image = classmethod(patched)
    return lambda: setattr(
        panel_images._V2Pixels, "for_image", classmethod(original)
    )


def _report_profile(image_path: Path, cols: int, rows: int, radius: int | None = None) -> None:
    from PIL import Image

    panel_images, _, _ = _load()
    restore = _override_neighbourhood(panel_images, radius)
    try:
        _profile_with(panel_images, image_path, cols, rows)
    finally:
        restore()


def _profile_with(panel_images, image_path: Path, cols: int, rows: int) -> None:
    from PIL import Image

    image = Image.open(image_path).convert("RGB")
    pixels = panel_images._V2Pixels.for_image(image)
    luminance = image.convert("L").convert("F")
    print(
        f"image={image_path.name} size={image.size} neighbourhood={pixels.neighbourhood} "
        f"delta_threshold={panel_images._V2_DELTA} band_max={pixels.band_max} framed_max={pixels.framed_max}"
    )
    for axis, total, span in (("y", rows, image.height), ("x", cols, image.width)):
        means = panel_images._v2_means(luminance, axis)
        overall = statistics.median(means)
        cell = span / total
        for index in range(1, total):
            expected = cell * index
            darkest = min(means[int(expected) - 70 : int(expected) + 70])
            best, best_at = 0.0, None
            for position in range(int(expected) - 70, int(expected) + 70):
                lo = max(0, position - pixels.neighbourhood)
                hi = min(len(means), position + pixels.neighbourhood + 1)
                deviation = abs(means[position] - statistics.median(means[lo:hi]))
                if deviation > best:
                    best, best_at = deviation, position
            verdict = "MEETS" if best >= panel_images._V2_DELTA else "BELOW"
            print(
                f"  {axis} split{index}: expected={expected:.0f} sheet_median={overall:.1f} "
                f"darkest_in_band={darkest:.1f} max_deviation={best:.1f}@{best_at} -> {verdict}"
            )


def _report_check(image_path: Path, panels: int, aspect: str, layout: str) -> None:
    panel_images, _, _ = _load()
    try:
        panel_images.check_storyboard_panel_layout(
            image_path, panel_count=panels, aspect_ratio=aspect, layout_version=layout
        )
    except Exception as exc:  # noqa: BLE001 - 复现脚本要如实打印业务异常
        print(f"{image_path.name} panels={panels} REJECT {type(exc).__name__}: {exc}")
        return
    print(f"{image_path.name} panels={panels} PASS")


def _report_sweep(image_path: Path, panels: int, aspect: str, layout: str, values: list[int]) -> None:
    from PIL import Image

    panel_images, grid_layout_for, _ = _load()
    image = Image.open(image_path).convert("RGB")
    for radius in values:
        restore = _override_neighbourhood(panel_images, radius)
        try:
            boxes = panel_images.grid_v2_panel_boxes(
                image, grid_layout_for(panels, aspect, layout), panels
            )
            sizes = sorted({(box[2] - box[0], box[3] - box[1]) for box in boxes})
            print(f"  neighbourhood={radius}: OK boxes={len(boxes)} sizes={sizes}")
        except Exception as exc:  # noqa: BLE001
            print(f"  neighbourhood={radius}: REJECT {exc}")
        finally:
            restore()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "profile", "sweep"))
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--panels", type=int, default=4)
    parser.add_argument("--cols", type=int, default=2)
    parser.add_argument("--rows", type=int, default=2)
    parser.add_argument("--aspect", default="16:9")
    parser.add_argument("--layout", default="grid-v2")
    parser.add_argument("--neighbourhoods", default="65,120,300")
    parser.add_argument(
        "--neighbourhood", type=int, default=None, help="profile 模式强制使用的邻域半径"
    )
    parser.add_argument("--src", default=None, help="后端源码目录，默认 <repo>/backend/src")
    args = parser.parse_args(argv)

    _bootstrap(args.src)
    if args.mode == "profile":
        _report_profile(args.image, args.cols, args.rows, args.neighbourhood)
    elif args.mode == "check":
        _report_check(args.image, args.panels, args.aspect, args.layout)
    else:
        values = [int(item) for item in args.neighbourhoods.split(",") if item.strip()]
        _report_sweep(args.image, args.panels, args.aspect, args.layout, values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
