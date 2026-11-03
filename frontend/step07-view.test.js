import test from "node:test";
import assert from "node:assert/strict";

import {
  actionLabel,
  actionListText,
  algorithmVersionLabel,
  blockingReasonLabel,
  conflictShotId,
  conflictSummary,
  conflictText,
  DIRECTOR_GROUP_FILTERS,
  directorViewFromHash,
  groupCandidateAnnotation,
  groupCandidateTone,
  groupCardMatchesFilter,
  groupCardModel,
  hashForDirectorView,
  materializationStatusLabel,
  narrationDurationIsSuspicious,
  parseGroupSequenceInput,
  preplanRunCalmSummary,
  preplanRunIsCalm,
  reviewStateLabel,
  videoGroupTabTone,
} from "./step07-view.js";

test("action labels translate known codes and keep unknown codes readable", () => {
  assert.equal(actionLabel("GENERATE_STORYBOARD"), "生成故事板");
  assert.equal(actionLabel("REGENERATE_DIRECTOR_PREPLAN"), "重新处理前置导演");
  assert.equal(actionLabel("SOME_FUTURE_ACTION"), "SOME_FUTURE_ACTION");
  assert.equal(actionListText([]), "无");
  assert.equal(actionListText(null), "无");
  assert.equal(actionListText(["VIEW_MEDIA", "SELECT_CURRENT"]), "预览媒体 / 切换为当前版本");
});

test("blocking reason labels cover known codes and fall back to null", () => {
  assert.equal(blockingReasonLabel("final_director_review_required"), "待确认最终导演");
  assert.equal(blockingReasonLabel("previous_stage_incomplete"), "前置阶段未完成");
  assert.equal(blockingReasonLabel("video_attempt_running"), "视频任务进行中");
  assert.equal(blockingReasonLabel("director_preplan_remote_state_unknown"), "前置导演远端状态未知");
  assert.equal(blockingReasonLabel("composition_has_blocked_groups"), "存在被阻止的分组");
  assert.equal(blockingReasonLabel("storyboard_attempt_failed"), "故事板任务失败");
  assert.equal(blockingReasonLabel("project_purging"), "项目正在删除");
  assert.equal(blockingReasonLabel("brand_new_reason"), null);
  assert.equal(blockingReasonLabel(""), null);
  assert.equal(blockingReasonLabel(null), null);
});

test("review state labels translate review vocabulary", () => {
  assert.equal(reviewStateLabel("REQUIRED"), "待人工修正");
  assert.equal(reviewStateLabel("REVIEW_REQUIRED"), "待人工修正");
  assert.equal(reviewStateLabel("NOT_REQUIRED"), "无需变更");
  assert.equal(reviewStateLabel("SOMETHING_ELSE"), "SOMETHING_ELSE");
  assert.equal(reviewStateLabel(""), "");
  assert.equal(reviewStateLabel(null), "");
});

test("materialization status prefers review wording over raw status", () => {
  assert.equal(materializationStatusLabel({ status: "REVIEW_REQUIRED" }), "待人工修正");
  assert.equal(materializationStatusLabel({ status: "SUCCEEDED" }), "SUCCEEDED");
  assert.equal(materializationStatusLabel({ public_status: "NOT_REQUIRED" }), "无需变更");
  assert.equal(materializationStatusLabel(null), "未知状态");
});

test("algorithm version label rewrites lineage-remap versions", () => {
  assert.equal(algorithmVersionLabel("controlled-semantic-lineage-remap-v3"), "确定性重映射 v3");
  assert.equal(algorithmVersionLabel("lineage-remap-v9"), "确定性重映射 v9");
  assert.equal(algorithmVersionLabel("other-algorithm-1"), "other-algorithm-1");
  assert.equal(algorithmVersionLabel(""), "—");
  assert.equal(algorithmVersionLabel(null), "—");
});

test("preplanRunIsCalm only accepts fully succeeded quiet runs", () => {
  const calm = {
    status: "SUCCEEDED",
    total_units: 51,
    completed_units: 51,
    inflight_units: 0,
    retry_pending_units: 0,
    unsent_units: 0,
    failure_code: null,
    failed_unit_ordinal: null,
  };
  assert.equal(preplanRunIsCalm(calm), true);
  assert.equal(preplanRunIsCalm({ ...calm, status: "RUNNING" }), false);
  assert.equal(preplanRunIsCalm({ ...calm, completed_units: 50 }), false);
  assert.equal(preplanRunIsCalm({ ...calm, inflight_units: 1 }), false);
  assert.equal(preplanRunIsCalm({ ...calm, retry_pending_units: 1 }), false);
  assert.equal(preplanRunIsCalm({ ...calm, unsent_units: 1 }), false);
  assert.equal(preplanRunIsCalm({ ...calm, failure_code: "x" }), false);
  assert.equal(preplanRunIsCalm({ ...calm, failed_unit_ordinal: 3 }), false);
  assert.equal(preplanRunIsCalm({ ...calm, total_units: 0, completed_units: 0 }), false);
  assert.equal(preplanRunIsCalm(null), false);
  assert.equal(preplanRunCalmSummary(calm), "前置导演 51/51 已完成 · 无在途、无失败批次");
});

test("group candidate tone derives only from loaded facts", () => {
  const readyCandidate = { review_state: "NOT_REQUIRED", conflicts: [] };
  assert.equal(groupCandidateTone({ video_blocked: true }, readyCandidate), "blocked");
  assert.equal(groupCandidateTone({}, null), "missing");
  assert.equal(groupCandidateTone({}, { review_state: "REQUIRED" }), "review");
  assert.equal(groupCandidateTone({}, { review_state: "REVIEW_REQUIRED" }), "review");
  assert.equal(groupCandidateTone({}, { review_state: "NOT_REQUIRED", conflicts: [{}] }), "review");
  assert.equal(groupCandidateTone({}, readyCandidate), "ready");
  assert.equal(groupCandidateTone({}, { review_state: "PENDING" }), "missing");
  assert.deepEqual(
    groupCandidateAnnotation({}, readyCandidate),
    { tone: "ready", label: "导演候选就绪" },
  );
});

test("suspicious narration duration flags sub-second ready audio", () => {
  assert.equal(narrationDurationIsSuspicious(0), false);
  assert.equal(narrationDurationIsSuspicious(999), true);
  assert.equal(narrationDurationIsSuspicious(1000), false);
  assert.equal(narrationDurationIsSuspicious(1400), false);
  assert.equal(narrationDurationIsSuspicious("800"), true);
  assert.equal(narrationDurationIsSuspicious("abc"), false);
  assert.equal(narrationDurationIsSuspicious(null), false);
});

test("conflict display keeps human summary and full technical detail", () => {
  assert.equal(conflictText("raw text"), "raw text");
  assert.equal(conflictSummary("raw text"), "raw text");
  const conflict = {
    code: "source_preplan_quality_conflict",
    message: "句子与分镜关联不完整",
    shot_id: "shot-1",
    sentence_ids: ["s1", "s2"],
  };
  assert.equal(conflictSummary(conflict), "继承自前置方案的质量冲突");
  assert.match(conflictText(conflict), /source_preplan_quality_conflict/);
  assert.match(conflictText(conflict), /shot-1/);
  assert.match(conflictText(conflict), /句子 s1、s2/);
  assert.equal(conflictShotId(conflict), "shot-1");
  assert.equal(conflictShotId({ shot_ids: ["only-one"] }), "only-one");
  assert.equal(conflictShotId({ shot_ids: ["a", "b"] }), "a");
  assert.equal(conflictShotId(null), "");
  assert.equal(conflictSummary({ code: "adjacent_meaningful_change_insufficient" }), "相邻分镜的有效叙事或构图变化不足");
  assert.equal(conflictSummary({ code: "group_shot_limit_exceeded" }), "分镜数量超过每组上限");
  assert.equal(conflictSummary({ code: "unknown_future_code" }), "unknown_future_code");
  assert.equal(conflictSummary(null), "后端返回未分类质量冲突");
  assert.equal(conflictText({}), "后端返回未分类质量冲突");
});

test("groupCardModel derives card facts from loaded data only", () => {
  const group = {
    group_id: "group-1",
    sequence: 2,
    start_ms: 4000,
    end_ms: 9400,
    target_duration_ms: 5400,
    video_blocked: false,
  };
  const sentences = [
    { sentence_id: "s1", sequence: 3, text: "第三句", normalized_qc: { measured_duration_ms: 800 } },
    { sentence: { sequence: 4, text: "第四句" }, sentence_id: "s2" },
  ];
  const timeline = [{ sentence_sequence: 4, start_ms: 8000, end_ms: 9400 }];
  const model = groupCardModel({
    group,
    index: 1,
    candidate: { review_state: "NOT_REQUIRED", conflicts: [], shots: [{}, {}, {}] },
    overviewItem: { storyboard_status: "SUCCEEDED", current_generation_id: "gen-9" },
    sentences,
    timeline,
  });

  assert.equal(model.groupId, "group-1");
  assert.equal(model.sequence, 2);
  assert.equal(model.tone, "ready");
  assert.equal(model.toneLabel, "导演候选就绪");
  assert.equal(model.durationMs, 5400);
  assert.equal(model.shotCount, 3);
  assert.equal(model.generationId, "gen-9");
  assert.equal(model.storyboardStatus, "SUCCEEDED");
  assert.deepEqual(model.sentences, [
    { sequence: 3, text: "第三句", durationMs: 800, suspicious: true },
    { sequence: 4, text: "第四句", durationMs: 1400, suspicious: false },
  ]);
});

test("groupCardModel handles missing candidate overview and timeline", () => {
  const model = groupCardModel({ group: { group_id: "g", video_blocked: true }, index: 0 });
  assert.equal(model.sequence, 1);
  assert.equal(model.tone, "blocked");
  assert.equal(model.durationMs, 0);
  assert.equal(model.shotCount, null);
  assert.equal(model.generationId, null);
  assert.deepEqual(model.sentences, []);

  const empty = groupCardModel();
  assert.equal(empty.groupId, "");
  assert.equal(empty.tone, "missing");

  const fallbackMs = groupCardModel({ group: { start_ms: 1000, end_ms: 2600 }, index: 0 });
  assert.equal(fallbackMs.durationMs, 1600);
});

test("groupCardMatchesFilter covers every filter key and safe fallbacks", () => {
  const base = groupCardModel({
    group: { group_id: "g1" },
    candidate: { review_state: "NOT_REQUIRED", conflicts: [] },
    overviewItem: { current_generation_id: "gen-1" },
  });
  const noBoard = { ...base, generationId: null };
  assert.equal(groupCardMatchesFilter(base, "all"), true);
  assert.equal(groupCardMatchesFilter(base, "ready"), true);
  assert.equal(groupCardMatchesFilter(base, "review"), false);
  assert.equal(groupCardMatchesFilter(base, "blocked"), false);
  assert.equal(groupCardMatchesFilter(base, "missing"), false);
  assert.equal(groupCardMatchesFilter(base, "no-storyboard"), false);
  assert.equal(groupCardMatchesFilter(noBoard, "no-storyboard"), true);
  assert.equal(groupCardMatchesFilter(base, "some-future-filter"), true);
  assert.equal(groupCardMatchesFilter(null, "all"), false);
  assert.deepEqual(DIRECTOR_GROUP_FILTERS.map((item) => item.key), [
    "all", "ready", "review", "blocked", "missing", "no-storyboard",
  ]);
});

test("parseGroupSequenceInput only accepts in-range integers", () => {
  assert.equal(parseGroupSequenceInput("3", 51), 3);
  assert.equal(parseGroupSequenceInput(" 2 ", 2), 2);
  assert.equal(parseGroupSequenceInput(1, 7), 1);
  assert.equal(parseGroupSequenceInput("0", 5), null);
  assert.equal(parseGroupSequenceInput("-1", 5), null);
  assert.equal(parseGroupSequenceInput("6", 5), null);
  assert.equal(parseGroupSequenceInput("abc", 5), null);
  assert.equal(parseGroupSequenceInput("2.5", 5), null);
  assert.equal(parseGroupSequenceInput("", 5), null);
  assert.equal(parseGroupSequenceInput(null, 5), null);
  assert.equal(parseGroupSequenceInput("1", 0), null);
  assert.equal(parseGroupSequenceInput("1", -3), null);
});

test("directorViewFromHash parses list detail and other stage hashes", () => {
  assert.deepEqual(
    directorViewFromHash("#/project/p-1/director_storyboard"),
    { projectId: "p-1", stage: "director_storyboard", groupSequence: null },
  );
  assert.deepEqual(
    directorViewFromHash("#/project/p-1/director_storyboard/group/12"),
    { projectId: "p-1", stage: "director_storyboard", groupSequence: 12 },
  );
  assert.deepEqual(
    directorViewFromHash("#/project/p-1/script"),
    { projectId: "p-1", stage: "script", groupSequence: null },
  );
  assert.equal(directorViewFromHash(""), null);
  assert.equal(directorViewFromHash(null), null);
  assert.equal(directorViewFromHash("#/project/p-1"), null);
  assert.equal(directorViewFromHash("#/project/"), null);
  assert.equal(directorViewFromHash("#/other/p-1/script"), null);
  assert.equal(directorViewFromHash("#/project/p-1/director_storyboard/group/0"), null);
  assert.equal(directorViewFromHash("#/project/p-1/director_storyboard/group/abc"), null);
  assert.equal(directorViewFromHash("#/project/p-1/script/group/2"), null);
});

test("hashForDirectorView writes list hash unless the group sequence is valid", () => {
  assert.equal(hashForDirectorView("p-1", 4), "#/project/p-1/director_storyboard/group/4");
  assert.equal(hashForDirectorView("p-1", "2"), "#/project/p-1/director_storyboard/group/2");
  assert.equal(hashForDirectorView("p-1"), "#/project/p-1/director_storyboard");
  assert.equal(hashForDirectorView("p-1", null), "#/project/p-1/director_storyboard");
  assert.equal(hashForDirectorView("p-1", 0), "#/project/p-1/director_storyboard");
  assert.equal(hashForDirectorView("p-1", -2), "#/project/p-1/director_storyboard");
  assert.equal(hashForDirectorView("p-1", "abc"), "#/project/p-1/director_storyboard");
});

test("video group tab tone follows group video progress facts", () => {
  assert.equal(videoGroupTabTone({ video_blocked: true, has_current_video: true }), "blocked");
  assert.equal(videoGroupTabTone({ has_current_video: true }), "ready");
  assert.equal(videoGroupTabTone({ latest_attempt_stage: "RUNNING" }), "review");
  assert.equal(videoGroupTabTone({ latest_attempt_stage: "SUBMITTED" }), "review");
  assert.equal(videoGroupTabTone({ latest_attempt_stage: "FAILED" }), "missing");
  assert.equal(videoGroupTabTone({}), "missing");
  assert.equal(videoGroupTabTone(null), "missing");
});
