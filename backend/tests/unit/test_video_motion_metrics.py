"""Observation-only motion metrics recorded at archive (2026-11-06 user decision)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from test_video_content_qc_ladder import FakeSampler, black_frames, drive_to_qc, setup, submit_calls

from tubeflow_test.video_generation.motion_metrics import (
    MOTION_METRICS_ALGORITHM,
    MotionSignals,
    motion_metrics_evidence,
)
from tubeflow_test.video_generation.orchestration import ArchiveStage, AttemptStage

STILL = (0.0, 0.0)


def _evidence(
    diffs: list[float], shifts: list[tuple[float, float]] | None = None
) -> dict[str, object]:
    return motion_metrics_evidence(
        MotionSignals(tuple(diffs), tuple(shifts or [STILL] * len(diffs))), fps=24.0
    )


# --- pure metrics -----------------------------------------------------------------------


def test_motion_only_on_every_other_frame_is_fully_held() -> None:
    evidence = _evidence([4.0, 0.1] * 30)
    assert evidence["algorithm"] == MOTION_METRICS_ALGORITHM
    assert evidence["status"] == "ok"
    assert evidence["frames"] == 61 and evidence["cuts"] == 0
    assert evidence["moving_windows"] == evidence["held_windows"] == 8
    assert evidence["held_share"] == 1.0
    assert evidence["hf"] == 0.0 and evidence["jitter_shots"] == 0
    assert evidence["reference"] == {"held_notable": 0.2, "jitter_shot_hf": 0.25}


def test_motion_on_every_frame_is_not_held_and_still_video_has_no_windows() -> None:
    smooth = _evidence([3.0] * 60)
    assert smooth["moving_windows"] == 8 and smooth["held_share"] == 0.0
    still = _evidence([0.1] * 60)
    assert still["moving_windows"] == 0 and still["held_share"] is None


def test_a_cut_splits_shots_and_no_window_crosses_it() -> None:
    diffs = [3.0] * 40 + [60.0] + [4.0, 0.1] * 20
    evidence = _evidence(diffs)
    assert evidence["cuts"] == 1
    shots = evidence["shots"]
    assert isinstance(shots, list) and len(shots) == 2
    assert (shots[0]["t0"], shots[0]["t1"]) == (0.0, round(41 / 24, 2))
    assert (shots[1]["t0"], shots[1]["t1"]) == (round(41 / 24, 2), round(82 / 24, 2))
    assert shots[0]["held_share"] == 0.0 and shots[1]["held_share"] == 1.0
    crossing_free = [
        start for start in range(0, len(diffs) - 12, 6) if not start <= 40 < start + 12
    ]
    assert evidence["moving_windows"] == len(crossing_free)
    held = sum(1 for start in crossing_free if start > 40)
    assert evidence["held_windows"] == held


def test_hf_marks_a_single_frame_camera_jump_but_not_a_steady_pan() -> None:
    jump = [STILL] * 40
    jump[20], jump[21] = (12.0, 0.0), (-12.0, 0.0)
    jumping = _evidence([3.0] * 40, jump)
    assert isinstance(jumping["hf"], float) and jumping["hf"] >= 0.25
    assert jumping["jitter_shots"] == 1
    panning = _evidence([3.0] * 40, [(2.0, 0.5)] * 40)
    assert panning["hf"] == 0.0 and panning["jitter_shots"] == 0


def test_flash_shots_are_skipped_and_inputs_are_validated() -> None:
    evidence = _evidence([60.0, 3.0, 60.0] + [3.0] * 40)
    shots = evidence["shots"]
    assert isinstance(shots, list) and len(shots) == 1 and shots[0]["t0"] == round(3 / 24, 2)
    with pytest.raises(ValueError, match="same frame transitions"):
        motion_metrics_evidence(MotionSignals((1.0, 2.0), (STILL,)), fps=24.0)
    with pytest.raises(ValueError, match="same frame transitions"):
        motion_metrics_evidence(MotionSignals((), ()), fps=24.0)
    with pytest.raises(ValueError, match="same frame transitions"):
        motion_metrics_evidence(MotionSignals((1.0,), (STILL,)), fps=0.0)


# --- archive integration ----------------------------------------------------------------


class FakeExtractor:
    def __init__(self, signals: MotionSignals | None = None, error: Exception | None = None):
        self.signals, self.error, self.calls = signals, error, 0

    def extract(self, content: bytes) -> MotionSignals:
        self.calls += 1
        assert content
        if self.error is not None:
            raise self.error
        assert self.signals is not None
        return self.signals


def test_qc_pass_records_motion_metrics_next_to_content_qc() -> None:
    source, repository, engine, _assets, _compiler, service = setup(sampler=FakeSampler())
    extractor = FakeExtractor(MotionSignals((4.0, 0.1) * 30, (STILL,) * 60))
    service._motion_extractor = extractor
    intent = service.trigger(source.owner_id, source.group_id, "motion-metrics")

    archived = drive_to_qc(source, engine, service, intent.attempt_id)

    assert archived.archive_stage is ArchiveStage.COMPLETE
    evidence = archived.probe_evidence or {}
    assert evidence["qc_passed"] is True and isinstance(evidence["content_qc"], dict)
    metrics = evidence["motion_metrics"]
    assert isinstance(metrics, dict)
    assert metrics["status"] == "ok" and metrics["held_share"] == 1.0
    assert extractor.calls == 1
    assert repository.is_current(source.group_id, intent.attempt_id)


def test_a_failing_measurement_never_blocks_or_resubmits_the_video() -> None:
    source, repository, engine, _assets, _compiler, service = setup(sampler=FakeSampler())
    service._motion_extractor = FakeExtractor(error=RuntimeError("ffmpeg crashed"))
    intent = service.trigger(source.owner_id, source.group_id, "motion-metrics-error")

    archived = drive_to_qc(source, engine, service, intent.attempt_id)

    assert archived.archive_stage is ArchiveStage.COMPLETE
    assert archived.stage is AttemptStage.ARCHIVED
    evidence = archived.probe_evidence or {}
    assert evidence["qc_passed"] is True
    assert evidence["motion_metrics"] == {
        "algorithm": MOTION_METRICS_ALGORITHM,
        "status": "error",
        "error": "RuntimeError: ffmpeg crashed",
    }
    assert submit_calls(engine) == 1
    assert repository.is_current(source.group_id, intent.attempt_id)


def test_unconfigured_extractor_is_recorded_not_silent() -> None:
    source, _repository, engine, _assets, _compiler, service = setup(sampler=FakeSampler())
    intent = service.trigger(source.owner_id, source.group_id, "motion-metrics-none")

    archived = drive_to_qc(source, engine, service, intent.attempt_id)

    evidence = archived.probe_evidence or {}
    assert evidence["motion_metrics"] == {
        "algorithm": MOTION_METRICS_ALGORITHM,
        "status": "extractor_not_configured",
    }


def test_metrics_are_not_measured_for_a_video_that_failed_qc() -> None:
    source, _repository, engine, _assets, _compiler, service = setup(
        production_mode="MANUAL", sampler=FakeSampler(black_frames())
    )
    extractor = FakeExtractor(MotionSignals((3.0,) * 30, (STILL,) * 30))
    service._motion_extractor = extractor
    intent = service.trigger(source.owner_id, source.group_id, "motion-metrics-qc-failed")

    failed = drive_to_qc(source, engine, service, intent.attempt_id)

    assert failed.archive_stage is ArchiveStage.QC_FAILED
    assert "motion_metrics" not in (failed.probe_evidence or {})
    assert extractor.calls == 0


# --- adapter (ffmpeg + OpenCV) ----------------------------------------------------------


requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _render(path: Path, source_rate: int) -> bytes:
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi"]
    command += ["-i", f"testsrc2=size=320x180:rate={source_rate}:duration=3"]
    # 12 fps re-timed to 24 fps repeats every frame once: motion on every other frame.
    command += ["-vf", "fps=24", "-c:v", "ffv1", str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path.read_bytes()


@requires_ffmpeg
def test_extractor_separates_animation_on_twos_from_full_rate(tmp_path: Path) -> None:
    from tubeflow_test.adapters.video_motion_metrics import FfmpegOpenCvMotionExtractor

    extractor = FfmpegOpenCvMotionExtractor(timeout_seconds=60.0)
    twos = extractor.extract(_render(tmp_path / "twos.mkv", 12))
    full = extractor.extract(_render(tmp_path / "full.mkv", 24))
    for signals in (twos, full):
        assert len(signals.diffs) == len(signals.shifts) == 71
    assert motion_metrics_evidence(twos, fps=24.0)["held_share"] == 1.0
    assert motion_metrics_evidence(full, fps=24.0)["held_share"] == 0.0
    with pytest.raises(ValueError, match="empty video content"):
        extractor.extract(b"")
