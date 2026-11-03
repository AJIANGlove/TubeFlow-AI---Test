# ruff: noqa: E501
"""Durable group-video attempts and H3 node capacity.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "h3_engine_nodes",
        sa.Column("node_id", sa.String(64), primary_key=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("instance_identity", sa.String(128), nullable=False),
        sa.Column("token_secret_ref", sa.Text(), nullable=False),
        sa.Column("token_sha256", sa.String(64), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("claimed_slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("health", sa.String(16), nullable=False, server_default="READY"),
        sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("model_key", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("workflow_id", sa.String(128), nullable=False),
        sa.Column("workflow_version", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint("capacity > 0", name="positive_capacity"),
        sa.CheckConstraint(
            "claimed_slots >= 0 AND claimed_slots <= capacity",
            name="claimed_capacity",
        ),
    )
    op.create_table(
        "video_generation_attempts",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("storyboard_generations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("generation_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("request_json", postgresql.JSONB(), nullable=False),
        sa.Column("node_id", sa.String(64), sa.ForeignKey("h3_engine_nodes.node_id")),
        sa.Column("remote_id", sa.Text()),
        sa.Column("remote_phase", sa.String(32)),
        sa.Column("remote_acceptance", sa.String(32)),
        sa.Column("raw_receipt_json", postgresql.JSONB()),
        sa.Column("requested_action", sa.String(32)),
        sa.Column("cleanup_terminal_stage", sa.String(32)),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("staged_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("cleaned_staged_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("archive_stage", sa.String(32), nullable=False),
        sa.Column("output_id", sa.Text()),
        sa.Column("output_sha256", sa.String(64)),
        sa.Column("output_size_bytes", sa.BigInteger()),
        sa.Column("output_media_type", sa.String(64)),
        sa.Column("archived_asset_json", postgresql.JSONB()),
        sa.Column("input_snapshot_json", postgresql.JSONB()),
        sa.Column("probe_evidence_json", postgresql.JSONB()),
        sa.Column("error_code", sa.String(128)),
        sa.Column("retry_evidence", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("slot_claimed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("slot_lease_token", sa.String(64)),
        sa.Column("slot_lease_owner", sa.String(128)),
        sa.Column("slot_lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("narration_validation_json", postgresql.JSONB()),
        sa.Column("intent_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_video_attempt_owner_key"),
        sa.UniqueConstraint("attempt_id", "output_sha256", name="uq_video_attempt_output"),
    )
    for column in ("owner_id", "project_id", "group_id", "generation_id", "node_id"):
        op.create_index(
            f"ix_video_generation_attempts_{column}", "video_generation_attempts", [column]
        )
    op.create_table(
        "video_generation_outbox",
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("video_generation_attempts.attempt_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_video_generation_outbox_lease_token", "video_generation_outbox", ["lease_token"])
    op.create_table(
        "group_current_videos",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("video_generation_attempts.attempt_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intent_sequence", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("group_current_videos")
    op.drop_table("video_generation_outbox")
    op.drop_table("video_generation_attempts")
    op.drop_table("h3_engine_nodes")
