"""Add manual project publication marker and audit ledger.

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL = 未发布；只由发布/取消发布接口直写，不参与项目编辑与快照。
    op.add_column(
        "projects",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "project_publication_events",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "actor_account_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('PUBLISHED', 'UNPUBLISHED')",
            name="action",
        ),
    )
    op.create_index(
        "ix_project_publication_events_project_id",
        "project_publication_events",
        ["project_id"],
    )
    op.create_index(
        "ix_project_publication_events_owner_id",
        "project_publication_events",
        ["owner_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_publication_events_owner_id",
        table_name="project_publication_events",
    )
    op.drop_index(
        "ix_project_publication_events_project_id",
        table_name="project_publication_events",
    )
    op.drop_table("project_publication_events")
    op.drop_column("projects", "published_at")
