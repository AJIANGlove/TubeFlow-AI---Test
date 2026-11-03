from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from tubeflow_test.adapters.object_storage import PrivateObjectStorage, StoredObject
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.staged_claims import lock_staged_claim_identity
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.ports import StagedObjectClaim, StoredImage

from .models import StagedObjectRow


class RequestStagedStorage:
    """Durably records objects before the business transaction may reference them."""

    def __init__(
        self,
        database: Database,
        storage: PrivateObjectStorage,
        *,
        ownership_scope: str = "GLOBAL",
        reclaim_matching_unattached: bool = False,
    ) -> None:
        self._database = database
        self._storage = storage
        self._ownership_scope = ownership_scope
        self._reclaim_matching_unattached = reclaim_matching_unattached
        self._claims: list[StagedObjectClaim] = []

    def put(
        self, body: bytes, media_type: str, *, owner_id: UUID | None = None
    ) -> StoredImage:
        key = str(uuid4())
        actual_key, generation, token = self._record_pending(
            key,
            owner_id=owner_id,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            media_type=media_type,
        )
        self._renew(actual_key, generation, token)
        stored = self._storage.put_private_at_key(actual_key, body, media_type)
        if not self._mark_staged(actual_key, generation, token):
            # A different writer may already have reused and attached this exact
            # immutable key. Only the canonical cleaner may delete after claiming
            # the journal and proving there are no authoritative references.
            raise DomainError("staged_object_claim_lost", "Upload was claimed for cleanup")
        claim = StagedObjectClaim(stored.key, generation, token)
        self._claims.append(claim)
        return _ClaimedStoredImage(stored, claim)

    def put_at_key(
        self,
        key: str,
        body: bytes,
        media_type: str,
        expected_sha256: str,
        *,
        owner_id: UUID | None = None,
    ) -> StoredImage:
        actual_key, generation, token = self._record_pending(
            key,
            owner_id=owner_id,
            sha256=expected_sha256,
            size_bytes=len(body),
            media_type=media_type,
        )
        self._renew(actual_key, generation, token)
        stored = self._storage.put_at_key(actual_key, body, media_type, expected_sha256)
        if not self._mark_staged(actual_key, generation, token):
            # Do not compensate here: created=True does not prove that another
            # request has not reused and attached the same content-addressed key.
            raise DomainError("staged_object_claim_lost", "Upload was claimed for cleanup")
        claim = StagedObjectClaim(stored.key, generation, token)
        self._claims.append(claim)
        return _ClaimedStoredImage(stored, claim)

    def _record_pending(
        self,
        key: str,
        *,
        owner_id: UUID | None = None,
        sha256: str,
        size_bytes: int,
        media_type: str,
    ) -> tuple[str, int, UUID]:
        scope = "OWNER" if owner_id is not None else self._ownership_scope
        token = uuid4()
        with self._database.unit_of_work() as session:
            lock_staged_claim_identity(
                session,
                owner_id=owner_id,
                ownership_scope=scope,
                project_id=None,
                logical_object_key=key,
            )
            existing = session.scalar(
                select(StagedObjectRow)
                .where(StagedObjectRow.logical_object_key == key)
                .order_by(
                    StagedObjectRow.generation.desc(),
                    StagedObjectRow.created_at.desc(),
                    StagedObjectRow.id.desc(),
                )
                .with_for_update()
                .limit(1)
            )
            if existing is None:
                session.add(
                    StagedObjectRow(
                        object_key=key,
                        logical_object_key=key,
                        created_at=datetime.now(UTC),
                        ownership_scope=scope,
                        owner_id=owner_id,
                        state="PENDING_UPLOAD",
                        generation=1,
                        execution_token=token,
                        lease_until=datetime.now(UTC) + timedelta(minutes=5),
                        sha256=sha256,
                        size_bytes=size_bytes,
                        media_type=media_type,
                    )
                )
                generation = 1
                actual_key = key
            elif (
                existing.ownership_scope != scope
                or existing.owner_id != owner_id
                or existing.project_id is not None
                or existing.sha256 != sha256
                or existing.size_bytes != size_bytes
                or existing.media_type != media_type
            ):
                raise DomainError("staged_object_claim_lost", "Upload journal facts conflict")
            elif self._reclaim_matching_unattached and existing.state in (
                "PENDING_UPLOAD",
                "STAGED",
            ):
                # Fixed-catalog callers hold the matching voice advisory lock for
                # the surrounding transaction. Reclaiming here therefore only
                # resumes a crashed writer; ordinary uploads retain strict claims.
                generation = existing.generation + 1
                actual_key = existing.object_key
                existing.state = "PENDING_UPLOAD"
                existing.generation = generation
                existing.execution_token = token
                existing.lease_until = datetime.now(UTC) + timedelta(minutes=5)
                existing.cleanup_after = None
                existing.cleanup_checked_at = None
                existing.cleanup_attempt = 0
            elif existing.state == "CLEANUP_PENDING" and existing.cleanup_checked_at is not None:
                generation = existing.generation + 1
                actual_key = f"{key}.generation-{generation}-{uuid4().hex}"
                session.add(
                    StagedObjectRow(
                        object_key=actual_key,
                        logical_object_key=key,
                        created_at=datetime.now(UTC),
                        ownership_scope=scope,
                        owner_id=owner_id,
                        state="PENDING_UPLOAD",
                        generation=generation,
                        execution_token=token,
                        lease_until=datetime.now(UTC) + timedelta(minutes=5),
                        sha256=sha256,
                        size_bytes=size_bytes,
                        media_type=media_type,
                    )
                )
            else:
                raise DomainError("staged_object_claim_lost", "Upload journal is already claimed")
        return actual_key, generation, token

    def _renew(self, key: str, generation: int, token: UUID) -> None:
        with self._database.unit_of_work() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StagedObjectRow)
                    .where(
                        StagedObjectRow.object_key == key,
                        StagedObjectRow.state == "PENDING_UPLOAD",
                        StagedObjectRow.generation == generation,
                        StagedObjectRow.execution_token == token,
                    )
                    .values(lease_until=datetime.now(UTC) + timedelta(minutes=5))
                ),
            )
            if not result.rowcount:
                raise DomainError("staged_object_claim_lost", "Upload was claimed for cleanup")

    def _mark_staged(self, key: str, generation: int, token: UUID) -> bool:
        with self._database.unit_of_work() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(StagedObjectRow)
                    .where(
                        StagedObjectRow.object_key == key,
                        StagedObjectRow.state == "PENDING_UPLOAD",
                        StagedObjectRow.generation == generation,
                        StagedObjectRow.execution_token == token,
                    )
                    .values(state="STAGED", lease_until=None)
                ),
            )
            return bool(result.rowcount)

    def get(self, key: str) -> bytes:
        return self._storage.get(key)

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        return self._storage.get_verified(key, expected_sha256, expected_size)

    def delete_exact(self, key: str) -> None:
        self._claims = [item for item in self._claims if item.key != key]
        with self._database.unit_of_work() as session:
            session.execute(
                update(StagedObjectRow)
                .where(
                    StagedObjectRow.object_key == key,
                    StagedObjectRow.state.in_(("PENDING_UPLOAD", "STAGED", "ATTACHED")),
                )
                .values(
                    state="CLEANUP_PENDING",
                    generation=StagedObjectRow.generation + 1,
                    execution_token=uuid4(),
                    lease_until=None,
                    cleanup_after=datetime.now(UTC),
                    cleanup_checked_at=None,
                    cleanup_attempt=0,
                )
            )

    def finalize(self) -> None:
        # Finalization is deliberately best-effort. The durable ATTACHED state is
        # committed with the business reference, so a crash here cannot orphan it.
        claims, self._claims = tuple(self._claims), []
        if claims:
            with self._database.unit_of_work() as session:
                for claim in claims:
                    session.execute(
                        update(StagedObjectRow)
                        .where(
                            StagedObjectRow.object_key == claim.key,
                            StagedObjectRow.state == "ATTACHED",
                            StagedObjectRow.generation == claim.generation,
                            StagedObjectRow.execution_token == claim.execution_token,
                        )
                        .values(state="FINALIZED", execution_token=None, lease_until=None)
                    )


class _ClaimedStoredImage:
    def __init__(self, stored: StoredObject, claim: StagedObjectClaim) -> None:
        self._stored = stored
        self.claim = claim

    @property
    def key(self) -> str:
        return self._stored.key

    @property
    def created(self) -> bool:
        return self._stored.created
