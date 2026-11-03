# ruff: noqa: E501
"""Register authoritative storyboard schema.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in STORYBOARD_DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in STORYBOARD_TABLES:
        op.drop_table(table)


STORYBOARD_DDL = r"""
CREATE TABLE storyboard_outbox (id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, aggregate_type VARCHAR(32) NOT NULL, aggregate_id UUID NOT NULL, event_type VARCHAR(64) NOT NULL, request_sha256 VARCHAR(64) NOT NULL, workflow_id VARCHAR(192) NOT NULL, state VARCHAR(16) NOT NULL, lease_token UUID, lease_until TIMESTAMPTZ, attempt_count INTEGER NOT NULL, next_attempt_at TIMESTAMPTZ NOT NULL, acknowledged_at TIMESTAMPTZ, last_error VARCHAR(256), created_at TIMESTAMPTZ NOT NULL, CONSTRAINT uq_storyboard_outbox_workflow_id UNIQUE(workflow_id));
CREATE INDEX ix_storyboard_outbox_owner_id ON storyboard_outbox(owner_id); CREATE INDEX ix_storyboard_outbox_project_id ON storyboard_outbox(project_id); CREATE INDEX ix_storyboard_outbox_aggregate_id ON storyboard_outbox(aggregate_id);
CREATE TABLE storyboard_director_revisions (id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, group_id UUID NOT NULL REFERENCES authoritative_narration_groups(id) ON DELETE CASCADE, revision_number INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, shots_json JSON NOT NULL, links_json JSON NOT NULL, group_sentence_ids_json JSON NOT NULL, review_state VARCHAR(16) NOT NULL, continuous_observation_approvals_json JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL, CONSTRAINT uq_storyboard_director_revisions_owner_group_revision UNIQUE(owner_id,group_id,revision_number), CONSTRAINT uq_storyboard_director_revisions_owner_id UNIQUE(owner_id,id));
CREATE INDEX ix_storyboard_director_revisions_owner_id ON storyboard_director_revisions(owner_id); CREATE INDEX ix_storyboard_director_revisions_project_id ON storyboard_director_revisions(project_id); CREATE INDEX ix_storyboard_director_revisions_group_id ON storyboard_director_revisions(group_id);
CREATE TABLE director_generation_attempts (id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, group_id UUID NOT NULL REFERENCES authoritative_narration_groups(id) ON DELETE CASCADE, expected_draft_revision_id UUID, idempotency_key VARCHAR(128) NOT NULL, provider_idempotency_key VARCHAR(64) NOT NULL, canonical_request_sha256 VARCHAR(64) NOT NULL, input_json JSON NOT NULL, prompt_ref VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL, prompt_content TEXT NOT NULL, model VARCHAR(128) NOT NULL, parameters_json JSON NOT NULL, status VARCHAR(32) NOT NULL, state_revision INTEGER NOT NULL, lease_token UUID, lease_until TIMESTAMPTZ, provider_receipt TEXT, raw_response_sha256 VARCHAR(64), draft_revision_id UUID REFERENCES storyboard_director_revisions(id) ON DELETE SET NULL, failure_code VARCHAR(128), history_json JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL, CONSTRAINT uq_director_generation_attempts_owner_key UNIQUE(owner_id,idempotency_key));
CREATE INDEX ix_director_generation_attempts_owner_id ON director_generation_attempts(owner_id); CREATE INDEX ix_director_generation_attempts_group_id ON director_generation_attempts(group_id);
CREATE TABLE storyboard_director_pointers (owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, group_id UUID NOT NULL REFERENCES authoritative_narration_groups(id) ON DELETE CASCADE, draft_revision_id UUID REFERENCES storyboard_director_revisions(id) ON DELETE SET NULL, applied_revision_id UUID REFERENCES storyboard_director_revisions(id) ON DELETE SET NULL, PRIMARY KEY(owner_id,group_id));
CREATE TABLE storyboard_generations (id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, group_id UUID NOT NULL REFERENCES authoritative_narration_groups(id) ON DELETE CASCADE, applied_revision_id UUID NOT NULL REFERENCES storyboard_director_revisions(id) ON DELETE CASCADE, version INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, input_json JSON NOT NULL, idempotency_key VARCHAR(128) NOT NULL, provider_idempotency_key VARCHAR(128) NOT NULL, canonical_request_sha256 VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, state_revision INTEGER NOT NULL, execution_token UUID, lease_until TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL, provider_task_id TEXT, provider_receipt TEXT, checkpoint_json JSON NOT NULL, asset_id UUID, CONSTRAINT uq_storyboard_generations_owner_key UNIQUE(owner_id,idempotency_key), CONSTRAINT uq_storyboard_generations_owner_group_version UNIQUE(owner_id,group_id,version), CONSTRAINT uq_storyboard_generations_owner_id UNIQUE(owner_id,id));
CREATE INDEX ix_storyboard_generations_owner_id ON storyboard_generations(owner_id); CREATE INDEX ix_storyboard_generations_project_id ON storyboard_generations(project_id); CREATE INDEX ix_storyboard_generations_group_id ON storyboard_generations(group_id);
CREATE TABLE storyboard_current (owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, group_id UUID NOT NULL REFERENCES authoritative_narration_groups(id) ON DELETE CASCADE, generation_id UUID REFERENCES storyboard_generations(id) ON DELETE SET NULL, version INTEGER NOT NULL, PRIMARY KEY(owner_id,group_id), CONSTRAINT uq_storyboard_current_generation_id UNIQUE(generation_id));
CREATE TABLE storyboard_media (generation_id UUID PRIMARY KEY REFERENCES storyboard_generations(id) ON DELETE CASCADE, storyboard_asset_id UUID NOT NULL, asset_id UUID NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT, width INTEGER NOT NULL, height INTEGER NOT NULL, image_format VARCHAR(16) NOT NULL, CONSTRAINT uq_storyboard_media_asset_id UNIQUE(storyboard_asset_id)); CREATE INDEX ix_storyboard_media_asset_id ON storyboard_media(asset_id);
CREATE TABLE video_storyboard_bindings (video_id UUID PRIMARY KEY, owner_id UUID NOT NULL, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, group_id UUID NOT NULL REFERENCES authoritative_narration_groups(id) ON DELETE CASCADE, storyboard_generation_id UUID NOT NULL, applied_revision_id UUID NOT NULL, storyboard_asset_sha256 VARCHAR(64) NOT NULL, FOREIGN KEY(owner_id,storyboard_generation_id) REFERENCES storyboard_generations(owner_id,id) ON DELETE CASCADE, FOREIGN KEY(owner_id,applied_revision_id) REFERENCES storyboard_director_revisions(owner_id,id) ON DELETE CASCADE);
CREATE INDEX ix_video_storyboard_bindings_owner_id ON video_storyboard_bindings(owner_id); CREATE INDEX ix_video_storyboard_bindings_project_id ON video_storyboard_bindings(project_id); CREATE INDEX ix_video_storyboard_bindings_group_id ON video_storyboard_bindings(group_id);
"""

STORYBOARD_TABLES = ("video_storyboard_bindings", "storyboard_media", "storyboard_current", "storyboard_generations", "storyboard_director_pointers", "director_generation_attempts", "storyboard_director_revisions", "storyboard_outbox")
