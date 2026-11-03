from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from tubeflow_test.auto_pipeline.timing import phase_timings
from tubeflow_test.domain.common import AspectRatio, MediaAssetRef, SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import Language, validate_script_language

from .domain import (
    Account,
    AccountRole,
    Channel,
    CharacterImage,
    Project,
    ProjectSnapshot,
    SnapshotCharacter,
    TtsProvider,
    VoiceVersion,
    channel_default_voice_gate,
    normalized_channel_default_visual_style,
    normalized_tts_provider,
)
from .images import verify_character_image
from .ports import (
    WORKFLOW_FAILED_ATTEMPT_STATES,
    WORKFLOW_RUNNING_ATTEMPT_STATES,
    WORKFLOW_SUCCESS_ATTEMPT_STATES,
    IdentityProjectRepository,
    OpaqueTokenPort,
    PasswordHasher,
    PrivateImageStorage,
)

_WORKFLOW_RECONCILABLE_ATTEMPTS = {"storyboard", "video"}
_WORKFLOW_TERMINAL_RETRY_ACTIONS = {
    "director": "generate_director_plan",
    "storyboard": "generate_storyboard",
    "video": "generate_group_video",
    "composition": "RETRY_COMPOSITION",
}

# §15.9：AUTO 一键生产 RUNNING 期间关闭人工阶段动作，避免与自动链竞争（含单组绕过）。
RESUME_AUTO_PIPELINE_ACTION = "RESUME_AUTO_PIPELINE"
_AUTO_PIPELINE_MANUAL_ACTIONS = frozenset(
    {
        "generate_storyboard",
        "generate_group_video",
        "generate_group_videos_batch",
        "compose",
        "RETRY_COMPOSITION",
        "generate_director_plan",
        "RUN_DIRECTOR_PREPLAN",
        "REGENERATE_DIRECTOR_PREPLAN",
        "EDIT_FINAL_DIRECTOR",
        "RETRY",
    }
)


def _strip_auto_pipeline_manual_actions(actions: object) -> list[str]:
    if not isinstance(actions, list):
        return []
    return [
        str(action)
        for action in actions
        if str(action) not in _AUTO_PIPELINE_MANUAL_ACTIONS
    ]


def _workflow_attempt_gate(
    kind: str, status: str | None, *, allow_new_attempt: bool = False
) -> tuple[str, tuple[str, ...]] | None:
    if status is None or status in WORKFLOW_SUCCESS_ATTEMPT_STATES[kind]:
        return None
    if status == "REMOTE_STATE_UNKNOWN":
        actions = (
            ("RECONCILE", "REFRESH")
            if kind in _WORKFLOW_RECONCILABLE_ATTEMPTS
            else ("REFRESH",)
        )
        return f"{kind}_attempt_remote_state_unknown", actions
    if status == "CANCELLED":
        return f"{kind}_attempt_cancelled", ("REFRESH",)
    if status in WORKFLOW_FAILED_ATTEMPT_STATES[kind]:
        retry = _WORKFLOW_TERMINAL_RETRY_ACTIONS.get(kind) if allow_new_attempt else None
        actions = (retry, "REFRESH") if retry is not None else ("REFRESH",)
        return f"{kind}_attempt_failed", actions
    if kind == "video" and status == "SUCCEEDED":
        return "video_attempt_running", ("RECONCILE", "REFRESH")
    if status in WORKFLOW_RUNNING_ATTEMPT_STATES[kind]:
        return f"{kind}_attempt_running", ("REFRESH",)
    return f"{kind}_attempt_status_unknown", ("REFRESH",)


def _workflow_preplan_gate(
    status: str | None, *, has_revision: bool
) -> tuple[str, tuple[str, ...]]:
    if status is None:
        return "director_preplan_not_started", ("RUN_DIRECTOR_PREPLAN",)
    if status == "INTENT_PERSISTED":
        return "director_preplan_queued", ("REFRESH",)
    if status == "RUNNING":
        return "director_preplan_running", ("REFRESH",)
    if status == "REMOTE_STATE_UNKNOWN":
        return "director_preplan_remote_state_unknown", ("REGENERATE_DIRECTOR_PREPLAN", "REFRESH")
    if status == "FAILED_RETRY_BLOCKED":
        return "director_preplan_failed", ()
    if status == "FAILED":
        return "director_preplan_failed", ("RETRY",)
    if status == "SUCCEEDED" and has_revision:
        return (
            "narration_not_ready_for_final_mapping",
            ("VIEW_CURRENT", "REGENERATE_DIRECTOR_PREPLAN"),
        )
    return "director_preplan_authority_inconsistent", ("REFRESH",)


def _workflow_final_director_gate(
    status: str | None,
    *,
    has_revision: bool,
    has_materialization: bool,
) -> tuple[str, tuple[str, ...]]:
    if has_materialization:
        actions = ["EDIT_FINAL_DIRECTOR"]
        if status == "FAILED":
            actions.append("RETRY")
        elif status == "SUCCEEDED":
            actions.append("REGENERATE_DIRECTOR_PREPLAN")
        elif status == "REMOTE_STATE_UNKNOWN":
            actions.extend(("REGENERATE_DIRECTOR_PREPLAN", "REFRESH"))
        return "final_director_review_required", tuple(actions)
    if status == "SUCCEEDED" and has_revision:
        return "director_materialization_pending", ("REFRESH",)
    return _workflow_preplan_gate(status, has_revision=has_revision)


def _workflow_preplan_background_actions(status: str | None) -> tuple[str, ...]:
    if status in {"INTENT_PERSISTED", "RUNNING"}:
        return ("REFRESH",)
    if status == "REMOTE_STATE_UNKNOWN":
        return ("REGENERATE_DIRECTOR_PREPLAN", "REFRESH")
    if status == "FAILED":
        return ("RETRY",)
    return ()


def _merge_workflow_actions(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(action for group in groups for action in group))


def _workflow_authority_inconsistency(
    counts: dict[str, int], groups: int
) -> tuple[str, str] | None:
    if counts["directed"] > groups:
        return "DIRECTING", "director_authority_inconsistent"
    if counts["storyboards"] > groups:
        return "STORYBOARD", "storyboard_authority_inconsistent"
    if counts["videos"] > groups:
        return "VIDEO", "video_authority_inconsistent"
    return None


def _workflow_stage_facts(
    project: Project,
    counts: dict[str, int],
    attempts: dict[str, str | None],
) -> tuple[str, str | None, tuple[str, ...], bool]:
    groups = counts["groups"]
    unblocked_groups = groups - counts["blocked"]
    narration_ready = (
        project.snapshot is not None
        and counts["sentences"] > 0
        and counts["ready_sentences"] == counts["sentences"]
        and counts["grouped_sentences"] == counts["sentences"]
        and groups > 0
    )
    stage: str
    reason: str | None
    actions: tuple[str, ...]
    if project.production_state == "PURGING":
        stage, reason, actions = "DELETING", "project_purging", ()
    elif project.snapshot is None:
        stage, reason, actions = "DRAFT", None, ("edit_project", "confirm_narration")
    elif not narration_ready:
        stage, reason = "NARRATION", "narration_not_ready"
        _preplan_reason, actions = _workflow_preplan_gate(
            attempts.get("project_director"),
            has_revision=counts.get("director_preplan_revisions", 0) > 0,
        )
    elif (
        inconsistency := _workflow_authority_inconsistency(counts, groups)
    ) is not None:
        stage, reason = inconsistency
        actions = ("REFRESH",)
    elif counts.get("director_ready", counts["directed"]) < unblocked_groups:
        director_gate = _workflow_final_director_gate(
            attempts.get("project_director"),
            has_revision=counts.get("director_preplan_revisions", 0) > 0,
            has_materialization=counts.get("director_materializations", 0) > 0,
        )
        reason, actions = director_gate
        stage = "DIRECTING"
    elif counts["storyboards"] < unblocked_groups:
        storyboard_attempt_gate = _workflow_attempt_gate(
            "storyboard", attempts["storyboard"], allow_new_attempt=True
        )
        reason, actions = (
            storyboard_attempt_gate
            if storyboard_attempt_gate is not None
            else ("storyboards_incomplete", ("generate_storyboard",))
        )
        stage = "STORYBOARD"
    elif counts["videos"] < unblocked_groups:
        storyboard_gate = _workflow_attempt_gate(
            "storyboard", attempts["storyboard"], allow_new_attempt=True
        )
        video_attempt_gate = _workflow_attempt_gate(
            "video", attempts["video"], allow_new_attempt=True
        )
        reason, actions = (
            video_attempt_gate
            if video_attempt_gate is not None
            else ("videos_incomplete", ("generate_group_video", "generate_group_videos_batch"))
        )
        actions = _merge_workflow_actions(
            actions,
            storyboard_gate[1] if storyboard_gate is not None else (),
        )
        stage = "VIDEO"
    elif unblocked_groups == 0:
        stage, reason, actions = "VIDEO", "all_groups_video_blocked", ()
    else:
        storyboard_gate = _workflow_attempt_gate(
            "storyboard", attempts["storyboard"], allow_new_attempt=True
        )
        video_gate = _workflow_attempt_gate(
            "video", attempts["video"], allow_new_attempt=True
        )
        composition_gate = _workflow_attempt_gate(
            "composition", attempts["composition"], allow_new_attempt=True
        )
        recovery_actions = _merge_workflow_actions(
            storyboard_gate[1] if storyboard_gate is not None else (),
            video_gate[1] if video_gate is not None else (),
        )
        if composition_gate is None:
            reason = None
            actions = (
                ("compose", "export")
                if counts["compositions"] > 0
                else ("compose",)
            )
        else:
            reason, actions = composition_gate
        if counts["compositions"] > 0:
            actions = ("export", *actions)
        actions = _merge_workflow_actions(actions, recovery_actions)
        stage = "VIDEO_READY"
    if (
        narration_ready
        and counts.get("director_ready", counts["directed"]) == unblocked_groups
    ):
        actions = _merge_workflow_actions(
            actions,
            _workflow_preplan_background_actions(
                attempts.get("project_director")
            ),
        )
    return stage, reason, actions, narration_ready


def _workflow_attempt_attention(attempts: dict[str, str | None]) -> bool:
    if attempts.get("project_director") in {"FAILED", "FAILED_RETRY_BLOCKED"}:
        return True
    return any(
        attempts.get(kind) in WORKFLOW_FAILED_ATTEMPT_STATES[kind]
        for kind in ("director", "storyboard", "video", "composition")
    )


def _cover_workflow_stage(project: Project, counts: dict[str, int]) -> dict[str, object]:
    """封面阶段事实（§9 追加式触点）：completed = 存在当前封面图版本。

    封面是可选增强：永不构成 blocking_reason，不参与其他阶段门禁、列表徽章、
    "视频生产完成"判定与 AUTO 运行；仅项目冻结（存在不可变快照）后开放。
    """
    stage: dict[str, object] = {
        "key": "cover",
        "label": "封面",
        "status": "blocked",
        "blocking_reason": None,
        "allowed_actions": [],
        "counts": {
            "cover_prompts": counts.get("cover_prompts", 0),
            "cover_images": counts.get("cover_images", 0),
            "current_cover_prompt": counts.get("current_cover_prompt", 0),
            "current_cover_image": counts.get("current_cover_image", 0),
        },
    }
    if project.production_state == "PURGING":
        stage["blocking_reason"] = "project_purging"
        return stage
    if project.snapshot is None:
        stage["blocking_reason"] = "narration_not_confirmed"
        return stage
    stage["status"] = (
        "complete" if counts.get("current_cover_image", 0) > 0 else "current"
    )
    if _workflow_authority_inconsistency(counts, counts["groups"]) is None:
        stage["allowed_actions"] = ["generate_cover_prompt", "generate_cover_image"]
    return stage


def _description_workflow_stage(project: Project, counts: dict[str, int]) -> dict[str, object]:
    """简介阶段事实（§36 追加式可选后处理）：不阻塞主流程、列表徽章或 AUTO。"""
    stage: dict[str, object] = {
        "key": "description",
        "label": "简介",
        "status": "blocked",
        "blocking_reason": None,
        "allowed_actions": [],
        "counts": {
            "current_descriptions": counts.get("current_description", 0),
            "current_compositions": counts.get("compositions", 0),
        },
    }
    if project.production_state == "PURGING":
        stage["blocking_reason"] = "project_purging"
        return stage
    if counts.get("compositions", 0) <= 0:
        stage["blocking_reason"] = "description_composition_required"
        return stage
    stage["status"] = (
        "complete" if counts.get("current_description", 0) > 0 else "current"
    )
    stage["allowed_actions"] = ["generate_description"]
    return stage


class IdentityProjectService:
    def __init__(
        self,
        repo: IdentityProjectRepository,
        passwords: PasswordHasher,
        *,
        setup_username: str | None = None,
        setup_password: str | None = None,
        image_storage: PrivateImageStorage | None = None,
        opaque_tokens: OpaqueTokenPort | None = None,
    ) -> None:
        self.repo = repo
        self.passwords = passwords
        self.setup_username = setup_username
        self.setup_password = setup_password
        self.image_storage = image_storage
        self.opaque_tokens = opaque_tokens

    def setup_complete(self) -> bool:
        return self.repo.setup_complete()

    def initialize_admin(self, username: str, password: str) -> Account:
        configured = self.setup_username is not None and self.setup_password is not None
        username_ok = hmac.compare_digest(username.encode(), (self.setup_username or "").encode())
        password_ok = hmac.compare_digest(password.encode(), (self.setup_password or "").encode())
        if not configured or not (username_ok and password_ok):
            raise DomainError(
                "invalid_initialization_credentials", "Initialization credentials are invalid"
            )
        account = self._new_account(username, password, AccountRole.ADMIN, check_existing=False)
        if not self.repo.initialize_admin(account, datetime.now(UTC)):
            raise DomainError(
                "admin_already_initialized", "Administrator initialization is already complete"
            )
        return account

    def login(self, username: str, password: str) -> str:
        account = self.repo.account_by_username(username.strip())
        valid = account is not None and self.passwords.verify(account.password_hash, password)
        if not valid or account is None or not account.active:
            raise DomainError("invalid_credentials", "Invalid username or password")
        if self.opaque_tokens is None:
            from .adapters import OpaqueSessionTokens

            self.opaque_tokens = OpaqueSessionTokens()
        token, token_sha256 = self.opaque_tokens.issue()
        self.repo.create_session(token_sha256, account.id, account.session_generation)
        return token

    def authenticate(self, token: str) -> Account:
        if not token or len(token) > 256:
            raise DomainError("invalid_session", "Session is invalid")
        found = self.repo.session_account(self._token_hash(token))
        if found is None:
            raise DomainError("invalid_session", "Session is invalid")
        account, generation = found
        if not account.active or generation != account.session_generation:
            raise DomainError("invalid_session", "Session is invalid")
        return account

    def logout(self, actor: Account, token: str | None = None) -> None:
        if token:
            self.repo.revoke_session(self._token_hash(token))
        self.repo.logout(actor.id, actor.session_generation)

    def list_accounts(self, actor: Account) -> tuple[Account, ...]:
        self._require_admin(actor)
        return self.repo.list_accounts()

    def create_account(self, actor: Account, username: str, password: str) -> Account:
        self._require_admin(actor)
        account = self._new_account(username, password, AccountRole.USER)
        try:
            self.repo.add_account(account)
        except (ValueError, DomainError) as exc:
            raise DomainError("invalid_username", "Username is unavailable") from exc
        return account

    def disable_account(self, actor: Account, account_id: UUID) -> None:
        self._require_admin(actor)
        target = self.repo.account(account_id)
        if target is None:
            raise DomainError("not_found", "Resource not found")
        if actor.id == account_id:
            raise DomainError(
                "admin_lockout_forbidden", "The active administrator cannot be disabled"
            )
        result = self.repo.disable_account_preserving_admin(account_id)
        if result == "last_admin":
            raise DomainError(
                "admin_lockout_forbidden", "The active administrator cannot be disabled"
            )
        if result == "not_found":
            raise DomainError("not_found", "Resource not found")

    def reset_password(self, actor: Account, account_id: UUID, password: str) -> None:
        self._require_admin(actor)
        self._validate_password(password)
        if not self.repo.reset_password(account_id, self.passwords.hash(password)):
            raise DomainError("not_found", "Resource not found")

    def create_channel(self, owner: Account, url: str) -> Channel:
        channel = Channel(uuid4(), owner.id, self._channel_url(url))
        self.repo.add_channel(channel)
        return channel

    def list_channels(self, owner: Account) -> tuple[Channel, ...]:
        return self.repo.list_channels(owner.id)

    def get_channel(self, owner: Account, channel_id: UUID) -> Channel:
        channel = self.repo.channel(owner.id, channel_id)
        if channel is None:
            raise DomainError("not_found", "Resource not found")
        return channel

    def update_channel(
        self,
        owner: Account,
        channel_id: UUID,
        url: str | None = None,
        *,
        default_voice_version_id: UUID | None = None,
        default_character_image_ids: list[UUID] | None = None,
        name: str | None = None,
        default_tts_provider: str | None = None,
        default_cloned_voice_id: UUID | None = None,
        default_visual_style: str | None = None,
    ) -> Channel:
        self.repo.assert_channel_writable(owner.id, channel_id)
        channel = self.get_channel(owner, channel_id)
        updated_url = channel.url
        updated_default_voice_version_id = channel.default_voice_version_id
        updated_default_character_image_ids = channel.default_character_image_ids
        updated_name = channel.name
        updated_provider = channel.default_tts_provider
        updated_default_cloned_voice_id = channel.default_cloned_voice_id
        updated_default_visual_style = channel.default_visual_style
        has_change = False
        if url is not None:
            updated_url = self._channel_url(url)
            has_change = True
        if default_voice_version_id is not None:
            voice = self.repo.voice_version(default_voice_version_id)
            if voice is None or not voice.enabled:
                raise DomainError(
                    "invalid_channel_default_voice",
                    "Channel default voice is unavailable",
                )
            updated_default_voice_version_id = default_voice_version_id
            has_change = True
        if default_tts_provider is not None:
            updated_provider = normalized_tts_provider(default_tts_provider)
            has_change = True
        if default_cloned_voice_id is not None:
            cloned = self.repo.cloned_voice(channel_id, default_cloned_voice_id)
            if cloned is None:
                raise DomainError(
                    "invalid_channel_default_cloned_voice",
                    "Channel default cloned voice is unavailable",
                )
            updated_default_cloned_voice_id = default_cloned_voice_id
            has_change = True
        if default_character_image_ids is not None:
            deduped = tuple(dict.fromkeys(default_character_image_ids))
            available = {image.id for image in self.repo.list_images(owner.id, channel_id)}
            if (
                len(deduped) > 3
                or len(deduped) != len(default_character_image_ids)
                or any(image_id not in available for image_id in deduped)
            ):
                raise DomainError(
                    "invalid_channel_default_character_images",
                    "Channel default character images are unavailable",
                )
            updated_default_character_image_ids = deduped
            has_change = True
        # 空串=清空（回到"未设置"）；None=本次不改。
        if default_visual_style is not None:
            updated_default_visual_style = normalized_channel_default_visual_style(
                default_visual_style
            )
            has_change = True
        if name is not None:
            updated_name = self._channel_name(name)
            has_change = True
        if not has_change:
            raise DomainError("invalid_channel_update", "Channel update has no fields")
        if default_tts_provider is not None or default_cloned_voice_id is not None:
            # §23.2：改选默认配音源时必须同时具备该源下的默认音色（对齐 §3.13 门禁）。
            channel_default_voice_gate(
                updated_provider,
                updated_default_voice_version_id,
                updated_default_cloned_voice_id,
            )
        updated = replace(
            channel,
            url=updated_url,
            default_voice_version_id=updated_default_voice_version_id,
            default_character_image_ids=updated_default_character_image_ids,
            name=updated_name,
            default_tts_provider=updated_provider,
            default_cloned_voice_id=updated_default_cloned_voice_id,
            default_visual_style=updated_default_visual_style,
        )
        self.repo.save_channel(updated)
        return updated

    def upload_image(self, owner: Account, channel_id: UUID, content: bytes) -> CharacterImage:
        if self.image_storage is None:
            raise DomainError("storage_unavailable", "Image storage is unavailable")
        verified = verify_character_image(content)
        self.get_channel(owner, channel_id)
        if any(
            item.asset.sha256 == verified.sha256
            for item in self.repo.list_images(owner.id, channel_id)
        ):
            raise DomainError("duplicate_character_image", "Character image already exists")
        stored = self.image_storage.put(verified.content, verified.media_type, owner_id=owner.id)
        key = stored.key
        image = CharacterImage(
            uuid4(),
            owner.id,
            MediaAssetRef(uuid4(), verified.sha256, verified.media_type, len(content)),
            verified.width,
            verified.height,
            key,
            channel_id,
        )
        # The staged journal is the cleanup authority.  A repository failure can
        # include an uncertain commit response, so the writer must not delete bytes
        # that may already have an authoritative reference.
        self.repo.add_image(image, stored.claim)
        return image

    def get_image(
        self, owner: Account, channel_id: UUID, image_id: UUID
    ) -> tuple[CharacterImage, bytes]:
        image = self.repo.image(owner.id, channel_id, image_id)
        if image is None or self.image_storage is None:
            raise DomainError("not_found", "Resource not found")
        return image, self.image_storage.get_verified(
            image.object_key, image.asset.sha256, image.asset.size_bytes
        )

    def list_images(self, owner: Account, channel_id: UUID) -> tuple[CharacterImage, ...]:
        self.get_channel(owner, channel_id)
        return self.repo.list_images(owner.id, channel_id)

    def list_voice_versions(self, owner: Account) -> tuple[VoiceVersion, ...]:
        del owner
        return self.repo.list_voice_versions()

    def get_voice_media(
        self, owner: Account, voice_version_id: UUID, kind: str
    ) -> tuple[bytes, str]:
        del owner
        voice = self.repo.voice_version(voice_version_id)
        media = self.repo.voice_media(voice_version_id, kind)
        if voice is None or not voice.enabled or media is None or self.image_storage is None:
            raise DomainError("not_found", "Resource not found")
        return self.image_storage.get_verified(media[0], media[2], media[3]), media[1]

    def create_project(
        self,
        owner: Account,
        *,
        channel_id: UUID,
        script: str,
        image_ids: tuple[UUID, ...],
        visual_style: str | None = None,
        voice_version_id: UUID | None = None,
        locale: SupportedLocale,
        aspect_ratio: AspectRatio,
        expected_duration_seconds: int | None = None,
        title: str = "Untitled project",
        tts_provider: str | None = None,
        cloned_voice_id: UUID | None = None,
    ) -> Project:
        if aspect_ratio is not AspectRatio.LANDSCAPE:
            raise DomainError(
                "aspect_ratio_not_supported", "Only landscape 16:9 projects are supported"
            )
        channel = self.get_channel(owner, channel_id)
        provider = normalized_tts_provider(tts_provider or channel.default_tts_provider)
        # §3.13/§23.2：频道未设置默认音色时不能创建项目——显式给出音色也不能绕过。
        if provider == TtsProvider.COSYVOICE.value:
            if channel.default_voice_version_id is None:
                raise DomainError(
                    "channel_default_voice_required",
                    "Set a channel default voice before creating a project",
                )
            selected_voice_version_id = voice_version_id or channel.default_voice_version_id
            selected_cloned_voice_id = None
        else:
            if channel.default_cloned_voice_id is None:
                # §23.2：EL 源且频道无可用克隆音色时不能创建项目（同码阻断）。
                raise DomainError(
                    "channel_default_voice_required",
                    "Set a channel default cloned voice before creating a project",
                )
            selected_cloned_voice_id = cloned_voice_id or channel.default_cloned_voice_id
            selected_voice_version_id = None
        # 频道默认画面风格：入参为空时回落频道默认；两者都为空则拒绝（避免空风格流到下游才报错）。
        selected_visual_style = (visual_style or "").strip() or channel.default_visual_style
        if not selected_visual_style:
            raise DomainError(
                "visual_style_required",
                "Set a visual style or a channel default visual style before creating a project",
            )
        self._validate_project_refs(
            owner,
            channel_id,
            image_ids,
            selected_voice_version_id,
            locale,
            cloned_voice_id=selected_cloned_voice_id,
        )
        project = Project(
            uuid4(),
            owner.id,
            channel_id,
            script,
            image_ids,
            selected_visual_style,
            locale,
            aspect_ratio,
            expected_duration_seconds,
            title=title.strip(),
            production_mode="AUTO",
            tts_provider=provider,
            voice_version_id=selected_voice_version_id,
            cloned_voice_id=selected_cloned_voice_id,
        )
        self.repo.add_project(project)
        return project

    def get_project(self, owner: Account, project_id: UUID) -> Project:
        project = self.repo.project(owner.id, project_id)
        if project is None:
            raise DomainError("not_found", "Resource not found")
        return project

    def list_projects(self, owner: Account) -> tuple[Project, ...]:
        return self.repo.list_projects(owner.id)

    def workflow_summary(
        self, owner: Account, project_id: UUID, *, now: datetime | None = None
    ) -> dict[str, object]:
        project = self.get_project(owner, project_id)
        counts = self.repo.workflow_counts(owner.id, project_id)
        attempts = self.repo.workflow_attempt_states(owner.id, project_id)
        stage, reason, actions, narration_ready = _workflow_stage_facts(project, counts, attempts)
        summary: dict[str, object] = {
            "stage": stage,
            "blocking_reason": reason,
            "allowed_actions": list(actions),
            "counts": {
                "sentences": counts["sentences"],
                "groups": counts["groups"],
                "videos": counts["videos"],
            },
            "stages": self._workflow_stages(project, counts, narration_ready, attempts),
        }
        return self._with_auto_pipeline(project, summary, now=now or datetime.now(UTC))

    def _with_auto_pipeline(
        self, project: Project, summary: dict[str, object], *, now: datetime
    ) -> dict[str, object]:
        """§15：AUTO 项目暴露一键生产运行状态与暂停/恢复闸门；MANUAL 逐字节不变。"""
        if project.production_mode != "AUTO":
            return summary
        run = self.repo.auto_pipeline_run(project.owner_id, project.id)
        state, reason_code, payload = run if run is not None else (None, None, {})
        summary["auto_pipeline"] = {
            "state": state,
            "reason_code": reason_code,
            "phase": payload.get("phase"),
            # 带病交付缺陷分组（2026-10-16 决策）：无缺陷时为空数组，键始终存在。
            "defect_groups": [
                dict(item)
                for item in self.repo.video_defect_groups(project.owner_id, project.id)
            ],
        }
        # 阶段用时只在 payload 有计时数据时出现；历史运行保持原响应形状（无 timing）。
        timing = phase_timings(payload, state, now)
        if timing is not None:
            cast(dict[str, object], summary["auto_pipeline"])["timing"] = timing
        if state == "PAUSED":
            summary["blocking_reason"] = reason_code
            # §20 暂停自描述：向页面暴露可执行细节白名单；内部书签（视频 job、
            # 导演失败 run 等）与 phase 不暴露。
            details = {
                key: payload[key]
                for key in (
                    "group_sequences",
                    "failure_code",
                    "error_code",
                    "sequences",
                    "failure_messages",
                )
                if key in payload
            }
            if details:
                cast(dict[str, object], summary["auto_pipeline"])["details"] = details
            # PAUSED 保留底层阶段动作，仅追加恢复入口。
            kept = [str(action) for action in cast(list[object], summary["allowed_actions"])]
            if RESUME_AUTO_PIPELINE_ACTION not in kept:
                kept.append(RESUME_AUTO_PIPELINE_ACTION)
            summary["allowed_actions"] = kept
        elif state == "RUNNING":
            summary["allowed_actions"] = _strip_auto_pipeline_manual_actions(
                summary["allowed_actions"]
            )
            stages = summary.get("stages")
            if isinstance(stages, list):
                for entry in stages:
                    if isinstance(entry, dict) and "allowed_actions" in entry:
                        entry["allowed_actions"] = _strip_auto_pipeline_manual_actions(
                            entry["allowed_actions"]
                        )
        elif state == "COMPLETED":
            # §17.2：收口完成前的残留合成失败提示；完成后新派发的合成失败
            # （dispatched_at 晚于完成时间）保留如实展示。
            composition_stage: dict[str, object] | None = None
            stages = summary.get("stages")
            if isinstance(stages, list):
                for entry in stages:
                    if isinstance(entry, dict) and entry.get("key") == "composition_export":
                        composition_stage = entry
                        break
            if (
                composition_stage is not None
                and composition_stage.get("blocking_reason") == "composition_attempt_failed"
                and not self._auto_late_composition_failure(project)
            ):
                composition_stage["blocking_reason"] = None
                if summary.get("blocking_reason") == "composition_attempt_failed":
                    summary["blocking_reason"] = None
        return summary

    def _auto_late_composition_failure(self, project: Project) -> bool:
        """COMPLETED 精确版豁免：最新合成行在完成时间之后仍有新派发（完成后的新尝试）。"""
        completed_at = self.repo.auto_pipeline_completed_at(project.owner_id, project.id)
        if completed_at is None:
            return False
        dispatched_at = self.repo.latest_composition_dispatched_at(
            project.owner_id, project.id
        )
        return dispatched_at is not None and dispatched_at > completed_at

    def project_flow(self, owner: Account, project: Project) -> dict[str, object]:
        counts = self.repo.workflow_counts(owner.id, project.id)
        attempts = self.repo.workflow_attempt_states(owner.id, project.id)
        stage, reason, _actions, _narration_ready = _workflow_stage_facts(
            project, counts, attempts
        )
        attention = _workflow_attempt_attention(attempts)
        if project.production_mode == "AUTO":
            auto_run = self.repo.auto_pipeline_run(owner.id, project.id)
            if auto_run is not None and auto_run[0] == "PAUSED":
                # 列表「需要恢复」徽章同步覆盖：blocking_reason 置暂停稳定码。
                reason = auto_run[1]
                attention = True
            elif auto_run is not None and auto_run[0] == "COMPLETED":
                # §17.2：COMPLETED 为权威终态，完成前的残留失败不再驱动「需要恢复」；
                # 完成后新派发的合成失败（dispatched_at 晚于完成时间）仍如实提示。
                if not (
                    attempts.get("composition") == "FAILED"
                    and self._auto_late_composition_failure(project)
                ):
                    attention = False
                    if reason == "composition_attempt_failed":
                        reason = None
        return {
            "stage": stage,
            "blocking_reason": reason,
            "attention": attention,
            "sentences": counts["sentences"],
            "ready_sentences": counts["ready_sentences"],
            "groups": counts["groups"],
            "videos": counts["videos"],
            "compositions": counts["compositions"],
        }

    @staticmethod
    def _workflow_stages(
        project: Project,
        counts: dict[str, int],
        narration_ready: bool,
        attempts: dict[str, str | None],
    ) -> list[dict[str, object]]:
        stages = IdentityProjectService._workflow_stages_core(
            project, counts, narration_ready, attempts
        )
        stages.append(_cover_workflow_stage(project, counts))
        stages.append(_description_workflow_stage(project, counts))
        return stages

    @staticmethod
    def _workflow_stages_core(
        project: Project,
        counts: dict[str, int],
        narration_ready: bool,
        attempts: dict[str, str | None],
    ) -> list[dict[str, object]]:
        stage_facts = (
            ("project_configuration", "项目配置", {"projects": 1}),
            ("script", "文案", {"scripts": 1}),
            (
                "character_ip",
                "角色 IP",
                {"character_images": len(project.character_image_ids)},
            ),
            ("visual_style", "画面风格", {"visual_styles": 1}),
            (
                "narration_settings",
                "配音设置",
                {"project_snapshots": int(project.snapshot is not None)},
            ),
            (
                "narration_timeline",
                "配音与时间线",
                {
                    "sentences": counts["sentences"],
                    "ready_sentences": counts["ready_sentences"],
                    "grouped_sentences": counts["grouped_sentences"],
                    "groups": counts["groups"],
                    "blocked_groups": counts["blocked"],
                },
            ),
            (
                "director_storyboard",
                "导演与故事板",
                {
                    "groups": counts["groups"],
                    "directed_groups": counts["directed"],
                    "ready_director_groups": counts.get(
                        "director_ready", counts["directed"]
                    ),
                    "storyboard_groups": counts["storyboards"],
                    "preplan_total_units": counts.get("director_preplan_total_units", 0),
                    "preplan_completed_units": counts.get(
                        "director_preplan_completed_units", 0
                    ),
                    "preplan_revisions": counts.get(
                        "director_preplan_revisions", 0
                    ),
                    "materializations": counts.get(
                        "director_materializations", 0
                    ),
                },
            ),
            (
                "group_video_preview",
                "分组视频与预览",
                {"groups": counts["groups"], "video_groups": counts["videos"]},
            ),
            (
                "composition_export",
                "合成与导出",
                {
                    "current_compositions": counts["compositions"],
                    "skipped_groups": counts["blocked"],
                },
            ),
        )
        stages: list[dict[str, object]] = [
            {
                "key": key,
                "label": label,
                "status": "blocked",
                "blocking_reason": "previous_stage_incomplete",
                "allowed_actions": [],
                "counts": facts,
            }
            for key, label, facts in stage_facts
        ]

        def update(
            index: int,
            status: str,
            reason: str | None = None,
            actions: tuple[str, ...] = (),
        ) -> None:
            stages[index]["status"] = status
            stages[index]["blocking_reason"] = reason
            stages[index]["allowed_actions"] = list(actions)

        if project.production_state == "PURGING":
            for index in range(len(stages)):
                update(index, "blocked", "project_purging")
            return stages

        editable_actions = ("edit_project",) if project.snapshot is None else ()
        for index in range(4):
            update(index, "complete", actions=editable_actions)
        if project.snapshot is None:
            update(4, "current", actions=("edit_project", "confirm_narration"))
            for index in range(5, len(stages)):
                update(index, "blocked", "narration_not_confirmed")
            return stages

        update(4, "complete")
        if not narration_ready:
            narration_reason = "narration_not_ready"
            update(5, "current", narration_reason)
        else:
            narration_reason = None
            update(5, "complete")
        if not narration_ready:
            preplan_reason, preplan_actions = _workflow_preplan_gate(
                attempts.get("project_director"),
                has_revision=counts.get("director_preplan_revisions", 0) > 0,
            )
            update(6, "current", preplan_reason, preplan_actions)
            stages[6]["mode"] = "PREPLANNING"
            for index in range(7, len(stages)):
                update(index, "blocked", narration_reason)
            return stages

        stages[6]["mode"] = "FINAL"

        groups = counts["groups"]
        unblocked_groups = groups - counts["blocked"]
        inconsistency = _workflow_authority_inconsistency(counts, groups)
        if inconsistency is not None:
            _stage, reason = inconsistency
            if reason in {"director_authority_inconsistent", "storyboard_authority_inconsistent"}:
                update(6, "blocked", reason, ("REFRESH",))
                update(7, "blocked", reason)
            else:
                update(6, "complete")
                update(7, "blocked", reason, ("REFRESH",))
            update(8, "blocked", reason)
            return stages

        directed_complete = (
            counts.get("director_ready", counts["directed"]) == unblocked_groups
        )
        storyboards_complete = counts["storyboards"] == unblocked_groups
        director_reason: str | None
        if not directed_complete:
            director_reason, actions = _workflow_final_director_gate(
                attempts.get("project_director"),
                has_revision=counts.get("director_preplan_revisions", 0) > 0,
                has_materialization=counts.get("director_materializations", 0) > 0,
            )
            update(6, "current", director_reason, actions)
        elif not storyboards_complete:
            gate = _workflow_attempt_gate(
                "storyboard", attempts["storyboard"], allow_new_attempt=True
            )
            director_reason, actions = (
                gate if gate is not None else ("storyboards_incomplete", ("generate_storyboard",))
            )
            actions = _merge_workflow_actions(
                actions,
                _workflow_preplan_background_actions(
                    attempts.get("project_director")
                ),
            )
            update(6, "current", director_reason, actions)
        else:
            storyboard_gate = _workflow_attempt_gate(
                "storyboard", attempts["storyboard"], allow_new_attempt=True
            )
            if storyboard_gate is None:
                actions = ("generate_storyboard",)
                director_reason = None
            else:
                director_reason, actions = storyboard_gate
            actions = _merge_workflow_actions(
                actions,
                _workflow_preplan_background_actions(
                    attempts.get("project_director")
                ),
            )
            update(6, "complete", director_reason, actions)
        if not directed_complete or not storyboards_complete:
            update(7, "blocked", director_reason)
            update(8, "blocked", director_reason)
            return stages

        videos_complete = counts["videos"] == unblocked_groups
        if counts["videos"] > groups:
            video_reason = "video_authority_inconsistent"
            update(7, "blocked", video_reason)
        elif not videos_complete:
            gate = _workflow_attempt_gate("video", attempts["video"], allow_new_attempt=True)
            video_reason, actions = (
                gate
                if gate is not None
                else (
                    "videos_incomplete",
                    ("generate_group_video", "generate_group_videos_batch"),
                )
            )
            update(7, "current", video_reason, actions)
        else:
            gate = _workflow_attempt_gate(
                "video", attempts["video"], allow_new_attempt=True
            )
            if gate is None:
                video_reason, actions = (
                    None,
                    ("generate_group_video", "generate_group_videos_batch"),
                )
            else:
                video_reason, actions = gate
            update(7, "complete", video_reason, actions)
        if not videos_complete:
            update(8, "blocked", video_reason)
            return stages
        if unblocked_groups == 0:
            update(8, "blocked", "all_groups_video_blocked")
            return stages

        gate = _workflow_attempt_gate(
            "composition", attempts["composition"], allow_new_attempt=True
        )
        if gate is not None:
            composition_reason, actions = gate
            if counts["compositions"] > 0:
                actions = ("export", *actions)
            update(
                8,
                "complete" if counts["compositions"] > 0 else "current",
                composition_reason,
                actions,
            )
        elif counts["compositions"] > 0:
            update(8, "complete", actions=("compose", "export"))
        else:
            update(8, "current", actions=("compose",))
        return stages

    def edit_project(
        self, owner: Account, project_id: UUID, expected_revision: int, changes: dict[str, object]
    ) -> Project:
        project = self.repo.project_for_write(owner.id, project_id)
        if project is None:
            raise DomainError("not_found", "Resource not found")
        if "visual_style" in changes:
            visual_style = changes["visual_style"]
            if not isinstance(visual_style, str) or not visual_style.strip():
                raise DomainError("visual_style_required", "Visual style must not be blank")
        if changes.get("aspect_ratio") in {AspectRatio.PORTRAIT, AspectRatio.PORTRAIT.value}:
            raise DomainError(
                "aspect_ratio_not_supported", "Only landscape 16:9 projects are supported"
            )
        candidate = project.edited(**changes)
        self._validate_project_refs(
            owner,
            candidate.channel_id,
            candidate.character_image_ids,
            candidate.voice_version_id,
            candidate.locale,
            cloned_voice_id=candidate.cloned_voice_id,
        )
        if not self.repo.save_project(candidate, expected_revision):
            raise DomainError("project_snapshot_locked", "Project changed or is already locked")
        return candidate

    def confirm_narration(
        self, owner: Account, project_id: UUID, expected_revision: int, now: datetime
    ) -> Project:
        project = self.repo.project_for_write(owner.id, project_id)
        if project is None:
            raise DomainError("not_found", "Resource not found")
        if project.production_state != "ACTIVE":
            raise DomainError("project_purging", "Project is being permanently deleted")
        if project.snapshot is not None:
            raise DomainError("project_snapshot_locked", "Project changed or is already locked")
        channel = self.get_channel(owner, project.channel_id)
        images = tuple(
            self.repo.image(owner.id, project.channel_id, image_id)
            for image_id in project.character_image_ids
        )
        if any(item is None for item in images):
            raise DomainError("invalid_project_reference", "Project inputs are unavailable")
        validate_script_language(project.script, Language(project.locale.value))
        characters = tuple(
            SnapshotCharacter(
                i,
                image.id,
                image.asset.asset_id,
                image.asset.sha256,
                image.asset.media_type,
                image.asset.size_bytes,
            )
            for i, image in enumerate(images)
            if image is not None
        )
        snapshot = self._build_snapshot(channel, project, characters, now)
        if not self.repo.confirm_project(owner.id, project.id, expected_revision, snapshot):
            raise DomainError("project_snapshot_locked", "Project changed or is already locked")
        return replace(project, snapshot=snapshot)

    def set_project_publication(
        self, owner: Account, project_id: UUID, publish: bool, now: datetime
    ) -> Project:
        """人工发布标记：只写 published_at 与审计，不进入编辑/快照/生产版本。"""
        project = self.repo.project_for_write(owner.id, project_id)
        if project is None:
            raise DomainError("not_found", "Resource not found")
        if project.production_state != "ACTIVE":
            raise DomainError("project_purging", "Project is being permanently deleted")
        if publish and not self.repo.project_has_current_composition(owner.id, project_id):
            raise DomainError(
                "project_composition_required",
                "A current composition is required before publishing",
            )
        if publish == (project.published_at is not None):
            return project
        published_at = now if publish else None
        if not self.repo.set_project_publication(
            owner.id,
            project_id,
            published_at=published_at,
            actor_account_id=owner.id,
            occurred_at=now,
        ):
            raise DomainError(
                "project_publication_conflict",
                "Project publication state changed",
            )
        return replace(project, published_at=published_at)

    def _build_snapshot(
        self,
        channel: Channel,
        project: Project,
        characters: tuple[SnapshotCharacter, ...],
        now: datetime,
    ) -> ProjectSnapshot:
        """按配音源冻结快照（§23.1）：EL 分支用逻辑克隆音色+源音频哈希替换三件媒体事实。"""
        common: dict[str, object] = {
            "script": project.script,
            "characters": characters,
            "visual_style": project.visual_style,
            "locale": project.locale,
            "aspect_ratio": project.aspect_ratio,
            "channel_url": channel.url,
            "expected_duration_seconds": project.expected_duration_seconds,
            "confirmed_at": now,
            "tts_provider": project.tts_provider,
        }
        if project.tts_provider == TtsProvider.ELEVENLABS.value:
            cloned_id = project.cloned_voice_id
            if cloned_id is None:
                raise DomainError(
                    "invalid_project_reference", "Cloned voice reference is unavailable"
                )
            cloned = self.repo.cloned_voice(channel.id, cloned_id)
            if cloned is None:
                raise DomainError(
                    "invalid_cloned_voice", "Cloned voice is unavailable for the channel"
                )
            return ProjectSnapshot(
                cloned_voice_id=cloned.id,
                cloned_voice_name=cloned.name,
                cloned_voice_source_sha256=cloned.source_sha256,
                **common,  # type: ignore[arg-type]
            )
        voice = (
            self.repo.voice_version(project.voice_version_id)
            if project.voice_version_id
            else None
        )
        if (
            voice is None
            or not voice.enabled
            or project.locale not in voice.supported_locales
            or voice.reference_zip is None
            or voice.preview is None
            or voice.avatar is None
        ):
            raise DomainError("invalid_project_reference", "Project inputs are unavailable")
        return ProjectSnapshot(
            voice_version_id=voice.id,
            voice_metadata_sha256=voice.metadata_sha256,
            voice_catalog_source_sha256=voice.catalog_source_sha256,
            voice_reference_zip=voice.reference_zip,
            voice_preview=voice.preview,
            voice_avatar=voice.avatar,
            **common,  # type: ignore[arg-type]
        )

    def _validate_project_refs(
        self,
        owner: Account,
        channel_id: UUID,
        image_ids: tuple[UUID, ...],
        voice_version_id: UUID | None,
        locale: SupportedLocale,
        *,
        cloned_voice_id: UUID | None = None,
    ) -> None:
        if len(set(image_ids)) != len(image_ids):
            raise DomainError("duplicate_character_image", "Character images must be unique")
        if self.repo.channel(owner.id, channel_id) is None or any(
            self.repo.image(owner.id, channel_id, image_id) is None for image_id in image_ids
        ):
            raise DomainError("not_found", "Resource not found")
        if cloned_voice_id is not None:
            # §23.2/§23.10：EL 克隆音色由多语言模型全覆盖六种首期语言，恒兼容；
            # 只校验归属（频道共享，不得跨频道引用）。
            if self.repo.cloned_voice(channel_id, cloned_voice_id) is None:
                raise DomainError(
                    "invalid_cloned_voice", "Cloned voice is unavailable for the channel"
                )
            return
        voice = self.repo.voice_version(voice_version_id) if voice_version_id else None
        if voice is None or not voice.enabled or locale not in voice.supported_locales:
            raise DomainError(
                "invalid_voice_version", "Voice version is unavailable for the project language"
            )

    def _new_account(
        self, username: str, password: str, role: AccountRole, *, check_existing: bool = True
    ) -> Account:
        normalized = username.strip()
        if not normalized or (
            check_existing and self.repo.account_by_username(normalized) is not None
        ):
            raise DomainError("invalid_username", "Username is unavailable")
        self._validate_password(password)
        return Account(uuid4(), normalized, self.passwords.hash(password), role)

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 8 or len(password) > 1024:
            raise DomainError("weak_password", "Password must contain 8 to 1024 characters")

    @staticmethod
    def _require_admin(actor: Account) -> None:
        if actor.role is not AccountRole.ADMIN or not actor.active:
            raise DomainError("forbidden", "Administrator access required")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _channel_name(value: str) -> str | None:
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > 30:
            raise DomainError(
                "invalid_channel_name", "Channel name must contain at most 30 characters"
            )
        return stripped

    @staticmethod
    def _channel_url(value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
            or any(ch.isspace() for ch in value)
        ):
            raise DomainError("invalid_channel_url", "Channel URL must be an absolute HTTP(S) URL")
        host = parsed.hostname
        if host is None:
            raise DomainError("invalid_channel_url", "Channel URL must be an absolute HTTP(S) URL")
        try:
            port = parsed.port
        except ValueError as exc:
            raise DomainError(
                "invalid_channel_url", "Channel URL must be an absolute HTTP(S) URL"
            ) from exc
        canonical_host = f"[{host.lower()}]" if ":" in host else host.lower()
        authority = canonical_host if port is None else f"{canonical_host}:{port}"
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, ""))
