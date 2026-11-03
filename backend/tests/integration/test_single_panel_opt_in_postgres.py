from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import PostgresDatabase, ProductionParentChain
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.identity_project.models import ProjectRow
from tubeflow_test.adapters.persistence.models import MediaAssetRow, ProjectAssetReferenceRow
from tubeflow_test.adapters.persistence.production_authority import SqlCanonicalVideoGroupLoader
from tubeflow_test.adapters.persistence.schema import canonical_metadata
from tubeflow_test.config import Settings
from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink
from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    ReferencePolicy,
    ReferenceStrategy,
    ShotPanelImage,
    VideoRuleViolation,
)
from tubeflow_test.video_generation.orchestration import (
    AuthoritativeGroupInput,
    PersistentAttempt,
    prepare_frozen_single_panel_replay_intent,
)
from tubeflow_test.video_generation.persistence import (
    H3ProjectWorkflowRouteRecord,
    VideoAttemptRecord,
    VideoOutboxRecord,
)
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository


@pytest.mark.integration
def test_fresh_reference_policy_schema_matches_metadata(
    postgres_database: PostgresDatabase,
) -> None:
    with postgres_database.engine.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(connection), canonical_metadata()
        )
    assert differences == []

    columns = {
        item["name"]: item
        for item in inspect(postgres_database.engine).get_columns("h3_project_workflow_routes")
    }
    assert columns["reference_strategy"]["nullable"] is True
    assert columns["reference_derivation_json"]["nullable"] is True


class FakePanelDerivatives:
    def __init__(self) -> None:
        self.content = b"fake-panel"
        self.fail = False

    def derive(
        self,
        session: Session,
        source: AuthoritativeGroupInput,
        policy: ReferencePolicy,
        *,
        for_intent: bool,
    ) -> tuple[ShotPanelImage, ...]:
        digest = hashlib.sha256(self.content).hexdigest()
        asset_id = UUID(digest[:32])
        asset = MediaAssetRef(asset_id, digest, "image/png", len(self.content))
        if for_intent:
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=source.owner_id,
                    project_id=source.project_id,
                    ownership_scope="PROJECT",
                    kind="H3_SHOT_PANEL",
                    object_key=f"test-panels/{source.project_id}/{asset_id}.png",
                    sha256=digest,
                    media_type="image/png",
                    size_bytes=len(self.content),
                    metadata_json={"derivation": policy.derivation_payload},
                    shared=False,
                )
            )
            session.add(
                ProjectAssetReferenceRow(
                    project_id=source.project_id,
                    asset_id=asset_id,
                    owner_id=source.owner_id,
                )
            )
            session.flush()
            if self.fail:
                raise RuntimeError("derivative failed after media insert")
        return (
            ShotPanelImage(
                asset,
                1,
                source.storyboard_image.sha256,
                "fake-crop-v1:shot-1",
            ),
        )


def _prepared_attempt(source: AuthoritativeGroupInput) -> PersistentAttempt:
    attempt_id = uuid4()
    prompt = "P2 fake replay prompt"
    snapshot, generation_sha, request, request_sha = prepare_frozen_single_panel_replay_intent(
        source,
        attempt_id,
        replay_id="p2-transaction-test",
        prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
    )
    return PersistentAttempt(
        attempt_id,
        source.owner_id,
        source.project_id,
        source.group_id,
        source.generation_id,
        "p2-transaction-test",
        generation_sha,
        request_sha,
        request,
        input_snapshot=snapshot,
    )


def _fixture_source(
    ids: ProductionParentChain, generation_id: UUID
) -> AuthoritativeGroupInput:
    audio = MediaAssetRef(uuid4(), hashlib.sha256(b"audio").hexdigest(), "audio/wav", 5)
    character = MediaAssetRef(uuid4(), hashlib.sha256(b"character").hexdigest(), "image/png", 9)
    storyboard = MediaAssetRef(uuid4(), hashlib.sha256(b"board").hexdigest(), "image/png", 5)
    shot = DirectorShot(
        uuid4(), 1, "scene", "WIDE", "OBJECTIVE", "action", "function", "description"
    )
    sentence_id = uuid4()
    revision_id = uuid4()
    return AuthoritativeGroupInput(
        owner_id=ids.owner_id,
        project_id=ids.project_id,
        group_id=ids.group_id,
        generation_id=generation_id,
        snapshot_frozen=True,
        script_text_sha256=hashlib.sha256(b"fixture").hexdigest(),
        sentence_text_sha256=("1" * 64,),
        narrated_text_sha256=("1" * 64,),
        sentence_durations_ms=(3000,),
        narration_qc_passed=(True,),
        normalized_audio_assets=(audio,),
        timeline_ranges_ms=((0, 3000),),
        group_sentence_indexes=(0,),
        group_start_ms=0,
        group_end_ms=3000,
        group_target_duration_ms=3000,
        character_images=(character,),
        storyboard_image=storyboard,
        storyboard_kind="GRID_SHEET",
        storyboard_is_current=True,
        applied_revision_id=revision_id,
        storyboard_applied_revision_id=revision_id,
        prompt="fixture prompt",
        aspect_ratio=AspectRatio.LANDSCAPE,
        seed=1,
        workflow=ImmutableWorkflowSnapshot(
            "h3",
            "1",
            "workflow",
            "1",
            hashlib.sha256(b"manifest").hexdigest(),
            "preview",
            {"parameters": {"resolution_preset": "preview"}},
        ),
        immutable_script="fixture",
        authoritative_group_sentence_indexes=(0,),
        raw_audio_assets=(audio,),
        storyboard_generation_id=generation_id,
        storyboard_asset_sha256=storyboard.sha256,
        sentence_sample_counts=(144000,),
        sentence_sample_rates=(48000,),
        timeline_ranges_frames=((0, 144000),),
        group_start_frame=0,
        group_end_frame=144000,
        group_sample_rate=48000,
        authority_snapshot={"fixture": "p2"},
        director_shots=(shot,),
        shot_sentence_links=(ShotSentenceLink(shot.shot_id, sentence_id),),
        group_sentence_ids=(sentence_id,),
        visual_style="fixture style",
    )


def _repository(
    database: PostgresDatabase,
    ids: ProductionParentChain,
    generation_id: UUID,
) -> tuple[SqlAlchemyVideoRepository, AuthoritativeGroupInput, FakePanelDerivatives]:
    source = _fixture_source(ids, generation_id)
    derivatives = FakePanelDerivatives()
    policy = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "fake-crop-v1"})
    authority = SqlCanonicalVideoGroupLoader(
        Settings(_env_file=None),
        reference_policy=policy,
        single_panel_allowed=lambda _project_id: True,
        panel_derivatives=derivatives,
    )

    def load(
        session: Session,
        owner_id: UUID,
        group_id: UUID,
        *,
        for_intent: bool = False,
    ) -> AuthoritativeGroupInput:
        project = session.get(ProjectRow, ids.project_id)
        if owner_id != ids.owner_id or group_id != ids.group_id or project is None:
            raise VideoRuleViolation("narration group not found")
        if project.owner_id != owner_id:
            raise VideoRuleViolation("narration group not found")
        return authority.apply_reference_policy(session, source, for_intent=for_intent)

    repository = SqlAlchemyVideoRepository(
        database.sessions,
        lambda session, owner_id, group_id: load(session, owner_id, group_id),
        token_resolver=lambda _node_id: "fake-token",
        intent_group_loader=load,
    )
    return repository, source, derivatives


def _counts(database: PostgresDatabase) -> tuple[int, int, int, int]:
    with database.sessions() as session:
        return tuple(
            int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                H3ProjectWorkflowRouteRecord,
                MediaAssetRow,
                VideoAttemptRecord,
                VideoOutboxRecord,
            )
        )  # type: ignore[return-value]


def test_p2_preview_has_no_writes_then_intent_binds_atomically(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    ids, generation_id = storyboard_parent_chain
    repository, _source, _derivatives = _repository(postgres_database, ids, generation_id)
    preview = repository.load_authoritative_group(ids.owner_id, ids.group_id)
    assert preview.reference_policy is not None
    assert len(preview.shot_panel_images) == 1
    assert _counts(postgres_database) == (0, 0, 0, 0)

    created = repository.create_attempt(_prepared_attempt(preview))

    assert created.project_id == ids.project_id
    assert _counts(postgres_database) == (1, 1, 1, 1)
    with postgres_database.sessions() as session:
        route = session.get(H3ProjectWorkflowRouteRecord, ids.project_id)
        assert route is not None
        assert route.reference_strategy == "single_panel_v1"
        assert route.reference_derivation_json == {"version": "fake-crop-v1"}
        media = session.scalar(select(MediaAssetRow))
        assert media is not None and media.kind == "H3_SHOT_PANEL"


def test_p2_derivative_failure_rolls_back_policy_media_attempt_and_outbox(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    ids, generation_id = storyboard_parent_chain
    repository, _source, derivatives = _repository(postgres_database, ids, generation_id)
    preview = repository.load_authoritative_group(ids.owner_id, ids.group_id)
    derivatives.fail = True
    with pytest.raises(RuntimeError, match="derivative failed"):
        repository.create_attempt(_prepared_attempt(preview))
    assert _counts(postgres_database) == (0, 0, 0, 0)


def test_p2_preview_drift_rolls_back_all_writes(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    ids, generation_id = storyboard_parent_chain
    repository, _source, derivatives = _repository(postgres_database, ids, generation_id)
    preview = repository.load_authoritative_group(ids.owner_id, ids.group_id)
    derivatives.content = b"changed-after-compile"
    with pytest.raises(VideoRuleViolation, match="video_reference_input_changed"):
        repository.create_attempt(_prepared_attempt(preview))
    assert _counts(postgres_database) == (0, 0, 0, 0)


def test_p2_concurrent_first_create_keeps_one_policy_media_attempt_and_outbox(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    ids, generation_id = storyboard_parent_chain
    repository, _source, _derivatives = _repository(postgres_database, ids, generation_id)
    preview = repository.load_authoritative_group(ids.owner_id, ids.group_id)
    attempt: PersistentAttempt = _prepared_attempt(preview)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(repository.create_attempt, attempt) for _ in range(2)]
        results = [future.result() for future in futures]
    assert {item.attempt_id for item in results} == {attempt.attempt_id}
    assert _counts(postgres_database) == (1, 1, 1, 1)


def test_p2_cross_account_preview_has_no_policy_or_media_writes(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
) -> None:
    ids, generation_id = storyboard_parent_chain
    repository, _source, _derivatives = _repository(postgres_database, ids, generation_id)
    with pytest.raises(VideoRuleViolation, match="narration group not found"):
        repository.load_authoritative_group(uuid4(), ids.group_id)
    assert _counts(postgres_database) == (0, 0, 0, 0)
