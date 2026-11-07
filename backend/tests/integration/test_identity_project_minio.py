from __future__ import annotations

import hashlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from minio import Minio
from minio.error import S3Error
from object_store_guard import refuse_application_object_store

from tubeflow_test.adapters.object_storage import PrivateObjectStorage


@pytest.mark.integration
def test_real_minio_bucket_and_verified_round_trip() -> None:
    endpoint = os.environ.get("TUBEFLOW_TEST_MINIO_ENDPOINT")
    if not endpoint:
        pytest.skip("TUBEFLOW_TEST_MINIO_ENDPOINT is not configured")
    access_key = os.environ.get("TUBEFLOW_TEST_MINIO_ACCESS_KEY")
    secret_key = os.environ.get("TUBEFLOW_TEST_MINIO_SECRET_KEY")
    if not access_key or not secret_key:
        pytest.fail("real MinIO tests require explicit access and secret key environment variables")
    bucket = f"identity-test-{uuid4().hex}"
    client = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=False,
    )
    refuse_application_object_store(client)
    storage = PrivateObjectStorage(client, bucket)
    storage.ensure_private_bucket()
    client.set_bucket_policy(
        bucket,
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket}/*"],
                    }
                ],
            }
        ),
    )
    storage.ensure_private_bucket()
    with pytest.raises(S3Error) as policy_error:
        client.get_bucket_policy(bucket)
    assert policy_error.value.code == "NoSuchBucketPolicy"
    body = b"verified-media"
    digest = hashlib.sha256(body).hexdigest()
    key = "voice-catalog/v1/test-voice/preview.wav"
    try:
        created = storage.put_at_key(key, body, "audio/wav", digest)
        existing = storage.put_at_key(key, body, "audio/wav", digest)
        assert created.created is True
        assert existing.created is False
        assert storage.get_verified(key, digest, len(body)) == body
        trace = io.StringIO()
        client.trace_on(trace)
        binary = b"\xff\xfe\x00conditional-binary"
        binary_key = "voice-catalog/v1/test-voice/binary.bin"
        binary_digest = hashlib.sha256(binary).hexdigest()
        binary_created = storage.put_at_key(
            binary_key, binary, "application/octet-stream", binary_digest
        )
        client.trace_off()
        assert binary_created.created
        assert storage.get_verified(binary_key, binary_digest, len(binary)) == binary
        assert "conditional-binary" not in trace.getvalue()
        concurrent_key = "voice-catalog/v1/test-voice/concurrent.bin"
        concurrent_body = b"one-immutable-winner"
        concurrent_digest = hashlib.sha256(concurrent_body).hexdigest()

        def create_once(_ordinal: int) -> bool:
            return storage.put_at_key(
                concurrent_key,
                concurrent_body,
                "application/octet-stream",
                concurrent_digest,
            ).created

        with ThreadPoolExecutor(max_workers=32) as pool:
            winners = tuple(pool.map(create_once, range(32)))
        assert winners.count(True) == 1
        assert storage.get_verified(
            concurrent_key, concurrent_digest, len(concurrent_body)
        ) == concurrent_body
    finally:
        storage.delete_exact(key)
        storage.delete_exact("voice-catalog/v1/test-voice/binary.bin")
        storage.delete_exact("voice-catalog/v1/test-voice/concurrent.bin")
        client.remove_bucket(bucket)
