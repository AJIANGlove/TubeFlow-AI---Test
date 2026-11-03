"""Keep canonical staged cleanup evidence across late object writes."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE staged_objects DROP CONSTRAINT staged_objects_project_id_fkey")
    op.execute("ALTER TABLE staged_objects ADD COLUMN lease_until TIMESTAMPTZ")
    op.execute("ALTER TABLE staged_objects ADD COLUMN cleanup_after TIMESTAMPTZ")
    op.execute("ALTER TABLE staged_objects ADD COLUMN cleanup_checked_at TIMESTAMPTZ")
    op.execute("ALTER TABLE staged_objects ADD COLUMN cleanup_attempt INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE staged_objects ADD COLUMN logical_object_key TEXT")
    op.execute("UPDATE staged_objects SET logical_object_key = object_key")
    op.execute(
        "CREATE INDEX ix_staged_objects_logical_object_key "
        "ON staged_objects(logical_object_key)"
    )
    op.execute("DROP INDEX ix_staged_objects_cleanup")
    op.execute(
        "CREATE INDEX ix_staged_objects_cleanup "
        "ON staged_objects(state, cleanup_after, lease_until)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_staged_objects_cleanup")
    op.execute("DROP INDEX ix_staged_objects_logical_object_key")
    op.execute(
        "CREATE INDEX ix_staged_objects_cleanup "
        "ON staged_objects(ownership_scope,state,created_at)"
    )
    op.execute("ALTER TABLE staged_objects DROP COLUMN cleanup_attempt")
    op.execute("ALTER TABLE staged_objects DROP COLUMN cleanup_checked_at")
    op.execute("ALTER TABLE staged_objects DROP COLUMN cleanup_after")
    op.execute("ALTER TABLE staged_objects DROP COLUMN lease_until")
    op.execute("ALTER TABLE staged_objects DROP COLUMN logical_object_key")
    # Anonymous project-deletion tombstones deliberately cannot be reattached to
    # a deleted ProjectRow. Downgrade discards only those exact-key cleanup rows,
    # then removes any legacy dangling PROJECT rows before restoring the old FK.
    op.execute("DELETE FROM staged_objects WHERE purpose = 'PROJECT_DELETION'")
    op.execute(
        "DELETE FROM staged_objects s WHERE s.ownership_scope = 'PROJECT' "
        "AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id = s.project_id)"
    )
    op.execute(
        "ALTER TABLE staged_objects ADD CONSTRAINT staged_objects_project_id_fkey "
        "FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE"
    )
