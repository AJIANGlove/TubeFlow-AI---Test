"""Allow user-uploaded cover image versions.

Revision ID: 0042
Revises: 0041

封面图版本新增来源列：``generated``（AI 出图，历史行默认）与 ``upload``
（用户直接上传）。上传版本没有提示词血缘，因此 ``prompt_generation_id``、
``model``、``size``、``quality`` 放宽为可空；``prompt`` 仍非空，上传行写空串。

降级为 fail-closed：存在上传版本时拒绝降级，避免静默丢弃用户封面历史。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cover_image_generations",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="generated"),
    )
    op.create_check_constraint(
        "source",
        "cover_image_generations",
        "source IN ('generated', 'upload')",
    )
    op.alter_column(
        "cover_image_generations",
        "prompt_generation_id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column(
        "cover_image_generations",
        "model",
        existing_type=sa.String(length=128),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column(
        "cover_image_generations",
        "size",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column(
        "cover_image_generations",
        "quality",
        existing_type=sa.String(length=16),
        existing_nullable=False,
        nullable=True,
    )


def downgrade() -> None:
    upload_rows = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM cover_image_generations WHERE source = 'upload'")
    )
    if upload_rows:
        raise RuntimeError(
            "Cannot downgrade 0042 while uploaded cover versions exist. "
            "Resolve them first: "
            "UPDATE projects SET current_cover_image_generation_id = NULL "
            "WHERE current_cover_image_generation_id IN "
            "(SELECT id FROM cover_image_generations WHERE source = 'upload'); "
            "DELETE FROM cover_image_generations WHERE source = 'upload';"
        )
    op.alter_column(
        "cover_image_generations",
        "quality",
        existing_type=sa.String(length=16),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "cover_image_generations",
        "size",
        existing_type=sa.String(length=16),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "cover_image_generations",
        "model",
        existing_type=sa.String(length=128),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "cover_image_generations",
        "prompt_generation_id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=True,
        nullable=False,
    )
    op.drop_constraint("source", "cover_image_generations", type_="check")
    op.drop_column("cover_image_generations", "source")
