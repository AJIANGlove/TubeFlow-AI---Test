from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.cover.models import (
    CoverCharacterCardRow,
    CoverImageGenerationRow,
    CoverOutboxRow,
    CoverPromptGenerationRow,
    CoverRefImageRow,
    CoverStyleDraftRow,
    CoverStyleProfileRow,
)
from tubeflow_test.adapters.persistence.identity_project.models import (
    ChannelRow,
    CharacterImageRow,
    ProjectRow,
)
from tubeflow_test.application.cover_outbox import (
    COVER_AGGREGATE_CHARACTER_CARD,
    COVER_AGGREGATE_IMAGE_GENERATION,
    COVER_AGGREGATE_PROMPT_GENERATION,
    COVER_CHARACTER_CARD_ANALYSIS_REQUESTED,
    COVER_IMAGE_GENERATION_REQUESTED,
    COVER_PROMPT_GENERATION_REQUESTED,
    COVER_REF_ANALYSIS_REQUESTED,
    CoverOutboxEvent,
)
from tubeflow_test.application.cover_ports import (
    COVER_IMAGE_SOURCE_UPLOAD,
    COVER_SCOPE_CHANNEL,
    COVER_SCOPE_PROJECT,
    COVER_STATUS_FAILED,
    COVER_STATUS_SUCCEEDED,
    COVER_TERMINAL_STATUSES,
    CoverCharacterCard,
    CoverCharacterImageRef,
    CoverImageGeneration,
    CoverPromptGeneration,
    CoverRefImage,
    CoverStyleDraft,
    CoverStyleProfile,
    cover_prompt_request_fingerprint,
)
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.domain.errors import DomainError


class SqlAlchemyCoverRepository:
    """PostgreSQL repository; each public mutation is one transaction/UoW."""

    def __init__(self, unit_of_work: Callable[[], AbstractContextManager[Session]]) -> None:
        self._uow = unit_of_work

    def channel_scope_exists(self, owner_id: UUID, channel_id: UUID) -> bool:
        with self._uow() as session:
            return (
                session.scalar(
                    select(ChannelRow.id).where(
                        ChannelRow.id == channel_id,
                        ChannelRow.owner_id == owner_id,
                    )
                )
                is not None
            )

    def add_ref_image(self, image: CoverRefImage) -> CoverRefImage:
        try:
            with self._uow() as session:
                existing = _find_ref_image_by_hash(session, image)
                if existing is not None:
                    return _ref_image(existing)
                session.add(_ref_image_row(image))
            return image
        except IntegrityError:
            with self._uow() as session:
                existing = _find_ref_image_by_hash(session, image)
                if existing is not None:
                    return _ref_image(existing)
            raise

    def get_ref_image(self, owner_id: UUID, image_id: UUID) -> CoverRefImage | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverRefImageRow).where(
                    CoverRefImageRow.owner_id == owner_id,
                    CoverRefImageRow.id == image_id,
                )
            )
            return _ref_image(row) if row else None

    def list_ref_images_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(CoverRefImageRow)
                .where(
                    CoverRefImageRow.owner_id == owner_id,
                    CoverRefImageRow.channel_id == channel_id,
                    CoverRefImageRow.scope == COVER_SCOPE_CHANNEL,
                )
                .order_by(CoverRefImageRow.created_at, CoverRefImageRow.id)
            ).all()
            return tuple(_ref_image(row) for row in rows)

    def list_ref_images_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(CoverRefImageRow)
                .where(
                    CoverRefImageRow.owner_id == owner_id,
                    CoverRefImageRow.project_id == project_id,
                    CoverRefImageRow.scope == COVER_SCOPE_PROJECT,
                )
                .order_by(CoverRefImageRow.created_at, CoverRefImageRow.id)
            ).all()
            return tuple(_ref_image(row) for row in rows)

    def delete_ref_images_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(CoverRefImageRow)
                .where(
                    CoverRefImageRow.owner_id == owner_id,
                    CoverRefImageRow.project_id == project_id,
                    CoverRefImageRow.scope == COVER_SCOPE_PROJECT,
                )
                .order_by(CoverRefImageRow.created_at, CoverRefImageRow.id)
                .with_for_update()
            ).all()
            if not rows:
                return ()
            image_ids = [row.id for row in rows]
            session.execute(
                update(CoverStyleDraftRow)
                .where(CoverStyleDraftRow.ref_image_id.in_(image_ids))
                .values(ref_image_id=None)
            )
            session.execute(
                update(CoverStyleProfileRow)
                .where(CoverStyleProfileRow.ref_image_id.in_(image_ids))
                .values(ref_image_id=None)
            )
            session.execute(delete(CoverRefImageRow).where(CoverRefImageRow.id.in_(image_ids)))
            return tuple(_ref_image(row) for row in rows)

    def get_style_draft_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> CoverStyleDraft | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverStyleDraftRow).where(
                    CoverStyleDraftRow.owner_id == owner_id,
                    CoverStyleDraftRow.channel_id == channel_id,
                    CoverStyleDraftRow.scope == COVER_SCOPE_CHANNEL,
                )
            )
            return _draft(row) if row else None

    def get_style_draft_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverStyleDraft | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverStyleDraftRow).where(
                    CoverStyleDraftRow.owner_id == owner_id,
                    CoverStyleDraftRow.project_id == project_id,
                    CoverStyleDraftRow.scope == COVER_SCOPE_PROJECT,
                )
            )
            return _draft(row) if row else None

    def upsert_style_draft(self, draft: CoverStyleDraft) -> CoverStyleDraft:
        try:
            with self._uow() as session:
                existing = _find_draft(session, draft)
                if existing is None:
                    session.add(_draft_row(draft))
                    return draft
                _copy_draft_mutable(existing, draft)
                return _draft(existing)
        except IntegrityError as exc:
            with self._uow() as session:
                existing = _find_draft(session, draft)
                if existing is None:
                    raise DomainError(
                        "revision_conflict", "Concurrent cover style draft upsert conflict"
                    ) from exc
                _copy_draft_mutable(existing, draft)
                return _draft(existing)

    def create_style_profile(self, profile: CoverStyleProfile) -> CoverStyleProfile:
        try:
            with self._uow() as session:
                _lock_channel(session, profile.owner_id, profile.channel_id)
                latest = session.scalar(
                    select(CoverStyleProfileRow)
                    .where(CoverStyleProfileRow.channel_id == profile.channel_id)
                    .order_by(CoverStyleProfileRow.version.desc())
                    .with_for_update()
                    .limit(1)
                )
                stored = replace(
                    profile,
                    version=(latest.version + 1) if latest else 1,
                    supersedes_id=latest.id if latest else None,
                )
                session.add(_profile_row(stored))
            return stored
        except IntegrityError as exc:
            raise DomainError(
                "revision_conflict", "Concurrent revision allocation conflict"
            ) from exc

    def get_style_profile(self, owner_id: UUID, profile_id: UUID) -> CoverStyleProfile | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverStyleProfileRow).where(
                    CoverStyleProfileRow.owner_id == owner_id,
                    CoverStyleProfileRow.id == profile_id,
                )
            )
            return _profile(row) if row else None

    def list_style_profiles_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverStyleProfile, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(CoverStyleProfileRow)
                .where(
                    CoverStyleProfileRow.owner_id == owner_id,
                    CoverStyleProfileRow.channel_id == channel_id,
                )
                .order_by(CoverStyleProfileRow.version.desc())
            ).all()
            return tuple(_profile(row) for row in rows)

    def current_style_profile_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> CoverStyleProfile | None:
        with self._uow() as session:
            profile_id = session.scalar(
                select(ChannelRow.current_cover_style_profile_id).where(
                    ChannelRow.id == channel_id,
                    ChannelRow.owner_id == owner_id,
                )
            )
            if profile_id is None:
                return None
            row = session.scalar(
                select(CoverStyleProfileRow).where(
                    CoverStyleProfileRow.owner_id == owner_id,
                    CoverStyleProfileRow.id == profile_id,
                )
            )
            return _profile(row) if row else None

    def set_channel_current_style_profile(
        self, owner_id: UUID, channel_id: UUID, profile_id: UUID
    ) -> CoverStyleProfile:
        with self._uow() as session:
            _lock_channel(session, owner_id, channel_id)
            row = session.scalar(
                select(CoverStyleProfileRow)
                .where(
                    CoverStyleProfileRow.id == profile_id,
                    CoverStyleProfileRow.owner_id == owner_id,
                    CoverStyleProfileRow.channel_id == channel_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("invalid_cover_candidate", "Cover style profile is unavailable")
            session.execute(
                update(ChannelRow)
                .where(ChannelRow.id == channel_id, ChannelRow.owner_id == owner_id)
                .values(current_cover_style_profile_id=profile_id)
            )
            return _profile(row)

    def create_prompt_generation(self, generation: CoverPromptGeneration) -> CoverPromptGeneration:
        try:
            with self._uow() as session:
                _lock_active_project(session, generation.owner_id, generation.project_id)
                existing = session.scalar(
                    select(CoverPromptGenerationRow)
                    .where(
                        CoverPromptGenerationRow.owner_id == generation.owner_id,
                        CoverPromptGenerationRow.idempotency_key == generation.idempotency_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    value = _prompt(existing)
                    if cover_prompt_request_fingerprint(value) == cover_prompt_request_fingerprint(
                        generation
                    ):
                        return value
                    raise DomainError(
                        "idempotency_conflict", "Idempotency key is bound to another request"
                    )
                version = (
                    cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(func.max(CoverPromptGenerationRow.version), 0)
                            ).where(
                                CoverPromptGenerationRow.owner_id == generation.owner_id,
                                CoverPromptGenerationRow.project_id == generation.project_id,
                            )
                        ),
                    )
                    + 1
                )
                stored = replace(generation, version=version)
                session.add(_prompt_row(stored))
                session.add(
                    _outbox_row(
                        event_id=stored.id,
                        owner_id=stored.owner_id,
                        project_id=stored.project_id,
                        aggregate_type=COVER_AGGREGATE_PROMPT_GENERATION,
                        aggregate_id=stored.id,
                        event_type=COVER_PROMPT_GENERATION_REQUESTED,
                        request_sha256=hashlib.sha256(
                            cover_prompt_request_fingerprint(stored).encode()
                        ).hexdigest(),
                        workflow_id=(
                            f"cover-prompt-generation-{stored.owner_id}-{stored.id}"
                        ),
                        now=stored.created_at,
                    )
                )
            return stored
        except IntegrityError as exc:
            with self._uow() as session:
                existing = session.scalar(
                    select(CoverPromptGenerationRow).where(
                        CoverPromptGenerationRow.owner_id == generation.owner_id,
                        CoverPromptGenerationRow.idempotency_key == generation.idempotency_key,
                    )
                )
                if existing is not None:
                    value = _prompt(existing)
                    if cover_prompt_request_fingerprint(value) == cover_prompt_request_fingerprint(
                        generation
                    ):
                        return value
                    raise DomainError(
                        "idempotency_conflict", "Idempotency key is bound to another request"
                    ) from exc
            raise DomainError(
                "revision_conflict", "Concurrent revision allocation conflict"
            ) from exc

    def get_prompt_generation(
        self, owner_id: UUID, generation_id: UUID
    ) -> CoverPromptGeneration | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverPromptGenerationRow).where(
                    CoverPromptGenerationRow.owner_id == owner_id,
                    CoverPromptGenerationRow.id == generation_id,
                )
            )
            return _prompt(row) if row else None

    def list_prompt_generations_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverPromptGeneration, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(CoverPromptGenerationRow)
                .where(
                    CoverPromptGenerationRow.owner_id == owner_id,
                    CoverPromptGenerationRow.project_id == project_id,
                )
                .order_by(CoverPromptGenerationRow.version.desc())
            ).all()
            return tuple(_prompt(row) for row in rows)

    def current_prompt_generation_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverPromptGeneration | None:
        with self._uow() as session:
            generation_id = session.scalar(
                select(ProjectRow.current_cover_prompt_generation_id).where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                )
            )
            if generation_id is None:
                return None
            row = session.scalar(
                select(CoverPromptGenerationRow).where(
                    CoverPromptGenerationRow.owner_id == owner_id,
                    CoverPromptGenerationRow.id == generation_id,
                )
            )
            return _prompt(row) if row else None

    def update_prompt_current(
        self, owner_id: UUID, generation_id: UUID, prompt_current: str
    ) -> CoverPromptGeneration:
        with self._uow() as session:
            row = session.scalar(
                select(CoverPromptGenerationRow)
                .where(
                    CoverPromptGenerationRow.owner_id == owner_id,
                    CoverPromptGenerationRow.id == generation_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Cover prompt generation was not found")
            _lock_active_project(session, owner_id, row.project_id)
            if row.status != COVER_STATUS_SUCCEEDED:
                raise DomainError("revision_conflict", "Cover prompt generation is not editable")
            row.prompt_current = prompt_current
            return _prompt(row)

    def set_current_prompt_generation(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverPromptGeneration:
        with self._uow() as session:
            _lock_active_project(session, owner_id, project_id)
            row = session.scalar(
                select(CoverPromptGenerationRow)
                .where(
                    CoverPromptGenerationRow.id == generation_id,
                    CoverPromptGenerationRow.owner_id == owner_id,
                    CoverPromptGenerationRow.project_id == project_id,
                    CoverPromptGenerationRow.status == COVER_STATUS_SUCCEEDED,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError(
                    "invalid_cover_candidate", "Cover prompt generation is unavailable"
                )
            session.execute(
                update(ProjectRow)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .values(current_cover_prompt_generation_id=generation_id)
            )
            return _prompt(row)

    def mark_prompt_generation_failed(
        self, owner_id: UUID, generation_id: UUID, failure_stage: str, failure_code: str
    ) -> CoverPromptGeneration:
        with self._uow() as session:
            row = session.scalar(
                select(CoverPromptGenerationRow)
                .where(
                    CoverPromptGenerationRow.owner_id == owner_id,
                    CoverPromptGenerationRow.id == generation_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Cover prompt generation was not found")
            _lock_active_project(session, owner_id, row.project_id)
            if row.status in COVER_TERMINAL_STATUSES:
                return _prompt(row)
            row.status = COVER_STATUS_FAILED
            row.failure_stage = failure_stage
            row.failure_code = failure_code
            return _prompt(row)

    def complete_prompt_generation(
        self, generation: CoverPromptGeneration
    ) -> CoverPromptGeneration:
        with self._uow() as session:
            _lock_active_project(session, generation.owner_id, generation.project_id)
            row = session.scalar(
                select(CoverPromptGenerationRow)
                .where(
                    CoverPromptGenerationRow.owner_id == generation.owner_id,
                    CoverPromptGenerationRow.id == generation.id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Cover prompt generation was not found")
            if row.status in COVER_TERMINAL_STATUSES:
                return _prompt(row)
            _copy_prompt_product(row, generation)
            return _prompt(row)

    def create_image_generation(self, generation: CoverImageGeneration) -> CoverImageGeneration:
        try:
            with self._uow() as session:
                _lock_active_project(session, generation.owner_id, generation.project_id)
                existing = session.scalar(
                    select(CoverImageGenerationRow)
                    .where(
                        CoverImageGenerationRow.owner_id == generation.owner_id,
                        CoverImageGenerationRow.idempotency_key == generation.idempotency_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    value = _image(existing)
                    if value.canonical_request_sha256 == generation.canonical_request_sha256:
                        return value
                    raise DomainError(
                        "idempotency_conflict", "Idempotency key is bound to another request"
                    )
                version = (
                    cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(func.max(CoverImageGenerationRow.version), 0)
                            ).where(
                                CoverImageGenerationRow.owner_id == generation.owner_id,
                                CoverImageGenerationRow.project_id == generation.project_id,
                            )
                        ),
                    )
                    + 1
                )
                stored = replace(generation, version=version)
                session.add(_image_row(stored))
                session.add(
                    _outbox_row(
                        event_id=stored.id,
                        owner_id=stored.owner_id,
                        project_id=stored.project_id,
                        aggregate_type=COVER_AGGREGATE_IMAGE_GENERATION,
                        aggregate_id=stored.id,
                        event_type=COVER_IMAGE_GENERATION_REQUESTED,
                        request_sha256=stored.canonical_request_sha256,
                        workflow_id=f"cover-image-generation-{stored.owner_id}-{stored.id}",
                        now=stored.created_at,
                    )
                )
            return stored
        except IntegrityError as exc:
            with self._uow() as session:
                existing = session.scalar(
                    select(CoverImageGenerationRow).where(
                        CoverImageGenerationRow.owner_id == generation.owner_id,
                        CoverImageGenerationRow.idempotency_key == generation.idempotency_key,
                    )
                )
                if existing is not None:
                    value = _image(existing)
                    if value.canonical_request_sha256 == generation.canonical_request_sha256:
                        return value
                    raise DomainError(
                        "idempotency_conflict", "Idempotency key is bound to another request"
                    ) from exc
            raise DomainError(
                "revision_conflict", "Concurrent revision allocation conflict"
            ) from exc

    def create_uploaded_image_generation(
        self, generation: CoverImageGeneration
    ) -> tuple[CoverImageGeneration, bool]:
        """用户上传版本落库：项目内同字节去重、无条件切当前、不写 outbox。

        上传没有远端任务，不产生 INTENT_PERSISTED/FAILED 行；命中既有上传版本时
        只把当前指针指回它并返回 ``reused=True``。
        """
        try:
            with self._uow() as session:
                _lock_active_project(session, generation.owner_id, generation.project_id)
                existing = _find_uploaded_image_generation(session, generation)
                if existing is not None:
                    _switch_current_image(
                        session, generation.owner_id, generation.project_id, existing.id
                    )
                    return _image(existing), True
                version = (
                    cast(
                        int,
                        session.scalar(
                            select(
                                func.coalesce(func.max(CoverImageGenerationRow.version), 0)
                            ).where(
                                CoverImageGenerationRow.owner_id == generation.owner_id,
                                CoverImageGenerationRow.project_id == generation.project_id,
                            )
                        ),
                    )
                    + 1
                )
                stored = replace(generation, version=version)
                session.add(_image_row(stored))
                _switch_current_image(session, stored.owner_id, stored.project_id, stored.id)
            return stored, False
        except IntegrityError as exc:
            with self._uow() as session:
                _lock_active_project(session, generation.owner_id, generation.project_id)
                existing = _find_uploaded_image_generation(session, generation)
                if existing is not None:
                    _switch_current_image(
                        session, generation.owner_id, generation.project_id, existing.id
                    )
                    return _image(existing), True
            raise DomainError(
                "revision_conflict", "Concurrent revision allocation conflict"
            ) from exc

    def get_image_generation(
        self, owner_id: UUID, generation_id: UUID
    ) -> CoverImageGeneration | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverImageGenerationRow).where(
                    CoverImageGenerationRow.owner_id == owner_id,
                    CoverImageGenerationRow.id == generation_id,
                )
            )
            return _image(row) if row else None

    def list_image_generations_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverImageGeneration, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(CoverImageGenerationRow)
                .where(
                    CoverImageGenerationRow.owner_id == owner_id,
                    CoverImageGenerationRow.project_id == project_id,
                )
                .order_by(CoverImageGenerationRow.version.desc())
            ).all()
            return tuple(_image(row) for row in rows)

    def current_image_generation_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverImageGeneration | None:
        with self._uow() as session:
            generation_id = session.scalar(
                select(ProjectRow.current_cover_image_generation_id).where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                )
            )
            if generation_id is None:
                return None
            row = session.scalar(
                select(CoverImageGenerationRow).where(
                    CoverImageGenerationRow.owner_id == owner_id,
                    CoverImageGenerationRow.id == generation_id,
                )
            )
            return _image(row) if row else None

    def complete_image_generation_and_switch_current(
        self, generation: CoverImageGeneration, *, switch_current: bool
    ) -> CoverImageGeneration:
        with self._uow() as session:
            _lock_active_project(session, generation.owner_id, generation.project_id)
            row = session.scalar(
                select(CoverImageGenerationRow)
                .where(
                    CoverImageGenerationRow.owner_id == generation.owner_id,
                    CoverImageGenerationRow.id == generation.id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Cover image generation was not found")
            if row.status in COVER_TERMINAL_STATUSES:
                return _image(row)
            _copy_image_result(row, generation)
            if switch_current:
                session.execute(
                    update(ProjectRow)
                    .where(
                        ProjectRow.id == generation.project_id,
                        ProjectRow.owner_id == generation.owner_id,
                    )
                    .values(current_cover_image_generation_id=generation.id)
                )
            return _image(row)

    def mark_image_generation_failed(
        self, owner_id: UUID, generation_id: UUID, failure_stage: str, failure_code: str
    ) -> CoverImageGeneration:
        with self._uow() as session:
            row = session.scalar(
                select(CoverImageGenerationRow)
                .where(
                    CoverImageGenerationRow.owner_id == owner_id,
                    CoverImageGenerationRow.id == generation_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Cover image generation was not found")
            _lock_active_project(session, owner_id, row.project_id)
            if row.status in COVER_TERMINAL_STATUSES:
                return _image(row)
            row.status = COVER_STATUS_FAILED
            row.failure_stage = failure_stage
            row.failure_code = failure_code
            return _image(row)

    def set_current_image_generation(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverImageGeneration:
        with self._uow() as session:
            _lock_active_project(session, owner_id, project_id)
            row = session.scalar(
                select(CoverImageGenerationRow)
                .where(
                    CoverImageGenerationRow.id == generation_id,
                    CoverImageGenerationRow.owner_id == owner_id,
                    CoverImageGenerationRow.project_id == project_id,
                    CoverImageGenerationRow.status == COVER_STATUS_SUCCEEDED,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError(
                    "invalid_cover_candidate", "Cover image generation is unavailable"
                )
            session.execute(
                update(ProjectRow)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
                .values(current_cover_image_generation_id=generation_id)
            )
            return _image(row)

    def get_character_image(
        self, owner_id: UUID, image_id: UUID
    ) -> CoverCharacterImageRef | None:
        with self._uow() as session:
            row = session.scalar(
                select(CharacterImageRow).where(
                    CharacterImageRow.owner_id == owner_id,
                    CharacterImageRow.id == image_id,
                )
            )
            return _character_image(row) if row else None

    def get_character_card(
        self, owner_id: UUID, card_id: UUID
    ) -> CoverCharacterCard | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverCharacterCardRow).where(
                    CoverCharacterCardRow.owner_id == owner_id,
                    CoverCharacterCardRow.id == card_id,
                )
            )
            return _character_card(row) if row else None

    def list_character_cards_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverCharacterCard, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(CoverCharacterCardRow)
                .where(
                    CoverCharacterCardRow.owner_id == owner_id,
                    CoverCharacterCardRow.channel_id == channel_id,
                )
                .order_by(CoverCharacterCardRow.created_at, CoverCharacterCardRow.id)
            ).all()
            return tuple(_character_card(row) for row in rows)

    def get_character_cards_by_ids(
        self, owner_id: UUID, card_ids: tuple[UUID, ...]
    ) -> tuple[CoverCharacterCard, ...]:
        if not card_ids:
            return ()
        with self._uow() as session:
            rows = session.scalars(
                select(CoverCharacterCardRow).where(
                    CoverCharacterCardRow.owner_id == owner_id,
                    CoverCharacterCardRow.id.in_(card_ids),
                )
            ).all()
            by_id = {row.id: row for row in rows}
            return tuple(
                _character_card(by_id[card_id]) for card_id in card_ids if card_id in by_id
            )

    def upsert_character_card(self, card: CoverCharacterCard) -> CoverCharacterCard:
        try:
            with self._uow() as session:
                _lock_channel(session, card.owner_id, card.channel_id)
                existing = session.scalar(
                    select(CoverCharacterCardRow)
                    .where(
                        CoverCharacterCardRow.channel_id == card.channel_id,
                        CoverCharacterCardRow.character_image_id == card.character_image_id,
                    )
                    .with_for_update()
                )
                if existing is None:
                    session.add(_character_card_row(card))
                    return card
                existing.identity_text = card.identity_text
                existing.appearance_text = card.appearance_text
                existing.source = card.source
                existing.updated_at = card.updated_at
                return _character_card(existing)
        except IntegrityError as exc:
            with self._uow() as session:
                existing = session.scalar(
                    select(CoverCharacterCardRow).where(
                        CoverCharacterCardRow.channel_id == card.channel_id,
                        CoverCharacterCardRow.character_image_id == card.character_image_id,
                    )
                )
                if existing is None:
                    raise DomainError(
                        "revision_conflict", "Concurrent cover character card upsert conflict"
                    ) from exc
                existing.identity_text = card.identity_text
                existing.appearance_text = card.appearance_text
                existing.source = card.source
                existing.updated_at = card.updated_at
                return _character_card(existing)

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        """提示词/出图编排的项目事实（冻结文案、标题、语言、所属频道、快照角色图）。"""
        with self._uow() as session:
            row = session.execute(
                select(
                    ProjectRow.channel_id,
                    ProjectRow.title,
                    ProjectRow.snapshot_script,
                    ProjectRow.snapshot_locale,
                    ProjectRow.locale,
                    ProjectRow.snapshot_character_image_ids,
                ).where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                )
            ).one_or_none()
            if row is None:
                return None
            return CoverProjectContext(
                project_id=project_id,
                channel_id=row.channel_id,
                title=row.title,
                frozen_script=row.snapshot_script,
                locale=row.snapshot_locale or row.locale,
                snapshot_character_image_ids=tuple(row.snapshot_character_image_ids or ()),
            )

    def request_ref_analysis(
        self,
        *,
        event_id: UUID,
        owner_id: UUID,
        project_id: UUID | None,
        aggregate_type: str,
        aggregate_id: UUID,
        request_sha256: str,
        workflow_id: str,
        now: datetime,
    ) -> bool:
        if self._pending_workflow_event(workflow_id) is not None:
            return False
        try:
            with self._uow() as session:
                session.add(
                    _outbox_row(
                        event_id=event_id,
                        owner_id=owner_id,
                        project_id=project_id,
                        aggregate_type=aggregate_type,
                        aggregate_id=aggregate_id,
                        event_type=COVER_REF_ANALYSIS_REQUESTED,
                        request_sha256=request_sha256,
                        workflow_id=workflow_id,
                        now=now,
                    )
                )
            return True
        except IntegrityError:
            # 并发重复点击命中部分唯一索引（在途同键事件）→ 去重；其余完整性
            # 错误（如 FK）重新抛出。
            if self._pending_workflow_event(workflow_id) is not None:
                return False
            raise

    def request_character_card_analysis(
        self,
        *,
        event_id: UUID,
        owner_id: UUID,
        aggregate_id: UUID,
        request_sha256: str,
        workflow_id: str,
        now: datetime,
    ) -> bool:
        if self._pending_workflow_event(workflow_id) is not None:
            return False
        try:
            with self._uow() as session:
                session.add(
                    _outbox_row(
                        event_id=event_id,
                        owner_id=owner_id,
                        project_id=None,
                        aggregate_type=COVER_AGGREGATE_CHARACTER_CARD,
                        aggregate_id=aggregate_id,
                        event_type=COVER_CHARACTER_CARD_ANALYSIS_REQUESTED,
                        request_sha256=request_sha256,
                        workflow_id=workflow_id,
                        now=now,
                    )
                )
            return True
        except IntegrityError:
            # 并发重复点击命中部分唯一索引（在途同键事件）→ 去重；其余完整性
            # 错误（如 FK）重新抛出。
            if self._pending_workflow_event(workflow_id) is not None:
                return False
            raise

    def _pending_workflow_event(self, workflow_id: str) -> UUID | None:
        with self._uow() as session:
            return session.scalar(
                select(CoverOutboxRow.id).where(
                    CoverOutboxRow.workflow_id == workflow_id,
                    CoverOutboxRow.acknowledged_at.is_(None),
                )
            )

    def claim_outbox(
        self, now: datetime, lease_until: datetime, token: UUID
    ) -> CoverOutboxEvent | None:
        with self._uow() as session:
            row = session.scalar(
                select(CoverOutboxRow)
                .where(
                    CoverOutboxRow.acknowledged_at.is_(None),
                    CoverOutboxRow.next_attempt_at <= now,
                    or_(
                        CoverOutboxRow.project_id.is_(None),
                        CoverOutboxRow.project_id.in_(
                            select(ProjectRow.id).where(ProjectRow.production_state == "ACTIVE")
                        ),
                    ),
                    or_(
                        CoverOutboxRow.lease_until.is_(None),
                        CoverOutboxRow.lease_until < now,
                    ),
                )
                .order_by(CoverOutboxRow.created_at, CoverOutboxRow.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            if row.project_id is not None:
                _lock_active_project(session, row.owner_id, row.project_id)
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(CoverOutboxRow)
                    .where(
                        CoverOutboxRow.id == row.id,
                        or_(
                            CoverOutboxRow.lease_until.is_(None),
                            CoverOutboxRow.lease_until < now,
                        ),
                        CoverOutboxRow.acknowledged_at.is_(None),
                    )
                    .values(
                        state="CLAIMED",
                        lease_token=token,
                        lease_until=lease_until,
                        attempt_count=CoverOutboxRow.attempt_count + 1,
                    )
                ),
            )
            if result.rowcount != 1:
                return None
            session.flush()
            session.refresh(row)
            return CoverOutboxEvent(
                row.id,
                row.owner_id,
                row.project_id,
                row.aggregate_type,
                row.aggregate_id,
                row.event_type,
                row.request_sha256,
                row.workflow_id,
                token,
                row.attempt_count,
            )

    def acknowledge_outbox(self, event_id: UUID, token: UUID, now: datetime) -> bool:
        with self._uow() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(CoverOutboxRow)
                    .where(
                        CoverOutboxRow.id == event_id,
                        CoverOutboxRow.lease_token == token,
                        CoverOutboxRow.acknowledged_at.is_(None),
                    )
                    .values(
                        state="ACKED",
                        acknowledged_at=now,
                        lease_token=None,
                        lease_until=None,
                        last_error=None,
                    )
                ),
            )
            return result.rowcount == 1

    def retry_outbox(self, event_id: UUID, token: UUID, next_at: datetime, error: str) -> bool:
        with self._uow() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(CoverOutboxRow)
                    .where(
                        CoverOutboxRow.id == event_id,
                        CoverOutboxRow.lease_token == token,
                        CoverOutboxRow.acknowledged_at.is_(None),
                    )
                    .values(
                        state="PENDING",
                        next_attempt_at=next_at,
                        lease_token=None,
                        lease_until=None,
                        last_error=error[:256],
                    )
                ),
            )
            return result.rowcount == 1


def _outbox_row(
    *,
    event_id: UUID,
    owner_id: UUID,
    project_id: UUID | None,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    request_sha256: str,
    workflow_id: str,
    now: datetime,
) -> CoverOutboxRow:
    return CoverOutboxRow(
        id=event_id,
        owner_id=owner_id,
        project_id=project_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        request_sha256=request_sha256,
        workflow_id=workflow_id,
        state="PENDING",
        attempt_count=0,
        next_attempt_at=now,
        created_at=now,
    )


def _lock_active_project(session: Session, owner_id: UUID, project_id: UUID) -> None:
    state = session.scalar(
        select(ProjectRow.production_state)
        .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
        .with_for_update()
    )
    if state is None:
        raise DomainError("not_found", "Cover resource was not found")
    if state != "ACTIVE":
        raise DomainError("project_purging", "Project is being purged")


def _lock_channel(session: Session, owner_id: UUID, channel_id: UUID) -> None:
    channel = session.scalar(
        select(ChannelRow.id)
        .where(ChannelRow.id == channel_id, ChannelRow.owner_id == owner_id)
        .with_for_update()
    )
    if channel is None:
        raise DomainError("not_found", "Cover resource was not found")


def _find_uploaded_image_generation(
    session: Session, generation: CoverImageGeneration
) -> CoverImageGenerationRow | None:
    """同一项目内同字节上传版本（去重依据由调用方持有项目锁保证串行）。"""
    return session.scalar(
        select(CoverImageGenerationRow)
        .where(
            CoverImageGenerationRow.owner_id == generation.owner_id,
            CoverImageGenerationRow.project_id == generation.project_id,
            CoverImageGenerationRow.source == COVER_IMAGE_SOURCE_UPLOAD,
            CoverImageGenerationRow.object_sha256 == generation.object_sha256,
        )
        .with_for_update()
    )


def _switch_current_image(
    session: Session, owner_id: UUID, project_id: UUID, generation_id: UUID
) -> None:
    session.execute(
        update(ProjectRow)
        .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
        .values(current_cover_image_generation_id=generation_id)
    )


def _find_ref_image_by_hash(session: Session, image: CoverRefImage) -> CoverRefImageRow | None:
    if image.scope == COVER_SCOPE_CHANNEL and image.channel_id is not None:
        _lock_channel(session, image.owner_id, image.channel_id)
        return session.scalar(
            select(CoverRefImageRow)
            .where(
                CoverRefImageRow.channel_id == image.channel_id,
                CoverRefImageRow.sha256 == image.sha256,
            )
            .with_for_update()
        )
    if image.scope == COVER_SCOPE_PROJECT and image.project_id is not None:
        _lock_active_project(session, image.owner_id, image.project_id)
        return session.scalar(
            select(CoverRefImageRow)
            .where(
                CoverRefImageRow.project_id == image.project_id,
                CoverRefImageRow.sha256 == image.sha256,
            )
            .with_for_update()
        )
    raise DomainError("invalid_cover_scope", "Cover scope is invalid")


def _find_draft(session: Session, draft: CoverStyleDraft) -> CoverStyleDraftRow | None:
    if draft.scope == COVER_SCOPE_CHANNEL and draft.channel_id is not None:
        _lock_channel(session, draft.owner_id, draft.channel_id)
        return session.scalar(
            select(CoverStyleDraftRow)
            .where(CoverStyleDraftRow.channel_id == draft.channel_id)
            .with_for_update()
        )
    if draft.scope == COVER_SCOPE_PROJECT and draft.project_id is not None:
        _lock_active_project(session, draft.owner_id, draft.project_id)
        return session.scalar(
            select(CoverStyleDraftRow)
            .where(CoverStyleDraftRow.project_id == draft.project_id)
            .with_for_update()
        )
    raise DomainError("invalid_cover_scope", "Cover scope is invalid")


def _ref_image(row: CoverRefImageRow) -> CoverRefImage:
    return CoverRefImage(
        id=row.id,
        owner_id=row.owner_id,
        scope=row.scope,
        channel_id=row.channel_id,
        project_id=row.project_id,
        sha256=row.sha256,
        object_key=row.object_key,
        byte_size=row.byte_size,
        width=row.width,
        height=row.height,
        image_format=row.image_format,
        created_at=row.created_at,
    )


def _ref_image_row(value: CoverRefImage) -> CoverRefImageRow:
    return CoverRefImageRow(
        id=value.id,
        owner_id=value.owner_id,
        scope=value.scope,
        channel_id=value.channel_id,
        project_id=value.project_id,
        sha256=value.sha256,
        object_key=value.object_key,
        byte_size=value.byte_size,
        width=value.width,
        height=value.height,
        image_format=value.image_format,
        created_at=value.created_at,
    )


def _draft(row: CoverStyleDraftRow) -> CoverStyleDraft:
    return CoverStyleDraft(
        id=row.id,
        owner_id=row.owner_id,
        scope=row.scope,
        channel_id=row.channel_id,
        project_id=row.project_id,
        ref_image_id=row.ref_image_id,
        dims=dict(row.dims),
        analysis=dict(row.analysis),
        analysis_dims=dict(row.analysis_dims),
        ref_image_sha256=row.ref_image_sha256,
        edited_text=row.edited_text,
        updated_at=row.updated_at,
    )


def _draft_row(value: CoverStyleDraft) -> CoverStyleDraftRow:
    return CoverStyleDraftRow(
        id=value.id,
        owner_id=value.owner_id,
        scope=value.scope,
        channel_id=value.channel_id,
        project_id=value.project_id,
        ref_image_id=value.ref_image_id,
        dims=dict(value.dims),
        analysis=dict(value.analysis),
        analysis_dims=dict(value.analysis_dims),
        ref_image_sha256=value.ref_image_sha256,
        edited_text=value.edited_text,
        updated_at=value.updated_at,
    )


def _copy_draft_mutable(row: CoverStyleDraftRow, value: CoverStyleDraft) -> None:
    row.ref_image_id = value.ref_image_id
    row.dims = dict(value.dims)
    row.analysis = dict(value.analysis)
    row.analysis_dims = dict(value.analysis_dims)
    row.ref_image_sha256 = value.ref_image_sha256
    row.edited_text = value.edited_text
    row.updated_at = value.updated_at


def _profile(row: CoverStyleProfileRow) -> CoverStyleProfile:
    return CoverStyleProfile(
        id=row.id,
        owner_id=row.owner_id,
        channel_id=row.channel_id,
        version=row.version,
        ref_image_id=row.ref_image_id,
        dims=dict(row.dims),
        analysis_dims=dict(row.analysis_dims),
        edited_text=row.edited_text,
        created_at=row.created_at,
        supersedes_id=row.supersedes_id,
        label=row.label,
    )


def _profile_row(value: CoverStyleProfile) -> CoverStyleProfileRow:
    return CoverStyleProfileRow(
        id=value.id,
        owner_id=value.owner_id,
        channel_id=value.channel_id,
        version=value.version,
        ref_image_id=value.ref_image_id,
        dims=dict(value.dims),
        analysis_dims=dict(value.analysis_dims),
        edited_text=value.edited_text,
        created_at=value.created_at,
        supersedes_id=value.supersedes_id,
        label=value.label,
    )


def _character_image(row: CharacterImageRow) -> CoverCharacterImageRef:
    return CoverCharacterImageRef(
        id=row.id,
        owner_id=row.owner_id,
        channel_id=row.channel_id,
        object_key=row.object_key,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
    )


def _character_card(row: CoverCharacterCardRow) -> CoverCharacterCard:
    return CoverCharacterCard(
        id=row.id,
        owner_id=row.owner_id,
        channel_id=row.channel_id,
        character_image_id=row.character_image_id,
        identity_text=row.identity_text,
        appearance_text=row.appearance_text,
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _character_card_row(value: CoverCharacterCard) -> CoverCharacterCardRow:
    return CoverCharacterCardRow(
        id=value.id,
        owner_id=value.owner_id,
        channel_id=value.channel_id,
        character_image_id=value.character_image_id,
        identity_text=value.identity_text,
        appearance_text=value.appearance_text,
        source=value.source,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _prompt(row: CoverPromptGenerationRow) -> CoverPromptGeneration:
    return CoverPromptGeneration(
        id=row.id,
        owner_id=row.owner_id,
        project_id=row.project_id,
        version=row.version,
        style_source=row.style_source,
        channel_style_profile_id=row.channel_style_profile_id,
        style_text_used=row.style_text_used,
        style_dims_used=dict(row.style_dims_used),
        style_override_text=row.style_override_text,
        brief=row.brief,
        cover_text_candidates=[dict(item) for item in row.cover_text_candidates],
        cover_text_default=row.cover_text_default,
        cover_text_used=row.cover_text_used,
        prompt_body=row.prompt_body,
        prompt_system=row.prompt_system,
        prompt_current=row.prompt_current,
        title_used=row.title_used,
        script_sha256=row.script_sha256,
        warnings=list(row.warnings),
        judge=dict(row.judge) if row.judge is not None else None,
        model=row.model,
        parameters=dict(row.parameters),
        idempotency_key=row.idempotency_key,
        status=row.status,
        failure_stage=row.failure_stage,
        failure_code=row.failure_code,
        created_at=row.created_at,
        character_cards_used=[dict(item) for item in row.character_cards_used],
    )


def _prompt_row(value: CoverPromptGeneration) -> CoverPromptGenerationRow:
    return CoverPromptGenerationRow(
        id=value.id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        version=value.version,
        style_source=value.style_source,
        channel_style_profile_id=value.channel_style_profile_id,
        style_text_used=value.style_text_used,
        style_dims_used=dict(value.style_dims_used),
        style_override_text=value.style_override_text,
        brief=value.brief,
        cover_text_candidates=[dict(item) for item in value.cover_text_candidates],
        cover_text_default=value.cover_text_default,
        cover_text_used=value.cover_text_used,
        prompt_body=value.prompt_body,
        prompt_system=value.prompt_system,
        prompt_current=value.prompt_current,
        title_used=value.title_used,
        script_sha256=value.script_sha256,
        warnings=list(value.warnings),
        judge=dict(value.judge) if value.judge is not None else None,
        model=value.model,
        parameters=dict(value.parameters),
        idempotency_key=value.idempotency_key,
        status=value.status,
        failure_stage=value.failure_stage,
        failure_code=value.failure_code,
        created_at=value.created_at,
        character_cards_used=[dict(item) for item in value.character_cards_used],
    )


def _copy_prompt_product(row: CoverPromptGenerationRow, value: CoverPromptGeneration) -> None:
    row.status = value.status
    row.cover_text_candidates = [dict(item) for item in value.cover_text_candidates]
    row.cover_text_default = value.cover_text_default
    row.cover_text_used = value.cover_text_used
    row.prompt_body = value.prompt_body
    row.prompt_system = value.prompt_system
    row.prompt_current = value.prompt_current
    row.warnings = list(value.warnings)
    row.judge = dict(value.judge) if value.judge is not None else None
    row.failure_stage = None
    row.failure_code = None


def _image(row: CoverImageGenerationRow) -> CoverImageGeneration:
    return CoverImageGeneration(
        id=row.id,
        owner_id=row.owner_id,
        project_id=row.project_id,
        prompt_generation_id=row.prompt_generation_id,
        version=row.version,
        prompt=row.prompt,
        model=row.model,
        size=row.size,
        quality=row.quality,
        parameters=dict(row.parameters),
        idempotency_key=row.idempotency_key,
        provider_idempotency_key=row.provider_idempotency_key,
        canonical_request_sha256=row.canonical_request_sha256,
        status=row.status,
        provider_receipt=row.provider_receipt,
        expected_sha256=row.expected_sha256,
        downloaded_sha256=row.downloaded_sha256,
        downloaded_size=row.downloaded_size,
        qc_width=row.qc_width,
        qc_height=row.qc_height,
        qc_mime=row.qc_mime,
        object_key=row.object_key,
        object_sha256=row.object_sha256,
        failure_stage=row.failure_stage,
        failure_code=row.failure_code,
        created_at=row.created_at,
        visual_review=dict(row.visual_review) if row.visual_review is not None else None,
        source=row.source,
    )


def _image_row(value: CoverImageGeneration) -> CoverImageGenerationRow:
    return CoverImageGenerationRow(
        id=value.id,
        owner_id=value.owner_id,
        project_id=value.project_id,
        prompt_generation_id=value.prompt_generation_id,
        version=value.version,
        prompt=value.prompt,
        model=value.model,
        size=value.size,
        quality=value.quality,
        parameters=dict(value.parameters),
        idempotency_key=value.idempotency_key,
        provider_idempotency_key=value.provider_idempotency_key,
        canonical_request_sha256=value.canonical_request_sha256,
        status=value.status,
        provider_receipt=value.provider_receipt,
        expected_sha256=value.expected_sha256,
        downloaded_sha256=value.downloaded_sha256,
        downloaded_size=value.downloaded_size,
        qc_width=value.qc_width,
        qc_height=value.qc_height,
        qc_mime=value.qc_mime,
        object_key=value.object_key,
        object_sha256=value.object_sha256,
        failure_stage=value.failure_stage,
        failure_code=value.failure_code,
        created_at=value.created_at,
        visual_review=value.visual_review,
        source=value.source,
    )


def _copy_image_result(row: CoverImageGenerationRow, value: CoverImageGeneration) -> None:
    row.status = value.status
    row.provider_receipt = value.provider_receipt
    row.expected_sha256 = value.expected_sha256
    row.downloaded_sha256 = value.downloaded_sha256
    row.downloaded_size = value.downloaded_size
    row.qc_width = value.qc_width
    row.qc_height = value.qc_height
    row.qc_mime = value.qc_mime
    row.object_key = value.object_key
    row.object_sha256 = value.object_sha256
    row.failure_stage = None
    row.failure_code = None
    row.visual_review = value.visual_review
