from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import PostgresDatabase
from sqlalchemy import select

from tubeflow_test.config import Settings
from tubeflow_test.video_generation.persistence import H3NodeRecord
from tubeflow_test.video_generation.runtime import _bind_configured_h3_nodes


@pytest.mark.integration
def test_concurrent_assemblies_bind_one_consistent_three_node_set(
    postgres_database: PostgresDatabase,
) -> None:
    settings = Settings(
        database_url=postgres_database.url,
        h3_node_ids=("node-a", "node-b", "node-c"),
        h3_node_instance_ids=("gpu-a", "gpu-b", "gpu-c"),
        h3_node_urls=("http://a", "http://b", "http://c"),
        h3_node_tokens=("token-a", "token-b", "token-c"),
        h3_node_capacities=(1, 2, 3),
        h3_workflow_manifest_sha256="a" * 64,
    )
    barrier = Barrier(2)

    def assemble() -> None:
        barrier.wait()
        with postgres_database.sessions.begin() as session:
            _bind_configured_h3_nodes(session, settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(lambda _index: assemble(), range(2)))

    with postgres_database.sessions() as session:
        rows = tuple(session.scalars(select(H3NodeRecord).order_by(H3NodeRecord.node_id)))
        assert tuple(row.node_id for row in rows) == settings.h3_node_ids
        assert tuple(row.instance_identity for row in rows) == settings.h3_node_instance_ids
        assert tuple(row.capacity for row in rows) == settings.h3_node_capacities


def test_duplicate_node_or_instance_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match="node IDs must be unique"):
        Settings(
            h3_node_ids=("node-a", "node-a", "node-c"),
            h3_node_instance_ids=("gpu-a", "gpu-b", "gpu-c"),
            h3_node_urls=("http://a", "http://b", "http://c"),
            h3_node_tokens=("token-a", "token-b", "token-c"),
            h3_node_capacities=(1, 1, 1),
        )
