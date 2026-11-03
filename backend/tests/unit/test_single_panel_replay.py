from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest
from test_video_orchestration import Probe, setup

from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink
from tubeflow_test.video_generation.api import _view
from tubeflow_test.video_generation.domain import ShotPanelImage, VideoRuleViolation
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    AuthoritativeGroupInput,
    DurableGroupVideoOrchestrator,
    PersistentAttempt,
    _authorize,
    _input_snapshot,
    prepare_frozen_single_panel_replay_intent,
)
from tubeflow_test.video_prompt.fake_compiler import (
    FakeVideoPromptCompiler as StubVideoPromptCompiler,
)


def _with_panels(
    source: AuthoritativeGroupInput,
    assets,
    *,
    panel_count: int = 1,
    character_count: int = 1,
) -> AuthoritativeGroupInput:
    characters = list(source.character_images)
    while len(characters) < character_count:
        characters.append(assets.add(f"character-{len(characters)}".encode()))
    sentence_id = source.group_sentence_ids[0]
    shots = tuple(
        DirectorShot(
            uuid4(),
            order,
            f"scene-{order}",
            "WIDE",
            "OBJECTIVE",
            f"action-{order}",
            f"function-{order}",
            f"description-{order}",
        )
        for order in range(1, panel_count + 1)
    )
    panels = tuple(
        ShotPanelImage(
            assets.add(f"panel-{order}".encode()),
            order,
            source.storyboard_image.sha256,
            f"deterministic-crop-v1:shot-{order}",
        )
        for order in range(1, panel_count + 1)
    )
    return replace(
        source,
        character_images=tuple(characters[:character_count]),
        director_shots=shots,
        shot_sentence_links=tuple(ShotSentenceLink(shot.shot_id, sentence_id) for shot in shots),
        shot_panel_images=panels,
    )


def _prepared_attempt(
    source: AuthoritativeGroupInput,
    *,
    idempotency_key: str = "frozen-replay-1",
    prompt: str = "frozen R5 prompt",
) -> PersistentAttempt:
    attempt_id = uuid4()
    snapshot, generation_sha, request, request_sha = prepare_frozen_single_panel_replay_intent(
        source,
        attempt_id,
        replay_id="g1-r5-isolated-replay",
        prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
    )
    return PersistentAttempt(
        attempt_id,
        source.owner_id,
        source.project_id,
        source.group_id,
        source.generation_id,
        idempotency_key,
        generation_sha,
        request_sha,
        request,
        input_snapshot=snapshot,
    )


def test_frozen_replay_full_fake_provider_chain_is_immutable_and_idempotent() -> None:
    source, repository, engine, assets, service = setup()
    source = _with_panels(source, assets)
    original_storyboard = source.storyboard_image
    attempt = _prepared_attempt(source)

    first = repository.create_attempt(attempt)
    second = repository.create_attempt(attempt)
    assert first.attempt_id == second.attempt_id == attempt.attempt_id
    assert len(repository.attempts) == 1
    assert attempt.input_snapshot is not None
    assert attempt.input_snapshot["compiler_input"] is None
    assert attempt.input_snapshot["isolated_replay"] == {
        "version": "frozen-single-panel-replay-v1",
        "id": "g1-r5-isolated-replay",
        "real_compiler_called": False,
        "automatic_resubmit_allowed": False,
    }
    assert attempt.input_snapshot["storyboard"]["sha256"] == original_storyboard.sha256
    assert attempt.input_snapshot["shot_panel_assets"][0]["shot_order"] == 1
    assert attempt.request["references"] == [
        {
            "binding_key": "reference_image_1",
            "ordinal": 1,
            "asset_sha256": source.character_images[0].sha256,
        },
        {
            "binding_key": "reference_image_2",
            "ordinal": 2,
            "asset_sha256": source.shot_panel_images[0].asset.sha256,
        },
    ]

    submitted = service.advance(source.owner_id, attempt.attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    assert [call for _, call in engine.calls].count("stage") == 2
    assert [call for _, call in engine.calls].count("submit") == 1

    engine.inspect_status = "SUCCEEDED"
    succeeded = service.resume(source.owner_id, attempt.attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED
    archived = service.archive(source.owner_id, attempt.attempt_id)
    assert archived.stage is AttemptStage.ARCHIVED
    assert archived.archive_stage is ArchiveStage.COMPLETE
    assert archived.archived_asset is not None
    assert assets.read_verified(archived.archived_asset) == engine.output
    assert source.storyboard_image == original_storyboard
    assert source.shot_panel_images[0].source_storyboard_sha256 == original_storyboard.sha256


def test_nine_references_allowed_and_ten_or_fourteen_fail_before_provider() -> None:
    source, _repository, engine, assets, _service = setup()
    nine = _with_panels(source, assets, panel_count=6, character_count=3)
    attempt = _prepared_attempt(nine)
    assert len(attempt.request["references"]) == 9

    ten = _with_panels(source, assets, panel_count=6, character_count=4)
    with pytest.raises(VideoRuleViolation, match="video reference image limit exceeded"):
        _prepared_attempt(ten)

    fourteen = _with_panels(source, assets, panel_count=6, character_count=8)
    with pytest.raises(VideoRuleViolation, match="video reference image limit exceeded"):
        _prepared_attempt(fourteen)
    assert engine.calls == []


def test_real_compiler_is_blocked_before_call_or_attempt_for_single_panel_input() -> None:
    source, repository, engine, assets, service = setup()
    source = _with_panels(source, assets)
    repository.groups[source.group_id] = source

    with pytest.raises(VideoRuleViolation, match="single_panel_prompt_strategy_required"):
        service.trigger(source.owner_id, source.group_id, "must-not-compile")

    assert repository.attempts == {}
    assert engine.calls == []


def test_unknown_recovers_original_attempt_without_replacement_after_restart() -> None:
    source, repository, engine, assets, service = setup()
    source = _with_panels(source, assets)
    engine.lose_submit_response = True
    attempt = repository.create_attempt(_prepared_attempt(source))

    unknown = service.advance(source.owner_id, attempt.attempt_id)
    assert unknown.stage is AttemptStage.REMOTE_STATE_UNKNOWN
    assert [call for _, call in engine.calls].count("submit") == 1

    restarted = DurableGroupVideoOrchestrator(
        repository,
        engine,
        assets,
        Probe(),
        prompt_compiler=StubVideoPromptCompiler(),
    )
    engine.inspect_status = "SUCCEEDED"
    succeeded = restarted.resume(source.owner_id, attempt.attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED
    archived = restarted.archive(source.owner_id, attempt.attempt_id)
    assert archived.stage is AttemptStage.ARCHIVED
    assert [call for _, call in engine.calls].count("submit") == 1
    assert len(repository.attempts) == 1


def test_single_panel_auto_replacement_is_fail_closed() -> None:
    source, _repository, engine, assets, service = setup()
    source = _with_panels(source, assets)
    attempt = _prepared_attempt(source)

    with pytest.raises(VideoRuleViolation, match="single_panel_automatic_resubmit_not_supported"):
        service._build_auto_replacement(attempt, next_attempt_index=2)
    assert engine.calls == []


def test_prompt_hash_and_panel_source_mismatch_are_rejected_before_provider() -> None:
    source, _repository, engine, assets, _service = setup()
    source = _with_panels(source, assets)
    with pytest.raises(VideoRuleViolation, match="frozen_replay_prompt_hash_mismatch"):
        prepare_frozen_single_panel_replay_intent(
            source,
            uuid4(),
            replay_id="g1-r5-isolated-replay",
            prompt="frozen R5 prompt",
            prompt_sha256="0" * 64,
        )

    bad_panel = ShotPanelImage(
        source.shot_panel_images[0].asset,
        1,
        "0" * 64,
        "deterministic-crop-v1:shot-1",
    )
    with pytest.raises(VideoRuleViolation, match="shot panel source storyboard SHA-256 mismatch"):
        _prepared_attempt(replace(source, shot_panel_images=(bad_panel,)))
    assert engine.calls == []


def test_public_view_exposes_safe_panel_lineage_without_object_keys() -> None:
    source, _repository, _engine, assets, _service = setup()
    source = _with_panels(source, assets)
    view = _view(_prepared_attempt(source))

    assert view["storyboard_binding"]["sha256"] == source.storyboard_image.sha256
    assert view["shot_panel_bindings"][0]["derivation"] == "deterministic-crop-v1:shot-1"
    assert view["shot_panel_bindings"][0]["shot_order"] == 1
    assert "object_key" not in view["shot_panel_bindings"][0]
    assert view["prompt_strategy"] == "frozen-single-panel-replay-v1"


def test_composite_storyboard_snapshot_does_not_gain_panel_state() -> None:
    source, _repository, _engine, _assets, _service = setup()

    snapshot = _input_snapshot(source, _authorize(source), compiled=None)

    assert "shot_panel_assets" not in snapshot
    assert snapshot["compiler_input"] is not None
