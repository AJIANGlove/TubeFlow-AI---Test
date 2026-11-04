"""Create one new AUTO project copying the immutable inputs of fa3a0b30.

Runs inside the deployed web container (Settings + DB available):
    docker compose exec -T web python - < create_v2_project.py

No Provider calls. Prints the new project id and a field comparison.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import text

from tubeflow_test.adapters.persistence.database import Database
from tubeflow_test.adapters.persistence.identity_project.repository import (
    AccountRow,
    SqlAlchemyIdentityProjectRepository,
)
from tubeflow_test.config import Settings
from tubeflow_test.domain.common import AspectRatio, SupportedLocale
from tubeflow_test.identity_project.adapters import Argon2PasswordHasher
from tubeflow_test.identity_project.domain import Account, AccountRole
from tubeflow_test.identity_project.service import IdentityProjectService

SOURCE = UUID("fa3a0b30-9222-4ca4-a475-3bfd6cc57220")

settings = Settings()
database = Database(settings.database_url)


def sha(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


with database.unit_of_work() as session:
    source = session.execute(
        text(
            "select title, script, channel_id, character_image_ids, visual_style, "
            "locale, aspect_ratio, expected_duration_seconds, tts_provider, "
            "cloned_voice_id, voice_version_id, owner_id "
            "from projects where id=:p"
        ),
        {"p": SOURCE},
    ).one()
    account_row = session.get(AccountRow, source.owner_id)
    assert account_row is not None
    owner = Account(
        account_row.id,
        account_row.username,
        account_row.password_hash,
        AccountRole(account_row.role),
        account_row.active,
        account_row.session_generation,
    )
    service = IdentityProjectService(
        SqlAlchemyIdentityProjectRepository(session), Argon2PasswordHasher()
    )
    project = service.create_project(
        owner,
        channel_id=source.channel_id,
        script=source.script,
        image_ids=tuple(UUID(str(i)) for i in (source.character_image_ids or [])),
        visual_style=source.visual_style,
        voice_version_id=source.voice_version_id,
        locale=SupportedLocale(source.locale),
        aspect_ratio=AspectRatio(source.aspect_ratio),
        expected_duration_seconds=source.expected_duration_seconds,
        title=source.title,
        tts_provider=source.tts_provider,
        cloned_voice_id=source.cloned_voice_id,
    )
    new_id = project.id

with database.session() as session:
    check = session.execute(
        text(
            "select title, script, channel_id, character_image_ids, visual_style, "
            "locale, aspect_ratio, expected_duration_seconds, tts_provider, "
            "cloned_voice_id, voice_version_id, production_mode, production_state "
            "from projects where id=:p"
        ),
        {"p": new_id},
    ).mappings().one()
    source_check = session.execute(
        text(
            "select title, script, channel_id, character_image_ids, visual_style, "
            "locale, aspect_ratio, expected_duration_seconds, tts_provider, "
            "cloned_voice_id, voice_version_id from projects where id=:p"
        ),
        {"p": SOURCE},
    ).mappings().one()

fields = [
    "title",
    "channel_id",
    "character_image_ids",
    "visual_style",
    "locale",
    "aspect_ratio",
    "expected_duration_seconds",
    "tts_provider",
    "cloned_voice_id",
    "voice_version_id",
]
comparison = {}
for field in fields:
    left = source_check[field]
    right = check[field]
    comparison[field] = {
        "equal": str(left) == str(right),
        "source": str(left),
        "new": str(right),
    }
comparison["script_sha256"] = {
    "equal": sha(source_check["script"]) == sha(check["script"]),
    "source": sha(source_check["script"]),
    "new": sha(check["script"]),
}
print(
    json.dumps(
        {
            "project_id": str(new_id),
            "production_mode": check["production_mode"],
            "production_state": check["production_state"],
            "comparison": comparison,
        },
        ensure_ascii=False,
        indent=2,
    )
)
database.engine.dispose()
