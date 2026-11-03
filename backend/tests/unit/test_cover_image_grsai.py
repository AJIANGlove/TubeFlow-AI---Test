"""grsai 封面锚图适配器测试：httpx.MockTransport 全链路（零真实网络）。

覆盖：上传→提交→轮询→下载顺利路径（两模型家族请求合同）、
错误映射（429/401/402/审核拒绝/缺任务 ID/远端失败/轮询超时/下载失败）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from tubeflow_test.adapters.cover_image_grsai import GrsaiCoverAnchorImageProvider
from tubeflow_test.domain.errors import DomainError

BASE = "https://grsai.test"
PROMPT = "封面提示词"
REF = b"anchor-bytes"
IMAGE_BYTES = b"fake-image-png"


def _provider(handler, *, model: str = "nano-banana-pro") -> GrsaiCoverAnchorImageProvider:
    transport = httpx.MockTransport(handler)
    return GrsaiCoverAnchorImageProvider(
        BASE,
        "token",
        model=model,
        client=httpx.Client(transport=transport, timeout=10.0),
        sleeper=lambda _s: None,
        poll_timeout_seconds=5.0,
    )


def _happy_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/client/resource/newUploadTokenZH":
        return httpx.Response(
            200,
            json={
                "data": {
                    "url": f"{BASE}/upload",
                    "key": "k/anchor.png",
                    "domain": "https://cdn.test",
                    "token": "up-token",
                }
            },
        )
    if path == "/upload":
        return httpx.Response(200, json={"code": 0})
    if path == "/v1/draw/nano-banana":
        body = json.loads(request.content)
        assert body["aspectRatio"] == "16:9" and body["imageSize"] == "2K"
        assert body["urls"] == ["https://cdn.test/k/anchor.png"]
        return httpx.Response(200, json={"code": 0, "data": {"id": "task-nb-1"}})
    if path == "/v1/draw/completions":
        body = json.loads(request.content)
        assert body["aspectRatio"] == "1672x941" and body["quality"] == "high"
        return httpx.Response(200, json={"code": 0, "data": {"id": "task-gpt-1"}})
    if path == "/v1/draw/result":
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"status": "succeeded", "results": [{"url": f"{BASE}/img.png"}]},
            },
        )
    if path == "/img.png":
        return httpx.Response(200, content=IMAGE_BYTES)
    raise AssertionError(f"unexpected request {path}")


def test_anchor_happy_path_nano_banana() -> None:
    provider = _provider(_happy_handler)
    result = provider.generate(prompt=PROMPT, references=(REF,))
    assert result.content == IMAGE_BYTES
    assert result.receipt == "task-nb-1"
    assert result.source == "url"
    import hashlib

    assert result.sha256 == hashlib.sha256(IMAGE_BYTES).hexdigest()


def test_anchor_happy_path_gpt_completions() -> None:
    provider = _provider(_happy_handler, model="gpt-image-2.5")
    result = provider.generate(prompt=PROMPT, references=(REF,))
    assert result.receipt == "task-gpt-1"


def test_submit_http_error_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/resource/newUploadTokenZH":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "url": f"{BASE}/u", "key": "k",
                        "domain": "https://cdn.test", "token": "t",
                    }
                },
            )
        if request.url.path == "/u":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(429, json={"code": 429, "msg": "throttled"})

    provider = _provider(handler)
    with pytest.raises(DomainError) as caught:
        provider.generate(prompt=PROMPT, references=(REF,))
    assert caught.value.code == "cover_image_provider_http_429"


def test_submit_business_moderation_and_balance_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/resource/newUploadTokenZH":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "url": f"{BASE}/u", "key": "k",
                        "domain": "https://cdn.test", "token": "t",
                    }
                },
            )
        if request.url.path == "/u":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 1, "error": {"type": "input_moderation"}})

    with pytest.raises(DomainError) as caught:
        _provider(handler).generate(prompt=PROMPT, references=(REF,))
    assert caught.value.code == "cover_image_provider_rejected"

    def balance_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/resource/newUploadTokenZH":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "url": f"{BASE}/u", "key": "k",
                        "domain": "https://cdn.test", "token": "t",
                    }
                },
            )
        if request.url.path == "/u":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 402})

    with pytest.raises(DomainError) as caught2:
        _provider(balance_handler).generate(prompt=PROMPT, references=(REF,))
    assert caught2.value.code == "cover_image_provider_http_402"


def test_submit_missing_task_id_and_poll_failure_mapping() -> None:
    def no_task_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/client/resource/newUploadTokenZH":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "url": f"{BASE}/u", "key": "k",
                        "domain": "https://cdn.test", "token": "t",
                    }
                },
            )
        if request.url.path == "/u":
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 0, "data": {}})

    with pytest.raises(DomainError) as caught:
        _provider(no_task_handler).generate(prompt=PROMPT, references=(REF,))
    assert caught.value.code == "cover_image_response_invalid"

    def failed_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/draw/result":
            return httpx.Response(
                200, json={"code": 0, "data": {"status": "failed", "failure_reason": "boom"}}
            )
        return _happy_handler(request)

    with pytest.raises(DomainError) as caught2:
        _provider(failed_handler).generate(prompt=PROMPT, references=(REF,))
    assert caught2.value.code == "cover_image_provider_failed"


def test_poll_timeout_and_download_failure_mapping() -> None:
    def running_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/draw/result":
            return httpx.Response(200, json={"code": 0, "data": {"status": "running"}})
        return _happy_handler(request)

    with pytest.raises(DomainError) as caught:
        _provider(running_handler).generate(prompt=PROMPT, references=(REF,))
    assert caught.value.code == "cover_image_provider_timeout"

    def bad_download(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/img.png":
            return httpx.Response(404)
        return _happy_handler(request)

    with pytest.raises(DomainError) as caught2:
        _provider(bad_download).generate(prompt=PROMPT, references=(REF,))
    assert caught2.value.code == "cover_image_download_failed"


def test_upload_incomplete_token_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"url": "", "key": "", "domain": "", "token": ""}})

    with pytest.raises(DomainError) as caught:
        _provider(handler).generate(prompt=PROMPT, references=(REF,))
    assert caught.value.code == "cover_image_response_invalid"
