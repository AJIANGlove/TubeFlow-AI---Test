"""Add durable storyboard provider failover lineage and budgets.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


_COLUMNS = (
    "provider",
    "failover_chain_id",
    "failover_policy_version",
    "failover_route_index",
    "failover_model_attempt",
    "failover_total_attempt",
)


def upgrade() -> None:
    op.add_column("storyboard_generations", sa.Column("provider", sa.String(32), nullable=True))
    op.add_column(
        "storyboard_generations",
        sa.Column("failover_chain_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "storyboard_generations",
        sa.Column("failover_policy_version", sa.String(64), nullable=True),
    )
    op.add_column(
        "storyboard_generations", sa.Column("failover_route_index", sa.Integer(), nullable=True)
    )
    op.add_column(
        "storyboard_generations", sa.Column("failover_model_attempt", sa.Integer(), nullable=True)
    )
    op.add_column(
        "storyboard_generations", sa.Column("failover_total_attempt", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_storyboard_generations_failover_chain",
        "storyboard_generations",
        ["owner_id", "failover_chain_id"],
    )
    op.create_check_constraint(
        "ck_storyboard_failover_route_index",
        "storyboard_generations",
        "failover_route_index BETWEEN 0 AND 6",
    )
    op.create_check_constraint(
        "ck_storyboard_failover_model_attempt",
        "storyboard_generations",
        "failover_model_attempt BETWEEN 1 AND 2",
    )
    op.create_check_constraint(
        "ck_storyboard_failover_total_attempt",
        "storyboard_generations",
        "failover_total_attempt BETWEEN 1 AND 14",
    )
    all_null = " AND ".join(f"{column} IS NULL" for column in _COLUMNS)
    all_present = " AND ".join(f"{column} IS NOT NULL" for column in _COLUMNS)
    op.create_check_constraint(
        "ck_sb_failover_fields_all_or_none",
        "storyboard_generations",
        f"({all_null}) OR ({all_present})",
    )
    op.create_unique_constraint(
        "uq_storyboard_failover_chain_total",
        "storyboard_generations",
        ["owner_id", "failover_chain_id", "failover_total_attempt"],
    )
    op.create_unique_constraint(
        "uq_storyboard_failover_chain_model_attempt",
        "storyboard_generations",
        ["owner_id", "failover_chain_id", "failover_route_index", "failover_model_attempt"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_storyboard_failover_chain_model_attempt",
        "storyboard_generations",
        type_="unique",
    )
    op.drop_constraint(
        "uq_storyboard_failover_chain_total", "storyboard_generations", type_="unique"
    )
    op.drop_constraint(
        "ck_sb_failover_fields_all_or_none", "storyboard_generations", type_="check"
    )
    op.drop_constraint(
        "ck_storyboard_failover_total_attempt", "storyboard_generations", type_="check"
    )
    op.drop_constraint(
        "ck_storyboard_failover_model_attempt", "storyboard_generations", type_="check"
    )
    op.drop_constraint(
        "ck_storyboard_failover_route_index", "storyboard_generations", type_="check"
    )
    op.drop_index(
        "ix_storyboard_generations_failover_chain", table_name="storyboard_generations"
    )
    for column in reversed(_COLUMNS):
        op.drop_column("storyboard_generations", column)
