"""Add cover character cards, style profile labels and prompt card lineage.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

COVER_CHARACTER_CARDS_TABLE = "cover_character_cards"


def upgrade() -> None:
    op.create_table(
        "cover_character_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "character_image_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("character_images.id"),
            nullable=False,
        ),
        sa.Column("identity_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("appearance_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'analysis')",
            name="source",
        ),
        sa.UniqueConstraint(
            "channel_id",
            "character_image_id",
            name="uq_cover_character_cards_channel_image",
        ),
    )
    op.create_index(
        "ix_cover_character_cards_owner_id", "cover_character_cards", ["owner_id"]
    )
    op.create_index(
        "ix_cover_character_cards_channel_id", "cover_character_cards", ["channel_id"]
    )

    op.add_column(
        "cover_style_profiles",
        sa.Column("label", sa.String(length=60), nullable=False, server_default=""),
    )
    op.add_column(
        "cover_prompt_generations",
        sa.Column(
            "character_cards_used",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("cover_prompt_generations", "character_cards_used")
    op.drop_column("cover_style_profiles", "label")
    op.drop_table(COVER_CHARACTER_CARDS_TABLE)
