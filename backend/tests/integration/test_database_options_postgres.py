"""`Database` connections stay in the URL's schema and keep the S6 timeouts (2026-11-06).

Before the fix, `Database` dropped the URL's `options=-csearch_path=...`, so its
connections ran in `public` (integration triage, Pending issues/2026-11-06/
INTEGRATION_FAILURES_TRIAGE). This test checks the real connection, not the arguments.
"""

from __future__ import annotations

import pytest
from conftest import PostgresDatabase
from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database


@pytest.mark.integration
def test_database_connections_use_the_url_schema_and_the_timeouts(
    postgres_database: PostgresDatabase,
) -> None:
    database = Database(postgres_database.url)
    try:
        with database.unit_of_work() as session:
            schema = session.execute(text("select current_schema()")).scalar()
            lock_timeout = session.execute(text("show lock_timeout")).scalar()
            statement_timeout = session.execute(text("show statement_timeout")).scalar()
    finally:
        database.engine.dispose()
    assert schema == postgres_database.schema
    assert (lock_timeout, statement_timeout) == ("15s", "1min")
