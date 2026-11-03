from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.production_authority import (
    _assert_storyboard_shot_ranges,
    _storyboard_media_ref,
)
from tubeflow_test.adapters.persistence.storyboards.models import (
    StoryboardGenerationRow,
    StoryboardMediaRow,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.storyboards import (
    SHOT_TIMING_PROJECTION_V1,
    DirectorShot,
    ShotSentenceLink,
    ShotTimeRange,
)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE accounts (id CHAR(32) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE projects (id CHAR(32) PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE authoritative_narration_groups (id CHAR(32) PRIMARY KEY)")
        )
        connection.execute(
            text("CREATE TABLE storyboard_director_revisions (id CHAR(32) PRIMARY KEY)")
        )
    for table in (
        MediaAssetRow.__table__,
        ProjectAssetReferenceRow.__table__,
        StoryboardGenerationRow.__table__,
        StoryboardMediaRow.__table__,
    ):
        table.create(engine)
    return Session(engine)


def _facts(session: Session):  # type: ignore[no-untyped-def]
    owner_id, project_id, group_id, generation_id, revision_id, asset_id = (
        uuid4() for _ in range(6)
    )
    digest = "a" * 64
    generation = StoryboardGenerationRow(
        id=generation_id,
        owner_id=owner_id,
        project_id=project_id,
        group_id=group_id,
        applied_revision_id=revision_id,
        version=1,
        kind="GRID_SHEET",
        input_json={},
        idempotency_key="manual",
        provider_idempotency_key="provider",
        canonical_request_sha256="b" * 64,
        status="SUCCEEDED",
        state_revision=1,
        execution_token=None,
        lease_until=None,
        created_at=datetime.now(UTC),
        checkpoint_json={
            "object_asset": {
                "asset_id": str(asset_id),
                "sha256": digest,
                "media_type": "image/png",
                "size_bytes": 123,
            },
            "expected_sha256": digest,
            "downloaded_sha256": digest,
            "downloaded_size": 123,
            "qc_mime": "image/png",
        },
        asset_id=asset_id,
    )
    media = StoryboardMediaRow(
        generation_id=generation_id,
        storyboard_asset_id=uuid4(),
        asset_id=asset_id,
        width=1920,
        height=1080,
        image_format="png",
    )
    asset = MediaAssetRow(
        id=asset_id,
        owner_id=owner_id,
        project_id=None,
        ownership_scope="OWNER",
        kind="STORYBOARD_GRID",
        object_key=f"storyboard/{asset_id}",
        sha256=digest,
        media_type="image/png",
        size_bytes=123,
        metadata_json={},
        shared=True,
        purging=False,
        deletion_state="ACTIVE",
    )
    reference = ProjectAssetReferenceRow(
        project_id=project_id, asset_id=asset_id, owner_id=owner_id
    )
    session.add_all((generation, media, asset, reference))
    session.commit()
    return generation, media, asset, reference, owner_id, project_id


def test_shared_current_storyboard_media_is_authoritative() -> None:
    with _session() as session:
        generation, media, asset, _reference, owner_id, project_id = _facts(session)
        result = _storyboard_media_ref(
            session, generation, media, owner_id, project_id, generation.group_id
        )
        assert result.asset_id == asset.id
        assert result.sha256 == asset.sha256


@pytest.mark.parametrize(
    "invalid", ["unreferenced", "cross_owner", "bad_hash", "wrong_group"]
)
def test_storyboard_media_rejects_invalid_authority(invalid: str) -> None:
    with _session() as session:
        generation, media, asset, reference, owner_id, project_id = _facts(session)
        if invalid == "unreferenced":
            session.delete(reference)
        elif invalid == "cross_owner":
            asset.owner_id = uuid4()
        elif invalid == "bad_hash":
            asset.sha256 = "c" * 64
        else:
            generation.group_id = uuid4()
        session.commit()
        with pytest.raises(DomainError, match="Canonical storyboard media is unavailable"):
            _storyboard_media_ref(
                session,
                generation,
                media,
                owner_id,
                project_id,
                uuid4() if invalid == "wrong_group" else generation.group_id,
            )


def _timing_gate_facts() -> tuple[
    tuple[DirectorShot, ...], tuple[ShotSentenceLink, ...], dict[object, tuple[int, int]]
]:
    """共享句链接形态：镜头 1={句1,句2}、镜头 2={句2,句3}，句区间 0-3/3-6/6-9s。

    legacy min/max → (0,6000)/(3000,9000)（重叠）；投影 v1 → (0,6000)/(6000,9000)。
    """
    shots = tuple(
        DirectorShot(
            uuid4(), order, f"scene {order}", "wide", "eye", "arrive", "establish", "desc"
        )
        for order in (1, 2)
    )
    sentences = (uuid4(), uuid4(), uuid4())
    links = (
        ShotSentenceLink(shots[0].shot_id, sentences[0]),
        ShotSentenceLink(shots[0].shot_id, sentences[1]),
        ShotSentenceLink(shots[1].shot_id, sentences[1]),
        ShotSentenceLink(shots[1].shot_id, sentences[2]),
    )
    ranges = {sentences[0]: (0, 3000), sentences[1]: (3000, 6000), sentences[2]: (6000, 9000)}
    return shots, links, ranges


def _ranges_json(values: tuple[tuple[int, int, int], ...]) -> list[dict[str, int]]:
    return [
        {"order": order, "start_ms": start_ms, "end_ms": end_ms}
        for order, start_ms, end_ms in values
    ]


def test_shot_ranges_gate_accepts_legacy_records_without_algorithm_key() -> None:
    """历史记录（无算法键）：按 legacy min/max 重算比对，相符即放行并返回区间。"""
    shots, links, ranges = _timing_gate_facts()
    inputs = {"shot_time_ranges": _ranges_json(((1, 0, 6000), (2, 3000, 9000)))}

    result = _assert_storyboard_shot_ranges(inputs, shots, links, ranges)  # type: ignore[arg-type]

    assert result == (ShotTimeRange(1, 0, 6000), ShotTimeRange(2, 3000, 9000))


def test_shot_ranges_gate_accepts_projection_v1_records() -> None:
    """v1 记录：按 monotone-projection-v1 投影重算比对（共享句唯一归属）。"""
    shots, links, ranges = _timing_gate_facts()
    inputs = {
        "shot_time_ranges": _ranges_json(((1, 0, 6000), (2, 6000, 9000))),
        "shot_time_ranges_algorithm": SHOT_TIMING_PROJECTION_V1,
    }

    result = _assert_storyboard_shot_ranges(inputs, shots, links, ranges)  # type: ignore[arg-type]

    assert result == (ShotTimeRange(1, 0, 6000), ShotTimeRange(2, 6000, 9000))


def test_shot_ranges_gate_ignores_records_without_ranges() -> None:
    """早期历史记录（无 shot_time_ranges 键）：不校验，返回空。"""
    shots, links, ranges = _timing_gate_facts()
    assert _assert_storyboard_shot_ranges({}, shots, links, ranges) == ()


@pytest.mark.parametrize("algorithm", [None, SHOT_TIMING_PROJECTION_V1])
def test_shot_ranges_gate_reports_drift_on_tampered_ranges(algorithm: str | None) -> None:
    """两种算法口径下篡改区间仍报 storyboard_timeline_drift。"""
    shots, links, ranges = _timing_gate_facts()
    correct = (
        ((1, 0, 6000), (2, 3000, 9000))
        if algorithm is None
        else ((1, 0, 6000), (2, 6000, 9000))
    )
    tampered = (*correct[:1], (correct[1][0], correct[1][1], correct[1][2] + 1))
    inputs: dict[str, object] = {"shot_time_ranges": _ranges_json(tampered)}
    if algorithm is not None:
        inputs["shot_time_ranges_algorithm"] = algorithm

    with pytest.raises(DomainError) as excinfo:
        _assert_storyboard_shot_ranges(inputs, shots, links, ranges)  # type: ignore[arg-type]
    assert excinfo.value.code == "storyboard_timeline_drift"


def test_shot_ranges_gate_fails_closed_on_unknown_algorithm() -> None:
    """未知算法键：血统不一致（h3_inputs_not_ready），不做任何猜测性比对。"""
    shots, links, ranges = _timing_gate_facts()
    inputs = {
        "shot_time_ranges": _ranges_json(((1, 0, 6000), (2, 6000, 9000))),
        "shot_time_ranges_algorithm": "mystery-v9",
    }
    with pytest.raises(DomainError) as excinfo:
        _assert_storyboard_shot_ranges(inputs, shots, links, ranges)  # type: ignore[arg-type]
    assert excinfo.value.code == "h3_inputs_not_ready"


def test_shot_ranges_gate_fails_closed_when_v1_record_cannot_project() -> None:
    """v1 记录遇到无解链接形态（镜头 1={1,2,4}、镜头 2={3}）：重算失败即血统不一致。"""
    shots, _links, _ranges = _timing_gate_facts()
    sentences = (uuid4(), uuid4(), uuid4(), uuid4())
    nested_links = (
        ShotSentenceLink(shots[0].shot_id, sentences[0]),
        ShotSentenceLink(shots[0].shot_id, sentences[1]),
        ShotSentenceLink(shots[1].shot_id, sentences[2]),
        ShotSentenceLink(shots[0].shot_id, sentences[3]),
    )
    nested_ranges = {
        sentences[0]: (0, 2800),
        sentences[1]: (2800, 5600),
        sentences[2]: (5600, 8400),
        sentences[3]: (8400, 11400),
    }
    inputs = {
        "shot_time_ranges": _ranges_json(((1, 0, 11400), (2, 5600, 8400))),
        "shot_time_ranges_algorithm": SHOT_TIMING_PROJECTION_V1,
    }
    with pytest.raises(DomainError) as excinfo:
        _assert_storyboard_shot_ranges(inputs, shots, nested_links, nested_ranges)  # type: ignore[arg-type]
    assert excinfo.value.code == "h3_inputs_not_ready"
