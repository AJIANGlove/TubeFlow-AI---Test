"""Confirm narration for the new §46 AUTO test project.

Runs inside the deployed web container:
    NEW_PROJECT_ID=<uuid> docker compose exec -T -e NEW_PROJECT_ID web python - < confirm_v2_project.py

This is the single authorized action that starts the full AUTO chain
(sentence split + snapshot freeze + narration job + AUTO run).
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID

from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.api.narration_runtime import create_narration_runtime
from tubeflow_test.config import Settings

PROJECT_ID = UUID(os.environ["NEW_PROJECT_ID"])

settings = Settings()
database = Database(settings.database_url)
runtime = create_narration_runtime(settings, database=database)


async def main() -> dict[str, object]:
    with database.session() as session:
        owner = session.execute(
            text("select owner_id from projects where id = :p"), {"p": PROJECT_ID}
        ).scalar_one()
    result = await runtime.service.confirm_and_start(owner, PROJECT_ID)
    return {"result": result}


result = asyncio.run(main())

with database.session() as session:
    job = session.execute(
        text(
            "select payload->>'state', jsonb_array_length(payload->'job'->'sentence_sequences') "
            "from narration_project_records where project_id = :p"
        ),
        {"p": PROJECT_ID},
    ).one()
    auto = session.execute(
        text("select state, authorization_round from auto_pipeline_runs where project_id = :p"),
        {"p": PROJECT_ID},
    ).one()
    frozen = session.execute(
        text("select count(*) from authoritative_narration_sentences where project_id = :p"),
        {"p": PROJECT_ID},
    ).scalar_one()
print(
    json.dumps(
        {
            "project_id": str(PROJECT_ID),
            "confirm": result,
            "narration_job_state": job[0],
            "narration_sentences": job[1],
            "auto_state": auto[0],
            "auto_round": auto[1],
            "frozen_sentences": frozen,
        },
        ensure_ascii=False,
        indent=2,
    )
)
database.engine.dispose()
