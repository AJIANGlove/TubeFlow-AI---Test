"""Test-only stateless harness retained for low-level Engine contract tests.

Production assembly must use DurableGroupVideoOrchestrator and a persistent VideoRepository.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.video_generation.domain import (
    ArchivedGroupVideo,
    GroupVideoGeneration,
    provider_duration_seconds,
)
from tubeflow_test.video_generation.engine import (
    EngineGateway,
    EngineNode,
    EngineUncertain,
    SafeNodeScheduler,
    verify_content,
)


class AssetBytes(Protocol):
    def read_verified(self, asset: MediaAssetRef) -> bytes: ...
    def archive_immutable(
        self, *, generation_id: UUID, content: bytes, media_type: str, expected_sha256: str
    ) -> MediaAssetRef: ...


class NarrationVideoGate(Protocol):
    def assert_project_video_allowed(self, project_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class Submission:
    attempt_id: UUID
    node_id: str
    execution: dict[str, object] | None
    uncertain: bool


class LowLevelEngineContractHarness:
    def __init__(
        self,
        engine: EngineGateway,
        assets: AssetBytes,
        nodes: tuple[EngineNode, ...],
        narration_gate: NarrationVideoGate,
    ) -> None:
        if len(nodes) != 3:
            raise ValueError("exactly three H3 engine nodes must be configured")
        self._engine, self._assets, self._nodes, self._narration_gate = (
            engine,
            assets,
            nodes,
            narration_gate,
        )
        self._scheduler = SafeNodeScheduler()
        self._assignments: dict[UUID, EngineNode] = {}
        self._staged: dict[UUID, tuple[str, ...]] = {}

    def submit(self, generation: GroupVideoGeneration, attempt_id: UUID) -> Submission:
        self._narration_gate.assert_project_video_allowed(generation.project_id)
        if attempt_id in self._assignments:
            node = self._assignments[attempt_id]
            return Submission(
                attempt_id, node.node_id, self._engine.inspect(node, attempt_id), False
            )
        node = self._scheduler.select(self._nodes)
        self._assignments[attempt_id] = node
        staged_ids: list[str] = []
        bound: list[dict[str, object]] = []
        try:
            for reference in generation.references:
                content = self._assets.read_verified(reference.asset)
                staged = self._engine.stage_asset(
                    node,
                    content=content,
                    media_type=reference.asset.media_type,
                    filename=f"reference-{reference.ordinal}",
                    expected_sha256=reference.asset.sha256,
                    idempotency_key=f"{attempt_id}:stage:{reference.ordinal}",
                )
                if (
                    staged.get("sha256") != reference.asset.sha256
                    or staged.get("size_bytes") != reference.asset.size_bytes
                ):
                    raise ValueError("staged asset integrity mismatch")
                staged_id = str(staged["id"])
                staged_ids.append(staged_id)
                bound.append(
                    {
                        "staged_asset_id": staged_id,
                        "binding_key": reference.binding_key,
                        "ordinal": reference.ordinal,
                    }
                )
            self._staged[attempt_id] = tuple(staged_ids)
            snap = generation.snapshot
            body = {
                "attempt_id": str(attempt_id),
                "workflow": {
                    "capability": "video.reference_images_to_video",
                    "model_key": snap.model_key,
                    "model_version": snap.model_version,
                    "workflow_id": snap.workflow_id,
                    "workflow_version": snap.workflow_version,
                    "workflow_manifest_sha256": snap.workflow_manifest_sha256,
                },
                "prompt": generation.group_prompt,
                "staged_assets": bound,
                "parameters": {
                    "duration_seconds": provider_duration_seconds(generation.target_duration_ms),
                    "aspect_ratio": generation.aspect_ratio.value,
                    # KineStack's REF2VA schema calls its native H3 output preset "preview".
                    "resolution_preset": "preview",
                    "reference_image_size": "match",
                    "seed": generation.seed,
                },
            }
            try:
                execution = self._engine.submit(
                    node, idempotency_key=f"{attempt_id}:submit", body=body
                )
            except EngineUncertain:
                return Submission(attempt_id, node.node_id, None, True)
            return Submission(attempt_id, node.node_id, execution, False)
        except Exception:
            for staged_id in reversed(staged_ids):
                self._engine.delete_staged_asset(node, staged_id)
            raise

    def reconcile(self, attempt_id: UUID) -> dict[str, object]:
        return self._engine.inspect(self._assigned(attempt_id), attempt_id)

    def cancel(self, attempt_id: UUID) -> dict[str, object]:
        return self._engine.cancel(
            self._assigned(attempt_id),
            attempt_id,
            idempotency_key=f"{attempt_id}:cancel",
        )

    def archive(self, generation_id: UUID, attempt_id: UUID) -> ArchivedGroupVideo:
        node = self._assigned(attempt_id)
        items = self._engine.outputs(node, attempt_id).get("items", [])
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("successful group video must expose exactly one output")
        output = items[0]
        content = self._engine.download(node, attempt_id, str(output["id"]))
        size, digest = int(output["size_bytes"]), str(output["sha256"])
        verify_content(content, size, digest)
        asset = self._assets.archive_immutable(
            generation_id=generation_id,
            content=content,
            media_type=str(output["media_type"]),
            expected_sha256=digest,
        )
        self._engine.confirm_archive(
            node,
            attempt_id,
            idempotency_key=f"{attempt_id}:archive-confirm",
        )
        for staged_id in self._staged.pop(attempt_id, ()):
            self._engine.delete_staged_asset(node, staged_id)
        return ArchivedGroupVideo(generation_id, attempt_id, asset, size, digest)

    def _assigned(self, attempt_id: UUID) -> EngineNode:
        try:
            return self._assignments[attempt_id]
        except KeyError as exc:
            raise ValueError("unknown attempt; node transfer or resubmission is forbidden") from exc
