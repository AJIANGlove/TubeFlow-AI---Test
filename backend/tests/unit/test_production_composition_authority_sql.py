from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from fractions import Fraction
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence import production_authority as authority_module
from tubeflow_test.adapters.persistence.identity_project.models import (
    CharacterImageRow,
    ProjectSnapshotCharacterRow,
)
from tubeflow_test.adapters.persistence.models import MediaAssetRow
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationMembershipRecord,
    AuthoritativeNarrationSentenceRecord,
    NarrationAttemptRecord,
)
from tubeflow_test.adapters.persistence.production_authority import (
    SqlCanonicalCompositionInputs,
    _assert_composition_membership,
    _assert_composition_sentence_timeline,
    _assert_membership_ordinals,
    _assert_storyboard_identity,
    _character_media_ref,
    _composition_audio_asset,
    _composition_video_asset,
    _narration_audio_lineage,
)
from tubeflow_test.domain.common import MediaAssetRef, SupportedLocale
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.narration import TimelineEntry, group_timeline, segment_pasted_script
from tubeflow_test.video_generation.orchestration import DELIVER_WITH_DEFECT_MARKER
from tubeflow_test.video_generation.persistence import (
    GroupCurrentVideoRecord,
    VideoAttemptRecord,
)


def test_h3_membership_ordinals_require_one_based_unique_contiguous_order() -> None:
    group_id = uuid4()

    def memberships(ordinals: tuple[int, ...]):
        return tuple(
            AuthoritativeNarrationMembershipRecord(
                group_id=group_id, sentence_id=uuid4(), ordinal=ordinal
            )
            for ordinal in ordinals
        )

    _assert_membership_ordinals(memberships((1, 2)))
    for invalid in ((5, 6), (1, 3), (1, 1), (0, 1)):
        with pytest.raises(DomainError, match="membership order"):
            _assert_membership_ordinals(memberships(invalid))

    duplicate_sentence = uuid4()
    with pytest.raises(DomainError, match="membership order"):
        _assert_membership_ordinals(
            (
                AuthoritativeNarrationMembershipRecord(
                    group_id=group_id, sentence_id=duplicate_sentence, ordinal=1
                ),
                AuthoritativeNarrationMembershipRecord(
                    group_id=group_id, sentence_id=duplicate_sentence, ordinal=2
                ),
            )
        )


def _timeline_sentence(
    sequence: int, start_frame: int, span: int, rate: int = 48000
) -> AuthoritativeNarrationSentenceRecord:
    end_frame = start_frame + span
    return AuthoritativeNarrationSentenceRecord(
        id=uuid4(),
        project_id=uuid4(),
        owner_id=uuid4(),
        snapshot_sha256="a" * 64,
        snapshot_revision=1,
        sequence=sequence,
        text="sentence",
        text_sha256="b" * 64,
        raw_asset_id=uuid4(),
        normalized_asset_id=uuid4(),
        raw_qc_passed=True,
        normalized_qc_passed=True,
        measured_frame_count=span,
        sample_rate_hz=rate,
        measured_duration_ms=round(Fraction(span * 1000, rate)),
        start_frame=start_frame,
        end_frame=end_frame,
        start_ms=round(Fraction(start_frame * 1000, rate)),
        end_ms=round(Fraction(end_frame * 1000, rate)),
    )


def test_composition_timeline_accepts_sparse_increasing_sequences_from_separators() -> None:
    sentences = (
        _timeline_sentence(1, 0, 48000),
        _timeline_sentence(3, 48000, 48000),  # sequence 2 被分隔句占用，稀疏递增合法
        _timeline_sentence(5, 96000, 24000),
    )

    _assert_composition_sentence_timeline(sentences)


def test_composition_timeline_rejects_unordered_sequences_and_frame_gaps() -> None:
    with pytest.raises(DomainError, match="timeline"):
        _assert_composition_sentence_timeline(
            (_timeline_sentence(3, 0, 48000), _timeline_sentence(1, 48000, 48000))
        )
    with pytest.raises(DomainError, match="timeline"):
        _assert_composition_sentence_timeline(
            (_timeline_sentence(1, 0, 48000), _timeline_sentence(2, 96000, 48000))
        )


def test_h3_storyboard_identity_binds_hash_and_ordered_group_membership() -> None:
    first, second = uuid4(), uuid4()
    _assert_storyboard_identity([str(first), str(second)], (first, second), "a" * 64, "a" * 64)
    with pytest.raises(DomainError, match="membership"):
        _assert_storyboard_identity(
            [str(second), str(first)], (first, second), "a" * 64, "a" * 64
        )
    with pytest.raises(DomainError, match="request hash"):
        _assert_storyboard_identity(
            [str(first), str(second)], (first, second), "a" * 64, "b" * 64
        )



def _video_session():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (
        MediaAssetRow.__table__,
        VideoAttemptRecord.__table__,
        GroupCurrentVideoRecord.__table__,
    ):
        table.create(engine)
    session = Session(engine)
    owner_id, project_id, group_id, generation_id, attempt_id, asset_id = (
        uuid4() for _ in range(6)
    )
    digest = "a" * 64
    attempt = VideoAttemptRecord(
        attempt_id=attempt_id,
        owner_id=owner_id,
        project_id=project_id,
        group_id=group_id,
        generation_id=generation_id,
        idempotency_key="archived",
        generation_sha256="b" * 64,
        request_sha256="c" * 64,
        request_json={},
        stage="ARCHIVED",
        staged_ids=[],
        cleaned_staged_ids=[],
        archive_stage="COMPLETE",
        output_id="output",
        output_sha256=digest,
        output_size_bytes=321,
        output_media_type="video/mp4",
        archived_asset_json={
            "asset_id": str(asset_id),
            "sha256": digest,
            "media_type": "video/mp4",
            "size_bytes": 321,
        },
        retry_evidence=[],
        intent_sequence=1,
        slot_claimed=False,
        probe_evidence_json={"width": 1920, "height": 1080, "qc_passed": True},
        updated_at=datetime.now(UTC),
    )
    current = GroupCurrentVideoRecord(
        group_id=group_id,
        attempt_id=attempt_id,
        generation_id=generation_id,
        intent_sequence=1,
        updated_at=datetime.now(UTC),
    )
    asset = MediaAssetRow(
        id=asset_id,
        owner_id=owner_id,
        project_id=project_id,
        kind="H3_ORIGINAL_MP4",
        object_key=f"h3/{asset_id}",
        sha256=digest,
        media_type="video/mp4",
        size_bytes=321,
        metadata_json={},
        shared=False,
        purging=False,
        deletion_state="ACTIVE",
    )
    session.add_all((attempt, current, asset))
    session.commit()
    return session, current, attempt, asset, owner_id, project_id, group_id


def test_composition_video_authority_accepts_fully_bound_asset() -> None:
    session, current, attempt, asset, owner_id, project_id, group_id = _video_session()
    with session:
        assert (
            _composition_video_asset(
                session, current, attempt, group_id, owner_id, project_id
            )
            is asset
        )


@pytest.mark.parametrize(
    "invalid",
    (
        "cross_owner",
        "wrong_project",
        "wrong_group",
        "wrong_generation",
        "bad_hash",
        "deleting",
        "archive_incomplete",
        "qc_failed",
        "wrong_kind",
        "intent_mismatch",
    ),
)
def test_composition_video_authority_rejects_polluted_chain(invalid: str) -> None:
    session, current, attempt, asset, owner_id, project_id, group_id = _video_session()
    with session:
        if invalid == "cross_owner":
            attempt.owner_id = uuid4()
        elif invalid == "wrong_project":
            attempt.project_id = uuid4()
        elif invalid == "wrong_group":
            attempt.group_id = uuid4()
        elif invalid == "wrong_generation":
            current.generation_id = uuid4()
        elif invalid == "bad_hash":
            attempt.archived_asset_json = {
                **(attempt.archived_asset_json or {}),
                "sha256": "d" * 64,
            }
        elif invalid == "deleting":
            asset.deletion_state = "DELETING"
        elif invalid == "archive_incomplete":
            attempt.archive_stage = "CONFIRMED"
        elif invalid == "qc_failed":
            attempt.probe_evidence_json = {"width": 1920, "height": 1080, "qc_passed": False}
        elif invalid == "wrong_kind":
            asset.kind = "composition_mp4"
        else:
            current.intent_sequence = attempt.intent_sequence + 1
        session.commit()
        assert (
            _composition_video_asset(
                session, current, attempt, group_id, owner_id, project_id
            )
            is None
        )


def _defect_probe_evidence() -> dict[str, object]:
    """带病交付的 probe 证据形态：qc_passed False + 内容级 QC 块（黑屏 findings）。"""
    return {
        "width": 1920,
        "height": 1080,
        "qc_passed": False,
        "error_code": "video_content_black_region",
        "content_qc": {
            "algorithm": "sustained_black_region_v1",
            "findings": [
                {
                    "start_second": 0.0,
                    "end_second": 4.0,
                    "bbox_columns": [0, 1919],
                    "bbox_rows": [0, 1079],
                    "area_fraction": 0.97,
                }
            ],
        },
    }


def test_composition_video_authority_accepts_defect_delivered_asset() -> None:
    # 带病交付（QC 梯子耗尽 + deliver_with_defect 标记）的当前视频可装载进合成。
    session, current, attempt, asset, owner_id, project_id, group_id = _video_session()
    with session:
        attempt.probe_evidence_json = _defect_probe_evidence()
        attempt.retry_evidence = [DELIVER_WITH_DEFECT_MARKER]
        session.commit()
        assert (
            _composition_video_asset(
                session, current, attempt, group_id, owner_id, project_id
            )
            is asset
        )


def test_composition_video_authority_rejects_unmarked_content_qc_failure() -> None:
    # 同样的缺陷证据但没有交付标记：不放行（带病交付只能由编排层标记产生）。
    session, current, attempt, _asset, owner_id, project_id, group_id = _video_session()
    with session:
        attempt.probe_evidence_json = _defect_probe_evidence()
        session.commit()
        assert (
            _composition_video_asset(
                session, current, attempt, group_id, owner_id, project_id
            )
            is None
        )


@pytest.mark.parametrize(
    "invalid",
    ("bad_archived_hash", "bad_output_hash", "bad_size", "unlinked_asset"),
)
def test_composition_video_authority_defect_delivery_keeps_integrity_gates(
    invalid: str,
) -> None:
    # 放行不豁免完整性：篡改归档 sha256、输出 sha256、尺寸或资产归属仍拒绝装载。
    session, current, attempt, asset, owner_id, project_id, group_id = _video_session()
    with session:
        attempt.probe_evidence_json = _defect_probe_evidence()
        attempt.retry_evidence = [DELIVER_WITH_DEFECT_MARKER]
        if invalid == "bad_archived_hash":
            attempt.archived_asset_json = {
                **(attempt.archived_asset_json or {}),
                "sha256": "d" * 64,
            }
        elif invalid == "bad_output_hash":
            attempt.output_sha256 = "e" * 64
        elif invalid == "bad_size":
            attempt.output_size_bytes = 322
        else:
            asset.owner_id = uuid4()
        session.commit()
        assert (
            _composition_video_asset(
                session, current, attempt, group_id, owner_id, project_id
            )
            is None
        )


def _audio_session(attempt_state: str = "COMPLETED"):  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MediaAssetRow.__table__.create(engine)
    AuthoritativeNarrationSentenceRecord.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE narration_attempt_records "
                "(id CHAR(32) PRIMARY KEY, owner_id CHAR(32), project_id CHAR(32), "
                "idempotency_key VARCHAR(128), request_sha256 VARCHAR(64), "
                "provider_request_id TEXT, "
                "revision INTEGER, payload JSON)"
            )
        )
    session = Session(engine)
    owner_id, project_id, asset_id, raw_asset_id, sentence_id = (uuid4() for _ in range(5))
    digest = "e" * 64
    asset = MediaAssetRow(
        id=asset_id,
        owner_id=owner_id,
        project_id=project_id,
        kind="narration_normalized",
        object_key=f"audio/{asset_id}",
        sha256=digest,
        media_type="audio/wav",
        size_bytes=456,
        metadata_json={},
        shared=False,
        purging=False,
        deletion_state="ACTIVE",
    )
    raw_asset = MediaAssetRow(
        id=raw_asset_id,
        owner_id=owner_id,
        project_id=project_id,
        kind="narration_raw",
        object_key=f"audio/{raw_asset_id}",
        sha256="d" * 64,
        media_type="audio/wav",
        size_bytes=455,
        metadata_json={},
        shared=False,
        purging=False,
        deletion_state="ACTIVE",
    )
    sentence = AuthoritativeNarrationSentenceRecord(
        id=sentence_id,
        project_id=project_id,
        owner_id=owner_id,
        snapshot_sha256="f" * 64,
        snapshot_revision=1,
        sequence=1,
        text="hello",
        text_sha256="1" * 64,
        raw_asset_id=raw_asset_id,
        normalized_asset_id=asset_id,
        raw_qc_passed=True,
        normalized_qc_passed=True,
        measured_frame_count=48000,
        sample_rate_hz=48000,
        measured_duration_ms=1000,
        start_frame=0,
        end_frame=48000,
        start_ms=0,
        end_ms=1000,
    )
    session.add_all((asset, raw_asset, sentence))
    session.flush()
    payload = {
        "narration": {
            "project_id": str(project_id),
            "owner_id": str(owner_id),
            "voice_version_id": str(uuid4()),
            "locale": "en-US",
            "provider_call_id": "call-1",
            "reference_package_sha256": "9" * 64,
            "idempotency_key": "audio",
            "normalization_manifest": {"normalizer": "test"},
            "sentence": {"sequence": 1, "text": "hello", "text_sha256": "1" * 64},
            "raw_asset": {
                "asset_id": str(raw_asset_id),
                "sha256": "d" * 64,
                "media_type": "audio/wav",
                "size_bytes": 455,
            },
            "normalized_asset": {
                "asset_id": str(asset_id),
                "sha256": digest,
                "media_type": "audio/wav",
                "size_bytes": 456,
            },
            "raw_qc": {"passed": True},
            "normalized_qc": {
                "passed": True,
                "frame_count": 48000,
                "sample_rate_hz": 48000,
                "measured_duration_ms": 1000,
            },
        },
        "attempt": {
            "state": attempt_state,
            "owner_id": str(owner_id),
            "project_id": str(project_id),
            "sentence_sequence": 1,
            "idempotency_key": "audio",
            "provider_call_id": "call-1",
        },
    }
    session.execute(
        text(
            "INSERT INTO narration_attempt_records "
            "(id, owner_id, project_id, idempotency_key, request_sha256, revision, payload) "
            "VALUES (:id, :owner, :project, 'audio', :hash, 1, :payload)"
        ),
        {
            "id": uuid4().hex,
            "owner": owner_id.hex,
            "project": project_id.hex,
            "hash": "2" * 64,
            "payload": json.dumps(payload),
        },
    )
    session.commit()
    return session, sentence, asset, owner_id, project_id


_EL_SOURCE_SHA = "9" * 64


def _el_audio_session(**narration_overrides: object):  # type: ignore[no-untyped-def]
    """EL 快照场景（§23.1）：voice_version_id 置空，其余字段按需覆盖。"""

    session, sentence, asset, owner_id, project_id = _audio_session()
    row = session.scalar(select(NarrationAttemptRecord))
    assert row is not None
    payload = dict(row.payload)
    narration = dict(payload["narration"])
    narration["voice_version_id"] = None
    for key, value in narration_overrides.items():
        narration[key] = value
    payload["narration"] = narration
    row.payload = payload
    session.commit()
    return session, sentence, asset, owner_id, project_id


def test_composition_audio_authority_accepts_canonical_facts() -> None:
    session, sentence, asset, owner_id, project_id = _audio_session()
    with session:
        result = _composition_audio_asset(session, sentence, owner_id, project_id)
        assert result.asset_id == asset.id


@pytest.mark.parametrize("state", ("SUCCEEDED", "SUBMISSION_STARTED", "FAILED"))
def test_composition_audio_authority_rejects_noncompleted_attempt(state: str) -> None:
    session, sentence, _asset, owner_id, project_id = _audio_session(state)
    with session, pytest.raises(DomainError, match="Authoritative narration audio"):
        _composition_audio_asset(session, sentence, owner_id, project_id)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("state", "SUCCEEDED"),
        ("sentence_sequence", 2),
        ("provider_call_id", "other-call"),
        ("normalized_frame_count", 47_999),
        ("normalized_sample_rate", 24_000),
    ),
)
def test_composition_audio_authority_rejects_noncanonical_attempt_lineage(
    field: str, value: object
) -> None:
    session, sentence, _asset, owner_id, project_id = _audio_session()
    with session:
        row = session.scalar(select(NarrationAttemptRecord))
        assert row is not None
        payload = dict(row.payload)
        attempt = dict(payload["attempt"])
        narration = dict(payload["narration"])
        normalized_qc = dict(narration["normalized_qc"])
        if field == "normalized_frame_count":
            normalized_qc["frame_count"] = value
        elif field == "normalized_sample_rate":
            normalized_qc["sample_rate_hz"] = value
        else:
            attempt[field] = value
        narration["normalized_qc"] = normalized_qc
        payload["attempt"] = attempt
        payload["narration"] = narration
        row.payload = payload
        session.commit()

        with pytest.raises(DomainError, match="Authoritative narration audio"):
            _composition_audio_asset(session, sentence, owner_id, project_id)


@pytest.mark.parametrize(
    "invalid", ("cross_owner", "wrong_project", "deleting", "bad_hash")
)
def test_composition_audio_authority_rejects_polluted_facts(invalid: str) -> None:
    session, sentence, asset, owner_id, project_id = _audio_session()
    with session:
        if invalid == "cross_owner":
            asset.owner_id = uuid4()
        elif invalid == "wrong_project":
            asset.project_id = uuid4()
        elif invalid == "deleting":
            asset.deletion_state = "DELETING"
        else:
            asset.sha256 = "3" * 64
        session.commit()
        with pytest.raises(DomainError, match="Authoritative narration audio"):
            _composition_audio_asset(session, sentence, owner_id, project_id)


def test_composition_audio_authority_accepts_el_cloned_voice_evidence() -> None:
    session, sentence, asset, owner_id, project_id = _el_audio_session()
    with session:
        result = _composition_audio_asset(
            session,
            sentence,
            owner_id,
            project_id,
            el_cloned_voice_source_sha256=_EL_SOURCE_SHA,
        )
        assert result.asset_id == asset.id


def test_narration_audio_lineage_accepts_el_evidence_with_video_expected_arguments() -> None:
    session, sentence, asset, owner_id, project_id = _el_audio_session()
    with session:
        result = _narration_audio_lineage(
            session,
            sentence,
            owner_id,
            project_id,
            None,  # EL 项目没有公共音色（§23.1）
            "en-US",
            None,
            el_cloned_voice_source_sha256=_EL_SOURCE_SHA,
        )
        assert result[1].asset_id == asset.id


@pytest.mark.parametrize(
    ("override_key", "override_value"),
    (
        ("voice_version_id", "6f0e2e51-0000-0000-0000-000000000000"),
        ("reference_package_sha256", "8" * 64),
        ("reference_package_sha256", None),
    ),
)
def test_composition_audio_authority_rejects_el_evidence_mismatch(
    override_key: str, override_value: object
) -> None:
    session, sentence, _asset, owner_id, project_id = _el_audio_session(
        **{override_key: override_value}
    )
    with session, pytest.raises(DomainError, match="Authoritative narration audio"):
        _composition_audio_asset(
            session,
            sentence,
            owner_id,
            project_id,
            el_cloned_voice_source_sha256=_EL_SOURCE_SHA,
        )


def test_composition_audio_authority_el_without_evidence_context_fails_closed() -> None:
    # 未提供快照克隆音色源（无法完成等价校验）时维持既有口径：EL 快照必须被拒绝。
    session, sentence, _asset, owner_id, project_id = _el_audio_session()
    with session, pytest.raises(DomainError, match="Authoritative narration audio"):
        _composition_audio_asset(
            session,
            sentence,
            owner_id,
            project_id,
            el_cloned_voice_source_sha256=None,
        )


@pytest.mark.parametrize("invalid", ("missing", "cross_revision"))
def test_composition_membership_fails_closed(invalid: str) -> None:
    session, sentence, _asset, _owner_id, _project_id = _audio_session()
    with session:
        sentence_id = sentence.id
        sentence_by_id = {} if invalid == "missing" else {sentence_id: sentence}
        if invalid == "cross_revision":
            sentence.snapshot_revision = 2
        with pytest.raises(DomainError, match="membership is not authoritative"):
            _assert_composition_membership(sentence_by_id, (sentence_id,), 1)


def _character_session():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (
        CharacterImageRow.__table__,
        ProjectSnapshotCharacterRow.__table__,
    ):
        table.create(engine)
    session = Session(engine)
    owner_id, project_id, image_id, asset_id = (uuid4() for _ in range(4))
    digest = "4" * 64
    image = CharacterImageRow(
        id=image_id,
        owner_id=owner_id,
        channel_id=uuid4(),
        asset_id=asset_id,
        object_key=f"characters/{asset_id}",
        sha256=digest,
        media_type="image/png",
        size_bytes=789,
        width=512,
        height=512,
    )
    snapshot = ProjectSnapshotCharacterRow(
        id=uuid4(),
        project_id=project_id,
        ordinal=0,
        character_image_id=image_id,
        asset_id=asset_id,
        sha256=digest,
        media_type="image/png",
        size_bytes=789,
    )
    session.add_all((image, snapshot))
    session.commit()
    return session, snapshot, image, owner_id, project_id


def test_character_authority_accepts_fully_bound_snapshot() -> None:
    session, snapshot, image, owner_id, project_id = _character_session()
    with session:
        assert (
            _character_media_ref(session, snapshot, owner_id, project_id).asset_id
            == image.asset_id
        )


@pytest.mark.parametrize(
    "invalid",
    ("cross_owner", "wrong_binding", "bad_facts", "non_image"),
)
def test_character_authority_rejects_polluted_chain(invalid: str) -> None:
    session, snapshot, image, owner_id, project_id = _character_session()
    with session:
        if invalid == "cross_owner":
            image.owner_id = uuid4()
        elif invalid == "wrong_binding":
            image.asset_id = uuid4()
        elif invalid == "non_image":
            image.media_type = "video/mp4"
            snapshot.media_type = "video/mp4"
        else:
            snapshot.sha256 = "5" * 64
        session.commit()
        with pytest.raises(DomainError, match="Character snapshot asset"):
            _character_media_ref(session, snapshot, owner_id, project_id)


def _load_numbered_domain_timeline(  # type: ignore[no-untyped-def]
    monkeypatch, invalid: str | None = None, *, fractional: bool = False
):
    owner_id, project_id = uuid4(), uuid4()
    script = segment_pasted_script(
        "Hello world. Another sentence.", SupportedLocale.EN_US
    )
    frames_per_sentence = 48024 if fractional else 768000
    timeline = tuple(
        TimelineEntry(
            sentence.sequence,
            round(Fraction((sentence.sequence - 1) * frames_per_sentence * 1000, 48000)),
            round(Fraction(sentence.sequence * frames_per_sentence * 1000, 48000)),
            str(uuid4()),
            (sentence.sequence - 1) * frames_per_sentence,
            sentence.sequence * frames_per_sentence,
            48000,
        )
        for sentence in script.sentences
    )
    domain_groups = group_timeline(timeline)
    sentence_rows = [
        SimpleNamespace(
            id=uuid4(),
            sequence=sentence.sequence,
            text=sentence.text,
            text_sha256=sentence.text_sha256,
            snapshot_revision=1,
            normalized_asset_id=uuid4(),
            start_frame=entry.start_frame,
            end_frame=entry.end_frame,
            sample_rate_hz=entry.sample_rate_hz,
            start_ms=entry.start_ms,
            end_ms=entry.end_ms,
            measured_frame_count=entry.end_frame - entry.start_frame,
            measured_duration_ms=round(
                Fraction((entry.end_frame - entry.start_frame) * 1000, entry.sample_rate_hz)
            ),
        )
        for sentence, entry in zip(script.sentences, timeline, strict=True)
    ]
    group_rows = [
        SimpleNamespace(
            id=uuid4(),
            sequence=group.sequence,
            snapshot_revision=1,
            start_ms=group.start_ms,
            end_ms=group.end_ms,
            video_blocked=False,
        )
        for group in domain_groups
    ]
    if invalid == "sentence_zero":
        sentence_rows[0].sequence = 0
    elif invalid == "sentence_gap":
        sentence_rows[1].sequence = 3
    elif invalid == "group_zero":
        group_rows[0].sequence = 0
    elif invalid == "group_gap":
        group_rows[1].sequence = 3
    elif invalid == "measured_bad":
        sentence_rows[1].measured_duration_ms += 1
    memberships = [
        [sentence_rows[sequence - 1].id for sequence in group.sentence_sequences]
        for group in domain_groups
    ]
    currents = {
        group.id: SimpleNamespace(attempt_id=uuid4(), generation_id=uuid4())
        for group in group_rows
    }
    attempts = {
        current.attempt_id: SimpleNamespace(
            probe_evidence_json={"width": 1280, "height": 720}
        )
        for current in currents.values()
    }

    class FakeSession:
        def __init__(self) -> None:
            self.scalar_batches = iter((sentence_rows, (), group_rows, *memberships))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def begin(self):
            return nullcontext()

        def scalar(self, _statement):
            return SimpleNamespace(
                id=project_id,
                snapshot_confirmed_at=datetime.now(UTC),
                snapshot_aspect_ratio="16:9",
                aspect_ratio="16:9",
                production_revision=1,
            )

        def scalars(self, _statement):
            return next(self.scalar_batches)

        def get(self, model, identity):
            if model is GroupCurrentVideoRecord:
                return currents.get(identity)
            return attempts.get(identity)

    monkeypatch.setattr(
        authority_module,
        "_el_cloned_voice_source",
        lambda _project: None,
    )
    monkeypatch.setattr(
        authority_module,
        "_composition_audio_asset",
        lambda _session, sentence, _owner, _project, *_args, **_kwargs: MediaAssetRef(
            sentence.normalized_asset_id, "6" * 64, "audio/wav", 1
        ),
    )
    monkeypatch.setattr(
        authority_module,
        "_composition_video_asset",
        lambda _session, _current, _attempt, _group, _owner, _project: SimpleNamespace(
            id=uuid4()
        ),
    )
    return SqlCanonicalCompositionInputs(lambda: FakeSession()).load(project_id, owner_id)


def test_sql_composition_load_accepts_domain_generated_one_based_timeline(monkeypatch) -> None:
    request = _load_numbered_domain_timeline(monkeypatch)
    assert request is not None
    assert [sentence.order for sentence in request.sentences] == [0, 1]
    assert [group.order for group in request.groups] == [0, 1]


def test_sql_composition_load_accepts_fractional_frame_rounding(monkeypatch) -> None:
    request = _load_numbered_domain_timeline(monkeypatch, fractional=True)
    assert request is not None
    assert [item.end_ms - item.start_ms for item in request.sentences] == [1000, 1001]
    assert [item.duration_ms for item in request.groups[0].narration_segments] == [1000, 1001]


def test_sql_composition_load_accepts_sparse_sentence_numbering(monkeypatch) -> None:
    # 分隔句占用原始编号但不进权威表，稀疏递增编号必须可正常装载。
    request = _load_numbered_domain_timeline(monkeypatch, "sentence_gap")
    assert request is not None
    assert [sentence.order for sentence in request.sentences] == [0, 1]
    assert [group.order for group in request.groups] == [0, 1]


def test_sql_composition_load_rejects_polluted_measured_duration(monkeypatch) -> None:
    with pytest.raises(DomainError):
        _load_numbered_domain_timeline(monkeypatch, "measured_bad", fractional=True)


@pytest.mark.parametrize(
    "invalid", ("sentence_zero", "group_zero", "group_gap")
)
def test_sql_composition_load_rejects_noncanonical_numbering(monkeypatch, invalid: str) -> None:
    with pytest.raises(DomainError):
        _load_numbered_domain_timeline(monkeypatch, invalid)
