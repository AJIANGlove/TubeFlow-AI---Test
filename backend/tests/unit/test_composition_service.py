from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from tubeflow_test.application.composition import (
    CompositionClaim,
    CompositionVersion,
    ManualCompositionService,
)
from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.composition import (
    CompositionRequest,
    GroupInput,
    NarrationSegmentInput,
    SentenceTiming,
)
from tubeflow_test.domain.errors import DomainError


class Repository:
    def __init__(self, current: CompositionVersion) -> None:
        self.value = current
        self.switched = False

    def load_authoritative(self, project_id: UUID, owner_id: UUID) -> None:
        return None

    def activate_candidate(
        self,
        project_id: UUID,
        owner_id: UUID,
        video: Path,
        srt: bytes,
        manifest: dict[str, object],
        idempotency_key: str,
        execution_token: UUID,
    ) -> CompositionVersion:
        raise AssertionError("not reached")

    def begin_composition(
        self,
        project_id: UUID,
        owner_id: UUID,
        idempotency_key: str,
        workflow_generation: int,
    ) -> CompositionClaim:
        return CompositionClaim(uuid4())

    def fail_composition(
        self, idempotency_key: str, execution_token: UUID, error_code: str
    ) -> None:
        pass

    def schedule_auto_retry(self, *args, **kwargs) -> bool:
        return False

    def current(self, project_id: UUID, owner_id: UUID) -> CompositionVersion | None:
        return self.value if owner_id == OWNER else None


class Composer:
    pass


OWNER = uuid4()


def test_failed_new_composition_does_not_replace_current_version() -> None:
    old = CompositionVersion(uuid4(), "old-video", "old-srt", {})
    repository = Repository(old)
    service = ManualCompositionService(repository, Composer())  # type: ignore[arg-type]
    with pytest.raises(DomainError) as error:
        service.compose(uuid4(), OWNER)
    assert error.value.code == "project_not_found"
    assert repository.value is old and not repository.switched


def test_current_composition_is_owner_isolated() -> None:
    old = CompositionVersion(uuid4(), "old-video", "old-srt", {})
    service = ManualCompositionService(Repository(old), Composer())  # type: ignore[arg-type]
    assert service.current(uuid4(), OWNER) is old
    with pytest.raises(DomainError) as error:
        service.current(uuid4(), uuid4())
    assert error.value.code == "not_found"


def test_candidate_file_is_cleaned_when_persistence_fails(tmp_path: Path) -> None:
    text = "narration"
    request = CompositionRequest(
        "p",
        "a" * 64,
        (
            GroupInput(
                "g", 0, "video", (NarrationSegmentInput("s", "tts", 1000, 48000, 48000),), 1000
            ),
        ),
        (SentenceTiming("s", 0, text, hashlib.sha256(text.encode()).hexdigest(), 0, 1000),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"video")

    class FailingRepository(Repository):
        def load_authoritative(self, project_id: UUID, owner_id: UUID) -> CompositionRequest:
            return request

        def activate_candidate(self, *args, **kwargs) -> CompositionVersion:
            raise OSError("database failed")

    class Media:
        def compose(self, value: CompositionRequest) -> tuple[Path, dict[str, object]]:
            return candidate, {"trace": "manifest"}

    service = ManualCompositionService(
        FailingRepository(CompositionVersion(uuid4(), "v", "s", {})), Media()
    )
    with pytest.raises(OSError):
        service.compose(uuid4(), OWNER)
    assert not candidate.exists()


def test_running_composition_claim_does_not_execute_ffmpeg_again() -> None:
    text = "narration"
    request = CompositionRequest(
        "p",
        "a" * 64,
        (
            GroupInput(
                "g", 0, "video", (NarrationSegmentInput("s", "tts", 1000, 48000, 48000),), 1000
            ),
        ),
        (SentenceTiming("s", 0, text, hashlib.sha256(text.encode()).hexdigest(), 0, 1000),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )

    class RunningRepository(Repository):
        def load_authoritative(self, project_id: UUID, owner_id: UUID) -> CompositionRequest:
            return request

        def begin_composition(
            self, project_id, owner_id, idempotency_key, workflow_generation
        ) -> CompositionClaim:
            return CompositionClaim(None)

    class RecordingMedia:
        calls = 0

        def compose(self, value):
            self.calls += 1
            raise AssertionError

    media = RecordingMedia()
    service = ManualCompositionService(
        RunningRepository(CompositionVersion(uuid4(), "v", "s", {})), media
    )
    with pytest.raises(DomainError) as error:
        service.compose(uuid4(), OWNER)
    assert error.value.code == "composition_in_progress" and media.calls == 0


def test_revision_execution_does_not_reload_changed_current_inputs(tmp_path: Path) -> None:
    text = "captured"
    captured = CompositionRequest(
        "p",
        "a" * 64,
        (
            GroupInput(
                "g",
                0,
                "captured-video",
                (NarrationSegmentInput("s", "captured-tts", 1000, 48000, 48000),),
                1000,
            ),
        ),
        (SentenceTiming("s", 0, text, hashlib.sha256(text.encode()).hexdigest(), 0, 1000),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"video")

    class RevisionRepository(Repository):
        def load_authoritative(self, project_id, owner_id):
            raise AssertionError("activity must not reload latest inputs")

        def load_revision(self, revision_id, project_id, owner_id):
            return captured

        def activate_candidate(self, *args, **kwargs):
            return self.value

        def project_production_mode(self, project_id, owner_id):
            return "MANUAL"

    class Media:
        seen = None

        def compose(self, value):
            self.seen = value
            return candidate, {}

    repository = RevisionRepository(CompositionVersion(uuid4(), "v", "s", {}))
    media = Media()
    result = ManualCompositionService(repository, media).compose_revision(uuid4(), uuid4(), OWNER)
    assert result is repository.value and media.seen is captured

class _AutoSelectRepository:
    def __init__(self, mode: str, current: CompositionVersion | None) -> None:
        self.mode = mode
        self._current = current
        self.selected: list[UUID] = []
        self.value = CompositionVersion(uuid4(), "video", "srt", {})

    def load_revision(self, revision_id, project_id, owner_id):
        return self.request

    def begin_composition(self, project_id, owner_id, idempotency_key, workflow_generation):
        return CompositionClaim(uuid4())

    def activate_candidate(self, *args, **kwargs):
        return self.value

    def fail_composition(self, *args, **kwargs):
        raise AssertionError("must not fail")

    def schedule_auto_retry(self, *args, **kwargs):
        return False

    def current(self, project_id, owner_id):
        return self._current

    def select_current(self, project_id, owner_id, version_id):
        self.selected.append(version_id)
        return CompositionVersion(version_id, "video", "srt", {}, True)

    def project_production_mode(self, project_id, owner_id):
        return self.mode


def _auto_select_request() -> CompositionRequest:
    text = "narration"
    return CompositionRequest(
        "p",
        "a" * 64,
        (
            GroupInput(
                "g", 0, "video", (NarrationSegmentInput("s", "tts", 1000, 48000, 48000),), 1000
            ),
        ),
        (SentenceTiming("s", 0, text, hashlib.sha256(text.encode()).hexdigest(), 0, 1000),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )


def _ok_composer(path: Path):
    class _Composer:
        def compose(self, request):
            return path, {}

    return _Composer()


def test_auto_first_success_is_auto_selected_as_current(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"video")
    repository = _AutoSelectRepository("AUTO", None)
    repository.request = _auto_select_request()
    service = ManualCompositionService(repository, _ok_composer(candidate))  # type: ignore[arg-type]

    version = service.compose_revision(uuid4(), uuid4(), OWNER, 1)

    # §15.10：AUTO 首版成功自动切换为当前成片。
    assert repository.selected == [repository.value.id]
    assert version.current is True


def test_manual_first_success_is_not_auto_selected(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"video")
    repository = _AutoSelectRepository("MANUAL", None)
    repository.request = _auto_select_request()
    service = ManualCompositionService(repository, _ok_composer(candidate))  # type: ignore[arg-type]

    version = service.compose_revision(uuid4(), uuid4(), OWNER, 1)

    assert repository.selected == []
    assert version.current is False

