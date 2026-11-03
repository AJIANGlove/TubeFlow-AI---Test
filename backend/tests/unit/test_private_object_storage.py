from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import pytest
from minio.error import S3Error

from tubeflow_test.adapters.object_storage import ConditionalWriteConflict, PrivateObjectStorage


class _Response(BytesIO):
    def close(self) -> None:
        super().close()

    def release_conn(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.policy_removed = False
        self.conflict_code = "PreconditionFailed"
        self.lose_success_response = False

    def bucket_exists(self, _bucket: str) -> bool:
        return True

    def delete_bucket_policy(self, _bucket: str) -> None:
        self.policy_removed = True

    def put_object(self, _bucket: str, key: str, body: BytesIO, size: int, **_kwargs) -> None:
        self.objects[key] = body.read(size)

    def _execute(
        self, _method: str, _bucket: str, key: str, *, body: bytes, no_body_trace: bool, **_kwargs
    ):
        assert no_body_trace is True
        if key in self.objects:
            raise S3Error(None, self.conflict_code, "exists", key, "request", "host")  # type: ignore[arg-type]
        self.objects[key] = body
        if self.lose_success_response:
            raise S3Error(None, "PreconditionFailed", "response lost", key, "request", "host")  # type: ignore[arg-type]
        return _Response()

    def stat_object(self, _bucket: str, key: str) -> object:
        if key not in self.objects:
            raise S3Error(None, "NoSuchKey", "missing", key, "request", "host")  # type: ignore[arg-type]
        return object()

    def get_object(self, _bucket: str, key: str) -> _Response:
        return _Response(self.objects[key])

    def remove_object(self, _bucket: str, key: str) -> None:
        self.objects.pop(key, None)


def test_private_storage_supports_safe_hierarchical_exact_keys_and_verification() -> None:
    client = _Client()
    storage = PrivateObjectStorage(client, "private")  # type: ignore[arg-type]
    body = b"catalog-object"
    digest = sha256(body).hexdigest()

    storage.ensure_private_bucket()
    stored = storage.put_at_key("voices/v1/reference.zip", body, "application/zip", digest)
    replay = storage.put_at_key("voices/v1/reference.zip", body, "application/zip", digest)

    assert client.policy_removed
    assert stored.created
    assert not replay.created
    assert stored.key == replay.key
    assert stored.sha256 == replay.sha256
    assert storage.get_verified(stored.key, digest, len(body)) == body
    with pytest.raises(ValueError, match="SHA-256"):
        storage.put_at_key(stored.key, b"changed", "application/zip", digest)
    with pytest.raises(ValueError, match="exact safe object"):
        storage.put_exact("voices/../escape", body, "application/zip")


def test_conditional_request_conflict_requires_an_observable_matching_winner() -> None:
    client = _Client()
    client.conflict_code = "ConditionalRequestConflict"
    storage = PrivateObjectStorage(client, "private")  # type: ignore[arg-type]
    body = b"winner"
    digest = sha256(body).hexdigest()
    client.objects["voices/winner.zip"] = body

    replay = storage.put_at_key(
        "voices/winner.zip", body, "application/zip", digest
    )
    assert not replay.created

    client.objects["voices/winner.zip"] = b"different"
    with pytest.raises(ConditionalWriteConflict):
        storage.put_at_key("voices/winner.zip", body, "application/zip", digest)


def test_response_lost_after_server_commit_reconciles_without_second_write() -> None:
    client = _Client()
    client.lose_success_response = True
    storage = PrivateObjectStorage(client, "private")  # type: ignore[arg-type]
    body = b"committed-before-response-loss"
    digest = sha256(body).hexdigest()

    reconciled = storage.put_at_key("voices/lost.zip", body, "application/zip", digest)

    assert not reconciled.created
    assert client.objects["voices/lost.zip"] == body
