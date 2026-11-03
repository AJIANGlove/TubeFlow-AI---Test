"""Bind provider audit identity to its H3 attempt.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_generation_attempts",
        sa.Column("cancel_dispatched", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "video_generation_attempts",
        sa.Column("cancel_idempotency_key", sa.String(length=128)),
    )
    op.add_column(
        "video_generation_attempts",
        sa.Column("cancel_inspect_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "video_generation_attempts",
        sa.Column(
            "archive_confirm_dispatched", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column(
        "video_generation_attempts",
        sa.Column("archive_confirm_idempotency_key", sa.String(length=128)),
    )
    op.add_column(
        "video_generation_attempts",
        sa.Column(
            "archive_confirm_inspect_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.alter_column(
        "provider_audit_events",
        "event_type",
        type_=sa.String(length=64),
        existing_type=sa.String(length=32),
    )
    op.alter_column(
        "provider_audit_events",
        "schema_version",
        type_=sa.String(length=512),
        existing_type=sa.String(length=64),
    )
    op.create_unique_constraint(
        "uq_video_attempt_audit_identity",
        "video_generation_attempts",
        ["attempt_id", "owner_id", "project_id", "group_id"],
    )
    op.drop_constraint(
        "provider_audit_events_attempt_id_fkey", "provider_audit_events", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_provider_audit_attempt_identity",
        "provider_audit_events",
        "video_generation_attempts",
        ["attempt_id", "owner_id", "project_id", "group_id"],
        ["attempt_id", "owner_id", "project_id", "group_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.alter_column(
        "provider_audit_events",
        "schema_version",
        type_=sa.String(length=64),
        existing_type=sa.String(length=512),
    )
    op.alter_column(
        "provider_audit_events",
        "event_type",
        type_=sa.String(length=32),
        existing_type=sa.String(length=64),
    )
    op.drop_constraint(
        "fk_provider_audit_attempt_identity", "provider_audit_events", type_="foreignkey"
    )
    op.create_foreign_key(
        "provider_audit_events_attempt_id_fkey",
        "provider_audit_events",
        "video_generation_attempts",
        ["attempt_id"],
        ["attempt_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_video_attempt_audit_identity", "video_generation_attempts", type_="unique"
    )
    op.drop_column("video_generation_attempts", "cancel_idempotency_key")
    op.drop_column("video_generation_attempts", "cancel_inspect_count")
    op.drop_column("video_generation_attempts", "cancel_dispatched")
    op.drop_column("video_generation_attempts", "archive_confirm_inspect_count")
    op.drop_column("video_generation_attempts", "archive_confirm_idempotency_key")
    op.drop_column("video_generation_attempts", "archive_confirm_dispatched")
