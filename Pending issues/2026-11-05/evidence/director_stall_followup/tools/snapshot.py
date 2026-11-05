"""Post-deploy snapshot (read-only). Run inside the worker container with T0 set.

    docker exec -i -e T0=2026-11-05T13:59:46Z tubeflowai-test-worker-1 python - < snapshot.py
"""

from __future__ import annotations

import json
import os

import psycopg

DSN = "postgresql://tubeflow:tubeflow@tubeflowai-test-postgres-1/tubeflow"
T0 = os.environ["T0"]

QUERIES: dict[str, str] = {
    "attempts_by_stage": """
        select stage, count(*) from video_generation_attempts
        where updated_at >= '{t0}' group by 1 order by 2 desc
    """,
    "attempts_by_project": """
        select left(project_id::text, 8) as project, stage, count(*) from video_generation_attempts
        where updated_at >= '{t0}' group by 1, 2 order by 3 desc
    """,
    "attempt_errors": """
        select coalesce(error_code,'(none)') as error_code, count(*) from video_generation_attempts
        where updated_at >= '{t0}' and error_code is not null group by 1 order by 2 desc
    """,
    "trigger_markers": """
        select
          count(*) filter (where stage = 'FAILED') as failed,
          count(*) filter (where stage = 'REMOTE_STATE_UNKNOWN') as remote_unknown,
          count(*) filter (where stage = 'QC_FAILED') as qc_failed,
          count(*) filter (where stage = 'LOCAL_INTEGRITY_BLOCKED') as local_integrity_blocked
        from video_generation_attempts where updated_at >= '{t0}'
    """,
    "storyboards_created": """
        select left(project_id::text, 8) as project,
               coalesce(input_json::jsonb ->> 'grid_layout_version','none') as layout,
               status,
               coalesce(checkpoint_json::jsonb ->> 'failure_code','(none)') as failure_code,
               count(*)
        from storyboard_generations where created_at >= '{t0}'
        group by 1,2,3,4 order by 5 desc
    """,
    "director_revisions_created": """
        select left(project_id::text, 8) as project, count(*),
               count(*) filter (where composition_recut_approvals_json::text <> '[]') as with_recut
        from storyboard_director_revisions where created_at >= '{t0}' group by 1 order by 2 desc
    """,
    "recut_approvals_total": """
        select count(*) as total,
               count(*) filter (where composition_recut_approvals_json::text <> '[]') as non_empty
        from storyboard_director_revisions
    """,
    "recut_too_similar": """
        select count(*) as total,
               count(*) filter (where created_at >= '{t0}') as since_t0
        from storyboard_generations
        where checkpoint_json::jsonb ->> 'failure_code' = 'storyboard_recut_panels_too_similar'
    """,
    "director_runs_since": """
        select count(*) as runs, coalesce(min(created_at)::text,'') as first_run,
               coalesce(max(created_at)::text,'') as last_run
        from project_director_runs where created_at >= '{t0}'
    """,
    "monitor_projects": """
        select left(p.id::text,8) as project, r.state, r.reason_code, r.authorization_round,
               r.updated_at
        from projects p left join auto_pipeline_runs r on r.project_id = p.id
        where p.id in ('7cf2e79d-4d9d-48fa-8639-dde4100b6469',
                       'f881122d-1846-4d18-8f8b-4b89b0cbc0db',
                       'ff67c5e9-1db8-49c6-8f49-c96d9d0e140e')
        order by p.id
    """,
    "alembic": "select version_num from alembic_version",
}

with psycopg.connect(DSN) as conn:
    with conn.cursor() as cur:
        out: dict[str, object] = {"t0": T0}
        for name, sql in QUERIES.items():
            cur.execute(sql.format(t0=T0))
            columns = [d.name for d in cur.description]
            out[name] = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        print(json.dumps(out, indent=1, default=str))
