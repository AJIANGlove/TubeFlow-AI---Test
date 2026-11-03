"""Add multi-item channel cover templates (2026-10-27).

Revision ID: 0044
Revises: 0043

新结构：一个频道多个模板，每行「封面 + 提示词 + 名称 + 顺序」。
旧表 `channel_cover_templates` 冻结保留（回滚用），现有行复制为一条「模板 1」。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_cover_template_items",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "channel_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
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
        sa.Column("last_save_key", sa.String(length=128), nullable=True),
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
    )
    op.create_index(
        "ix_channel_cover_template_items_channel_position",
        "channel_cover_template_items",
        ["channel_id", "position"],
    )
    # 旧结构每频道一行：迁成一条「模板 1」，只保留 prompt_a（用户确认 8a）。
    op.execute(
        """
        INSERT INTO channel_cover_template_items (
            id, channel_id, name, position, prompt,
            cover_original, cover_original_media_type, cover_original_format,
            cover_original_width, cover_original_height, cover_original_byte_size,
            cover_original_sha256,
            cover_preview, cover_preview_media_type, cover_preview_format,
            cover_preview_width, cover_preview_height, cover_preview_byte_size,
            cover_preview_sha256, created_at, updated_at
        )
        SELECT
            gen_random_uuid(), channel_id, '模板 1', 0, prompt_a,
            cover_original, cover_original_media_type, cover_original_format,
            cover_original_width, cover_original_height, cover_original_byte_size,
            cover_original_sha256,
            cover_preview, cover_preview_media_type, cover_preview_format,
            cover_preview_width, cover_preview_height, cover_preview_byte_size,
            cover_preview_sha256,
            COALESCE(updated_at, now()), COALESCE(updated_at, now())
        FROM channel_cover_templates
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_cover_template_items_channel_position",
        table_name="channel_cover_template_items",
    )
    op.drop_table("channel_cover_template_items")
