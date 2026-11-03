"""Add production_mode to projects (MANUAL for existing rows, AUTO for new projects).

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "production_mode",
            sa.String(length=8),
            nullable=False,
            server_default="MANUAL",
        ),
    )
    op.create_check_constraint(
        "production_mode",
        "projects",
        "production_mode IN ('MANUAL', 'AUTO')",
    )


def downgrade() -> None:
    op.drop_constraint("production_mode", "projects", type_="check")
    op.drop_column("projects", "production_mode")
