"""Add the frozen locale to project director technical units.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_director_units",
        sa.Column("required_locale", sa.String(16), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE project_director_units AS unit
            SET required_locale = run.input_json ->> 'locale'
            FROM project_director_runs AS run
            WHERE run.id = unit.run_id
              AND unit.required_locale IS NULL
            """
        )
    )
    op.alter_column("project_director_units", "required_locale", nullable=False)


def downgrade() -> None:
    op.drop_column("project_director_units", "required_locale")
