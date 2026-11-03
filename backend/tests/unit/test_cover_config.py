from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from tubeflow_test.config import Settings

COVER_TOKEN = "test-cover-token-that-must-stay-redacted"

URL_FIELD = {
    "analysis": "cover_analysis_provider_url",
    "prompt": "cover_prompt_provider_url",
    "image": "cover_image_provider_url",
}
TOKEN_FIELD = {
    "analysis": "cover_analysis_provider_token",
    "prompt": "cover_prompt_provider_token",
    "image": "cover_image_provider_token",
}
MODEL_FIELD = {
    "analysis": "cover_analysis_provider_model",
    "prompt": "cover_prompt_provider_model",
    "image": "cover_image_provider_model",
}
TIMEOUT_FIELD = {
    "analysis": "cover_analysis_provider_timeout_seconds",
    "prompt": "cover_prompt_provider_timeout_seconds",
    "image": "cover_image_provider_timeout_seconds",
}
GROUPS = ("analysis", "prompt", "image")


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_cover_providers_default_to_unconfigured_with_planned_defaults() -> None:
    settings = _settings()

    assert settings.cover_analysis_provider_configured is False
    assert settings.cover_prompt_provider_configured is False
    assert settings.cover_image_provider_configured is False
    assert settings.cover_analysis_provider_url == "https://grsai.dakka.com.cn/v1"
    assert settings.cover_analysis_provider_model == "gpt-5.5"
    assert settings.cover_analysis_provider_fallback_model == "gemini-3.1-pro"
    assert settings.cover_analysis_provider_timeout_seconds == 120.0
    assert settings.cover_prompt_provider_url == "https://api.deepseek.com/v1"
    assert settings.cover_prompt_provider_model == "deepseek-v4-flash"
    assert settings.cover_prompt_provider_fallback_model == "deepseek-v4-pro"
    assert settings.cover_prompt_provider_timeout_seconds == 300.0
    assert settings.cover_image_provider_url == "https://api.lingkeai.ai/v1"
    assert settings.cover_image_provider_model == "gpt-image-2"
    assert settings.cover_image_provider_size == "2560x1440"
    assert settings.cover_image_provider_quality == "high"
    assert settings.cover_image_provider_timeout_seconds == 180.0


@pytest.mark.parametrize("group", GROUPS)
def test_cover_provider_with_token_is_configured_and_valid(group: str) -> None:
    settings = _settings(**{TOKEN_FIELD[group]: SecretStr(COVER_TOKEN)})

    configured = {
        "analysis": settings.cover_analysis_provider_configured,
        "prompt": settings.cover_prompt_provider_configured,
        "image": settings.cover_image_provider_configured,
    }
    assert configured[group] is True
    assert all(value == (name == group) for name, value in configured.items())


@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("invalid_url", ("", "cover.invalid/v1", "ftp://cover.invalid/v1"))
def test_enabled_cover_provider_rejects_empty_or_invalid_url(
    group: str, invalid_url: str
) -> None:
    with pytest.raises(ValidationError, match=r"absolute HTTP\(S\) URL"):
        _settings(
            **{
                TOKEN_FIELD[group]: SecretStr(COVER_TOKEN),
                URL_FIELD[group]: invalid_url,
            }
        )


@pytest.mark.parametrize("group", GROUPS)
def test_enabled_cover_provider_rejects_blank_model(group: str) -> None:
    with pytest.raises(ValidationError, match="model is required"):
        _settings(
            **{
                TOKEN_FIELD[group]: SecretStr(COVER_TOKEN),
                MODEL_FIELD[group]: "   ",
            }
        )


@pytest.mark.parametrize("group", GROUPS)
def test_cover_provider_timeout_must_be_positive(group: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{TIMEOUT_FIELD[group]: 0})


@pytest.mark.parametrize("group", GROUPS)
def test_cover_token_is_secret_and_not_dumped_in_plaintext(group: str) -> None:
    settings = _settings(**{TOKEN_FIELD[group]: SecretStr(COVER_TOKEN)})

    token = getattr(settings, TOKEN_FIELD[group])
    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == COVER_TOKEN
    assert COVER_TOKEN not in repr(settings)
    assert COVER_TOKEN not in repr(settings.model_dump())
    assert COVER_TOKEN not in settings.model_dump_json()
