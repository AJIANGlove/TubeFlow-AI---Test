"""Observation-only motion metrics for archived group videos (2026-11-06 user decision).

Two signals, recorded per video and per shot; neither takes part in any QC decision:
- held-frame share ("on twos", jitter A): 12-frame windows every 6 frames over the per-frame
  mean absolute grey difference at 264x152; a moving window is "held" when the weaker of the
  even/odd difference means is below 35 % of the stronger one. Same algorithm as
  Pending issues/2026-11-05/JITTER_SURVEY_20261105/held_frames.py (survey) and ab/ab_measure.py.
- HF (camera jump, jitter B): global per-frame shift (phase correlation at 528x304) minus its
  5-frame moving average, RMS per shot. Same as
  Pending issues/2026-11-05/JITTER_484746C4_20261105/scripts/motion.py (2026-10-18 definition).

This module is pure: the signals are extracted by an adapter (ffmpeg + OpenCV).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

MOTION_METRICS_ALGORITHM = "motion-cadence-v1"
WINDOW, STEP, MIN_MOTION, HELD_RATIO = 12, 6, 0.8, 0.35
MIN_SHOT_FRAMES = 6
# Reference lines only (survey "notable" and the 2026-10-18 jitter-shot line); not gates.
HELD_NOTABLE = 0.2
JITTER_SHOT_HF = 0.25


@dataclass(frozen=True, slots=True)
class MotionSignals:
    """diffs[i]: mean |grey(i+1) - grey(i)|; shifts[i]: global (dx, dy) from frame i to i+1."""

    diffs: tuple[float, ...]
    shifts: tuple[tuple[float, float], ...]


def _cuts(diffs: Sequence[float]) -> list[int]:
    median = statistics.median(diffs) if diffs else 0.0
    return [index for index, value in enumerate(diffs) if value > max(12.0, 6 * median)]


def _is_held(window: Sequence[float]) -> bool:
    even, odd = statistics.fmean(window[0::2]), statistics.fmean(window[1::2])
    return max(even, odd) > 0 and min(even, odd) / max(even, odd) < HELD_RATIO


def _share(held: int, moving: int) -> float | None:
    return round(held / moving, 2) if moving else None


def _whole_video(diffs: Sequence[float], cuts: set[int]) -> tuple[int, int]:
    moving = held = 0
    for start in range(0, len(diffs) - WINDOW, STEP):
        window = diffs[start : start + WINDOW]
        if any(index in cuts for index in range(start, start + WINDOW)):
            continue
        if statistics.fmean(window) < MIN_MOTION:
            continue
        moving += 1
        held += _is_held(window)
    return moving, held


def _shot_held(diffs: Sequence[float], start: int, end: int) -> tuple[int, int]:
    moving = held = 0
    # A window must stay inside the shot: diffs start .. end-2 belong to frames start .. end-1.
    for first in range(start, end - 1 - WINDOW + 1, STEP):
        window = diffs[first : first + WINDOW]
        if len(window) < WINDOW or statistics.fmean(window) < MIN_MOTION:
            continue
        moving += 1
        held += _is_held(window)
    return moving, held


def _shot_residuals(shifts: Sequence[tuple[float, float]]) -> list[float]:
    """Squared distance of each shift from its 5-frame moving average (edges repeated)."""
    padded = [shifts[0], shifts[0], *shifts, shifts[-1], shifts[-1]]
    residuals: list[float] = []
    for index, (dx, dy) in enumerate(shifts):
        around = padded[index : index + 5]
        mean_x = sum(point[0] for point in around) / 5
        mean_y = sum(point[1] for point in around) / 5
        residuals.append((dx - mean_x) ** 2 + (dy - mean_y) ** 2)
    return residuals


def motion_metrics_evidence(signals: MotionSignals, *, fps: float) -> dict[str, object]:
    diffs, shifts = signals.diffs, signals.shifts
    if not diffs or len(shifts) != len(diffs) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("motion signals must cover the same frame transitions")
    cuts = _cuts(diffs)
    moving, held = _whole_video(diffs, set(cuts))
    bounds = [0, *(index + 1 for index in cuts), len(diffs) + 1]
    shots: list[dict[str, object]] = []
    squares: list[float] = []
    jitter_shots = 0
    for start, end in zip(bounds[:-1], bounds[1:], strict=True):
        if end - start < MIN_SHOT_FRAMES:
            continue
        shot_moving, shot_held = _shot_held(diffs, start, end)
        segment = shifts[start : end - 1]
        hf: float | None = None
        if len(segment) >= MIN_SHOT_FRAMES:
            residuals = _shot_residuals(segment)
            hf = round(math.sqrt(statistics.fmean(residuals)), 3)
            squares.extend(residuals)
            jitter_shots += hf >= JITTER_SHOT_HF
        shots.append(
            {
                "t0": round(start / fps, 2),
                "t1": round(end / fps, 2),
                "moving_windows": shot_moving,
                "held_windows": shot_held,
                "held_share": _share(shot_held, shot_moving),
                "hf": hf,
            }
        )
    return {
        "algorithm": MOTION_METRICS_ALGORITHM,
        "status": "ok",
        "frames": len(diffs) + 1,
        "cuts": len(cuts),
        "moving_windows": moving,
        "held_windows": held,
        "held_share": _share(held, moving),
        "hf": round(math.sqrt(statistics.fmean(squares)), 3) if squares else None,
        "jitter_shots": jitter_shots,
        "shots": shots,
        "reference": {"held_notable": HELD_NOTABLE, "jitter_shot_hf": JITTER_SHOT_HF},
    }
