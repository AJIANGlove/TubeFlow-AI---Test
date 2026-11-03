from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio.testing import WorkflowEnvironment
from test_storyboard_auto_retry import create, image_digest, setup

from tubeflow_test.application.storyboards import ProviderPoll, ProviderSubmission
from tubeflow_test.domain.storyboards import GenerationStatus
from tubeflow_test.workflows.storyboards import (
    GridSheetRecoveryWorkflow,
    StoryboardActivities,
    StoryboardWorkflowInput,
    create_storyboard_worker,
)


@pytest.mark.asyncio
async def test_recovery_continue_as_new_preserves_attempt_and_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, service, provider, owner, group, revision = setup(
        tmp_path, [ProviderSubmission("r-1", "ok")]
    )
    first = create(service, owner, group, revision.revision_id)
    assert service.resume(first.generation_id, owner).status is GenerationStatus.SUBMITTED
    provider.polls = [
        ProviderPoll(GenerationStatus.RUNNING, "running-1"),
        ProviderPoll(GenerationStatus.RUNNING, "running-2"),
        ProviderPoll(GenerationStatus.SUCCEEDED, "ok", "token", image_digest()),
    ]
    monkeypatch.setattr(
        "tubeflow_test.workflows.storyboards.RECOVERY_CONTINUE_AS_NEW_ITERATIONS", 2
    )
    activities = StoryboardActivities(
        director=None,  # type: ignore[arg-type]
        storyboards=service,
        project_director=None,  # type: ignore[arg-type]
    )
    task_queue = f"storyboard-recovery-{uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        async with (
            await WorkflowEnvironment.start_time_skipping() as environment,
            create_storyboard_worker(environment.client, task_queue, activities, executor),
        ):
            handle = await environment.client.start_workflow(
                GridSheetRecoveryWorkflow.run,
                StoryboardWorkflowInput(
                    str(owner), str(first.generation_id), first.canonical_request_sha256
                ),
                id=f"storyboard-recovery-{uuid4()}",
                task_queue=task_queue,
            )
            initial_run_id = handle.run_id
            await handle.result()
            assert handle.result_run_id != initial_run_id
    final = repo.get_generation(owner, first.generation_id)
    assert final is not None and final.status is GenerationStatus.SUCCEEDED
    assert provider.submissions == 1
