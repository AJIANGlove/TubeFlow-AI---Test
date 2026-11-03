"""封面提示词服务测试：create（幂等/风格解析/冻结门禁）与生成编排。

编排覆盖：串行顺序（文字→画面）、cover_text 已给跳过文字调用、文字失败回退
标题不阻断、两版并行单版失败跳过警告、全失败抛错、评委失败本地择优、整改仅
更优替换、参考约束 provided 逐字拼接与 dim_line_invalid 拒绝、幂等重放零重复
Provider 调用、面板编辑/重置/换候选。全部内存 fake，零网络。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.in_memory_cover_providers import FakeCoverPromptProvider
from tubeflow_test.application.cover_ports import (
    COVER_STATUS_FAILED,
    COVER_STATUS_SUCCEEDED,
    CoverStyleDraft,
    CoverStyleProfile,
)
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_prompts import CoverPromptService
from tubeflow_test.application.cover_providers import CoverLlmResult
from tubeflow_test.domain.cover import REF_DIMENSIONS
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
TITLE = "测试标题"
SCRIPT = "冻结文案全文，一字不截断。"


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeProjects:
    def __init__(self) -> None:
        self.contexts: dict[tuple[UUID, UUID], CoverProjectContext] = {}

    def add(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        channel_id: UUID | None = None,
        title: str = TITLE,
        script: str | None = SCRIPT,
        locale: str = "zh-CN",
    ) -> None:
        self.contexts[(owner_id, project_id)] = CoverProjectContext(
            project_id, channel_id or uuid4(), title, script, locale
        )

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


def llm(payload: dict[str, Any], *, model: str = "cover-model") -> CoverLlmResult:
    return CoverLlmResult(payload, f"receipt-{uuid4()}", model, "r" * 64)


def text_payload(
    candidates: tuple[str, ...] = ("短钩子", "对比冲击", "三个错误", "就这？", "到此为止"),
    recommended: int = 2,
) -> dict[str, Any]:
    return {
        "candidates": [{"text": text, "pattern": "句式"} for text in candidates],
        "recommended": recommended,
    }


def full_dims(**overrides: str) -> dict[str, Any]:
    dims: dict[str, Any] = {}
    for dim in REF_DIMENSIONS:
        dims[dim.key] = {
            child: overrides.get(f"{dim.key}::{child}", f"{dim.label}{child}描述")
            for child in dim.children
        }
    return dims


def cover_payload(dims: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ratio": "16:9",
        "covers": [
            {"rank": 1, "forTitle": TITLE, "dims": dims if dims is not None else full_dims()}
        ],
    }


def dims_missing(*missing_keys: str) -> dict[str, Any]:
    return {key: value for key, value in full_dims().items() if key not in missing_keys}


def judge_payload(best: int = 2, *, total_best: int = 120) -> dict[str, Any]:
    def scores(total: int) -> dict[str, int]:
        per = total // 7
        rest = total - per * 7
        keys = [
            "visual_impact",
            "thumbnail_clarity",
            "title_match",
            "text_visual_match",
            "script_fidelity",
            "style_consistency",
            "technical_quality",
        ]
        return {key: per + (1 if index < rest else 0) for index, key in enumerate(keys)}

    return {
        "verdicts": [
            {"version": 1, "scores": scores(100), "comment": "一般"},
            {"version": 2, "scores": scores(total_best), "comment": "更好"},
        ],
        "best": best,
        "advice": "",
    }


@pytest.fixture
def harness():
    repo = InMemoryCoverRepository()
    projects = FakeProjects()
    provider = FakeCoverPromptProvider()
    service = CoverPromptService(repo, projects, provider, Ids(), Clock(), model="cover-model")
    return repo, projects, provider, service


def _project(repo, projects, owner_id: UUID, *, channel_id: UUID | None = None) -> UUID:
    project_id = uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.add(owner_id, project_id, channel_id=channel_id or uuid4())
    return project_id


def _seed_project_draft(
    repo, owner_id: UUID, project_id: UUID, *, dims: dict, edited_text: str
) -> None:
    repo.upsert_style_draft(
        CoverStyleDraft(
            uuid4(), owner_id, "project", None, project_id, None, dims, {}, {}, "",
            edited_text, NOW,
        )
    )


def _seed_channel_profile(
    repo, owner_id: UUID, channel_id: UUID, *, dims: dict, edited_text: str
) -> CoverStyleProfile:
    profile = repo.create_style_profile(
        CoverStyleProfile(uuid4(), owner_id, channel_id, 0, None, dims, {}, edited_text, NOW, None)
    )
    return repo.set_channel_current_style_profile(owner_id, channel_id, profile.id)


def _run(coro):
    return asyncio.run(coro)


# ---------- create ----------


def test_create_requires_frozen_project(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id = uuid4()
    project_id = uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.add(owner_id, project_id, script=None)
    with pytest.raises(DomainError) as unfrozen:
        service.create(owner_id, project_id, idempotency_key="k1")
    assert unfrozen.value.code == "cover_project_not_frozen"
    with pytest.raises(DomainError) as missing:
        service.create(owner_id, uuid4(), idempotency_key="k2")
    assert missing.value.code == "not_found"


def test_create_validates_brief_and_style_source(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    with pytest.raises(DomainError) as brief:
        service.create(owner_id, project_id, brief="长" * 501, idempotency_key="k1")
    assert brief.value.code == "cover_brief_too_long"
    with pytest.raises(DomainError) as invalid:
        service.create(owner_id, project_id, style_source="bogus", idempotency_key="k2")
    assert invalid.value.code == "cover_style_source_invalid"
    with pytest.raises(DomainError) as unavailable:
        service.create(owner_id, project_id, style_source="channel", idempotency_key="k3")
    assert unavailable.value.code == "cover_style_source_unavailable"


def test_create_style_default_chain_channel_then_project_then_none(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id = uuid4()
    channel_id = uuid4()
    repo.add_channel(owner_id, channel_id)
    project_id = _project(repo, projects, owner_id, channel_id=channel_id)

    none = service.create(owner_id, project_id, idempotency_key="k-none")
    assert none.style_source == "none" and none.style_text_used == ""

    _seed_project_draft(
        repo, owner_id, project_id,
        dims={"selected": ["subject"]}, edited_text="主体：人物身份：青年女性",
    )
    draft = service.create(owner_id, project_id, idempotency_key="k-draft")
    assert draft.style_source == "project_draft"
    assert draft.style_text_used == "主体：人物身份：青年女性"
    assert draft.style_dims_used == {"selected": ["subject"]}

    profile = _seed_channel_profile(
        repo, owner_id, channel_id,
        dims={"selected": ["style"]}, edited_text="风格：艺术流派：写实摄影",
    )
    channel = service.create(owner_id, project_id, idempotency_key="k-channel")
    assert channel.style_source == "channel_profile"
    assert channel.channel_style_profile_id == profile.id
    assert channel.style_text_used == "风格：艺术流派：写实摄影"


def test_create_cover_text_provided_prefills_candidates(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = service.create(
        owner_id, project_id, cover_text=" 给定封面文字 ", idempotency_key="k1"
    )
    assert generation.cover_text_used == "给定封面文字"
    assert generation.cover_text_default == "给定封面文字"
    assert generation.cover_text_candidates == [{"text": "给定封面文字"}]
    assert generation.parameters == {"cover_text": "给定封面文字"}


def test_create_idempotency_replay_and_conflict(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    first = service.create(owner_id, project_id, brief="b", idempotency_key="same")
    replay = service.create(owner_id, project_id, brief="b", idempotency_key="same")
    assert replay.id == first.id and replay.version == first.version
    with pytest.raises(DomainError) as conflict:
        service.create(owner_id, project_id, brief="changed", idempotency_key="same")
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(DomainError) as conflict_text:
        service.create(
            owner_id, project_id, brief="b", cover_text="新文字", idempotency_key="same"
        )
    assert conflict_text.value.code == "idempotency_conflict"
    assert len(repo.prompt_generations) == 1
    assert len(repo.outbox) == 1  # 重放不重复写事件


def test_create_rejects_dim_line_invalid_before_any_provider_call(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    # 勾选了 composition::留白 但风格文本只有主体行 → 勾选维度整行缺失。
    _seed_project_draft(
        repo, owner_id, project_id,
        dims={"selected": ["composition::留白"]}, edited_text="主体：人物身份：青年女性",
    )
    with pytest.raises(DomainError) as invalid:
        service.create(owner_id, project_id, idempotency_key="k1")
    assert invalid.value.code == "COVER_REF_TEXT_INVALID"
    assert provider.calls == []


# ---------- generate orchestration ----------


def _create(service, repo, projects, owner_id, project_id, **kwargs):
    kwargs.setdefault("idempotency_key", f"k-{uuid4()}")
    return service.create(owner_id, project_id, **kwargs)


def test_generate_happy_path_serial_then_parallel_then_judge(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id, brief="突出反差")
    provider.queue(llm(text_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload(best=2)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    temps = [temperature for _messages, temperature in provider.calls]
    assert temps[0] == 0.9  # 文字调用串行在先
    assert sorted(temps[1:3]) == [0.8, 1.0]  # 两版并行
    assert temps[3] == 0.2  # 评委在后
    assert len(provider.calls) == 4
    assert completed.cover_text_candidates == [
        {"text": text} for text in ("短钩子", "对比冲击", "三个错误", "就这？", "到此为止")
    ]
    assert completed.cover_text_default == "对比冲击"
    assert completed.cover_text_used == "对比冲击"
    assert "画面中加入标题文字「对比冲击」" in completed.prompt_system
    assert completed.prompt_current == completed.prompt_system
    assert "主体：" in completed.prompt_body
    assert "画面主体完整入画" in completed.prompt_body  # 锚点补入
    assert "一眼辨认" in completed.prompt_body
    assert completed.judge is not None and completed.judge["best"] == 2
    # 切 projects 当前指针
    current = repo.current_prompt_generation_for_project(owner_id, project_id)
    assert current is not None and current.id == completed.id


def test_generate_skips_text_call_when_cover_text_provided(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(
        service, repo, projects, owner_id, project_id, cover_text="所选文字"
    )
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload(best=1)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    temps = sorted(temperature for _messages, temperature in provider.calls)
    assert temps == [0.2, 0.8, 1.0]  # 无 0.9 文字调用
    assert completed.cover_text_candidates == [{"text": "所选文字"}]
    assert completed.cover_text_used == "所选文字"
    assert "画面中加入标题文字「所选文字」" in completed.prompt_current


def test_generate_text_failure_falls_back_to_title_without_blocking(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(DomainError("cover_llm_provider_http_429", "rate limited"))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload(best=1)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    assert "cover_text_fallback_title" in completed.warnings
    assert completed.cover_text_default == TITLE[:30]
    assert completed.cover_text_used == TITLE[:30]
    assert len(provider.calls) == 4


def test_generate_single_variant_failure_skips_with_warning(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    provider.queue(DomainError("cover_llm_provider_http_500", "boom"))
    provider.queue(llm(cover_payload()))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    assert "cover_variant_failed" in completed.warnings
    assert completed.judge is None  # 仅一版成功，不调用评委
    assert len(provider.calls) == 3


def test_generate_all_variants_failed_marks_failed(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    provider.queue(DomainError("cover_llm_provider_http_429", "rate"))
    provider.queue(DomainError("cover_llm_provider_timeout", "timeout"))

    failed = _run(service.generate(owner_id, generation.id))

    assert failed.status == COVER_STATUS_FAILED
    assert failed.failure_stage == "generate"
    assert failed.failure_code == "cover_llm_provider_http_429"
    assert len(provider.calls) == 3  # 评委未调用
    assert repo.current_prompt_generation_for_project(owner_id, project_id) is None


def test_generate_judge_failure_falls_back_to_local_pick(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(DomainError("cover_llm_provider_http_503", "down"))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    assert completed.judge is None
    assert "cover_judge_fallback_local" in completed.warnings


def test_generate_judge_low_score_records_warning(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload(best=2, total_best=100)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    assert completed.judge is not None
    assert "cover_judge_low_score" in completed.warnings


def test_generate_repair_replaces_only_when_better(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    # 两版首发都缺 camera（needsRetry → 各一次定向整改），整改版均补齐 → 替换。
    provider.queue(llm(cover_payload(dims_missing("camera"))))
    provider.queue(llm(cover_payload(dims_missing("camera"))))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload(best=1)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    assert "镜头：" in completed.prompt_body  # 整改版被采用
    temps = [temperature for _messages, temperature in provider.calls]
    assert len(temps) == 6  # 1 文字 + 2 首发 + 2 整改 + 1 评委
    # 整改消息带【定向整改要求】
    directive_calls = [
        messages
        for messages, _temperature in provider.calls
        if "【定向整改要求】" in str(messages[-1].get("content"))
    ]
    assert len(directive_calls) == 2
    assert "camera" in str(directive_calls[0][-1].get("content"))


def test_generate_repair_not_worse_replacement_keeps_original(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    # 首发缺 camera（1 问题），整改版缺 style（同条数同问题数）→ 不替换。
    provider.queue(llm(cover_payload(dims_missing("camera"))))
    provider.queue(llm(cover_payload(dims_missing("camera"))))
    provider.queue(llm(cover_payload(dims_missing("style"))))
    provider.queue(llm(cover_payload(dims_missing("style"))))
    provider.queue(llm(judge_payload(best=1)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    assert "镜头：" not in completed.prompt_body  # 保留首发（缺 camera）
    assert "风格：" in completed.prompt_body


def test_generate_ref_provided_children_spliced_verbatim(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    _seed_project_draft(
        repo, owner_id, project_id,
        dims={"selected": ["composition::留白"]}, edited_text="构图：留白：顶部大面积留白",
    )
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload(("钩子",))))
    dims = full_dims(**{"composition::留白": "模型撰写的留白"})
    provider.queue(llm(cover_payload(dims)))
    provider.queue(llm(cover_payload(dims)))
    provider.queue(llm(judge_payload(best=1)))

    completed = _run(service.generate(owner_id, generation.id))

    assert completed.status == COVER_STATUS_SUCCEEDED
    # provided 子项逐字拼接：模型值被忽略，采用参考文本逐字值。
    assert "留白：顶部大面积留白" in completed.prompt_body
    assert "模型撰写的留白" not in completed.prompt_body
    # 画面任务单含参考图豁免段（provided 非空）。
    variant_messages = [
        messages
        for messages, temperature in provider.calls
        if temperature in (0.8, 1.0)
    ]
    assert variant_messages
    assert all("【参考图豁免】" in str(messages[0].get("content")) for messages in variant_messages)


def test_generate_idempotent_replay_makes_no_duplicate_provider_calls(harness) -> None:
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload(best=1)))

    completed = _run(service.generate(owner_id, generation.id))
    assert completed.status == COVER_STATUS_SUCCEEDED
    calls_before = len(provider.calls)
    replay = _run(service.generate(owner_id, generation.id))
    assert replay.status == COVER_STATUS_SUCCEEDED
    assert len(provider.calls) == calls_before  # 重放零重复 Provider 调用


# ---------- panel ----------


def _succeeded_generation(harness):
    repo, projects, provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    provider.queue(llm(text_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(cover_payload()))
    provider.queue(llm(judge_payload(best=2)))
    completed = _run(service.generate(owner_id, generation.id))
    return service, owner_id, project_id, completed


def test_panel_edit_reset_and_choose_candidate(harness) -> None:
    service, owner_id, project_id, completed = _succeeded_generation(harness)
    edited = service.edit_prompt_current(owner_id, completed.id, "手工编辑的提示词")
    assert edited.prompt_current == "手工编辑的提示词"
    reset = service.reset_to_system(owner_id, completed.id)
    assert reset.prompt_current == completed.prompt_system
    chosen = service.choose_cover_text(owner_id, completed.id, "三个错误")
    expected_field = (
        "画面中加入标题文字「三个错误」：大号醒目字体，粗体描边或与背景高对比，"
        "拼写正确、清晰可读，位于画面显著位置且不遮挡主体；"
        "除该文字外，画面中不得出现其他任何文字、字母、数字、招牌或水印"
    )
    assert chosen.prompt_current == f"{completed.prompt_body}\n{expected_field}"
    with pytest.raises(DomainError) as unknown:
        service.choose_cover_text(owner_id, completed.id, "不在候选里的文字")
    assert unknown.value.code == "invalid_cover_candidate"
    with pytest.raises(DomainError) as empty:
        service.edit_prompt_current(owner_id, completed.id, "   ")
    assert empty.value.code == "cover_prompt_empty"
    with pytest.raises(DomainError) as too_long:
        service.edit_prompt_current(owner_id, completed.id, "长" * 4001)
    assert too_long.value.code == "cover_prompt_too_long"


def test_panel_edit_requires_succeeded(harness) -> None:
    repo, projects, _provider, service = harness
    owner_id = uuid4()
    project_id = _project(repo, projects, owner_id)
    generation = _create(service, repo, projects, owner_id, project_id)
    with pytest.raises(DomainError) as conflict:
        service.edit_prompt_current(owner_id, generation.id, "编辑")
    assert conflict.value.code == "revision_conflict"


def test_view_returns_current_and_versions(harness) -> None:
    service, owner_id, project_id, completed = _succeeded_generation(harness)
    current, versions = service.view(owner_id, project_id)
    assert current is not None and current.id == completed.id
    assert [item.id for item in versions] == [completed.id]
