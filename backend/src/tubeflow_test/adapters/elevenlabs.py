"""ElevenLabs HTTP 适配器：TTS 合成、IVC 克隆、音色管理与免费配额查询。

合同来源：https://elevenlabs.io/docs/api-reference/text-to-speech/convert（2026-10-13
核实 output_format 枚举与 xi-api-key 鉴权头）；失败语义见 docs/GUIDE_elevenlabs.md §2.3，
401 复核判定复用 elevenlabs.domain.classify_key_failure 矩阵。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from tubeflow_test.adapters.cosyvoice import _retry_after_seconds
from tubeflow_test.domain.errors import (
    DomainError,
    ProviderHttpStatusError,
    ProviderRateLimitedError,
)
from tubeflow_test.elevenlabs.domain import QuotaSnapshot, RecheckOutcome, classify_key_failure

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 20 * 1024 * 1024
TTS_OUTPUT_FORMAT = "mp3_44100_128"


@dataclass(frozen=True, slots=True)
class ElvSynthesisResult:
    audio: bytes
    request_id: str | None


class ElevenLabsHttpClient:
    """EL HTTP 适配器。

    §23.9：代理在构造 `httpx.AsyncClient(proxy=...)` 时固定（httpx 的 per-request
    proxy 不适用于 stream/request 调用），调用方必须传入已按 `.env` 装配代理的客户端。
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient,
        per_key_concurrency: int = 4,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("ElevenLabs base URL must be HTTP or HTTPS.")
        if per_key_concurrency < 1 or per_key_concurrency > 5:
            raise ValueError("ElevenLabs per-key concurrency must be between 1 and 5.")
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._per_key_concurrency = per_key_concurrency
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def synthesize(
        self,
        api_key: str,
        voice_id: str,
        text: str,
        *,
        model_id: str,
        previous_request_ids: tuple[str, ...] = (),
        voice_settings: dict[str, Any] | None = None,
    ) -> ElvSynthesisResult:
        body: dict[str, Any] = {"text": text, "model_id": model_id}
        if previous_request_ids:
            body["previous_request_ids"] = list(previous_request_ids)
        # §35：voice_settings 仅在显式配置时发送；缺省不携带该字段，
        # EL 按音色存储默认值合成（v2 回退路径行为不变）。
        if voice_settings:
            body["voice_settings"] = dict(voice_settings)
        status_error: httpx.Response | None = None
        request_id: str | None = None
        chunks: list[bytes] = []
        size = 0
        try:
            async with (
                self._semaphore_for(api_key),
                self._client.stream(
                    "POST",
                    f"{self._base_url}/v1/text-to-speech/{voice_id}",
                    params={"output_format": TTS_OUTPUT_FORMAT},
                    json=body,
                    headers={"xi-api-key": api_key},
                ) as response,
            ):
                if response.status_code != 200:
                    await response.aread()
                    status_error = response
                else:
                    request_id = response.headers.get("request-id")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_AUDIO_BYTES:
                            raise DomainError(
                                "provider_output_invalid",
                                "ElevenLabs audio exceeds the response size limit.",
                            )
                        chunks.append(chunk)
        except httpx.ConnectTimeout as error:
            raise DomainError(
                "provider_not_reached", "ElevenLabs connection timed out before delivery."
            ) from error
        except httpx.ConnectError as error:
            raise DomainError(
                "provider_not_reached", "ElevenLabs connection failed before delivery."
            ) from error
        except httpx.ReadTimeout as error:
            raise DomainError(
                "provider_submission_unknown",
                "ElevenLabs may have accepted the request; manual reconciliation is required.",
            ) from error
        except httpx.RequestError as error:
            raise DomainError(
                "provider_submission_unknown",
                "ElevenLabs submission outcome is unknown; manual reconciliation is required.",
            ) from error
        if status_error is not None:
            raise await self._map_http_error(api_key, status_error)
        return ElvSynthesisResult(audio=b"".join(chunks), request_id=request_id)

    async def clone_voice(
        self,
        api_key: str,
        name: str,
        *,
        audio: bytes,
        filename: str,
        description: str = "",
    ) -> str:
        data: dict[str, str] = {"name": name}
        if description:
            data["description"] = description
        files = {"files": (filename, audio, "application/octet-stream")}
        response = await self._request("POST", "/v1/voices/add", api_key, data=data, files=files)
        if response.status_code != 200:
            raise await self._map_http_error(api_key, response)
        payload: object = response.json()
        voice_id = payload.get("voice_id") if isinstance(payload, dict) else None
        if not isinstance(voice_id, str) or not voice_id:
            raise DomainError(
                "provider_output_invalid", "ElevenLabs clone response is missing the voice id."
            )
        return voice_id

    async def get_voice(self, api_key: str, voice_id: str) -> bool:
        response = await self._request("GET", f"/v1/voices/{voice_id}", api_key)
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise await self._map_http_error(api_key, response)

    async def list_account_voices(self, api_key: str) -> tuple[str, ...]:
        """账号下全部音色 id（槽位满自动清理时用于确认远端确实存在该音色）。"""
        response = await self._request("GET", "/v1/voices", api_key)
        if response.status_code != 200:
            raise await self._map_http_error(api_key, response)
        try:
            payload: object = response.json()
        except ValueError as error:
            raise DomainError(
                "provider_output_invalid", "ElevenLabs voices response is not valid JSON."
            ) from error
        data = payload if isinstance(payload, dict) else {}
        voices = data.get("voices")
        if not isinstance(voices, list):
            return ()
        result: list[str] = []
        for item in voices:
            voice_id = item.get("voice_id") if isinstance(item, dict) else None
            if isinstance(voice_id, str) and voice_id:
                result.append(voice_id)
        return tuple(result)

    async def delete_voice(self, api_key: str, voice_id: str) -> None:
        response = await self._request("DELETE", f"/v1/voices/{voice_id}", api_key)
        if response.status_code == 404 or 200 <= response.status_code < 300:
            return
        raise await self._map_http_error(api_key, response)

    async def get_subscription(self, api_key: str) -> QuotaSnapshot:
        response = await self._request("GET", "/v1/user/subscription", api_key)
        if response.status_code != 200:
            raise await self._map_http_error(api_key, response)
        try:
            payload: object = response.json()
        except ValueError as error:
            raise DomainError(
                "provider_output_invalid", "ElevenLabs subscription response is not valid JSON."
            ) from error
        data = payload if isinstance(payload, dict) else {}
        return QuotaSnapshot(
            tier=_text_field(data, "tier"),
            character_count=_int_field(data, "character_count"),
            character_limit=_int_field(data, "character_limit"),
            voice_slots_used=_int_field(data, "voice_slots_used"),
            voice_limit=_int_field(data, "voice_limit"),
            status=_text_field(data, "status"),
            can_use_instant_voice_cloning=data.get("can_use_instant_voice_cloning") is True,
            next_reset_unix=_optional_int_field(data, "next_character_count_reset_unix"),
        )

    async def _request(
        self, method: str, path: str, api_key: str, **kwargs: Any
    ) -> httpx.Response:
        try:
            async with self._semaphore_for(api_key):
                return await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers={"xi-api-key": api_key},
                    **kwargs,
                )
        except httpx.ConnectTimeout as error:
            raise DomainError(
                "provider_not_reached", "ElevenLabs connection timed out before delivery."
            ) from error
        except httpx.ConnectError as error:
            raise DomainError(
                "provider_not_reached", "ElevenLabs connection failed before delivery."
            ) from error
        except httpx.ReadTimeout as error:
            raise DomainError(
                "provider_submission_unknown",
                "ElevenLabs may have accepted the request; manual reconciliation is required.",
            ) from error
        except httpx.RequestError as error:
            raise DomainError(
                "provider_submission_unknown",
                "ElevenLabs submission outcome is unknown; manual reconciliation is required.",
            ) from error

    async def _map_http_error(self, api_key: str, response: httpx.Response) -> DomainError:
        status = response.status_code
        # 2026-10-31：记录失败状态与响应体摘要（不含 Key），否则下游只能看到映射后的
        # 笼统错误码，无法还原服务商真实拒绝原因。
        logger.warning(
            "elevenlabs rejected request: status=%s body=%s",
            status,
            " ".join(response.text[:200].split()),
        )
        if status == 402:
            return ProviderHttpStatusError(
                "provider_quota_exhausted",
                "ElevenLabs reported status 402 (payment required).",
                402,
            )
        if status == 401:
            classification = classify_key_failure(status, await self._recheck_key(api_key))
            if classification == "invalid":
                return ProviderHttpStatusError(
                    "provider_auth_failed",
                    "ElevenLabs API key is invalid (status 401 confirmed by recheck).",
                    401,
                )
            if classification == "exhausted":
                return ProviderHttpStatusError(
                    "provider_quota_exhausted",
                    "ElevenLabs quota is exhausted (status 401; subscription still readable).",
                    401,
                )
            return DomainError(
                "provider_rejected",
                "ElevenLabs returned status 401 and the free recheck was unreachable.",
            )
        if status == 403:
            if "content_against_policy" in response.text:
                return DomainError(
                    "provider_content_rejected",
                    "ElevenLabs content moderation rejected the request (content_against_policy).",
                )
            return DomainError(
                "provider_rejected", "ElevenLabs rejected the request with status 403."
            )
        if status == 404:
            return DomainError(
                "provider_voice_missing", "ElevenLabs voice is not present in the account."
            )
        if status == 429:
            return ProviderRateLimitedError(
                "provider_rate_limited",
                "ElevenLabs rate limited the request (status 429).",
                _retry_after_seconds(response),
            )
        return DomainError(
            "provider_rejected", f"ElevenLabs rejected the request with status {status}."
        )

    async def _recheck_key(self, api_key: str) -> RecheckOutcome:
        try:
            response = await self._client.get(
                f"{self._base_url}/v1/user/subscription",
                headers={"xi-api-key": api_key},
            )
        except httpx.RequestError:
            return RecheckOutcome(reachable=False, rejected=False)
        return RecheckOutcome(reachable=True, rejected=response.status_code in {401, 403})

    def _semaphore_for(self, api_key: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(api_key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._per_key_concurrency)
            self._semaphores[api_key] = semaphore
        return semaphore


class ElevenLabsQuotaAdapter:
    """号池的配额端口实现（§23.6：免费订阅接口，不计字符费）。"""

    def __init__(self, client: ElevenLabsHttpClient) -> None:
        self._client = client

    async def subscription(self, api_key: str) -> QuotaSnapshot:
        return await self._client.get_subscription(api_key)


def _text_field(data: dict[Any, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _int_field(data: dict[Any, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _optional_int_field(data: dict[Any, Any], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)
