from __future__ import annotations

from dataclasses import replace
from urllib.parse import unquote
from uuid import uuid4

import httpx
from test_storyboard_sql_repository import generation

from tubeflow_test.adapters.storyboard_provider import HttpGridSheetImageProvider
from tubeflow_test.application.storyboards import GridSheetReference
from tubeflow_test.domain.storyboards import GenerationStatus


def test_http_provider_uses_owner_scoped_key_for_submit_and_reconcile() -> None:
    seen: list[tuple[str, str]] = []
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(("submit", request.headers["Idempotency-Key"]))
            bodies.append(request.content)
        else:
            seen.append(("reconcile", unquote(request.url.path.rsplit("/", 1)[-1])))
        return httpx.Response(200, json={"task_id": "remote", "receipt": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = HttpGridSheetImageProvider("https://provider.test", "token", client=client)
    first = generation(1, GenerationStatus.INTENT_PERSISTED)
    second = replace(
        first,
        generation_id=first.generation_id,
        owner_id=uuid4(),
        provider_idempotency_key="e" * 64,
    )
    provider.submit_grid_sheet(
        first,
        (GridSheetReference(1, "image/png", "a" * 64, b"private-image-bytes"),),
    )
    provider.reconcile(second.provider_idempotency_key)
    assert seen == [
        ("submit", first.provider_idempotency_key),
        ("reconcile", second.provider_idempotency_key),
    ]
    assert b"private-image-bytes" in bodies[0]
    assert first.prompt.encode() in bodies[0]
    assert first.style_snapshot.encode() in bodies[0]
