"""视频简介 SQL 适配器：频道模板、最终 SRT 来源、幂等尝试与版本当前指针。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.identity_project.models import ChannelRow, ProjectRow
from tubeflow_test.adapters.persistence.models import CompositionVersionRow, MediaAssetRow
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.video_descriptions.domain import (
    DescriptionAttempt,
    DescriptionPayload,
    DescriptionProjectSource,
    DescriptionTemplate,
    ProjectDescription,
    parse_srt,
    prompt_sha256,
)
from tubeflow_test.video_descriptions.models import (
    ChannelDescriptionTemplateCounterRow,
    ChannelDescriptionTemplateRow,
    ProjectDescriptionAttemptRow,
    ProjectDescriptionRow,
)


class SqlVideoDescriptionRepository:
    def __init__(
        self,
        unit_of_work: Callable[[], AbstractContextManager[Session]],
        storage: PrivateObjectStorage,
    ) -> None:
        self._uow = unit_of_work
        self._storage = storage

    def channel_exists(self, owner_id: UUID, channel_id: UUID) -> bool:
        with self._uow() as session:
            return (
                session.scalar(
                    select(ChannelRow.id).where(
                        ChannelRow.id == channel_id, ChannelRow.owner_id == owner_id
                    )
                )
                is not None
            )

    def ensure_default_template(
        self, owner_id: UUID, channel_id: UUID, prompt: str, now: datetime
    ) -> None:
        try:
            with self._uow() as session:
                self._require_channel(session, owner_id, channel_id)
                counter = self._counter_for_update(session, channel_id)
                if counter is None:
                    session.add(
                        ChannelDescriptionTemplateCounterRow(
                            channel_id=channel_id, next_version=2
                        )
                    )
                    session.add(
                        ChannelDescriptionTemplateRow(
                            channel_id=channel_id,
                            version=1,
                            prompt=prompt,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                session.flush()
        except IntegrityError:
            # 并发初始化撞唯一键：只要另一个事务已经建立默认模板即视为幂等成功。
            with self._uow() as session:
                self._require_channel(session, owner_id, channel_id)
                if self._template_count(session, channel_id):
                    return
            raise

    def list_templates(self, owner_id: UUID, channel_id: UUID) -> tuple[DescriptionTemplate, ...]:
        with self._uow() as session:
            self._require_channel(session, owner_id, channel_id)
            rows = session.scalars(
                select(ChannelDescriptionTemplateRow)
                .where(ChannelDescriptionTemplateRow.channel_id == channel_id)
                .order_by(ChannelDescriptionTemplateRow.version)
            ).all()
            return tuple(self._template(row) for row in rows)

    def create_template(
        self, owner_id: UUID, channel_id: UUID, prompt: str, now: datetime
    ) -> DescriptionTemplate:
        with self._uow() as session:
            self._require_channel(session, owner_id, channel_id)
            counter = self._counter_for_update(session, channel_id)
            if counter is None:
                raise DomainError(
                    "description_template_counter_missing", "简介模板版本计数不可用。"
                )
            version = counter.next_version
            row = ChannelDescriptionTemplateRow(
                channel_id=channel_id,
                version=version,
                prompt=prompt,
                created_at=now,
                updated_at=now,
            )
            counter.next_version += 1
            session.add(row)
            session.flush()
            return self._template(row)

    def update_template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID, prompt: str, now: datetime
    ) -> DescriptionTemplate:
        with self._uow() as session:
            row = self._template_for_update(session, owner_id, channel_id, template_id)
            row.prompt = prompt
            row.updated_at = now
            session.flush()
            return self._template(row)

    def delete_template(self, owner_id: UUID, channel_id: UUID, template_id: UUID) -> None:
        with self._uow() as session:
            row = self._template_for_update(session, owner_id, channel_id, template_id)
            session.delete(row)
            session.flush()

    def template(
        self, owner_id: UUID, channel_id: UUID, template_id: UUID
    ) -> DescriptionTemplate | None:
        with self._uow() as session:
            row = session.scalar(
                select(ChannelDescriptionTemplateRow)
                .join(ChannelRow, ChannelRow.id == ChannelDescriptionTemplateRow.channel_id)
                .where(
                    ChannelDescriptionTemplateRow.id == template_id,
                    ChannelDescriptionTemplateRow.channel_id == channel_id,
                    ChannelRow.owner_id == owner_id,
                )
            )
            return self._template(row) if row is not None else None

    def project_ref(self, owner_id: UUID, project_id: UUID) -> tuple[UUID, str] | None:
        with self._uow() as session:
            row = session.execute(
                select(ProjectRow.channel_id, ChannelRow.name, ChannelRow.url)
                .join(ChannelRow, ChannelRow.id == ProjectRow.channel_id)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            ).first()
            if row is None:
                return None
            channel_id, name, url = row
            return channel_id, self._channel_description_name(name, url)

    def project_source(
        self, owner_id: UUID, project_id: UUID
    ) -> DescriptionProjectSource | None:
        with self._uow() as session:
            row = session.execute(
                select(ProjectRow, ChannelRow)
                .join(ChannelRow, ChannelRow.id == ProjectRow.channel_id)
                .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            ).first()
            if row is None:
                raise DomainError("not_found", "Resource not found")
            project, channel = row
            if project.production_state != "ACTIVE":
                raise DomainError("project_purging", "Project is being permanently deleted")
            composition = session.scalar(
                select(CompositionVersionRow).where(
                    CompositionVersionRow.project_id == project_id,
                    CompositionVersionRow.owner_id == owner_id,
                    CompositionVersionRow.current.is_(True),
                )
            )
            if composition is None:
                return None
            if not project.snapshot_script:
                raise DomainError(
                    "description_project_not_frozen", "项目尚未形成冻结文案快照。"
                )
            srt = session.scalar(
                select(MediaAssetRow).where(
                    MediaAssetRow.id == composition.srt_asset_id,
                    MediaAssetRow.owner_id == owner_id,
                    MediaAssetRow.project_id == project_id,
                    MediaAssetRow.kind == "SRT",
                    MediaAssetRow.media_type == "application/x-subrip",
                    MediaAssetRow.purging.is_(False),
                    MediaAssetRow.deletion_state == "ACTIVE",
                )
            )
            if (
                srt is None
                or composition.manifest_json.get("project_id") != str(project_id)
                or composition.manifest_json.get("srt_sha256") != srt.sha256
                or len(srt.sha256) != 64
                or srt.size_bytes <= 0
            ):
                raise DomainError(
                    "description_srt_unavailable", "当前最终 SRT 资产不可用或血缘不一致。"
                )
            object_key = srt.object_key
            expected_sha256 = srt.sha256
            expected_size = srt.size_bytes
            script = project.snapshot_script
            locale = project.snapshot_locale or project.locale
            title = project.title
            channel_name = self._channel_description_name(channel.name, channel.url)
            channel_url = channel.url
        try:
            content = self._storage.get_verified(object_key, expected_sha256, expected_size)
        except Exception as exc:
            raise DomainError(
                "description_srt_unavailable", "当前最终 SRT 完整性校验失败。"
            ) from exc
        entries = parse_srt(content)
        return DescriptionProjectSource(
            project_id=project_id,
            owner_id=owner_id,
            channel_id=channel.id,
            title=title,
            script=script or "",
            locale=locale,
            channel_name=channel_name,
            channel_url=channel_url,
            composition_id=composition.id,
            srt_asset_id=srt.id,
            srt_sha256=srt.sha256,
            srt_entries=entries,
        )

    def list_versions(self, owner_id: UUID, project_id: UUID) -> tuple[ProjectDescription, ...]:
        with self._uow() as session:
            rows = session.scalars(
                select(ProjectDescriptionRow)
                .where(
                    ProjectDescriptionRow.project_id == project_id,
                    ProjectDescriptionRow.owner_id == owner_id,
                )
                .order_by(ProjectDescriptionRow.version_number.desc())
            ).all()
            return tuple(self._description(row) for row in rows)

    def version(
        self, owner_id: UUID, project_id: UUID, version_id: UUID
    ) -> ProjectDescription | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDescriptionRow).where(
                    ProjectDescriptionRow.id == version_id,
                    ProjectDescriptionRow.project_id == project_id,
                    ProjectDescriptionRow.owner_id == owner_id,
                )
            )
            return self._description(row) if row is not None else None

    def active_attempt(self, owner_id: UUID, project_id: UUID) -> DescriptionAttempt | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDescriptionAttemptRow)
                .where(
                    ProjectDescriptionAttemptRow.project_id == project_id,
                    ProjectDescriptionAttemptRow.owner_id == owner_id,
                    ProjectDescriptionAttemptRow.status == "RUNNING",
                )
                .order_by(ProjectDescriptionAttemptRow.created_at.desc())
            )
            return self._attempt(row) if row is not None else None

    def attempt_by_key(
        self, owner_id: UUID, project_id: UUID, idempotency_key: str
    ) -> DescriptionAttempt | None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDescriptionAttemptRow).where(
                    ProjectDescriptionAttemptRow.project_id == project_id,
                    ProjectDescriptionAttemptRow.owner_id == owner_id,
                    ProjectDescriptionAttemptRow.idempotency_key == idempotency_key,
                )
            )
            return self._attempt(row) if row is not None else None

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
        try:
            with self._uow() as session:
                self._project_for_update(session, owner_id, project_id)
                existing = session.scalar(
                    select(ProjectDescriptionAttemptRow).where(
                        ProjectDescriptionAttemptRow.project_id == project_id,
                        ProjectDescriptionAttemptRow.owner_id == owner_id,
                        ProjectDescriptionAttemptRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.status == "FAILED":
                        existing.status = "RUNNING"
                        existing.error_code = None
                        existing.description_id = None
                        existing.template_id = template.id
                        existing.template_version = template.version
                        existing.input_sha256 = input_sha256
                        existing.created_at = now
                        existing.finished_at = None
                        session.flush()
                        return self._attempt(existing), True
                    return self._attempt(existing), False
                active = session.scalar(
                    select(ProjectDescriptionAttemptRow).where(
                        ProjectDescriptionAttemptRow.project_id == project_id,
                        ProjectDescriptionAttemptRow.status == "RUNNING",
                    )
                )
                if (
                    active is not None
                    and stale_before is not None
                    and active.created_at < stale_before
                ):
                    active.status = "FAILED"
                    active.error_code = "description_generation_interrupted"
                    active.finished_at = now
                    session.flush()
                    active = None
                if active is not None:
                    raise DomainError(
                        "description_generation_in_progress", "该项目已有正在进行的简介生成。"
                    )
                row = ProjectDescriptionAttemptRow(
                    project_id=project_id,
                    owner_id=owner_id,
                    idempotency_key=idempotency_key,
                    template_id=template.id,
                    template_version=template.version,
                    input_sha256=input_sha256,
                    status="RUNNING",
                    created_at=now,
                )
                session.add(row)
                session.flush()
                return self._attempt(row), True
        except IntegrityError:
            with self._uow() as session:
                existing = session.scalar(
                    select(ProjectDescriptionAttemptRow).where(
                        ProjectDescriptionAttemptRow.project_id == project_id,
                        ProjectDescriptionAttemptRow.owner_id == owner_id,
                        ProjectDescriptionAttemptRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return self._attempt(existing), False
                active_row = session.scalar(
                    select(ProjectDescriptionAttemptRow).where(
                        ProjectDescriptionAttemptRow.project_id == project_id,
                        ProjectDescriptionAttemptRow.status == "RUNNING",
                    )
                )
                if active_row is not None:
                    raise DomainError(
                        "description_generation_in_progress", "该项目已有正在进行的简介生成。"
                    ) from None
            raise

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
        with self._uow() as session:
            self._project_for_update(session, source.owner_id, source.project_id)
            attempt_row = self._attempt_for_update(session, attempt.id, source.owner_id)
            if attempt_row.status == "SUCCEEDED" and attempt_row.description_id is not None:
                existing = session.get(ProjectDescriptionRow, attempt_row.description_id)
                if existing is not None:
                    return self._description(existing)
            if attempt_row.status != "RUNNING":
                raise DomainError(
                    "description_generation_conflict", "简介生成任务状态已变化。"
                )
            session.execute(
                update(ProjectDescriptionRow)
                .where(
                    ProjectDescriptionRow.project_id == source.project_id,
                    ProjectDescriptionRow.owner_id == source.owner_id,
                    ProjectDescriptionRow.current.is_(True),
                )
                .values(current=False)
            )
            version_number = int(
                session.scalar(
                    select(func.coalesce(func.max(ProjectDescriptionRow.version_number), 0)).where(
                        ProjectDescriptionRow.project_id == source.project_id,
                        ProjectDescriptionRow.owner_id == source.owner_id,
                    )
                )
                or 0
            ) + 1
            row = ProjectDescriptionRow(
                project_id=source.project_id,
                owner_id=source.owner_id,
                version_number=version_number,
                source="generated",
                base_version_id=None,
                prompt_template_version=template.version,
                prompt_template_sha256=prompt_sha256(template.prompt),
                prompt_template_snapshot=template.prompt,
                title_used=source.title,
                channel_name_used=source.channel_name,
                locale=source.locale,
                script_sha256=source.script_sha256,
                srt_asset_id=source.srt_asset_id,
                srt_sha256=source.srt_sha256,
                payload_json=payload.as_dict(),
                content=content,
                disclaimer_key=payload.disclaimer_key,
                model=provider_model,
                provider_receipt=provider_receipt,
                raw_response_sha256=raw_response_sha256,
                provider_calls=provider_calls,
                elapsed_ms=elapsed_ms,
                current=True,
                created_at=now,
            )
            session.add(row)
            session.flush()
            attempt_row.status = "SUCCEEDED"
            attempt_row.description_id = row.id
            attempt_row.finished_at = now
            session.flush()
            return self._description(row)

    def fail_attempt(
        self, attempt: DescriptionAttempt, *, error_code: str, now: datetime
    ) -> None:
        with self._uow() as session:
            row = session.scalar(
                select(ProjectDescriptionAttemptRow)
                .where(
                    ProjectDescriptionAttemptRow.id == attempt.id,
                    ProjectDescriptionAttemptRow.owner_id == attempt.owner_id,
                )
                .with_for_update()
            )
            if row is None or row.status != "RUNNING":
                return
            row.status = "FAILED"
            row.error_code = error_code[:128]
            row.finished_at = now
            session.flush()

    def create_manual_edit(
        self, base: ProjectDescription, *, content: str, now: datetime
    ) -> ProjectDescription:
        with self._uow() as session:
            self._project_for_update(session, base.owner_id, base.project_id)
            session.execute(
                update(ProjectDescriptionRow)
                .where(
                    ProjectDescriptionRow.project_id == base.project_id,
                    ProjectDescriptionRow.owner_id == base.owner_id,
                    ProjectDescriptionRow.current.is_(True),
                )
                .values(current=False)
            )
            version_number = int(
                session.scalar(
                    select(func.coalesce(func.max(ProjectDescriptionRow.version_number), 0)).where(
                        ProjectDescriptionRow.project_id == base.project_id,
                        ProjectDescriptionRow.owner_id == base.owner_id,
                    )
                )
                or 0
            ) + 1
            row = ProjectDescriptionRow(
                project_id=base.project_id,
                owner_id=base.owner_id,
                version_number=version_number,
                source="manual",
                base_version_id=base.id,
                prompt_template_version=base.prompt_template_version,
                prompt_template_sha256=base.prompt_template_sha256,
                prompt_template_snapshot=base.prompt_template_snapshot,
                title_used=base.title_used,
                channel_name_used=base.channel_name_used,
                locale=base.locale,
                script_sha256=base.script_sha256,
                srt_asset_id=base.srt_asset_id,
                srt_sha256=base.srt_sha256,
                payload_json={},
                content=content,
                disclaimer_key=base.disclaimer_key,
                model=base.model,
                provider_receipt=base.provider_receipt,
                raw_response_sha256=base.raw_response_sha256,
                provider_calls=0,
                elapsed_ms=0,
                current=True,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return self._description(row)

    def select_current(
        self, owner_id: UUID, project_id: UUID, version_id: UUID
    ) -> ProjectDescription:
        with self._uow() as session:
            self._project_for_update(session, owner_id, project_id)
            row = session.scalar(
                select(ProjectDescriptionRow)
                .where(
                    ProjectDescriptionRow.id == version_id,
                    ProjectDescriptionRow.project_id == project_id,
                    ProjectDescriptionRow.owner_id == owner_id,
                )
                .with_for_update()
            )
            if row is None:
                raise DomainError("not_found", "Description version was not found")
            session.execute(
                update(ProjectDescriptionRow)
                .where(
                    ProjectDescriptionRow.project_id == project_id,
                    ProjectDescriptionRow.owner_id == owner_id,
                    ProjectDescriptionRow.id != version_id,
                    ProjectDescriptionRow.current.is_(True),
                )
                .values(current=False)
            )
            row.current = True
            session.flush()
            return self._description(row)

    @staticmethod
    def _channel_description_name(name: str | None, url: str) -> str:
        # 自定义频道名只用于简介订阅引导；剔除 #，避免系统注入破坏默认模板的禁 Hashtag 合同。
        cleaned = (name or "").replace("#", "").strip()
        return cleaned or url

    @staticmethod
    def _require_channel(session: Session, owner_id: UUID, channel_id: UUID) -> None:
        if (
            session.scalar(
                select(ChannelRow.id).where(
                    ChannelRow.id == channel_id, ChannelRow.owner_id == owner_id
                )
            )
            is None
        ):
            raise DomainError("not_found", "Channel was not found")

    @staticmethod
    def _counter_for_update(
        session: Session, channel_id: UUID
    ) -> ChannelDescriptionTemplateCounterRow | None:
        return session.scalar(
            select(ChannelDescriptionTemplateCounterRow)
            .where(ChannelDescriptionTemplateCounterRow.channel_id == channel_id)
            .with_for_update()
        )

    @staticmethod
    def _template_count(session: Session, channel_id: UUID) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ChannelDescriptionTemplateRow)
                .where(ChannelDescriptionTemplateRow.channel_id == channel_id)
            )
            or 0
        )

    @staticmethod
    def _template_for_update(
        session: Session, owner_id: UUID, channel_id: UUID, template_id: UUID
    ) -> ChannelDescriptionTemplateRow:
        row = session.scalar(
            select(ChannelDescriptionTemplateRow)
            .join(ChannelRow, ChannelRow.id == ChannelDescriptionTemplateRow.channel_id)
            .where(
                ChannelDescriptionTemplateRow.id == template_id,
                ChannelDescriptionTemplateRow.channel_id == channel_id,
                ChannelRow.owner_id == owner_id,
            )
            .with_for_update(of=ChannelDescriptionTemplateRow)
        )
        if row is None:
            raise DomainError("not_found", "Description template was not found")
        return row

    @staticmethod
    def _project_for_update(session: Session, owner_id: UUID, project_id: UUID) -> None:
        row = session.scalar(
            select(ProjectRow.id)
            .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            .with_for_update()
        )
        if row is None:
            raise DomainError("not_found", "Resource not found")

    @staticmethod
    def _attempt_for_update(
        session: Session, attempt_id: UUID, owner_id: UUID
    ) -> ProjectDescriptionAttemptRow:
        row = session.scalar(
            select(ProjectDescriptionAttemptRow)
            .where(
                ProjectDescriptionAttemptRow.id == attempt_id,
                ProjectDescriptionAttemptRow.owner_id == owner_id,
            )
            .with_for_update()
        )
        if row is None:
            raise DomainError("not_found", "Description generation attempt was not found")
        return row

    @staticmethod
    def _template(row: ChannelDescriptionTemplateRow) -> DescriptionTemplate:
        return DescriptionTemplate(
            id=row.id,
            channel_id=row.channel_id,
            version=row.version,
            prompt=row.prompt,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _attempt(row: ProjectDescriptionAttemptRow) -> DescriptionAttempt:
        return DescriptionAttempt(
            id=row.id,
            project_id=row.project_id,
            owner_id=row.owner_id,
            idempotency_key=row.idempotency_key,
            template_id=row.template_id,
            template_version=row.template_version,
            input_sha256=row.input_sha256,
            status=row.status,
            error_code=row.error_code,
            description_id=row.description_id,
            created_at=row.created_at,
            finished_at=row.finished_at,
        )

    @staticmethod
    def _description(row: ProjectDescriptionRow) -> ProjectDescription:
        return ProjectDescription(
            id=row.id,
            project_id=row.project_id,
            owner_id=row.owner_id,
            version_number=row.version_number,
            source=row.source,
            base_version_id=row.base_version_id,
            prompt_template_version=row.prompt_template_version,
            prompt_template_sha256=row.prompt_template_sha256,
            prompt_template_snapshot=row.prompt_template_snapshot,
            title_used=row.title_used,
            channel_name_used=row.channel_name_used,
            locale=row.locale,
            script_sha256=row.script_sha256,
            srt_asset_id=row.srt_asset_id,
            srt_sha256=row.srt_sha256,
            payload=dict(row.payload_json),
            content=row.content,
            disclaimer_key=row.disclaimer_key,
            model=row.model,
            provider_receipt=row.provider_receipt,
            raw_response_sha256=row.raw_response_sha256,
            provider_calls=row.provider_calls,
            elapsed_ms=row.elapsed_ms,
            current=row.current,
            created_at=row.created_at,
        )
