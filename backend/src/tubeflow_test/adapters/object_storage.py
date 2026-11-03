from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from inspect import signature
from io import BytesIO
from pathlib import PurePosixPath
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

_SUPPORTED_MINIO_VERSION = "7.2.20"


class ConditionalWriteConflict(RuntimeError):
    """A conditional create raced, but no identical committed winner is observable yet."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int
    media_type: str
    created: bool = True


class PrivateObjectStorage:
    """Private MinIO adapter. Business code persists returned exact keys."""

    def __init__(self, client: Minio, bucket: str) -> None:
        if isinstance(client, Minio):
            _validate_minio_conditional_api()
        self._client = client
        self._bucket = bucket

    @property
    def client(self) -> Minio:
        return self._client

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_private_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            try:
                self._client.make_bucket(self._bucket)
            except S3Error as error:
                if error.code not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                    raise
        # A previously configured anonymous policy survives bucket reuse. Removing
        # it is idempotent in MinIO and makes startup fail closed on authorization
        # or connectivity errors instead of silently serving private media.
        self._client.delete_bucket_policy(self._bucket)

    def ensure_bucket(self) -> None:
        """Compatibility spelling for workers; the bucket is always private."""
        self.ensure_private_bucket()

    def put(self, body: bytes, media_type: str, *, object_key: str | None = None) -> StoredObject:
        if not body:
            raise ValueError("empty objects are not accepted")
        return self.put_private_at_key(object_key or str(uuid4()), body, media_type)

    def put_exact(self, key: str, body: bytes, media_type: str) -> StoredObject:
        return self.put_private_at_key(key, body, media_type)

    def put_private_at_key(self, key: str, body: bytes, media_type: str) -> StoredObject:
        _validate_exact_key(key)
        if not body:
            raise ValueError("empty objects are not accepted")
        digest = sha256(body).hexdigest()
        self._client.put_object(
            self._bucket,
            key,
            BytesIO(body),
            len(body),
            content_type=media_type,
            metadata={"sha256": digest},
        )
        return StoredObject(key, digest, len(body), media_type)

    def put_at_key(
        self, key: str, body: bytes, media_type: str, expected_sha256: str
    ) -> StoredObject:
        """Atomically create an immutable exact key, or verify the existing winner."""
        _validate_exact_key(key)
        digest = sha256(body).hexdigest()
        if not body or digest != expected_sha256:
            raise ValueError("object SHA-256 integrity mismatch")
        try:
            response = self._client._execute(
                "PUT",
                self._bucket,
                key,
                body=body,
                headers={
                    "Content-Length": str(len(body)),
                    "Content-Type": media_type,
                    "If-None-Match": "*",
                    "X-Amz-Meta-Sha256": digest,
                },
                no_body_trace=True,
            )
        except S3Error as error:
            if error.code == "PreconditionFailed":
                self.get_verified(key, expected_sha256, len(body))
                return StoredObject(key, digest, len(body), media_type, created=False)
            if error.code != "ConditionalRequestConflict":
                raise
            try:
                self.get_verified(key, expected_sha256, len(body))
            except (S3Error, ValueError) as reconcile_error:
                raise ConditionalWriteConflict(
                    "conditional object write conflicted without a matching committed winner"
                ) from reconcile_error
            return StoredObject(key, digest, len(body), media_type, created=False)
        response.close()
        response.release_conn()
        # Verify the server committed the exact bytes, rather than trusting only
        # request success or mutable object metadata.
        self.get_verified(key, expected_sha256, len(body))
        return StoredObject(key, digest, len(body), media_type)

    def get(self, key: str) -> bytes:
        _validate_exact_key(key)
        response = self._client.get_object(self._bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def get_verified(self, key: str, expected_sha256: str, expected_size: int) -> bytes:
        payload = self.get(key)
        if len(payload) != expected_size or sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("object SHA-256 integrity mismatch")
        return payload

    def exists(self, key: str) -> bool:
        _validate_exact_key(key)
        try:
            self._client.stat_object(self._bucket, key)
        except S3Error as error:
            if error.code in {"NoSuchBucket", "NoSuchKey", "NoSuchObject"}:
                return False
            raise
        return True

    def delete_exact(self, key: str) -> None:
        _validate_exact_key(key)
        self._client.remove_object(self._bucket, key)


def _validate_exact_key(key: str) -> None:
    """Allow explicit hierarchy while rejecting prefixes, traversal, and wildcards."""
    if not key or "\\" in key or "\x00" in key or any(char in key for char in "*?[]"):
        raise ValueError("object key must identify one exact safe object")
    path = PurePosixPath(key)
    if (
        path.is_absolute()
        or path.as_posix() != key
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in path.parts)
    ):
        raise ValueError("object key must identify one exact safe object")


def _validate_minio_conditional_api() -> None:
    installed = version("minio")
    parameters = signature(Minio._execute).parameters
    if installed != _SUPPORTED_MINIO_VERSION or "no_body_trace" not in parameters:
        raise RuntimeError(
            "unsupported MinIO client contract: conditional writes require "
            f"minio=={_SUPPORTED_MINIO_VERSION} with _execute(no_body_trace=...)"
        )
