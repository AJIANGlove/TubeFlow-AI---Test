"""Add video prompt compilation diagnostics (2026-10-27).

Revision ID: 0045
Revises: 0044

提示词编译失败此前没有任何落库证据（组级 attempt 根本不会创建），导致
`h3_prompt_storyboard_retention_invalid` 这类失败只能靠重跑猜原因。
本表按「每次尝试一行」记录策略、模型、原因码与模型原始输出，作为诊断证据，
不参与任何业务判定。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_prompt_compilations",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("project_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("group_id", PGUUID(as_uuid=True), nullable=True),
        sa.Column("attempt_id", PGUUID(as_uuid=True), nullable=True),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_video_prompt_compilations_project_created",
        "video_prompt_compilations",
        ["project_id", "group_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_video_prompt_compilations_project_created",
        table_name="video_prompt_compilations",
    )
    op.drop_table("video_prompt_compilations")
