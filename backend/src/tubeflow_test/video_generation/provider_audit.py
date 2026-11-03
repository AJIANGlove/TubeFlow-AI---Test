from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

SAFE_RECEIPT_FIELDS = frozenset(
    {
        "id",
        "attempt_id",
        "task_id",
        "status",
        "stage",
        "staged_count",
        "remote_acceptance",
        "schema",
        "sha256",
        "size_bytes",
        "media_type",
        "codec_name",
        "start_time",
        "duration",
        "items",
        "confirmed",
        "archive_confirmed",
        "error_code",
        "qc_passed",
        "width",
        "height",
        "fps",
        "duration_ms",
        "video_tracks",
        "audio_tracks",
        "container_names",
        "video_duration_ms",
        "video_frame_count",
        "audio_start_ms",
        "audio_duration_ms",
        "audio_end_ms",
        "audio_codec",
    }
)
SECRET_FIELD_FRAGMENTS = ("authorization", "token", "cookie", "signature", "signed_url", "secret")
MAX_SCALAR_TEXT_LENGTH = 512
MAX_SAFE_LIST_ITEMS = 32


def canonical_redacted_receipt(value: Mapping[str, object]) -> dict[str, object]:
    """Return the stable, credential-free provider evidence allowed in permanent storage."""
    result: dict[str, object] = {}
    for key, raw in sorted(value.items()):
        lowered = key.lower()
        if key in SAFE_RECEIPT_FIELDS and not any(
            fragment in lowered for fragment in SECRET_FIELD_FRAGMENTS
        ):
            safe = _safe_value(raw)
            if safe is not None:
                result[key] = safe
    return result


def _safe_value(value: object) -> object | None:
    if isinstance(value, str):
        return value[:MAX_SCALAR_TEXT_LENGTH]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        if len(value) > MAX_SAFE_LIST_ITEMS:
            value = value[:MAX_SAFE_LIST_ITEMS]
        items: list[object] = []
        for item in value:
            if isinstance(item, Mapping):
                items.append(canonical_redacted_receipt(item))
                continue
            safe = _safe_value(item)
            if safe is None or isinstance(safe, (dict, list)):
                return None
            items.append(safe)
        return items
    return None


def canonical_hash(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderAuditEvent:
    attempt_id: UUID
    owner_id: UUID
    project_id: UUID
    group_id: UUID
    provider: str
    node_id: str
    schema_version: str
    workflow_id: str
    workflow_version: str
    model_key: str
    model_version: str
    local_idempotency_key: str
    provider_task_key: str | None
    event_type: str
    occurred_at: datetime
    request_sha256: str | None
    receipt: dict[str, object]
    receipt_sha256: str
    error_code: str | None = None
