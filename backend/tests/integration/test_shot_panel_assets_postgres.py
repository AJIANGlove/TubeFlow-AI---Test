from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import pytest
from conftest import PostgresDatabase, ProductionParentChain
from PIL import Image, ImageDraw
from sqlalchemy import func, select
from test_single_panel_opt_in_postgres import _fixture_source

from tubeflow_test.adapters.object_storage import StoredObject
from tubeflow_test.adapters.persistence.media_repository import reconcile_staged_objects
from tubeflow_test.adapters.persistence.models import (
    MediaAssetRow,
    ProjectAssetReferenceRow,
    StagedObjectRow,
)
from tubeflow_test.adapters.persistence.shot_panel_assets import ShotPanelAssetService
from tubeflow_test.domain.common import MediaAssetRef
from tubeflow_test.video_generation.domain import ReferencePolicy, ReferenceStrategy
from tubeflow_test.video_generation.orchestration import AuthoritativeGroupInput


class FakeObjects:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_delete = False

    def put_at_key(
        self, key: str, body: bytes, media_type: str, expected_sha256: str
    ) -> StoredObject:
        if key in self.values and self.values[key] != body:
            raise ValueError("refusing to overwrite different bytes")
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("hash mismatch")
        created = key not in self.values
        self.values[key] = body
        return StoredObject(key, expected_sha256, len(body), media_type, created)

    def get(self, key: str) -> bytes:
        return self.values[key]

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        body = self.values[key]
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError("stored bytes do not match")
        return body

    def exists(self, key: str) -> bool:
        return key in self.values

    def delete_exact(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.values.pop(key, None)


class Reader:
    def __init__(self, content: bytes, expected: MediaAssetRef) -> None:
        self.content = content
        self.expected = expected

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        assert asset == self.expected
        return self.content


def _png() -> bytes:
    image = Image.new("RGB", (120, 100), (20, 20, 20))
    ImageDraw.Draw(image).rectangle((0, 54, 119, 99), fill=(80, 100, 120))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _service(
    database: PostgresDatabase,
    ids: ProductionParentChain,
) -> tuple[ShotPanelAssetService, AuthoritativeGroupInput, FakeObjects, ReferencePolicy]:
    content = _png()
    storyboard = MediaAssetRef(
        uuid4(), hashlib.sha256(content).hexdigest(), "image/png", len(content)
    )
    source = replace(
        _fixture_source(ids, uuid4()),
        storyboard_image=storyboard,
        storyboard_asset_sha256=storyboard.sha256,
    )
    objects = FakeObjects()
    service = ShotPanelAssetService(database.sessions, objects, Reader(content, storyboard))  # type: ignore[arg-type]
    policy = ReferencePolicy(ReferenceStrategy.SINGLE_PANEL_V1, {"version": "crop-title54-v1"})
    return service, source, objects, policy


def _counts(
    database: PostgresDatabase, objects: FakeObjects
) -> tuple[int, int, int, int]:
    with database.sessions() as session:
        media = int(session.scalar(select(func.count()).select_from(MediaAssetRow)) or 0)
        journals = int(session.scalar(select(func.count()).select_from(StagedObjectRow)) or 0)
        references = int(
            session.scalar(select(func.count()).select_from(ProjectAssetReferenceRow)) or 0
        )
    return media, journals, references, len(objects.values)


def test_preview_has_no_writes_and_intent_archives_exact_media_and_journal(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    service, source, objects, policy = _service(postgres_database, production_parent_chain)
    with postgres_database.sessions.begin() as session:
        preview = service.derive(session, source, policy, for_intent=False)
    assert len(preview) == 1
    assert _counts(postgres_database, objects) == (0, 0, 0, 0)

    with postgres_database.sessions.begin() as session:
        persisted = service.derive(session, source, policy, for_intent=True)

    assert persisted[0].asset == preview[0].asset
    assert _counts(postgres_database, objects) == (1, 1, 1, 1)
    with postgres_database.sessions() as session:
        media = session.scalar(select(MediaAssetRow))
        journal = session.scalar(select(StagedObjectRow))
        assert media is not None and media.kind == "H3_SHOT_PANEL"
        assert media.shared is False and media.project_id == source.project_id
        assert media.metadata_json["source_storyboard_sha256"] == source.storyboard_image.sha256
        assert media.metadata_json["shot_order"] == 1
        assert media.metadata_json["derivation"] == "crop-title54-v1"
        assert journal is not None and journal.state == "FINALIZED"
        assert journal.asset_id == persisted[0].asset.asset_id


def test_outer_transaction_rollback_deletes_new_object_and_database_rows(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    service, source, objects, policy = _service(postgres_database, production_parent_chain)
    with (
        pytest.raises(RuntimeError, match="force rollback"),
        postgres_database.sessions.begin() as session,
    ):
        service.derive(session, source, policy, for_intent=True)
        raise RuntimeError("force rollback")
    assert _counts(postgres_database, objects) == (0, 0, 0, 0)


def test_delete_failure_is_recovered_through_cleanup_journal(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    service, source, objects, policy = _service(postgres_database, production_parent_chain)
    objects.fail_delete = True
    with (
        pytest.raises(RuntimeError, match="force rollback"),
        postgres_database.sessions.begin() as session,
    ):
        service.derive(session, source, policy, for_intent=True)
        raise RuntimeError("force rollback")
    media, journals, references, stored = _counts(postgres_database, objects)
    assert (media, references, stored) == (0, 0, 1)
    assert journals == 1
    with postgres_database.sessions() as session:
        journal = session.scalar(select(StagedObjectRow))
        assert journal is not None and journal.state == "CLEANUP_PENDING"

    objects.fail_delete = False
    reconcile_staged_objects(postgres_database.sessions, objects, older_than_seconds=0)  # type: ignore[arg-type]
    assert objects.values == {}


def test_tampered_existing_object_is_rejected_without_overwrite(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    service, source, objects, policy = _service(postgres_database, production_parent_chain)
    with postgres_database.sessions.begin() as session:
        service.derive(session, source, policy, for_intent=True)
    key = next(iter(objects.values))
    objects.values[key] = b"tampered"
    with (
        pytest.raises(ValueError, match="stored bytes do not match"),
        postgres_database.sessions.begin() as session,
    ):
        service.derive(session, source, policy, for_intent=True)
    assert objects.values[key] == b"tampered"
    assert _counts(postgres_database, objects) == (1, 1, 1, 1)


def test_concurrent_archives_share_one_immutable_winner(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    service, source, objects, policy = _service(postgres_database, production_parent_chain)

    def archive():  # type: ignore[no-untyped-def]
        with postgres_database.sessions.begin() as session:
            return service.derive(session, source, policy, for_intent=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: archive(), range(2)))
    assert results[0][0].asset == results[1][0].asset
    assert _counts(postgres_database, objects) == (1, 1, 1, 1)
