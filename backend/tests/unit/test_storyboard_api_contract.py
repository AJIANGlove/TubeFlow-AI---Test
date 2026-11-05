from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.adapters.persistence.identity_project.models import ProjectSnapshotCharacterRow
from tubeflow_test.adapters.persistence.storyboards.repository import SqlAlchemyStoryboardRepository
from tubeflow_test.api.storyboard_batch import create_storyboard_batch_router
from tubeflow_test.api.storyboard_runtime import create_storyboard_runtime_router
from tubeflow_test.api.storyboards import create_storyboard_router
from tubeflow_test.application.narration_authority import AuthoritativeNarrationGroup
from tubeflow_test.application.storyboard_batch import (
    StoryboardBatchGroupOverview,
    StoryboardBatchService,
)
from tubeflow_test.application.storyboards import DirectorPlanService, StoryboardGenerationService
from tubeflow_test.config import Settings
from tubeflow_test.identity_project.adapters import MemoryRepository, OpaqueSessionTokens
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.identity_project.service import IdentityProjectService


def current() -> Account:
    return Account(uuid4(), "user", "hash", AccountRole.USER)


def test_router_uses_session_dependency_and_excludes_group_director_ai() -> None:
    app = FastAPI()
    app.include_router(
        create_storyboard_router(
            cast(DirectorPlanService, object()),
            cast(StoryboardGenerationService, object()),
            current,
        )
    )
    schema = app.openapi()
    paths = set(schema["paths"])
    operation = schema["paths"]["/storyboard-groups/{group_id}/grid-sheet-generations"]["post"]
    headers = {item["name"] for item in operation["parameters"] if item["in"] == "header"}
    body_schema = schema["components"]["schemas"]["GenerateGridSheetInput"]
    assert "Idempotency-Key" in headers
    assert "X-Owner-ID" not in headers
    assert "/storyboard-groups/{group_id}/director-generations" not in paths
    assert "/storyboard-groups/{group_id}/director-generations/latest" not in paths
    assert "/storyboard-groups/director-generations/{attempt_id}" not in paths
    assert "/storyboard-groups/generations/{generation_id}/resume" not in paths
    assert "/storyboard-groups/generations/{generation_id}/detail" in paths
    assert "/storyboard-groups/generations/{generation_id}/image" in paths
    assert body_schema["additionalProperties"] is False
    assert set(body_schema["properties"]) == {"expected_director_revision_id"}
    assert "/storyboard-groups/{group_id}/director/applied" in paths
    assert "/storyboard-groups/{group_id}/current" in paths
    assert all("single" not in path.casefold() and "panel" not in path.casefold() for path in paths)


def test_unknown_generation_fields_and_hidden_single_panel_are_rejected() -> None:
    app = FastAPI()
    app.include_router(
        create_storyboard_router(
            cast(DirectorPlanService, object()),
            cast(StoryboardGenerationService, object()),
            current,
        )
    )
    response = TestClient(app).post(
        f"/storyboard-groups/{uuid4()}/grid-sheet-generations",
        headers={"Idempotency-Key": "key"},
        json={"expected_director_revision_id": str(uuid4()), "kind": "SINGLE_PANEL"},
    )
    assert response.status_code == 422


def test_zero_based_snapshot_character_history_is_empty_instead_of_422(
    tmp_path: Path,
) -> None:
    owner_id, project_id, group_id = uuid4(), uuid4(), uuid4()
    character_image_id, asset_id = uuid4(), uuid4()
    character = ProjectSnapshotCharacterRow(
        id=uuid4(),
        project_id=project_id,
        ordinal=0,
        character_image_id=character_image_id,
        asset_id=asset_id,
        sha256="a" * 64,
        media_type="image/png",
        size_bytes=128,
    )
    project = SimpleNamespace(
        id=project_id,
        production_state="ACTIVE",
        production_mode="MANUAL",
        snapshot_script="",
        snapshot_confirmed_at=object(),
        snapshot_visual_style="documentary",
        snapshot_locale="en-US",
        snapshot_aspect_ratio="16:9",
    )
    group = AuthoritativeNarrationGroup(
        group_id=group_id,
        owner_id=owner_id,
        project_id=project_id,
        snapshot_sha256="b" * 64,
        snapshot_revision=1,
        start_ms=0,
        end_ms=0,
        video_blocked=False,
        sentences=(),
    )
    media_rows, character_rows, history_rows = Mock(), Mock(), Mock()
    media_rows.all.return_value = []
    character_rows.all.return_value = [character]
    history_rows.all.return_value = []
    session = Mock()
    session.scalar.return_value = project
    session.scalars.side_effect = [media_rows, character_rows]
    session.get.return_value = None
    narration = Mock()
    narration.current_group.return_value = group
    repository = SqlAlchemyStoryboardRepository(lambda: nullcontext(session), narration)

    context = repository.context(owner_id, group_id)
    assert context is not None
    mapped = context.character_assets[0]
    assert character.ordinal == 0
    assert (
        mapped.ordinal,
        mapped.character_image_id,
        mapped.asset_id,
        mapped.sha256,
        mapped.media_type,
        mapped.size_bytes,
    ) == (1, character_image_id, asset_id, "a" * 64, "image/png", 128)

    session.scalars.side_effect = [media_rows, character_rows, history_rows]
    generations = StoryboardGenerationService(
        repository,
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        tmp_path,
        Mock(),
    )
    account = Account(owner_id, "user", "hash", AccountRole.USER)
    app = FastAPI()
    app.include_router(
        create_storyboard_router(
            cast(DirectorPlanService, object()),
            generations,
            lambda: account,
        )
    )

    response = TestClient(app).get(f"/storyboard-groups/{group_id}/history")

    assert response.status_code == 200
    assert response.json() == []


def test_runtime_factory_constructs_real_feature_dependencies(tmp_path: Path) -> None:
    class Passwords:
        def hash(self, password: str) -> str:
            return password

        def verify(self, password_hash: str, password: str) -> bool:
            return password_hash == password

    identity = IdentityProjectService(
        MemoryRepository(), Passwords(), opaque_tokens=OpaqueSessionTokens()
    )
    settings = Settings(
        database_url="sqlite:///runtime-does-not-connect.db",
        storyboard_provider_url="https://provider.test",
    )
    router = create_storyboard_runtime_router(
        settings,
        identity,
        work_dir=tmp_path,
        narration_authority=object(),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    paths = set(app.openapi()["paths"])
    assert "/storyboard-groups/{group_id}/grid-sheet-generations" in paths
    assert "/projects/{project_id}/director-preplan/runs" in paths


def test_batch_overview_item_exposes_current_generation_id() -> None:
    account = current()
    generation_id = uuid4()
    item = StoryboardBatchGroupOverview(
        group_id=uuid4(),
        group_sequence=1,
        has_draft=False,
        draft_review_state=None,
        has_applied=True,
        storyboard_status="SUCCEEDED",
        storyboard_version=1,
        current_generation_id=generation_id,
        can_generate=True,
    )
    batch = Mock()
    batch.overview.return_value = (item,)
    app = FastAPI()
    app.include_router(
        create_storyboard_batch_router(cast(StoryboardBatchService, batch), lambda: account)
    )

    response = TestClient(app).get(f"/projects/{uuid4()}/storyboard-batch/overview")

    assert response.status_code == 200
    (body,) = response.json()["items"]
    assert body["current_generation_id"] == str(generation_id)
    assert set(body) == {
        "group_id",
        "group_sequence",
        "has_draft",
        "draft_review_state",
        "has_applied",
        "storyboard_status",
        "storyboard_version",
        "current_generation_id",
        "can_generate",
    }
