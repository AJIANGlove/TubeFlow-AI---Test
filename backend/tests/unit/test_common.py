from uuid import uuid4

import pytest

from tubeflow_test.domain.common import AspectRatio, MediaAssetRef, SupportedLocale


def test_confirmed_aspect_ratios_and_locales_are_stable() -> None:
    assert {value.value for value in AspectRatio} == {"16:9", "9:16"}
    assert {value.value for value in SupportedLocale} == {
        "en-US",
        "ja-JP",
        "ko-KR",
        "es-ES",
        "fr-FR",
        "pt-BR",
    }


def test_media_asset_ref_rejects_unverified_metadata() -> None:
    with pytest.raises(ValueError, match="sha256"):
        MediaAssetRef(
            asset_id=uuid4(),
            sha256="not-a-digest",
            media_type="image/png",
            size_bytes=1,
        )

    with pytest.raises(ValueError, match="positive"):
        MediaAssetRef(
            asset_id=uuid4(),
            sha256="a" * 64,
            media_type="image/png",
            size_bytes=0,
        )
