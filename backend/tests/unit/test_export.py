from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import anyio
import pytest

from tubeflow_test.api.media_router import CleanupStreamingResponse
from tubeflow_test.application.export import StreamingExport, cleanup_orphan_exports


def test_export_contains_only_final_video_and_srt_and_cleans_after_completion(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    subtitles = tmp_path / "captions.srt"
    video.write_bytes(b"video")
    subtitles.write_text("subtitle", encoding="utf-8")
    export = StreamingExport(video, subtitles, private_root=tmp_path / "private", chunk_size=3)
    payload = b"".join(export)
    assert export.temporary_path is None
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["final.mp4", "subtitles.srt"]


def test_export_appends_cover_entry_after_srt_and_cleans(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    subtitles = tmp_path / "captions.srt"
    cover = tmp_path / "cover.png"
    video.write_bytes(b"video")
    subtitles.write_text("subtitle", encoding="utf-8")
    cover.write_bytes(b"cover-bytes")
    export = StreamingExport(
        video,
        subtitles,
        private_root=tmp_path / "private",
        chunk_size=3,
        cover=(cover, "cover.png"),
    )
    payload = b"".join(export)
    assert export.temporary_path is None
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["final.mp4", "subtitles.srt", "cover.png"]
        assert archive.read("cover.png") == b"cover-bytes"


def test_export_cleans_temporary_zip_when_cover_stream_is_interrupted(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    subtitles = tmp_path / "captions.srt"
    cover = tmp_path / "cover.png"
    video.write_bytes(b"video-data")
    subtitles.write_text("subtitle", encoding="utf-8")
    cover.write_bytes(b"cover-bytes")
    export = StreamingExport(
        video,
        subtitles,
        private_root=tmp_path / "private",
        chunk_size=1,
        cover=(cover, "cover.png"),
    )
    stream = iter(export)
    next(stream)
    temporary_path = export.temporary_path
    assert temporary_path is not None and temporary_path.exists()
    stream.close()
    assert not temporary_path.exists()
    assert export.temporary_path is None


def test_export_cleans_temporary_zip_when_stream_is_interrupted(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    subtitles = tmp_path / "captions.srt"
    video.write_bytes(b"video-data")
    subtitles.write_text("subtitle", encoding="utf-8")
    export = StreamingExport(video, subtitles, private_root=tmp_path / "private", chunk_size=1)
    stream = iter(export)
    next(stream)
    temporary_path = export.temporary_path
    assert temporary_path is not None and temporary_path.exists()
    stream.close()
    assert not temporary_path.exists()
    assert export.temporary_path is None


def test_startup_cleanup_removes_only_timed_out_export_directories(tmp_path: Path) -> None:
    root = tmp_path / "private"
    old = root / "export-old"
    fresh = root / "export-fresh"
    unrelated = root / "keep"
    for item in (old, fresh, unrelated):
        item.mkdir(parents=True)
    now = time.time()
    os.utime(old, (now - 1000, now - 1000))
    assert cleanup_orphan_exports(root, older_than_seconds=100, now=now) == 1
    assert not old.exists() and fresh.exists() and unrelated.exists()


def test_orphan_cleanup_does_not_report_failed_removal(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "private"
    old = root / "export-old"
    old.mkdir(parents=True)
    monkeypatch.setattr(
        "tubeflow_test.application.export.shutil.rmtree",
        lambda *a, **k: (_ for _ in ()).throw(OSError()),
    )
    assert cleanup_orphan_exports(root, older_than_seconds=0, now=time.time() + 1) == 0
    assert old.exists()


@pytest.mark.asyncio
async def test_real_asgi_send_disconnect_closes_export_generator(tmp_path: Path) -> None:
    video, subtitles = tmp_path / "video.mp4", tmp_path / "captions.srt"
    video.write_bytes(b"video-data")
    subtitles.write_text("subtitle", encoding="utf-8")
    private = tmp_path / "private"
    export = StreamingExport(video, subtitles, private_root=private, chunk_size=1)
    response = CleanupStreamingResponse(export, cleanup=export.close, media_type="application/zip")
    blocker = anyio.Event()

    async def receive() -> dict[str, str]:
        await blocker.wait()
        return {"type": "http.disconnect"}

    body_seen = False

    async def send(message: dict[str, object]) -> None:
        nonlocal body_seen
        if message["type"] == "http.response.body":
            body_seen = True
            raise ConnectionError("client disconnected")

    with pytest.raises((ConnectionError, BaseExceptionGroup)):
        await response({"type": "http", "method": "GET", "path": "/"}, receive, send)
    assert body_seen and export.temporary_path is None
    assert not private.exists() or not any(private.iterdir())
