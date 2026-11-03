from __future__ import annotations

from datetime import UTC, datetime

from conftest import PostgresDatabase, ProductionParentChain
from sqlalchemy import delete, func, inspect, select

from tubeflow_test.adapters.persistence.identity_project.models import (
    ProjectPublicationEventRow,
    ProjectRow,
)
from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)


def test_project_publication_schema_and_repository_round_trip(
    postgres_database: PostgresDatabase,
    production_parent_chain: ProductionParentChain,
) -> None:
    inspector = inspect(postgres_database.engine)
    project_columns = {item["name"]: item for item in inspector.get_columns("projects")}
    assert project_columns["published_at"]["nullable"] is True
    ledger_columns = {
        item["name"]: item
        for item in inspector.get_columns("project_publication_events")
    }
    assert set(ledger_columns) == {
        "id",
        "project_id",
        "owner_id",
        "actor_account_id",
        "action",
        "occurred_at",
    }
    foreign_keys = {
        item["constrained_columns"][0]: item
        for item in inspector.get_foreign_keys("project_publication_events")
    }
    assert foreign_keys["project_id"]["options"]["ondelete"] == "CASCADE"
    index_names = {
        item["name"] for item in inspector.get_indexes("project_publication_events")
    }
    assert {
        "ix_project_publication_events_project_id",
        "ix_project_publication_events_owner_id",
    } <= index_names

    ids = production_parent_chain
    now = datetime.now(UTC)
    with postgres_database.sessions.begin() as session:
        repo = SqlAlchemyIdentityProjectRepository(session)
        assert repo.set_project_publication(
            ids.owner_id,
            ids.project_id,
            published_at=now,
            actor_account_id=ids.owner_id,
            occurred_at=now,
        )
        loaded = repo.project(ids.owner_id, ids.project_id)
        assert loaded is not None and loaded.published_at == now
        assert not repo.set_project_publication(
            ids.owner_id,
            ids.project_id,
            published_at=now,
            actor_account_id=ids.owner_id,
            occurred_at=now,
        )
        assert session.scalar(
            select(func.count()).select_from(ProjectPublicationEventRow)
        ) == 1

    with postgres_database.sessions.begin() as session:
        session.execute(delete(ProjectRow).where(ProjectRow.id == ids.project_id))
    with postgres_database.sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(ProjectPublicationEventRow)
        ) == 0
