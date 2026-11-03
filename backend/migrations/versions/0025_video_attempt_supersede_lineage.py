"""Add superseded_attempt_id lineage column to video_generation_attempts.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_generation_attempts",
        sa.Column("superseded_attempt_id", PGUUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_video_generation_attempts_superseded_attempt_id",
        "video_generation_attempts",
        ["superseded_attempt_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_video_generation_attempts_superseded_attempt_id",
        table_name="video_generation_attempts",
    )
    op.drop_column("video_generation_attempts", "superseded_attempt_id")
