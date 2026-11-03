from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.api.project_director import create_project_director_router
from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorConflict,
    ProjectDirectorGroupCandidate,
    ProjectDirectorInputSnapshot,
    ProjectDirectorMaterialization,
    ProjectDirectorRevision,
    ProjectDirectorRun,
    ProjectDirectorRunProgress,
    ProjectDirectorRunStatus,
    ProjectDirectorSentenceSnapshot,
    ProjectDirectorSourceShotLink,
    ProjectDirectorTrigger,
    ProjectDirectorUnitKind,
)
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    DirectorShot,
    ReviewState,
    ShotSentenceLink,
)
from tubeflow_test.identity_project.domain import Account, AccountRole

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _progress_for(run: ProjectDirectorRun) -> ProjectDirectorRunProgress:
    total = run.total_units
    if run.status is ProjectDirectorRunStatus.SUCCEEDED:
        return ProjectDirectorRunProgress(
            succeeded_units=total,
            inflight_units=0,
            retry_pending_units=0,
            unsent_units=0,
            failed_unit_ordinal=None,
            failed_unit_kind=None,
            auto_retries_used=0,
            auto_retry_ceiling=2 * total,
        )
    if run.status is ProjectDirectorRunStatus.RUNNING:
        return ProjectDirectorRunProgress(
            succeeded_units=1,
            inflight_units=1,
            retry_pending_units=1,
            unsent_units=total - 3,
            failed_unit_ordinal=None,
            failed_unit_kind=None,
            auto_retries_used=1,
            auto_retry_ceiling=2 * total,
        )
    if run.status in {
        ProjectDirectorRunStatus.FAILED,
        ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN,
    }:
        return ProjectDirectorRunProgress(
            succeeded_units=1,
            inflight_units=0,
            retry_pending_units=0,
            unsent_units=total - 2,
            failed_unit_ordinal=2,
            failed_unit_kind=ProjectDirectorUnitKind.SHOT_PLAN,
            auto_retries_used=2,
            auto_retry_ceiling=2 * total,
        )
    return ProjectDirectorRunProgress(
        succeeded_units=0,
        inflight_units=0,
        retry_pending_units=0,
        unsent_units=total,
        failed_unit_ordinal=None,
        failed_unit_kind=None,
        auto_retries_used=0,
        auto_retry_ceiling=2 * total,
    )


class FakeProjectDirectorService:
    def __init__(
        self,
        run: ProjectDirectorRun,
        *,
        revision: ProjectDirectorRevision | None = None,
        materialization: ProjectDirectorMaterialization | None = None,
    ) -> None:
        self.run = run
        self.revision = revision
        self.materialization = materialization
        self.create_calls: list[tuple[UUID, UUID, str, ProjectDirectorTrigger]] = []
        self.resolve_calls: list[tuple[UUID, UUID, str, str]] = []

    def _check_owner(self, owner_id: UUID) -> None:
        if owner_id != self.run.owner_id:
            raise DomainError("not_found", "Project director run was not found")

    def create_run(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        idempotency_key: str,
        trigger: ProjectDirectorTrigger,
        confirm_new_input: bool = False,
    ) -> ProjectDirectorRun:
        self._check_owner(owner_id)
        if project_id != self.run.project_id:
            raise DomainError("not_found", "Frozen project snapshot was not found")
        self.create_calls.append((owner_id, project_id, idempotency_key, trigger))
        return self.run

    def status(self, owner_id: UUID, run_id: UUID) -> ProjectDirectorRun:
        self._check_owner(owner_id)
        if run_id != self.run.run_id:
            raise DomainError("not_found", "Project director run was not found")
        return self.run

    def latest(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRun:
        self._check_owner(owner_id)
        if project_id != self.run.project_id:
            raise DomainError("not_found", "Project director run was not found")
        return self.run

    def progress(self, owner_id: UUID, run_id: UUID) -> ProjectDirectorRunProgress:
        self._check_owner(owner_id)
        if run_id != self.run.run_id:
            raise DomainError("not_found", "Project director run was not found")
        return _progress_for(self.run)

    def resolve_unknown(
        self,
        owner_id: UUID,
        run_id: UUID,
        resolution: str,
        external_evidence: str,
    ) -> ProjectDirectorRun:
        self._check_owner(owner_id)
        if run_id != self.run.run_id:
            raise DomainError("not_found", "Project director run was not found")
        if not external_evidence.strip():
            raise DomainError("reconciliation_evidence_required", "External evidence is required")
        if self.run.status is not ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN:
            raise DomainError("reconciliation_not_allowed", "Project director run is not uncertain")
        if resolution not in {"CONFIRMED_NOT_ACCEPTED", "CONFIRMED_FAILED"}:
            raise DomainError("invalid_reconciliation", "Manual resolution is invalid")
        self.resolve_calls.append((owner_id, run_id, resolution, external_evidence))
        prefix = (
            "manual_confirmed_not_accepted"
            if resolution == "CONFIRMED_NOT_ACCEPTED"
            else "manual_confirmed_failed"
        )
        self.run = replace(
            self.run,
            status=ProjectDirectorRunStatus.FAILED,
            failure_code=f"{prefix}:{hashlib.sha256(external_evidence.encode()).hexdigest()}",
            retry_blocked=resolution == "CONFIRMED_FAILED",
        )
        return self.run

    def current(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRevision:
        self._check_owner(owner_id)
        if project_id != self.run.project_id or self.revision is None:
            raise DomainError("not_found", "Project director revision was not found")
        return self.revision

    def current_materialization(
        self, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorMaterialization:
        self._check_owner(owner_id)
        if project_id != self.run.project_id or self.materialization is None:
            raise DomainError("not_found", "Project director materialization was not found")
        return self.materialization


def _snapshot(
    owner_id: UUID, project_id: UUID, *, sentence_count: int = 3
) -> ProjectDirectorInputSnapshot:
    sentences = tuple(
        ProjectDirectorSentenceSnapshot(
            sentence_id=uuid4(),
            sequence=sequence,
            text=f"Sentence {sequence}",
            text_sha256=f"{sequence:064x}",
        )
        for sequence in range(1, sentence_count + 1)
    )
    return ProjectDirectorInputSnapshot(
        owner_id=owner_id,
        project_id=project_id,
        snapshot_sha256="a" * 64,
        snapshot_revision=1,
        sentences=sentences,
        characters=(
            CharacterAssetSnapshot(
                ordinal=1,
                character_image_id=uuid4(),
                asset_id=uuid4(),
                sha256="b" * 64,
                media_type="image/png",
                size_bytes=128,
            ),
        ),
        visual_style="ink wash",
        locale="en-US",
        aspect_ratio="16:9",
    )


def _run(
    *,
    owner_id: UUID | None = None,
    project_id: UUID | None = None,
    run_status: ProjectDirectorRunStatus = ProjectDirectorRunStatus.INTENT_PERSISTED,
    retry_blocked: bool = False,
    failure_code: str | None = None,
    revision_id: UUID | None = None,
    recovery_source_run_id: UUID | None = None,
    auto_retries_used: int = 0,
) -> ProjectDirectorRun:
    owner = owner_id or uuid4()
    project = project_id or uuid4()
    snapshot = _snapshot(owner, project)
    if run_status is ProjectDirectorRunStatus.SUCCEEDED and revision_id is None:
        revision_id = uuid4()
    return ProjectDirectorRun(
        run_id=uuid4(),
        owner_id=owner,
        project_id=project,
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
        idempotency_key="project-director-run",
        canonical_input_sha256="c" * 64,
        input_snapshot=snapshot,
        model="director-model",
        parameters={"temperature": 0.1},
        status=run_status,
        total_units=len(snapshot.sentences),
        created_at=NOW,
        revision_id=revision_id,
        failure_code=failure_code,
        retry_blocked=retry_blocked,
        recovery_source_run_id=recovery_source_run_id,
        auto_retries_used=auto_retries_used,
    )


def _revision(run: ProjectDirectorRun) -> ProjectDirectorRevision:
    shots = tuple(
        DirectorShot(
            shot_id=uuid4(),
            order=sentence.sequence,
            scene=f"scene-{sentence.sequence}",
            shot_size=f"size-{sentence.sequence}",
            camera_viewpoint=f"viewpoint-{sentence.sequence}",
            subject_action_outcome=f"event-{sentence.sequence}",
            narrative_function=f"function-{sentence.sequence}",
            description=f"Shot {sentence.sequence}",
        )
        for sentence in run.input_snapshot.sentences
    )
    links = tuple(
        ShotSentenceLink(shot.shot_id, sentence.sentence_id)
        for shot, sentence in zip(shots, run.input_snapshot.sentences, strict=True)
    )
    return ProjectDirectorRevision(
        revision_id=run.revision_id or uuid4(),
        owner_id=run.owner_id,
        project_id=run.project_id,
        run_id=run.run_id,
        revision_number=1,
        snapshot_sha256=run.input_snapshot.snapshot_sha256,
        snapshot_revision=run.input_snapshot.snapshot_revision,
        sentence_ids=tuple(item.sentence_id for item in run.input_snapshot.sentences),
        shots=shots,
        sentence_links=links,
        quality_conflicts=(),
        created_at=NOW,
    )


def _materialization(
    run: ProjectDirectorRun, status: MaterializationStatus
) -> ProjectDirectorMaterialization:
    sentence = run.input_snapshot.sentences[0]
    shot = DirectorShot(
        uuid4(),
        1,
        "mapped-scene",
        "wide",
        "eye-level",
        "mapped-event",
        "setup",
        "Mapped shot",
    )
    conflicts = (
        ()
        if status is MaterializationStatus.SUCCEEDED
        else (ProjectDirectorConflict("manual_review_required", (sentence.sentence_id,)),)
    )
    candidate = ProjectDirectorGroupCandidate(
        candidate_id=uuid4(),
        group_id=uuid4(),
        group_sequence=1,
        shots=(shot,),
        sentence_links=(ShotSentenceLink(shot.shot_id, sentence.sentence_id),),
        source_shot_links=(ProjectDirectorSourceShotLink(uuid4(), shot.shot_id),),
        conflicts=conflicts,
        review_state=(
            ReviewState.NOT_REQUIRED
            if status is MaterializationStatus.SUCCEEDED
            else ReviewState.REQUIRED
        ),
    )
    return ProjectDirectorMaterialization(
        materialization_id=uuid4(),
        owner_id=run.owner_id,
        project_id=run.project_id,
        preplan_revision_id=run.revision_id or uuid4(),
        snapshot_sha256=run.input_snapshot.snapshot_sha256,
        snapshot_revision=run.input_snapshot.snapshot_revision,
        algorithm_version="project-director-materialization-v1",
        canonical_input_sha256="d" * 64,
        status=status,
        candidates=(candidate,),
        created_at=NOW,
    )


def _app(
    service: FakeProjectDirectorService,
    account: Account,
    *,
    provider_configured: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_project_director_router(
            cast(ProjectDirectorService, service),
            lambda: account,
            provider_configured=provider_configured,
        )
    )
    return app


def _account(owner_id: UUID) -> Account:
    return Account(owner_id, "project-director-user", "hash", AccountRole.USER)


def test_openapi_idempotency_header_and_strict_input_confirmation_contract() -> None:
    run = _run()
    service = FakeProjectDirectorService(run)
    app = _app(service, _account(run.owner_id))
    schema = app.openapi()
    expected_paths = {
        "/projects/{project_id}/director-preplan/runs",
        "/projects/{project_id}/director-preplan/runs/latest",
        "/projects/{project_id}/director-preplan/runs/{run_id}",
        "/projects/{project_id}/director-preplan/runs/{run_id}/manual-resolution",
        "/projects/{project_id}/director-preplan/current",
        "/projects/{project_id}/director-materialization/current",
    }
    assert set(schema["paths"]) == expected_paths

    operation = schema["paths"]["/projects/{project_id}/director-preplan/runs"]["post"]
    header = next(
        item
        for item in operation["parameters"]
        if item["in"] == "header" and item["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
    assert header["schema"]["minLength"] == 1
    assert header["schema"]["maxLength"] == 128
    body_schema = schema["components"]["schemas"]["StartProjectDirectorRunInput"]
    assert body_schema["additionalProperties"] is False
    assert set(body_schema["properties"]) == {"confirm_new_input"}
    assert body_schema["properties"]["confirm_new_input"]["default"] is False
    assert operation["requestBody"]["required"] is True

    client = TestClient(app)
    path = f"/projects/{run.project_id}/director-preplan/runs"
    missing_header = client.post(path, json={})
    missing_body = client.post(path, headers={"Idempotency-Key": "manual-run"})
    unknown_body = client.post(
        path,
        headers={"Idempotency-Key": "manual-run"},
        json={"duration_ms": 1},
    )
    assert [missing_header.status_code, missing_body.status_code, unknown_body.status_code] == [
        422,
        422,
        422,
    ]
    assert service.create_calls == []

    accepted = client.post(
        path,
        headers={"Idempotency-Key": "manual-run"},
        json={},
    )
    assert accepted.status_code == 202
    assert service.create_calls == [
        (
            run.owner_id,
            run.project_id,
            "manual-run",
            ProjectDirectorTrigger.MANUAL_REGENERATE,
        )
    ]


def test_provider_not_configured_returns_503_without_creating_run() -> None:
    run = _run()
    service = FakeProjectDirectorService(run)
    client = TestClient(_app(service, _account(run.owner_id), provider_configured=False))

    response = client.post(
        f"/projects/{run.project_id}/director-preplan/runs",
        headers={"Idempotency-Key": "must-not-create"},
        json={},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "director_provider_not_configured",
        "message": "Director Provider is not configured.",
    }
    assert service.create_calls == []


@pytest.mark.parametrize(
    ("run_status", "retry_blocked", "failure_code", "public_status", "allowed_actions"),
    [
        (
            ProjectDirectorRunStatus.INTENT_PERSISTED,
            False,
            None,
            "QUEUED",
            ["REFRESH"],
        ),
        (ProjectDirectorRunStatus.RUNNING, False, None, "RUNNING", ["REFRESH"]),
        (
            ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN,
            False,
            "transport_unknown",
            "RECOVERY_REQUIRED",
            ["REGENERATE_DIRECTOR_PREPLAN", "REFRESH"],
        ),
        (
            ProjectDirectorRunStatus.SUCCEEDED,
            False,
            None,
            "SUCCEEDED",
            ["VIEW_CURRENT", "REGENERATE_DIRECTOR_PREPLAN"],
        ),
        (
            ProjectDirectorRunStatus.FAILED,
            False,
            "provider_http_400",
            "FAILED",
            ["RETRY"],
        ),
        (
            ProjectDirectorRunStatus.FAILED,
            True,
            "manual_confirmed_failed:" + "e" * 64,
            "FAILED",
            [],
        ),
    ],
)
def test_run_states_expose_only_safe_public_actions(
    run_status: ProjectDirectorRunStatus,
    retry_blocked: bool,
    failure_code: str | None,
    public_status: str,
    allowed_actions: list[str],
) -> None:
    run = _run(
        run_status=run_status,
        retry_blocked=retry_blocked,
        failure_code=failure_code,
    )
    service = FakeProjectDirectorService(run)
    response = TestClient(_app(service, _account(run.owner_id))).get(
        f"/projects/{run.project_id}/director-preplan/runs/{run.run_id}"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == run_status.value
    assert payload["public_status"] == public_status
    assert payload["allowed_actions"] == allowed_actions
    assert isinstance(payload["message"], str) and payload["message"]
    if run_status is ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN:
        assert "RETRY" not in payload["allowed_actions"]
    assert "next_unit_ordinal" not in payload
    progress = _progress_for(run)
    assert payload["total_units"] == run.total_units
    assert payload["completed_units"] == progress.succeeded_units
    assert payload["succeeded_units"] == progress.succeeded_units
    assert payload["inflight_units"] == progress.inflight_units
    assert payload["retry_pending_units"] == progress.retry_pending_units
    assert payload["unsent_units"] == progress.unsent_units
    assert payload["auto_retries_used"] == progress.auto_retries_used
    assert payload["auto_retry_ceiling"] == 2 * run.total_units
    assert payload["failed_unit_ordinal"] == progress.failed_unit_ordinal
    expected_kind = (
        progress.failed_unit_kind.value if progress.failed_unit_kind is not None else None
    )
    assert payload["failed_unit_kind"] == expected_kind


def test_failed_run_reports_unrecoverable_failure_message_and_failure_position() -> None:
    run = _run(
        run_status=ProjectDirectorRunStatus.FAILED,
        failure_code="director_output_sentence_coverage_invalid",
    )
    service = FakeProjectDirectorService(run)

    response = TestClient(_app(service, _account(run.owner_id))).get(
        f"/projects/{run.project_id}/director-preplan/runs/{run.run_id}"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == (
        "The project director run stopped after an unrecoverable unit failure."
    )
    assert payload["failure_code"] == "director_output_sentence_coverage_invalid"
    assert payload["failed_unit_ordinal"] == 2
    assert payload["failed_unit_kind"] == "SHOT_PLAN"
    assert payload["completed_units"] == 1
    assert payload["unsent_units"] == 1
    assert payload["auto_retries_used"] == 2
    assert payload["auto_retry_ceiling"] == 6


def test_run_status_serializes_recovery_source_lineage() -> None:
    recovery_source_run_id = uuid4()
    run = _run(
        run_status=ProjectDirectorRunStatus.RUNNING,
        recovery_source_run_id=recovery_source_run_id,
    )
    service = FakeProjectDirectorService(run)

    response = TestClient(_app(service, _account(run.owner_id))).get(
        f"/projects/{run.project_id}/director-preplan/runs/{run.run_id}"
    )

    assert response.status_code == 200
    assert response.json()["recovery_source_run_id"] == str(recovery_source_run_id)


def test_run_project_path_mismatch_is_404_for_status_and_manual_resolution() -> None:
    run = _run(run_status=ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN)
    service = FakeProjectDirectorService(run)
    client = TestClient(_app(service, _account(run.owner_id)))
    other_project = uuid4()

    status_response = client.get(f"/projects/{other_project}/director-preplan/runs/{run.run_id}")
    resolution_response = client.post(
        f"/projects/{other_project}/director-preplan/runs/{run.run_id}/manual-resolution",
        json={
            "resolution": "CONFIRMED_FAILED",
            "external_evidence": "Provider console evidence",
        },
    )

    assert status_response.status_code == 404
    assert resolution_response.status_code == 404
    assert status_response.json()["detail"]["code"] == "not_found"
    assert resolution_response.json()["detail"]["code"] == "not_found"
    assert service.resolve_calls == []


def test_manual_resolution_requires_non_blank_evidence() -> None:
    run = _run(run_status=ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN)
    service = FakeProjectDirectorService(run)
    client = TestClient(_app(service, _account(run.owner_id)))
    path = f"/projects/{run.project_id}/director-preplan/runs/{run.run_id}/manual-resolution"

    missing = client.post(path, json={"resolution": "CONFIRMED_NOT_ACCEPTED"})
    empty = client.post(
        path,
        json={"resolution": "CONFIRMED_NOT_ACCEPTED", "external_evidence": ""},
    )
    blank = client.post(
        path,
        json={"resolution": "CONFIRMED_NOT_ACCEPTED", "external_evidence": "   "},
    )

    assert missing.status_code == 422
    assert empty.status_code == 422
    assert blank.status_code == 422
    assert blank.json()["detail"]["code"] == "reconciliation_evidence_required"
    assert service.resolve_calls == []


@pytest.mark.parametrize(
    ("resolution", "allowed_actions"),
    [
        ("CONFIRMED_NOT_ACCEPTED", ["RETRY"]),
        ("CONFIRMED_FAILED", []),
    ],
)
def test_owned_manual_resolution_forwards_evidence_and_returns_safe_actions(
    resolution: str, allowed_actions: list[str]
) -> None:
    run = _run(run_status=ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN)
    service = FakeProjectDirectorService(run)
    client = TestClient(_app(service, _account(run.owner_id)))
    evidence = "Provider console confirms the terminal outcome."

    response = client.post(
        (f"/projects/{run.project_id}/director-preplan/runs/{run.run_id}/manual-resolution"),
        json={"resolution": resolution, "external_evidence": evidence},
    )

    assert response.status_code == 200
    assert response.json()["public_status"] == "FAILED"
    assert response.json()["allowed_actions"] == allowed_actions
    assert response.json()["failed_unit_ordinal"] == 2
    assert evidence not in response.text
    assert service.resolve_calls == [(run.owner_id, run.run_id, resolution, evidence)]


def test_manual_resolution_is_owner_isolated() -> None:
    run = _run(run_status=ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN)
    service = FakeProjectDirectorService(run)
    client = TestClient(_app(service, _account(uuid4())))

    response = client.post(
        (f"/projects/{run.project_id}/director-preplan/runs/{run.run_id}/manual-resolution"),
        json={
            "resolution": "CONFIRMED_FAILED",
            "external_evidence": "Evidence from a different owner",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    assert service.resolve_calls == []


def test_current_revision_pagination_returns_only_page_shots_and_sentences() -> None:
    revision_id = uuid4()
    run = _run(
        run_status=ProjectDirectorRunStatus.SUCCEEDED,
        revision_id=revision_id,
    )
    revision = _revision(run)
    service = FakeProjectDirectorService(run, revision=revision)
    response = TestClient(_app(service, _account(run.owner_id))).get(
        f"/projects/{run.project_id}/director-preplan/current?offset=1&limit=1"
    )

    assert response.status_code == 200
    payload = response.json()
    selected_shot = revision.shots[1]
    selected_sentence = run.input_snapshot.sentences[1]
    assert payload["total_shots"] == 3
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert [item["shot_id"] for item in payload["shots"]] == [str(selected_shot.shot_id)]
    assert [item["sentence_id"] for item in payload["sentences"]] == [
        str(selected_sentence.sentence_id)
    ]
    assert payload["shots"][0]["sentence_ids"] == [str(selected_sentence.sentence_id)]
    assert payload["public_status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    ("materialization_status", "public_status", "allowed_actions"),
    [
        (
            MaterializationStatus.SUCCEEDED,
            "SUCCEEDED",
            ["VIEW_FINAL_DIRECTOR"],
        ),
        (
            MaterializationStatus.REVIEW_REQUIRED,
            "RECOVERY_REQUIRED",
            ["EDIT_FINAL_DIRECTOR"],
        ),
    ],
)
def test_materialization_status_exposes_final_director_actions(
    materialization_status: MaterializationStatus,
    public_status: str,
    allowed_actions: list[str],
) -> None:
    run = _run(run_status=ProjectDirectorRunStatus.SUCCEEDED)
    materialization = _materialization(run, materialization_status)
    service = FakeProjectDirectorService(run, materialization=materialization)

    response = TestClient(_app(service, _account(run.owner_id))).get(
        f"/projects/{run.project_id}/director-materialization/current"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == materialization_status.value
    assert payload["public_status"] == public_status
    assert payload["allowed_actions"] == allowed_actions
    source_link = materialization.candidates[0].source_shot_links[0]
    assert payload["candidates"][0]["source_shot_links"] == [
        {
            "source_shot_id": str(source_link.source_shot_id),
            "candidate_shot_id": str(source_link.candidate_shot_id),
        }
    ]
    candidate = materialization.candidates[0]
    assert payload["candidates"][0]["shots"] == [
        {
            "shot_id": str(candidate.shots[0].shot_id),
            "order": 1,
            "scene": "mapped-scene",
            "shot_size": "wide",
            "camera_viewpoint": "eye-level",
            "subject_action_outcome": "mapped-event",
            "narrative_function": "setup",
            "description": "Mapped shot",
            "sentence_ids": [str(run.input_snapshot.sentences[0].sentence_id)],
        }
    ]
    assert payload["candidates"][0]["review_state"] == (
        "NOT_REQUIRED" if materialization_status is MaterializationStatus.SUCCEEDED else "REQUIRED"
    )


@pytest.mark.parametrize("changed_input", [False, True])
def test_real_service_api_reprocesses_unknown_without_evidence(changed_input: bool) -> None:
    from test_project_director import complete, context, create
    from test_project_director import service as make_service

    value = context(12)
    real_service, repository, model, _clock = make_service(value, unknown_on=3)
    source = create(real_service, value, "api-waive-source")
    assert complete(real_service, value, source.run_id).status is (
        ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
    )
    client = TestClient(_app(real_service, _account(value.owner_id)))
    endpoint = f"/projects/{value.project_id}/director-preplan/runs"
    unknown = client.get(f"{endpoint}/latest").json()
    assert unknown["allowed_actions"] == ["REGENERATE_DIRECTOR_PREPLAN", "REFRESH"]
    headers = {"Idempotency-Key": "api-waive-recovery"}
    if changed_input:
        real_service._model_name = "updated-director-model"
    response = client.post(endpoint, headers=headers, json={})
    if changed_input:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "director_input_changed"
        assert len(repository.runs) == 1
        assert real_service.status(value.owner_id, source.run_id).status is (
            ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
        )
        response = client.post(endpoint, headers=headers, json={"confirm_new_input": True})
    assert response.status_code == 202
    result = response.json()
    assert result["recovery_source_run_id"] == (
        None if changed_input else str(source.run_id)
    )
    assert client.post(
        endpoint, headers=headers, json={"confirm_new_input": changed_input}
    ).json()["run_id"] == result["run_id"]
    assert len(repository.runs) == 2
    assert model.calls == [1, 2, 3]
    model.unknown_on = None
    assert complete(real_service, value, UUID(result["run_id"])).status is (
        ProjectDirectorRunStatus.SUCCEEDED
    )
    assert model.calls == [1, 2, 3] + ([1, 2, 3] if changed_input else [3])
