"""Confirm narration for the short test project (starts the AUTO chain).

Runs the real confirm_and_start: sentence-split agent call (DeepSeek) + snapshot
freeze + narration job + AUTO run creation, in the same transaction as the API.
"""
from __future__ import annotations

import asyncio
import json
from uuid import UUID

from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.api.narration_runtime import create_narration_runtime
from tubeflow_test.config import Settings

PROJECT_ID = UUID("fa3a0b30-9222-4ca4-a475-3bfd6cc57220")

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
