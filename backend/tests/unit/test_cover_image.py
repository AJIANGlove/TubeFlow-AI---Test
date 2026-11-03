from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from tubeflow_test.adapters.cover_image import LingkeaiCoverImageProvider
from tubeflow_test.domain.errors import DomainError

TOKEN = "cover-image-secret-token"
PROMPT = "主体：人物身份：测试主体\n画面中加入标题文字「测试」"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
IMAGE_URL = "https://cdn.example.com/cover/abc.png"


def _provider(handler, **overrides: object) -> LingkeaiCoverImageProvider:
    kwargs: dict[str, object] = {
        "model": "gpt-image-2",
        "client": httpx.Client(transport=httpx.MockTransport(handler)),
    }
    kwargs.update(overrides)
    return LingkeaiCoverImageProvider("https://api.lingkeai.ai/v1", TOKEN, **kwargs)


def _b64_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "req-image"},
        json={"data": [{"b64_json": base64.b64encode(IMAGE_BYTES).decode()}]},
        request=request,
    )


def test_image_request_contract_b64_branch() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _b64_response(request)

    result = _provider(handler).generate(prompt=PROMPT)

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/images/generations"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body == {
        "model": "gpt-image-2",
        "prompt": PROMPT,
        "size": "2560x1440",
        "quality": "high",
        "n": 1,
    }
    assert result.content == IMAGE_BYTES
    assert result.sha256 == hashlib.sha256(IMAGE_BYTES).hexdigest()
    assert result.source == "b64_json"
    assert result.receipt == "req-image"


def test_image_url_branch_downloads_and_hashes_bytes() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200, json={"data": [{"url": IMAGE_URL}]}, request=request
            )
        return httpx.Response(200, content=IMAGE_BYTES, request=request)

    result = _provider(handler).generate(prompt=PROMPT)

    assert seen_urls == ["https://api.lingkeai.ai/v1/images/generations", IMAGE_URL]
    assert result.content == IMAGE_BYTES
    assert result.sha256 == hashlib.sha256(IMAGE_BYTES).hexdigest()
    assert result.source == "url"


@pytest.mark.parametrize("status", (401, 403, 429, 500, 503, 400, 422))
def test_image_http_errors_mapped_without_retry(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).generate(prompt=PROMPT)
    assert captured.value.code == f"cover_image_provider_http_{status}"
    assert calls == 1  # 出图通道不自动重试
    assert TOKEN not in str(captured.value)
    assert PROMPT not in str(captured.value)


def test_image_timeout_mapped_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).generate(prompt=PROMPT)

    assert captured.value.code == "cover_image_provider_timeout"
    assert calls == 1


def test_image_connect_error_mapped_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).generate(prompt=PROMPT)

    assert captured.value.code == "cover_image_provider_network"
    assert calls == 1


@pytest.mark.parametrize(
    "envelope",
    (
        {"data": []},
        {"data": [{}]},
        {"data": [{"b64_json": "  ", "url": ""}]},
        {"data": [{"b64_json": "!!!not-base64!!!"}]},
        {"result": "ok"},
    ),
)
def test_image_invalid_response_contract(envelope: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope, request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).generate(prompt=PROMPT)

    assert captured.value.code == "cover_image_response_invalid"


def test_image_non_json_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>error page</html>", request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).generate(prompt=PROMPT)

    assert captured.value.code == "cover_image_response_invalid"


def test_image_download_failure_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200, json={"data": [{"url": IMAGE_URL}]}, request=request
            )
        return httpx.Response(404, request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).generate(prompt=PROMPT)

    assert captured.value.code == "cover_image_download_failed"


def test_image_download_size_limit() -> None:
    big = b"0" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200, json={"data": [{"url": IMAGE_URL}]}, request=request
            )
        return httpx.Response(200, content=big, request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler, max_download_bytes=512).generate(prompt=PROMPT)

    assert captured.value.code == "cover_image_download_failed"


def test_image_download_empty_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/images/generations":
            return httpx.Response(
                200, json={"data": [{"url": IMAGE_URL}]}, request=request
            )
        return httpx.Response(200, content=b"", request=request)

    with pytest.raises(DomainError) as captured:
        _provider(handler).generate(prompt=PROMPT)

    assert captured.value.code == "cover_image_download_failed"
