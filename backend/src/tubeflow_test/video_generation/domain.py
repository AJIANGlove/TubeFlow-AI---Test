from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from types import MappingProxyType
from uuid import UUID

from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.domain.errors import DomainError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VideoRuleViolation(ValueError):
    """A stable business-rule rejection raised before any provider call."""


class VideoAssetIntegrityError(ValueError):
    """A deterministic catalog/object absence or byte-integrity mismatch."""


class RetryableVideoIntentFailure(DomainError):
    """AUTO 项目意图创建的可重试失败（2026-10-21 决策 C3）。

    意图阶段（提示词编译）失败不产生 attempt 行，因此不会进入既有的
    6 次提交预算；该异常把失败交给 AUTO 编排按同一预算做有界重试，
    MANUAL 项目仍按原口径直接失败。
    """


class VideoCapacityUnavailable(DomainError):
    """当前没有可用 H3 节点槽位（容量耗尽，属瞬时事实，不是内容/规则失败）。

    2026-10-31：AUTO 项目必须把它当作「排队等待容量」而不是终态失败；
    MANUAL 项目仍保持同步报错。
    """

    def __init__(self) -> None:
        super().__init__(
            "video_capacity_unavailable", "no compatible H3 node has capacity"
        )


class ReferenceKind(StrEnum):
    CHARACTER = "CHARACTER"
    STORYBOARD = "STORYBOARD"
    SHOT_PANEL = "SHOT_PANEL"


class ReferenceStrategy(StrEnum):
    COMPOSITE = "composite"
    SINGLE_PANEL_V1 = "single_panel_v1"


@dataclass(frozen=True, slots=True)
class ReferencePolicy:
    """Immutable project-level reference policy, independent of node routing."""

    strategy: ReferenceStrategy = ReferenceStrategy.COMPOSITE
    derivation: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.strategy is ReferenceStrategy.COMPOSITE:
            if self.derivation is not None:
                raise VideoRuleViolation("composite_reference_derivation_not_supported")
            return
        if not isinstance(self.derivation, Mapping):
            raise VideoRuleViolation("single_panel_derivation_required")
        version = self.derivation.get("version")
        if not isinstance(version, str) or not version.strip():
            raise VideoRuleViolation("single_panel_derivation_required")
        object.__setattr__(self, "derivation", _deep_freeze(_canonical_json_value(self.derivation)))

    @property
    def derivation_payload(self) -> dict[str, object] | None:
        if self.derivation is None:
            return None
        return {key: json.loads(canonical_json(value)) for key, value in self.derivation.items()}


@dataclass(frozen=True, slots=True)
class ImmutableWorkflowSnapshot:
    model_key: str
    model_version: str
    workflow_id: str
    workflow_version: str
    workflow_manifest_sha256: str
    resolution_preset: str
    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        if not all((self.model_key, self.model_version, self.workflow_id, self.workflow_version)):
            raise VideoRuleViolation("model and workflow snapshot fields must be non-empty")
        if not _SHA256.fullmatch(self.workflow_manifest_sha256):
            raise VideoRuleViolation("workflow manifest sha256 is invalid")
        if self.resolution_preset != "preview":
            raise VideoRuleViolation("KineStack H3 resolution preset must be preview")
        frozen = _deep_freeze(_canonical_json_value(self.manifest))
        object.__setattr__(self, "manifest", frozen)

    @property
    def manifest_parameters_sha256(self) -> str:
        """Canonical business-parameter hash, distinct from the raw manifest file hash."""
        return canonical_sha256(self.manifest)


@dataclass(frozen=True, slots=True)
class BoundReferenceImage:
    asset: MediaAssetRef
    kind: ReferenceKind
    ordinal: int
    binding_key: str
    prompt_label: str


@dataclass(frozen=True, slots=True)
class ShotPanelImage:
    """One immutable shot reference derived from the bound composite storyboard."""

    asset: MediaAssetRef
    shot_order: int
    source_storyboard_sha256: str
    derivation: str

    def __post_init__(self) -> None:
        if self.shot_order < 1:
            raise VideoRuleViolation("shot panel order must be positive")
        if not _SHA256.fullmatch(self.source_storyboard_sha256):
            raise VideoRuleViolation("shot panel source storyboard sha256 is invalid")
        if not self.derivation.strip():
            raise VideoRuleViolation("shot panel derivation must be non-empty")


def provider_duration_seconds(target_duration_ms: int) -> int:
    if target_duration_ms <= 0:
        raise VideoRuleViolation("target duration must be positive")
    seconds = max(3, math.ceil(target_duration_ms / 1000))
    # H3 实际产出可能略短于请求值；余量不足 0.5 秒时 +1 秒保底（合同 §7.8）。
    if seconds < 15 and seconds * 1000 - target_duration_ms < 500:
        seconds += 1
    if seconds > 15:
        raise VideoRuleViolation("a sentence or group exceeds the 15 second video limit")
    return seconds


@dataclass(frozen=True, slots=True)
class GroupVideoGeneration:
    generation_id: UUID
    project_id: UUID
    group_id: UUID
    character_images: tuple[MediaAssetRef, ...]
    storyboard_image: MediaAssetRef
    group_prompt: str
    target_duration_ms: int
    aspect_ratio: AspectRatio
    seed: int | str | None
    snapshot: ImmutableWorkflowSnapshot
    storyboard_generation_id: UUID
    storyboard_applied_revision_id: UUID
    storyboard_asset_sha256: str
    target_sample_count: int | None = None
    target_sample_rate: int | None = None
    shot_panel_images: tuple[ShotPanelImage, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= len(self.character_images) <= 8:
            raise VideoRuleViolation("a group video requires 1 to 8 character images")
        if not self.group_prompt.strip():
            raise VideoRuleViolation("group video prompt must be non-empty")
        if self.target_sample_count is not None or self.target_sample_rate is not None:
            if not self.target_sample_count or not self.target_sample_rate:
                raise VideoRuleViolation("measured group sample duration is incomplete")
            seconds = max(3, math.ceil(Fraction(self.target_sample_count, self.target_sample_rate)))
            if seconds > 15:
                raise VideoRuleViolation("a sentence or group exceeds the 15 second video limit")
        else:
            provider_duration_seconds(self.target_duration_ms)
        _validate_seed(self.seed)
        if self.storyboard_asset_sha256 != self.storyboard_image.sha256:
            raise VideoRuleViolation("bound storyboard SHA-256 does not match the input image")
        if self.shot_panel_images:
            orders = tuple(item.shot_order for item in self.shot_panel_images)
            if len(orders) > 6 or orders != tuple(range(1, len(orders) + 1)):
                raise VideoRuleViolation("shot panel references must be ordered and at most six")
            if len(self.character_images) + len(orders) > 9:
                raise VideoRuleViolation("video reference image limit exceeded")
            if any(
                item.source_storyboard_sha256 != self.storyboard_image.sha256
                for item in self.shot_panel_images
            ):
                raise VideoRuleViolation("shot panel source storyboard SHA-256 mismatch")

    @property
    def references(self) -> tuple[BoundReferenceImage, ...]:
        items = [
            BoundReferenceImage(
                asset,
                ReferenceKind.CHARACTER,
                index,
                f"reference_image_{index}",
                f"@图{_cn(index)}",
            )
            for index, asset in enumerate(self.character_images, 1)
        ]
        if self.shot_panel_images:
            for panel in self.shot_panel_images:
                ordinal = len(items) + 1
                items.append(
                    BoundReferenceImage(
                        panel.asset,
                        ReferenceKind.SHOT_PANEL,
                        ordinal,
                        f"reference_image_{ordinal}",
                        f"@图{_cn(ordinal)}",
                    )
                )
            return tuple(items)
        ordinal = len(items) + 1
        items.append(
            BoundReferenceImage(
                self.storyboard_image,
                ReferenceKind.STORYBOARD,
                ordinal,
                f"reference_image_{ordinal}",
                f"@图{_cn(ordinal)}",
            )
        )
        return tuple(items)

    @property
    def provider_duration_seconds(self) -> int:
        if self.target_sample_count is not None and self.target_sample_rate is not None:
            return provider_duration_seconds(
                round(Fraction(self.target_sample_count * 1000, self.target_sample_rate))
            )
        return provider_duration_seconds(self.target_duration_ms)


def _cn(number: int) -> str:
    return ("一", "二", "三", "四", "五", "六", "七", "八", "九")[number - 1]


def _validate_seed(seed: int | str | None) -> None:
    if seed is None:
        return
    if isinstance(seed, bool):
        raise VideoRuleViolation("seed must not be boolean")
    if isinstance(seed, int):
        if not 0 <= seed <= 2**53 - 1:
            raise VideoRuleViolation("integer seed exceeds the JSON safe integer range")
        return
    if not re.fullmatch(r"0|[1-9][0-9]*", seed):
        raise VideoRuleViolation("string seed must be canonical unsigned decimal")
    value = int(seed)
    if value <= 2**53 - 1 or value > 2**64 - 1:
        raise VideoRuleViolation("string seed must encode a large unsigned 64-bit value")


@dataclass(frozen=True, slots=True)
class ArchivedGroupVideo:
    generation_id: UUID
    attempt_id: UUID
    original_mp4: MediaAssetRef
    provider_size_bytes: int
    provider_sha256: str
    native_audio_preserved_in_original: bool = True
    immutable: bool = True


def canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_sha256(value: object) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _canonical_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise VideoRuleViolation("workflow parameters must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise VideoRuleViolation("workflow parameter keys must be strings")
        return {key: _canonical_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    raise VideoRuleViolation("workflow parameters must be canonical JSON values")


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value
