from __future__ import annotations

import logging
import shutil
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path


class StreamingExport:
    def __init__(
        self,
        video: Path,
        subtitles: Path,
        *,
        private_root: Path,
        chunk_size: int = 64 * 1024,
        cover: tuple[Path, str] | None = None,
    ):
        self._video = video
        self._subtitles = subtitles
        self._private_root = private_root
        self._chunk_size = chunk_size
        # 可选封面条目（路径, ZIP 内文件名）；None 时产物与原两条目合同逐字节一致。
        self._cover = cover
        self._temporary_dir: Path | None = None

    def __iter__(self) -> Iterator[bytes]:
        self._private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._temporary_dir = Path(tempfile.mkdtemp(prefix="export-", dir=self._private_root))
        archive_path = self._temporary_dir / "download.zip"
        try:
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(self._video, "final.mp4")
                archive.write(self._subtitles, "subtitles.srt")
                if self._cover is not None:
                    archive.write(self._cover[0], self._cover[1])
            offset = 0
            size = archive_path.stat().st_size
            while offset < size:
                with archive_path.open("rb") as source:
                    source.seek(offset)
                    chunk = source.read(self._chunk_size)
                if not chunk:
                    break
                offset += len(chunk)
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        temporary_dir = self._temporary_dir
        if temporary_dir is None:
            return
        shutil.rmtree(temporary_dir, ignore_errors=False)
        if temporary_dir.exists():
            raise RuntimeError("temporary export cleanup failed")
        self._temporary_dir = None

    @property
    def temporary_path(self) -> Path | None:
        return self._temporary_dir


def cleanup_orphan_exports(
    private_root: Path, *, older_than_seconds: float, now: float | None = None
) -> int:
    if not private_root.exists():
        return 0
    cutoff = (time.time() if now is None else now) - older_than_seconds
    removed = 0
    for candidate in private_root.iterdir():
        if (
            candidate.is_dir()
            and candidate.name.startswith("export-")
            and candidate.stat().st_mtime < cutoff
        ):
            try:
                shutil.rmtree(candidate, ignore_errors=False)
            except OSError:
                logging.getLogger(__name__).warning("orphan export cleanup failed")
                continue
            if not candidate.exists():
                removed += 1
    return removed
