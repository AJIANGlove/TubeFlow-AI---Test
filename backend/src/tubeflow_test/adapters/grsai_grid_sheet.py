from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import cast

import httpx

from tubeflow_test.application.storyboards import (
    GridSheetReference,
    ProviderChannelUnavailableError,
    ProviderPoll,
    ProviderReconcileUnsupportedError,
    ProviderSubmission,
    ProviderSubmitNotDeliveredError,
    ProviderSubmitRejectedError,
    ProviderSubmitServerError,
    ProviderSubmitThrottledError,
    provider_safe_error_text,
)
from tubeflow_test.domain.storyboards import GenerationStatus, StoryboardGeneration

# grsai gpt-image 官方 1K 像素档（见 grsai gpt-image 文档的 Size 列表）。
_ASPECT_RATIO_1K_PIXELS = {"16:9": "1672x941", "9:16": "941x1672"}
_DEFAULT_QUALITY = "high"


class GrsaiGridSheetImageProvider:
    """Binds the grsai gpt-image task API to the GridSheetImageProvider port.

    Reference images are uploaded to grsai's file service first because the
    provider must fetch them over the public internet. grsai cannot look tasks
    up by our idempotency key, so reconcile() reports an unsupported capability;
    submit failures are classified so the service can finalize attempts that
    provably never reached the provider instead of blocking re-submission forever.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 30.0,
        *,
        client: httpx.Client | None = None,
        upload_timeout_seconds: float = 120.0,
        max_download_bytes: int = 64 * 1024 * 1024,
        default_size: str = "16:9",
        max_concurrency: int = 20,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._upload_timeout = upload_timeout_seconds
        self._max_download_bytes = max_download_bytes
        self._default_size = default_size
        self._submit_slots = threading.BoundedSemaphore(max_concurrency)
        self._download_cache: dict[str, tuple[bytes, str]] = {}
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[GridSheetReference, ...] = ()
    ) -> ProviderSubmission:
        with self._submit_slots:
            return self._submit(generation, references)

    def _submit(
        self, generation: StoryboardGeneration, references: tuple[GridSheetReference, ...]
    ) -> ProviderSubmission:
        urls = [self._upload_reference(item) for item in references]
        aspect_ratio = str(generation.parameters.get("aspect_ratio") or self._default_size)
        if aspect_ratio not in _ASPECT_RATIO_1K_PIXELS:
            raise ValueError(f"unsupported aspect ratio for grsai grid sheet: {aspect_ratio!r}")
        if generation.model in {"nano-banana-2", "nano-banana-pro"}:
            # Nano Banana 与 GPT 的请求合同不同：比例直接传 16:9，清晰度使用 imageSize。
            body = self._submit_request(
                "/v1/draw/nano-banana",
                {
                    "model": generation.model,
                    "prompt": generation.prompt,
                    "urls": urls,
                    "aspectRatio": aspect_ratio,
                    "imageSize": str(generation.parameters.get("imageSize") or "2K"),
                    "webHook": "-1",
                    "shutProgress": True,
                },
            )
        else:
            quality = str(generation.parameters.get("quality") or _DEFAULT_QUALITY)
            body = self._submit_request(
                "/v1/draw/completions",
                {
                    "model": generation.model,
                    "prompt": generation.prompt,
                    "urls": urls,
                    "aspectRatio": _ASPECT_RATIO_1K_PIXELS[aspect_ratio],
                    "quality": quality,
                    "webHook": "-1",
                    "shutProgress": True,
                },
            )
        data = body.get("data")
        task_id = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
        if not task_id:
            # 2xx 但缺少可信任务身份：无法判断是否已受理，不能当作可重提失败。
            raise ValueError("grsai submit returned no task id")
        return ProviderSubmission(task_id, json.dumps(body, ensure_ascii=False))

    def reconcile(self, idempotency_key: str) -> ProviderSubmission | None:
        del idempotency_key
        raise ProviderReconcileUnsupportedError("grsai cannot reconcile without a task id")

    def poll(self, task_id: str) -> ProviderPoll:
        body = self._query_request("/v1/draw/result", {"id": task_id})
        receipt = json.dumps(body, ensure_ascii=False)
        data = body.get("data")
        if not isinstance(data, dict):
            raise ValueError("grsai query returned no task data")
        status = str(data.get("status") or "")
        if status == "succeeded":
            results = data.get("results")
            url = ""
            if isinstance(results, list) and results and isinstance(results[0], dict):
                url = str(results[0].get("url") or "")
            if not url:
                raise ValueError("grsai succeeded without a result url")
            payload, digest = self._fetch(url)
            with self._cache_lock:
                self._download_cache[url] = (payload, digest)
            return ProviderPoll(
                GenerationStatus.SUCCEEDED,
                receipt,
                download_token=url,
                expected_sha256=digest,
            )
        if status == "failed":
            failure = provider_safe_error_text(
                data.get("error") or data.get("failure_reason") or "provider_failed"
            )
            return ProviderPoll(GenerationStatus.FAILED, receipt, failure_code=failure)
        return ProviderPoll(GenerationStatus.RUNNING, receipt)

    def download(self, download_token: str, destination: Path) -> None:
        with self._cache_lock:
            cached = self._download_cache.pop(download_token, None)
        payload = cached[0] if cached is not None else self._fetch(download_token)[0]
        destination.write_bytes(payload)

    def _submit_request(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        try:
            response = self._client.post(
                f"{self._base_url}{path}", json=payload, headers=self._headers
            )
        except httpx.ConnectError as exc:
            raise ProviderSubmitNotDeliveredError("grsai submit was not delivered") from exc
        if response.status_code >= 400:
            _raise_submit_http_error(response, "submit")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("grsai returned a non-object submit response")
        body = cast(dict[str, object], body)
        if _submission_from_body(body) is not None:
            return body
        if body.get("code") is None:
            raise ValueError("grsai submit response has no status code")
        if body.get("code") != 0:
            failure_code = _body_error_code(body)
            status = _business_status(body)
            if failure_code in {
                "input_moderation",
                "output_moderation",
                "content_against_policy",
            }:
                raise ProviderSubmitRejectedError(
                    f"grsai rejected the submit: {failure_code}", failure_code=failure_code
                )
            if status == 429:
                raise ProviderSubmitThrottledError(
                    "grsai throttled the submit: HTTP 429",
                    retry_after_seconds=_retry_after_seconds(response),
                )
            if status in {401, 402, 403, 1001} or failure_code == "insufficient_balance":
                raise ProviderChannelUnavailableError(
                    "grsai channel authentication or balance failed",
                    failure_code=(
                        "provider_quota_exhausted"
                        if status in {402, 1001} or failure_code == "insufficient_balance"
                        else "provider_auth_failed"
                    ),
                )
            if status is not None and status >= 500:
                raise ProviderSubmitServerError(
                    f"grsai submit failed: {status}",
                    retry_after_seconds=_retry_after_seconds(response),
                )
            raise ProviderSubmitRejectedError(
                f"grsai rejected the submit: {body.get('msg') or body}",
                failure_code=failure_code,
            )
        return body

    def _query_request(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        response = self._client.post(f"{self._base_url}{path}", json=payload, headers=self._headers)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("code") != 0:
            raise ValueError(f"grsai query failed: {body!r}")
        return cast(dict[str, object], body)

    def _upload_reference(self, item: GridSheetReference) -> str:
        suffix = item.media_type.removeprefix("image/").strip() or "png"
        try:
            token_response = self._client.post(
                f"{self._base_url}/client/resource/newUploadTokenZH",
                json={"sux": suffix},
                headers=self._headers,
                timeout=self._upload_timeout,
            )
        except httpx.ConnectError as exc:
            raise ProviderSubmitNotDeliveredError(
                "grsai reference upload was not delivered"
            ) from exc
        if token_response.status_code >= 400:
            _raise_submit_http_error(token_response, "reference upload token")
        token_body = token_response.json()
        token_data = token_body.get("data") if isinstance(token_body, dict) else None
        if not isinstance(token_data, dict):
            raise ProviderSubmitRejectedError("grsai returned an incomplete upload token")
        upload_url = str(token_data.get("url") or "")
        key = str(token_data.get("key") or "")
        domain = str(token_data.get("domain") or "").rstrip("/")
        upload_token = str(token_data.get("token") or "")
        if not upload_url or not key or not domain or not upload_token:
            raise ProviderSubmitRejectedError("grsai returned an incomplete upload token")
        try:
            upload_response = self._client.post(
                upload_url,
                data={"token": upload_token, "key": key},
                files={"file": (f"character-{item.ordinal}", item.content, item.media_type)},
                timeout=self._upload_timeout,
            )
        except httpx.ConnectError as exc:
            raise ProviderSubmitNotDeliveredError(
                "grsai reference upload was not delivered"
            ) from exc
        if upload_response.status_code >= 400:
            _raise_submit_http_error(upload_response, "reference upload")
        return f"{domain}/{key}"

    def _fetch(self, url: str) -> tuple[bytes, str]:
        response = self._client.get(url)
        response.raise_for_status()
        payload = response.content
        if not payload or len(payload) > self._max_download_bytes:
            raise ValueError("grsai result image is empty or exceeds the byte limit")
        return payload, hashlib.sha256(payload).hexdigest()


def _raise_submit_http_error(response: httpx.Response, operation: str) -> None:
    status = response.status_code
    body: dict[str, object] | None = None
    try:
        candidate = response.json()
        if isinstance(candidate, dict):
            body = cast(dict[str, object], candidate)
    except ValueError:
        body = None
    failure_code = _body_error_code(body) if body is not None else f"http_{status}"
    submission = _submission_from_body(body)
    if status == 429:
        raise ProviderSubmitThrottledError(
            f"grsai throttled the {operation}: HTTP 429",
            retry_after_seconds=_retry_after_seconds(response),
            submission=submission,
        )
    if status in {401, 402, 403}:
        if failure_code in {"input_moderation", "output_moderation", "content_against_policy"}:
            raise ProviderSubmitRejectedError(
                f"grsai rejected the {operation}: {failure_code}",
                failure_code=failure_code,
                submission=submission,
            )
        raise ProviderChannelUnavailableError(
            f"grsai {operation} failed: HTTP {status}",
            failure_code=_channel_failure_code(status),
            submission=submission,
        )
    if status >= 500:
        raise ProviderSubmitServerError(
            f"grsai {operation} failed: HTTP {status}",
            retry_after_seconds=_retry_after_seconds(response),
            submission=submission,
        )
    raise ProviderSubmitRejectedError(
        f"grsai rejected the {operation}: HTTP {status}",
        failure_code=failure_code,
        submission=submission,
    )


def _submission_from_body(body: dict[str, object] | None) -> ProviderSubmission | None:
    if body is None:
        return None
    data = body.get("data")
    task_id = str(data.get("id") or "").strip() if isinstance(data, dict) else ""
    if not task_id:
        return None
    return ProviderSubmission(task_id, json.dumps(body, ensure_ascii=False))


def _business_status(body: dict[str, object]) -> int | None:
    try:
        return int(str(body.get("code")))
    except (TypeError, ValueError):
        return None


def _channel_failure_code(status: int | None) -> str:
    if status == 402:
        return "provider_quota_exhausted"
    if status in {401, 403}:
        return "provider_auth_failed"
    return "provider_channel_unavailable"


def _body_error_code(body: dict[str, object] | None) -> str:
    if body is None:
        return "provider_rejected"
    error = body.get("error")
    if isinstance(error, dict):
        for key in ("type", "message", "code"):
            value = error.get(key)
            if value is not None and str(value).strip():
                return provider_safe_error_text(value)
    for key in ("msg", "message", "failure_reason", "code"):
        value = body.get(key)
        if value is not None and str(value).strip():
            return provider_safe_error_text(value)
    return "provider_rejected"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None
