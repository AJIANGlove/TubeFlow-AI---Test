from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.video_generation.domain import (
    GroupVideoGeneration,
    ImmutableWorkflowSnapshot,
    VideoRuleViolation,
    provider_duration_seconds,
)
from tubeflow_test.video_generation.engine import EngineError, EngineNode, NodeHealth
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.service import LowLevelEngineContractHarness


class MemoryAssets:
    def __init__(self) -> None:
        self.content: dict[UUID, bytes] = {}

    def make(self, value: bytes) -> MediaAssetRef:
        asset = MediaAssetRef(uuid4(), hashlib.sha256(value).hexdigest(), "image/png", len(value))
        self.content[asset.asset_id] = value
        return asset

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        return self.content[asset.asset_id]

    def archive_immutable(
        self,
        *,
        generation_id: UUID,
        content: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef:
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        asset = MediaAssetRef(uuid4(), expected_sha256, media_type, len(content))
        self.content[asset.asset_id] = content
        return asset


class AllowNarration:
    def assert_project_video_allowed(self, project_id: UUID) -> None:
        assert isinstance(project_id, UUID)


class BlockNarration:
    def assert_project_video_allowed(self, project_id: UUID) -> None:
        raise RuntimeError(f"narration blocked for {project_id}")


def snapshot() -> ImmutableWorkflowSnapshot:
    manifest = {"frozen": True}
    return ImmutableWorkflowSnapshot(
        "minimax-h3",
        "1",
        "h3-r2v-images",
        "1",
        hashlib.sha256(b'{"frozen":true}\n').hexdigest(),
        "preview",
        manifest,
    )


def generation(
    assets: MemoryAssets,
    character_count: int = 1,
    duration_ms: int = 3000,
    aspect: AspectRatio = AspectRatio.LANDSCAPE,
) -> GroupVideoGeneration:
    return GroupVideoGeneration(
        uuid4(),
        uuid4(),
        uuid4(),
        tuple(assets.make(f"character-{index}".encode()) for index in range(character_count)),
        assets.make(b"whole-storyboard"),
        "按故事板顺序表现组内事件",
        duration_ms,
        aspect,
        7,
        snapshot(),
        uuid4(),
        uuid4(),
        assets.make(b"whole-storyboard").sha256,
    )


def nodes(*, first_health: NodeHealth = NodeHealth.READY) -> tuple[EngineNode, ...]:
    return (
        EngineNode("node-a", "http://node-a/engine/v1", "secret-a", 1, first_health),
        EngineNode("node-b", "http://node-b/engine/v1", "secret-b", 2),
        EngineNode("node-c", "http://node-c/engine/v1", "secret-c", 1),
    )


@pytest.mark.parametrize(
    "milliseconds, expected",
    [
        (1, 3),
        (2500, 3),
        (2501, 4),
        (3000, 4),
        (3001, 4),
        (8500, 9),
        (8501, 10),
        (8840, 10),
        (14500, 15),
        (14600, 15),
        (15000, 15),
    ],
)
def test_duration_mapping(milliseconds: int, expected: int) -> None:
    assert provider_duration_seconds(milliseconds) == expected


@pytest.mark.parametrize("milliseconds", [0, 15001, 16000])
def test_duration_rejects_invalid_or_over_limit(milliseconds: int) -> None:
    with pytest.raises(VideoRuleViolation):
        provider_duration_seconds(milliseconds)


@pytest.mark.parametrize("character_count", [1, 8])
@pytest.mark.parametrize("aspect", [AspectRatio.LANDSCAPE, AspectRatio.PORTRAIT])
def test_reference_order_count_prompt_and_aspect(character_count: int, aspect: AspectRatio) -> None:
    assets = MemoryAssets()
    item = generation(assets, character_count, aspect=aspect)
    assert len(item.references) == character_count + 1
    assert [reference.ordinal for reference in item.references] == list(
        range(1, character_count + 2)
    )
    assert item.references[-1].kind.value == "STORYBOARD"
    assert item.references[-1].prompt_label == f"@图{'九' if character_count == 8 else '二'}"
    assert item.aspect_ratio is aspect


def test_ten_total_references_are_rejected() -> None:
    assets = MemoryAssets()
    with pytest.raises(VideoRuleViolation):
        generation(assets, 9)


def test_contract_contains_no_forbidden_media_modes() -> None:
    assets = MemoryAssets()
    engine = FakeEngine()
    service = LowLevelEngineContractHarness(engine, assets, nodes(), AllowNarration())
    service.submit(generation(assets), uuid4())
    body = next(iter(engine._submissions.values()))
    serialized = repr(body).lower()
    assert body["workflow"]["capability"] == "video.reference_images_to_video"
    assert body["parameters"]["resolution_preset"] == "preview"
    for forbidden in ("first_frame", "last_frame", "reference_video", "reference_audio"):
        assert forbidden not in serialized


def test_project_narration_gate_blocks_before_any_h3_side_effect() -> None:
    assets = MemoryAssets()
    engine = FakeEngine()
    service = LowLevelEngineContractHarness(engine, assets, nodes(), BlockNarration())
    with pytest.raises(RuntimeError, match="narration blocked"):
        service.submit(generation(assets), uuid4())
    assert engine.calls == []


def test_upload_failure_cleans_only_successfully_staged_assets() -> None:
    assets = MemoryAssets()
    engine = FakeEngine()
    engine.fail_stage_at = 2
    service = LowLevelEngineContractHarness(engine, assets, nodes(), AllowNarration())
    with pytest.raises(RuntimeError, match="upload failure"):
        service.submit(generation(assets), uuid4())
    assert sum(call.startswith("delete:") for _, call in engine.calls) == 1
    assert not any(call == "submit" for _, call in engine.calls)


def test_lost_accepted_response_reconciles_same_attempt_and_node_without_resubmit() -> None:
    assets = MemoryAssets()
    engine = FakeEngine()
    engine.lose_submit_response = True
    service = LowLevelEngineContractHarness(engine, assets, nodes(), AllowNarration())
    attempt = uuid4()
    result = service.submit(generation(assets), attempt)
    assert result.uncertain is True
    assert service.reconcile(attempt)["attempt_id"] == str(attempt)
    second = service.submit(generation(assets), attempt)
    assert second.node_id == result.node_id
    assert sum(call == "submit" for _, call in engine.calls) == 1
    assert {node for node, _ in engine.calls} == {result.node_id}


def test_offline_node_is_not_selected() -> None:
    assets = MemoryAssets()
    engine = FakeEngine()
    service = LowLevelEngineContractHarness(
        engine, assets, nodes(first_health=NodeHealth.OFFLINE), AllowNarration()
    )
    result = service.submit(generation(assets), uuid4())
    assert result.node_id == "node-b"


def test_no_available_node_blocks_before_upload() -> None:
    offline = tuple(
        EngineNode(f"n{i}", f"http://n{i}", "t", 1, NodeHealth.OFFLINE) for i in range(3)
    )
    assets = MemoryAssets()
    engine = FakeEngine()
    service = LowLevelEngineContractHarness(engine, assets, offline, AllowNarration())
    with pytest.raises(EngineError, match="no healthy"):
        service.submit(generation(assets), uuid4())
    assert engine.calls == []


def test_hash_mismatch_prevents_archive_confirmation_and_cleanup() -> None:
    assets = MemoryAssets()
    engine = FakeEngine()
    engine.output_sha256_override = "0" * 64
    service = LowLevelEngineContractHarness(engine, assets, nodes(), AllowNarration())
    item = generation(assets)
    attempt = uuid4()
    service.submit(item, attempt)
    with pytest.raises(EngineError, match="sha256 mismatch"):
        service.archive(item.generation_id, attempt)
    assert not any(call == "archive" or call.startswith("delete:") for _, call in engine.calls)


def test_success_archives_original_with_native_audio_then_confirms_and_cleans_exact_ids() -> None:
    assets = MemoryAssets()
    engine = FakeEngine()
    service = LowLevelEngineContractHarness(engine, assets, nodes(), AllowNarration())
    item = generation(assets, 2)
    attempt = uuid4()
    service.submit(item, attempt)
    archived = service.archive(item.generation_id, attempt)
    assert archived.native_audio_preserved_in_original and archived.immutable
    assert assets.content[archived.original_mp4.asset_id] == engine.output
    archive_index = engine.calls.index(("node-b", "archive"))
    delete_indexes = [i for i, (_, call) in enumerate(engine.calls) if call.startswith("delete:")]
    assert len(delete_indexes) == 3 and all(index > archive_index for index in delete_indexes)


def test_exactly_three_nodes_are_required() -> None:
    with pytest.raises(ValueError, match="exactly three"):
        LowLevelEngineContractHarness(FakeEngine(), MemoryAssets(), nodes()[:2], AllowNarration())


@pytest.mark.parametrize("seed", [True, -1, 2**53, "01", "-1", str(2**53 - 1), str(2**64)])
def test_seed_rejects_noncanonical_or_unsafe_values(seed: int | str) -> None:
    assets = MemoryAssets()
    with pytest.raises(VideoRuleViolation):
        GroupVideoGeneration(
            uuid4(),
            uuid4(),
            uuid4(),
            (assets.make(b"c"),),
            assets.make(b"s"),
            "prompt",
            3000,
            AspectRatio.LANDSCAPE,
            seed,
            snapshot(),
            uuid4(),
            uuid4(),
            assets.make(b"s").sha256,
        )


def test_seed_accepts_large_uint64_as_canonical_decimal_string() -> None:
    assets = MemoryAssets()
    item = GroupVideoGeneration(
        uuid4(),
        uuid4(),
        uuid4(),
        (assets.make(b"c"),),
        assets.make(b"s"),
        "prompt",
        3000,
        AspectRatio.LANDSCAPE,
        str(2**53),
        snapshot(),
        uuid4(),
        uuid4(),
        assets.make(b"s").sha256,
    )
    assert item.seed == str(2**53)
