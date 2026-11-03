"""Add explicit lineage for manually confirmed project director recovery runs.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_director_runs",
        sa.Column("recovery_source_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_project_director_runs_recovery_source_run_id",
        "project_director_runs",
        "project_director_runs",
        ["recovery_source_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "project_director_units",
        sa.Column("reused_from_unit_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_project_director_units_reused_from_unit_id",
        "project_director_units",
        "project_director_units",
        ["reused_from_unit_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_project_director_units_reused_from_unit_id",
        "project_director_units",
        type_="foreignkey",
    )
    op.drop_column("project_director_units", "reused_from_unit_id")
    op.drop_constraint(
        "fk_project_director_runs_recovery_source_run_id",
        "project_director_runs",
        type_="foreignkey",
    )
    op.drop_column("project_director_runs", "recovery_source_run_id")
