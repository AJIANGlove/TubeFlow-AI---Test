from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tubeflow_test.domain.errors import DomainError

from .models import CharacterImageRow, ProjectRow


class SqlProjectWriteGate:
    """Authoritative row-lock gate shared by every project-writing feature."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_writable(self, owner_id: UUID, project_id: UUID) -> ProjectRow:
        row = self._session.scalar(
            select(ProjectRow)
            .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            .with_for_update()
        )
        if row is None:
            raise DomainError("project_not_found", "Project was not found")
        if row.production_state != "ACTIVE":
            raise DomainError("project_purging", "Project is being permanently deleted")
        return row

    def freeze_for_purge(
        self,
        owner_id: UUID,
        project_id: UUID,
        *,
        locked_row: ProjectRow | None = None,
    ) -> ProjectRow:
        row = locked_row or self.lock_writable(owner_id, project_id)
        if (
            row.id != project_id
            or row.owner_id != owner_id
            or row.production_state != "ACTIVE"
        ):
            raise DomainError("project_purging", "Project is being permanently deleted")
        row.production_state = "PURGING"
        row.production_revision += 1
        self._session.flush()
        return row

    def exact_character_assets(
        self, owner_id: UUID, project_id: UUID
    ) -> tuple[tuple[UUID, str, int, str], ...]:
        row = self._session.scalar(
            select(ProjectRow)
            .where(ProjectRow.id == project_id, ProjectRow.owner_id == owner_id)
            .with_for_update()
        )
        if row is None:
            raise DomainError("project_not_found", "Project was not found")
        ids = tuple(row.snapshot_character_image_ids or row.character_image_ids)
        if not ids:
            return ()
        assets = self._session.scalars(
            select(CharacterImageRow).where(
                CharacterImageRow.owner_id == owner_id, CharacterImageRow.id.in_(ids)
            )
        )
        by_id = {asset.id: asset for asset in assets}
        return tuple(
            (
                by_id[item].asset_id,
                by_id[item].object_key,
                by_id[item].size_bytes,
                by_id[item].sha256,
            )
            for item in ids
            if item in by_id
        )
