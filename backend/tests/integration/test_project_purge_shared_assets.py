from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from uuid import UUID, uuid4

import pytest
from conftest import PostgresDatabase, ProductionParentChain
from minio import Minio
from object_store_guard import refuse_application_object_store
from sqlalchemy import event, func, select

from tubeflow_test.adapters.object_storage import PrivateObjectStorage
from tubeflow_test.adapters.persistence.identity_project.models import (
    AccountRow,
    ProjectRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.identity_project.write_gate import SqlProjectWriteGate
from tubeflow_test.adapters.persistence.media_repository import (
    SqlAlchemyDeletionRepository,
    reconcile_staged_objects,
)
from tubeflow_test.adapters.persistence.models import (
    DeletionTombstoneRow,
    MediaAssetRow,
    ProjectAssetReferenceRow,
    ProjectPurgeJobRow,
    ProjectPurgeObjectRow,
    PurgeOutboxRow,
)
from tubeflow_test.application.deletion import ProjectDeletionService
from tubeflow_test.domain.errors import DomainError


@dataclass(frozen=True)
class IdleGate:
    name: str

    def has_active(self, session: object, project_id: UUID) -> bool:
        return False


@dataclass(frozen=True)
class ActiveGate:
    name: str = "active"

    def has_active(self, session: object, project_id: UUID) -> bool:
        return True


class CountingStorage:
    def __init__(self, storage: PrivateObjectStorage) -> None:
        self.storage = storage
        self.calls: list[str] = []
        self._lock = Lock()

    def delete_exact(self, object_key: str) -> None:
        with self._lock:
            self.calls.append(object_key)
        self.storage.delete_exact(object_key)


def _repository(database: PostgresDatabase) -> SqlAlchemyDeletionRepository:
    return SqlAlchemyDeletionRepository(
        database.sessions,
        tuple(IdleGate(name) for name in ("narration", "storyboard", "h3", "composition")),
    )


def _clone_project(session, source: ProjectRow, project_id: UUID) -> None:
    session.add(
        ProjectRow(
            id=project_id,
            owner_id=source.owner_id,
            channel_id=source.channel_id,
            title="Shared purge peer",
            script="shared purge peer",
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


@pytest.mark.integration
def test_begin_active_gate_rolls_back_project_freeze_and_purge_rows(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    ids = production_parent_chain
    repository = SqlAlchemyDeletionRepository(
        postgres_database.sessions,
        (ActiveGate("narration"),)
        + tuple(IdleGate(name) for name in ("storyboard", "h3", "composition")),
    )

    assert not repository.begin_with_lock_and_outbox(ids.project_id, ids.owner_id)
    with postgres_database.sessions() as session:
        project = session.get_one(ProjectRow, ids.project_id)
        assert project.production_state == "ACTIVE"
        assert project.production_revision == 1
        assert session.get(ProjectPurgeJobRow, ids.project_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(PurgeOutboxRow)
                .where(PurgeOutboxRow.project_id == ids.project_id)
            )
            == 0
        )


@pytest.mark.integration
def test_begin_flush_failure_rolls_back_project_freeze_and_purge_rows(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    ids = production_parent_chain

    def fail_after_freeze(session, flush_context, instances) -> None:
        if any(isinstance(row, ProjectPurgeJobRow) for row in session.new):
            raise RuntimeError("injected purge flush failure")

    event.listen(postgres_database.sessions.class_, "before_flush", fail_after_freeze)
    try:
        with pytest.raises(RuntimeError, match="injected purge flush failure"):
            _repository(postgres_database).begin_with_lock_and_outbox(
                ids.project_id, ids.owner_id
            )
    finally:
        event.remove(postgres_database.sessions.class_, "before_flush", fail_after_freeze)

    with postgres_database.sessions() as session:
        project = session.get_one(ProjectRow, ids.project_id)
        assert project.production_state == "ACTIVE"
        assert project.production_revision == 1
        assert session.get(ProjectPurgeJobRow, ids.project_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(PurgeOutboxRow)
                .where(PurgeOutboxRow.project_id == ids.project_id)
            )
            == 0
        )


@pytest.mark.integration
def test_concurrent_project_purges_elect_one_shared_object_deleter(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    counting = CountingStorage(storage)
    ids = production_parent_chain
    peer_id, asset_id = uuid4(), uuid4()
    key, body = f"shared/{asset_id}.png", b"shared-purge"
    digest = hashlib.sha256(body).hexdigest()
    storage.put_at_key(key, body, "image/png", digest)
    try:
        with postgres_database.sessions.begin() as session:
            source = session.get_one(ProjectRow, ids.project_id)
            _clone_project(session, source, peer_id)
            session.flush()
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=None,
                    ownership_scope="OWNER",
                    kind="STORYBOARD_GRID",
                    object_key=key,
                    sha256=digest,
                    media_type="image/png",
                    size_bytes=len(body),
                    metadata_json={},
                    shared=True,
                )
            )
            session.add_all(
                ProjectAssetReferenceRow(
                    project_id=project, asset_id=asset_id, owner_id=ids.owner_id
                )
                for project in (ids.project_id, peer_id)
            )
        repository = _repository(postgres_database)
        service = ProjectDeletionService(repository, counting)
        service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
        service.request(peer_id, ids.owner_id, confirmation="Shared purge peer")
        with postgres_database.sessions() as session:
            assert session.get_one(ProjectRow, ids.project_id).production_state == "PURGING"
            assert session.get_one(ProjectRow, ids.project_id).production_revision == 2
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ProjectPurgeObjectRow)
                    .where(ProjectPurgeObjectRow.asset_id == asset_id)
                )
                == 2
            )
            with pytest.raises(DomainError, match="permanently deleted"):
                SqlProjectWriteGate(session).lock_writable(ids.owner_id, ids.project_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(service.clean, project, ids.owner_id)
                for project in (ids.project_id, peer_id)
            )
            for future in futures:
                future.result()
        with postgres_database.sessions() as session:
            assert session.get(MediaAssetRow, asset_id) is None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ProjectAssetReferenceRow)
                    .where(ProjectAssetReferenceRow.asset_id == asset_id)
                )
                == 0
            )
        assert counting.calls.count(key) == 1
        assert not storage.exists(key)
    finally:
        if storage.exists(key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
@pytest.mark.parametrize("ownership_scope", ("OWNER", "GLOBAL"))
def test_non_project_shared_survivor_remains_unchanged_when_one_project_is_deleted(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
    ownership_scope: str,
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-owner-survivor-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    ids, peer_id, asset_id = production_parent_chain, uuid4(), uuid4()
    key, body = f"owner-shared/{asset_id}.png", b"owner-survivor"
    digest = hashlib.sha256(body).hexdigest()
    storage.put_at_key(key, body, "image/png", digest)
    try:
        with postgres_database.sessions.begin() as session:
            source = session.get_one(ProjectRow, ids.project_id)
            _clone_project(session, source, peer_id)
            session.flush()
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=None,
                    ownership_scope=ownership_scope,
                    kind="STORYBOARD_GRID",
                    object_key=key,
                    sha256=digest,
                    media_type="image/png",
                    size_bytes=len(body),
                    metadata_json={"immutable": ownership_scope},
                    shared=True,
                )
            )
            session.add_all(
                ProjectAssetReferenceRow(
                    project_id=project, asset_id=asset_id, owner_id=ids.owner_id
                )
                for project in (ids.project_id, peer_id)
            )
        service = ProjectDeletionService(_repository(postgres_database), storage)
        service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
        service.clean(ids.project_id, ids.owner_id)
        with postgres_database.sessions() as session:
            survivor = session.get_one(MediaAssetRow, asset_id)
            assert survivor.ownership_scope == ownership_scope
            assert survivor.project_id is None and survivor.owner_id == ids.owner_id
            assert survivor.object_key == key and survivor.sha256 == digest
            assert survivor.metadata_json == {"immutable": ownership_scope}
            assert survivor.shared and not survivor.purging
            assert session.get(ProjectAssetReferenceRow, (peer_id, asset_id)) is not None
        assert storage.get_verified(key, digest, len(body)) == body
    finally:
        if storage.exists(key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_global_shared_survivor_is_rechecked_and_preserved_after_retry(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-global-retry-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    counting = CountingStorage(storage)
    ids, peer_id, asset_id = production_parent_chain, uuid4(), uuid4()
    key, body = f"global-retry/{asset_id}.png", b"global-retry"
    digest = hashlib.sha256(body).hexdigest()
    storage.put_at_key(key, body, "image/png", digest)
    try:
        with postgres_database.sessions.begin() as session:
            source = session.get_one(ProjectRow, ids.project_id)
            _clone_project(session, source, peer_id)
            session.flush()
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=None,
                    ownership_scope="GLOBAL",
                    kind="STORYBOARD_GRID",
                    object_key=key,
                    sha256=digest,
                    media_type="image/png",
                    size_bytes=len(body),
                    metadata_json={"immutable": "retry"},
                    shared=True,
                )
            )
            session.add_all(
                ProjectAssetReferenceRow(
                    project_id=project, asset_id=asset_id, owner_id=ids.owner_id
                )
                for project in (ids.project_id, peer_id)
            )
        base = _repository(postgres_database)
        expired = False

        class ExpireAfterFirstSave:
            def __getattr__(self, name: str):
                return getattr(base, name)

            def save_results(self, claim, results):
                nonlocal expired
                base.save_results(claim, results)
                if not expired:
                    expired = True
                    with postgres_database.sessions.begin() as session:
                        job = session.get_one(ProjectPurgeJobRow, ids.project_id)
                        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)

        ProjectDeletionService(base, counting).request(
            ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project"
        )
        with pytest.raises(DomainError, match="no longer active"):
            ProjectDeletionService(ExpireAfterFirstSave(), counting).clean(  # type: ignore[arg-type]
                ids.project_id, ids.owner_id
            )
        base.reenqueue(ids.project_id, ids.owner_id)
        ProjectDeletionService(base, counting).clean(ids.project_id, ids.owner_id)

        assert counting.calls == []
        assert storage.get_verified(key, digest, len(body)) == body
        with postgres_database.sessions() as session:
            survivor = session.get_one(MediaAssetRow, asset_id)
            assert survivor.ownership_scope == "GLOBAL"
            assert survivor.owner_id == ids.owner_id and survivor.project_id is None
            assert survivor.object_key == key and survivor.sha256 == digest
            assert survivor.metadata_json == {"immutable": "retry"}
            assert session.get(ProjectAssetReferenceRow, (peer_id, asset_id)) is not None
    finally:
        if storage.exists(key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_concurrent_global_shared_purges_preserve_external_survivor_idempotently(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-global-concurrent-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    counting = CountingStorage(storage)
    ids, peer_id, survivor_id, asset_id = production_parent_chain, uuid4(), uuid4(), uuid4()
    key, body = f"global-concurrent/{asset_id}.png", b"global-concurrent"
    digest = hashlib.sha256(body).hexdigest()
    storage.put_at_key(key, body, "image/png", digest)
    try:
        with postgres_database.sessions.begin() as session:
            source = session.get_one(ProjectRow, ids.project_id)
            _clone_project(session, source, peer_id)
            _clone_project(session, source, survivor_id)
            session.flush()
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=None,
                    ownership_scope="GLOBAL",
                    kind="STORYBOARD_GRID",
                    object_key=key,
                    sha256=digest,
                    media_type="image/png",
                    size_bytes=len(body),
                    metadata_json={"immutable": "concurrent"},
                    shared=True,
                )
            )
            session.add_all(
                ProjectAssetReferenceRow(
                    project_id=project, asset_id=asset_id, owner_id=ids.owner_id
                )
                for project in (ids.project_id, peer_id, survivor_id)
            )
        service = ProjectDeletionService(_repository(postgres_database), counting)
        for project in (ids.project_id, peer_id):
            title = (
                "PostgreSQL fixture project" if project == ids.project_id else "Shared purge peer"
            )
            service.request(project, ids.owner_id, confirmation=title)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(service.clean, project, ids.owner_id)
                for project in (ids.project_id, peer_id)
            )
            for future in futures:
                future.result()

        assert counting.calls == []
        assert storage.get_verified(key, digest, len(body)) == body
        with postgres_database.sessions() as session:
            survivor = session.get_one(MediaAssetRow, asset_id)
            assert survivor.ownership_scope == "GLOBAL"
            assert survivor.owner_id == ids.owner_id and survivor.project_id is None
            assert survivor.object_key == key and survivor.sha256 == digest
            assert survivor.metadata_json == {"immutable": "concurrent"}
            assert session.get(ProjectAssetReferenceRow, (survivor_id, asset_id)) is not None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ProjectAssetReferenceRow)
                    .where(ProjectAssetReferenceRow.asset_id == asset_id)
                )
                == 1
            )
    finally:
        if storage.exists(key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_replace_media_then_peer_begin_project_lock_order_does_not_deadlock(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-lock-order-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    counting = CountingStorage(storage)
    ids, peer_id = production_parent_chain, uuid4()
    asset_ids = (uuid4(), uuid4())
    objects = tuple(
        (asset_id, f"global-lock-order/{asset_id}.bin", f"body-{index}".encode())
        for index, asset_id in enumerate(reversed(sorted(asset_ids, key=str)))
    )
    for _, key, body in objects:
        storage.put_at_key(key, body, "application/octet-stream", hashlib.sha256(body).hexdigest())
    replace_holds_media, peer_holds_project, release_replace = Event(), Event(), Event()

    @dataclass(frozen=True)
    class PeerProjectBarrierGate:
        name: str

        def has_active(self, session: object, project_id: UUID) -> bool:
            if project_id == peer_id:
                peer_holds_project.set()
            return False

    def pause_external_reference_lock(
        connection, cursor, statement, parameters, context, executemany
    ) -> None:
        normalized = statement.lower()
        if (
            "join projects" in normalized
            and "for update of project_asset_references" in normalized
            and not replace_holds_media.is_set()
        ):
            replace_holds_media.set()
            assert release_replace.wait(10)

    event.listen(
        postgres_database.engine, "before_cursor_execute", pause_external_reference_lock
    )
    try:
        with postgres_database.sessions.begin() as session:
            source = session.get_one(ProjectRow, ids.project_id)
            _clone_project(session, source, peer_id)
            session.flush()
            for asset_id, key, body in objects:
                session.add(
                    MediaAssetRow(
                        id=asset_id,
                        owner_id=ids.owner_id,
                        project_id=None,
                        ownership_scope="GLOBAL",
                        kind="COMPOSITION_VIDEO",
                        object_key=key,
                        sha256=hashlib.sha256(body).hexdigest(),
                        media_type="application/octet-stream",
                        size_bytes=len(body),
                        metadata_json={"immutable": str(asset_id)},
                        shared=True,
                    )
                )
                session.add_all(
                    (
                        ProjectAssetReferenceRow(
                            project_id=ids.project_id,
                            asset_id=asset_id,
                            owner_id=ids.owner_id,
                        ),
                        ProjectAssetReferenceRow(
                            project_id=peer_id,
                            asset_id=asset_id,
                            owner_id=ids.owner_id,
                        ),
                    )
                )
        repository = SqlAlchemyDeletionRepository(
            postgres_database.sessions,
            (PeerProjectBarrierGate("narration"),)
            + tuple(IdleGate(name) for name in ("storyboard", "h3", "composition")),
        )
        service = ProjectDeletionService(repository, counting)
        service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
        with ThreadPoolExecutor(max_workers=2) as pool:
            deleting = pool.submit(service.clean, ids.project_id, ids.owner_id)
            assert replace_holds_media.wait(10)
            beginning = pool.submit(
                service.request, peer_id, ids.owner_id, confirmation="Shared purge peer"
            )
            assert peer_holds_project.wait(10)
            release_replace.set()
            deleting.result(timeout=10)
            beginning.result(timeout=10)

        service.clean(peer_id, ids.owner_id)
        with postgres_database.sessions() as session:
            assert all(session.get(MediaAssetRow, asset_id) is None for asset_id in asset_ids)
        assert sorted(counting.calls) == sorted(key for _, key, _ in objects)
        assert all(not storage.exists(key) for _, key, _ in objects)
    finally:
        release_replace.set()
        event.remove(
            postgres_database.engine, "before_cursor_execute", pause_external_reference_lock
        )
        for _, key, _ in objects:
            if storage.exists(key):
                storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_last_reference_disappears_after_skipped_save_and_is_re_elected_for_delete(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-late-election-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    ids, peer_id, asset_id = production_parent_chain, uuid4(), uuid4()
    key, body = f"late-election/{asset_id}.png", b"late-election"
    digest = hashlib.sha256(body).hexdigest()
    storage.put_at_key(key, body, "image/png", digest)
    saved, release = Event(), Event()
    try:
        with postgres_database.sessions.begin() as session:
            source = session.get_one(ProjectRow, ids.project_id)
            _clone_project(session, source, peer_id)
            session.flush()
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=None,
                    ownership_scope="OWNER",
                    kind="STORYBOARD_GRID",
                    object_key=key,
                    sha256=digest,
                    media_type="image/png",
                    size_bytes=len(body),
                    metadata_json={},
                    shared=True,
                )
            )
            session.add_all(
                ProjectAssetReferenceRow(
                    project_id=project, asset_id=asset_id, owner_id=ids.owner_id
                )
                for project in (ids.project_id, peer_id)
            )
        base = _repository(postgres_database)

        class PausingRepository:
            def __getattr__(self, name: str):
                return getattr(base, name)

            def save_results(self, claim, results):
                base.save_results(claim, results)
                if results[-1].skipped_shared:
                    saved.set()
                    assert release.wait(10)

        errors: list[BaseException] = []

        def run_delete() -> None:
            try:
                ProjectDeletionService(PausingRepository(), storage).clean(  # type: ignore[arg-type]
                    ids.project_id, ids.owner_id, workflow_generation=1
                )
            except BaseException as error:
                errors.append(error)

        ProjectDeletionService(base, storage).request(
            ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_delete)
            assert saved.wait(10)
            with postgres_database.sessions.begin() as session:
                session.delete(session.get_one(ProjectAssetReferenceRow, (peer_id, asset_id)))
            release.set()
            future.result(timeout=10)

        assert errors == []
        assert not storage.exists(key)
        with postgres_database.sessions() as session:
            assert session.get(MediaAssetRow, asset_id) is None
            assert session.get(ProjectRow, ids.project_id) is None
            assert session.get(DeletionTombstoneRow, ids.project_id) is not None
    finally:
        release.set()
        if storage.exists(key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_purge_cleans_direct_media_and_claimed_failed_upload_journal(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    ids = production_parent_chain
    direct_id, journal_id = uuid4(), uuid4()
    with postgres_database.sessions.begin() as session:
        session.add(
            MediaAssetRow(
                id=direct_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                ownership_scope="PROJECT",
                kind="NARRATION_AUDIO",
                object_key=f"direct/{direct_id}.wav",
                sha256="a" * 64,
                media_type="audio/wav",
                size_bytes=1,
                metadata_json={},
            )
        )
        session.add(
            StagedObjectRow(
                id=journal_id,
                object_key=f"staged/{journal_id}.part",
                project_id=ids.project_id,
                owner_id=ids.owner_id,
                purpose="UPLOAD",
                kind="STORYBOARD_GRID",
                ownership_scope="PROJECT",
                state="STAGED",
                sha256="b" * 64,
                size_bytes=1,
                media_type="image/png",
                generation=1,
                created_at=datetime.now(UTC),
            )
        )
    deleted: list[str] = []

    class Deleter:
        def delete_exact(self, object_key: str) -> None:
            deleted.append(object_key)

    service = ProjectDeletionService(_repository(postgres_database), Deleter())
    service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
    with postgres_database.sessions() as session:
        claimed = session.get_one(StagedObjectRow, journal_id)
        assert claimed.project_id == ids.project_id
        assert claimed.ownership_scope == "PROJECT"
        assert claimed.state == "CLEANUP_PENDING"
        assert claimed.execution_token is not None
        assert claimed.generation == 2
    service.clean(ids.project_id, ids.owner_id)
    with postgres_database.sessions() as session:
        assert session.get(MediaAssetRow, direct_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(MediaAssetRow)
                .where(
                    MediaAssetRow.ownership_scope == "PROJECT", MediaAssetRow.project_id.is_(None)
                )
            )
            == 0
        )
    assert set(deleted) == {f"direct/{direct_id}.wav", f"staged/{journal_id}.part"}


@pytest.mark.integration
def test_missing_direct_object_is_an_idempotent_purge_success(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-missing-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    ids, asset_id = production_parent_chain, uuid4()
    key = f"missing/{asset_id}.wav"
    try:
        with postgres_database.sessions.begin() as session:
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    ownership_scope="PROJECT",
                    kind="NARRATION_AUDIO",
                    object_key=key,
                    sha256="c" * 64,
                    media_type="audio/wav",
                    size_bytes=1,
                    metadata_json={},
                )
            )
        service = ProjectDeletionService(_repository(postgres_database), storage)
        service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
        service.clean(ids.project_id, ids.owner_id)

        assert not storage.exists(key)
        with postgres_database.sessions() as session:
            assert session.get(MediaAssetRow, asset_id) is None
            assert session.get(ProjectRow, ids.project_id) is None
            assert session.get(DeletionTombstoneRow, ids.project_id) is not None
    finally:
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_purge_anonymizes_every_project_staged_state_and_cleans_late_put(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-staged-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    ids = production_parent_chain
    states = ("PENDING_UPLOAD", "STAGED", "ATTACHED", "FINALIZED", "CLEANUP_PENDING")
    keys = {state: f"staged/{uuid4()}.part" for state in states}
    owner_key = f"owner/{uuid4()}.part"
    late_key = keys["PENDING_UPLOAD"]
    body = b"late project bytes"
    digest = hashlib.sha256(body).hexdigest()

    class LatePutStorage:
        def delete_exact(self, object_key: str) -> None:
            storage.delete_exact(object_key)
            if object_key == late_key:
                storage.put_at_key(object_key, body, "application/octet-stream", digest)

    try:
        with postgres_database.sessions.begin() as session:
            for state, key in keys.items():
                session.add(
                    StagedObjectRow(
                        id=uuid4(),
                        object_key=key,
                        project_id=ids.project_id,
                        owner_id=ids.owner_id,
                        purpose="sensitive-purpose",
                        kind="sensitive-kind",
                        ownership_scope="PROJECT",
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
                    id=uuid4(),
                    object_key=owner_key,
                    project_id=None,
                    owner_id=ids.owner_id,
                    ownership_scope="OWNER",
                    state="CLEANUP_PENDING",
                    generation=1,
                    execution_token=uuid4(),
                    created_at=datetime.now(UTC),
                )
            )
        service = ProjectDeletionService(_repository(postgres_database), LatePutStorage())
        service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
        service.clean(ids.project_id, ids.owner_id)

        assert storage.exists(late_key)
        with postgres_database.sessions() as session:
            for key in keys.values():
                row = session.scalar(
                    select(StagedObjectRow).where(StagedObjectRow.object_key == key)
                )
                assert row is not None
                assert row.ownership_scope == "GLOBAL"
                assert row.state == "CLEANUP_PENDING"
                assert row.owner_id is None and row.project_id is None
                assert row.purpose == "PROJECT_DELETION"
                assert row.kind is None and row.asset_id is None
                assert row.sha256 is None and row.size_bytes is None
                assert row.media_type is None and row.reference_type is None
            owner_row = session.scalar(
                select(StagedObjectRow).where(StagedObjectRow.object_key == owner_key)
            )
            assert owner_row is not None and owner_row.ownership_scope == "OWNER"

        reconcile_staged_objects(
            postgres_database.sessions, storage, older_than_seconds=-1
        )
        assert not storage.exists(late_key)
    finally:
        for key in keys.values():
            if storage.exists(key):
                storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_expired_purge_claim_is_fenced_before_old_worker_deletes(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-claim-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    counting = CountingStorage(storage)
    ids = production_parent_chain
    asset_id = uuid4()
    key, body = f"claim-fence/{asset_id}.bin", b"claim-fence"
    digest = hashlib.sha256(body).hexdigest()
    storage.put_at_key(key, body, "application/octet-stream", digest)
    try:
        with postgres_database.sessions.begin() as session:
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    ownership_scope="PROJECT",
                    kind="PROVIDER_INPUT",
                    object_key=key,
                    sha256=digest,
                    media_type="application/octet-stream",
                    size_bytes=len(body),
                    metadata_json={},
                )
            )
        base = _repository(postgres_database)
        ProjectDeletionService(base, counting).request(
            ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project"
        )
        paused, release = Event(), Event()

        class PausingRepository:
            def __getattr__(self, name: str):
                return getattr(base, name)

            def get_for_owner(self, project_id, owner_id, claim=None):
                record = base.get_for_owner(project_id, owner_id, claim)
                if claim is not None:
                    paused.set()
                    assert release.wait(10)
                return record

        stale_error: list[DomainError] = []

        def run_old_worker() -> None:
            try:
                ProjectDeletionService(PausingRepository(), counting).clean(  # type: ignore[arg-type]
                    ids.project_id, ids.owner_id, workflow_generation=1
                )
            except DomainError as error:
                stale_error.append(error)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_old_worker)
            assert paused.wait(10)
            with postgres_database.sessions.begin() as session:
                job = session.get_one(ProjectPurgeJobRow, ids.project_id)
                old_token = job.execution_token
                job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            replacement = base.claim_clean(ids.project_id, ids.owner_id, 1)
            assert replacement is not None and replacement.execution_token != old_token
            release.set()
            future.result(timeout=10)

        assert stale_error and stale_error[0].code == "stale_purge_workflow"
        assert counting.calls == []
        assert storage.exists(key)
        with postgres_database.sessions() as session:
            job = session.get_one(ProjectPurgeJobRow, ids.project_id)
            assert job.object_results == []
            assert job.execution_token == replacement.execution_token
    finally:
        if storage.exists(key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)


@pytest.mark.integration
def test_project_scoped_shared_survivor_is_promoted_to_owner_scope(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    ids = production_parent_chain
    peer_id, asset_id = uuid4(), uuid4()
    key = f"project-shared/{asset_id}.png"
    with postgres_database.sessions.begin() as session:
        source = session.get_one(ProjectRow, ids.project_id)
        _clone_project(session, source, peer_id)
        session.flush()
        session.add(
            MediaAssetRow(
                id=asset_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                ownership_scope="PROJECT",
                kind="STORYBOARD_GRID",
                object_key=key,
                sha256="f" * 64,
                media_type="image/png",
                size_bytes=1,
                metadata_json={},
                shared=True,
            )
        )
        session.add_all(
            ProjectAssetReferenceRow(project_id=project, asset_id=asset_id, owner_id=ids.owner_id)
            for project in (ids.project_id, peer_id)
        )
    deleted: list[str] = []

    class Deleter:
        def delete_exact(self, object_key: str) -> None:
            deleted.append(object_key)

    service = ProjectDeletionService(_repository(postgres_database), Deleter())
    service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
    service.clean(ids.project_id, ids.owner_id)
    with postgres_database.sessions() as session:
        survivor = session.get_one(MediaAssetRow, asset_id)
        assert survivor.ownership_scope == "OWNER"
        assert survivor.project_id is None
        assert survivor.shared is True and survivor.purging is False
        assert session.get(ProjectAssetReferenceRow, (peer_id, asset_id)) is not None
    assert deleted == []


@pytest.mark.integration
def test_disappeared_external_reference_deletes_instead_of_promoting(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    ids = production_parent_chain
    peer_id, asset_id = uuid4(), uuid4()
    key = f"project-shared/{asset_id}.png"
    with postgres_database.sessions.begin() as session:
        source = session.get_one(ProjectRow, ids.project_id)
        _clone_project(session, source, peer_id)
        session.flush()
        session.add(
            MediaAssetRow(
                id=asset_id,
                owner_id=ids.owner_id,
                project_id=ids.project_id,
                ownership_scope="PROJECT",
                kind="STORYBOARD_GRID",
                object_key=key,
                sha256="2" * 64,
                media_type="image/png",
                size_bytes=1,
                metadata_json={},
                shared=True,
            )
        )
        session.add_all(
            ProjectAssetReferenceRow(project_id=project, asset_id=asset_id, owner_id=ids.owner_id)
            for project in (ids.project_id, peer_id)
        )
    deleted: list[str] = []

    class Deleter:
        def delete_exact(self, object_key: str) -> None:
            deleted.append(object_key)

    service = ProjectDeletionService(_repository(postgres_database), Deleter())
    service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
    with postgres_database.sessions.begin() as session:
        session.delete(session.get_one(ProjectAssetReferenceRow, (peer_id, asset_id)))
    service.clean(ids.project_id, ids.owner_id)
    with postgres_database.sessions() as session:
        assert session.get(MediaAssetRow, asset_id) is None
    assert deleted == [key]


@pytest.mark.integration
def test_cross_owner_global_shared_reference_fails_closed(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    ids = production_parent_chain
    source_id, disappearing_id, crossed_id = uuid4(), uuid4(), uuid4()
    other_owner = uuid4()
    with postgres_database.sessions.begin() as session:
        source = session.get_one(ProjectRow, ids.project_id)
        _clone_project(session, source, disappearing_id)
        session.add(
            AccountRow(
                id=other_owner,
                username=f"cross-owner-{other_owner.hex}",
                password_hash="test-password-hash",
                role="USER",
                active=True,
                session_generation=1,
            )
        )
        session.flush()
        _clone_project(session, source, crossed_id)
        crossed = session.get_one(ProjectRow, crossed_id)
        crossed.owner_id = other_owner
        session.add(
            MediaAssetRow(
                id=source_id,
                owner_id=ids.owner_id,
                project_id=None,
                ownership_scope="GLOBAL",
                kind="STORYBOARD_GRID",
                object_key=f"project-shared/{source_id}.png",
                sha256="1" * 64,
                media_type="image/png",
                size_bytes=1,
                metadata_json={},
                shared=True,
            )
        )
        session.add_all(
            (
                ProjectAssetReferenceRow(
                    project_id=ids.project_id, asset_id=source_id, owner_id=ids.owner_id
                ),
                ProjectAssetReferenceRow(
                    project_id=disappearing_id, asset_id=source_id, owner_id=ids.owner_id
                ),
            )
        )
    deleted: list[str] = []

    class Deleter:
        def delete_exact(self, object_key: str) -> None:
            deleted.append(object_key)

    service = ProjectDeletionService(_repository(postgres_database), Deleter())
    service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
    with postgres_database.sessions.begin() as session:
        session.delete(session.get_one(ProjectAssetReferenceRow, (disappearing_id, source_id)))
        session.add(
            ProjectAssetReferenceRow(
                project_id=crossed_id, asset_id=source_id, owner_id=other_owner
            )
        )
    with pytest.raises(DomainError, match="crosses account ownership"):
        service.clean(ids.project_id, ids.owner_id)
    with postgres_database.sessions() as session:
        retained = session.get_one(MediaAssetRow, source_id)
        assert retained.ownership_scope == "GLOBAL"
        assert retained.owner_id == ids.owner_id and retained.project_id is None
        assert session.get(ProjectRow, ids.project_id) is not None
    assert deleted == []


@pytest.mark.integration
def test_lease_expiry_during_delete_fences_results_but_not_issued_idempotent_io(
    postgres_database: PostgresDatabase, production_parent_chain: ProductionParentChain
) -> None:
    endpoint = os.environ["TUBEFLOW_TEST_MINIO_ENDPOINT"]
    client = Minio(
        endpoint,
        access_key=os.environ["TUBEFLOW_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["TUBEFLOW_TEST_MINIO_SECRET_KEY"],
        secure=False,
    )
    refuse_application_object_store(client)
    bucket = f"purge-issued-io-{uuid4().hex}"
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    ids, asset_id = production_parent_chain, uuid4()
    key, body = f"issued-io/{asset_id}.bin", b"issued-io"
    digest = hashlib.sha256(body).hexdigest()
    storage.put_at_key(key, body, "application/octet-stream", digest)
    entered, release = Event(), Event()

    class BlockingDeleter:
        calls = 0

        def delete_exact(self, object_key: str) -> None:
            self.calls += 1
            entered.set()
            assert release.wait(10)
            storage.delete_exact(object_key)

    deleter = BlockingDeleter()
    try:
        with postgres_database.sessions.begin() as session:
            session.add(
                MediaAssetRow(
                    id=asset_id,
                    owner_id=ids.owner_id,
                    project_id=ids.project_id,
                    ownership_scope="PROJECT",
                    kind="PROVIDER_INPUT",
                    object_key=key,
                    sha256=digest,
                    media_type="application/octet-stream",
                    size_bytes=len(body),
                    metadata_json={},
                )
            )
        base = _repository(postgres_database)
        service = ProjectDeletionService(base, deleter)
        service.request(ids.project_id, ids.owner_id, confirmation="PostgreSQL fixture project")
        errors: list[DomainError] = []

        def run_old_worker() -> None:
            try:
                service.clean(ids.project_id, ids.owner_id, workflow_generation=1)
            except DomainError as error:
                errors.append(error)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_old_worker)
            assert entered.wait(10)
            with postgres_database.sessions.begin() as session:
                job = session.get_one(ProjectPurgeJobRow, ids.project_id)
                old_token = job.execution_token
                job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            replacement = base.claim_clean(ids.project_id, ids.owner_id, 1)
            assert replacement is not None and replacement.execution_token != old_token
            release.set()
            future.result(timeout=10)

        assert errors and errors[0].code == "stale_purge_workflow"
        assert deleter.calls == 1
        assert not storage.exists(key)
        with postgres_database.sessions() as session:
            job = session.get_one(ProjectPurgeJobRow, ids.project_id)
            assert job.object_results == []
            assert session.get(DeletionTombstoneRow, ids.project_id) is None
            assert session.get(ProjectRow, ids.project_id) is not None
    finally:
        if storage.exists(key):
            storage.delete_exact(key)
        client.remove_bucket(bucket)
