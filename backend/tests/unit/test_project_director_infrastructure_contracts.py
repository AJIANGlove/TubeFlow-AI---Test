from __future__ import annotations

import warnings
from typing import cast
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, Table, UniqueConstraint, create_engine, text
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import Session

from tubeflow_test.adapters.persistence.media_repository import SqlStoryboardActiveJobGate
from tubeflow_test.adapters.persistence.schema import canonical_metadata
from tubeflow_test.adapters.persistence.storyboards.project_director_models import (
    DirectorRevisionLineageRow,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    _shot as project_director_shot,
)
from tubeflow_test.adapters.persistence.storyboards.project_director_repository import (
    _shot_json as project_director_shot_json,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    _shot as storyboard_shot,
)
from tubeflow_test.adapters.persistence.storyboards.repository import (
    _shot_json as storyboard_shot_json,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.project_director import ProjectDirectorRunStatus
from tubeflow_test.domain.storyboards import (
    DirectorLocationRelation,
    DirectorSemanticEvent,
    DirectorShot,
    DirectorSubjectFocus,
    DirectorTimeRelation,
)

DIRECTOR_TOKEN = "test-director-token-that-must-stay-redacted"
DOMESTIC_DIRECTOR_URL = "https://grsai.dakka.com.cn/v1"
PROJECT_DIRECTOR_TABLES = {
    "project_director_runs",
    "project_director_units",
    "project_director_revisions",
    "project_director_current",
    "project_director_materializations",
    "project_director_group_candidates",
    "director_revision_lineage",
}


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "session_secret": "test-session-secret-that-is-at-least-32-bytes",
        "cosyvoice_url": "http://cosyvoice.test",
        "cosyvoice_token": SecretStr(""),
        "director_provider_url": DOMESTIC_DIRECTOR_URL,
        "director_provider_token": SecretStr(DIRECTOR_TOKEN),
        "director_provider_model": "gpt-5.6-sol",
        "h3_node_urls": (),
        "h3_node_ids": (),
        "h3_node_instance_ids": (),
        "h3_node_tokens": (),
        "h3_node_capacities": (),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_director_token_is_secret_and_never_dumped_in_plaintext() -> None:
    settings = _settings()

    assert isinstance(settings.director_provider_token, SecretStr)
    assert settings.director_provider_token.get_secret_value() == DIRECTOR_TOKEN
    assert DIRECTOR_TOKEN not in repr(settings)
    assert DIRECTOR_TOKEN not in repr(settings.model_dump())
    assert DIRECTOR_TOKEN not in settings.model_dump_json()


@pytest.mark.parametrize("invalid_url", ("", "director.invalid/v1", "ftp://director.invalid/v1"))
def test_enabled_director_rejects_empty_or_invalid_url(invalid_url: str) -> None:
    with pytest.raises(ValidationError, match=r"absolute HTTP\(S\) URL"):
        _settings(director_provider_url=invalid_url)


def test_enabled_director_rejects_empty_model() -> None:
    with pytest.raises(ValidationError, match="model is required"):
        _settings(director_provider_model="   ")


def test_valid_domestic_director_configuration_is_ready() -> None:
    settings = _settings()

    assert settings.director_provider_configured is True
    assert settings.director_provider_url == DOMESTIC_DIRECTOR_URL
    assert settings.director_provider_model == "gpt-5.6-sol"


def test_director_parallelism_defaults_to_four_and_is_bounded() -> None:
    assert _settings().director_parallelism == 4
    assert _settings(director_parallelism=1).director_parallelism == 1
    assert _settings(director_parallelism=16).director_parallelism == 16
    with pytest.raises(ValidationError):
        _settings(director_parallelism=0)
    with pytest.raises(ValidationError):
        _settings(director_parallelism=17)


def test_canonical_metadata_registers_all_0014_tables_without_cycle_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        metadata = canonical_metadata()
        sorted_tables = tuple(metadata.sorted_tables)

    assert PROJECT_DIRECTOR_TABLES.issubset(metadata.tables)
    assert PROJECT_DIRECTOR_TABLES.issubset({table.name for table in sorted_tables})
    assert metadata.tables["project_director_units"].c.required_locale.nullable is False
    assert metadata.tables["project_director_units"].c.response_diagnostics_json.nullable is False
    cycle_warnings = [
        item
        for item in caught
        if issubclass(item.category, SAWarning)
        and "unresolvable cycles" in str(item.message).lower()
    ]
    assert cycle_warnings == []


def test_project_director_recovery_lineage_uses_nullable_self_references() -> None:
    metadata = canonical_metadata()
    run_source = metadata.tables["project_director_runs"].c.recovery_source_run_id
    unit_source = metadata.tables["project_director_units"].c.reused_from_unit_id

    assert run_source.nullable is True
    assert {
        (foreign_key.constraint.name, foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in run_source.foreign_keys
    } == {
        (
            "fk_project_director_runs_recovery_source_run_id",
            "project_director_runs.id",
            "SET NULL",
        )
    }
    assert unit_source.nullable is True
    assert {
        (foreign_key.constraint.name, foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in unit_source.foreign_keys
    } == {
        (
            "fk_project_director_units_reused_from_unit_id",
            "project_director_units.id",
            "SET NULL",
        )
    }


def test_director_lineage_allows_each_immutable_edit_to_share_one_candidate() -> None:
    table = cast(Table, DirectorRevisionLineageRow.__table__)
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert tuple(column.name for column in table.primary_key.columns) == ("revision_id",)
    assert ("candidate_id",) not in unique_column_sets


def test_controlled_semantics_round_trip_through_both_director_repositories() -> None:
    shot = DirectorShot(
        UUID(int=7001),
        1,
        "supported cave",
        "WIDE",
        "EYE_LEVEL",
        "person enters → shelter is found",
        "establish shelter",
        "A person enters the supported cave.",
        DirectorSemanticEvent.PERFORM_ACTION,
        DirectorSubjectFocus.PRIMARY_SUBJECT,
        DirectorTimeRelation.LATER,
        DirectorLocationRelation.SCRIPT_SUPPORTED_NEW_LOCATION,
    )

    assert project_director_shot(project_director_shot_json(shot)) == shot
    assert storyboard_shot(storyboard_shot_json(shot)) == shot


def _create_storyboard_gate_tables() -> Engine:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    statements = (
        "CREATE TABLE storyboard_generations "
        "(id CHAR(32) PRIMARY KEY, project_id CHAR(32), status VARCHAR(32))",
        "CREATE TABLE authoritative_narration_groups "
        "(id CHAR(32) PRIMARY KEY, project_id CHAR(32))",
        "CREATE TABLE director_generation_attempts "
        "(id CHAR(32) PRIMARY KEY, group_id CHAR(32), status VARCHAR(32))",
        "CREATE TABLE project_director_runs "
        "(id CHAR(32) PRIMARY KEY, project_id CHAR(32), status VARCHAR(32), "
        "revision_id CHAR(32))",
        "CREATE TABLE project_director_current "
        "(owner_id CHAR(32), project_id CHAR(32), revision_id CHAR(32), "
        "PRIMARY KEY (owner_id, project_id))",
        "CREATE TABLE project_director_materializations "
        "(id CHAR(32) PRIMARY KEY, project_id CHAR(32), preplan_revision_id CHAR(32))",
        "CREATE TABLE storyboard_outbox "
        "(id CHAR(32) PRIMARY KEY, project_id CHAR(32), acknowledged_at DATETIME)",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    return engine


@pytest.mark.parametrize(
    ("status", "active"),
    (
        (ProjectDirectorRunStatus.INTENT_PERSISTED, True),
        (ProjectDirectorRunStatus.RUNNING, True),
        (ProjectDirectorRunStatus.REMOTE_STATE_UNKNOWN, True),
        (ProjectDirectorRunStatus.SUCCEEDED, False),
        (ProjectDirectorRunStatus.FAILED, False),
    ),
)
def test_storyboard_gate_classifies_project_director_run_states(
    status: ProjectDirectorRunStatus,
    active: bool,
) -> None:
    engine = _create_storyboard_gate_tables()
    project_id = "1" * 32
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO project_director_runs (id, project_id, status) "
                "VALUES (:id, :project_id, :status)"
            ),
            {"id": "2" * 32, "project_id": project_id, "status": status.value},
        )

    with Session(engine) as session:
        assert SqlStoryboardActiveJobGate().has_active(session, UUID(project_id)) is active
    engine.dispose()


@pytest.mark.parametrize(
    ("narration_ready", "materialized", "active"),
    (
        (False, False, False),
        (True, False, True),
        (True, True, False),
    ),
)
def test_storyboard_gate_closes_only_the_ready_materialization_window(
    narration_ready: bool,
    materialized: bool,
    active: bool,
) -> None:
    engine = _create_storyboard_gate_tables()
    owner_id, project_id, revision_id = ("1" * 32, "2" * 32, "3" * 32)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO project_director_runs "
                "(id, project_id, status, revision_id) "
                "VALUES (:id, :project_id, :status, :revision_id)"
            ),
            {
                "id": "4" * 32,
                "project_id": project_id,
                "status": ProjectDirectorRunStatus.SUCCEEDED.value,
                "revision_id": revision_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO project_director_current "
                "(owner_id, project_id, revision_id) "
                "VALUES (:owner_id, :project_id, :revision_id)"
            ),
            {
                "owner_id": owner_id,
                "project_id": project_id,
                "revision_id": revision_id,
            },
        )
        if narration_ready:
            connection.execute(
                text(
                    "INSERT INTO authoritative_narration_groups (id, project_id) "
                    "VALUES (:id, :project_id)"
                ),
                {"id": "5" * 32, "project_id": project_id},
            )
        if materialized:
            connection.execute(
                text(
                    "INSERT INTO project_director_materializations "
                    "(id, project_id, preplan_revision_id) "
                    "VALUES (:id, :project_id, :revision_id)"
                ),
                {
                    "id": "6" * 32,
                    "project_id": project_id,
                    "revision_id": revision_id,
                },
            )

    with Session(engine) as session:
        assert SqlStoryboardActiveJobGate().has_active(session, UUID(project_id)) is active
    engine.dispose()


def test_storyboard_provider_quality_defaults_to_high() -> None:
    assert _settings().storyboard_provider_quality == "high"


def test_storyboard_provider_quality_must_be_a_known_tier() -> None:
    with pytest.raises(ValidationError, match="quality must be"):
        _settings(storyboard_provider_quality="ultra")
