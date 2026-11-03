# ruff: noqa: E501
"""Initial authoritative schema frozen as explicit PostgreSQL DDL.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

BASELINE_DDL = r"""
CREATE TABLE accounts (id UUID PRIMARY KEY, username VARCHAR(128) NOT NULL, password_hash TEXT NOT NULL, role VARCHAR(16) NOT NULL, active BOOLEAN NOT NULL, session_generation INTEGER NOT NULL, CONSTRAINT uq_accounts_username UNIQUE(username));
CREATE TABLE system_initialization (singleton INTEGER PRIMARY KEY, admin_account_id UUID NOT NULL REFERENCES accounts(id), completed_at TIMESTAMPTZ NOT NULL, CONSTRAINT ck_system_initialization_singleton CHECK (singleton=1), CONSTRAINT uq_system_initialization_admin_account_id UNIQUE(admin_account_id));
CREATE TABLE channels (id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES accounts(id), url TEXT NOT NULL);
CREATE INDEX ix_channels_owner_id ON channels(owner_id);
CREATE TABLE character_images (id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES accounts(id), asset_id UUID NOT NULL, object_key TEXT NOT NULL, sha256 VARCHAR(64) NOT NULL, media_type VARCHAR(32) NOT NULL, size_bytes INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, CONSTRAINT uq_character_images_owner_sha256 UNIQUE(owner_id, sha256));
CREATE INDEX ix_character_images_owner_id ON character_images(owner_id);
CREATE TABLE voice_catalog_imports (source_sha256 VARCHAR(64) PRIMARY KEY, catalog_version INTEGER NOT NULL, expected_count INTEGER NOT NULL, imported_count INTEGER NOT NULL, status VARCHAR(16) NOT NULL, updated_at TIMESTAMPTZ NOT NULL);
CREATE TABLE voice_versions (id UUID PRIMARY KEY, voice_id VARCHAR(128) NOT NULL, version INTEGER NOT NULL, display_name VARCHAR(300) NOT NULL, reference_zip_asset_id UUID NOT NULL, reference_zip_object_key TEXT NOT NULL, reference_zip_media_type VARCHAR(64) NOT NULL, reference_zip_size_bytes INTEGER NOT NULL, preview_asset_id UUID NOT NULL, preview_object_key TEXT NOT NULL, preview_media_type VARCHAR(64) NOT NULL, preview_size_bytes INTEGER NOT NULL, avatar_asset_id UUID NOT NULL, avatar_object_key TEXT NOT NULL, avatar_media_type VARCHAR(64) NOT NULL, avatar_size_bytes INTEGER NOT NULL, metadata_sha256 VARCHAR(64) NOT NULL, catalog_source_sha256 VARCHAR(64) NOT NULL, reference_zip_sha256 VARCHAR(64) NOT NULL, preview_sha256 VARCHAR(64) NOT NULL, avatar_sha256 VARCHAR(64) NOT NULL, supported_locales VARCHAR(8)[] NOT NULL, raw_language_codes VARCHAR(4)[] NOT NULL, catalog_metadata_json TEXT NOT NULL, enabled BOOLEAN NOT NULL, CONSTRAINT uq_voice_versions_voice_version UNIQUE(voice_id, version));
CREATE TABLE projects (id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES accounts(id), channel_id UUID NOT NULL REFERENCES channels(id), script TEXT NOT NULL, character_image_ids UUID[] NOT NULL, visual_style TEXT NOT NULL, voice_version_id UUID NOT NULL REFERENCES voice_versions(id), locale VARCHAR(8) NOT NULL, aspect_ratio VARCHAR(5) NOT NULL, expected_duration_seconds INTEGER, draft_revision INTEGER NOT NULL DEFAULT 1, production_state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE', production_revision INTEGER NOT NULL DEFAULT 1, snapshot_script TEXT, snapshot_character_image_ids UUID[], snapshot_visual_style TEXT, snapshot_voice_version_id UUID, snapshot_locale VARCHAR(8), snapshot_aspect_ratio VARCHAR(5), snapshot_channel_url TEXT, snapshot_voice_metadata_sha256 VARCHAR(64), snapshot_voice_catalog_source_sha256 VARCHAR(64), snapshot_voice_reference_asset_id UUID, snapshot_voice_reference_sha256 VARCHAR(64), snapshot_voice_reference_media_type VARCHAR(64), snapshot_voice_reference_size_bytes INTEGER, snapshot_voice_preview_asset_id UUID, snapshot_voice_preview_sha256 VARCHAR(64), snapshot_voice_preview_media_type VARCHAR(64), snapshot_voice_preview_size_bytes INTEGER, snapshot_voice_avatar_asset_id UUID, snapshot_voice_avatar_sha256 VARCHAR(64), snapshot_voice_avatar_media_type VARCHAR(64), snapshot_voice_avatar_size_bytes INTEGER, snapshot_expected_duration_seconds INTEGER, snapshot_confirmed_at TIMESTAMPTZ, CONSTRAINT uq_projects_owner_id UNIQUE(owner_id,id), CONSTRAINT ck_projects_production_state CHECK (production_state IN ('ACTIVE','PURGING')), CONSTRAINT ck_projects_draft_revision CHECK (draft_revision > 0), CONSTRAINT ck_projects_production_revision CHECK (production_revision > 0));
CREATE INDEX ix_projects_owner_id ON projects(owner_id);
CREATE TABLE project_snapshot_characters (id UUID PRIMARY KEY, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, character_image_id UUID NOT NULL REFERENCES character_images(id), asset_id UUID NOT NULL, sha256 VARCHAR(64) NOT NULL, media_type VARCHAR(64) NOT NULL, size_bytes INTEGER NOT NULL, CONSTRAINT uq_project_snapshot_characters_project_ordinal UNIQUE(project_id, ordinal), CONSTRAINT uq_project_snapshot_characters_project_character UNIQUE(project_id, character_image_id));
CREATE TABLE account_sessions (token_sha256 VARCHAR(64) PRIMARY KEY, account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, generation INTEGER NOT NULL);
CREATE INDEX ix_account_sessions_account_id ON account_sessions(account_id);
CREATE TABLE media_assets (id UUID PRIMARY KEY, owner_id UUID REFERENCES accounts(id) ON DELETE SET NULL, project_id UUID REFERENCES projects(id) ON DELETE SET NULL, ownership_scope VARCHAR(16) NOT NULL DEFAULT 'PROJECT', kind VARCHAR(32) NOT NULL, object_key TEXT NOT NULL, sha256 VARCHAR(64) NOT NULL, media_type VARCHAR(64) NOT NULL, size_bytes BIGINT NOT NULL, metadata_json JSONB NOT NULL, shared BOOLEAN NOT NULL DEFAULT FALSE, purging BOOLEAN NOT NULL DEFAULT FALSE, deletion_state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE', deletion_token UUID, deletion_generation INTEGER NOT NULL DEFAULT 0, CONSTRAINT uq_media_assets_object_key UNIQUE(object_key), CONSTRAINT ck_media_assets_ownership_scope CHECK (ownership_scope IN ('GLOBAL','OWNER','PROJECT')), CONSTRAINT ck_media_assets_deletion_state CHECK (deletion_state IN ('ACTIVE','DELETING')));
CREATE INDEX ix_media_assets_owner_id ON media_assets(owner_id);
CREATE INDEX ix_media_assets_project_id ON media_assets(project_id);
CREATE TABLE project_asset_references (project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE, asset_id UUID NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT, owner_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, PRIMARY KEY(project_id,asset_id));
CREATE INDEX ix_project_asset_references_owner_id ON project_asset_references(owner_id);
CREATE TABLE staged_objects (id UUID PRIMARY KEY, object_key TEXT NOT NULL, project_id UUID REFERENCES projects(id) ON DELETE CASCADE, owner_id UUID REFERENCES accounts(id) ON DELETE CASCADE, purpose VARCHAR(32), kind VARCHAR(32), ownership_scope VARCHAR(16) NOT NULL DEFAULT 'GLOBAL', state VARCHAR(16) NOT NULL DEFAULT 'PENDING_UPLOAD', sha256 VARCHAR(64), size_bytes BIGINT, media_type VARCHAR(64), generation INTEGER NOT NULL DEFAULT 1, execution_token UUID, asset_id UUID, created_at TIMESTAMPTZ NOT NULL, reference_type VARCHAR(32), CONSTRAINT uq_staged_objects_object_key UNIQUE(object_key), CONSTRAINT ck_staged_objects_state CHECK (state IN ('PENDING_UPLOAD','STAGED','ATTACHED','FINALIZED','CLEANUP_PENDING')), CONSTRAINT ck_staged_objects_ownership_scope CHECK (ownership_scope IN ('GLOBAL','OWNER','PROJECT')), CONSTRAINT ck_staged_objects_scope_ownership CHECK ((ownership_scope = 'GLOBAL' AND owner_id IS NULL AND project_id IS NULL) OR (ownership_scope = 'OWNER' AND owner_id IS NOT NULL AND project_id IS NULL) OR (ownership_scope = 'PROJECT' AND owner_id IS NOT NULL AND project_id IS NOT NULL)));
CREATE INDEX ix_staged_objects_project_id ON staged_objects(project_id);
CREATE INDEX ix_staged_objects_owner_id ON staged_objects(owner_id);
CREATE INDEX ix_staged_objects_cleanup ON staged_objects(ownership_scope, state, created_at);
"""

BASELINE_TABLES = (
    "staged_objects", "project_asset_references", "media_assets", "account_sessions", "project_snapshot_characters", "projects", "voice_versions", "voice_catalog_imports", "character_images", "channels", "system_initialization", "accounts",
)


def upgrade() -> None:
    for statement in BASELINE_DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table_name in BASELINE_TABLES:
        op.drop_table(table_name)
