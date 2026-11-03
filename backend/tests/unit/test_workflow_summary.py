from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.domain.common import AspectRatio, SupportedLocale
from tubeflow_test.identity_project.adapters import MemoryRepository
from tubeflow_test.identity_project.domain import (
    Account,
    AccountRole,
    Project,
    ProjectSnapshot,
    SnapshotCharacter,
    VoiceMediaFact,
)
from tubeflow_test.identity_project.service import IdentityProjectService
from tubeflow_test.video_generation.orchestration import DELIVER_WITH_DEFECT_MARKER


class Passwords:
    def hash(self, password: str) -> str:
        return password

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == password


class WorkflowRepository(MemoryRepository):
    def __init__(
        self,
        counts: dict[str, int],
        attempt_states: dict[str, str | None] | None = None,
    ) -> None:
        super().__init__()
        self.counts = counts
        self.attempt_states = attempt_states or {
            "project_director": None,
            "director": None,
            "storyboard": None,
            "video": None,
            "composition": None,
        }

    def workflow_counts(self, owner_id: UUID, project_id: UUID) -> dict[str, int]:
        del owner_id, project_id
        return dict(self.counts)

    def workflow_attempt_states(
        self, owner_id: UUID, project_id: UUID
    ) -> dict[str, str | None]:
        del owner_id, project_id
        return dict(self.attempt_states)


def _counts(**overrides: int) -> dict[str, int]:
    values = {
        "sentences": 0,
        "ready_sentences": 0,
        "grouped_sentences": 0,
        "groups": 0,
        "blocked": 0,
        "director_preplan_total_units": 0,
        "director_preplan_completed_units": 0,
        "director_preplan_revisions": 0,
        "director_materializations": 0,
        "directed": 0,
        "director_ready": 0,
        "storyboards": 0,
        "videos": 0,
        "compositions": 0,
        "current_description": 0,
    }
    values.update(overrides)
    if "director_ready" not in overrides:
        values["director_ready"] = values["directed"]
    return values


def _setup(
    counts: dict[str, int],
    *,
    snapshot: bool = False,
    purging: bool = False,
    attempt_states: dict[str, str | None] | None = None,
) -> tuple[IdentityProjectService, Account, Project]:
    owner = Account(uuid4(), "owner", "hash", AccountRole.USER)
    image_id = uuid4()
    project = Project(
        id=uuid4(),
        owner_id=owner.id,
        channel_id=uuid4(),
        script="A real saved script.",
        character_image_ids=(image_id,),
        visual_style="cinematic",
        locale=SupportedLocale.EN_US,
        aspect_ratio=AspectRatio.LANDSCAPE,
        voice_version_id=uuid4(),
    )
    if snapshot:
        media = VoiceMediaFact(uuid4(), "a" * 64, "audio/wav", 10)
        project = replace(
            project,
            snapshot=ProjectSnapshot(
                script=project.script,
                characters=(
                    SnapshotCharacter(0, image_id, uuid4(), "b" * 64, "image/png", 20),
                ),
                visual_style=project.visual_style,
                locale=project.locale,
                aspect_ratio=project.aspect_ratio,
                channel_url="https://example.test/channel",
                expected_duration_seconds=None,
                confirmed_at=datetime(2026, 8, 29, tzinfo=UTC),
                voice_version_id=project.voice_version_id,
                voice_metadata_sha256="c" * 64,
                voice_catalog_source_sha256="d" * 64,
                voice_reference_zip=media,
                voice_preview=media,
                voice_avatar=media,
            ),
        )
    if purging:
        project = replace(project, production_state="PURGING")
    repo = WorkflowRepository(counts, attempt_states)
    repo.projects[project.id] = project
    return IdentityProjectService(repo, Passwords()), owner, project


def _stage(summary: dict[str, object], key: str) -> dict[str, object]:
    stages = summary["stages"]
    assert isinstance(stages, list)
    return next(stage for stage in stages if stage["key"] == key)


def test_draft_summary_preserves_top_level_contract_and_projects_eleven_stages() -> None:
    service, owner, project = _setup(_counts())
    summary = service.workflow_summary(owner, project.id)

    assert summary["stage"] == "DRAFT"
    assert summary["blocking_reason"] is None
    assert summary["allowed_actions"] == ["edit_project", "confirm_narration"]
    assert summary["counts"] == {"sentences": 0, "groups": 0, "videos": 0}
    stages = summary["stages"]
    assert isinstance(stages, list)
    assert [(stage["key"], stage["label"]) for stage in stages] == [
        ("project_configuration", "项目配置"),
        ("script", "文案"),
        ("character_ip", "角色 IP"),
        ("visual_style", "画面风格"),
        ("narration_settings", "配音设置"),
        ("narration_timeline", "配音与时间线"),
        ("director_storyboard", "导演与故事板"),
        ("group_video_preview", "分组视频与预览"),
        ("composition_export", "合成与导出"),
        ("cover", "封面"),
        ("description", "简介"),
    ]
    assert [stage["status"] for stage in stages] == [
        "complete",
        "complete",
        "complete",
        "complete",
        "current",
        "blocked",
        "blocked",
        "blocked",
        "blocked",
        "blocked",
        "blocked",
    ]
    assert all(
        {
            "key",
            "label",
            "status",
            "blocking_reason",
            "allowed_actions",
            "counts",
        }
        <= stage.keys()
        for stage in stages
    )
    assert _stage(summary, "narration_timeline")["blocking_reason"] == (
        "narration_not_confirmed"
    )


def test_incomplete_narration_exposes_only_untimed_preplanning_and_blocks_later_stages() -> None:
    incomplete = _counts(
        sentences=2,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
    )
    service, owner, project = _setup(incomplete, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "NARRATION"
    assert _stage(summary, "narration_timeline")["status"] == "current"
    director = _stage(summary, "director_storyboard")
    assert director["status"] == "current"
    assert director["mode"] == "PREPLANNING"
    assert director["allowed_actions"] == ["RUN_DIRECTOR_PREPLAN"]
    assert all(
        _stage(summary, key)["status"] == "blocked"
        for key in ("group_video_preview", "composition_export")
    )

    blocked = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        blocked=1,
    )
    service, owner, project = _setup(blocked, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "VIDEO"
    assert summary["blocking_reason"] == "all_groups_video_blocked"
    assert _stage(summary, "narration_timeline")["status"] == "complete"
    assert _stage(summary, "group_video_preview")["status"] == "complete"
    composition = _stage(summary, "composition_export")
    assert composition["status"] == "blocked"
    assert composition["blocking_reason"] == "all_groups_video_blocked"


def test_blocked_groups_are_skipped_and_normal_groups_continue_to_video() -> None:
    counts = _counts(
        sentences=2,
        ready_sentences=2,
        grouped_sentences=2,
        groups=2,
        blocked=1,
        directed=1,
        storyboards=1,
    )
    service, owner, project = _setup(counts, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "VIDEO"
    assert summary["blocking_reason"] == "videos_incomplete"
    assert "generate_group_video" in summary["allowed_actions"]
    assert _stage(summary, "director_storyboard")["status"] == "complete"
    video_stage = _stage(summary, "group_video_preview")
    assert video_stage["status"] == "current"
    assert "generate_group_video" in video_stage["allowed_actions"]

    ready = _counts(
        sentences=2,
        ready_sentences=2,
        grouped_sentences=2,
        groups=2,
        blocked=1,
        directed=1,
        storyboards=1,
        videos=1,
    )
    service, owner, project = _setup(ready, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "VIDEO_READY"
    assert summary["blocking_reason"] is None
    assert "compose" in summary["allowed_actions"]
    assert _stage(summary, "group_video_preview")["status"] == "complete"
    composition = _stage(summary, "composition_export")
    assert composition["status"] == "current"
    assert "compose" in composition["allowed_actions"]
    assert composition["counts"]["skipped_groups"] == 1


@pytest.mark.parametrize(
    ("overrides", "current_key", "current_action"),
    (
        ({}, "director_storyboard", "RUN_DIRECTOR_PREPLAN"),
        ({"directed": 1}, "director_storyboard", "generate_storyboard"),
        (
            {"directed": 1, "storyboards": 1},
            "group_video_preview",
            "generate_group_video",
        ),
        (
            {"directed": 1, "storyboards": 1, "videos": 1},
            "composition_export",
            "compose",
        ),
    ),
)
def test_ready_authority_facts_unlock_exactly_the_next_stage(
    overrides: dict[str, int], current_key: str, current_action: str
) -> None:
    counts = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        **overrides,
    )
    service, owner, project = _setup(counts, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    current = _stage(summary, current_key)
    assert current["status"] == "current"
    assert current_action in current["allowed_actions"]


def test_current_composition_completes_export_stage_and_purge_blocks_all_stages() -> None:
    counts = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        directed=1,
        storyboards=1,
        videos=1,
        compositions=1,
    )
    service, owner, project = _setup(counts, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    final_stage = _stage(summary, "composition_export")
    assert final_stage["status"] == "complete"
    assert final_stage["allowed_actions"] == ["compose", "export"]

    service, owner, project = _setup(counts, snapshot=True, purging=True)
    purging = service.workflow_summary(owner, project.id)
    assert purging["stage"] == "DELETING"
    assert all(stage["status"] == "blocked" for stage in purging["stages"])
    assert all(stage["blocking_reason"] == "project_purging" for stage in purging["stages"])


def test_cover_stage_facts_never_block_or_gate_the_summary() -> None:
    base = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        directed=1,
        storyboards=1,
        videos=1,
        compositions=1,
    )
    service, owner, project = _setup(base, snapshot=True)
    without_cover = service.workflow_summary(owner, project.id)
    cover = _stage(without_cover, "cover")
    assert cover["status"] == "current"
    assert cover["blocking_reason"] is None
    assert cover["allowed_actions"] == ["generate_cover_prompt", "generate_cover_image"]
    assert cover["counts"] == {
        "cover_prompts": 0,
        "cover_images": 0,
        "current_cover_prompt": 0,
        "current_cover_image": 0,
    }

    with_facts_service, with_facts_owner, with_facts_project = _setup(
        {
            **base,
            "cover_prompts": 2,
            "cover_images": 3,
            "current_cover_prompt": 1,
            "current_cover_image": 1,
        },
        snapshot=True,
    )
    summary = with_facts_service.workflow_summary(with_facts_owner, with_facts_project.id)
    cover = _stage(summary, "cover")
    assert cover["status"] == "complete"
    assert cover["blocking_reason"] is None
    assert cover["counts"] == {
        "cover_prompts": 2,
        "cover_images": 3,
        "current_cover_prompt": 1,
        "current_cover_image": 1,
    }
    # 封面事实不改变顶层阶段、blocking_reason、徽章动作与其他阶段门禁。
    assert summary["stage"] == without_cover["stage"]
    assert summary["blocking_reason"] == without_cover["blocking_reason"]
    assert summary["allowed_actions"] == without_cover["allowed_actions"]
    assert summary["counts"] == without_cover["counts"]
    for key in (
        "project_configuration",
        "narration_timeline",
        "director_storyboard",
        "group_video_preview",
        "composition_export",
    ):
        assert _stage(summary, key) == _stage(without_cover, key)


def test_description_stage_is_optional_and_gated_by_current_final_srt() -> None:
    blocked_counts = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        directed=1,
        storyboards=1,
        videos=1,
        compositions=0,
    )
    service, owner, project = _setup(blocked_counts, snapshot=True)
    description = _stage(service.workflow_summary(owner, project.id), "description")
    assert description["status"] == "blocked"
    assert description["blocking_reason"] == "description_composition_required"
    assert description["allowed_actions"] == []

    ready_counts = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        directed=1,
        storyboards=1,
        videos=1,
        compositions=1,
        current_description=1,
    )
    service, owner, project = _setup(ready_counts, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    description = _stage(summary, "description")
    assert description["status"] == "complete"
    assert description["blocking_reason"] is None
    assert description["allowed_actions"] == ["generate_description"]
    assert description["counts"] == {
        "current_descriptions": 1,
        "current_compositions": 1,
    }
    # 可选简介事实不改变顶层阶段、主流程动作与既有阶段事实。
    without_service, without_owner, without_project = _setup(
        {**ready_counts, "current_description": 0}, snapshot=True
    )
    without_description = without_service.workflow_summary(
        without_owner, without_project.id
    )
    assert summary["stage"] == without_description["stage"]
    assert summary["allowed_actions"] == without_description["allowed_actions"]
    assert _stage(summary, "cover") == _stage(without_description, "cover")


def test_cover_stage_blocks_only_on_purging_or_unfrozen_project() -> None:
    counts = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        directed=1,
        storyboards=1,
        videos=1,
        current_cover_image=1,
    )
    service, owner, project = _setup(counts, snapshot=False)
    cover = _stage(service.workflow_summary(owner, project.id), "cover")
    assert cover["status"] == "blocked"
    assert cover["blocking_reason"] == "narration_not_confirmed"
    assert cover["allowed_actions"] == []
    # 未冻结项目即使有封面事实计数也不显示 complete。
    service, owner, project = _setup(counts, snapshot=True, purging=True)
    cover = _stage(service.workflow_summary(owner, project.id), "cover")
    assert cover["status"] == "blocked"
    assert cover["blocking_reason"] == "project_purging"

    # 权威计数不一致时封面付费动作一并关闭（fail-closed），但事实照常展示。
    inconsistent = _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        directed=2,
        current_cover_image=1,
    )
    service, owner, project = _setup(inconsistent, snapshot=True)
    summary = service.workflow_summary(owner, project.id)
    cover = _stage(summary, "cover")
    assert cover["status"] == "complete"
    assert cover["blocking_reason"] is None
    assert cover["allowed_actions"] == []
    assert summary["blocking_reason"] == "director_authority_inconsistent"


def _production_counts_before(kind: str, *, current: bool = False) -> dict[str, int]:
    completed = {
        "director": (1, 0, 0, 0),
        "storyboard": (1, 1, 0, 0),
        "video": (1, 1, 1, 0),
        "composition": (1, 1, 1, 1),
    }
    directed, storyboards, videos, compositions = completed[kind]
    if not current:
        if kind == "director":
            directed = 0
        elif kind == "storyboard":
            storyboards = 0
        elif kind == "video":
            videos = 0
        elif kind == "composition":
            compositions = 0
    return _counts(
        sentences=1,
        ready_sentences=1,
        grouped_sentences=1,
        groups=1,
        directed=directed,
        storyboards=storyboards,
        videos=videos,
        compositions=compositions,
    )


@pytest.mark.parametrize(
    ("kind", "running_state", "expected_reason"),
    (
        ("project_director", "RUNNING", "director_preplan_running"),
        ("storyboard", "RUNNING", "storyboard_attempt_running"),
        ("video", "RUNNING", "video_attempt_running"),
        ("composition", "RUNNING", "composition_attempt_running"),
    ),
)
def test_latest_running_without_current_only_allows_refresh(
    kind: str, running_state: str, expected_reason: str
) -> None:
    attempts = {
        name: None
        for name in ("project_director", "director", "storyboard", "video", "composition")
    }
    attempts[kind] = running_state
    service, owner, project = _setup(
        _production_counts_before("director" if kind == "project_director" else kind),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert summary["allowed_actions"] == ["REFRESH"]
    assert summary["blocking_reason"] == expected_reason


@pytest.mark.parametrize(
    ("kind", "expected_actions"),
    (
        ("project_director", ["REGENERATE_DIRECTOR_PREPLAN", "REFRESH"]),
        ("storyboard", ["RECONCILE", "REFRESH"]),
        ("video", ["RECONCILE", "REFRESH"]),
        ("composition", ["REFRESH"]),
    ),
)
def test_latest_remote_unknown_attempt_only_exposes_supported_recovery(
    kind: str, expected_actions: list[str]
) -> None:
    attempts = {
        name: None
        for name in ("project_director", "director", "storyboard", "video", "composition")
    }
    attempts[kind] = "REMOTE_STATE_UNKNOWN"
    service, owner, project = _setup(
        _production_counts_before("director" if kind == "project_director" else kind),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert summary["allowed_actions"] == expected_actions
    expected_reason = (
        "director_preplan_remote_state_unknown"
        if kind == "project_director"
        else f"{kind}_attempt_remote_state_unknown"
    )
    assert summary["blocking_reason"] == expected_reason
    assert "generate_director_plan" not in summary["allowed_actions"]
    assert "generate_storyboard" not in summary["allowed_actions"]
    assert "generate_group_video" not in summary["allowed_actions"]
    assert "compose" not in summary["allowed_actions"]


@pytest.mark.parametrize(
    ("kind", "expected_actions"),
    (
        ("project_director", ["RETRY"]),
        ("storyboard", ["generate_storyboard", "REFRESH"]),
        ("video", ["generate_group_video", "REFRESH"]),
        ("composition", ["RETRY_COMPOSITION", "REFRESH"]),
    ),
)
def test_latest_terminal_failure_without_current_allows_only_existing_manual_retry(
    kind: str, expected_actions: list[str]
) -> None:
    attempts = {
        name: None
        for name in ("project_director", "director", "storyboard", "video", "composition")
    }
    attempts[kind] = "FAILED"
    service, owner, project = _setup(
        _production_counts_before("director" if kind == "project_director" else kind),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert summary["allowed_actions"] == expected_actions
    expected_reason = (
        "director_preplan_failed"
        if kind == "project_director"
        else f"{kind}_attempt_failed"
    )
    assert summary["blocking_reason"] == expected_reason
    if kind not in {"composition", "project_director"}:
        assert all("retry" not in action.casefold() for action in summary["allowed_actions"])


@pytest.mark.parametrize(
    ("kind", "state", "downstream_action", "same_generation"),
    (
        (
            "project_director",
            "RUNNING",
            "generate_storyboard",
            "REGENERATE_DIRECTOR_PREPLAN",
        ),
        ("storyboard", "REMOTE_STATE_UNKNOWN", "generate_group_video", "generate_storyboard"),
        ("video", "RUNNING", "compose", "generate_group_video"),
        ("composition", "RUNNING", "export", "compose"),
    ),
)
def test_old_current_remains_usable_during_new_non_successful_attempt(
    kind: str, state: str, downstream_action: str, same_generation: str
) -> None:
    attempts = {
        name: None
        for name in ("project_director", "director", "storyboard", "video", "composition")
    }
    attempts[kind] = state
    service, owner, project = _setup(
        _production_counts_before(
            "director" if kind == "project_director" else kind, current=True
        ),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert downstream_action in summary["allowed_actions"]
    assert same_generation not in summary["allowed_actions"]
    assert "REFRESH" in summary["allowed_actions"]
    if state == "REMOTE_STATE_UNKNOWN":
        assert "RECONCILE" in summary["allowed_actions"]


@pytest.mark.parametrize(
    ("kind", "state", "downstream_action", "retry_action"),
    (
        ("project_director", "FAILED", "generate_storyboard", "RETRY"),
        ("storyboard", "FAILED", "generate_group_video", "generate_storyboard"),
        ("video", "FAILED", "compose", "generate_group_video"),
    ),
)
def test_old_current_and_latest_terminal_failure_allow_downstream_and_manual_retry(
    kind: str, state: str, downstream_action: str, retry_action: str
) -> None:
    attempts = {
        name: None
        for name in ("project_director", "director", "storyboard", "video", "composition")
    }
    attempts[kind] = state
    service, owner, project = _setup(
        _production_counts_before(
            "director" if kind == "project_director" else kind, current=True
        ),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert downstream_action in summary["allowed_actions"]
    assert retry_action in summary["allowed_actions"]
    if kind != "project_director":
        assert "REFRESH" in summary["allowed_actions"]


def test_old_current_and_latest_cancelled_video_preserve_downstream_without_retry() -> None:
    attempts = {
        "director": None,
        "storyboard": None,
        "video": "CANCELLED",
        "composition": None,
    }
    service, owner, project = _setup(
        _production_counts_before("video", current=True),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert "compose" in summary["allowed_actions"]
    assert "REFRESH" in summary["allowed_actions"]
    assert "generate_group_video" not in summary["allowed_actions"]


def test_old_current_composition_and_latest_failure_preserve_export_and_retry() -> None:
    attempts = {
        "director": None,
        "storyboard": None,
        "video": None,
        "composition": "FAILED",
    }
    service, owner, project = _setup(
        _production_counts_before("composition", current=True),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert summary["allowed_actions"] == ["export", "RETRY_COMPOSITION", "REFRESH"]
    assert "compose" not in summary["allowed_actions"]


def test_unarchived_succeeded_video_remains_recovery_only() -> None:
    attempts = {
        "director": None,
        "storyboard": None,
        "video": "SUCCEEDED",
        "composition": None,
    }
    service, owner, project = _setup(
        _production_counts_before("video"),
        snapshot=True,
        attempt_states=attempts,
    )

    summary = service.workflow_summary(owner, project.id)

    assert summary["allowed_actions"] == ["RECONCILE", "REFRESH"]
    assert summary["blocking_reason"] == "video_attempt_running"
    assert "generate_group_video" not in summary["allowed_actions"]


@pytest.mark.parametrize(
    ("overrides", "expected_stage", "expected_reason", "stage_key"),
    (
        ({"directed": 2}, "DIRECTING", "director_authority_inconsistent", "director_storyboard"),
        (
            {"directed": 1, "storyboards": 2},
            "STORYBOARD",
            "storyboard_authority_inconsistent",
            "director_storyboard",
        ),
        (
            {"directed": 1, "storyboards": 1, "videos": 2},
            "VIDEO",
            "video_authority_inconsistent",
            "group_video_preview",
        ),
    ),
)
def test_excess_authority_counts_fail_closed_in_summary_and_stages(
    overrides: dict[str, int], expected_stage: str, expected_reason: str, stage_key: str
) -> None:
    service, owner, project = _setup(
        _counts(
            sentences=1,
            ready_sentences=1,
            grouped_sentences=1,
            groups=1,
            **overrides,
        ),
        snapshot=True,
    )

    summary = service.workflow_summary(owner, project.id)

    assert summary["stage"] == expected_stage
    assert summary["blocking_reason"] == expected_reason
    assert summary["allowed_actions"] == ["REFRESH"]
    assert _stage(summary, stage_key)["status"] == "blocked"
    assert _stage(summary, stage_key)["blocking_reason"] == expected_reason
    assert not any(
        action.startswith("generate") or action == "compose"
        for stage in summary["stages"]
        for action in stage["allowed_actions"]
    )


def test_memory_attempt_states_prioritize_unsafe_state_across_groups() -> None:
    owner_id, project_id, group_one, group_two = (uuid4() for _ in range(4))
    repository = MemoryRepository()
    repository.workflow_group_attempt_states.update(
        {
            (owner_id, project_id, "storyboard", group_one): "RUNNING",
            (owner_id, project_id, "storyboard", group_two): "SUCCEEDED",
            (owner_id, project_id, "video", group_one): "REMOTE_STATE_UNKNOWN",
            (owner_id, project_id, "video", group_two): "ARCHIVED",
        }
    )

    states = repository.workflow_attempt_states(owner_id, project_id)

    assert states["storyboard"] == "RUNNING"
    assert states["video"] == "REMOTE_STATE_UNKNOWN"


@pytest.mark.parametrize("kind", ("director", "storyboard", "video", "composition"))
def test_latest_succeeded_attempt_or_no_attempt_preserves_normal_next_action(kind: str) -> None:
    attempts = {name: None for name in ("director", "storyboard", "video", "composition")}
    attempts[kind] = "ARCHIVED" if kind == "video" else "SUCCEEDED"
    service, owner, project = _setup(
        _counts(
            sentences=1,
            ready_sentences=1,
            grouped_sentences=1,
            groups=1,
            directed=1,
            storyboards=1,
            videos=1,
        ),
        snapshot=True,
        attempt_states=attempts,
    )

    assert service.workflow_summary(owner, project.id)["allowed_actions"] == ["compose"]

    service, owner, project = _setup(
        _counts(
            sentences=1,
            ready_sentences=1,
            grouped_sentences=1,
            groups=1,
            directed=1,
            storyboards=1,
            videos=1,
        ),
        snapshot=True,
    )
    assert service.workflow_summary(owner, project.id)["allowed_actions"] == ["compose"]


def test_sql_counts_only_qced_and_current_authority_facts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = (
        "CREATE TABLE authoritative_narration_sentences ("
        "id TEXT PRIMARY KEY, owner_id TEXT, project_id TEXT, snapshot_revision INTEGER, "
        "raw_qc_passed BOOLEAN, normalized_qc_passed BOOLEAN, measured_frame_count INTEGER, "
        "sample_rate_hz INTEGER, measured_duration_ms INTEGER, start_frame INTEGER, "
        "end_frame INTEGER, start_ms INTEGER, end_ms INTEGER)",
        "CREATE TABLE authoritative_narration_groups ("
        "id TEXT PRIMARY KEY, owner_id TEXT, project_id TEXT, snapshot_revision INTEGER, "
        "video_blocked BOOLEAN)",
        "CREATE TABLE authoritative_narration_memberships (sentence_id TEXT, group_id TEXT)",
        "CREATE TABLE project_director_runs ("
        "id TEXT, owner_id TEXT, project_id TEXT, total_units INTEGER, status TEXT, "
        "retry_blocked BOOLEAN, created_at TEXT)",
        "CREATE TABLE project_director_units ("
        "owner_id TEXT, project_id TEXT, run_id TEXT, status TEXT)",
        "CREATE TABLE project_director_current ("
        "owner_id TEXT, project_id TEXT, revision_id TEXT)",
        "CREATE TABLE project_director_materializations ("
        "owner_id TEXT, project_id TEXT, preplan_revision_id TEXT)",
        "CREATE TABLE storyboard_director_revisions ("
        "id TEXT, owner_id TEXT, review_state TEXT)",
        "CREATE TABLE storyboard_director_pointers ("
        "owner_id TEXT, group_id TEXT, draft_revision_id TEXT, applied_revision_id TEXT)",
        "CREATE TABLE storyboard_current (owner_id TEXT, group_id TEXT, generation_id TEXT)",
        "CREATE TABLE storyboard_generations ("
        "id TEXT, owner_id TEXT, project_id TEXT, group_id TEXT, status TEXT, version INTEGER)",
        "CREATE TABLE storyboard_media (generation_id TEXT)",
        "CREATE TABLE video_generation_attempts ("
        "attempt_id TEXT, owner_id TEXT, project_id TEXT, group_id TEXT, "
        "stage TEXT, archive_stage TEXT)",
        "CREATE TABLE group_current_videos (group_id TEXT, attempt_id TEXT)",
        "CREATE TABLE composition_versions ("
        "owner_id TEXT, project_id TEXT, current BOOLEAN)",
        "CREATE TABLE cover_prompt_generations (owner_id TEXT, project_id TEXT)",
        "CREATE TABLE cover_image_generations (owner_id TEXT, project_id TEXT)",
            "CREATE TABLE projects (id TEXT, owner_id TEXT, "
            "current_cover_prompt_generation_id TEXT, current_cover_image_generation_id TEXT)",
            "CREATE TABLE project_descriptions (owner_id TEXT, project_id TEXT, current BOOLEAN)",
        )
    owner, project, group, historical_group, blocked_group = (
        uuid4().hex for _ in range(5)
    )
    sentence, invalid_sentence, generation, attempt, historical_attempt = (
        uuid4().hex for _ in range(5)
    )
    with engine.begin() as connection:
        for statement in tables:
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO authoritative_narration_sentences VALUES "
                "(:sentence,:owner,:project,1,1,1,48000,48000,1000,0,48000,0,1000),"
                "(:invalid,:owner,:project,1,1,0,0,48000,0,48000,48000,1000,1000)"
            ),
            {
                "sentence": sentence,
                "invalid": invalid_sentence,
                "owner": owner,
                "project": project,
            },
        )
        connection.execute(
            text(
                "INSERT INTO authoritative_narration_groups VALUES "
                "(:group,:owner,:project,1,0),(:historical,:owner,:project,1,0),"
                "(:blocked_group,:owner,:project,1,1)"
            ),
            {
                "group": group,
                "historical": historical_group,
                "blocked_group": blocked_group,
                "owner": owner,
                "project": project,
            },
        )
        connection.execute(
            text("INSERT INTO authoritative_narration_memberships VALUES (:sentence,:group)"),
            {"sentence": sentence, "group": group},
        )
        connection.execute(
            text(
                "INSERT INTO storyboard_director_pointers VALUES "
                "(:owner,:group,NULL,'revision')"
            ),
            {"owner": owner, "group": group},
        )
        connection.execute(
            text("INSERT INTO storyboard_current VALUES (:owner,:group,:generation)"),
            {"owner": owner, "group": group, "generation": generation},
        )
        connection.execute(
            text(
                "INSERT INTO storyboard_generations VALUES "
                "(:generation,:owner,:project,:group,'SUCCEEDED',1)"
            ),
            {
                "generation": generation,
                "owner": owner,
                "project": project,
                "group": group,
            },
        )
        connection.execute(
            text("INSERT INTO storyboard_media VALUES (:generation)"),
            {"generation": generation},
        )
        connection.execute(
            text(
                "INSERT INTO video_generation_attempts VALUES "
                "(:attempt,:owner,:project,:group,'ARCHIVED','COMPLETE'),"
                "(:historical_attempt,:owner,:project,:historical_group,'ARCHIVED','COMPLETE')"
            ),
            {
                "attempt": attempt,
                "historical_attempt": historical_attempt,
                "owner": owner,
                "project": project,
                "group": group,
                "historical_group": historical_group,
            },
        )
        connection.execute(
            text("INSERT INTO group_current_videos VALUES (:group,:attempt)"),
            {"group": group, "attempt": attempt},
        )
        connection.execute(
            text(
                "INSERT INTO storyboard_director_pointers VALUES "
                "(:owner,:blocked_group,NULL,'revision')"
            ),
            {"owner": owner, "blocked_group": blocked_group},
        )
        connection.execute(
            text("INSERT INTO storyboard_current VALUES (:owner,:group,:generation)"),
            {"owner": owner, "group": blocked_group, "generation": attempt},
        )
        connection.execute(
            text(
                "INSERT INTO storyboard_generations VALUES "
                "(:generation,:owner,:project,:group,'SUCCEEDED',1)"
            ),
            {
                "generation": attempt,
                "owner": owner,
                "project": project,
                "group": blocked_group,
            },
        )
        connection.execute(
            text("INSERT INTO storyboard_media VALUES (:generation)"),
            {"generation": attempt},
        )
        connection.execute(
            text("INSERT INTO group_current_videos VALUES (:group,:attempt)"),
            {"group": blocked_group, "attempt": historical_attempt},
        )
        connection.execute(
            text(
                "INSERT INTO composition_versions VALUES "
                "(:owner,:project,1),(:owner,:project,0)"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO cover_prompt_generations VALUES "
                "(:owner,:project),(:owner,:project),(:owner,:other_project)"
            ),
            {"owner": owner, "project": project, "other_project": historical_group},
        )
        connection.execute(
            text("INSERT INTO cover_image_generations VALUES (:owner,:project)"),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text("INSERT INTO projects VALUES (:project,:owner,'prompt-1','image-1')"),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO project_descriptions VALUES "
                "(:owner,:project,1),(:owner,:project,0),(:other,:project,1),"
                "(:owner,:other_project,1)"
            ),
            {
                "owner": owner,
                "project": project,
                "other": uuid4().hex,
                "other_project": uuid4().hex,
            },
        )

    with Session(engine) as session:
        result = SqlAlchemyIdentityProjectRepository(session).workflow_counts(  # type: ignore[arg-type]
            owner, project
        )
    assert result == {
        "sentences": 2,
        "ready_sentences": 1,
        "groups": 3,
        "grouped_sentences": 1,
        "blocked": 1,
        "director_preplan_total_units": 0,
        "director_preplan_completed_units": 0,
        "director_preplan_revisions": 0,
        "director_materializations": 0,
        "directed": 1,
        "director_ready": 1,
        "storyboards": 1,
        "videos": 1,
        "compositions": 1,
        "cover_prompts": 2,
        "cover_images": 1,
        "current_cover_prompt": 1,
        "current_cover_image": 1,
        "current_description": 1,
    }


def test_sql_attempt_states_use_latest_owned_attempts_and_composition_intent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = (
        "CREATE TABLE project_director_runs (id TEXT, owner_id TEXT, project_id TEXT, "
        "status TEXT, retry_blocked BOOLEAN, created_at TEXT)",
        "CREATE TABLE authoritative_narration_groups (id TEXT PRIMARY KEY, owner_id TEXT, "
        "project_id TEXT)",
        "CREATE TABLE director_generation_attempts (id TEXT, owner_id TEXT, group_id TEXT, "
        "status TEXT, created_at TEXT)",
        "CREATE TABLE storyboard_generations (id TEXT, owner_id TEXT, project_id TEXT, "
        "group_id TEXT, status TEXT, created_at TEXT, version INTEGER)",
        "CREATE TABLE video_generation_attempts (attempt_id TEXT, owner_id TEXT, project_id TEXT, "
        "group_id TEXT, stage TEXT, intent_sequence INTEGER, updated_at TEXT)",
        "CREATE TABLE composition_input_revisions (id TEXT, owner_id TEXT, project_id TEXT, "
        "inputs_sha256 TEXT, created_at TEXT)",
        "CREATE TABLE composition_outbox (id TEXT, owner_id TEXT, project_id TEXT, "
        "revision_id TEXT, state TEXT, workflow_generation INTEGER, budget_round INTEGER)",
        "CREATE TABLE composition_jobs (id TEXT, owner_id TEXT, project_id TEXT, "
        "idempotency_key TEXT, state TEXT, workflow_generation INTEGER)",
    )
    owner, project, group = (uuid4().hex for _ in range(3))
    other_owner, other_project, other_group = (uuid4().hex for _ in range(3))
    digest = "a" * 64
    with engine.begin() as connection:
        for statement in tables:
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO project_director_runs VALUES "
                "('p1',:owner,:project,'FAILED',0,'2026-08-29T00:00:00Z'),"
                "('p2',:owner,:project,'RUNNING',0,'2026-08-29T00:01:00Z'),"
                "('p3',:other_owner,:other_project,'REMOTE_STATE_UNKNOWN',0,"
                "'2026-08-29T00:02:00Z')"
            ),
            {
                "owner": owner,
                "project": project,
                "other_owner": other_owner,
                "other_project": other_project,
            },
        )
        connection.execute(
            text(
                "INSERT INTO authoritative_narration_groups VALUES "
                "(:group,:owner,:project),(:other_group,:other_owner,:other_project)"
            ),
            {
                "group": group,
                "owner": owner,
                "project": project,
                "other_group": other_group,
                "other_owner": other_owner,
                "other_project": other_project,
            },
        )
        connection.execute(
            text(
                "INSERT INTO director_generation_attempts VALUES "
                "('d1',:owner,:group,'FAILED','2026-08-29T00:00:00Z'),"
                "('d2',:owner,:group,'SUBMITTING','2026-08-29T00:01:00Z'),"
                "('d3',:other_owner,:other_group,'REMOTE_STATE_UNKNOWN','2026-08-29T00:02:00Z')"
            ),
            {
                "owner": owner,
                "group": group,
                "other_owner": other_owner,
                "other_group": other_group,
            },
        )
        connection.execute(
            text(
                "INSERT INTO storyboard_generations VALUES "
                "('s1',:owner,:project,:group,'FAILED','2026-08-29T00:00:00Z',1),"
                "('s2',:owner,:project,:group,'REMOTE_STATE_UNKNOWN','2026-08-28T00:00:00Z',2)"
            ),
            {"owner": owner, "project": project, "group": group},
        )
        connection.execute(
            text(
                "INSERT INTO video_generation_attempts VALUES "
                "('v1',:owner,:project,:group,'FAILED',1,'2026-08-29T00:00:00Z'),"
                "('v2',:owner,:project,:group,'RUNNING',2,'2026-08-29T00:01:00Z')"
            ),
            {"owner": owner, "project": project, "group": group},
        )
        connection.execute(
            text(
                "INSERT INTO composition_input_revisions VALUES "
                "('r1',:owner,:project,:digest,'2026-08-29T00:01:00Z')"
            ),
            {"owner": owner, "project": project, "digest": digest},
        )
        connection.execute(
            text(
                "INSERT INTO composition_outbox VALUES "
                "('o1',:owner,:project,'r1','DISPATCHED',1,1)"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO composition_jobs VALUES "
                "('c1',:owner,:project,:key,'FAILED',1)"
            ),
            {
                "owner": owner,
                "project": project,
                "key": f"compose:{owner}:{project}:{digest}",
            },
        )

    with Session(engine) as session:
        result = SqlAlchemyIdentityProjectRepository(session).workflow_attempt_states(  # type: ignore[arg-type]
            owner, project
        )

    assert result == {
        "project_director": "RUNNING",
        "director": "SUBMITTING",
        "storyboard": "REMOTE_STATE_UNKNOWN",
        "video": "RUNNING",
        "composition": "FAILED",
    }


def test_sql_attempt_states_aggregate_latest_attempt_per_group() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    owner, project, group_one, group_two = (uuid4().hex for _ in range(4))
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE project_director_runs (id TEXT, owner_id TEXT, project_id TEXT, "
            "status TEXT, retry_blocked BOOLEAN, created_at TEXT)",
            "CREATE TABLE authoritative_narration_groups (id TEXT, owner_id TEXT, project_id TEXT)",
            "CREATE TABLE director_generation_attempts (id TEXT, owner_id TEXT, group_id TEXT, "
            "status TEXT, created_at TEXT)",
            "CREATE TABLE storyboard_generations (id TEXT, owner_id TEXT, project_id TEXT, "
            "group_id TEXT, status TEXT, created_at TEXT, version INTEGER)",
            "CREATE TABLE video_generation_attempts (attempt_id TEXT, owner_id TEXT, "
            "project_id TEXT, group_id TEXT, stage TEXT, intent_sequence INTEGER, "
            "updated_at TEXT)",
            "CREATE TABLE composition_input_revisions (id TEXT, owner_id TEXT, project_id TEXT, "
            "inputs_sha256 TEXT, created_at TEXT)",
            "CREATE TABLE composition_outbox (id TEXT, owner_id TEXT, project_id TEXT, "
            "revision_id TEXT, state TEXT, workflow_generation INTEGER, budget_round INTEGER)",
            "CREATE TABLE composition_jobs (id TEXT, owner_id TEXT, project_id TEXT, "
            "idempotency_key TEXT, state TEXT, workflow_generation INTEGER)",
        ):
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO authoritative_narration_groups VALUES "
                "(:one,:owner,:project),(:two,:owner,:project)"
            ),
            {"one": group_one, "two": group_two, "owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO storyboard_generations VALUES "
                "('s1',:owner,:project,:one,'RUNNING','2026-08-29T00:00:00Z',1),"
                "('s2',:owner,:project,:two,'SUCCEEDED','2026-08-29T00:01:00Z',1)"
            ),
            {"owner": owner, "project": project, "one": group_one, "two": group_two},
        )
        connection.execute(
            text(
                "INSERT INTO video_generation_attempts VALUES "
                "('v1',:owner,:project,:one,'RUNNING',1,'2026-08-29T00:02:00Z'),"
                "('v2',:owner,:project,:two,'ARCHIVED',2,'2026-08-29T00:00:00Z')"
            ),
            {"owner": owner, "project": project, "one": group_one, "two": group_two},
        )

    with Session(engine) as session:
        states = SqlAlchemyIdentityProjectRepository(session).workflow_attempt_states(  # type: ignore[arg-type]
            owner, project
        )

    assert states["storyboard"] == "RUNNING"
    assert states["video"] == "RUNNING"


def test_sql_newer_composition_outbox_generation_overrides_stale_failed_job() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    owner, project, digest = uuid4().hex, uuid4().hex, "c" * 64
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE project_director_runs (id TEXT, owner_id TEXT, project_id TEXT, "
            "status TEXT, retry_blocked BOOLEAN, created_at TEXT)",
            "CREATE TABLE authoritative_narration_groups (id TEXT, owner_id TEXT, project_id TEXT)",
            "CREATE TABLE director_generation_attempts (id TEXT, owner_id TEXT, group_id TEXT, "
            "status TEXT, created_at TEXT)",
            "CREATE TABLE storyboard_generations (id TEXT, owner_id TEXT, project_id TEXT, "
            "group_id TEXT, status TEXT, created_at TEXT, version INTEGER)",
            "CREATE TABLE video_generation_attempts (attempt_id TEXT, owner_id TEXT, "
            "project_id TEXT, group_id TEXT, stage TEXT, intent_sequence INTEGER, "
            "updated_at TEXT)",
            "CREATE TABLE composition_input_revisions (id TEXT, owner_id TEXT, project_id TEXT, "
            "inputs_sha256 TEXT, created_at TEXT)",
            "CREATE TABLE composition_outbox (id TEXT, owner_id TEXT, project_id TEXT, "
            "revision_id TEXT, state TEXT, workflow_generation INTEGER, budget_round INTEGER)",
            "CREATE TABLE composition_jobs (id TEXT, owner_id TEXT, project_id TEXT, "
            "idempotency_key TEXT, state TEXT, workflow_generation INTEGER)",
        ):
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO composition_input_revisions VALUES "
                "('r1',:owner,:project,:digest,'2026-08-29T00:01:00Z')"
            ),
            {"owner": owner, "project": project, "digest": digest},
        )
        connection.execute(
            text(
                "INSERT INTO composition_outbox VALUES "
                "('o1',:owner,:project,'r1','PENDING',2,1)"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO composition_jobs VALUES "
                "('c1',:owner,:project,:key,'FAILED',1)"
            ),
            {
                "owner": owner,
                "project": project,
                "key": f"compose:{owner}:{project}:{digest}",
            },
        )

    with Session(engine) as session:
        states = SqlAlchemyIdentityProjectRepository(session).workflow_attempt_states(  # type: ignore[arg-type]
            owner, project
        )

    assert states["composition"] == "PENDING"


def test_sql_video_defect_groups_only_current_marked_attempts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    owner, project = uuid4().hex, uuid4().hex
    group_one, group_two, group_three, foreign_group = (uuid4().hex for _ in range(4))
    current_two, current_one, current_three, current_foreign, historical = (
        uuid4().hex for _ in range(5)
    )
    defect_probe = json.dumps(
        {
            "qc_passed": False,
            "error_code": "video_content_black_region",
            "content_qc": {
                "algorithm": "sustained_black_region_v1",
                "findings": [
                    {
                        "start_second": 0.0,
                        "end_second": 4.0,
                        "bbox_columns": [0, 1279],
                        "bbox_rows": [0, 719],
                        "area_fraction": 0.97,
                    },
                    # 缺数值时段的 finding 不进入摘要。
                    {"bbox_columns": [0, 10]},
                ],
            },
        }
    )
    marked_evidence = json.dumps([DELIVER_WITH_DEFECT_MARKER])
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE authoritative_narration_groups (id TEXT PRIMARY KEY, "
            "owner_id TEXT, project_id TEXT, sequence INTEGER)",
            "CREATE TABLE video_generation_attempts (attempt_id TEXT, owner_id TEXT, "
            "project_id TEXT, group_id TEXT, retry_evidence TEXT, probe_evidence_json TEXT)",
            "CREATE TABLE group_current_videos (group_id TEXT, attempt_id TEXT)",
        ):
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO authoritative_narration_groups VALUES "
                "(:one,:owner,:project,1),(:two,:owner,:project,2),"
                "(:three,:owner,:project,3),(:foreign,:other_owner,:other_project,9)"
            ),
            {
                "one": group_one,
                "two": group_two,
                "three": group_three,
                "foreign": foreign_group,
                "owner": owner,
                "project": project,
                "other_owner": uuid4().hex,
                "other_project": uuid4().hex,
            },
        )
        attempts = (
            # 组 2：当前视频带病交付（标记 + content_qc 证据）。
            (current_two, owner, project, group_two, marked_evidence, defect_probe),
            # 组 1：当前视频健康；同组历史 attempt 带标记（非当前指针，不计入）。
            (current_one, owner, project, group_one, json.dumps([]),
             json.dumps({"qc_passed": True})),
            (historical, owner, project, group_one, marked_evidence, defect_probe),
            # 组 3：标记但 probe 缺 content_qc → 计入且 findings 为空，不伪造时段。
            (current_three, owner, project, group_three, marked_evidence, None),
            # 其他 owner/项目的带病交付组不得混入。
            (current_foreign, uuid4().hex, uuid4().hex, foreign_group,
             marked_evidence, defect_probe),
        )
        connection.execute(
            text(
                "INSERT INTO video_generation_attempts VALUES "
                "(:attempt,:owner,:project,:group,:evidence,:probe)"
            ),
            [
                {
                    "attempt": attempt,
                    "owner": attempt_owner,
                    "project": attempt_project,
                    "group": group,
                    "evidence": evidence,
                    "probe": probe,
                }
                for attempt, attempt_owner, attempt_project, group, evidence, probe in attempts
            ],
        )
        connection.execute(
            text("INSERT INTO group_current_videos VALUES (:group,:attempt)"),
            [
                {"group": group_two, "attempt": current_two},
                {"group": group_one, "attempt": current_one},
                {"group": group_three, "attempt": current_three},
                {"group": foreign_group, "attempt": current_foreign},
            ],
        )

    with Session(engine) as session:
        facts = SqlAlchemyIdentityProjectRepository(session).video_defect_groups(  # type: ignore[arg-type]
            owner, project
        )

    assert facts == (
        {
            "group_sequence": 2,
            "error_code": "video_content_black_region",
            "findings": [{"start_second": 0.0, "end_second": 4.0}],
        },
        {"group_sequence": 3, "error_code": "video_content_black_region", "findings": []},
    )


def test_sql_completed_at_and_latest_composition_dispatched_at() -> None:
    # §17.2 精确版豁免的两个时间源：COMPLETED 完成时间与最新合成行派发时间。
    engine = create_engine("sqlite+pysqlite:///:memory:")
    owner, project = uuid4().hex, uuid4().hex
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE composition_input_revisions (id TEXT, owner_id TEXT, project_id TEXT, "
            "created_at TEXT)",
            "CREATE TABLE composition_outbox (id TEXT, owner_id TEXT, project_id TEXT, "
            "revision_id TEXT, dispatched_at TEXT)",
            "CREATE TABLE auto_pipeline_runs (id TEXT, owner_id TEXT, project_id TEXT, "
            "state TEXT, updated_at TEXT, created_at TEXT)",
        ):
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO composition_input_revisions VALUES "
                "('r1',:owner,:project,'2026-09-17T15:32:09Z')"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO composition_input_revisions VALUES "
                "('r2',:owner,:project,'2026-09-17T16:03:51Z')"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO composition_outbox VALUES "
                "('o1',:owner,:project,'r1','2026-09-17T16:44:41Z')"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO composition_outbox VALUES "
                "('o2',:owner,:project,'r2','2026-09-17T16:12:02Z')"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO auto_pipeline_runs VALUES "
                "('run1',:owner,:project,'RUNNING','2026-09-17T10:00:00Z','2026-09-16T16:29:22Z')"
            ),
            {"owner": owner, "project": project},
        )
        connection.execute(
            text(
                "INSERT INTO auto_pipeline_runs VALUES "
                "('run2',:owner,:project,'COMPLETED','2026-09-17T16:12:26Z','2026-09-17T11:00:00Z')"
            ),
            {"owner": owner, "project": project},
        )

    with Session(engine) as session:
        repo = SqlAlchemyIdentityProjectRepository(session)  # type: ignore[arg-type]
        # 与 workflow_attempt_states 同口径：最新 revision（r2）行的 dispatched_at。
        assert repo.latest_composition_dispatched_at(owner, project) == "2026-09-17T16:12:02Z"
        assert repo.auto_pipeline_completed_at(owner, project) == "2026-09-17T16:12:26Z"
        # 无 COMPLETED 运行 / 无合成行的项目返回 None。
        assert repo.auto_pipeline_completed_at(owner, uuid4().hex) is None
        assert repo.latest_composition_dispatched_at(owner, uuid4().hex) is None


def test_sql_composition_pending_outbox_is_running_before_job_creation() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    owner, project, digest = uuid4().hex, uuid4().hex, "b" * 64
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE project_director_runs (id TEXT, owner_id TEXT, project_id TEXT, "
            "status TEXT, retry_blocked BOOLEAN, created_at TEXT)",
            "CREATE TABLE authoritative_narration_groups (id TEXT, owner_id TEXT, project_id TEXT)",
            "CREATE TABLE director_generation_attempts (id TEXT, owner_id TEXT, group_id TEXT, "
            "status TEXT, created_at TEXT)",
            "CREATE TABLE storyboard_generations (id TEXT, owner_id TEXT, project_id TEXT, "
            "group_id TEXT, status TEXT, created_at TEXT, version INTEGER)",
            "CREATE TABLE video_generation_attempts (attempt_id TEXT, owner_id TEXT, "
            "project_id TEXT, group_id TEXT, stage TEXT, intent_sequence INTEGER, "
            "updated_at TEXT)",
            "CREATE TABLE composition_input_revisions (id TEXT, owner_id TEXT, project_id TEXT, "
            "inputs_sha256 TEXT, created_at TEXT)",
            "CREATE TABLE composition_outbox (id TEXT, owner_id TEXT, project_id TEXT, "
            "revision_id TEXT, state TEXT, workflow_generation INTEGER, budget_round INTEGER)",
            "CREATE TABLE composition_jobs (id TEXT, owner_id TEXT, project_id TEXT, "
            "idempotency_key TEXT, state TEXT, workflow_generation INTEGER)",
        ):
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO composition_input_revisions VALUES "
                "('r1',:owner,:project,:digest,'2026-08-29T00:01:00Z')"
            ),
            {"owner": owner, "project": project, "digest": digest},
        )
        connection.execute(
            text(
                "INSERT INTO composition_outbox VALUES "
                "('o1',:owner,:project,'r1','PENDING',1,1)"
            ),
            {"owner": owner, "project": project},
        )

    with Session(engine) as session:
        result = SqlAlchemyIdentityProjectRepository(session).workflow_attempt_states(  # type: ignore[arg-type]
            owner, project
        )

    assert result["composition"] == "PENDING"


def test_project_flow_reports_draft_stage_and_zero_counts() -> None:
    service, owner, project = _setup(_counts())
    assert service.project_flow(owner, project) == {
        "stage": "DRAFT",
        "blocking_reason": None,
        "attention": False,
        "sentences": 0,
        "ready_sentences": 0,
        "groups": 0,
        "videos": 0,
        "compositions": 0,
    }


def test_project_flow_mirrors_workflow_summary_stage_and_counts() -> None:
    counts = _counts(
        sentences=4,
        ready_sentences=4,
        grouped_sentences=4,
        groups=2,
        videos=1,
        compositions=1,
    )
    service, owner, project = _setup(counts, snapshot=True)
    flow = service.project_flow(owner, project)
    summary = service.workflow_summary(owner, project.id)
    assert summary["stage"] == "DIRECTING"
    assert flow == {
        "stage": summary["stage"],
        "blocking_reason": summary["blocking_reason"],
        "attention": False,
        "sentences": 4,
        "ready_sentences": 4,
        "groups": 2,
        "videos": 1,
        "compositions": 1,
    }


def _failed_attempt_states(kind: str, status: str) -> dict[str, str | None]:
    attempt_states: dict[str, str | None] = {
        "project_director": None,
        "director": None,
        "storyboard": None,
        "video": None,
        "composition": None,
    }
    attempt_states[kind] = status
    return attempt_states


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("project_director", "FAILED"),
        ("project_director", "FAILED_RETRY_BLOCKED"),
        ("director", "FAILED"),
        ("storyboard", "FAILED"),
        ("video", "FAILED"),
        ("composition", "FAILED"),
    ],
)
def test_project_flow_attention_flags_latest_failed_attempts(kind: str, status: str) -> None:
    service, owner, project = _setup(
        _counts(), attempt_states=_failed_attempt_states(kind, status)
    )
    assert service.project_flow(owner, project)["attention"] is True


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("project_director", "RUNNING"),
        ("storyboard", "REMOTE_STATE_UNKNOWN"),
        ("video", "ARCHIVED"),
        ("composition", "SUCCEEDED"),
    ],
)
def test_project_flow_attention_ignores_non_failed_attempts(kind: str, status: str) -> None:
    service, owner, project = _setup(
        _counts(), attempt_states=_failed_attempt_states(kind, status)
    )
    assert service.project_flow(owner, project)["attention"] is False


@pytest.mark.parametrize(
    ("video_state", "videos", "reason"),
    [
        (None, 0, "videos_incomplete"),
        ("ARCHIVED", 1, "videos_incomplete"),
        ("CREATED", 0, "video_attempt_running"),
        ("STAGING", 0, "video_attempt_running"),
        ("NOT_SUBMITTED", 0, "video_attempt_running"),
        ("SUBMITTING", 0, "video_attempt_running"),
        ("SUBMITTED", 0, "video_attempt_running"),
        ("RUNNING", 1, "video_attempt_running"),
        ("SUCCEEDED", 0, "video_attempt_running"),
        ("REMOTE_STATE_UNKNOWN", 0, "video_attempt_remote_state_unknown"),
        ("FAILED", 0, "video_attempt_failed"),
        ("CANCELLED", 0, "video_attempt_cancelled"),
    ],
)
def test_project_flow_exposes_authoritative_video_wait_reason(
    video_state: str | None, videos: int, reason: str
) -> None:
    service, owner, project = _setup(
        _counts(
            sentences=2, ready_sentences=2, grouped_sentences=2,
            groups=2, directed=2, storyboards=2, videos=videos,
        ),
        snapshot=True,
        attempt_states={
            "project_director": None, "director": None,
            "storyboard": "SUCCEEDED", "video": video_state, "composition": None,
        },
    )
    flow = service.project_flow(owner, project)
    summary = service.workflow_summary(owner, project.id)
    assert flow["stage"] == summary["stage"] == "VIDEO"
    assert flow["blocking_reason"] == summary["blocking_reason"] == reason
    assert flow["attention"] is (video_state == "FAILED")
