"""Record explicit project director evidence waivers on replacement runs.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_director_runs",
        sa.Column("manual_recovery_audit_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_director_runs", "manual_recovery_audit_json")
