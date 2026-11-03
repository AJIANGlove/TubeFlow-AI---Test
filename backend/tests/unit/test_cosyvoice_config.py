from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from tubeflow_test.config import Settings

COSYVOICE_URL = "https://cosyvoice.example.test:8443"
COSYVOICE_TOKEN = "dedicated-cosyvoice-token"


def test_https_cosyvoice_provider_requires_token() -> None:
    with pytest.raises(ValidationError, match="token is required"):
        Settings(
            cosyvoice_url=COSYVOICE_URL,
            cosyvoice_token=SecretStr(""),
        )


def test_cosyvoice_token_is_secret_and_not_dumped_in_plaintext() -> None:
    settings = Settings(
        cosyvoice_url=COSYVOICE_URL,
        cosyvoice_token=SecretStr(COSYVOICE_TOKEN),
    )

    assert isinstance(settings.cosyvoice_token, SecretStr)
    assert settings.cosyvoice_token.get_secret_value() == COSYVOICE_TOKEN
    assert COSYVOICE_TOKEN not in repr(settings)
    assert COSYVOICE_TOKEN not in repr(settings.model_dump())
    assert COSYVOICE_TOKEN not in settings.model_dump_json()


def test_cosyvoice_concurrency_above_four_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            cosyvoice_url=COSYVOICE_URL,
            cosyvoice_token=SecretStr(COSYVOICE_TOKEN),
            cosyvoice_max_concurrency=5,
        )
