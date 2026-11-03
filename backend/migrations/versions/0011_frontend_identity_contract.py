"""Add project management titles and channel-scoped character assets.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("title", sa.String(length=200), nullable=False))
    op.add_column(
        "character_images",
        sa.Column("channel_id", sa.UUID(), nullable=False),
    )
    op.create_foreign_key(
        "fk_character_images_channel_id_channels",
        "character_images",
        "channels",
        ["channel_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_character_images_channel_id", "character_images", ["channel_id"])
    op.drop_constraint("uq_character_images_owner_sha256", "character_images", type_="unique")
    op.create_unique_constraint(
        "uq_character_images_channel_sha256", "character_images", ["channel_id", "sha256"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_character_images_channel_sha256", "character_images", type_="unique")
    op.create_unique_constraint(
        "uq_character_images_owner_sha256", "character_images", ["owner_id", "sha256"]
    )
    op.drop_index("ix_character_images_channel_id", table_name="character_images")
    op.drop_constraint(
        "fk_character_images_channel_id_channels", "character_images", type_="foreignkey"
    )
    op.drop_column("character_images", "channel_id")
    op.drop_column("projects", "title")
