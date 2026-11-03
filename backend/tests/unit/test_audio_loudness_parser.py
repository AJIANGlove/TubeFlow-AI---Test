from __future__ import annotations

import json
import math

import pytest

from tubeflow_test.adapters.audio_qc import FfmpegAudioNormalizer


def _report() -> dict[str, object]:
    return {
        "input_i": -16.0,
        "input_tp": -1.0,
        "input_lra": 0.0,
        "input_thresh": -26.0,
        "target_offset": 0.0,
    }


def _encoded(report: dict[str, object]) -> bytes:
    return json.dumps(report).encode()


def test_transform_analysis_requires_all_finite_transform_fields() -> None:
    complete = _report()

    assert FfmpegAudioNormalizer._parse_loudness_report(_encoded(complete), phase="analysis") == {
        "input_i": -16.0,
        "input_tp": -1.0,
        "input_lra": 0.0,
        "input_thresh": -26.0,
        "target_offset": 0.0,
    }

    for field_name in ("input_lra", "input_thresh", "target_offset"):
        missing = {name: value for name, value in complete.items() if name != field_name}
        with pytest.raises(ValueError, match="was invalid"):
            FfmpegAudioNormalizer._parse_loudness_report(_encoded(missing), phase="analysis")


def test_verification_accepts_only_finite_gate_fields() -> None:
    report = _report()
    report["target_offset"] = math.inf

    assert FfmpegAudioNormalizer._parse_loudness_report(
        _encoded(report),
        phase="verification",
        require_transform_fields=False,
    ) == {"input_i": -16.0, "input_tp": -1.0}

    with pytest.raises(ValueError, match="was invalid"):
        FfmpegAudioNormalizer._parse_loudness_report(_encoded(report), phase="analysis")


@pytest.mark.parametrize("missing_field", ["input_i", "input_tp"])
def test_verification_rejects_missing_gate_fields(missing_field: str) -> None:
    report = _report()
    report.pop(missing_field)

    with pytest.raises(ValueError, match="was invalid"):
        FfmpegAudioNormalizer._parse_loudness_report(
            _encoded(report),
            phase="verification",
            require_transform_fields=False,
        )


@pytest.mark.parametrize("field_name", ["input_i", "input_tp"])
@pytest.mark.parametrize("value", ["not-a-number", math.nan, math.inf, -math.inf])
def test_verification_rejects_invalid_gate_values(field_name: str, value: object) -> None:
    report = _report()
    report[field_name] = value

    with pytest.raises(ValueError, match="was invalid"):
        FfmpegAudioNormalizer._parse_loudness_report(
            _encoded(report),
            phase="verification",
            require_transform_fields=False,
        )
