"""Read-only check of the narration job-id collision (NARRATION_JOB_ID_COLLISION_20261104.md).

For every narration job in DISPATCHED state, list other projects whose current or finished job has
the same job id, and describe the Temporal workflow with that id (status, times, and the project_id
in its start input). No writes, no workflow starts, no Provider calls.

    docker compose exec -T worker python - < verify_job_collision.py
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text
from temporalio.client import Client
from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.config import Settings

SQL = """
select s.project_id::text as stuck_project, s.job_id,
       r.project_id::text as other_project,
       r.payload->'job'->>'state' as other_job_state,
       coalesce(jsonb_exists(r.payload->'finished_jobs', s.job_id), false) as in_other_finished
from (
  select project_id, payload->'job'->>'job_id' as job_id
  from narration_project_records
  where payload->'job'->>'state' = 'DISPATCHED'
) s
left join narration_project_records r
  on r.project_id <> s.project_id
 and (r.payload->'job'->>'job_id' = s.job_id
      or coalesce(jsonb_exists(r.payload->'finished_jobs', s.job_id), false))
order by s.project_id
"""


async def describe(client: Client, job_id: str) -> dict[str, object]:
    handle = client.get_workflow_handle(job_id)
    try:
        description = await handle.describe()
    except Exception as error:  # e.g. not found after namespace retention
        return {"error": f"{type(error).__name__}: {error}"}
    result: dict[str, object] = {
        "status": str(description.status),
        "run_id": description.run_id,
        "start_time": str(description.start_time),
        "close_time": str(description.close_time),
    }
    try:
        history = await handle.fetch_history()
        started = history.events[0].workflow_execution_started_event_attributes
        data = json.loads(started.input.payloads[0].data)
        result["input_project_id"] = data.get("project_id")
    except Exception as error:
        result["input_error"] = f"{type(error).__name__}: {error}"
    return result


async def main() -> None:
    settings = Settings()
    database = Database(settings.database_url)
    with database.session() as session:
        rows = [dict(row) for row in session.execute(text(SQL)).mappings().all()]
    database.engine.dispose()
    client = await Client.connect(settings.temporal_target)
    workflows = {job_id: await describe(client, job_id) for job_id in {row["job_id"] for row in rows}}
    print(json.dumps({"rows": rows, "workflows": workflows}, ensure_ascii=False, indent=1, default=str))


asyncio.run(main())
