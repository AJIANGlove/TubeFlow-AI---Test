"""Integration tests create schemas and databases only in a test database (2026-11-06).

An empty `contract_*` schema was found in the application database `tubeflow`, so an
integration run had once been pointed at it. Every integration test that creates a schema
or a database calls `refuse_application_database` first.

The rule is a name allowlist: the connected database's name must end with "test" (the
shared test database is `tubeflowtest`; the application database is `tubeflow`). A first
version refused databases whose `public.projects` had rows, but the test database also
holds such a row (a migration probe), so the data cannot tell the two apart.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

TEST_DATABASE_SUFFIX = "test"


def refuse_application_database(engine: Engine) -> None:
    with engine.connect() as connection:
        name = connection.execute(text("select current_database()")).scalar()
    if not isinstance(name, str) or not name.lower().endswith(TEST_DATABASE_SUFFIX):
        pytest.fail(
            "TUBEFLOW_TEST_POSTGRES_URL must name a database whose name ends with "
            f"'{TEST_DATABASE_SUFFIX}' (connected to {name!r}); integration tests refuse to "
            "create schemas or databases anywhere else",
            pytrace=False,
        )
