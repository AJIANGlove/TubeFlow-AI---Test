from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.persistence.models import MediaAssetRow
from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    AuthoritativeNarrationMembershipRecord,
    AuthoritativeNarrationSentenceRecord,
    SqlStoryboardNarrationAuthority,
)
from tubeflow_test.domain.errors import DomainError


class RecordingStorage:
    def __init__(
        self,
        failure: Exception | None = None,
        after_get=None,  # type: ignore[no-untyped-def]
        *,
        fail_key: str | None = None,
    ) -> None:
        self.failure = failure
        self.after_get = after_get
        self.fail_key = fail_key
        self.calls: list[tuple[str, str, int]] = []

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        self.calls.append((key, expected_sha256, expected_size))
        if self.after_get is not None:
            self.after_get()
        if self.failure is not None and (self.fail_key is None or self.fail_key == key):
            raise self.failure
        return b"audio"


def _authority_fixture():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (
        MediaAssetRow.__table__,
        AuthoritativeNarrationGroupRecord.__table__,
        AuthoritativeNarrationSentenceRecord.__table__,
        AuthoritativeNarrationMembershipRecord.__table__,
    ):
        table.create(engine)
    sessions = sessionmaker(engine)
    owner_id, project_id, group_id, sentence_id, raw_id, normalized_id = (uuid4() for _ in range(6))
    text = "hello"
    digest = sha256(b"audio").hexdigest()
    with sessions.begin() as session:
        session.add_all(
            (
                MediaAssetRow(
                    id=raw_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    ownership_scope="PROJECT",
                    kind="narration_raw",
                    object_key="actual/raw.wav",
                    sha256=digest,
                    media_type="audio/wav",
                    size_bytes=5,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
                MediaAssetRow(
                    id=normalized_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    ownership_scope="PROJECT",
                    kind="narration_normalized",
                    object_key="actual/normalized.wav",
                    sha256=digest,
                    media_type="audio/wav",
                    size_bytes=5,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
                AuthoritativeNarrationGroupRecord(
                    id=group_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    snapshot_sha256=sha256(text.encode()).hexdigest(),
                    snapshot_revision=1,
                    sequence=1,
                    start_frame=0,
                    end_frame=24_013,
                    sample_rate_hz=24_000,
                    start_ms=0,
                    end_ms=1001,
                    video_blocked=False,
                ),
                AuthoritativeNarrationSentenceRecord(
                    id=sentence_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    snapshot_sha256=sha256(text.encode()).hexdigest(),
                    snapshot_revision=1,
                    sequence=1,
                    text=text,
                    text_sha256=sha256(text.encode()).hexdigest(),
                    raw_asset_id=raw_id,
                    normalized_asset_id=normalized_id,
                    raw_qc_passed=True,
                    normalized_qc_passed=True,
                    measured_frame_count=24_013,
                    sample_rate_hz=24_000,
                    measured_duration_ms=1001,
                    start_frame=0,
                    end_frame=24_013,
                    start_ms=0,
                    end_ms=1001,
                ),
                AuthoritativeNarrationMembershipRecord(
                    group_id=group_id, sentence_id=sentence_id, ordinal=1
                ),
            )
        )
    return sessions, owner_id, group_id, raw_id, normalized_id


def test_sql_authority_verifies_actual_raw_and_normalized_object_keys() -> None:
    sessions, owner_id, group_id, _, _ = _authority_fixture()
    storage = RecordingStorage()
    group = SqlStoryboardNarrationAuthority(sessions, storage).current_group(  # type: ignore[arg-type]
        owner_id, group_id
    )
    assert group is not None and group.sentences[0].end_frame == 24_013
    assert storage.calls == [
        ("actual/raw.wav", sha256(b"audio").hexdigest(), 5),
        ("actual/normalized.wav", sha256(b"audio").hexdigest(), 5),
    ]


@pytest.mark.parametrize("failure", [FileNotFoundError(), ValueError("corrupt"), OSError("lost")])
def test_sql_authority_fails_closed_on_object_failure(failure: Exception) -> None:
    sessions, owner_id, group_id, _, _ = _authority_fixture()
    with pytest.raises(DomainError, match="unavailable") as caught:
        SqlStoryboardNarrationAuthority(  # type: ignore[arg-type]
            sessions, RecordingStorage(failure)
        ).current_group(owner_id, group_id)
    assert caught.value.code == "narration_not_ready"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("owner_id", uuid4()),
        ("project_id", uuid4()),
        ("kind", "storyboard_grid"),
        ("media_type", "audio/mpeg"),
        ("deletion_state", "DELETING"),
        ("purging", True),
        ("size_bytes", 0),
    ],
)
def test_sql_authority_rejects_polluted_normalized_asset(column: str, value: object) -> None:
    sessions, owner_id, group_id, _, normalized_id = _authority_fixture()
    with sessions.begin() as session:
        session.execute(
            update(MediaAssetRow).where(MediaAssetRow.id == normalized_id).values({column: value})
        )
    storage = RecordingStorage()
    with pytest.raises(DomainError) as caught:
        SqlStoryboardNarrationAuthority(sessions, storage).current_group(  # type: ignore[arg-type]
            owner_id, group_id
        )
    assert caught.value.code == "narration_not_ready"
    assert storage.calls == []


def test_sql_authority_revalidates_database_after_object_io() -> None:
    sessions, owner_id, group_id, _, normalized_id = _authority_fixture()

    def mutate() -> None:
        with Session(sessions.kw["bind"]) as session, session.begin():
            session.execute(
                update(MediaAssetRow)
                .where(MediaAssetRow.id == normalized_id)
                .values(object_key="replacement.wav")
            )

    with pytest.raises(DomainError, match="changed") as caught:
        SqlStoryboardNarrationAuthority(  # type: ignore[arg-type]
            sessions, RecordingStorage(after_get=mutate)
        ).current_group(owner_id, group_id)
    assert caught.value.code == "narration_not_ready"


def test_sql_authority_fails_closed_when_raw_asset_row_is_missing() -> None:
    sessions, owner_id, group_id, raw_id, _ = _authority_fixture()
    with sessions.begin() as session:
        session.execute(delete(MediaAssetRow).where(MediaAssetRow.id == raw_id))
    storage = RecordingStorage()
    with pytest.raises(DomainError) as caught:
        SqlStoryboardNarrationAuthority(sessions, storage).current_group(  # type: ignore[arg-type]
            owner_id, group_id
        )
    assert caught.value.code == "narration_not_ready"
    assert storage.calls == []


@pytest.mark.parametrize("failure", [FileNotFoundError(), ValueError("hash mismatch")])
def test_sql_authority_fails_closed_when_raw_bytes_are_missing_or_corrupt(
    failure: Exception,
) -> None:
    sessions, owner_id, group_id, _, _ = _authority_fixture()
    storage = RecordingStorage(failure, fail_key="actual/raw.wav")
    with pytest.raises(DomainError) as caught:
        SqlStoryboardNarrationAuthority(sessions, storage).current_group(  # type: ignore[arg-type]
            owner_id, group_id
        )
    assert caught.value.code == "narration_not_ready"
    assert storage.calls == [("actual/raw.wav", sha256(b"audio").hexdigest(), 5)]


def test_sql_authority_detects_raw_key_drift_after_byte_verification() -> None:
    sessions, owner_id, group_id, raw_id, _ = _authority_fixture()

    def mutate() -> None:
        with Session(sessions.kw["bind"]) as session, session.begin():
            session.execute(
                update(MediaAssetRow)
                .where(MediaAssetRow.id == raw_id)
                .values(object_key="replacement-raw.wav")
            )

    with pytest.raises(DomainError, match="changed") as caught:
        SqlStoryboardNarrationAuthority(  # type: ignore[arg-type]
            sessions, RecordingStorage(after_get=mutate)
        ).current_group(owner_id, group_id)
    assert caught.value.code == "narration_not_ready"


def test_sql_authority_rejects_same_raw_and_normalized_asset_id() -> None:
    sessions, owner_id, group_id, raw_id, _ = _authority_fixture()
    with sessions.begin() as session:
        session.execute(
            update(AuthoritativeNarrationSentenceRecord).values(normalized_asset_id=raw_id)
        )
    storage = RecordingStorage()
    with pytest.raises(DomainError, match="unique") as caught:
        SqlStoryboardNarrationAuthority(sessions, storage).current_group(  # type: ignore[arg-type]
            owner_id, group_id
        )
    assert caught.value.code == "narration_not_ready"
    assert storage.calls == []


def test_sql_authority_rejects_asset_id_reuse_across_sentences() -> None:
    sessions, owner_id, group_id, raw_id, _ = _authority_fixture()
    second_sentence_id, second_normalized_id = uuid4(), uuid4()
    digest = sha256(b"audio").hexdigest()
    with sessions.begin() as session:
        group = session.get_one(AuthoritativeNarrationGroupRecord, group_id)
        group.end_frame = 48_026
        group.end_ms = 2001
        session.add_all(
            (
                MediaAssetRow(
                    id=second_normalized_id,
                    owner_id=owner_id,
                    project_id=group.project_id,
                    ownership_scope="PROJECT",
                    kind="narration_normalized",
                    object_key="actual/normalized-2.wav",
                    sha256=digest,
                    media_type="audio/wav",
                    size_bytes=5,
                    metadata_json={},
                    shared=False,
                    purging=False,
                    deletion_state="ACTIVE",
                ),
                AuthoritativeNarrationSentenceRecord(
                    id=second_sentence_id,
                    owner_id=owner_id,
                    project_id=group.project_id,
                    snapshot_sha256=group.snapshot_sha256,
                    snapshot_revision=1,
                    sequence=2,
                    text="again",
                    text_sha256=sha256(b"again").hexdigest(),
                    raw_asset_id=raw_id,
                    normalized_asset_id=second_normalized_id,
                    raw_qc_passed=True,
                    normalized_qc_passed=True,
                    measured_frame_count=24_013,
                    sample_rate_hz=24_000,
                    measured_duration_ms=1001,
                    start_frame=24_013,
                    end_frame=48_026,
                    start_ms=1001,
                    end_ms=2001,
                ),
                AuthoritativeNarrationMembershipRecord(
                    group_id=group_id, sentence_id=second_sentence_id, ordinal=2
                ),
            )
        )
    storage = RecordingStorage()
    with pytest.raises(DomainError, match="unique") as caught:
        SqlStoryboardNarrationAuthority(sessions, storage).current_group(  # type: ignore[arg-type]
            owner_id, group_id
        )
    assert caught.value.code == "narration_not_ready"
    assert storage.calls == []
