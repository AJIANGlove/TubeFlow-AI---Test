"""Add the fixed public voice library channel contract.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "voice_versions",
        "raw_language_codes",
        existing_type=postgresql.ARRAY(sa.String(length=4)),
        type_=postgresql.ARRAY(sa.String(length=16)),
        existing_nullable=False,
    )
    op.add_column(
        "channels",
        sa.Column("default_voice_version_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_channels_default_voice_version_id_voice_versions",
        "channels",
        "voice_versions",
        ["default_voice_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_channels_default_voice_version_id",
        "channels",
        ["default_voice_version_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "0013 is intentionally irreversible because narrowing raw language codes would lose data"
    )
