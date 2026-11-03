"""Pin existing projects to their legacy H3 route before enabling candidate nodes.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "h3_project_workflow_routes",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("profile", sa.String(16), nullable=False),
        sa.Column(
            "workflow_snapshot_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True,
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "profile IN ('legacy', 'candidate')",
            name=op.f("ck_h3_project_workflow_routes_profile"),
        ),
    )
    op.execute(sa.text(
        "INSERT INTO h3_project_workflow_routes (project_id, profile, bound_at) "
        "SELECT id, 'legacy', CURRENT_TIMESTAMP FROM projects"
    ))


def downgrade() -> None:
    op.drop_table("h3_project_workflow_routes")
