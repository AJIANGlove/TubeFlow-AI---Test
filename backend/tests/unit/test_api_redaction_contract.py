from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.api.deletion_receipt import DeletionReceiptCodec
from tubeflow_test.api.media_router import AuthenticatedOwner, create_media_router
from tubeflow_test.api.media_runtime import SqlDeletionStatus
from tubeflow_test.api.storyboards import create_storyboard_router
from tubeflow_test.domain.deletion import (
    DeletionObject,
    DeletionRecord,
    DeletionStatus,
    ObjectDeletionResult,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    DirectorShot,
    GenerationStatus,
    ShotSentenceLink,
    StoryboardAssetKind,
    StoryboardGeneration,
)
from tubeflow_test.identity_project.domain import Account, AccountRole

_RECEIPT_SECRET = "test-deletion-receipt-secret-is-at-least-32-bytes"


def test_deletion_status_api_never_exposes_exact_object_keys() -> None:
    project, owner = uuid4(), uuid4()
    secret_key = "owners/victim/projects/private/secret.mp4"
    record = DeletionRecord(
        project,
        owner,
        DeletionStatus.DELETE_FAILED,
        (DeletionObject(secret_key, "PROJECT", project),),
        (
            ObjectDeletionResult(
                secret_key,
                1,
                datetime.now(UTC),
                False,
                "storage-private-key=/secret",
            ),
        ),
        "dispatch-detail=internal-worker-host",
    )

    class Repository:
        def get_for_owner(self, project_id: UUID, owner_id: UUID) -> DeletionRecord | None:
            return record if (project_id, owner_id) == (project, owner) else None

    app = FastAPI()
    app.include_router(
        create_media_router(
            current_owner=lambda: AuthenticatedOwner(owner),
            compositions=cast(Any, object()),
            composition_commands=cast(Any, object()),
            deletions=cast(Any, object()),
            deletion_status=SqlDeletionStatus(
                cast(Any, Repository()), DeletionReceiptCodec(_RECEIPT_SECRET)
            ),
            export_factory=cast(Any, lambda _project, _owner: iter(())),
            content_authority=cast(Any, object()),
            content_stream=cast(Any, object()),
        )
    )

    response = TestClient(app).get(f"/projects/{project}/deletion")

    assert response.status_code == 200
    payload = response.json()
    receipt = payload.pop("deletion_receipt")
    expires_at = payload.pop("receipt_expires_at")
    assert DeletionReceiptCodec(_RECEIPT_SECRET).verify(
        receipt, project_id=project, owner_id=owner
    ).expires_at.isoformat().replace("+00:00", "Z") == expires_at
    assert payload == {
        "project_id": str(project),
        "status": "DELETE_FAILED",
        "object_count": 1,
        "succeeded_count": 0,
        "failed_count": 1,
        "errors": ["PROJECT_OBJECT_DELETE_FAILED", "PROJECT_DELETE_DISPATCH_FAILED"],
        "public_status": "FAILED",
        "message": "Project deletion could not be completed. Retry the existing deletion.",
        "allowed_actions": ["RETRY_DELETE", "REFRESH"],
    }
    assert secret_key not in response.text
    assert "storage-private-key" not in response.text
    assert "internal-worker-host" not in response.text


def test_delete_and_retry_responses_expose_stable_running_contract() -> None:
    project, owner = uuid4(), uuid4()

    class Deletions:
        requested: tuple[UUID, UUID, str] | None = None

        def request(self, project_id: UUID, owner_id: UUID, *, confirmation: str) -> None:
            self.requested = (project_id, owner_id, confirmation)

    class DeletionStatus:
        retried: tuple[UUID, UUID] | None = None

        def __init__(self) -> None:
            self.receipts = DeletionReceiptCodec(_RECEIPT_SECRET)

        def receipt(self, project_id: UUID, owner_id: UUID) -> dict[str, object]:
            return self.receipts.issue(project_id, owner_id).as_dict()

        def retry(
            self, project_id: UUID, owner_id: UUID, receipt: str | None
        ) -> dict[str, object]:
            assert receipt is None
            self.retried = (project_id, owner_id)
            return self.receipt(project_id, owner_id)

    deletions = Deletions()
    deletion_status = DeletionStatus()
    app = FastAPI()
    app.include_router(
        create_media_router(
            current_owner=lambda: AuthenticatedOwner(owner),
            compositions=cast(Any, object()),
            composition_commands=cast(Any, object()),
            deletions=cast(Any, deletions),
            deletion_status=cast(Any, deletion_status),
            export_factory=cast(Any, lambda _project, _owner: iter(())),
            content_authority=cast(Any, object()),
            content_stream=cast(Any, object()),
        )
    )
    client = TestClient(app)
    expected = {
        "project_id": str(project),
        "status": "DELETING",
        "public_status": "RUNNING",
        "message": "Project deletion is in progress.",
        "allowed_actions": ["REFRESH"],
    }

    deleted = client.delete(
        f"/projects/{project}", headers={"X-Confirm-Project-Title": "Project title"}
    )
    retried = client.post(f"/projects/{project}/deletion/retry")

    assert deleted.status_code == 202
    deleted_payload = deleted.json()
    deleted_receipt = deleted_payload.pop("deletion_receipt")
    deleted_payload.pop("receipt_expires_at")
    assert deleted_payload == expected
    deletion_status.receipts.verify(deleted_receipt, project_id=project, owner_id=owner)
    assert deletions.requested == (project, owner, "Project title")
    assert retried.status_code == 202
    retried_payload = retried.json()
    retry_receipt = retried_payload.pop("deletion_receipt")
    retried_payload.pop("receipt_expires_at")
    assert retried_payload == expected
    deletion_status.receipts.verify(retry_receipt, project_id=project, owner_id=owner)
    assert deletion_status.retried == (project, owner)


def test_delete_confirmation_accepts_base64_titles_and_rejects_invalid_encoding() -> None:
    project, owner = uuid4(), uuid4()

    class Deletions:
        requested: tuple[UUID, UUID, str] | None = None

        def request(self, project_id: UUID, owner_id: UUID, *, confirmation: str) -> None:
            self.requested = (project_id, owner_id, confirmation)

    class DeletionStatus:
        def __init__(self) -> None:
            self.receipts = DeletionReceiptCodec(_RECEIPT_SECRET)

        def receipt(self, project_id: UUID, owner_id: UUID) -> dict[str, object]:
            return self.receipts.issue(project_id, owner_id).as_dict()

    deletions, deletion_status = Deletions(), DeletionStatus()
    app = FastAPI()
    app.include_router(
        create_media_router(
            current_owner=lambda: AuthenticatedOwner(owner),
            compositions=cast(Any, object()),
            composition_commands=cast(Any, object()),
            deletions=cast(Any, deletions),
            deletion_status=cast(Any, deletion_status),
            export_factory=cast(Any, lambda _project, _owner: iter(())),
            content_authority=cast(Any, object()),
            content_stream=cast(Any, object()),
        )
    )
    client = TestClient(app)
    title = "测试"
    encoded = base64.b64encode(title.encode("utf-8")).decode("ascii")

    created = client.delete(
        f"/projects/{project}", headers={"X-Confirm-Project-Title-B64": encoded}
    )
    assert created.status_code == 202
    assert deletions.requested == (project, owner, title)

    deletions.requested = None
    legacy = client.delete(
        f"/projects/{project}", headers={"X-Confirm-Project-Title": "Project title"}
    )
    assert legacy.status_code == 202
    assert deletions.requested == (project, owner, "Project title")

    deletions.requested = None
    both = client.delete(
        f"/projects/{project}",
        headers={
            "X-Confirm-Project-Title": "ignored",
            "X-Confirm-Project-Title-B64": encoded,
        },
    )
    assert both.status_code == 202
    assert deletions.requested == (project, owner, title)

    with pytest.raises(DomainError) as invalid_base64:
        client.delete(
            f"/projects/{project}",
            headers={"X-Confirm-Project-Title-B64": "not*base64"},
        )
    assert invalid_base64.value.code == "deletion_confirmation_required"
    assert deletions.requested == (project, owner, title)

    with pytest.raises(DomainError) as invalid_utf8:
        client.delete(
            f"/projects/{project}",
            headers={"X-Confirm-Project-Title-B64": "//4="},
        )
    assert invalid_utf8.value.code == "deletion_confirmation_required"

    with pytest.raises(DomainError) as missing:
        client.delete(f"/projects/{project}")
    assert missing.value.code == "deletion_confirmation_required"
    assert deletions.requested == (project, owner, title)

    oversized = client.delete(
        f"/projects/{project}",
        headers={"X-Confirm-Project-Title-B64": "A" * 4097},
    )
    assert oversized.status_code == 422


@pytest.mark.parametrize(
    ("status", "public_status", "message", "allowed_actions"),
    (
        (DeletionStatus.ACTIVE, "NOT_STARTED", "Project deletion has not started.", []),
        (DeletionStatus.DELETING, "RUNNING", "Project deletion is in progress.", ["REFRESH"]),
        (
            DeletionStatus.DELETE_FAILED,
            "FAILED",
            "Project deletion could not be completed. Retry the existing deletion.",
            ["RETRY_DELETE", "REFRESH"],
        ),
        (DeletionStatus.DELETED, "SUCCEEDED", "Project deletion completed.", []),
    ),
)
def test_deletion_get_maps_all_domain_states(
    status: DeletionStatus,
    public_status: str,
    message: str,
    allowed_actions: list[str],
) -> None:
    project, owner = uuid4(), uuid4()
    record = DeletionRecord(project, owner, status, ())

    class Repository:
        def get_for_owner(self, project_id: UUID, owner_id: UUID) -> DeletionRecord | None:
            return record if (project_id, owner_id) == (project, owner) else None

    app = FastAPI()
    app.include_router(
        create_media_router(
            current_owner=lambda: AuthenticatedOwner(owner),
            compositions=cast(Any, object()),
            composition_commands=cast(Any, object()),
            deletions=cast(Any, object()),
            deletion_status=SqlDeletionStatus(
                cast(Any, Repository()), DeletionReceiptCodec(_RECEIPT_SECRET)
            ),
            export_factory=cast(Any, lambda _project, _owner: iter(())),
            content_authority=cast(Any, object()),
            content_stream=cast(Any, object()),
        )
    )

    response = TestClient(app).get(f"/projects/{project}/deletion")

    assert response.status_code == 200
    payload = response.json()
    receipt = payload.pop("deletion_receipt")
    expires_at = payload.pop("receipt_expires_at")
    assert DeletionReceiptCodec(_RECEIPT_SECRET).verify(
        receipt, project_id=project, owner_id=owner
    ).expires_at.isoformat().replace("+00:00", "Z") == expires_at
    assert payload == {
        "project_id": str(project),
        "status": status.value,
        "object_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "errors": [],
        "public_status": public_status,
        "message": message,
        "allowed_actions": allowed_actions,
    }


def test_public_storyboard_views_never_expose_provider_receipts() -> None:
    account = Account(uuid4(), "user", "hash", AccountRole.USER)
    group_id, generation_id = uuid4(), uuid4()
    shot = DirectorShot(uuid4(), 1, "scene", "wide", "eye", "arrive", "establish", "desc")
    generation = StoryboardGeneration(
        generation_id=generation_id,
        owner_id=account.id,
        project_id=uuid4(),
        group_id=group_id,
        applied_revision_id=uuid4(),
        shots_snapshot=(shot,),
        sentence_links_snapshot=(ShotSentenceLink(shot.shot_id, uuid4()),),
        version=1,
        kind=StoryboardAssetKind.GRID_SHEET,
        prompt="grid",
        character_assets_snapshot=(
            CharacterAssetSnapshot(1, uuid4(), uuid4(), "a" * 64, "image/png", 10),
        ),
        style_snapshot="ink",
        model="fake",
        parameters={},
        idempotency_key="key-1",
        provider_idempotency_key="b" * 64,
        canonical_request_sha256="c" * 64,
        status=GenerationStatus.RUNNING,
        created_at=datetime.now(UTC),
        provider_task_id="internal-node-task",
        provider_receipt="Bearer SECRET /internal/bucket/key",
    )

    class Generations:
        def current(self, owner_id: UUID, requested_group_id: UUID) -> StoryboardGeneration:
            assert owner_id == account.id and requested_group_id == group_id
            return generation

        def history(self, owner_id: UUID, requested_group_id: UUID) -> list[StoryboardGeneration]:
            assert owner_id == account.id and requested_group_id == group_id
            return [generation]

        def detail(self, owner_id: UUID, requested_generation_id: UUID) -> StoryboardGeneration:
            assert owner_id == account.id and requested_generation_id == generation_id
            return generation

    app = FastAPI()
    app.include_router(
        create_storyboard_router(cast(Any, object()), cast(Any, Generations()), lambda: account)
    )
    client = TestClient(app)
    responses = (
        client.get(f"/storyboard-groups/{group_id}/current"),
        client.get(f"/storyboard-groups/{group_id}/history"),
        client.get(f"/storyboard-groups/generations/{generation_id}/detail"),
    )

    for response in responses:
        assert response.status_code == 200
        assert "provider_receipt" not in response.text
        assert "provider_task_id" not in response.text
        assert "Bearer SECRET /internal/bucket/key" not in response.text
