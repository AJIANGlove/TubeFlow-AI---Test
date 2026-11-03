"""Record auto-confirmed director input changes from deployment version changes.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_director_runs",
        sa.Column("auto_input_change_audit_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_director_runs", "auto_input_change_audit_json")
