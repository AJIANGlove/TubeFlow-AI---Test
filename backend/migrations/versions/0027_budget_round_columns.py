"""Per-round budget columns: video_generation_attempts.budget_round and composition_outbox.budget_round.

AUTO pipeline resume (§15.9) resets the per-object submission budget by
counting only attempts inside the current projects.auto_budget_round.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "video_generation_attempts",
        sa.Column(
            "budget_round",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "composition_outbox",
        sa.Column(
            "budget_round",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("composition_outbox", "budget_round")
    op.drop_column("video_generation_attempts", "budget_round")
