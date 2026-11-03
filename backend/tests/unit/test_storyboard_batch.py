from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_storyboard_generation import (
    Clock,
    Ids,
    Provider,
    Storage,
    image_digest,
)

from tubeflow_test.adapters.in_memory_storyboards import InMemoryStoryboardRepository
from tubeflow_test.adapters.storyboard_image_qc import PillowStoryboardTechnicalQc
from tubeflow_test.api.storyboard_batch import create_storyboard_batch_router
from tubeflow_test.application.storyboard_batch import StoryboardBatchService
from tubeflow_test.application.storyboard_failover import (
    StoryboardFailoverPolicy,
    StoryboardFailoverStep,
)
from tubeflow_test.application.storyboards import (
    DirectorPlanService,
    ProviderPoll,
    StoryboardContext,
    StoryboardGenerationService,
)
from tubeflow_test.domain.director_generation import DirectorSentenceSnapshot
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorGroupCandidate,
    ProjectDirectorMaterialization,
    ProjectDirectorSourceShotLink,
)
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    DirectorLocationRelation,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
    GenerationStatus,
    ReviewState,
    ShotSentenceLink,
)
from tubeflow_test.identity_project.domain import Account, AccountRole


class FakeMaterializations:
    def __init__(self, value: ProjectDirectorMaterialization) -> None:
        self.value = value

    def current_materialization(
        self, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorMaterialization:
        return self.value


class UnknownSubmit(Provider):
    def submit_grid_sheet(self, generation, references=()):  # type: ignore[no-untyped-def]
        raise TimeoutError


def current() -> Account:
    return Account(uuid4(), "user", "hash", AccountRole.USER)


def shot(order: int, scene: str, action: str, function: str) -> DirectorShot:
    return DirectorShot(uuid4(), order, scene, "wide", "eye", action, function, f"Shot {order}")


def semantic_shot(
    order: int,
    event: str,
    focus: str,
    time: str,
    location: str,
    *,
    scene: str = "cave",
    action: str = "enters",
    function: str = "establish",
) -> DirectorShot:
    return DirectorShot(
        uuid4(),
        order,
        scene,
        "wide",
        "eye",
        action,
        function,
        f"Shot {order}",
        DirectorSemanticEvent(event),
        DirectorSubjectFocus(focus),
        DirectorTimeRelation(time),
        DirectorLocationRelation(location),
    )


def make_candidate(
    group: UUID, sequence: int, sentence: UUID, shots: tuple[DirectorShot, ...]
) -> ProjectDirectorGroupCandidate:
    links = tuple(ShotSentenceLink(item.shot_id, sentence) for item in shots)
    lineage = tuple(ProjectDirectorSourceShotLink(uuid4(), item.shot_id) for item in shots)
    return ProjectDirectorGroupCandidate(
        uuid4(), group, sequence, shots, links, lineage, (), ReviewState.REQUIRED
    )


def ambiguous_pair() -> tuple[DirectorShot, DirectorShot]:
    return (shot(1, "cave", "enters", "establish"), shot(2, "cave", "enters", "resolve"))


def setup(
    tmp_path: Path,
    specs: list[tuple[UUID, int, UUID, tuple[DirectorShot, ...]]],
    *,
    orphan_specs: list[tuple[UUID, int, UUID, tuple[DirectorShot, ...]]] | None = None,
):
    owner, project = uuid4(), uuid4()
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
    candidates = []
    for group, sequence, sentence, shots in specs:
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
        candidates.append(make_candidate(group, sequence, sentence, shots))
    for group, sequence, sentence, shots in orphan_specs or []:
        candidates.append(make_candidate(group, sequence, sentence, shots))
    materialization = ProjectDirectorMaterialization(
        uuid4(),
        owner,
        project,
        uuid4(),
        "a" * 64,
        1,
        "test-remap",
        "b" * 64,
        MaterializationStatus.REVIEW_REQUIRED,
        tuple(candidates),
        datetime(2026, 8, 28, tzinfo=UTC),
    )
    provider, storage = Provider(), Storage()
    generations = StoryboardGenerationService(
        repo,
        provider,
        storage,
        PillowStoryboardTechnicalQc(),
        Ids(),
        Clock(),
        tmp_path,
        StoryboardFailoverPolicy(steps=(StoryboardFailoverStep("contract", "fake-v1", {}),)),
    )
    director = DirectorPlanService(repo, Ids(), Clock())
    batch = StoryboardBatchService(
        repo, director, generations, FakeMaterializations(materialization), Ids()
    )
    return repo, batch, generations, provider, owner, project


def apply_current_draft(repo: InMemoryStoryboardRepository, owner: UUID, group: UUID) -> UUID:
    draft = repo.get_draft_revision(owner, group)
    assert draft is not None
    applied = repo.apply_revision(owner, group, draft.revision_id, draft.revision_id)
    return applied.revision_id


def succeed_current_generation(
    generations: StoryboardGenerationService, provider: Provider, owner: UUID, generation_id: UUID
) -> None:
    generations.resume(generation_id, owner)
    provider.polls = [ProviderPoll(GenerationStatus.SUCCEEDED, "ok", "token", image_digest())]
    generations.resume(generation_id, owner)


def test_seed_drafts_reports_states_and_preserves_controlled_semantics(tmp_path: Path) -> None:
    g1, g2, g3 = uuid4(), uuid4(), uuid4()
    s1, s2, s3 = uuid4(), uuid4(), uuid4()
    semantic = semantic_shot(1, "ESTABLISH_CONTEXT", "ENVIRONMENT", "TIMELESS", "NON_SPATIAL")
    repo, batch, _, _, owner, project = setup(
        tmp_path,
        [
            (g1, 1, s1, (semantic,)),
            (g2, 2, s2, ambiguous_pair()),
            (g3, 3, s3, (shot(1, "storm", "hides", "establish"),)),
        ],
    )
    existing_shot = shot(1, "storm", "hides", "establish")
    existing = DirectorPlanRevision(
        uuid4(),
        owner,
        project,
        g3,
        1,
        DirectorRevisionKind.DRAFT,
        (existing_shot,),
        (ShotSentenceLink(existing_shot.shot_id, s3),),
        (s3,),
        ReviewState.NOT_REQUIRED,
        (),
        Clock().now(),
    )
    repo.save_revision(existing)

    results = {item.group_sequence: item for item in batch.seed_drafts(owner, project)}

    assert results[1].outcome == "seeded" and results[1].review_state == "NOT_REQUIRED"
    assert results[2].outcome == "seeded" and results[2].review_state == "REQUIRED"
    assert results[3].outcome == "already_draft"
    seeded = repo.get_draft_revision(owner, g1)
    assert seeded is not None
    # §20 统一评判基准：候选的受控语义随草稿保留，不再剥离为 UNSPECIFIED。
    assert seeded.shots[0].semantic_event is DirectorSemanticEvent.ESTABLISH_CONTEXT
    assert seeded.shots[0].subject_focus is DirectorSubjectFocus.ENVIRONMENT
    assert seeded.shots[0].time_relation is DirectorTimeRelation.TIMELESS
    assert seeded.shots[0].location_relation is DirectorLocationRelation.NON_SPATIAL
    assert repo.get_draft_revision(owner, g3) == existing


def test_seed_drafts_semantic_diversity_passes_despite_similar_text(tmp_path: Path) -> None:
    """§20：语义轴不同但文本近似的相邻镜头不再误判 REQUIRED（统一尺子）。"""
    g1, s1 = uuid4(), uuid4()
    shots = (
        semantic_shot(1, "ESTABLISH_CONTEXT", "ENVIRONMENT", "TIMELESS", "NON_SPATIAL"),
        semantic_shot(2, "PERFORM_ACTION", "PRIMARY_SUBJECT", "LATER", "SAME_LOCATION"),
    )
    repo, batch, _, _, owner, project = setup(tmp_path, [(g1, 1, s1, shots)])

    (result,) = batch.seed_drafts(owner, project)

    assert result.outcome == "seeded" and result.review_state == "NOT_REQUIRED"
    draft = repo.get_draft_revision(owner, g1)
    assert draft is not None and draft.review_state is ReviewState.NOT_REQUIRED


def test_seed_drafts_auto_approve_covers_ambiguous_pairs(tmp_path: Path) -> None:
    """§20：AUTO 播种为歧义相邻对附加系统审批并落 APPROVED；默认路径不附加。"""
    g1, s1 = uuid4(), uuid4()
    repo, batch, _, _, owner, project = setup(tmp_path, [(g1, 1, s1, ambiguous_pair())])

    (result,) = batch.seed_drafts(owner, project, auto_approve_ambiguous=True)

    assert result.outcome == "seeded" and result.review_state == "APPROVED"
    draft = repo.get_draft_revision(owner, g1)
    assert draft is not None and draft.review_state is ReviewState.APPROVED
    assert len(draft.continuous_observation_approvals) == 1
    approval = draft.continuous_observation_approvals[0]
    assert (approval.previous_shot_id, approval.next_shot_id) == (
        draft.shots[0].shot_id,
        draft.shots[1].shot_id,
    )
    assert "系统审批" in approval.reason and "非人工批准" in approval.reason


def test_repair_ambiguous_drafts_supersedes_required(tmp_path: Path) -> None:
    """§20 存量修复：既有 REQUIRED 草稿被系统审批修订取代；未达标组不受影响。"""
    g1, g2, s1, s2 = uuid4(), uuid4(), uuid4(), uuid4()
    repo, batch, _, _, owner, project = setup(
        tmp_path,
        [
            (g1, 1, s1, ambiguous_pair()),
            (g2, 2, s2, (shot(1, "storm", "hides", "establish"),)),
        ],
    )
    batch.seed_drafts(owner, project)
    required = repo.get_draft_revision(owner, g1)
    assert required is not None and required.review_state is ReviewState.REQUIRED

    results = {item.group_sequence: item for item in batch.repair_ambiguous_drafts(owner, project)}

    assert results[1].outcome == "repaired" and results[1].review_state == "APPROVED"
    assert 2 not in results  # 未 REQUIRED 的组不触碰
    repaired = repo.get_draft_revision(owner, g1)
    assert repaired is not None and repaired.revision_id != required.revision_id
    assert repaired.review_state is ReviewState.APPROVED
    assert len(repaired.continuous_observation_approvals) == 1
    # 旧修订不可变保留。
    assert required.review_state is ReviewState.REQUIRED


def test_seed_drafts_isolates_group_errors(tmp_path: Path) -> None:
    g1, g2 = uuid4(), uuid4()
    s1, s2 = uuid4(), uuid4()
    repo, batch, _, _, owner, project = setup(
        tmp_path,
        [(g1, 1, s1, (shot(1, "cave", "runs in", "establish"),))],
        orphan_specs=[(g2, 2, s2, (shot(1, "storm", "hides", "establish"),))],
    )

    results = {item.group_sequence: item for item in batch.seed_drafts(owner, project)}

    assert results[1].outcome == "seeded"
    assert results[2].outcome == "error"
    assert repo.get_draft_revision(owner, g2) is None


def test_overview_reflects_draft_applied_and_storyboard_state(tmp_path: Path) -> None:
    g1, s1 = uuid4(), uuid4()
    repo, batch, generations, provider, owner, project = setup(
        tmp_path, [(g1, 1, s1, (shot(1, "cave", "runs in", "establish"),))]
    )
    batch.seed_drafts(owner, project)
    created = generations.create(
        group_id=g1,
        owner_id=owner,
        expected_applied_revision_id=apply_current_draft(repo, owner, g1),
        idempotency_key="k1",
    )
    succeed_current_generation(generations, provider, owner, created.generation_id)

    (item,) = batch.overview(owner, project)

    assert item.group_sequence == 1
    assert item.has_applied is True
    assert item.storyboard_status == "SUCCEEDED" and item.storyboard_version == 1
    assert item.current_generation_id == created.generation_id
    assert item.can_generate is True


def test_overview_reports_null_current_generation_id_without_current_pointer(
    tmp_path: Path,
) -> None:
    g1, g2 = uuid4(), uuid4()
    s1, s2 = uuid4(), uuid4()
    repo, batch, generations, _, owner, project = setup(
        tmp_path,
        [
            (g1, 1, s1, (shot(1, "cave", "runs in", "establish"),)),
            (g2, 2, s2, (shot(1, "storm", "hides", "establish"),)),
        ],
    )
    batch.seed_drafts(owner, project)
    created = generations.create(
        group_id=g1,
        owner_id=owner,
        expected_applied_revision_id=apply_current_draft(repo, owner, g1),
        idempotency_key="k1",
    )
    generations.resume(created.generation_id, owner)

    items = {item.group_sequence: item for item in batch.overview(owner, project)}

    # g1 is mid-generation: no current pointer exists yet, so the id stays null.
    assert items[1].storyboard_status == "SUBMITTED"
    assert items[1].current_generation_id is None
    # g2 has no generation history at all.
    assert items[2].storyboard_status is None
    assert items[2].current_generation_id is None


def test_generate_applies_draft_and_queues(tmp_path: Path) -> None:
    g1, s1 = uuid4(), uuid4()
    repo, batch, _, _, owner, project = setup(
        tmp_path, [(g1, 1, s1, (shot(1, "cave", "runs in", "establish"),))]
    )
    batch.seed_drafts(owner, project)

    (result,) = batch.generate(owner, project, (g1,), force=False)

    assert result.outcome == "queued" and result.generation_id is not None
    generation = repo.get_generation(owner, result.generation_id)
    assert generation is not None
    assert generation.status is GenerationStatus.INTENT_PERSISTED
    assert generation.idempotency_key.startswith("batch-")
    assert repo.get_applied_revision(owner, g1) is not None


def test_generate_skips_succeeded_without_force_and_regenerates_with_force(tmp_path: Path) -> None:
    g1, s1 = uuid4(), uuid4()
    repo, batch, generations, provider, owner, project = setup(
        tmp_path, [(g1, 1, s1, (shot(1, "cave", "runs in", "establish"),))]
    )
    batch.seed_drafts(owner, project)
    (first,) = batch.generate(owner, project, (g1,), force=False)
    assert first.generation_id is not None
    succeed_current_generation(generations, provider, owner, first.generation_id)

    (skipped,) = batch.generate(owner, project, (g1,), force=False)
    assert skipped.outcome == "skipped_succeeded"

    (regenerated,) = batch.generate(owner, project, (g1,), force=True)
    assert regenerated.outcome == "queued" and regenerated.generation_id is not None
    regenerated_generation = repo.get_generation(owner, regenerated.generation_id)
    assert regenerated_generation is not None
    assert regenerated_generation.version == 2


def test_generate_reports_needs_review_no_director_and_already_running(tmp_path: Path) -> None:
    g1, g2, g3 = uuid4(), uuid4(), uuid4()
    s1, s2, s3 = uuid4(), uuid4(), uuid4()
    repo, batch, _, _, owner, project = setup(
        tmp_path,
        [
            (g1, 1, s1, ambiguous_pair()),
            (g2, 2, s2, (shot(1, "storm", "hides", "establish"),)),
            (g3, 3, s3, (shot(1, "plain", "waits", "establish"),)),
        ],
    )
    batch.seed_drafts(owner, project)
    (running,) = batch.generate(owner, project, (g2,), force=False)
    assert running.outcome == "queued"

    generated = batch.generate(owner, project, (g1, g2, g3), force=False)
    results = {item.group_sequence: item for item in generated}

    assert results[1].outcome == "needs_review"
    assert results[2].outcome == "already_running"
    assert results[3].outcome == "queued"


def test_generate_marks_unresolved_remote_state(tmp_path: Path) -> None:
    g1, s1 = uuid4(), uuid4()
    _, batch, generations, _, owner, project = setup(
        tmp_path, [(g1, 1, s1, (shot(1, "cave", "runs in", "establish"),))]
    )
    batch.seed_drafts(owner, project)
    (queued,) = batch.generate(owner, project, (g1,), force=False)
    assert queued.generation_id is not None
    generations._provider = UnknownSubmit()  # noqa: SLF001
    stuck = generations.resume(queued.generation_id, owner)
    assert stuck.status is GenerationStatus.REMOTE_STATE_UNKNOWN

    (result,) = batch.generate(owner, project, (g1,), force=True)

    assert result.outcome == "unresolved"


def test_batch_router_contract_and_strict_body() -> None:
    app = FastAPI()
    app.include_router(
        create_storyboard_batch_router(cast(StoryboardBatchService, object()), current)
    )
    schema = app.openapi()
    paths = set(schema["paths"])
    assert "/projects/{project_id}/storyboard-batch/overview" in paths
    assert "/projects/{project_id}/storyboard-batch/director-drafts" in paths
    assert "/projects/{project_id}/storyboard-batch/grid-sheet-generations" in paths
    body_schema = schema["components"]["schemas"]["BatchGenerateInput"]
    assert body_schema["additionalProperties"] is False
    assert set(body_schema["properties"]) == {"group_ids", "force"}

    response = TestClient(app).post(
        f"/projects/{uuid4()}/storyboard-batch/grid-sheet-generations",
        json={"group_ids": [str(uuid4())], "force": False, "bulk": True},
    )
    assert response.status_code == 422
