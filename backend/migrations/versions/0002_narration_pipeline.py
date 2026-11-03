"""Add durable narration attempts, event history, and authoritative project timeline.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "narration_attempt_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_narration_attempt_records_owner_key"
        ),
    )
    op.create_index(
        "ix_narration_attempt_records_owner_id", "narration_attempt_records", ["owner_id"]
    )
    op.create_index(
        "ix_narration_attempt_records_project_id", "narration_attempt_records", ["project_id"]
    )
    op.create_table(
        "narration_project_records",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_narration_project_records_owner_id", "narration_project_records", ["owner_id"]
    )
    op.create_table(
        "narration_attempt_event_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_narration_attempt_event_records_owner_id",
        "narration_attempt_event_records",
        ["owner_id"],
    )
    op.create_index(
        "ix_narration_attempt_event_records_project_id",
        "narration_attempt_event_records",
        ["project_id"],
    )
    op.create_index(
        "ix_narration_attempt_event_records_idempotency_key",
        "narration_attempt_event_records",
        ["idempotency_key"],
    )
    op.create_table(
        "authoritative_narration_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_revision", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("start_frame", sa.BigInteger(), nullable=False),
        sa.Column("end_frame", sa.BigInteger(), nullable=False),
        sa.Column("sample_rate_hz", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("video_blocked", sa.Boolean(), nullable=False),
        sa.Column("block_reason", sa.String(128)),
        sa.UniqueConstraint(
            "project_id", "snapshot_revision", "sequence",
            name="uq_narration_groups_project_revision_sequence",
        ),
    )
    op.create_index(
        "ix_authoritative_narration_groups_project_id",
        "authoritative_narration_groups",
        ["project_id"],
    )
    op.create_index(
        "ix_authoritative_narration_groups_owner_id", "authoritative_narration_groups", ["owner_id"]
    )
    op.create_table(
        "authoritative_narration_sentences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_revision", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column(
            "raw_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_assets.id"),
            nullable=False,
        ),
        sa.Column(
            "normalized_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("media_assets.id"),
            nullable=False,
        ),
        sa.Column("raw_qc_passed", sa.Boolean(), nullable=False),
        sa.Column("normalized_qc_passed", sa.Boolean(), nullable=False),
        sa.Column("measured_frame_count", sa.BigInteger(), nullable=False),
        sa.Column("sample_rate_hz", sa.Integer(), nullable=False),
        sa.Column("measured_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("start_frame", sa.BigInteger(), nullable=False),
        sa.Column("end_frame", sa.BigInteger(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "project_id", "snapshot_revision", "sequence",
            name="uq_narration_sentences_project_revision_sequence",
        ),
    )
    op.create_index(
        "ix_authoritative_narration_sentences_project_id",
        "authoritative_narration_sentences",
        ["project_id"],
    )
    op.create_index(
        "ix_authoritative_narration_sentences_owner_id",
        "authoritative_narration_sentences",
        ["owner_id"],
    )
    op.create_table(
        "authoritative_narration_memberships",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "sentence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("authoritative_narration_sentences.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "sentence_id", name="uq_authoritative_narration_memberships_sentence_id"
        ),
        sa.UniqueConstraint(
            "group_id", "ordinal", name="uq_authoritative_narration_memberships_group_ordinal"
        ),
    )


def downgrade() -> None:
    op.drop_table("authoritative_narration_memberships")
    op.drop_table("authoritative_narration_sentences")
    op.drop_table("authoritative_narration_groups")
    op.drop_table("narration_attempt_event_records")
    op.drop_table("narration_project_records")
    op.drop_table("narration_attempt_records")
