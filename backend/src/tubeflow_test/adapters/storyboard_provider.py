from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from tubeflow_test.application.storyboards import (
    GridSheetReference,
    ProviderPoll,
    ProviderSubmission,
    ProviderSubmitNotDeliveredError,
)
from tubeflow_test.domain.storyboards import GenerationStatus, StoryboardGeneration


class HttpGridSheetImageProvider:
    """Controlled HTTP adapter; transport uncertainty is intentionally surfaced to the service."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        max_download_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.Client(timeout=timeout_seconds, headers=self._headers)
        self._max_download_bytes = max_download_bytes

    def close(self) -> None:
        self._client.close()

    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[GridSheetReference, ...] = ()
    ) -> ProviderSubmission:
        manifest = {
            "kind": "GRID_SHEET",
            "prompt": generation.prompt,
            "style": generation.style_snapshot,
            "model": generation.model,
            "parameters": dict(generation.parameters),
            "characters": [
                {
                    "ordinal": item.ordinal,
                    "sha256": item.sha256,
                    "media_type": item.media_type,
                    "size_bytes": len(item.content),
                }
                for item in references
            ],
        }
        try:
            response = self._client.post(
                f"{self._base_url}/grid-sheets",
                headers={"Idempotency-Key": generation.provider_idempotency_key},
                data={
                    "manifest": __import__("json").dumps(
                        manifest, separators=(",", ":"), ensure_ascii=False
                    )
                },
                files=[
                    ("references", (f"character-{item.ordinal}", item.content, item.media_type))
                    for item in references
                ],
            )
        except httpx.ConnectError as exc:
            raise ProviderSubmitNotDeliveredError(
                "grid sheet submit was not delivered"
            ) from exc
        response.raise_for_status()
        return self._submission(response.json())

    def reconcile(self, idempotency_key: str) -> ProviderSubmission | None:
        response = self._client.get(
            f"{self._base_url}/grid-sheets/by-idempotency-key/{quote(idempotency_key, safe='')}"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return self._submission(response.json())

    def poll(self, task_id: str) -> ProviderPoll:
        response = self._client.get(f"{self._base_url}/grid-sheets/{quote(task_id, safe='')}")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        remote = str(data.get("status", "")).upper()
        states = {
            "SUBMITTED": GenerationStatus.SUBMITTED,
            "RUNNING": GenerationStatus.RUNNING,
            "SUCCEEDED": GenerationStatus.SUCCEEDED,
            "FAILED": GenerationStatus.FAILED,
            "UNKNOWN": GenerationStatus.REMOTE_STATE_UNKNOWN,
        }
        if remote not in states:
            raise ValueError("provider returned an unsupported task status")
        return ProviderPoll(
            states[remote],
            str(data.get("receipt", "")),
            str(data["download_token"]) if data.get("download_token") else None,
            str(data["sha256"]) if data.get("sha256") else None,
            str(data["failure_code"]) if data.get("failure_code") else None,
        )

    def download(self, download_token: str, destination: Path) -> None:
        size = 0
        digest = hashlib.sha256()
        with self._client.stream(
            "GET", f"{self._base_url}/downloads/{quote(download_token, safe='')}"
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self._max_download_bytes:
                        raise ValueError("storyboard download exceeds configured byte limit")
                    digest.update(chunk)
                    output.write(chunk)

    @staticmethod
    def _submission(data: dict[str, Any]) -> ProviderSubmission:
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("provider response has no task id")
        return ProviderSubmission(task_id, str(data.get("receipt", "")))
