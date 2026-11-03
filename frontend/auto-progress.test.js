import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ApiClient } from "./api-client.js";
import {
  autoPauseReason,
  autoPauseReasonLabel,
  autoPipelineBusy,
  autoPipelineState,
  autoPipelineStateLabel,
  autoPhaseLabel,
  autoResumeAllowed,
  autoStageRows,
  autoStageTiming,
  formatElapsedMs,
} from "./auto-progress.js";

const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8").replaceAll("\r\n", "\n");

function productionFunction(name) {
  const start = appSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing production function ${name}`);
  const end = appSource.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated production function ${name}`);
  return appSource.slice(start, end + 2);
}

test("auto pipeline resume follows the identity contract route", async () => {
  let seen;
  const api = new ApiClient(async (url, init = {}) => {
    seen = { url, init };
    return new Response(JSON.stringify({ state: "RUNNING" }), {
      status: 202,
      headers: { "content-type": "application/json" },
    });
  });
  await api.autoPipelineResume("p1");
  assert.equal(seen.url, "/identity/projects/p1/auto-pipeline/resume");
  assert.equal(seen.init.method, "POST");
});

test("auto pipeline state parsing is strict and never guesses semantics", () => {
  assert.equal(autoPipelineState(null), null);
  assert.equal(autoPipelineState({}), null);
  assert.equal(autoPipelineState({ auto_pipeline: null }), null);
  assert.equal(autoPipelineState({ auto_pipeline: { state: "RUNNING" } }), "RUNNING");
  assert.equal(autoPipelineState({ auto_pipeline: { state: "PAUSED" } }), "PAUSED");
  assert.equal(autoPipelineState({ auto_pipeline: { state: "COMPLETED" } }), "COMPLETED");
  assert.equal(autoPipelineState({ auto_pipeline: { state: "UNKNOWN_FUTURE" } }), null);
  assert.equal(autoPipelineStateLabel("RUNNING"), "自动生产进行中");
  assert.equal(autoPipelineStateLabel("PAUSED"), "自动生产已暂停");
  assert.equal(autoPipelineStateLabel("COMPLETED"), "视频生产完成");
  assert.equal(autoPipelineStateLabel(null), "等待提交");
});

test("phase labels map known phases and fall back to the backend raw code", () => {
  assert.equal(autoPhaseLabel("narration"), "真实配音");
  assert.equal(autoPhaseLabel("director_preplan"), "前置导演");
  assert.equal(autoPhaseLabel("director_materialization"), "确定性重映射");
  assert.equal(autoPhaseLabel("storyboard_drafts"), "导演草稿播种");
  assert.equal(autoPhaseLabel("storyboard"), "故事板生成");
  assert.equal(autoPhaseLabel("video"), "组级视频生成");
  assert.equal(autoPhaseLabel("composition"), "合成");
  assert.equal(autoPhaseLabel("completed"), "完成");
  assert.equal(autoPhaseLabel("FUTURE_PHASE"), "FUTURE_PHASE");
  assert.equal(autoPhaseLabel(null), "—");
});

test("pause reason maps stable codes, carries backend message and falls back to raw code", () => {
  const paused = {
    blocking_reason: "storyboard_failed:submission_budget_exhausted",
    auto_pipeline: {
      state: "PAUSED",
      reason_code: "storyboard_failed:submission_budget_exhausted",
      phase: "storyboard",
      message: "故事板连续失败",
    },
  };
  assert.equal(autoPauseReason(paused).label, "故事板生成失败：自动重试/重提预算已用尽（每组 6 次）");
  assert.equal(autoPauseReason(paused).message, "故事板连续失败");
  assert.equal(autoPauseReason(paused).code, "storyboard_failed:submission_budget_exhausted");
  // 固定码与子码前缀映射
  assert.equal(autoPauseReasonLabel("oversized_sentence"), "存在实测超过 15 秒的配音句");
  assert.equal(autoPauseReasonLabel("video_failed:intent_rejected"), "组级视频生成失败：生成意图被既定拒绝");
  assert.equal(
    autoPauseReasonLabel("storyboard_failed:failover_budget_exhausted"),
    "故事板生成失败：备用渠道与模型已按序尝试，仍未成功（每组 14 次、每模型最多 2 次）",
  );
  // 其他对象的既有 6 次预算文案不随故事板 16/2 规则改变。
  assert.equal(
    autoPauseReasonLabel("video_failed:submission_budget_exhausted"),
    "组级视频生成失败：自动重试/重提预算已用尽（每组 6 次）",
  );
  assert.equal(autoPauseReasonLabel("video_failed:future_subcode"), "组级视频生成失败：future_subcode");
  // reason_code 缺失时回退顶层 blocking_reason；两者皆未知时展示后端原文码。
  assert.equal(autoPauseReason({ blocking_reason: "videos_incomplete" }).code, "videos_incomplete");
  assert.equal(
    autoPauseReason({ auto_pipeline: { state: "PAUSED", reason_code: "future_code" } }).label,
    "future_code",
  );
  assert.equal(autoPauseReason(null).label, "未知原因");
  assert.equal(autoPauseReason(null).message, "");
});

test("composition pause surfaces the backend error code instead of an empty detail", () => {
  const paused = {
    auto_pipeline: {
      state: "PAUSED",
      reason_code: "composition_failed",
      phase: "composition",
      details: { error_code: "composition_exec_failed" },
    },
  };
  const reason = autoPauseReason(paused);
  assert.equal(reason.label, "合成失败（既定失败，未自动重试）");
  assert.equal(reason.message, "失败码：composition_exec_failed。");
  // 缺细节时不伪造文案，仍交由调用方兜底。
  assert.equal(autoPauseReason({ auto_pipeline: { reason_code: "composition_failed" } }).message, "");
});

test("pause reason composes director validation details and exposes group sequences", () => {
  const paused = {
    auto_pipeline: {
      state: "PAUSED",
      reason_code: "director_validation_failed",
      phase: "storyboard_drafts",
      details: { group_sequences: [3, 7, 21] },
    },
  };
  const reason = autoPauseReason(paused);
  assert.equal(reason.label, "导演方案校验未通过，需人工修正");
  assert.equal(reason.message, "导演方案校验未通过的分组：03、07、21（共 3 组）。");
  assert.deepEqual(reason.groupSequences, [3, 7, 21]);

  // 无 details 或组号为空时回落默认文案路径；非法组号被过滤。
  assert.equal(
    autoPauseReason({ auto_pipeline: { state: "PAUSED", reason_code: "director_validation_failed" } })
      .message,
    "",
  );
  assert.deepEqual(
    autoPauseReason({
      auto_pipeline: {
        state: "PAUSED",
        reason_code: "director_validation_failed",
        details: { group_sequences: [0, -1, 2.5, "4", 5] },
      },
    }).groupSequences,
    [4, 5],
  );
  // 其他原因码不消费 group_sequences。
  assert.equal(
    autoPauseReason({
      auto_pipeline: {
        state: "PAUSED",
        reason_code: "composition_failed",
        details: { group_sequences: [1] },
      },
    }).message,
    "",
  );
});

test("pause reason appends seed failure messages when present", () => {
  const reason = autoPauseReason({
    auto_pipeline: {
      state: "PAUSED",
      reason_code: "director_validation_failed",
      phase: "storyboard_drafts",
      details: {
        group_sequences: [88],
        failure_messages: { 88: "director plan repeats an event or composition across the group" },
      },
    },
  });
  assert.equal(
    reason.message,
    "导演方案校验未通过的分组：88（共 1 组）。失败原因：第 88 组：director plan repeats an event or composition across the group",
  );
  // 超过 2 条时截断并标注
  const many = autoPauseReason({
    auto_pipeline: {
      state: "PAUSED",
      reason_code: "director_validation_failed",
      details: {
        failure_messages: { 1: "m1", 2: "m2", 3: "m3" },
      },
    },
  });
  assert.equal(many.message, "失败原因：第 1 组：m1；第 2 组：m2 等");
});

test("resume is only allowed when PAUSED and the backend opens RESUME_AUTO_PIPELINE", () => {
  assert.equal(
    autoResumeAllowed({ auto_pipeline: { state: "PAUSED" }, allowed_actions: ["RESUME_AUTO_PIPELINE"] }),
    true,
  );
  assert.equal(autoResumeAllowed({ auto_pipeline: { state: "PAUSED" }, allowed_actions: [] }), false);
  assert.equal(
    autoResumeAllowed({ auto_pipeline: { state: "RUNNING" }, allowed_actions: ["RESUME_AUTO_PIPELINE"] }),
    false,
  );
  assert.equal(autoResumeAllowed({}), false);
});

test("busy lock covers RUNNING and PAUSED only", () => {
  assert.equal(autoPipelineBusy({ auto_pipeline: { state: "RUNNING" } }), true);
  assert.equal(autoPipelineBusy({ auto_pipeline: { state: "PAUSED" } }), true);
  assert.equal(autoPipelineBusy({ auto_pipeline: { state: "COMPLETED" } }), false);
  assert.equal(autoPipelineBusy(null), false);
  assert.equal(autoPipelineBusy({ auto_pipeline: null }), false);
});

test("stage rows read authoritative per-stage counts from workflow summary", () => {
  const summary = {
    stages: [
      { key: "narration_timeline", status: "current", counts: { sentences: 10, ready_sentences: 4, groups: 3 } },
      { key: "director_storyboard", status: "blocked", counts: { groups: 3, storyboard_groups: 1, preplan_total_units: 8, preplan_completed_units: 5 } },
      { key: "group_video_preview", status: "blocked", counts: { groups: 3, video_groups: 0 } },
      { key: "composition_export", status: "blocked", counts: { current_compositions: 0 } },
    ],
  };
  const rows = autoStageRows(summary);
  assert.deepEqual(rows.map((row) => row.key), ["narration", "director", "storyboard", "video", "composition"]);
  assert.equal(rows[0].detail, "4 / 10 句");
  assert.equal(rows[1].detail, "前置 5 / 8 单元");
  assert.equal(rows[2].detail, "1 / 3 组");
  assert.equal(rows[3].detail, "0 / 3 组");
  assert.equal(rows[4].detail, "未合成");
  assert.equal(rows[0].fact.status, "current");
  assert.equal(rows[4].fact.status, "blocked");

  const completed = autoStageRows({ stages: [
    { key: "composition_export", status: "complete", counts: { current_compositions: 1 } },
  ] });
  assert.equal(completed[4].detail, "已产出当前成片");
  // 无权威数据时全部回退零值且不伪造事实。
  assert.equal(autoStageRows(null).every((row) => row.fact === null), true);
  assert.equal(autoStageRows(null)[0].detail, "0 / 0 句");
});

test("elapsed formatting is second-granular and never fabricates unknown", () => {
  assert.equal(formatElapsedMs(null), "—");
  assert.equal(formatElapsedMs("12"), "—");
  assert.equal(formatElapsedMs(-1), "—");
  assert.equal(formatElapsedMs(0), "0秒");
  assert.equal(formatElapsedMs(59_999), "59秒");
  assert.equal(formatElapsedMs(60_000), "1分");
  assert.equal(formatElapsedMs(754_000), "12分34秒");
  assert.equal(formatElapsedMs(3_600_000), "1小时");
  assert.equal(formatElapsedMs(93_720_000), "26小时02分");
});

test("stage timing sums director phases and hides partial history rows", () => {
  const timing = {
    from_phase: "narration",
    total_elapsed_ms: 1_000_000,
    paused_ms: 0,
    phases: {
      narration: { elapsed_ms: 120_000, running: false },
      director_preplan: { elapsed_ms: 300_000, running: false },
      storyboard_drafts: { elapsed_ms: 20_000, running: false },
      storyboard: { elapsed_ms: 60_000, running: true },
    },
  };
  const rows = autoStageTiming({ auto_pipeline: { timing } });
  assert.deepEqual(rows.narration, { elapsedMs: 120_000, running: false });
  assert.deepEqual(rows.director, { elapsedMs: 320_000, running: false });
  assert.deepEqual(rows.storyboard, { elapsedMs: 60_000, running: true });
  assert.equal(rows.video, null);
  assert.equal(rows.composition, null);

  const legacy = autoStageTiming({
    auto_pipeline: { timing: { ...timing, from_phase: "storyboard" } },
  });
  assert.equal(legacy.narration, null);
  assert.equal(legacy.director, null);
  assert.deepEqual(legacy.storyboard, { elapsedMs: 60_000, running: true });

  assert.deepEqual(autoStageTiming(null), {
    narration: null,
    director: null,
    storyboard: null,
    video: null,
    composition: null,
  });
});

test("stage rows carry elapsed fields without changing authoritative counts", () => {
  const rows = autoStageRows({
    stages: [
      { key: "narration_timeline", status: "current", counts: { sentences: 10, ready_sentences: 4, groups: 3 } },
    ],
    auto_pipeline: {
      timing: {
        from_phase: "narration",
        total_elapsed_ms: 1_000,
        paused_ms: 0,
        phases: { narration: { elapsed_ms: 1_000, running: true } },
      },
    },
  });
  assert.equal(rows[0].detail, "4 / 10 句");
  assert.equal(rows[0].elapsedMs, 1_000);
  assert.equal(rows[0].elapsedRunning, true);
  assert.equal(rows[1].elapsedMs, null);
  assert.equal(rows[1].elapsedRunning, false);
});

test("elapsed ticker only patches text and is cleaned up with polling", () => {
  assert.match(appSource, /autoTickTimer: null/);
  assert.match(appSource, /function stopAutoElapsedTicker\(\)/);
  assert.match(appSource, /window\.clearInterval\(state\.autoTickTimer\)/);
  assert.match(appSource, /sentence-elapsed/);
  assert.match(appSource, /总用时/);

  const stop = productionFunction("stopAutoPipelinePolling");
  assert.match(stop, /stopAutoElapsedTicker\(\)/);
  const ticker = productionFunction("startAutoElapsedTicker");
  assert.match(ticker, /window\.setInterval\(\(\) => \{/);
  assert.match(ticker, /document\.visibilityState === "hidden"/);
  const render = productionFunction("renderAutoProgressStage");
  // ticker 必须在 scheduleAutoProgressPolling 之后启动（该函数会先 stop 并清理 ticker）。
  assert.ok(
    render.indexOf("scheduleAutoProgressPolling();") < render.indexOf("startAutoElapsedTicker();"),
  );
});

test("auto progress tab is prepended only for AUTO projects and default-selected", () => {
  assert.match(appSource, /const AUTO_PROGRESS_STAGE = \{ key: "auto_progress"/);
  const STAGES = [{ key: "project_configuration" }, { key: "script" }];
  const helpers = new Function(
    "state",
    "STAGES",
    "AUTO_PROGRESS_STAGE",
    `const isAutoProduction = (project = state.project) => project?.production_mode === "AUTO";
     ${productionFunction("stageDefinitions")}
     ${productionFunction("defaultStageKey")}
     return { stageDefinitions, defaultStageKey };`,
  )({}, STAGES, { key: "auto_progress" });
  assert.deepEqual(
    helpers.stageDefinitions({ production_mode: "MANUAL" }).map((stage) => stage.key),
    ["project_configuration", "script"],
  );
  assert.deepEqual(
    helpers.stageDefinitions({ production_mode: "AUTO" }).map((stage) => stage.key),
    ["auto_progress", "project_configuration", "script"],
  );
  assert.deepEqual(helpers.stageDefinitions({}), [STAGES[0], STAGES[1]]);
  assert.equal(helpers.defaultStageKey({ production_mode: "AUTO" }), "auto_progress");
  assert.equal(helpers.defaultStageKey({ production_mode: "MANUAL" }), "project_configuration");
  assert.match(appSource, /auto_progress: renderAutoProgressStage/);
  assert.match(appSource, /stageDefinitions\(state\.project\)\.some/);
  // MANUAL 工作台保持十一阶段（含封面与简介）：STAGES 常量本身不含 auto 页签。
  const stageList = appSource.slice(appSource.indexOf("const STAGES = ["), appSource.indexOf("];", appSource.indexOf("const STAGES = [")));
  assert.doesNotMatch(stageList, /auto_progress/);
  assert.equal((stageList.match(/key: "/g) || []).length, 11);
});

test("AUTO submit relabels the button, shows progress, skips preflight and lands on the progress view", () => {
  const start = appSource.indexOf("async function confirmNarration()");
  const end = appSource.indexOf("async function deleteCurrentProject", start);
  const confirmSource = appSource.slice(start, end);
  assert.match(confirmSource, /const auto = isAutoProduction\(\)/);
  assert.match(confirmSource, /submitButton\.disabled = true/);
  assert.match(confirmSource, /提交中…正在分句与超长句处理/);
  assert.match(confirmSource, /const preflight = auto \? null : await api\.preflight\(projectId, state\.project\.script\)/);
  assert.match(confirmSource, /await api\.confirmNarration\(projectId\)/);
  assert.match(confirmSource, /state\.activeStage = auto \? AUTO_PROGRESS_STAGE\.key : "narration_timeline"/);
  assert.match(confirmSource, /进度见「自动生产」页/);

  const settingsStart = appSource.indexOf("function renderNarrationSettingsStage()");
  const settingsEnd = appSource.indexOf("async function playProtectedAudioPath", settingsStart);
  const settingsSource = appSource.slice(settingsStart, settingsEnd);
  assert.match(settingsSource, /提交并开始生产/);
  assert.match(settingsSource, /确认并开始配音与前置导演/);
  assert.match(settingsSource, /confirmNarrationAck/);
});

test("script preflight stays for MANUAL projects and is removed for AUTO projects", () => {
  const start = appSource.indexOf("function renderScriptStage()");
  const end = appSource.indexOf("function scheduleScriptPreflight", start);
  const source = appSource.slice(start, end);
  assert.match(source, /const auto = isAutoProduction\(\)/);
  assert.match(source, /\$\{frozen \|\| auto \? "" : `<section class="panel"><div class="panel-head"><div><h2>分句预检<\/h2>/);
  assert.match(source, /if \(!auto\) scheduleScriptPreflight\(script\)/);
  assert.match(source, /if \(!auto && \(state\.project\.script \|\| ""\)\.trim\(\)\)/);
});

test("auto progress polling reuses the backoff framework with single-flight and terminal stop", () => {
  const scheduleSource = productionFunction("scheduleAutoProgressPolling");
  assert.match(scheduleSource, /DIRECTOR_POLL_DELAYS_MS\[step\]/);
  assert.match(scheduleSource, /state\.autoPollInFlight/);
  assert.match(scheduleSource, /document\.visibilityState === "hidden"/);
  assert.match(scheduleSource, /JSON\.stringify\(summary\) !== JSON\.stringify\(state\.summary\)/);
  assert.match(scheduleSource, /autoPipelineState\(state\.summary\) !== "RUNNING"/);
  assert.match(scheduleSource, /autoPipelineState\(summary\) !== "RUNNING"/);
  // 轮询只发 GET：不提交任何写操作，也不弹 toast 打扰。
  assert.doesNotMatch(scheduleSource, /method:\s*["']POST/);
  assert.doesNotMatch(scheduleSource, /toast\(/);

  const guardSource = productionFunction("autoPipelinePollIsCurrent");
  assert.match(guardSource, /context\.generation === state\.autoPollGeneration/);
  assert.match(guardSource, /context\.epoch === state\.renderEpoch/);
  assert.match(guardSource, /context\.projectId === state\.project\?\.id/);
  assert.match(guardSource, /state\.activeStage === AUTO_PROGRESS_STAGE\.key/);
});

test("auto progress polling stops together with the other workspace pollers", () => {
  for (const name of ["renderProjectWorkspace", "renderProjects", "navigate", "stopAutoPipelinePolling"]) {
    assert.notEqual(appSource.indexOf(`function ${name}(`), -1, `missing ${name}`);
  }
  for (const name of ["renderProjectWorkspace", "renderProjects", "navigate"]) {
    assert.match(productionFunction(name), /stopAutoPipelinePolling\(\)/, `${name} must stop auto polling`);
  }
  assert.match(appSource, /window\.addEventListener\("pagehide", stopAutoPipelinePolling\)/);
});

test("progress view renders RUNNING, PAUSED and COMPLETED states from the authoritative summary", () => {
  const source = productionFunction("renderAutoProgressStage");
  assert.match(source, /pipelineState === "PAUSED"/);
  assert.match(source, /pipelineState === "RUNNING"/);
  assert.match(source, /pipelineState === "COMPLETED"/);
  assert.match(source, /自动生产已暂停：/);
  assert.match(source, /原因码/);
  assert.match(source, /自动生产进行中/);
  assert.match(source, /视频生产完成/);
  assert.match(source, /autoStageRows\(summary\)/);
  assert.match(source, /autoPipelineStateLabel\(pipelineState\)/);
  assert.match(source, /scheduleAutoProgressPolling\(\)/);
});

test("resume action is gated on RESUME_AUTO_PIPELINE and goes through the resume endpoint", () => {
  const source = productionFunction("renderAutoProgressStage");
  assert.match(source, /const canResume = autoResumeAllowed\(summary\)/);
  assert.match(source, /恢复自动执行/);
  assert.match(source, /allowed_actions 不含 RESUME_AUTO_PIPELINE/);
  assert.match(source, /api\.autoPipelineResume\(state\.project\.id\)/);
  assert.match(source, /runManualAction/);
});

test("completed view embeds the current composition and reuses the formal ZIP export", () => {
  const source = productionFunction("renderAutoProgressStage");
  assert.match(source, /api\.composition\(state\.project\.id\)/);
  assert.match(source, /autoProgressVideo/);
  assert.match(source, /composition\.video_asset_id/);
  assert.match(source, /allows\(composition, "VIEW_MEDIA"\)/);
  assert.match(source, /allows\(stageFact\("composition_export"\), "export"\)/);
  assert.match(source, /downloadFormalExport/);
  // 不新增独立 MP4 下载入口：导出只走既有 downloadFormalExport。
  assert.doesNotMatch(source, /api\.blob\(/);
});

test("locally hardcoded generation entries lock while the auto pipeline is busy", () => {
  assert.match(
    appSource,
    /const canGenerateStoryboard = materializationAvailable[\s\S]*?&& !autoBusy\(\);/,
  );
  for (const name of ["runBatchDirectorSave", "openStoryboardBatchModal", "openVideoBatchModal"]) {
    assert.match(
      productionFunction(name),
      /if \(autoBusy\(\)\) \{\n {4}toast\(autoBusyReason\(\), true\);\n {4}return;\n {2}\}/,
      `${name} must guard on autoBusy`,
    );
  }
  const stage08 = productionFunction("renderGroupVideoStage");
  assert.match(stage08, /const autoLockReason = autoBusy\(\) \? autoBusyReason\(\) : null;/);
  assert.match(stage08, /const generateBlockedReason = autoLockReason \|\| generateDisabledReason;/);
  assert.match(stage08, /const batchBlockedReason = autoLockReason \|\| batchDisabledReason;/);
  assert.match(stage08, /toast\(generateBlockedReason \|\| "后端当前未允许生成组级视频。", true\)/);
  // 既有门禁链保持原样，AUTO 锁定只是叠加在最前。
  assert.match(stage08, /generateDisabledReason = videoService\?\.available !== true/);
});
