from __future__ import annotations

import pytest
from sqlalchemy import inspect


@pytest.mark.integration
def test_empty_postgres_alembic_registers_storyboard_schema(postgres_database) -> None:
    names = set(inspect(postgres_database.engine).get_table_names())
    assert "authoritative_narration_groups" in names
    assert "storyboard_generations" in names
    assert "storyboard_media" in names
    assert "video_storyboard_bindings" in names
    assert "narration_groups" not in names
    inspector = inspect(postgres_database.engine)
    columns = {item["name"] for item in inspector.get_columns("storyboard_generations")}
    assert {
        "provider",
        "failover_chain_id",
        "failover_policy_version",
        "failover_route_index",
        "failover_model_attempt",
        "failover_total_attempt",
    } <= columns
    constraints = {
        item["name"] for item in inspector.get_unique_constraints("storyboard_generations")
    }
    assert "uq_storyboard_failover_chain_total" in constraints
    assert "uq_storyboard_failover_chain_model_attempt" in constraints
    checks = {item["name"] for item in inspector.get_check_constraints("storyboard_generations")}
    assert "ck_storyboard_generations_ck_sb_failover_fields_all_or_none" in checks
    assert "ck_storyboard_generations_ck_storyboard_failover_route_index" in checks
    assert "ck_storyboard_generations_ck_storyboard_failover_model_attempt" in checks
    assert "ck_storyboard_generations_ck_storyboard_failover_total_attempt" in checks
