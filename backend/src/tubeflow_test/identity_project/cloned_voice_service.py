"""克隆音色管理编排（§23.3/§23.4/§23.5）。

职责：上传音频 → 本地校验 → 源音频入对象存储 → 逐 active 账号克隆并记录映射；
槽位满时先自动清理未被引用的旧克隆音色再重试；删除走三类引用保护守卫。
副作用端口：`IdentityProjectRepository`、`ClonedVoiceSourceStorage`、`ClonedVoiceClonePort`、
`ElevenlabsKeyPoolService`（提供 active key 与其配额缓存）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.elevenlabs.ports import PoolKeyEntry
from tubeflow_test.elevenlabs.service import ElevenlabsKeyPoolService

from .cloned_voices import (
    ClonedVoiceClonePort,
    ClonedVoiceManagementPort,
    ClonedVoiceSourceStorage,
    ClonedVoiceUpload,
    ClonedVoiceUploadResult,
    CloneOutcome,
    cleanup_selection,
    ensure_channel_owned,
    ensure_unique_name,
    normalize_cloned_voice_name,
    normalize_cloned_voice_remark,
    slot_available,
    source_suffix,
)
from .domain import ClonedVoice, ClonedVoiceAccount, ClonedVoiceUsage, cloned_voice_delete_guard
from .ports import ClonedVoiceRemoteCopy, ClonedVoiceRemoteFailure


class ClonedVoiceService:
    def __init__(
        self,
        repository: ClonedVoiceManagementPort,
        storage: ClonedVoiceSourceStorage,
        cloner: ClonedVoiceClonePort,
        pool: ElevenlabsKeyPoolService,
        *,
        clock: Callable[[], datetime] | None = None,
        voice_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._cloner = cloner
        self._pool = pool
        self._clock = clock or (lambda: datetime.now(UTC))
        self._new_id = voice_id_factory or uuid4

    def list_channel_voices(self, owner_id: UUID, channel_id: UUID) -> tuple[ClonedVoice, ...]:
        channel = ensure_channel_owned(
            self._repository.channel(owner_id, channel_id), owner_id, channel_id
        )
        return self._repository.list_cloned_voices(channel.id)

    def get_source(
        self, owner_id: UUID, channel_id: UUID, cloned_voice_id: UUID
    ) -> tuple[ClonedVoice, bytes]:
        channel = ensure_channel_owned(
            self._repository.channel(owner_id, channel_id), owner_id, channel_id
        )
        voice = self._repository.cloned_voice(channel.id, cloned_voice_id)
        if voice is None:
            raise DomainError("not_found", "Resource not found.")
        content = self._storage.get_verified(
            voice.source_object_key, voice.source_sha256, voice.source_size_bytes
        )
        return voice, content

    async def upload(
        self, owner_id: UUID, channel_id: UUID, upload: ClonedVoiceUpload
    ) -> ClonedVoiceUploadResult:
        channel = ensure_channel_owned(
            self._repository.channel(owner_id, channel_id), owner_id, channel_id
        )
        if not upload.content:
            raise DomainError("invalid_cloned_voice_source", "Cloned voice source audio is empty")
        name = normalize_cloned_voice_name(upload.name)
        remark = normalize_cloned_voice_remark(upload.remark)
        ensure_unique_name(name, self._repository.list_cloned_voices(channel.id))
        cloned_voice_id = self._new_id()
        suffix = source_suffix(upload.filename)
        object_key = f"cloned-voices/{channel.id}/{cloned_voice_id}/source{suffix}"
        digest = hashlib.sha256(upload.content).hexdigest()
        media_type = upload.media_type or "application/octet-stream"
        stored = self._storage.put_at_key(object_key, upload.content, media_type, digest)
        voice = ClonedVoice(
            id=cloned_voice_id,
            channel_id=channel.id,
            owner_id=owner_id,
            name=name,
            remark=remark,
            source_object_key=stored.key,
            source_sha256=digest,
            source_size_bytes=len(upload.content),
            source_media_type=media_type,
            created_at=self._clock(),
        )
        self._repository.add_cloned_voice(voice)
        outcomes = await self._clone_into_accounts(voice, upload, suffix)
        return ClonedVoiceUploadResult(voice, outcomes)

    async def delete(self, owner_id: UUID, channel_id: UUID, cloned_voice_id: UUID) -> None:
        channel = ensure_channel_owned(
            self._repository.channel(owner_id, channel_id), owner_id, channel_id
        )
        voice = self._repository.cloned_voice(channel.id, cloned_voice_id)
        if voice is None:
            raise DomainError("not_found", "Resource not found.")
        # §23.5：手动删除与自动清理共用同一守卫（冻结快照/草稿/频道默认三类引用）。
        cloned_voice_delete_guard(self._repository.cloned_voice_usage(voice.id))
        await self._delete_remote_copies(voice)
        self._storage.delete_exact(voice.source_object_key)
        if not self._repository.delete_cloned_voice(channel.id, voice.id):
            raise DomainError("not_found", "Resource not found.")

    async def delete_remote_copies(
        self, copies: tuple[ClonedVoiceRemoteCopy, ...]
    ) -> tuple[ClonedVoiceRemoteFailure, ...]:
        """频道删除专用：只清理 ElevenLabs 远端副本。

        与 `delete` 的差异：不读守卫（频道默认/冻结快照/草稿引用会阻止单音色删除，
        但频道整体删除时这些引用随频道一起消失）、不改本地仓储；单个失败只计数。
        """
        failures: list[ClonedVoiceRemoteFailure] = []
        for copy in copies:
            entry = self._pool.key_entry(copy.pool_key_id)
            if entry is None or not copy.el_voice_id:
                failures.append(ClonedVoiceRemoteFailure(copy.cloned_voice_id, copy.pool_key_id))
                continue
            try:
                await self._cloner.delete_voice(entry.api_key, copy.el_voice_id)
            except DomainError:
                failures.append(ClonedVoiceRemoteFailure(copy.cloned_voice_id, copy.pool_key_id))
        return tuple(failures)

    async def _clone_into_accounts(
        self, voice: ClonedVoice, upload: ClonedVoiceUpload, suffix: str
    ) -> tuple[CloneOutcome, ...]:
        outcomes: list[CloneOutcome] = []
        filename = upload.filename.strip() or f"source{suffix}"
        for entry in self._pool.active_keys():
            outcomes.append(await self._clone_one(voice, upload, filename, entry, tuple(outcomes)))
        return tuple(outcomes)

    async def _clone_one(
        self,
        voice: ClonedVoice,
        upload: ClonedVoiceUpload,
        filename: str,
        entry: PoolKeyEntry,
        freshly_cloned: tuple[CloneOutcome, ...],
    ) -> CloneOutcome:
        if not await self._ensure_slot(entry, freshly_cloned):
            return CloneOutcome(
                entry.id,
                entry.label,
                False,
                error_code="provider_voice_slots_full",
                error_message="ElevenLabs 音色槽位已满且没有可清理的旧克隆音色。",
            )
        try:
            el_voice_id = await self._cloner.clone_voice(
                entry.api_key,
                voice.name,
                audio=upload.content,
                filename=filename,
                description=voice.remark,
            )
        except DomainError as error:
            return CloneOutcome(
                entry.id, entry.label, False, error_code=error.code, error_message=error.message
            )
        try:
            self._repository.save_cloned_voice_account(
                ClonedVoiceAccount(voice.id, entry.id, el_voice_id, self._clock())
            )
        except DomainError:
            # §23.4：跨进程并发补克隆由主键兜底；冲突说明另一进程已写入，重读映射即一致。
            existing = self._repository.cloned_voice_account(voice.id, entry.id)
            if existing is None or not existing.el_voice_id:
                raise
            return CloneOutcome(entry.id, entry.label, True, el_voice_id=existing.el_voice_id)
        return CloneOutcome(entry.id, entry.label, True, el_voice_id=el_voice_id)

    async def _ensure_slot(
        self, entry: PoolKeyEntry, freshly_cloned: tuple[CloneOutcome, ...]
    ) -> bool:
        """槽位自检：未知先刷新（免费订阅接口，不计字符费）；已满则先自动清理腾位（§23.5）。"""
        keep = frozenset(
            item.el_voice_id for item in freshly_cloned if item.ok and item.el_voice_id
        )
        quota = await self._pool.refresh_slot_quota(entry.id)
        if quota is None:
            return False
        availability = slot_available(quota.voice_slots_used, quota.voice_limit)
        if availability is True:
            return True
        if availability is None:
            return False
        needed = max((quota.voice_slots_used or 0) - (quota.voice_limit or 0) + 1, 1)
        if not await self._auto_cleanup(entry, needed=needed, keep_el_voice_ids=keep):
            return False
        refreshed = await self._pool.refresh_slot_quota(entry.id)
        if refreshed is None:
            return False
        return slot_available(refreshed.voice_slots_used, refreshed.voice_limit) is True

    async def _auto_cleanup(
        self,
        entry: PoolKeyEntry,
        *,
        needed: int,
        keep_el_voice_ids: frozenset[str],
    ) -> bool:
        mappings = self._account_mappings(entry.id)
        if not mappings:
            return False
        present = await self._cloner.list_account_voices(entry.api_key)
        selection = cleanup_selection(
            account_voice_ids=present,
            mappings=mappings,
            keep_el_voice_ids=keep_el_voice_ids,
            needed=needed,
        )
        if not selection:
            return False
        for cloned_voice_id, el_voice_id in selection:
            await self._cloner.delete_voice(entry.api_key, el_voice_id)
            self._repository.forget_cloned_voice_account(cloned_voice_id, entry.id)
            self._drop_if_orphan(cloned_voice_id)
        return True

    async def _delete_remote_copies(self, voice: ClonedVoice) -> None:
        """删除该音色在各账号的远端副本（单账号失败不阻断本地删除，如实留待清理）。"""
        for mapping in self._repository.list_cloned_voice_accounts(voice.id):
            entry = self._pool.key_entry(mapping.pool_key_id)
            if entry is None or not mapping.el_voice_id:
                continue
            try:
                await self._cloner.delete_voice(entry.api_key, mapping.el_voice_id)
            except DomainError:
                continue
            self._repository.forget_cloned_voice_account(voice.id, mapping.pool_key_id)

    def _account_mappings(
        self, pool_key_id: UUID
    ) -> tuple[tuple[UUID, str, ClonedVoice, ClonedVoiceUsage], ...]:
        result: list[tuple[UUID, str, ClonedVoice, ClonedVoiceUsage]] = []
        for channel_id in self._repository.list_cloned_voice_channels():
            for voice in self._repository.list_cloned_voices(channel_id):
                mapping = self._repository.cloned_voice_account(voice.id, pool_key_id)
                if mapping is None or not mapping.el_voice_id:
                    continue
                result.append(
                    (
                        voice.id,
                        mapping.el_voice_id,
                        voice,
                        self._repository.cloned_voice_usage(voice.id),
                    )
                )
        return tuple(result)

    def _drop_if_orphan(self, cloned_voice_id: UUID) -> None:
        """清理后孤儿判定：远端映射全无且三类引用全无时，才删除本地行与源音频。"""
        voice = self._repository.cloned_voice_by_id(cloned_voice_id)
        if voice is None:
            return
        if self._repository.list_cloned_voice_accounts(voice.id):
            return
        if self._repository.cloned_voice_usage(voice.id).protected:
            return
        self._storage.delete_exact(voice.source_object_key)
        self._repository.delete_cloned_voice(voice.channel_id, voice.id)
