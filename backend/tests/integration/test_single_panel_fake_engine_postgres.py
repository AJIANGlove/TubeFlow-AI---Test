from __future__ import annotations

# Prompt fixture strings are data; keep their exact line structure.
# ruff: noqa: E501
import hashlib
from dataclasses import replace
from uuid import UUID

import pytest
from conftest import PostgresDatabase, ProductionParentChain
from sqlalchemy import func, select
from test_shot_panel_assets_postgres import FakeObjects, _png
from test_single_panel_opt_in_postgres import _fixture_source

from tubeflow_test.adapters.persistence.production_authority import (
    SqlCanonicalVideoGroupLoader,
    single_panel_policy_settings,
)
from tubeflow_test.adapters.persistence.shot_panel_assets import ShotPanelAssetService
from tubeflow_test.adapters.persistence.storyboards.models import StoryboardCurrentRow
from tubeflow_test.config import Settings
from tubeflow_test.video_generation.adapters import SqlObjectVideoAssetStore
from tubeflow_test.video_generation.api import _view
from tubeflow_test.video_generation.authority import LatestAuthorityLoader
from tubeflow_test.video_generation.domain import ImmutableWorkflowSnapshot
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.orchestration import (
    ArchiveStage,
    AttemptStage,
    AuthoritativeGroupInput,
    DurableGroupVideoOrchestrator,
    VideoMediaEvidence,
)
from tubeflow_test.video_generation.persistence import (
    H3NodeRecord,
    H3ProjectWorkflowRouteRecord,
    MediaAssetRow,
    VideoAttemptRecord,
)
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler
from tubeflow_test.video_prompt.domain import VideoPromptStrategy
from tubeflow_test.video_prompt.provider import VideoPromptProviderResult


class Provider:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def compile(self, *, user_payload, **_kwargs) -> VideoPromptProviderResult:  # type: ignore[no-untyped-def]
        self.payloads.append(user_payload)
        text = (
            "subject_definitions:\n"
            "- (S1): A lantern-headed investigator with a dark cloak and one amber light.\n"
            "- Picture 2: The single-panel storyboard reference for Shot 1, used as the composition, action, and style reference for Shot 1 only.\n"
            "summary: A quiet investigation advances through the single-panel references in shot order.\n"
            "retention_analysis:\n"
            "- (S1) — fully_preserved: retain the cloak, silhouette and amber light.\n"
            "- Picture 2 — shot_plan_reference_only: retain Shot 1 composition and style; never reproduce reference borders, captions, or unrelated panel content as image content.\n"
            "detailed_description:\n"
            "Dark oil-painted illustration with restrained amber light and thick texture.\n"
            "[Shot 1] <Picture 2>: the investigator studies one map with a measured push-in.\n"
            "Maintain identity, map, candle and the navy-and-amber style.\n"
            "No storyboard grid, panel borders, split screen, captions, text, logos, or watermarks.\n"
            "overall_soundscape: quiet room tone and a soft candle hiss, no voices.\n"
            "non_diegetic_music: N/A"
        )
        return VideoPromptProviderResult(text, "receipt", "0" * 64)

    def close(self) -> None:
        pass


class Probe:
    def inspect(self, _content: bytes) -> VideoMediaEvidence:
        return VideoMediaEvidence(
            "video/mp4", 1056, 608, 24.0, 3100, 1, 1, ("mp4",), 3100, 74, 0, 3100, 3100, "aac"
        )


def _seed_input_media(
    database: PostgresDatabase,
    source: AuthoritativeGroupInput,
    objects: FakeObjects,
) -> None:
    content_by_asset = {
        source.character_images[0].asset_id: b"character",
        source.storyboard_image.asset_id: _png(),
        source.raw_audio_assets[0].asset_id: b"audio",  # type: ignore[index]
        source.normalized_audio_assets[0].asset_id: b"audio",  # type: ignore[index]
    }
    assets = tuple(
        {
            asset.asset_id: asset
            for asset in (
                source.character_images[0],
                source.storyboard_image,
                source.raw_audio_assets[0],
                source.normalized_audio_assets[0],
            )
            if asset is not None
        }.values()
    )
    with database.sessions.begin() as session:
        for asset in assets:
            assert asset is not None
            key = f"test-inputs/{source.project_id}/{asset.asset_id}"
            objects.values[key] = content_by_asset[asset.asset_id]
            session.add(
                MediaAssetRow(
                    id=asset.asset_id,
                    owner_id=source.owner_id,
                    project_id=source.project_id,
                    ownership_scope="PROJECT",
                    kind="TEST_INPUT",
                    object_key=key,
                    sha256=asset.sha256,
                    media_type=asset.media_type,
                    size_bytes=asset.size_bytes,
                    metadata_json={},
                    shared=False,
                )
            )


def _service(
    database: PostgresDatabase,
    ids: ProductionParentChain,
    generation_id: UUID,
    *,
    lose_submit_response: bool,
) -> tuple[
    DurableGroupVideoOrchestrator,
    SqlAlchemyVideoRepository,
    FakeEngine,
    FakeObjects,
    Provider,
    AuthoritativeGroupInput,
]:
    from tubeflow_test.domain.common import MediaAssetRef

    source = _fixture_source(ids, generation_id)
    storyboard_bytes = _png()
    source = replace(
        source,
        storyboard_image=MediaAssetRef(
            source.storyboard_image.asset_id,
            hashlib.sha256(storyboard_bytes).hexdigest(),
            "image/png",
            len(storyboard_bytes),
        ),
        storyboard_asset_sha256=hashlib.sha256(storyboard_bytes).hexdigest(),
    )
    workflow = ImmutableWorkflowSnapshot(
        "minimax-h3",
        "1",
        "h3.community.reference-images-to-video",
        "1.1.0-mvp",
        source.workflow.workflow_manifest_sha256,
        "preview",
        {
            "workflow_id": "h3.community.reference-images-to-video",
            "workflow_version": "1.1.0-mvp",
            "parameters": {"resolution_preset": "preview"},
        },
    )
    source = replace(source, workflow=workflow)
    objects = FakeObjects()
    _seed_input_media(database, source, objects)
    with database.sessions.begin() as session:
        session.add(
            StoryboardCurrentRow(
                owner_id=ids.owner_id,
                group_id=ids.group_id,
                generation_id=generation_id,
                version=1,
            )
        )
        session.add(
            H3NodeRecord(
                node_id="fake-node",
                base_url="http://fake-node",
                token_secret_ref="fake-token",
                capacity=1,
                claimed_slots=0,
                enabled=True,
                health="READY",
                capability="video.reference_images_to_video",
                model_key=workflow.model_key,
                model_version=workflow.model_version,
                workflow_id=workflow.workflow_id,
                workflow_version=workflow.workflow_version,
                manifest_sha256=workflow.workflow_manifest_sha256,
            )
        )
    assets = SqlObjectVideoAssetStore(database.sessions, objects)  # type: ignore[arg-type]
    settings = Settings(
        _env_file=None,
        h3_single_panel_opt_in_enabled=True,
        h3_single_panel_project_ids=(ids.project_id,),
    )
    requested, allowed = single_panel_policy_settings(settings)
    authority = SqlCanonicalVideoGroupLoader(
        settings,
        reference_policy=requested,
        single_panel_allowed=allowed,
        panel_derivatives=ShotPanelAssetService(database.sessions, objects, assets),  # type: ignore[arg-type]
    )

    class Narration:
        def load_group(self, _session, _owner_id, _group_id):  # type: ignore[no-untyped-def]
            return source

    class Storyboard:
        def bind_current(self, _session, narration):  # type: ignore[no-untyped-def]
            return narration

    loader = LatestAuthorityLoader(Narration(), Storyboard(), authority)
    repository = SqlAlchemyVideoRepository(
        database.sessions,
        loader,
        token_resolver=lambda _node: "fake-token",
        intent_group_loader=loader,
    )
    engine = FakeEngine()
    engine.lose_submit_response = lose_submit_response
    provider = Provider()
    compiler = VideoPromptCompiler(
        provider,
        model="fake-model",
        strategy=VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3,
    )
    service = DurableGroupVideoOrchestrator(
        repository,
        engine,
        assets,
        Probe(),
        prompt_compiler=compiler,  # type: ignore[arg-type]
    )
    return service, repository, engine, objects, provider, source


@pytest.mark.integration
@pytest.mark.parametrize("lose_submit_response", [False, True])
def test_single_panel_fake_engine_full_chain_and_unknown_recovers_original_attempt(
    postgres_database: PostgresDatabase,
    storyboard_parent_chain: tuple[ProductionParentChain, UUID],
    lose_submit_response: bool,
) -> None:
    ids, generation_id = storyboard_parent_chain
    service, repository, engine, objects, provider, source = _service(
        postgres_database, ids, generation_id, lose_submit_response=lose_submit_response
    )
    original_storyboard_sha = source.storyboard_image.sha256
    attempt = service.trigger(ids.owner_id, ids.group_id, "single-panel-full-chain")
    duplicate = service.trigger(ids.owner_id, ids.group_id, "single-panel-full-chain")
    assert duplicate.attempt_id == attempt.attempt_id
    assert len(provider.payloads) == 1
    assert attempt.input_snapshot is not None
    assert attempt.input_snapshot["video_prompt"]["strategy"] == (
        "tubeflow_storyboard_v3_single_panel_opt_in_v1"
    )
    assert attempt.input_snapshot["compiler_input"]["panels"][0]["shot_order"] == 1  # type: ignore[index]
    reloaded = repository.load_authoritative_group(ids.owner_id, ids.group_id)
    assert reloaded.authority_snapshot == attempt.input_snapshot["authority"]

    advanced = service.advance(ids.owner_id, attempt.attempt_id)
    if lose_submit_response:
        assert advanced.stage is AttemptStage.REMOTE_STATE_UNKNOWN, advanced.error_code
    else:
        assert advanced.stage is AttemptStage.SUBMITTED, advanced.error_code
    engine.inspect_status = "SUCCEEDED"
    restarted = DurableGroupVideoOrchestrator(
        repository,
        engine,
        service._assets,
        Probe(),
        prompt_compiler=service._prompt_compiler,
    )
    succeeded = restarted.resume(ids.owner_id, attempt.attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED
    archived = restarted.archive(ids.owner_id, attempt.attempt_id)
    assert archived.stage is AttemptStage.ARCHIVED, (
        archived.archive_stage,
        archived.error_code,
        archived.staged_ids,
        archived.cleaned_staged_ids,
    )
    assert archived.archive_stage is ArchiveStage.COMPLETE
    assert [call for _, call in engine.calls].count("submit") == 1

    with postgres_database.sessions() as session:
        route = session.get(H3ProjectWorkflowRouteRecord, ids.project_id)
        assert route is not None and route.reference_strategy == "single_panel_v1"
        assert session.scalar(select(func.count()).select_from(VideoAttemptRecord)) == 1
        kinds = set(session.scalars(select(MediaAssetRow.kind)).all())
        assert {"H3_SHOT_PANEL", "H3_ORIGINAL_MP4", "TEST_INPUT"} <= kinds
    assert source.storyboard_image.sha256 == original_storyboard_sha
    public = _view(archived)
    assert public["shot_panel_bindings"][0]["shot_order"] == 1
    assert "object_key" not in public["shot_panel_bindings"][0]
    assert public["prompt_strategy"] == "tubeflow_storyboard_v3_single_panel_opt_in_v1"
    assert objects.values
