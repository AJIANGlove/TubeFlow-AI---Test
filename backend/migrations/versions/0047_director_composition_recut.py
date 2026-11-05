"""Record system-approved non-adjacent composition recuts on director revisions.

Revision ID: 0047
Revises: 0046

USER_DECISIONS §48.3: AUTO projects may keep one non-adjacent composition repeat per
group when the recut conditions hold. The approval is stored on the immutable revision
next to the existing continuous-observation approvals. Existing rows get an empty list,
so their validation is unchanged.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storyboard_director_revisions",
        sa.Column(
            "composition_recut_approvals_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("storyboard_director_revisions", "composition_recut_approvals_json")
