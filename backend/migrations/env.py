from __future__ import annotations

import contextlib
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from tubeflow_test.adapters.persistence.schema import canonical_metadata

target_metadata = canonical_metadata()

# URL precedence is part of the programmatic migration contract:
# caller-owned connection / Config.attributes URL > environment > alembic.ini.
# ConfigParser treats percent signs as interpolation markers, so values copied
# into the main section must be escaped. The caller-owned connection itself is
# passed directly to Alembic and never converted back to a URL.
configured_connection = context.config.attributes.get("connection")
configured_url = context.config.attributes.get("database_url")
environment_url = os.getenv("TUBEFLOW_DATABASE_URL")
if configured_connection is None and configured_url is not None:
    context.config.set_main_option("sqlalchemy.url", str(configured_url).replace("%", "%%"))
elif configured_connection is None and environment_url:
    context.config.set_main_option("sqlalchemy.url", environment_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=context.config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = context.config.attributes.get("connection")
    connectable = supplied_connection or engine_from_config(
        context.config.get_section(context.config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    connection_context = (
        connectable.connect()
        if supplied_connection is None
        else contextlib.nullcontext(connectable)
    )
    with connection_context as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
