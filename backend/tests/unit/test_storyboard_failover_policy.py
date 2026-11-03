from __future__ import annotations

import pytest

from tubeflow_test.application.storyboard_failover import (
    DEFAULT_STORYBOARD_FAILOVER_POLICY,
    StoryboardFailoverPolicy,
    StoryboardFailoverStep,
)


def test_default_policy_matches_the_frozen_seven_model_route() -> None:
    policy = DEFAULT_STORYBOARD_FAILOVER_POLICY
    assert [(step.provider, step.model) for step in policy.steps] == [
        ("grsai", "gpt-image-2.5"),
        ("grsai", "gpt-image-2"),
        ("grsai", "nano-banana-2"),
        ("grsai", "nano-banana-pro"),
        ("pix", "tt-image-2.5"),
        ("pix", "tt-image-2"),
        ("pix", "banana-2.1"),
    ]
    assert policy.first.total_attempt == 1
    assert policy.steps[2].parameters["imageSize"] == "2K"
    assert policy.steps[4].parameters["resolution"] == "2K"
    assert policy.steps[5].parameters["size"] == "1920x1088"
    assert policy.steps[6].parameters["thinkingLevel"] == "minimal"
    assert "web_search" not in policy.steps[6].parameters


def test_policy_retries_same_model_once_before_moving_forward() -> None:
    policy = DEFAULT_STORYBOARD_FAILOVER_POLICY
    second = policy.next_position(route_index=0, model_attempt=1, total_attempt=1)
    assert second is not None
    assert (second.route_index, second.model_attempt, second.total_attempt) == (0, 2, 2)
    third = policy.next_position(route_index=0, model_attempt=2, total_attempt=2)
    assert third is not None
    assert (third.route_index, third.model_attempt, third.total_attempt) == (1, 1, 3)
    assert third.step.model == "gpt-image-2"


def test_policy_channel_failure_skips_every_remaining_model_in_that_channel() -> None:
    policy = DEFAULT_STORYBOARD_FAILOVER_POLICY
    for route_index in range(4):
        position = policy.next_position(
            route_index=route_index,
            model_attempt=1,
            total_attempt=route_index + 1,
            skip_channel=True,
        )
        assert position is not None
        assert position.step.provider == "pix"
        assert position.route_index == 4
        assert position.model_attempt == 1
    assert policy.next_position(
        route_index=5, model_attempt=1, total_attempt=5, skip_channel=True
    ) is None


def test_policy_never_creates_the_fifteenth_attempt() -> None:
    policy = DEFAULT_STORYBOARD_FAILOVER_POLICY
    assert policy.next_position(route_index=6, model_attempt=2, total_attempt=14) is None
    with pytest.raises(ValueError, match="outside the budget"):
        policy.next_position(route_index=6, model_attempt=2, total_attempt=15)
    with pytest.raises(ValueError, match="outside the budget"):
        policy.next_position(route_index=0, model_attempt=3, total_attempt=3)


def test_policy_validates_shape_and_immutable_parameters() -> None:
    with pytest.raises(ValueError, match="one to seven"):
        StoryboardFailoverPolicy(steps=())
    step = StoryboardFailoverStep("pix", "model", {"quality": "high"})
    with pytest.raises(TypeError):
        step.parameters["quality"] = "low"  # type: ignore[index]
