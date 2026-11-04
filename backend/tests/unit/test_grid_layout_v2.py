"""Grid layout v2, per-project pin and storyboard-time croppability (USER_DECISIONS §46)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from test_storyboard_generation import Clock, Ids, Provider, Storage, create, image_digest, setup

from tubeflow_test.adapters.storyboard_image_qc import PillowStoryboardTechnicalQc
from tubeflow_test.application.storyboard_failover import (
    StoryboardFailoverPolicy,
    StoryboardFailoverStep,
)
from tubeflow_test.application.storyboard_prompt import build_grid_sheet_prompt
from tubeflow_test.application.storyboards import (
    STORYBOARD_PANEL_LAYOUT_UNREADABLE,
    ProviderPoll,
    StoryboardGenerationService,
)
from tubeflow_test.domain.storyboards import (
    GRID_LAYOUT_V1,
    GRID_LAYOUT_V2,
    DirectorShot,
    GenerationStatus,
    GridLayout,
    ShotTimeRange,
    grid_layout_for,
)

# Golden values produced by main at 3af818d (before §46) for the same inputs.
V1_PROMPT_GOLDEN = {
    (1, "16:9"): "1624bf4e0b9cd3f97845ee90e4d1cfd953789806e0fe603fe89d0c1037eb5d4b",
    (1, "9:16"): "1624bf4e0b9cd3f97845ee90e4d1cfd953789806e0fe603fe89d0c1037eb5d4b",
    (2, "16:9"): "b12d55b4ab5ec5f6db77fcaac22f5273ffa83261ad103162847ce8cf7ad29201",
    (2, "9:16"): "097e66c5a81cc3ef55c04aaced235d17c21a6500133eabe7833fe2ffdd76a887",
    (3, "16:9"): "e6b4084eb2fe9c0fc6ef8a6eb75ff1cd4786d2afa8c204cc5b75fbee7564f7e3",
    (3, "9:16"): "cd57d22bdcb83cc434fb51edcc874b8f73dbae263191b6b6b76031978c03244b",
    (4, "16:9"): "13a1aebdfa2f4b3cb893c315b8ee0701fbae6eb15804ca84cd5f64a1a57bf077",
    (4, "9:16"): "13a1aebdfa2f4b3cb893c315b8ee0701fbae6eb15804ca84cd5f64a1a57bf077",
    (5, "16:9"): "a8bfbd4f5ccba8155d75afd5999741d21e4b30bab408b073c82e81d21ee5bfe0",
    (5, "9:16"): "b919be7aa2871dc19ee3fda3c2f8daed53bac6cbe6c58711b3d44db7104c97d7",
    (6, "16:9"): "78686afcaed36c0fac67e6a1d2c0f5692c55650971618737e121eb5f3b413c82",
    (6, "9:16"): "efb50368ca8c29cf551e0b690effab009bf2c6dad0ea9740ae42a72d7000ffc5",
}


def _shots(count: int) -> tuple[DirectorShot, ...]:
    return tuple(
        DirectorShot(
            UUID(int=index),
            index,
            f"scene {index}",
            "medium",
            "eye level",
            f"action {index}",
            "explain",
            f"description {index}",
        )
        for index in range(1, count + 1)
    )


def _ranges(count: int) -> tuple[ShotTimeRange, ...]:
    return tuple(
        ShotTimeRange(index, (index - 1) * 2000, index * 2000) for index in range(1, count + 1)
    )


def _prompt(count: int, ratio: str, version: str | None = None) -> str:
    kwargs = {} if version is None else {"layout_version": version}
    return build_grid_sheet_prompt(
        style="ink wash",
        aspect_ratio=ratio,
        shots=_shots(count),
        ranges=_ranges(count),
        character_count=1,
        **kwargs,
    )


# --- layout tables ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, GridLayout(1, 1)),
        (2, GridLayout(1, 2)),
        (3, GridLayout(1, 3)),
        (4, GridLayout(2, 2)),
        (5, GridLayout(2, 3, (6,))),
        (6, GridLayout(2, 3)),
    ],
)
def test_v1_landscape_layout_is_unchanged(count: int, expected: GridLayout) -> None:
    assert grid_layout_for(count, "16:9") == expected
    assert grid_layout_for(count, "16:9", GRID_LAYOUT_V1) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, GridLayout(1, 1)),
        (2, GridLayout(2, 2, (3, 4))),
        (3, GridLayout(2, 2, (4,))),
        (4, GridLayout(2, 2)),
        (5, GridLayout(2, 3, (6,))),
        (6, GridLayout(2, 3)),
    ],
)
def test_v2_landscape_layout_uses_2x2_for_two_to_four_shots(
    count: int, expected: GridLayout
) -> None:
    assert grid_layout_for(count, "16:9", GRID_LAYOUT_V2) == expected


def test_portrait_layout_is_the_same_in_both_versions() -> None:
    for count in range(1, 7):
        assert grid_layout_for(count, "9:16", GRID_LAYOUT_V2) == grid_layout_for(count, "9:16")


def test_unknown_layout_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="grid layout version"):
        grid_layout_for(3, "16:9", "grid-v9")


# --- storyboard prompt --------------------------------------------------------------


@pytest.mark.parametrize(("count", "ratio"), sorted(V1_PROMPT_GOLDEN))
def test_v1_storyboard_prompt_stays_byte_identical(count: int, ratio: str) -> None:
    for version in (None, GRID_LAYOUT_V1):
        digest = hashlib.sha256(_prompt(count, ratio, version).encode()).hexdigest()
        assert digest == V1_PROMPT_GOLDEN[(count, ratio)]


def test_v2_prompt_asks_for_solid_black_gutters_and_blank_cells() -> None:
    prompt = _prompt(3, "16:9", GRID_LAYOUT_V2)
    assert "strict grid of 2 rows and 2 columns of equal-sized panels" in prompt
    assert "solid black gutters about 12 pixels wide" in prompt
    assert "Do not draw white frames" in prompt
    assert "Panel 4 must stay completely black and empty." in prompt
    assert "Panel 3 (row 2, column 1)" in prompt
    assert "thin uniform black gutters" not in prompt
    two = _prompt(2, "16:9", GRID_LAYOUT_V2)
    assert "Panel 3 must stay completely black and empty." in two
    assert "Panel 4 must stay completely black and empty." in two
    # One shot stays a single full-frame panel.
    assert _prompt(1, "16:9", GRID_LAYOUT_V2) == _prompt(1, "16:9")


# --- per-project pin ----------------------------------------------------------------


def _close(repo, generation) -> None:  # type: ignore[no-untyped-def]
    """Settle a generation so the in-memory repository accepts the next one for the group."""
    repo.generations[generation.generation_id] = replace(generation, status=GenerationStatus.FAILED)


def _service(repo, tmp_path: Path, *, switch: bool, check=None) -> StoryboardGenerationService:  # type: ignore[no-untyped-def]
    return StoryboardGenerationService(
        repo,
        Provider(),
        Storage(),
        PillowStoryboardTechnicalQc(),
        Ids(),
        Clock(),
        tmp_path,
        StoryboardFailoverPolicy(
            steps=(StoryboardFailoverStep("contract", "fake-v1", {"seed": 7}),)
        ),
        single_panel_new_projects=switch,
        panel_layout_check=check,
    )


def test_switch_off_keeps_new_projects_on_v1(tmp_path: Path) -> None:
    repo, service, _provider, owner, group, revision = setup(tmp_path)
    generation = create(service, owner, group, revision.revision_id)
    assert generation.grid_layout_version == GRID_LAYOUT_V1
    assert repo.project_grid_layout_version(owner, revision.project_id) == GRID_LAYOUT_V1


def test_switch_on_pins_v2_at_first_storyboard_and_keeps_it(tmp_path: Path) -> None:
    repo, _service_off, _provider, owner, group, revision = setup(tmp_path)
    assert repo.project_grid_layout_version(owner, revision.project_id) is None
    service = _service(repo, tmp_path, switch=True)
    first = create(service, owner, group, revision.revision_id, key="first")
    assert first.grid_layout_version == GRID_LAYOUT_V2
    assert "equal-sized panels" not in first.prompt  # one shot = full frame either way
    _close(repo, first)
    # Turning the switch off later does not move an already pinned project.
    later = create(
        _service(repo, tmp_path, switch=False), owner, group, revision.revision_id, key="later"
    )
    assert later.grid_layout_version == GRID_LAYOUT_V2


def test_project_with_a_v1_storyboard_stays_v1_when_switch_turns_on(tmp_path: Path) -> None:
    repo, service, _provider, owner, group, revision = setup(tmp_path)
    _close(repo, create(service, owner, group, revision.revision_id, key="old"))
    pinned = create(
        _service(repo, tmp_path, switch=True), owner, group, revision.revision_id, key="new"
    )
    assert pinned.grid_layout_version == GRID_LAYOUT_V1


def test_grid_layout_version_rejects_unknown_values(tmp_path: Path) -> None:
    repo, service, _provider, owner, group, revision = setup(tmp_path)
    generation = create(service, owner, group, revision.revision_id)
    with pytest.raises(ValueError, match="grid layout version"):
        replace(generation, grid_layout_version="grid-v9")


# --- storyboard-time croppability ---------------------------------------------------


def _poll() -> ProviderPoll:
    return ProviderPoll(GenerationStatus.SUCCEEDED, "ok", "token", image_digest())


def test_v2_sheet_that_cannot_be_cropped_is_regenerated(tmp_path: Path) -> None:
    repo, _service_off, provider, owner, group, revision = setup(tmp_path)
    calls: list[tuple[int, str, str]] = []

    def check(path: Path, *, panel_count: int, aspect_ratio: str, layout_version: str) -> None:
        assert path.exists()
        calls.append((panel_count, aspect_ratio, layout_version))
        raise ValueError("shot_panel_gutter_not_found")

    service = _service(repo, tmp_path, switch=True, check=check)
    service._provider = provider  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id)
    assert service.resume(first.generation_id, owner).status is GenerationStatus.SUBMITTED
    provider.polls = [_poll()]
    failed = service.resume(first.generation_id, owner)

    assert calls == [(1, "16:9", GRID_LAYOUT_V2)]
    assert failed.status is GenerationStatus.FAILED
    assert failed.failure_stage == "download_or_qc"
    assert failed.failure_code == STORYBOARD_PANEL_LAYOUT_UNREADABLE
    assert repo.get_current_generation(owner, group) is None
    successor = repo.find_generation_by_key(owner, f"auto-retry-{first.generation_id}")
    assert successor is not None
    assert successor.status is GenerationStatus.INTENT_PERSISTED
    assert successor.grid_layout_version == GRID_LAYOUT_V2
    assert successor.prompt == first.prompt
    assert successor.failover_total_attempt == 2


def test_v2_sheet_that_crops_is_accepted(tmp_path: Path) -> None:
    repo, _service_off, provider, owner, group, revision = setup(tmp_path)
    calls: list[str] = []

    def check(path: Path, *, panel_count: int, aspect_ratio: str, layout_version: str) -> None:
        calls.append(layout_version)

    service = _service(repo, tmp_path, switch=True, check=check)
    service._provider = provider  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    provider.polls = [_poll()]
    done = service.resume(first.generation_id, owner)
    assert calls == [GRID_LAYOUT_V2]
    assert done.status is not GenerationStatus.FAILED
    assert done.failure_code is None


def test_v1_sheet_is_never_crop_checked(tmp_path: Path) -> None:
    repo, _service_off, provider, owner, group, revision = setup(tmp_path)

    def check(path: Path, **_kwargs: object) -> None:
        raise AssertionError("v1 storyboards must not be crop-checked")

    service = _service(repo, tmp_path, switch=False, check=check)
    service._provider = provider  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    provider.polls = [_poll()]
    assert service.resume(first.generation_id, owner).failure_code is None


def test_v2_without_a_configured_check_fails_closed(tmp_path: Path) -> None:
    from tubeflow_test.domain.errors import DomainError

    repo, _service_off, provider, owner, group, revision = setup(tmp_path)
    service = _service(repo, tmp_path, switch=True, check=None)
    service._provider = provider  # noqa: SLF001
    first = create(service, owner, group, revision.revision_id)
    service.resume(first.generation_id, owner)
    provider.polls = [_poll()]
    with pytest.raises(DomainError) as raised:
        service.resume(first.generation_id, owner)
    assert raised.value.code == "storyboard_panel_layout_check_not_configured"


def test_sql_input_json_carries_the_layout_key_only_for_v2(tmp_path: Path) -> None:
    from tubeflow_test.adapters.persistence.storyboards.models import StoryboardGenerationRow
    from tubeflow_test.adapters.persistence.storyboards.repository import (
        _copy_generation,
        _grid_layout_version,
    )

    _repo, service, _provider, owner, group, revision = setup(tmp_path)
    generation = create(service, owner, group, revision.revision_id)
    v1_row, v2_row = StoryboardGenerationRow(), StoryboardGenerationRow()
    _copy_generation(v1_row, generation)
    _copy_generation(v2_row, replace(generation, grid_layout_version=GRID_LAYOUT_V2))
    assert "grid_layout_version" not in v1_row.input_json
    assert v2_row.input_json["grid_layout_version"] == GRID_LAYOUT_V2
    assert _grid_layout_version(v1_row.input_json) == GRID_LAYOUT_V1
    assert _grid_layout_version(v2_row.input_json) == GRID_LAYOUT_V2
