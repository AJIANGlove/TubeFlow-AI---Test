"""Add channel default visual style.

Revision ID: 0038
Revises: 0037
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 空串 = 未设置（与 default_character_image_ids 用 [] 表示未设置同先例）：
    # 只作为未来新建项目的默认值来源，不进入项目快照。
    op.add_column(
        "channels",
        sa.Column(
            "default_visual_style",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("channels", "default_visual_style")
