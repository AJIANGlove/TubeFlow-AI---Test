"""Persist bounded H3 outbox delivery state.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_video_attempt_active_group",
        "video_generation_attempts",
        ["owner_id", "group_id"],
        unique=True,
        postgresql_where=sa.text(
            "stage IN ('CREATED','NOT_SUBMITTED','STAGING','SUBMITTING',"
            "'REMOTE_STATE_UNKNOWN','SUBMITTED','RUNNING')"
        ),
    )
    op.add_column(
        "video_generation_outbox",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("video_generation_outbox", sa.Column("last_error", sa.String(128)))
    op.add_column(
        "video_generation_outbox",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE video_generation_outbox SET next_attempt_at = created_at")
    op.alter_column("video_generation_outbox", "next_attempt_at", nullable=False)
    op.add_column("video_generation_outbox", sa.Column("terminal_state", sa.String(16)))
    op.create_check_constraint(
        op.f("ck_video_outbox_terminal_state"),
        "video_generation_outbox",
        "terminal_state IS NULL OR terminal_state IN ('UNKNOWN', 'DISPATCHED', 'DEAD_LETTER')",
    )
    op.create_index(
        "ix_video_outbox_delivery_due",
        "video_generation_outbox",
        ["terminal_state", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("uq_video_attempt_active_group", table_name="video_generation_attempts")
    op.drop_index("ix_video_outbox_delivery_due", table_name="video_generation_outbox")
    op.drop_constraint(
        op.f("ck_video_outbox_terminal_state"),
        "video_generation_outbox",
        type_="check",
    )
    op.drop_column("video_generation_outbox", "terminal_state")
    op.drop_column("video_generation_outbox", "next_attempt_at")
    op.drop_column("video_generation_outbox", "last_error")
    op.drop_column("video_generation_outbox", "retry_count")
