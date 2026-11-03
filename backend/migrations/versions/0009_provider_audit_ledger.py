"""Add immutable H3 provider evidence ledger.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_audit_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("video_generation_attempts.attempt_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(128), nullable=False),
        sa.Column("workflow_version", sa.String(64), nullable=False),
        sa.Column("model_key", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("local_idempotency_key", sa.String(128), nullable=False),
        sa.Column("provider_task_key", sa.Text()),
        sa.Column("request_sha256", sa.String(64)),
        sa.Column("receipt_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("receipt_sha256", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.CheckConstraint(
            "event_sequence > 0",
            name="provider_audit_positive_sequence",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_provider_audit_events"),
        sa.UniqueConstraint(
            "attempt_id", "event_sequence", name="uq_provider_audit_attempt_sequence"
        ),
    )
    for column in ("attempt_id", "owner_id", "project_id", "group_id"):
        op.create_index(f"ix_provider_audit_events_{column}", "provider_audit_events", [column])
    op.execute("""
        CREATE FUNCTION reject_provider_audit_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'provider_audit_events is append-only'; END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER provider_audit_events_append_only
        BEFORE UPDATE OR DELETE ON provider_audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_provider_audit_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER provider_audit_events_append_only ON provider_audit_events")
    op.execute("DROP FUNCTION reject_provider_audit_mutation()")
    op.drop_table("provider_audit_events")
