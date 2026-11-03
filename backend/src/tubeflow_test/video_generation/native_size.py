"""H3 工作流原生输出尺寸（版本化绑定层）。

质检必须按“该工作流自己的原生像素尺寸”校验，而不是全局固定尺寸：

- 2026-10-20 起新项目绑定 HD 工作流（`h3.tubeflow.storyboard-to-video-hd@1.0.1`），
  引擎 manifest 明确记录实际对齐输出为 16:9 1600x896、9:16 896x1600；
- 存量项目仍绑定 legacy 工作流（1056x608 / 608x1056）。

未登记身份直接 fail-closed：新增工作流必须在此登记原生尺寸后才会被质检接受，
避免再次出现“工作流换了、质检没换”的静默失配（2026-10-21 事故）。
"""

from __future__ import annotations

from collections.abc import Mapping

from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.video_generation.domain import VideoRuleViolation

WORKFLOW_NATIVE_SIZE_UNKNOWN = "video_workflow_native_size_unknown"

# 每工作流登记的“横向（16:9）原生像素”；纵向（9:16）由此交换得到。
# 依据：引擎 manifest notes 的 "Actual aligned output sizes"（HD）与历史
# 1056x608 归档实测（legacy / community，无上采样分支）。
_LANDSCAPE_NATIVE_SIZES: dict[tuple[str, str], tuple[int, int]] = {
    ("h3.tubeflow.storyboard-to-video", "1.0.0"): (1056, 608),
    ("h3.community.reference-images-to-video", "1.1.0-mvp"): (1056, 608),
    ("h3.tubeflow.storyboard-to-video-hd", "1.0.1"): (1600, 896),
}


def workflow_identity(manifest: object) -> tuple[str, str] | None:
    """从冻结的 workflow_manifest 读取 (workflow_id, workflow_version)。"""
    if not isinstance(manifest, Mapping):
        return None
    workflow_id = manifest.get("workflow_id")
    workflow_version = manifest.get("workflow_version")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return None
    if not isinstance(workflow_version, str) or not workflow_version.strip():
        return None
    return workflow_id.strip(), workflow_version.strip()


def is_registered(workflow_id: str, workflow_version: str) -> bool:
    return (workflow_id, workflow_version) in _LANDSCAPE_NATIVE_SIZES


def expected_native_size(manifest: object, aspect: AspectRatio) -> tuple[int, int]:
    """返回该工作流在该画幅下的原生像素尺寸；未登记身份 fail-closed。"""
    identity = workflow_identity(manifest)
    if identity is None:
        raise VideoRuleViolation(WORKFLOW_NATIVE_SIZE_UNKNOWN)
    landscape = _LANDSCAPE_NATIVE_SIZES.get(identity)
    if landscape is None:
        raise VideoRuleViolation(WORKFLOW_NATIVE_SIZE_UNKNOWN)
    width, height = landscape
    return (width, height) if aspect is AspectRatio.LANDSCAPE else (height, width)
