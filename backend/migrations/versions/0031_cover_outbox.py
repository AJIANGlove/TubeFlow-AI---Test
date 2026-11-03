"""Add the cover outbox table.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

COVER_OUTBOX_TABLE = "cover_outbox"


def upgrade() -> None:
    op.create_table(
        "cover_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=192), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_cover_outbox_owner_id", "cover_outbox", ["owner_id"])
    op.create_index("ix_cover_outbox_project_id", "cover_outbox", ["project_id"])
    op.create_index("ix_cover_outbox_aggregate_id", "cover_outbox", ["aggregate_id"])
    # 仅未确认事件占用 workflow_id：解析动作在途重复点击去重；确认后（含失败
    # 工作流）同一确定 workflow_id 可再次入队，由 Temporal 启动策略兜底去重。
    op.create_index(
        "uq_cover_outbox_pending_workflow_id",
        "cover_outbox",
        ["workflow_id"],
        unique=True,
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table(COVER_OUTBOX_TABLE)
