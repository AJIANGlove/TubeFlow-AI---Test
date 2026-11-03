"""封面锚图出图适配器（grsai，v6）：参考图以图像条件随任务传入。

同步封装 grsai 异步任务：上传参考图（`/client/resource/newUploadTokenZH`）
→ 提交（nano-banana 系 `/v1/draw/nano-banana`，其余 `/v1/draw/completions`）
→ 轮询 `/v1/draw/result` → 下载结果字节。合同知识与故事板生产在用的
`adapters/grsai_grid_sheet.py` 一致（封面自包含实现，不反向依赖故事板模块）。

全部失败映射为封面既有稳定码族（DomainError），由服务落 FAILED：
不自动重提、不自动换模型；远端任务提交成功后的本地失败不替代提交。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from tubeflow_test.application.cover_providers import CoverImageResult
from tubeflow_test.domain.errors import DomainError

_POLL_INTERVAL_SECONDS = 5.0
_MODERATION_CODES = {"input_moderation", "output_moderation", "content_against_policy"}


class GrsaiCoverAnchorImageProvider:
    """封面锚图出图通道（grsai，模型默认 nano-banana-pro）。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        model: str,
        timeout_seconds: float = 60.0,
        upload_timeout_seconds: float = 120.0,
        poll_timeout_seconds: float = 420.0,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_download_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._model = model
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._upload_timeout = upload_timeout_seconds
        self._poll_timeout = poll_timeout_seconds
        self._sleeper = sleeper
        self._max_download_bytes = max_download_bytes

    def close(self) -> None:
        self._client.close()

    def generate(self, *, prompt: str, references: tuple[bytes, ...]) -> CoverImageResult:
        urls = [self._upload_reference(content) for content in references]
        task_id = self._submit(prompt, urls)
        url = self._poll(task_id)
        content = self._download(url)
        return CoverImageResult(
            content, hashlib.sha256(content).hexdigest(), task_id, "url"
        )

    def _upload_reference(self, content: bytes) -> str:
        try:
            token_response = self._client.post(
                f"{self._base_url}/client/resource/newUploadTokenZH",
                json={"sux": "png"},
                headers=self._headers,
                timeout=self._upload_timeout,
            )
        except httpx.ConnectError as exc:
            raise DomainError("cover_image_provider_network", "封面锚图上传未送达。") from exc
        except httpx.TimeoutException as exc:
            raise DomainError("cover_image_provider_timeout", "封面锚图上传超时。") from exc
        self._raise_for_status(token_response)
        token_data = token_response.json().get("data")
        if not isinstance(token_data, Mapping):
            raise DomainError("cover_image_response_invalid", "封面锚图上传凭证不完整。")
        upload_url = str(token_data.get("url") or "")
        key = str(token_data.get("key") or "")
        domain = str(token_data.get("domain") or "").rstrip("/")
        upload_token = str(token_data.get("token") or "")
        if not upload_url or not key or not domain or not upload_token:
            raise DomainError("cover_image_response_invalid", "封面锚图上传凭证不完整。")
        try:
            upload_response = self._client.post(
                upload_url,
                data={"token": upload_token, "key": key},
                files={"file": ("cover-anchor.png", content, "image/png")},
                timeout=self._upload_timeout,
            )
        except httpx.ConnectError as exc:
            raise DomainError("cover_image_provider_network", "封面锚图上传未送达。") from exc
        except httpx.TimeoutException as exc:
            raise DomainError("cover_image_provider_timeout", "封面锚图上传超时。") from exc
        self._raise_for_status(upload_response)
        return f"{domain}/{key}"

    def _submit(self, prompt: str, urls: list[str]) -> str:
        if self._model.startswith("nano-banana"):
            path = "/v1/draw/nano-banana"
            payload: dict[str, Any] = {
                "model": self._model,
                "prompt": prompt,
                "urls": urls,
                "aspectRatio": "16:9",
                "imageSize": "2K",
                "webHook": "-1",
                "shutProgress": True,
            }
        else:
            path = "/v1/draw/completions"
            payload = {
                "model": self._model,
                "prompt": prompt,
                "urls": urls,
                "aspectRatio": "1672x941",
                "quality": "high",
                "webHook": "-1",
                "shutProgress": True,
            }
        try:
            response = self._client.post(
                f"{self._base_url}{path}", json=payload, headers=self._headers
            )
        except httpx.ConnectError as exc:
            # 连接未建立（可证明未送达远端）
            raise DomainError("cover_image_provider_network", "封面出图请求未送达。") from exc
        except httpx.TimeoutException as exc:
            raise DomainError("cover_image_provider_timeout", "封面出图提交超时。") from exc
        self._raise_for_status(response)
        try:
            body: Any = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise DomainError(
                "cover_image_response_invalid", "封面出图响应不是合法 JSON。"
            ) from exc
        if not isinstance(body, dict):
            raise DomainError("cover_image_response_invalid", "封面出图响应外层不是 JSON 对象。")
        data = body.get("data")
        task_id = str(data.get("id") or "").strip() if isinstance(data, Mapping) else ""
        if task_id:
            return task_id
        if body.get("code") is not None and body.get("code") != 0:
            raise self._business_error(body)
        raise DomainError("cover_image_response_invalid", "封面出图响应缺少任务 ID。")

    def _poll(self, task_id: str) -> str:
        deadline = time.monotonic() + self._poll_timeout
        while time.monotonic() < deadline:
            try:
                response = self._client.post(
                    f"{self._base_url}/v1/draw/result",
                    json={"id": task_id},
                    headers=self._headers,
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                raise DomainError("cover_image_provider_network", "封面出图结果查询失败。") from exc
            self._raise_for_status(response)
            try:
                body: Any = response.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise DomainError(
                    "cover_image_response_invalid", "封面出图查询响应不是合法 JSON。"
                ) from exc
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, Mapping):
                raise DomainError("cover_image_response_invalid", "封面出图查询缺少任务数据。")
            status = str(data.get("status") or "")
            if status == "succeeded":
                results = data.get("results")
                url = (
                    str(results[0].get("url") or "")
                    if isinstance(results, list) and results and isinstance(results[0], Mapping)
                    else ""
                )
                if not url:
                    raise DomainError(
                        "cover_image_response_invalid", "封面出图成功但缺少结果地址。"
                    )
                return url
            if status == "failed":
                raise DomainError("cover_image_provider_failed", "封面出图远端任务失败。")
            self._sleeper(_POLL_INTERVAL_SECONDS)
        raise DomainError("cover_image_provider_timeout", "封面出图等待远端结果超时。")

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

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        raise DomainError(
            f"cover_image_provider_http_{status}", f"封面出图请求失败（HTTP {status}）。"
        )

    @staticmethod
    def _business_error(body: Mapping[str, Any]) -> DomainError:
        error = body.get("error")
        failure = ""
        if isinstance(error, Mapping):
            failure = str(error.get("type") or error.get("message") or error.get("code") or "")
        if not failure:
            failure = str(
                body.get("msg") or body.get("message") or body.get("failure_reason") or ""
            )
        code = failure.strip()[:128]
        if code in _MODERATION_CODES:
            return DomainError("cover_image_provider_rejected", "封面出图被内容审核拒绝。")
        if code == "insufficient_balance" or body.get("code") in (402, 1001):
            return DomainError("cover_image_provider_http_402", "封面出图余额不足。")
        return DomainError(
            "cover_image_provider_rejected", "封面出图被 Provider 拒绝。"
        )
