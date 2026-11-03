from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.video_descriptions.api import create_video_description_router
from tubeflow_test.video_descriptions.domain import (
    DEFAULT_DESCRIPTION_PROMPT,
    DescriptionAttempt,
    DescriptionPayload,
    DescriptionProjectSource,
    DescriptionTemplate,
    ProjectDescription,
    parse_srt,
    prompt_sha256,
)
from tubeflow_test.video_descriptions.ports import DescriptionProviderResult
from tubeflow_test.video_descriptions.service import VideoDescriptionService

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
OWNER = Account(uuid4(), "owner", "hashed:x", AccountRole.USER)
OTHER = Account(uuid4(), "other", "hashed:x", AccountRole.USER)


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeProvider:
    def __init__(self, outcomes: object = ()) -> None:
        self.outcomes = deque(outcomes)  # type: ignore[arg-type]
        self.calls = 0

    def queue(self, outcome: object) -> None:
        self.outcomes.append(outcome)

    def complete_json(self, messages: object, *, temperature: float) -> DescriptionProviderResult:
        del messages
        assert temperature == 0.4
        self.calls += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, DescriptionProviderResult)
        return outcome

    def close(self) -> None:
        return None


class FakeRepo:
    def __init__(self) -> None:
        self.channels: dict[UUID, UUID] = {}
        self.next_versions: dict[UUID, int] = {}
        self.templates: dict[UUID, list[DescriptionTemplate]] = {}
        self.projects: dict[
            tuple[UUID, UUID], tuple[UUID, str, DescriptionProjectSource | None]
        ] = {}
        self.attempts: list[DescriptionAttempt] = []
        self.versions: list[ProjectDescription] = []

    def add_channel(self, owner_id: UUID = OWNER.id) -> UUID:
        channel_id = uuid4()
        self.channels[channel_id] = owner_id
        return channel_id

    def add_project(
        self, channel_id: UUID, *, source: DescriptionProjectSource | None
    ) -> UUID:
        project_id = uuid4()
        self.projects[(OWNER.id, project_id)] = (channel_id, "韩国小生意经济财经", source)
        return project_id

    def channel_exists(self, owner_id: UUID, channel_id: UUID) -> bool:
        return self.channels.get(channel_id) == owner_id

    def ensure_default_template(
        self, owner_id: UUID, channel_id: UUID, prompt: str, now: datetime
    ) -> None:
        if not self.channel_exists(owner_id, channel_id):
            raise DomainError("not_found", "Channel was not found")
        if channel_id in self.next_versions:
            return
        self.next_versions[channel_id] = 2
        if not self.templates.get(channel_id):
            version = 1
            self.templates.setdefault(channel_id, []).append(
                DescriptionTemplate(uuid4(), channel_id, version, prompt, now, now)
            )

    def list_templates(self, owner_id: UUID, channel_id: UUID) -> tuple[DescriptionTemplate, ...]:
        return tuple(self.templates.get(channel_id, []))

    def create_template(
        self, owner_id: UUID, channel_id: UUID, prompt: str, now: datetime
    ) -> DescriptionTemplate:
        version = self.next_versions[channel_id]
        self.next_versions[channel_id] += 1
        row = DescriptionTemplate(uuid4(), channel_id, version, prompt, now, now)
        self.templates[channel_id].append(row)
        return row

    def update_template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID, prompt: str, now: datetime
    ) -> DescriptionTemplate:
        items = self.templates.get(channel_id, [])
        index = next((i for i, item in enumerate(items) if item.id == template_id), None)
        if index is None:
            raise DomainError("not_found", "Description template was not found")
        old = items[index]
        items[index] = DescriptionTemplate(
            old.id, channel_id, old.version, prompt, old.created_at, now
        )
        return items[index]

    def delete_template(self, owner_id: UUID, channel_id: UUID, template_id: UUID) -> None:
        items = self.templates.get(channel_id, [])
        if not any(item.id == template_id for item in items):
            raise DomainError("not_found", "Description template was not found")
        self.templates[channel_id] = [item for item in items if item.id != template_id]

    def template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID
    ) -> DescriptionTemplate | None:
        return next(
            (item for item in self.templates.get(channel_id, []) if item.id == template_id), None
        )

    def project_ref(self, owner_id: UUID, project_id: UUID) -> tuple[UUID, str] | None:
        value = self.projects.get((owner_id, project_id))
        return None if value is None else (value[0], value[1])

    def project_source(
        self, owner_id: UUID, project_id: UUID
    ) -> DescriptionProjectSource | None:
        value = self.projects.get((owner_id, project_id))
        return None if value is None else value[2]

    def list_versions(self, owner_id: UUID, project_id: UUID) -> tuple[ProjectDescription, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.versions
                    if item.owner_id == owner_id and item.project_id == project_id
                ),
                key=lambda item: item.version_number,
                reverse=True,
            )
        )

    def version(
        self, owner_id: UUID, project_id: UUID, version_id: UUID
    ) -> ProjectDescription | None:
        return next(
            (
                item
                for item in self.versions
                if item.owner_id == owner_id
                and item.project_id == project_id
                and item.id == version_id
            ),
            None,
        )

    def active_attempt(self, owner_id: UUID, project_id: UUID) -> DescriptionAttempt | None:
        return next(
            (
                item
                for item in self.attempts
                if item.owner_id == owner_id
                and item.project_id == project_id
                and item.status == "RUNNING"
            ),
            None,
        )

    def attempt_by_key(
        self, owner_id: UUID, project_id: UUID, idempotency_key: str
    ) -> DescriptionAttempt | None:
        return next(
            (
                item
                for item in self.attempts
                if item.owner_id == owner_id
                and item.project_id == project_id
                and item.idempotency_key == idempotency_key
            ),
            None,
        )

    def begin_attempt(
        self,
        *,
        owner_id: UUID,
        project_id: UUID,
        idempotency_key: str,
        template: DescriptionTemplate,
        input_sha256: str,
        now: datetime,
        stale_before: datetime | None = None,
    ) -> tuple[DescriptionAttempt, bool]:
        existing = self.attempt_by_key(owner_id, project_id, idempotency_key)
        if existing is not None:
            if existing.status == "FAILED":
                index = next(i for i, item in enumerate(self.attempts) if item.id == existing.id)
                self.attempts[index] = DescriptionAttempt(
                    existing.id,
                    project_id,
                    owner_id,
                    idempotency_key,
                    template.id,
                    template.version,
                    input_sha256,
                    "RUNNING",
                    None,
                    None,
                    now,
                    None,
                )
                return self.attempts[index], True
            return existing, False
        if self.active_attempt(owner_id, project_id) is not None:
            raise DomainError("description_generation_in_progress", "已有生成中")
        row = DescriptionAttempt(
            uuid4(),
            project_id,
            owner_id,
            idempotency_key,
            template.id,
            template.version,
            input_sha256,
            "RUNNING",
            None,
            None,
            now,
            None,
        )
        self.attempts.append(row)
        return row, True

    def complete_attempt(
        self,
        attempt: DescriptionAttempt,
        *,
        source: DescriptionProjectSource,
        template: DescriptionTemplate,
        payload: DescriptionPayload,
        content: str,
        provider_model: str,
        provider_receipt: str,
        raw_response_sha256: str,
        provider_calls: int,
        elapsed_ms: int,
        now: datetime,
    ) -> ProjectDescription:
        row = ProjectDescription(
            uuid4(),
            source.project_id,
            source.owner_id,
            max((item.version_number for item in self.versions), default=0) + 1,
            "generated",
            None,
            template.version,
            prompt_sha256(template.prompt),
            template.prompt,
            source.title,
            source.channel_name,
            source.locale,
            source.script_sha256,
            source.srt_asset_id,
            source.srt_sha256,
            payload.as_dict(),
            content,
            payload.disclaimer_key,
            provider_model,
            provider_receipt,
            raw_response_sha256,
            provider_calls,
            elapsed_ms,
            True,
            now,
        )
        self.versions = [
            item
            if item.project_id != source.project_id
            else replace(item, current=False)
            for item in self.versions
        ]
        self.versions.append(row)
        index = next(i for i, item in enumerate(self.attempts) if item.id == attempt.id)
        old = self.attempts[index]
        self.attempts[index] = DescriptionAttempt(
            old.id,
            old.project_id,
            old.owner_id,
            old.idempotency_key,
            old.template_id,
            old.template_version,
            old.input_sha256,
            "SUCCEEDED",
            None,
            row.id,
            old.created_at,
            now,
        )
        return row

    def fail_attempt(self, attempt: DescriptionAttempt, *, error_code: str, now: datetime) -> None:
        index = next(i for i, item in enumerate(self.attempts) if item.id == attempt.id)
        old = self.attempts[index]
        self.attempts[index] = DescriptionAttempt(
            old.id,
            old.project_id,
            old.owner_id,
            old.idempotency_key,
            old.template_id,
            old.template_version,
            old.input_sha256,
            "FAILED",
            error_code,
            None,
            old.created_at,
            now,
        )

    def create_manual_edit(
        self, base: ProjectDescription, *, content: str, now: datetime
    ) -> ProjectDescription:
        self.versions = [
            item
            if item.project_id != base.project_id
            else replace(item, current=False)
            for item in self.versions
        ]
        row = ProjectDescription(
            uuid4(),
            base.project_id,
            base.owner_id,
            max(item.version_number for item in self.versions) + 1,
            "manual",
            base.id,
            base.prompt_template_version,
            base.prompt_template_sha256,
            base.prompt_template_snapshot,
            base.title_used,
            base.channel_name_used,
            base.locale,
            base.script_sha256,
            base.srt_asset_id,
            base.srt_sha256,
            {},
            content,
            base.disclaimer_key,
            base.model,
            base.provider_receipt,
            base.raw_response_sha256,
            0,
            0,
            True,
            now,
        )
        self.versions.append(row)
        return row

    def select_current(
        self, owner_id: UUID, project_id: UUID, version_id: UUID
    ) -> ProjectDescription:
        selected = self.version(owner_id, project_id, version_id)
        if selected is None:
            raise DomainError("not_found", "Description version was not found")
        self.versions = [
            replace(item, current=item.id == version_id)
            for item in self.versions
        ]
        return selected


def _srt(count: int = 7) -> bytes:
    rows = []
    for index in range(1, count + 1):
        start = (index - 1) * 2_000
        rows.append(
            f"{index}\n00:00:{start // 1000:02},000 --> 00:00:{index * 2:02},000\n字幕 {index}"
        )
    return ("\n\n".join(rows) + "\n").encode()


def _source(owner_id: UUID, channel_id: UUID, project_id: UUID) -> DescriptionProjectSource:
    return DescriptionProjectSource(
        project_id=project_id,
        owner_id=owner_id,
        channel_id=channel_id,
        title="测试标题",
        script="冻结文案全文",
        locale="ko-KR",
        channel_name="韩国小生意经济财经",
        channel_url="https://example.test/channel",
        composition_id=uuid4(),
        srt_asset_id=uuid4(),
        srt_sha256="b" * 64,
        srt_entries=parse_srt(_srt()),
    )


def _payload(entries: tuple[int, ...] = (2, 3, 4, 5, 6)) -> dict[str, object]:
    return {
        "hook": "钩子一。钩子二。",
        "intro_title": "开场",
        "segments": [{"entry": entry, "title": f"章节 {entry}"} for entry in entries],
        "learn_points": ["要点一", "要点二", "要点三", "要点四", "要点五"],
        "closing": "收尾。",
        "disclaimer_key": "general",
    }


def _result(payload: dict[str, object]) -> DescriptionProviderResult:
    return DescriptionProviderResult(payload, "receipt-1", "fake-deepseek", "c" * 64)


def _client(repo: FakeRepo, provider: FakeProvider, account: Account = OWNER) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_video_description_router(
            VideoDescriptionService(repo, provider, Clock(), model="fake-deepseek"),
            lambda: account,
        )
    )
    return TestClient(app)


def _generated_project(repo: FakeRepo) -> tuple[UUID, UUID]:
    channel_id = repo.add_channel()
    project_id = repo.add_project(channel_id, source=None)
    repo.projects[(OWNER.id, project_id)] = (
        channel_id,
        "韩国小生意经济财经",
        _source(OWNER.id, channel_id, project_id),
    )
    return channel_id, project_id


def test_template_lifecycle_seeds_v1_and_never_reuses_deleted_version() -> None:
    repo, provider = FakeRepo(), FakeProvider()
    channel_id = repo.add_channel()
    client = _client(repo, provider)
    first = client.get(f"/identity/channels/{channel_id}/description-templates")
    assert first.status_code == 200
    items = first.json()["items"]
    assert [item["version"] for item in items] == [1]
    assert items[0]["prompt"] == DEFAULT_DESCRIPTION_PROMPT

    created = client.post(
        f"/identity/channels/{channel_id}/description-templates",
        json={"prompt": DEFAULT_DESCRIPTION_PROMPT + "\n追加要求"},
    )
    assert created.status_code == 201
    assert [item["version"] for item in created.json()["items"]] == [1, 2]
    updated = client.put(
        f"/identity/channels/{channel_id}/description-templates/{created.json()['items'][1]['id']}",
        json={"prompt": DEFAULT_DESCRIPTION_PROMPT + "\n修订"},
    )
    assert updated.status_code == 200
    deleted = client.delete(
        f"/identity/channels/{channel_id}/description-templates/{first.json()['items'][0]['id']}"
    )
    assert deleted.status_code == 200
    assert [item["version"] for item in deleted.json()["items"]] == [2]
    recreated = client.post(
        f"/identity/channels/{channel_id}/description-templates",
        json={"prompt": DEFAULT_DESCRIPTION_PROMPT},
    )
    assert [item["version"] for item in recreated.json()["items"]] == [2, 3]
    for item in recreated.json()["items"]:
        assert client.delete(
            f"/identity/channels/{channel_id}/description-templates/{item['id']}"
        ).status_code == 200
    empty = client.get(f"/identity/channels/{channel_id}/description-templates")
    assert empty.json()["items"] == []
    created_again = client.post(
        f"/identity/channels/{channel_id}/description-templates",
        json={"prompt": DEFAULT_DESCRIPTION_PROMPT},
    )
    assert [item["version"] for item in created_again.json()["items"]] == [4]

    other = _client(repo, provider, OTHER)
    assert other.get(f"/identity/channels/{channel_id}/description-templates").status_code == 404
    invalid = client.post(
        f"/identity/channels/{channel_id}/description-templates",
        json={"prompt": "{{topic}} {{language}} {{script}} {{channel_name}}"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "description_template_placeholder_required"


def test_generation_uses_final_srt_times_and_idempotency_key_replay() -> None:
    repo, provider = FakeRepo(), FakeProvider([_result(_payload())])
    channel_id, project_id = _generated_project(repo)
    client = _client(repo, provider)
    templates = client.get(f"/identity/channels/{channel_id}/description-templates").json()["items"]
    state = client.get(f"/projects/{project_id}/description")
    assert state.status_code == 200
    assert state.json()["available"] is True
    response = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": templates[0]["id"]},
        headers={"Idempotency-Key": "desc-test-1"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["current"]["version_number"] == 1
    assert body["current"]["prompt_template_version"] == 1
    assert body["current"]["provider_calls"] == 1
    assert "0:00 인트로: 开场" in body["current"]["content"]
    assert "0:02 章节 2" in body["current"]["content"]
    assert "韩国小生意经济财经" in body["current"]["content"]

    replay = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": templates[0]["id"]},
        headers={"Idempotency-Key": "desc-test-1"},
    )
    assert replay.status_code == 201
    assert replay.json()["current"]["id"] == body["current"]["id"]
    assert provider.calls == 1


def test_generation_repairs_invalid_output_once_and_records_two_calls() -> None:
    repo, provider = FakeRepo(), FakeProvider(
        [_result(_payload((1, 2, 3, 4, 5))), _result(_payload())]
    )
    channel_id, project_id = _generated_project(repo)
    client = _client(repo, provider)
    template = client.get(f"/identity/channels/{channel_id}/description-templates").json()[
        "items"
    ][0]
    response = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": template["id"]},
        headers={"Idempotency-Key": "desc-repair"},
    )
    assert response.status_code == 201
    assert response.json()["current"]["provider_calls"] == 2
    assert provider.calls == 2


def test_provider_failure_preserves_current_and_allows_fresh_manual_retry() -> None:
    repo, provider = FakeRepo(), FakeProvider(
        [
            DomainError("description_provider_unavailable", "暂不可用"),
            _result(_payload()),
        ]
    )
    channel_id, project_id = _generated_project(repo)
    client = _client(repo, provider)
    template = client.get(f"/identity/channels/{channel_id}/description-templates").json()[
        "items"
    ][0]
    failed = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": template["id"]},
        headers={"Idempotency-Key": "desc-fail-1"},
    )
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "description_provider_unavailable"
    retry = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": template["id"]},
        headers={"Idempotency-Key": "desc-fail-1"},
    )
    assert retry.status_code == 201
    assert provider.calls == 2
    assert len(repo.attempts) == 1
    assert repo.attempts[0].status == "SUCCEEDED"


def test_history_manual_edit_and_current_switch() -> None:
    repo, provider = FakeRepo(), FakeProvider([_result(_payload()), _result(_payload())])
    channel_id, project_id = _generated_project(repo)
    client = _client(repo, provider)
    template = client.get(f"/identity/channels/{channel_id}/description-templates").json()[
        "items"
    ][0]
    first = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": template["id"]},
        headers={"Idempotency-Key": "history-1"},
    ).json()["current"]
    second = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": template["id"]},
        headers={"Idempotency-Key": "history-2"},
    ).json()["current"]
    assert second["version_number"] == 2

    edited = client.post(
        f"/projects/{project_id}/description/versions/{first['id']}/edits",
        json={"content": "人工修订简介"},
    )
    assert edited.status_code == 201
    manual = edited.json()["created"]
    assert manual["source"] == "manual"
    assert manual["base_version_id"] == first["id"]
    assert edited.json()["current"]["id"] == manual["id"]

    selected = client.post(
        f"/projects/{project_id}/description/current", json={"version_id": first["id"]}
    )
    assert selected.status_code == 200
    assert selected.json()["current"]["id"] == first["id"]
    assert len(selected.json()["versions"]) == 3


def test_missing_final_srt_blocks_generation_without_provider_call() -> None:
    repo, provider = FakeRepo(), FakeProvider()
    channel_id = repo.add_channel()
    project_id = repo.add_project(channel_id, source=None)
    client = _client(repo, provider)
    state = client.get(f"/projects/{project_id}/description")
    assert state.json()["available"] is False
    assert state.json()["blocking_reason"] == "description_composition_required"
    template = client.get(f"/identity/channels/{channel_id}/description-templates").json()[
        "items"
    ][0]
    response = client.post(
        f"/projects/{project_id}/description/generations",
        json={"template_id": template["id"]},
        headers={"Idempotency-Key": "blocked"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "description_composition_required"
    assert provider.calls == 0


def test_cross_account_project_description_is_not_found() -> None:
    repo, provider = FakeRepo(), FakeProvider()
    _channel_id, project_id = _generated_project(repo)
    other = _client(repo, provider, OTHER)
    response = other.get(f"/projects/{project_id}/description")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
