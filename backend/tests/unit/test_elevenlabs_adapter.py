from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from tubeflow_test.adapters.elevenlabs import (
    MAX_AUDIO_BYTES,
    ElevenLabsHttpClient,
    ElvSynthesisResult,
)
from tubeflow_test.domain.errors import DomainError, ProviderRateLimitedError
from tubeflow_test.elevenlabs.domain import QuotaSnapshot

BASE_URL = "https://api.elevenlabs.io"
Handler = Callable[[httpx.Request], httpx.Response]


def _make_client(
    handler: Handler, *, per_key_concurrency: int = 4
) -> ElevenLabsHttpClient:
    return ElevenLabsHttpClient(
        BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        per_key_concurrency=per_key_concurrency,
    )


def test_base_url_must_be_http_or_https() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        ElevenLabsHttpClient("ftp://example.com", client=httpx.AsyncClient(transport=transport))


@pytest.mark.parametrize("value", [0, 6])
def test_per_key_concurrency_bounds(value: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with pytest.raises(ValueError, match="between 1 and 5"):
        ElevenLabsHttpClient(
            BASE_URL,
            client=httpx.AsyncClient(transport=transport),
            per_key_concurrency=value,
        )


@pytest.mark.asyncio
async def test_synthesize_sends_documented_request_and_returns_audio() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"mp3-audio", headers={"request-id": "req-1"})

    result = await _make_client(handler).synthesize(
        "key-1",
        "voice-9",
        "你好世界",
        model_id="eleven_multilingual_v2",
        previous_request_ids=("prev-1", "prev-2"),
    )

    assert result == ElvSynthesisResult(audio=b"mp3-audio", request_id="req-1")
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/text-to-speech/voice-9"
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert request.headers["xi-api-key"] == "key-1"
    assert json.loads(request.content) == {
        "text": "你好世界",
        "model_id": "eleven_multilingual_v2",
        "previous_request_ids": ["prev-1", "prev-2"],
    }


@pytest.mark.asyncio
async def test_synthesize_omits_previous_request_ids_when_empty() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"a")

    result = await _make_client(handler).synthesize("key-1", "voice-9", "文本", model_id="m")

    assert result.request_id is None
    assert "previous_request_ids" not in json.loads(seen[0].content)


@pytest.mark.asyncio
async def test_synthesize_sends_voice_settings_only_when_configured() -> None:
    """§35：显式配置时请求体携带 voice_settings 原值。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"a")

    await _make_client(handler).synthesize(
        "key-1",
        "voice-9",
        "文本",
        model_id="eleven_v4",
        voice_settings={"stability": 0.5, "similarity_boost": 0.5, "use_speaker_boost": True},
    )

    body = json.loads(seen[0].content)
    assert body["voice_settings"] == {
        "stability": 0.5,
        "similarity_boost": 0.5,
        "use_speaker_boost": True,
    }


@pytest.mark.asyncio
async def test_synthesize_omits_voice_settings_when_none_or_empty() -> None:
    """§35：None 或空字典都不发送 voice_settings 字段（EL 按音色存储默认值合成）。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"a")

    client = _make_client(handler)
    await client.synthesize("key-1", "voice-9", "文本", model_id="m", voice_settings=None)
    await client.synthesize("key-1", "voice-9", "文本", model_id="m", voice_settings={})

    assert "voice_settings" not in json.loads(seen[0].content)
    assert "voice_settings" not in json.loads(seen[1].content)



@pytest.mark.asyncio
async def test_synthesize_402_maps_to_quota_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": {"status": "quota_exceeded"}})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_quota_exhausted"


@pytest.mark.asyncio
async def test_synthesize_401_with_rejected_recheck_maps_to_auth_failed() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(401, json={"detail": {"status": "quota_exceeded"}})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_auth_failed"
    assert paths == ["/v1/text-to-speech/v", "/v1/user/subscription"]


@pytest.mark.asyncio
async def test_synthesize_401_with_readable_recheck_maps_to_quota_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user/subscription":
            return httpx.Response(200, json={"tier": "creator"})
        return httpx.Response(401)

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_quota_exhausted"


@pytest.mark.asyncio
async def test_synthesize_401_with_unreachable_recheck_maps_to_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user/subscription":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(401)

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_rejected"


@pytest.mark.asyncio
async def test_synthesize_403_content_against_policy_maps_to_content_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"detail": {"status": "content_against_policy", "message": "业务文本"}}
        )

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_content_rejected"
    assert "业务文本" not in captured.value.message


@pytest.mark.asyncio
async def test_synthesize_403_other_maps_to_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": {"status": "voice_limit_reached"}})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_rejected"


@pytest.mark.asyncio
async def test_synthesize_404_maps_to_voice_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"status": "voice_not_found"}})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_voice_missing"


@pytest.mark.asyncio
async def test_synthesize_409_maps_to_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"status": "conflict"}})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_rejected"


@pytest.mark.asyncio
async def test_synthesize_rejection_logs_status_and_body_without_key(caplog) -> None:
    """2026-10-31：拒绝原因必须可从日志还原，且不得输出 API Key。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": {"status": "text_empty", "message": "text is empty"}}
        )

    with caplog.at_level("WARNING", logger="tubeflow_test.adapters.elevenlabs"):
        with pytest.raises(DomainError) as captured:
            await _make_client(handler).synthesize(
                "super-secret-key", "v", "\u200b", model_id="m"
            )
    assert captured.value.code == "provider_rejected"
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "status=422" in logged
    assert "text_empty" in logged
    assert "super-secret-key" not in logged


@pytest.mark.asyncio
async def test_synthesize_429_maps_to_rate_limited_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "2.5"})

    with pytest.raises(ProviderRateLimitedError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_rate_limited"
    assert captured.value.retry_after_seconds == 2.5


@pytest.mark.asyncio
async def test_synthesize_429_without_retry_after_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(ProviderRateLimitedError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.retry_after_seconds is None


@pytest.mark.asyncio
async def test_synthesize_500_maps_to_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": {"status": "internal_error"}})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_rejected"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
@pytest.mark.asyncio
async def test_synthesize_connect_failures_map_to_not_reached(
    error_type: type[httpx.RequestError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("boom", request=request)

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_not_reached"


@pytest.mark.asyncio
async def test_synthesize_read_timeout_maps_to_submission_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_submission_unknown"


@pytest.mark.asyncio
async def test_synthesize_audio_over_limit_maps_to_output_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_AUDIO_BYTES + 1))

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).synthesize("k", "v", "t", model_id="m")
    assert captured.value.code == "provider_output_invalid"


@pytest.mark.asyncio
async def test_clone_voice_uploads_multipart_and_returns_voice_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"voice_id": "elv-voice-1", "requires_verification": False}
        )

    voice_id = await _make_client(handler).clone_voice(
        "key-1", "我的音色", audio=b"wav-bytes", filename="sample.wav", description="备注"
    )

    assert voice_id == "elv-voice-1"
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/voices/add"
    assert request.headers["xi-api-key"] == "key-1"
    assert request.headers["content-type"].startswith("multipart/form-data")
    payload = request.content
    assert b'name="name"' in payload
    assert "我的音色".encode() in payload
    assert b'name="files"; filename="sample.wav"' in payload
    assert b"wav-bytes" in payload
    assert b'name="description"' in payload
    assert "备注".encode() in payload


@pytest.mark.asyncio
async def test_clone_voice_omits_empty_description() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"voice_id": "elv-voice-2"})

    voice_id = await _make_client(handler).clone_voice(
        "key-1", "音色", audio=b"a", filename="f.wav"
    )

    assert voice_id == "elv-voice-2"
    assert b'name="description"' not in seen[0].content


@pytest.mark.asyncio
async def test_clone_voice_422_maps_to_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": {"status": "invalid_audio"}})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).clone_voice("k", "n", audio=b"a", filename="f.wav")
    assert captured.value.code == "provider_rejected"


@pytest.mark.asyncio
async def test_clone_voice_missing_voice_id_maps_to_output_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"requires_verification": True})

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).clone_voice("k", "n", audio=b"a", filename="f.wav")
    assert captured.value.code == "provider_output_invalid"


@pytest.mark.asyncio
async def test_get_voice_returns_true_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/voices/voice-9"
        assert request.headers["xi-api-key"] == "key-1"
        return httpx.Response(200, json={"voice_id": "voice-9"})

    assert await _make_client(handler).get_voice("key-1", "voice-9") is True


@pytest.mark.asyncio
async def test_get_voice_returns_false_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"status": "voice_not_found"}})

    assert await _make_client(handler).get_voice("key-1", "voice-9") is False


@pytest.mark.asyncio
async def test_get_voice_500_maps_to_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).get_voice("key-1", "voice-9")
    assert captured.value.code == "provider_rejected"


@pytest.mark.asyncio
async def test_delete_voice_succeeds_on_200() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    await _make_client(handler).delete_voice("key-1", "voice-9")
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/v1/voices/voice-9"


@pytest.mark.asyncio
async def test_delete_voice_404_is_idempotent_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    await _make_client(handler).delete_voice("key-1", "voice-9")


@pytest.mark.asyncio
async def test_delete_voice_500_maps_to_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).delete_voice("key-1", "voice-9")
    assert captured.value.code == "provider_rejected"


@pytest.mark.asyncio
async def test_get_subscription_maps_all_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/user/subscription"
        assert request.headers["xi-api-key"] == "key-1"
        return httpx.Response(
            200,
            json={
                "tier": "creator",
                "character_count": 1200,
                "character_limit": 500000,
                "voice_slots_used": 3,
                "voice_limit": 30,
                "status": "active",
                "can_use_instant_voice_cloning": True,
                "next_character_count_reset_unix": 1760000000,
            },
        )

    snapshot = await _make_client(handler).get_subscription("key-1")
    assert snapshot == QuotaSnapshot(
        tier="creator",
        character_count=1200,
        character_limit=500000,
        voice_slots_used=3,
        voice_limit=30,
        status="active",
        can_use_instant_voice_cloning=True,
        next_reset_unix=1760000000,
    )


@pytest.mark.asyncio
async def test_get_subscription_missing_fields_fall_back_to_conservative_defaults() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    snapshot = await _make_client(handler).get_subscription("key-1")
    assert snapshot == QuotaSnapshot(
        tier="",
        character_count=0,
        character_limit=0,
        voice_slots_used=0,
        voice_limit=0,
        status="",
        can_use_instant_voice_cloning=False,
        next_reset_unix=None,
    )


@pytest.mark.asyncio
async def test_get_subscription_401_maps_via_recheck_to_auth_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(DomainError) as captured:
        await _make_client(handler).get_subscription("key-1")
    assert captured.value.code == "provider_auth_failed"


@pytest.mark.asyncio
async def test_same_key_concurrency_is_capped() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=b"a")

    client = _make_client(handler, per_key_concurrency=2)
    await asyncio.gather(
        *[client.synthesize("key-1", "v", "t", model_id="m") for _ in range(5)]
    )
    assert peak == 2


@pytest.mark.asyncio
async def test_different_keys_have_independent_semaphores() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=b"a")

    client = _make_client(handler, per_key_concurrency=1)
    await asyncio.gather(
        client.synthesize("key-1", "v", "t", model_id="m"),
        client.synthesize("key-2", "v", "t", model_id="m"),
    )
    assert peak == 2
