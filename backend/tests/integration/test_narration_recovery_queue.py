from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from tubeflow_test.adapters.persistence.narration import (
    NarrationProjectRecord,
    SqlNarrationRepository,
)
from tubeflow_test.application.narration_ports import NarrationJob
from tubeflow_test.domain.common import SupportedLocale
from tubeflow_test.domain.narration import (
    NarrationAttempt,
    NarrationAttemptState,
    segment_pasted_script,
)


def _seed(database: object, parent: object) -> tuple[
    SqlNarrationRepository, tuple[NarrationAttempt, ...]
]:
    repo = SqlNarrationRepository(database.sessions)
    repo.save_script(
        parent.owner_id,
        parent.project_id,
        segment_pasted_script(
            "First sentence. Second sentence. Third sentence.", SupportedLocale.EN_US
        ),
    )
    with database.sessions.begin() as session:
        row = session.get(NarrationProjectRecord, parent.project_id)
        row.payload = {
            **row.payload,
            "job": {
                "job_id": "initial-job",
                "owner_id": str(parent.owner_id),
                "project_id": str(parent.project_id),
                "sentence_sequences": [1, 2, 3],
                "state": "DISPATCHED",
                "failure_sequences": [],
            },
        }
    attempts = tuple(
        NarrationAttempt(
            parent.project_id, parent.owner_id, sequence, f"failed-{sequence}",
            "a" * 64, NarrationAttemptState.OUTPUT_INVALID, f"call-{sequence}",
            safe_error_code="processing_failed",
        )
        for sequence in (2, 3)
    )
    for attempt in attempts:
        repo.save_attempt(attempt)
    return repo, attempts


def _authorize(repo: SqlNarrationRepository, attempt: NarrationAttempt) -> None:
    repo.reconcile_and_enqueue(
        replace(
            attempt,
            state=NarrationAttemptState.SAFE_TO_RETRY,
            safe_error_code="manual:RETRY_INVALID_OUTPUT:" + "b" * 64,
        ),
        "manual_reconciliation",
        NarrationJob(
            f"retry-{attempt.sentence_sequence}", attempt.owner_id, attempt.project_id,
            (attempt.sentence_sequence,), "PENDING",
        ),
    )


@pytest.mark.integration
def test_recovery_queue_preserves_active_job_survives_restart_and_ignores_old_completion(
    postgres_database: object, production_parent_chain: object
) -> None:
    parent = production_parent_chain
    repo, attempts = _seed(postgres_database, parent)
    for attempt in attempts:
        _authorize(repo, attempt)
    _authorize(repo, attempts[0])
    assert repo.get_job(parent.owner_id, parent.project_id).job_id == "initial-job"
    assert repo.pending_jobs() == ()

    restarted = SqlNarrationRepository(postgres_database.sessions)
    restarted.finish_job("initial-job", "PARTIAL_FAILED", (2, 3))
    first = restarted.get_job(parent.owner_id, parent.project_id)
    assert first.job_id == "retry-2" and first.sentence_sequences == (2,)
    restarted.mark_job_dispatched(first.job_id)
    restarted.finish_job("initial-job", "PARTIAL_FAILED", (2, 3))
    restarted.mark_job_dispatched("initial-job")
    assert restarted.get_job(parent.owner_id, parent.project_id).job_id == first.job_id
    assert restarted.pending_jobs() == ()
    restarted.finish_job(first.job_id, "PARTIAL_FAILED", (3,))
    second = restarted.get_job(parent.owner_id, parent.project_id)
    assert second.job_id == "retry-3" and second.sentence_sequences == (3,)
    assert restarted.pending_jobs() == (second,)
    restarted.mark_job_dispatched(second.job_id)
    restarted.finish_job(second.job_id, "COMPLETED", ())
    _authorize(restarted, attempts[0])
    _authorize(restarted, attempts[1])
    assert restarted.pending_jobs() == ()
    with postgres_database.sessions() as session:
        payload = session.get(NarrationProjectRecord, parent.project_id).payload
    assert set(payload["finished_jobs"]) == {"initial-job", "retry-2", "retry-3"}
    assert payload["pending_recovery_jobs"] == []


@pytest.mark.integration
def test_concurrent_recoveries_only_enqueue_one_job_per_authorized_attempt(
    postgres_database: object, production_parent_chain: object
) -> None:
    parent = production_parent_chain
    repo, attempts = _seed(postgres_database, parent)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_authorize, repo, attempt)
            for attempt in (attempts[0], attempts[1], attempts[0], attempts[1])
        ]
        for future in futures:
            future.result(timeout=15)
    assert repo.get_job(parent.owner_id, parent.project_id).job_id == "initial-job"
    with postgres_database.sessions() as session:
        payload = session.get(NarrationProjectRecord, parent.project_id).payload
    queued = payload["pending_recovery_jobs"]
    assert len(queued) == 2
    assert {item["job_id"] for item in queued} == {"retry-2", "retry-3"}
    assert {tuple(item["sentence_sequences"]) for item in queued} == {(2,), (3,)}


@pytest.mark.integration
def test_completion_and_recovery_serialize_without_losing_authorization(
    postgres_database: object, production_parent_chain: object
) -> None:
    parent = production_parent_chain
    repo, attempts = _seed(postgres_database, parent)
    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery = executor.submit(_authorize, repo, attempts[0])
        completion = executor.submit(repo.finish_job, "initial-job", "PARTIAL_FAILED", (2, 3))
        recovery.result(timeout=15)
        completion.result(timeout=15)
    current = repo.get_job(parent.owner_id, parent.project_id)
    assert current.job_id == "retry-2" and current.state == "PENDING"
    assert repo.pending_jobs() == (current,)
