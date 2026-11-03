"""STEP 11 简介的 PostgreSQL 集成测试（真实迁移、外键、幂等与字节完整性）。

需要显式隔离库：`TUBEFLOW_TEST_POSTGRES_URL`；本文件不调用真实 Provider。
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    ChannelRow,
    ProjectRow,
)
from tubeflow_test.adapters.persistence.models import CompositionVersionRow, MediaAssetRow
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_descriptions.domain import DEFAULT_DESCRIPTION_PROMPT
from tubeflow_test.video_descriptions.models import (
    ChannelDescriptionTemplateCounterRow,
    ProjectDescriptionAttemptRow,
    ProjectDescriptionRow,
)
from tubeflow_test.video_descriptions.ports import DescriptionProviderResult
from tubeflow_test.video_descriptions.repository import SqlVideoDescriptionRepository
from tubeflow_test.video_descriptions.service import VideoDescriptionService

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Storage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.objects[key]
        assert len(body) == expected_size
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        return body


class _Provider:
    def __init__(self, outcomes: Sequence[DescriptionProviderResult | Exception]) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[Sequence[Mapping[str, Any]]] = []

    def complete_json(
        self, messages: Sequence[Mapping[str, Any]], *, temperature: float
    ) -> DescriptionProviderResult:
        assert temperature == 0.4
        self.calls.append(list(messages))
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


def _srt(count: int = 7) -> bytes:
    rows = []
    for index in range(1, count + 1):
        start = (index - 1) * 2_000
        end = index * 2_000
        rows.append(
            f"{index}\n00:00:{start // 1000:02},000 --> 00:00:{end // 1000:02},000\n字幕 {index}"
        )
    return ("\n\n".join(rows) + "\n").encode()


def _payload() -> dict[str, object]:
    return {
        "hook": "钩子一。钩子二。",
        "intro_title": "开场",
        "segments": [
            {"entry": 2, "title": "成本"},
            {"entry": 3, "title": "客流"},
            {"entry": 4, "title": "固定支出"},
            {"entry": 5, "title": "利润"},
            {"entry": 6, "title": "答案"},
        ],
        "learn_points": ["要点一", "要点二", "要点三", "要点四", "要点五"],
        "closing": "收尾。",
        "disclaimer_key": "general",
    }


def _result() -> DescriptionProviderResult:
    return DescriptionProviderResult(_payload(), "pg-receipt", "fake-deepseek", "d" * 64)


def _seed_current_composition(
    postgres_database: object, owner_id: UUID, channel_id: UUID, project_id: UUID
) -> bytes:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    srt = _srt()
    video = f"fake-video-{uuid4()}".encode()
    srt_key = f"srt/{project_id}.srt"
    video_key = f"video/{project_id}.mp4"
    srt_asset_id, video_asset_id = uuid4(), uuid4()
    with sessions.begin() as session:
        channel = session.get(ChannelRow, channel_id)
        project = session.get(ProjectRow, project_id)
        assert channel is not None and project is not None
        channel.name = "韩国小生意经济财经"
        project.title = "最终简介项目"
        project.snapshot_script = "冻结文案全文"
        project.snapshot_locale = "ko-KR"
        project.snapshot_confirmed_at = NOW
        session.add_all(
            [
                MediaAssetRow(
                    id=video_asset_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    kind="COMPOSITION",
                    object_key=video_key,
                    sha256=hashlib.sha256(video).hexdigest(),
                    media_type="video/mp4",
                    size_bytes=len(video),
                    metadata_json={},
                ),
                MediaAssetRow(
                    id=srt_asset_id,
                    owner_id=owner_id,
                    project_id=project_id,
                    kind="SRT",
                    object_key=srt_key,
                    sha256=hashlib.sha256(srt).hexdigest(),
                    media_type="application/x-subrip",
                    size_bytes=len(srt),
                    metadata_json={},
                ),
            ]
        )
        session.flush()
        session.add(
            CompositionVersionRow(
                id=uuid4(),
                project_id=project_id,
                owner_id=owner_id,
                video_asset_id=video_asset_id,
                srt_asset_id=srt_asset_id,
                manifest_json={
                    "project_id": str(project_id),
                    "srt_sha256": hashlib.sha256(srt).hexdigest(),
                },
                current=True,
            )
        )
    return srt


def _service(postgres_database: object, provider: _Provider, objects: dict[str, bytes]):
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    return VideoDescriptionService(
        SqlVideoDescriptionRepository(sessions.begin, _Storage(objects)),  # type: ignore[arg-type]
        provider,
        _Clock(),
        model="fake-deepseek",
    )


def test_description_templates_and_generation_postgres_round_trip(
    postgres_database: object, production_parent_chain: object
) -> None:
    ids = production_parent_chain
    objects: dict[str, bytes] = {}
    provider = _Provider([_result()])
    service = _service(postgres_database, provider, objects)
    owner_id = ids.owner_id  # type: ignore[attr-defined]
    channel_id = ids.channel_id  # type: ignore[attr-defined]
    project_id = ids.project_id  # type: ignore[attr-defined]
    objects[f"srt/{project_id}.srt"] = _seed_current_composition(
        postgres_database, owner_id, channel_id, project_id
    )

    templates = service.list_templates(owner_id, channel_id)
    assert [item.version for item in templates] == [1]
    assert templates[0].prompt == DEFAULT_DESCRIPTION_PROMPT
    added = service.create_template(owner_id, channel_id, DEFAULT_DESCRIPTION_PROMPT + "\n补充")
    assert [item.version for item in added] == [1, 2]
    template_id = added[0].id
    service.delete_template(owner_id, channel_id, template_id)
    recreated = service.create_template(owner_id, channel_id, DEFAULT_DESCRIPTION_PROMPT)
    assert [item.version for item in recreated] == [2, 3]

    generated = service.generate(owner_id, project_id, recreated[0].id, "pg-desc-1")
    assert generated.version_number == 1
    assert generated.current is True
    assert generated.prompt_template_version == 2
    assert generated.provider_calls == 1
    assert "0:00 인트로: 开场" in generated.content
    assert "0:02 成本" in generated.content
    assert "韩国小生意经济财经" in generated.content
    replay = service.generate(owner_id, project_id, recreated[0].id, "pg-desc-1")
    assert replay.id == generated.id
    assert len(provider.calls) == 1

    edited = service.edit_version(owner_id, project_id, generated.id, "人工编辑后的简介")
    assert edited.source == "manual"
    assert edited.base_version_id == generated.id
    assert edited.current is True
    selected = service.select_current(owner_id, project_id, generated.id)
    assert selected.id == generated.id

    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        assert session.get(ChannelDescriptionTemplateCounterRow, channel_id) is not None
        rows = session.scalars(
            select(ProjectDescriptionRow).where(ProjectDescriptionRow.project_id == project_id)
        ).all()
        assert len(rows) == 2
        assert sum(row.current for row in rows) == 1
        attempts = session.scalars(
            select(ProjectDescriptionAttemptRow).where(
                ProjectDescriptionAttemptRow.project_id == project_id
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "SUCCEEDED"


def test_description_generation_blocks_without_current_composition(
    postgres_database: object, production_parent_chain: object
) -> None:
    ids = production_parent_chain
    provider = _Provider([])
    service = _service(postgres_database, provider, {})
    owner_id = ids.owner_id  # type: ignore[attr-defined]
    channel_id = ids.channel_id  # type: ignore[attr-defined]
    project_id = ids.project_id  # type: ignore[attr-defined]
    template = service.list_templates(owner_id, channel_id)[0]
    with pytest.raises(DomainError) as blocked:
        service.generate(owner_id, project_id, template.id, "pg-blocked")
    assert blocked.value.code == "description_composition_required"
    assert provider.calls == []


def test_stale_running_attempt_is_closed_only_by_a_new_manual_click(
    postgres_database: object, production_parent_chain: object
) -> None:
    ids = production_parent_chain
    objects: dict[str, bytes] = {}
    provider = _Provider([_result()])
    service = _service(postgres_database, provider, objects)
    owner_id = ids.owner_id  # type: ignore[attr-defined]
    channel_id = ids.channel_id  # type: ignore[attr-defined]
    project_id = ids.project_id  # type: ignore[attr-defined]
    objects[f"srt/{project_id}.srt"] = _seed_current_composition(
        postgres_database, owner_id, channel_id, project_id
    )
    template = service.list_templates(owner_id, channel_id)[0]
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    with sessions.begin() as session:
        session.add(
            ProjectDescriptionAttemptRow(
                id=uuid4(),
                project_id=project_id,
                owner_id=owner_id,
                idempotency_key="stale-running",
                template_id=template.id,
                template_version=template.version,
                input_sha256="e" * 64,
                status="RUNNING",
                created_at=NOW - timedelta(hours=1),
            )
        )
    generated = service.generate(owner_id, project_id, template.id, "fresh-manual-click")
    assert generated.current is True
    with sessions() as session:
        attempts = session.scalars(
            select(ProjectDescriptionAttemptRow)
            .where(ProjectDescriptionAttemptRow.project_id == project_id)
            .order_by(ProjectDescriptionAttemptRow.created_at)
        ).all()
    assert [item.status for item in attempts] == ["FAILED", "SUCCEEDED"]
    assert attempts[0].error_code == "description_generation_interrupted"
    assert len(provider.calls) == 1


def test_description_rows_cascade_when_bare_project_is_deleted(
    postgres_database: object,
) -> None:
    sessions = postgres_database.sessions  # type: ignore[attr-defined]
    owner_id, channel_id, project_id = uuid4(), uuid4(), uuid4()
    description_id, attempt_id = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            AccountRow(
                id=owner_id,
                username=f"desc-owner-{owner_id.hex[:8]}",
                password_hash="hashed",
                role="USER",
                active=True,
                session_generation=1,
            )
        )
        session.add(
            ChannelRow(
                id=channel_id,
                owner_id=owner_id,
                url="https://example.test/description-cascade",
                name="简介频道",
            )
        )
        session.flush()
        session.add(
            ProjectRow(
                id=project_id,
                owner_id=owner_id,
                channel_id=channel_id,
                title="bare project",
                script="script",
                character_image_ids=[uuid4()],
                visual_style="style",
                locale="en-US",
                aspect_ratio="16:9",
            )
        )
        session.flush()
        session.add(
            ProjectDescriptionRow(
                id=description_id,
                project_id=project_id,
                owner_id=owner_id,
                version_number=1,
                source="manual",
                base_version_id=None,
                prompt_template_version=1,
                prompt_template_sha256="a" * 64,
                prompt_template_snapshot="prompt",
                title_used="bare project",
                channel_name_used="简介频道",
                locale="en-US",
                script_sha256="b" * 64,
                srt_asset_id=None,
                srt_sha256="c" * 64,
                payload_json={},
                content="manual description",
                disclaimer_key="general",
                model=None,
                provider_receipt="",
                raw_response_sha256="",
                provider_calls=0,
                elapsed_ms=0,
                current=True,
                created_at=NOW,
            )
        )
        session.add(
            ProjectDescriptionAttemptRow(
                id=attempt_id,
                project_id=project_id,
                owner_id=owner_id,
                idempotency_key="cascade-key",
                template_id=uuid4(),
                template_version=1,
                input_sha256="d" * 64,
                status="SUCCEEDED",
                description_id=description_id,
                created_at=NOW,
                finished_at=NOW,
            )
        )
        session.delete(session.get(ProjectRow, project_id))
    with sessions() as session:
        assert session.get(ProjectDescriptionRow, description_id) is None
        assert session.get(ProjectDescriptionAttemptRow, attempt_id) is None
