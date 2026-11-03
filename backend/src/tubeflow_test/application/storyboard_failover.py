from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

STORYBOARD_FAILOVER_POLICY_VERSION = "storyboard-failover-v1"
STORYBOARD_FAILOVER_MAX_TOTAL_ATTEMPTS = 14
STORYBOARD_FAILOVER_MAX_MODEL_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class StoryboardFailoverStep:
    provider: str
    model: str
    parameters: Mapping[str, str | int | float | bool]

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("failover provider and model must not be empty")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class StoryboardFailoverPosition:
    step: StoryboardFailoverStep
    route_index: int
    model_attempt: int
    total_attempt: int


@dataclass(frozen=True, slots=True)
class StoryboardFailoverPolicy:
    steps: tuple[StoryboardFailoverStep, ...]
    version: str = STORYBOARD_FAILOVER_POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.steps or len(self.steps) > 7 or not self.version.strip():
            raise ValueError("failover policy requires one to seven steps and a version")

    @property
    def first(self) -> StoryboardFailoverPosition:
        return StoryboardFailoverPosition(self.steps[0], 0, 1, 1)

    def step_at(self, route_index: int) -> StoryboardFailoverStep:
        return self.steps[route_index]

    def next_position(
        self,
        *,
        route_index: int,
        model_attempt: int,
        total_attempt: int,
        skip_channel: bool = False,
    ) -> StoryboardFailoverPosition | None:
        if not 0 <= route_index < len(self.steps):
            raise ValueError("failover route index is outside the policy")
        if not 1 <= model_attempt <= STORYBOARD_FAILOVER_MAX_MODEL_ATTEMPTS:
            raise ValueError("failover model attempt is outside the budget")
        if not 1 <= total_attempt <= STORYBOARD_FAILOVER_MAX_TOTAL_ATTEMPTS:
            raise ValueError("failover total attempt is outside the budget")
        if total_attempt >= STORYBOARD_FAILOVER_MAX_TOTAL_ATTEMPTS:
            return None
        next_total = total_attempt + 1
        if skip_channel:
            provider = self.steps[route_index].provider
            next_index = route_index + 1
            while next_index < len(self.steps) and self.steps[next_index].provider == provider:
                next_index += 1
            if next_index >= len(self.steps):
                return None
            return StoryboardFailoverPosition(self.steps[next_index], next_index, 1, next_total)
        if model_attempt < STORYBOARD_FAILOVER_MAX_MODEL_ATTEMPTS:
            return StoryboardFailoverPosition(
                self.steps[route_index], route_index, model_attempt + 1, next_total
            )
        next_index = route_index + 1
        if next_index >= len(self.steps):
            return None
        return StoryboardFailoverPosition(self.steps[next_index], next_index, 1, next_total)


DEFAULT_STORYBOARD_FAILOVER_POLICY = StoryboardFailoverPolicy(
    version=STORYBOARD_FAILOVER_POLICY_VERSION,
    steps=(
        StoryboardFailoverStep(
            "grsai", "gpt-image-2.5", {"aspect_ratio": "16:9", "quality": "high"}
        ),
        StoryboardFailoverStep("grsai", "gpt-image-2", {"aspect_ratio": "16:9", "quality": "high"}),
        StoryboardFailoverStep(
            "grsai", "nano-banana-2", {"aspect_ratio": "16:9", "imageSize": "2K"}
        ),
        StoryboardFailoverStep(
            "grsai", "nano-banana-pro", {"aspect_ratio": "16:9", "imageSize": "2K"}
        ),
        StoryboardFailoverStep(
            "pix",
            "tt-image-2.5",
            {
                "version": "flare",
                "aspect_ratio": "16:9",
                "resolution": "2K",
                "quality": "high",
                "background": "opaque",
            },
        ),
        StoryboardFailoverStep("pix", "tt-image-2", {"size": "1920x1088", "quality": "high"}),
        StoryboardFailoverStep(
            "pix",
            "banana-2.1",
            {"aspectRatio": "16:9", "imageSize": "2K", "thinkingLevel": "minimal"},
        ),
    ),
)
