"""Align staged_objects state check with CLEANUP_PENDING.

Revision ID: 0040
Revises: 0039
"""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_staged_objects_state"
_FULL_STATES = "'PENDING_UPLOAD','STAGED','ATTACHED','FINALIZED','CLEANUP_PENDING'"
_LEGACY_STATES = "'PENDING_UPLOAD','STAGED','ATTACHED','FINALIZED'"


def upgrade() -> None:
    # 长时间存活的数据库由旧版 0001 建表，缺少 CLEANUP_PENDING；
    # 项目清理接管暂存对象时会写入该状态，缺失会导致删除工作流永久失败。
    op.execute(f"ALTER TABLE staged_objects DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE staged_objects ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (state IN ({_FULL_STATES}))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE staged_objects DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE staged_objects ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (state IN ({_LEGACY_STATES}))"
    )
