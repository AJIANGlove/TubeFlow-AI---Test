from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from .models import ExecutionRequest, WorkflowSelection

MODEL_KEY = "minimax-h3-community"
MODEL_VERSION = "2026-08-13-community-duration-v1"
T2V_WORKFLOW_ID = "h3.community.text-to-video"
R2V_WORKFLOW_ID = "h3.community.reference-images-to-video"
WORKFLOW_VERSION = "1.1.0-mvp"
CANDIDATE_WORKFLOW_VERSION = "1.0.0"
KINESTACK_T2V_WORKFLOW_ID = "h3.kinestack.text-to-video"
KINESTACK_R2V_WORKFLOW_ID = "h3.kinestack.reference-images-to-video"
KINESTACK_T2V_HD_WORKFLOW_ID = "h3.kinestack.text-to-video-hd"
KINESTACK_R2V_HD_WORKFLOW_ID = "h3.kinestack.reference-images-to-video-hd"
TUBEFLOW_STORYBOARD_WORKFLOW_ID = "h3.tubeflow.storyboard-to-video"
MIN_DURATION_SECONDS = 3
MAX_DURATION_SECONDS = 15
FRAMES_PER_SECOND = 24
TEMPORAL_GRID_STEP = 17
TEMPORAL_GRID_OFFSET = 5
PROFILE_WORKFLOWS = {
    "legacy": ("h3-t2v", "h3-r2v-images"),
    "candidate": ("h3-kinestack-t2v", "h3-kinestack-r2v-images", "h3-tubeflow-storyboard"),
    "candidate-hd": (
        "h3-kinestack-t2v-hd-1.0.1", "h3-kinestack-r2v-images-hd-1.0.1", "h3-tubeflow-storyboard",
    ),
}

# Superseded immutable identities are only available for history and cleanup.
HISTORICAL_CANDIDATE_WORKFLOWS = ("h3-kinestack-t2v-hd", "h3-kinestack-r2v-images-hd")

LEGACY_SELECTIONS = {
    "video.text_to_video": {
        "model_version": "2026-08-02-community-mvp",
        "workflow_version": "1.0.0-mvp",
        "workflow_manifest_sha256": "9acd91f2476e11a1b2a0029bd852dff08a426ba8617ee5f1b7073748cc7579fb",
    },
    "video.reference_images_to_video": {
        "model_version": "2026-08-02-community-mvp",
        "workflow_version": "1.0.0-mvp",
        "workflow_manifest_sha256": "87227501946afb8c7508544a8b7ab71137f834930d3b002805199b03a8d8c8d5",
    },
}


class H3WorkflowCatalog:
    """Only the immutable workflow set installed for this Engine profile is accepted."""

    def __init__(self, profile: str = "legacy") -> None:
        if profile not in PROFILE_WORKFLOWS:
            raise ValueError("Unknown H3 workflow profile")
        self.profile = profile
        self.is_candidate = profile != "legacy"
        root = Path(__file__).resolve().parent / "workflows"
        active_names = PROFILE_WORKFLOWS[profile]
        # Historical namespaces remain resolvable after switching or rolling back.
        # This registry does not admit submissions or make old graphs executable.
        names = (
            tuple(dict.fromkeys(
                active_names + PROFILE_WORKFLOWS["candidate"] + PROFILE_WORKFLOWS["candidate-hd"]
                + HISTORICAL_CANDIDATE_WORKFLOWS
            ))
            if self.is_candidate else active_names
        )
        self.templates: dict[str, dict[str, Any]] = {}
        self.manifests: dict[str, dict[str, Any]] = {}
        self.digests: dict[str, str] = {}
        self._selections: dict[tuple[str, ...], str] = {}
        self._history_selections: dict[tuple[str, ...], str] = {}
        weights_path = root / "h3-approved-weights.json"
        self.approved_weights = json.loads(weights_path.read_text(encoding="utf-8"))
        approved_digest = sha256(weights_path.read_bytes()).hexdigest()
        for name in names:
            manifest_path = root / f"{name}.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            workflow_id = manifest["workflow_id"]
            if manifest["model"]["weight_manifest_sha256"] != approved_digest:
                raise RuntimeError(f"{workflow_id} weight manifest digest mismatch")
            workflow_path = root / manifest["api_workflow"]["relative_path"]
            workflow_bytes = workflow_path.read_bytes()
            if sha256(workflow_bytes).hexdigest() != manifest["api_workflow"]["sha256"]:
                raise RuntimeError(f"{workflow_id} API workflow digest mismatch")
            digest = sha256(manifest_path.read_bytes()).hexdigest()
            identity = {
                "capability": manifest["capability"],
                "model_key": manifest["model"]["model_key"],
                "model_version": manifest["model"]["model_version"],
                "workflow_id": workflow_id,
                "workflow_version": manifest["workflow_version"],
                "workflow_manifest_sha256": digest,
            }
            self._history_selections[_selection_key(identity)] = workflow_id
            if name in active_names:
                self.templates[workflow_id] = json.loads(workflow_bytes)
                self.manifests[workflow_id] = manifest
                self.digests[workflow_id] = digest
        for item in self.capability_items():
            self._selections[_selection_key(item)] = item["workflow_id"]

    def capability_items(self) -> list[dict[str, Any]]:
        return [
            {
                "capability": manifest["capability"],
                "model_key": manifest["model"]["model_key"],
                "model_version": manifest["model"]["model_version"],
                "workflow_id": workflow_id,
                "workflow_version": manifest["workflow_version"],
                "workflow_manifest_sha256": self.digests[workflow_id],
                "status": "AVAILABLE",
            }
            for workflow_id, manifest in self.manifests.items()
        ]

    def _selected_workflow_id(self, selection: WorkflowSelection) -> str | None:
        actual = selection.model_dump()
        workflow_id = self._selections.get(_selection_key(actual))
        if workflow_id is not None or self.profile != "legacy":
            return workflow_id
        legacy = LEGACY_SELECTIONS.get(selection.capability)
        if legacy is None:
            return None
        expected = {
            "capability": selection.capability,
            "model_key": MODEL_KEY,
            "workflow_id": (
                T2V_WORKFLOW_ID if selection.capability == "video.text_to_video" else R2V_WORKFLOW_ID
            ),
            **legacy,
        }
        return expected["workflow_id"] if _selection_key(actual) == _selection_key(expected) else None

    def validate_selection(self, request: ExecutionRequest) -> list[dict[str, Any]]:
        if not any(
            item["capability"] == request.workflow.capability for item in self.capability_items()
        ):
            return [_problem("WORKFLOW_NOT_AVAILABLE", "工作流未安装")]
        workflow_id = self._selected_workflow_id(request.workflow)
        if workflow_id is not None:
            current = _selection_key(request.workflow.model_dump()) in self._selections
            if current or request.parameters.duration_seconds == 5:
                return []
        return [_problem("WORKFLOW_VERSION_MISMATCH", "任务引用的模型或工作流版本不受支持")]

    def namespace_for(self, selection: WorkflowSelection) -> str:
        workflow_id = (
            self._history_selections.get(_selection_key(selection.model_dump()))
            if self.is_candidate else self._selected_workflow_id(selection)
        )
        if workflow_id is None:
            raise ValueError("Workflow selection is not approved as a historical identity")
        return "tubeflow-test" if workflow_id == TUBEFLOW_STORYBOARD_WORKFLOW_ID else "kinestack"

    def bind(
        self,
        request: ExecutionRequest,
        staged_files: dict[str, str],
        output_prefix: str,
    ) -> dict[str, Any]:
        if self.validate_selection(request):
            raise ValueError("Workflow selection is not approved for this Engine profile")
        workflow_id = self._selected_workflow_id(request.workflow)
        assert workflow_id is not None
        workflow = deepcopy(self.templates[workflow_id])
        width, height = dimensions(request.parameters.aspect_ratio)
        length = frame_count(request.parameters.duration_seconds)
        seed = request.parameters.seed
        if seed is None:
            seed = int.from_bytes(sha256(str(request.attempt_id).encode()).digest()[:8], "big")
        else:
            seed = int(seed)

        workflow["noise"]["inputs"]["noise_seed"] = seed
        workflow["condition"]["inputs"].update(
            {
                "prompt": translate_references(request.prompt, len(request.staged_assets)),
                "width": width,
                "height": height,
                "length": length,
            }
        )
        workflow["save"]["inputs"]["filename_prefix"] = output_prefix
        if request.workflow.capability == "video.reference_images_to_video":
            workflow["condition"]["inputs"]["ref_image_size"] = (
                request.parameters.reference_image_size or "match"
            )
            for item in sorted(request.staged_assets, key=lambda value: value.ordinal):
                node_id = f"reference_{item.ordinal}"
                workflow[node_id] = {
                    "class_type": "LoadImage",
                    "inputs": {"image": staged_files[str(item.staged_asset_id)]},
                }
                workflow["condition"]["inputs"][f"ref_images.ref_image_{item.ordinal - 1}"] = [node_id, 0]
        return workflow


def _selection_key(selection: dict[str, Any]) -> tuple[str, ...]:
    return tuple(selection[field] for field in WorkflowSelection.model_fields)


def _problem(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "category": "INPUT", "retryable": False, "message": message}


def frame_count(duration_seconds: int) -> int:
    if not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
        raise ValueError("duration_seconds must be between 3 and 15")
    requested = duration_seconds * FRAMES_PER_SECOND
    grid_index = round((requested - TEMPORAL_GRID_OFFSET) / TEMPORAL_GRID_STEP)
    return TEMPORAL_GRID_OFFSET + TEMPORAL_GRID_STEP * grid_index


def dimensions(aspect_ratio: str) -> tuple[int, int]:
    return {
        "16:9": (1056, 608),
        "9:16": (608, 1056),
        "1:1": (768, 768),
        "4:3": (896, 672),
        "3:4": (672, 896),
    }[aspect_ratio]


def translate_references(prompt: str, count: int) -> str:
    labels = "一二三四五六七八九"
    translated = prompt
    for ordinal in range(1, count + 1):
        translated = translated.replace(f"@图{labels[ordinal - 1]}", f"<Picture {ordinal}>")
    return translated
