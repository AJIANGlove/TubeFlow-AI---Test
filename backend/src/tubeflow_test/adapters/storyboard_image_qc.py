from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from tubeflow_test.application.storyboards import StoryboardAspectRatioError


class PillowStoryboardTechnicalQc:
    def __init__(self, *, max_bytes: int = 25 * 1024 * 1024, max_pixels: int = 40_000_000) -> None:
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels

    def inspect(
        self, path: Path, expected_sha256: str | None = None
    ) -> tuple[str, int, int, str, int]:
        payload = path.read_bytes()
        if not payload:
            raise ValueError("storyboard image is empty")
        if len(payload) > self._max_bytes:
            raise ValueError("storyboard image exceeds the byte limit")
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("storyboard image hash does not match provider metadata")
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                width, height = image.size
                image_format = image.format
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            raise ValueError("storyboard image cannot be decoded") from exc
        if width <= 0 or height <= 0 or image_format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("storyboard image has unsupported technical metadata")
        if width * height > self._max_pixels:
            raise ValueError("storyboard image exceeds the pixel limit")
        if abs((width / height) - (16 / 9)) > 0.03:
            raise StoryboardAspectRatioError(
                f"storyboard image aspect ratio is not 16:9 ({width}x{height})"
            )
        mime = "image/jpeg" if image_format == "JPEG" else f"image/{image_format.lower()}"
        return digest, width, height, mime, len(payload)
