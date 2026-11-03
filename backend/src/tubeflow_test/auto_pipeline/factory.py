"""AUTO 编排器的生产装配：把既有服务绑定为编排器端口。

只组合既有构件（运行仓储、事实视图、配音/导演/故事板/视频/合成服务），
不包含任何阶段规则；API 进程与 Worker 进程各自调用本工厂获得服务实例。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from tubeflow_test.adapters.persistence.auto_pipeline import (
    SqlAutoPipelineFacts,
    SqlAutoPipelineRunRepository,
)
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyCompositionOutbox,
    SqlAlchemyCompositionRepository,
)
from tubeflow_test.application.narration import ProjectNarrationService
from tubeflow_test.application.project_director import ProjectDirectorService
from tubeflow_test.application.storyboard_batch import StoryboardBatchService
from tubeflow_test.application.video_batch import GroupVideoBatchService
from tubeflow_test.auto_pipeline.service import AutoPipelineService
from tubeflow_test.domain.project_director import ProjectDirectorTrigger


class _DirectorResumeAdapter:
    """AUTO 恢复入口：经既有 prepare_run 前缀复用机制创建新一轮导演运行。"""

    def __init__(self, service: ProjectDirectorService) -> None:
        self._service = service

    def create_recovery_run(self, owner_id: UUID, project_id: UUID, *, round: int) -> None:
        self._service.create_run(
            owner_id=owner_id,
            project_id=project_id,
            # 确定性幂等键：同一授权轮次的重入/重放命中既有运行，不创建第二个 run。
            idempotency_key=f"auto-pipeline-director:{project_id}:r{round}",
            trigger=ProjectDirectorTrigger.AUTO_RESUME,
        )

    def materialize_if_ready(self, owner_id: UUID, project_id: UUID) -> None:
        self._service.materialize_if_ready(owner_id, project_id)


class _CompositionStartAdapter:
    """按 (inputs_hash, 预算轮次) 幂等入队合成（与手动入口同一仓储路径）。"""

    def __init__(
        self,
        repository: SqlAlchemyCompositionRepository,
        outbox: SqlAlchemyCompositionOutbox | None,
    ) -> None:
        self._repository = repository
        self._outbox = outbox

    def start(self, owner_id: UUID, project_id: UUID) -> None:
        self._repository.enqueue_composition(project_id, owner_id)

    def close_superseded(self, owner_id: UUID, project_id: UUID) -> None:
        if self._outbox is not None:
            self._outbox.close_superseded(owner_id, project_id)


def build_auto_pipeline_service(
    *,
    database: Database,
    narration_service: ProjectNarrationService,
    project_director: ProjectDirectorService | None,
    storyboards: StoryboardBatchService | None,
    videos: GroupVideoBatchService | None,
    composition_repository: SqlAlchemyCompositionRepository | None,
    composition_outbox: SqlAlchemyCompositionOutbox | None = None,
) -> AutoPipelineService:
    """组装 AUTO 编排器；缺失的阶段服务由 advance 防御性空转（不阻断其他阶段）。"""
    return AutoPipelineService(
        SqlAutoPipelineRunRepository(database.session),
        SqlAutoPipelineFacts(database.session),
        narration=narration_service,
        director=(
            _DirectorResumeAdapter(project_director) if project_director is not None else None
        ),
        storyboards=storyboards,
        videos=videos,
        compositions=(
            _CompositionStartAdapter(composition_repository, composition_outbox)
            if composition_repository is not None
            else None
        ),
        clock=lambda: datetime.now(UTC),
    )
