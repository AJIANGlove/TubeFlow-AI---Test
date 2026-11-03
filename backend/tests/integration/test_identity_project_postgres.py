from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tubeflow_test.adapters.persistence.identity_project.repository import (
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.config import Settings
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.identity_project.runtime import create_identity_project_runtime

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_real_postgres_schema_round_trip_and_atomic_initialization() -> None:
    url = os.environ.get("TUBEFLOW_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TUBEFLOW_TEST_POSTGRES_URL is not configured")
    schema = f"identity_test_{uuid4().hex}"
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        migration_url = f"{url}?options=-csearch_path%3D{schema}"
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
        config.set_main_option("prepend_sys_path", str(BACKEND_ROOT / "src"))
        config.attributes["database_url"] = migration_url
        command.upgrade(config, "head")

        runtime = create_identity_project_runtime(
            Settings(
                database_url=migration_url,
                identity_setup_username="same-admin",
                identity_setup_password="strong-password",
            )
        )
        app = FastAPI()
        app.include_router(runtime.router)
        assert "/identity/projects/{project_id}/confirm-narration" in app.openapi()["paths"]

        def initialize(_ordinal: int) -> tuple[int, str | None]:
            response = TestClient(app).post(
                "/identity/setup/initialize",
                json={"username": "same-admin", "password": "strong-password"},
            )
            detail = response.json().get("detail")
            return response.status_code, detail.get("code") if isinstance(detail, dict) else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(initialize, (1, 2)))
        assert sorted(results) == [(201, None), (409, "admin_already_initialized")]
        with runtime.database.unit_of_work() as session:
            repo = SqlAlchemyIdentityProjectRepository(session)
            assert repo.setup_complete()
            first_admin = repo.list_accounts()[0]
            second_admin = Account(uuid4(), "second-admin", "hash", AccountRole.ADMIN, True, 1)
            repo.add_account(second_admin)

        def disable(account_id: UUID) -> str:
            with runtime.database.unit_of_work() as session:
                return SqlAlchemyIdentityProjectRepository(
                    session
                ).disable_account_preserving_admin(account_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            disable_results = list(pool.map(disable, (first_admin.id, second_admin.id)))
        assert sorted(disable_results) == ["disabled", "last_admin"]
        with runtime.database.unit_of_work() as session:
            assert SqlAlchemyIdentityProjectRepository(session).active_admin_count() == 1
        runtime.database.engine.dispose()
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
