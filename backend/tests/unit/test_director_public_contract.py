from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.adapters.in_memory_storyboards import InMemoryStoryboardRepository
from tubeflow_test.api.storyboards import (
    _storyboard_generation_public_state,
    create_storyboard_router,
)
from tubeflow_test.application.storyboards import (
    DirectorPlanService,
    StoryboardContext,
    StoryboardGenerationService,
)
from tubeflow_test.domain.storyboards import (
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


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 29, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StoryboardAttempt:
    generation_id: UUID
    kind: str
    status: GenerationStatus
    version: int


class StoryboardGenerations:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.expected_revision_ids: list[UUID] = []

    def create(
        self,
        *,
        group_id: UUID,
        owner_id: UUID,
        expected_applied_revision_id: UUID,
        idempotency_key: str,
    ) -> StoryboardAttempt:
        del group_id, owner_id, idempotency_key
        self.expected_revision_ids.append(expected_applied_revision_id)
        if self.fail_once:
            self.fail_once = False
            raise ValueError("injected create failure")
        return StoryboardAttempt(
            uuid4(),
            "GRID_SHEET",
            GenerationStatus.INTENT_PERSISTED,
            1,
        )


def _client(
    storyboard_generations: StoryboardGenerations | None = None,
) -> tuple[TestClient, UUID, UUID]:
    owner = Account(uuid4(), "director-user", "hash", AccountRole.USER)
    group_id, sentence_id = uuid4(), uuid4()
    repository = InMemoryStoryboardRepository()
    repository.add_context(
        StoryboardContext(
            owner.id,
            uuid4(),
            group_id,
            (sentence_id,),
            True,
            (),
            "ink",
        )
    )
    app = FastAPI()
    app.include_router(
        create_storyboard_router(
            DirectorPlanService(repository, Ids(), Clock()),
            cast(StoryboardGenerationService, storyboard_generations or object()),
            lambda: owner,
        )
    )
    return TestClient(app), group_id, sentence_id


def _shot(sentence_id: UUID, *, order: int = 1) -> dict[str, object]:
    return {
        "shot_id": str(uuid4()),
        "order": order,
        "scene": "room",
        "shot_size": "wide",
        "camera_viewpoint": "eye-level",
        "subject_action_outcome": "the light turns on",
        "narrative_function": "reveal",
        "description": f"Shot {order}",
        "sentence_ids": [str(sentence_id)],
    }


class StubAutoPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, str]] = []

    def resume_if_paused_for(self, owner_id: UUID, project_id: UUID, *, reason_code: str) -> bool:
        self.calls.append((owner_id, project_id, reason_code))
        return True


def _client_with_repo() -> tuple[
    TestClient, InMemoryStoryboardRepository, UUID, UUID, UUID, Account
]:
    owner = Account(uuid4(), "director-user", "hash", AccountRole.USER)
    project_id, group_id, sentence_id = uuid4(), uuid4(), uuid4()
    repository = InMemoryStoryboardRepository()
    repository.add_context(
        StoryboardContext(
            owner.id,
            project_id,
            group_id,
            (sentence_id,),
            True,
            (),
            "ink",
        )
    )
    app = FastAPI()
    app.include_router(
        create_storyboard_router(
            DirectorPlanService(repository, Ids(), Clock()),
            cast(StoryboardGenerationService, object()),
            lambda: owner,
        )
    )
    return TestClient(app), repository, project_id, group_id, sentence_id, owner


def _semantic_draft_shot(sentence_id: UUID) -> DirectorShot:
    return DirectorShot(
        uuid4(),
        1,
        "room",
        "wide",
        "eye-level",
        "the light turns on",
        "reveal",
        "Shot 1",
        DirectorSemanticEvent.ESTABLISH_CONTEXT,
        DirectorSubjectFocus.PRIMARY_SUBJECT,
        DirectorTimeRelation.LATER,
        DirectorLocationRelation.NON_SPATIAL,
    )


def test_put_inherits_controlled_semantics_and_get_exposes_them() -> None:
    """§20：PUT 合同不含语义字段；服务端按 shot_id 从当前草稿继承，GET 回显。"""
    client, repository, project_id, group_id, sentence_id, owner = _client_with_repo()
    existing_shot = _semantic_draft_shot(sentence_id)
    repository.save_revision(
        DirectorPlanRevision(
            uuid4(),
            owner.id,
            project_id,
            group_id,
            1,
            DirectorRevisionKind.DRAFT,
            (existing_shot,),
            (ShotSentenceLink(existing_shot.shot_id, sentence_id),),
            (sentence_id,),
            ReviewState.NOT_REQUIRED,
            (),
            Clock().now(),
        )
    )

    current = client.get(f"/storyboard-groups/{group_id}/director/draft")
    assert current.status_code == 200
    current_shot = current.json()["shots"][0]
    assert current_shot["semantic_event"] == "ESTABLISH_CONTEXT"
    assert current_shot["subject_focus"] == "PRIMARY_SUBJECT"
    assert current_shot["time_relation"] == "LATER"
    assert current_shot["location_relation"] == "NON_SPATIAL"

    edited = _shot(sentence_id)
    edited["shot_id"] = str(existing_shot.shot_id)
    edited["scene"] = "edited room"
    saved = client.put(
        f"/storyboard-groups/{group_id}/director/draft",
        json={
            "shots": [edited],
            "expected_draft_revision_id": current.json()["revision_id"],
            "continuous_observation_approvals": [],
        },
    )
    assert saved.status_code == 200

    after = client.get(f"/storyboard-groups/{group_id}/director/draft").json()["shots"][0]
    assert after["scene"] == "edited room"
    assert after["semantic_event"] == "ESTABLISH_CONTEXT"
    assert after["location_relation"] == "NON_SPATIAL"


def test_put_new_shot_without_semantics_stays_unspecified() -> None:
    """全新镜头没有可继承来源：语义保持 UNSPECIFIED（混合对回退文本轴比较）。"""
    client, _repository, _project_id, group_id, sentence_id, _owner = _client_with_repo()
    client.put(
        f"/storyboard-groups/{group_id}/director/draft",
        json={
            "shots": [_shot(sentence_id)],
            "expected_draft_revision_id": None,
            "continuous_observation_approvals": [],
        },
    )
    shot = client.get(f"/storyboard-groups/{group_id}/director/draft").json()["shots"][0]
    assert shot["semantic_event"] == "UNSPECIFIED"
    assert shot["subject_focus"] == "UNSPECIFIED"


def test_save_draft_auto_resumes_only_when_may_apply() -> None:
    """§20 保存即续跑：可应用草稿触发原因码守卫恢复；REQUIRED 草稿不触发。"""
    client, _repository, project_id, group_id, sentence_id, owner = _client_with_repo()
    pipeline = StubAutoPipeline()
    client.app.state.auto_pipeline_service = pipeline  # type: ignore[attr-defined]

    saved = client.put(
        f"/storyboard-groups/{group_id}/director/draft",
        json={
            "shots": [_shot(sentence_id)],
            "expected_draft_revision_id": None,
            "continuous_observation_approvals": [],
        },
    )
    assert saved.status_code == 200
    assert pipeline.calls == [(owner.id, project_id, "director_validation_failed")]

    ambiguous = client.put(
        f"/storyboard-groups/{group_id}/director/draft",
        json={
            "shots": [_shot(sentence_id, order=1), _shot(sentence_id, order=2)],
            "expected_draft_revision_id": saved.json()["revision_id"],
            "continuous_observation_approvals": [],
        },
    )
    assert ambiguous.status_code == 200
    assert ambiguous.json()["review_state"] == "REQUIRED"
    assert len(pipeline.calls) == 1  # 未再触发


def test_draft_edit_get_and_apply_return_gate_aware_public_actions() -> None:
    client, group_id, sentence_id = _client()
    created = client.put(
        f"/storyboard-groups/{group_id}/director/draft",
        json={
            "shots": [_shot(sentence_id)],
            "expected_draft_revision_id": None,
            "continuous_observation_approvals": [],
        },
    )

    assert created.status_code == 200
    created_payload = created.json()
    assert created_payload["kind"] == "DRAFT"
    assert created_payload["public_status"] == "SUCCEEDED"
    assert created_payload["allowed_actions"] == [
        "EDIT_DIRECTOR_DRAFT",
        "GENERATE_STORYBOARD",
    ]

    current = client.get(f"/storyboard-groups/{group_id}/director/draft")
    assert current.status_code == 200
    assert current.json()["allowed_actions"] == [
        "EDIT_DIRECTOR_DRAFT",
        "GENERATE_STORYBOARD",
    ]

    applied = client.post(
        f"/storyboard-groups/{group_id}/director/apply",
        json={"expected_draft_revision_id": created_payload["revision_id"]},
    )
    assert applied.status_code == 200
    assert applied.json()["kind"] == "APPLIED"
    assert applied.json()["public_status"] == "SUCCEEDED"
    assert applied.json()["allowed_actions"] == ["GENERATE_STORYBOARD"]

    current_applied = client.get(f"/storyboard-groups/{group_id}/director/applied")
    assert current_applied.status_code == 200
    assert current_applied.json()["allowed_actions"] == ["GENERATE_STORYBOARD"]


def test_review_required_draft_allows_edit_but_not_apply() -> None:
    client, group_id, sentence_id = _client()
    response = client.put(
        f"/storyboard-groups/{group_id}/director/draft",
        json={
            "shots": [_shot(sentence_id, order=1), _shot(sentence_id, order=2)],
            "expected_draft_revision_id": None,
            "continuous_observation_approvals": [],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["review_state"] == "REQUIRED"
    assert payload["public_status"] == "SUCCEEDED"
    assert payload["allowed_actions"] == ["EDIT_DIRECTOR_DRAFT"]
    assert "continuous-observation approval" in payload["message"]


def test_known_storyboard_progress_states_are_publicly_running() -> None:
    for raw_status in (
        GenerationStatus.SUBMITTING,
        GenerationStatus.SUBMITTED,
        GenerationStatus.RUNNING,
        GenerationStatus.DOWNLOADED,
        GenerationStatus.QC_PASSED,
        GenerationStatus.OBJECT_STORED,
    ):
        state = _storyboard_generation_public_state(raw_status)
        assert state.public_status.value == "RUNNING"
        assert state.allowed_actions == ("REFRESH",)


def test_known_queued_and_unknown_storyboard_states_remain_fail_closed() -> None:
    storyboard_queued = _storyboard_generation_public_state(GenerationStatus.INTENT_PERSISTED)
    unknown = _storyboard_generation_public_state("NEW_INTERNAL_PROVIDER_STATE")

    assert storyboard_queued.public_status.value == "QUEUED"
    assert unknown.public_status.value == "RECOVERY_REQUIRED"
    assert unknown.allowed_actions == ("REFRESH",)


def test_grid_generation_recovers_from_an_already_applied_revision() -> None:
    generations = StoryboardGenerations(fail_once=True)
    client, group_id, sentence_id = _client(generations)
    draft = client.put(
        f"/storyboard-groups/{group_id}/director/draft",
        json={
            "shots": [_shot(sentence_id)],
            "expected_draft_revision_id": None,
            "continuous_observation_approvals": [],
        },
    ).json()

    first = client.post(
        f"/storyboard-groups/{group_id}/grid-sheet-generations",
        headers={"Idempotency-Key": "storyboard-first"},
        json={"expected_director_revision_id": draft["revision_id"]},
    )
    assert first.status_code == 422

    applied = client.get(f"/storyboard-groups/{group_id}/director/applied")
    assert applied.status_code == 200
    applied_revision_id = applied.json()["revision_id"]
    retry = client.post(
        f"/storyboard-groups/{group_id}/grid-sheet-generations",
        headers={"Idempotency-Key": "storyboard-retry"},
        json={"expected_director_revision_id": applied_revision_id},
    )

    assert retry.status_code == 202
    assert retry.json()["public_status"] == "QUEUED"
    assert generations.expected_revision_ids == [
        UUID(applied_revision_id),
        UUID(applied_revision_id),
    ]
