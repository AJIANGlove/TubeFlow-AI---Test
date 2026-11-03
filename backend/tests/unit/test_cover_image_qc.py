from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image

from tubeflow_test.adapters.cover_image_qc import PillowCoverImageQc
from tubeflow_test.domain.errors import DomainError


def _write_image(path: Path, fmt: str, size: tuple[int, int] = (2560, 1440)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 30, 200)).save(buffer, format=fmt)
    payload = buffer.getvalue()
    path.write_bytes(payload)
    return payload


def test_qc_accepts_png_2560x1440(tmp_path: Path) -> None:
    payload = _write_image(tmp_path / "cover.png", "PNG")
    digest, width, height, mime, byte_size = PillowCoverImageQc().inspect(tmp_path / "cover.png")
    assert (width, height) == (2560, 1440)
    assert mime == "image/png"
    assert digest == hashlib.sha256(payload).hexdigest()
    assert byte_size == len(payload)


@pytest.mark.parametrize(("fmt", "expected_mime"), (("JPEG", "image/jpeg"), ("WEBP", "image/webp")))
def test_qc_accepts_jpeg_and_webp(tmp_path: Path, fmt: str, expected_mime: str) -> None:
    _write_image(tmp_path / "cover.img", fmt)
    _digest, _width, _height, mime, _size = PillowCoverImageQc().inspect(tmp_path / "cover.img")
    assert mime == expected_mime


def test_qc_rejects_wrong_size(tmp_path: Path) -> None:
    _write_image(tmp_path / "cover.png", "PNG", size=(1280, 720))
    with pytest.raises(DomainError) as captured:
        PillowCoverImageQc().inspect(tmp_path / "cover.png")
    assert captured.value.code == "cover_image_qc_size_mismatch"


def test_qc_rejects_unsupported_format_by_magic_bytes(tmp_path: Path) -> None:
    # GIF 魔数按内容识别，与 .png 扩展名无关。
    _write_image(tmp_path / "cover.png", "GIF")
    with pytest.raises(DomainError) as captured:
        PillowCoverImageQc().inspect(tmp_path / "cover.png")
    assert captured.value.code == "cover_image_qc_format_unsupported"


def test_qc_rejects_undecodable(tmp_path: Path) -> None:
    (tmp_path / "cover.png").write_bytes(b"this is not an image")
    with pytest.raises(DomainError) as captured:
        PillowCoverImageQc().inspect(tmp_path / "cover.png")
    assert captured.value.code == "cover_image_qc_undecodable"


def test_qc_rejects_empty(tmp_path: Path) -> None:
    (tmp_path / "cover.png").write_bytes(b"")
    with pytest.raises(DomainError) as captured:
        PillowCoverImageQc().inspect(tmp_path / "cover.png")
    assert captured.value.code == "cover_image_qc_empty"


def test_qc_rejects_oversized(tmp_path: Path) -> None:
    (tmp_path / "cover.png").write_bytes(b"0" * 64)
    with pytest.raises(DomainError) as captured:
        PillowCoverImageQc(max_bytes=32).inspect(tmp_path / "cover.png")
    assert captured.value.code == "cover_image_qc_too_large"


def test_qc_hash_match_and_mismatch(tmp_path: Path) -> None:
    payload = _write_image(tmp_path / "cover.png", "PNG")
    digest = hashlib.sha256(payload).hexdigest()
    result = PillowCoverImageQc().inspect(tmp_path / "cover.png", expected_sha256=digest)
    assert result[0] == digest
    with pytest.raises(DomainError) as captured:
        PillowCoverImageQc().inspect(tmp_path / "cover.png", expected_sha256="0" * 64)
    assert captured.value.code == "cover_image_qc_hash_mismatch"


def test_make_thumbnail_exact_dimensions(tmp_path: Path) -> None:
    _write_image(tmp_path / "cover.png", "PNG")
    data = PillowCoverImageQc().make_thumbnail(tmp_path / "cover.png", width=168, height=94)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (168, 94)
        assert image.format == "PNG"


def test_inspect_and_normalize_resizes_and_returns_dual_hash(tmp_path: Path) -> None:
    payload = _write_image(tmp_path / "cover.png", "PNG", size=(2752, 1536))
    raw_sha = hashlib.sha256(payload).hexdigest()
    qc = PillowCoverImageQc()
    raw_digest, digest, width, height, mime, size = qc.inspect_and_normalize(
        tmp_path / "cover.png", raw_sha
    )
    assert raw_digest == raw_sha
    assert digest != raw_sha  # 归一化后哈希不同
    assert (width, height) == (2560, 1440)
    assert mime == "image/png"
    # 写回的归一化产物通过既有严格 QC
    again = qc.inspect(tmp_path / "cover.png", digest)
    assert again[1:3] == (2560, 1440)
    assert size == len((tmp_path / "cover.png").read_bytes())


def test_inspect_and_normalize_exact_size_passes_through(tmp_path: Path) -> None:
    payload = _write_image(tmp_path / "cover.png", "PNG")
    raw_sha = hashlib.sha256(payload).hexdigest()
    raw_digest, digest, width, height, mime, _size = PillowCoverImageQc().inspect_and_normalize(
        tmp_path / "cover.png", raw_sha
    )
    assert raw_digest == digest == raw_sha
    assert (width, height) == (2560, 1440)
    assert mime == "image/png"


def test_inspect_and_normalize_rejects_hash_mismatch_and_undecodable(tmp_path: Path) -> None:
    _write_image(tmp_path / "cover.png", "PNG", size=(2752, 1536))
    with pytest.raises(DomainError) as mismatch:
        PillowCoverImageQc().inspect_and_normalize(tmp_path / "cover.png", "0" * 64)
    assert mismatch.value.code == "cover_image_qc_hash_mismatch"
    (tmp_path / "bad.png").write_bytes(b"not-an-image")
    with pytest.raises(DomainError) as undecodable:
        PillowCoverImageQc().inspect_and_normalize(
            tmp_path / "bad.png", hashlib.sha256(b"not-an-image").hexdigest()
        )
    assert undecodable.value.code == "cover_image_qc_undecodable"
