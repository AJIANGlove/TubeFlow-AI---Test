"""Durable job rows for asynchronous batch H3 intent creation.

Revision ID: 0019
Revises: 0018

A batch job is created by one explicit user click; items are processed
sequentially by a web-tier background runner with per-group idempotency keys
``batch-{job_id}-{group_id}`` so an interrupted job can safely resume.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_batch_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("accounts.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("force", sa.Boolean(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "video_batch_items",
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("video_batch_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("group_id", sa.Uuid(), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(48), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("message", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("video_batch_items")
    op.drop_table("video_batch_jobs")
