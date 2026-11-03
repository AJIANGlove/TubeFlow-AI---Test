from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from test_h3_candidate_routing import route_session
from test_video_orchestration import setup

from tubeflow_test.adapters.persistence.production_authority import (
    SqlCanonicalVideoGroupLoader,
    single_panel_policy_settings,
)
from tubeflow_test.config import Settings
from tubeflow_test.video_generation.domain import (
    ReferencePolicy,
    ReferenceStrategy,
    ShotPanelImage,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.factory import build_video_feature
from tubeflow_test.video_generation.persistence import H3ProjectWorkflowRouteRecord


def test_production_default_prompt_strategy_is_composite_v4() -> None:
    settings = Settings(_env_file=None)

    # 2026-11-03（USER_DECISIONS §43）：默认整图策略由 v3 改为 v4，单格参考仍为显式 opt-in。
    assert settings.video_prompt_strategy == "tubeflow_storyboard_v4_natural_motion"
    assert settings.h3_single_panel_opt_in_enabled is False
    assert settings.h3_single_panel_project_ids == ()
    requested, allowed = single_panel_policy_settings(settings)
    project_id = uuid4()
    assert requested(project_id).strategy is ReferenceStrategy.COMPOSITE
    assert allowed(project_id) is False


def test_opt_in_requires_switch_and_explicit_allowlist() -> None:
    project_id = uuid4()
    with pytest.raises(ValueError, match="allowlist requires the opt-in switch"):
        Settings(_env_file=None, h3_single_panel_project_ids=(project_id,))
    with pytest.raises(ValueError, match="requires an explicit project allowlist"):
        Settings(_env_file=None, h3_single_panel_opt_in_enabled=True)

    settings = Settings(
        _env_file=None,
        h3_single_panel_opt_in_enabled=True,
        h3_single_panel_project_ids=(project_id,),
    )
    requested, allowed = single_panel_policy_settings(settings)
    assert requested(project_id).strategy is ReferenceStrategy.SINGLE_PANEL_V1
    assert requested(uuid4()).strategy is ReferenceStrategy.COMPOSITE
    assert allowed(project_id) is True
    assert allowed(uuid4()) is False


def test_factory_uses_same_loader_for_intent_validation() -> None:
    source, _repository, _engine, assets, _service = setup()
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

    feature = build_video_feature(
        database_engine=create_engine("sqlite://"),
        narration_authority=Narration(),
        storyboard_authority=Storyboard(),
        reference_policy_authority=Policy(),
        write_guard=lambda _owner, _project: None,
        token_resolver=lambda _node: "fake",
        authenticated_owner=lambda: source.owner_id,
        asset_store=assets,
    )
    loaded = feature.repository._intent_group_loader(
        None, source.owner_id, source.group_id, for_intent=True
    )
    assert loaded == source
    assert calls == [True]
    feature.close()


def test_allowlisted_project_binds_only_for_intent_and_kill_switch_stops_new_intent() -> None:
    project_id = uuid4()
    source, _repository, _engine, assets, _service = setup()
    source = replace(source, project_id=project_id)

    class Derivatives:
        def derive(self, _session, value, _policy, *, for_intent):  # type: ignore[no-untyped-def]
            return (
                ShotPanelImage(
                    assets.add(b"panel"),
                    1,
                    value.storyboard_image.sha256,
                    "crop-title54-v1",
                ),
            )

    requested, _ = single_panel_policy_settings(
        Settings(
            _env_file=None,
            h3_single_panel_opt_in_enabled=True,
            h3_single_panel_project_ids=(project_id,),
        )
    )
    permitted = True

    def allowed(_project_id) -> bool:  # type: ignore[no-untyped-def]
        return permitted

    authority = SqlCanonicalVideoGroupLoader(
        Settings(_env_file=None),
        reference_policy=requested,
        single_panel_allowed=allowed,
        panel_derivatives=Derivatives(),  # type: ignore[arg-type]
    )
    with route_session(project_id) as session, session.begin():
        session.add(
            H3ProjectWorkflowRouteRecord(
                project_id=project_id, profile="legacy", bound_at=datetime.now(UTC)
            )
        )
        session.flush()
        preview = authority.apply_reference_policy(session, source, for_intent=False)
        assert len(preview.shot_panel_images) == 1
        route = session.get(H3ProjectWorkflowRouteRecord, project_id)
        assert route is not None and route.reference_strategy is None
        authority.apply_reference_policy(session, source, for_intent=True)
        assert route.reference_strategy == "single_panel_v1"
        permitted = False
        with pytest.raises(VideoRuleViolation, match="single_panel_opt_in_disabled"):
            authority.apply_reference_policy(session, source, for_intent=True)


def test_opt_in_without_derivative_service_fails_closed() -> None:
    source, _repository, _engine, _assets, _service = setup()
    authority = SqlCanonicalVideoGroupLoader(
        Settings(_env_file=None),
        reference_policy=ReferencePolicy(
            ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-title54-v1"}
        ),
        single_panel_allowed=lambda _project_id: True,
    )
    with route_session(source.project_id) as session, session.begin():
        session.add(
            H3ProjectWorkflowRouteRecord(
                project_id=source.project_id, profile="legacy", bound_at=datetime.now(UTC)
            )
        )
        session.flush()
        with pytest.raises(VideoRuleViolation, match="single_panel_derivation_not_configured"):
            authority.apply_reference_policy(session, source, for_intent=False)
