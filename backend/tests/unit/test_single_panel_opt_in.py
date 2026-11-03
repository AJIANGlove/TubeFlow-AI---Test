from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_h3_candidate_routing import route_session
from test_single_panel_replay import _prepared_attempt, _with_panels
from test_video_orchestration import setup

from tubeflow_test.video_generation.authority import LatestAuthorityLoader
from tubeflow_test.video_generation.domain import (
    ReferencePolicy,
    ReferenceStrategy,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import (
    _authorize,
    _input_snapshot,
    _request,
    validate_reference_intent,
)
from tubeflow_test.video_generation.persistence import H3ProjectWorkflowRouteRecord
from tubeflow_test.video_generation.project_routing import (
    reference_input_fingerprint,
    resolve_reference_policy,
)


def test_composite_storyboard_baseline_has_no_single_panel_state() -> None:
    source, _repository, _engine, _assets, _service = setup()

    generation = _authorize(source)
    snapshot = _input_snapshot(source, generation, compiled=None)
    request = _request(generation, None, prompt="baseline")

    assert "shot_panel_assets" not in snapshot
    assert snapshot["compiler_input"] is not None
    assert len(generation.references) == len(source.character_images) + 1
    assert generation.references[-1].kind.value == "STORYBOARD"
    assert request["references"][-1]["asset_sha256"] == source.storyboard_image.sha256


def _route(session, project_id):  # type: ignore[no-untyped-def]
    route = H3ProjectWorkflowRouteRecord(
        project_id=project_id, profile="legacy", bound_at=datetime.now(UTC)
    )
    session.add(route)
    session.flush()
    return route


def _single_panel() -> ReferencePolicy:
    return ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "fake-crop-v1"})


def test_preview_computes_single_panel_candidate_without_binding() -> None:
    project_id = uuid4()
    with route_session(project_id) as session, session.begin():
        route = _route(session, project_id)
        selected = resolve_reference_policy(
            session,
            project_id,
            _single_panel(),
            for_intent=False,
            single_panel_allowed=True,
        )
        assert selected == _single_panel()
        assert route.reference_strategy is None
        assert route.reference_derivation_json is None


def test_intent_binds_policy_and_existing_binding_does_not_drift() -> None:
    project_id = uuid4()
    with route_session(project_id) as session, session.begin():
        route = _route(session, project_id)
        selected = resolve_reference_policy(
            session,
            project_id,
            _single_panel(),
            for_intent=True,
            single_panel_allowed=True,
        )
        assert selected == _single_panel()
        assert route.reference_strategy == "single_panel_v1"
        assert route.reference_derivation_json == {"version": "fake-crop-v1"}

        rebound = resolve_reference_policy(
            session,
            project_id,
            ReferencePolicy(),
            for_intent=True,
            single_panel_allowed=True,
        )
        assert rebound == selected
        assert route.reference_derivation_json == {"version": "fake-crop-v1"}
        with pytest.raises(VideoRuleViolation, match="single_panel_opt_in_disabled"):
            resolve_reference_policy(
                session,
                project_id,
                ReferencePolicy(),
                for_intent=True,
                single_panel_allowed=False,
            )


def test_null_binds_composite_only_for_intent_and_cannot_switch_to_single_panel() -> None:
    project_id = uuid4()
    with route_session(project_id) as session, session.begin():
        route = _route(session, project_id)
        resolve_reference_policy(
            session,
            project_id,
            ReferencePolicy(),
            for_intent=False,
            single_panel_allowed=True,
        )
        assert route.reference_strategy is None
        resolve_reference_policy(
            session,
            project_id,
            ReferencePolicy(),
            for_intent=True,
            single_panel_allowed=True,
        )
        assert route.reference_strategy == "composite"
        with pytest.raises(VideoRuleViolation, match="reference_policy_already_bound"):
            resolve_reference_policy(
                session,
                project_id,
                _single_panel(),
                for_intent=True,
                single_panel_allowed=True,
            )


def test_single_panel_preview_is_disabled_by_default() -> None:
    project_id = uuid4()
    with route_session(project_id) as session, session.begin():
        route = _route(session, project_id)
        with pytest.raises(VideoRuleViolation, match="single_panel_opt_in_disabled"):
            resolve_reference_policy(
                session,
                project_id,
                _single_panel(),
                for_intent=False,
                single_panel_allowed=False,
            )
        assert route.reference_strategy is None


def test_reference_intent_validation_detects_derivative_drift() -> None:
    source, _repository, _engine, assets, _service = setup()
    source = _with_panels(source, assets)
    policy = _single_panel()
    fingerprint = reference_input_fingerprint(
        source.authority_snapshot, policy, source.shot_panel_images
    )
    source = replace(
        source, reference_policy=policy, reference_input_sha256=fingerprint
    )
    attempt = _prepared_attempt(source)
    validate_reference_intent(source, attempt)
    assert attempt.input_snapshot is not None
    assert attempt.input_snapshot["reference_input_sha256"] == fingerprint
    assert attempt.input_snapshot["reference_policy"]["strategy"] == "single_panel_v1"

    changed = replace(
        source,
        shot_panel_images=(replace(source.shot_panel_images[0], asset=assets.add(b"changed")),),
    )
    changed = replace(
        changed,
        reference_input_sha256=reference_input_fingerprint(
            changed.authority_snapshot, policy, changed.shot_panel_images
        ),
    )
    with pytest.raises(VideoRuleViolation, match="video_reference_input_changed"):
        validate_reference_intent(changed, attempt)


def test_latest_authority_forwards_for_intent_to_reference_policy() -> None:
    source, _repository, _engine, _assets, _service = setup()
    calls: list[bool] = []

    class Narration:
        def load_group(self, _session, _owner_id, _group_id):  # type: ignore[no-untyped-def]
            return source

    class Storyboard:
        def bind_current(self, _session, narration):  # type: ignore[no-untyped-def]
            return narration

    class Policy:
        def apply_reference_policy(self, _session, narration, *, for_intent):  # type: ignore[no-untyped-def]
            calls.append(for_intent)
            return narration

    loaded = LatestAuthorityLoader(Narration(), Storyboard(), Policy())(
        None, source.owner_id, source.group_id, for_intent=True
    )

    assert loaded == source
    assert calls == [True]
