"""Add ElevenLabs cloned voices and per-project TTS provider.

Revision ID: 0035
Revises: 0034

决策依据：`docs/planning/USER_DECISIONS.md` §23.1–§23.5、§23.10。
- 频道保存默认配音源与该源下的默认音色（CosyVoice 公共音色 / EL 克隆音色二选一）。
- 克隆音色频道共享，源音频为频道级资产（无 TTL），账户映射按号池 key 逐账号记录。
- 项目与快照冻结配音源；EL 分支只冻结逻辑音色事实（id/名称/源音频 sha256），
  不要求 CosyVoice 的 reference/preview/avatar 三件媒体事实。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cloned_voices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channels.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_object_key", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_media_type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", "name", name="uq_cloned_voices_channel_name"),
    )
    op.create_index("ix_cloned_voices_channel_id", "cloned_voices", ["channel_id"])
    op.create_index("ix_cloned_voices_owner_id", "cloned_voices", ["owner_id"])
    op.create_table(
        "cloned_voice_accounts",
        sa.Column(
            "cloned_voice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cloned_voices.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "pool_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("elevenlabs_keys.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("el_voice_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_cloned_voice_accounts_pool_key_id", "cloned_voice_accounts", ["pool_key_id"]
    )
    op.add_column(
        "channels",
        sa.Column(
            "default_tts_provider",
            sa.Text(),
            nullable=False,
            server_default="cosyvoice",
        ),
    )
    op.add_column(
        "channels",
        sa.Column("default_cloned_voice_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # 刻意不加 FK：channels ↔ cloned_voices 互为引用会形成 SQLAlchemy 无法解析的
    # 建表环（既有先例：channels.current_cover_style_profile_id、
    # projects.snapshot_voice_version_id 亦为无 FK 指针列）。归属与保护由领域层保证：
    # 服务层只接受本频道的克隆音色，删除保护三个引用计数之一即本列。
    op.create_index(
        "ix_channels_default_cloned_voice_id", "channels", ["default_cloned_voice_id"]
    )
    op.add_column(
        "projects",
        sa.Column("tts_provider", sa.Text(), nullable=False, server_default="cosyvoice"),
    )
    op.add_column(
        "projects",
        sa.Column("cloned_voice_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_cloned_voice_id_cloned_voices",
        "projects",
        "cloned_voices",
        ["cloned_voice_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_projects_cloned_voice_id", "projects", ["cloned_voice_id"])
    # EL 项目没有 CosyVoice 公共音色；快照改用逻辑克隆音色事实。
    op.alter_column("projects", "voice_version_id", existing_type=postgresql.UUID(), nullable=True)
    op.add_column("projects", sa.Column("snapshot_tts_provider", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("snapshot_cloned_voice_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "projects", sa.Column("snapshot_cloned_voice_name", sa.Text(), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("snapshot_cloned_voice_source_sha256", sa.String(64), nullable=True),
    )
    # EL request-id 对账凭证（拼接 previous_request_ids 的事实来源）。
    op.add_column(
        "narration_attempt_records",
        sa.Column("provider_request_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("narration_attempt_records", "provider_request_id")
    op.drop_column("projects", "snapshot_cloned_voice_source_sha256")
    op.drop_column("projects", "snapshot_cloned_voice_name")
    op.drop_column("projects", "snapshot_cloned_voice_id")
    op.drop_column("projects", "snapshot_tts_provider")
    op.alter_column("projects", "voice_version_id", existing_type=postgresql.UUID(), nullable=False)
    op.drop_index("ix_projects_cloned_voice_id", table_name="projects")
    op.drop_constraint("fk_projects_cloned_voice_id_cloned_voices", "projects", type_="foreignkey")
    op.drop_column("projects", "cloned_voice_id")
    op.drop_column("projects", "tts_provider")
    op.drop_index("ix_channels_default_cloned_voice_id", table_name="channels")
    op.drop_column("channels", "default_cloned_voice_id")
    op.drop_column("channels", "default_tts_provider")
    op.drop_index("ix_cloned_voice_accounts_pool_key_id", table_name="cloned_voice_accounts")
    op.drop_table("cloned_voice_accounts")
    op.drop_index("ix_cloned_voices_owner_id", table_name="cloned_voices")
    op.drop_index("ix_cloned_voices_channel_id", table_name="cloned_voices")
    op.drop_table("cloned_voices")
