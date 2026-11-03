from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.persistence.models import ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.narration import AuthoritativeNarrationGroupRecord
from tubeflow_test.adapters.persistence.storyboards.metadata import storyboard_metadata
from tubeflow_test.adapters.persistence.storyboards.models import (
    DirectorGenerationAttemptRow,
    DirectorPointerRow,
    DirectorRevisionRow,
    StoryboardCurrentRow,
    StoryboardGenerationRow,
    StoryboardMediaRow,
    StoryboardOutboxRow,
    VideoStoryboardBindingRow,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    SqlAlchemyStoryboardRepository,
    _director_attempt_row,
    _generation_row,
)
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.director_generation import (
    DirectorAttemptStatus,
    DirectorGenerationAttempt,
    DirectorInputSnapshot,
)
from tubeflow_test.domain.storyboards import (
    CharacterAssetSnapshot,
    ContinuousObservationApproval,
    DirectorLocationRelation,
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
    GenerationStatus,
    ReviewState,
    ShotSentenceLink,
    ShotTimeRange,
    StoryboardAsset,
    StoryboardAssetKind,
    StoryboardGeneration,
    VideoStoryboardBinding,
)
from tubeflow_test.video_generation.persistence import VideoAttemptRecord


def generation(version: int, status: GenerationStatus) -> StoryboardGeneration:
    owner, project, group, revision, sentence = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    shot = DirectorShot(uuid4(), 1, "scene", "wide", "eye", "arrive", "establish", "desc")
    character = CharacterAssetSnapshot(1, uuid4(), uuid4(), "a" * 64, "image/png", 10)
    return StoryboardGeneration(
        generation_id=uuid4(),
        owner_id=owner,
        project_id=project,
        group_id=group,
        applied_revision_id=revision,
        shots_snapshot=(shot,),
        sentence_links_snapshot=(ShotSentenceLink(shot.shot_id, sentence),),
        version=version,
        kind=StoryboardAssetKind.GRID_SHEET,
        prompt="grid",
        character_assets_snapshot=(character,),
        style_snapshot="ink",
        model="fake",
        parameters={},
        idempotency_key=f"key-{version}",
        provider_idempotency_key="b" * 64,
        canonical_request_sha256="c" * 64,
        status=status,
        created_at=datetime.now(UTC),
    )


def director_attempt(
    *,
    attempt_id: UUID,
    owner_id: UUID,
    project_id: UUID,
    group_id: UUID,
    created_at: datetime,
) -> DirectorGenerationAttempt:
    return DirectorGenerationAttempt(
        attempt_id=attempt_id,
        owner_id=owner_id,
        group_id=group_id,
        idempotency_key=f"key-{attempt_id.hex}",
        provider_idempotency_key="a" * 64,
        canonical_request_sha256="b" * 64,
        input_snapshot=DirectorInputSnapshot(
            owner_id,
            project_id,
            group_id,
            None,
            (),
            (),
            "ink",
            "en-US",
            "16:9",
        ),
        prompt_ref="director-plan-v1",
        prompt_sha256="c" * 64,
        prompt_content="prompt",
        model="director-v1",
        parameters={},
        status=DirectorAttemptStatus.INTENT_PERSISTED,
        created_at=created_at,
        history=(DirectorAttemptStatus.INTENT_PERSISTED.value,),
    )


def repositories(
    path: Path,
) -> tuple[SqlAlchemyStoryboardRepository, SqlAlchemyStoryboardRepository, sessionmaker[Session]]:
    assert AuthoritativeNarrationGroupRecord.__tablename__ == "authoritative_narration_groups"
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )
    for table in (
        DirectorPointerRow.__table__,
        DirectorRevisionRow.__table__,
        StoryboardGenerationRow.__table__,
        StoryboardCurrentRow.__table__,
        StoryboardMediaRow.__table__,
        StoryboardOutboxRow.__table__,
        ProjectAssetReferenceRow.__table__,
        VideoStoryboardBindingRow.__table__,
        VideoAttemptRecord.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, "
                "owner_id CHAR(32), production_state VARCHAR(16))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE media_assets (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "project_id CHAR(32), ownership_scope VARCHAR(16) DEFAULT 'PROJECT', "
                "kind VARCHAR(32), object_key TEXT UNIQUE, "
                "sha256 VARCHAR(64), "
                "media_type VARCHAR(64), size_bytes BIGINT, metadata_json JSON, shared BOOLEAN, "
                "purging BOOLEAN DEFAULT 0, deletion_state VARCHAR(16), "
                "deletion_token CHAR(32), deletion_generation INTEGER DEFAULT 0)"
            )
        )
    sessions = sessionmaker(engine, expire_on_commit=False)

    from collections.abc import Iterator
    from contextlib import contextmanager

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

    return (
        SqlAlchemyStoryboardRepository(uow),
        SqlAlchemyStoryboardRepository(uow),
        sessions,
    )


def test_two_repositories_concurrently_create_one_idempotent_generation(tmp_path: Path) -> None:
    first, second, sessions = repositories(tmp_path / "idempotency.sqlite")
    value = generation(1, GenerationStatus.INTENT_PERSISTED)
    with sessions.begin() as session:
        session.add(
            DirectorPointerRow(
                owner_id=value.owner_id,
                group_id=value.group_id,
                draft_revision_id=None,
                applied_revision_id=value.applied_revision_id,
            )
        )
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state) "
                "VALUES (:id, :owner, 'ACTIVE')"
            ),
            {"id": value.project_id.hex, "owner": value.owner_id.hex},
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda repo: repo.create_generation(value), (first, second)))
    assert results[0].generation_id == results[1].generation_id
    with sessions() as session:
        rows = session.scalars(select(StoryboardGenerationRow)).all()
        assert len(rows) == 1


def test_latest_director_attempt_is_owner_scoped_and_stably_ordered(tmp_path: Path) -> None:
    first, _, sessions = repositories(tmp_path / "director-latest.sqlite")
    engine = sessions.kw["bind"]
    assert engine is not None
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE director_generation_attempts ("
                "id CHAR(32) PRIMARY KEY, owner_id CHAR(32) NOT NULL, "
                "group_id CHAR(32) NOT NULL, expected_draft_revision_id CHAR(32), "
                "idempotency_key VARCHAR(128) NOT NULL, "
                "provider_idempotency_key VARCHAR(64) NOT NULL, "
                "canonical_request_sha256 VARCHAR(64) NOT NULL, input_json JSON NOT NULL, "
                "prompt_ref VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL, "
                "prompt_content TEXT NOT NULL, model VARCHAR(128) NOT NULL, "
                "parameters_json JSON NOT NULL, status VARCHAR(32) NOT NULL, "
                "state_revision INTEGER NOT NULL, lease_token CHAR(32), lease_until DATETIME, "
                "provider_receipt TEXT, raw_response_sha256 VARCHAR(64), "
                "draft_revision_id CHAR(32), failure_code VARCHAR(128), "
                "history_json JSON NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
    owner, other_owner, project, group = uuid4(), uuid4(), uuid4(), uuid4()
    timestamp = datetime(2026, 8, 29, tzinfo=UTC)
    lower_id = director_attempt(
        attempt_id=UUID(int=1),
        owner_id=owner,
        project_id=project,
        group_id=group,
        created_at=timestamp,
    )
    higher_id = director_attempt(
        attempt_id=UUID(int=2),
        owner_id=owner,
        project_id=project,
        group_id=group,
        created_at=timestamp,
    )
    foreign = director_attempt(
        attempt_id=UUID(int=3),
        owner_id=other_owner,
        project_id=project,
        group_id=group,
        created_at=timestamp + timedelta(minutes=1),
    )
    with sessions.begin() as session:
        session.add_all(
            [
                _director_attempt_row(lower_id),
                _director_attempt_row(higher_id),
                _director_attempt_row(foreign),
            ]
        )

    latest = first.latest_for_group(owner, group)
    assert latest is not None and latest.attempt_id == higher_id.attempt_id
    assert first.latest_for_group(uuid4(), group) is None
    assert first.latest_for_group(owner, uuid4()) is None
    with sessions() as session:
        assert session.scalar(select(DirectorGenerationAttemptRow.id)) is not None


def test_two_repository_workers_get_one_processing_lease(tmp_path: Path) -> None:
    first, second, sessions = repositories(tmp_path / "lease.sqlite")
    value = generation(1, GenerationStatus.RUNNING)
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state) "
                "VALUES (:id, :owner, 'ACTIVE')"
            ),
            {"id": value.project_id.hex, "owner": value.owner_id.hex},
        )
        session.add(_generation_row(value))
    now = datetime.now(UTC)

    def claim(repo: SqlAlchemyStoryboardRepository) -> StoryboardGeneration | None:
        return repo.claim_processing(
            value.owner_id,
            value.generation_id,
            value.state_revision,
            uuid4(),
            now,
            now + timedelta(minutes=5),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(claim, (first, second)))
    assert sum(item is not None for item in claims) == 1


def test_metadata_registration_and_two_session_state_cas_survive_restart(tmp_path: Path) -> None:
    names = storyboard_metadata().tables
    assert "storyboard_generations" in names
    assert "authoritative_narration_groups" in names
    assert "latest_narration_groups" not in names
    assert "storyboard_project_states" not in names
    source_root = Path(__file__).parents[2] / "src" / "tubeflow_test"
    definitions = [
        path
        for path in source_root.rglob("*.py")
        if '__tablename__ = "authoritative_narration_groups"'
        in path.read_text(encoding="utf-8")
    ]
    assert len(definitions) == 1
    assert definitions[0].name == "narration.py"
    assert [table.name for table in storyboard_metadata().sorted_tables]
    first, second, sessions = repositories(tmp_path / "cas.sqlite")
    value = generation(1, GenerationStatus.SUBMITTED)
    with sessions.begin() as session:
        session.add(_generation_row(value))
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state) VALUES (:id,:owner,'ACTIVE')"
            ),
            {"id": value.project_id.hex, "owner": value.owner_id.hex},
        )
    stale_a = first.get_generation(value.owner_id, value.generation_id)
    stale_b = second.get_generation(value.owner_id, value.generation_id)
    assert stale_a is not None and stale_b is not None
    running = first.save_generation(replace(stale_a, status=GenerationStatus.RUNNING))
    lost = second.save_generation(replace(stale_b, status=GenerationStatus.SUBMITTED))
    restarted = SqlAlchemyStoryboardRepository(first._uow)  # noqa: SLF001
    current = restarted.get_generation(value.owner_id, value.generation_id)
    assert current is not None
    assert running.status is GenerationStatus.RUNNING
    assert lost.status is GenerationStatus.RUNNING
    assert current.status is GenerationStatus.RUNNING
    assert current.state_revision == 1


def test_independent_current_pointer_never_regresses_and_asset_id_round_trips(
    tmp_path: Path,
) -> None:
    first, second, sessions = repositories(tmp_path / "current.sqlite")
    base = generation(2, GenerationStatus.OBJECT_STORED)
    newer = replace(base, generation_id=uuid4(), version=3, idempotency_key="key-3")
    with sessions.begin() as session:
        session.add_all([_generation_row(base), _generation_row(newer)])
        session.add(
            StoryboardCurrentRow(
                owner_id=base.owner_id, group_id=base.group_id, generation_id=None, version=0
            )
        )
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state) "
                "VALUES (:id, :owner, 'ACTIVE')"
            ),
            {"id": base.project_id.hex, "owner": base.owner_id.hex},
        )

    def succeeded(value: StoryboardGeneration) -> StoryboardGeneration:
        media = MediaAssetRef(uuid4(), "d" * 64, "image/png", 20)
        asset = StoryboardAsset(
            uuid4(), value.generation_id, StoryboardAssetKind.GRID_SHEET, media, 10, 10, "PNG"
        )
        with sessions.begin() as session:
            session.execute(
                text(
                    "INSERT INTO media_assets "
                    "(id,owner_id,project_id,kind,object_key,sha256,media_type,"
                    "size_bytes,metadata_json,shared,deletion_state) VALUES "
                    "(:id,:owner,:project,'STORYBOARD_GRID',:key,:sha,'image/png',"
                    "20,'{}',1,'ACTIVE')"
                ),
                {
                    "id": media.asset_id.hex,
                        "owner": value.owner_id.hex,
                        "project": value.project_id.hex,
                    "key": str(media.asset_id),
                    "sha": media.sha256,
                },
            )
            session.add(
                ProjectAssetReferenceRow(
                    project_id=value.project_id, asset_id=media.asset_id, owner_id=value.owner_id
                )
            )
        return replace(
            value,
            status=GenerationStatus.SUCCEEDED,
            object_asset=media,
            qc_width=10,
            qc_height=10,
            qc_mime="image/png",
            asset=asset,
        )

    newer_done = second.complete_and_switch_current(succeeded(newer))
    first.complete_and_switch_current(succeeded(base))
    current = first.get_current_generation(base.owner_id, base.group_id)
    assert current is not None and current.generation_id == newer.generation_id
    assert current.asset is not None
    assert current.asset.storyboard_asset_id == newer_done.asset.storyboard_asset_id
    with sessions() as session:
        assert session.scalar(select(StoryboardCurrentRow.version)) == 3


def test_video_binding_owner_validation_and_sql_round_trip(tmp_path: Path) -> None:
    first, _, sessions = repositories(tmp_path / "binding.sqlite")
    value = generation(1, GenerationStatus.OBJECT_STORED)
    media = MediaAssetRef(uuid4(), "d" * 64, "image/png", 20)
    asset = StoryboardAsset(
        uuid4(), value.generation_id, StoryboardAssetKind.GRID_SHEET, media, 10, 10, "PNG"
    )
    value = replace(value, status=GenerationStatus.SUCCEEDED, asset=asset)
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state) VALUES (:id,:owner,'ACTIVE')"
            ),
            {"id": value.project_id.hex, "owner": value.owner_id.hex},
        )
        session.execute(
            text(
                "INSERT INTO media_assets "
                "(id,owner_id,project_id,kind,object_key,sha256,media_type,"
                "size_bytes,metadata_json,shared,deletion_state) VALUES "
                "(:id,:owner,:project,'STORYBOARD_GRID',:key,:sha,'image/png',"
                "20,'{}',1,'ACTIVE')"
            ),
            {
                "id": media.asset_id.hex,
                    "owner": value.owner_id.hex,
                    "project": value.project_id.hex,
                "key": str(media.asset_id),
                "sha": media.sha256,
            },
        )
        session.add(_generation_row(value))
        session.add(
            ProjectAssetReferenceRow(
                project_id=value.project_id, asset_id=media.asset_id, owner_id=value.owner_id
            )
        )
        session.add(
            StoryboardMediaRow(
                generation_id=value.generation_id,
                storyboard_asset_id=asset.storyboard_asset_id,
                asset_id=media.asset_id,
                width=10,
                height=10,
                image_format="PNG",
            )
        )
        session.add(
            StoryboardCurrentRow(
                owner_id=value.owner_id,
                group_id=value.group_id,
                generation_id=value.generation_id,
                version=value.version,
            )
        )
    binding = VideoStoryboardBinding(
        uuid4(),
        value.owner_id,
        value.project_id,
        value.group_id,
        value.generation_id,
        value.applied_revision_id,
        media.sha256,
    )
    with sessions.begin() as session:
        first.bind_current_in_transaction(session, binding)
    assert first.get_video_binding(value.owner_id, binding.video_id) == binding
    assert first.get_video_binding(uuid4(), binding.video_id) is None


def test_project_purge_foreign_keys_are_declared_cascading() -> None:
    generation_fks = {
        fk.target_fullname: fk.ondelete for fk in StoryboardGenerationRow.__table__.foreign_keys
    }
    binding_fks = {
        fk.target_fullname: fk.ondelete for fk in VideoStoryboardBindingRow.__table__.foreign_keys
    }
    assert generation_fks["projects.id"] == "CASCADE"
    assert generation_fks["authoritative_narration_groups.id"] == "CASCADE"
    assert binding_fks["storyboard_generations.id"] == "CASCADE"


def test_director_draft_revision_controlled_semantics_round_trip(tmp_path: Path) -> None:
    """§20：草稿修订的受控语义与系统审批经 SQL JSON 载荷完整往返。"""
    first, _second, sessions = repositories(tmp_path / "draft-semantics.sqlite")
    owner, project, group, sentence = uuid4(), uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, production_state) "
                "VALUES (:id, :owner, 'ACTIVE')"
            ),
            {"id": project.hex, "owner": owner.hex},
        )
    semantics = (
        DirectorSemanticEvent.ESTABLISH_CONTEXT,
        DirectorSubjectFocus.ENVIRONMENT,
        DirectorTimeRelation.TIMELESS,
        DirectorLocationRelation.NON_SPATIAL,
    )
    shots = tuple(
        DirectorShot(
            uuid4(), order, "scene", "wide", "eye", "arrive", "establish", "desc", *semantics
        )
        for order in (1, 2)
    )
    approval = ContinuousObservationApproval(
        shots[0].shot_id, shots[1].shot_id, "AUTO 系统审批：测试理由"
    )
    revision = DirectorPlanRevision(
        uuid4(),
        owner,
        project,
        group,
        1,
        DirectorRevisionKind.DRAFT,
        shots,
        tuple(ShotSentenceLink(shot.shot_id, sentence) for shot in shots),
        (sentence,),
        ReviewState.APPROVED,
        (approval,),
        datetime(2026, 10, 12, tzinfo=UTC),
    )

    stored = first.create_revision(revision, None)
    loaded = first.get_draft_revision(owner, group)

    assert loaded is not None and loaded.revision_id == stored.revision_id
    assert loaded.review_state is ReviewState.APPROVED
    for loaded_shot in loaded.shots:
        assert loaded_shot.semantic_event is DirectorSemanticEvent.ESTABLISH_CONTEXT
        assert loaded_shot.subject_focus is DirectorSubjectFocus.ENVIRONMENT
        assert loaded_shot.time_relation is DirectorTimeRelation.TIMELESS
        assert loaded_shot.location_relation is DirectorLocationRelation.NON_SPATIAL
    assert loaded.continuous_observation_approvals == (approval,)


def test_prompt_safety_audit_round_trips_through_input_json(tmp_path: Path) -> None:
    # 2026-10-14：提示词安全替换的命中规则随生成输入持久化并可回读（审计）；
    # 修复前写入的历史行（无该键）回读为默认空元组，不报错。
    first, _second, sessions = repositories(tmp_path / "prompt-safety.sqlite")
    value = replace(
        generation(1, GenerationStatus.INTENT_PERSISTED),
        prompt_safety=("age_marker_zh", "sensational_wording"),
    )
    legacy = generation(2, GenerationStatus.INTENT_PERSISTED)
    legacy_row = _generation_row(legacy)
    legacy_row.input_json.pop("prompt_safety")
    with sessions.begin() as session:
        session.add(_generation_row(value))
        session.add(legacy_row)
        for project_id, owner_id in (
            (value.project_id, value.owner_id),
            (legacy.project_id, legacy.owner_id),
        ):
            session.execute(
                text(
                    "INSERT INTO projects (id, owner_id, production_state) "
                    "VALUES (:id,:owner,'ACTIVE')"
                ),
                {"id": project_id.hex, "owner": owner_id.hex},
            )

    loaded = first.get_generation(value.owner_id, value.generation_id)
    loaded_legacy = first.get_generation(legacy.owner_id, legacy.generation_id)

    assert loaded is not None
    assert loaded.prompt_safety == ("age_marker_zh", "sensational_wording")
    assert loaded_legacy is not None and loaded_legacy.prompt_safety == ()


def test_shot_time_ranges_algorithm_round_trips_through_input_json(tmp_path: Path) -> None:
    """2026-10-16：计时算法键随生成输入持久化并可回读；历史行（无该键）回读为空。"""
    first, _second, sessions = repositories(tmp_path / "shot-timing-algorithm.sqlite")
    value = replace(
        generation(1, GenerationStatus.INTENT_PERSISTED),
        shot_time_ranges_snapshot=(ShotTimeRange(1, 0, 3000),),
        shot_time_ranges_algorithm="monotone-projection-v1",
    )
    legacy = generation(2, GenerationStatus.INTENT_PERSISTED)
    legacy_row = _generation_row(legacy)
    legacy_row.input_json.pop("shot_time_ranges_algorithm", None)
    with sessions.begin() as session:
        session.add(_generation_row(value))
        session.add(legacy_row)
        for project_id, owner_id in (
            (value.project_id, value.owner_id),
            (legacy.project_id, legacy.owner_id),
        ):
            session.execute(
                text(
                    "INSERT INTO projects (id, owner_id, production_state) "
                    "VALUES (:id,:owner,'ACTIVE')"
                ),
                {"id": project_id.hex, "owner": owner_id.hex},
            )

    loaded = first.get_generation(value.owner_id, value.generation_id)
    loaded_legacy = first.get_generation(legacy.owner_id, legacy.generation_id)

    assert loaded is not None
    assert loaded.shot_time_ranges_algorithm == "monotone-projection-v1"
    assert [
        (item.shot_order, item.start_ms, item.end_ms)
        for item in loaded.shot_time_ranges_snapshot
    ] == [(1, 0, 3000)]
    assert loaded_legacy is not None
    assert loaded_legacy.shot_time_ranges_algorithm == ""
