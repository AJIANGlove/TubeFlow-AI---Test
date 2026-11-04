from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from fractions import Fraction
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.persistence.identity_project.models import (
    CharacterImageRow,
    ProjectRow,
    ProjectSnapshotCharacterRow,
)
from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    AuthoritativeNarrationMembershipRecord,
    AuthoritativeNarrationSentenceRecord,
    NarrationAttemptRecord,
    NarrationProjectRecord,
)
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorRevisionRow,
    StoryboardCurrentRow,
    StoryboardGenerationRow,
    StoryboardMediaRow,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    _shot as _director_shot_from_payload,
)
from tubeflow_test.application.media_authority import (
    OUTPUT_RESOLUTION,
    CompositionProjectSnapshot,
    LatestFeatureCompositionInputs,
    LatestH3Group,
    LatestNarrationSentence,
)
from tubeflow_test.application.storyboards import (
    canonical_character_payload,
    canonical_storyboard_request_sha256,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.common import AspectRatio, MediaAssetRef, SupportedLocale
from tubeflow_test.domain.composition import CompositionRequest
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import (
    frozen_script_sha256,
    is_nonvisual_script_separator,
    segment_pasted_script,
)
from tubeflow_test.domain.storyboards import (
    GRID_LAYOUT_V1,
    GRID_LAYOUT_V2,
    SHOT_TIMING_PROJECTION_V1,
    DirectorShot,
    ShotSentenceLink,
    ShotTimeRange,
    projected_shot_time_ranges_ms,
    shot_time_ranges_ms,
)
from tubeflow_test.identity_project.domain import TtsProvider, normalized_tts_provider
from tubeflow_test.sentence_split.domain import (
    apply_sentence_split_plan,
    sentence_split_plan_from_json,
)
from tubeflow_test.video_generation.authority import LatestAuthorityLoader
from tubeflow_test.video_generation.domain import (
    ReferencePolicy,
    ReferenceStrategy,
    ShotPanelImage,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import (
    DELIVER_WITH_DEFECT_MARKER,
    AuthoritativeGroupInput,
)
from tubeflow_test.video_generation.panel_images import CROP_GRID_V2
from tubeflow_test.video_generation.persistence import (
    GroupCurrentVideoRecord,
    VideoAttemptRecord,
)
from tubeflow_test.video_generation.project_routing import (
    reference_input_fingerprint,
    resolve_project_workflow,
    resolve_reference_policy,
)


class SqlCanonicalCompositionInputs:
    """Latest-contract composition authority; all reads share one transaction."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def load(self, project_id: UUID, owner_id: UUID) -> CompositionRequest | None:
        with self._sessions() as session, session.begin():
            project = session.scalar(
                select(ProjectRow).where(
                    ProjectRow.id == project_id, ProjectRow.owner_id == owner_id
                )
            )
            if project is None or project.snapshot_confirmed_at is None:
                return None
            sentence_rows = tuple(
                session.scalars(
                    select(AuthoritativeNarrationSentenceRecord)
                    .where(
                        AuthoritativeNarrationSentenceRecord.project_id == project_id,
                        AuthoritativeNarrationSentenceRecord.owner_id == owner_id,
                    )
                    .order_by(AuthoritativeNarrationSentenceRecord.sequence)
                )
            )
            _assert_composition_sentence_timeline(sentence_rows)
            sentence_by_id = {row.id: row for row in sentence_rows}
            attempts_index = _narration_attempt_index(session, owner_id, project_id)
            groups: list[LatestH3Group] = []
            kept: list[LatestNarrationSentence] = []
            frame_cursor = 0
            sentence_order = 0
            # 重排前先验证原始分组序号连续规范，杜绝被重排掩盖的脏数据。
            group_rows = tuple(
                session.scalars(
                    select(AuthoritativeNarrationGroupRecord)
                    .where(
                        AuthoritativeNarrationGroupRecord.project_id == project_id,
                        AuthoritativeNarrationGroupRecord.owner_id == owner_id,
                    )
                    .order_by(AuthoritativeNarrationGroupRecord.sequence)
                )
            )
            if tuple(row.sequence for row in group_rows) != tuple(
                range(1, len(group_rows) + 1)
            ):
                raise DomainError(
                    "composition_authority_invalid",
                    "Narration group sequence is not canonical",
                )
            for group in group_rows:
                # 被阻止组整体排除：其画面与配音均不进入成片，剩余内容时间轴连续重排。
                if group.video_blocked:
                    continue
                current = session.get(GroupCurrentVideoRecord, group.id)
                attempt = session.get(VideoAttemptRecord, current.attempt_id) if current else None
                asset = _composition_video_asset(
                    session, current, attempt, group.id, owner_id, project_id
                )
                if asset is None:
                    return None
                assert attempt is not None
                evidence = attempt.probe_evidence_json or {}
                sentence_ids = tuple(
                    session.scalars(
                        select(AuthoritativeNarrationMembershipRecord.sentence_id)
                        .where(AuthoritativeNarrationMembershipRecord.group_id == group.id)
                        .order_by(AuthoritativeNarrationMembershipRecord.ordinal)
                    )
                )
                _assert_composition_membership(
                    sentence_by_id, sentence_ids, group.snapshot_revision
                )
                kept_order = len(groups) + 1
                group_start_cursor = frame_cursor
                sample_rate = 0
                el_cloned_voice_source = _el_cloned_voice_source(project)
                for sentence_id in sentence_ids:
                    row = sentence_by_id[sentence_id]
                    span = row.end_frame - row.start_frame
                    sample_rate = row.sample_rate_hz
                    sentence_order += 1
                    kept.append(
                        LatestNarrationSentence(
                            row.id,
                            sentence_order,
                            row.text,
                            row.text_sha256,
                            frame_cursor,
                            frame_cursor + span,
                            row.sample_rate_hz,
                            _composition_audio_asset(
                                session,
                                row,
                                owner_id,
                                project_id,
                                attempts_index,
                                el_cloned_voice_source_sha256=el_cloned_voice_source,
                            ).asset_id,
                        )
                    )
                    frame_cursor += span
                groups.append(
                    LatestH3Group(
                        group.id,
                        kept_order,
                        asset.id,
                        sentence_ids,
                        round(Fraction(group_start_cursor * 1000, sample_rate)),
                        round(Fraction(frame_cursor * 1000, sample_rate)),
                        int(str(evidence["width"])),
                        int(str(evidence["height"])),
                    )
                )
            sentences: tuple[LatestNarrationSentence, ...] = tuple(kept)
            aspect = AspectRatio(project.snapshot_aspect_ratio or project.aspect_ratio)
            if not groups:
                return None
            group_width, group_height = groups[0].width, groups[0].height
            width, height = OUTPUT_RESOLUTION[aspect]
            snapshot_payload = {
                "project_id": str(project.id),
                "production_revision": project.production_revision,
                "aspect_ratio": aspect.value,
            }
            snapshot = CompositionProjectSnapshot(
                hashlib.sha256(
                    json.dumps(snapshot_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                aspect,
                width,
                height,
                group_width,
                group_height,
            )
        return LatestFeatureCompositionInputs(
            _Narration(sentences), _Groups(tuple(groups)), _Snapshot(snapshot)
        ).load(project_id, owner_id)


# §46：网格 v2 故事板的固定参考策略。
GRID_V2_REFERENCE_POLICY = ReferencePolicy(
    ReferenceStrategy.SINGLE_PANEL_V1, {"version": CROP_GRID_V2}
)


class PanelDerivationService(Protocol):
    def derive(
        self,
        session: Session,
        source: AuthoritativeGroupInput,
        policy: ReferencePolicy,
        *,
        for_intent: bool,
    ) -> tuple[ShotPanelImage, ...]: ...


class SqlCanonicalVideoGroupLoader:
    """Builds the immutable H3 input exclusively from latest canonical rows."""

    def __init__(
        self,
        settings: Settings,
        *,
        reference_policy: ReferencePolicy | Callable[[UUID], ReferencePolicy] | None = None,
        single_panel_allowed: Callable[[UUID], bool] | None = None,
        panel_derivatives: PanelDerivationService | None = None,
    ) -> None:
        self._settings = settings
        self._reference_policy = reference_policy or ReferencePolicy()
        self._single_panel_allowed = single_panel_allowed or (lambda _project_id: False)
        self._panel_derivatives = panel_derivatives

    def apply_reference_policy(
        self,
        session: Session,
        narration: AuthoritativeGroupInput,
        *,
        for_intent: bool,
    ) -> AuthoritativeGroupInput:
        if narration.storyboard_grid_layout_version == GRID_LAYOUT_V2:
            # §46：v2 网格只为逐格参考而画；整图参考不适用，因此无需 allowlist。
            requested = GRID_V2_REFERENCE_POLICY
            allowed = True
        else:
            requested = (
                self._reference_policy(narration.project_id)
                if callable(self._reference_policy)
                else self._reference_policy
            )
            allowed = self._single_panel_allowed(narration.project_id)
        selected = resolve_reference_policy(
            session,
            narration.project_id,
            requested,
            for_intent=for_intent,
            single_panel_allowed=allowed,
        )
        if selected.strategy is ReferenceStrategy.COMPOSITE:
            return narration
        if self._panel_derivatives is None:
            raise VideoRuleViolation("single_panel_derivation_not_configured")
        panels = self._panel_derivatives.derive(
            session, narration, selected, for_intent=for_intent
        )
        fingerprint = reference_input_fingerprint(
            narration.authority_snapshot, selected, panels
        )
        return replace(
            narration,
            shot_panel_images=panels,
            reference_policy=selected,
            reference_input_sha256=fingerprint,
        )

    def __call__(self, session: Session, owner_id: UUID, group_id: UUID) -> AuthoritativeGroupInput:
        group = session.scalar(
            select(AuthoritativeNarrationGroupRecord).where(
                AuthoritativeNarrationGroupRecord.id == group_id,
                AuthoritativeNarrationGroupRecord.owner_id == owner_id,
            )
        )
        if group is None:
            raise DomainError("not_found", "Narration group was not found")
        project = session.scalar(
            select(ProjectRow).where(
                ProjectRow.id == group.project_id,
                ProjectRow.owner_id == owner_id,
            ).with_for_update()
        )
        current = session.scalar(
            select(StoryboardCurrentRow).where(
                StoryboardCurrentRow.owner_id == owner_id,
                StoryboardCurrentRow.group_id == group_id,
            ).with_for_update()
        )
        generation = (
            session.get(StoryboardGenerationRow, current.generation_id)
            if current is not None and current.generation_id is not None
            else None
        )
        media_link = (
            session.get(StoryboardMediaRow, generation.id) if generation is not None else None
        )
        if (
            project is None
            or project.production_state != "ACTIVE"
            or project.snapshot_confirmed_at is None
            or project.snapshot_script is None
            or not project.snapshot_character_image_ids
            or not project.snapshot_visual_style
            or _frozen_voice_missing(project)
            or project.snapshot_locale is None
            or project.snapshot_aspect_ratio is None
            or group.snapshot_revision != project.production_revision
            or frozen_script_sha256(project.snapshot_script) != group.snapshot_sha256
            or generation is None
            or generation.status != "SUCCEEDED"
            or generation.owner_id != owner_id
            or generation.project_id != project.id
            or generation.group_id != group.id
            or media_link is None
            or current is None
            or current.version <= 0
        ):
            raise DomainError("h3_inputs_not_ready", "Canonical H3 inputs are not ready")
        storyboard_media = _storyboard_media_ref(
            session, generation, media_link, owner_id, project.id, group.id
        )
        sentence_rows = tuple(
            session.scalars(
                select(AuthoritativeNarrationSentenceRecord)
                .where(
                    AuthoritativeNarrationSentenceRecord.project_id == project.id,
                    AuthoritativeNarrationSentenceRecord.owner_id == owner_id,
                    AuthoritativeNarrationSentenceRecord.snapshot_revision
                    == group.snapshot_revision,
                )
                .order_by(AuthoritativeNarrationSentenceRecord.sequence)
            )
        )
        memberships = tuple(
            session.scalars(
                select(AuthoritativeNarrationMembershipRecord)
                .where(AuthoritativeNarrationMembershipRecord.group_id == group.id)
                .order_by(AuthoritativeNarrationMembershipRecord.ordinal)
            )
        )
        _assert_membership_ordinals(memberships)
        member_ids = tuple(row.sentence_id for row in memberships)
        frozen_script = segment_pasted_script(
            project.snapshot_script,
            SupportedLocale(project.snapshot_locale or project.locale),
        )
        narration_record = session.get(NarrationProjectRecord, project.id)
        split_plan = (
            sentence_split_plan_from_json(narration_record.payload.get("sentence_splits"))
            if narration_record is not None
            else None
        )
        if split_plan is not None and split_plan.splits:
            # AUTO 项目冻结时的拆句血缘参与同一确定性句子比对。
            frozen_script = apply_sentence_split_plan(frozen_script, split_plan)
        if (
            not sentence_rows
            or group.snapshot_sha256 != frozen_script.source_sha256
            or tuple(
                (row.sequence, row.text, row.text_sha256, row.snapshot_sha256)
                for row in sentence_rows
            )
            != tuple(
                (item.sequence, item.text, item.text_sha256, frozen_script.source_sha256)
                for item in frozen_script.sentences
                if not is_nonvisual_script_separator(item.text)
            )
            or not member_ids
        ):
            raise DomainError("h3_inputs_not_ready", "Narration lineage is inconsistent")
        index_by_id = {row.id: index for index, row in enumerate(sentence_rows)}
        if any(sentence_id not in index_by_id for sentence_id in member_ids):
            raise DomainError("h3_inputs_not_ready", "Narration membership is incomplete")
        member_rows = tuple(sentence_rows[index_by_id[value]] for value in member_ids)
        if (
            any(row.sample_rate_hz != group.sample_rate_hz for row in member_rows)
            or member_rows[0].start_frame != group.start_frame
            or member_rows[-1].end_frame != group.end_frame
            or any(
                row.end_frame - row.start_frame != row.measured_frame_count
                for row in member_rows
            )
            or any(
                left.end_frame != right.start_frame
                for left, right in zip(member_rows, member_rows[1:], strict=False)
            )
            or sum(row.measured_frame_count for row in member_rows)
            != group.end_frame - group.start_frame
        ):
            raise DomainError("h3_inputs_not_ready", "Narration sample timeline is inconsistent")
        attempts_index = _narration_attempt_index(session, owner_id, project.id)
        el_cloned_voice_source = _el_cloned_voice_source(project)
        audio_lineage = tuple(
            _narration_audio_lineage(
                session,
                row,
                owner_id,
                project.id,
                project.snapshot_voice_version_id,
                project.snapshot_locale,
                attempts_index,
                el_cloned_voice_source_sha256=el_cloned_voice_source,
            )
            for row in sentence_rows
        )
        raw_assets = tuple(value[0] for value in audio_lineage)
        normalized_assets = tuple(value[1] for value in audio_lineage)
        character_rows = tuple(
            session.scalars(
                select(ProjectSnapshotCharacterRow)
                .where(ProjectSnapshotCharacterRow.project_id == project.id)
                .order_by(ProjectSnapshotCharacterRow.ordinal)
            )
        )
        if (
            tuple(row.ordinal for row in character_rows) != tuple(range(len(character_rows)))
            or tuple(row.character_image_id for row in character_rows)
            != tuple(project.snapshot_character_image_ids)
        ):
            raise DomainError("h3_inputs_not_ready", "Character snapshot is inconsistent")
        characters = tuple(
            _character_media_ref(session, row, owner_id, project.id) for row in character_rows
        )
        inputs = generation.input_json
        parameters = inputs.get("parameters", {})
        generation_model = str(inputs.get("model") or "")
        generation_provider = str(inputs.get("provider") or "")
        director = session.scalar(
            select(DirectorRevisionRow).where(
                DirectorRevisionRow.id == generation.applied_revision_id,
                DirectorRevisionRow.owner_id == owner_id,
                DirectorRevisionRow.project_id == project.id,
                DirectorRevisionRow.group_id == group.id,
            )
        )
        expected_characters = [
            # 与故事板生成端共用同一序列化口径；数据库 ordinal 为 0 基，载荷为 1 基
            canonical_character_payload(
                ordinal=row.ordinal + 1,
                character_image_id=row.character_image_id,
                asset_id=row.asset_id,
                sha256=row.sha256,
                media_type=row.media_type,
                size_bytes=row.size_bytes,
            )
            for row in character_rows
        ]
        canonical_request_sha256 = canonical_storyboard_request_sha256(
            owner_id=owner_id,
            group_id=group.id,
            revision_id=generation.applied_revision_id,
            prompt=str(inputs.get("prompt", "")),
            characters=expected_characters,
            style=str(project.snapshot_visual_style),
            model=generation_model,
            parameters=dict(parameters) if isinstance(parameters, dict) else {},
            provider=generation_provider,
        )
        _assert_storyboard_identity(
            director.group_sentence_ids_json if director is not None else [],
            member_ids,
            generation.canonical_request_sha256,
            canonical_request_sha256,
        )
        if (
            director is None
            or director.kind != "APPLIED"
            or director.review_state == "REQUIRED"
            or not director.shots_json
            or inputs.get("shots") != director.shots_json
            or inputs.get("links") != director.links_json
            or inputs.get("character_assets") != expected_characters
            or inputs.get("style") != project.snapshot_visual_style
            or not isinstance(inputs.get("prompt"), str)
            or not str(inputs["prompt"]).strip()
            or not generation_model
            or not isinstance(parameters, dict)
            or not generation.idempotency_key
            or not generation.provider_idempotency_key
            or generation.state_revision <= 0
        ):
            raise DomainError("h3_inputs_not_ready", "Storyboard lineage is inconsistent")
        director_shots = tuple(_director_shot_from_payload(item) for item in director.shots_json)
        shot_links = tuple(
            ShotSentenceLink(UUID(str(item["shot_id"])), UUID(str(item["sentence_id"])))
            for item in director.links_json
        )
        storyboard_shot_ranges = _assert_storyboard_shot_ranges(
            inputs,
            director_shots,
            shot_links,
            {row.id: (row.start_ms, row.end_ms) for row in member_rows},
        )
        seed = parameters.get("seed") if isinstance(parameters, dict) else None
        workflow = resolve_project_workflow(session, project.id, self._settings)
        return AuthoritativeGroupInput(
            owner_id=owner_id,
            project_id=project.id,
            group_id=group.id,
            generation_id=generation.id,
            snapshot_frozen=True,
            script_text_sha256=hashlib.sha256(project.snapshot_script.encode()).hexdigest(),
            sentence_text_sha256=tuple(row.text_sha256 for row in sentence_rows),
            narrated_text_sha256=tuple(row.text_sha256 for row in sentence_rows),
            sentence_durations_ms=tuple(row.measured_duration_ms for row in sentence_rows),
            sentence_sample_counts=tuple(row.measured_frame_count for row in sentence_rows),
            sentence_sample_rates=tuple(row.sample_rate_hz for row in sentence_rows),
            timeline_ranges_frames=tuple(
                (row.start_frame, row.end_frame) for row in sentence_rows
            ),
            narration_qc_passed=tuple(
                row.raw_qc_passed and row.normalized_qc_passed for row in sentence_rows
            ),
            normalized_audio_assets=normalized_assets,
            timeline_ranges_ms=tuple((row.start_ms, row.end_ms) for row in sentence_rows),
            group_sentence_indexes=tuple(index_by_id[value] for value in member_ids),
            group_start_ms=group.start_ms,
            group_end_ms=group.end_ms,
            group_target_duration_ms=round(
                Fraction(
                    (group.end_frame - group.start_frame) * 1000,
                    group.sample_rate_hz,
                )
            ),
            group_start_frame=group.start_frame,
            group_end_frame=group.end_frame,
            group_sample_rate=group.sample_rate_hz,
            character_images=characters,
            storyboard_image=storyboard_media,
            storyboard_kind=generation.kind,
            storyboard_is_current=True,
            applied_revision_id=generation.applied_revision_id,
            storyboard_applied_revision_id=generation.applied_revision_id,
            prompt=str(inputs["prompt"]),
            aspect_ratio=AspectRatio(project.snapshot_aspect_ratio),
            seed=seed if isinstance(seed, (int, str)) else None,
            workflow=workflow,
            immutable_script=project.snapshot_script,
            authoritative_group_sentence_indexes=tuple(index_by_id[value] for value in member_ids),
            storyboard_version=generation.version,
            raw_audio_assets=raw_assets,
            storyboard_generation_id=generation.id,
            storyboard_asset_sha256=storyboard_media.sha256,
            director_shots=director_shots,
            shot_sentence_links=shot_links,
            group_sentence_ids=member_ids,
            visual_style=project.snapshot_visual_style,
            production_mode=project.production_mode,
            storyboard_shot_ranges=storyboard_shot_ranges,
            shot_time_ranges_algorithm=str(inputs.get("shot_time_ranges_algorithm") or ""),
            storyboard_grid_layout_version=str(
                inputs.get("grid_layout_version") or GRID_LAYOUT_V1
            ),
            authority_snapshot={
                "project": {
                    "id": str(project.id),
                    "owner_id": str(project.owner_id),
                    "production_revision": project.production_revision,
                    "production_state": project.production_state,
                    "script_sha256": group.snapshot_sha256,
                    "visual_style": project.snapshot_visual_style,
                    "locale": project.snapshot_locale,
                    "aspect_ratio": project.snapshot_aspect_ratio,
                },
                "group": {
                    "id": str(group.id),
                    "sequence": group.sequence,
                    "snapshot_revision": group.snapshot_revision,
                    "member_ids": [str(value) for value in member_ids],
                    "memberships": [
                        {"ordinal": row.ordinal, "sentence_id": str(row.sentence_id)}
                        for row in memberships
                    ],
                    "start_frame": group.start_frame,
                    "end_frame": group.end_frame,
                    "sample_rate_hz": group.sample_rate_hz,
                },
                "sentences": [
                    {
                        "id": str(row.id),
                        "sequence": row.sequence,
                        "text_sha256": row.text_sha256,
                        "start_frame": row.start_frame,
                        "end_frame": row.end_frame,
                        "sample_count": row.measured_frame_count,
                        "sample_rate_hz": row.sample_rate_hz,
                        "raw": lineage[2],
                        "normalized": lineage[3],
                    }
                    for row, lineage in zip(sentence_rows, audio_lineage, strict=True)
                ],
                "storyboard": {
                    "generation_id": str(generation.id),
                    "version": generation.version,
                    "kind": generation.kind,
                    "status": generation.status,
                    "applied_revision_id": str(generation.applied_revision_id),
                    "asset_id": str(storyboard_media.asset_id),
                    "sha256": storyboard_media.sha256,
                    "current": True,
                    "current_version": current.version,
                    "state_revision": generation.state_revision,
                    "idempotency_key": generation.idempotency_key,
                    "provider_idempotency_key": generation.provider_idempotency_key,
                    "canonical_request_sha256": generation.canonical_request_sha256,
                    "input": inputs,
                    "director": {
                        "id": str(director.id),
                        "revision_number": director.revision_number,
                        "kind": director.kind,
                        "review_state": director.review_state,
                        "shots": director.shots_json,
                        "links": director.links_json,
                        "group_sentence_ids": director.group_sentence_ids_json,
                    },
                    "media": _shared_asset_authority_snapshot(
                        session, storyboard_media.asset_id, owner_id, project.id
                    ),
                },
                "characters": [
                    {
                        "ordinal": row.ordinal,
                        "character_image_id": str(row.character_image_id),
                        "media": _character_authority_snapshot(session, row, owner_id),
                    }
                    for row in character_rows
                ],
                "provider": {
                    "model_key": workflow.model_key,
                    "model_version": workflow.model_version,
                    "workflow_id": workflow.workflow_id,
                    "workflow_version": workflow.workflow_version,
                    "native_audio": True,
                    "capability": "video.reference_images_to_video",
                },
            },
        )


class SqlCanonicalNarrationAuthority:
    """Production narration authority backed only by canonical rows."""

    def __init__(self, loader: SqlCanonicalVideoGroupLoader) -> None:
        self._loader = loader

    def load_group(
        self, session: Session, owner_id: UUID, group_id: UUID
    ) -> AuthoritativeGroupInput:
        return self._loader(session, owner_id, group_id)


class SqlCanonicalStoryboardAuthority:
    """Production storyboard authority that revalidates the current pointer."""

    def bind_current(
        self, session: Session, narration: AuthoritativeGroupInput
    ) -> AuthoritativeGroupInput:
        current = session.scalar(
            select(StoryboardCurrentRow).where(
                StoryboardCurrentRow.owner_id == narration.owner_id,
                StoryboardCurrentRow.group_id == narration.group_id,
                StoryboardCurrentRow.generation_id == narration.storyboard_generation_id,
            )
        )
        if current is None:
            raise DomainError("revision_conflict", "H3 intent requires the current storyboard")
        return narration


def single_panel_policy_settings(
    settings: Settings,
) -> tuple[Callable[[UUID], ReferencePolicy], Callable[[UUID], bool]]:
    """Default-off project selection; no global gamma, resize or implicit enablement."""
    enabled = settings.h3_single_panel_opt_in_enabled
    allowed_projects = frozenset(settings.h3_single_panel_project_ids)
    single_panel = ReferencePolicy(
        ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-title54-v1"}
    )

    def requested(project_id: UUID) -> ReferencePolicy:
        if enabled and project_id in allowed_projects:
            return single_panel
        return ReferencePolicy()

    def allowed(project_id: UUID) -> bool:
        return enabled and project_id in allowed_projects

    return requested, allowed


def canonical_video_authorities(
    settings: Settings,
    *,
    reference_policy: ReferencePolicy | Callable[[UUID], ReferencePolicy] | None = None,
    single_panel_allowed: Callable[[UUID], bool] | None = None,
    panel_derivatives: PanelDerivationService | None = None,
) -> tuple[
    SqlCanonicalNarrationAuthority,
    SqlCanonicalStoryboardAuthority,
    LatestAuthorityLoader,
]:
    loader = SqlCanonicalVideoGroupLoader(
        settings,
        reference_policy=reference_policy,
        single_panel_allowed=single_panel_allowed,
        panel_derivatives=panel_derivatives,
    )
    narration = SqlCanonicalNarrationAuthority(loader)
    storyboard = SqlCanonicalStoryboardAuthority()
    return narration, storyboard, LatestAuthorityLoader(narration, storyboard, loader)


def _media_ref(
    session: Session,
    asset_id: UUID,
    owner_id: UUID,
    project_id: UUID,
    expected_kind: str,
) -> MediaAssetRef:
    row = session.scalar(
        select(MediaAssetRow).where(
            MediaAssetRow.id == asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.project_id == project_id,
            MediaAssetRow.kind == expected_kind,
            MediaAssetRow.media_type == "audio/wav",
            MediaAssetRow.purging.is_(False),
            MediaAssetRow.deletion_state == "ACTIVE",
        )
    )
    if row is None:
        raise DomainError("h3_inputs_not_ready", "Canonical H3 media is unavailable")
    return MediaAssetRef(row.id, row.sha256, row.media_type, row.size_bytes)


def _qc_passed_or_defect_delivered(attempt: VideoAttemptRecord) -> bool:
    """视频 QC 装载门禁：QC 通过，或内容级缺陷带病交付（2026-10-16 决策）。

    带病交付标记只由编排层在内容级 QC 梯子耗尽后写入 retry_evidence；该放行
    不豁免任何其他完整性校验（sha256/尺寸/媒体类型/归属/归档指针保持不变）。
    """
    if (attempt.probe_evidence_json or {}).get("qc_passed") is True:
        return True
    evidence = attempt.retry_evidence
    return isinstance(evidence, list) and DELIVER_WITH_DEFECT_MARKER in evidence


def _composition_video_asset(
    session: Session,
    current: GroupCurrentVideoRecord | None,
    attempt: VideoAttemptRecord | None,
    group_id: UUID,
    owner_id: UUID,
    project_id: UUID,
) -> MediaAssetRow | None:
    if (
        current is None
        or current.group_id != group_id
        or attempt is None
        or attempt.stage != "ARCHIVED"
        or attempt.archive_stage != "COMPLETE"
        or attempt.owner_id != owner_id
        or attempt.project_id != project_id
        or attempt.group_id != group_id
        or current.attempt_id != attempt.attempt_id
        or current.generation_id != attempt.generation_id
        or current.intent_sequence != attempt.intent_sequence
        or not attempt.archived_asset_json
    ):
        return None
    archived = attempt.archived_asset_json
    try:
        asset_id = UUID(str(archived["asset_id"]))
        archived_size = int(str(archived["size_bytes"]))
    except (KeyError, TypeError, ValueError):
        return None
    asset = session.scalar(
        select(MediaAssetRow).where(
            MediaAssetRow.id == asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.project_id == project_id,
            MediaAssetRow.kind == "H3_ORIGINAL_MP4",
            MediaAssetRow.purging.is_(False),
            MediaAssetRow.deletion_state == "ACTIVE",
        )
    )
    if (
        asset is None
        or not _qc_passed_or_defect_delivered(attempt)
        or archived.get("sha256") != asset.sha256
        or archived.get("media_type") != asset.media_type
        or archived_size != asset.size_bytes
        or attempt.output_id is None
        or attempt.output_sha256 != asset.sha256
        or attempt.output_media_type != asset.media_type
        or attempt.output_size_bytes != asset.size_bytes
    ):
        return None
    return asset


def _character_media_ref(
    session: Session,
    snapshot: ProjectSnapshotCharacterRow,
    owner_id: UUID,
    project_id: UUID,
) -> MediaAssetRef:
    character = session.scalar(
        select(CharacterImageRow).where(
            CharacterImageRow.id == snapshot.character_image_id,
            CharacterImageRow.owner_id == owner_id,
        )
    )
    # 角色资产以频道共享库的 character_images 行为权威（与
    # SqlAlchemyAssetCatalog.resolve 同一口径）；字节完整性在 H3 暂存阶段
    # 经对象存储按 sha256 校验，本层不重复读取字节。
    if (
        snapshot.project_id != project_id
        or character is None
        or character.asset_id != snapshot.asset_id
        or character.sha256 != snapshot.sha256
        or character.media_type != snapshot.media_type
        or character.size_bytes != snapshot.size_bytes
        or not character.media_type.startswith("image/")
    ):
        raise DomainError(
            "h3_inputs_not_ready",
            "Character snapshot asset is not authoritative",
        )
    return MediaAssetRef(
        character.asset_id,
        character.sha256,
        character.media_type,
        character.size_bytes,
    )


def _composition_audio_asset(
    session: Session,
    sentence: AuthoritativeNarrationSentenceRecord,
    owner_id: UUID,
    project_id: UUID,
    attempts_index: dict[
        int, list[tuple[NarrationAttemptRecord, dict[str, object], dict[str, object]]]
    ]
    | None = None,
    el_cloned_voice_source_sha256: str | None = None,
) -> MediaAssetRef:
    return _narration_audio_lineage(
        session,
        sentence,
        owner_id,
        project_id,
        attempts_index=attempts_index,
        el_cloned_voice_source_sha256=el_cloned_voice_source_sha256,
    )[1]


def _frozen_voice_missing(project: ProjectRow) -> bool:
    """冻结音色是否缺失：按配音源二选一（§23.1）。

    EL 项目没有 CosyVoice 公共音色，快照只冻结克隆音色（`snapshot_cloned_voice_id`）；
    CosyVoice 项目反之。合法范围外的 provider 一律判为缺失，保持失败关闭。
    """

    provider = _snapshot_tts_provider(project)
    if provider == TtsProvider.ELEVENLABS.value:
        return project.snapshot_cloned_voice_id is None
    if provider == TtsProvider.COSYVOICE.value:
        return project.snapshot_voice_version_id is None
    return True


def _snapshot_tts_provider(project: ProjectRow) -> str | None:
    """快照配音源：未标注的历史快照按 cosyvoice 归一（与仓储读侧口径一致）。"""

    raw = project.snapshot_tts_provider or project.tts_provider
    if not raw:
        return None
    try:
        return normalized_tts_provider(raw)
    except DomainError:
        return None


def _el_cloned_voice_source(project: ProjectRow) -> str | None:
    """§23.1：EL 快照的克隆音色源哈希（配音参考包的等价音色证据）。

    仅当快照配音源为 EL 时返回；CosyVoice/未标注快照返回 None，调用侧据此
    保持既有校验口径。EL 项目缺少该证据时不放宽，由血缘校验保持失败关闭。
    """

    if _snapshot_tts_provider(project) != TtsProvider.ELEVENLABS.value:
        return None
    return project.snapshot_cloned_voice_source_sha256


def _narration_attempt_index(
    session: Session, owner_id: UUID, project_id: UUID
) -> dict[int, list[tuple[NarrationAttemptRecord, dict[str, object], dict[str, object]]]]:
    """一次性加载项目全部配音尝试并按句子序号索引。

    逐句血缘校验此前每句都全表扫描并重解析全部尝试 payload，装配一组
    H3 输入的成本为 O(句子数×尝试数)；索引后为 O(句子数+尝试数)。
    """
    index: dict[
        int, list[tuple[NarrationAttemptRecord, dict[str, object], dict[str, object]]]
    ] = {}
    for attempt in session.scalars(
        select(NarrationAttemptRecord).where(
            NarrationAttemptRecord.owner_id == owner_id,
            NarrationAttemptRecord.project_id == project_id,
        )
    ):
        payload = getattr(attempt, "payload", None)
        if not isinstance(payload, dict):
            continue
        narrated = payload.get("narration")
        attempt_fact = payload.get("attempt")
        if not isinstance(narrated, dict) or not isinstance(attempt_fact, dict):
            continue
        sequence = attempt_fact.get("sentence_sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            index.setdefault(sequence, []).append((attempt, narrated, attempt_fact))
    return index


def _voice_fact_matches(
    narrated: dict[str, object],
    *,
    expected_voice_version_id: str | None,
    el_cloned_voice_source_sha256: str | None,
) -> bool:
    """音色事实核验（§23.1 分支）。

    EL 项目没有公共音色（`voice_version_id` 恒为空，音色身份见项目快照）：
    以"配音参考包 == 快照冻结的克隆音色源"作为音色一致性证据；其余
    （CosyVoice/未标注历史快照）维持原口径：`voice_version_id` 必须为
    非空字符串且与项目快照一致。
    """

    if el_cloned_voice_source_sha256 is not None:
        return (
            narrated.get("voice_version_id") is None
            and narrated.get("reference_package_sha256")
            == el_cloned_voice_source_sha256
        )
    return isinstance(narrated.get("voice_version_id"), str) and (
        expected_voice_version_id is None
        or narrated.get("voice_version_id") == expected_voice_version_id
    )


def _narration_audio_lineage(
    session: Session,
    sentence: AuthoritativeNarrationSentenceRecord,
    owner_id: UUID,
    project_id: UUID,
    expected_voice_version_id: UUID | None = None,
    expected_locale: str | None = None,
    attempts_index: dict[
        int, list[tuple[NarrationAttemptRecord, dict[str, object], dict[str, object]]]
    ]
    | None = None,
    el_cloned_voice_source_sha256: str | None = None,
) -> tuple[MediaAssetRef, MediaAssetRef, dict[str, object], dict[str, object]]:
    expected_voice = str(expected_voice_version_id) if expected_voice_version_id else None
    raw = _narration_media_row(
        session, sentence.raw_asset_id, owner_id, project_id, "narration_raw"
    )
    normalized = _narration_media_row(
        session,
        sentence.normalized_asset_id,
        owner_id,
        project_id,
        "narration_normalized",
    )
    if attempts_index is None:
        attempts_index = _narration_attempt_index(session, owner_id, project_id)
    narration_fact: dict[str, object] | None = None
    attempt_snapshot: dict[str, object] | None = None
    for attempt, narrated, attempt_fact in attempts_index.get(sentence.sequence, ()):
        raw_fact = narrated.get("raw_asset")
        normalized_fact_value = narrated.get("normalized_asset")
        raw_qc = narrated.get("raw_qc")
        normalized_qc = narrated.get("normalized_qc")
        narrated_sentence = narrated.get("sentence")
        if (
            isinstance(attempt_fact, dict)
            and attempt_fact.get("state") == "COMPLETED"
            and attempt_fact.get("project_id") == str(project_id)
            and attempt_fact.get("owner_id") == str(owner_id)
            and attempt_fact.get("sentence_sequence") == sentence.sequence
            and attempt_fact.get("idempotency_key") == attempt.idempotency_key
            and attempt_fact.get("provider_call_id") == narrated.get("provider_call_id")
            and narrated.get("owner_id") == str(owner_id)
            and narrated.get("project_id") == str(project_id)
            and _voice_fact_matches(
                narrated,
                expected_voice_version_id=expected_voice,
                el_cloned_voice_source_sha256=el_cloned_voice_source_sha256,
            )
            and isinstance(narrated.get("locale"), str)
            and (expected_locale is None or narrated.get("locale") == expected_locale)
            and narrated.get("idempotency_key") == attempt.idempotency_key
            and isinstance(narrated.get("reference_package_sha256"), str)
            and len(str(narrated.get("reference_package_sha256"))) == 64
            and isinstance(narrated.get("normalization_manifest"), dict)
            and isinstance(raw_fact, dict)
            and raw_fact.get("asset_id") == str(sentence.raw_asset_id)
            and isinstance(normalized_fact_value, dict)
            and normalized_fact_value.get("asset_id") == str(sentence.normalized_asset_id)
            and isinstance(raw_qc, dict)
            and raw_qc.get("passed") is True
            and isinstance(normalized_qc, dict)
            and normalized_qc.get("passed") is True
            and normalized_qc.get("frame_count") == sentence.measured_frame_count
            and normalized_qc.get("sample_rate_hz") == sentence.sample_rate_hz
            and normalized_qc.get("measured_duration_ms") == sentence.measured_duration_ms
            and sentence.raw_qc_passed
            and sentence.normalized_qc_passed
            and isinstance(narrated_sentence, dict)
            and narrated_sentence.get("sequence") == sentence.sequence
            and narrated_sentence.get("text") == sentence.text
            and narrated_sentence.get("text_sha256") == sentence.text_sha256
        ):
            narration_fact = narrated
            attempt_snapshot = {
                "id": str(attempt.id),
                "idempotency_key": attempt.idempotency_key,
                "request_sha256": attempt.request_sha256,
                "attempt": attempt_fact,
                "provider_call_id": narrated.get("provider_call_id"),
                "voice_version_id": narrated.get("voice_version_id"),
                "locale": narrated.get("locale"),
                "reference_package_sha256": narrated.get("reference_package_sha256"),
                "normalization_manifest": narrated.get("normalization_manifest"),
                "raw_qc": raw_qc,
                "normalized_qc": normalized_qc,
            }
            break
    raw_fact = narration_fact.get("raw_asset") if narration_fact else None
    normalized_fact = narration_fact.get("normalized_asset") if narration_fact else None
    if (
        raw is None
        or normalized is None
        or not isinstance(raw_fact, dict)
        or not isinstance(normalized_fact, dict)
        or attempt_snapshot is None
        or not _asset_fact_matches(raw_fact, raw)
        or not _asset_fact_matches(normalized_fact, normalized)
    ):
        raise DomainError(
            "composition_authority_invalid",
            "Authoritative narration audio is unavailable",
        )
    return (
        MediaAssetRef(raw.id, raw.sha256, raw.media_type, raw.size_bytes),
        MediaAssetRef(
            normalized.id,
            normalized.sha256,
            normalized.media_type,
            normalized.size_bytes,
        ),
        {"media": _media_snapshot(raw), "lineage": attempt_snapshot},
        {"media": _media_snapshot(normalized), "lineage": attempt_snapshot},
    )


def _narration_media_row(
    session: Session,
    asset_id: UUID,
    owner_id: UUID,
    project_id: UUID,
    kind: str,
) -> MediaAssetRow | None:
    return session.scalar(
        select(MediaAssetRow).where(
            MediaAssetRow.id == asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.project_id == project_id,
            MediaAssetRow.kind == kind,
            MediaAssetRow.media_type == "audio/wav",
            MediaAssetRow.purging.is_(False),
            MediaAssetRow.deletion_state == "ACTIVE",
        )
    )


def _asset_fact_matches(fact: dict[str, object], asset: MediaAssetRow) -> bool:
    return (
        fact.get("sha256") == asset.sha256
        and fact.get("media_type") == asset.media_type
        and fact.get("size_bytes") == asset.size_bytes
        and len(asset.sha256) == 64
        and asset.size_bytes > 0
    )


def _media_snapshot(asset: MediaAssetRow) -> dict[str, object]:
    return {
        "id": str(asset.id),
        "owner_id": str(asset.owner_id) if asset.owner_id is not None else None,
        "project_id": str(asset.project_id) if asset.project_id is not None else None,
        "object_key": asset.object_key,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "media_type": asset.media_type,
        "kind": asset.kind,
        "deletion_state": asset.deletion_state,
        "purging": asset.purging,
    }


def _shared_asset_authority_snapshot(
    session: Session, asset_id: UUID, owner_id: UUID, project_id: UUID
) -> dict[str, object]:
    # 故事板等账号级共享资产以 project_asset_references 表达项目绑定，
    # 资产行本身不带 project_id。
    asset = session.scalar(
        select(MediaAssetRow)
        .join(
            ProjectAssetReferenceRow,
            ProjectAssetReferenceRow.asset_id == MediaAssetRow.id,
        )
        .where(
            MediaAssetRow.id == asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.deletion_state == "ACTIVE",
            MediaAssetRow.purging.is_(False),
            ProjectAssetReferenceRow.project_id == project_id,
            ProjectAssetReferenceRow.owner_id == owner_id,
        )
    )
    if asset is None:
        raise DomainError("h3_inputs_not_ready", "Canonical H3 media is unavailable")
    return _media_snapshot(asset)


def _character_authority_snapshot(
    session: Session, snapshot: ProjectSnapshotCharacterRow, owner_id: UUID
) -> dict[str, object]:
    # 角色资产不写 media_assets，权威事实来自 character_images；该行没有
    # deletion_state/purging 概念，快照只记录真实存在的字段。
    character = session.scalar(
        select(CharacterImageRow).where(
            CharacterImageRow.id == snapshot.character_image_id,
            CharacterImageRow.owner_id == owner_id,
            CharacterImageRow.asset_id == snapshot.asset_id,
        )
    )
    if character is None:
        raise DomainError("h3_inputs_not_ready", "Canonical H3 media is unavailable")
    return {
        "id": str(character.asset_id),
        "owner_id": str(character.owner_id),
        "project_id": None,
        "object_key": character.object_key,
        "sha256": character.sha256,
        "size_bytes": character.size_bytes,
        "media_type": character.media_type,
        "kind": "character_image",
    }


def _assert_composition_membership(
    sentence_by_id: dict[UUID, AuthoritativeNarrationSentenceRecord],
    sentence_ids: tuple[UUID, ...],
    snapshot_revision: int,
) -> None:
    if any(
        sentence_id not in sentence_by_id
        or sentence_by_id[sentence_id].snapshot_revision != snapshot_revision
        for sentence_id in sentence_ids
    ):
        raise DomainError(
            "composition_authority_invalid",
            "H3 group narration membership is not authoritative",
        )


def _assert_membership_ordinals(
    memberships: tuple[AuthoritativeNarrationMembershipRecord, ...],
) -> None:
    ordinals = tuple(row.ordinal for row in memberships)
    sentence_ids = tuple(row.sentence_id for row in memberships)
    if (
        not memberships
        or ordinals != tuple(range(1, len(memberships) + 1))
        or len(set(ordinals)) != len(ordinals)
        or len(set(sentence_ids)) != len(sentence_ids)
    ):
        raise DomainError("h3_inputs_not_ready", "Narration membership order is inconsistent")


def _assert_storyboard_identity(
    director_sentence_ids: list[str],
    member_ids: tuple[UUID, ...],
    persisted_sha256: str,
    canonical_sha256: str,
) -> None:
    if director_sentence_ids != [str(sentence_id) for sentence_id in member_ids]:
        raise DomainError("h3_inputs_not_ready", "Director narration membership is inconsistent")
    if not hmac.compare_digest(persisted_sha256, canonical_sha256):
        raise DomainError("h3_inputs_not_ready", "Storyboard request hash is inconsistent")


def _assert_storyboard_shot_ranges(
    inputs: Mapping[str, object],
    director_shots: tuple[DirectorShot, ...],
    shot_links: tuple[ShotSentenceLink, ...],
    sentence_ranges_ms: Mapping[UUID, tuple[int, int]],
) -> tuple[ShotTimeRange, ...]:
    """故事板组级镜头区间门禁（2026-10-16 起双算法自洽），返回已校验的持久化区间。

    inputs 无 shot_time_ranges 键（早期历史记录）→ 不校验，返回空元组；
    无 shot_time_ranges_algorithm 键 → 按 legacy min/max 重算比对；
    monotone-projection-v1 → 按单调计时投影重算比对；不符报 storyboard_timeline_drift。
    未知算法键与重算失败均为血统不一致（h3_inputs_not_ready），失败关闭。
    返回的区间与持久化值逐项相等（比对保证），供权威输入与编译侧同源复用。
    """
    stored_ranges = inputs.get("shot_time_ranges")
    if stored_ranges is None:
        return ()
    algorithm = inputs.get("shot_time_ranges_algorithm")
    try:
        if algorithm is None:
            expected_ranges = shot_time_ranges_ms(director_shots, shot_links, sentence_ranges_ms)
        elif algorithm == SHOT_TIMING_PROJECTION_V1:
            expected_ranges = projected_shot_time_ranges_ms(
                director_shots, shot_links, sentence_ranges_ms
            )
        else:
            raise DomainError("h3_inputs_not_ready", "Storyboard shot lineage is inconsistent")
    except ValueError as exc:
        raise DomainError(
            "h3_inputs_not_ready", "Storyboard shot lineage is inconsistent"
        ) from exc
    if [
        {
            "order": value.shot_order,
            "start_ms": value.start_ms,
            "end_ms": value.end_ms,
        }
        for value in expected_ranges
    ] != stored_ranges:
        raise DomainError(
            "storyboard_timeline_drift",
            "Storyboard imprint no longer matches the measured timeline",
        )
    return expected_ranges


def _assert_composition_sentence_timeline(
    sentences: tuple[AuthoritativeNarrationSentenceRecord, ...],
) -> None:
    frame_cursor = 0
    millisecond_cursor = 0
    previous_sequence = 0
    # 分隔句不进权威表但占用原始编号，sequence 合法形态是稀疏递增，只要求严格递增。
    for sentence in sentences:
        if sentence.sample_rate_hz <= 0:
            raise DomainError(
                "composition_authority_invalid", "Narration timeline is not authoritative"
            )
        expected_start_ms = round(
            Fraction(sentence.start_frame * 1000, sentence.sample_rate_hz)
        )
        expected_end_ms = round(Fraction(sentence.end_frame * 1000, sentence.sample_rate_hz))
        if (
            sentence.sequence <= previous_sequence
            or sentence.start_frame != frame_cursor
            or sentence.end_frame <= sentence.start_frame
            or sentence.start_ms != millisecond_cursor
            or sentence.start_ms != expected_start_ms
            or sentence.end_ms != expected_end_ms
            or sentence.measured_frame_count != sentence.end_frame - sentence.start_frame
            or sentence.measured_duration_ms
            != round(
                Fraction(sentence.measured_frame_count * 1000, sentence.sample_rate_hz)
            )
        ):
            raise DomainError(
                "composition_authority_invalid", "Narration timeline is not authoritative"
            )
        previous_sequence = sentence.sequence
        frame_cursor = sentence.end_frame
        millisecond_cursor = sentence.end_ms


def _storyboard_media_ref(
    session: Session,
    generation: StoryboardGenerationRow,
    media_link: StoryboardMediaRow,
    owner_id: UUID,
    project_id: UUID,
    group_id: UUID,
) -> MediaAssetRef:
    row = session.scalar(
        select(MediaAssetRow)
        .join(ProjectAssetReferenceRow, ProjectAssetReferenceRow.asset_id == MediaAssetRow.id)
        .where(
            MediaAssetRow.id == media_link.asset_id,
            MediaAssetRow.owner_id == owner_id,
            MediaAssetRow.kind == "STORYBOARD_GRID",
            MediaAssetRow.media_type.like("image/%"),
            MediaAssetRow.purging.is_(False),
            MediaAssetRow.deletion_state == "ACTIVE",
            ProjectAssetReferenceRow.project_id == project_id,
            ProjectAssetReferenceRow.owner_id == owner_id,
        )
    )
    object_fact = generation.checkpoint_json.get("object_asset")
    expected = object_fact if isinstance(object_fact, dict) else {}
    if (
        row is None
        or generation.owner_id != owner_id
        or generation.project_id != project_id
        or generation.group_id != group_id
        or generation.asset_id != media_link.asset_id
        or expected.get("asset_id") != str(media_link.asset_id)
        or expected.get("sha256") != row.sha256
        or expected.get("media_type") != row.media_type
        or expected.get("size_bytes") != row.size_bytes
        or generation.checkpoint_json.get("expected_sha256") != row.sha256
        or generation.checkpoint_json.get("downloaded_sha256") != row.sha256
        or generation.checkpoint_json.get("downloaded_size") != row.size_bytes
        or generation.checkpoint_json.get("qc_mime") != row.media_type
    ):
        raise DomainError("h3_inputs_not_ready", "Canonical storyboard media is unavailable")
    return MediaAssetRef(row.id, row.sha256, row.media_type, row.size_bytes)


class _Narration:
    def __init__(self, value: tuple[LatestNarrationSentence, ...]) -> None:
        self.value = value

    def timeline(self, _project_id: UUID, _owner_id: UUID) -> tuple[LatestNarrationSentence, ...]:
        return self.value


class _Groups:
    def __init__(self, value: tuple[LatestH3Group, ...]) -> None:
        self.value = value

    def current_groups(self, _project_id: UUID, _owner_id: UUID) -> tuple[LatestH3Group, ...]:
        return self.value


class _Snapshot:
    def __init__(self, value: CompositionProjectSnapshot) -> None:
        self.value = value

    def snapshot(self, _project_id: UUID, _owner_id: UUID) -> CompositionProjectSnapshot:
        return self.value
