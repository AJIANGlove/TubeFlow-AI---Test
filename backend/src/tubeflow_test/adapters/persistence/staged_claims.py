from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def lock_staged_claim_identity(
    session: Session,
    *,
    owner_id: UUID | None,
    ownership_scope: str,
    project_id: UUID | None,
    logical_object_key: str,
) -> None:
    """Serialize generation creation for one canonical staged identity."""
    if session.get_bind().dialect.name != "postgresql":
        return
    identity = json.dumps(
        [
            str(owner_id) if owner_id is not None else None,
            ownership_scope,
            str(project_id) if project_id is not None else None,
            logical_object_key,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(identity, 0))))
