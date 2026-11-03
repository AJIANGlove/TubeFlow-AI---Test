"""Add auto_pipeline_runs table and projects.auto_budget_round (§15 one-shot AUTO pipeline).

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "auto_budget_round",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_table(
        "auto_pipeline_runs",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=True),
        sa.Column(
            "authorization_round",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('RUNNING', 'PAUSED', 'COMPLETED')",
            name="state",
        ),
        sa.CheckConstraint("authorization_round > 0", name="authorization_round"),
    )
    op.create_index(
        "ix_auto_pipeline_runs_owner_id",
        "auto_pipeline_runs",
        ["owner_id"],
    )
    op.create_index(
        "ix_auto_pipeline_runs_project_id",
        "auto_pipeline_runs",
        ["project_id"],
    )
    op.create_index(
        "uq_auto_pipeline_runs_active_project",
        "auto_pipeline_runs",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('RUNNING', 'PAUSED')"),
    )


def downgrade() -> None:
    op.drop_index("uq_auto_pipeline_runs_active_project", table_name="auto_pipeline_runs")
    op.drop_index("ix_auto_pipeline_runs_project_id", table_name="auto_pipeline_runs")
    op.drop_index("ix_auto_pipeline_runs_owner_id", table_name="auto_pipeline_runs")
    op.drop_table("auto_pipeline_runs")
    op.drop_column("projects", "auto_budget_round")
