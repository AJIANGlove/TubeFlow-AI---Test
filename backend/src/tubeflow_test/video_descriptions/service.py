"""视频简介应用服务：频道模板 CRUD、最终 SRT 门禁、手动生成与版本管理。"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from tubeflow_test.application.ports import Clock
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_descriptions.domain import (
    DEFAULT_DESCRIPTION_PROMPT,
    MIN_SEGMENTS,
    DescriptionAttempt,
    DescriptionPageState,
    DescriptionPayload,
    DescriptionProjectSource,
    DescriptionTemplate,
    ProjectDescription,
    assemble_description,
    build_description_messages,
    description_input_sha256,
    parse_description_payload,
    validate_description_content,
    validate_template_prompt,
)
from tubeflow_test.video_descriptions.ports import (
    DescriptionProvider,
    DescriptionProviderResult,
)

GENERATION_TEMPERATURE = 0.4


class VideoDescriptionRepository(Protocol):
    def channel_exists(self, owner_id: UUID, channel_id: UUID) -> bool: ...

    def ensure_default_template(
        self, owner_id: UUID, channel_id: UUID, prompt: str, now: datetime
    ) -> None: ...

    def list_templates(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[DescriptionTemplate, ...]: ...

    def create_template(
        self, owner_id: UUID, channel_id: UUID, prompt: str, now: datetime
    ) -> DescriptionTemplate: ...

    def update_template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID, prompt: str, now: datetime
    ) -> DescriptionTemplate: ...

    def delete_template(self, owner_id: UUID, channel_id: UUID, template_id: UUID) -> None: ...

    def template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID
    ) -> DescriptionTemplate | None: ...

    def project_ref(self, owner_id: UUID, project_id: UUID) -> tuple[UUID, str] | None:
        """返回 (channel_id, channel_name_or_url)；缺失/跨账号返回 None。"""
        ...

    def project_source(
        self, owner_id: UUID, project_id: UUID
    ) -> DescriptionProjectSource | None:
        """存在当前合成时返回完整来源；无当前合成返回 None；SRT 损坏抛稳定错误。"""
        ...

    def list_versions(self, owner_id: UUID, project_id: UUID) -> tuple[ProjectDescription, ...]: ...

    def version(
        self, owner_id: UUID, project_id: UUID, version_id: UUID
    ) -> ProjectDescription | None: ...

    def active_attempt(self, owner_id: UUID, project_id: UUID) -> DescriptionAttempt | None: ...

    def attempt_by_key(
        self, owner_id: UUID, project_id: UUID, idempotency_key: str
    ) -> DescriptionAttempt | None: ...

    def begin_attempt(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        idempotency_key: str,
        template: DescriptionTemplate,
        input_sha256: str,
        now: datetime,
        stale_before: datetime | None = None,
    ) -> tuple[DescriptionAttempt, bool]: ...

    def complete_attempt(
        self,
        attempt: DescriptionAttempt,
        *,
        source: DescriptionProjectSource,
        template: DescriptionTemplate,
        payload: DescriptionPayload,
        content: str,
        provider_model: str,
        provider_receipt: str,
        raw_response_sha256: str,
        provider_calls: int,
        elapsed_ms: int,
        now: datetime,
    ) -> ProjectDescription: ...

    def fail_attempt(
        self, attempt: DescriptionAttempt, *, error_code: str, now: datetime
    ) -> None: ...

    def create_manual_edit(
        self,
        base: ProjectDescription,
        *,
        content: str,
        now: datetime,
    ) -> ProjectDescription: ...

    def select_current(
        self, owner_id: UUID, project_id: UUID, version_id: UUID
    ) -> ProjectDescription: ...


class VideoDescriptionService:
    def __init__(
        self,
        repository: VideoDescriptionRepository,
        provider: DescriptionProvider,
        clock: Clock,
        *,
        model: str,
        provider_timeout_seconds: float = 45.0,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._repo = repository
        self._provider = provider
        self._clock = clock
        self._model = model
        self._stale_after = timedelta(seconds=provider_timeout_seconds * 2 + 60)
        self._monotonic = monotonic

    def list_templates(self, owner_id: UUID, channel_id: UUID) -> tuple[DescriptionTemplate, ...]:
        self._require_channel(owner_id, channel_id)
        self._repo.ensure_default_template(
            owner_id, channel_id, DEFAULT_DESCRIPTION_PROMPT, self._clock.now()
        )
        return self._repo.list_templates(owner_id, channel_id)

    def create_template(
        self, owner_id: UUID, channel_id: UUID, prompt: str
    ) -> tuple[DescriptionTemplate, ...]:
        self._require_channel(owner_id, channel_id)
        self._repo.ensure_default_template(
            owner_id, channel_id, DEFAULT_DESCRIPTION_PROMPT, self._clock.now()
        )
        self._repo.create_template(
            owner_id, channel_id, validate_template_prompt(prompt), self._clock.now()
        )
        return self._repo.list_templates(owner_id, channel_id)

    def update_template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID, prompt: str
    ) -> tuple[DescriptionTemplate, ...]:
        self._require_channel(owner_id, channel_id)
        self._repo.update_template(
            owner_id,
            channel_id,
            template_id,
            validate_template_prompt(prompt),
            self._clock.now(),
        )
        return self._repo.list_templates(owner_id, channel_id)

    def delete_template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID
    ) -> tuple[DescriptionTemplate, ...]:
        self._require_channel(owner_id, channel_id)
        self._repo.delete_template(owner_id, channel_id, template_id)
        return self._repo.list_templates(owner_id, channel_id)

    def view(self, owner_id: UUID, project_id: UUID) -> DescriptionPageState:
        ref = self._repo.project_ref(owner_id, project_id)
        if ref is None:
            raise DomainError("not_found", "Resource not found")
        channel_id, channel_name = ref
        source = self._repo.project_source(owner_id, project_id)
        versions = self._repo.list_versions(owner_id, project_id)
        current = next((item for item in versions if item.current), None)
        return DescriptionPageState(
            project_id=project_id,
            channel_id=channel_id,
            channel_name=channel_name,
            available=source is not None,
            blocking_reason=None if source is not None else "description_composition_required",
            timeline_entries=len(source.srt_entries) if source is not None else 0,
            duration_ms=source.duration_ms if source is not None else 0,
            current=current,
            versions=versions,
            active_attempt=self._repo.active_attempt(owner_id, project_id),
        )

    def generate(
        self, owner_id: UUID, project_id: UUID, template_id: UUID, idempotency_key: str
    ) -> ProjectDescription:
        source = self._repo.project_source(owner_id, project_id)
        if source is None:
            raise DomainError(
                "description_composition_required",
                "当前项目还没有可用的最终 SRT，请先完成合成。",
            )
        if len(source.srt_entries) < MIN_SEGMENTS + 1:
            raise DomainError(
                "description_timeline_too_short",
                "最终 SRT 条目过少，无法生成包含 Intro 与 5 个章节的简介。",
            )
        template = self._repo.template(owner_id, source.channel_id, template_id)
        if template is None:
            raise DomainError("not_found", "Description template was not found")
        key = idempotency_key.strip()
        if not key:
            raise DomainError("idempotency_key_required", "Idempotency-Key header is required")
        input_sha256 = description_input_sha256(template, source)
        attempt, started = self._repo.begin_attempt(
            owner_id=owner_id,
            project_id=project_id,
            idempotency_key=key,
            template=template,
            input_sha256=input_sha256,
            now=self._clock.now(),
            stale_before=self._clock.now() - self._stale_after,
        )
        if not started:
            if attempt.status == "SUCCEEDED" and attempt.description_id is not None:
                existing = self._repo.version(owner_id, project_id, attempt.description_id)
                if existing is not None:
                    return existing
            if attempt.status == "RUNNING":
                raise DomainError(
                    "description_generation_in_progress", "该项目已有正在进行的简介生成。"
                )
            raise DomainError(
                attempt.error_code or "description_generation_failed",
                "上一次简介生成未成功，请重新点击生成。",
            )

        started_at = self._monotonic()
        provider_calls = 0
        try:
            payload, provider_result, provider_calls = self._generate_payload(
                template, source, provider_calls
            )
            elapsed_ms = max(0, round((self._monotonic() - started_at) * 1000))
            content = assemble_description(payload, source)
            return self._repo.complete_attempt(
                attempt,
                source=source,
                template=template,
                payload=payload,
                content=content,
                provider_model=provider_result.model,
                provider_receipt=provider_result.receipt,
                raw_response_sha256=provider_result.raw_response_sha256,
                provider_calls=provider_calls,
                elapsed_ms=elapsed_ms,
                now=self._clock.now(),
            )
        except Exception as error:
            code = error.code if isinstance(error, DomainError) else "description_generation_failed"
            self._repo.fail_attempt(attempt, error_code=code, now=self._clock.now())
            raise

    def edit_version(
        self, owner_id: UUID, project_id: UUID, version_id: UUID, content: str
    ) -> ProjectDescription:
        base = self._repo.version(owner_id, project_id, version_id)
        if base is None:
            raise DomainError("not_found", "Description version was not found")
        return self._repo.create_manual_edit(
            base, content=validate_description_content(content), now=self._clock.now()
        )

    def select_current(
        self, owner_id: UUID, project_id: UUID, version_id: UUID
    ) -> ProjectDescription:
        return self._repo.select_current(owner_id, project_id, version_id)

    def _generate_payload(
        self,
        template: DescriptionTemplate,
        source: DescriptionProjectSource,
        provider_calls: int,
    ) -> tuple[DescriptionPayload, DescriptionProviderResult, int]:
        repair_reason = ""
        last_error: DomainError | None = None
        for attempt in range(2):
            messages = build_description_messages(
                template.prompt, source, repair_reason=repair_reason
            )
            try:
                result = self._provider.complete_json(
                    messages, temperature=GENERATION_TEMPERATURE
                )
                provider_calls += 1
            except DomainError as error:
                provider_calls += 1
                if error.code != "description_output_invalid":
                    raise
                last_error = error
            else:
                try:
                    parsed = parse_description_payload(result.payload, source.srt_entries)
                except DomainError as error:
                    if error.code != "description_output_invalid":
                        raise
                    last_error = error
                else:
                    return parsed, result, provider_calls
            if attempt == 0 and last_error is not None:
                repair_reason = last_error.message
                continue
            assert last_error is not None
            raise DomainError(
                "description_output_invalid",
                f"简介模型两次输出均未通过校验：{last_error.message}",
            ) from last_error
        raise DomainError("description_output_invalid", "简介模型输出未通过校验。")

    def _require_channel(self, owner_id: UUID, channel_id: UUID) -> None:
        if not self._repo.channel_exists(owner_id, channel_id):
            raise DomainError("not_found", "Channel was not found")
