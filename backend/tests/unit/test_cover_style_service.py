"""封面风格服务测试：上传校验/去重、草稿保存、解析缓存与编排、频道风格版本。

全部内存 fake（仓储/存储/Provider），零真实调用零网络。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.in_memory_cover_providers import FakeCoverRefAnalysisProvider
from tubeflow_test.application.cover_ports import COVER_SCOPE_CHANNEL, COVER_SCOPE_PROJECT
from tubeflow_test.application.cover_project import CoverProjectContext
from tubeflow_test.application.cover_providers import CoverLlmResult
from tubeflow_test.application.cover_style import CoverStyleService
from tubeflow_test.domain.errors import DomainError

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


class Ids:
    def new(self) -> UUID:
        return uuid4()


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[tuple[str, str]] = []

    def put_at_key(self, key: str, body: bytes, media_type: str, expected_sha256: str) -> None:
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("hash mismatch")
        self.puts.append((key, media_type))
        self.objects.setdefault(key, body)

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.objects[key]
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("integrity mismatch")
        return body


class FakeProjects:
    def __init__(self) -> None:
        self.contexts: dict[tuple[UUID, UUID], CoverProjectContext] = {}

    def add(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        channel_id: UUID | None = None,
        title: str = "测试标题",
        script: str | None = "冻结文案全文",
        locale: str = "zh-CN",
    ) -> None:
        self.contexts[(owner_id, project_id)] = CoverProjectContext(
            project_id, channel_id or uuid4(), title, script, locale
        )

    def project_cover_context(
        self, owner_id: UUID, project_id: UUID
    ) -> CoverProjectContext | None:
        return self.contexts.get((owner_id, project_id))


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def analysis_result(payload: dict) -> CoverLlmResult:
    return CoverLlmResult(payload, "receipt-1", "analysis-model", "a" * 64)


@pytest.fixture
def harness():
    repo = InMemoryCoverRepository()
    storage = FakeStorage()
    projects = FakeProjects()
    analysis = FakeCoverRefAnalysisProvider()
    service = CoverStyleService(repo, repo, projects, storage, analysis, Ids(), Clock())
    return repo, storage, projects, analysis, service


def _channel(repo: InMemoryCoverRepository, owner_id: UUID) -> UUID:
    channel_id = uuid4()
    repo.add_channel(owner_id, channel_id)
    return channel_id


def _project(
    repo: InMemoryCoverRepository, projects: FakeProjects, owner_id: UUID, *, channel_id: UUID
) -> UUID:
    project_id = uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.add(owner_id, project_id, channel_id=channel_id)
    return project_id


def test_upload_ref_image_success_and_dedup_hit(harness) -> None:
    repo, storage, _projects, _analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    content = png_bytes()

    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id, filename="ref.PNG",
        content=content,
    )
    assert image.sha256 == hashlib.sha256(content).hexdigest()
    assert image.image_format == "PNG"
    assert image.object_key == f"cover-ref-images/channel/{channel_id}/{image.sha256}"
    assert storage.objects[image.object_key] == content
    assert len(storage.puts) == 1

    again = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id, filename="other.png",
        content=content,
    )
    assert again.id == image.id
    assert len(storage.puts) == 1  # 去重命中：零重复对象写入、零 Provider 调用


def test_upload_ref_image_validation_failures(harness) -> None:
    repo, _storage, _projects, _analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    content = png_bytes()
    with pytest.raises(DomainError) as bad_ext:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
            filename="ref.gif", content=content,
        )
    assert bad_ext.value.code == "invalid_image_type"
    with pytest.raises(DomainError) as too_big:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
            filename="ref.png", content=b"x" * (10 * 1024 * 1024 + 1),
        )
    assert too_big.value.code == "invalid_image_size"
    with pytest.raises(DomainError) as undecodable:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
            filename="ref.png", content=b"definitely not an image",
        )
    assert undecodable.value.code == "invalid_image"
    gif = BytesIO()
    Image.new("RGB", (4, 4)).save(gif, format="GIF")
    with pytest.raises(DomainError) as wrong_media:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
            filename="ref.png", content=gif.getvalue(),
        )
    assert wrong_media.value.code == "invalid_image_type"
    with pytest.raises(DomainError) as unknown_channel:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=uuid4(),
            filename="ref.png", content=content,
        )
    assert unknown_channel.value.code == "not_found"


def test_upload_ref_image_authorization_precheck_precedes_byte_writes(harness) -> None:
    """频道/项目存在性与所有权校验必须先于字节落盘：鉴权失败不得产生孤儿对象。"""
    repo, storage, _projects, _analysis, service = harness
    owner_id = uuid4()
    # 频道不存在（含跨账号场景）：not_found 且零对象写入。
    with pytest.raises(DomainError) as missing:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=uuid4(),
            filename="ref.png", content=png_bytes(),
        )
    assert missing.value.code == "not_found"
    foreign_channel = uuid4()
    repo.add_channel(uuid4(), foreign_channel)  # 他人频道
    with pytest.raises(DomainError) as foreign:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=foreign_channel,
            filename="ref.png", content=png_bytes(),
        )
    assert foreign.value.code == "not_found"
    assert storage.puts == [] and storage.objects == {}

    # 属主频道正常写入。
    channel_id = _channel(repo, owner_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        filename="ref.png", content=png_bytes(),
    )
    assert storage.objects[image.object_key]


def test_project_scope_actions_require_frozen_project(harness) -> None:
    repo, _storage, projects, _analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    project_id = uuid4()
    repo.set_project_state(owner_id, project_id, "ACTIVE")
    projects.add(owner_id, project_id, channel_id=channel_id, script=None)
    with pytest.raises(DomainError) as unfrozen:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_PROJECT, project_id=project_id,
            filename="ref.png", content=png_bytes(),
        )
    assert unfrozen.value.code == "cover_project_not_frozen"
    with pytest.raises(DomainError) as missing:
        service.upload_ref_image(
            owner_id, scope=COVER_SCOPE_PROJECT, project_id=uuid4(),
            filename="ref.png", content=png_bytes(),
        )
    assert missing.value.code == "not_found"


def test_save_draft_normalizes_dims_and_preserves_analysis(harness) -> None:
    repo, _storage, _projects, analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        filename="ref.png", content=png_bytes(),
    )
    draft = service.save_draft(
        owner_id,
        scope=COVER_SCOPE_CHANNEL,
        channel_id=channel_id,
        ref_image_id=image.id,
        dims=["camera::焦距", "bogus", "subject", "camera::焦距"],
        edited_text="手工文本",
    )
    assert draft.dims == {"selected": ["subject", "camera::焦距"]}
    assert draft.edited_text == "手工文本"

    # 解析覆盖后再保存：解析字段保留，仅更新勾选/文本/引用图。
    analysis.queue(analysis_result({"subject": {"人物身份": "青年女性"}}))
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "SUCCEEDED"
    saved = service.save_draft(
        owner_id,
        scope=COVER_SCOPE_CHANNEL,
        channel_id=channel_id,
        ref_image_id=image.id,
        dims=["subject"],
        edited_text="改过的文本",
    )
    assert saved.analysis == {"subject": {"人物身份": "青年女性"}}
    assert saved.ref_image_sha256 == image.sha256
    assert saved.analysis_dims == {"selected": ["subject", "camera::焦距"]}
    assert saved.dims == {"selected": ["subject"]}

    with pytest.raises(DomainError) as too_long:
        service.save_draft(
            owner_id,
            scope=COVER_SCOPE_CHANNEL,
            channel_id=channel_id,
            ref_image_id=None,
            dims=[],
            edited_text="长" * 2001,
        )
    assert too_long.value.code == "cover_style_text_too_long"


def test_save_draft_ref_image_scope_ownership(harness) -> None:
    repo, _storage, _projects, _analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    other_channel = _channel(repo, owner_id)
    foreign = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=other_channel,
        filename="ref.png", content=png_bytes(),
    )
    with pytest.raises(DomainError) as wrong_scope:
        service.save_draft(
            owner_id,
            scope=COVER_SCOPE_CHANNEL,
            channel_id=channel_id,
            ref_image_id=foreign.id,
            dims=[],
            edited_text="",
        )
    assert wrong_scope.value.code == "not_found"


def test_analysis_intent_cache_hit_and_reanalysis_cover(harness) -> None:
    repo, _storage, _projects, analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        filename="ref.png", content=png_bytes(),
    )
    with pytest.raises(DomainError) as no_draft:
        service.create_analysis_intent(
            owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
        )
    assert no_draft.value.code == "cover_ref_image_missing"
    service.save_draft(
        owner_id,
        scope=COVER_SCOPE_CHANNEL,
        channel_id=channel_id,
        ref_image_id=image.id,
        dims=["subject"],
        edited_text="",
    )
    intent = service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    )
    assert intent.enqueued and not intent.reused and intent.workflow_id
    # 在途去重：同一确定 workflow_id 不重复入队。
    again = service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    )
    assert not again.enqueued and not again.reused
    assert again.workflow_id == intent.workflow_id

    analysis.queue(analysis_result({"subject": {"人物身份": "青年女性", "外貌": "短发"}}))
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "SUCCEEDED"
    assert len(analysis.calls) == 1
    draft = repo.get_style_draft_for_channel(owner_id, channel_id)
    assert draft is not None
    assert draft.edited_text == "主体：人物身份：青年女性；外貌：短发"
    assert draft.ref_image_sha256 == image.sha256
    assert draft.analysis_dims == {"selected": ["subject"]}

    # 缓存命中：显式点击直接完成，零 Provider 调用、零新事件。
    cached = service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    )
    assert cached.reused and not cached.enqueued
    assert len(analysis.calls) == 1
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "REUSED"
    assert len(analysis.calls) == 1

    # 勾选变化 → 缓存失效 → 重解析并覆盖编辑文本（沿用参考实现语义）。
    service.save_draft(
        owner_id,
        scope=COVER_SCOPE_CHANNEL,
        channel_id=channel_id,
        ref_image_id=image.id,
        dims=["subject", "camera"],
        edited_text=draft.edited_text,
    )
    changed = service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    )
    assert changed.enqueued and not changed.reused
    assert changed.workflow_id != intent.workflow_id
    analysis.queue(
        analysis_result({"subject": {"人物身份": "青年女性"}, "camera": {"焦距": "35mm"}})
    )
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "SUCCEEDED"
    assert len(analysis.calls) == 2
    updated = repo.get_style_draft_for_channel(owner_id, channel_id)
    assert updated is not None
    assert "焦距：35mm" in updated.edited_text
    assert updated.analysis_dims == {"selected": ["subject", "camera"]}


def _drain_outbox(repo) -> None:
    """模拟 dispatcher 完成后确认：认领并 ACK 全部在途事件（用远超任何时钟的哨兵时间）。"""
    from datetime import UTC, datetime, timedelta

    sentinel = datetime(2999, 1, 1, tzinfo=UTC)
    token = uuid4()
    while True:
        event = repo.claim_outbox(sentinel, sentinel + timedelta(minutes=1), token)
        if event is None:
            return
        assert repo.acknowledge_outbox(event.event_id, token, sentinel)


def test_analysis_cache_miss_when_text_does_not_cover_dims(harness) -> None:
    # 陷阱态（2026-09-21 实测复现）：勾选与解析时一致，但文本只覆盖部分维度——
    # 必须判未命中并重新入队；复用该文本会在下游被 COVER_REF_TEXT_INVALID 必然拒绝。
    repo, _storage, _projects, analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        filename="ref.png", content=png_bytes(),
    )
    dims = ["subject", "camera"]
    service.save_draft(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        ref_image_id=image.id, dims=dims, edited_text="",
    )
    first = service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    )
    assert first.enqueued
    # 模型只回了 subject（或用户手工把文本收窄到 subject）：渲染文本不覆盖 camera。
    analysis.queue(analysis_result({"subject": {"人物身份": "青年女性", "外貌": "短发"}}))
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "SUCCEEDED"
    draft = repo.get_style_draft_for_channel(owner_id, channel_id)
    assert draft is not None
    assert draft.analysis_dims == {"selected": dims}
    assert draft.edited_text == "主体：人物身份：青年女性；外貌：短发"
    _drain_outbox(repo)

    second = service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    )
    assert second.enqueued and not second.reused


def test_analysis_force_reparse_clears_cache_and_reenqueues(harness) -> None:
    # force=True（「重新解析」）：有效缓存也被清键强制重解；旧文本保留到新解析成功。
    repo, _storage, _projects, analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        filename="ref.png", content=png_bytes(),
    )
    service.save_draft(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        ref_image_id=image.id, dims=["subject"], edited_text="",
    )
    assert service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    ).enqueued
    analysis.queue(analysis_result({"subject": {"人物身份": "青年女性", "外貌": "短发"}}))
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "SUCCEEDED"
    _drain_outbox(repo)
    assert service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    ).reused
    assert len(analysis.calls) == 1

    forced = service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id, force=True
    )
    assert forced.enqueued and not forced.reused
    draft = repo.get_style_draft_for_channel(owner_id, channel_id)
    assert draft is not None
    assert draft.ref_image_sha256 == "" and draft.analysis_dims == {}
    assert draft.edited_text == "主体：人物身份：青年女性；外貌：短发"

    # 重解析成功后缓存重建，普通点击恢复复用。
    analysis.queue(analysis_result({"subject": {"人物身份": "青年女性", "外貌": "短发"}}))
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "SUCCEEDED"
    assert len(analysis.calls) == 2
    _drain_outbox(repo)
    assert service.create_analysis_intent(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id
    ).reused


def test_run_ref_analysis_empty_result_is_stable_failure(harness) -> None:
    repo, _storage, _projects, analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        filename="ref.png", content=png_bytes(),
    )
    service.save_draft(
        owner_id,
        scope=COVER_SCOPE_CHANNEL,
        channel_id=channel_id,
        ref_image_id=image.id,
        dims=["subject"],
        edited_text="",
    )
    analysis.queue(analysis_result({"subject": {"人物身份": "不明显"}}))
    with pytest.raises(DomainError) as empty:
        service.run_ref_analysis(owner_id, "channel", channel_id)
    assert empty.value.code == "cover_ref_analysis_empty"
    # 不静默降级为无参考：草稿不写入空解析。
    draft = repo.get_style_draft_for_channel(owner_id, channel_id)
    assert draft is not None and draft.ref_image_sha256 == ""


def test_promote_and_channel_current_switch(harness) -> None:
    repo, _storage, projects, analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_CHANNEL, channel_id=channel_id,
        filename="ref.png", content=png_bytes(),
    )
    service.save_draft(
        owner_id,
        scope=COVER_SCOPE_CHANNEL,
        channel_id=channel_id,
        ref_image_id=image.id,
        dims=["subject"],
        edited_text="",
    )
    analysis.queue(analysis_result({"subject": {"人物身份": "青年女性"}}))
    assert service.run_ref_analysis(owner_id, "channel", channel_id) == "SUCCEEDED"

    first = service.promote_to_channel(owner_id, channel_id)
    assert first.version == 1 and first.supersedes_id is None
    assert first.edited_text == "主体：人物身份：青年女性"
    current = repo.current_style_profile_for_channel(owner_id, channel_id)
    assert current is not None and current.id == first.id

    second = service.promote_to_channel(owner_id, channel_id)
    assert second.version == 2 and second.supersedes_id == first.id
    assert repo.current_style_profile_for_channel(owner_id, channel_id).id == second.id  # type: ignore[union-attr]

    switched = service.set_channel_current(owner_id, channel_id, first.id)
    assert switched.id == first.id
    assert repo.current_style_profile_for_channel(owner_id, channel_id).id == first.id  # type: ignore[union-attr]
    versions = repo.list_style_profiles_for_channel(owner_id, channel_id)
    assert [item.version for item in versions] == [2, 1]
    with pytest.raises(DomainError) as invalid:
        service.set_channel_current(owner_id, channel_id, uuid4())
    assert invalid.value.code == "invalid_cover_candidate"
    del projects


def test_promote_project_draft_to_channel(harness) -> None:
    repo, _storage, projects, _analysis, service = harness
    owner_id = uuid4()
    channel_id = _channel(repo, owner_id)
    project_id = _project(repo, projects, owner_id, channel_id=channel_id)
    image = service.upload_ref_image(
        owner_id, scope=COVER_SCOPE_PROJECT, project_id=project_id,
        filename="ref.png", content=png_bytes(),
    )
    assert image.scope == COVER_SCOPE_PROJECT
    service.save_draft(
        owner_id,
        scope=COVER_SCOPE_PROJECT,
        project_id=project_id,
        ref_image_id=image.id,
        dims=["style"],
        edited_text="风格：艺术流派：写实摄影",
    )
    profile = service.promote_to_channel(owner_id, channel_id, project_id=project_id)
    assert profile.version == 1
    assert profile.edited_text == "风格：艺术流派：写实摄影"
    assert profile.dims == {"selected": ["style"]}
    with pytest.raises(DomainError) as wrong_channel:
        service.promote_to_channel(owner_id, uuid4(), project_id=project_id)
    assert wrong_channel.value.code == "not_found"
