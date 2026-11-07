"""Integration tests create buckets only in a MinIO that is not the application's (2026-11-06).

Integration tests that use real MinIO create buckets, write objects and delete them. A
test MinIO (`tubeflow-contract-minio`, 127.0.0.1:59000 on the deployment machine) now
exists, so a mistyped endpoint or tunnel could point those tests at the application MinIO.
Every integration test that builds a MinIO client calls `refuse_application_object_store`
before it creates a bucket.

The rule: the target must not hold the application bucket. The application bucket is the
`minio_bucket` default in `tubeflow_test.config.Settings`; the test MinIO starts with no
buckets and the tests only create uniquely named ones, so it never holds that bucket.
"""

from __future__ import annotations

from typing import Protocol

import pytest

APPLICATION_BUCKET = "tubeflow-private"


class _BucketLookup(Protocol):
    def bucket_exists(self, bucket_name: str) -> bool: ...


def refuse_application_object_store(client: _BucketLookup) -> None:
    if client.bucket_exists(APPLICATION_BUCKET):
        pytest.fail(
            f"TUBEFLOW_TEST_MINIO_ENDPOINT points at a MinIO that holds the application bucket "
            f"{APPLICATION_BUCKET!r}; integration tests refuse to create or delete buckets or "
            "objects there",
            pytrace=False,
        )
