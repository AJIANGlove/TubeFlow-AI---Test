# ruff: noqa: E501
"""Explicit media, composition, and purge schema."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

DDL = r"""
CREATE TABLE deletion_tombstones (project_id UUID PRIMARY KEY, deleted_at TIMESTAMPTZ NOT NULL, object_count INTEGER NOT NULL);
CREATE TABLE project_purge_jobs (project_id UUID PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, state VARCHAR(24) NOT NULL, dispatch_error_code VARCHAR(64), object_results JSONB NOT NULL, workflow_generation INTEGER NOT NULL DEFAULT 1, execution_token UUID, lease_until TIMESTAMPTZ); CREATE INDEX ix_project_purge_jobs_owner_id ON project_purge_jobs(owner_id);
CREATE TABLE purge_outbox (id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES project_purge_jobs(project_id) ON DELETE CASCADE, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, dispatched_at TIMESTAMPTZ, lease_until TIMESTAMPTZ, attempt INTEGER NOT NULL DEFAULT 0, last_error_code VARCHAR(64), workflow_generation INTEGER NOT NULL DEFAULT 1, lease_token UUID, CONSTRAINT uq_purge_outbox_project_id UNIQUE(project_id)); CREATE INDEX ix_purge_outbox_owner_id ON purge_outbox(owner_id);
CREATE TABLE project_purge_objects (project_id UUID NOT NULL REFERENCES project_purge_jobs(project_id) ON DELETE CASCADE, object_key TEXT NOT NULL, asset_id UUID NOT NULL, sha256 VARCHAR(64) NOT NULL, owner_id UUID NOT NULL, external_reference_count INTEGER NOT NULL, deletion_token UUID, deletion_generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(project_id,object_key));
CREATE TABLE composition_input_revisions (id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, inputs_sha256 VARCHAR(64) NOT NULL, payload_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL); CREATE INDEX ix_composition_input_revisions_project_id ON composition_input_revisions(project_id); CREATE INDEX ix_composition_input_revisions_owner_id ON composition_input_revisions(owner_id); CREATE INDEX ix_composition_input_revisions_inputs_sha256 ON composition_input_revisions(inputs_sha256);
CREATE TABLE composition_jobs (id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, idempotency_key VARCHAR(256) NOT NULL, state VARCHAR(24) NOT NULL, status_detail TEXT, error_code VARCHAR(128), intent_sequence BIGINT NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0, workflow_generation INTEGER NOT NULL DEFAULT 1, lease_until TIMESTAMPTZ, execution_token UUID, composition_version_id UUID, CONSTRAINT uq_composition_jobs_idempotency_key UNIQUE(idempotency_key)); CREATE INDEX ix_composition_jobs_project_id ON composition_jobs(project_id); CREATE INDEX ix_composition_jobs_owner_id ON composition_jobs(owner_id);
CREATE TABLE composition_outbox (id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, revision_id UUID NOT NULL REFERENCES composition_input_revisions(id) ON DELETE CASCADE, inputs_sha256 VARCHAR(64) NOT NULL, workflow_id VARCHAR(256) NOT NULL, state VARCHAR(24) NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, workflow_generation INTEGER NOT NULL DEFAULT 1, lease_token UUID, lease_until TIMESTAMPTZ, next_attempt_at TIMESTAMPTZ NOT NULL, dispatched_at TIMESTAMPTZ, last_error_code VARCHAR(64), CONSTRAINT uq_composition_outbox_revision_id UNIQUE(revision_id), CONSTRAINT uq_composition_outbox_workflow_id UNIQUE(workflow_id)); CREATE INDEX ix_composition_outbox_project_id ON composition_outbox(project_id); CREATE INDEX ix_composition_outbox_owner_id ON composition_outbox(owner_id);
CREATE TABLE composition_versions (id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, owner_id UUID NOT NULL REFERENCES accounts(id), video_asset_id UUID NOT NULL REFERENCES media_assets(id), srt_asset_id UUID NOT NULL REFERENCES media_assets(id), manifest_json JSONB NOT NULL, intent_sequence BIGINT NOT NULL DEFAULT 0, current BOOLEAN NOT NULL DEFAULT FALSE); CREATE INDEX ix_composition_versions_project_id ON composition_versions(project_id); CREATE INDEX ix_composition_versions_owner_id ON composition_versions(owner_id); CREATE UNIQUE INDEX uq_composition_current_project ON composition_versions(project_id) WHERE current;
"""

TABLES = ("composition_versions", "composition_outbox", "composition_jobs", "composition_input_revisions", "project_purge_objects", "purge_outbox", "project_purge_jobs", "deletion_tombstones")


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
