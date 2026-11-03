"""封面三工作流的 Temporal 测试环境验证：顺利路径与 DomainError 不重试断言。

内存仓储 + fake Provider（零网络）；工作流执行一次活动到达终态；DomainError
（既定失败）由活动转为 FAILED 终态，活动不被 Temporal 重试（Provider 调用数断言）。
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image
from temporalio.testing import WorkflowEnvironment

from tubeflow_test.adapters.cover_image_qc import PillowCoverImageQc
from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.in_memory_cover_providers import (
    FakeCoverImageProvider,
    FakeCoverPromptProvider,
    FakeCoverRefAnalysisProvider,
)
from tubeflow_test.application.cover_characters import CoverCharacterService
from tubeflow_test.application.cover_images import CoverImageService
from tubeflow_test.application.cover_ports import CoverCharacterImageRef
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_prompts import CoverPromptService
from tubeflow_test.application.cover_providers import CoverImageResult, CoverLlmResult
from tubeflow_test.application.cover_style import CoverStyleService
from tubeflow_test.domain.cover import REF_DIMENSIONS
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.workflows.cover import (
    CoverActivities,
    CoverCharacterCardAnalysisWorkflow,
    CoverImageGenerationWorkflow,
    CoverPromptGenerationWorkflow,
    CoverRefAnalysisWorkflow,
    CoverWorkflowInput,
    create_cover_worker,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
TITLE = "测试标题"


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_at_key(self, key: str, body: bytes, media_type: str, expected_sha256: str) -> None:
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("hash mismatch")
        self.objects.setdefault(key, body)

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.objects[key]
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("integrity mismatch")
        return body


class FakeProjects:
    def __init__(self) -> None:
        self.contexts: dict[tuple[UUID, UUID], CoverProjectContext] = {}

    def add(self, owner_id: UUID, project_id: UUID, *, script: str | None = "冻结文案全文") -> None:
        self.contexts[(owner_id, project_id)] = CoverProjectContext(
            project_id, uuid4(), TITLE, script, "zh-CN"
        )

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


def png_bytes(width: int = 2560, height: int = 1440) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (8, 16, 32)).save(buffer, format="PNG")
    return buffer.getvalue()


def llm(payload: dict) -> CoverLlmResult:
    return CoverLlmResult(payload, "receipt", "model", "r" * 64)


def _text_payload() -> dict:
    return {"candidates": [{"text": "钩子", "pattern": "p"}], "recommended": 1}


def full_cover_payload() -> dict:
    dims = {
        dim.key: {child: f"{dim.label}{child}描述" for child in dim.children}
        for dim in REF_DIMENSIONS
    }
    return {"ratio": "16:9", "covers": [{"rank": 1, "forTitle": TITLE, "dims": dims}]}


def judge_payload() -> dict:
    scores = {key: 18 for key, _desc in (
        ("visual_impact", ""), ("thumbnail_clarity", ""), ("title_match", ""),
        ("text_visual_match", ""), ("script_fidelity", ""), ("style_consistency", ""),
        ("technical_quality", ""),
    )}
    return {
        "verdicts": [
            {"version": 1, "scores": scores, "comment": "好"},
            {"version": 2, "scores": scores, "comment": "也好"},
        ],
        "best": 1,
        "advice": "",
    }


@dataclass(slots=True)
class _Harness:
    repo: InMemoryCoverRepository
    storage: FakeStorage
    projects: FakeProjects
    analysis: FakeCoverRefAnalysisProvider
    prompt_provider: FakeCoverPromptProvider
    image_provider: FakeCoverImageProvider
    activities: CoverActivities
    style: CoverStyleService
    prompts: CoverPromptService
    images: CoverImageService
    characters: CoverCharacterService


@pytest.fixture
def harness(tmp_path: Path):
    repo = InMemoryCoverRepository()
    storage = FakeStorage()
    projects = FakeProjects()
    analysis = FakeCoverRefAnalysisProvider()
    prompt_provider = FakeCoverPromptProvider()
    image_provider = FakeCoverImageProvider()
    style = CoverStyleService(repo, repo, projects, storage, analysis, Ids(), Clock())
    prompts = CoverPromptService(
        repo, projects, prompt_provider, Ids(), Clock(), model="cover-model"
    )
    images = CoverImageService(
        repo,
        projects,
        image_provider,
        PillowCoverImageQc(),
        storage,
        Ids(),
        Clock(),
        tmp_path / "cover-work",
        model="gpt-image-2",
        size="2560x1440",
        quality="high",
    )
    characters = CoverCharacterService(repo, repo, storage, analysis, Ids(), Clock())
    activities = CoverActivities(style, prompts, images, characters)
    return _Harness(
        repo, storage, projects, analysis, prompt_provider, image_provider, activities,
        style, prompts, images, characters,
    )


@pytest.mark.asyncio
async def test_ref_analysis_workflow_success_and_domain_error_no_retry(harness) -> None:
    repo, analysis = harness.repo, harness.analysis
    activities, style = harness.activities, harness.style
    owner_id = uuid4()
    task_queue = f"cover-analysis-{uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with create_cover_worker(environment.client, task_queue, activities, executor):
                # 顺利路径
                channel_id = uuid4()
                repo.add_channel(owner_id, channel_id)
                content = png_bytes(8, 8)
                service = style
                image = service.upload_ref_image(
                    owner_id, scope="channel", channel_id=channel_id,
                    filename="ref.png", content=content,
                )
                service.save_draft(
                    owner_id, scope="channel", channel_id=channel_id,
                    ref_image_id=image.id, dims=["subject"], edited_text="",
                )
                analysis.queue(llm({"subject": {"人物身份": "青年女性"}}))
                result = await environment.client.execute_workflow(
                    CoverRefAnalysisWorkflow.run,
                    CoverWorkflowInput(str(owner_id), str(channel_id), "a" * 64, scope="channel"),
                    id=f"cover-analysis-ok-{uuid4()}",
                    task_queue=task_queue,
                )
                assert result == "SUCCEEDED"
                assert len(analysis.calls) == 1
                draft = repo.get_style_draft_for_channel(owner_id, channel_id)
                assert draft is not None and draft.edited_text == "主体：人物身份：青年女性"

                # DomainError（既定失败）：活动不重试，Provider 只被调用一次。
                other_channel = uuid4()
                repo.add_channel(owner_id, other_channel)
                other_image = service.upload_ref_image(
                    owner_id, scope="channel", channel_id=other_channel,
                    filename="ref.png", content=content,
                )
                service.save_draft(
                    owner_id, scope="channel", channel_id=other_channel,
                    ref_image_id=other_image.id, dims=["subject"], edited_text="",
                )
                analysis.queue(DomainError("cover_llm_provider_http_403", "forbidden"))
                failed = await environment.client.execute_workflow(
                    CoverRefAnalysisWorkflow.run,
                    CoverWorkflowInput(
                        str(owner_id), str(other_channel), "b" * 64, scope="channel"
                    ),
                    id=f"cover-analysis-fail-{uuid4()}",
                    task_queue=task_queue,
                )
                assert failed == "FAILED:cover_llm_provider_http_403"
                assert len(analysis.calls) == 2


@pytest.mark.asyncio
async def test_prompt_workflow_success_and_domain_error_no_retry(harness) -> None:
    repo, projects = harness.repo, harness.projects
    prompt_provider = harness.prompt_provider
    activities, prompts = harness.activities, harness.prompts
    owner_id = uuid4()
    task_queue = f"cover-prompt-{uuid4()}"
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with create_cover_worker(environment.client, task_queue, activities, executor):
                project_id = uuid4()
                repo.set_project_state(owner_id, project_id, "ACTIVE")
                projects.add(owner_id, project_id)
                generation = prompts.create(owner_id, project_id, idempotency_key="wf-p1")
                prompt_provider.queue(llm(_text_payload()))
                prompt_provider.queue(llm(full_cover_payload()))
                prompt_provider.queue(llm(full_cover_payload()))
                prompt_provider.queue(llm(judge_payload()))
                result = await environment.client.execute_workflow(
                    CoverPromptGenerationWorkflow.run,
                    CoverWorkflowInput(str(owner_id), str(generation.id), "c" * 64),
                    id=f"cover-prompt-ok-{uuid4()}",
                    task_queue=task_queue,
                )
                assert result == "SUCCEEDED"
                assert repo.get_prompt_generation(owner_id, generation.id).status == "SUCCEEDED"  # type: ignore[union-attr]

                failing = prompts.create(owner_id, project_id, idempotency_key="wf-p2")
                prompt_provider.queue(llm(_text_payload()))
                prompt_provider.queue(DomainError("cover_llm_provider_http_401", "unauthorized"))
                prompt_provider.queue(DomainError("cover_llm_provider_http_401", "unauthorized"))
                failed = await environment.client.execute_workflow(
                    CoverPromptGenerationWorkflow.run,
                    CoverWorkflowInput(str(owner_id), str(failing.id), "d" * 64),
                    id=f"cover-prompt-fail-{uuid4()}",
                    task_queue=task_queue,
                )
                assert failed == "FAILED"
                stored = repo.get_prompt_generation(owner_id, failing.id)
                assert stored is not None and stored.status == "FAILED"
                assert stored.failure_code == "cover_llm_provider_http_401"
                # 1 文字 + 2 画面首发 + 2 画面首发 = 5 次成功路径 + 失败路径 1+2；无重试放大。
                assert len(prompt_provider.calls) == 4 + 3


@pytest.mark.asyncio
async def test_image_workflow_success_and_domain_error_no_retry(harness) -> None:
    repo, projects = harness.repo, harness.projects
    image_provider = harness.image_provider
    activities, prompts, images = harness.activities, harness.prompts, harness.images
    owner_id = uuid4()
    task_queue = f"cover-image-{uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with create_cover_worker(environment.client, task_queue, activities, executor):
                project_id = uuid4()
                repo.set_project_state(owner_id, project_id, "ACTIVE")
                projects.add(owner_id, project_id)
                # 造一个 SUCCEEDED 的当前提示词版本（不经提示词工作流）。
                intent = prompts.create(owner_id, project_id, idempotency_key="wf-i0")
                from dataclasses import replace as dc_replace

                completed_prompt = repo.complete_prompt_generation(
                    dc_replace(
                        intent,
                        status="SUCCEEDED",
                        cover_text_candidates=[{"text": "钩子"}],
                        cover_text_default="钩子",
                        cover_text_used="钩子",
                        prompt_body="维度文本",
                        prompt_system="系统版提示词",
                        prompt_current="系统版提示词",
                    )
                )
                repo.set_current_prompt_generation(owner_id, project_id, completed_prompt.id)

                generation = images.create(owner_id, project_id, idempotency_key="wf-i1")
                content = png_bytes()
                image_provider.queue(
                    CoverImageResult(content, hashlib.sha256(content).hexdigest(), "r", "b64_json")
                )
                result = await environment.client.execute_workflow(
                    CoverImageGenerationWorkflow.run,
                    CoverWorkflowInput(str(owner_id), str(generation.id), "e" * 64),
                    id=f"cover-image-ok-{uuid4()}",
                    task_queue=task_queue,
                )
                assert result == "SUCCEEDED"
                stored = repo.get_image_generation(owner_id, generation.id)
                assert stored is not None and stored.status == "SUCCEEDED"
                assert stored.object_sha256 == hashlib.sha256(content).hexdigest()

                failing = images.create(owner_id, project_id, idempotency_key="wf-i2")
                image_provider.queue(DomainError("cover_image_provider_http_500", "boom"))
                failed = await environment.client.execute_workflow(
                    CoverImageGenerationWorkflow.run,
                    CoverWorkflowInput(str(owner_id), str(failing.id), "f" * 64),
                    id=f"cover-image-fail-{uuid4()}",
                    task_queue=task_queue,
                )
                assert failed == "FAILED"
                stored_failed = repo.get_image_generation(owner_id, failing.id)
                assert stored_failed is not None and stored_failed.status == "FAILED"
                assert stored_failed.failure_code == "cover_image_provider_http_500"
                assert len(image_provider.calls) == 2  # 既定失败不重试


@pytest.mark.asyncio
async def test_character_card_analysis_workflow_success_and_empty_identity_no_retry(
    harness,
) -> None:
    repo, analysis = harness.repo, harness.analysis
    activities, characters = harness.activities, harness.characters
    owner_id = uuid4()
    task_queue = f"cover-card-analysis-{uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with create_cover_worker(environment.client, task_queue, activities, executor):
                # 顺利路径：解析结果覆盖卡文本并置 source=analysis。
                channel_id = uuid4()
                repo.add_channel(owner_id, channel_id)
                content = png_bytes(8, 8)
                image_id = uuid4()
                storage_key = f"character-images/{owner_id}/{image_id}"
                harness.storage.objects[storage_key] = content
                repo.add_character_image(
                    CoverCharacterImageRef(
                        image_id,
                        owner_id,
                        channel_id,
                        storage_key,
                        hashlib.sha256(content).hexdigest(),
                        len(content),
                        "image/png",
                    )
                )
                card = characters.upsert_card(
                    owner_id, channel_id, character_image_id=image_id, identity_text="手写"
                )
                analysis.queue(
                    llm({"identity": "原创角色小艾", "appearance": "二次元二头身大眼睛"})
                )
                result = await environment.client.execute_workflow(
                    CoverCharacterCardAnalysisWorkflow.run,
                    CoverWorkflowInput(str(owner_id), str(card.id), "a" * 64),
                    id=f"cover-card-analysis-ok-{uuid4()}",
                    task_queue=task_queue,
                )
                assert result == "SUCCEEDED"
                assert len(analysis.calls) == 1
                stored = repo.get_character_card(owner_id, card.id)
                assert stored is not None and stored.identity_text == "原创角色小艾"
                assert stored.appearance_text == "二次元二头身大眼睛"
                assert stored.source == "analysis"

                # identity 空（既定失败 cover_character_card_empty）：活动不重试、
                # Provider 只调用一次、卡内容不被破坏。
                other_card = characters.upsert_card(
                    owner_id, channel_id, character_image_id=image_id, identity_text="保留我"
                )
                assert other_card.id == card.id  # 每角色图一卡
                analysis.queue(llm({"identity": "  ", "appearance": "只有外貌"}))
                failed = await environment.client.execute_workflow(
                    CoverCharacterCardAnalysisWorkflow.run,
                    CoverWorkflowInput(str(owner_id), str(card.id), "b" * 64),
                    id=f"cover-card-analysis-fail-{uuid4()}",
                    task_queue=task_queue,
                )
                assert failed == "FAILED:cover_character_card_empty"
                assert len(analysis.calls) == 2
                still = repo.get_character_card(owner_id, card.id)
                assert still is not None and still.identity_text == "保留我"
