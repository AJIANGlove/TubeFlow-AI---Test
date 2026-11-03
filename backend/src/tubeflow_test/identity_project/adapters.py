from __future__ import annotations

import hashlib
import secrets
from dataclasses import replace
from datetime import datetime
from threading import Lock
from uuid import UUID

from argon2 import PasswordHasher as Argon2
from argon2.exceptions import VerificationError

from tubeflow_test.domain.errors import DomainError

from .domain import (
    Account,
    AccountRole,
    Channel,
    CharacterImage,
    ClonedVoice,
    ClonedVoiceAccount,
    ClonedVoiceUsage,
    Project,
    ProjectSnapshot,
    VoiceVersion,
)
from .ports import VideoDefectGroupFact, aggregate_workflow_attempt_states


class Argon2PasswordHasher:
    def __init__(self) -> None:
        self._hasher = Argon2()

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except VerificationError:
            return False


class MemoryRepository:
    def __init__(self) -> None:
        self.accounts: dict[UUID, Account] = {}
        self.channels: dict[UUID, Channel] = {}
        self.images: dict[UUID, CharacterImage] = {}
        self.projects: dict[UUID, Project] = {}
        self.cloned_voices: dict[UUID, ClonedVoice] = {}
        self.cloned_voice_accounts: dict[tuple[UUID, UUID], ClonedVoiceAccount] = {}
        self.voices: dict[UUID, VoiceVersion] = {}
        self.sessions: dict[str, tuple[UUID, int]] = {}
        self.voice_media_items: dict[tuple[UUID, str], tuple[str, str, str, int]] = {}
        self.catalog_imports: dict[str, tuple[int, str]] = {}
        self.workflow_group_attempt_states: dict[
            tuple[UUID, UUID, str, UUID], str
        ] = {}
        # 发布标记测试口径：current_compositions 打开发布门禁，publication_events 断言审计。
        self.current_compositions: set[UUID] = set()
        self.publication_events: list[dict[str, object]] = []
        self.initialized = False
        self._lock = Lock()

    def setup_complete(self) -> bool:
        return self.initialized

    def initialize_admin(self, account: Account, completed_at: datetime) -> bool:
        del completed_at
        with self._lock:
            if self.initialized:
                return False
            self.add_account(account)
            self.initialized = True
            return True

    def add_account(self, account: Account) -> None:
        if any(item.username == account.username for item in self.accounts.values()):
            raise ValueError("duplicate username")
        self.accounts[account.id] = account

    def account_by_username(self, username: str) -> Account | None:
        return next((a for a in self.accounts.values() if a.username == username), None)

    def account(self, account_id: UUID) -> Account | None:
        return self.accounts.get(account_id)

    def list_accounts(self) -> tuple[Account, ...]:
        return tuple(sorted(self.accounts.values(), key=lambda item: item.username))

    def active_admin_count(self) -> int:
        return sum(
            1 for item in self.accounts.values() if item.role.value == "admin" and item.active
        )

    def disable_account(self, account_id: UUID) -> bool:
        with self._lock:
            item = self.accounts.get(account_id)
            if item is None:
                return False
            self.accounts[account_id] = replace(
                item, active=False, session_generation=item.session_generation + 1
            )
            return True

    def disable_account_preserving_admin(self, account_id: UUID) -> str:
        with self._lock:
            target = self.accounts.get(account_id)
            if target is None:
                return "not_found"
            if (
                target.role is AccountRole.ADMIN
                and target.active
                and self.active_admin_count() <= 1
            ):
                return "last_admin"
            self.accounts[account_id] = replace(
                target, active=False, session_generation=target.session_generation + 1
            )
            return "disabled"

    def reset_password(self, account_id: UUID, password_hash: str) -> bool:
        with self._lock:
            item = self.accounts.get(account_id)
            if item is None:
                return False
            self.accounts[account_id] = replace(
                item, password_hash=password_hash, session_generation=item.session_generation + 1
            )
            return True

    def logout(self, account_id: UUID, generation: int) -> None:
        with self._lock:
            item = self.accounts.get(account_id)
            if item is not None and item.session_generation == generation:
                self.accounts[account_id] = replace(item, session_generation=generation + 1)

    def create_session(self, token_sha256: str, account_id: UUID, generation: int) -> None:
        self.sessions[token_sha256] = (account_id, generation)

    def session_account(self, token_sha256: str) -> tuple[Account, int] | None:
        found = self.sessions.get(token_sha256)
        if found is None or found[0] not in self.accounts:
            return None
        return self.accounts[found[0]], found[1]

    def revoke_session(self, token_sha256: str) -> None:
        self.sessions.pop(token_sha256, None)

    def add_channel(self, channel: Channel) -> None:
        self.channels[channel.id] = channel

    def list_channels(self, owner_id: UUID) -> tuple[Channel, ...]:
        return tuple(item for item in self.channels.values() if item.owner_id == owner_id)

    def channel(self, owner_id: UUID, channel_id: UUID) -> Channel | None:
        item = self.channels.get(channel_id)
        return item if item and item.owner_id == owner_id else None

    def save_channel(self, channel: Channel) -> None:
        self.channels[channel.id] = channel

    def assert_channel_writable(self, owner_id: UUID, channel_id: UUID) -> None:
        if any(
            project.owner_id == owner_id
            and project.channel_id == channel_id
            and project.production_state == "PURGING"
            for project in self.projects.values()
        ):
            raise DomainError("project_purging", "Project is being permanently deleted")

    def add_image(self, image: CharacterImage, claim: object = None) -> None:
        del claim
        if any(
            item.channel_id == image.channel_id and item.asset.sha256 == image.asset.sha256
            for item in self.images.values()
        ):
            raise ValueError("duplicate image")
        self.images[image.id] = image

    def image(self, owner_id: UUID, channel_id: UUID, image_id: UUID) -> CharacterImage | None:
        item = self.images.get(image_id)
        return (
            item
            if item and item.owner_id == owner_id and item.channel_id in {None, channel_id}
            else None
        )

    def list_images(self, owner_id: UUID, channel_id: UUID) -> tuple[CharacterImage, ...]:
        return tuple(
            item
            for item in self.images.values()
            if item.owner_id == owner_id and item.channel_id in {None, channel_id}
        )

    def add_project(self, project: Project) -> None:
        self.projects[project.id] = project

    def list_projects(self, owner_id: UUID) -> tuple[Project, ...]:
        return tuple(item for item in self.projects.values() if item.owner_id == owner_id)

    def cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> ClonedVoice | None:
        item = self.cloned_voices.get(cloned_voice_id)
        return item if item is not None and item.channel_id == channel_id else None

    def cloned_voice_by_id(self, cloned_voice_id: UUID) -> ClonedVoice | None:
        return self.cloned_voices.get(cloned_voice_id)

    def list_cloned_voices(self, channel_id: UUID) -> tuple[ClonedVoice, ...]:
        return tuple(
            sorted(
                (item for item in self.cloned_voices.values() if item.channel_id == channel_id),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    def add_cloned_voice(self, voice: ClonedVoice) -> None:
        self.cloned_voices[voice.id] = voice

    def delete_cloned_voice(self, channel_id: UUID, cloned_voice_id: UUID) -> bool:
        item = self.cloned_voice(channel_id, cloned_voice_id)
        if item is None:
            return False
        del self.cloned_voices[cloned_voice_id]
        for mapping in tuple(self.cloned_voice_accounts):
            if mapping[0] == cloned_voice_id:
                del self.cloned_voice_accounts[mapping]
        return True

    def cloned_voice_usage(self, cloned_voice_id: UUID) -> ClonedVoiceUsage:
        frozen = sum(
            1
            for item in self.projects.values()
            if item.snapshot is not None and item.snapshot.cloned_voice_id == cloned_voice_id
        )
        drafts = sum(
            1
            for item in self.projects.values()
            if item.cloned_voice_id == cloned_voice_id
            and item.snapshot is None
            and item.production_state == "ACTIVE"
        )
        defaults = sum(
            1
            for item in self.channels.values()
            if item.default_cloned_voice_id == cloned_voice_id
        )
        return ClonedVoiceUsage(frozen, drafts, defaults)

    def cloned_voice_account(
        self, cloned_voice_id: UUID, pool_key_id: UUID
    ) -> ClonedVoiceAccount | None:
        return self.cloned_voice_accounts.get((cloned_voice_id, pool_key_id))

    def list_cloned_voice_accounts(
        self, cloned_voice_id: UUID
    ) -> tuple[ClonedVoiceAccount, ...]:
        return tuple(
            sorted(
                (
                    item
                    for key, item in self.cloned_voice_accounts.items()
                    if key[0] == cloned_voice_id
                ),
                key=lambda item: (item.created_at, str(item.pool_key_id)),
            )
        )

    def save_cloned_voice_account(self, account: ClonedVoiceAccount) -> None:
        self.cloned_voice_accounts[(account.cloned_voice_id, account.pool_key_id)] = account

    def forget_cloned_voice_account(self, cloned_voice_id: UUID, pool_key_id: UUID) -> None:
        self.cloned_voice_accounts.pop((cloned_voice_id, pool_key_id), None)

    def cloned_voice_ids_for_project(self, owner_id: UUID, project_id: UUID) -> tuple[UUID, ...]:
        item = self.project(owner_id, project_id)
        if item is None:
            return ()
        values = [
            value
            for value in (
                item.snapshot.cloned_voice_id if item.snapshot is not None else None,
                item.cloned_voice_id,
            )
            if value is not None
        ]
        return tuple(dict.fromkeys(values))

    def count_active_draft_projects_with_cloned_voice(self, cloned_voice_id: UUID) -> int:
        return self.cloned_voice_usage(cloned_voice_id).draft_references

    def list_cloned_voice_channels(self) -> tuple[UUID, ...]:
        return tuple(dict.fromkeys(item.channel_id for item in self.cloned_voices.values()))

    def workflow_counts(self, owner_id: UUID, project_id: UUID) -> dict[str, int]:
        del owner_id, project_id
        return {
            "sentences": 0,
            "ready_sentences": 0,
            "grouped_sentences": 0,
            "groups": 0,
            "directed": 0,
            "storyboards": 0,
            "videos": 0,
            "blocked": 0,
            "compositions": 0,
            "current_description": 0,
        }

    def workflow_attempt_states(
        self, owner_id: UUID, project_id: UUID
    ) -> dict[str, str | None]:
        return {
            kind: aggregate_workflow_attempt_states(
                kind,
                (
                    status
                    for (attempt_owner, attempt_project, attempt_kind, _group), status in (
                        self.workflow_group_attempt_states.items()
                    )
                    if attempt_owner == owner_id
                    and attempt_project == project_id
                    and attempt_kind == kind
                ),
            )
            for kind in ("director", "storyboard", "video", "composition")
        }

    def auto_pipeline_run(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[str, str | None, dict[str, object]] | None:
        del owner_id, project_id
        return None

    def auto_pipeline_completed_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        del owner_id, project_id
        return None

    def latest_composition_dispatched_at(
        self, owner_id: UUID, project_id: UUID
    ) -> datetime | None:
        del owner_id, project_id
        return None

    def video_defect_groups(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[VideoDefectGroupFact, ...]:
        del owner_id, project_id
        return ()

    def project(self, owner_id: UUID, project_id: UUID) -> Project | None:
        item = self.projects.get(project_id)
        return item if item and item.owner_id == owner_id else None

    def project_for_write(self, owner_id: UUID, project_id: UUID) -> Project | None:
        return self.project(owner_id, project_id)

    def save_project(self, project: Project, expected_revision: int) -> bool:
        with self._lock:
            current = self.projects.get(project.id)
            if (
                current is None
                or current.snapshot is not None
                or current.draft_revision != expected_revision
                or current.production_state != "ACTIVE"
            ):
                return False
            self.projects[project.id] = project
            return True

    def confirm_project(
        self, owner_id: UUID, project_id: UUID, expected_revision: int, snapshot: ProjectSnapshot
    ) -> bool:
        with self._lock:
            project = self.projects.get(project_id)
            if (
                project is None
                or project.owner_id != owner_id
                or project.snapshot is not None
                or project.draft_revision != expected_revision
                or project.production_state != "ACTIVE"
            ):
                return False
            self.projects[project_id] = replace(project, snapshot=snapshot)
            return True

    def project_has_current_composition(self, owner_id: UUID, project_id: UUID) -> bool:
        project = self.project(owner_id, project_id)
        return project is not None and project_id in self.current_compositions

    def set_project_publication(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        published_at: datetime | None,
        actor_account_id: UUID,
        occurred_at: datetime,
    ) -> bool:
        with self._lock:
            project = self.projects.get(project_id)
            if (
                project is None
                or project.owner_id != owner_id
                or project.production_state != "ACTIVE"
            ):
                return False
            if (published_at is not None) == (project.published_at is not None):
                return False
            self.projects[project_id] = replace(project, published_at=published_at)
            self.publication_events.append(
                {
                    "project_id": project_id,
                    "owner_id": owner_id,
                    "actor_account_id": actor_account_id,
                    "action": "PUBLISHED" if published_at is not None else "UNPUBLISHED",
                    "occurred_at": occurred_at,
                }
            )
            return True

    def voice_version(self, voice_version_id: UUID) -> VoiceVersion | None:
        voice = self.voices.get(voice_version_id)
        progress = (
            self.catalog_imports.get(voice.catalog_source_sha256)
            if voice is not None
            else None
        )
        return (
            voice
            if voice is not None
            and voice.enabled
            and progress is not None
            and progress[1] == "completed"
            else None
        )

    def list_voice_versions(self) -> tuple[VoiceVersion, ...]:
        return tuple(
            item
            for item in self.voices.values()
            if item.enabled
            and (progress := self.catalog_imports.get(item.catalog_source_sha256)) is not None
            and progress[1] == "completed"
        )

    def add_voice_version(
        self,
        voice: VoiceVersion,
        media: dict[str, tuple[UUID, str, str, int]],
        claims: object = None,
    ) -> bool:
        del claims
        existing = next(
            (
                item
                for item in self.voices.values()
                if item.voice_id == voice.voice_id and item.version == voice.version
            ),
            None,
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
            return False
        self.voices[voice.id] = voice
        hashes = {
            "reference": voice.reference_zip_sha256,
            "preview": voice.preview_sha256,
            "avatar": voice.avatar_sha256,
        }
        for kind, (_, key, media_type, size) in media.items():
            self.voice_media_items[(voice.id, kind)] = (key, media_type, hashes[kind], size)
        return True

    def lock_voice_version_import(self, voice_id: str, version: int) -> None:
        del voice_id, version

    def lock_voice_catalog_import(self, source_sha256: str) -> None:
        del source_sha256

    def voice_media(self, voice_version_id: UUID, kind: str) -> tuple[str, str, str, int] | None:
        if self.voice_version(voice_version_id) is None:
            return None
        return self.voice_media_items.get((voice_version_id, kind))

    def catalog_import_progress(self, source_sha256: str) -> tuple[int, str] | None:
        return self.catalog_imports.get(source_sha256)

    def save_catalog_import(
        self,
        source_sha256: str,
        catalog_version: int,
        expected_count: int,
        imported_count: int,
        status: str,
        updated_at: datetime,
    ) -> None:
        del catalog_version, expected_count, updated_at
        self.catalog_imports[source_sha256] = (imported_count, status)

    def publish_voice_catalog(
        self,
        source_sha256: str,
        catalog_version: int,
        expected_count: int,
        updated_at: datetime,
    ) -> None:
        del catalog_version, updated_at
        progress = self.catalog_imports.get(source_sha256)
        rows = tuple(
            voice
            for voice in self.voices.values()
            if voice.catalog_source_sha256 == source_sha256
        )
        if progress is None or progress[0] != expected_count or len(rows) != expected_count:
            raise DomainError(
                "voice_catalog_publish_incomplete",
                "Voice catalog is incomplete and cannot be published",
            )
        if progress[1] == "completed":
            if not all(voice.enabled for voice in rows):
                raise DomainError(
                    "voice_catalog_publish_incomplete",
                    "Published voice catalog is internally inconsistent",
                )
            return
        if progress[1] != "importing" or any(voice.enabled for voice in rows):
            raise DomainError(
                "voice_catalog_publish_incomplete",
                "Voice catalog is internally inconsistent and cannot be published",
            )
        for voice in rows:
            self.voices[voice.id] = replace(voice, enabled=True)
        self.catalog_imports[source_sha256] = (expected_count, "completed")

    def attach_staged_objects(self, claims: tuple[object, ...], reference_type: str) -> None:
        del claims, reference_type


class OpaqueSessionTokens:
    def issue(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        return token, hashlib.sha256(token.encode()).hexdigest()
