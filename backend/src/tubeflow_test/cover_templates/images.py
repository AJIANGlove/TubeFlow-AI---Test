"""上传校验与压缩：扩展名 + 真实格式 + 可解码 + 像素上限 → 原图字节 + WebP 压缩图。

参数由用户授权决定（2026-10-21）：长边 1280、WebP 质量 82、不裁剪、不放大；
透明图保留 alpha。压缩失败不回退原图冒充成功，直接报错。
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from tubeflow_test.domain.errors import DomainError

COVER_TEMPLATE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
PREVIEW_LONG_EDGE = 1280
PREVIEW_QUALITY = 82

_FORMATS = {
    "JPEG": ("image/jpeg", "JPEG"),
    "PNG": ("image/png", "PNG"),
    "WEBP": ("image/webp", "WEBP"),
}


@dataclass(frozen=True, slots=True)
class StoredImage:
    data: bytes
    media_type: str
    image_format: str
    width: int
    height: int
    sha256: str

    @property
    def byte_size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class PreparedCoverTemplate:
    original: StoredImage
    preview: StoredImage


def _image_format(image: Image.Image) -> tuple[str, str]:
    meta = _FORMATS.get(image.format or "")
    if meta is None:
        raise DomainError("invalid_image_type", "Only JPEG, PNG, and WebP are supported")
    return meta


def _webp_ready(image: Image.Image) -> Image.Image:
    """WebP 编码只接受 RGB/RGBA；带透明的调色板图保留 alpha，其余统一转 RGB。"""
    has_alpha = image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        return image if image.mode == "RGBA" else image.convert("RGBA")
    return image if image.mode == "RGB" else image.convert("RGB")


def prepare_cover_template(content: bytes, *, filename: str = "") -> PreparedCoverTemplate:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix and suffix not in COVER_TEMPLATE_EXTENSIONS:
        raise DomainError(
            "invalid_image_type", "Only .jpg, .jpeg, .png, and .webp files are supported"
        )
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise DomainError("invalid_image_size", "Image file size is outside the allowed range")
    try:
        with Image.open(io.BytesIO(content)) as opened:
            media_type, image_format = _image_format(opened)
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise DomainError(
                    "invalid_image_dimensions",
                    "Image pixel dimensions are outside the allowed range",
                )
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                opened.load()
            long_edge = max(width, height)
            scale = PREVIEW_LONG_EDGE / long_edge if long_edge > PREVIEW_LONG_EDGE else 1.0
            preview_image = _webp_ready(opened)
            if scale < 1.0:
                preview_size = (
                    max(1, round(width * scale)),
                    max(1, round(height * scale)),
                )
                preview_image = preview_image.resize(preview_size, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            preview_image.save(buffer, format="WEBP", quality=PREVIEW_QUALITY, method=4)
            preview_bytes = buffer.getvalue()
            preview_width, preview_height = preview_image.size
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
    original = StoredImage(
        data=content,
        media_type=media_type,
        image_format=image_format,
        width=width,
        height=height,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    preview = StoredImage(
        data=preview_bytes,
        media_type="image/webp",
        image_format="WEBP",
        width=preview_width,
        height=preview_height,
        sha256=hashlib.sha256(preview_bytes).hexdigest(),
    )
    return PreparedCoverTemplate(original, preview)
