from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.models import CompositionVersionRow
from tubeflow_test.domain.common import AspectRatio, MediaAssetRef, SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import (
    Account,
    AccountRole,
    Channel,
    CharacterImage,
    ClonedVoice,
    ClonedVoiceAccount,
    ClonedVoiceUsage,
    Project,
    ProjectSnapshot,
    SnapshotCharacter,
    TtsProvider,
    VoiceMediaFact,
    VoiceVersion,
)
from tubeflow_test.identity_project.ports import (
    StagedObjectClaim,
    VideoDefectGroupFact,
    aggregate_workflow_attempt_states,
)
from tubeflow_test.video_generation.orchestration import (
    DELIVER_WITH_DEFECT_MARKER,
    VIDEO_CONTENT_BLACK_REGION,
)

from .models import (
    AccountRow,
    ChannelRow,
    CharacterImageRow,
    ClonedVoiceAccountRow,
    ClonedVoiceRow,
    ProjectPublicationEventRow,
    ProjectRow,
    ProjectSnapshotCharacterRow,
    SessionRow,
    StagedObjectRow,
    SystemInitializationRow,
    VoiceCatalogImportRow,
    VoiceVersionRow,
)


def _normalized_provider(value: str | None) -> str:
    """历史行/缺省值一律回落到 cosyvoice（§23 不回填既有项目）。"""
    if value in {item.value for item in TtsProvider}:
        return str(value)
    return TtsProvider.COSYVOICE.value


def _fact(fact: VoiceMediaFact | None, field: str) -> object:
    """EL 分支没有三件媒体事实（§23.1）：缺失列写 NULL，不伪造零值。"""
    return None if fact is None else getattr(fact, field)


class SqlAlchemyIdentityProjectRepository:
    """Transaction-scoped repository; callers must create one per request/UoW."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def setup_complete(self) -> bool:
        return self.session.get(SystemInitializationRow, 1) is not None

    def initialize_admin(self, account: Account, completed_at: datetime) -> bool:
        try:
            with self.session.begin_nested():
                self.session.add(
                    AccountRow(
                        id=account.id,
                        username=account.username,
                        password_hash=account.password_hash,
                        role=account.role.value,
                        active=account.active,
                        session_generation=account.session_generation,
                    )
                )
                self.session.add(
                    SystemInitializationRow(
                        singleton=1, admin_account_id=account.id, completed_at=completed_at
                    )
                )
                self.session.flush()
            return True
        except IntegrityError:
            return False

    def disable_account_preserving_admin(self, account_id: UUID) -> str:
        sentinel = self.session.scalar(
            select(SystemInitializationRow)
            .where(SystemInitializationRow.singleton == 1)
            .with_for_update()
        )
        if sentinel is None:
            return "not_found"
        target = self.session.get(AccountRow, account_id)
        if target is None:
            return "not_found"
        if target.role == "admin" and target.active and self.active_admin_count() <= 1:
            return "last_admin"
        target.active = False
        target.session_generation += 1
        self.session.flush()
        return "disabled"

    def add_account(self, account: Account) -> None:
        try:
            with self.session.begin_nested():
                self.session.add(
                    AccountRow(
                        id=account.id,
                        username=account.username,
                        password_hash=account.password_hash,
                        role=account.role.value,
                        active=account.active,
                        session_generation=account.session_generation,
                    )
                )
                self.session.flush()
        except IntegrityError as exc:
            raise DomainError("invalid_username", "Username is unavailable") from exc

    def list_accounts(self) -> tuple[Account, ...]:
        values = (
            self._account(row)
            for row in self.session.scalars(select(AccountRow).order_by(AccountRow.username))
        )
        return tuple(item for item in values if item is not None)

    def active_admin_count(self) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(AccountRow)
                .where(AccountRow.role == "admin", AccountRow.active.is_(True))
            )
            or 0
        )

    def account_by_username(self, username: str) -> Account | None:
        return self._account(
            self.session.scalar(select(AccountRow).where(AccountRow.username == username))
        )

    def account(self, account_id: UUID) -> Account | None:
        return self._account(self.session.get(AccountRow, account_id))

    def disable_account(self, account_id: UUID) -> bool:
        result = cast(
            CursorResult[Any],
            self.session.execute(
                update(AccountRow)
                .where(AccountRow.id == account_id)
                .values(active=False, session_generation=AccountRow.session_generation + 1)
            ),
        )
        self.session.flush()

        return bool(result.rowcount)

    def reset_password(self, account_id: UUID, password_hash: str) -> bool:
        result = cast(
            CursorResult[Any],
            self.session.execute(
                update(AccountRow)
                .where(AccountRow.id == account_id)
                .values(
                    password_hash=password_hash,
                    session_generation=AccountRow.session_generation + 1,
                )
            ),
        )
        self.session.flush()
        return bool(result.rowcount)

    def logout(self, account_id: UUID, generation: int) -> None:
        self.session.execute(
            update(AccountRow)
            .where(AccountRow.id == account_id, AccountRow.session_generation == generation)
            .values(session_generation=generation + 1)
        )
        self.session.flush()

    def create_session(self, token_sha256: str, account_id: UUID, generation: int) -> None:
        self.session.add(
            SessionRow(token_sha256=token_sha256, account_id=account_id, generation=generation)
        )
        self.session.flush()

    def session_account(self, token_sha256: str) -> tuple[Account, int] | None:
        row = self.session.execute(
            select(AccountRow, SessionRow.generation)
            .join(SessionRow, SessionRow.account_id == AccountRow.id)
            .where(SessionRow.token_sha256 == token_sha256)
        ).one_or_none()
        if row is None:
            return None
        account = self._account(row[0])
        return (account, int(row[1])) if account is not None else None

    def revoke_session(self, token_sha256: str) -> None:
        self.session.execute(delete(SessionRow).where(SessionRow.token_sha256 == token_sha256))
        self.session.flush()

    def add_channel(self, channel: Channel) -> None:
        self.session.add(
            ChannelRow(
                id=channel.id,
                owner_id=channel.owner_id,
                url=channel.url,
                name=channel.name,
                default_voice_version_id=channel.default_voice_version_id,
                default_character_image_ids=[
                    str(item) for item in channel.default_character_image_ids
                ],
                default_tts_provider=channel.default_tts_provider,
                default_cloned_voice_id=channel.default_cloned_voice_id,
                default_visual_style=channel.default_visual_style,
            )
        )
        self.session.flush()

    def list_channels(self, owner_id: UUID) -> tuple[Channel, ...]:
        return tuple(
            self._channel(row)
            for row in self.session.scalars(
                select(ChannelRow).where(ChannelRow.owner_id == owner_id).order_by(ChannelRow.id)
            )
        )

    def channel(self, owner_id: UUID, channel_id: UUID) -> Channel | None:
        row = self.session.scalar(
            select(ChannelRow).where(ChannelRow.id == channel_id, ChannelRow.owner_id == owner_id)
        )
        return self._channel(row) if row else None

    def save_channel(self, channel: Channel) -> None:
        self.session.execute(
            update(ChannelRow)
            .where(ChannelRow.id == channel.id, ChannelRow.owner_id == channel.owner_id)
            .values(
                url=channel.url,
                name=channel.name,
                default_voice_version_id=channel.default_voice_version_id,
                default_character_image_ids=[
                    str(item) for item in channel.default_character_image_ids
                ],
                default_tts_provider=channel.default_tts_provider,
                default_cloned_voice_id=channel.default_cloned_voice_id,
                default_visual_style=channel.default_visual_style,
            )
        )
        self.session.flush()

    @staticmethod
    def _channel(row: ChannelRow) -> Channel:
        return Channel(
            id=row.id,
            owner_id=row.owner_id,
            url=row.url,
            default_voice_version_id=row.default_voice_version_id,
            default_character_image_ids=tuple(
                UUID(item) for item in row.default_character_image_ids
            ),
            name=row.name,
            current_cover_style_profile_id=row.current_cover_style_profile_id,
            default_tts_provider=_normalized_provider(row.default_tts_provider),
            default_cloned_voice_id=row.default_cloned_voice_id,
            default_visual_style=row.default_visual_style or "",
        )

    def cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> ClonedVoice | None:
        row = self.session.get(ClonedVoiceRow, cloned_voice_id)
        if row is None or row.channel_id != channel_id:
            return None
        return self._cloned_voice(row)

    def cloned_voice_by_id(self, cloned_voice_id: UUID) -> ClonedVoice | None:
        row = self.session.get(ClonedVoiceRow, cloned_voice_id)
        return self._cloned_voice(row) if row is not None else None

    def list_cloned_voices(self, channel_id: UUID) -> tuple[ClonedVoice, ...]:
        return tuple(
            self._cloned_voice(row)
            for row in self.session.scalars(
                select(ClonedVoiceRow)
                .where(ClonedVoiceRow.channel_id == channel_id)
                .order_by(ClonedVoiceRow.created_at, ClonedVoiceRow.id)
            )
        )

    def add_cloned_voice(self, voice: ClonedVoice) -> None:
        self.session.add(
            ClonedVoiceRow(
                id=voice.id,
                channel_id=voice.channel_id,
                owner_id=voice.owner_id,
                name=voice.name,
                remark=voice.remark,
                source_object_key=voice.source_object_key,
                source_sha256=voice.source_sha256,
                source_size_bytes=voice.source_size_bytes,
                source_media_type=voice.source_media_type,
                created_at=voice.created_at,
            )
        )
        self.session.flush()

    def delete_cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> bool:
        result = cast(
            CursorResult[Any],
            self.session.execute(
                delete(ClonedVoiceRow).where(
                    ClonedVoiceRow.id == cloned_voice_id,
                    ClonedVoiceRow.channel_id == channel_id,
                )
            ),
        )
        self.session.flush()
        return bool(result.rowcount)

    def cloned_voice_usage(self, cloned_voice_id: UUID) -> ClonedVoiceUsage:
        frozen = self.session.scalar(
            select(func.count())
            .select_from(ProjectRow)
            .where(ProjectRow.snapshot_cloned_voice_id == cloned_voice_id)
        )
        drafts = self.session.scalar(
            select(func.count())
            .select_from(ProjectRow)
            .where(
                ProjectRow.cloned_voice_id == cloned_voice_id,
                ProjectRow.production_state == "ACTIVE",
                ProjectRow.snapshot_confirmed_at.is_(None),
            )
        )
        defaults = self.session.scalar(
            select(func.count())
            .select_from(ChannelRow)
            .where(ChannelRow.default_cloned_voice_id == cloned_voice_id)
        )
        return ClonedVoiceUsage(int(frozen or 0), int(drafts or 0), int(defaults or 0))

    def cloned_voice_account(
        self, cloned_voice_id: UUID, pool_key_id: UUID
    ) -> ClonedVoiceAccount | None:
        row = self.session.get(ClonedVoiceAccountRow, (cloned_voice_id, pool_key_id))
        return self._cloned_voice_account(row) if row is not None else None

    def list_cloned_voice_accounts(
        self, cloned_voice_id: UUID
    ) -> tuple[ClonedVoiceAccount, ...]:
        return tuple(
            self._cloned_voice_account(row)
            for row in self.session.scalars(
                select(ClonedVoiceAccountRow)
                .where(ClonedVoiceAccountRow.cloned_voice_id == cloned_voice_id)
                .order_by(ClonedVoiceAccountRow.created_at, ClonedVoiceAccountRow.pool_key_id)
            )
        )

    def save_cloned_voice_account(self, account: ClonedVoiceAccount) -> None:
        row = self.session.get(
            ClonedVoiceAccountRow, (account.cloned_voice_id, account.pool_key_id)
        )
        if row is None:
            self.session.add(
                ClonedVoiceAccountRow(
                    cloned_voice_id=account.cloned_voice_id,
                    pool_key_id=account.pool_key_id,
                    el_voice_id=account.el_voice_id,
                    created_at=account.created_at,
                )
            )
        else:
            row.el_voice_id = account.el_voice_id
        self.session.flush()

    def forget_cloned_voice_account(self, cloned_voice_id: UUID, pool_key_id: UUID) -> None:
        self.session.execute(
            delete(ClonedVoiceAccountRow).where(
                ClonedVoiceAccountRow.cloned_voice_id == cloned_voice_id,
                ClonedVoiceAccountRow.pool_key_id == pool_key_id,
            )
        )
        self.session.flush()

    def cloned_voice_ids_for_project(self, owner_id: UUID, project_id: UUID) -> tuple[UUID, ...]:
        row = self.session.scalar(
            select(ProjectRow).where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
        )
        if row is None:
            return ()
        values = [
            item
            for item in (row.snapshot_cloned_voice_id, row.cloned_voice_id)
            if item is not None
        ]
        return tuple(dict.fromkeys(values))

    def count_active_draft_projects_with_cloned_voice(self, cloned_voice_id: UUID) -> int:
        count = self.session.scalar(
            select(func.count())
            .select_from(ProjectRow)
            .where(
                ProjectRow.cloned_voice_id == cloned_voice_id,
                ProjectRow.production_state == "ACTIVE",
                ProjectRow.snapshot_confirmed_at.is_(None),
            )
        )
        return int(count or 0)

    def list_cloned_voice_channels(self) -> tuple[UUID, ...]:
        return tuple(
            self.session.scalars(select(ClonedVoiceRow.channel_id).distinct())
        )

    @staticmethod
    def _cloned_voice(row: ClonedVoiceRow) -> ClonedVoice:
        return ClonedVoice(
            id=row.id,
            channel_id=row.channel_id,
            owner_id=row.owner_id,
            name=row.name,
            remark=row.remark,
            source_object_key=row.source_object_key,
            source_sha256=row.source_sha256,
            source_size_bytes=row.source_size_bytes,
            source_media_type=row.source_media_type,
            created_at=row.created_at,
        )

    @staticmethod
    def _cloned_voice_account(row: ClonedVoiceAccountRow) -> ClonedVoiceAccount:
        return ClonedVoiceAccount(
            cloned_voice_id=row.cloned_voice_id,
            pool_key_id=row.pool_key_id,
            el_voice_id=row.el_voice_id,
            created_at=row.created_at,
        )

    def assert_channel_writable(self, owner_id: UUID, channel_id: UUID) -> None:
        purging = self.session.scalar(
            select(ProjectRow.id).where(
                ProjectRow.owner_id == owner_id,
                ProjectRow.channel_id == channel_id,
                ProjectRow.production_state == "PURGING",
            )
        )
        if purging is not None:
            raise DomainError("project_purging", "Project is being permanently deleted")

    def add_image(self, image: CharacterImage, claim: StagedObjectClaim | None = None) -> None:
        if claim is None or claim.key != image.object_key:
            raise DomainError("staged_object_claim_lost", "Image upload claim is unavailable")
        try:
            with self.session.begin_nested():
                self.session.add(
                    CharacterImageRow(
                        id=image.id,
                        owner_id=image.owner_id,
                        channel_id=image.channel_id,
                        asset_id=image.asset.asset_id,
                        object_key=image.object_key,
                        sha256=image.asset.sha256,
                        media_type=image.asset.media_type,
                        size_bytes=image.asset.size_bytes,
                        width=image.width,
                        height=image.height,
                    )
                )
                self.session.flush()
                self.attach_staged_objects((claim,), "character_image")
        except IntegrityError as exc:
            raise DomainError(
                "duplicate_character_image", "Character image already exists"
            ) from exc

    def image(self, owner_id: UUID, channel_id: UUID, image_id: UUID) -> CharacterImage | None:
        row = self.session.scalar(
            select(CharacterImageRow).where(
                CharacterImageRow.id == image_id,
                CharacterImageRow.owner_id == owner_id,
                CharacterImageRow.channel_id == channel_id,
            )
        )
        return self._image(row)

    def list_images(self, owner_id: UUID, channel_id: UUID) -> tuple[CharacterImage, ...]:
        values = (
            self._image(row)
            for row in self.session.scalars(
                select(CharacterImageRow).where(
                    CharacterImageRow.owner_id == owner_id,
                    CharacterImageRow.channel_id == channel_id,
                )
            )
        )
        return tuple(item for item in values if item is not None)

    def add_project(self, project: Project) -> None:
        self.session.add(self._project_row(project))
        self.session.flush()

    def list_projects(self, owner_id: UUID) -> tuple[Project, ...]:
        return tuple(
            self._project(row)
            for row in self.session.scalars(
                select(ProjectRow).where(ProjectRow.owner_id == owner_id).order_by(ProjectRow.id)
            )
        )

    def project(self, owner_id: UUID, project_id: UUID) -> Project | None:
        row = self.session.scalar(
            select(ProjectRow).where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
        )
        return self._project(row) if row else None

    def project_for_write(self, owner_id: UUID, project_id: UUID) -> Project | None:
        row = self.session.scalar(
            select(ProjectRow)
            .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            .with_for_update()
        )
        return self._project(row) if row else None

    def save_project(self, project: Project, expected_revision: int) -> bool:
        result = cast(
            CursorResult[Any],
            self.session.execute(
                update(ProjectRow)
                .where(
                    ProjectRow.id == project.id,
                    ProjectRow.owner_id == project.owner_id,
                    ProjectRow.draft_revision == expected_revision,
                    ProjectRow.production_state == "ACTIVE",
                )
                .values(
                    script=project.script,
                    character_image_ids=list(project.character_image_ids),
                    visual_style=project.visual_style,
                    tts_provider=project.tts_provider,
                    voice_version_id=project.voice_version_id,
                    cloned_voice_id=project.cloned_voice_id,
                    locale=project.locale.value,
                    aspect_ratio=project.aspect_ratio.value,
                    expected_duration_seconds=project.expected_duration_seconds,
                    draft_revision=project.draft_revision,
                )
            ),
        )
        self.session.flush()
        return bool(result.rowcount)

    def confirm_project(
        self, owner_id: UUID, project_id: UUID, expected_revision: int, snapshot: ProjectSnapshot
    ) -> bool:
        result = cast(
            CursorResult[Any],
            self.session.execute(
                update(ProjectRow)
                .where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                    ProjectRow.snapshot_confirmed_at.is_(None),
                    ProjectRow.draft_revision == expected_revision,
                    ProjectRow.production_state == "ACTIVE",
                )
                .values(
                    snapshot_script=snapshot.script,
                    snapshot_character_image_ids=list(snapshot.character_image_ids),
                    snapshot_visual_style=snapshot.visual_style,
                    snapshot_tts_provider=snapshot.tts_provider,
                    snapshot_voice_version_id=snapshot.voice_version_id,
                    snapshot_voice_metadata_sha256=snapshot.voice_metadata_sha256,
                    snapshot_voice_catalog_source_sha256=snapshot.voice_catalog_source_sha256,
                    snapshot_voice_reference_asset_id=_fact(
                        snapshot.voice_reference_zip, "asset_id"
                    ),
                    snapshot_voice_reference_sha256=_fact(snapshot.voice_reference_zip, "sha256"),
                    snapshot_voice_reference_media_type=_fact(
                        snapshot.voice_reference_zip, "media_type"
                    ),
                    snapshot_voice_reference_size_bytes=_fact(
                        snapshot.voice_reference_zip, "size_bytes"
                    ),
                    snapshot_voice_preview_asset_id=_fact(snapshot.voice_preview, "asset_id"),
                    snapshot_voice_preview_sha256=_fact(snapshot.voice_preview, "sha256"),
                    snapshot_voice_preview_media_type=_fact(snapshot.voice_preview, "media_type"),
                    snapshot_voice_preview_size_bytes=_fact(snapshot.voice_preview, "size_bytes"),
                    snapshot_voice_avatar_asset_id=_fact(snapshot.voice_avatar, "asset_id"),
                    snapshot_voice_avatar_sha256=_fact(snapshot.voice_avatar, "sha256"),
                    snapshot_voice_avatar_media_type=_fact(snapshot.voice_avatar, "media_type"),
                    snapshot_voice_avatar_size_bytes=_fact(snapshot.voice_avatar, "size_bytes"),
                    snapshot_cloned_voice_id=snapshot.cloned_voice_id,
                    snapshot_cloned_voice_name=snapshot.cloned_voice_name,
                    snapshot_cloned_voice_source_sha256=snapshot.cloned_voice_source_sha256,
                    snapshot_locale=snapshot.locale.value,
                    snapshot_aspect_ratio=snapshot.aspect_ratio.value,
                    snapshot_channel_url=snapshot.channel_url,
                    snapshot_expected_duration_seconds=snapshot.expected_duration_seconds,
                    snapshot_confirmed_at=snapshot.confirmed_at,
                )
            ),
        )
        if not result.rowcount:
            return False
        for item in snapshot.characters:
            self.session.add(
                ProjectSnapshotCharacterRow(
                    id=uuid4(),
                    project_id=project_id,
                    ordinal=item.ordinal,
                    character_image_id=item.character_image_id,
                    asset_id=item.asset_id,
                    sha256=item.sha256,
                    media_type=item.media_type,
                    size_bytes=item.size_bytes,
                )
            )
        self.session.flush()
        return True

    def project_has_current_composition(self, owner_id: UUID, project_id: UUID) -> bool:
        """发布门禁只看当前成片（与列表 flow.compositions 同口径）。"""
        return (
            self.session.scalar(
                select(CompositionVersionRow.id)
                .where(
                    CompositionVersionRow.owner_id == owner_id,
                    CompositionVersionRow.project_id == project_id,
                    CompositionVersionRow.current.is_(True),
                )
                .limit(1)
            )
            is not None
        )

    def set_project_publication(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        published_at: datetime | None,
        actor_account_id: UUID,
        occurred_at: datetime,
    ) -> bool:
        """条件 UPDATE + 审计 INSERT 同事务；调用方必须已持有项目行锁。"""
        target_state = (
            ProjectRow.published_at.is_(None)
            if published_at is not None
            else ProjectRow.published_at.is_not(None)
        )
        result = cast(
            CursorResult[Any],
            self.session.execute(
                update(ProjectRow)
                .where(
                    ProjectRow.id == project_id,
                    ProjectRow.owner_id == owner_id,
                    ProjectRow.production_state == "ACTIVE",
                    target_state,
                )
                .values(published_at=published_at)
            ),
        )
        if result.rowcount != 1:
            return False
        self.session.add(
            ProjectPublicationEventRow(
                id=uuid4(),
                project_id=project_id,
                owner_id=owner_id,
                actor_account_id=actor_account_id,
                action="PUBLISHED" if published_at is not None else "UNPUBLISHED",
                occurred_at=occurred_at,
            )
        )
        self.session.flush()
        return True

    def voice_version(self, voice_version_id: UUID) -> VoiceVersion | None:
        row = self.session.scalar(
            select(VoiceVersionRow)
            .join(
                VoiceCatalogImportRow,
                VoiceCatalogImportRow.source_sha256
                == VoiceVersionRow.catalog_source_sha256,
            )
            .where(
                VoiceVersionRow.id == voice_version_id,
                VoiceVersionRow.enabled.is_(True),
                VoiceCatalogImportRow.status == "completed",
            )
        )
        return self._voice(row)

    def list_voice_versions(self) -> tuple[VoiceVersion, ...]:
        values = (
            self._voice(row)
            for row in self.session.scalars(
                select(VoiceVersionRow)
                .join(
                    VoiceCatalogImportRow,
                    VoiceCatalogImportRow.source_sha256
                    == VoiceVersionRow.catalog_source_sha256,
                )
                .where(
                    VoiceVersionRow.enabled.is_(True),
                    VoiceCatalogImportRow.status == "completed",
                )
            )
        )
        return tuple(item for item in values if item is not None)

    def add_voice_version(
        self,
        voice: VoiceVersion,
        media: dict[str, tuple[UUID, str, str, int]],
        claims: dict[str, StagedObjectClaim] | None = None,
    ) -> bool:
        if claims is None or set(claims) != set(media):
            raise DomainError("staged_object_claim_lost", "Voice upload claims are unavailable")
        existing = self.session.scalar(
            select(VoiceVersionRow).where(
                VoiceVersionRow.voice_id == voice.voice_id,
                VoiceVersionRow.version == voice.version,
            )
        )
        if existing is not None:
            if (
                existing.metadata_sha256 != voice.metadata_sha256
                or existing.catalog_source_sha256 != voice.catalog_source_sha256
                or existing.reference_zip_sha256 != voice.reference_zip_sha256
                or existing.preview_sha256 != voice.preview_sha256
                or existing.avatar_sha256 != voice.avatar_sha256
            ):
                raise DomainError(
                    "voice_version_conflict",
                    "Voice version already exists with different content",
                )
            self.attach_staged_objects(tuple(claims.values()), "voice_version")
            return False
        reference = media["reference"]
        preview = media["preview"]
        avatar = media["avatar"]
        self.session.add(
            VoiceVersionRow(
                id=voice.id,
                voice_id=voice.voice_id,
                version=voice.version,
                display_name=voice.display_name,
                reference_zip_asset_id=reference[0],
                reference_zip_object_key=reference[1],
                reference_zip_media_type=reference[2],
                reference_zip_size_bytes=reference[3],
                preview_asset_id=preview[0],
                preview_object_key=preview[1],
                preview_media_type=preview[2],
                preview_size_bytes=preview[3],
                avatar_asset_id=avatar[0],
                avatar_object_key=avatar[1],
                avatar_media_type=avatar[2],
                avatar_size_bytes=avatar[3],
                metadata_sha256=voice.metadata_sha256,
                catalog_source_sha256=voice.catalog_source_sha256,
                reference_zip_sha256=voice.reference_zip_sha256,
                preview_sha256=voice.preview_sha256,
                avatar_sha256=voice.avatar_sha256,
                supported_locales=[item.value for item in voice.supported_locales],
                raw_language_codes=list(voice.raw_language_codes),
                catalog_metadata_json=voice.catalog_metadata_json,
                enabled=voice.enabled,
            )
        )
        self.session.flush()
        self.attach_staged_objects(tuple(claims.values()), "voice_version")
        return True

    def lock_voice_version_import(self, voice_id: str, version: int) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        lock_key = f"public-voice:{voice_id}:v{version}"
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))

    def lock_voice_catalog_import(self, source_sha256: str) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        lock_key = f"public-voice-catalog:{source_sha256}"
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))

    def voice_media(self, voice_version_id: UUID, kind: str) -> tuple[str, str, str, int] | None:
        row = self.session.scalar(
            select(VoiceVersionRow)
            .join(
                VoiceCatalogImportRow,
                VoiceCatalogImportRow.source_sha256
                == VoiceVersionRow.catalog_source_sha256,
            )
            .where(
                VoiceVersionRow.id == voice_version_id,
                VoiceVersionRow.enabled.is_(True),
                VoiceCatalogImportRow.status == "completed",
            )
        )
        if row is None:
            return None
        values = {
            "reference": (
                row.reference_zip_object_key,
                row.reference_zip_media_type,
                row.reference_zip_sha256,
                row.reference_zip_size_bytes,
            ),
            "preview": (
                row.preview_object_key,
                row.preview_media_type,
                row.preview_sha256,
                row.preview_size_bytes,
            ),
            "avatar": (
                row.avatar_object_key,
                row.avatar_media_type,
                row.avatar_sha256,
                row.avatar_size_bytes,
            ),
        }
        return values.get(kind)

    def catalog_import_progress(self, source_sha256: str) -> tuple[int, str] | None:
        row = self.session.get(VoiceCatalogImportRow, source_sha256)
        return (row.imported_count, row.status) if row else None

    def save_catalog_import(
        self,
        source_sha256: str,
        catalog_version: int,
        expected_count: int,
        imported_count: int,
        status: str,
        updated_at: datetime,
    ) -> None:
        row = self.session.get(VoiceCatalogImportRow, source_sha256)
        if row is None:
            self.session.add(
                VoiceCatalogImportRow(
                    source_sha256=source_sha256,
                    catalog_version=catalog_version,
                    expected_count=expected_count,
                    imported_count=imported_count,
                    status=status,
                    updated_at=updated_at,
                )
            )
        else:
            row.imported_count = imported_count
            row.status = status
            row.updated_at = updated_at
        self.session.flush()

    def publish_voice_catalog(
        self,
        source_sha256: str,
        catalog_version: int,
        expected_count: int,
        updated_at: datetime,
    ) -> None:
        catalog = self.session.scalar(
            select(VoiceCatalogImportRow)
            .where(VoiceCatalogImportRow.source_sha256 == source_sha256)
            .with_for_update()
        )
        if (
            catalog is None
            or catalog.catalog_version != catalog_version
            or catalog.expected_count != expected_count
            or catalog.imported_count != expected_count
            or expected_count <= 0
        ):
            raise DomainError(
                "voice_catalog_publish_incomplete",
                "Voice catalog is incomplete and cannot be published",
            )
        rows = tuple(
            self.session.scalars(
                select(VoiceVersionRow).where(
                    VoiceVersionRow.catalog_source_sha256 == source_sha256
                )
            )
        )
        if len(rows) != expected_count:
            raise DomainError(
                "voice_catalog_publish_incomplete",
                "Voice catalog is incomplete and cannot be published",
            )
        if catalog.status == "completed":
            if not all(row.enabled for row in rows):
                raise DomainError(
                    "voice_catalog_publish_incomplete",
                    "Published voice catalog is internally inconsistent",
                )
            return
        if catalog.status != "importing" or any(row.enabled for row in rows):
            raise DomainError(
                "voice_catalog_publish_incomplete",
                "Voice catalog is internally inconsistent and cannot be published",
            )
        result = cast(
            CursorResult[Any],
            self.session.execute(
                update(VoiceVersionRow)
                .where(VoiceVersionRow.catalog_source_sha256 == source_sha256)
                .values(enabled=True)
            ),
        )
        if result.rowcount != expected_count:
            raise DomainError(
                "voice_catalog_publish_incomplete",
                "Voice catalog publication did not update every voice",
            )
        catalog.status = "completed"
        catalog.updated_at = updated_at
        self.session.flush()

    def attach_staged_objects(
        self, claims: tuple[StagedObjectClaim, ...], reference_type: str
    ) -> None:
        attached_tokens: list[UUID] = []
        for claim in claims:
            attach_token = uuid4()
            result = cast(
                CursorResult[Any],
                self.session.execute(
                    update(StagedObjectRow)
                    .where(
                        StagedObjectRow.object_key == claim.key,
                        StagedObjectRow.state == "STAGED",
                        StagedObjectRow.generation == claim.generation,
                        StagedObjectRow.execution_token == claim.execution_token,
                    )
                    .values(
                        state="ATTACHED",
                        reference_type=reference_type,
                        execution_token=attach_token,
                    )
                ),
            )
            if result.rowcount != 1:
                raise DomainError(
                    "staged_object_claim_lost",
                    "A staged object is no longer available for attachment",
                )
            attached_tokens.append(attach_token)
        for claim, attach_token in zip(claims, attached_tokens, strict=True):
            claim.execution_token = attach_token

    @staticmethod
    def _account(row: AccountRow | None) -> Account | None:
        return (
            Account(
                row.id,
                row.username,
                row.password_hash,
                AccountRole(row.role),
                row.active,
                row.session_generation,
            )
            if row
            else None
        )

    @staticmethod
    def _image(row: CharacterImageRow | None) -> CharacterImage | None:
        return (
            CharacterImage(
                row.id,
                row.owner_id,
                MediaAssetRef(row.asset_id, row.sha256, row.media_type, row.size_bytes),
                row.width,
                row.height,
                row.object_key,
                row.channel_id,
            )
            if row
            else None
        )

    @staticmethod
    def _voice(row: VoiceVersionRow | None) -> VoiceVersion | None:
        return (
            VoiceVersion(
                row.id,
                row.metadata_sha256,
                tuple(SupportedLocale(x) for x in row.supported_locales),
                row.enabled,
                row.reference_zip_sha256,
                row.preview_sha256,
                row.avatar_sha256,
                row.voice_id,
                row.version,
                row.display_name,
                row.catalog_source_sha256,
                VoiceMediaFact(
                    row.reference_zip_asset_id,
                    row.reference_zip_sha256,
                    row.reference_zip_media_type,
                    row.reference_zip_size_bytes,
                ),
                VoiceMediaFact(
                    row.preview_asset_id,
                    row.preview_sha256,
                    row.preview_media_type,
                    row.preview_size_bytes,
                ),
                VoiceMediaFact(
                    row.avatar_asset_id,
                    row.avatar_sha256,
                    row.avatar_media_type,
                    row.avatar_size_bytes,
                ),
                tuple(row.raw_language_codes),
                row.catalog_metadata_json,
            )
            if row
            else None
        )

    @staticmethod
    def _project_row(p: Project) -> ProjectRow:
        return ProjectRow(
            id=p.id,
            owner_id=p.owner_id,
            channel_id=p.channel_id,
            title=p.title,
            script=p.script,
            character_image_ids=list(p.character_image_ids),
            visual_style=p.visual_style,
            tts_provider=p.tts_provider,
            voice_version_id=p.voice_version_id,
            cloned_voice_id=p.cloned_voice_id,
            locale=p.locale.value,
            aspect_ratio=p.aspect_ratio.value,
            expected_duration_seconds=p.expected_duration_seconds,
            draft_revision=p.draft_revision,
            production_state=p.production_state,
            production_revision=p.production_revision,
            production_mode=p.production_mode,
        )

    def _project(self, row: ProjectRow) -> Project:
        provider = _normalized_provider(row.tts_provider)
        snapshot = None
        if row.snapshot_confirmed_at is not None:
            if row.snapshot_locale is None or row.snapshot_aspect_ratio is None:
                raise ValueError("incomplete project snapshot")
            snapshot_provider = _normalized_provider(row.snapshot_tts_provider or provider)
            if snapshot_provider == TtsProvider.COSYVOICE.value and (
                row.snapshot_voice_version_id is None
                or row.snapshot_voice_reference_asset_id is None
                or row.snapshot_voice_preview_asset_id is None
                or row.snapshot_voice_avatar_asset_id is None
            ):
                raise ValueError("incomplete project snapshot")
            if (
                snapshot_provider == TtsProvider.ELEVENLABS.value
                and row.snapshot_cloned_voice_id is None
            ):
                raise ValueError("incomplete project snapshot")
            chars = tuple(
                SnapshotCharacter(
                    x.ordinal,
                    x.character_image_id,
                    x.asset_id,
                    x.sha256,
                    x.media_type,
                    x.size_bytes,
                )
                for x in self.session.scalars(
                    select(ProjectSnapshotCharacterRow)
                    .where(ProjectSnapshotCharacterRow.project_id == row.id)
                    .order_by(ProjectSnapshotCharacterRow.ordinal)
                )
            )
            reference_zip = None
            preview = None
            avatar = None
            if row.snapshot_voice_reference_asset_id is not None:
                reference_zip = VoiceMediaFact(
                    row.snapshot_voice_reference_asset_id,
                    row.snapshot_voice_reference_sha256 or "",
                    row.snapshot_voice_reference_media_type or "",
                    row.snapshot_voice_reference_size_bytes or 0,
                )
            if row.snapshot_voice_preview_asset_id is not None:
                preview = VoiceMediaFact(
                    row.snapshot_voice_preview_asset_id,
                    row.snapshot_voice_preview_sha256 or "",
                    row.snapshot_voice_preview_media_type or "",
                    row.snapshot_voice_preview_size_bytes or 0,
                )
            if row.snapshot_voice_avatar_asset_id is not None:
                avatar = VoiceMediaFact(
                    row.snapshot_voice_avatar_asset_id,
                    row.snapshot_voice_avatar_sha256 or "",
                    row.snapshot_voice_avatar_media_type or "",
                    row.snapshot_voice_avatar_size_bytes or 0,
                )
            snapshot = ProjectSnapshot(
                script=row.snapshot_script or "",
                characters=chars,
                visual_style=row.snapshot_visual_style or "",
                locale=SupportedLocale(row.snapshot_locale),
                aspect_ratio=AspectRatio(row.snapshot_aspect_ratio),
                channel_url=row.snapshot_channel_url or "",
                expected_duration_seconds=row.snapshot_expected_duration_seconds,
                confirmed_at=row.snapshot_confirmed_at,
                tts_provider=snapshot_provider,
                voice_version_id=row.snapshot_voice_version_id,
                voice_metadata_sha256=row.snapshot_voice_metadata_sha256 or "",
                voice_catalog_source_sha256=row.snapshot_voice_catalog_source_sha256 or "",
                voice_reference_zip=reference_zip,
                voice_preview=preview,
                voice_avatar=avatar,
                cloned_voice_id=row.snapshot_cloned_voice_id,
                cloned_voice_name=row.snapshot_cloned_voice_name or "",
                cloned_voice_source_sha256=row.snapshot_cloned_voice_source_sha256 or "",
            )
        return Project(
            row.id,
            row.owner_id,
            row.channel_id,
            row.script,
            tuple(row.character_image_ids),
            row.visual_style,
            SupportedLocale(row.locale),
            AspectRatio(row.aspect_ratio),
            row.expected_duration_seconds,
            row.draft_revision,
            snapshot,
            row.production_state,
            row.production_revision,
            row.title,
            created_at=row.created_at,
            production_mode=row.production_mode or "MANUAL",
            current_cover_image_generation_id=row.current_cover_image_generation_id,
            tts_provider=provider,
            voice_version_id=row.voice_version_id,
            cloned_voice_id=row.cloned_voice_id,
            published_at=row.published_at,
        )

    def workflow_counts(self, owner_id: UUID, project_id: UUID) -> dict[str, int]:
        values = (
            self.session.execute(
                text(
                    """
                SELECT
                  (SELECT count(*) FROM authoritative_narration_sentences
                    WHERE owner_id=:owner AND project_id=:project) AS sentences,
                  (SELECT count(*) FROM authoritative_narration_sentences
                    WHERE owner_id=:owner AND project_id=:project
                      AND raw_qc_passed AND normalized_qc_passed
                      AND measured_frame_count > 0 AND sample_rate_hz > 0
                      AND measured_duration_ms > 0
                      AND end_frame > start_frame AND end_ms > start_ms) AS ready_sentences,
                  (SELECT count(*) FROM authoritative_narration_groups
                    WHERE owner_id=:owner AND project_id=:project) AS groups,
                  (SELECT count(DISTINCT m.sentence_id)
                    FROM authoritative_narration_memberships m
                    JOIN authoritative_narration_sentences s ON s.id=m.sentence_id
                    JOIN authoritative_narration_groups g ON g.id=m.group_id
                    WHERE s.owner_id=:owner AND s.project_id=:project
                      AND g.owner_id=:owner AND g.project_id=:project
                      AND s.snapshot_revision=g.snapshot_revision) AS grouped_sentences,
                  (SELECT count(*) FROM authoritative_narration_groups
                    WHERE owner_id=:owner AND project_id=:project AND video_blocked) AS blocked,
                  COALESCE((SELECT total_units FROM project_director_runs
                    WHERE owner_id=:owner AND project_id=:project
                    ORDER BY created_at DESC, id DESC LIMIT 1), 0)
                    AS director_preplan_total_units,
                  (SELECT count(*) FROM project_director_units u
                    WHERE u.owner_id=:owner AND u.project_id=:project
                      AND u.status='SUCCEEDED'
                      AND u.run_id=(SELECT r.id FROM project_director_runs r
                        WHERE r.owner_id=:owner AND r.project_id=:project
                        ORDER BY r.created_at DESC, r.id DESC LIMIT 1))
                    AS director_preplan_completed_units,
                  (SELECT count(*) FROM project_director_current c
                    WHERE c.owner_id=:owner AND c.project_id=:project
                      AND c.revision_id IS NOT NULL) AS director_preplan_revisions,
                  (SELECT count(*) FROM project_director_materializations m
                    JOIN project_director_current c
                      ON c.owner_id=m.owner_id AND c.project_id=m.project_id
                      AND c.revision_id=m.preplan_revision_id
                    WHERE m.owner_id=:owner AND m.project_id=:project)
                    AS director_materializations,
                  (SELECT count(*) FROM storyboard_director_pointers p
                    JOIN authoritative_narration_groups g ON g.id=p.group_id
                    WHERE p.owner_id=:owner AND g.project_id=:project
                      AND NOT g.video_blocked
                      AND p.applied_revision_id IS NOT NULL) AS directed,
                  (SELECT count(*) FROM storyboard_director_pointers p
                    JOIN authoritative_narration_groups g ON g.id=p.group_id
                    LEFT JOIN storyboard_director_revisions d
                      ON d.id=p.draft_revision_id AND d.owner_id=p.owner_id
                    WHERE p.owner_id=:owner AND g.project_id=:project
                      AND NOT g.video_blocked
                      AND (
                        p.applied_revision_id IS NOT NULL
                        OR (
                          p.draft_revision_id IS NOT NULL
                          AND d.review_state <> 'REQUIRED'
                        )
                      )) AS director_ready,
                  (SELECT count(*) FROM storyboard_current c
                    JOIN authoritative_narration_groups g ON g.id=c.group_id
                    JOIN storyboard_generations s ON s.id=c.generation_id
                    JOIN storyboard_media m ON m.generation_id=s.id
                    WHERE c.owner_id=:owner AND g.project_id=:project
                      AND g.owner_id=:owner AND NOT g.video_blocked
                      AND s.owner_id=:owner AND s.project_id=:project
                      AND s.group_id=c.group_id AND s.status='SUCCEEDED') AS storyboards,
                  (SELECT count(*) FROM group_current_videos c
                    JOIN authoritative_narration_groups g ON g.id=c.group_id
                    JOIN video_generation_attempts v ON v.attempt_id=c.attempt_id
                    WHERE g.owner_id=:owner AND g.project_id=:project
                      AND NOT g.video_blocked
                      AND v.owner_id=:owner AND v.project_id=:project
                      AND v.group_id=c.group_id
                      AND v.stage='ARCHIVED' AND v.archive_stage='COMPLETE') AS videos,
                  (SELECT count(*) FROM composition_versions
                    WHERE owner_id=:owner AND project_id=:project
                      AND current = true) AS compositions,
                  (SELECT count(*) FROM cover_prompt_generations
                    WHERE owner_id=:owner AND project_id=:project) AS cover_prompts,
                  (SELECT count(*) FROM cover_image_generations
                    WHERE owner_id=:owner AND project_id=:project) AS cover_images,
                  (SELECT count(*) FROM projects
                    WHERE owner_id=:owner AND id=:project
                      AND current_cover_prompt_generation_id IS NOT NULL)
                    AS current_cover_prompt,
                  (SELECT count(*) FROM projects
                    WHERE owner_id=:owner AND id=:project
                      AND current_cover_image_generation_id IS NOT NULL)
                    AS current_cover_image,
                  (SELECT count(*) FROM project_descriptions
                    WHERE owner_id=:owner AND project_id=:project
                      AND current = true) AS current_description
                """
                ),
                {"owner": owner_id, "project": project_id},
            )
            .mappings()
            .one()
        )
        return {key: int(values[key] or 0) for key in values}

    def workflow_attempt_states(
        self, owner_id: UUID, project_id: UUID
    ) -> dict[str, str | None]:
        params = {"owner": owner_id, "project": project_id}
        project_director_attempt = self.session.execute(
            text(
                """
                SELECT status, retry_blocked
                FROM project_director_runs
                WHERE owner_id=:owner AND project_id=:project
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ),
            params,
        ).mappings().one_or_none()
        director_states = self.session.execute(
            text(
                """
                SELECT status FROM (
                  SELECT d.status,
                    row_number() OVER (
                      PARTITION BY d.group_id ORDER BY d.created_at DESC, d.id DESC
                    ) AS latest_in_group
                  FROM director_generation_attempts d
                  JOIN authoritative_narration_groups g ON g.id=d.group_id
                  WHERE d.owner_id=:owner
                    AND g.owner_id=:owner AND g.project_id=:project
                ) attempts
                WHERE latest_in_group=1
                """
            ),
            params,
        ).scalars().all()
        storyboard_states = self.session.execute(
            text(
                """
                SELECT status FROM (
                  SELECT status,
                    row_number() OVER (
                      PARTITION BY group_id ORDER BY version DESC
                    ) AS latest_in_group
                  FROM storyboard_generations
                  WHERE owner_id=:owner AND project_id=:project
                ) attempts
                WHERE latest_in_group=1
                """
            ),
            params,
        ).scalars().all()
        video_states = self.session.execute(
            text(
                """
                SELECT stage FROM (
                  SELECT stage,
                    row_number() OVER (
                      PARTITION BY group_id
                      ORDER BY intent_sequence DESC, updated_at DESC, attempt_id DESC
                    ) AS latest_in_group
                  FROM video_generation_attempts
                  WHERE owner_id=:owner AND project_id=:project
                ) attempts
                WHERE latest_in_group=1
                """
            ),
            params,
        ).scalars().all()
        composition_request = self.session.execute(
            text(
                """
                SELECT o.state, o.workflow_generation, r.inputs_sha256, o.budget_round
                FROM composition_outbox o
                JOIN composition_input_revisions r ON r.id=o.revision_id
                WHERE o.owner_id=:owner AND o.project_id=:project
                  AND r.owner_id=:owner AND r.project_id=:project
                ORDER BY r.created_at DESC, o.id DESC
                LIMIT 1
                """
            ),
            params,
        ).mappings().one_or_none()
        composition: object | None = None
        if composition_request is not None:
            # AUTO 第 2 轮起的合成幂等键带 :rN 后缀（§15.9）；第 1 轮与 MANUAL 不变。
            outbox_round = int(composition_request["budget_round"] or 1)
            scope = f":r{outbox_round}" if outbox_round > 1 else ""
            idempotency_key = (
                f"compose:{owner_id}:{project_id}:{composition_request['inputs_sha256']}{scope}"
            )
            composition_job = self.session.execute(
                text(
                    """
                    SELECT state, workflow_generation
                    FROM composition_jobs
                    WHERE owner_id=:owner AND project_id=:project
                      AND idempotency_key=:idempotency_key
                    LIMIT 1
                    """
                ),
                {**params, "idempotency_key": idempotency_key},
            ).mappings().one_or_none()
            if (
                composition_job is None
                or int(composition_request["workflow_generation"])
                > int(composition_job["workflow_generation"])
            ):
                composition = composition_request["state"]
            else:
                composition = composition_job["state"]
        return {
            "project_director": (
                "FAILED_RETRY_BLOCKED"
                if project_director_attempt is not None
                and str(project_director_attempt["status"]) == "FAILED"
                and bool(project_director_attempt["retry_blocked"])
                else str(project_director_attempt["status"])
                if project_director_attempt is not None
                else None
            ),
            "director": aggregate_workflow_attempt_states(
                "director", (str(value) for value in director_states)
            ),
            "storyboard": aggregate_workflow_attempt_states(
                "storyboard", (str(value) for value in storyboard_states)
            ),
            "video": aggregate_workflow_attempt_states(
                "video", (str(value) for value in video_states)
            ),
            "composition": str(composition) if composition is not None else None,
        }

    def auto_pipeline_run(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, str | None, dict[str, object]] | None:
        row = (
            self.session.execute(
                text(
                    """
                    SELECT state, reason_code, payload
                    FROM auto_pipeline_runs
                    WHERE owner_id=:owner AND project_id=:project
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"owner": owner_id, "project": project_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        payload = row["payload"]
        return (
            str(row["state"]),
            str(row["reason_code"]) if row["reason_code"] is not None else None,
            dict(payload) if isinstance(payload, dict) else {},
        )

    def auto_pipeline_completed_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        value = (
            self.session.execute(
                text(
                    """
                    SELECT updated_at
                    FROM auto_pipeline_runs
                    WHERE owner_id=:owner AND project_id=:project AND state='COMPLETED'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"owner": owner_id, "project": project_id},
            )
            .scalars()
            .first()
        )
        return value

    def latest_composition_dispatched_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        # 排序口径与 workflow_attempt_states 的合成解析一致（最新 revision 优先）。
        return (
            self.session.execute(
                text(
                    """
                    SELECT o.dispatched_at
                    FROM composition_outbox o
                    JOIN composition_input_revisions r ON r.id=o.revision_id
                    WHERE o.owner_id=:owner AND o.project_id=:project
                      AND r.owner_id=:owner AND r.project_id=:project
                    ORDER BY r.created_at DESC, o.id DESC
                    LIMIT 1
                    """
                ),
                {"owner": owner_id, "project": project_id},
            )
            .scalars()
            .first()
        )


    def video_defect_groups(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[VideoDefectGroupFact, ...]:
        rows = (
            self.session.execute(
                text(
                    """
                    SELECT g.sequence AS group_sequence,
                           v.retry_evidence AS retry_evidence,
                           v.probe_evidence_json AS probe_evidence
                    FROM group_current_videos c
                    JOIN authoritative_narration_groups g ON g.id = c.group_id
                    JOIN video_generation_attempts v ON v.attempt_id = c.attempt_id
                    WHERE g.owner_id=:owner AND g.project_id=:project
                      AND v.owner_id=:owner AND v.project_id=:project
                      AND v.group_id=c.group_id
                    ORDER BY g.sequence
                    """
                ),
                {"owner": owner_id, "project": project_id},
            )
            .mappings()
            .all()
        )
        facts: list[VideoDefectGroupFact] = []
        for row in rows:
            evidence = _json_value(row["retry_evidence"])
            if not isinstance(evidence, list) or DELIVER_WITH_DEFECT_MARKER not in evidence:
                continue
            facts.append(
                {
                    "group_sequence": int(row["group_sequence"]),
                    "error_code": VIDEO_CONTENT_BLACK_REGION,
                    "findings": _defect_windows(_json_value(row["probe_evidence"])),
                }
            )
        return tuple(facts)


def _json_value(value: object) -> object:
    """text() 原生查询的 JSON 列：Postgres 驱动返回已解析对象，SQLite 返回字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _defect_windows(probe_evidence: object) -> list[dict[str, float]]:
    """黑屏时段摘要：只保留带数值 start/end 的 finding，原数值精度不变。"""
    if not isinstance(probe_evidence, dict):
        return []
    content_qc = probe_evidence.get("content_qc")
    if not isinstance(content_qc, dict):
        return []
    findings = content_qc.get("findings")
    if not isinstance(findings, list):
        return []
    windows: list[dict[str, float]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        start, end = finding.get("start_second"), finding.get("end_second")
        if isinstance(start, int | float) and isinstance(end, int | float):
            windows.append({"start_second": start, "end_second": end})
    return windows
