"""H3 原生输出尺寸：按工作流身份登记，未知身份 fail-closed（2026-10-21）。"""

from __future__ import annotations

import pytest

from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.video_generation.domain import VideoRuleViolation
from tubeflow_test.video_generation.native_size import (
    expected_native_size,
    is_registered,
    workflow_identity,
)


def _manifest(workflow_id: str, workflow_version: str) -> dict[str, object]:
    return {
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "resolution_preset": "preview",
    }


@pytest.mark.parametrize(
    ("workflow_id", "workflow_version", "landscape", "portrait"),
    [
        ("h3.tubeflow.storyboard-to-video", "1.0.0", (1056, 608), (608, 1056)),
        (
            "h3.community.reference-images-to-video",
            "1.1.0-mvp",
            (1056, 608),
            (608, 1056),
        ),
        ("h3.tubeflow.storyboard-to-video-hd", "1.0.1", (1600, 896), (896, 1600)),
    ],
)
def test_registered_workflows_resolve_their_own_native_size(
    workflow_id: str,
    workflow_version: str,
    landscape: tuple[int, int],
    portrait: tuple[int, int],
) -> None:
    manifest = _manifest(workflow_id, workflow_version)
    assert expected_native_size(manifest, AspectRatio.LANDSCAPE) == landscape
    assert expected_native_size(manifest, AspectRatio.PORTRAIT) == portrait
    assert is_registered(workflow_id, workflow_version)


def test_hd_workflow_is_not_accepted_at_legacy_size() -> None:
    """HD 工作流必须按 1600x896 校验；legacy 尺寸是另一个工作流的合同。"""
    manifest = _manifest("h3.tubeflow.storyboard-to-video-hd", "1.0.1")
    assert expected_native_size(manifest, AspectRatio.LANDSCAPE) != (1056, 608)


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        {},
        {"workflow_id": "h3.unknown", "workflow_version": "1.0.0"},
        {"workflow_id": "h3.tubeflow.storyboard-to-video"},
        {"workflow_version": "1.0.0"},
        {"workflow_id": "", "workflow_version": "1.0.0"},
        "not-a-mapping",
    ],
)
def test_unknown_or_malformed_identity_fails_closed(manifest: object) -> None:
    with pytest.raises(VideoRuleViolation, match="video_workflow_native_size_unknown"):
        expected_native_size(manifest, AspectRatio.LANDSCAPE)


def test_workflow_identity_reads_only_string_scalars() -> None:
    assert workflow_identity(
        {"workflow_id": "h3.tubeflow.storyboard-to-video", "workflow_version": "1.0.0"}
    ) == ("h3.tubeflow.storyboard-to-video", "1.0.0")
    assert workflow_identity({"workflow_id": 1, "workflow_version": "1.0.0"}) is None
    assert workflow_identity([]) is None
