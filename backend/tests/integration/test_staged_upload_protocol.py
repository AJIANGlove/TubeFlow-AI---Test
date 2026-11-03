from __future__ import annotations

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import IntegrityError

from tubeflow_test.adapters.object_storage import StoredObject
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    ChannelRow,
    ProjectRow,
)
from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.adapters.persistence.identity_project.staged_objects import (
    RequestStagedStorage,
)
from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyCompositionRepository,
    SqlAlchemyDeletionRepository,
    reconcile_staged_objects,
)
from tubeflow_test.adapters.persistence.models import (
    CompositionJobRow,
    CompositionVersionRow,
    MediaAssetRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.narration import PrivateNarrationAudioArchive
from tubeflow_test.adapters.persistence.storyboards.asset_catalog import SqlAlchemyAssetCatalog
from tubeflow_test.application.media_authority import AuthoritativeCompositionInputPort
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.domain.errors import DomainError
from tubeflow_test.identity_project.ports import StagedObjectClaim


class ProtocolStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.on_put: Callable[[], None] | None = None
        self.on_delete: Callable[[], None] | None = None

    def put_exact(self, key: str, body: bytes, media_type: str) -> StoredObject:
        self.values[key] = body
        if self.on_put is not None:
            self.on_put()
        return StoredObject(key, hashlib.sha256(body).hexdigest(), len(body), media_type)

    def put_private_at_key(self, key: str, body: bytes, media_type: str) -> StoredObject:
        return self.put_at_key(key, body, media_type, hashlib.sha256(body).hexdigest())

    def put_at_key(
        self, key: str, body: bytes, media_type: str, expected_sha256: str
    ) -> StoredObject:
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        existing = self.values.get(key)
        if existing is not None:
            assert existing == body
            return StoredObject(key, expected_sha256, len(body), media_type, created=False)
        stored = self.put_exact(key, body, media_type)
        return StoredObject(
            stored.key, stored.sha256, stored.size_bytes, stored.media_type, created=True
        )

    def put(
        self, body: bytes, media_type: str, *, object_key: str | None = None
    ) -> StoredObject:
        assert object_key is not None
        return self.put_exact(object_key, body, media_type)

    def delete_exact(self, key: str) -> None:
        if self.on_delete is not None:
            callback, self.on_delete = self.on_delete, None
            callback()
        self.values.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.values

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        value = self.values[key]
        if len(value) != expected_size or hashlib.sha256(value).hexdigest() != expected_sha256:
            raise ValueError("object SHA-256 integrity mismatch")
        return value


@pytest.mark.integration
def test_postgres_advisory_claim_lock_prevents_same_generation_phantoms(
    postgres_database,
) -> None:
    database = Database(postgres_database.url)
    storage = ProtocolStorage()
    key = f"concurrent-logical-{uuid4()}"
    body = b"same logical candidate"
    digest = hashlib.sha256(body).hexdigest()
    with postgres_database.sessions.begin() as session:
        session.add(
            StagedObjectRow(
                object_key=key,
                logical_object_key=key,
                created_at=datetime.now(UTC),
                ownership_scope="GLOBAL",
                state="CLEANUP_PENDING",
                generation=1,
                execution_token=uuid4(),
                cleanup_checked_at=datetime.now(UTC),
                cleanup_after=datetime.now(UTC),
                sha256=digest,
                size_bytes=len(body),
                media_type="application/octet-stream",
            )
        )

    def claim() -> tuple[str, int] | None:
        writer = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
        try:
            actual_key, generation, _ = writer._record_pending(  # noqa: SLF001
                key,
                sha256=digest,
                size_bytes=len(body),
                media_type="application/octet-stream",
            )
        except DomainError as exc:
            assert exc.code == "staged_object_claim_lost"
            return None
        return actual_key, generation

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = tuple(executor.map(lambda _: claim(), range(32)))

    winners = tuple(result for result in results if result is not None)
    assert len(winners) == 1
    assert winners[0][1] == 2
    assert winners[0][0] != key
    with postgres_database.sessions() as session:
        rows = tuple(
            session.scalars(
                select(StagedObjectRow)
                .where(StagedObjectRow.logical_object_key == key)
                .order_by(
                    StagedObjectRow.generation,
                    StagedObjectRow.created_at,
                    StagedObjectRow.id,
                )
            )
        )
    assert [row.generation for row in rows] == [1, 2]
    assert len({(row.logical_object_key, row.generation) for row in rows}) == 2


@pytest.mark.integration
def test_fixed_catalog_writer_can_resume_matching_unattached_stage(postgres_database) -> None:
    database = Database(postgres_database.url)
    storage = ProtocolStorage()
    key = f"voice-catalog/v1/resume-{uuid4()}/preview.wav"
    body = b"fixed catalog preview"
    digest = hashlib.sha256(body).hexdigest()

    interrupted = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
    first = interrupted.put_at_key(key, body, "audio/wav", digest)
    with pytest.raises(DomainError, match="already claimed"):
        RequestStagedStorage(database, storage).put_at_key(  # type: ignore[arg-type]
            key, body, "audio/wav", digest
        )

    resumed = RequestStagedStorage(
        database,
        storage,  # type: ignore[arg-type]
        reclaim_matching_unattached=True,
    ).put_at_key(key, body, "audio/wav", digest)

    assert resumed.key == key
    assert resumed.created is False
    assert resumed.claim.generation == first.claim.generation + 1
    with postgres_database.sessions() as session:
        row = session.scalar(select(StagedObjectRow).where(StagedObjectRow.object_key == key))
        assert row is not None
        assert row.state == "STAGED"
        assert row.generation == resumed.claim.generation
        assert row.execution_token == resumed.claim.execution_token


@pytest.mark.integration
def test_identity_claim_handle_blocks_aba_and_refreshes_reopened_generation(
    postgres_database,
) -> None:
    database = Database(postgres_database.url)
    storage = ProtocolStorage()
    staged = RequestStagedStorage(database, storage)  # type: ignore[arg-type]
    key = f"identity-claim-{uuid4()}"
    body = b"identity-claim-body"
    digest = hashlib.sha256(body).hexdigest()
    try:
        first = staged.put_at_key(key, body, "application/octet-stream", digest)
        stale = StagedObjectClaim(
            first.claim.key, first.claim.generation, first.claim.execution_token
        )
        with database.unit_of_work() as session:
            SqlAlchemyIdentityProjectRepository(session).attach_staged_objects(
                (first.claim,), "identity-test"
            )
        staged.finalize()
        with postgres_database.sessions() as session:
            finalized = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == key)
            )
            assert finalized is not None and finalized.state == "FINALIZED"

        with postgres_database.sessions.begin() as session:
            finalized = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == key)
            )
            assert finalized is not None
            finalized.state = "CLEANUP_PENDING"
            finalized.generation += 1
            finalized.execution_token = uuid4()
            finalized.cleanup_checked_at = datetime.now(UTC)
            finalized.cleanup_after = datetime.now(UTC)
        storage.values[key] = b"late bytes from the old generation"
        reopened_values = []

        def reopen_during_old_delete() -> None:
            reopened_values.append(
                staged.put_at_key(key, body, "application/octet-stream", digest)
            )

        storage.on_delete = reopen_during_old_delete
        assert reconcile_staged_objects(
            postgres_database.sessions,
            storage,  # type: ignore[arg-type]
            older_than_seconds=-1,
        ) == 0
        reopened = reopened_values[0]
        assert reopened.claim.generation == stale.generation + 2
        assert reopened.key != key
        assert key not in storage.values and storage.values[reopened.key] == body
        with postgres_database.sessions() as session:
            old_tombstone = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == key)
            )
            current = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == reopened.key)
            )
            assert old_tombstone is not None and old_tombstone.state == "CLEANUP_PENDING"
            assert current is not None and current.state == "STAGED"
            assert current.logical_object_key == key
            assert current.generation == reopened.claim.generation
            assert current.execution_token == reopened.claim.execution_token

        with (
            pytest.raises(DomainError, match="no longer available"),
            database.unit_of_work() as session,
        ):
            SqlAlchemyIdentityProjectRepository(session).attach_staged_objects(
                (stale,), "identity-test"
            )
        assert reconcile_staged_objects(
            postgres_database.sessions,
            storage,  # type: ignore[arg-type]
            older_than_seconds=3600,
        ) == 0

        with database.unit_of_work() as session:
            SqlAlchemyIdentityProjectRepository(session).attach_staged_objects(
                (reopened.claim,), "identity-test"
            )
        staged.finalize()
        with postgres_database.sessions() as session:
            old_tombstone = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == key)
            )
            current = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == reopened.key)
            )
            assert old_tombstone is not None and old_tombstone.state == "CLEANUP_PENDING"
            assert current is not None and current.state == "FINALIZED"
            assert current.execution_token is None

        owner_key = f"identity-owner-{uuid4()}"
        owner_id, other_owner_id = uuid4(), uuid4()
        with postgres_database.sessions.begin() as session:
            for account_id in (owner_id, other_owner_id):
                session.add(
                    AccountRow(
                        id=account_id,
                        username=f"identity-{account_id.hex}",
                        password_hash="test",
                        role="USER",
                        active=True,
                        session_generation=1,
                    )
                )
        owner_staged = RequestStagedStorage(
            database, storage, ownership_scope="OWNER"
        )  # type: ignore[arg-type]
        claimed = owner_staged.put_at_key(
            owner_key, body, "application/octet-stream", digest, owner_id=owner_id
        )
        with postgres_database.sessions.begin() as session:
            row = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == owner_key)
            )
            assert row is not None
            row.state = "CLEANUP_PENDING"
            row.generation += 1
            row.execution_token = uuid4()
            row.cleanup_checked_at = datetime.now(UTC)
        storage.values.pop(owner_key)
        with pytest.raises(DomainError, match="facts conflict"):
            owner_staged.put_at_key(
                owner_key,
                body,
                "application/octet-stream",
                digest,
                owner_id=other_owner_id,
            )
        same_owner = owner_staged.put_at_key(
            owner_key, body, "application/octet-stream", digest, owner_id=owner_id
        )
        assert same_owner.claim.generation == claimed.claim.generation + 2
        assert same_owner.key != owner_key
    finally:
        database.engine.dispose()


@pytest.mark.integration
def test_postgres_staged_rows_enforce_created_at_and_scope_check(
    postgres_database, production_parent_chain
) -> None:
    values = {
        "id": uuid4(),
        "key": f"scope-check-{uuid4()}",
        "owner": production_parent_chain.owner_id,
        "project": production_parent_chain.project_id,
    }
    with pytest.raises(IntegrityError), postgres_database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO staged_objects "
                "(id,object_key,owner_id,project_id,ownership_scope,state,generation) "
                "VALUES (:id,:key,:owner,:project,'PROJECT','PENDING_UPLOAD',1)"
            ),
            values,
        )
    values["id"], values["key"] = uuid4(), f"scope-check-{uuid4()}"
    with pytest.raises(IntegrityError), postgres_database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO staged_objects "
                "(id,object_key,owner_id,project_id,ownership_scope,state,generation,created_at) "
                "VALUES (:id,:key,:owner,:project,'GLOBAL','PENDING_UPLOAD',1,:created)"
            ),
            {**values, "created": datetime.now(UTC)},
        )


@pytest.mark.integration
def test_narration_cleaner_claim_prevents_late_completion_and_media_reference(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    storage = ProtocolStorage()
    def expire_and_clean() -> None:
        with sessions.begin() as session:
            session.execute(update(StagedObjectRow).values(lease_until=datetime.now(UTC)))
        reconcile_staged_objects(sessions, storage, older_than_seconds=-1)  # type: ignore[arg-type]

    storage.on_put = expire_and_clean
    archive = PrivateNarrationAudioArchive(storage, sessions)  # type: ignore[arg-type]

    with pytest.raises(DomainError) as lost:
        archive.save(
            owner_id=production_parent_chain.owner_id,
            project_id=production_parent_chain.project_id,
            kind="narration_provider_zip",
            payload=b"provider evidence",
            metadata={
                "provider_request_key": "cleaner-claim-interleave",
                "manifest": ["0001-cleaner-claim.wav"],
            },
        )
    assert lost.value.code == "archive_staging_lost"

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(MediaAssetRow)) == 0
        tombstone = session.scalar(select(StagedObjectRow))
        assert tombstone is not None and tombstone.state == "CLEANUP_PENDING"
    assert storage.values == {}


@pytest.mark.integration
def test_storyboard_reservation_is_not_a_reference_and_cleaner_blocks_attach(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    catalog = SqlAlchemyAssetCatalog(sessions.begin)
    asset = MediaAssetRef(uuid4(), "a" * 64, "image/png", 123)
    reservation = catalog.prepare_storyboard_upload(
        production_parent_chain.owner_id,
        production_parent_chain.project_id,
        f"storyboard-{uuid4()}",
        asset,
    )
    with sessions() as session:
        assert session.get(MediaAssetRow, asset.asset_id) is None

    storage = ProtocolStorage()

    def late_writer_attach() -> None:
        with pytest.raises(DomainError, match="claim was lost"):
            catalog.mark_storyboard_uploaded(
                production_parent_chain.owner_id,
                production_parent_chain.project_id,
                reservation,
            )

    storage.on_delete = late_writer_attach
    with sessions.begin() as session:
        journal = session.get_one(StagedObjectRow, reservation.journal_id)
        journal.lease_until = datetime.now(UTC)
    assert (
        reconcile_staged_objects(
            sessions,
            storage,
            older_than_seconds=-1,  # type: ignore[arg-type]
        )
        == 1
    )
    with pytest.raises(DomainError):
        catalog.finalize_storyboard_upload(
            production_parent_chain.owner_id,
            production_parent_chain.project_id,
            reservation,
        )
    with sessions() as session:
        assert session.get(MediaAssetRow, asset.asset_id) is None
        journal = session.get(StagedObjectRow, reservation.journal_id)
        assert journal is not None and journal.state == "CLEANUP_PENDING"


@pytest.mark.integration
def test_storyboard_success_cas_atomically_creates_media_and_reference(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    catalog = SqlAlchemyAssetCatalog(sessions.begin)
    asset = MediaAssetRef(uuid4(), "b" * 64, "image/png", 321)
    reservation = catalog.prepare_storyboard_upload(
        production_parent_chain.owner_id,
        production_parent_chain.project_id,
        f"storyboard-{uuid4()}",
        asset,
    )
    catalog.mark_storyboard_uploaded(
        production_parent_chain.owner_id,
        production_parent_chain.project_id,
        reservation,
    )
    assert (
        catalog.finalize_storyboard_upload(
            production_parent_chain.owner_id,
            production_parent_chain.project_id,
            reservation,
        )
        == asset
    )
    with sessions() as session:
        media = session.get(MediaAssetRow, asset.asset_id)
        journal = session.get(StagedObjectRow, reservation.journal_id)
        assert media is not None and media.object_key == reservation.object_key
        assert media.project_id is None and media.ownership_scope == "OWNER"
        assert journal is not None and journal.state == "FINALIZED"


@pytest.mark.integration
def test_owner_storyboard_tombstone_reopens_across_projects_but_rejects_other_owner(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    second_project_id, other_owner_id, other_channel_id, other_project_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    with sessions.begin() as session:
        source = session.get_one(ProjectRow, production_parent_chain.project_id)
        session.add(
            ProjectRow(
                id=second_project_id,
                owner_id=source.owner_id,
                channel_id=source.channel_id,
                title="Same owner retry",
                script="same owner retry",
                character_image_ids=source.character_image_ids,
                visual_style=source.visual_style,
                voice_version_id=source.voice_version_id,
                locale=source.locale,
                aspect_ratio=source.aspect_ratio,
                draft_revision=1,
                production_state="ACTIVE",
                production_revision=1,
            )
        )
        session.add(
            AccountRow(
                id=other_owner_id,
                username=f"other-{other_owner_id.hex}",
                password_hash="test",
                role="USER",
                active=True,
                session_generation=1,
            )
        )
        session.flush()
        session.add(ChannelRow(id=other_channel_id, owner_id=other_owner_id, url="https://other"))
        session.add(
            ProjectRow(
                id=other_project_id,
                owner_id=other_owner_id,
                channel_id=other_channel_id,
                title="Other owner",
                script="other owner",
                character_image_ids=[],
                visual_style="documentary",
                voice_version_id=source.voice_version_id,
                locale="en-US",
                aspect_ratio="16:9",
                draft_revision=1,
                production_state="ACTIVE",
                production_revision=1,
            )
        )
    catalog = SqlAlchemyAssetCatalog(sessions.begin)
    asset = MediaAssetRef(uuid4(), "c" * 64, "image/png", 456)
    key = f"owner-storyboard-{uuid4()}"
    first = catalog.prepare_storyboard_upload(
        production_parent_chain.owner_id, production_parent_chain.project_id, key, asset
    )
    with sessions.begin() as session:
        journal = session.get_one(StagedObjectRow, first.journal_id)
        assert journal.ownership_scope == "OWNER" and journal.project_id is None
        journal.lease_until = datetime.now(UTC)
    assert reconcile_staged_objects(sessions, ProtocolStorage(), older_than_seconds=-1) == 1  # type: ignore[arg-type]

    reopened = catalog.prepare_storyboard_upload(
        production_parent_chain.owner_id, second_project_id, key, asset
    )
    assert reopened.journal_id != first.journal_id
    assert reopened.object_key != first.object_key
    assert reopened.generation > first.generation
    with pytest.raises(DomainError, match="owner or facts conflict"):
        catalog.prepare_storyboard_upload(other_owner_id, other_project_id, key, asset)
    catalog.mark_storyboard_uploaded(
        production_parent_chain.owner_id, second_project_id, reopened
    )
    assert catalog.finalize_storyboard_upload(
        production_parent_chain.owner_id, second_project_id, reopened
    ) == asset
    reused = catalog.prepare_storyboard_upload(
        production_parent_chain.owner_id,
        production_parent_chain.project_id,
        key,
        asset,
    )
    assert reused.reused and reused.object_key == reopened.object_key


@pytest.mark.integration
def test_project_takeover_anonymizes_every_state_and_retains_late_put_evidence(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    keys = {state: f"takeover-{state.lower()}-{uuid4()}" for state in (
        "PENDING_UPLOAD", "STAGED", "ATTACHED", "FINALIZED", "CLEANUP_PENDING"
    )}
    owner_key = f"takeover-owner-{uuid4()}"
    with sessions.begin() as session:
        for state, key in keys.items():
            session.add(
                StagedObjectRow(
                    object_key=key,
                    owner_id=production_parent_chain.owner_id,
                    project_id=production_parent_chain.project_id,
                    ownership_scope="PROJECT",
                    purpose="sensitive-purpose",
                    kind="sensitive-kind",
                    state=state,
                    sha256="d" * 64,
                    size_bytes=99,
                    media_type="video/mp4",
                    generation=4,
                    execution_token=uuid4(),
                    asset_id=uuid4(),
                    reference_type="sensitive-reference",
                    created_at=datetime.now(UTC),
                )
            )
        session.add(
            StagedObjectRow(
                object_key=owner_key,
                owner_id=production_parent_chain.owner_id,
                project_id=None,
                ownership_scope="OWNER",
                state="CLEANUP_PENDING",
                generation=1,
                execution_token=uuid4(),
                created_at=datetime.now(UTC),
            )
        )
    gates = tuple(type("Gate", (), {"name": name})() for name in (
        "narration", "storyboard", "h3", "composition"
    ))
    repository = SqlAlchemyDeletionRepository(sessions, gates)  # type: ignore[arg-type]
    assert repository.take_over_staged_objects(
        production_parent_chain.owner_id, production_parent_chain.project_id
    ) == len(keys)
    with sessions() as session:
        for key in keys.values():
            row = session.scalar(select(StagedObjectRow).where(StagedObjectRow.object_key == key))
            assert row is not None
            assert row.state == "CLEANUP_PENDING" and row.purpose == "PROJECT_DELETION"
            assert row.ownership_scope == "GLOBAL"
            assert row.owner_id is None and row.project_id is None and row.asset_id is None
            assert row.sha256 is None and row.size_bytes is None and row.media_type is None
            assert row.kind is None and row.reference_type is None
        owner_row = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == owner_key)
        )
        assert owner_row is not None and owner_row.ownership_scope == "OWNER"

    storage = ProtocolStorage()
    late_key = keys["PENDING_UPLOAD"]
    assert reconcile_staged_objects(sessions, storage, older_than_seconds=-1) >= 1  # type: ignore[arg-type]
    storage.values[late_key] = b"late project bytes"
    with sessions.begin() as session:
        row = session.scalar(select(StagedObjectRow).where(StagedObjectRow.object_key == late_key))
        assert row is not None
        row.cleanup_after = datetime.now(UTC)
    reconcile_staged_objects(sessions, storage, older_than_seconds=-1)  # type: ignore[arg-type]
    assert late_key not in storage.values
    with sessions() as session:
        row = session.scalar(select(StagedObjectRow).where(StagedObjectRow.object_key == late_key))
        assert row is not None and row.state == "CLEANUP_PENDING"
@pytest.mark.integration
def test_storyboard_direct_registration_creates_owner_shared_media(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    catalog = SqlAlchemyAssetCatalog(sessions.begin)
    asset = MediaAssetRef(uuid4(), "c" * 64, "image/png", 456)

    assert catalog.register_storyboard(
        production_parent_chain.owner_id,
        production_parent_chain.project_id,
        f"storyboard-register-{uuid4()}",
        asset,
    ) == asset

    with sessions() as session:
        media = session.get_one(MediaAssetRow, asset.asset_id)
        assert media.shared is True
        assert media.owner_id == production_parent_chain.owner_id
        assert media.project_id is None
        assert media.ownership_scope == "OWNER"


@pytest.mark.integration
def test_project_deletion_survivor_reference_never_finalizes_cleanup_tombstone(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    storage = ProtocolStorage()
    key = f"project-survivor-{uuid4()}"
    body = b"shared survivor"
    asset_id = uuid4()
    with sessions.begin() as session:
        session.add(
            StagedObjectRow(
                object_key=key,
                ownership_scope="GLOBAL",
                purpose="PROJECT_DELETION",
                state="CLEANUP_PENDING",
                generation=7,
                execution_token=uuid4(),
                cleanup_after=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            MediaAssetRow(
                id=asset_id,
                owner_id=production_parent_chain.owner_id,
                project_id=None,
                ownership_scope="GLOBAL",
                kind="STORYBOARD_GRID",
                object_key=key,
                sha256=hashlib.sha256(body).hexdigest(),
                media_type="image/png",
                size_bytes=len(body),
                metadata_json={},
                shared=True,
            )
        )
    storage.values[key] = body

    assert reconcile_staged_objects(sessions, storage, older_than_seconds=-1) == 0  # type: ignore[arg-type]
    with sessions() as session:
        tombstone = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == key)
        )
        assert tombstone is not None
        assert tombstone.state == "CLEANUP_PENDING"
        assert tombstone.ownership_scope == "GLOBAL"
        token, generation = tombstone.execution_token, tombstone.generation
    assert storage.values[key] == body

    with sessions.begin() as session:
        session.execute(text("DELETE FROM media_assets WHERE id=:id"), {"id": asset_id})
        tombstone = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == key)
        )
        assert tombstone is not None
        tombstone.cleanup_after = datetime.now(UTC)
    storage.values.pop(key)
    reconcile_staged_objects(sessions, storage, older_than_seconds=-1)  # type: ignore[arg-type]
    storage.values[key] = b"indefinitely late bytes"
    with sessions.begin() as session:
        tombstone = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == key)
        )
        assert tombstone is not None
        assert tombstone.execution_token == token
        assert tombstone.generation == generation
        tombstone.cleanup_after = datetime.now(UTC)
    reconcile_staged_objects(sessions, storage, older_than_seconds=-1)  # type: ignore[arg-type]
    assert key not in storage.values
    with sessions() as session:
        tombstone = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == key)
        )
        assert tombstone is not None and tombstone.state == "CLEANUP_PENDING"


@pytest.mark.integration
def test_project_deletion_final_check_reference_race_keeps_tombstone(
    postgres_database, production_parent_chain
) -> None:
    sessions = postgres_database.sessions
    storage = ProtocolStorage()
    key = f"project-final-check-{uuid4()}"
    body = b"delete-race"
    with sessions.begin() as session:
        session.add(
            StagedObjectRow(
                object_key=key,
                ownership_scope="GLOBAL",
                purpose="PROJECT_DELETION",
                state="CLEANUP_PENDING",
                generation=3,
                execution_token=uuid4(),
                cleanup_after=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
    storage.values[key] = body

    def add_reference_after_delete() -> None:
        with sessions.begin() as session:
            session.add(
                MediaAssetRow(
                    id=uuid4(),
                    owner_id=production_parent_chain.owner_id,
                    project_id=None,
                    ownership_scope="GLOBAL",
                    kind="STORYBOARD_GRID",
                    object_key=key,
                    sha256=hashlib.sha256(body).hexdigest(),
                    media_type="image/png",
                    size_bytes=len(body),
                    metadata_json={},
                    shared=True,
                )
            )

    storage.on_delete = add_reference_after_delete
    reconcile_staged_objects(sessions, storage, older_than_seconds=-1)  # type: ignore[arg-type]
    with sessions() as session:
        tombstone = session.scalar(
            select(StagedObjectRow).where(StagedObjectRow.object_key == key)
        )
        assert tombstone is not None
        assert tombstone.state == "CLEANUP_PENDING"
        assert tombstone.ownership_scope == "GLOBAL"


@pytest.mark.integration
def test_composition_commit_response_loss_never_deletes_committed_assets(
    postgres_database, production_parent_chain, tmp_path: Path
) -> None:
    sessions = postgres_database.sessions
    token = uuid4()
    with sessions.begin() as session:
        session.add(
            CompositionJobRow(
                id=uuid4(),
                project_id=production_parent_chain.project_id,
                owner_id=production_parent_chain.owner_id,
                idempotency_key="commit-response-loss",
                state="RUNNING",
                attempt=1,
                execution_token=token,
            )
        )
    storage = ProtocolStorage()
    repository = SqlAlchemyCompositionRepository(
        sessions,
        storage,  # type: ignore[arg-type]
        cast(AuthoritativeCompositionInputPort, object()),
    )
    video = tmp_path / "candidate.mp4"
    video.write_bytes(b"video-candidate")
    commits = 0

    def lose_final_commit_response(_session: object) -> None:
        nonlocal commits
        commits += 1
        if commits == 3:
            raise RuntimeError("simulated response loss after server commit")

    event.listen(sessions.class_, "after_commit", lose_final_commit_response)
    try:
        result = repository.activate_candidate(
            production_parent_chain.project_id,
            production_parent_chain.owner_id,
            video,
            b"1\n00:00:00,000 --> 00:00:01,000\ncaption\n",
            {"source": "commit-response-loss"},
            "commit-response-loss",
            token,
        )
    finally:
        event.remove(sessions.class_, "after_commit", lose_final_commit_response)

    assert len(storage.values) == 2
    with sessions() as session:
        assert session.get(CompositionVersionRow, result.id) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(StagedObjectRow)
                .where(StagedObjectRow.state == "FINALIZED")
            )
            == 2
        )
