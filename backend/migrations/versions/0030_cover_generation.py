"""Add cover generation tables and current-cover pointer columns.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

COVER_TABLES = (
    "cover_image_generations",
    "cover_prompt_generations",
    "cover_style_profiles",
    "cover_style_drafts",
    "cover_ref_images",
)


def upgrade() -> None:
    op.create_table(
        "cover_ref_images",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("image_format", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "scope IN ('channel', 'project')",
            name="scope",
        ),
        sa.UniqueConstraint("channel_id", "sha256", name="uq_cover_ref_images_channel_sha256"),
        sa.UniqueConstraint("project_id", "sha256", name="uq_cover_ref_images_project_sha256"),
    )
    op.create_index("ix_cover_ref_images_owner_id", "cover_ref_images", ["owner_id"])
    op.create_index("ix_cover_ref_images_channel_id", "cover_ref_images", ["channel_id"])
    op.create_index("ix_cover_ref_images_project_id", "cover_ref_images", ["project_id"])

    op.create_table(
        "cover_style_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "ref_image_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cover_ref_images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "dims",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "analysis",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "analysis_dims",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "ref_image_sha256", sa.String(length=64), nullable=False, server_default=""
        ),
        sa.Column("edited_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "scope IN ('channel', 'project')",
            name="scope",
        ),
        sa.UniqueConstraint("channel_id", name="uq_cover_style_drafts_channel_id"),
        sa.UniqueConstraint("project_id", name="uq_cover_style_drafts_project_id"),
    )
    op.create_index("ix_cover_style_drafts_owner_id", "cover_style_drafts", ["owner_id"])
    op.create_index("ix_cover_style_drafts_channel_id", "cover_style_drafts", ["channel_id"])
    op.create_index("ix_cover_style_drafts_project_id", "cover_style_drafts", ["project_id"])

    op.create_table(
        "cover_style_profiles",
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
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "ref_image_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cover_ref_images.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "dims",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "analysis_dims",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("edited_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cover_style_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "channel_id", "version", name="uq_cover_style_profiles_channel_version"
        ),
    )
    op.create_index("ix_cover_style_profiles_owner_id", "cover_style_profiles", ["owner_id"])
    op.create_index(
        "ix_cover_style_profiles_channel_id", "cover_style_profiles", ["channel_id"]
    )

    op.create_table(
        "cover_prompt_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("style_source", sa.String(length=32), nullable=False),
        sa.Column(
            "channel_style_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cover_style_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("style_text_used", sa.Text(), nullable=False),
        sa.Column(
            "style_dims_used",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("style_override_text", sa.Text(), nullable=True),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column(
            "cover_text_candidates",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("cover_text_default", sa.Text(), nullable=False, server_default=""),
        sa.Column("cover_text_used", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_body", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_system", sa.Text(), nullable=False, server_default=""),
        sa.Column("prompt_current", sa.Text(), nullable=False, server_default=""),
        sa.Column("title_used", sa.Text(), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "warnings",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "judge",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "parameters",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_stage", sa.String(length=32), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "style_source IN ('channel_profile', 'project_draft', 'none')",
            name="style_source",
        ),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_cover_prompt_generations_owner_key"
        ),
        sa.UniqueConstraint(
            "project_id", "version", name="uq_cover_prompt_generations_project_version"
        ),
    )
    op.create_index(
        "ix_cover_prompt_generations_owner_id", "cover_prompt_generations", ["owner_id"]
    )
    op.create_index(
        "ix_cover_prompt_generations_project_id", "cover_prompt_generations", ["project_id"]
    )

    op.create_table(
        "cover_image_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prompt_generation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cover_prompt_generations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("size", sa.String(length=16), nullable=False),
        sa.Column("quality", sa.String(length=16), nullable=False),
        sa.Column(
            "parameters",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("canonical_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_receipt", sa.Text(), nullable=True),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("downloaded_sha256", sa.String(length=64), nullable=True),
        sa.Column("downloaded_size", sa.Integer(), nullable=True),
        sa.Column("qc_width", sa.Integer(), nullable=True),
        sa.Column("qc_height", sa.Integer(), nullable=True),
        sa.Column("qc_mime", sa.String(length=64), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("object_sha256", sa.String(length=64), nullable=True),
        sa.Column("failure_stage", sa.String(length=32), nullable=True),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_cover_image_generations_owner_key"
        ),
        sa.UniqueConstraint(
            "project_id", "version", name="uq_cover_image_generations_project_version"
        ),
    )
    op.create_index(
        "ix_cover_image_generations_owner_id", "cover_image_generations", ["owner_id"]
    )
    op.create_index(
        "ix_cover_image_generations_project_id", "cover_image_generations", ["project_id"]
    )
    op.create_index(
        "ix_cover_image_generations_prompt_generation_id",
        "cover_image_generations",
        ["prompt_generation_id"],
    )

    op.add_column(
        "channels",
        sa.Column("current_cover_style_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "current_cover_prompt_generation_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "current_cover_image_generation_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "current_cover_image_generation_id")
    op.drop_column("projects", "current_cover_prompt_generation_id")
    op.drop_column("channels", "current_cover_style_profile_id")
    for table in COVER_TABLES:
        op.drop_table(table)
