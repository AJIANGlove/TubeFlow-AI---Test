"""Add channel cover templates (temporary feature, 2026-10-21).

Revision ID: 0041
Revises: 0040

频道级封面模板：一频道一行，封面原图 + 压缩图（BYTEA）+ 三段提示词。
删除功能时执行 downgrade 即可整表移除。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_cover_templates",
        sa.Column(
            "channel_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("cover_original", sa.LargeBinary(), nullable=True),
        sa.Column("cover_original_media_type", sa.String(length=64), nullable=True),
        sa.Column("cover_original_format", sa.String(length=16), nullable=True),
        sa.Column("cover_original_width", sa.Integer(), nullable=True),
        sa.Column("cover_original_height", sa.Integer(), nullable=True),
        sa.Column("cover_original_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("cover_original_sha256", sa.String(length=64), nullable=True),
        sa.Column("cover_preview", sa.LargeBinary(), nullable=True),
        sa.Column("cover_preview_media_type", sa.String(length=64), nullable=True),
        sa.Column("cover_preview_format", sa.String(length=16), nullable=True),
        sa.Column("cover_preview_width", sa.Integer(), nullable=True),
        sa.Column("cover_preview_height", sa.Integer(), nullable=True),
        sa.Column("cover_preview_byte_size", sa.BigInteger(), nullable=True),
        sa.Column("cover_preview_sha256", sa.String(length=64), nullable=True),
        sa.Column("prompt_a", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_b", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_c", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_cover_templates")
