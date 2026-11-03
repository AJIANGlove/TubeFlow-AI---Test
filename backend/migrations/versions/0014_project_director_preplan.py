"""Add untimed project director preplanning and deterministic materialization.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_director_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "owner_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("canonical_input_sha256", sa.String(64), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_revision", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("next_unit_ordinal", sa.Integer(), nullable=False),
        sa.Column("total_units", sa.Integer(), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision_id", sa.UUID(), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("retry_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_id",
            "idempotency_key",
            name="uq_project_director_runs_owner_key",
        ),
        sa.UniqueConstraint("owner_id", "id", name="uq_project_director_runs_owner_id"),
    )
    op.create_index("ix_project_director_runs_owner_id", "project_director_runs", ["owner_id"])
    op.create_index("ix_project_director_runs_project_id", "project_director_runs", ["project_id"])

    op.create_table(
        "project_director_units",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("project_director_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("sentence_id", sa.UUID(), nullable=False),
        sa.Column("sentence_sequence", sa.Integer(), nullable=False),
        sa.Column("sentence_text", sa.Text(), nullable=False),
        sa.Column("sentence_sha256", sa.String(64), nullable=False),
        sa.Column("context_before_json", sa.JSON(), nullable=False),
        sa.Column("context_after_json", sa.JSON(), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(64), nullable=False),
        sa.Column("canonical_request_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_ref", sa.String(64), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_content", sa.Text(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.UUID(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_receipt", sa.Text(), nullable=True),
        sa.Column("raw_response_sha256", sa.String(64), nullable=True),
        sa.Column("shots_json", sa.JSON(), nullable=False),
        sa.Column("links_json", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_project_director_units_run_ordinal"),
        sa.UniqueConstraint(
            "owner_id",
            "provider_idempotency_key",
            name="uq_project_director_units_owner_provider_key",
        ),
    )
    op.create_index("ix_project_director_units_run_id", "project_director_units", ["run_id"])
    op.create_index("ix_project_director_units_owner_id", "project_director_units", ["owner_id"])
    op.create_index(
        "ix_project_director_units_project_id",
        "project_director_units",
        ["project_id"],
    )

    op.create_table(
        "project_director_revisions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "owner_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.UUID(),
            sa.ForeignKey("project_director_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_revision", sa.Integer(), nullable=False),
        sa.Column("sentence_ids_json", sa.JSON(), nullable=False),
        sa.Column("shots_json", sa.JSON(), nullable=False),
        sa.Column("links_json", sa.JSON(), nullable=False),
        sa.Column("quality_conflicts_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_project_director_revisions_run"),
        sa.UniqueConstraint(
            "owner_id",
            "project_id",
            "revision_number",
            name="uq_project_director_revisions_owner_project_revision",
        ),
        sa.UniqueConstraint("owner_id", "id", name="uq_project_director_revisions_owner_id"),
    )
    op.create_index(
        "ix_project_director_revisions_owner_id", "project_director_revisions", ["owner_id"]
    )
    op.create_index(
        "ix_project_director_revisions_project_id", "project_director_revisions", ["project_id"]
    )
    op.create_foreign_key(
        "fk_project_director_runs_revision_id_project_director_revisions",
        "project_director_runs",
        "project_director_revisions",
        ["revision_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "project_director_current",
        sa.Column(
            "owner_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "revision_id",
            sa.UUID(),
            sa.ForeignKey("project_director_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("pointer_version", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "project_director_materializations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "owner_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "preplan_revision_id",
            sa.UUID(),
            sa.ForeignKey("project_director_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_revision", sa.Integer(), nullable=False),
        sa.Column("algorithm_version", sa.String(64), nullable=False),
        sa.Column("canonical_input_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_id",
            "canonical_input_sha256",
            name="uq_project_director_materializations_owner_input",
        ),
        sa.UniqueConstraint(
            "owner_id", "id", name="uq_project_director_materializations_owner_id"
        ),
    )
    op.create_index(
        "ix_project_director_materializations_owner_id",
        "project_director_materializations",
        ["owner_id"],
    )
    op.create_index(
        "ix_project_director_materializations_project_id",
        "project_director_materializations",
        ["project_id"],
    )

    op.create_table(
        "project_director_group_candidates",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "materialization_id",
            sa.UUID(),
            sa.ForeignKey("project_director_materializations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_id", sa.UUID(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.UUID(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.UUID(),
            sa.ForeignKey("authoritative_narration_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("group_sequence", sa.Integer(), nullable=False),
        sa.Column("shots_json", sa.JSON(), nullable=False),
        sa.Column("links_json", sa.JSON(), nullable=False),
        sa.Column("source_shot_links_json", sa.JSON(), nullable=False),
        sa.Column("conflicts_json", sa.JSON(), nullable=False),
        sa.Column("review_state", sa.String(16), nullable=False),
        sa.Column(
            "final_revision_id",
            sa.UUID(),
            sa.ForeignKey("storyboard_director_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "materialization_id",
            "group_id",
            name="uq_project_director_group_candidates_materialization_group",
        ),
        sa.UniqueConstraint(
            "materialization_id",
            "group_sequence",
            name="uq_project_director_group_candidates_materialization_sequence",
        ),
    )
    op.create_index(
        "ix_project_director_group_candidates_materialization_id",
        "project_director_group_candidates",
        ["materialization_id"],
    )
    op.create_index(
        "ix_project_director_group_candidates_owner_id",
        "project_director_group_candidates",
        ["owner_id"],
    )
    op.create_index(
        "ix_project_director_group_candidates_project_id",
        "project_director_group_candidates",
        ["project_id"],
    )
    op.create_index(
        "ix_project_director_group_candidates_group_id",
        "project_director_group_candidates",
        ["group_id"],
    )

    op.create_table(
        "director_revision_lineage",
        sa.Column(
            "revision_id",
            sa.UUID(),
            sa.ForeignKey("storyboard_director_revisions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_director_revision_id",
            sa.UUID(),
            sa.ForeignKey("project_director_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "materialization_id",
            sa.UUID(),
            sa.ForeignKey("project_director_materializations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.UUID(),
            sa.ForeignKey("project_director_group_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_director_revision_lineage_project_director_revision_id",
        "director_revision_lineage",
        ["project_director_revision_id"],
    )
    op.create_index(
        "ix_director_revision_lineage_materialization_id",
        "director_revision_lineage",
        ["materialization_id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "0014 is intentionally irreversible because project director history is immutable"
    )
