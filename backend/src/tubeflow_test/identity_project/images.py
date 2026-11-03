from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from tubeflow_test.domain.errors import DomainError

_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass(frozen=True, slots=True)
class VerifiedImage:
    content: bytes
    media_type: str
    width: int
    height: int
    sha256: str


def verify_character_image(
    content: bytes, *, max_bytes: int = 10 * 1024 * 1024, max_pixels: int = 40_000_000
) -> VerifiedImage:
    if not content or len(content) > max_bytes:
        raise DomainError("invalid_image_size", "Image file size is outside the allowed range")
    try:
        with Image.open(io.BytesIO(content)) as image:
            media_type = _FORMATS.get(image.format or "")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise DomainError(
                    "invalid_image_dimensions",
                    "Image pixel dimensions are outside the allowed range",
                )
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image.load()
    except DomainError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise DomainError("invalid_image", "Image cannot be decoded") from exc
    if media_type is None:
        raise DomainError("invalid_image_type", "Only JPEG, PNG, and WebP are supported")
    return VerifiedImage(content, media_type, width, height, hashlib.sha256(content).hexdigest())
