from __future__ import annotations

import pytest
from pydantic import ValidationError

from tubeflow_test.config import Settings


def test_elevenlabs_defaults() -> None:
    settings = Settings()

    assert settings.elevenlabs_api_base == "https://api.elevenlabs.io"
    assert settings.elevenlabs_proxy == "http://mihomo:7890"
    assert settings.elevenlabs_tts_model == "eleven_multilingual_v2"
    assert settings.elevenlabs_per_key_concurrency == 4
    assert settings.elevenlabs_duration_guard_seconds == 60


def test_elevenlabs_api_base_must_be_absolute_http_url() -> None:
    with pytest.raises(ValidationError, match="ElevenLabs API base"):
        Settings(elevenlabs_api_base="not-a-url")
    with pytest.raises(ValidationError, match="ElevenLabs API base"):
        Settings(elevenlabs_api_base="ftp://api.example.test")

    assert Settings(elevenlabs_api_base="http://192.168.110.200:8080").elevenlabs_api_base


def test_elevenlabs_proxy_scheme_validation() -> None:
    assert Settings(elevenlabs_proxy="").elevenlabs_proxy == ""
    assert Settings(elevenlabs_proxy="http://mihomo:7890").elevenlabs_proxy

    # socks5 被拒：httpx 需要未安装的 `socksio` 依赖，允许它只会在建连时才失败。
    with pytest.raises(ValidationError, match="ElevenLabs proxy"):
        Settings(elevenlabs_proxy="socks5://mihomo:7891")
    with pytest.raises(ValidationError, match="ElevenLabs proxy"):
        Settings(elevenlabs_proxy="https://proxy.example.test:8443")
    with pytest.raises(ValidationError, match="ElevenLabs proxy"):
        Settings(elevenlabs_proxy="mihomo:7890")


def test_elevenlabs_per_key_concurrency_bounds() -> None:
    assert Settings(elevenlabs_per_key_concurrency=1).elevenlabs_per_key_concurrency == 1
    assert Settings(elevenlabs_per_key_concurrency=5).elevenlabs_per_key_concurrency == 5

    with pytest.raises(ValidationError):
        Settings(elevenlabs_per_key_concurrency=0)
    with pytest.raises(ValidationError):
        Settings(elevenlabs_per_key_concurrency=6)


def test_elevenlabs_duration_guard_bounds() -> None:
    assert Settings(elevenlabs_duration_guard_seconds=10).elevenlabs_duration_guard_seconds == 10
    assert Settings(elevenlabs_duration_guard_seconds=600).elevenlabs_duration_guard_seconds == 600

    with pytest.raises(ValidationError):
        Settings(elevenlabs_duration_guard_seconds=9)
    with pytest.raises(ValidationError):
        Settings(elevenlabs_duration_guard_seconds=601)


def test_elevenlabs_voice_settings_default_to_none() -> None:
    """§35：默认 None = 不发送 voice_settings（回退 v2 行为不变）。"""
    settings = Settings()

    assert settings.elevenlabs_stability is None
    assert settings.elevenlabs_similarity_boost is None
    assert settings.elevenlabs_use_speaker_boost is None


def test_elevenlabs_voice_settings_accept_configured_values() -> None:
    settings = Settings(
        elevenlabs_stability=0.5,
        elevenlabs_similarity_boost=0.5,
        elevenlabs_use_speaker_boost=True,
    )

    assert settings.elevenlabs_stability == 0.5
    assert settings.elevenlabs_similarity_boost == 0.5
    assert settings.elevenlabs_use_speaker_boost is True


def test_elevenlabs_voice_settings_bounds() -> None:
    for field in ("elevenlabs_stability", "elevenlabs_similarity_boost"):
        with pytest.raises(ValidationError):
            Settings(**{field: -0.1})
        with pytest.raises(ValidationError):
            Settings(**{field: 1.1})


def test_elevenlabs_voice_settings_wiring_helper() -> None:
    """§35：装配层把 env 三元组转成请求参数；全空 → None，部分空 → 只带已设字段。"""
    from tubeflow_test.api.narration_runtime import _elevenlabs_voice_settings

    assert _elevenlabs_voice_settings(Settings()) is None
    assert _elevenlabs_voice_settings(
        Settings(
            elevenlabs_stability=0.5,
            elevenlabs_similarity_boost=0.5,
            elevenlabs_use_speaker_boost=True,
        )
    ) == {"stability": 0.5, "similarity_boost": 0.5, "use_speaker_boost": True}
    assert _elevenlabs_voice_settings(Settings(elevenlabs_stability=0.8)) == {"stability": 0.8}

