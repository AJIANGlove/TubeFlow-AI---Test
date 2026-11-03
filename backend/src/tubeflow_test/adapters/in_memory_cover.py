from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from uuid import UUID

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
from tubeflow_test.domain.errors import DomainError


@dataclass(slots=True)
class _OutboxRecord:
    id: UUID
    owner_id: UUID
    project_id: UUID | None
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    request_sha256: str
    workflow_id: str
    state: str
    lease_token: UUID | None
    lease_until: datetime | None
    attempt_count: int
    next_attempt_at: datetime
    acknowledged_at: datetime | None
    last_error: str | None
    created_at: datetime


class InMemoryCoverRepository:
    """Test-only transactional model of the PostgreSQL cover repository contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.ref_images: dict[UUID, CoverRefImage] = {}
        self.style_drafts: dict[UUID, CoverStyleDraft] = {}
        self.style_profiles: dict[UUID, CoverStyleProfile] = {}
        self.prompt_generations: dict[UUID, CoverPromptGeneration] = {}
        self.image_generations: dict[UUID, CoverImageGeneration] = {}
        self.character_cards: dict[UUID, CoverCharacterCard] = {}
        # 测试种子：频道存在性与项目状态（照 InMemoryStoryboardRepository 的测试钩子）。
        self.channels: set[tuple[UUID, UUID]] = set()
        self.project_states: dict[tuple[UUID, UUID], str] = {}
        self.channel_current_profile: dict[tuple[UUID, UUID], UUID] = {}
        self.project_current_prompt: dict[tuple[UUID, UUID], UUID] = {}
        self.project_current_image: dict[tuple[UUID, UUID], UUID] = {}
        self.character_images: dict[UUID, CoverCharacterImageRef] = {}
        self.outbox: dict[UUID, _OutboxRecord] = {}

    def add_channel(self, owner_id: UUID, channel_id: UUID) -> None:
        self.channels.add((owner_id, channel_id))

    def set_project_state(self, owner_id: UUID, project_id: UUID, state: str) -> None:
        self.project_states[(owner_id, project_id)] = state

    def add_character_image(self, image: CoverCharacterImageRef) -> None:
        """测试种子：登记一张频道角色图（供角色卡归属校验与解析取字节）。"""
        self.character_images[image.id] = image

    def _channel(self, owner_id: UUID, channel_id: UUID) -> None:
        if (owner_id, channel_id) not in self.channels:
            raise DomainError("not_found", "Cover resource was not found")

    def _active_project(self, owner_id: UUID, project_id: UUID) -> None:
        state = self.project_states.get((owner_id, project_id))
        if state is None:
            raise DomainError("not_found", "Cover resource was not found")
        if state != "ACTIVE":
            raise DomainError("project_purging", "Project is being purged")

    def channel_scope_exists(self, owner_id: UUID, channel_id: UUID) -> bool:
        return (owner_id, channel_id) in self.channels

    def add_ref_image(self, image: CoverRefImage) -> CoverRefImage:
        with self._lock:
            if image.scope == COVER_SCOPE_CHANNEL and image.channel_id is not None:
                self._channel(image.owner_id, image.channel_id)
                existing = self._ref_image_by_hash(image.channel_id, None, image.sha256)
            elif image.scope == COVER_SCOPE_PROJECT and image.project_id is not None:
                self._active_project(image.owner_id, image.project_id)
                existing = self._ref_image_by_hash(None, image.project_id, image.sha256)
            else:
                raise DomainError("invalid_cover_scope", "Cover scope is invalid")
            if existing is not None:
                return existing
            self.ref_images[image.id] = image
            return image

    def _ref_image_by_hash(
        self, channel_id: UUID | None, project_id: UUID | None, sha256: str
    ) -> CoverRefImage | None:
        return next(
            (
                item
                for item in self.ref_images.values()
                if item.sha256 == sha256
                and (
                    (channel_id is not None and item.channel_id == channel_id)
                    or (project_id is not None and item.project_id == project_id)
                )
            ),
            None,
        )

    def get_ref_image(self, owner_id: UUID, image_id: UUID) -> CoverRefImage | None:
        value = self.ref_images.get(image_id)
        return value if value is not None and value.owner_id == owner_id else None

    def list_ref_images_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.ref_images.values()
                    if item.owner_id == owner_id
                    and item.channel_id == channel_id
                    and item.scope == COVER_SCOPE_CHANNEL
                ),
                key=lambda item: (item.created_at, item.id.hex),
            )
        )

    def list_ref_images_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.ref_images.values()
                    if item.owner_id == owner_id
                    and item.project_id == project_id
                    and item.scope == COVER_SCOPE_PROJECT
                ),
                key=lambda item: (item.created_at, item.id.hex),
            )
        )

    def delete_ref_images_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverRefImage, ...]:
        with self._lock:
            rows = tuple(
                sorted(
                    (
                        item
                        for item in self.ref_images.values()
                        if item.owner_id == owner_id
                        and item.project_id == project_id
                        and item.scope == COVER_SCOPE_PROJECT
                    ),
                    key=lambda item: (item.created_at, item.id.hex),
                )
            )
            if not rows:
                return ()
            image_ids = {item.id for item in rows}
            for draft_id, draft in self.style_drafts.items():
                if draft.ref_image_id in image_ids:
                    self.style_drafts[draft_id] = replace(draft, ref_image_id=None)
            for profile_id, profile in self.style_profiles.items():
                if profile.ref_image_id in image_ids:
                    self.style_profiles[profile_id] = replace(profile, ref_image_id=None)
            for item in rows:
                del self.ref_images[item.id]
            return rows

    def get_style_draft_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> CoverStyleDraft | None:
        return next(
            (
                item
                for item in self.style_drafts.values()
                if item.owner_id == owner_id
                and item.channel_id == channel_id
                and item.scope == COVER_SCOPE_CHANNEL
            ),
            None,
        )

    def get_style_draft_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverStyleDraft | None:
        return next(
            (
                item
                for item in self.style_drafts.values()
                if item.owner_id == owner_id
                and item.project_id == project_id
                and item.scope == COVER_SCOPE_PROJECT
            ),
            None,
        )

    def upsert_style_draft(self, draft: CoverStyleDraft) -> CoverStyleDraft:
        with self._lock:
            if draft.scope == COVER_SCOPE_CHANNEL and draft.channel_id is not None:
                self._channel(draft.owner_id, draft.channel_id)
                existing = self._draft_by_scope(draft.channel_id, None)
            elif draft.scope == COVER_SCOPE_PROJECT and draft.project_id is not None:
                self._active_project(draft.owner_id, draft.project_id)
                existing = self._draft_by_scope(None, draft.project_id)
            else:
                raise DomainError("invalid_cover_scope", "Cover scope is invalid")
            if existing is None:
                self.style_drafts[draft.id] = draft
                return draft
            stored = replace(
                existing,
                ref_image_id=draft.ref_image_id,
                dims=draft.dims,
                analysis=draft.analysis,
                analysis_dims=draft.analysis_dims,
                ref_image_sha256=draft.ref_image_sha256,
                edited_text=draft.edited_text,
                updated_at=draft.updated_at,
            )
            self.style_drafts[existing.id] = stored
            return stored

    def _draft_by_scope(
        self, channel_id: UUID | None, project_id: UUID | None
    ) -> CoverStyleDraft | None:
        return next(
            (
                item
                for item in self.style_drafts.values()
                if (channel_id is not None and item.channel_id == channel_id)
                or (project_id is not None and item.project_id == project_id)
            ),
            None,
        )

    def create_style_profile(self, profile: CoverStyleProfile) -> CoverStyleProfile:
        with self._lock:
            self._channel(profile.owner_id, profile.channel_id)
            latest = max(
                (
                    item
                    for item in self.style_profiles.values()
                    if item.channel_id == profile.channel_id
                ),
                key=lambda item: item.version,
                default=None,
            )
            stored = replace(
                profile,
                version=(latest.version + 1) if latest else 1,
                supersedes_id=latest.id if latest else None,
            )
            self.style_profiles[stored.id] = stored
            return stored

    def get_style_profile(self, owner_id: UUID, profile_id: UUID) -> CoverStyleProfile | None:
        value = self.style_profiles.get(profile_id)
        return value if value is not None and value.owner_id == owner_id else None

    def list_style_profiles_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverStyleProfile, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.style_profiles.values()
                    if item.owner_id == owner_id and item.channel_id == channel_id
                ),
                key=lambda item: item.version,
                reverse=True,
            )
        )

    def current_style_profile_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> CoverStyleProfile | None:
        profile_id = self.channel_current_profile.get((owner_id, channel_id))
        return self.style_profiles.get(profile_id) if profile_id else None

    def set_channel_current_style_profile(
        self, owner_id: UUID, channel_id: UUID, profile_id: UUID
    ) -> CoverStyleProfile:
        with self._lock:
            self._channel(owner_id, channel_id)
            value = self.style_profiles.get(profile_id)
            if value is None or value.owner_id != owner_id or value.channel_id != channel_id:
                raise DomainError("invalid_cover_candidate", "Cover style profile is unavailable")
            self.channel_current_profile[(owner_id, channel_id)] = profile_id
            return value

    def create_prompt_generation(self, generation: CoverPromptGeneration) -> CoverPromptGeneration:
        with self._lock:
            self._active_project(generation.owner_id, generation.project_id)
            existing = next(
                (
                    item
                    for item in self.prompt_generations.values()
                    if item.owner_id == generation.owner_id
                    and item.idempotency_key == generation.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if cover_prompt_request_fingerprint(existing) == cover_prompt_request_fingerprint(
                    generation
                ):
                    return existing
                raise DomainError(
                    "idempotency_conflict", "Idempotency key is bound to another request"
                )
            version = 1 + max(
                (
                    item.version
                    for item in self.prompt_generations.values()
                    if item.owner_id == generation.owner_id
                    and item.project_id == generation.project_id
                ),
                default=0,
            )
            stored = replace(generation, version=version)
            self.prompt_generations[stored.id] = stored
            self._append_outbox(
                event_id=stored.id,
                owner_id=stored.owner_id,
                project_id=stored.project_id,
                aggregate_type=COVER_AGGREGATE_PROMPT_GENERATION,
                aggregate_id=stored.id,
                event_type=COVER_PROMPT_GENERATION_REQUESTED,
                request_sha256=hashlib.sha256(
                    cover_prompt_request_fingerprint(stored).encode()
                ).hexdigest(),
                workflow_id=f"cover-prompt-generation-{stored.owner_id}-{stored.id}",
                now=stored.created_at,
            )
            return stored

    def get_prompt_generation(
        self, owner_id: UUID, generation_id: UUID
    ) -> CoverPromptGeneration | None:
        value = self.prompt_generations.get(generation_id)
        return value if value is not None and value.owner_id == owner_id else None

    def list_prompt_generations_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverPromptGeneration, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.prompt_generations.values()
                    if item.owner_id == owner_id and item.project_id == project_id
                ),
                key=lambda item: item.version,
                reverse=True,
            )
        )

    def current_prompt_generation_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverPromptGeneration | None:
        generation_id = self.project_current_prompt.get((owner_id, project_id))
        return self.prompt_generations.get(generation_id) if generation_id else None

    def update_prompt_current(
        self, owner_id: UUID, generation_id: UUID, prompt_current: str
    ) -> CoverPromptGeneration:
        with self._lock:
            value = self.prompt_generations.get(generation_id)
            if value is None or value.owner_id != owner_id:
                raise DomainError("not_found", "Cover prompt generation was not found")
            self._active_project(owner_id, value.project_id)
            if value.status != COVER_STATUS_SUCCEEDED:
                raise DomainError("revision_conflict", "Cover prompt generation is not editable")
            stored = replace(value, prompt_current=prompt_current)
            self.prompt_generations[generation_id] = stored
            return stored

    def set_current_prompt_generation(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverPromptGeneration:
        with self._lock:
            self._active_project(owner_id, project_id)
            value = self.prompt_generations.get(generation_id)
            if (
                value is None
                or value.owner_id != owner_id
                or value.project_id != project_id
                or value.status != COVER_STATUS_SUCCEEDED
            ):
                raise DomainError(
                    "invalid_cover_candidate", "Cover prompt generation is unavailable"
                )
            self.project_current_prompt[(owner_id, project_id)] = generation_id
            return value

    def mark_prompt_generation_failed(
        self, owner_id: UUID, generation_id: UUID, failure_stage: str, failure_code: str
    ) -> CoverPromptGeneration:
        with self._lock:
            value = self.prompt_generations.get(generation_id)
            if value is None or value.owner_id != owner_id:
                raise DomainError("not_found", "Cover prompt generation was not found")
            self._active_project(owner_id, value.project_id)
            if value.status in COVER_TERMINAL_STATUSES:
                return value
            stored = replace(
                value,
                status=COVER_STATUS_FAILED,
                failure_stage=failure_stage,
                failure_code=failure_code,
            )
            self.prompt_generations[generation_id] = stored
            return stored

    def complete_prompt_generation(
        self, generation: CoverPromptGeneration
    ) -> CoverPromptGeneration:
        with self._lock:
            self._active_project(generation.owner_id, generation.project_id)
            value = self.prompt_generations.get(generation.id)
            if value is None or value.owner_id != generation.owner_id:
                raise DomainError("not_found", "Cover prompt generation was not found")
            if value.status in COVER_TERMINAL_STATUSES:
                return value
            stored = replace(
                value,
                status=generation.status,
                cover_text_candidates=generation.cover_text_candidates,
                cover_text_default=generation.cover_text_default,
                cover_text_used=generation.cover_text_used,
                prompt_body=generation.prompt_body,
                prompt_system=generation.prompt_system,
                prompt_current=generation.prompt_current,
                warnings=generation.warnings,
                judge=generation.judge,
                failure_stage=None,
                failure_code=None,
            )
            self.prompt_generations[generation.id] = stored
            return stored

    def create_image_generation(self, generation: CoverImageGeneration) -> CoverImageGeneration:
        with self._lock:
            self._active_project(generation.owner_id, generation.project_id)
            existing = next(
                (
                    item
                    for item in self.image_generations.values()
                    if item.owner_id == generation.owner_id
                    and item.idempotency_key == generation.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing.canonical_request_sha256 == generation.canonical_request_sha256:
                    return existing
                raise DomainError(
                    "idempotency_conflict", "Idempotency key is bound to another request"
                )
            version = 1 + max(
                (
                    item.version
                    for item in self.image_generations.values()
                    if item.owner_id == generation.owner_id
                    and item.project_id == generation.project_id
                ),
                default=0,
            )
            stored = replace(generation, version=version)
            self.image_generations[stored.id] = stored
            self._append_outbox(
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
            return stored

    def create_uploaded_image_generation(
        self, generation: CoverImageGeneration
    ) -> tuple[CoverImageGeneration, bool]:
        """上传版本落库：项目内同字节去重、无条件切当前、不写 outbox。"""
        with self._lock:
            self._active_project(generation.owner_id, generation.project_id)
            existing = next(
                (
                    item
                    for item in self.image_generations.values()
                    if item.owner_id == generation.owner_id
                    and item.project_id == generation.project_id
                    and item.source == COVER_IMAGE_SOURCE_UPLOAD
                    and item.object_sha256 == generation.object_sha256
                ),
                None,
            )
            if existing is not None:
                self.project_current_image[
                    (existing.owner_id, existing.project_id)
                ] = existing.id
                return existing, True
            version = 1 + max(
                (
                    item.version
                    for item in self.image_generations.values()
                    if item.owner_id == generation.owner_id
                    and item.project_id == generation.project_id
                ),
                default=0,
            )
            stored = replace(generation, version=version)
            self.image_generations[stored.id] = stored
            self.project_current_image[(stored.owner_id, stored.project_id)] = stored.id
            return stored, False

    def get_image_generation(
        self, owner_id: UUID, generation_id: UUID
    ) -> CoverImageGeneration | None:
        value = self.image_generations.get(generation_id)
        return value if value is not None and value.owner_id == owner_id else None

    def list_image_generations_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[CoverImageGeneration, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.image_generations.values()
                    if item.owner_id == owner_id and item.project_id == project_id
                ),
                key=lambda item: item.version,
                reverse=True,
            )
        )

    def current_image_generation_for_project(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverImageGeneration | None:
        generation_id = self.project_current_image.get((owner_id, project_id))
        return self.image_generations.get(generation_id) if generation_id else None

    def complete_image_generation_and_switch_current(
        self, generation: CoverImageGeneration, *, switch_current: bool
    ) -> CoverImageGeneration:
        with self._lock:
            self._active_project(generation.owner_id, generation.project_id)
            value = self.image_generations.get(generation.id)
            if value is None or value.owner_id != generation.owner_id:
                raise DomainError("not_found", "Cover image generation was not found")
            if value.status in COVER_TERMINAL_STATUSES:
                return value
            stored = replace(
                value,
                status=generation.status,
                provider_receipt=generation.provider_receipt,
                expected_sha256=generation.expected_sha256,
                downloaded_sha256=generation.downloaded_sha256,
                downloaded_size=generation.downloaded_size,
                qc_width=generation.qc_width,
                qc_height=generation.qc_height,
                qc_mime=generation.qc_mime,
                object_key=generation.object_key,
                object_sha256=generation.object_sha256,
                failure_stage=None,
                failure_code=None,
                visual_review=generation.visual_review,
            )
            self.image_generations[generation.id] = stored
            if switch_current:
                self.project_current_image[(generation.owner_id, generation.project_id)] = (
                    generation.id
                )
            return stored

    def mark_image_generation_failed(
        self, owner_id: UUID, generation_id: UUID, failure_stage: str, failure_code: str
    ) -> CoverImageGeneration:
        with self._lock:
            value = self.image_generations.get(generation_id)
            if value is None or value.owner_id != owner_id:
                raise DomainError("not_found", "Cover image generation was not found")
            self._active_project(owner_id, value.project_id)
            if value.status in COVER_TERMINAL_STATUSES:
                return value
            stored = replace(
                value,
                status=COVER_STATUS_FAILED,
                failure_stage=failure_stage,
                failure_code=failure_code,
            )
            self.image_generations[generation_id] = stored
            return stored

    def set_current_image_generation(
        self, owner_id: UUID, project_id: UUID, generation_id: UUID
    ) -> CoverImageGeneration:
        with self._lock:
            self._active_project(owner_id, project_id)
            value = self.image_generations.get(generation_id)
            if (
                value is None
                or value.owner_id != owner_id
                or value.project_id != project_id
                or value.status != COVER_STATUS_SUCCEEDED
            ):
                raise DomainError(
                    "invalid_cover_candidate", "Cover image generation is unavailable"
                )
            self.project_current_image[(owner_id, project_id)] = generation_id
            return value

    def get_character_image(
        self, owner_id: UUID, image_id: UUID
    ) -> CoverCharacterImageRef | None:
        value = self.character_images.get(image_id)
        return value if value is not None and value.owner_id == owner_id else None

    def get_character_card(
        self, owner_id: UUID, card_id: UUID
    ) -> CoverCharacterCard | None:
        value = self.character_cards.get(card_id)
        return value if value is not None and value.owner_id == owner_id else None

    def list_character_cards_for_channel(
        self, owner_id: UUID, channel_id: UUID
    ) -> tuple[CoverCharacterCard, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.character_cards.values()
                    if item.owner_id == owner_id and item.channel_id == channel_id
                ),
                key=lambda item: (item.created_at, item.id.hex),
            )
        )

    def get_character_cards_by_ids(
        self, owner_id: UUID, card_ids: tuple[UUID, ...]
    ) -> tuple[CoverCharacterCard, ...]:
        return tuple(
            self.character_cards[card_id]
            for card_id in card_ids
            if card_id in self.character_cards
            and self.character_cards[card_id].owner_id == owner_id
        )

    def upsert_character_card(self, card: CoverCharacterCard) -> CoverCharacterCard:
        with self._lock:
            self._channel(card.owner_id, card.channel_id)
            existing = next(
                (
                    item
                    for item in self.character_cards.values()
                    if item.channel_id == card.channel_id
                    and item.character_image_id == card.character_image_id
                ),
                None,
            )
            if existing is None:
                self.character_cards[card.id] = card
                return card
            stored = replace(
                existing,
                identity_text=card.identity_text,
                appearance_text=card.appearance_text,
                source=card.source,
                updated_at=card.updated_at,
            )
            self.character_cards[existing.id] = stored
            return stored

    def _append_outbox(
        self,
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
    ) -> None:
        self.outbox[event_id] = _OutboxRecord(
            id=event_id,
            owner_id=owner_id,
            project_id=project_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            request_sha256=request_sha256,
            workflow_id=workflow_id,
            state="PENDING",
            lease_token=None,
            lease_until=None,
            attempt_count=0,
            next_attempt_at=now,
            acknowledged_at=None,
            last_error=None,
            created_at=now,
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
        with self._lock:
            if any(
                record.workflow_id == workflow_id and record.acknowledged_at is None
                for record in self.outbox.values()
            ):
                return False
            self._append_outbox(
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
            return True

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
        with self._lock:
            if any(
                record.workflow_id == workflow_id and record.acknowledged_at is None
                for record in self.outbox.values()
            ):
                return False
            self._append_outbox(
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
            return True

    def claim_outbox(
        self, now: datetime, lease_until: datetime, token: UUID
    ) -> CoverOutboxEvent | None:
        with self._lock:
            candidates = sorted(
                (
                    record
                    for record in self.outbox.values()
                    if record.acknowledged_at is None
                    and record.next_attempt_at <= now
                    and (
                        record.project_id is None
                        or self.project_states.get((record.owner_id, record.project_id))
                        == "ACTIVE"
                    )
                    and (record.lease_until is None or record.lease_until < now)
                ),
                key=lambda record: (record.created_at, record.id.hex),
            )
            if not candidates:
                return None
            record = candidates[0]
            record.state = "CLAIMED"
            record.lease_token = token
            record.lease_until = lease_until
            record.attempt_count += 1
            return CoverOutboxEvent(
                record.id,
                record.owner_id,
                record.project_id,
                record.aggregate_type,
                record.aggregate_id,
                record.event_type,
                record.request_sha256,
                record.workflow_id,
                token,
                record.attempt_count,
            )

    def acknowledge_outbox(self, event_id: UUID, token: UUID, now: datetime) -> bool:
        with self._lock:
            record = self.outbox.get(event_id)
            if (
                record is None
                or record.lease_token != token
                or record.acknowledged_at is not None
            ):
                return False
            record.state = "ACKED"
            record.acknowledged_at = now
            record.lease_token = None
            record.lease_until = None
            record.last_error = None
            return True

    def retry_outbox(self, event_id: UUID, token: UUID, next_at: datetime, error: str) -> bool:
        with self._lock:
            record = self.outbox.get(event_id)
            if (
                record is None
                or record.lease_token != token
                or record.acknowledged_at is not None
            ):
                return False
            record.state = "PENDING"
            record.next_attempt_at = next_at
            record.lease_token = None
            record.lease_until = None
            record.last_error = error[:256]
            return True
