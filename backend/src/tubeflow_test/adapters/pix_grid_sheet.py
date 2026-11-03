from __future__ import annotations

import base64
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


class PixGridSheetImageProvider:
    """Pix studio 异步媒体协议适配器；参考图按文档以 data URL 顺序传入。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 120.0,
        *,
        client: httpx.Client | None = None,
        max_download_bytes: int = 64 * 1024 * 1024,
        max_concurrency: int = 20,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_configured = bool(token.strip())
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._max_download_bytes = max_download_bytes
        self._submit_slots = threading.BoundedSemaphore(max_concurrency)
        self._download_cache: dict[str, tuple[bytes, str]] = {}
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def submit_grid_sheet(
        self, generation: StoryboardGeneration, references: tuple[GridSheetReference, ...] = ()
    ) -> ProviderSubmission:
        if not self._token_configured:
            raise ProviderChannelUnavailableError(
                "pix provider token is not configured", failure_code="provider_not_configured"
            )
        with self._submit_slots:
            if sum(len(item.content) for item in references) > 30 * 1024 * 1024:
                raise ProviderSubmitRejectedError(
                    "pix reference images exceed the documented 30MB aggregate limit",
                    failure_code="reference_image_limit_exceeded",
                )
            images = [_inline_reference(item) for item in references]
            params = _model_params(generation)
            if images:
                params["images"] = images
            try:
                response = self._client.post(
                    f"{self._base_url}/v1/media/generate",
                    json={"model": generation.model, "prompt": generation.prompt, "params": params},
                    headers=self._headers,
                )
            except httpx.ConnectError as exc:
                raise ProviderSubmitNotDeliveredError("pix submit was not delivered") from exc
            if response.status_code >= 400:
                _raise_submit_http_error(response)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("pix submit returned a non-object response")
            body = cast(dict[str, object], body)
            submission = _submission_from_body(body)
            if submission is not None:
                return submission
            if body.get("code") is None:
                raise ValueError("pix submit response has no status code")
            if body.get("code") != 200:
                _raise_business_error(body, response)
            if submission is None:
                # 2xx 但缺少可信任务身份：无法判断是否已受理，不能当作可重提失败。
                raise ValueError("pix submit returned no task id")
            return submission

    def reconcile(self, idempotency_key: str) -> ProviderSubmission | None:
        del idempotency_key
        raise ProviderReconcileUnsupportedError("pix cannot reconcile without a task id")

    def poll(self, task_id: str) -> ProviderPoll:
        response = self._client.get(
            f"{self._base_url}/v1/media/status",
            params={"task_id": task_id},
            headers=self._headers,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("pix status returned a non-object response")
        body = cast(dict[str, object], body)
        receipt = json.dumps(body, ensure_ascii=False)
        state = body.get("state")
        is_final = body.get("is_final")
        if state not in {"pending", "running", "success", "failed"} or not isinstance(
            is_final, bool
        ):
            raise ValueError("pix status returned an invalid state contract")
        if not is_final:
            return ProviderPoll(GenerationStatus.RUNNING, receipt)
        if state == "running" or state == "pending":
            raise ValueError("pix status marked a non-terminal state as final")
        if state == "failed":
            failure = provider_safe_error_text(body.get("error") or "provider_failed")
            return ProviderPoll(GenerationStatus.FAILED, receipt, failure_code=failure)
        result_url = str(body.get("result_url") or "").strip()
        if not result_url:
            raise ValueError("pix succeeded without a result url")
        payload, digest = self._fetch(result_url)
        with self._cache_lock:
            self._download_cache[result_url] = (payload, digest)
        return ProviderPoll(
            GenerationStatus.SUCCEEDED,
            receipt,
            download_token=result_url,
            expected_sha256=digest,
        )

    def download(self, download_token: str, destination: Path) -> None:
        with self._cache_lock:
            cached = self._download_cache.pop(download_token, None)
        payload = cached[0] if cached is not None else self._fetch(download_token)[0]
        destination.write_bytes(payload)

    def _fetch(self, url: str) -> tuple[bytes, str]:
        response = self._client.get(url)
        response.raise_for_status()
        payload = response.content
        if not payload or len(payload) > self._max_download_bytes:
            raise ValueError("pix result image is empty or exceeds the byte limit")
        return payload, hashlib.sha256(payload).hexdigest()


def _model_params(generation: StoryboardGeneration) -> dict[str, object]:
    source = dict(generation.parameters)
    if generation.model == "tt-image-2.5":
        return {
            "version": str(source.get("version") or "flare"),
            "aspect_ratio": str(source.get("aspect_ratio") or "16:9"),
            "resolution": str(source.get("resolution") or "2K"),
            "quality": str(source.get("quality") or "high"),
            "background": str(source.get("background") or "opaque"),
        }
    if generation.model == "tt-image-2":
        return {
            "size": str(source.get("size") or "1920x1088"),
            "quality": str(source.get("quality") or "high"),
            "n": 1,
        }
    if generation.model == "banana-2.1":
        return {
            "aspectRatio": str(source.get("aspectRatio") or "16:9"),
            "imageSize": str(source.get("imageSize") or "2K"),
            "thinkingLevel": str(source.get("thinkingLevel") or "minimal"),
            "n": 1,
        }
    raise ProviderSubmitRejectedError(
        f"unsupported pix storyboard model: {generation.model}", failure_code="parameter_invalid"
    )


def _inline_reference(item: GridSheetReference) -> str:
    if len(item.content) > 10 * 1024 * 1024:
        raise ProviderSubmitRejectedError(
            "pix reference image exceeds the documented 10MB limit",
            failure_code="reference_image_limit_exceeded",
        )
    encoded = base64.b64encode(item.content).decode("ascii")
    return f"data:{item.media_type};base64,{encoded}"


def _raise_submit_http_error(response: httpx.Response) -> None:
    status = response.status_code
    body: dict[str, object] | None = None
    try:
        candidate = response.json()
        if isinstance(candidate, dict):
            body = cast(dict[str, object], candidate)
    except ValueError:
        body = None
    failure_code = _error_code(body)
    submission = _submission_from_body(body)
    if status == 429:
        raise ProviderSubmitThrottledError(
            "pix throttled the submit: HTTP 429",
            retry_after_seconds=_retry_after_seconds(response),
            submission=submission,
        )
    if status in {401, 402, 403}:
        if failure_code in {"input_moderation", "output_moderation", "content_against_policy"}:
            raise ProviderSubmitRejectedError(
                f"pix rejected the submit: {failure_code}",
                failure_code=failure_code,
                submission=submission,
            )
        raise ProviderChannelUnavailableError(
            f"pix submit failed: HTTP {status}",
            failure_code=_channel_failure_code(status),
            submission=submission,
        )
    if status >= 500:
        raise ProviderSubmitServerError(
            f"pix submit failed: HTTP {status}",
            retry_after_seconds=_retry_after_seconds(response),
            submission=submission,
        )
    raise ProviderSubmitRejectedError(
        f"pix rejected the submit: HTTP {status}",
        failure_code=failure_code,
        submission=submission,
    )


def _raise_business_error(body: dict[str, object], response: httpx.Response) -> None:
    if _submission_from_body(body) is not None:
        return
    failure_code = _error_code(body)
    try:
        status = int(str(body.get("code")))
    except (TypeError, ValueError):
        status = 0
    if failure_code in {"input_moderation", "output_moderation", "content_against_policy"}:
        raise ProviderSubmitRejectedError(
            f"pix rejected the submit: {failure_code}", failure_code=failure_code
        )
    if status == 429:
        raise ProviderSubmitThrottledError(
            "pix throttled the submit: HTTP 429",
            retry_after_seconds=_retry_after_seconds(response),
        )
    if status in {401, 402, 403} or failure_code == "insufficient_balance":
        raise ProviderChannelUnavailableError(
            "pix channel authentication or balance failed",
            failure_code=(
                "provider_quota_exhausted"
                if status == 402 or failure_code == "insufficient_balance"
                else _channel_failure_code(status)
            ),
        )
    if status >= 500:
        raise ProviderSubmitServerError(
            f"pix submit failed: {status}", retry_after_seconds=_retry_after_seconds(response)
        )
    raise ProviderSubmitRejectedError(
        f"pix rejected the submit: {failure_code}", failure_code=failure_code
    )


def _submission_from_body(body: dict[str, object] | None) -> ProviderSubmission | None:
    if body is None:
        return None
    data = body.get("data")
    task_id = ""
    if isinstance(data, dict):
        task_id = str(data.get("task_id") or "").strip()
    if not task_id:
        return None
    return ProviderSubmission(task_id, json.dumps(body, ensure_ascii=False))


def _error_code(body: dict[str, object] | None) -> str:
    if body is None:
        return "provider_rejected"
    error = body.get("error")
    if isinstance(error, dict):
        for key in ("type", "message", "code"):
            value = error.get(key)
            if value is not None and str(value).strip():
                return provider_safe_error_text(value)
    for key in ("msg", "message", "code"):
        value = body.get(key)
        if value is not None and str(value).strip():
            return provider_safe_error_text(value)
    return "provider_rejected"


def _channel_failure_code(status: int) -> str:
    if status == 402:
        return "provider_quota_exhausted"
    if status in {401, 403}:
        return "provider_auth_failed"
    return "provider_channel_unavailable"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None
