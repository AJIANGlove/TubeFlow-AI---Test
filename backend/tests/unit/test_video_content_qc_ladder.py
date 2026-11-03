"""内容级 QC（黑屏类画面缺陷）检测接线、自动重试升级梯与带病交付终态测试。

2026-10-16 决策：
- 归档 QC 在技术规格检测通过后执行内容级抽帧检测；命中持续黑屏区域即 QC_FAILED
  （error_code=video_content_black_region）并进入 AUTO 重提链；抽样失败同样失败闭环。
- QC 画面缺陷自动重试是确定性升级梯（每组每预算轮总提交 ≤ 6）：i=2/5 仅换 seed，
  i=3/6 换 seed 并带 QC 原因 repair_reason 重编译，i=4 换 seed 并以 kinestack_v6_port
  策略重编译；重编译不可用/失败退化为仅换 seed 并留证据标记。
- 梯子耗尽后对内容级缺陷带病交付：照常归档完成、成为当前视频、缺陷证据持久保留；
  技术规格 QC 失败耗尽后维持既有 park/暂停语义；MANUAL 项目行为不变。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from tubeflow_test.auto_pipeline.ports import AutoPipelineFacts, VideoGroupFact
from tubeflow_test.domain.common import AspectRatio, MediaAssetRef
from tubeflow_test.domain.storyboards import DirectorShot, ShotSentenceLink
from tubeflow_test.video_generation.content_qc import (
    DEFAULT_THRESHOLDS,
    ContentQcThresholds,
    FrameStats,
    TileStat,
)
from tubeflow_test.video_generation.domain import (
    ImmutableWorkflowSnapshot,
    _validate_seed,
    canonical_sha256,
)
from tubeflow_test.video_generation.engine import EngineNode
from tubeflow_test.video_generation.fake_engine import FakeEngine
from tubeflow_test.video_generation.orchestration import (
    SUBMISSION_BUDGET_EXHAUSTED,
    VIDEO_CONTENT_BLACK_REGION,
    VIDEO_CONTENT_QC_SAMPLER_ERROR,
    ArchiveStage,
    AttemptStage,
    AuthoritativeGroupInput,
    AutoResubmitPolicy,
    DurableGroupVideoOrchestrator,
    PersistentAttempt,
    VideoMediaEvidence,
    _ladder_seed,
    _ladder_strategy,
)
from tubeflow_test.video_generation.repository import InMemoryVideoRepository
from tubeflow_test.video_prompt.domain import (
    CompiledVideoPrompt,
    VideoPromptCompilerInput,
    VideoPromptStrategy,
)

GRID_COLUMNS = DEFAULT_THRESHOLDS.grid_columns
GRID_ROWS = DEFAULT_THRESHOLDS.grid_rows

BLACK_TILE = TileStat(mean=4.0, max_luma=12, stddev=0.8)
TEXTURED_TILE = TileStat(mean=48.0, max_luma=181, stddev=12.0)


def _frame(second: float, black_cells: frozenset[tuple[int, int]] | None = None) -> FrameStats:
    black = black_cells if black_cells is not None else frozenset()
    return FrameStats(
        second=second,
        tiles=tuple(
            tuple(
                BLACK_TILE if (row, column) in black else TEXTURED_TILE
                for column in range(GRID_COLUMNS)
            )
            for row in range(GRID_ROWS)
        ),
    )


def _right_half_cells() -> frozenset[tuple[int, int]]:
    return frozenset((row, column) for row in range(GRID_ROWS) for column in range(4, 8))


def healthy_frames(count: int = 4) -> tuple[FrameStats, ...]:
    return tuple(_frame(float(second)) for second in range(count))


def black_frames(count: int = 4) -> tuple[FrameStats, ...]:
    return tuple(_frame(float(second), _right_half_cells()) for second in range(count))


class FakeSampler:
    """编程化抽帧器：返回预设帧统计或按配置抛错；记录每次调用。"""

    def __init__(self, frames: tuple[FrameStats, ...] | None = None) -> None:
        self.frames = frames if frames is not None else healthy_frames()
        self.calls: list[bytes] = []
        self.error: Exception | None = None

    def sample(
        self, content: bytes, *, thresholds: ContentQcThresholds = DEFAULT_THRESHOLDS
    ) -> tuple[FrameStats, ...]:
        self.calls.append(content)
        if self.error is not None:
            raise self.error
        return self.frames


class RecordingCompiler:
    """记录调用参数的确定性假编译器；支持 repair_reason 形参（梯子重编译透传验证）。"""

    def __init__(self, strategy: VideoPromptStrategy = VideoPromptStrategy.TUBEFLOW_ENHANCED_V1):
        self._strategy = strategy
        self.calls: list[dict[str, object]] = []
        self.fail_with: Exception | None = None

    @property
    def strategy(self) -> VideoPromptStrategy:
        return self._strategy

    def compile(
        self,
        value: VideoPromptCompilerInput,
        *,
        max_repairs: int = 1,
        repair_reason: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> CompiledVideoPrompt:
        del context
        self.calls.append(
            {"input": value, "max_repairs": max_repairs, "repair_reason": repair_reason}
        )
        if self.fail_with is not None:
            raise self.fail_with
        return CompiledVideoPrompt(
            f"compiled video prompt for {value.idempotency_key}",
            value.strategy,
            "fake-model",
            "fake-receipt",
            False,
            value.fingerprint,
        )

    def close(self) -> None:
        pass


class Assets:
    def __init__(self) -> None:
        self.values: dict[UUID, bytes] = {}

    def add(self, value: bytes, media_type: str = "image/png") -> MediaAssetRef:
        result = MediaAssetRef(uuid4(), hashlib.sha256(value).hexdigest(), media_type, len(value))
        self.values[result.asset_id] = value
        return result

    def read_verified(self, asset: MediaAssetRef) -> bytes:
        return self.values[asset.asset_id]

    def archive_immutable(
        self,
        *,
        attempt_id: UUID,
        generation_id: UUID,
        output_id: str,
        content: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> MediaAssetRef:
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        return self.add(content, media_type)


class Probe:
    def __init__(self, evidence: VideoMediaEvidence | None = None) -> None:
        self.evidence = evidence or VideoMediaEvidence(
            "video/mp4", 1056, 608, 24.0, 3100, 1, 1, ("mp4",), 3100, 74, 0, 3100, 3100, "aac"
        )

    def inspect(self, content: bytes) -> VideoMediaEvidence:
        return self.evidence


def setup(
    production_mode: str = "AUTO",
    probe: Probe | None = None,
    sampler: FakeSampler | None = None,
    compiler: RecordingCompiler | None = None,
    seed: int | str | None = 100,
) -> tuple[
    AuthoritativeGroupInput,
    InMemoryVideoRepository,
    FakeEngine,
    Assets,
    RecordingCompiler,
    DurableGroupVideoOrchestrator,
]:
    assets = Assets()
    owner, project, group, generation, revision = (uuid4() for _ in range(5))
    manifest_bytes = b'{"parameters": {"resolution_preset": "preview"}}\n'
    manifest = {
        "workflow_id": "h3.community.reference-images-to-video",
        "workflow_version": "1.1.0-mvp",
        "parameters": {"resolution_preset": "preview"},
    }
    workflow = ImmutableWorkflowSnapshot(
        "minimax-h3",
        "1",
        "h3.community.reference-images-to-video",
        "1.1.0-mvp",
        hashlib.sha256(manifest_bytes).hexdigest(),
        "preview",
        manifest,
    )
    script = "hello"
    source = AuthoritativeGroupInput(
        owner,
        project,
        group,
        generation,
        True,
        hashlib.sha256(script.encode()).hexdigest(),
        ("1" * 64,),
        ("1" * 64,),
        (3000,),
        (True,),
        (assets.add(b"narration", "audio/wav"),),
        ((0, 3000),),
        (0,),
        0,
        3000,
        3000,
        (assets.add(b"character"),),
        assets.add(b"grid"),
        "GRID_SHEET",
        True,
        revision,
        revision,
        "group prompt",
        AspectRatio.LANDSCAPE,
        seed,
        workflow,
        script,
        (0,),
        1,
        (assets.add(b"raw-narration", "audio/wav"),),
        generation,
        None,
    )
    source = replace(source, storyboard_asset_sha256=source.storyboard_image.sha256)
    source = replace(
        source,
        sentence_sample_counts=(144000,),
        sentence_sample_rates=(48000,),
        timeline_ranges_frames=((0, 144000),),
        group_start_frame=0,
        group_end_frame=144000,
        group_sample_rate=48000,
        authority_snapshot={"fixture": "canonical-v1"},
        production_mode=production_mode,
    )
    shot = DirectorShot(
        uuid4(), 1, "scene", "WIDE", "OBJECTIVE", "action", "function", "description"
    )
    sentence_id = uuid4()
    source = replace(
        source,
        director_shots=(shot,),
        shot_sentence_links=(ShotSentenceLink(shot.shot_id, sentence_id),),
        group_sentence_ids=(sentence_id,),
        visual_style="fixture style",
    )
    nodes = tuple(
        EngineNode(
            f"node-{index}",
            f"http://node-{index}",
            f"secret-{index}",
            1,
            workflow_id=workflow.workflow_id,
            workflow_version=workflow.workflow_version,
            manifest_sha256=workflow.workflow_manifest_sha256,
        )
        for index in range(3)
    )
    repository = InMemoryVideoRepository((source,), nodes)
    engine = FakeEngine()
    recording_compiler = compiler or RecordingCompiler()
    service = DurableGroupVideoOrchestrator(
        repository,
        engine,
        assets,
        probe or Probe(),
        prompt_compiler=recording_compiler,  # type: ignore[arg-type]
        content_sampler=sampler,
    )
    return source, repository, engine, assets, recording_compiler, service


def submit_calls(engine: FakeEngine) -> int:
    return sum(call == "submit" for _, call in engine.calls)


def drive_to_qc(
    source: AuthoritativeGroupInput,
    engine: FakeEngine,
    service: DurableGroupVideoOrchestrator,
    attempt_id: UUID,
) -> PersistentAttempt:
    """推进到归档 QC 段：提交 → 远端成功 → 归档（下载/存储/探测/内容检测）。"""
    submitted = service.advance(source.owner_id, attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    engine.inspect_status = "SUCCEEDED"
    succeeded = service.advance(source.owner_id, attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED
    return service.advance(source.owner_id, attempt_id)


def replacement_for(
    repository: InMemoryVideoRepository, owner_id: UUID, attempt: PersistentAttempt
) -> PersistentAttempt:
    replacement = repository.find_attempt(owner_id, f"auto-resubmit:{attempt.attempt_id}")
    assert replacement is not None
    return replacement


def test_content_qc_black_region_fails_qc_with_evidence() -> None:
    source, repository, engine, _assets, _compiler, service = setup(
        sampler=FakeSampler(black_frames())
    )
    intent = service.trigger(source.owner_id, source.group_id, "content-qc-hit")

    failed = drive_to_qc(source, engine, service, intent.attempt_id)

    assert failed.stage is AttemptStage.SUCCEEDED
    assert failed.archive_stage is ArchiveStage.QC_FAILED
    assert failed.error_code == VIDEO_CONTENT_BLACK_REGION
    assert f"probe:{VIDEO_CONTENT_BLACK_REGION}" in failed.retry_evidence
    assert "auto_resubmit:qc_failed" in failed.retry_evidence
    evidence = failed.probe_evidence
    assert evidence is not None
    assert evidence["qc_passed"] is False
    assert evidence["error_code"] == VIDEO_CONTENT_BLACK_REGION
    content_qc = evidence["content_qc"]
    assert isinstance(content_qc, dict)
    assert content_qc["algorithm"] == DEFAULT_THRESHOLDS.algorithm
    findings = content_qc["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    finding = findings[0]
    assert finding["start_second"] == 0.0
    assert finding["end_second"] == 3.0
    assert finding["bbox_columns"] == [4, 7]
    assert finding["bbox_rows"] == [0, 5]
    assert finding["area_fraction"] == 0.5
    # 命中后计入预算自动重提（i=2 仅换 seed）。
    replacement = replacement_for(repository, source.owner_id, failed)
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 2
    assert submit_calls(engine) == 1
    assert replacement.request["parameters"]["seed"] == 102  # type: ignore[index]
    assert replacement.request["prompt"] == failed.request["prompt"]
    assert "auto_ladder:attempt_2" in replacement.retry_evidence
    assert "auto_ladder:seed_only" in replacement.retry_evidence


def test_spec_qc_failure_carries_no_content_qc_block() -> None:
    bad = replace(Probe().evidence, audio_tracks=0)
    sampler = FakeSampler(healthy_frames())
    source, repository, engine, _assets, _compiler, service = setup(
        probe=Probe(bad), sampler=sampler
    )
    intent = service.trigger(source.owner_id, source.group_id, "spec-qc")

    failed = drive_to_qc(source, engine, service, intent.attempt_id)

    assert failed.archive_stage is ArchiveStage.QC_FAILED
    assert failed.error_code == "VideoRuleViolation"
    evidence = failed.probe_evidence
    assert evidence is not None
    assert evidence["qc_passed"] is False
    assert "content_qc" not in evidence
    # 技术规格失败不做内容检测（抽样器未被调用）。
    assert sampler.calls == []


def test_sampler_error_fails_closed_and_enters_retry_chain() -> None:
    sampler = FakeSampler()
    sampler.error = RuntimeError("ffmpeg timeout")
    source, repository, engine, _assets, _compiler, service = setup(sampler=sampler)
    intent = service.trigger(source.owner_id, source.group_id, "sampler-error")

    failed = drive_to_qc(source, engine, service, intent.attempt_id)

    assert failed.archive_stage is ArchiveStage.QC_FAILED
    assert failed.error_code == VIDEO_CONTENT_QC_SAMPLER_ERROR
    evidence = failed.probe_evidence
    assert evidence is not None
    assert evidence["qc_passed"] is False
    content_qc = evidence["content_qc"]
    assert isinstance(content_qc, dict)
    assert "RuntimeError: ffmpeg timeout" in str(content_qc["sampler_error"])
    # 抽样失败不静默放行：进入既有重提链。
    assert "auto_resubmit:qc_failed" in failed.retry_evidence
    assert replacement_for(repository, source.owner_id, failed) is not None


def test_qc_pass_records_content_qc_evidence() -> None:
    source, repository, engine, _assets, _compiler, service = setup(sampler=FakeSampler())
    intent = service.trigger(source.owner_id, source.group_id, "content-qc-pass")

    archived = drive_to_qc(source, engine, service, intent.attempt_id)

    assert archived.archive_stage is ArchiveStage.COMPLETE
    assert archived.stage is AttemptStage.ARCHIVED
    evidence = archived.probe_evidence
    assert evidence is not None
    assert evidence["qc_passed"] is True
    content_qc = evidence["content_qc"]
    assert isinstance(content_qc, dict)
    assert content_qc["algorithm"] == DEFAULT_THRESHOLDS.algorithm
    assert content_qc["findings"] == []
    assert repository.is_current(source.group_id, intent.attempt_id)


def test_unconfigured_sampler_is_recorded_not_silent() -> None:
    source, _repository, engine, _assets, _compiler, service = setup(sampler=None)
    intent = service.trigger(source.owner_id, source.group_id, "no-sampler")

    archived = drive_to_qc(source, engine, service, intent.attempt_id)

    assert archived.archive_stage is ArchiveStage.COMPLETE
    evidence = archived.probe_evidence
    assert evidence is not None and evidence["qc_passed"] is True
    content_qc = evidence["content_qc"]
    assert isinstance(content_qc, dict)
    assert content_qc["status"] == "sampler_not_configured"


def test_ladder_seed_derivation_is_deterministic_and_valid() -> None:
    superseded = PersistentAttempt(
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "k", "a" * 64, "b" * 64, {}
    )

    # int 基：base+i，确定性且过域校验。
    assert _ladder_seed(100, 2, superseded) == 102
    assert _ladder_seed(100, 6, superseded) == 106
    assert _ladder_seed(2**53 - 1, 2, superseded) == 1  # JSON 安全整数域内回绕
    # str 基：f"{base}#qc{i}" 标签哈希映射到合法字符串大整数域，确定性。
    str_seed_a = _ladder_seed("9007199254740993", 3, superseded)
    str_seed_b = _ladder_seed("9007199254740993", 3, superseded)
    assert str_seed_a == str_seed_b
    assert isinstance(str_seed_a, str) and str_seed_a != "9007199254740993"
    # None 基：由 group/attempt 身份派生正整数，确定性。
    none_seed_a = _ladder_seed(None, 4, superseded)
    none_seed_b = _ladder_seed(None, 4, superseded)
    assert none_seed_a == none_seed_b
    assert isinstance(none_seed_a, int) and none_seed_a >= 0
    other = replace(superseded, attempt_id=uuid4())
    assert _ladder_seed(None, 4, other) != none_seed_a
    for base in (100, "9007199254740993", None):
        for index in range(2, 7):
            _validate_seed(_ladder_seed(base, index, superseded))


def test_ladder_recompile_steps_and_strategy_mutation() -> None:
    compiler = RecordingCompiler()
    sampler = FakeSampler(black_frames())
    source, repository, engine, _assets, compiler, service = setup(
        sampler=sampler, compiler=compiler
    )
    attempt = service.trigger(source.owner_id, source.group_id, "ladder")
    assert len(compiler.calls) == 1  # 触发编译
    assert compiler.calls[0]["repair_reason"] is None

    chain = [attempt]
    for _ in range(5):
        failed = drive_to_qc(source, engine, service, chain[-1].attempt_id)
        assert failed.archive_stage is ArchiveStage.QC_FAILED
        assert failed.error_code == VIDEO_CONTENT_BLACK_REGION
        chain.append(replacement_for(repository, source.owner_id, failed))

    def seed_of(attempt: PersistentAttempt) -> object:
        return attempt.request["parameters"]["seed"]  # type: ignore[index]

    # seed 沿链复合（base 为被取代尝试的当前 seed）：100→102→105→109→114→120。
    # 编译调用累计：触发 1 次 + i=3/4/6 各重编译 1 次 = 4 次。
    assert len(compiler.calls) == 4
    call_keys = [
        call["input"].idempotency_key
        for call in compiler.calls
        if isinstance(call["input"], VideoPromptCompilerInput)
    ]
    assert call_keys == [f"video-prompt:{chain[index].attempt_id}" for index in (0, 2, 3, 5)]

    # i=2：仅换 seed，复用原提示词。
    assert seed_of(chain[1]) == 102
    assert chain[1].request["prompt"] == chain[0].request["prompt"]
    assert (chain[1].input_snapshot or {})["seed"] == 102

    # i=3：换 seed + 沿用原策略带 QC 原因重编译；快照 video_prompt 同步更新。
    assert seed_of(chain[2]) == 105
    call3 = compiler.calls[1]
    value3 = call3["input"]
    assert isinstance(value3, VideoPromptCompilerInput)
    assert value3.strategy is VideoPromptStrategy.TUBEFLOW_ENHANCED_V1
    reason3 = call3["repair_reason"]
    assert isinstance(reason3, str)
    assert "0.0s-3.0s" in reason3 and "columns 4-7" in reason3 and "black" in reason3
    assert call3["max_repairs"] == 2
    expected_prompt3 = f"compiled video prompt for video-prompt:{chain[2].attempt_id}"
    assert chain[2].request["prompt"] == expected_prompt3
    prompt_block3 = (chain[2].input_snapshot or {})["video_prompt"]
    assert isinstance(prompt_block3, dict)
    assert prompt_block3["strategy"] == "tubeflow_enhanced_v1"
    assert prompt_block3["text"] == chain[2].request["prompt"]
    assert "auto_ladder:recompiled:tubeflow_enhanced_v1" in chain[2].retry_evidence
    assert "auto_ladder:repair_reason:content_qc" in chain[2].retry_evidence

    # i=4：换 seed + kinestack_v6_port 策略重编译（不带 QC 原因）。
    assert seed_of(chain[3]) == 109
    call4 = compiler.calls[2]
    value4 = call4["input"]
    assert isinstance(value4, VideoPromptCompilerInput)
    assert value4.strategy is VideoPromptStrategy.KINESTACK_V6_PORT
    assert call4["repair_reason"] is None
    prompt_block4 = (chain[3].input_snapshot or {})["video_prompt"]
    assert isinstance(prompt_block4, dict)
    assert prompt_block4["strategy"] == "kinestack_v6_port"

    # i=5：仅换 seed（以 i=4 重编译结果为基准提示词）。
    assert seed_of(chain[4]) == 114
    assert chain[4].request["prompt"] == chain[3].request["prompt"]
    assert "auto_ladder:seed_only" in chain[4].retry_evidence

    # i=6：换 seed + 带 QC 原因重编译（沿用 i=4 的策略作为原策略）。
    assert seed_of(chain[5]) == 120
    call6 = compiler.calls[3]
    value6 = call6["input"]
    assert isinstance(value6, VideoPromptCompilerInput)
    assert value6.strategy is VideoPromptStrategy.KINESTACK_V6_PORT
    reason6 = call6["repair_reason"]
    assert isinstance(reason6, str) and "black" in reason6

    # 哈希自洽：每个替代的持久化哈希与其快照/请求一致。
    for item in chain[1:]:
        assert item.generation_sha256 == canonical_sha256(item.input_snapshot or {})
        assert item.request_sha256 == canonical_sha256(item.request)
        assert item.superseded_attempt_id is not None

    # 确定性：同一被取代尝试重建两次，seed/策略完全一致。
    again = service._build_auto_replacement(chain[2], next_attempt_index=4)
    assert again.request["parameters"]["seed"] == chain[3].request["parameters"]["seed"]  # type: ignore[index]
    assert (again.input_snapshot or {})["video_prompt"]["strategy"] == "kinestack_v6_port"  # type: ignore[index]


def test_ladder_recompile_failure_degrades_to_seed_only() -> None:
    compiler = RecordingCompiler()
    source, repository, engine, _assets, compiler, service = setup(
        sampler=FakeSampler(black_frames()), compiler=compiler
    )
    attempt = service.trigger(source.owner_id, source.group_id, "ladder-degraded")
    first = drive_to_qc(source, engine, service, attempt.attempt_id)
    second = replacement_for(repository, source.owner_id, first)  # i=2
    compiler.fail_with = RuntimeError("provider down")

    failed2 = drive_to_qc(source, engine, service, second.attempt_id)
    third = replacement_for(repository, source.owner_id, failed2)  # i=3 编译失败退化

    assert third.request["parameters"]["seed"] == 105  # type: ignore[index]  # 102+3 沿链复合
    assert third.request["prompt"] == second.request["prompt"]
    assert "auto_ladder:attempt_3" in third.retry_evidence
    assert "auto_ladder:degraded:compile_failed:RuntimeError" in third.retry_evidence
    assert "auto_ladder:seed_only" in third.retry_evidence


def test_old_snapshot_without_compiler_input_degrades_to_seed_only() -> None:
    source, repository, engine, _assets, compiler, service = setup(
        sampler=FakeSampler(black_frames())
    )
    attempt = service.trigger(source.owner_id, source.group_id, "old-snapshot")
    # 模拟旧快照：移除 compiler_input 块并重算哈希（保持持久化自洽）。
    snapshot = dict(attempt.input_snapshot or {})
    snapshot.pop("compiler_input", None)
    repository.attempts[attempt.attempt_id] = replace(
        attempt, input_snapshot=snapshot, generation_sha256=canonical_sha256(snapshot)
    )
    first = drive_to_qc(source, engine, service, attempt.attempt_id)  # i=2
    second = replacement_for(repository, source.owner_id, first)

    failed2 = drive_to_qc(source, engine, service, second.attempt_id)
    third = replacement_for(repository, source.owner_id, failed2)  # i=3 缺块退化

    assert third.request["parameters"]["seed"] == 105  # type: ignore[index]  # 102+3 沿链复合
    assert third.request["prompt"] == second.request["prompt"]
    assert len(compiler.calls) == 1  # 只发生触发编译
    assert "auto_ladder:degraded:video_compiler_input_snapshot_missing" in third.retry_evidence
    assert "auto_ladder:seed_only" in third.retry_evidence


def test_sixth_content_defect_delivers_with_defect_and_becomes_current() -> None:
    compiler = RecordingCompiler()
    source, repository, engine, _assets, compiler, service = setup(
        sampler=FakeSampler(black_frames()), compiler=compiler
    )
    attempt = service.trigger(source.owner_id, source.group_id, "defect-delivery")
    chain = [attempt]
    for _ in range(5):
        failed = drive_to_qc(source, engine, service, chain[-1].attempt_id)
        chain.append(replacement_for(repository, source.owner_id, failed))

    # 第 6 次提交仍内容缺陷：预算耗尽 → 带病交付（单次推进内完成归档确认与收尾）。
    delivered = drive_to_qc(source, engine, service, chain[-1].attempt_id)

    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    assert submit_calls(engine) == 6
    assert repository.find_attempt(source.owner_id, f"auto-resubmit:{chain[-1].attempt_id}") is None
    assert delivered.stage is AttemptStage.ARCHIVED
    assert delivered.archive_stage is ArchiveStage.COMPLETE
    assert delivered.archived_asset is not None
    # 持久缺陷标记：probe_evidence 保留 qc_passed False 与 content_qc 块，血缘带交付标记。
    assert f"deliver_with_defect:{VIDEO_CONTENT_BLACK_REGION}" in delivered.retry_evidence
    assert f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}" not in delivered.retry_evidence
    evidence = delivered.probe_evidence
    assert evidence is not None
    assert evidence["qc_passed"] is False
    assert evidence["error_code"] == VIDEO_CONTENT_BLACK_REGION
    content_qc = evidence["content_qc"]
    assert isinstance(content_qc, dict) and content_qc["findings"]
    # 成为当前视频（进入合成的组级事实）。
    current = repository.get_current(source.owner_id, source.group_id)
    assert current is not None and current.attempt_id == delivered.attempt_id
    # 归档确认复用既有机制：同一幂等键只派发一次。
    assert f"{delivered.attempt_id}:archive-confirm" in engine.archive_confirmation_effects
    delivered_events = [
        event for event in repository.provider_events if event.event_type == "QC_DEFECT_DELIVERED"
    ]
    assert len(delivered_events) == 1
    assert delivered_events[0].error_code == VIDEO_CONTENT_BLACK_REGION

    # 重放不重复动作：advance/archive 均不再产生事件、确认派发或新提交。
    events_before = len(repository.provider_events)
    confirms_before = sum(call == "archive" for _, call in engine.calls)
    replayed = service.advance(source.owner_id, delivered.attempt_id)
    assert replayed.stage is AttemptStage.ARCHIVED
    replayed_archive = service.archive(source.owner_id, delivered.attempt_id)
    assert replayed_archive.archive_stage is ArchiveStage.COMPLETE
    assert len(repository.provider_events) == events_before
    assert sum(call == "archive" for _, call in engine.calls) == confirms_before
    assert submit_calls(engine) == 6
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6


def test_defect_delivery_confirm_loss_recovers_via_reconcile() -> None:
    source, repository, engine, _assets, _compiler, service = setup(
        sampler=FakeSampler(black_frames())
    )
    attempt = service.trigger(source.owner_id, source.group_id, "defect-confirm-loss")
    chain = [attempt]
    for _ in range(5):
        failed = drive_to_qc(source, engine, service, chain[-1].attempt_id)
        chain.append(replacement_for(repository, source.owner_id, failed))
    # 第 6 次归档 QC 后先停在 STORED：把归档阶段拨回 QC_FAILED 之前的状态不可行，
    # 改为在交付确认时丢失一次回执。
    submitted = service.advance(source.owner_id, chain[-1].attempt_id)
    assert submitted.stage is AttemptStage.SUBMITTED
    engine.inspect_status = "SUCCEEDED"
    succeeded = service.advance(source.owner_id, chain[-1].attempt_id)
    assert succeeded.stage is AttemptStage.SUCCEEDED
    engine.lose_archive_response_once = True

    pending = service.advance(source.owner_id, chain[-1].attempt_id)

    assert pending.archive_stage is ArchiveStage.CONFIRMING
    assert f"deliver_with_defect:{VIDEO_CONTENT_BLACK_REGION}" in pending.retry_evidence
    # 既有 CONFIRMING 对账路径恢复完成；确认语义幂等（同一操作键）。
    recovered = service.advance(source.owner_id, chain[-1].attempt_id)
    assert recovered.archive_stage is ArchiveStage.COMPLETE
    assert recovered.stage is AttemptStage.ARCHIVED
    assert engine.archive_confirmation_effects == {f"{chain[-1].attempt_id}:archive-confirm"}
    delivered_events = [
        event for event in repository.provider_events if event.event_type == "QC_DEFECT_DELIVERED"
    ]
    assert len(delivered_events) == 1
    current = repository.get_current(source.owner_id, source.group_id)
    assert current is not None and current.attempt_id == chain[-1].attempt_id


def test_spec_failure_exhaustion_parks_without_defect_delivery() -> None:
    bad = replace(Probe().evidence, audio_tracks=0)
    source, repository, engine, _assets, _compiler, service = setup(
        probe=Probe(bad), sampler=FakeSampler(healthy_frames())
    )
    attempt = service.trigger(source.owner_id, source.group_id, "spec-exhaustion")
    for _round in range(6):
        failed = drive_to_qc(source, engine, service, attempt.attempt_id)
        assert failed.archive_stage is ArchiveStage.QC_FAILED
        assert failed.error_code == "VideoRuleViolation"
        replacement = repository.find_attempt(
            source.owner_id, f"auto-resubmit:{attempt.attempt_id}"
        )
        if replacement is None:
            break
        attempt = replacement

    assert repository.count_group_attempts(source.owner_id, source.group_id) == 6
    assert submit_calls(engine) == 6
    parked = repository.get_attempt(attempt.attempt_id)
    assert parked is not None
    assert parked.stage is AttemptStage.SUCCEEDED
    assert parked.archive_stage is ArchiveStage.QC_FAILED
    assert f"auto_resubmit:{SUBMISSION_BUDGET_EXHAUSTED}" in parked.retry_evidence
    assert not any(
        marker.startswith("deliver_with_defect:") for marker in parked.retry_evidence
    )
    assert repository.get_current(source.owner_id, source.group_id) is None
    assert not engine.archive_confirmation_effects
    assert not any(
        event.event_type == "QC_DEFECT_DELIVERED" for event in repository.provider_events
    )


def test_manual_content_qc_failure_neither_resubmits_nor_delivers() -> None:
    source, repository, engine, _assets, _compiler, service = setup(
        production_mode="MANUAL", sampler=FakeSampler(black_frames())
    )
    intent = service.trigger(source.owner_id, source.group_id, "manual-content-qc")

    failed = drive_to_qc(source, engine, service, intent.attempt_id)

    assert failed.stage is AttemptStage.SUCCEEDED
    assert failed.archive_stage is ArchiveStage.QC_FAILED
    assert failed.error_code == VIDEO_CONTENT_BLACK_REGION
    evidence = failed.probe_evidence
    assert evidence is not None and evidence["qc_passed"] is False
    assert isinstance(evidence["content_qc"], dict)
    assert repository.count_group_attempts(source.owner_id, source.group_id) == 1
    assert repository.scheduled_delays == {}
    assert submit_calls(engine) == 1
    assert not any(
        marker.startswith(("auto_resubmit:", "deliver_with_defect:", "auto_ladder:"))
        for marker in failed.retry_evidence
    )
    assert repository.get_current(source.owner_id, source.group_id) is None


def test_auto_pipeline_facts_treat_defect_delivered_group_as_complete() -> None:
    group_id = uuid4()
    delivered = VideoGroupFact(
        group_id=group_id,
        group_sequence=1,
        has_current_video=True,
        latest_stage="ARCHIVED",
        latest_archive_stage="COMPLETE",
        latest_error_code=None,
        latest_budget_round=1,
    )
    facts = AutoPipelineFacts(
        production_mode="AUTO",
        budget_round=1,
        narration_ready=True,
        narration_blocking=None,
        oversized_groups=(),
        director_preplan_done=True,
        director_latest_run_id=None,
        director_latest_status=None,
        director_latest_failure_code=None,
        materialization_ready=True,
        storyboards_complete=True,
        storyboard_groups=(),
        videos_complete=True,
        video_groups=(delivered,),
        composition_current_exists=False,
        composition_in_flight=False,
        composition_failed_code=None,
    )
    # 带病交付的组不再计入终态失败（不触发暂停），也不再需要提交。
    assert delivered.terminal_failure_code(1) is None
    assert facts.video_terminal_failures() == ()
    assert facts.video_needs_submission() == ()
    assert not delivered.in_flight
    assert facts.videos_complete

    # 交付进行中（CONFIRMING）的中间态也不算终态失败。
    confirming = VideoGroupFact(
        group_id=group_id,
        group_sequence=1,
        has_current_video=False,
        latest_stage="SUCCEEDED",
        latest_archive_stage="CONFIRMING",
        latest_error_code="EngineUncertain",
        latest_budget_round=1,
    )
    assert confirming.terminal_failure_code(1) is None
    assert confirming.in_flight

    # 对照：技术规格失败耗尽后的 park 态维持既有暂停语义。
    parked = VideoGroupFact(
        group_id=group_id,
        group_sequence=1,
        has_current_video=False,
        latest_stage="SUCCEEDED",
        latest_archive_stage="QC_FAILED",
        latest_error_code="VideoRuleViolation",
        latest_budget_round=1,
    )
    facts_parked = replace(facts, video_groups=(parked,), videos_complete=False)
    assert parked.terminal_failure_code(1) == "VideoRuleViolation"
    assert facts_parked.video_terminal_failures() == (parked,)


def test_auto_resubmit_policy_default_budget_is_six() -> None:
    assert AutoResubmitPolicy().max_submissions == 6


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        # 2026-11-03（USER_DECISIONS §43）：配置为 v3 输出合同时，i=4 使用配置策略。
        (
            VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION,
            VideoPromptStrategy.TUBEFLOW_STORYBOARD_V4_NATURAL_MOTION,
        ),
        (VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3, VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3),
        # 其他配置保持原 kinestack_v6_port 行为。
        (VideoPromptStrategy.TUBEFLOW_ENHANCED_V1, VideoPromptStrategy.KINESTACK_V6_PORT),
        (VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2, VideoPromptStrategy.KINESTACK_V6_PORT),
    ],
)
def test_ladder_step_four_uses_v3_contract_default(
    default: VideoPromptStrategy, expected: VideoPromptStrategy
) -> None:
    superseded = cast(
        PersistentAttempt,
        SimpleNamespace(input_snapshot={"video_prompt": {"strategy": "tubeflow_storyboard_v3"}}),
    )
    assert _ladder_strategy(superseded, 4, default) is expected
    # 其他梯级仍沿用被取代尝试的原策略。
    assert _ladder_strategy(superseded, 3, default) is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3
