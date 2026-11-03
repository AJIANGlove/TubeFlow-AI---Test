"""Add ElevenLabs key pool tables.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "elevenlabs_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quota_tier", sa.Text(), nullable=True),
        sa.Column("character_count", sa.BigInteger(), nullable=True),
        sa.Column("character_limit", sa.BigInteger(), nullable=True),
        sa.Column("voice_slots_used", sa.Integer(), nullable=True),
        sa.Column("voice_limit", sa.Integer(), nullable=True),
        sa.Column("quota_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("api_key", name="uq_elevenlabs_keys_api_key"),
    )
    op.create_table(
        "elevenlabs_key_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("elevenlabs_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_elevenlabs_key_events_key_id", "elevenlabs_key_events", ["key_id"])


def downgrade() -> None:
    op.drop_table("elevenlabs_key_events")
    op.drop_table("elevenlabs_keys")
