from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from tubeflow_test.application.director_generation import (
    DirectorResponseDiagnostics,
    DirectorStructuredResponseError,
    PermanentDirectorModelError,
    RetryableDirectorModelError,
)
from tubeflow_test.application.project_director import (
    GLOBAL_PLAN_DOCUMENT_PLACEHOLDER,
    PROJECT_DIRECTOR_PLAN_PROMPT_REF,
    PROJECT_DIRECTOR_PROMPT_REF,
    PROJECT_DIRECTOR_RUN_RETRY_BUDGET_FACTOR,
    PROJECT_DIRECTOR_UNITIZATION_VERSION,
    PROJECT_DIRECTOR_UNKNOWN_EXHAUSTED_FAILURE,
    ProjectDirectorService,
    ProjectDirectorUnitBinding,
    _hash,
    _snapshot_json,
    prepare_manual_reprocessing,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    PROJECT_DIRECTOR_WORKER_LOST_FAILURE,
    MaterializationStatus,
    ProjectDirectorGroupContext,
    ProjectDirectorInputSnapshot,
    ProjectDirectorMaterialization,
    ProjectDirectorRevision,
    ProjectDirectorRun,
    ProjectDirectorRunProgress,
    ProjectDirectorRunStatus,
    ProjectDirectorSentenceSnapshot,
    ProjectDirectorTrigger,
    ProjectDirectorUnit,
    ProjectDirectorUnitKind,
    ProjectDirectorUnitStatus,
)
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    DirectorLocationRelation,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
    ReviewState,
    ShotSentenceLink,
    projected_shot_time_ranges_ms,
)

TERMINAL_RUN_STATUSES = {
    ProjectDirectorRunStatus.SUCCEEDED,
    ProjectDirectorRunStatus.FAILED,
    ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN,
}


class Ids:
    def __init__(self) -> None:
        self.value = 1000

    def new(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 8, 30, tzinfo=UTC)

    def now(self) -> datetime:
        return self.moment

    def advance_by(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class Result:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.receipt = "provider-receipt"
        self.raw_response_sha256 = "a" * 64


PLAN_PROSE_BY_LOCALE = {
    "en-US": "The people enter the cave and see the world.",
    "ja-JP": "人々は洞窟に入り、周囲を見渡す。",
    "ko-KR": "사람들은 동굴에 들어가 주변을 살핀다.",
    "es-ES": "La gente entra en la cueva y mira el mundo.",
    "fr-FR": "Les gens entrent dans la grotte avec une lumière.",
    "pt-BR": "As pessoas entram na caverna com uma luz.",
}

_SEMANTIC_EVENTS = (
    "ESTABLISH_CONTEXT",
    "PERFORM_ACTION",
    "SHOW_CONSEQUENCE",
    "REVEAL_EVIDENCE",
    "EXPLAIN_CAUSE",
    "COMPARE",
    "COUNTERFACTUAL",
    "EXPRESS_UNCERTAINTY",
    "TRANSITION",
)
_SUBJECT_FOCUSES = (
    "ENVIRONMENT",
    "PRIMARY_SUBJECT",
    "MULTIPLE_SUBJECTS",
    "OBJECT_OR_EVIDENCE",
    "PROCESS",
    "ABSTRACT_CONCEPT",
)
_TIME_RELATIONS = ("SAME_MOMENT", "EARLIER", "LATER", "TIMELESS")
_LOCATION_RELATIONS = (
    "SAME_LOCATION",
    "SCRIPT_SUPPORTED_NEW_LOCATION",
    "NON_SPATIAL",
)


def rotated_semantics(key: int) -> dict[str, str]:
    event_index = key % 9
    subject_digit = (key // 9) % 6
    time_digit = (key // 54) % 4
    location_digit = (key // 216) % 3
    return {
        "semantic_event": _SEMANTIC_EVENTS[event_index],
        "subject_focus": _SUBJECT_FOCUSES[(event_index + subject_digit) % 6],
        "time_relation": _TIME_RELATIONS[
            (event_index + subject_digit + time_digit) % len(_TIME_RELATIONS)
        ],
        "location_relation": _LOCATION_RELATIONS[
            (event_index + subject_digit + time_digit + location_digit) % len(_LOCATION_RELATIONS)
        ],
    }


def shot_json(
    unit: ProjectDirectorUnit,
    order: int,
    key: int,
    *,
    links: tuple[UUID, ...] | None = None,
) -> dict[str, object]:
    linked = links if links is not None else tuple(item.sentence_id for item in unit.sentences)
    return {
        "order": order,
        "scene": f"scene-{key}",
        "shot_size": "WIDE" if key % 2 else "CLOSE_UP",
        "camera_viewpoint": "HIGH_ANGLE" if key % 2 else "LOW_ANGLE",
        "action": f"event-{key}",
        "outcome": f"result-{key}",
        "narrative_function": f"function-{key}",
        "description": f"description-{key}",
        **rotated_semantics(key),
        "sentence_ids": [str(item) for item in linked],
    }


class Model:
    def __init__(
        self,
        *,
        fail_on: int | None = None,
        unknown_on: int | None = None,
        structured_on: int | None = None,
        invalid_output_on: int | None = None,
        retryable_on: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.calls: list[int] = []
        self.fail_on = fail_on
        self.unknown_on = unknown_on
        self.structured_on = structured_on
        self.invalid_output_on = invalid_output_on
        self.retryable_on = retryable_on
        self.retry_after_seconds = retry_after_seconds

    def generate(self, unit: ProjectDirectorUnit) -> Result:
        self.calls.append(unit.ordinal)
        if unit.ordinal == self.fail_on:
            raise PermanentDirectorModelError("provider_http_400")
        if unit.ordinal == self.retryable_on:
            raise RetryableDirectorModelError(
                "provider_http_429", retry_after_seconds=self.retry_after_seconds
            )
        if unit.ordinal == self.unknown_on:
            raise TimeoutError("response lost")
        if unit.ordinal == self.structured_on:
            raise DirectorStructuredResponseError(
                "director_response_content_json_invalid",
                provider_receipt="provider-structured-receipt",
                diagnostics=DirectorResponseDiagnostics(
                    http_status=200,
                    content_type="application/json",
                    request_id_present=True,
                    request_id_source="header",
                    raw_size_bytes=41,
                    raw_response_sha256="d" * 64,
                    parse_stage="content_json",
                    exception_class="JSONDecodeError",
                ),
            )
        if unit.kind is ProjectDirectorUnitKind.GLOBAL_PLAN:
            return Result(self.plan_payload(unit))
        if unit.ordinal == self.invalid_output_on:
            return Result({"output_locale": unit.required_locale, "shots": []})
        return Result(self.shot_payload(unit))

    def plan_payload(self, unit: ProjectDirectorUnit) -> dict[str, object]:
        if unit.ordinal == self.invalid_output_on:
            return {"output_locale": unit.required_locale, "chapters": []}
        prose = PLAN_PROSE_BY_LOCALE.get(unit.required_locale, PLAN_PROSE_BY_LOCALE["en-US"])
        return {
            "output_locale": unit.required_locale,
            "chapters": [
                {
                    "order": 1,
                    "sentence_ids": [str(item.sentence_id) for item in unit.context_before],
                    "visual_beat": prose,
                    "setting": prose,
                }
            ],
        }

    def shot_payload(self, unit: ProjectDirectorUnit) -> dict[str, object]:
        return {
            "output_locale": unit.required_locale,
            "shots": [shot_json(unit, 1, unit.ordinal)],
        }


class PerSentenceModel(Model):
    """Returns one shot per unit sentence with conflict-free semantic rotation."""

    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        shots = [
            shot_json(
                unit,
                index,
                unit.ordinal * 8 + index,
                links=(sentence.sentence_id,),
            )
            for index, sentence in enumerate(unit.sentences, start=1)
        ]
        return Result({"output_locale": unit.required_locale, "shots": shots})


class MemoryRepository:
    def __init__(self, context: ProjectDirectorInputSnapshot) -> None:
        self.context = context
        self.runs: dict[UUID, ProjectDirectorRun] = {}
        self.run_keys: dict[tuple[UUID, str], UUID] = {}
        self.units: dict[UUID, list[ProjectDirectorUnit]] = {}
        self.revisions: dict[UUID, ProjectDirectorRevision] = {}
        self.current: dict[tuple[UUID, UUID], UUID] = {}
        self.groups: tuple[ProjectDirectorGroupContext, ...] = ()
        self.materializations: dict[str, ProjectDirectorMaterialization] = {}
        self.response_diagnostics: dict[UUID, dict[str, str | int | bool]] = {}
        self.auto_input_change_audits: dict[UUID, dict[str, object]] = {}
        self.production_mode = "MANUAL"

    def project_context(
        self, owner_id: UUID, project_id: UUID
    ) -> ProjectDirectorInputSnapshot | None:
        return (
            self.context
            if (owner_id, project_id)
            == (
                self.context.owner_id,
                self.context.project_id,
            )
            else None
        )

    def project_production_mode(self, owner_id: UUID, project_id: UUID) -> str | None:
        if (owner_id, project_id) != (self.context.owner_id, self.context.project_id):
            return None
        return self.production_mode

    def find_run_by_key(self, owner_id: UUID, key: str) -> ProjectDirectorRun | None:
        run_id = self.run_keys.get((owner_id, key))
        return self.runs.get(run_id) if run_id else None

    def find_blocking_run(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRun | None:
        values = [
            item
            for item in self.runs.values()
            if item.owner_id == owner_id
            and item.project_id == project_id
            and (
                item.status
                in {
                    ProjectDirectorRunStatus.INTENT_PERSISTED,
                    ProjectDirectorRunStatus.RUNNING,
                    ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN,
                }
                or item.retry_blocked
            )
        ]
        return values[-1] if values else None

    def create_run(
        self,
        run: ProjectDirectorRun,
        units: tuple[ProjectDirectorUnit, ...],
        *,
        confirm_new_input: bool = False,
        expected_latest_run_id: UUID | None = None,
        auto_input_change_audit: dict[str, object] | None = None,
    ) -> ProjectDirectorRun:
        if auto_input_change_audit is not None:
            self.auto_input_change_audits[run.run_id] = auto_input_change_audit
        stored_units = units
        if run.trigger is ProjectDirectorTrigger.MANUAL_REGENERATE:
            source = self.latest_run(run.owner_id, run.project_id)
            blocking = tuple(
                item
                for item in self.runs.values()
                if item.owner_id == run.owner_id
                and item.project_id == run.project_id
                and (
                    item.status in {
                        ProjectDirectorRunStatus.INTENT_PERSISTED,
                        ProjectDirectorRunStatus.RUNNING,
                        ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN,
                    }
                    or item.retry_blocked
                )
            )
            run, stored_units, closed_source = prepare_manual_reprocessing(
                run,
                units,
                source,
                tuple(self.units[source.run_id]) if source else (),
                blocking,
                confirm_new_input=confirm_new_input,
                expected_latest_run_id=expected_latest_run_id,
            )
            if closed_source is not None:
                self.runs[closed_source.run_id] = closed_source
                self.units[closed_source.run_id] = [
                    replace(item, status=ProjectDirectorUnitStatus.FAILED,
                            state_revision=item.state_revision + 1)
                    if item.status is ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN else item
                    for item in self.units[closed_source.run_id]
                ]
        self.runs[run.run_id] = run
        self.run_keys[(run.owner_id, run.idempotency_key)] = run.run_id
        self.units[run.run_id] = list(stored_units)
        return run

    def get_run(self, owner_id: UUID, run_id: UUID) -> ProjectDirectorRun | None:
        value = self.runs.get(run_id)
        return value if value is not None and value.owner_id == owner_id else None

    def latest_run(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRun | None:
        values = [
            item
            for item in self.runs.values()
            if item.owner_id == owner_id and item.project_id == project_id
        ]
        return max(values, key=lambda item: (item.created_at, item.run_id.int)) if values else None

    def _claimable(self, unit: ProjectDirectorUnit, now: datetime) -> bool:
        if unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED:
            if unit.next_attempt_at is not None and unit.next_attempt_at > now:
                return False
            return (
                unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN
                or unit.plan_document_sha256 is not None
            )
        return (
            unit.status is ProjectDirectorUnitStatus.LEASED
            and unit.lease_until is not None
            and unit.lease_until < now
        )

    def next_claimable_unit(
        self, owner_id: UUID, run_id: UUID, now: datetime
    ) -> ProjectDirectorUnit | None:
        run = self.get_run(owner_id, run_id)
        if run is None or run.status not in {
            ProjectDirectorRunStatus.INTENT_PERSISTED,
            ProjectDirectorRunStatus.RUNNING,
        }:
            return None
        return next(
            (unit for unit in self.units[run_id] if self._claimable(unit, now)),
            None,
        )

    def find_stale_submitting_unit(
        self, owner_id: UUID, run_id: UUID, now: datetime
    ) -> ProjectDirectorUnit | None:
        if self.get_run(owner_id, run_id) is None:
            return None
        return next(
            (
                unit
                for unit in self.units[run_id]
                if unit.status is ProjectDirectorUnitStatus.SUBMITTING
                and unit.lease_until is not None
                and unit.lease_until < now
            ),
            None,
        )

    def reclaim_expired_units(
        self, owner_id: UUID, project_id: UUID, *, now: datetime
    ) -> tuple[UUID, ...]:
        reclaimed: list[UUID] = []
        for run_id, units in self.units.items():
            run = self.runs.get(run_id)
            if (
                run is None
                or run.owner_id != owner_id
                or run.project_id != project_id
                or run.status
                not in {
                    ProjectDirectorRunStatus.FAILED,
                    ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN,
                }
            ):
                continue
            for index, unit in enumerate(units):
                if unit.status not in {
                    ProjectDirectorUnitStatus.LEASED,
                    ProjectDirectorUnitStatus.SUBMITTING,
                }:
                    continue
                if unit.lease_until is None or unit.lease_until >= now:
                    continue
                updates: dict[str, object] = {
                    "lease_token": None,
                    "lease_until": None,
                    "next_attempt_at": None,
                    "state_revision": unit.state_revision + 1,
                }
                if unit.status is ProjectDirectorUnitStatus.SUBMITTING:
                    updates["status"] = ProjectDirectorUnitStatus.FAILED
                    updates["failure_code"] = PROJECT_DIRECTOR_WORKER_LOST_FAILURE
                    updates["last_failure_code"] = PROJECT_DIRECTOR_WORKER_LOST_FAILURE
                else:
                    updates["status"] = ProjectDirectorUnitStatus.INTENT_PERSISTED
                units[index] = replace(unit, **updates)
                reclaimed.append(unit.unit_id)
        return tuple(reclaimed)

    def claim_unit(
        self,
        owner_id: UUID,
        run_id: UUID,
        unit_id: UUID,
        token: UUID,
        now: datetime,
        lease_until: datetime,
    ) -> ProjectDirectorUnit | None:
        run = self.get_run(owner_id, run_id)
        if run is None or run.status not in {
            ProjectDirectorRunStatus.INTENT_PERSISTED,
            ProjectDirectorRunStatus.RUNNING,
        }:
            return None
        unit = next((item for item in self.units[run_id] if item.unit_id == unit_id), None)
        if unit is None or not self._claimable(unit, now):
            return None
        claimed = replace(
            unit,
            status=ProjectDirectorUnitStatus.LEASED,
            state_revision=unit.state_revision + 1,
            lease_token=token,
            lease_until=lease_until,
        )
        self.units[run_id][unit.ordinal - 1] = claimed
        self.runs[run_id] = replace(
            run,
            status=ProjectDirectorRunStatus.RUNNING,
            state_revision=run.state_revision + 1,
        )
        return claimed

    def mark_unit_call_started(self, unit: ProjectDirectorUnit) -> ProjectDirectorUnit | None:
        current = self.units[unit.run_id][unit.ordinal - 1]
        if current != unit or unit.status is not ProjectDirectorUnitStatus.LEASED:
            return None
        started = replace(
            unit,
            status=ProjectDirectorUnitStatus.SUBMITTING,
            state_revision=unit.state_revision + 1,
            attempt_count=unit.attempt_count + 1,
        )
        self.units[unit.run_id][unit.ordinal - 1] = started
        return started

    def complete_unit(
        self,
        unit: ProjectDirectorUnit,
        shots: tuple[DirectorShot, ...],
        links: tuple[ShotSentenceLink, ...],
        receipt: str,
        raw_response_sha256: str,
    ) -> ProjectDirectorRun:
        current = self.units[unit.run_id][unit.ordinal - 1]
        run = self.runs[unit.run_id]
        if current != unit:
            return run
        completed = replace(
            unit,
            status=ProjectDirectorUnitStatus.SUCCEEDED,
            state_revision=unit.state_revision + 1,
            lease_token=None,
            lease_until=None,
            provider_receipt=receipt,
            raw_response_sha256=raw_response_sha256,
            shots=shots,
            sentence_links=links,
            last_failure_code=None,
            next_attempt_at=None,
        )
        self.units[unit.run_id][unit.ordinal - 1] = completed
        self.runs[run.run_id] = replace(run, state_revision=run.state_revision + 1)
        return self.runs[run.run_id]

    def complete_plan_unit(
        self,
        unit: ProjectDirectorUnit,
        plan_document: str,
        receipt: str,
        raw_response_sha256: str,
        bindings: tuple[ProjectDirectorUnitBinding, ...],
    ) -> ProjectDirectorRun:
        current = self.units[unit.run_id][unit.ordinal - 1]
        run = self.runs[unit.run_id]
        if current != unit:
            return run
        plan_document_sha256 = hashlib.sha256(plan_document.encode()).hexdigest()
        assert all(item.plan_document_sha256 == plan_document_sha256 for item in bindings)
        completed = replace(
            unit,
            status=ProjectDirectorUnitStatus.SUCCEEDED,
            state_revision=unit.state_revision + 1,
            lease_token=None,
            lease_until=None,
            provider_receipt=receipt,
            raw_response_sha256=raw_response_sha256,
            plan_document=plan_document,
            plan_document_sha256=plan_document_sha256,
            last_failure_code=None,
            next_attempt_at=None,
        )
        self.units[unit.run_id][unit.ordinal - 1] = completed
        indexes = {item.unit_id: index for index, item in enumerate(self.units[unit.run_id])}
        for binding in bindings:
            index = indexes[binding.unit_id]
            candidate = self.units[unit.run_id][index]
            if (
                candidate.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
                and candidate.plan_document_sha256 is None
            ):
                self.units[unit.run_id][index] = replace(
                    candidate,
                    prompt_content=binding.prompt_content,
                    prompt_sha256=binding.prompt_sha256,
                    canonical_request_sha256=binding.canonical_request_sha256,
                    plan_document_sha256=binding.plan_document_sha256,
                    state_revision=candidate.state_revision + 1,
                )
        self.runs[run.run_id] = replace(run, state_revision=run.state_revision + 1)
        return self.runs[run.run_id]

    def schedule_unit_retry(
        self,
        unit: ProjectDirectorUnit,
        *,
        failure_code: str,
        next_attempt_at: datetime,
        provider_receipt: str | None = None,
        raw_response_sha256: str | None = None,
        response_diagnostics: dict[str, str | int | bool] | None = None,
    ) -> ProjectDirectorRun | None:
        run = self.runs[unit.run_id]
        ceiling = PROJECT_DIRECTOR_RUN_RETRY_BUDGET_FACTOR * run.total_units
        current = self.units[unit.run_id][unit.ordinal - 1]
        if current != unit:
            return run
        if self.production_mode != "AUTO" and run.auto_retries_used >= ceiling:
            return None
        self.units[unit.run_id][unit.ordinal - 1] = replace(
            unit,
            status=ProjectDirectorUnitStatus.INTENT_PERSISTED,
            state_revision=unit.state_revision + 1,
            lease_token=None,
            lease_until=None,
            provider_receipt=provider_receipt,
            raw_response_sha256=raw_response_sha256,
            last_failure_code=failure_code,
            next_attempt_at=next_attempt_at,
        )
        self.response_diagnostics[unit.unit_id] = dict(response_diagnostics or {})
        self.runs[unit.run_id] = replace(
            run,
            auto_retries_used=run.auto_retries_used + 1,
            state_revision=run.state_revision + 1,
        )
        return self.runs[unit.run_id]

    def fail_unit(
        self,
        unit: ProjectDirectorUnit,
        *,
        failure_code: str,
        remote_state_unknown: bool,
        provider_receipt: str | None = None,
        raw_response_sha256: str | None = None,
        response_diagnostics: dict[str, str | int | bool] | None = None,
    ) -> ProjectDirectorRun:
        unit_status = (
            ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN
            if remote_state_unknown
            else ProjectDirectorUnitStatus.FAILED
        )
        current = self.units[unit.run_id][unit.ordinal - 1]
        run = self.runs[unit.run_id]
        if current != unit:
            return run
        self.units[unit.run_id][unit.ordinal - 1] = replace(
            unit,
            status=unit_status,
            state_revision=unit.state_revision + 1,
            lease_token=None,
            lease_until=None,
            failure_code=failure_code,
            last_failure_code=failure_code,
            next_attempt_at=None,
            provider_receipt=provider_receipt,
            raw_response_sha256=raw_response_sha256,
        )
        self.response_diagnostics[unit.unit_id] = dict(response_diagnostics or {})
        if run.status not in TERMINAL_RUN_STATUSES:
            self.runs[unit.run_id] = replace(
                run,
                status=(
                    ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
                    if remote_state_unknown
                    else ProjectDirectorRunStatus.FAILED
                ),
                state_revision=run.state_revision + 1,
                failure_code=failure_code,
            )
        return self.runs[unit.run_id]

    def list_units(self, owner_id: UUID, run_id: UUID) -> tuple[ProjectDirectorUnit, ...]:
        run = self.get_run(owner_id, run_id)
        return tuple(self.units[run_id]) if run else ()

    def complete_run(
        self, run: ProjectDirectorRun, revision: ProjectDirectorRevision
    ) -> ProjectDirectorRun:
        succeeded = sum(
            item.status is ProjectDirectorUnitStatus.SUCCEEDED for item in self.units[run.run_id]
        )
        if succeeded != run.total_units:
            raise DomainError(
                "director_run_incomplete",
                "Project director run cannot complete before every unit succeeds",
            )
        number = 1 + max(
            (
                item.revision_number
                for item in self.revisions.values()
                if item.owner_id == run.owner_id and item.project_id == run.project_id
            ),
            default=0,
        )
        stored = replace(revision, revision_number=number)
        self.revisions[stored.revision_id] = stored
        self.current[(run.owner_id, run.project_id)] = stored.revision_id
        completed = replace(
            run,
            status=ProjectDirectorRunStatus.SUCCEEDED,
            revision_id=stored.revision_id,
            state_revision=run.state_revision + 1,
        )
        self.runs[run.run_id] = completed
        return completed

    def resolve_unknown(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        failure_code: str,
        retry_blocked: bool,
    ) -> ProjectDirectorRun:
        run = self.get_run(owner_id, run_id)
        assert run is not None
        value = replace(
            run,
            status=ProjectDirectorRunStatus.FAILED,
            failure_code=failure_code,
            retry_blocked=retry_blocked,
            state_revision=run.state_revision + 1,
        )
        unresolved = next(
            (
                item
                for item in self.units[run_id]
                if item.status is ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN
            ),
            None,
        )
        assert unresolved is not None
        self.units[run_id][unresolved.ordinal - 1] = replace(
            unresolved,
            status=ProjectDirectorUnitStatus.FAILED,
            failure_code=failure_code,
            state_revision=unresolved.state_revision + 1,
        )
        self.runs[run_id] = value
        return value

    def run_progress(self, owner_id: UUID, run_id: UUID) -> ProjectDirectorRunProgress:
        run = self.get_run(owner_id, run_id)
        assert run is not None
        units = self.units[run_id]
        failed = next(
            (
                item
                for item in units
                if item.status
                in {
                    ProjectDirectorUnitStatus.FAILED,
                    ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN,
                }
            ),
            None,
        )
        return ProjectDirectorRunProgress(
            succeeded_units=sum(
                item.status is ProjectDirectorUnitStatus.SUCCEEDED for item in units
            ),
            inflight_units=sum(
                item.status
                in {
                    ProjectDirectorUnitStatus.LEASED,
                    ProjectDirectorUnitStatus.SUBMITTING,
                }
                for item in units
            ),
            retry_pending_units=sum(
                item.status is ProjectDirectorUnitStatus.INTENT_PERSISTED and item.attempt_count > 0
                for item in units
            ),
            unsent_units=sum(
                item.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
                and item.attempt_count == 0
                for item in units
            ),
            failed_unit_ordinal=failed.ordinal if failed is not None else None,
            failed_unit_kind=failed.kind if failed is not None else None,
            auto_retries_used=run.auto_retries_used,
            auto_retry_ceiling=PROJECT_DIRECTOR_RUN_RETRY_BUDGET_FACTOR * run.total_units,
        )

    def current_revision(self, owner_id: UUID, project_id: UUID) -> ProjectDirectorRevision | None:
        revision_id = self.current.get((owner_id, project_id))
        return self.revisions.get(revision_id) if revision_id else None

    def materialization_groups(
        self,
        owner_id: UUID,
        project_id: UUID,
        snapshot_sha256: str,
        snapshot_revision: int,
    ) -> tuple[ProjectDirectorGroupContext, ...]:
        assert (owner_id, project_id) == (self.context.owner_id, self.context.project_id)
        assert (snapshot_sha256, snapshot_revision) == (
            self.context.snapshot_sha256,
            self.context.snapshot_revision,
        )
        return self.groups

    def find_materialization(
        self, owner_id: UUID, canonical_hash: str
    ) -> ProjectDirectorMaterialization | None:
        value = self.materializations.get(canonical_hash)
        return value if value is not None and value.owner_id == owner_id else None

    def persist_materialization(
        self, value: ProjectDirectorMaterialization
    ) -> ProjectDirectorMaterialization:
        existing = self.materializations.get(value.canonical_input_sha256)
        if existing is not None:
            return existing
        self.materializations[value.canonical_input_sha256] = value
        return value


def context(sentence_count: int) -> ProjectDirectorInputSnapshot:
    owner, project = uuid4(), uuid4()
    sentences = tuple(
        ProjectDirectorSentenceSnapshot(
            uuid4(),
            sequence,
            f"sentence {sequence}",
            hashlib.sha256(f"sentence {sequence}".encode()).hexdigest(),
        )
        for sequence in range(1, sentence_count + 1)
    )
    return ProjectDirectorInputSnapshot(
        owner_id=owner,
        project_id=project,
        snapshot_sha256="b" * 64,
        snapshot_revision=1,
        sentences=sentences,
        characters=(CharacterAssetSnapshot(1, uuid4(), uuid4(), "c" * 64, "image/png", 10),),
        visual_style="documentary realism",
        locale="en-US",
        aspect_ratio="16:9",
    )


def context_with_texts(texts: tuple[str, ...] | list[str]) -> ProjectDirectorInputSnapshot:
    base = context(len(texts))
    sentences = tuple(
        replace(item, text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest())
        for item, text in zip(base.sentences, texts, strict=True)
    )
    return replace(base, sentences=sentences)


def service(
    value: ProjectDirectorInputSnapshot,
    *,
    fail_on: int | None = None,
    unknown_on: int | None = None,
    structured_on: int | None = None,
    invalid_output_on: int | None = None,
    retryable_on: int | None = None,
    retry_after_seconds: float | None = None,
) -> tuple[ProjectDirectorService, MemoryRepository, Model, Clock]:
    repository = MemoryRepository(value)
    model = Model(
        fail_on=fail_on,
        unknown_on=unknown_on,
        structured_on=structured_on,
        invalid_output_on=invalid_output_on,
        retryable_on=retryable_on,
        retry_after_seconds=retry_after_seconds,
    )
    clock = Clock()
    return (
        ProjectDirectorService(
            repository,
            model,
            Ids(),
            clock,
            model_name="gpt-5.6-sol",
            parameters={"temperature": 0.2},
        ),
        repository,
        model,
        clock,
    )


def create(
    svc: ProjectDirectorService,
    value: ProjectDirectorInputSnapshot,
    key: str,
    *,
    trigger: ProjectDirectorTrigger = ProjectDirectorTrigger.NARRATION_CONFIRMATION,
) -> ProjectDirectorRun:
    return svc.create_run(
        owner_id=value.owner_id,
        project_id=value.project_id,
        idempotency_key=key,
        trigger=trigger,
    )


def complete(
    svc: ProjectDirectorService,
    value: ProjectDirectorInputSnapshot,
    run_id: UUID,
    clock: Clock | None = None,
) -> ProjectDirectorRun:
    result = svc.status(value.owner_id, run_id)
    idle = 0
    while result.status not in TERMINAL_RUN_STATUSES:
        previous = result
        result = svc.advance(value.owner_id, run_id)
        if result == previous:
            idle += 1
            assert clock is not None, "run stalled without a controllable clock"
            clock.advance_by(61)
        assert idle < 1000, "run did not reach a terminal state"
    return result


def units_of(
    repository: MemoryRepository, value: ProjectDirectorInputSnapshot, run_id: UUID
) -> tuple[ProjectDirectorUnit, ...]:
    return repository.list_units(value.owner_id, run_id)


def shot_pack_sequences(
    units: tuple[ProjectDirectorUnit, ...],
) -> list[tuple[int, ...]]:
    return [
        tuple(item.sequence for item in unit.sentences)
        for unit in units
        if unit.kind is ProjectDirectorUnitKind.SHOT_PLAN
    ]


def test_unresolved_run_blocks_new_submission_even_when_model_changes() -> None:
    value = context(2)
    svc, repository, _model, _clock = service(value)
    unresolved = create(svc, value, "original-model")
    changed_model = ProjectDirectorService(
        repository,
        Model(),
        Ids(),
        Clock(),
        model_name="gpt-5.6-sol-new-deployment",
        parameters={"temperature": 0.1},
    )

    prepared, _units = changed_model.prepare_run(
        context=value,
        idempotency_key="changed-model",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )
    assert prepared.canonical_input_sha256 != unresolved.canonical_input_sha256
    with pytest.raises(DomainError) as blocked:
        create(changed_model, value, "changed-model")

    assert blocked.value.code == "director_run_unresolved"
    assert len(repository.runs) == 1


@pytest.mark.parametrize(
    ("texts", "expected_packs"),
    [
        (["s1", "s2", "s3"], [(1, 2, 3)]),
        (["s1", "s2", "s3", "s4", "s5", "s6"], [(1, 2, 3, 4, 5, 6)]),
        (["s1", "s2", "s3", "s4", "s5", "s6", "s7"], [(1, 2, 3, 4, 5, 6, 7)]),
        (["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"], [(1, 2, 3, 4, 5, 6, 7, 8)]),
        (
            ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"],
            [(1, 2, 3, 4, 5, 6), (7, 8, 9)],
        ),
        (["s1"], [(1,)]),
        (["s1", "s2"], [(1, 2)]),
        (["s1", "---", "s2", "s3"], [(1,), (3, 4)]),
        (["x" * 500] * 6, [(1, 2, 3), (4, 5, 6)]),
        (["x" * 1300, "s2", "s3", "s4", "s5"], [(1,), (2, 3, 4, 5)]),
    ],
)
def test_unitization_packs_sections_with_bounded_size_and_tail_merge(
    texts: list[str], expected_packs: list[tuple[int, ...]]
) -> None:
    value = context_with_texts(texts)
    svc, _repository, _model, _clock = service(value)

    _run, units = svc.prepare_run(
        context=value,
        idempotency_key="unitization",
        trigger=ProjectDirectorTrigger.NARRATION_CONFIRMATION,
    )

    assert units[0].kind is ProjectDirectorUnitKind.GLOBAL_PLAN
    assert units[0].sentences == ()
    assert shot_pack_sequences(units) == expected_packs
    separators = [
        unit for unit in units if unit.kind is ProjectDirectorUnitKind.NONVISUAL_SEPARATOR
    ]
    assert [item.sentences[0].sequence for item in separators] == [
        index for index, text in enumerate(texts, start=1) if set(text) <= {"-"}
    ]
    assert [item.ordinal for item in units] == list(range(1, len(units) + 1))


def test_run_creation_builds_global_plan_and_unbound_shot_templates() -> None:
    value = context(312)
    svc, repository, model, _clock = service(value)

    run = create(svc, value, "confirmation")
    units = units_of(repository, value, run.run_id)

    assert run.total_units == 53
    assert len(units) == 53
    assert [item.ordinal for item in units] == list(range(1, 54))
    plan = units[0]
    assert plan.kind is ProjectDirectorUnitKind.GLOBAL_PLAN
    assert plan.prompt_ref == PROJECT_DIRECTOR_PLAN_PROMPT_REF
    assert [item.sentence_id for item in plan.context_before] == [
        item.sentence_id for item in value.sentences
    ]
    shot_units = [item for item in units if item.kind is ProjectDirectorUnitKind.SHOT_PLAN]
    assert len(shot_units) == 52
    assert all(len(item.sentences) == 6 for item in shot_units)
    assert all(
        item.plan_document_sha256 is None
        and GLOBAL_PLAN_DOCUMENT_PLACEHOLDER in item.prompt_content
        for item in shot_units
    )
    assert all(
        "start_ms" not in item.prompt_content and "end_ms" not in item.prompt_content
        for item in units
    )
    assert model.calls == []
    expected_canonical = _hash(
        {
            "unitization_version": PROJECT_DIRECTOR_UNITIZATION_VERSION,
            "snapshot": _snapshot_json(value),
            "prompt_ref": PROJECT_DIRECTOR_PROMPT_REF,
            "plan_prompt_ref": PROJECT_DIRECTOR_PLAN_PROMPT_REF,
            "model": "gpt-5.6-sol",
            "parameters": {"temperature": 0.2},
        }
    )
    assert run.canonical_input_sha256 == expected_canonical


def test_shot_units_stay_gated_until_the_global_plan_binds_them() -> None:
    value = context_with_texts(["s1", "s2", "---", "s3", "s4"])
    svc, repository, model, clock = service(value)
    run = create(svc, value, "gating")
    now = clock.now()

    claimable = repository.next_claimable_unit(value.owner_id, run.run_id, now)
    assert claimable is not None and claimable.kind is ProjectDirectorUnitKind.GLOBAL_PLAN

    token = uuid4()
    claimed = repository.claim_unit(
        value.owner_id, run.run_id, claimable.unit_id, token, now, now + timedelta(minutes=10)
    )
    assert claimed is not None
    gated = repository.next_claimable_unit(value.owner_id, run.run_id, now)
    assert gated is not None and gated.kind is ProjectDirectorUnitKind.NONVISUAL_SEPARATOR
    assert model.calls == []


def test_global_plan_completion_binds_shot_units_with_plan_lineage() -> None:
    value = context(12)
    svc, repository, model, _clock = service(value)
    run = create(svc, value, "binding")
    template_hashes = {
        item.unit_id: (item.prompt_sha256, item.canonical_request_sha256)
        for item in units_of(repository, value, run.run_id)
    }

    result = svc.advance(value.owner_id, run.run_id)
    units = units_of(repository, value, run.run_id)
    plan = units[0]

    assert result.status is ProjectDirectorRunStatus.RUNNING
    assert model.calls == [1]
    assert plan.status is ProjectDirectorUnitStatus.SUCCEEDED
    assert plan.plan_document is not None
    assert plan.plan_document_sha256 == hashlib.sha256(plan.plan_document.encode()).hexdigest()
    assert plan.provider_receipt == "provider-receipt"
    assert plan.raw_response_sha256 == "a" * 64
    assert plan.attempt_count == 1
    for unit in units[1:]:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            continue
        assert unit.plan_document_sha256 == plan.plan_document_sha256
        assert GLOBAL_PLAN_DOCUMENT_PLACEHOLDER not in unit.prompt_content
        assert plan.plan_document in unit.prompt_content
        assert (unit.prompt_sha256, unit.canonical_request_sha256) != template_hashes[unit.unit_id]
    claimable = repository.next_claimable_unit(
        value.owner_id, run.run_id, repository.runs[run.run_id].created_at
    )
    assert claimable is not None and claimable.kind is ProjectDirectorUnitKind.SHOT_PLAN
    assert claimable.ordinal == 2


class BadPlanModel(Model):
    def __init__(self, case: str) -> None:
        super().__init__()
        self.case = case

    def plan_payload(self, unit: ProjectDirectorUnit) -> dict[str, object]:
        payload = super().plan_payload(unit)
        chapters = payload["chapters"]
        assert isinstance(chapters, list)
        chapter = chapters[0]
        assert isinstance(chapter, dict)
        all_ids = [str(item.sentence_id) for item in unit.context_before]
        if self.case == "coverage":
            chapter["sentence_ids"] = all_ids[:-1]
        elif self.case == "duplicate":
            chapter["sentence_ids"] = [all_ids[0], all_ids[0]]
        elif self.case == "unknown":
            chapter["sentence_ids"] = [str(uuid4())]
        elif self.case == "order":
            chapter["order"] = 2
        elif self.case == "declared_locale":
            payload["output_locale"] = "ja-JP"
        elif self.case == "locale_content":
            chapter["visual_beat"] = "这是不应出现在英文输出中的秘密内容"
        elif self.case == "empty_text":
            chapter["setting"] = " "
        elif self.case == "split":
            midpoint = max(1, len(all_ids) // 2)
            payload["chapters"] = [
                {
                    "order": 1,
                    "sentence_ids": all_ids[:midpoint],
                    "visual_beat": chapter["visual_beat"],
                    "setting": chapter["setting"],
                },
                {
                    "order": 2,
                    "sentence_ids": all_ids[midpoint:],
                    "visual_beat": chapter["visual_beat"],
                    "setting": chapter["setting"],
                },
            ]
        else:  # pragma: no cover - the parametrized contract owns all cases
            raise AssertionError(self.case)
        return payload


@pytest.mark.parametrize(
    ("case", "failure_code"),
    [
        ("coverage", "director_output_plan_coverage_invalid"),
        ("duplicate", "director_output_sentence_id_duplicate"),
        ("unknown", "director_output_plan_sentence_unknown"),
        ("order", "director_output_order_invalid"),
        ("declared_locale", "director_output_locale_declared_mismatch"),
        ("locale_content", "director_output_locale_content_mismatch"),
        ("empty_text", "director_output_text_empty"),
    ],
)
def test_invalid_global_plan_is_retried_then_terminal_with_stable_code(
    case: str, failure_code: str
) -> None:
    value = context(3)
    model = BadPlanModel(case)
    repository = MemoryRepository(value)
    clock = Clock()
    svc = ProjectDirectorService(
        repository,
        model,
        Ids(),
        clock,
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )
    run = create(svc, value, f"plan-{case}")

    before = clock.now()
    first = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[0]

    assert first.status is ProjectDirectorRunStatus.RUNNING
    assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert unit.attempt_count == 1
    assert unit.last_failure_code == failure_code
    assert unit.next_attempt_at == before + timedelta(seconds=5)
    assert "SECRET" not in failure_code

    clock.advance_by(5)
    before = clock.now()
    svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[0]
    assert unit.attempt_count == 2
    assert unit.next_attempt_at == before + timedelta(seconds=15)

    clock.advance_by(15)
    third = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[0]
    assert third.status is ProjectDirectorRunStatus.FAILED
    assert third.failure_code == failure_code
    assert unit.status is ProjectDirectorUnitStatus.FAILED
    assert unit.attempt_count == 3
    assert model.calls == [1, 1, 1]
    assert svc.advance(value.owner_id, run.run_id) == third
    assert model.calls == [1, 1, 1]


def test_partitioned_global_plan_is_accepted() -> None:
    value = context(12)
    model = BadPlanModel("split")
    repository = MemoryRepository(value)
    svc = ProjectDirectorService(
        repository,
        model,
        Ids(),
        Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )
    run = create(svc, value, "plan-split")

    result = svc.advance(value.owner_id, run.run_id)

    assert result.status is ProjectDirectorRunStatus.RUNNING
    plan = units_of(repository, value, run.run_id)[0]
    assert plan.status is ProjectDirectorUnitStatus.SUCCEEDED
    assert plan.plan_document is not None and '"order":2' in plan.plan_document


def test_structured_response_failure_retries_with_backoff_and_keeps_safe_evidence() -> None:
    value = context(3)
    svc, repository, model, clock = service(value, structured_on=2)
    run = create(svc, value, "structured-retry")
    svc.advance(value.owner_id, run.run_id)

    before = clock.now()
    svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]

    assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert unit.attempt_count == 1
    assert unit.last_failure_code == "director_response_content_json_invalid"
    assert unit.next_attempt_at == before + timedelta(seconds=5)
    assert unit.provider_receipt == "provider-structured-receipt"
    assert unit.raw_response_sha256 == "d" * 64
    assert repository.runs[run.run_id].auto_retries_used == 1
    assert repository.response_diagnostics[unit.unit_id] == {
        "http_status": 200,
        "content_type": "application/json",
        "request_id_present": True,
        "request_id_source": "header",
        "raw_size_bytes": 41,
        "raw_response_sha256": "d" * 64,
        "parse_stage": "content_json",
        "exception_class": "JSONDecodeError",
    }

    clock.advance_by(5)
    before = clock.now()
    svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]
    assert unit.attempt_count == 2
    assert unit.next_attempt_at == before + timedelta(seconds=15)

    clock.advance_by(15)
    failed = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]
    assert failed.status is ProjectDirectorRunStatus.FAILED
    assert failed.failure_code == "director_response_content_json_invalid"
    assert failed.auto_retries_used == 2
    assert unit.status is ProjectDirectorUnitStatus.FAILED
    assert unit.attempt_count == 3
    assert unit.failure_code == "director_response_content_json_invalid"
    assert model.calls == [1, 2, 2, 2]
    assert svc.advance(value.owner_id, run.run_id) == failed
    assert model.calls == [1, 2, 2, 2]


def test_retryable_provider_error_respects_retry_after_header() -> None:
    value = context(3)
    svc, repository, model, clock = service(value, retryable_on=2, retry_after_seconds=20.0)
    run = create(svc, value, "throttled")
    svc.advance(value.owner_id, run.run_id)

    before = clock.now()
    result = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]

    assert result.status is ProjectDirectorRunStatus.RUNNING
    assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert unit.attempt_count == 1
    assert unit.last_failure_code == "provider_http_429"
    assert unit.next_attempt_at == before + timedelta(seconds=20)

    clock.advance_by(19)
    assert repository.next_claimable_unit(value.owner_id, run.run_id, clock.now()) is None
    model.retryable_on = None
    clock.advance_by(1)
    completed = complete(svc, value, run.run_id, clock)
    assert completed.status is ProjectDirectorRunStatus.SUCCEEDED
    assert model.calls == [1, 2, 2]


def test_permanent_provider_error_is_terminal_without_consuming_retry_budget() -> None:
    value = context(12)
    svc, _repository, model, _clock = service(value, fail_on=2)
    run = create(svc, value, "permanent")

    result = complete(svc, value, run.run_id)

    assert result.status is ProjectDirectorRunStatus.FAILED
    assert result.failure_code == "provider_http_400"
    assert result.auto_retries_used == 0
    assert model.calls == [1, 2]
    assert svc.advance(value.owner_id, run.run_id) == result
    assert model.calls == [1, 2]


def test_unknown_exception_marks_remote_state_unknown_without_retry() -> None:
    value = context(3)
    svc, repository, model, _clock = service(value, unknown_on=2)
    run = create(svc, value, "unknown")
    svc.advance(value.owner_id, run.run_id)

    result = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]

    assert result.status is ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
    assert result.failure_code == "TimeoutError"
    assert result.auto_retries_used == 0
    assert unit.status is ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN
    assert unit.attempt_count == 1
    assert unit.next_attempt_at is None
    assert model.calls == [1, 2]
    assert svc.advance(value.owner_id, run.run_id) == result
    assert model.calls == [1, 2]


def test_retry_budget_exhaustion_fails_terminally_with_the_real_code() -> None:
    value = context(3)
    svc, repository, model, _clock = service(value, structured_on=2)
    run = create(svc, value, "budget")
    svc.advance(value.owner_id, run.run_id)
    stored = repository.runs[run.run_id]
    ceiling = PROJECT_DIRECTOR_RUN_RETRY_BUDGET_FACTOR * stored.total_units
    repository.runs[run.run_id] = replace(stored, auto_retries_used=ceiling)

    result = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]

    assert result.status is ProjectDirectorRunStatus.FAILED
    assert result.failure_code == "director_response_content_json_invalid"
    assert result.auto_retries_used == ceiling
    assert unit.status is ProjectDirectorUnitStatus.FAILED
    assert unit.attempt_count == 1
    assert unit.failure_code == "director_response_content_json_invalid"
    assert unit.provider_receipt == "provider-structured-receipt"
    assert model.calls == [1, 2]


def test_auto_mode_retry_ignores_round_wide_budget() -> None:
    value = context(3)
    svc, repository, model, _clock = service(value, structured_on=2)
    repository.production_mode = "AUTO"
    run = create(svc, value, "auto-budget")
    svc.advance(value.owner_id, run.run_id)
    stored = repository.runs[run.run_id]
    ceiling = PROJECT_DIRECTOR_RUN_RETRY_BUDGET_FACTOR * stored.total_units
    repository.runs[run.run_id] = replace(stored, auto_retries_used=ceiling + 10)

    result = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]

    assert result.status is ProjectDirectorRunStatus.RUNNING
    assert result.auto_retries_used == ceiling + 11
    assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert unit.attempt_count == 1
    assert unit.last_failure_code == "director_response_content_json_invalid"
    assert unit.provider_receipt == "provider-structured-receipt"
    assert model.calls == [1, 2]


def test_auto_mode_unknown_exception_retries_then_fails_with_stable_code() -> None:
    value = context(3)
    svc, repository, model, clock = service(value, unknown_on=2)
    repository.production_mode = "AUTO"
    run = create(svc, value, "auto-unknown")
    svc.advance(value.owner_id, run.run_id)

    before = clock.now()
    result = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]

    assert result.status is ProjectDirectorRunStatus.RUNNING
    assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert unit.attempt_count == 1
    assert unit.last_failure_code == "TimeoutError"
    assert unit.next_attempt_at == before + timedelta(seconds=5)
    assert repository.runs[run.run_id].auto_retries_used == 1

    clock.advance_by(5)
    before = clock.now()
    svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]
    assert unit.attempt_count == 2
    assert unit.last_failure_code == "TimeoutError"
    assert unit.next_attempt_at == before + timedelta(seconds=15)

    clock.advance_by(15)
    failed = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]
    assert failed.status is ProjectDirectorRunStatus.FAILED
    assert failed.failure_code == PROJECT_DIRECTOR_UNKNOWN_EXHAUSTED_FAILURE
    assert failed.auto_retries_used == 2
    assert unit.status is ProjectDirectorUnitStatus.FAILED
    assert unit.attempt_count == 3
    assert unit.failure_code == PROJECT_DIRECTOR_UNKNOWN_EXHAUSTED_FAILURE
    assert model.calls == [1, 2, 2, 2]
    assert svc.advance(value.owner_id, run.run_id) == failed
    assert model.calls == [1, 2, 2, 2]


def test_auto_mode_stale_submitting_unit_retries_then_fails_with_stable_code() -> None:
    value = context(3)
    svc, repository, model, clock = service(value)
    repository.production_mode = "AUTO"
    run = create(svc, value, "auto-stale")
    svc.advance(value.owner_id, run.run_id)

    failed: ProjectDirectorRun | None = None
    for attempt in range(1, 4):
        now = clock.now()
        claimable = repository.next_claimable_unit(value.owner_id, run.run_id, now)
        assert claimable is not None and claimable.ordinal == 2
        claimed = repository.claim_unit(
            value.owner_id,
            run.run_id,
            claimable.unit_id,
            uuid4(),
            now,
            now + timedelta(minutes=10),
        )
        assert claimed is not None
        assert repository.mark_unit_call_started(claimed) is not None
        clock.advance_by(601)
        before = clock.now()
        result = svc.advance(value.owner_id, run.run_id)
        unit = units_of(repository, value, run.run_id)[1]
        if attempt < 3:
            assert result.status is ProjectDirectorRunStatus.RUNNING
            assert unit.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
            assert unit.attempt_count == attempt
            assert unit.last_failure_code == "worker_lost_after_call_started"
            assert unit.next_attempt_at == before + timedelta(seconds=(5, 15)[attempt - 1])
            clock.advance_by((5, 15)[attempt - 1])
        else:
            failed = result
            assert failed.status is ProjectDirectorRunStatus.FAILED
            assert failed.failure_code == PROJECT_DIRECTOR_UNKNOWN_EXHAUSTED_FAILURE
            assert failed.auto_retries_used == 2
            assert unit.status is ProjectDirectorUnitStatus.FAILED
            assert unit.attempt_count == 3
            assert unit.failure_code == PROJECT_DIRECTOR_UNKNOWN_EXHAUSTED_FAILURE

    assert failed is not None
    assert model.calls == [1]
    assert svc.advance(value.owner_id, run.run_id) == failed
    assert model.calls == [1]


def test_manual_mode_stale_submitting_unit_marks_remote_state_unknown() -> None:
    value = context(3)
    svc, repository, model, clock = service(value)
    run = create(svc, value, "manual-stale")
    svc.advance(value.owner_id, run.run_id)
    now = clock.now()
    claimable = repository.next_claimable_unit(value.owner_id, run.run_id, now)
    assert claimable is not None and claimable.ordinal == 2
    claimed = repository.claim_unit(
        value.owner_id, run.run_id, claimable.unit_id, uuid4(), now, now + timedelta(minutes=10)
    )
    assert claimed is not None
    assert repository.mark_unit_call_started(claimed) is not None

    clock.advance_by(601)
    result = svc.advance(value.owner_id, run.run_id)
    unit = units_of(repository, value, run.run_id)[1]

    assert result.status is ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
    assert result.failure_code == "worker_lost_after_call_started"
    assert result.auto_retries_used == 0
    assert unit.status is ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN
    assert unit.attempt_count == 1
    assert unit.next_attempt_at is None
    assert model.calls == [1]
    assert svc.advance(value.owner_id, run.run_id) == result
    assert model.calls == [1]


def test_inflight_unit_results_still_land_after_run_terminal() -> None:
    value = context(12)
    svc, repository, model, clock = service(value)
    run = create(svc, value, "inflight")
    svc.advance(value.owner_id, run.run_id)
    now = clock.now()
    claimable = repository.next_claimable_unit(value.owner_id, run.run_id, now)
    assert claimable is not None and claimable.ordinal == 2
    claimed = repository.claim_unit(
        value.owner_id, run.run_id, claimable.unit_id, uuid4(), now, now + timedelta(minutes=10)
    )
    assert claimed is not None
    submitting = repository.mark_unit_call_started(claimed)
    assert submitting is not None
    model.fail_on = 3

    failed = svc.advance(value.owner_id, run.run_id)

    assert failed.status is ProjectDirectorRunStatus.FAILED
    assert failed.failure_code == "provider_http_400"
    shot = DirectorShot(
        uuid4(),
        1,
        "inflight scene",
        "WIDE",
        "EYE_LEVEL",
        "event → outcome",
        "function",
        "Inflight description.",
        DirectorSemanticEvent.PERFORM_ACTION,
        DirectorSubjectFocus.PRIMARY_SUBJECT,
        DirectorTimeRelation.LATER,
        DirectorLocationRelation.SAME_LOCATION,
    )
    links = tuple(ShotSentenceLink(shot.shot_id, item.sentence_id) for item in submitting.sentences)
    landed = repository.complete_unit(submitting, (shot,), links, "late-receipt", "e" * 64)
    assert landed.status is ProjectDirectorRunStatus.FAILED
    assert landed.failure_code == "provider_http_400"
    landed_unit = units_of(repository, value, run.run_id)[1]
    assert landed_unit.status is ProjectDirectorUnitStatus.SUCCEEDED
    assert landed_unit.provider_receipt == "late-receipt"
    assert repository.runs[run.run_id] == landed

    failed_landing = repository.fail_unit(
        replace(submitting, state_revision=submitting.state_revision + 1),
        failure_code="provider_http_500",
        remote_state_unknown=False,
    )
    assert failed_landing.status is ProjectDirectorRunStatus.FAILED
    assert failed_landing.failure_code == "provider_http_400"


def test_progress_reports_unit_buckets_and_retry_budget() -> None:
    value = context(3)
    svc, repository, model, clock = service(value, structured_on=2)
    run = create(svc, value, "progress")

    progress = svc.progress(value.owner_id, run.run_id)
    assert progress == ProjectDirectorRunProgress(
        succeeded_units=0,
        inflight_units=0,
        retry_pending_units=0,
        unsent_units=2,
        failed_unit_ordinal=None,
        failed_unit_kind=None,
        auto_retries_used=0,
        auto_retry_ceiling=4,
    )

    svc.advance(value.owner_id, run.run_id)
    progress = svc.progress(value.owner_id, run.run_id)
    assert progress.succeeded_units == 1
    assert progress.unsent_units == 1

    svc.advance(value.owner_id, run.run_id)
    progress = svc.progress(value.owner_id, run.run_id)
    assert progress.retry_pending_units == 1
    assert progress.unsent_units == 0
    assert progress.auto_retries_used == 1
    assert progress.failed_unit_ordinal is None

    clock.advance_by(5)
    svc.advance(value.owner_id, run.run_id)
    clock.advance_by(15)
    failed = svc.advance(value.owner_id, run.run_id)
    assert failed.status is ProjectDirectorRunStatus.FAILED
    progress = svc.progress(value.owner_id, run.run_id)
    assert progress.retry_pending_units == 0
    assert progress.failed_unit_ordinal == 2
    assert progress.failed_unit_kind is ProjectDirectorUnitKind.SHOT_PLAN
    assert model.calls == [1, 2, 2, 2]

    with pytest.raises(DomainError) as missing:
        svc.progress(uuid4(), run.run_id)
    assert missing.value.code == "not_found"
    assert repository.runs[run.run_id].auto_retries_used == 2


@pytest.mark.parametrize("failure_kind", ("structured", "output", "permanent", "legacy"))
def test_recovery_reuses_plan_and_succeeded_units_and_regenerates_holes(
    failure_kind: str,
) -> None:
    value = context_with_texts(["s1", "s2", "---", "s3", "s4", "s5", "---", "s6", "s7"])
    svc, repository, model, clock = service(
        value,
        structured_on=4 if failure_kind in {"structured", "legacy"} else None,
        invalid_output_on=4 if failure_kind == "output" else None,
        fail_on=4 if failure_kind == "permanent" else None,
    )
    source_run = create(svc, value, f"source-{failure_kind}")
    source = complete(svc, value, source_run.run_id, clock)
    expected_code = {
        "structured": "director_response_content_json_invalid",
        "output": "director_output_shot_count_invalid",
        "permanent": "provider_http_400",
        "legacy": "director_response_content_json_invalid",
    }[failure_kind]
    if failure_kind == "legacy":
        repository.runs[source_run.run_id] = replace(
            source, failure_code="invalid_structured_response"
        )
        repository.units[source_run.run_id][3] = replace(
            repository.units[source_run.run_id][3],
            failure_code="invalid_structured_response",
        )
        source = repository.runs[source_run.run_id]
    source_units = units_of(repository, value, source_run.run_id)
    source_snapshot = source
    calls_before = tuple(model.calls)
    model.structured_on = None
    model.invalid_output_on = None
    model.fail_on = None

    assert source.status is ProjectDirectorRunStatus.FAILED
    assert source.failure_code == (
        "invalid_structured_response" if failure_kind == "legacy" else expected_code
    )
    expected_statuses = [
        ProjectDirectorUnitStatus.SUCCEEDED,
        ProjectDirectorUnitStatus.SUCCEEDED,
        ProjectDirectorUnitStatus.SUCCEEDED,
        ProjectDirectorUnitStatus.FAILED,
        ProjectDirectorUnitStatus.SUCCEEDED
        if failure_kind != "permanent"
        else ProjectDirectorUnitStatus.INTENT_PERSISTED,
        ProjectDirectorUnitStatus.SUCCEEDED
        if failure_kind != "permanent"
        else ProjectDirectorUnitStatus.INTENT_PERSISTED,
    ]
    assert [item.status for item in source_units] == expected_statuses

    recovery = create(
        svc,
        value,
        f"retry-{failure_kind}",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )
    recovery_units = units_of(repository, value, recovery.run_id)

    assert recovery.recovery_source_run_id == source_run.run_id
    assert tuple(model.calls) == calls_before
    plan = recovery_units[0]
    assert plan.status is ProjectDirectorUnitStatus.SUCCEEDED
    assert plan.reused_from_unit_id == source_units[0].unit_id
    assert plan.plan_document == source_units[0].plan_document
    assert plan.plan_document_sha256 == source_units[0].plan_document_sha256
    assert plan.provider_receipt == source_units[0].provider_receipt
    expected_reuse = [
        source_units[0].unit_id,
        source_units[1].unit_id,
        source_units[2].unit_id,
        None,
        source_units[4].unit_id if failure_kind != "permanent" else None,
        source_units[5].unit_id if failure_kind != "permanent" else None,
    ]
    assert [item.reused_from_unit_id for item in recovery_units] == expected_reuse
    for recovered, previous in zip(recovery_units, source_units, strict=True):
        if recovered.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            continue
        assert recovered.plan_document_sha256 == plan.plan_document_sha256
        assert GLOBAL_PLAN_DOCUMENT_PLACEHOLDER not in recovered.prompt_content
        assert recovered.prompt_sha256 == previous.prompt_sha256
        assert recovered.canonical_request_sha256 == previous.canonical_request_sha256
    hole = recovery_units[3]
    assert hole.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert hole.attempt_count == 0
    assert hole.shots == () and hole.sentence_links == ()

    completed = complete(svc, value, recovery.run_id, clock)

    assert completed.status is ProjectDirectorRunStatus.SUCCEEDED
    expected_new_calls = [4] if failure_kind != "permanent" else [4, 6]
    assert model.calls[len(calls_before) :] == expected_new_calls
    assert repository.runs[source_run.run_id] == source_snapshot
    revision = svc.current(value.owner_id, value.project_id)
    assert {item.sentence_id for item in revision.sentence_links} == {
        item.sentence_id for item in value.sentences
    }


def test_recovery_with_failed_plan_regenerates_every_unit() -> None:
    value = context(6)
    svc, repository, model, _clock = service(value, fail_on=1)
    source_run = create(svc, value, "plan-failed")
    source = complete(svc, value, source_run.run_id)
    assert source.status is ProjectDirectorRunStatus.FAILED
    assert source.failure_code == "provider_http_400"
    assert units_of(repository, value, source_run.run_id)[0].status is (
        ProjectDirectorUnitStatus.FAILED
    )
    model.fail_on = None

    recovery = create(
        svc,
        value,
        "plan-retry",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )
    recovery_units = units_of(repository, value, recovery.run_id)

    assert recovery.recovery_source_run_id == source_run.run_id
    assert all(item.status is ProjectDirectorUnitStatus.INTENT_PERSISTED for item in recovery_units)
    assert all(item.reused_from_unit_id is None for item in recovery_units)
    assert all(item.plan_document_sha256 is None for item in recovery_units)
    assert GLOBAL_PLAN_DOCUMENT_PLACEHOLDER in recovery_units[1].prompt_content

    completed = complete(svc, value, recovery.run_id)

    assert completed.status is ProjectDirectorRunStatus.SUCCEEDED
    assert model.calls == [1, 1, 2]


@pytest.mark.parametrize("corruption", ("receipt", "inflight", "binding"))
def test_recovery_rejects_tampered_source_without_creating_run(corruption: str) -> None:
    value = context_with_texts(["s1", "s2", "---", "s3", "s4", "s5", "---", "s6", "s7"])
    svc, repository, _model, _clock = service(value, fail_on=4)
    source_run = create(svc, value, f"tampered-{corruption}")
    complete(svc, value, source_run.run_id)
    stored = repository.units[source_run.run_id]
    if corruption == "receipt":
        stored[1] = replace(stored[1], provider_receipt=None)
    elif corruption == "inflight":
        stored[5] = replace(stored[5], status=ProjectDirectorUnitStatus.LEASED)
    else:
        stored[1] = replace(stored[1], plan_document_sha256="0" * 64)
    run_ids_before = set(repository.runs)

    with pytest.raises(DomainError) as invalid:
        create(
            svc,
            value,
            f"invalid-{corruption}",
            trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
        )

    expected_code = (
        "director_run_unresolved"
        if corruption == "inflight"
        else "director_recovery_source_invalid"
    )
    assert invalid.value.code == expected_code
    assert set(repository.runs) == run_ids_before
    assert (value.owner_id, f"invalid-{corruption}") not in repository.run_keys


def test_non_whitelisted_failure_disables_prefix_recovery() -> None:
    value = context(6)
    svc, repository, _model, _clock = service(value, fail_on=1)
    source = complete(svc, value, create(svc, value, "non-whitelisted").run_id)
    repository.runs[source.run_id] = replace(source, failure_code="provider_boom")

    retry = create(
        svc,
        value,
        "non-whitelisted-retry",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )

    assert retry.recovery_source_run_id is None
    assert all(
        item.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
        for item in units_of(repository, value, retry.run_id)
    )


@pytest.mark.parametrize(
    ("resolution", "retry_allowed"),
    [("CONFIRMED_NOT_ACCEPTED", True), ("CONFIRMED_FAILED", False)],
)
def test_unknown_stops_and_manual_evidence_controls_new_run(
    resolution: str, retry_allowed: bool
) -> None:
    value = context(2)
    svc, _repository, model, _clock = service(value, unknown_on=1)
    run = create(svc, value, "unknown")
    uncertain = complete(svc, value, run.run_id)
    assert uncertain.status is ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
    assert model.calls == [1]

    resolved = svc.resolve_unknown(
        value.owner_id, run.run_id, resolution, "Provider console evidence"
    )
    assert resolved.status is ProjectDirectorRunStatus.FAILED
    assert resolved.retry_blocked is not retry_allowed
    assert (
        svc.resolve_unknown(
            value.owner_id,
            run.run_id,
            resolution,
            "Provider console evidence",
        )
        == resolved
    )
    with pytest.raises(DomainError) as changed_evidence:
        svc.resolve_unknown(
            value.owner_id,
            run.run_id,
            resolution,
            "Different provider evidence",
        )
    assert changed_evidence.value.code == "reconciliation_not_allowed"
    other_resolution = (
        "CONFIRMED_FAILED" if resolution == "CONFIRMED_NOT_ACCEPTED" else "CONFIRMED_NOT_ACCEPTED"
    )
    with pytest.raises(DomainError) as changed_resolution:
        svc.resolve_unknown(
            value.owner_id,
            run.run_id,
            other_resolution,
            "Provider console evidence",
        )
    assert changed_resolution.value.code == "reconciliation_not_allowed"
    if retry_allowed:
        assert (
            create(
                svc,
                value,
                "manual-retry",
                trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
            ).status
            is ProjectDirectorRunStatus.INTENT_PERSISTED
        )
    else:
        with pytest.raises(DomainError) as blocked:
            create(
                svc,
                value,
                "manual-retry",
                trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
            )
        assert blocked.value.code == "director_retry_blocked"


def test_confirmed_not_accepted_recovery_reuses_verified_units() -> None:
    value = context(12)
    svc, repository, model, _clock = service(value)
    baseline = complete(svc, value, create(svc, value, "baseline").run_id)
    baseline_current = svc.current(value.owner_id, value.project_id)
    model.unknown_on = 3
    source_run = create(
        svc,
        value,
        "uncertain",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )
    uncertain = complete(svc, value, source_run.run_id)
    source_units = units_of(repository, value, source_run.run_id)

    assert baseline.status is ProjectDirectorRunStatus.SUCCEEDED
    assert uncertain.status is ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
    assert [item.status for item in source_units] == [
        ProjectDirectorUnitStatus.SUCCEEDED,
        ProjectDirectorUnitStatus.SUCCEEDED,
        ProjectDirectorUnitStatus.REMOTE_STATE_UNKNOWN,
    ]
    resolved = svc.resolve_unknown(
        value.owner_id,
        source_run.run_id,
        "CONFIRMED_NOT_ACCEPTED",
        "Provider console proves request 3 was not accepted",
    )
    calls_before_recovery = tuple(model.calls)
    model.unknown_on = None

    recovery_run = create(
        svc,
        value,
        "recovery",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )
    recovery_units = units_of(repository, value, recovery_run.run_id)

    assert resolved.status is ProjectDirectorRunStatus.FAILED
    assert recovery_run.recovery_source_run_id == source_run.run_id
    assert tuple(model.calls) == calls_before_recovery
    assert svc.current(value.owner_id, value.project_id) == baseline_current
    for recovered, source in zip(recovery_units[:2], source_units[:2], strict=True):
        assert recovered.status is ProjectDirectorUnitStatus.SUCCEEDED
        assert recovered.reused_from_unit_id == source.unit_id
        assert recovered.provider_idempotency_key != source.provider_idempotency_key
        assert recovered.provider_receipt == source.provider_receipt
        assert recovered.raw_response_sha256 == source.raw_response_sha256
        assert recovered.shots == source.shots
        assert recovered.sentence_links == source.sentence_links
    fresh = recovery_units[2]
    assert fresh.status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert fresh.reused_from_unit_id is None
    assert fresh.plan_document_sha256 == source_units[2].plan_document_sha256
    assert fresh.prompt_sha256 == source_units[2].prompt_sha256
    assert fresh.canonical_request_sha256 == source_units[2].canonical_request_sha256

    recovered_run = complete(svc, value, recovery_run.run_id)
    recovered_current = svc.current(value.owner_id, value.project_id)

    assert recovered_run.status is ProjectDirectorRunStatus.SUCCEEDED
    assert model.calls[len(calls_before_recovery) :] == [3]
    assert repository.runs[source_run.run_id] == resolved
    assert recovered_current.run_id == recovery_run.run_id
    assert recovered_current.revision_number == 2
    assert recovered_current.revision_id != baseline_current.revision_id


def test_manual_success_creates_new_immutable_revision_and_only_switches_current_on_success() -> (
    None
):
    value = context(2)
    svc, repository, _model, _clock = service(value)
    first = complete(svc, value, create(svc, value, "first").run_id)
    first_current = svc.current(value.owner_id, value.project_id)

    second_run = create(svc, value, "manual")
    assert svc.current(value.owner_id, value.project_id) == first_current
    second = complete(svc, value, second_run.run_id)
    second_current = svc.current(value.owner_id, value.project_id)

    assert first.status is second.status is ProjectDirectorRunStatus.SUCCEEDED
    assert first_current.revision_number == 1
    assert second_current.revision_number == 2
    assert first_current.revision_id != second_current.revision_id
    assert len(repository.revisions) == 2


class SplitShotModel(Model):
    """Returns two sentence-scoped shots for the second shot plan unit."""

    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN or unit.ordinal != 4:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        first, second = (item.sentence_id for item in unit.sentences)
        shots = [
            shot_json(unit, 1, 100, links=(first,)),
            shot_json(unit, 2, 101, links=(second,)),
        ]
        return Result({"output_locale": unit.required_locale, "shots": shots})


def test_finalize_merges_multi_unit_shots_and_anchors_separators() -> None:
    value = context_with_texts(["s1", "s2", "---", "s3", "s4"])
    repository = MemoryRepository(value)
    svc = ProjectDirectorService(
        repository,
        SplitShotModel(),
        Ids(),
        Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )

    run = complete(svc, value, create(svc, value, "merge").run_id)

    assert run.status is ProjectDirectorRunStatus.SUCCEEDED
    revision = svc.current(value.owner_id, value.project_id)
    assert [shot.order for shot in revision.shots] == [1, 2, 3]
    separator = value.sentences[2].sentence_id
    first, second = (item.sentence_id for item in value.sentences[:2])
    third, fourth = (item.sentence_id for item in value.sentences[3:])
    links_by_shot = {
        shot.shot_id: {
            item.sentence_id for item in revision.sentence_links if item.shot_id == shot.shot_id
        }
        for shot in revision.shots
    }
    assert links_by_shot[revision.shots[0].shot_id] == {first, second}
    assert links_by_shot[revision.shots[1].shot_id] == {third, separator}
    assert links_by_shot[revision.shots[2].shot_id] == {fourth}


def test_deterministic_materialization_maps_stable_sentence_ids_without_provider_calls() -> None:
    value = context(2)
    svc, repository, model, _clock = service(value)
    complete(svc, value, create(svc, value, "complete").run_id)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(), 1, tuple(item.sentence_id for item in value.sentences)
        ),
    )
    provider_calls = tuple(model.calls)

    result = svc.materialize(value.owner_id, value.project_id)
    replay = svc.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.SUCCEEDED
    assert result == replay
    assert tuple(model.calls) == provider_calls
    candidate = result.candidates[0]
    assert candidate.final_revision is not None
    assert candidate.final_revision.group_sentence_ids == tuple(
        item.sentence_id for item in value.sentences
    )


def test_six_mapped_shots_pass_without_convergence_and_become_final() -> None:
    """§24：组级上限为 6——6 镜切片不触发收敛（恒等），无冲突时直接生成最终修订。"""
    value = context(6)
    repository = MemoryRepository(value)
    svc = ProjectDirectorService(
        repository,
        PerSentenceModel(),
        Ids(),
        Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )
    complete(svc, value, create(svc, value, "complete").run_id)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(), 1, tuple(item.sentence_id for item in value.sentences)
        ),
    )

    result = svc.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.SUCCEEDED
    candidate = result.candidates[0]
    assert len(candidate.shots) == 6
    assert [shot.order for shot in candidate.shots] == [1, 2, 3, 4, 5, 6]
    assert candidate.conflicts == ()
    assert candidate.review_state is ReviewState.NOT_REQUIRED
    assert candidate.final_revision is not None
    # 恒等：来源与候选一一对应（无多父血缘）
    assert len(candidate.source_shot_links) == 6
    assert len({item.candidate_shot_id for item in candidate.source_shot_links}) == 6


def test_seven_mapped_shots_converge_to_six_with_multi_parent_lineage() -> None:
    """§24（adjacent-merge-v1）：7 镜切片确定性收敛为 6 镜后生成最终修订。

    合并保留前镜身份；后镜来源镜头血缘重定向到保留镜头（多父），句子链接
    并集去重，镜头序号重排连续——收敛结果直接满足播种与生成合同。
    """
    value = context(7)
    repository = MemoryRepository(value)
    svc = ProjectDirectorService(
        repository,
        PerSentenceModel(),
        Ids(),
        Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )
    complete(svc, value, create(svc, value, "complete").run_id)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(), 1, tuple(item.sentence_id for item in value.sentences)
        ),
    )

    result = svc.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.SUCCEEDED
    candidate = result.candidates[0]
    assert len(candidate.shots) == 6
    assert [shot.order for shot in candidate.shots] == [1, 2, 3, 4, 5, 6]
    assert candidate.conflicts == ()
    assert candidate.review_state is ReviewState.NOT_REQUIRED
    # 收敛审计：7 条来源血缘全部保留，恰有一对合并（同一保留镜头承载 2 个来源）
    lineage_candidates = [item.candidate_shot_id for item in candidate.source_shot_links]
    assert len(lineage_candidates) == 7
    assert sorted(
        lineage_candidates.count(shot_id) for shot_id in set(lineage_candidates)
    ) == [1, 1, 1, 1, 1, 2]
    final = candidate.final_revision
    assert final is not None
    assert len(final.shots) == 6
    # 句子覆盖不因合并丢失：最终修订覆盖全部 7 句
    assert {link.sentence_id for link in final.sentence_links} == {
        item.sentence_id for item in value.sentences
    }


def test_source_quality_conflict_fails_closed_and_never_creates_storyboard_ready_revision() -> None:
    value = context(12)
    repository = MemoryRepository(value)
    model = Model()
    original_generate = model.generate

    def same_composition(unit: ProjectDirectorUnit) -> Result:
        result = original_generate(unit)
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return result
        shot = result.payload["shots"][0]
        assert isinstance(shot, dict)
        shot.update(
            {
                "scene": "same",
                "shot_size": "WIDE",
                "camera_viewpoint": "EYE_LEVEL",
                "action": "same",
                "outcome": "same",
                "narrative_function": "same",
                "semantic_event": "PERFORM_ACTION",
                "subject_focus": "PRIMARY_SUBJECT",
                "time_relation": "SAME_MOMENT",
                "location_relation": "SAME_LOCATION",
            }
        )
        return result

    model.generate = same_composition  # type: ignore[method-assign]
    svc = ProjectDirectorService(
        repository,
        model,
        Ids(),
        Clock(),
        model_name="gpt-5.6-sol",
        parameters={"temperature": 0.2},
    )
    complete(svc, value, create(svc, value, "complete").run_id)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(), 1, tuple(item.sentence_id for item in value.sentences)
        ),
    )

    result = svc.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.REVIEW_REQUIRED
    candidate = result.candidates[0]
    assert candidate.final_revision is None
    assert "source_preplan_quality_conflict" in {item.code for item in candidate.conflicts}
    assert candidate.review_state is ReviewState.REQUIRED


def test_materialization_domain_allows_empty_candidates_only_for_review_required() -> None:
    base = dict(
        materialization_id=uuid4(),
        owner_id=uuid4(),
        project_id=uuid4(),
        preplan_revision_id=uuid4(),
        snapshot_sha256="a" * 64,
        snapshot_revision=1,
        algorithm_version="controlled-semantic-lineage-remap-v3",
        canonical_input_sha256="b" * 64,
        candidates=(),
        created_at=datetime.now(UTC),
    )
    # REVIEW_REQUIRED + 零候选：合法（映射全数失败需人工处理，可加载展示）。
    value = ProjectDirectorMaterialization(status=MaterializationStatus.REVIEW_REQUIRED, **base)
    assert value.candidates == ()
    # 其余状态零候选 = 不完整写入痕迹，拒绝加载。
    with pytest.raises(ValueError, match="materialization is incomplete"):
        ProjectDirectorMaterialization(status=MaterializationStatus.SUCCEEDED, **base)


def test_manual_unknown_recovery_waives_evidence_and_reuses_successful_units() -> None:
    value = context(12)
    svc, repository, model, _clock = service(value, unknown_on=3)
    source = create(svc, value, "waive-source")
    uncertain = complete(svc, value, source.run_id)
    assert uncertain.status is ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN
    successful = tuple(units_of(repository, value, source.run_id)[:2])
    assert model.calls == [1, 2, 3]
    model.unknown_on = None

    recovered = create(
        svc, value, "waive-recovery", trigger=ProjectDirectorTrigger.MANUAL_REGENERATE
    )
    assert recovered.recovery_source_run_id == source.run_id
    assert model.calls == [1, 2, 3]
    assert tuple(units_of(repository, value, source.run_id)[:2]) == successful
    assert [unit.reused_from_unit_id for unit in units_of(repository, value, recovered.run_id)] == [
        successful[0].unit_id, successful[1].unit_id, None
    ]
    assert complete(svc, value, recovered.run_id).status is ProjectDirectorRunStatus.SUCCEEDED
    assert model.calls == [1, 2, 3, 3]


def test_manual_changed_input_requires_explicit_confirmation() -> None:
    value = context(2)
    svc, repository, model, _clock = service(value, unknown_on=1)
    source = create(svc, value, "changed-source")
    complete(svc, value, source.run_id)
    svc._model_name = "new-director-model"
    with pytest.raises(DomainError) as changed:
        create(svc, value, "changed-recovery", trigger=ProjectDirectorTrigger.MANUAL_REGENERATE)
    assert changed.value.code == "director_input_changed"
    assert len(repository.runs) == 1
    assert model.calls == [1]

    restarted = svc.create_run(
        owner_id=value.owner_id,
        project_id=value.project_id,
        idempotency_key="changed-recovery",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
        confirm_new_input=True,
    )
    assert restarted.recovery_source_run_id is None
    assert restarted.input_snapshot == source.input_snapshot
    assert restarted.canonical_input_sha256 != source.canonical_input_sha256
    assert all(
        unit.reused_from_unit_id is None
        for unit in units_of(repository, value, restarted.run_id)
    )


def test_auto_resume_auto_confirms_deployment_input_change_with_audit() -> None:
    value = context(2)
    svc, repository, model, _clock = service(value, unknown_on=1)
    source = create(svc, value, "auto-source")
    complete(svc, value, source.run_id)
    # 部署版本变化（模型/提示词更新）：冻结快照身份不变、canonical 输入变化。
    svc._model_name = "new-director-model"

    rerun = svc.create_run(
        owner_id=value.owner_id,
        project_id=value.project_id,
        idempotency_key="auto-resume-1",
        trigger=ProjectDirectorTrigger.AUTO_RESUME,
    )
    # §15.13：自动整轮重跑，不复用旧单元，审计记录新旧输入哈希。
    assert rerun.canonical_input_sha256 != source.canonical_input_sha256
    assert rerun.recovery_source_run_id is None
    audit = repository.auto_input_change_audits[rerun.run_id]
    assert audit["action"] == "auto_confirm_new_input"
    assert audit["reason"] == "deployment_version_change"
    assert audit["source_run_id"] == str(source.run_id)
    assert audit["source_canonical_input_sha256"] == source.canonical_input_sha256
    assert audit["new_canonical_input_sha256"] == rerun.canonical_input_sha256


def test_auto_resume_snapshot_identity_change_fails_closed() -> None:
    value = context(2)
    svc, repository, model, _clock = service(value, unknown_on=1)
    source = create(svc, value, "auto-source-closed")
    complete(svc, value, source.run_id)
    svc._model_name = "new-director-model"
    # 冻结快照身份变化：异常信号（快照理论上不可变），fail-closed。
    repository.context = replace(repository.context, snapshot_sha256="f" * 64)
    with pytest.raises(DomainError) as violation:
        svc.create_run(
            owner_id=value.owner_id,
            project_id=value.project_id,
            idempotency_key="auto-resume-2",
            trigger=ProjectDirectorTrigger.AUTO_RESUME,
        )
    assert violation.value.code == "director_snapshot_integrity_violation"
    assert len(repository.runs) == 1


def test_manual_recovery_reclaims_expired_orphan_unit_before_reprocessing() -> None:
    # 2026-10-14 缺陷回归：worker 崩溃（或运行被并发终结）遗留的 SUBMITTING 单元
    # 永久阻塞恢复创建；租约到期后应在恢复前被回收为 FAILED（worker 丢失语义）。
    value = context(12)
    svc, repository, model, clock = service(value)
    source = create(svc, value, "orphan-source")
    units = repository.units[source.run_id]
    units[1] = replace(
        units[1],
        status=ProjectDirectorUnitStatus.SUBMITTING,
        lease_token=uuid4(),
        lease_until=clock.now() - timedelta(seconds=1),
        attempt_count=1,
    )
    units[2] = replace(
        units[2],
        status=ProjectDirectorUnitStatus.SUBMITTING,
        lease_token=uuid4(),
        lease_until=clock.now() + timedelta(minutes=5),
        attempt_count=1,
    )
    repository.runs[source.run_id] = replace(
        repository.runs[source.run_id],
        status=ProjectDirectorRunStatus.FAILED,
        failure_code=PROJECT_DIRECTOR_UNKNOWN_EXHAUSTED_FAILURE,
    )
    # 活跃租约（未到期）的单元不回收：恢复仍按既有语义被拒。
    with pytest.raises(DomainError) as blocked:
        svc.create_run(
            owner_id=value.owner_id,
            project_id=value.project_id,
            idempotency_key="orphan-recovery",
            trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
        )
    assert blocked.value.code == "director_run_unresolved"
    assert len(repository.runs) == 1
    # 已到期的孤儿单元（ordinal 2）在第一次尝试中已被回收为 FAILED；active 租约的
    # ordinal 3 未被动。
    refreshed = {unit.ordinal: unit for unit in repository.units[source.run_id]}
    assert refreshed[2].status is ProjectDirectorUnitStatus.FAILED
    assert refreshed[2].failure_code == PROJECT_DIRECTOR_WORKER_LOST_FAILURE
    assert refreshed[3].status is ProjectDirectorUnitStatus.SUBMITTING
    assert refreshed[3].lease_token is not None
    # 活跃租约也到期后：回收剩余孤儿，恢复创建成功。
    refreshed[3] = replace(refreshed[3], lease_until=clock.now() - timedelta(seconds=1))
    repository.units[source.run_id][2] = refreshed[3]
    recovered = svc.create_run(
        owner_id=value.owner_id,
        project_id=value.project_id,
        idempotency_key="orphan-recovery",
        trigger=ProjectDirectorTrigger.MANUAL_REGENERATE,
    )
    reclaimed = {unit.ordinal: unit for unit in repository.units[source.run_id]}
    assert reclaimed[3].status is ProjectDirectorUnitStatus.FAILED
    assert reclaimed[3].failure_code == PROJECT_DIRECTOR_WORKER_LOST_FAILURE
    assert reclaimed[3].last_failure_code == PROJECT_DIRECTOR_WORKER_LOST_FAILURE
    assert reclaimed[3].lease_token is None and reclaimed[3].lease_until is None
    assert recovered.status is ProjectDirectorRunStatus.INTENT_PERSISTED


def test_auto_resume_reclaims_orphan_units_of_terminal_run() -> None:
    # AUTO 恢复：终态运行里的过期 SUBMITTING → FAILED；过期 LEASED → INTENT_PERSISTED。
    value = context(12)
    svc, repository, model, clock = service(value)
    repository.production_mode = "AUTO"
    source = create(svc, value, "auto-orphan-source")
    units = repository.units[source.run_id]
    units[1] = replace(
        units[1],
        status=ProjectDirectorUnitStatus.SUBMITTING,
        lease_token=uuid4(),
        lease_until=clock.now() - timedelta(seconds=1),
        attempt_count=1,
    )
    units[2] = replace(
        units[2],
        status=ProjectDirectorUnitStatus.LEASED,
        lease_token=uuid4(),
        lease_until=clock.now() - timedelta(seconds=1),
        attempt_count=0,
    )
    repository.runs[source.run_id] = replace(
        repository.runs[source.run_id],
        status=ProjectDirectorRunStatus.FAILED,
        failure_code=PROJECT_DIRECTOR_UNKNOWN_EXHAUSTED_FAILURE,
    )

    rerun = svc.create_run(
        owner_id=value.owner_id,
        project_id=value.project_id,
        idempotency_key="auto-orphan-recovery",
        trigger=ProjectDirectorTrigger.AUTO_RESUME,
    )

    refreshed = {unit.ordinal: unit for unit in repository.units[source.run_id]}
    assert refreshed[2].status is ProjectDirectorUnitStatus.FAILED
    assert refreshed[2].failure_code == PROJECT_DIRECTOR_WORKER_LOST_FAILURE
    assert refreshed[2].lease_token is None and refreshed[2].lease_until is None
    assert refreshed[3].status is ProjectDirectorUnitStatus.INTENT_PERSISTED
    assert refreshed[3].lease_token is None and refreshed[3].lease_until is None
    assert rerun.status is ProjectDirectorRunStatus.INTENT_PERSISTED
    assert rerun.run_id != source.run_id


def test_sql_reclaim_expired_units_scopes_terminal_runs_only(tmp_path) -> None:
    # SQL 仓储行为：只回收终态运行中租约已过期的 LEASED/SUBMITTING 单元；
    # 活跃运行中的过期单元保持原样（仍由 advance 的 stale 路径负责）。
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tubeflow_test.adapters.persistence.storyboards import (
        project_director_repository as repo_module,
    )
    from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
        ProjectDirectorRunRow,
        ProjectDirectorUnitRow,
    )
    from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
        SqlAlchemyProjectDirectorRepository,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'reclaim.sqlite'}")
    ProjectDirectorRunRow.__table__.create(engine)
    ProjectDirectorUnitRow.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def uow():
        session = sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    owner_id, project_id = uuid4(), uuid4()
    now = datetime(2026, 10, 14, tzinfo=UTC)
    failed_run_id, running_run_id = uuid4(), uuid4()

    def run_row(run_id: UUID, status: str) -> ProjectDirectorRunRow:
        return ProjectDirectorRunRow(
            id=run_id,
            owner_id=owner_id,
            project_id=project_id,
            trigger="NARRATION_CONFIRMATION",
            idempotency_key=f"key-{run_id}",
            canonical_input_sha256="a" * 64,
            input_json={},
            snapshot_sha256="b" * 64,
            snapshot_revision=1,
            model="gpt-5.6-sol",
            parameters_json={},
            status=status,
            total_units=4,
            created_at=now,
        )

    def unit_row(
        run_id: UUID, ordinal: int, status: str, lease_until: datetime | None
    ) -> ProjectDirectorUnitRow:
        return ProjectDirectorUnitRow(
            id=uuid4(),
            run_id=run_id,
            owner_id=owner_id,
            project_id=project_id,
            ordinal=ordinal,
            kind="SHOT_PLAN",
            sentences_json=[],
            context_before_json=[],
            context_after_json=[],
            provider_idempotency_key=uuid4().hex.ljust(64, "0")[:64],
            canonical_request_sha256="c" * 64,
            prompt_ref="project-director-unit-v5",
            prompt_sha256="d" * 64,
            prompt_content="prompt",
            required_locale="en-US",
            model="gpt-5.6-sol",
            parameters_json={},
            status=status,
            created_at=now,
            lease_token=uuid4() if lease_until is not None else None,
            lease_until=lease_until,
        )

    expired = now - timedelta(minutes=30)
    future = now + timedelta(minutes=30)
    with sessions.begin() as session:
        session.add(run_row(failed_run_id, "FAILED"))
        session.add(run_row(running_run_id, "RUNNING"))
        session.add(unit_row(failed_run_id, 1, "SUBMITTING", expired))
        session.add(unit_row(failed_run_id, 2, "LEASED", expired))
        session.add(unit_row(failed_run_id, 3, "SUBMITTING", future))
        session.add(unit_row(running_run_id, 1, "SUBMITTING", expired))

    monkeypatch_target = repo_module
    original_lock = monkeypatch_target._lock_active_project
    monkeypatch_target._lock_active_project = lambda *args, **kwargs: None  # type: ignore[assignment]
    try:
        repo = SqlAlchemyProjectDirectorRepository(uow)
        reclaimed = repo.reclaim_expired_units(owner_id, project_id, now=now)
    finally:
        monkeypatch_target._lock_active_project = original_lock

    assert len(reclaimed) == 2
    with sessions() as session:
        states = {
            row.ordinal: row
            for row in session.query(ProjectDirectorUnitRow)
            .filter_by(run_id=failed_run_id)
            .all()
        }
    assert states[1].status == "FAILED"
    assert states[1].failure_code == PROJECT_DIRECTOR_WORKER_LOST_FAILURE
    assert states[1].lease_until is None and states[1].lease_token is None
    assert states[2].status == "INTENT_PERSISTED"
    assert states[2].lease_until is None and states[2].lease_token is None
    assert states[3].status == "SUBMITTING"
    assert states[3].lease_until is not None
    with sessions() as session:
        running_unit = (
            session.query(ProjectDirectorUnitRow).filter_by(run_id=running_run_id).one()
        )
    assert running_unit.status == "SUBMITTING"
    assert running_unit.lease_until is not None

def test_sql_find_materialization_tolerates_orphan_row(tmp_path) -> None:
    # 孤儿行（父行无候选子行，历史不完整写入）：读取按"不存在"处理，交给确定性重写。
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
        ProjectDirectorGroupCandidateRow,
        ProjectDirectorMaterializationRow,
    )
    from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
        SqlAlchemyProjectDirectorRepository,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'orphan.sqlite'}")
    ProjectDirectorMaterializationRow.__table__.create(engine)
    ProjectDirectorGroupCandidateRow.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def uow():
        session = sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    owner_id, project_id = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            ProjectDirectorMaterializationRow(
                id=uuid4(),
                owner_id=owner_id,
                project_id=project_id,
                preplan_revision_id=uuid4(),
                snapshot_sha256="a" * 64,
                snapshot_revision=1,
                algorithm_version="controlled-semantic-lineage-remap-v3",
                canonical_input_sha256="c" * 64,
                status="REVIEW_REQUIRED",
                created_at=datetime.now(UTC),
            )
        )
    repo = SqlAlchemyProjectDirectorRepository(uow)
    assert repo.find_materialization(owner_id, "c" * 64) is None


def test_materialization_ready_requires_candidate_rows(tmp_path) -> None:
    # 就绪判定候选感知：孤儿父行不就绪，带子行的物化才就绪。
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tubeflow_test.adapters.persistence.auto_pipeline import _materialization_ready
    from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
        ProjectDirectorGroupCandidateRow,
        ProjectDirectorMaterializationRow,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'ready.sqlite'}")
    ProjectDirectorMaterializationRow.__table__.create(engine)
    ProjectDirectorGroupCandidateRow.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner_id, project_id = uuid4(), uuid4()
    materialization_id = uuid4()
    with sessions.begin() as session:
        session.add(
            ProjectDirectorMaterializationRow(
                id=materialization_id,
                owner_id=owner_id,
                project_id=project_id,
                preplan_revision_id=uuid4(),
                snapshot_sha256="a" * 64,
                snapshot_revision=1,
                algorithm_version="controlled-semantic-lineage-remap-v3",
                canonical_input_sha256="c" * 64,
                status="REVIEW_REQUIRED",
                created_at=datetime.now(UTC),
            )
        )
    with sessions() as session:
        assert _materialization_ready(session, owner_id, project_id) is False
    with sessions.begin() as session:
        session.add(
            ProjectDirectorGroupCandidateRow(
                id=uuid4(),
                materialization_id=materialization_id,
                owner_id=owner_id,
                project_id=project_id,
                group_id=uuid4(),
                group_sequence=1,
                shots_json=[],
                links_json=[],
                source_shot_links_json=[],
                conflicts_json=[{"code": "group_has_no_shots", "sentence_ids": [], "shot_ids": []}],
                review_state="REQUIRED",
                final_revision_id=None,
            )
        )
    with sessions() as session:
        assert _materialization_ready(session, owner_id, project_id) is True



class LinkPatternModel(Model):
    """按固定链接形态输出多镜头（0 基句下标），复现生产 group0 计时无解场景。"""

    def __init__(self, pattern: tuple[tuple[int, ...], ...]) -> None:
        super().__init__()
        self.pattern = pattern

    def generate(self, unit: ProjectDirectorUnit) -> Result:
        if unit.kind is not ProjectDirectorUnitKind.SHOT_PLAN:
            return super().generate(unit)
        self.calls.append(unit.ordinal)
        shots = [
            shot_json(
                unit,
                index,
                unit.ordinal * 8 + index,
                links=tuple(unit.sentences[item].sentence_id for item in linked),
            )
            for index, linked in enumerate(self.pattern, start=1)
        ]
        return Result({"output_locale": unit.required_locale, "shots": shots})


def _sequential_ranges(count: int, step_ms: int = 2800) -> tuple[tuple[int, int], ...]:
    return tuple((index * step_ms, (index + 1) * step_ms) for index in range(count))


def test_materialize_merges_adjacent_shots_until_timing_projectable() -> None:
    """2026-10-16（monotone-projection-v1）：镜头 1 链接第 1、2、4 句、镜头 2 链接
    第 3 句的组在物化时投影无解，自动相邻合并兜底到单镜；全自动、不进入人工，
    血缘与审计冲突随候选存档。"""
    value = context(4)
    svc, repository, model, _clock = service(value)
    model.generate = LinkPatternModel(((0, 1, 3), (2,))).generate  # type: ignore[method-assign]
    complete(svc, value, create(svc, value, "timing-merge").run_id)
    ranges = _sequential_ranges(4)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(),
            1,
            tuple(item.sentence_id for item in value.sentences),
            ranges,
        ),
    )

    result = svc.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.SUCCEEDED
    candidate = result.candidates[0]
    assert len(candidate.shots) == 1
    assert candidate.review_state is ReviewState.NOT_REQUIRED
    assert [item.code for item in candidate.conflicts] == ["shot_timing_converged"]
    # 血缘完整：两个来源镜头都重定向到保留镜头（多父），候选域校验通过。
    assert len(candidate.source_shot_links) == 2
    assert {item.candidate_shot_id for item in candidate.source_shot_links} == {
        candidate.shots[0].shot_id
    }
    assert candidate.final_revision is not None
    assert candidate.final_revision.shots == candidate.shots
    # 合并后可投影：单镜覆盖全部 4 句，区间等于组区间。
    projected = projected_shot_time_ranges_ms(
        candidate.shots,
        candidate.sentence_links,
        dict(zip((item.sentence_id for item in value.sentences), ranges, strict=True)),
    )
    assert [(item.shot_order, item.start_ms, item.end_ms) for item in projected] == [
        (1, 0, 4 * 2800)
    ]


def test_materialize_without_sentence_ranges_keeps_legacy_remap() -> None:
    """组上下文未携带实测时间轴（历史调用方）：跳过投影检查，行为同 v4。"""
    value = context(4)
    svc, repository, model, _clock = service(value)
    model.generate = LinkPatternModel(((0, 1, 3), (2,))).generate  # type: ignore[method-assign]
    complete(svc, value, create(svc, value, "timing-legacy").run_id)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(), 1, tuple(item.sentence_id for item in value.sentences)
        ),
    )

    result = svc.materialize(value.owner_id, value.project_id)

    candidate = result.candidates[0]
    assert len(candidate.shots) == 2
    assert candidate.conflicts == ()
    assert result.status is MaterializationStatus.SUCCEEDED


def test_materialize_projectable_group_is_not_rewritten() -> None:
    """可投影组零变化：不触发合并、无审计冲突，镜头与链接保持重映射原样。"""
    value = context(4)
    svc, repository, model, _clock = service(value)
    model.generate = LinkPatternModel(((0, 1), (2, 3))).generate  # type: ignore[method-assign]
    complete(svc, value, create(svc, value, "timing-clean").run_id)
    revision = svc.current(value.owner_id, value.project_id)
    ranges = _sequential_ranges(4)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(),
            1,
            tuple(item.sentence_id for item in value.sentences),
            ranges,
        ),
    )

    result = svc.materialize(value.owner_id, value.project_id)

    candidate = result.candidates[0]
    assert candidate.conflicts == ()
    assert len(candidate.shots) == 2
    # 与重映射输入一一对应：每候选恰好一个来源，句链接形态不变。
    assert len(candidate.source_shot_links) == 2
    assert len({item.candidate_shot_id for item in candidate.source_shot_links}) == 2
    source_links = {
        source.shot_id: tuple(
            link.sentence_id for link in revision.sentence_links if link.shot_id == source.shot_id
        )
        for source in revision.shots
    }
    for link in candidate.source_shot_links:
        mapped = tuple(
            item.sentence_id
            for item in candidate.sentence_links
            if item.shot_id == link.candidate_shot_id
        )
        assert mapped == source_links[link.source_shot_id]


def _timing_revision(
    value: ProjectDirectorInputSnapshot,
    pattern: tuple[tuple[int, ...], ...],
    *,
    long_text_at: int | None = None,
) -> ProjectDirectorRevision:
    shots = []
    links = []
    for index, linked in enumerate(pattern, start=1):
        text = "x" * 80 if index == long_text_at else "short"
        shots.append(
            DirectorShot(
                uuid4(),
                index,
                f"scene {index}",
                "WIDE" if index % 2 else "CLOSE_UP",
                "HIGH_ANGLE" if index % 2 else "LOW_ANGLE",
                f"{text} action {index}",
                f"function {index}",
                f"{text} description {index}",
                **rotated_semantics(index),
            )
        )
        links.extend(
            ShotSentenceLink(shots[-1].shot_id, value.sentences[item].sentence_id)
            for item in linked
        )
    return ProjectDirectorRevision(
        revision_id=uuid4(),
        owner_id=value.owner_id,
        project_id=value.project_id,
        run_id=uuid4(),
        revision_number=1,
        snapshot_sha256=value.snapshot_sha256,
        snapshot_revision=value.snapshot_revision,
        sentence_ids=tuple(item.sentence_id for item in value.sentences),
        shots=tuple(shots),
        sentence_links=tuple(links),
        quality_conflicts=(),
        created_at=datetime.now(UTC),
    )


def test_candidate_merges_one_adjacent_pair_when_projection_becomes_solvable() -> None:
    """多级兜底但按需停止：3 镜无解（1,2,1,3 非单调），按 adjacent-merge-v1
    合并最短相邻对（镜头 1+2）后 2 镜即可投影，不继续合并到单镜。"""
    value = context(4)
    svc, _repository, _model, _clock = service(value)
    revision = _timing_revision(value, ((0, 2), (1,), (3,)), long_text_at=3)
    ranges = _sequential_ranges(4)
    group = ProjectDirectorGroupContext(
        uuid4(), 1, tuple(item.sentence_id for item in value.sentences), ranges
    )

    candidate = svc._candidate(revision, group)

    assert len(candidate.shots) == 2
    assert [item.order for item in candidate.shots] == [1, 2]
    assert [item.code for item in candidate.conflicts] == ["shot_timing_converged"]
    assert candidate.review_state is ReviewState.NOT_REQUIRED
    assert candidate.final_revision is not None
    # 血缘：镜头 1、2 的来源都指向合并镜头，镜头 3 来源指向第二镜。
    targets = {item.source_shot_id: item.candidate_shot_id for item in candidate.source_shot_links}
    assert len(candidate.source_shot_links) == 3
    assert targets[revision.shots[0].shot_id] == candidate.shots[0].shot_id
    assert targets[revision.shots[1].shot_id] == candidate.shots[0].shot_id
    assert targets[revision.shots[2].shot_id] == candidate.shots[1].shot_id
    # 合并镜头句链接取并集：第一镜覆盖第 1、2、3 句，第二镜覆盖第 4 句
    # （链接保持合并前稳定序，成员资格按集合断言）。
    member = {
        shot.shot_id: {
            link.sentence_id for link in candidate.sentence_links if link.shot_id == shot.shot_id
        }
        for shot in candidate.shots
    }
    sentence_ids = tuple(item.sentence_id for item in value.sentences)
    assert member[candidate.shots[0].shot_id] == set(sentence_ids[:3])
    assert member[candidate.shots[1].shot_id] == set(sentence_ids[3:])
    projected = projected_shot_time_ranges_ms(
        candidate.shots, candidate.sentence_links, dict(zip(sentence_ids, ranges, strict=True))
    )
    assert [(item.shot_order, item.start_ms, item.end_ms) for item in projected] == [
        (1, 0, 3 * 2800),
        (2, 3 * 2800, 4 * 2800),
    ]


def test_candidate_audit_conflict_round_trips_without_blocking() -> None:
    """审计型冲突不阻断播种链路：final_revision 直落且物化状态保持 SUCCEEDED。"""
    value = context(3)
    svc, repository, model, _clock = service(value)
    model.generate = LinkPatternModel(((0, 2), (1,))).generate  # type: ignore[method-assign]
    complete(svc, value, create(svc, value, "timing-audit").run_id)
    ranges = _sequential_ranges(3)
    repository.groups = (
        ProjectDirectorGroupContext(
            uuid4(), 1, tuple(item.sentence_id for item in value.sentences), ranges
        ),
    )

    result = svc.materialize(value.owner_id, value.project_id)

    assert result.status is MaterializationStatus.SUCCEEDED
    candidate = result.candidates[0]
    assert len(candidate.shots) == 1
    assert [item.code for item in candidate.conflicts] == ["shot_timing_converged"]
    assert candidate.final_revision is not None
    # 幂等回读：审计冲突随候选存档，再次 materialize 返回同一物化。
    replay = svc.materialize(value.owner_id, value.project_id)
    assert replay.materialization_id == result.materialization_id
    assert [item.code for item in replay.candidates[0].conflicts] == ["shot_timing_converged"]
