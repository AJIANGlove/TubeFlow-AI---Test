"""Move project director runs to global-plan multi-sentence units with bounded retries.

Revision ID: 0018
Revises: 0017

Legacy v3 units stored one sentence per unit and are rewritten in place:
each row keeps its immutable sentence as a one-element sentences_json payload.
Legacy SUCCEEDED SHOT_PLAN rows keep their v3 links_json, which may contain
context-sentence links that the v4 domain contract rejects on deserialization.
That dead payload is intentionally left untouched: immutable history is never
rewritten, and the v4 read paths that deserialize units (claim/advance/list)
only serve runs created after this migration, while aggregate paths
(run_progress) never deserialize unit payloads at all.
"""

import re
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_MARKDOWN_SEPARATOR = re.compile(r"[-*_~=—–…`]+")
_TERMINAL_UNIT_STATUSES = {"SUCCEEDED", "FAILED", "REMOTE_STATE_UNKNOWN"}


def _is_nonvisual_script_separator(text: str) -> bool:
    compact = "".join(text.split())
    if not compact:
        return False
    return bool(_MARKDOWN_SEPARATOR.fullmatch(compact)) or all(
        unicodedata.category(character).startswith("P") for character in compact
    )


def upgrade() -> None:
    op.add_column(
        "project_director_runs",
        sa.Column("auto_retries_used", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_column("project_director_runs", "next_unit_ordinal")

    op.add_column(
        "project_director_units",
        sa.Column("kind", sa.String(32), nullable=True),
    )
    op.add_column(
        "project_director_units",
        sa.Column("sentences_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "project_director_units",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "project_director_units",
        sa.Column("last_failure_code", sa.String(128), nullable=True),
    )
    op.add_column(
        "project_director_units",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "project_director_units",
        sa.Column("plan_document", sa.Text(), nullable=True),
    )
    op.add_column(
        "project_director_units",
        sa.Column("plan_document_sha256", sa.String(64), nullable=True),
    )

    units = sa.table(
        "project_director_units",
        sa.column("id", sa.Uuid()),
        sa.column("sentence_id", sa.Uuid()),
        sa.column("sentence_sequence", sa.Integer()),
        sa.column("sentence_text", sa.Text()),
        sa.column("sentence_sha256", sa.String()),
        sa.column("status", sa.String()),
        sa.column("failure_code", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("sentences_json", sa.JSON()),
        sa.column("attempt_count", sa.Integer()),
        sa.column("last_failure_code", sa.String()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            units.c.id,
            units.c.sentence_id,
            units.c.sentence_sequence,
            units.c.sentence_text,
            units.c.sentence_sha256,
            units.c.status,
            units.c.failure_code,
        )
    ).all()
    for row in rows:
        sentences_json = [
            {
                "sentence_id": str(row.sentence_id),
                "sequence": row.sentence_sequence,
                "text": row.sentence_text,
                "text_sha256": row.sentence_sha256,
            }
        ]
        kind = (
            "NONVISUAL_SEPARATOR"
            if _is_nonvisual_script_separator(row.sentence_text)
            else "SHOT_PLAN"
        )
        attempt_count = 1 if row.status in _TERMINAL_UNIT_STATUSES else 0
        last_failure_code = row.failure_code if row.status == "FAILED" else None
        bind.execute(
            units.update()
            .where(units.c.id == row.id)
            .values(
                kind=kind,
                sentences_json=sentences_json,
                attempt_count=attempt_count,
                last_failure_code=last_failure_code,
            )
        )

    op.alter_column("project_director_units", "kind", nullable=False)
    op.alter_column("project_director_units", "sentences_json", nullable=False)
    op.drop_column("project_director_units", "sentence_id")
    op.drop_column("project_director_units", "sentence_sequence")
    op.drop_column("project_director_units", "sentence_text")
    op.drop_column("project_director_units", "sentence_sha256")


def downgrade() -> None:
    raise RuntimeError(
        "0018 is intentionally irreversible because project director history is immutable"
    )
