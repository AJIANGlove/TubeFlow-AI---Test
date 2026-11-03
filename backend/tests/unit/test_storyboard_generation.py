from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image

from tubeflow_test.adapters.in_memory_storyboards import InMemoryStoryboardRepository
from tubeflow_test.adapters.storyboard_image_qc import PillowStoryboardTechnicalQc
from tubeflow_test.application.storyboard_failover import (
    StoryboardFailoverPolicy,
    StoryboardFailoverStep,
)
from tubeflow_test.application.storyboards import (
    DirectorPlanService,
    ProviderPoll,
    ProviderSubmission,
    ProviderSubmitNotDeliveredError,
    ProviderSubmitRejectedError,
    StoryboardContext,
    StoryboardGenerationService,
    canonical_character_payload,
    canonical_storyboard_request_sha256,
)
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.director_generation import DirectorSentenceSnapshot
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    GenerationStatus,
    ReviewState,
    ShotSentenceLink,
    StoryboardAsset,
    StoryboardAssetKind,
    StoryboardGeneration,
    VideoStoryboardBinding,
)


def test_canonical_storyboard_hash_binds_all_provider_authority_facts() -> None:
    owner_id, group_id, revision_id = uuid4(), uuid4(), uuid4()
    character = {
        "ordinal": 0,
        "character_image_id": str(uuid4()),
        "asset_id": str(uuid4()),
        "sha256": "a" * 64,
        "media_type": "image/png",
        "size_bytes": 10,
    }
    arguments = {
        "owner_id": owner_id,
        "group_id": group_id,
        "revision_id": revision_id,
        "prompt": "canonical prompt",
        "characters": [character],
        "style": "ink",
        "model": "grid-v1",
        "parameters": {"seed": 7},
    }
    canonical = canonical_storyboard_request_sha256(**arguments)  # type: ignore[arg-type]
    assert len(canonical) == 64
    for field, changed in (
        ("prompt", "drifted prompt"),
        ("style", "drifted style"),
        ("model", "grid-v2"),
        ("parameters", {"seed": 8}),
        ("characters", [{**character, "ordinal": 1}]),
    ):
        drifted = {**arguments, field: changed}
        assert canonical_storyboard_request_sha256(**drifted) != canonical  # type: ignore[arg-type]


def test_canonical_character_payload_is_shared_by_generation_and_h3_assembly() -> None:
    # 生成端取 CharacterAssetSnapshot（1 基 ordinal）；H3 装配端从
    # project_snapshot_characters（0 基 ordinal）以 ordinal + 1 重建，两侧必须一致。
    character_image_id, asset_id = uuid4(), uuid4()
    digest = "b" * 64
    generation_side = canonical_character_payload(
        ordinal=1,
        character_image_id=character_image_id,
        asset_id=asset_id,
        sha256=digest,
        media_type="image/png",
        size_bytes=10,
    )
    assembly_side = canonical_character_payload(
        ordinal=0 + 1,
        character_image_id=character_image_id,
        asset_id=asset_id,
        sha256=digest,
        media_type="image/png",
        size_bytes=10,
    )
    assert (
        generation_side
        == assembly_side
        == {
            "ordinal": 1,
            "character_image_id": str(character_image_id),
            "asset_id": str(asset_id),
            "sha256": digest,
            "media_type": "image/png",
            "size_bytes": 10,
        }
    )


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 28, tzinfo=UTC)


class Provider:
    def __init__(self) -> None:
        self.submissions = 0
        self.reconciled: ProviderSubmission | None = None
        self.polls: list[ProviderPoll] = []
        self.corrupt_download = False

    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[object, ...] = ()
    ) -> ProviderSubmission:
        del references
        self.submissions += 1
        return ProviderSubmission(f"remote-{generation.idempotency_key}", "submitted")

    def reconcile(self, idempotency_key: str) -> ProviderSubmission | None:
        return self.reconciled

    def poll(self, task_id: str) -> ProviderPoll:
        del task_id
        return self.polls.pop(0)

    def download(self, download_token: str, destination: Path) -> None:
        del download_token
        if self.corrupt_download:
            destination.write_bytes(b"corrupt")
            return
        Image.new("RGB", (64, 36), "white").save(destination, "PNG")


class UnknownSubmitProvider(Provider):
    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[object, ...] = ()
    ) -> ProviderSubmission:
        del generation, references
        self.submissions += 1
        raise TimeoutError


class NotDeliveredSubmitProvider(Provider):
    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[object, ...] = ()
    ) -> ProviderSubmission:
        del generation, references
        self.submissions += 1
        raise ProviderSubmitNotDeliveredError("connect-stage failure")


class RejectedSubmitProvider(Provider):
    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[object, ...] = ()
    ) -> ProviderSubmission:
        del generation, references
        self.submissions += 1
        raise ProviderSubmitRejectedError("definitive rejection")


class PortraitProvider(Provider):
    """下载到非 16:9 画幅（模拟 Provider 单次输出波动）。"""

    def download(self, download_token: str, destination: Path) -> None:
        del download_token
        Image.new("RGB", (64, 114), "white").save(destination, "PNG")


def portrait_poll() -> ProviderPoll:
    stream = BytesIO()
    Image.new("RGB", (64, 114), "white").save(stream, "PNG")
    return ProviderPoll(
        GenerationStatus.SUCCEEDED,
        "ok",
        "portrait-token",
        hashlib.sha256(stream.getvalue()).hexdigest(),
    )


def image_digest() -> str:
    stream = BytesIO()
    Image.new("RGB", (64, 36), "white").save(stream, "PNG")
    return hashlib.sha256(stream.getvalue()).hexdigest()


class Storage:
    def __init__(self) -> None:
        self.by_hash: dict[str, MediaAssetRef] = {}
        self.puts = 0

    def put_verified(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        source: Path,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef:
        del owner_id, project_id
        self.puts += 1
        payload = source.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        return self.by_hash.setdefault(
            expected_sha256, MediaAssetRef(uuid4(), expected_sha256, media_type, len(payload))
        )

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        payload = b"character"
        assert asset.sha256 == hashlib.sha256(payload).hexdigest()
        return payload

    def delete_exact(self, asset: MediaAssetRef) -> None:
        raise NotImplementedError


def setup(
    tmp_path: Path,
    owner: UUID | None = None,
    *,
    steps: tuple[StoryboardFailoverStep, ...] | None = None,
) -> tuple[
    InMemoryStoryboardRepository,
    StoryboardGenerationService,
    Provider,
    UUID,
    UUID,
    DirectorPlanRevision,
]:
    owner = owner or uuid4()
    project, group, sentence = uuid4(), uuid4(), uuid4()
    repo = InMemoryStoryboardRepository()
    character_payload = b"character"
    character = CharacterAssetSnapshot(
        1,
        uuid4(),
        uuid4(),
        hashlib.sha256(character_payload).hexdigest(),
        "image/png",
        len(character_payload),
    )
    repo.add_context(
        StoryboardContext(
            owner,
            project,
            group,
            (sentence,),
            True,
            (character,),
            "ink",
            sentences=(DirectorSentenceSnapshot(sentence, 1, "line", "2" * 64, 0, 3000),),
        )
    )
    item = DirectorShot(uuid4(), 1, "station", "wide", "eye", "arrives", "establish", "Arrival")
    revision = DirectorPlanRevision(
        uuid4(),
        owner,
        project,
        group,
        1,
        DirectorRevisionKind.APPLIED,
        (item,),
        (ShotSentenceLink(item.shot_id, sentence),),
        (sentence,),
        ReviewState.NOT_REQUIRED,
        (),
        Clock().now(),
    )
    repo.save_revision(revision)
    provider, storage = Provider(), Storage()
    service = StoryboardGenerationService(
        repo,
        provider,
        storage,
        PillowStoryboardTechnicalQc(),
        Ids(),
        Clock(),
        tmp_path,
        StoryboardFailoverPolicy(
            steps=steps
            or (StoryboardFailoverStep("contract", "fake-v1", {"seed": 7}),)
        ),
    )
    return repo, service, provider, owner, group, revision


def create(
    service: StoryboardGenerationService, owner: UUID, group: UUID, revision: UUID, key: str = "key"
) -> StoryboardGeneration:
    return service.create(
        group_id=group, owner_id=owner, expected_applied_revision_id=revision, idempotency_key=key
    )


def test_owner_scope_and_idempotency_hash_conflict(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path)
    first = create(service, owner, group, revision.revision_id)
    assert create(service, owner, group, revision.revision_id).generation_id == first.generation_id
    with pytest.raises(DomainError, match="not found"):
        service.resume(first.generation_id, uuid4())
    changed = DirectorPlanRevision(
        uuid4(),
        owner,
        revision.project_id,
        group,
        2,
        DirectorRevisionKind.APPLIED,
        revision.shots,
        revision.sentence_links,
        revision.group_sentence_ids,
        ReviewState.NOT_REQUIRED,
        (),
        Clock().now(),
    )
    repo.save_revision(changed)
    with pytest.raises(DomainError, match="another request"):
        create(service, owner, group, changed.revision_id)


def test_same_idempotency_key_is_independent_between_owners(tmp_path: Path) -> None:
    _, first_service, _, first_owner, first_group, first_revision = setup(tmp_path)
    _, second_service, _, second_owner, second_group, second_revision = setup(tmp_path)
    first = create(first_service, first_owner, first_group, first_revision.revision_id, "same")
    second = create(second_service, second_owner, second_group, second_revision.revision_id, "same")
    assert first.owner_id != second.owner_id
    assert first.generation_id != second.generation_id
    assert first.provider_idempotency_key != second.provider_idempotency_key


def test_narration_gate_is_rechecked_for_edit_and_apply(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path)
    context = repo.context(owner, group)
    assert context is not None
    repo.add_context(replace(context, narration_ready=False))
    director = DirectorPlanService(repo, Ids(), Clock())
    with pytest.raises(DomainError, match="narration gate"):
        director.edit_draft(
            owner_id=owner,
            group_id=group,
            shots=revision.shots,
            links=revision.sentence_links,
            expected_draft_revision_id=None,
            continuous_observation_approvals=(),
        )


def test_character_byte_facts_are_bound_into_canonical_request(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path)
    create(service, owner, group, revision.revision_id, "same")
    context = repo.context(owner, group)
    assert context is not None
    original = context.character_assets[0]
    changed = replace(original, sha256="f" * 64, size_bytes=11)
    repo.add_context(replace(context, character_assets=(changed,)))
    with pytest.raises(DomainError, match="another request"):
        create(service, owner, group, revision.revision_id, "same")


def test_submit_claim_and_response_loss_never_resubmit(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path)
    generation = create(service, owner, group, revision.revision_id)
    claimed = repo.claim_submit(owner, generation.generation_id)
    assert claimed is not None
    provider.reconciled = ProviderSubmission("remote-lost", "recovered")
    recovered = service.resume(generation.generation_id, owner)
    assert recovered.provider_task_id == "remote-lost"
    assert provider.submissions == 0


def test_raw_narration_failure_blocks_storyboard_provider_call(
    tmp_path: Path,
) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path)
    generation = create(service, owner, group, revision.revision_id)
    context = repo.context(owner, group)
    assert context is not None
    repo.add_context(replace(context, narration_ready=False))
    with pytest.raises(DomainError) as blocked:
        service.resume(generation.generation_id, owner)
    assert blocked.value.code == "narration_not_ready"
    assert provider.submissions == 0


def test_completion_is_replay_safe_and_current_never_regresses(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path)
    provider.polls = [
        ProviderPoll(GenerationStatus.SUCCEEDED, "ok", "token", image_digest()) for _ in range(3)
    ]
    values = []
    for index in range(1, 4):
        value = create(service, owner, group, revision.revision_id, str(index))
        values.append(value)
        assert service.resume(value.generation_id, owner).status is GenerationStatus.SUBMITTED
        assert service.resume(value.generation_id, owner).status is GenerationStatus.SUCCEEDED
    assert [value.version for value in values] == [1, 2, 3]
    current = repo.get_current_generation(owner, group)
    assert current is not None and current.generation_id == values[-1].generation_id
    for value in values:
        completed = service.resume(value.generation_id, owner)
        assert completed.generation_id == value.generation_id
        assert completed.status is GenerationStatus.SUCCEEDED
    current = repo.get_current_generation(owner, group)
    assert current is not None and current.generation_id == values[-1].generation_id


def test_portrait_result_retries_same_route_before_terminal(tmp_path: Path) -> None:
    """画幅不符按可重试失败进入换路链：首跳同路由第 2 次，而不是直接终态。"""
    repo, service, _, owner, group, revision = setup(tmp_path)
    portrait = PortraitProvider()
    service._provider = portrait  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id)
    assert service.resume(first.generation_id, owner).status is GenerationStatus.SUBMITTED
    portrait.polls = [portrait_poll()]
    failed = service.resume(first.generation_id, owner)

    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "download_or_qc"
    assert failed.failure_code == "aspect_ratio_not_supported"
    assert repo.get_current_generation(owner, group) is None

    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert successor.status is GenerationStatus.INTENT_PERSISTED
    assert successor.failover_route_index == 0
    assert successor.failover_model_attempt == 2
    assert successor.failover_total_attempt == 2
    assert successor.provider == first.provider and successor.model == first.model


def test_portrait_result_is_terminal_only_after_failover_budget(tmp_path: Path) -> None:
    """单路由策略下用满"每模型 2 次"预算才终态（不再需要人工恢复）。"""
    repo, service, provider, owner, group, revision = setup(tmp_path)
    portrait = PortraitProvider()
    service._provider = portrait  # noqa: SLF001
    current = create(service, owner, group, revision.revision_id)
    portrait.polls = [portrait_poll() for _ in range(4)]
    assert service.resume(current.generation_id, owner).status is GenerationStatus.SUBMITTED

    attempts = 0
    while True:
        failed = service.resume(current.generation_id, owner)
        attempts += 1
        assert failed.status is GenerationStatus.FAILED
        assert failed.failure_code == "aspect_ratio_not_supported"
        successor = repo.find_generation_by_key(owner, f"auto-retry-{failed.generation_id}")
        if successor is None:
            break
        current = successor
        assert service.resume(current.generation_id, owner).status is GenerationStatus.SUBMITTED

    assert attempts == 2
    assert portrait.submissions == 2
    assert len([item for item in repo.generations.values() if item.group_id == group]) == 2


def test_portrait_result_moves_to_next_route_after_model_budget(tmp_path: Path) -> None:
    """同路由 2 次仍不符时切到下一条路由（不因画幅直接死掉）。"""
    steps = (
        StoryboardFailoverStep("contract", "fake-v1", {"seed": 7}),
        StoryboardFailoverStep("backup", "fake-v2", {"seed": 9}),
    )
    repo, service, provider, owner, group, revision = setup(tmp_path, steps=steps)
    portrait = PortraitProvider()
    service._provider = portrait  # noqa: SLF001
    current = create(service, owner, group, revision.revision_id)
    portrait.polls = [portrait_poll() for _ in range(4)]
    assert service.resume(current.generation_id, owner).status is GenerationStatus.SUBMITTED

    chain = []
    while True:
        failed = service.resume(current.generation_id, owner)
        chain.append(failed)
        successor = repo.find_generation_by_key(owner, f"auto-retry-{failed.generation_id}")
        if successor is None:
            break
        current = successor
        assert service.resume(current.generation_id, owner).status is GenerationStatus.SUBMITTED

    assert [item.failover_route_index for item in chain] == [0, 0, 1, 1]
    assert [item.provider for item in chain] == ["contract", "contract", "backup", "backup"]
    assert chain[-1].status is GenerationStatus.FAILED
    assert chain[-1].failure_code == "aspect_ratio_not_supported"




def test_draft_cas_sentence_coverage_and_persisted_video_binding(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path)
    director = DirectorPlanService(repo, Ids(), Clock())
    draft = director.edit_draft(
        owner_id=owner,
        group_id=group,
        shots=revision.shots,
        links=revision.sentence_links,
        expected_draft_revision_id=None,
        continuous_observation_approvals=(),
    )
    with pytest.raises(DomainError, match="changed"):
        director.edit_draft(
            owner_id=owner,
            group_id=group,
            shots=revision.shots,
            links=revision.sentence_links,
            expected_draft_revision_id=None,
            continuous_observation_approvals=(),
        )
    applied = director.apply_draft(
        owner_id=owner, group_id=group, expected_draft_revision_id=draft.revision_id
    )
    value = create(service, owner, group, applied.revision_id, "video-source")
    media = MediaAssetRef(uuid4(), "d" * 64, "image/png", 20)
    asset = StoryboardAsset(
        uuid4(), value.generation_id, StoryboardAssetKind.GRID_SHEET, media, 10, 10, "PNG"
    )
    repo.generations[value.generation_id] = replace(
        value, status=GenerationStatus.SUCCEEDED, asset=asset
    )
    repo.current_by_group[(owner, group)] = value.generation_id
    binding = VideoStoryboardBinding(
        uuid4(),
        owner,
        value.project_id,
        group,
        value.generation_id,
        applied.revision_id,
        media.sha256,
    )
    repo.bind_current_for_test(binding)
    assert repo.get_video_binding(owner, binding.video_id) == binding


def test_qc_rejects_bad_image_oversize_and_wrong_hash(tmp_path: Path) -> None:
    qc = PillowStoryboardTechnicalQc(max_bytes=100, max_pixels=100)
    bad = tmp_path / "bad"
    bad.write_bytes(b"not-image")
    with pytest.raises(ValueError, match="decode"):
        qc.inspect(bad)
    image = tmp_path / "image.png"
    Image.new("RGB", (11, 10), "white").save(image)
    with pytest.raises(ValueError, match="pixel"):
        qc.inspect(image)
    with pytest.raises(ValueError, match="hash"):
        PillowStoryboardTechnicalQc().inspect(image, "0" * 64)
    portrait = tmp_path / "portrait.png"
    Image.new("RGB", (64, 114), "white").save(portrait)
    with pytest.raises(ValueError, match="aspect ratio"):
        PillowStoryboardTechnicalQc().inspect(portrait)


def test_unknown_grid_blocks_same_input_under_new_client_key(tmp_path: Path) -> None:
    _, service, _, owner, group, revision = setup(tmp_path)
    unknown_provider = UnknownSubmitProvider()
    service._provider = unknown_provider  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id, "first-key")
    assert (
        service.resume(first.generation_id, owner).status is GenerationStatus.REMOTE_STATE_UNKNOWN
    )
    with pytest.raises(DomainError) as blocked:
        create(service, owner, group, revision.revision_id, "second-key")
    assert blocked.value.code == "remote_state_unresolved"
    assert unknown_provider.submissions == 1


def test_hash_mismatch_checkpoint_survives_crash_and_is_rechecked(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path)
    value = create(service, owner, group, revision.revision_id)
    service.resume(value.generation_id, owner)
    provider.polls = [ProviderPoll(GenerationStatus.SUCCEEDED, "ok", "token", image_digest())]
    provider.corrupt_download = True
    with pytest.raises(DomainError, match="download or QC"):
        service.resume(value.generation_id, owner)
    checkpoint = repo.get_generation(owner, value.generation_id)
    assert checkpoint is not None
    assert checkpoint.expected_sha256 == image_digest()
    assert checkpoint.status is GenerationStatus.DOWNLOADED
    assert checkpoint.execution_token is not None
    repo.release_processing(owner, value.generation_id, checkpoint.execution_token)
    provider.corrupt_download = False
    completed = service.resume(value.generation_id, owner)
    assert completed.status is GenerationStatus.SUCCEEDED


def test_connect_stage_submit_failure_uses_failover_successor(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path)
    failing = NotDeliveredSubmitProvider()
    service._provider = failing  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id, "first-key")
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "submit"
    assert failing.submissions == 1
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert successor.failover_total_attempt == 2
    service._provider = provider  # noqa: SLF001
    submitted = service.resume(successor.generation_id, owner)
    assert submitted.status is GenerationStatus.SUBMITTED


def test_definitive_submit_rejection_is_final_and_allows_new_attempt(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path)
    rejected = RejectedSubmitProvider()
    service._provider = rejected  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id, "first-key")
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "submit"
    second = create(service, owner, group, revision.revision_id, "second-key")
    assert second.generation_id != first.generation_id


def test_legacy_connect_error_unknown_uses_failover_without_reconcile(tmp_path: Path) -> None:
    repo, service, provider, owner, group, revision = setup(tmp_path)
    unknown = UnknownSubmitProvider()
    service._provider = unknown  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id, "first-key")
    stuck = service.resume(first.generation_id, owner)
    assert stuck.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    repo.generations[first.generation_id] = replace(stuck, failure_code="ConnectError")
    failed = service.resume(first.generation_id, owner)
    assert failed.status is GenerationStatus.FAILED
    assert unknown.reconciled is None
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    service._provider = provider  # noqa: SLF001
    assert service.resume(successor.generation_id, owner).status is GenerationStatus.SUBMITTED


def test_post_send_submit_uncertainty_stays_unknown(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path)
    unknown = UnknownSubmitProvider()
    service._provider = unknown  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id, "first-key")
    stuck = service.resume(first.generation_id, owner)
    assert stuck.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    again = service.resume(first.generation_id, owner)
    assert again.status is GenerationStatus.REMOTE_STATE_UNKNOWN
    assert again.failure_stage == "reconcile"
    with pytest.raises(DomainError) as blocked:
        create(service, owner, group, revision.revision_id, "second-key")
    assert blocked.value.code == "remote_state_unresolved"


def test_prompt_imprints_shot_time_labels_and_snapshot(tmp_path: Path) -> None:
    repo, service, _, owner, group, revision = setup(tmp_path)
    generation = create(service, owner, group, revision.revision_id)
    assert 'Caption: "SHOT 1 · 0.0s–3.0s".' in generation.prompt
    assert "print its caption text exactly as quoted" in generation.prompt
    ranges = generation.shot_time_ranges_snapshot
    assert [(value.shot_order, value.start_ms, value.end_ms) for value in ranges] == [(1, 0, 3000)]
    restored = repo.get_generation(owner, generation.generation_id)
    assert restored is not None
    assert restored.shot_time_ranges_snapshot == ranges


def test_create_applies_prompt_safety_and_records_audit(tmp_path: Path) -> None:
    # 2026-10-14：违禁/高风险表达在生成意图创建时被确定性替换，命中规则随生成留档。
    repo, service, _, owner, group, revision = setup(tmp_path)
    risky = DirectorShot(
        uuid4(),
        1,
        "the seventeen-year-old bride's chamber",
        "wide",
        "eye",
        "attendants continue the being bathed ritual",
        "establish",
        "a disturbing fungal growth spreads across the wall",
    )
    changed = DirectorPlanRevision(
        uuid4(),
        owner,
        revision.project_id,
        group,
        2,
        DirectorRevisionKind.APPLIED,
        (risky,),
        (ShotSentenceLink(risky.shot_id, revision.sentence_links[0].sentence_id),),
        revision.group_sentence_ids,
        ReviewState.NOT_REQUIRED,
        (),
        Clock().now(),
    )
    repo.save_revision(changed)

    created = create(service, owner, group, changed.revision_id)

    assert "seventeen-year-old" not in created.prompt
    assert "being bathed" not in created.prompt
    assert "disturbing" not in created.prompt
    assert "receiving ceremonial cleansing" in created.prompt
    assert created.prompt_safety == (
        "age_marker_hyphenated",
        "body_ritual_wording",
        "sensational_wording",
    )


def test_create_records_shot_timing_projection_algorithm(tmp_path: Path) -> None:
    """2026-10-16：新生成以 monotone-projection-v1 投影区间，算法键随生成留档。"""
    repo, service, _, owner, group, revision = setup(tmp_path)
    generation = create(service, owner, group, revision.revision_id)
    assert generation.shot_time_ranges_algorithm == "monotone-projection-v1"
    ranges = generation.shot_time_ranges_snapshot
    assert [(value.shot_order, value.start_ms, value.end_ms) for value in ranges] == [(1, 0, 3000)]
    restored = repo.get_generation(owner, generation.generation_id)
    assert restored is not None
    assert restored.shot_time_ranges_algorithm == "monotone-projection-v1"
    assert restored.shot_time_ranges_snapshot == ranges


def test_create_blocks_timing_unresolvable_manual_draft(tmp_path: Path) -> None:
    """MANUAL 草稿链接形态无解（镜头 1={1,2,4}、镜头 2={3}）时以稳定码阻止生成，
    不自动合并（自动合并只在重映射物化阶段发生），由人工调整链接。"""
    repo, service, _, owner, group, revision = setup(tmp_path)
    sentences = tuple(
        DirectorSentenceSnapshot(uuid4(), index, f"line {index}", "2" * 64, start, start + 2800)
        for index, start in enumerate((0, 2800, 5600, 8400), start=1)
    )
    context = repo.context(owner, group)
    assert context is not None
    repo.add_context(
        replace(
            context,
            sentence_ids=tuple(item.sentence_id for item in sentences),
            sentences=sentences,
        )
    )
    first = DirectorShot(
        uuid4(), 1, "station hall", "wide", "eye", "arrives", "establish", "Arrival"
    )
    second = DirectorShot(
        uuid4(), 2, "market street", "close", "high", "departs", "resolve", "Departure"
    )
    unsolvable = DirectorPlanRevision(
        uuid4(),
        owner,
        revision.project_id,
        group,
        2,
        DirectorRevisionKind.APPLIED,
        (first, second),
        (
            ShotSentenceLink(first.shot_id, sentences[0].sentence_id),
            ShotSentenceLink(first.shot_id, sentences[1].sentence_id),
            ShotSentenceLink(second.shot_id, sentences[2].sentence_id),
            ShotSentenceLink(first.shot_id, sentences[3].sentence_id),
        ),
        tuple(item.sentence_id for item in sentences),
        ReviewState.NOT_REQUIRED,
        (),
        Clock().now(),
    )
    repo.save_revision(unsolvable)

    with pytest.raises(DomainError) as blocked:
        create(service, owner, group, unsolvable.revision_id, "unresolvable-key")
    assert blocked.value.code == "shot_timing_unresolvable"
