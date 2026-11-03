from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from threading import Event

import pytest

from tubeflow_test.adapters.ffmpeg import FFmpegComposer, SafeSubprocessRunner
from tubeflow_test.domain.common import AspectRatio
from tubeflow_test.domain.composition import (
    CompositionRequest,
    GroupInput,
    NarrationSegmentInput,
    SentenceTiming,
    render_srt,
)
from tubeflow_test.domain.errors import DomainError


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Resolver:
    def __init__(self, root: Path):
        self.root = root

    def resolve_read(self, asset_id: str) -> Path:
        return self.root / asset_id

    def unique_candidate(self, project_id: str) -> Path:
        return self.root / f"candidate-{project_id}.mp4"


class Runner:
    def __init__(self, request: CompositionRequest, *, bad: str | None = None):
        self.calls: list[list[str]] = []
        self.request = request
        self.bad = bad

    def run(self, argv: list[str] | tuple[str, ...]) -> str:
        call = list(argv)
        self.calls.append(call)
        if "-version" in call:
            return f"{call[0]} version test\n"
        if call[0] == "ffmpeg":
            return ""
        path = call[-1]
        is_tts = "tts" in Path(path).name
        is_output = "candidate-" in path
        group_index = next(
            (index for index in range(len(self.request.groups)) if str(index) in path), 0
        )
        duration = (
            self.request.target_duration_ms
            if is_output
            else self.request.groups[group_index].target_duration_ms
        )
        if is_tts:
            duration = next(
                segment.duration_ms
                for group in self.request.groups
                for segment in group.narration_segments
                if segment.audio_asset_id == Path(path).name
            )
        frames = (
            self.request.total_frames
            if is_output
            else max(25, self.request.group_frame_counts()[group_index])
        )
        streams: list[dict[str, object]] = []
        if not is_tts:
            streams.append(
                {
                    "codec_type": "video",
                    "width": self.request.width if is_output else self.request.group_width,
                    "height": self.request.height if is_output else self.request.group_height,
                    "avg_frame_rate": "25/1" if self.bad == "fps" else "24/1",
                    "nb_read_frames": str(frames),
                }
            )
        if self.bad != "audio" or is_tts:
            streams.append({"codec_type": "audio", "sample_rate": "48000"})
        if self.bad == "short" and not is_tts and not is_output:
            duration = 900
        return json.dumps({"format": {"duration": str(duration / 1000)}, "streams": streams})


def request(durations: tuple[int, ...] = (1001, 1001)) -> CompositionRequest:
    total = sum(durations)
    return CompositionRequest(
        "p",
        "a" * 64,
        tuple(
            GroupInput(
                f"g{i}",
                i,
                f"video{i}",
                (NarrationSegmentInput(f"s{i}", f"tts{i}", ms, ms * 48, 48_000),),
                ms,
            )
            for i, ms in enumerate(durations)
        ),
        (SentenceTiming("s", 0, "多语言\n安全", text_hash("多语言\n安全"), 0, total),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )


def test_frame_budget_uses_cumulative_boundaries_and_rounds_total_once() -> None:
    item = request()
    assert item.group_frame_counts() == (24, 24)
    assert sum(item.group_frame_counts()) == item.total_frames == 48
    random.seed(7)
    for _ in range(200):
        durations = tuple(random.randint(1, 15000) for _ in range(random.randint(1, 20)))
        item = request(durations)
        try:
            counts = item.group_frame_counts()
        except DomainError as error:
            assert error.code == "insufficient_composition_frames"
        else:
            assert sum(counts) == item.total_frames
    assert request((20, 21, 22)).total_frames == 2
    assert request((20, 21)).total_frames == 1


def test_positive_groups_fail_when_frame_budget_cannot_give_each_one_frame() -> None:
    with pytest.raises(DomainError) as error:
        request((1, 1)).group_frame_counts()
    assert error.value.code == "insufficient_composition_frames"


def test_ffmpeg_validates_inputs_and_uses_safe_mix_contract(tmp_path: Path) -> None:
    item = request()
    runner = Runner(item)
    output, manifest = FFmpegComposer(runner, Resolver(tmp_path)).compose(item)
    command = next(call for call in runner.calls if call[0] == "ffmpeg")
    assert output.name.startswith("candidate-") and "-n" in command and "-y" not in command
    assert "aresample=48000" in manifest.filter_graph
    assert "sample_fmts=fltp:channel_layouts=stereo" in manifest.filter_graph
    assert "volume=0.25" in manifest.filter_graph and "volume=1.0" in manifest.filter_graph
    assert "alimiter=limit=0.95" in manifest.filter_graph
    assert manifest.total_frames == 48
    assert f"concat=n={len(item.groups)}:v=1:a=0[outv]" in manifest.filter_graph
    expected_audio_concat = (
        f"concat=n={len(item.groups)}:v=0:a=1,atrim=end_sample=96096[outa]"
    )
    assert expected_audio_concat in manifest.filter_graph
    assert "concat=n=2:v=1:a=1" not in manifest.filter_graph


def test_scaled_output_accepts_hd_group_and_writes_1080p_container(tmp_path: Path) -> None:
    item = CompositionRequest(
        "p",
        "a" * 64,
        (GroupInput("g0", 0, "video0", (NarrationSegmentInput("s0", "tts0", 1001, 48048, 48_000),), 1001),),
        (SentenceTiming("s", 0, "多语言\n安全", text_hash("多语言\n安全"), 0, 1001),),
        AspectRatio.LANDSCAPE,
        1920,
        1080,
        24,
        1600,
        896,
        20,
        0.4,
    )
    runner = Runner(item)
    _, manifest = FFmpegComposer(runner, Resolver(tmp_path)).compose(item)
    assert "scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos" in manifest.filter_graph
    assert "crop=1920:1080" in manifest.filter_graph
    assert "unsharp=5:5:0.4:5:5:0.0" in manifest.filter_graph
    assert manifest.encoding["crf"] == 20
    assert manifest.encoding["sharpen"] == 0.4
    assert manifest.encoding["output_width"] == 1920
    assert manifest.encoding["output_height"] == 1080
    assert manifest.encoding["group_width"] == 1600
    command = next(call for call in runner.calls if call[0] == "ffmpeg")
    assert command[command.index("-crf") + 1] == "20"


def test_group_tts_is_built_from_multiple_authoritative_sentence_assets(tmp_path: Path) -> None:
    text = "one two"
    item = CompositionRequest(
        "p",
        "a" * 64,
        (
            GroupInput(
                "g0",
                0,
                "video0",
                (
                    NarrationSegmentInput("s0", "tts0a", 500, 24_000, 48_000),
                    NarrationSegmentInput("s1", "tts0b", 501, 24_048, 48_000),
                ),
                1001,
            ),
        ),
        (SentenceTiming("s0", 0, text, text_hash(text), 0, 1001),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )
    runner = Runner(item)
    _, manifest = FFmpegComposer(runner, Resolver(tmp_path)).compose(item)
    assert "[tts0_0][tts0_1]concat=n=2:v=0:a=1,volume=1.0[tts0]" in manifest.filter_graph
    assert "aresample=48000,atrim=end_sample=24000" in manifest.filter_graph
    assert "aresample=48000,atrim=end_sample=24048" in manifest.filter_graph


def test_fractional_sample_endpoints_drive_frames_and_exact_final_trim(tmp_path: Path) -> None:
    text = "fractional timeline"
    segments = (
        NarrationSegmentInput("s0", "tts0", 1001, 24013, 24000),
        NarrationSegmentInput("s1", "tts1", 1001, 24013, 24000),
        NarrationSegmentInput("s2", "tts2", 1001, 24013, 24000),
    )
    item = CompositionRequest(
        "p",
        "a" * 64,
        (
            GroupInput("g0", 0, "video0", segments[:2], 2001),
            GroupInput("g1", 1, "video1", segments[2:], 1001),
        ),
        (SentenceTiming("s", 0, text, text_hash(text), 0, 3002),),
        AspectRatio.LANDSCAPE,
        1280,
        720,
    )
    assert item.group_frame_counts() == (48, 24)
    assert item.total_frames == 72
    _, manifest = FFmpegComposer(Runner(item), Resolver(tmp_path)).compose(item)
    assert manifest.filter_graph.count("atrim=end_sample=48026") == 4
    assert "concat=n=2:v=0:a=1,atrim=end_sample=144078[outa]" in manifest.filter_graph


def test_alternating_frame_rounding_never_moves_audio_segment_boundaries(tmp_path: Path) -> None:
    item = request((1021,) * 5)
    runner = Runner(item)
    _, manifest = FFmpegComposer(runner, Resolver(tmp_path)).compose(item)
    assert item.group_frame_counts() == (25, 24, 25, 24, 25)
    assert sum(item.group_frame_counts()) == item.total_frames == 123
    assert manifest.filter_graph.count("atrim=end_sample=49008") == 10


@pytest.mark.parametrize(
    "bad,code",
    [
        ("short", "group_video_too_short"),
        ("audio", "invalid_group_streams"),
        ("fps", "group_video_spec_mismatch"),
    ],
)
def test_ffmpeg_rejects_bad_group_media(tmp_path: Path, bad: str, code: str) -> None:
    item = request()
    with pytest.raises(DomainError) as error:
        FFmpegComposer(Runner(item, bad=bad), Resolver(tmp_path)).compose(item)
    assert error.value.code == code


def test_srt_is_contiguous_exact_and_binds_multiline_text() -> None:
    sentences = (
        SentenceTiming("a", 0, "こんにちは\n世界", text_hash("こんにちは\n世界"), 0, 1001),
        SentenceTiming("b", 1, "Olá", text_hash("Olá"), 1001, 2002),
    )
    rendered = render_srt(sentences, total_duration_ms=2002)
    assert rendered.startswith("1\n00:00:00,000 --> 00:00:01,001\nこんにちは\n世界")
    assert "00:00:01,001 --> 00:00:02,002" in rendered
    with pytest.raises(DomainError, match="contiguous"):
        render_srt((SentenceTiming("a", 0, "A", text_hash("A"), 1, 2),), total_duration_ms=2)
    with pytest.raises(DomainError, match="end"):
        render_srt((SentenceTiming("a", 0, "A", text_hash("A"), 0, 1),), total_duration_ms=2)


def test_sentence_rejects_wrong_text_provenance_hash() -> None:
    with pytest.raises(DomainError) as error:
        SentenceTiming("a", 0, "日本語", text_hash("different"), 0, 1)
    assert error.value.code == "subtitle_text_hash_mismatch"


def test_sentence_normalizes_blank_lines_before_hash_validation() -> None:
    normalized = "first\nsecond"
    sentence = SentenceTiming("a", 0, "first\r\n\r\nsecond", text_hash(normalized), 0, 1)
    assert sentence.text == normalized
    with pytest.raises(DomainError) as error:
        SentenceTiming("a", 0, "first\n\nsecond", text_hash("first\n\nsecond"), 0, 1)
    assert error.value.code == "subtitle_text_hash_mismatch"


class HangingProcess:
    returncode: int | None = None

    def __init__(self) -> None:
        self.terminated = False

    def poll(self) -> None:
        return None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        raise __import__("subprocess").TimeoutExpired("ffmpeg", timeout)

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = -1
        return -1

    def kill(self) -> None:
        self.returncode = -9


def test_subprocess_runner_cancellation_terminates_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = HangingProcess()
    monkeypatch.setattr(
        "tubeflow_test.adapters.ffmpeg.subprocess.Popen", lambda *args, **kwargs: process
    )
    cancelled = Event()
    cancelled.set()
    with pytest.raises(DomainError) as error:
        SafeSubprocessRunner(cancel=cancelled).run(["ffmpeg"])
    assert error.value.code == "media_command_cancelled" and process.terminated


def test_subprocess_runner_timeout_terminates_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = HangingProcess()
    monkeypatch.setattr(
        "tubeflow_test.adapters.ffmpeg.subprocess.Popen", lambda *args, **kwargs: process
    )
    with pytest.raises(DomainError) as error:
        SafeSubprocessRunner(timeout_seconds=0).run(["ffmpeg"])
    assert error.value.code == "media_command_timeout" and process.terminated

def test_media_authority_normalizes_split_boundary_whitespace() -> None:
    # 生产实例（2026-09-17）：拆句 agent 保留原句边界空白，字幕输入须先规范化并配套哈希，
    # 否则合成入口的 subtitle_text_hash_mismatch 会阻断整项目。
    from uuid import uuid4 as _uuid4

    from tubeflow_test.application.media_authority import (
        CompositionProjectSnapshot,
        LatestFeatureCompositionInputs,
        LatestH3Group,
        LatestNarrationSentence,
    )
    sentence_id, group_id, video_asset, audio_asset, project, owner = (_uuid4() for _ in range(6))
    raw_text = "The instinct to seek cover " + chr(0x2014) + " "
    sentence = LatestNarrationSentence(
        sentence_id,
        1,
        raw_text,
        text_hash(raw_text),
        0,
        48000,
        48000,
        audio_asset,
    )
    group = LatestH3Group(group_id, 1, video_asset, (sentence_id,), 0, 1000, 1280, 720)
    snapshot = CompositionProjectSnapshot("a" * 64, AspectRatio.LANDSCAPE, 1280, 720)

    class _Narration:
        def timeline(self, project_id, owner_id):
            return (sentence,)

    class _Groups:
        def current_groups(self, project_id, owner_id):
            return (group,)

    class _Snapshot:
        def snapshot(self, project_id, owner_id):
            return snapshot

    request = LatestFeatureCompositionInputs(_Narration(), _Groups(), _Snapshot()).load(
        project, owner
    )
    assert request is not None
    (timing,) = request.sentences
    assert timing.text == "The instinct to seek cover " + chr(0x2014)
    assert timing.text_sha256 == text_hash(timing.text)


def test_normalize_subtitle_text_boundaries() -> None:
    from tubeflow_test.domain.composition import normalize_subtitle_text

    cr, lf = chr(13), chr(10)
    assert normalize_subtitle_text("  hello world.  ") == "hello world."
    assert normalize_subtitle_text("a" + cr + lf + cr + lf + cr + lf + "b") == "a" + lf + "b"
    assert normalize_subtitle_text("a" + lf + " " + lf + lf + "b") == "a" + lf + "b"
    assert normalize_subtitle_text("x") == "x"
