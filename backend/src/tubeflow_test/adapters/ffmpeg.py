from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from tubeflow_test.domain.composition import CompositionRequest
from tubeflow_test.domain.errors import DomainError


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> str: ...


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class AssetPathResolver(Protocol):
    def resolve_read(self, asset_id: str) -> Path: ...

    def unique_candidate(self, project_id: str) -> Path: ...


class SafeSubprocessRunner:
    def __init__(
        self, timeout_seconds: float = 600, cancel: CancellationSignal | None = None
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._cancel = cancel

    def run(self, argv: Sequence[str]) -> str:
        if not argv or any("\x00" in argument for argument in argv):
            raise ValueError("invalid subprocess arguments")
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        deadline = time.monotonic() + self._timeout_seconds
        try:
            stdout = stderr = ""
            while True:
                if self._cancel is not None and self._cancel.is_set():
                    raise DomainError("media_command_cancelled", "media processing was cancelled")
                if time.monotonic() >= deadline:
                    raise DomainError("media_command_timeout", "media processing timed out")
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(0.1, max(0.001, deadline - time.monotonic()))
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            self._terminate(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pid = getattr(process, "pid", 0)
                if os.name != "nt" and pid:
                    with suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)  # type: ignore[attr-defined]
                else:
                    process.kill()
                process.wait()
            raise
        if process.returncode != 0:
            stderr_digest = sha256(stderr.encode(errors="replace")).hexdigest()[:16]
            logging.getLogger(__name__).error(
                "media command failed rc=%s stderr_sha256_prefix=%s",
                process.returncode,
                stderr_digest,
            )
            raise DomainError("media_command_failed", "media processing failed")
        return stdout

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        pid = getattr(process, "pid", 0)
        if os.name == "nt" and pid:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                shell=False,
            )
            return
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)  # type: ignore[attr-defined]
                return
            except ProcessLookupError:
                return
        process.terminate()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    duration_ms: int
    width: int
    height: int
    fps: str
    frame_count: int
    has_video: bool
    has_audio: bool
    audio_sample_rate: int | None


@dataclass(frozen=True, slots=True)
class CompositionManifest:
    ffmpeg_version: str
    ffprobe_version: str
    inputs: tuple[dict[str, object], ...]
    filter_graph: str
    encoding: dict[str, object]
    environment_mix: dict[str, object]
    output_probe: ProbeResult
    total_frames: int


class FFmpegComposer:
    def __init__(
        self,
        runner: CommandRunner,
        resolver: AssetPathResolver,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ):
        self._runner = runner
        self._resolver = resolver
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    def compose(self, request: CompositionRequest) -> tuple[Path, CompositionManifest]:
        output = self._resolver.unique_candidate(request.project_id)
        if output.exists():
            raise DomainError(
                "composition_candidate_exists", "composition candidate already exists"
            )
        inputs: list[str] = []
        filters: list[str] = []
        video_labels: list[str] = []
        audio_labels: list[str] = []
        trace_inputs: list[dict[str, object]] = []
        frames = request.group_frame_counts()
        input_index = 0
        for index, (group, allocated_frames) in enumerate(zip(request.groups, frames, strict=True)):
            video_path = self._resolver.resolve_read(group.video_asset_id)
            video_probe = self.probe(video_path)
            self._validate_group_video(
                request, group.target_duration_ms, allocated_frames, video_probe
            )
            inputs.extend(["-i", str(video_path)])
            video_index = input_index
            input_index += 1
            segment_labels: list[str] = []
            narration_probes: list[ProbeResult] = []
            for segment_index, segment in enumerate(group.narration_segments):
                narration_path = self._resolver.resolve_read(segment.audio_asset_id)
                narration_probe = self.probe(narration_path)
                self._validate_narration(segment.duration_ms, narration_probe)
                narration_probes.append(narration_probe)
                inputs.extend(["-i", str(narration_path)])
                label = f"tts{index}_{segment_index}"
                segment_samples = _output_samples(
                    segment.measured_sample_count, segment.sample_rate_hz
                )
                filters.append(
                    f"[{input_index}:a]aresample=48000,"
                    f"atrim=end_sample={segment_samples},"
                    "asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:channel_layouts=stereo"
                    f"[{label}]"
                )
                segment_labels.append(f"[{label}]")
                input_index += 1
            filters.append(
                "".join(segment_labels)
                + f"concat=n={len(segment_labels)}:v=0:a=1,volume=1.0[tts{index}]"
            )
            filters.extend(
                [
                    f"[{video_index}:v]trim=end_frame={allocated_frames},setpts=PTS-STARTPTS,fps=24[v{index}]",
                    f"[{video_index}:a]aresample=48000,"
                    f"atrim=end_sample={sum(_output_samples(item.measured_sample_count, item.sample_rate_hz) for item in group.narration_segments)},"  # noqa: E501
                    "asetpts=PTS-STARTPTS,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"volume=0.25[env{index}]",
                    f"[env{index}][tts{index}]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.95:attack=5:release=50[a{index}]",
                ]
            )
            video_labels.append(f"[v{index}]")
            audio_labels.append(f"[a{index}]")
            trace_inputs.append(
                {
                    **asdict(group),
                    "video_probe": asdict(video_probe),
                    "narration_probes": [asdict(item) for item in narration_probes],
                    "allocated_frames": allocated_frames,
                }
            )
        video_tail = ""
        if (request.width, request.height) != (request.group_width, request.group_height):
            video_tail += (
                f",scale={request.width}:{request.height}:"
                "force_original_aspect_ratio=increase:flags=lanczos"
                f",crop={request.width}:{request.height}"
            )
        if request.sharpen > 0:
            video_tail += f",unsharp=5:5:{request.sharpen}:5:5:0.0"
        filters.append(
            "".join(video_labels)
            + f"concat=n={len(request.groups)}:v=1:a=0{video_tail}[outv]"
        )
        filters.append(
            "".join(audio_labels)
            + f"concat=n={len(request.groups)}:v=0:a=1,"
            + f"atrim=end_sample={request.output_sample_count()}[outa]"
        )
        graph = ";".join(filters)
        # Linux 单个 argv 参数上限 128 KiB（MAX_ARG_STRLEN）；大项目（170 组/711 句）
        # 的 filter graph 约 150 KB，直接作为参数会 execve E2BIG，故落盘用
        # -filter_complex_script 传入（图内容等价，manifest 仍保存明文）。
        script_path = _write_filter_script(graph, output.parent)
        argv = [
            self._ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-n",
            *inputs,
            "-filter_complex_script",
            str(script_path),
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(request.crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            self._runner.run(argv)
            output_probe = self.probe(output)
            self._validate_output(request, output_probe)
        except BaseException as error:
            output.unlink(missing_ok=True)
            if isinstance(error, OSError):
                # execve/IO 层失败（E2BIG、EMFILE、ENOENT…）要落成稳定码，
                # 否则调用方只能记成泛化的 composition_failed。
                raise DomainError(
                    "composition_exec_failed",
                    f"media command failed: [Errno {error.errno}] {error.strerror}",
                ) from error
            raise
        finally:
            script_path.unlink(missing_ok=True)
        try:
            manifest = CompositionManifest(
                ffmpeg_version=self._version(self._ffmpeg),
                ffprobe_version=self._version(self._ffprobe),
                inputs=tuple(trace_inputs),
                filter_graph=graph,
                encoding={
                    "video": "libx264",
                    "crf": request.crf,
                    "sharpen": request.sharpen,
                    "output_width": request.width,
                    "output_height": request.height,
                    "group_width": request.group_width,
                    "group_height": request.group_height,
                    "pixel_format": "yuv420p",
                    "audio": "aac",
                    "fps": 24,
                    "sample_rate": 48000,
                    "channels": 2,
                },
                environment_mix={
                    "h3_volume": 0.25,
                    "tts_volume": 1.0,
                    "normalize": False,
                    "limiter": 0.95,
                },
                output_probe=output_probe,
                total_frames=request.total_frames,
            )
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        return output, manifest

    def _validate_group_video(
        self,
        request: CompositionRequest,
        target_ms: int,
        allocated_frames: int,
        video: ProbeResult,
    ) -> None:
        if not video.has_video or not video.has_audio:
            raise DomainError(
                "invalid_group_streams", "group video requires native video and audio tracks"
            )
        if video.duration_ms < target_ms:
            raise DomainError("group_video_too_short", "group video is shorter than its target")
        if video.frame_count < allocated_frames:
            raise DomainError(
                "group_video_too_short", "group video has fewer frames than allocated"
            )
        if video.fps != "24/1" or (video.width, video.height) != (
            request.group_width,
            request.group_height,
        ):
            raise DomainError(
                "group_video_spec_mismatch", "group video does not match native project format"
            )

    def _validate_narration(self, target_ms: int, narration: ProbeResult) -> None:
        if (
            not narration.has_audio
            or narration.duration_ms <= 0
            or abs(narration.duration_ms - target_ms) > 1
        ):
            raise DomainError("narration_duration_mismatch", "measured narration is invalid")

    def _validate_output(self, request: CompositionRequest, probe: ProbeResult) -> None:
        if (
            not probe.has_video
            or not probe.has_audio
            or probe.fps != "24/1"
            or probe.audio_sample_rate != 48000
        ):
            raise DomainError(
                "composition_format_mismatch", "composition streams or frame rate are invalid"
            )
        if (probe.width, probe.height) != (request.width, request.height):
            raise DomainError("composition_format_mismatch", "composition resolution is invalid")
        if (
            abs(probe.frame_count - request.total_frames) > 1
            or abs(probe.duration_ms - request.target_duration_ms) > 1000 / request.fps
        ):
            raise DomainError("composition_duration_mismatch", "output exceeds one-frame tolerance")

    def probe(self, path: Path) -> ProbeResult:
        raw = self._runner.run(
            [
                self._ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ]
        )
        try:
            data = json.loads(raw)
            streams = data.get("streams", [])
            video = next((item for item in streams if item.get("codec_type") == "video"), None)
            audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
            duration_ms = round(float(data["format"]["duration"]) * 1000)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DomainError("invalid_probe_result", "media probe result is invalid") from exc
        try:
            return ProbeResult(
                duration_ms,
                int(video.get("width", 0)) if video else 0,
                int(video.get("height", 0)) if video else 0,
                str(video.get("avg_frame_rate", "")) if video else "",
                int(video.get("nb_read_frames", 0)) if video else 0,
                video is not None,
                audio is not None,
                int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
            )
        except (TypeError, ValueError) as exc:
            raise DomainError("invalid_probe_result", "media probe result is invalid") from exc

    def _version(self, executable: str) -> str:
        return self._runner.run([executable, "-version"]).splitlines()[0]


def _write_filter_script(graph: str, directory: Path) -> Path:
    """filter graph 落盘后交给 ffmpeg 读取，避免超长 argv 触发 execve E2BIG。"""
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".ffgraph",
        prefix="compose-",
        dir=directory,
        delete=False,
    ) as handle:
        handle.write(graph)
        return Path(handle.name)


def _output_samples(sample_count: int, sample_rate_hz: int) -> int:
    numerator = sample_count * 48_000
    if numerator % sample_rate_hz:
        raise DomainError(
            "composition_sample_rate_mismatch",
            "measured narration cannot be represented at the output sample rate",
        )
    return numerator // sample_rate_hz
