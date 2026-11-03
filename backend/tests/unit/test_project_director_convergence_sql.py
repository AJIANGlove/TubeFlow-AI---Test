"""多父血缘与收敛结果的 SQL 仓储往返（§24；SQLite 级，Postgres 由部署验收复核）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.persistence.narration import (
    AuthoritativeNarrationGroupRecord,
    AuthoritativeNarrationMembershipRecord,
    AuthoritativeNarrationSentenceRecord,
)
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorPointerRow,
    DirectorRevisionRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    DirectorRevisionLineageRow,
    ProjectDirectorGroupCandidateRow,
    ProjectDirectorMaterializationRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    SqlAlchemyProjectDirectorRepository,
)
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.domain.project_director import (
    MaterializationStatus,
    ProjectDirectorGroupCandidate,
    ProjectDirectorMaterialization,
    ProjectDirectorSourceShotLink,
)
from tubeflow_test.domain.storyboards import (
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    ReviewState,
    ShotSentenceLink,
)


def materialization_fixture() -> ProjectDirectorMaterialization:
    """6 镜、7 句、7 条来源血缘（含一处多父）且带最终修订的物化。

    形态对应「7 镜切片经 adjacent-merge-v1 收敛为 6 镜」的持久化结果：
    第 6 个保留镜头承载两个来源镜头（多父），句子链接覆盖 7 句。
    """
    owner, project, group, preplan_revision = uuid4(), uuid4(), uuid4(), uuid4()
    sentences = tuple(uuid4() for _ in range(7))
    shots = tuple(
        DirectorShot(
            uuid4(),
            order,
            f"scene {order}",
            "WIDE",
            "EYE_LEVEL",
            f"action {order}",
            f"function {order}",
            f"description {order}",
        )
        for order in range(1, 7)
    )
    sentence_links = (
        ShotSentenceLink(shots[0].shot_id, sentences[0]),
        ShotSentenceLink(shots[1].shot_id, sentences[1]),
        ShotSentenceLink(shots[2].shot_id, sentences[2]),
        ShotSentenceLink(shots[3].shot_id, sentences[3]),
        ShotSentenceLink(shots[4].shot_id, sentences[4]),
        ShotSentenceLink(shots[5].shot_id, sentences[5]),
        ShotSentenceLink(shots[5].shot_id, sentences[6]),
    )
    source_shot_links = tuple(
        ProjectDirectorSourceShotLink(uuid4(), item.shot_id) for item in shots[:5]
    ) + (
        ProjectDirectorSourceShotLink(uuid4(), shots[5].shot_id),
        ProjectDirectorSourceShotLink(uuid4(), shots[5].shot_id),
    )
    final = DirectorPlanRevision(
        revision_id=uuid4(),
        owner_id=owner,
        project_id=project,
        group_id=group,
        revision_number=1,
        kind=DirectorRevisionKind.DRAFT,
        shots=shots,
        sentence_links=sentence_links,
        group_sentence_ids=sentences,
        review_state=ReviewState.NOT_REQUIRED,
        continuous_observation_approvals=(),
        created_at=datetime.now(UTC),
    )
    candidate = ProjectDirectorGroupCandidate(
        candidate_id=uuid4(),
        group_id=group,
        group_sequence=1,
        shots=shots,
        sentence_links=sentence_links,
        source_shot_links=source_shot_links,
        conflicts=(),
        review_state=ReviewState.NOT_REQUIRED,
        final_revision=final,
    )
    return ProjectDirectorMaterialization(
        materialization_id=uuid4(),
        owner_id=owner,
        project_id=project,
        preplan_revision_id=preplan_revision,
        snapshot_sha256="a" * 64,
        snapshot_revision=1,
        algorithm_version="controlled-semantic-lineage-remap-v4",
        canonical_input_sha256="b" * 64,
        status=MaterializationStatus.SUCCEEDED,
        candidates=(candidate,),
        created_at=datetime.now(UTC),
    )


# ProjectRow 使用 PG 专用类型（PGUUID/ARRAY），SQLite 无法直接建表；此处按
# persist 路径实际读取的列集建立等价宽松表（与 test_storyboard_sql_repository 同法）。
_PROJECTS_DDL = (
    "CREATE TABLE projects ("
    "id CHAR(32) PRIMARY KEY, owner_id CHAR(32), channel_id CHAR(32), "
    "title TEXT, script TEXT, character_image_ids JSON, visual_style TEXT, "
    "voice_version_id CHAR(32), tts_provider TEXT, cloned_voice_id CHAR(32), "
    "locale TEXT, aspect_ratio TEXT, expected_duration_seconds INTEGER, "
    "created_at TEXT, published_at TEXT, draft_revision INTEGER, production_state TEXT, "
    "production_mode TEXT, auto_budget_round INTEGER, production_revision INTEGER, "
    "snapshot_script TEXT, snapshot_character_image_ids JSON, snapshot_visual_style TEXT, "
    "snapshot_voice_version_id CHAR(32), snapshot_locale TEXT, snapshot_aspect_ratio TEXT, "
    "snapshot_channel_url TEXT, snapshot_voice_metadata_sha256 TEXT, "
    "snapshot_voice_catalog_source_sha256 TEXT, snapshot_voice_reference_asset_id CHAR(32), "
    "snapshot_voice_reference_sha256 TEXT, snapshot_voice_reference_media_type TEXT, "
    "snapshot_voice_reference_size_bytes INTEGER, snapshot_voice_preview_asset_id CHAR(32), "
    "snapshot_voice_preview_sha256 TEXT, snapshot_voice_preview_media_type TEXT, "
    "snapshot_voice_preview_size_bytes INTEGER, snapshot_voice_avatar_asset_id CHAR(32), "
    "snapshot_voice_avatar_sha256 TEXT, snapshot_voice_avatar_media_type TEXT, "
    "snapshot_voice_avatar_size_bytes INTEGER, snapshot_tts_provider TEXT, "
    "snapshot_cloned_voice_id CHAR(32), snapshot_cloned_voice_name TEXT, "
    "snapshot_cloned_voice_source_sha256 TEXT, snapshot_expected_duration_seconds INTEGER, "
    "snapshot_confirmed_at TEXT, current_cover_prompt_generation_id CHAR(32), "
    "current_cover_image_generation_id CHAR(32))"
)


def repository(
    path: Path, value: ProjectDirectorMaterialization
) -> tuple[SqlAlchemyProjectDirectorRepository, sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )
    for table in (
        ProjectDirectorMaterializationRow.__table__,
        ProjectDirectorGroupCandidateRow.__table__,
        DirectorRevisionLineageRow.__table__,
        DirectorRevisionRow.__table__,
        DirectorPointerRow.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(text(_PROJECTS_DDL))
        connection.execute(
            text(
                "INSERT INTO projects (id, owner_id, title, script, visual_style, locale, "
                "aspect_ratio, created_at, draft_revision, production_state, production_mode, "
                "auto_budget_round, production_revision) VALUES (:id, :owner_id, 'fixture', "
                "'script', 'style', 'en-US', '16:9', '2026-01-01 00:00:00', 1, 'ACTIVE', "
                "'MANUAL', 1, 1)"
            ),
            {"id": value.project_id.hex, "owner_id": value.owner_id.hex},
        )
    sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def uow() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    return SqlAlchemyProjectDirectorRepository(uow), sessions


def test_materialization_roundtrip_keeps_multi_parent_lineage_and_seeded_draft(
    tmp_path: Path,
) -> None:
    value = materialization_fixture()
    store, sessions = repository(tmp_path / "materialization.sqlite", value)

    store.persist_materialization(value)
    found = store.find_materialization(value.owner_id, value.canonical_input_sha256)

    assert found is not None
    assert found.materialization_id == value.materialization_id
    assert found.algorithm_version == "controlled-semantic-lineage-remap-v4"
    candidate = found.candidates[0]
    assert len(candidate.shots) == 6
    assert [item.order for item in candidate.shots] == [1, 2, 3, 4, 5, 6]
    # 多父血缘往返：7 条来源全部保留，恰一个保留镜头承载 2 个来源。
    lineage_targets = [item.candidate_shot_id for item in candidate.source_shot_links]
    assert len(lineage_targets) == 7
    assert sorted(lineage_targets.count(shot_id) for shot_id in set(lineage_targets)) == [
        1,
        1,
        1,
        1,
        1,
        2,
    ]
    # final_revision 直落草稿：6 镜修订与指针在同一往返中读回。
    final = candidate.final_revision
    assert final is not None
    assert final.revision_number == 1
    assert len(final.shots) == 6
    assert len(final.sentence_links) == 7
    with sessions.begin() as session:
        pointer = session.scalar(
            select(DirectorPointerRow).where(DirectorPointerRow.group_id == candidate.group_id)
        )
    assert pointer is not None
    assert pointer.draft_revision_id == final.revision_id


def test_repeated_persist_returns_the_same_materialization(tmp_path: Path) -> None:
    value = materialization_fixture()
    store, _ = repository(tmp_path / "idempotent.sqlite", value)

    first = store.persist_materialization(value)
    replay = store.persist_materialization(value)

    assert replay.materialization_id == first.materialization_id
    # 幂等回读：同一物化、同一候选与最终修订（比较稳定标识，避开读回时区归一化）。
    assert replay.candidates[0].candidate_id == first.candidates[0].candidate_id
    assert replay.candidates[0].shots == first.candidates[0].shots
    assert replay.candidates[0].source_shot_links == first.candidates[0].source_shot_links
    first_final = first.candidates[0].final_revision
    replay_final = replay.candidates[0].final_revision
    assert first_final is not None and replay_final is not None
    assert replay_final.revision_id == first_final.revision_id


def test_find_materialization_isolates_by_owner_and_canonical_input(tmp_path: Path) -> None:
    value = materialization_fixture()
    store, _ = repository(tmp_path / "isolation.sqlite", value)
    store.persist_materialization(value)

    assert store.find_materialization(value.owner_id, "c" * 64) is None
    assert store.find_materialization(uuid4(), value.canonical_input_sha256) is None


def _narration_groups_repository(
    path: Path,
) -> tuple[SqlAlchemyProjectDirectorRepository, sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )
    for table in (
        AuthoritativeNarrationGroupRecord.__table__,
        AuthoritativeNarrationSentenceRecord.__table__,
        AuthoritativeNarrationMembershipRecord.__table__,
    ):
        table.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def uow() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    return SqlAlchemyProjectDirectorRepository(uow), sessions


def _seed_narration_group(
    sessions: sessionmaker[Session],
    *,
    drop_sentence: bool = False,
) -> tuple[UUID, UUID, str, tuple[UUID, UUID]]:
    owner_id, project_id, group_id = (uuid4() for _ in range(3))
    sentences = (uuid4(), uuid4())
    digest = "d" * 64
    with sessions.begin() as session:
        session.add(
            AuthoritativeNarrationGroupRecord(
                id=group_id,
                project_id=project_id,
                owner_id=owner_id,
                snapshot_sha256=digest,
                snapshot_revision=1,
                sequence=1,
                start_frame=0,
                end_frame=288_000,
                sample_rate_hz=48_000,
                start_ms=0,
                end_ms=6_000,
                video_blocked=False,
            )
        )
        kept = sentences[:-1] if drop_sentence else sentences
        for index, sentence_id in enumerate(kept):
            session.add(
                AuthoritativeNarrationSentenceRecord(
                    id=sentence_id,
                    project_id=project_id,
                    owner_id=owner_id,
                    snapshot_sha256=digest,
                    snapshot_revision=1,
                    sequence=index + 1,
                    text=f"sentence {index}",
                    text_sha256="e" * 64,
                    raw_asset_id=uuid4(),
                    normalized_asset_id=uuid4(),
                    raw_qc_passed=True,
                    normalized_qc_passed=True,
                    measured_frame_count=144_000,
                    sample_rate_hz=48_000,
                    measured_duration_ms=3_000,
                    start_frame=index * 144_000,
                    end_frame=(index + 1) * 144_000,
                    start_ms=index * 3_000,
                    end_ms=(index + 1) * 3_000,
                )
            )
        for ordinal, sentence_id in enumerate(sentences, 1):
            session.add(
                AuthoritativeNarrationMembershipRecord(
                    group_id=group_id, sentence_id=sentence_id, ordinal=ordinal
                )
            )
    return owner_id, project_id, digest, sentences


def test_materialization_groups_carry_measured_sentence_ranges(tmp_path: Path) -> None:
    """2026-10-16：组上下文携带与 sentence_ids 位置对齐的句级实测时间轴。"""
    store, sessions = _narration_groups_repository(tmp_path / "groups-ranges.sqlite")
    owner_id, project_id, digest, sentences = _seed_narration_group(sessions)

    groups = store.materialization_groups(owner_id, project_id, digest, 1)

    assert len(groups) == 1
    assert groups[0].sentence_ids == sentences
    assert groups[0].sentence_ranges_ms == ((0, 3000), (3000, 6000))


def test_materialization_groups_fail_closed_on_missing_sentence_row(tmp_path: Path) -> None:
    """成员引用的句子行缺失 = 权威数据不一致，报 narration_not_ready。"""
    store, sessions = _narration_groups_repository(tmp_path / "groups-missing.sqlite")
    owner_id, project_id, digest, _sentences = _seed_narration_group(
        sessions, drop_sentence=True
    )

    with pytest.raises(DomainError) as excinfo:
        store.materialization_groups(owner_id, project_id, digest, 1)
    assert excinfo.value.code == "narration_not_ready"
