"""Bind H3 reference policy separately from node workflow routing.

Revision ID: 0046
Revises: 0045

Existing projects with attempts remain on the composite storyboard path. Projects
without attempts stay unbound (NULL) until their first real video intent.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_CHECK_NAME = "ck_h3_project_workflow_routes_reference_strategy"


def upgrade() -> None:
    op.add_column(
        "h3_project_workflow_routes",
        sa.Column("reference_strategy", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "h3_project_workflow_routes",
        sa.Column(
            "reference_derivation_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.execute(sa.text(
        "UPDATE h3_project_workflow_routes SET reference_strategy = 'composite' "
        "WHERE project_id IN (SELECT DISTINCT project_id FROM video_generation_attempts)"
    ))
    op.create_check_constraint(
        op.f(_CHECK_NAME),
        "h3_project_workflow_routes",
        "reference_strategy IS NULL OR reference_strategy IN ('composite', 'single_panel_v1')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    single_panel_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM h3_project_workflow_routes "
            "WHERE reference_strategy IS NOT NULL AND reference_strategy <> 'composite'"
        )
    )
    derivation_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM h3_project_workflow_routes "
            "WHERE reference_derivation_json IS NOT NULL"
        )
    )
    if single_panel_count or derivation_count:
        raise RuntimeError(
            "Cannot downgrade while a single-panel reference policy or derivation is bound"
        )
    op.drop_constraint(op.f(_CHECK_NAME), "h3_project_workflow_routes", type_="check")
    op.drop_column("h3_project_workflow_routes", "reference_derivation_json")
    op.drop_column("h3_project_workflow_routes", "reference_strategy")
