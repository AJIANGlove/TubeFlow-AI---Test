from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace

import pytest
from minio.error import S3Error

from tubeflow_test.adapters.object_storage import PrivateObjectStorage


class Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class ExistingClient:
    def __init__(self, body: bytes, metadata_hash: str) -> None:
        self.body = body
        self.metadata_hash = metadata_hash

    def stat_object(self, bucket: str, key: str) -> object:
        del bucket, key
        return SimpleNamespace(
            size=len(self.body), metadata={"x-amz-meta-sha256": self.metadata_hash}
        )

    def get_object(self, bucket: str, key: str) -> Response:
        del bucket, key
        return Response(self.body)

    def _execute(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise S3Error(None, "PreconditionFailed", "exists", None, None, None)


def test_existing_catalog_object_requires_byte_hash_not_just_metadata() -> None:
    expected = b"expected"
    digest = hashlib.sha256(expected).hexdigest()
    client = ExistingClient(b"tampered", digest)
    storage = PrivateObjectStorage(client, "bucket")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="integrity"):
        storage.put_at_key("voice-catalog/v1/voice/avatar.png", expected, "image/png", digest)


def test_get_verified_rejects_wrong_size_or_hash() -> None:
    client = ExistingClient(b"body", "unused")
    storage = PrivateObjectStorage(client, "bucket")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="integrity"):
        storage.get_verified("key", "0" * 64, 4)


def test_ensure_private_bucket_is_idempotent() -> None:
    class Client:
        def __init__(self) -> None:
            self.exists = False
            self.created = 0
            self.policy_deletes = 0

        def bucket_exists(self, bucket: str) -> bool:
            assert bucket == "private"
            return self.exists

        def make_bucket(self, bucket: str) -> None:
            assert bucket == "private"
            self.created += 1
            self.exists = True

        def delete_bucket_policy(self, bucket: str) -> None:
            assert bucket == "private"
            self.policy_deletes += 1

    client = Client()
    storage = PrivateObjectStorage(client, "private")  # type: ignore[arg-type]
    storage.ensure_private_bucket()
    storage.ensure_private_bucket()
    assert client.created == 1
    assert client.policy_deletes == 2


def test_concurrent_conflicting_exact_writes_preserve_one_verified_winner() -> None:
    class ConditionalClient:
        def __init__(self) -> None:
            self._lock = Lock()
            self.body: bytes | None = None

        def _execute(
            self,
            method: str,
            bucket: str,
            key: str,
            *,
            body: bytes,
            headers: dict[str, str],
            no_body_trace: bool,
        ) -> Response:
            assert no_body_trace is True
            assert (method, bucket, key, headers["If-None-Match"]) == (
                "PUT",
                "private",
                "voice-catalog/v1/voice/reference.zip",
                "*",
            )
            with self._lock:
                if self.body is not None:
                    raise S3Error(None, "PreconditionFailed", "exists", None, None, None)
                self.body = body
            return Response(b"")

        def get_object(self, bucket: str, key: str) -> Response:
            del bucket, key
            assert self.body is not None
            return Response(self.body)

    client = ConditionalClient()
    storage = PrivateObjectStorage(client, "private")  # type: ignore[arg-type]
    first, second = b"first-voice-zip", b"second-voice-zip"

    def write(body: bytes) -> bool:
        try:
            return storage.put_at_key(
                "voice-catalog/v1/voice/reference.zip",
                body,
                "application/zip",
                hashlib.sha256(body).hexdigest(),
            ).created
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(write, (first, second)))

    assert sorted(results) == [False, True]
    assert client.body in {first, second}
    assert (
        storage.get_verified(
            "voice-catalog/v1/voice/reference.zip",
            hashlib.sha256(client.body).hexdigest(),
            len(client.body),
        )
        == client.body
    )
