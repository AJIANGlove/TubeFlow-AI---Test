"""封面 outbox：派发器语义、启动器路由与仓储合同（内存 + SQL 双实现一致）。

照 `test_storyboard_outbox.py` 与 `test_cover_repository_contract.py` 的 SQLite 模式。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from temporalio.common import WorkflowIDReusePolicy

from tubeflow_test.adapters.in_memory_cover import InMemoryCoverRepository
from tubeflow_test.adapters.persistence.cover.models import (
    CoverImageGenerationRow,
    CoverOutboxRow,
    CoverPromptGenerationRow,
    CoverRefImageRow,
    CoverStyleDraftRow,
    CoverStyleProfileRow,
)
from tubeflow_test.adapters.persistence.cover.repository import SqlAlchemyCoverRepository
from tubeflow_test.application.cover_dispatcher import CoverOutboxDispatcher
from tubeflow_test.application.cover_outbox import (
    COVER_AGGREGATE_CHARACTER_CARD,
    COVER_AGGREGATE_IMAGE_GENERATION,
    COVER_AGGREGATE_PROMPT_GENERATION,
    COVER_AGGREGATE_STYLE_CHANNEL,
    COVER_AGGREGATE_STYLE_PROJECT,
    COVER_CHARACTER_CARD_ANALYSIS_REQUESTED,
    COVER_IMAGE_GENERATION_REQUESTED,
    COVER_PROMPT_GENERATION_REQUESTED,
    COVER_REF_ANALYSIS_REQUESTED,
    CoverOutboxEvent,
)
from tubeflow_test.application.cover_ports import (
    COVER_STATUS_INTENT_PERSISTED,
    CoverImageGeneration,
    CoverPromptGeneration,
    cover_prompt_request_fingerprint,
)
from tubeflow_test.workflows.cover import TemporalCoverWorkflowStarter

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class Ids:
    def new(self) -> UUID:
        return uuid4()


@dataclass(slots=True)
class _Backend:
    repo: Any
    sessions: sessionmaker[Session] | None

    def seed_project(self, owner_id: UUID, project_id: UUID, *, state: str = "ACTIVE") -> None:
        if self.sessions is None:
            self.repo.set_project_state(owner_id, project_id, state)
            return
        _seed_project(self.sessions, owner_id, project_id, state=state)


def _sql_repo(path: Path) -> tuple[SqlAlchemyCoverRepository, sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 10}
    )
    for table in (
        CoverRefImageRow.__table__,
        CoverStyleDraftRow.__table__,
        CoverStyleProfileRow.__table__,
        CoverPromptGenerationRow.__table__,
        CoverImageGenerationRow.__table__,
        CoverOutboxRow.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE channels (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "current_cover_style_profile_id CHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE projects (id CHAR(32) PRIMARY KEY, owner_id CHAR(32), "
                "channel_id CHAR(32), title VARCHAR(200), locale VARCHAR(8), "
                "snapshot_script TEXT, snapshot_locale VARCHAR(8), "
                "snapshot_character_image_ids TEXT, "
                "production_state VARCHAR(16), current_cover_prompt_generation_id CHAR(32), "
                "current_cover_image_generation_id CHAR(32))"
            )
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

    return SqlAlchemyCoverRepository(uow), sessions


def _seed_project(
    sessions: sessionmaker[Session],
    owner_id: UUID,
    project_id: UUID,
    *,
    state: str = "ACTIVE",
    channel_id: UUID | None = None,
    title: str = "测试标题",
    script: str | None = "冻结文案全文",
    locale: str = "zh-CN",
) -> None:
    with sessions.begin() as session:
        session.execute(
            text(
                "INSERT INTO projects (id, owner_id, channel_id, title, locale, "
                "snapshot_script, snapshot_locale, production_state) "
                "VALUES (:id, :owner, :channel, :title, :locale, :script, :slocale, :state)"
            ),
            {
                "id": project_id.hex,
                "owner": owner_id.hex,
                "channel": (channel_id or uuid4()).hex,
                "title": title,
                "locale": locale,
                "script": script,
                "slocale": locale if script is not None else None,
                "state": state,
            },
        )


@pytest.fixture(params=("memory", "sql"), ids=("memory", "sql"))
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> _Backend:
    if request.param == "memory":
        return _Backend(InMemoryCoverRepository(), None)
    repo, sessions = _sql_repo(tmp_path / "cover-outbox.sqlite")
    return _Backend(repo, sessions)


def _prompt_generation(owner_id: UUID, project_id: UUID, *, key: str) -> CoverPromptGeneration:
    return CoverPromptGeneration(
        uuid4(),
        owner_id,
        project_id,
        0,
        "none",
        None,
        "",
        {},
        None,
        "brief",
        [],
        "",
        "",
        "",
        "",
        "",
        "标题",
        "a" * 64,
        [],
        None,
        "model",
        {"cover_text": None},
        key,
        COVER_STATUS_INTENT_PERSISTED,
        None,
        None,
        NOW,
    )


def _image_generation(owner_id: UUID, project_id: UUID, *, key: str) -> CoverImageGeneration:
    return CoverImageGeneration(
        uuid4(),
        owner_id,
        project_id,
        uuid4(),
        0,
        "prompt",
        "model",
        "2560x1440",
        "high",
        {},
        key,
        "b" * 64,
        "c" * 64,
        COVER_STATUS_INTENT_PERSISTED,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        NOW,
    )


def test_create_generations_write_outbox_events_in_same_transaction(backend: _Backend) -> None:
    repo = backend.repo
    owner_id, project_id = uuid4(), uuid4()
    backend.seed_project(owner_id, project_id)

    prompt = repo.create_prompt_generation(_prompt_generation(owner_id, project_id, key="p1"))
    image = repo.create_image_generation(_image_generation(owner_id, project_id, key="i1"))

    now = NOW + timedelta(seconds=1)
    first = repo.claim_outbox(now, now + timedelta(seconds=30), uuid4())
    second = repo.claim_outbox(now, now + timedelta(seconds=30), uuid4())
    third = repo.claim_outbox(now, now + timedelta(seconds=30), uuid4())
    assert third is None
    assert first is not None and second is not None
    events = {first.event_type: first, second.event_type: second}
    prompt_event = events[COVER_PROMPT_GENERATION_REQUESTED]
    assert prompt_event.aggregate_type == COVER_AGGREGATE_PROMPT_GENERATION
    assert prompt_event.aggregate_id == prompt.id
    assert prompt_event.project_id == project_id
    assert prompt_event.workflow_id == f"cover-prompt-generation-{owner_id}-{prompt.id}"
    assert prompt_event.request_sha256 == hashlib.sha256(
        cover_prompt_request_fingerprint(prompt).encode()
    ).hexdigest()
    image_event = events[COVER_IMAGE_GENERATION_REQUESTED]
    assert image_event.aggregate_type == COVER_AGGREGATE_IMAGE_GENERATION
    assert image_event.aggregate_id == image.id
    assert image_event.workflow_id == f"cover-image-generation-{owner_id}-{image.id}"
    assert image_event.request_sha256 == image.canonical_request_sha256


def test_request_ref_analysis_dedupes_pending_workflow(backend: _Backend) -> None:
    repo = backend.repo
    owner_id = uuid4()
    kwargs: dict[str, Any] = {
        "event_id": uuid4(),
        "owner_id": owner_id,
        "project_id": None,
        "aggregate_type": COVER_AGGREGATE_STYLE_CHANNEL,
        "aggregate_id": uuid4(),
        "request_sha256": "d" * 64,
        "workflow_id": "cover-ref-analysis-wf",
        "now": NOW,
    }
    assert repo.request_ref_analysis(**kwargs) is True
    assert repo.request_ref_analysis(**(kwargs | {"event_id": uuid4()})) is False

    # 确认后同一 workflow_id 可再次入队（失败的解析被用户再次显式点击）。
    event = repo.claim_outbox(NOW, NOW + timedelta(seconds=30), uuid4())
    assert event is not None
    assert repo.acknowledge_outbox(event.event_id, event.lease_token, NOW)
    assert repo.request_ref_analysis(**(kwargs | {"event_id": uuid4()})) is True


def test_request_character_card_analysis_dedupes_and_requeues(backend: _Backend) -> None:
    repo = backend.repo
    owner_id, card_id = uuid4(), uuid4()
    kwargs: dict[str, Any] = {
        "event_id": uuid4(),
        "owner_id": owner_id,
        "aggregate_id": card_id,
        "request_sha256": "9" * 64,
        "workflow_id": "cover-card-analysis-wf",
        "now": NOW,
    }
    assert repo.request_character_card_analysis(**kwargs) is True
    assert repo.request_character_card_analysis(**(kwargs | {"event_id": uuid4()})) is False
    # 确认后同一 workflow_id 可再次入队（失败的解析被用户再次显式点击）。
    event = repo.claim_outbox(NOW, NOW + timedelta(seconds=30), uuid4())
    assert event is not None
    assert event.event_type == COVER_CHARACTER_CARD_ANALYSIS_REQUESTED
    assert event.aggregate_type == COVER_AGGREGATE_CHARACTER_CARD
    assert event.aggregate_id == card_id
    assert event.project_id is None
    assert repo.acknowledge_outbox(event.event_id, event.lease_token, NOW)
    assert repo.request_character_card_analysis(**(kwargs | {"event_id": uuid4()})) is True


def test_claim_skips_non_active_projects_but_allows_channel_scope(backend: _Backend) -> None:
    repo = backend.repo
    owner_id, project_id = uuid4(), uuid4()
    backend.seed_project(owner_id, project_id, state="PURGING")
    base: dict[str, Any] = {
        "owner_id": owner_id,
        "request_sha256": "e" * 64,
        "now": NOW,
    }
    assert repo.request_ref_analysis(
        **(
            base
            | {
                "event_id": uuid4(),
                "project_id": project_id,
                "aggregate_type": COVER_AGGREGATE_STYLE_PROJECT,
                "aggregate_id": project_id,
                "workflow_id": "wf-purging",
            }
        )
    )
    assert repo.request_ref_analysis(
        **(
            base
            | {
                "event_id": uuid4(),
                "project_id": None,
                "aggregate_type": COVER_AGGREGATE_STYLE_CHANNEL,
                "aggregate_id": uuid4(),
                "workflow_id": "wf-channel",
            }
        )
    )
    event = repo.claim_outbox(NOW, NOW + timedelta(seconds=30), uuid4())
    assert event is not None
    assert event.aggregate_type == COVER_AGGREGATE_STYLE_CHANNEL
    assert event.project_id is None
    assert repo.claim_outbox(NOW, NOW + timedelta(seconds=30), uuid4()) is None


def test_claim_lease_cas_and_retry_then_ack(backend: _Backend) -> None:
    repo = backend.repo
    owner_id = uuid4()
    assert repo.request_ref_analysis(
        event_id=uuid4(),
        owner_id=owner_id,
        project_id=None,
        aggregate_type=COVER_AGGREGATE_STYLE_CHANNEL,
        aggregate_id=uuid4(),
        request_sha256="f" * 64,
        workflow_id="wf-lease",
        now=NOW,
    )
    token = uuid4()
    event = repo.claim_outbox(NOW, NOW + timedelta(seconds=30), token)
    assert event is not None and event.attempt_count == 1
    # 租约未到期不可重复认领；错误 token 不能 ack/retry。
    assert repo.claim_outbox(NOW, NOW + timedelta(seconds=30), uuid4()) is None
    assert not repo.acknowledge_outbox(event.event_id, uuid4(), NOW)
    assert not repo.retry_outbox(event.event_id, uuid4(), NOW + timedelta(seconds=5), "Boom")
    assert repo.retry_outbox(event.event_id, token, NOW + timedelta(seconds=5), "Boom")
    assert repo.claim_outbox(NOW, NOW + timedelta(seconds=30), uuid4()) is None
    later = NOW + timedelta(seconds=6)
    retry = repo.claim_outbox(later, later + timedelta(seconds=30), uuid4())
    assert retry is not None and retry.attempt_count == 2
    assert repo.acknowledge_outbox(retry.event_id, retry.lease_token, later)


def test_project_cover_context_reads_frozen_facts(tmp_path: Path) -> None:
    repo, sessions = _sql_repo(tmp_path / "cover-context.sqlite")
    owner_id, project_id, channel_id = uuid4(), uuid4(), uuid4()
    assert repo.project_cover_context(owner_id, project_id) is None
    _seed_project(
        sessions,
        owner_id,
        project_id,
        channel_id=channel_id,
        title="标题A",
        script="冻结文案",
        locale="zh-CN",
    )
    context = repo.project_cover_context(owner_id, project_id)
    assert context is not None
    assert context.project_id == project_id
    assert context.channel_id == channel_id
    assert context.title == "标题A"
    assert context.frozen_script == "冻结文案"
    assert context.locale == "zh-CN"
    # 快照角色图 id：桩表为 NULL（PG ARRAY 顺序由 PG 保证，服务层顺序逻辑另有覆盖）。
    assert context.snapshot_character_image_ids == ()
    assert repo.project_cover_context(uuid4(), project_id) is None


class _StubRepo:
    def __init__(self, event: CoverOutboxEvent) -> None:
        self.event = event
        self.acked = False
        self.retried = False

    def claim_outbox(self, now, lease_until, token):
        del now, lease_until
        if self.acked:
            return None
        return CoverOutboxEvent(
            self.event.event_id,
            self.event.owner_id,
            self.event.project_id,
            self.event.aggregate_type,
            self.event.aggregate_id,
            self.event.event_type,
            self.event.request_sha256,
            self.event.workflow_id,
            token,
            self.event.attempt_count,
        )

    def acknowledge_outbox(self, event_id, token, now) -> bool:
        del event_id, token, now
        self.acked = True
        return True

    def retry_outbox(self, event_id, token, next_at, error) -> bool:
        del event_id, token, next_at, error
        self.retried = True
        return True


def _event(
    event_type: str = COVER_REF_ANALYSIS_REQUESTED,
    aggregate_type: str = COVER_AGGREGATE_STYLE_PROJECT,
) -> CoverOutboxEvent:
    return CoverOutboxEvent(
        uuid4(),
        uuid4(),
        uuid4(),
        aggregate_type,
        uuid4(),
        event_type,
        "a" * 64,
        "stable-cover-workflow",
        uuid4(),
        1,
    )


@pytest.mark.asyncio
async def test_dispatcher_acks_stable_workflow_start() -> None:
    repo = _StubRepo(_event())

    class Starter:
        def __init__(self) -> None:
            self.workflow_ids: list[str] = []

        async def start(self, event: CoverOutboxEvent) -> None:
            self.workflow_ids.append(event.workflow_id)

    starter = Starter()
    dispatcher = CoverOutboxDispatcher(repo, starter, Clock(), Ids())
    assert await dispatcher.dispatch_one()
    assert repo.acked and starter.workflow_ids == ["stable-cover-workflow"]


@pytest.mark.asyncio
async def test_dispatcher_releases_failed_start_for_retry() -> None:
    repo = _StubRepo(_event())

    class Starter:
        async def start(self, event: CoverOutboxEvent) -> None:
            raise OSError

    dispatcher = CoverOutboxDispatcher(repo, Starter(), Clock(), Ids())
    assert not await dispatcher.dispatch_one()
    assert repo.retried and not repo.acked


@pytest.mark.asyncio
async def test_temporal_starter_routes_event_types_and_rejects_unknown() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def start_workflow(self, workflow: str, value, **kwargs) -> None:
            self.calls.append({"workflow": workflow, "value": value, **kwargs})

    client = Client()
    starter = TemporalCoverWorkflowStarter(client, "cover")  # type: ignore[arg-type]
    await starter.start(_event(COVER_REF_ANALYSIS_REQUESTED, COVER_AGGREGATE_STYLE_CHANNEL))
    await starter.start(
        _event(COVER_PROMPT_GENERATION_REQUESTED, COVER_AGGREGATE_PROMPT_GENERATION)
    )
    await starter.start(
        _event(COVER_IMAGE_GENERATION_REQUESTED, COVER_AGGREGATE_IMAGE_GENERATION)
    )
    await starter.start(
        _event(COVER_CHARACTER_CARD_ANALYSIS_REQUESTED, COVER_AGGREGATE_CHARACTER_CARD)
    )
    assert [call["workflow"] for call in client.calls] == [
        "CoverRefAnalysisWorkflow",
        "CoverPromptGenerationWorkflow",
        "CoverImageGenerationWorkflow",
        "CoverCharacterCardAnalysisWorkflow",
    ]
    analysis_call = client.calls[0]
    assert analysis_call["id_reuse_policy"] is WorkflowIDReusePolicy.TERMINATE_IF_RUNNING
    assert analysis_call["value"].scope == "channel"
    assert client.calls[1]["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert client.calls[2]["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
    card_call = client.calls[3]
    assert card_call["id_reuse_policy"] is WorkflowIDReusePolicy.TERMINATE_IF_RUNNING
    assert card_call["value"].scope == ""
    with pytest.raises(ValueError, match="unsupported"):
        await starter.start(_event("UNKNOWN"))
