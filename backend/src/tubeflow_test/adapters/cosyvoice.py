from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

import httpx

from tubeflow_test.application.narration_ports import SynthesisRequest, SynthesisResult
from tubeflow_test.domain.errors import DomainError, ProviderRateLimitedError

MAX_OUTPUT_ZIP_BYTES = 256 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


class CosyVoiceHttpAdapter:
    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient,
        token: str = "",
        max_concurrency: int = 4,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("CosyVoice base URL must be HTTP or HTTPS.")
        if max_concurrency < 1 or max_concurrency > 4:
            raise ValueError("CosyVoice max concurrency must be between 1 and 4.")
        if token != token.strip():
            raise ValueError("CosyVoice token must not contain surrounding whitespace.")
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._authorization = f"Bearer {token}" if token else None
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def submit(self, request: SynthesisRequest) -> SynthesisResult:
        return await self.submit_batch((request,))

    async def submit_batch(self, requests: tuple[SynthesisRequest, ...]) -> SynthesisResult:
        if not requests or len(requests) > 20:
            raise DomainError("invalid_batch", "CosyVoice batch must contain 1 to 20 units.")
        request = requests[0]
        if any(
            item.reference_sha256 != request.reference_sha256
            or item.reference_zip != request.reference_zip
            or item.provider_call_id != request.provider_call_id
            for item in requests
        ):
            raise DomainError("invalid_batch", "CosyVoice batch inputs must share one call.")
        actual = hashlib.sha256(request.reference_zip).hexdigest()
        if not hmac.compare_digest(actual, request.reference_sha256):
            raise DomainError(
                "reference_hash_mismatch", "CosyVoice reference ZIP hash does not match."
            )
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("ref_audio", ("reference.zip", request.reference_zip, "application/zip"))
        ]
        expected: set[str] = set()
        for unit in requests:
            stem = f"{unit.sentence.sequence:04d}-{unit.sentence.text_sha256[:12]}"
            expected.add(f"{stem}.wav")
            text = f"情感: {unit.emotion}\n译文: {unit.sentence.text}\n".encode()
            files.append(("txt_files", (f"{stem}.txt", text, "text/plain; charset=utf-8")))
        response_error: DomainError | None = None
        output = io.BytesIO()
        try:
            async with self._semaphore:
                headers = {"X-TubeFlow-Call-Id": request.provider_call_id}
                if self._authorization is not None:
                    headers["Authorization"] = self._authorization
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/batch_generate",
                    files=files,
                    headers=headers,
                ) as response:
                    if response.status_code in {401, 403}:
                        response_error = DomainError(
                            "provider_auth_failed", "CosyVoice authentication failed."
                        )
                    elif response.status_code == 429:
                        response_error = ProviderRateLimitedError(
                            "provider_rate_limited",
                            "CosyVoice rate limited the request.",
                            _retry_after_seconds(response),
                        )
                    elif 400 <= response.status_code <= 499:
                        response_error = DomainError(
                            "provider_rejected", "CosyVoice rejected the request."
                        )
                    elif 500 <= response.status_code <= 599:
                        response_error = DomainError(
                            "provider_rejected", "CosyVoice explicitly failed the request."
                        )
                    elif response.status_code != 200:
                        response_error = DomainError(
                            "provider_output_invalid", "CosyVoice returned an unexpected status."
                        )
                    media_type = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    if response_error is None and media_type not in {
                        "application/zip",
                        "application/x-zip-compressed",
                    }:
                        response_error = DomainError(
                            "provider_output_invalid", "CosyVoice response is not a ZIP archive."
                        )
                    size = 0
                    if response_error is None:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_OUTPUT_ZIP_BYTES:
                                response_error = DomainError(
                                    "provider_output_invalid",
                                    "CosyVoice ZIP exceeds the response limit.",
                                )
                                break
                            output.write(chunk)
        except httpx.ConnectTimeout as error:
            raise DomainError(
                "provider_not_reached", "CosyVoice connection timed out before delivery."
            ) from error
        except httpx.ConnectError as error:
            raise DomainError(
                "provider_not_reached", "CosyVoice connection failed before delivery."
            ) from error
        except httpx.ReadTimeout as error:
            raise DomainError(
                "provider_submission_unknown",
                "CosyVoice may have accepted the request; manual reconciliation is required.",
            ) from error
        except httpx.RequestError as error:
            raise DomainError(
                "provider_submission_unknown",
                "CosyVoice submission outcome is unknown; manual reconciliation is required.",
            ) from error
        if response_error is not None:
            raise response_error
        payload = output.getvalue()
        extract_batch_audio(payload, expected)
        return SynthesisResult(request.provider_call_id, "completed", payload)


def validate_sentence_zip(output_zip: bytes, expected_filename: str) -> None:
    extract_sentence_audio(output_zip, expected_filename)


def extract_sentence_audio(output_zip: bytes, expected_filename: str) -> bytes:
    matched = extract_batch_audio(output_zip, {expected_filename})
    if expected_filename not in matched:
        raise DomainError("provider_output_missing", "CosyVoice omitted the requested unit.")
    return matched[expected_filename]


def extract_batch_audio(output_zip: bytes, expected_filenames: set[str]) -> dict[str, bytes]:
    if not output_zip or len(output_zip) > MAX_OUTPUT_ZIP_BYTES:
        raise DomainError("provider_output_invalid", "CosyVoice output ZIP size is invalid.")
    invalid_reason: str | None = None
    try:
        with ZipFile(io.BytesIO(output_zip)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if not infos or len(infos) > 20 or len(set(names)) != len(names):
                raise DomainError(
                    "provider_output_invalid", "CosyVoice output manifest is empty or duplicated."
                )
            result: dict[str, bytes] = {}
            total_size = 0
            for info in infos:
                path = PurePosixPath(info.filename)
                invalid = (
                    bool(info.flag_bits & 0x1)
                    or info.is_dir()
                    or path.is_absolute()
                    or len(path.parts) != 1
                    or any(part in {"", ".", ".."} for part in path.parts)
                    or path.name not in expected_filenames
                    or path.suffix.lower() != ".wav"
                    or info.file_size <= 0
                    or info.file_size > MAX_AUDIO_BYTES
                    or info.compress_size <= 0
                    or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                )
                invalid_reason = (
                    "CosyVoice output manifest is unsafe or unexpected." if invalid else None
                )
                if invalid_reason is not None:
                    raise DomainError("provider_output_invalid", invalid_reason)
                total_size += info.file_size
                if total_size > MAX_OUTPUT_ZIP_BYTES:
                    raise DomainError("provider_output_invalid", "CosyVoice output is too large.")
                payload = archive.read(info)
                if len(payload) != info.file_size:
                    raise DomainError(
                        "provider_output_invalid", "CosyVoice ZIP member is incomplete."
                    )
                result[path.name] = payload
    except (BadZipFile, RuntimeError, NotImplementedError) as error:
        raise DomainError(
            "provider_output_invalid", "CosyVoice output ZIP is unreadable."
        ) from error
    return result
