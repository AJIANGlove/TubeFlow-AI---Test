"""Measure the jitter A/B videos and apply the pre-registered decision rule (USER_DECISIONS §51).

Input: a directory produced by ab_export.py (attempts.json + <attempt_id>.mp4) and, after the
visual check, an optional visual.json. Needs ffmpeg; the HF part also needs numpy and
opencv-python (cv2). Without cv2 it stops with a clear message instead of reporting HF as 0.

    python3 ab_measure.py <results_dir>

Per video:
- held-frame share for the whole video (../held_frames.py, same algorithm as the survey) and per
  shot, where shots are the cuts found in the video (same frame-difference rule) and each is
  labelled with the planned shot whose start ("[Shot n] At mm:ss.mmm, cut to ...") is within 1 s;
- the camera move each planned shot asked for (v4 whitelist phrase, parsed from the prompt);
- HF per shot (shift minus 5-frame moving average, RMS; same as
  ../../JITTER_484746C4_20261105/scripts/motion.py), the frame-weighted RMS over all shots as the
  video HF, and "jitter shots" = shots with HF >= 0.25
  (the 2026-10-18 definition, docs/VIDEO_JITTER_DIAGNOSIS_20261018.md);
- a contact sheet every 0.5 s (sheets/<attempt_id>.png) for the visual check.

visual.json (written by the person doing the visual check): {"<attempt_id>": ["defect", ...]}
with an empty list for a clean video. Defect names: identity, extra_people, text, split_screen,
other:<short note>.

Decision (written before the run): over the 4 (group, seed) pairs the cadence arm must
1) have a lower whole-video held share than its baseline in at least 3 pairs (a pair with a
   failed or unmeasurable video counts as "not lower");
2) have no more jitter shots in total than the baseline arm;
3) add no visual defect that its own baseline does not have, in any pair.
All three -> "submit to the user: make the variant the default". Otherwise -> "ineffective or
inconclusive". Without visual.json the verdict is "pending visual check". Before any of this,
every cadence prompt must equal its baseline prompt plus the one sentence; otherwise the run is
reported as invalid.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from held_frames import (  # noqa: E402
    FPS,
    HELD_RATIO,
    MIN_MOTION,
    STEP,
    WINDOW,
    frame_differences,
    held_share,
)

JITTER_SHOT_HF = 0.25
# Same text as CADENCE_SENTENCE_V1 (backend video_prompt/domain.py) and ../README.md §2.
SENTENCE = (
    "Animate every frame at the full frame rate with smooth, continuous motion; never hold a "
    'pose for two frames or use limited animation ("animating on twos"); people, hands and '
    "objects move a little on every frame."
)
_CUT = re.compile(r"\[Shot (\d+)\] At (\d+):(\d+(?:\.\d+)?), cut to")
_CAMERA = (
    ("static", re.compile(r"Static camera, 0% movement")),
    ("push_in", re.compile(r"Slow push-in of about (\d+(?:\.\d+)?)%")),
    ("pull_out", re.compile(r"Slow pull-out of about (\d+(?:\.\d+)?)%")),
    ("pan", re.compile(r"Slow pan of about (\d+(?:\.\d+)?) degrees")),
    ("tilt", re.compile(r"Slow tilt of about (\d+(?:\.\d+)?) degrees")),
    ("tracking", re.compile(r"Gentle tracking at the subject's walking speed")),
)


def prompt_shots(prompt: str) -> list[dict[str, Any]]:
    """Shot start times and requested camera move, parsed from the compiled prompt."""
    starts = [(1, 0.0)] + [
        (int(match.group(1)), int(match.group(2)) * 60 + float(match.group(3)))
        for match in _CUT.finditer(prompt)
    ]
    blocks = list(re.finditer(r"\[Shot (\d+)\]", prompt))
    shots: list[dict[str, Any]] = []
    for order, start_s in starts:
        block_match = next((m for m in blocks if int(m.group(1)) == order), None)
        camera = "unparsed"
        if block_match is not None:
            following = [m.start() for m in blocks if m.start() > block_match.start()]
            tail = prompt.find("\nMaintain ", block_match.start())
            limits = [value for value in (*following[:1], tail) if value > 0]
            block = prompt[block_match.start() : min(limits) if limits else len(prompt)]
            for name, pattern in _CAMERA:
                found = pattern.search(block)
                if found:
                    camera = name if not found.groups() else f"{name}:{found.group(1)}"
                    break
        shots.append({"shot": order, "start_s": start_s, "camera": camera})
    return shots


def segments(diffs: list[float], planned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shots as cut in the video (frame-difference spikes, same rule as held_frames.py),
    each labelled with the planned shot whose start time is nearest (within 1 s)."""
    median = statistics.median(diffs) if diffs else 0.0
    cuts = [i + 1 for i, value in enumerate(diffs) if value > max(12.0, 6 * median)]
    bounds = [0, *cuts, len(diffs) + 1]
    result: list[dict[str, Any]] = []
    for start, end in zip(bounds[:-1], bounds[1:], strict=True):
        if end - start < 6:
            continue
        nearest = min(planned, key=lambda shot: abs(shot["start_s"] - start / FPS), default=None)
        matched = nearest is not None and abs(nearest["start_s"] - start / FPS) <= 1.0
        result.append(
            {
                "start_frame": start,
                "end_frame": end,
                "t0": round(start / FPS, 2),
                "t1": round(end / FPS, 2),
                "shot": nearest["shot"] if matched else None,
                "camera": nearest["camera"] if matched else "unmatched",
            }
        )
    return result


def held_by_segment(diffs: list[float], shots: list[dict[str, Any]]) -> None:
    for shot in shots:
        moving = held = 0
        # diff i is frame i -> i+1; a window must stay inside the segment (no cut inside).
        for start in range(shot["start_frame"], shot["end_frame"] - 1 - WINDOW + 1, STEP):
            window = diffs[start : start + WINDOW]
            if len(window) < WINDOW or statistics.fmean(window) < MIN_MOTION:
                continue
            moving += 1
            even, odd = statistics.fmean(window[0::2]), statistics.fmean(window[1::2])
            if max(even, odd) > 0 and min(even, odd) / max(even, odd) < HELD_RATIO:
                held += 1
        shot["moving_windows"] = moving
        shot["held_windows"] = held
        shot["held_share"] = round(held / moving, 2) if moving else None


def hf_by_segment(path: Path, shots: list[dict[str, Any]]) -> float | None:
    try:
        import cv2  # type: ignore[import-not-found]
        import numpy as np
    except ImportError as exc:  # never report a missing HF as zero
        raise SystemExit(f"HF needs numpy and opencv-python (cv2): {exc}") from exc
    width, height = 528, 304
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={width}:{height},format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True,
        check=True,
    ).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, height, width).astype(np.float32)
    window = cv2.createHanningWindow((width, height), cv2.CV_32F)
    shifts = np.array(
        [cv2.phaseCorrelate(frames[i - 1], frames[i], window)[0] for i in range(1, len(frames))]
    )
    squares: list[float] = []
    for shot in shots:
        # Same slice as motion.py: shifts inside the shot, excluding the cut into the next one.
        segment = shifts[shot["start_frame"] : shot["end_frame"] - 1]
        if len(segment) < 6:
            shot["hf"] = None
            continue
        pad = np.pad(segment, ((2, 2), (0, 0)), mode="edge")
        average = np.array([pad[i : i + 5].mean(0) for i in range(len(segment))])
        residual = ((segment - average) ** 2).sum(1)
        shot["hf"] = round(float(np.sqrt(residual.mean())), 3)
        shot["jitter_shot"] = shot["hf"] >= JITTER_SHOT_HF
        squares.extend(float(value) for value in residual)
    return round(float(np.sqrt(statistics.fmean(squares))), 3) if squares else None


def contact_sheet(path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf",
         "fps=2,scale=352:-1,drawtext=text='%{pts\\:hms}':x=4:y=4:fontsize=14:fontcolor=white:box=1:boxcolor=black@0.6,tile=6x5",
         "-frames:v", "1", str(target)],
        check=True,
    )


def main() -> None:
    root = Path(sys.argv[1])
    records = json.loads((root / "attempts.json").read_text(encoding="utf-8"))
    visual_path = root / "visual.json"
    visual = json.loads(visual_path.read_text(encoding="utf-8")) if visual_path.exists() else None
    for record in records:
        video = record.get("video_file")
        if not video:
            record["measured"] = False
            continue
        path = root / video
        diffs = frame_differences(str(path))
        record["whole"] = held_share(diffs)
        planned = prompt_shots(str(record.get("prompt") or ""))
        shots = segments(diffs, planned)
        held_by_segment(diffs, shots)
        record["planned_shots"] = planned
        record["hf_video"] = hf_by_segment(path, shots)
        record["jitter_shots"] = sum(1 for shot in shots if shot.get("jitter_shot"))
        record["shots"] = shots
        record["measured"] = True
        contact_sheet(path, root / "sheets" / f"{record['attempt_id']}.png")
    pairs: dict[tuple[Any, Any], dict[str, Any]] = {}
    for record in records:
        experiment = record.get("experiment") or {}
        key = (record.get("group_sequence"), record.get("seed"))
        pairs.setdefault(key, {})[experiment.get("arm", "unknown")] = record
    lines = [
        "| group | seed | arm | attempt | node | stage | held share | moving windows | HF | jitter shots | shots (camera: held/HF) |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    lower = jitter_base = jitter_variant = 0
    new_defects: list[str] = []
    prompt_mismatch: list[str] = []
    for (group, seed), arms in sorted(pairs.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        for arm in ("baseline", "cadence_v1"):
            r = arms.get(arm)
            if r is None:
                lines.append(f"| {group} | {seed} | {arm} | — | — | missing | — | — | — | — | — |")
                continue
            whole = r.get("whole") or {}
            shots = "; ".join(
                f"{s['t0']}s S{s['shot']} {s['camera']}: {s.get('held_share')}/{s.get('hf')}"
                for s in r.get("shots", [])
            )
            lines.append(
                f"| {group} | {seed} | {arm} | `{r['attempt_id'][:8]}` | {r.get('node_id')} | {r.get('stage')} | "
                f"{whole.get('held_share')} | {whole.get('moving_windows')} | {r.get('hf_video')} | "
                f"{r.get('jitter_shots')} | {shots} |"
            )
        base, variant = arms.get("baseline"), arms.get("cadence_v1")
        if base and base.get("measured"):
            jitter_base += base["jitter_shots"]
        if variant and variant.get("measured"):
            jitter_variant += variant["jitter_shots"]
        if base and variant:
            text = str(variant.get("prompt") or "")
            stripped = (text.replace(SENTENCE + "\n", "", 1), text.replace("\n" + SENTENCE + "\n", "", 1))
            if text.count(SENTENCE) != 1 or str(base.get("prompt") or "") not in stripped:
                prompt_mismatch.append(f"group {group} seed {seed}")
        base_share = ((base or {}).get("whole") or {}).get("held_share")
        variant_share = ((variant or {}).get("whole") or {}).get("held_share")
        if base_share is not None and variant_share is not None and variant_share < base_share:
            lower += 1
        if visual is not None and base and variant:
            added = set(visual.get(variant["attempt_id"], [])) - set(visual.get(base["attempt_id"], []))
            new_defects.extend(f"group {group} seed {seed}: {item}" for item in sorted(added))
    rule_1 = lower >= 3
    rule_2 = jitter_variant <= jitter_base
    if prompt_mismatch:
        verdict = "invalid run: a cadence prompt is not its baseline plus the sentence"
    elif visual is None:
        verdict = "pending visual check"
    elif rule_1 and rule_2 and not new_defects:
        verdict = "submit to the user: make the variant the default"
    else:
        verdict = "ineffective or inconclusive"
    summary = {
        "pairs": len(pairs),
        "variant_lower_held_share_pairs": lower,
        "rule_1_at_least_3_lower": rule_1,
        "jitter_shots_baseline": jitter_base,
        "jitter_shots_variant": jitter_variant,
        "rule_2_no_more_jitter_shots": rule_2,
        "new_visual_defects": new_defects if visual is not None else None,
        "pairs_whose_prompts_differ_by_more_than_the_sentence": prompt_mismatch,
        "verdict": verdict,
    }
    (root / "measure.json").write_text(
        json.dumps({"summary": summary, "attempts": records}, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "pairs.md").write_text(
        "\n".join(lines) + "\n\n```json\n" + json.dumps(summary, indent=1, ensure_ascii=False) + "\n```\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
