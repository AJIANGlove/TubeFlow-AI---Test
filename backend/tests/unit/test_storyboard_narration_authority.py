from __future__ import annotations

import hashlib
from uuid import uuid4

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.models import MediaAssetRow
from tubeflow_test.adapters.persistence.storyboards.repository import (
    _authoritative_narration_ready,
)
from tubeflow_test.application.narration_authority import (
    AuthoritativeNarrationGroup,
    AuthoritativeNarrationSentence,
)
from tubeflow_test.domain.narration import frozen_script_sha256


def test_canonical_narration_drift_fails_closed() -> None:
    owner, project_id, group_id = uuid4(), uuid4(), uuid4()
    script = "hello"
    project = ProjectRow(
        id=project_id, owner_id=owner, snapshot_script=script, production_state="ACTIVE"
    )
    raw_id, normalized_id = uuid4(), uuid4()
    sentence = AuthoritativeNarrationSentence(
        sentence_id=uuid4(),
        ordinal=1,
        text=script,
        text_sha256=hashlib.sha256(script.encode()).hexdigest(),
        start_ms=0,
        end_ms=1000,
        raw_asset_id=raw_id,
        raw_sha256="a" * 64,
        normalized_asset_id=normalized_id,
        normalized_sha256="b" * 64,
        qc_passed=True,
        sample_count=48000,
        sample_rate=48000,
        start_frame=0,
        end_frame=48000,
        raw_size_bytes=10,
        normalized_size_bytes=10,
    )
    group = AuthoritativeNarrationGroup(
        group_id=group_id,
        owner_id=owner,
        project_id=project_id,
        snapshot_sha256=hashlib.sha256(script.encode()).hexdigest(),
        snapshot_revision=1,
        start_ms=0,
        end_ms=1000,
        video_blocked=False,
        sentences=(sentence,),
    )
    raw = MediaAssetRow(
        id=raw_id,
        owner_id=owner,
        project_id=project_id,
        sha256="a" * 64,
        kind="narration_raw",
        object_key="narration/raw.wav",
        media_type="audio/wav",
        size_bytes=10,
        purging=False,
        deletion_state="ACTIVE",
    )
    normalized = MediaAssetRow(
        id=normalized_id,
        owner_id=owner,
        project_id=project_id,
        sha256="b" * 64,
        kind="narration_normalized",
        object_key="narration/normalized.wav",
        media_type="audio/wav",
        size_bytes=10,
        purging=False,
        deletion_state="ACTIVE",
    )
    media = {raw_id: raw, normalized_id: normalized}
    assert _authoritative_narration_ready(project, group, media)
    drifted = AuthoritativeNarrationSentence(
        **{**sentence.__dict__, "text": "changed after narration"}
    )
    assert not _authoritative_narration_ready(
        project, AuthoritativeNarrationGroup(**{**group.__dict__, "sentences": (drifted,)}), media
    )


def test_fractional_cumulative_timeline_uses_canonical_frame_endpoints() -> None:
    owner, project_id, group_id = uuid4(), uuid4(), uuid4()
    script = "one two"
    project = ProjectRow(
        id=project_id, owner_id=owner, snapshot_script=script, production_state="ACTIVE"
    )
    sentences = []
    media: dict[object, MediaAssetRow] = {}
    frame_cursor = 0
    for ordinal, frame_count in enumerate((24_013, 24_013), start=1):
        raw_id, normalized_id = uuid4(), uuid4()
        start_frame, end_frame = frame_cursor, frame_cursor + frame_count
        sentence = AuthoritativeNarrationSentence(
            uuid4(),
            ordinal,
            "one" if ordinal == 1 else "two",
            hashlib.sha256(("one" if ordinal == 1 else "two").encode()).hexdigest(),
            raw_id,
            normalized_id,
            "a" * 64,
            "b" * 64,
            True,
            frame_count,
            24_000,
            round(start_frame * 1000 / 24_000),
            round(end_frame * 1000 / 24_000),
            start_frame,
            end_frame,
            10,
            10,
        )
        sentences.append(sentence)
        media[raw_id] = MediaAssetRow(
            id=raw_id,
            owner_id=owner,
            project_id=project_id,
            kind="narration_raw",
            object_key=f"raw/{ordinal}",
            sha256="a" * 64,
            media_type="audio/wav",
            size_bytes=10,
            purging=False,
            deletion_state="ACTIVE",
        )
        media[normalized_id] = MediaAssetRow(
            id=normalized_id,
            owner_id=owner,
            project_id=project_id,
            kind="narration_normalized",
            object_key=f"normalized/{ordinal}",
            sha256="b" * 64,
            media_type="audio/wav",
            size_bytes=10,
            purging=False,
            deletion_state="ACTIVE",
        )
        frame_cursor = end_frame
    group = AuthoritativeNarrationGroup(
        group_id,
        owner,
        project_id,
        hashlib.sha256(script.encode()).hexdigest(),
        1,
        0,
        round(frame_cursor * 1000 / 24_000),
        False,
        tuple(sentences),
    )
    assert _authoritative_narration_ready(project, group, media)  # type: ignore[arg-type]
    tampered = AuthoritativeNarrationSentence(
        **{**sentences[1].__dict__, "end_frame": sentences[1].end_frame + 1}
    )
    assert not _authoritative_narration_ready(
        project,
        AuthoritativeNarrationGroup(**{**group.__dict__, "sentences": (sentences[0], tampered)}),
        media,  # type: ignore[arg-type]
    )


def test_narration_ready_uses_normalized_frozen_script_hash() -> None:
    """§22 回归（Vikings 生产现场）：库存原始文本带尾随换行时门禁仍按归一化基线放行；
    文本真实漂移仍 fail-closed。"""
    owner, project_id, group_id = uuid4(), uuid4(), uuid4()
    script = "hello world.\n"  # 粘贴文本常见尾随换行；分句哈希按 NFC+strip 基线写入
    project = ProjectRow(
        id=project_id, owner_id=owner, snapshot_script=script, production_state="ACTIVE"
    )
    raw_id, normalized_id = uuid4(), uuid4()
    sentence = AuthoritativeNarrationSentence(
        sentence_id=uuid4(),
        ordinal=1,
        text="hello world.",
        text_sha256=hashlib.sha256(b"hello world.").hexdigest(),
        start_ms=0,
        end_ms=1000,
        raw_asset_id=raw_id,
        raw_sha256="a" * 64,
        normalized_asset_id=normalized_id,
        normalized_sha256="b" * 64,
        qc_passed=True,
        sample_count=48000,
        sample_rate=48000,
        start_frame=0,
        end_frame=48000,
        raw_size_bytes=10,
        normalized_size_bytes=10,
    )
    group = AuthoritativeNarrationGroup(
        group_id=group_id,
        owner_id=owner,
        project_id=project_id,
        snapshot_sha256=frozen_script_sha256(script),
        snapshot_revision=1,
        start_ms=0,
        end_ms=1000,
        video_blocked=False,
        sentences=(sentence,),
    )
    raw = MediaAssetRow(
        id=raw_id,
        owner_id=owner,
        project_id=project_id,
        sha256="a" * 64,
        kind="narration_raw",
        object_key="narration/raw.wav",
        media_type="audio/wav",
        size_bytes=10,
        purging=False,
        deletion_state="ACTIVE",
    )
    normalized = MediaAssetRow(
        id=normalized_id,
        owner_id=owner,
        project_id=project_id,
        sha256="b" * 64,
        kind="narration_normalized",
        object_key="narration/normalized.wav",
        media_type="audio/wav",
        size_bytes=10,
        purging=False,
        deletion_state="ACTIVE",
    )
    media = {raw_id: raw, normalized_id: normalized}
    assert _authoritative_narration_ready(project, group, media)
    drifted = ProjectRow(
        id=project_id,
        owner_id=owner,
        snapshot_script="hello world. changed",
        production_state="ACTIVE",
    )
    assert not _authoritative_narration_ready(drifted, group, media)
