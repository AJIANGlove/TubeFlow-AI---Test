"""组内非相邻构图回切（USER_DECISIONS §48.3–§48.5）。

夹具：第 8 组（2c291aba）与第 36 组（23c59183）真实导演候选，均为“首镜与末镜同场景、同景别、
同机位”而被原规则拒绝的组；第 36 组受控语义恰好差 2 维（R1 边界）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_storyboard_batch import FakeMaterializations
from test_storyboard_generation import Clock, Ids, Provider, Storage, image_digest

from tubeflow_test.adapters.in_memory_storyboards import InMemoryStoryboardRepository
from tubeflow_test.adapters.storyboard_image_qc import PillowStoryboardTechnicalQc
from tubeflow_test.application.storyboard_batch import StoryboardBatchService
from tubeflow_test.application.storyboard_failover import (
    StoryboardFailoverPolicy,
    StoryboardFailoverStep,
)
from tubeflow_test.application.storyboard_prompt import build_grid_sheet_prompt
from tubeflow_test.application.storyboards import (
    AUTO_COMPOSITION_RECUT_REASON,
    COMPOSITION_RECUT_NOT_ALLOWED,
    STORYBOARD_PANEL_LAYOUT_UNREADABLE,
    STORYBOARD_RECUT_PANELS_TOO_SIMILAR,
    DirectorPlanService,
    ProviderPoll,
    StoryboardContext,
    StoryboardGenerationService,
)
from tubeflow_test.domain.director_generation import DirectorSentenceSnapshot
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorGroupCandidate,
    ProjectDirectorMaterialization,
    ProjectDirectorSourceShotLink,
)
from tubeflow_test.domain.storyboards import (
    GRID_LAYOUT_V1,
    GRID_LAYOUT_V2,
    CharacterAssetSnapshot,
    CompositionRecutApproval,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    GenerationStatus,
    ReviewState,
    ShotSentenceLink,
    ShotTimeRange,
    composition_recut_pairs,
    non_adjacent_composition_repeats,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GROUP08 = REPO_ROOT / "Pending issues/2026-11-04/evidence/autoresume_group08/group08_candidate.json"
GROUP36 = (
    REPO_ROOT
    / "Pending issues/2026-11-05/evidence/autoresume_group36/group36_candidate.before.json"
)
REPEAT_MESSAGE = "director plan repeats an event or composition across the group"
RECUT_MESSAGE = "composition recut does not meet the recut conditions"
THRESHOLD = 0.2


# --- 构造工具 -----------------------------------------------------------------------


def shot(
    order: int,
    scene: str,
    event: str,
    focus: str,
    time: str = "SAME_MOMENT",
    *,
    size: str = "MEDIUM",
    view: str = "EYE_LEVEL",
    action: str | None = None,
    semantics: bool = True,
) -> DirectorShot:
    controlled = (event, focus, time, "SAME_LOCATION") if semantics else ()
    return DirectorShot(
        uuid4(),
        order,
        scene,
        size,
        view,
        action or f"action {order}",
        f"function {order}",
        f"description {order}",
        *controlled,
    )


def revision(
    shots: tuple[DirectorShot, ...],
    approvals: tuple[CompositionRecutApproval, ...] = (),
    *,
    kind: DirectorRevisionKind = DirectorRevisionKind.DRAFT,
    owner: UUID | None = None,
    project: UUID | None = None,
    group: UUID | None = None,
    links: tuple[ShotSentenceLink, ...] | None = None,
    sentences: tuple[UUID, ...] | None = None,
) -> DirectorPlanRevision:
    sentence = uuid4()
    return DirectorPlanRevision(
        uuid4(),
        owner or uuid4(),
        project or uuid4(),
        group or uuid4(),
        1,
        kind,
        shots,
        links or tuple(ShotSentenceLink(item.shot_id, sentence) for item in shots),
        sentences or (sentence,),
        ReviewState.REQUIRED
        if any(b.changes_from(a) < 2 for a, b in zip(shots, shots[1:], strict=False))
        else ReviewState.NOT_REQUIRED,
        (),
        Clock().now(),
        composition_recut_approvals=approvals,
    )


def approve(first: DirectorShot, second: DirectorShot) -> CompositionRecutApproval:
    return CompositionRecutApproval(first.shot_id, second.shot_id, AUTO_COMPOSITION_RECUT_REASON)


def eligible_group() -> tuple[DirectorShot, ...]:
    """第 1、5 镜同构图（隔 3 镜），受控语义差 3 维；相邻镜头均差 ≥ 2 维。"""
    return (
        shot(1, "kitchen", "PERFORM_ACTION", "PRIMARY_SUBJECT", "LATER"),
        shot(2, "hall", "REVEAL_EVIDENCE", "OBJECT_OR_EVIDENCE"),
        shot(3, "yard", "EXPLAIN_CAUSE", "ENVIRONMENT", "LATER"),
        shot(4, "street", "SHOW_CONSEQUENCE", "PROCESS"),
        shot(5, "kitchen", "ESTABLISH_CONTEXT", "MULTIPLE_SUBJECTS"),
    )


def load_fixture(path: Path) -> tuple[tuple[DirectorShot, ...], tuple[ShotSentenceLink, ...]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    shots = tuple(
        DirectorShot(
            UUID(item["shot_id"]),
            item["order"],
            item["scene"],
            item["shot_size"],
            item["camera_viewpoint"],
            item["subject_action_outcome"],
            item["narrative_function"],
            item["description"],
            item["semantic_event"],
            item["subject_focus"],
            item["time_relation"],
            item["location_relation"],
        )
        for item in sorted(data["shots_json"], key=lambda value: value["order"])
    )
    links = tuple(
        ShotSentenceLink(UUID(item["shot_id"]), UUID(item["sentence_id"]))
        for item in data["links_json"]
    )
    return shots, links


# --- 领域：资格判定与修订校验 -------------------------------------------------------


def test_eligible_recut_with_approval_passes() -> None:
    shots = eligible_group()
    assert composition_recut_pairs(shots) == ((shots[0].shot_id, shots[4].shot_id),)
    value = revision(shots, (approve(shots[0], shots[4]),))
    assert len(value.composition_recut_approvals) == 1


def test_eligible_recut_without_approval_keeps_the_original_error() -> None:
    with pytest.raises(ValueError, match=REPEAT_MESSAGE):
        revision(eligible_group())


def test_groups_without_repeats_have_no_recut_pairs() -> None:
    shots = eligible_group()[:4]
    assert composition_recut_pairs(shots) == ()
    assert revision(shots).composition_recut_approvals == ()


def _one_semantic_change() -> tuple[DirectorShot, ...]:
    shots = list(eligible_group())
    shots[4] = shot(5, "kitchen", "PERFORM_ACTION", "MULTIPLE_SUBJECTS", "LATER")
    return tuple(shots)


def _missing_semantics() -> tuple[DirectorShot, ...]:
    shots = list(eligible_group())
    shots[4] = shot(5, "kitchen", "", "", semantics=False)
    return tuple(shots)


def _gap_of_two() -> tuple[DirectorShot, ...]:
    shots = list(eligible_group()[:4])
    shots[2] = shot(3, "kitchen", "EXPLAIN_CAUSE", "ENVIRONMENT", "LATER")
    return tuple(shots)


def _two_pairs() -> tuple[DirectorShot, ...]:
    return (
        shot(1, "kitchen", "PERFORM_ACTION", "PRIMARY_SUBJECT", "LATER"),
        shot(2, "hall", "REVEAL_EVIDENCE", "OBJECT_OR_EVIDENCE"),
        shot(3, "yard", "EXPLAIN_CAUSE", "ENVIRONMENT", "LATER"),
        shot(4, "kitchen", "ESTABLISH_CONTEXT", "MULTIPLE_SUBJECTS"),
        shot(5, "hall", "SHOW_CONSEQUENCE", "PROCESS", "LATER"),
        shot(6, "street", "EXPRESS_UNCERTAINTY", "ABSTRACT_CONCEPT"),
    )


def _three_occurrences() -> tuple[DirectorShot, ...]:
    shots = list(eligible_group())
    shots[1] = shot(2, "kitchen", "REVEAL_EVIDENCE", "OBJECT_OR_EVIDENCE")
    return tuple(shots)


@pytest.mark.parametrize(
    "build",
    [_one_semantic_change, _missing_semantics, _gap_of_two, _two_pairs, _three_occurrences],
    ids=["one-semantic-change", "missing-semantics", "gap-of-two", "two-pairs", "three-times"],
)
def test_ineligible_recuts_are_rejected_even_with_approvals(build) -> None:  # type: ignore[no-untyped-def]
    shots = build()
    assert composition_recut_pairs(shots) is None
    approvals = tuple(
        approve(first, second) for first, second in non_adjacent_composition_repeats(shots)
    )
    with pytest.raises(ValueError, match=f"{REPEAT_MESSAGE}|{RECUT_MESSAGE}"):
        revision(shots, approvals)
    with pytest.raises(ValueError, match=REPEAT_MESSAGE):
        revision(shots)


def test_three_occurrences_with_approvals_fail_the_recut_conditions() -> None:
    shots = _three_occurrences()
    # 第 1、2 镜相邻同构图（不是回切），第 2、5 镜为唯一的非相邻重复对。
    with pytest.raises(ValueError, match=RECUT_MESSAGE):
        revision(shots, (approve(shots[1], shots[4]),))


def test_approval_must_bind_a_real_non_adjacent_repeat() -> None:
    shots = eligible_group()
    with pytest.raises(ValueError, match="must bind a non-adjacent composition repeat"):
        revision(shots, (approve(shots[0], shots[1]),))
    with pytest.raises(ValueError, match="must bind a non-adjacent composition repeat"):
        revision(shots, (approve(shots[0], shots[4]), approve(shots[1], shots[3])))
    with pytest.raises(ValueError, match="must be unique"):
        revision(shots, (approve(shots[0], shots[4]), approve(shots[0], shots[4])))


def test_event_repeat_is_rejected_with_or_without_approval() -> None:
    shots = list(eligible_group())
    first = shots[0]
    shots[4] = replace(first, shot_id=uuid4(), order=5)
    group = tuple(shots)
    assert composition_recut_pairs(group) is None
    with pytest.raises(ValueError, match=REPEAT_MESSAGE):
        revision(group, (approve(group[0], group[4]),))
    with pytest.raises(ValueError, match=REPEAT_MESSAGE):
        revision(group)


@pytest.mark.parametrize(("path", "changes"), [(GROUP08, 3), (GROUP36, 2)], ids=["g08", "g36"])
def test_real_groups_qualify_for_one_recut(path: Path, changes: int) -> None:
    shots, links = load_fixture(path)
    pairs = composition_recut_pairs(shots)
    assert pairs == ((shots[0].shot_id, shots[-1].shot_id),)
    assert shots[-1].changes_from(shots[0]) == changes
    sentences = tuple(dict.fromkeys(link.sentence_id for link in links))
    with pytest.raises(ValueError, match=REPEAT_MESSAGE):
        revision(shots, links=links, sentences=sentences)
    accepted = revision(shots, (approve(shots[0], shots[-1]),), links=links, sentences=sentences)
    assert len(accepted.composition_recut_approvals) == 1


# --- 应用层：推导条件 ---------------------------------------------------------------


def _service(
    repo: InMemoryStoryboardRepository,
    tmp_path: Path,
    *,
    recut: bool = True,
    threshold: float | None = THRESHOLD,
    single_panel: bool = True,
    layout_check=None,  # type: ignore[no-untyped-def]
    similarity=None,  # type: ignore[no-untyped-def]
    provider: Provider | None = None,
) -> StoryboardGenerationService:
    return StoryboardGenerationService(
        repo,
        provider or Provider(),
        Storage(),
        PillowStoryboardTechnicalQc(),
        Ids(),
        Clock(),
        tmp_path,
        StoryboardFailoverPolicy(
            steps=(StoryboardFailoverStep("contract", "fake-v1", {"seed": 7}),)
        ),
        single_panel_new_projects=single_panel,
        panel_layout_check=layout_check,
        composition_recut=recut,
        recut_similarity_check=similarity,
        recut_min_distance=threshold,
    )


def _context_for(
    repo: InMemoryStoryboardRepository,
    links: tuple[ShotSentenceLink, ...],
    *,
    mode: str = "AUTO",
) -> tuple[UUID, UUID, UUID, tuple[UUID, ...]]:
    owner, project, group = uuid4(), uuid4(), uuid4()
    sentences = tuple(dict.fromkeys(link.sentence_id for link in links))
    payload = b"character"
    character = CharacterAssetSnapshot(
        1, uuid4(), uuid4(), hashlib.sha256(payload).hexdigest(), "image/png", len(payload)
    )
    repo.add_context(
        StoryboardContext(
            owner,
            project,
            group,
            sentences,
            True,
            (character,),
            "ink",
            sentences=tuple(
                DirectorSentenceSnapshot(
                    item, index, "line", "2" * 64, (index - 1) * 3000, index * 3000
                )
                for index, item in enumerate(sentences, 1)
            ),
            production_mode=mode,
        )
    )
    return owner, project, group, sentences


def _batch(
    repo: InMemoryStoryboardRepository,
    generations: StoryboardGenerationService,
    path: Path = GROUP08,
    *,
    mode: str = "AUTO",
    wire: bool = True,
) -> tuple[StoryboardBatchService, DirectorPlanService, UUID, UUID, UUID]:
    shots, links = load_fixture(path)
    owner, project, group, _sentences = _context_for(repo, links, mode=mode)
    director = DirectorPlanService(
        repo,
        Ids(),
        Clock(),
        composition_recut_allowed=generations.composition_recut_allowed if wire else None,
    )
    candidate = ProjectDirectorGroupCandidate(
        uuid4(),
        group,
        1,
        shots,
        links,
        tuple(ProjectDirectorSourceShotLink(uuid4(), item.shot_id) for item in shots),
        (),
        ReviewState.REQUIRED,
    )
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
        (candidate,),
        Clock().now(),
    )
    batch = StoryboardBatchService(
        repo, director, generations, FakeMaterializations(materialization), Ids()
    )
    return batch, director, owner, project, group


@pytest.mark.parametrize("path", [GROUP08, GROUP36], ids=["g08", "g36"])
def test_auto_v2_with_switch_on_seeds_the_group_with_one_recut(tmp_path: Path, path: Path) -> None:
    repo = InMemoryStoryboardRepository()
    batch, _director, owner, project, group = _batch(repo, _service(repo, tmp_path), path)
    (seeded,) = batch.seed_drafts(owner, project, auto_approve_ambiguous=True)
    assert seeded.outcome == "seeded", seeded.message
    draft = repo.get_draft_revision(owner, group)
    assert draft is not None and draft.may_apply
    assert len(draft.composition_recut_approvals) == 1
    approval = draft.composition_recut_approvals[0]
    assert (approval.previous_shot_id, approval.next_shot_id) == (
        draft.shots[0].shot_id,
        draft.shots[-1].shot_id,
    )
    assert approval.reason == AUTO_COMPOSITION_RECUT_REASON
    applied = repo.apply_revision(owner, group, draft.revision_id, draft.revision_id)
    assert applied.composition_recut_approvals == (approval,)


def test_put_and_auto_repair_paths_also_derive_the_recut(tmp_path: Path) -> None:
    repo = InMemoryStoryboardRepository()
    batch, director, owner, project, group = _batch(repo, _service(repo, tmp_path), GROUP36)
    # 不带系统审批播种：第 36 组第 1、2 镜只差 1 维 → REQUIRED 草稿，但回切已推导。
    (seeded,) = batch.seed_drafts(owner, project)
    assert seeded.review_state == ReviewState.REQUIRED.value
    draft = repo.get_draft_revision(owner, group)
    assert draft is not None and len(draft.composition_recut_approvals) == 1
    # AUTO 修复路径（repair_ambiguous_drafts → edit_draft）。
    (repaired,) = batch.repair_ambiguous_drafts(owner, project)
    assert repaired.outcome == "repaired"
    repaired_draft = repo.get_draft_revision(owner, group)
    assert repaired_draft is not None and repaired_draft.may_apply
    assert repaired_draft.composition_recut_approvals == draft.composition_recut_approvals
    # 第 07 步保存（PUT → edit_draft）：合同不带回切字段，由服务端重新推导。
    saved = director.edit_draft(
        owner_id=owner,
        group_id=group,
        shots=repaired_draft.shots,
        links=repaired_draft.sentence_links,
        expected_draft_revision_id=repaired_draft.revision_id,
        continuous_observation_approvals=repaired_draft.continuous_observation_approvals,
    )
    assert saved.composition_recut_approvals == draft.composition_recut_approvals


@pytest.mark.parametrize(
    "options",
    [
        {"recut": False},
        {"single_panel": False},  # 未钉版且单格开关关 → v1
        {"threshold": None},
    ],
    ids=["switch-off", "v1", "no-threshold"],
)
def test_recut_is_not_derived_unless_switch_threshold_and_v2(
    tmp_path: Path, options: dict[str, object]
) -> None:
    repo = InMemoryStoryboardRepository()
    service = _service(repo, tmp_path, **options)  # type: ignore[arg-type]
    batch, _director, owner, project, group = _batch(repo, service)
    (seeded,) = batch.seed_drafts(owner, project, auto_approve_ambiguous=True)
    assert seeded.outcome == "error"
    assert seeded.message == REPEAT_MESSAGE
    assert repo.get_draft_revision(owner, group) is None


def test_recut_is_not_derived_for_a_project_pinned_to_v1(tmp_path: Path) -> None:
    repo = InMemoryStoryboardRepository()
    service = _service(repo, tmp_path)
    batch, _director, owner, project, _group = _batch(repo, service)
    repo.project_grid_layout_version = lambda o, p: GRID_LAYOUT_V1  # type: ignore[method-assign]
    assert not service.composition_recut_allowed(owner, project)
    (seeded,) = batch.seed_drafts(owner, project, auto_approve_ambiguous=True)
    assert seeded.message == REPEAT_MESSAGE


@pytest.mark.parametrize(
    "options",
    [{"mode": "MANUAL"}, {"mode": ""}, {"wire": False}],
    ids=["manual", "unknown", "unwired"],
)
def test_manual_unknown_and_unwired_do_not_derive(
    tmp_path: Path, options: dict[str, object]
) -> None:
    repo = InMemoryStoryboardRepository()
    batch, _director, owner, project, _group = _batch(
        repo,
        _service(repo, tmp_path),
        **options,  # type: ignore[arg-type]
    )
    (seeded,) = batch.seed_drafts(owner, project, auto_approve_ambiguous=True)
    assert seeded.message == REPEAT_MESSAGE


# --- 提交前第二道闸 -----------------------------------------------------------------


def _applied_recut(
    repo: InMemoryStoryboardRepository, path: Path = GROUP08
) -> tuple[DirectorPlanRevision, UUID]:
    shots, links = load_fixture(path)
    owner, project, group, sentences = _context_for(repo, links)
    value = revision(
        shots,
        (approve(shots[0], shots[-1]),),
        kind=DirectorRevisionKind.APPLIED,
        owner=owner,
        project=project,
        group=group,
        links=links,
        sentences=sentences,
    )
    repo.save_revision(value)
    return value, owner


def _create(service: StoryboardGenerationService, value: DirectorPlanRevision, key: str = "key"):  # type: ignore[no-untyped-def]
    return service.create(
        group_id=value.group_id,
        owner_id=value.owner_id,
        expected_applied_revision_id=value.revision_id,
        idempotency_key=key,
    )


@pytest.mark.parametrize(
    "options", [{"recut": False}, {"single_panel": False}], ids=["switch-off", "v1"]
)
def test_create_fails_closed_when_recut_is_no_longer_allowed(
    tmp_path: Path, options: dict[str, object]
) -> None:
    repo = InMemoryStoryboardRepository()
    value, _owner = _applied_recut(repo)
    with pytest.raises(DomainError) as raised:
        _create(_service(repo, tmp_path, **options), value)  # type: ignore[arg-type]
    assert raised.value.code == COMPOSITION_RECUT_NOT_ALLOWED


def test_batch_maps_a_disallowed_recut_to_needs_review(tmp_path: Path) -> None:
    repo = InMemoryStoryboardRepository()
    allowed = _service(repo, tmp_path)
    batch, _director, owner, project, group = _batch(repo, allowed)
    (seeded,) = batch.seed_drafts(owner, project, auto_approve_ambiguous=True)
    assert seeded.outcome == "seeded"
    # 放行之后开关被关闭：批量生成不提交，交人工改构图（AUTO 据此以导演校验失败暂停）。
    batch._generations = _service(repo, tmp_path, recut=False)  # noqa: SLF001
    (result,) = batch.generate(owner, project, (group,), force=False)
    assert result.outcome == "needs_review"
    assert "第 07 步" in (result.message or "")
    assert repo.get_current_generation(owner, group) is None
    assert repo.get_applied_revision(owner, group) is None


# --- 提示词 ---------------------------------------------------------------------------


def test_prompt_adds_the_recut_sentence_only_for_recut_groups(tmp_path: Path) -> None:
    repo = InMemoryStoryboardRepository()
    value, _owner = _applied_recut(repo)
    generation = _create(_service(repo, tmp_path), value)
    assert generation.grid_layout_version == GRID_LAYOUT_V2
    sentence = (
        "Panel 6 returns to the same place and camera position as panel 1. Keep the place "
        "recognisable, but show only panel 6's own action and objects, so that panels 1 and 6 "
        "look clearly different from each other."
    )
    assert sentence in generation.prompt

    ranges = tuple(ShotTimeRange(i, (i - 1) * 1000, i * 1000) for i in range(1, 7))
    common = {
        "style": "ink",
        "aspect_ratio": "16:9",
        "shots": value.shots,
        "ranges": ranges,
        "character_count": 1,
        "layout_version": GRID_LAYOUT_V2,
    }
    without = build_grid_sheet_prompt(**common)  # type: ignore[arg-type]
    assert build_grid_sheet_prompt(**common, recut_pairs=()) == without  # type: ignore[arg-type]
    assert "returns to the same place" not in without
    assert build_grid_sheet_prompt(**common, recut_pairs=((1, 6),)) == f"{without} {sentence}"  # type: ignore[arg-type]


# --- 回切两格相似度质检（§48.5） -----------------------------------------------------


def _poll() -> ProviderPoll:
    return ProviderPoll(GenerationStatus.SUCCEEDED, "ok", "token", image_digest())


def _run_to_qc(
    service: StoryboardGenerationService, provider: Provider, value: DirectorPlanRevision
):  # type: ignore[no-untyped-def]
    first = _create(service, value)
    service.resume(first.generation_id, value.owner_id)
    provider.polls = [_poll()]
    return first, service.resume(first.generation_id, value.owner_id)


def _noop_layout(path: Path, **_kwargs: object) -> None:
    return None


def test_recut_panels_that_look_alike_are_regenerated(tmp_path: Path) -> None:
    repo, provider = InMemoryStoryboardRepository(), Provider()
    value, owner = _applied_recut(repo)
    calls: list[tuple[tuple[int, int], ...]] = []

    def similarity(path: Path, *, pairs, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(pairs)
        return (THRESHOLD / 2,)

    service = _service(
        repo, tmp_path, layout_check=_noop_layout, similarity=similarity, provider=provider
    )
    first, failed = _run_to_qc(service, provider, value)
    assert calls == [((1, 6),)]
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "download_or_qc"
    assert failed.failure_code == STORYBOARD_RECUT_PANELS_TOO_SIMILAR
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None and successor.prompt == first.prompt


def test_recut_panels_that_differ_are_accepted(tmp_path: Path) -> None:
    repo, provider = InMemoryStoryboardRepository(), Provider()
    value, _owner = _applied_recut(repo)
    service = _service(
        repo,
        tmp_path,
        layout_check=_noop_layout,
        similarity=lambda path, **_kwargs: (THRESHOLD * 3,),
        provider=provider,
    )
    _first, done = _run_to_qc(service, provider, value)
    assert done.status is not GenerationStatus.FAILED
    assert done.failure_code is None


def test_recut_check_that_cannot_crop_is_treated_as_unreadable_layout(tmp_path: Path) -> None:
    repo, provider = InMemoryStoryboardRepository(), Provider()
    value, _owner = _applied_recut(repo)

    def similarity(path: Path, **_kwargs: object) -> tuple[float, ...]:
        raise ValueError("shot_panel_crop_unsafe")

    service = _service(
        repo, tmp_path, layout_check=_noop_layout, similarity=similarity, provider=provider
    )
    _first, failed = _run_to_qc(service, provider, value)
    assert failed.failure_code == STORYBOARD_PANEL_LAYOUT_UNREADABLE


def test_recut_check_fails_closed_when_not_configured(tmp_path: Path) -> None:
    repo, provider = InMemoryStoryboardRepository(), Provider()
    value, _owner = _applied_recut(repo)
    service = _service(repo, tmp_path, layout_check=_noop_layout, provider=provider)
    first = _create(service, value)
    service.resume(first.generation_id, value.owner_id)
    provider.polls = [_poll()]
    with pytest.raises(DomainError) as raised:
        service.resume(first.generation_id, value.owner_id)
    assert raised.value.code == "storyboard_recut_check_not_configured"


def test_groups_without_recut_are_never_similarity_checked(tmp_path: Path) -> None:
    repo, provider = InMemoryStoryboardRepository(), Provider()
    shots, links = load_fixture(GROUP08)
    shots = (*shots[:-1], replace(shots[-1], camera_viewpoint="EYE_LEVEL"))
    owner, project, group, sentences = _context_for(repo, links)
    value = revision(
        shots,
        kind=DirectorRevisionKind.APPLIED,
        owner=owner,
        project=project,
        group=group,
        links=links,
        sentences=sentences,
    )
    repo.save_revision(value)

    def similarity(path: Path, **_kwargs: object) -> tuple[float, ...]:
        raise AssertionError("groups without a recut must not be similarity-checked")

    service = _service(
        repo, tmp_path, layout_check=_noop_layout, similarity=similarity, provider=provider
    )
    _first, done = _run_to_qc(service, provider, value)
    assert done.failure_code is None
    assert "returns to the same place" not in done.prompt


# --- 持久化（SQL 往返、应用、旧行） ------------------------------------------------


def test_sql_round_trip_apply_and_legacy_rows(tmp_path: Path) -> None:
    from sqlalchemy import text
    from test_storyboard_sql_repository import repositories

    from tubeflow_test.adapters.persistence.storyboards.models import DirectorRevisionRow
    from tubeflow_test.adapters.persistence.storyboards.repository import _revision

    first, _second, sessions = repositories(tmp_path / "recut.sqlite")
    owner, project, group = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state) "
                "VALUES (:id, :owner, 'ACTIVE')"
            ),
            {"id": project.hex, "owner": owner.hex},
        )
    shots = eligible_group()
    approval = approve(shots[0], shots[4])
    draft = revision(shots, (approval,), owner=owner, project=project, group=group)

    stored = first.create_revision(draft, None)
    loaded = first.get_draft_revision(owner, group)
    assert loaded is not None and loaded.composition_recut_approvals == (approval,)
    applied = first.apply_revision(owner, group, stored.revision_id, stored.revision_id)
    assert applied.composition_recut_approvals == (approval,)
    again = first.get_applied_revision(owner, group)
    assert again is not None and again.composition_recut_approvals == (approval,)

    # 迁移 0047 之前写入的行：列取服务端默认 []，读出为空元组、校验与改动前一致。
    with sessions.begin() as session:
        session.execute(
            text(
                "UPDATE storyboard_director_revisions "
                "SET composition_recut_approvals_json = '[]' WHERE id = :id"
            ),
            {"id": stored.revision_id.hex},
        )
    with pytest.raises(ValueError, match=REPEAT_MESSAGE):
        first.get_applied_revision(owner, group)
    legacy_group, legacy_id = uuid4(), uuid4()
    legacy = revision(eligible_group()[:4], owner=owner, project=project, group=legacy_group)
    first.create_revision(legacy, None)
    columns = (
        "owner_id, project_id, group_id, revision_number, kind, shots_json, links_json, "
        "group_sentence_ids_json, review_state, continuous_observation_approvals_json, created_at"
    )
    with sessions.begin() as session:
        # 不写新列的插入（迁移前的写法）：取服务端默认值。
        session.execute(
            text(
                f"INSERT INTO storyboard_director_revisions (id, {columns}) "
                f"SELECT :id, {columns.replace('revision_number', 'revision_number + 1')} "
                "FROM storyboard_director_revisions WHERE group_id = :group"
            ),
            {"id": legacy_id.hex, "group": legacy_group.hex},
        )
        row = session.get(DirectorRevisionRow, legacy_id)
        assert row is not None
        loaded_legacy = _revision(row)
    assert loaded_legacy.composition_recut_approvals == ()


# --- 配置 ---------------------------------------------------------------------------


def test_switch_defaults_off_and_requires_a_confirmed_threshold() -> None:
    from pydantic import ValidationError

    from tubeflow_test.config import Settings

    settings = Settings(_env_file=None)
    assert settings.director_composition_recut is False
    assert settings.director_composition_recut_min_distance is None
    with pytest.raises(ValidationError, match="confirmed minimum panel distance"):
        Settings(_env_file=None, director_composition_recut=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, director_composition_recut_min_distance=0)
    enabled = Settings(
        _env_file=None, director_composition_recut=True, director_composition_recut_min_distance=0.1
    )
    assert enabled.director_composition_recut_min_distance == 0.1
