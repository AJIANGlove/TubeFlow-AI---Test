from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

from tubeflow_test.application.composition_outbox import CompositionDispatchReceipt
from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.composition import (
    CompositionRequest,
    GroupInput,
    NarrationSegmentInput,
    SentenceTiming,
    render_srt,
    subtitle_bindings,
)
from tubeflow_test.domain.errors import DomainError

COMPOSITION_AUTO_MAX_ATTEMPTS = 3
COMPOSITION_AUTO_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 30.0)
# media_command_cancelled：容器/Worker 崩溃把 FFmpeg 杀掉也表现为该码，属临时故障；
# 真实清理（项目删除触发的取消）由 schedule_auto_retry 的 purge 门禁拦住，不会误重试。
_COMPOSITION_TRANSIENT_ERROR_CODES = frozenset(
    {"media_command_failed", "media_command_timeout", "media_command_cancelled"}
)


def _is_transient_composition_failure(error: BaseException) -> bool:
    if isinstance(error, DomainError):
        return error.code in _COMPOSITION_TRANSIENT_ERROR_CODES
    return isinstance(error, Exception)


@dataclass(frozen=True, slots=True)
class CompositionVersion:
    id: UUID
    video_asset_id: str
    srt_asset_id: str
    manifest: dict[str, object]
    current: bool = False


@dataclass(frozen=True, slots=True)
class CompositionAttempt:
    id: UUID
    state: str
    attempt: int
    error_code: str | None
    composition_version_id: UUID | None
    workflow_generation: int


@dataclass(frozen=True, slots=True)
class CompositionClaim:
    execution_token: UUID | None
    existing: CompositionVersion | None = None


class CompositionRepository(Protocol):
    def load_authoritative(self, project_id: UUID, owner_id: UUID) -> CompositionRequest | None: ...

    def capture_revision(self, project_id: UUID, owner_id: UUID) -> tuple[UUID, str]: ...

    def enqueue_composition(
        self, project_id: UUID, owner_id: UUID
    ) -> CompositionDispatchReceipt: ...

    def retry_composition(
        self, project_id: UUID, owner_id: UUID
    ) -> CompositionDispatchReceipt: ...

    def load_revision(
        self, revision_id: UUID, project_id: UUID, owner_id: UUID
    ) -> CompositionRequest: ...

    def activate_candidate(
        self,
        project_id: UUID,
        owner_id: UUID,
        video: Path,
        srt: bytes,
        manifest: dict[str, object],
        idempotency_key: str,
        execution_token: UUID,
    ) -> CompositionVersion: ...

    def begin_composition(
        self,
        project_id: UUID,
        owner_id: UUID,
        idempotency_key: str,
        workflow_generation: int,
    ) -> CompositionClaim: ...

    def fail_composition(
        self, idempotency_key: str, execution_token: UUID, error_code: str
    ) -> None: ...

    def schedule_auto_retry(
        self,
        project_id: UUID,
        owner_id: UUID,
        idempotency_key: str,
        execution_token: UUID,
        *,
        max_attempts: int,
        backoff_seconds: tuple[float, ...],
    ) -> bool: ...

    def current(self, project_id: UUID, owner_id: UUID) -> CompositionVersion | None: ...
    def project_production_mode(self, project_id: UUID, owner_id: UUID) -> str | None: ...
    def history(self, project_id: UUID, owner_id: UUID) -> tuple[CompositionVersion, ...]: ...
    def attempts(self, project_id: UUID, owner_id: UUID) -> tuple[CompositionAttempt, ...]: ...
    def select_current(
        self, project_id: UUID, owner_id: UUID, version_id: UUID
    ) -> CompositionVersion: ...


class CompositionMediaPort(Protocol):
    def compose(self, request: CompositionRequest) -> tuple[Path, dict[str, object]]: ...


class ManualCompositionService:
    def __init__(
        self,
        repository: CompositionRepository,
        composer: CompositionMediaPort,
        *,
        key_scope: Callable[[UUID, UUID], str] | None = None,
    ) -> None:
        self._repository = repository
        self._composer = composer
        # §15.9：AUTO 项目的合成幂等键按预算轮次命名空间隔离；MANUAL 恒为空串。
        self._key_scope = key_scope or (lambda _project_id, _owner_id: "")

    def compose(self, project_id: UUID, owner_id: UUID) -> CompositionVersion:
        request = self._repository.load_authoritative(project_id, owner_id)
        if request is None:
            raise DomainError("project_not_found", "project was not found")
        return self._compose_request(request, project_id, owner_id)

    def _compose_request(
        self,
        request: CompositionRequest,
        project_id: UUID,
        owner_id: UUID,
        workflow_generation: int = 1,
    ) -> CompositionVersion:
        srt = render_srt(request.sentences, total_duration_ms=request.target_duration_ms).encode()
        inputs_hash = hashlib.sha256(
            json.dumps(asdict(request), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        idempotency_key = (
            f"compose:{owner_id}:{project_id}:{inputs_hash}"
            f"{self._key_scope(project_id, owner_id)}"
        )
        claim = self._repository.begin_composition(
            project_id, owner_id, idempotency_key, workflow_generation
        )
        if claim.existing is not None:
            return claim.existing
        if claim.execution_token is None:
            raise DomainError("composition_in_progress", "composition is already running")
        video: Path | None = None
        try:
            video, manifest = self._composer.compose(request)
            payload = {
                "project_id": request.project_id,
                "snapshot_sha256": request.snapshot_sha256,
                "sentences": list(subtitle_bindings(request.sentences)),
                "srt_sha256": hashlib.sha256(srt).hexdigest(),
                "inputs_sha256": inputs_hash,
                "composition": json.loads(json.dumps(manifest)),
            }
            version = self._repository.activate_candidate(
                project_id,
                owner_id,
                video,
                srt,
                payload,
                idempotency_key,
                claim.execution_token,
            )
            if (
                self._repository.project_production_mode(project_id, owner_id) == "AUTO"
                and self._repository.current(project_id, owner_id) is None
            ):
                # §15.10：AUTO 项目首版成功即自动切换为当前成片，用户可直接观看。
                return self._repository.select_current(project_id, owner_id, version.id)
            return version
        except BaseException as error:
            error_code = error.code if isinstance(error, DomainError) else "composition_failed"
            self._repository.fail_composition(
                idempotency_key, claim.execution_token, error_code
            )
            if _is_transient_composition_failure(error):
                self._repository.schedule_auto_retry(
                    project_id,
                    owner_id,
                    idempotency_key,
                    claim.execution_token,
                    max_attempts=COMPOSITION_AUTO_MAX_ATTEMPTS,
                    backoff_seconds=COMPOSITION_AUTO_RETRY_BACKOFF_SECONDS,
                )
            raise
        finally:
            if video is not None:
                video.unlink(missing_ok=True)

    def current(self, project_id: UUID, owner_id: UUID) -> CompositionVersion:
        current = self._repository.current(project_id, owner_id)
        if current is None:
            raise DomainError("not_found", "project or current composition was not found")
        return current

    def history(self, project_id: UUID, owner_id: UUID) -> tuple[CompositionVersion, ...]:
        return self._repository.history(project_id, owner_id)

    def attempts(self, project_id: UUID, owner_id: UUID) -> tuple[CompositionAttempt, ...]:
        return self._repository.attempts(project_id, owner_id)

    def select_current(
        self, project_id: UUID, owner_id: UUID, version_id: UUID
    ) -> CompositionVersion:
        return self._repository.select_current(project_id, owner_id, version_id)

    def compose_revision(
        self,
        revision_id: UUID,
        project_id: UUID,
        owner_id: UUID,
        workflow_generation: int = 1,
    ) -> CompositionVersion:
        request = self._repository.load_revision(revision_id, project_id, owner_id)
        return self._compose_request(request, project_id, owner_id, workflow_generation)


def composition_from_payload(payload: dict[str, object]) -> CompositionRequest:
    group_payloads = cast(list[dict[str, Any]], payload["groups"])
    groups = tuple(
        GroupInput(
            str(group["id"]),
            int(group["order"]),
            str(group["video_asset_id"]),
            tuple(
                NarrationSegmentInput(
                    str(segment["sentence_id"]),
                    str(segment["audio_asset_id"]),
                    int(segment["duration_ms"]),
                    int(segment["sample_count"]),
                    int(segment["sample_rate_hz"]),
                )
                for segment in group["narration_segments"]
            ),
            int(group["target_duration_ms"]),
        )
        for group in group_payloads
    )
    sentence_payloads = cast(list[dict[str, Any]], payload["sentences"])
    sentences = tuple(
        SentenceTiming(
            str(item["id"]),
            int(item["order"]),
            str(item["text"]),
            str(item["text_sha256"]),
            int(item["start_ms"]),
            int(item["end_ms"]),
        )
        for item in sentence_payloads
    )
    return CompositionRequest(
        str(payload["project_id"]),
        str(payload["snapshot_sha256"]),
        groups,
        sentences,
        AspectRatio(str(payload["aspect_ratio"])),
        int(cast(Any, payload["width"])),
        int(cast(Any, payload["height"])),
        int(cast(Any, payload["fps"])),
        int(cast(Any, payload.get("group_width", payload["width"]))),
        int(cast(Any, payload.get("group_height", payload["height"]))),
        int(cast(Any, payload.get("crf", 23))),
        float(cast(Any, payload.get("sharpen", 0.0))),
    )
