"""Track H3 remote phase change time and stall detection state.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_generation_attempts",
        sa.Column("remote_phase_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "video_generation_attempts",
        sa.Column("stalled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("video_generation_attempts", "stalled_at")
    op.drop_column("video_generation_attempts", "remote_phase_changed_at")
