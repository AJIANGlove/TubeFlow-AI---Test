from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from tubeflow_test.adapters.video_content_qc import FfmpegBlackFrameSampler
from tubeflow_test.video_generation.adapters import FfprobeVideoProbe
from tubeflow_test.video_generation.api import AuthenticatedOwner, create_group_video_router
from tubeflow_test.video_generation.authority import (
    LatestAuthorityLoader,
    NarrationAuthorityPort,
    ReferencePolicyAuthorityPort,
    StoryboardAuthorityPort,
    VideoWriteGuard,
)
from tubeflow_test.video_generation.engine import EngineTimeouts, HttpKineStackEngine
from tubeflow_test.video_generation.gpu_probe import GpuStatusProbe, HttpGpuStatusProbe
from tubeflow_test.video_generation.orchestration import (
    AssetStore,
    AuthoritativeGroupInput,
    DurableGroupVideoOrchestrator,
    StallPolicy,
)
from tubeflow_test.video_generation.sqlalchemy_repository import SqlAlchemyVideoRepository
from tubeflow_test.video_prompt.compiler import VideoPromptCompiler


@dataclass(frozen=True, slots=True)
class VideoFeatureDependencies:
    repository: SqlAlchemyVideoRepository
    engine: HttpKineStackEngine
    assets: AssetStore
    probe: FfprobeVideoProbe
    router: APIRouter
    service: DurableGroupVideoOrchestrator
    gpu_probe: GpuStatusProbe | None = None

    def close(self) -> None:
        self.engine.close()
        if isinstance(self.gpu_probe, HttpGpuStatusProbe):
            self.gpu_probe.close()


def build_video_feature(
    *,
    database_engine: Engine,
    group_loader: Callable[[Session, UUID, UUID], AuthoritativeGroupInput] | None = None,
    narration_authority: NarrationAuthorityPort | None = None,
    storyboard_authority: StoryboardAuthorityPort | None = None,
    write_guard: VideoWriteGuard,
    token_resolver: Callable[[str], str],
    authenticated_owner: AuthenticatedOwner,
    timeouts: EngineTimeouts | None = None,
    asset_store: AssetStore,
    prompt_compiler: VideoPromptCompiler | None = None,
    stall_policy: StallPolicy | None = None,
    gpu_probe: GpuStatusProbe | None = None,
    reference_policy_authority: ReferencePolicyAuthorityPort | None = None,
) -> VideoFeatureDependencies:
    sessions = sessionmaker(database_engine, expire_on_commit=False)
    if group_loader is None:
        if narration_authority is None or storyboard_authority is None:
            raise ValueError("latest narration and storyboard authority ports are required")
        group_loader = LatestAuthorityLoader(
            narration_authority, storyboard_authority, reference_policy_authority
        )
    repository = SqlAlchemyVideoRepository(
        sessions, group_loader, token_resolver, intent_group_loader=group_loader
    )
    engine = HttpKineStackEngine(timeouts=timeouts)
    assets = asset_store
    probe = FfprobeVideoProbe()
    # 内容级 QC（黑屏类画面缺陷，2026-10-16 决策）：与既有 ffprobe 探测同一 30s 子进程超时口径。
    content_sampler = FfmpegBlackFrameSampler(timeout_seconds=30.0)
    service = DurableGroupVideoOrchestrator(
        repository,
        engine,
        assets,
        probe,
        write_guard=write_guard,
        prompt_compiler=prompt_compiler,
        stall_policy=stall_policy,
        gpu_probe=gpu_probe,
        content_sampler=content_sampler,
    )
    router = create_group_video_router(service, authenticated_owner)
    return VideoFeatureDependencies(repository, engine, assets, probe, router, service, gpu_probe)
