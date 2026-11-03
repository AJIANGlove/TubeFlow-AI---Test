"""封面图技术质检（Pillow，照 `adapters/storyboard_image_qc.py` 模式）。

校验：非空、字节上限、sha256 与 Provider 记录一致、Pillow 可解码、格式为
png/jpeg/webp（按魔数识别，与扩展名无关）、尺寸正好 2560x1440。失败抛
DomainError 稳定码（`cover_image_qc_*`），供出图活动落 failure_code。
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from tubeflow_test.domain.errors import DomainError

EXPECTED_WIDTH = 2560
EXPECTED_HEIGHT = 1440
_FORMAT_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class PillowCoverImageQc:
    """封面图技术质检：返回 (sha256, width, height, mime, byte_size)。"""

    def __init__(self, *, max_bytes: int = 25 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes

    def inspect(
        self, path: Path, expected_sha256: str | None = None
    ) -> tuple[str, int, int, str, int]:
        payload = path.read_bytes()
        if not payload:
            raise DomainError("cover_image_qc_empty", "封面图为空。")
        if len(payload) > self._max_bytes:
            raise DomainError("cover_image_qc_too_large", "封面图超出字节上限。")
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise DomainError(
                "cover_image_qc_hash_mismatch", "封面图哈希与 Provider 记录不一致。"
            )
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                width, height = image.size
                image_format = image.format
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            raise DomainError("cover_image_qc_undecodable", "封面图无法解码。") from exc
        mime = _FORMAT_MIME.get(image_format or "")
        if mime is None:
            raise DomainError("cover_image_qc_format_unsupported", "封面图格式不受支持。")
        if width != EXPECTED_WIDTH or height != EXPECTED_HEIGHT:
            raise DomainError("cover_image_qc_size_mismatch", "封面图尺寸不是 2560x1440。")
        return digest, width, height, mime, len(payload)

    def make_thumbnail(self, path: Path, *, width: int, height: int) -> bytes:
        """复审用缩略图：PNG 字节；解码失败由调用方按复审失败降级处理。"""
        with Image.open(path) as image:
            image.load()
            thumbnail = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        thumbnail.save(buffer, format="PNG")
        return buffer.getvalue()

    def inspect_and_normalize(
        self, path: Path, expected_sha256: str
    ) -> tuple[str, str, int, int, str, int]:
        """锚图通道 QC：原始字节校验 → 归一化 2560x1440 写回 → 既有 QC 复核。

        返回 (原始 sha256, 归一化 sha256, 宽, 高, mime, 归一化字节数)。"""
        payload = path.read_bytes()
        if not payload:
            raise DomainError("cover_image_qc_empty", "封面图为空。")
        if len(payload) > self._max_bytes:
            raise DomainError("cover_image_qc_too_large", "封面图超出字节上限。")
        raw_digest = hashlib.sha256(payload).hexdigest()
        if raw_digest != expected_sha256:
            raise DomainError(
                "cover_image_qc_hash_mismatch", "封面图哈希与 Provider 记录不一致。"
            )
        try:
            with Image.open(path) as image:
                image.load()
                width, height = image.size
                image_format = image.format
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            raise DomainError("cover_image_qc_undecodable", "封面图无法解码。") from exc
        mime = _FORMAT_MIME.get(image_format or "")
        if mime is None:
            raise DomainError("cover_image_qc_format_unsupported", "封面图格式不受支持。")
        if (width, height) == (EXPECTED_WIDTH, EXPECTED_HEIGHT):
            return raw_digest, raw_digest, width, height, mime, len(payload)
        with Image.open(path) as image:
            image.load()
            normalized = image.convert("RGB").resize(
                (EXPECTED_WIDTH, EXPECTED_HEIGHT), Image.Resampling.LANCZOS
            )
        buffer = BytesIO()
        normalized.save(buffer, format="PNG")
        path.write_bytes(buffer.getvalue())
        digest, norm_width, norm_height, norm_mime, norm_size = self.inspect(path)
        return raw_digest, digest, norm_width, norm_height, norm_mime, norm_size
