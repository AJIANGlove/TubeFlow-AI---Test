"""Held-frame ("on twos") share of a video, computed with ffmpeg only (no numpy/opencv).

Same algorithm as `../JITTER_484746C4_20261105/scripts/cadence.py`: frames scaled to 264x152
grey, per-frame mean absolute difference d[i]; cuts are d > max(12, 6*median); 12-frame windows
every 6 frames, skipping windows that contain a cut or barely move (mean < 0.8); a window is
"held" when the weaker of the even/odd frame-difference means is < 35% of the stronger one.

    python3 held_frames.py video.mp4 [...]
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys

_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
WINDOW, STEP, MIN_MOTION, HELD_RATIO, FPS = 12, 6, 0.8, 0.35, 24


def frame_differences(path: str) -> list[float]:
    """Mean absolute grey difference between consecutive frames (tblend + signalstats)."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            "scale=264:152,format=gray,tblend=all_mode=difference,signalstats,"
            "metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [float(value) for value in _YAVG.findall(out)]


def held_share(diffs: list[float]) -> dict[str, object]:
    if len(diffs) <= WINDOW:
        return {
            "frames": len(diffs) + 1,
            "moving_windows": 0,
            "held_windows": 0,
            "held_share": None,
        }
    median = statistics.median(diffs)
    cuts = {index for index, value in enumerate(diffs) if value > max(12.0, 6 * median)}
    moving = held = 0
    held_at: list[float] = []
    for start in range(0, len(diffs) - WINDOW, STEP):
        window = diffs[start : start + WINDOW]
        if any(index in cuts for index in range(start, start + WINDOW)):
            continue
        if statistics.fmean(window) < MIN_MOTION:
            continue
        moving += 1
        even, odd = statistics.fmean(window[0::2]), statistics.fmean(window[1::2])
        if max(even, odd) > 0 and min(even, odd) / max(even, odd) < HELD_RATIO:
            held += 1
            held_at.append(round(start / FPS, 1))
    return {
        "frames": len(diffs) + 1,
        "cuts": len(cuts),
        "moving_windows": moving,
        "held_windows": held,
        "held_share": round(held / moving, 2) if moving else None,
        "held_at_s": held_at[:12],
    }


if __name__ == "__main__":
    for item in sys.argv[1:]:
        print(item.rsplit("/", 1)[-1][:12], json.dumps(held_share(frame_differences(item))))
