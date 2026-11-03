from __future__ import annotations

from tubeflow_test.video_generation.provider_audit import (
    canonical_hash,
    canonical_redacted_receipt,
)


def test_provider_receipt_allowlist_never_persists_credentials_or_signed_urls() -> None:
    receipt = {
        "id": "task-1",
        "status": "ACCEPTED",
        "stage": "CANCELLED",
        "staged_count": 2,
        "schema": "kinestack.execution.v1",
        "Authorization": "Bearer secret",
        "cookie": "session=secret",
        "token": "secret",
        "signed_url": "https://objects.invalid/file?signature=secret",
        "unknown_raw": "must-not-persist",
        "items": [{"id": "output-1", "sha256": "a" * 64, "secret": "no"}],
    }

    safe = canonical_redacted_receipt(receipt)

    assert safe == {
        "id": "task-1",
        "items": [{"id": "output-1", "sha256": "a" * 64}],
        "schema": "kinestack.execution.v1",
        "stage": "CANCELLED",
        "staged_count": 2,
        "status": "ACCEPTED",
    }
    assert canonical_hash(safe) == canonical_hash(dict(reversed(tuple(safe.items()))))
    assert "secret" not in str(safe).lower()


def test_provider_receipt_allows_bounded_scalar_qc_lists() -> None:
    safe = canonical_redacted_receipt(
        {
            "container_names": ["mp4", "mov"],
            "audio_codec": "x" * 700,
            "schema": "s" * 700,
            "unknown": "never persisted",
        }
    )
    assert safe["container_names"] == ["mp4", "mov"]
    assert safe["audio_codec"] == "x" * 512
    assert safe["schema"] == "s" * 512
    assert "unknown" not in safe
