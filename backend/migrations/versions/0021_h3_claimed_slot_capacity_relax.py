"""Relax the H3 claimed-slot capacity check so stalled slots can be re-lent.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def _claimed_capacity_check_names() -> list[str]:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'h3_engine_nodes'::regclass "
            "AND conname LIKE '%claimed%capacity%'"
        )
    )
    return [row[0] for row in rows]


def upgrade() -> None:
    for name in _claimed_capacity_check_names():
        op.execute(sa.text(f'ALTER TABLE h3_engine_nodes DROP CONSTRAINT "{name}"'))
    op.create_check_constraint("claimed_capacity", "h3_engine_nodes", "claimed_slots >= 0")


def downgrade() -> None:
    for name in _claimed_capacity_check_names():
        op.execute(sa.text(f'ALTER TABLE h3_engine_nodes DROP CONSTRAINT "{name}"'))
    op.create_check_constraint(
        "claimed_capacity",
        "h3_engine_nodes",
        "claimed_slots >= 0 AND claimed_slots <= capacity",
    )
