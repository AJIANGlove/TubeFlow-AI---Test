from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import update

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.models import MediaAssetRow
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    AuthoritativeNarrationMembershipRecord,
    AuthoritativeNarrationSentenceRecord,
    NarrationAttemptRecord,
)
from tubeflow_test.adapters.persistence.production_authority import (
    SqlCanonicalCompositionInputs,
)
from tubeflow_test.video_generation.persistence import (
    GroupCurrentVideoRecord,
    VideoAttemptRecord,
)


@pytest.mark.integration
def test_real_postgres_composition_authority_loads_canonical_chain_and_rejects_pollution(
    postgres_database, storyboard_parent_chain
) -> None:
    ids, storyboard_generation_id = storyboard_parent_chain
    sentence_id, raw_id, normalized_id, video_id, attempt_id = (uuid4() for _ in range(5))
    raw_sha, normalized_sha, video_sha = "a" * 64, "b" * 64, "c" * 64
    sentence_text = "A complete PostgreSQL fixture."
    sentence_sha = hashlib.sha256(sentence_text.encode()).hexdigest()

    with postgres_database.sessions.begin() as session:
        project = session.get(ProjectRow, ids.project_id)
        assert project is not None
        project.snapshot_confirmed_at = datetime.now(UTC)
        project.snapshot_aspect_ratio = "16:9"
        project.snapshot_script = project.script
        session.add_all(
            (
                MediaAssetRow(
                    id=raw_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    kind="narration_raw",
                    object_key=f"narration/{raw_id}",
                    sha256=raw_sha,
                    media_type="audio/wav",
                    size_bytes=100,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=normalized_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    kind="narration_normalized",
                    object_key=f"narration/{normalized_id}",
                    sha256=normalized_sha,
                    media_type="audio/wav",
                    size_bytes=101,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=video_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    kind="H3_ORIGINAL_MP4",
                    object_key=f"h3/{video_id}",
                    sha256=video_sha,
                    media_type="video/mp4",
                    size_bytes=1024,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
            )
        )
        session.flush()
        session.add(
            AuthoritativeNarrationSentenceRecord(
                id=sentence_id,
                project_id=ids.project_id,
                owner_id=ids.owner_id,
                snapshot_sha256="d" * 64,
                snapshot_revision=1,
                sequence=1,
                text=sentence_text,
                text_sha256=sentence_sha,
                raw_asset_id=raw_id,
                normalized_asset_id=normalized_id,
                raw_qc_passed=True,
                normalized_qc_passed=True,
                measured_frame_count=48_000,
                sample_rate_hz=48_000,
                measured_duration_ms=1_000,
                start_frame=0,
                end_frame=48_000,
                start_ms=0,
                end_ms=1_000,
            )
        )
        session.flush()
        session.add(
            AuthoritativeNarrationMembershipRecord(
                group_id=ids.group_id, sentence_id=sentence_id, ordinal=1
            )
        )
        session.add(
            NarrationAttemptRecord(
                id=uuid4(),
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                idempotency_key="composition-authority-audio",
                request_sha256="f" * 64,
                revision=1,
                payload={
                    "attempt": {
                        "state": "COMPLETED",
                        "project_id": str(ids.project_id),
                        "owner_id": str(ids.owner_id),
                        "sentence_sequence": 1,
                        "idempotency_key": "composition-authority-audio",
                        "provider_call_id": "composition-provider-call",
                    },
                    "narration": {
                        "project_id": str(ids.project_id),
                        "owner_id": str(ids.owner_id),
                        "voice_version_id": str(uuid4()),
                        "locale": "en-US",
                        "provider_call_id": "composition-provider-call",
                        "reference_package_sha256": "9" * 64,
                        "idempotency_key": "composition-authority-audio",
                        "normalization_manifest": {"sample_rate_hz": 48_000},
                        "sentence": {
                            "sequence": 1,
                            "text": sentence_text,
                            "text_sha256": sentence_sha,
                        },
                        "raw_asset": {
                            "asset_id": str(raw_id),
                            "sha256": raw_sha,
                            "media_type": "audio/wav",
                            "size_bytes": 100,
                        },
                        "normalized_asset": {
                            "asset_id": str(normalized_id),
                            "sha256": normalized_sha,
                            "media_type": "audio/wav",
                            "size_bytes": 101,
                        },
                        "raw_qc": {"passed": True},
                        "normalized_qc": {
                            "passed": True,
                            "frame_count": 48_000,
                            "sample_rate_hz": 48_000,
                            "measured_duration_ms": 1_000,
                        },
                    },
                },
            )
        )
        session.add(
            VideoAttemptRecord(
                attempt_id=attempt_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                group_id=ids.group_id,
                generation_id=storyboard_generation_id,
                idempotency_key="composition-authority-video",
                generation_sha256="1" * 64,
                request_sha256="2" * 64,
                request_json={},
                stage="ARCHIVED",
                staged_ids=[],
                cleaned_staged_ids=[],
                archive_stage="COMPLETE",
                output_id="canonical-output",
                output_sha256=video_sha,
                output_size_bytes=1024,
                output_media_type="video/mp4",
                archived_asset_json={
                    "asset_id": str(video_id),
                    "sha256": video_sha,
                    "media_type": "video/mp4",
                    "size_bytes": 1024,
                },
                retry_evidence=[],
                intent_sequence=1,
                slot_claimed=False,
                probe_evidence_json={
                    "width": 1280,
                    "height": 720,
                    "qc_passed": True,
                },
                updated_at=datetime.now(UTC),
            )
        )
        session.flush()
        session.add(
            GroupCurrentVideoRecord(
                group_id=ids.group_id,
                attempt_id=attempt_id,
                generation_id=storyboard_generation_id,
                intent_sequence=1,
                updated_at=datetime.now(UTC),
            )
        )

    authority = SqlCanonicalCompositionInputs(postgres_database.sessions)
    request = authority.load(ids.project_id, ids.owner_id)
    assert request is not None
    assert [item.order for item in request.sentences] == [0]
    assert [item.order for item in request.groups] == [0]
    assert request.groups[0].narration_segments[0].audio_asset_id == str(normalized_id)
    assert request.groups[0].video_asset_id == str(video_id)
    assert authority.load(ids.project_id, uuid4()) is None

    with postgres_database.sessions.begin() as session:
        current = session.get(GroupCurrentVideoRecord, ids.group_id)
        assert current is not None
        current.intent_sequence = 2
    assert authority.load(ids.project_id, ids.owner_id) is None


@pytest.mark.integration
def test_composition_skips_blocked_group_and_compresses_timeline(
    postgres_database, storyboard_parent_chain
) -> None:
    ids, storyboard_generation_id = storyboard_parent_chain
    blocked_sentence_id, kept_sentence_id = uuid4(), uuid4()
    blocked_group_id = uuid4()
    raw_id, normalized_id, video_id, attempt_id = (uuid4() for _ in range(4))
    raw_sha, normalized_sha, video_sha = "a" * 64, "b" * 64, "c" * 64
    blocked_text = "First blocked long sentence."
    kept_text = "Second kept sentence."
    blocked_sha = hashlib.sha256(blocked_text.encode()).hexdigest()
    kept_sha = hashlib.sha256(kept_text.encode()).hexdigest()

    with postgres_database.sessions.begin() as session:
        project = session.get(ProjectRow, ids.project_id)
        assert project is not None
        project.snapshot_confirmed_at = datetime.now(UTC)
        project.snapshot_aspect_ratio = "16:9"
        project.snapshot_script = project.script
        session.add_all(
            (
                MediaAssetRow(
                    id=raw_id, owner_id=ids.owner_id, project_id=ids.project_id,
                    kind="narration_raw", object_key=f"narration/{raw_id}", sha256=raw_sha,
                    media_type="audio/wav", size_bytes=100, metadata_json={},
                    shared=False, purging=False, deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=normalized_id, owner_id=ids.owner_id, project_id=ids.project_id,
                    kind="narration_normalized", object_key=f"narration/{normalized_id}",
                    sha256=normalized_sha, media_type="audio/wav", size_bytes=101,
                    metadata_json={}, shared=False, purging=False, deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=video_id, owner_id=ids.owner_id, project_id=ids.project_id,
                    kind="H3_ORIGINAL_MP4", object_key=f"h3/{video_id}", sha256=video_sha,
                    media_type="video/mp4", size_bytes=1024, metadata_json={},
                    shared=False, purging=False, deletion_state="ACTIVE",
                ),
            )
        )
        session.flush()
        # 原始时间轴保持连续：句 1（被阻止组）0-48000，句 2（保留组）48000-96000。
        session.add_all(
            (
                AuthoritativeNarrationSentenceRecord(
                    id=blocked_sentence_id, project_id=ids.project_id, owner_id=ids.owner_id,
                    snapshot_sha256="d" * 64, snapshot_revision=1, sequence=1,
                    text=blocked_text, text_sha256=blocked_sha,
                    raw_asset_id=raw_id, normalized_asset_id=normalized_id,
                    raw_qc_passed=True, normalized_qc_passed=True,
                    measured_frame_count=816_000, sample_rate_hz=48_000,
                    measured_duration_ms=17_000,
                    start_frame=0, end_frame=816_000, start_ms=0, end_ms=17_000,
                ),
                AuthoritativeNarrationSentenceRecord(
                    id=kept_sentence_id, project_id=ids.project_id, owner_id=ids.owner_id,
                    snapshot_sha256="d" * 64, snapshot_revision=1, sequence=2,
                    text=kept_text, text_sha256=kept_sha,
                    raw_asset_id=raw_id, normalized_asset_id=normalized_id,
                    raw_qc_passed=True, normalized_qc_passed=True,
                    measured_frame_count=48_000, sample_rate_hz=48_000,
                    measured_duration_ms=1_000,
                    start_frame=816_000, end_frame=864_000, start_ms=17_000, end_ms=18_000,
                ),
            )
        )
        session.flush()
        session.execute(
            update(AuthoritativeNarrationGroupRecord)
            .where(AuthoritativeNarrationGroupRecord.id == ids.group_id)
            .values(sequence=2)
        )
        session.add(
            AuthoritativeNarrationGroupRecord(
                id=blocked_group_id, project_id=ids.project_id, owner_id=ids.owner_id,
                snapshot_sha256="d" * 64, snapshot_revision=1, sequence=1,
                start_frame=0, end_frame=816_000, sample_rate_hz=48_000,
                start_ms=0, end_ms=17_000,
                video_blocked=True, block_reason="single_sentence_exceeds_15000ms",
            )
        )
        # 既有夹具分组作为保留组（其句子改为指向保留句）
        session.add(
            AuthoritativeNarrationMembershipRecord(
                group_id=blocked_group_id, sentence_id=blocked_sentence_id, ordinal=1
            )
        )
        session.add(
            AuthoritativeNarrationMembershipRecord(
                group_id=ids.group_id, sentence_id=kept_sentence_id, ordinal=1
            )
        )
        session.add(
            NarrationAttemptRecord(
                id=uuid4(), owner_id=ids.owner_id, project_id=ids.project_id,
                idempotency_key="composition-skip-audio", request_sha256="f" * 64, revision=1,
                payload={
                    "attempt": {
                        "state": "COMPLETED", "project_id": str(ids.project_id),
                        "owner_id": str(ids.owner_id), "sentence_sequence": 2,
                        "idempotency_key": "composition-skip-audio",
                        "provider_call_id": "composition-provider-call",
                    },
                    "narration": {
                        "project_id": str(ids.project_id), "owner_id": str(ids.owner_id),
                        "voice_version_id": str(uuid4()), "locale": "en-US",
                        "provider_call_id": "composition-provider-call",
                        "reference_package_sha256": "9" * 64,
                        "idempotency_key": "composition-skip-audio",
                        "normalization_manifest": {"sample_rate_hz": 48_000},
                        "sentence": {"sequence": 2, "text": kept_text, "text_sha256": kept_sha},
                        "raw_asset": {
                            "asset_id": str(raw_id), "sha256": raw_sha,
                            "media_type": "audio/wav", "size_bytes": 100,
                        },
                        "normalized_asset": {
                            "asset_id": str(normalized_id), "sha256": normalized_sha,
                            "media_type": "audio/wav", "size_bytes": 101,
                        },
                        "raw_qc": {"passed": True},
                        "normalized_qc": {
                            "passed": True, "frame_count": 48_000,
                            "sample_rate_hz": 48_000, "measured_duration_ms": 1_000,
                        },
                    },
                },
            )
        )
        session.add(
            VideoAttemptRecord(
                attempt_id=attempt_id, owner_id=ids.owner_id, project_id=ids.project_id,
                group_id=ids.group_id, generation_id=storyboard_generation_id,
                idempotency_key="composition-skip-video",
                generation_sha256="1" * 64, request_sha256="2" * 64, request_json={},
                stage="ARCHIVED", staged_ids=[], cleaned_staged_ids=[],
                archive_stage="COMPLETE", output_id="canonical-output",
                output_sha256=video_sha, output_size_bytes=1024, output_media_type="video/mp4",
                archived_asset_json={
                    "asset_id": str(video_id), "sha256": video_sha,
                    "media_type": "video/mp4", "size_bytes": 1024,
                },
                retry_evidence=[], intent_sequence=1, slot_claimed=False,
                probe_evidence_json={"width": 1280, "height": 720, "qc_passed": True},
                updated_at=datetime.now(UTC),
            )
        )
        session.flush()
        session.add(
            GroupCurrentVideoRecord(
                group_id=ids.group_id, attempt_id=attempt_id,
                generation_id=storyboard_generation_id, intent_sequence=1,
                updated_at=datetime.now(UTC),
            )
        )

    authority = SqlCanonicalCompositionInputs(postgres_database.sessions)
    request = authority.load(ids.project_id, ids.owner_id)

    assert request is not None
    assert [group.order for group in request.groups] == [0]
    assert request.groups[0].video_asset_id == str(video_id)
    assert [sentence.order for sentence in request.sentences] == [0]
    sentence = request.sentences[0]
    assert sentence.text == kept_text
    # 保留句从 0 开始连续重排（原始时间轴中为 1000-2000ms）
    assert sentence.start_ms == 0 and sentence.end_ms == 1_000
    assert request.groups[0].narration_segments[0].audio_asset_id == str(normalized_id)
    assert request.target_duration_ms == 1_000


@pytest.mark.integration
def test_composition_loads_defect_delivered_video_and_still_rejects_tampering(
    postgres_database, storyboard_parent_chain
) -> None:
    # 带病交付（内容级 QC 梯子耗尽 + deliver_with_defect 标记）的当前组视频可装载；
    # 篡改归档 sha256 后仍拒绝装载（放行不豁免完整性校验）。
    ids, storyboard_generation_id = storyboard_parent_chain
    sentence_id, raw_id, normalized_id, video_id, attempt_id = (uuid4() for _ in range(5))
    raw_sha, normalized_sha, video_sha = "a" * 64, "b" * 64, "c" * 64
    sentence_text = "A defect delivered PostgreSQL fixture."
    sentence_sha = hashlib.sha256(sentence_text.encode()).hexdigest()

    with postgres_database.sessions.begin() as session:
        project = session.get(ProjectRow, ids.project_id)
        assert project is not None
        project.snapshot_confirmed_at = datetime.now(UTC)
        project.snapshot_aspect_ratio = "16:9"
        project.snapshot_script = project.script
        session.add_all(
            (
                MediaAssetRow(
                    id=raw_id, owner_id=ids.owner_id, project_id=ids.project_id,
                    kind="narration_raw", object_key=f"narration/{raw_id}", sha256=raw_sha,
                    media_type="audio/wav", size_bytes=100, metadata_json={},
                    shared=False, purging=False, deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=normalized_id, owner_id=ids.owner_id, project_id=ids.project_id,
                    kind="narration_normalized", object_key=f"narration/{normalized_id}",
                    sha256=normalized_sha, media_type="audio/wav", size_bytes=101,
                    metadata_json={}, shared=False, purging=False, deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=video_id, owner_id=ids.owner_id, project_id=ids.project_id,
                    kind="H3_ORIGINAL_MP4", object_key=f"h3/{video_id}", sha256=video_sha,
                    media_type="video/mp4", size_bytes=1024, metadata_json={},
                    shared=False, purging=False, deletion_state="ACTIVE",
                ),
            )
        )
        session.flush()
        session.add(
            AuthoritativeNarrationSentenceRecord(
                id=sentence_id, project_id=ids.project_id, owner_id=ids.owner_id,
                snapshot_sha256="d" * 64, snapshot_revision=1, sequence=1,
                text=sentence_text, text_sha256=sentence_sha,
                raw_asset_id=raw_id, normalized_asset_id=normalized_id,
                raw_qc_passed=True, normalized_qc_passed=True,
                measured_frame_count=48_000, sample_rate_hz=48_000,
                measured_duration_ms=1_000, start_frame=0, end_frame=48_000,
                start_ms=0, end_ms=1_000,
            )
        )
        session.flush()
        session.add(
            AuthoritativeNarrationMembershipRecord(
                group_id=ids.group_id, sentence_id=sentence_id, ordinal=1
            )
        )
        session.add(
            NarrationAttemptRecord(
                id=uuid4(), owner_id=ids.owner_id, project_id=ids.project_id,
                idempotency_key="composition-defect-audio", request_sha256="f" * 64, revision=1,
                payload={
                    "attempt": {
                        "state": "COMPLETED", "project_id": str(ids.project_id),
                        "owner_id": str(ids.owner_id), "sentence_sequence": 1,
                        "idempotency_key": "composition-defect-audio",
                        "provider_call_id": "composition-provider-call",
                    },
                    "narration": {
                        "project_id": str(ids.project_id), "owner_id": str(ids.owner_id),
                        "voice_version_id": str(uuid4()), "locale": "en-US",
                        "provider_call_id": "composition-provider-call",
                        "reference_package_sha256": "9" * 64,
                        "idempotency_key": "composition-defect-audio",
                        "normalization_manifest": {"sample_rate_hz": 48_000},
                        "sentence": {
                            "sequence": 1, "text": sentence_text, "text_sha256": sentence_sha,
                        },
                        "raw_asset": {
                            "asset_id": str(raw_id), "sha256": raw_sha,
                            "media_type": "audio/wav", "size_bytes": 100,
                        },
                        "normalized_asset": {
                            "asset_id": str(normalized_id), "sha256": normalized_sha,
                            "media_type": "audio/wav", "size_bytes": 101,
                        },
                        "raw_qc": {"passed": True},
                        "normalized_qc": {
                            "passed": True, "frame_count": 48_000,
                            "sample_rate_hz": 48_000, "measured_duration_ms": 1_000,
                        },
                    },
                },
            )
        )
        session.add(
            VideoAttemptRecord(
                attempt_id=attempt_id, owner_id=ids.owner_id, project_id=ids.project_id,
                group_id=ids.group_id, generation_id=storyboard_generation_id,
                idempotency_key="composition-defect-video",
                generation_sha256="1" * 64, request_sha256="2" * 64, request_json={},
                stage="ARCHIVED", staged_ids=[], cleaned_staged_ids=[],
                archive_stage="COMPLETE", output_id="defect-output",
                output_sha256=video_sha, output_size_bytes=1024, output_media_type="video/mp4",
                archived_asset_json={
                    "asset_id": str(video_id), "sha256": video_sha,
                    "media_type": "video/mp4", "size_bytes": 1024,
                },
                retry_evidence=["deliver_with_defect:video_content_black_region"],
                intent_sequence=1, slot_claimed=False,
                probe_evidence_json={
                    "width": 1280, "height": 720, "qc_passed": False,
                    "error_code": "video_content_black_region",
                    "content_qc": {
                        "algorithm": "sustained_black_region_v1",
                        "findings": [
                            {
                                "start_second": 0.0, "end_second": 4.0,
                                "bbox_columns": [0, 1279], "bbox_rows": [0, 719],
                                "area_fraction": 0.97,
                            }
                        ],
                    },
                },
                updated_at=datetime.now(UTC),
            )
        )
        session.flush()
        session.add(
            GroupCurrentVideoRecord(
                group_id=ids.group_id, attempt_id=attempt_id,
                generation_id=storyboard_generation_id, intent_sequence=1,
                updated_at=datetime.now(UTC),
            )
        )

    authority = SqlCanonicalCompositionInputs(postgres_database.sessions)
    request = authority.load(ids.project_id, ids.owner_id)
    assert request is not None
    assert request.groups[0].video_asset_id == str(video_id)

    with postgres_database.sessions.begin() as session:
        attempt = session.get(VideoAttemptRecord, attempt_id)
        assert attempt is not None
        attempt.archived_asset_json = {
            **(attempt.archived_asset_json or {}),
            "sha256": "e" * 64,
        }
    assert authority.load(ids.project_id, ids.owner_id) is None
