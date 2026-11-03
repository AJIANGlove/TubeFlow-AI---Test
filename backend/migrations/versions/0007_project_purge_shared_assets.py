"""Keep project-scoped media ownership internally consistent."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE media_assets SET ownership_scope = 'OWNER' "
        "WHERE shared IS TRUE AND owner_id IS NOT NULL AND project_id IS NULL "
        "AND ownership_scope = 'PROJECT'"
    )
    op.create_check_constraint(
        op.f("ck_media_assets_project_scope_requires_project"),
        "media_assets",
        "ownership_scope != 'PROJECT' OR project_id IS NOT NULL",
    )
    op.alter_column("project_purge_objects", "asset_id", nullable=True)
    op.alter_column("project_purge_objects", "sha256", nullable=True)
    op.add_column("project_purge_objects", sa.Column("staged_object_id", postgresql.UUID()))
    op.add_column(
        "project_purge_objects",
        sa.Column("object_kind", sa.String(16), nullable=False, server_default="MEDIA"),
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM project_purge_objects "
        "WHERE object_kind = 'STAGED' OR asset_id IS NULL OR sha256 IS NULL"
    )
    op.drop_column("project_purge_objects", "object_kind")
    op.drop_column("project_purge_objects", "staged_object_id")
    op.alter_column("project_purge_objects", "sha256", nullable=False)
    op.alter_column("project_purge_objects", "asset_id", nullable=False)
    op.drop_constraint(
        op.f("ck_media_assets_project_scope_requires_project"),
        "media_assets",
        type_="check",
    )
