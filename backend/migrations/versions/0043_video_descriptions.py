"""Add STEP 11 video description templates and immutable versions.

Revision ID: 0043
Revises: 0042

模板首次访问频道页面时由服务层幂等初始化，因此本迁移不回填提示词正文；
版本计数表保证模板硬删除后版本号不重用。项目简介版本随 projects CASCADE
清理。降级遇到已生成简介时 fail-closed，避免静默丢弃历史。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_description_template_counters",
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("next_version", sa.Integer(), nullable=False, server_default="2"),
        sa.CheckConstraint("next_version > 1", name="next_version"),
    )
    op.create_table(
        "channel_description_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("version > 0", name="version"),
        sa.UniqueConstraint(
            "channel_id", "version", name="uq_channel_description_templates_version"
        ),
    )
    op.create_index(
        "ix_channel_description_templates_channel",
        "channel_description_templates",
        ["channel_id"],
    )
    op.create_table(
        "project_descriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("base_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("prompt_template_version", sa.Integer(), nullable=True),
        sa.Column("prompt_template_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_template_snapshot", sa.Text(), nullable=False),
        sa.Column("title_used", sa.Text(), nullable=False),
        sa.Column("channel_name_used", sa.Text(), nullable=False),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column("srt_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("srt_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("disclaimer_key", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("provider_receipt", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw_response_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("provider_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("version_number > 0", name="version_number"),
        sa.CheckConstraint("source IN ('generated', 'manual')", name="source"),
        sa.UniqueConstraint(
            "project_id", "version_number", name="uq_project_descriptions_version"
        ),
    )
    op.create_index(
        "ix_project_descriptions_project",
        "project_descriptions",
        ["project_id", "version_number"],
    )
    op.create_index(
        "uq_project_descriptions_current",
        "project_descriptions",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("current"),
        sqlite_where=sa.text("\"current\" = 1"),
    )
    op.create_table(
        "project_description_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="RUNNING"),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("description_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')", name="status"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_project_description_attempts_key"
        ),
    )
    op.create_index(
        "ix_project_description_attempts_project",
        "project_description_attempts",
        ["project_id", "created_at"],
    )
    op.create_index(
        "uq_description_attempt_active_project",
        "project_description_attempts",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
        sqlite_where=sa.text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM project_descriptions"))
    if count:
        raise RuntimeError(
            "Cannot downgrade 0043 while project description versions exist. "
            "Resolve them first: DELETE FROM project_description_attempts; "
            "DELETE FROM project_descriptions;"
        )
    op.drop_index(
        "uq_description_attempt_active_project",
        table_name="project_description_attempts",
        postgresql_where=sa.text("status = 'RUNNING'"),
        sqlite_where=sa.text("status = 'RUNNING'"),
    )
    op.drop_index(
        "ix_project_description_attempts_project", table_name="project_description_attempts"
    )
    op.drop_table("project_description_attempts")
    op.drop_index(
        "uq_project_descriptions_current",
        table_name="project_descriptions",
        postgresql_where=sa.text("current"),
        sqlite_where=sa.text("\"current\" = 1"),
    )
    op.drop_index("ix_project_descriptions_project", table_name="project_descriptions")
    op.drop_table("project_descriptions")
    op.drop_index(
        "ix_channel_description_templates_channel", table_name="channel_description_templates"
    )
    op.drop_table("channel_description_templates")
    op.drop_table("channel_description_template_counters")
