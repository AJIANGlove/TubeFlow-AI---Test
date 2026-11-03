"""封面图生成适配器（lingkeai，OpenAI 兼容 images/generations）。

POST `{base_url}/images/generations`，body `{model, prompt, size, quality, n:1}`；
解析 `data[0].b64_json` 或 `data[0].url`（url 时下载字节并计算 sha256）。
同步请求-响应，无远端任务对账；不自动换模型、不自动重试（计划 §5/§1.7）。
超时/连接错/错误状态一律映射为稳定 DomainError 码，不含 token 与提示词。
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any

import httpx

from tubeflow_test.application.cover_providers import CoverImageResult
from tubeflow_test.domain.errors import DomainError


class LingkeaiCoverImageProvider:
    """封面出图通道（gpt-image-2）：一次性请求，失败如实抛稳定码。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        model: str,
        size: str = "2560x1440",
        quality: str = "high",
        timeout_seconds: float = 180.0,
        client: httpx.Client | None = None,
        max_download_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._authorization = f"Bearer {token}"
        self._model = model
        self._size = size
        self._quality = quality
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._max_download_bytes = max_download_bytes

    def close(self) -> None:
        self._client.close()

    def generate(self, *, prompt: str) -> CoverImageResult:
        try:
            response = self._client.post(
                f"{self._base_url}/images/generations",
                headers={"Authorization": self._authorization},
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "size": self._size,
                    "quality": self._quality,
                    "n": 1,
                },
            )
        except httpx.TimeoutException as exc:
            raise DomainError("cover_image_provider_timeout", "封面出图请求超时。") from exc
        except httpx.TransportError as exc:
            raise DomainError("cover_image_provider_network", "无法连接封面出图服务。") from exc
        status = response.status_code
        if status >= 400:
            raise DomainError(
                f"cover_image_provider_http_{status}", f"封面出图请求失败（HTTP {status}）。"
            )
        try:
            envelope: Any = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise DomainError(
                "cover_image_response_invalid", "封面出图响应不是合法 JSON。"
            ) from exc
        if not isinstance(envelope, dict):
            raise DomainError("cover_image_response_invalid", "封面出图响应外层不是 JSON 对象。")
        receipt = _receipt(response, envelope)
        content, source = self._image_bytes(envelope)
        return CoverImageResult(content, hashlib.sha256(content).hexdigest(), receipt, source)

    def _image_bytes(self, envelope: Mapping[str, Any]) -> tuple[bytes, str]:
        data = envelope.get("data")
        first: Any = data[0] if isinstance(data, list) and data else None
        if not isinstance(first, Mapping):
            raise DomainError("cover_image_response_invalid", "封面出图响应缺少图片数据。")
        b64 = first.get("b64_json")
        if isinstance(b64, str) and b64.strip():
            try:
                return base64.b64decode(b64), "b64_json"
            except ValueError as exc:
                raise DomainError(
                    "cover_image_response_invalid", "封面出图 b64_json 无法解码。"
                ) from exc
        url = first.get("url")
        if isinstance(url, str) and url.strip():
            return self._download(url.strip()), "url"
        raise DomainError(
            "cover_image_response_invalid", "封面出图响应缺少 b64_json 或 url。"
        )

    def _download(self, url: str) -> bytes:
        try:
            response = self._client.get(url)
        except httpx.TimeoutException as exc:
            raise DomainError("cover_image_download_failed", "封面图下载超时。") from exc
        except httpx.TransportError as exc:
            raise DomainError("cover_image_download_failed", "封面图下载连接失败。") from exc
        if response.status_code >= 400:
            raise DomainError(
                "cover_image_download_failed", f"封面图下载失败（HTTP {response.status_code}）。"
            )
        payload = response.content
        if not payload or len(payload) > self._max_download_bytes:
            raise DomainError("cover_image_download_failed", "封面图为空或超出字节上限。")
        return payload


def _receipt(response: httpx.Response, envelope: Mapping[str, Any]) -> str:
    header_receipt = str(response.headers.get("x-request-id", "")).strip()
    if header_receipt:
        return header_receipt[:512]
    envelope_id = envelope.get("id")
    if isinstance(envelope_id, str) and envelope_id.strip():
        return envelope_id.strip()[:512]
    return ""
