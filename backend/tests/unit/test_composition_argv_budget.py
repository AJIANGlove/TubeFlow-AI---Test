"""大项目合成不得再以超长 argv 触发 execve E2BIG（Linux 单参数上限 128 KiB）。"""

from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path

import pytest

from tubeflow_test.adapters.ffmpeg import FFmpegComposer
from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.composition import (
    CompositionRequest,
    GroupInput,
    NarrationSegmentInput,
    SentenceTiming,
)
from tubeflow_test.domain.errors import DomainError

MAX_ARG_STRLEN = 131_072  # Linux 单参数硬上限：32 * PAGE_SIZE
GROUP_COUNT = 170
SENTENCE_COUNT = 711
SENTENCE_MS = 1_000


class Resolver:
    def __init__(self, root: Path):
        self.root = root

    def resolve_read(self, asset_id: str) -> Path:
        return self.root / asset_id

    def unique_candidate(self, project_id: str) -> Path:
        return self.root / f"candidate-{project_id}.mp4"


class Runner:
    """按合成请求回答探测结果，并记录每次 argv；fail_with 用于模拟 execve 失败。"""

    def __init__(self, request: CompositionRequest, *, fail_with: OSError | None = None):
        self.request = request
        self.fail_with = fail_with
        self.calls: list[list[str]] = []

    def run(self, argv: list[str] | tuple[str, ...]) -> str:
        call = list(argv)
        self.calls.append(call)
        if "-version" in call:
            return f"{call[0]} version test\n"
        if call[0] == "ffmpeg":
            if self.fail_with is not None:
                raise self.fail_with
            return ""
        path = Path(call[-1])
        name = path.name
        is_output = name.startswith("candidate-")
        is_tts = name.startswith("tts")
        if is_tts:
            duration_ms = next(
                segment.duration_ms
                for group in self.request.groups
                for segment in group.narration_segments
                if segment.audio_asset_id == name
            )
            frames = max(1, duration_ms * 24 // 1000)
            width = height = 0
            streams: list[dict[str, object]] = [{"codec_type": "audio", "sample_rate": "48000"}]
        else:
            stem = path.stem
            index = int(stem.removeprefix("video")) if stem.startswith("video") else 0
            if is_output:
                duration_ms = self.request.target_duration_ms
                frames = self.request.total_frames
                width, height = self.request.width, self.request.height
            else:
                duration_ms = self.request.groups[index].target_duration_ms
                frames = self.request.group_frame_counts()[index]
                width, height = self.request.group_width, self.request.group_height
            streams = [
                {
                    "codec_type": "video",
                    "width": width,
                    "height": height,
                    "avg_frame_rate": "24/1",
                    "nb_read_frames": str(frames),
                },
                {"codec_type": "audio", "sample_rate": "48000"},
            ]
        return json.dumps({"format": {"duration": str(duration_ms / 1000)}, "streams": streams})


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def large_request() -> CompositionRequest:
    """170 组 / 711 句，与事故项目 8971d305 同规模。"""
    per_group = [SENTENCE_COUNT // GROUP_COUNT] * GROUP_COUNT
    for index in range(SENTENCE_COUNT % GROUP_COUNT):
        per_group[index] += 1
    groups: list[GroupInput] = []
    sentences: list[SentenceTiming] = []
    cursor_ms = 0
    order = 0
    for group_index, count in enumerate(per_group):
        segments: list[NarrationSegmentInput] = []
        for _ in range(count):
            text = f"第{order}句旁白"
            sentences.append(
                SentenceTiming(
                    f"s{order}",
                    order,
                    text,
                    _text_hash(text),
                    cursor_ms,
                    cursor_ms + SENTENCE_MS,
                )
            )
            segments.append(
                NarrationSegmentInput(
                    f"s{order}",
                    f"tts{order}.mp3",
                    SENTENCE_MS,
                    SENTENCE_MS * 48,
                    48_000,
                )
            )
            cursor_ms += SENTENCE_MS
            order += 1
        groups.append(
            GroupInput(
                f"g{group_index}",
                group_index,
                f"video{group_index}.mp4",
                tuple(segments),
                count * SENTENCE_MS,
            )
        )
    return CompositionRequest(
        "p",
        "a" * 64,
        tuple(groups),
        tuple(sentences),
        AspectRatio.LANDSCAPE,
        1920,
        1080,
    )


def test_large_project_passes_filter_graph_by_file_not_by_argument(tmp_path: Path) -> None:
    request = large_request()
    runner = Runner(request)
    composer = FFmpegComposer(runner, Resolver(tmp_path))

    _, manifest = composer.compose(request)

    ffmpeg_call = next(call for call in runner.calls if call[0] == "ffmpeg")
    assert "-filter_complex" not in ffmpeg_call
    assert "-filter_complex_script" in ffmpeg_call
    script_path = Path(ffmpeg_call[ffmpeg_call.index("-filter_complex_script") + 1])
    assert not script_path.exists(), "临时 filter 脚本必须在合成结束后清理"

    assert len(manifest.filter_graph) > MAX_ARG_STRLEN
    assert max(len(item) for item in ffmpeg_call) < MAX_ARG_STRLEN


def test_filter_script_content_matches_manifest(tmp_path: Path) -> None:
    request = large_request()
    captured: dict[str, str] = {}

    class InspectRunner(Runner):
        def run(self, argv: list[str] | tuple[str, ...]) -> str:
            call = list(argv)
            if call[0] == "ffmpeg" and "-filter_complex_script" in call:
                path = Path(call[call.index("-filter_complex_script") + 1])
                captured["graph"] = path.read_text(encoding="utf-8")
            return super().run(call)

    composer = FFmpegComposer(InspectRunner(request), Resolver(tmp_path))

    _, manifest = composer.compose(request)

    assert captured["graph"] == manifest.filter_graph


def test_execve_failure_maps_to_stable_code_and_cleans_up(tmp_path: Path) -> None:
    request = large_request()
    failure = OSError(errno.E2BIG, "Argument list too long")
    runner = Runner(request, fail_with=failure)
    composer = FFmpegComposer(runner, Resolver(tmp_path))

    with pytest.raises(DomainError) as raised:
        composer.compose(request)

    assert raised.value.code == "composition_exec_failed"
    assert f"[Errno {errno.E2BIG}]" in raised.value.message
    ffmpeg_call = next(call for call in runner.calls if call[0] == "ffmpeg")
    script_path = Path(ffmpeg_call[ffmpeg_call.index("-filter_complex_script") + 1])
    assert not script_path.exists()
    assert not (tmp_path / "candidate-p.mp4").exists()
