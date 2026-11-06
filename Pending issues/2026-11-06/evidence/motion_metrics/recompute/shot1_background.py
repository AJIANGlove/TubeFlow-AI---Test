"""Group 7 shot 1 (attempt 7460885c): does the background move between consecutive frames?

Reads the exported frames in ./shot1/ (native 1056x608) and, for every consecutive pair,
estimates the global motion of the background only, avoiding the person and the box in the
middle of the frame:
- phase correlation (OpenCV, Hanning window) on the left strip (x < 330: door and window)
  and on the right strip (x >= 830: shelves) separately;
- ORB features restricted to the same two strips + RANSAC similarity transform
  (translation tx/ty, scale, inliers/matches).

    python3 shot1_background.py   # run from this directory; needs numpy + opencv
"""

from __future__ import annotations

import glob
import re

import cv2
import numpy as np

LEFT, RIGHT = 330, 830


def main() -> None:
    frames = {
        int(re.search(r"frame_(\d+)", path).group(1)): cv2.imread(path)  # type: ignore[union-attr]
        for path in sorted(glob.glob("shot1/frame_*.png"))
    }
    grey = {k: cv2.cvtColor(v, cv2.COLOR_BGR2GRAY).astype(np.float32) for k, v in frames.items()}
    orb = cv2.ORB_create(6000)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    height, width = next(iter(grey.values())).shape
    mask = np.zeros((height, width), np.uint8)
    mask[:, :LEFT] = 255
    mask[:, RIGHT:] = 255

    def phase(a: np.ndarray, b: np.ndarray) -> str:
        window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
        (dx, dy), response = cv2.phaseCorrelate(a, b, window)
        return f"({dx:+.2f}, {dy:+.2f}; r={response:.2f})"

    def features(a: np.ndarray, b: np.ndarray) -> str:
        ka, da = orb.detectAndCompute(a, mask)
        kb, db = orb.detectAndCompute(b, mask)
        matches = matcher.match(da, db)
        pa = np.float32([ka[m.queryIdx].pt for m in matches])
        pb = np.float32([kb[m.trainIdx].pt for m in matches])
        transform, inliers = cv2.estimateAffinePartial2D(
            pa, pb, method=cv2.RANSAC, ransacReprojThreshold=1.0
        )
        scale = float(np.hypot(transform[0, 0], transform[1, 0]))
        return (
            f"tx {transform[0, 2]:+.2f} ty {transform[1, 2]:+.2f} scale {scale:.4f} "
            f"inliers {int(inliers.sum())}/{len(matches)}"
        )

    print("transition | left strip phase | right strip phase | background ORB+RANSAC")
    for first in sorted(grey):
        second = first + 1
        if second not in grey:
            continue
        a, b = grey[first], grey[second]
        print(
            f"{first}->{second} | {phase(a[:, :LEFT], b[:, :LEFT])} | "
            f"{phase(a[:, RIGHT:], b[:, RIGHT:])} | {features(frames[first], frames[second])}"
        )


if __name__ == "__main__":
    main()
