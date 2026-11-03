"""封面 Temporal 工作流：参考图解析 / 角色卡解析 / 提示词生成 / 封面图生成
（照 workflows/storyboards.py）。

四个动作均为同步请求-响应的单一活动：工作流函数本身确定性（仅执行一次活动
并返回其终态字符串）；Provider 调用全部在活动内执行。重试语义照故事板——
DomainError（既定失败，含 LLM 4xx/渠道故障/校验失败）由活动落 FAILED 终态或
以 ``FAILED:<code>`` 返回，不重试；未预期异常交由 Temporal 默认策略重试。
"""

from __future__ import annotations

from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from tubeflow_test.application.cover_outbox import (
    COVER_AGGREGATE_STYLE_CHANNEL,
    COVER_CHARACTER_CARD_ANALYSIS_REQUESTED,
    COVER_IMAGE_GENERATION_REQUESTED,
    COVER_PROMPT_GENERATION_REQUESTED,
    COVER_REF_ANALYSIS_REQUESTED,
    CoverOutboxEvent,
)
from tubeflow_test.domain.errors import DomainError

if TYPE_CHECKING:
    from tubeflow_test.application.cover_characters import CoverCharacterService
    from tubeflow_test.application.cover_images import CoverImageService
    from tubeflow_test.application.cover_prompts import CoverPromptService
    from tubeflow_test.application.cover_style import CoverStyleService


@dataclass(frozen=True)
class CoverWorkflowInput:
    owner_id: str
    aggregate_id: str
    request_sha256: str
    scope: str = ""


@dataclass(slots=True)
class CoverActivities:
    style: CoverStyleService
    prompts: CoverPromptService
    images: CoverImageService
    characters: CoverCharacterService

    @activity.defn(name="analyze_cover_ref_style")
    def analyze_style(self, value: CoverWorkflowInput) -> str:
        try:
            return self.style.run_ref_analysis(
                UUID(value.owner_id), value.scope, UUID(value.aggregate_id)
            )
        except DomainError as exc:
            return f"FAILED:{exc.code}"

    @activity.defn(name="analyze_cover_character_card")
    def analyze_character_card(self, value: CoverWorkflowInput) -> str:
        try:
            return self.characters.run_card_analysis(
                UUID(value.owner_id), UUID(value.aggregate_id)
            )
        except DomainError as exc:
            return f"FAILED:{exc.code}"

    @activity.defn(name="generate_cover_prompt")
    async def generate_prompt(self, value: CoverWorkflowInput) -> str:
        try:
            result = await self.prompts.generate(
                UUID(value.owner_id), UUID(value.aggregate_id)
            )
            return result.status
        except DomainError as exc:
            return f"FAILED:{exc.code}"

    @activity.defn(name="generate_cover_image")
    def generate_image(self, value: CoverWorkflowInput) -> str:
        try:
            result = self.images.generate(UUID(value.owner_id), UUID(value.aggregate_id))
            return result.status
        except DomainError as exc:
            return f"FAILED:{exc.code}"


@workflow.defn(name="CoverRefAnalysisWorkflow")
class CoverRefAnalysisWorkflow:
    @workflow.run
    async def run(self, value: CoverWorkflowInput) -> str:
        return str(await workflow.execute_activity(
            "analyze_cover_ref_style",
            value,
            start_to_close_timeout=timedelta(minutes=10),
        ))


@workflow.defn(name="CoverCharacterCardAnalysisWorkflow")
class CoverCharacterCardAnalysisWorkflow:
    @workflow.run
    async def run(self, value: CoverWorkflowInput) -> str:
        return str(await workflow.execute_activity(
            "analyze_cover_character_card",
            value,
            start_to_close_timeout=timedelta(minutes=10),
        ))


@workflow.defn(name="CoverPromptGenerationWorkflow")
class CoverPromptGenerationWorkflow:
    @workflow.run
    async def run(self, value: CoverWorkflowInput) -> str:
        return str(await workflow.execute_activity(
            "generate_cover_prompt",
            value,
            start_to_close_timeout=timedelta(minutes=20),
        ))


@workflow.defn(name="CoverImageGenerationWorkflow")
class CoverImageGenerationWorkflow:
    @workflow.run
    async def run(self, value: CoverWorkflowInput) -> str:
        return str(await workflow.execute_activity(
            "generate_cover_image",
            value,
            start_to_close_timeout=timedelta(minutes=20),
        ))


class TemporalCoverWorkflowStarter:
    def __init__(self, client: Client, task_queue: str) -> None:
        self._client, self._task_queue = client, task_queue

    async def start(self, event: CoverOutboxEvent) -> None:
        scope = ""
        if event.event_type == COVER_REF_ANALYSIS_REQUESTED:
            workflow_name = "CoverRefAnalysisWorkflow"
            # 确定 workflow_id（草稿+请求哈希）：合法的重新解析发生在旧运行 Completed 之后
            # （force 重解、覆盖校验未命中、勾选改回旧值），ALLOW_DUPLICATE_FAILED_ONLY 会把
            # 这类重提误判为重复并静默吞掉；TERMINATE_IF_RUNNING 允许完成/失败后的重提，
            # 同时终止失联在途运行，并发仍由 outbox 部分唯一索引保证。
            reuse_policy = WorkflowIDReusePolicy.TERMINATE_IF_RUNNING
            scope = (
                "channel"
                if event.aggregate_type == COVER_AGGREGATE_STYLE_CHANNEL
                else "project"
            )
        elif event.event_type == COVER_CHARACTER_CARD_ANALYSIS_REQUESTED:
            workflow_name = "CoverCharacterCardAnalysisWorkflow"
            # 与参考图解析同一显式动作语义：完成/失败后的重提必须可启动（同上，用
            # TERMINATE_IF_RUNNING 而非仅失败后重提）。
            reuse_policy = WorkflowIDReusePolicy.TERMINATE_IF_RUNNING
        elif event.event_type == COVER_PROMPT_GENERATION_REQUESTED:
            workflow_name = "CoverPromptGenerationWorkflow"
            reuse_policy = WorkflowIDReusePolicy.REJECT_DUPLICATE
        elif event.event_type == COVER_IMAGE_GENERATION_REQUESTED:
            workflow_name = "CoverImageGenerationWorkflow"
            reuse_policy = WorkflowIDReusePolicy.REJECT_DUPLICATE
        else:
            raise ValueError(f"unsupported cover outbox event: {event.event_type}")
        try:
            await self._client.start_workflow(
                workflow_name,
                CoverWorkflowInput(
                    str(event.owner_id),
                    str(event.aggregate_id),
                    event.request_sha256,
                    scope=scope,
                ),
                id=event.workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=reuse_policy,
            )
        except WorkflowAlreadyStartedError:
            return


def create_cover_worker(
    client: Client,
    task_queue: str,
    activities: CoverActivities,
    activity_executor: Executor,
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[
            CoverRefAnalysisWorkflow,
            CoverPromptGenerationWorkflow,
            CoverImageGenerationWorkflow,
            CoverCharacterCardAnalysisWorkflow,
        ],
        activities=[
            activities.analyze_style,
            activities.generate_prompt,
            activities.generate_image,
            activities.analyze_character_card,
        ],
        activity_executor=activity_executor,
    )
