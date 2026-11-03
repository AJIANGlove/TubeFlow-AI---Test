import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ApiClient } from "./api-client.js";
import { preplanRunCalmSummary, preplanRunIsCalm } from "./step07-view.js";

const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8").replaceAll("\r\n", "\n");

function productionFunction(name) {
  const start = appSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing production function ${name}`);
  const end = appSource.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated production function ${name}`);
  return appSource.slice(start, end + 2);
}

const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "content-type": "application/json" },
});

test("project director preplan routes follow the two-phase public contract", async () => {
  const seen = [];
  const api = new ApiClient(async (url, init = {}) => {
    seen.push({ url, init });
    return jsonResponse({ run_id: "run-1", shots: [], candidates: [] }, init.method === "POST" ? 202 : 200);
  });

  await api.startDirectorPreplan("project-1");
  await api.latestDirectorPreplanRun("project-1");
  await api.directorPreplanRun("project-1", "run-1");
  await api.resolveDirectorPreplan("project-1", "run-1", "CONFIRMED_NOT_ACCEPTED", "Provider console evidence");
  await api.directorPreplanCurrent("project-1", 40, 20);
  await api.directorMaterialization("project-1");

  assert.deepEqual(seen.map((entry) => entry.url), [
    "/projects/project-1/director-preplan/runs",
    "/projects/project-1/director-preplan/runs/latest",
    "/projects/project-1/director-preplan/runs/run-1",
    "/projects/project-1/director-preplan/runs/run-1/manual-resolution",
    "/projects/project-1/director-preplan/current?offset=40&limit=20",
    "/projects/project-1/director-materialization/current",
  ]);
  assert.equal(seen[0].init.method, "POST");
  assert.deepEqual(JSON.parse(seen[0].init.body), {});
  assert.match(seen[0].init.headers["Idempotency-Key"], /^[0-9a-f-]{36}$/);
  assert.deepEqual(JSON.parse(seen[3].init.body), {
    resolution: "CONFIRMED_NOT_ACCEPTED",
    external_evidence: "Provider console evidence",
  });
});

test("manual preplan retry reuses its idempotency key after network uncertainty", async () => {
  const keys = [];
  let first = true;
  const api = new ApiClient(async (_url, init) => {
    keys.push(init.headers["Idempotency-Key"]);
    if (first) {
      first = false;
      throw new Error("response lost");
    }
    return jsonResponse({ run_id: "run-2" }, 202);
  });

  await assert.rejects(api.startDirectorPreplan("project-1"), (error) => error.code === "backend_unavailable");
  await api.startDirectorPreplan("project-1");

  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
});

test("confirm narration does not issue a second frontend director POST", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const start = source.indexOf("async function confirmNarration()");
  const end = source.indexOf("async function deleteCurrentProject", start);
  const confirmSource = source.slice(start, end);

  assert.match(confirmSource, /await api\.confirmNarration\(projectId\)/);
  assert.doesNotMatch(confirmSource, /startDirectorPreplan|director-preplan/);
});

test("step 07 uses project preplan and materialization instead of group director generation", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const start = source.indexOf("async function renderDirectorStoryboardStage()");
  const end = source.indexOf("async function runManualAction", start);
  const stageSource = source.slice(start, end);

  assert.match(stageSource, /latestDirectorPreplanRun/);
  assert.match(stageSource, /directorPreplanCurrent/);
  assert.match(stageSource, /directorMaterialization/);
  assert.doesNotMatch(stageSource, /api\.generateDirector|latestDirectorAttempt|reconcileDirector/);
  assert.match(stageSource, /storyboardShotRow\(shot, index, canEditDirector, groupSentences\)/);
});

test("director polling is GET-only and final sentence choices are group-scoped", () => {
  const pollStart = appSource.indexOf("async function pollDirectorStatus");
  const pollEnd = appSource.indexOf("function directorRunControlButtonsMarkup", pollStart);
  const pollSource = appSource.slice(pollStart, pollEnd);
  const filterStart = appSource.indexOf("function sentencesForGroup");
  const filterEnd = appSource.indexOf("function renderGroupTabs", filterStart);
  const filterSource = appSource.slice(filterStart, filterEnd);

  assert.match(pollSource, /latestDirectorPreplanRun|directorMaterialization/);
  assert.doesNotMatch(pollSource, /startDirectorPreplan|method:\s*["']POST/);
  assert.doesNotMatch(pollSource, /refreshProjectFacts\(\)/);
  assert.match(filterSource, /group\?\.sentence_sequences/);
  assert.match(filterSource, /narrationSentences\(\)\.filter/);
});

test("director polling adapts from 2.5s to 5s and 10s, then resets on change", () => {
  const state = { directorPollStableCount: 0, directorPollDelayMs: 0 };
  const delays = Object.freeze([2500, 5000, 10000]);
  const advance = new Function(
    "state",
    "DIRECTOR_POLL_DELAYS_MS",
    `${productionFunction("advanceDirectorPollDelay")}; return advanceDirectorPollDelay;`,
  )(state, delays);

  advance(true);
  assert.deepEqual(state, { directorPollStableCount: 0, directorPollDelayMs: 2500 });
  advance(false);
  assert.deepEqual(state, { directorPollStableCount: 1, directorPollDelayMs: 5000 });
  advance(false);
  advance(false);
  assert.deepEqual(state, { directorPollStableCount: 3, directorPollDelayMs: 10000 });
  advance(true);
  assert.deepEqual(state, { directorPollStableCount: 0, directorPollDelayMs: 2500 });
});

test("director polling signatures distinguish data changes from structural and terminal changes", () => {
  const signature = new Function(`${productionFunction("directorPollSnapshotSignature")}; return directorPollSnapshotSignature;`)();
  const structure = new Function(`${productionFunction("directorPollStructureSignature")}; return directorPollStructureSignature;`)();
  const terminal = new Function(
    "publicStatus",
    `${productionFunction("directorValueIsActive")}\n${productionFunction("directorPollReachedTerminal")}; return directorPollReachedTerminal;`,
  )((value) => value?.public_status || value?.status || "NOT_STARTED");
  const base = {
    run: {
      run_id: "run-1",
      public_status: "RUNNING",
      completed_units: 10,
      succeeded_units: 10,
      inflight_units: 4,
      retry_pending_units: 1,
      auto_retries_used: 2,
      failed_unit_ordinal: null,
      failed_unit_kind: null,
      unsent_units: 39,
      failure_code: null,
    },
    narration: { public_status: "RUNNING", groups: [] },
    materialization: null,
    fact: { status: "current" },
  };
  const progress = structuredClone(base);
  progress.run.completed_units = 11;
  progress.run.succeeded_units = 11;
  progress.run.inflight_units = 3;
  const grouped = structuredClone(progress);
  grouped.narration.groups = [{ group_id: "group-1" }];
  const finished = structuredClone(progress);
  finished.run.public_status = "FAILED";

  assert.notEqual(signature(base), signature(progress));
  assert.equal(structure(base), structure(progress));
  assert.notEqual(structure(progress), structure(grouped));
  assert.equal(terminal(base, progress), false);
  assert.equal(terminal(progress, finished), true);
});

test("step 07 polling patches only changed data and fully renders only transitions", () => {
  const start = appSource.indexOf("async function pollDirectorStatus");
  const end = appSource.indexOf("function scheduleDirectorPolling", start);
  const pollSource = appSource.slice(start, end);
  const lifecycleStart = appSource.indexOf("function scheduleDirectorPolling");
  const lifecycleEnd = appSource.indexOf("function directorRunControlButtonsMarkup", lifecycleStart);
  const lifecycleSource = appSource.slice(lifecycleStart, lifecycleEnd);

  assert.match(pollSource, /state\.directorPollInFlight/);
  assert.match(pollSource, /directorPollContextIsCurrent\(context\)/);
  assert.match(pollSource, /if \(structureChanged \|\| terminalChanged\)[\s\S]*?renderProjectWorkspace\(\)/);
  assert.equal((pollSource.match(/renderProjectWorkspace\(\)/g) || []).length, 1);
  assert.match(pollSource, /if \(changed\)[\s\S]*?patchDirectorPollingUi\(/);
  assert.doesNotMatch(pollSource, /toast\(/);
  assert.match(lifecycleSource, /document\.visibilityState === "hidden"/);
  assert.match(lifecycleSource, /visibilitychange/);
  assert.match(lifecycleSource, /requestDirectorPollNow\(\)/);
});

test("director polling context rejects stale epoch, project, stage, and object identity", () => {
  const currentContext = {
    generation: 4,
    renderEpoch: 9,
    projectId: "project-1",
  };
  const state = {
    directorPollContext: currentContext,
    directorPollGeneration: 4,
    renderEpoch: 9,
    activeStage: "director_storyboard",
    project: { id: "project-1" },
  };
  const isCurrent = new Function(
    "state",
    `${productionFunction("directorPollContextIsCurrent")}; return directorPollContextIsCurrent;`,
  )(state);

  assert.equal(isCurrent(currentContext), true);
  assert.equal(isCurrent({ ...currentContext }), false);
  state.directorPollGeneration = 5;
  assert.equal(isCurrent(currentContext), false);
  state.directorPollGeneration = 4;
  state.renderEpoch = 10;
  assert.equal(isCurrent(currentContext), false);
  state.renderEpoch = 9;
  state.activeStage = "narration_timeline";
  assert.equal(isCurrent(currentContext), false);
  state.activeStage = "director_storyboard";
  state.project = { id: "project-2" };
  assert.equal(isCurrent(currentContext), false);
});

test("review-required materialization candidates seed an editable first draft without truncation", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const start = source.indexOf("async function renderDirectorStoryboardStage()");
  const end = source.indexOf("async function runManualAction", start);
  const stageSource = source.slice(start, end);

  assert.match(stageSource, /director\?\.shots \|\| candidate\?\.shots/);
  assert.match(stageSource, /allows\(materialization, "EDIT_FINAL_DIRECTOR"\)/);
  assert.match(
    stageSource,
    /draft \? null : candidate\?\.candidate_id \|\| null/,
  );
  assert.match(source, /source_candidate_id: sourceCandidateId/);
  assert.doesNotMatch(stageSource, /candidate\?\.shots\.slice\(0,\s*5\)/);
  assert.match(source, /\$\("\.empty-state", list\)\?\.remove\(\)/);
});

test("preplan run view renders retry and failure details from the new run contract", () => {
  const escapeHtml = (value = "") => String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
  const preplanRunContent = new Function(
    "renderUnavailable",
    "attemptStatusNotice",
    "escapeHtml",
    "compactCount",
    "preplanRunIsCalm",
    "preplanRunCalmSummary",
    `${productionFunction("preplanRunContent")}; return preplanRunContent;`,
  )(
    (title, detail) => `<unavailable>${title}:${detail}</unavailable>`,
    (label, attempt) => `<notice>${attempt.public_status || ""}</notice>`,
    escapeHtml,
    (value) => (Number.isFinite(Number(value)) ? String(Number(value)) : "—"),
    preplanRunIsCalm,
    preplanRunCalmSummary,
  );

  const failed = preplanRunContent({
    status: "FAILED",
    public_status: "FAILED",
    total_units: 53,
    completed_units: 20,
    inflight_units: 0,
    retry_pending_units: 0,
    unsent_units: 32,
    auto_retries_used: 2,
    auto_retry_ceiling: 106,
    failed_unit_ordinal: 7,
    failed_unit_kind: "GLOBAL_PLAN",
    failure_code: "director_output_plan_coverage_invalid",
  });
  assert.match(failed, /失败码：director_output_plan_coverage_invalid · 批次 7（全局规划）/);
  assert.match(failed, /<span>在途并行<\/span><strong>0<\/strong>/);
  assert.match(failed, /<span>失败批次<\/span><strong>7<\/strong><small>全局规划<\/small>/);
  assert.doesNotMatch(failed, /自动重试中 [1-9]\d* 个批次/);

  const retrying = preplanRunContent({
    status: "RUNNING",
    public_status: "RUNNING",
    total_units: 53,
    completed_units: 20,
    inflight_units: 3,
    retry_pending_units: 2,
    unsent_units: 28,
    auto_retries_used: 5,
    auto_retry_ceiling: 106,
    failed_unit_ordinal: null,
    failed_unit_kind: null,
    failure_code: null,
  });
  assert.match(retrying, /自动重试中 2 个批次 · 已用重试 5\/106/);
  assert.match(retrying, /<span>自动重试中<\/span><strong>2<\/strong><small>已用 5\/106<\/small>/);
  assert.match(retrying, /<span>在途并行<\/span><strong>3<\/strong>/);
  assert.doesNotMatch(retrying, /失败码：/);

  const clean = preplanRunContent({
    status: "RUNNING",
    public_status: "RUNNING",
    total_units: 2,
    completed_units: 1,
    retry_pending_units: 0,
    inflight_units: 1,
  });
  assert.doesNotMatch(clean, /自动重试中 \d+ 个批次/);
  assert.doesNotMatch(clean, /失败码：/);

  const calm = preplanRunContent({
    status: "SUCCEEDED",
    public_status: "SUCCEEDED",
    total_units: 51,
    completed_units: 51,
    inflight_units: 0,
    retry_pending_units: 0,
    unsent_units: 0,
    auto_retries_used: 0,
    auto_retry_ceiling: 0,
    failed_unit_ordinal: null,
    failed_unit_kind: null,
    failure_code: null,
    message: "ready",
  });
  assert.match(calm, /前置导演 51\/51 已完成/);
  assert.match(calm, /运行详情/);
  assert.doesNotMatch(calm, /<notice>/);
});

test("director list view consumes the batched overview without per-group fetches", () => {
  const listStart = appSource.indexOf("async function renderDirectorListView");
  const listEnd = appSource.indexOf("async function renderDirectorDetailView", listStart);
  assert.notEqual(listStart, -1, "missing renderDirectorListView");
  assert.notEqual(listEnd, -1, "missing renderDirectorDetailView");
  const listSource = appSource.slice(listStart, listEnd);

  assert.match(listSource, /api\.storyboardBatchOverview/);
  assert.match(listSource, /groupCardModel\(/);
  assert.doesNotMatch(listSource, /api\.draft\(|api\.appliedDirector\(|api\.storyboard\(|api\.storyboards\(/);

  const detailEnd = appSource.indexOf("async function saveDirectorDraftFromForm", listEnd);
  const detailSource = appSource.slice(listEnd, detailEnd);
  assert.match(detailSource, /api\.draft\(/);
  assert.match(detailSource, /api\.appliedDirector\(/);
  assert.match(detailSource, /api\.storyboard\(/);
  assert.match(detailSource, /api\.storyboards\(/);
  assert.match(detailSource, /id="saveDirector"/);
  assert.match(detailSource, /id="generateStoryboard"/);
  assert.match(detailSource, /id="storyboardHistory"/);
});

test("group switching autosaves a dirty draft before changing the active group", () => {
  const source = productionFunction("switchDirectorGroup");
  const saveIndex = source.indexOf("await ensureDirectorDraftSaved()");
  const assignIndex = source.indexOf("state.activeGroupId =");
  assert.notEqual(saveIndex, -1);
  assert.notEqual(assignIndex, -1);
  assert.ok(saveIndex < assignIndex, "autosave must run before activeGroupId changes");
  assert.match(source, /state\.directorView = "detail"/);

  const guardSource = productionFunction("ensureDirectorDraftSaved");
  assert.match(guardSource, /!state\.directorDraftDirty/);
  assert.match(guardSource, /await saveDirectorDraftFromForm\(state\.directorDraftContext\)/);
  assert.match(guardSource, /toast\(/);

  const saveSource = productionFunction("saveDirectorDraftFromForm");
  assert.match(saveSource, /readDirectorDraftForm\(/);
  assert.match(saveSource, /api\.editDirectorDraft\(context\.groupId, body\)/);
});

test("stage switching and workspace navigation flush the dirty director draft first", () => {
  const stageStart = appSource.indexOf('$$("[data-stage-key]").forEach');
  const stageSource = appSource.slice(stageStart, stageStart + 400);
  const flushIndex = stageSource.indexOf("await flushPendingSave()");
  const draftIndex = stageSource.indexOf("await ensureDirectorDraftSaved()");
  const assignIndex = stageSource.indexOf("state.activeStage =");
  assert.notEqual(flushIndex, -1);
  assert.notEqual(draftIndex, -1);
  assert.notEqual(assignIndex, -1);
  assert.ok(flushIndex < draftIndex && draftIndex < assignIndex);

  const navigateSource = productionFunction("navigate");
  assert.match(navigateSource, /await ensureDirectorDraftSaved\(\)/);
  assert.match(navigateSource, /clearLocationHash\(\)/);

  const detailStart = appSource.indexOf("async function renderDirectorDetailView");
  const detailEnd = appSource.indexOf("async function saveDirectorDraftFromForm", detailStart);
  const detailSource = appSource.slice(detailStart, detailEnd);
  assert.match(detailSource, /state\.directorDraftDirty = true/);
  assert.match(detailSource, /state\.directorDraftContext = \{/);
  assert.match(detailSource, /showDirectorListView\(\)/);
  assert.match(detailSource, /parseGroupSequenceInput\(sequenceInput\.value, state\.groups\.length\)/);
  assert.match(detailSource, /bindGroupTabs\(\(nextGroupId\) => switchDirectorGroup\(nextGroupId\)\)/);
});

test("director views sync location hash via replaceState and restore on workspace load", () => {
  assert.match(appSource, /globalThis\.history\?\.replaceState\(null, "", hash\)/);
  assert.match(appSource, /hashForDirectorView\(state\.project\.id, state\.directorView === "detail"/);
  assert.match(appSource, /`#\/project\/\$\{state\.project\.id\}\/\$\{state\.activeStage\}`/);
  assert.match(appSource, /directorViewFromHash\(globalThis\.location\?\.hash/);
  assert.match(appSource, /openProject\(route\.projectId, route\)/);
  assert.match(appSource, /state\.directorView = "detail"/);
  assert.doesNotMatch(appSource, /addEventListener\(["']popstate["']/);
});

test("arrow-key group switching is guarded by editable targets and open overlays", () => {
  const start = appSource.indexOf('document.addEventListener("keydown"');
  assert.notEqual(start, -1);
  const source = appSource.slice(start, start + 1900);
  assert.match(source, /editableTarget/);
  assert.match(source, /\$\$\("\.modal-backdrop, \.drawer-backdrop, details\.action-menu\[open\]"\)\.length/);
  assert.match(source, /state\.activeStage !== "director_storyboard"/);
  assert.match(source, /state\.directorView !== "detail"/);
  assert.match(source, /stepDirectorGroupByDelta\(-1\)/);
  assert.match(source, /stepDirectorGroupByDelta\(1\)/);
  // Lightbox 打开时方向键仍然归 Lightbox 自己切组。
  assert.match(source, /lightboxStepGroup\(-1\)/);
});

test("list view polling appends one batched overview GET and re-renders once per change", () => {
  const pollStart = appSource.indexOf("async function pollDirectorStatus");
  const pollEnd = appSource.indexOf("function scheduleDirectorPolling", pollStart);
  const pollSource = appSource.slice(pollStart, pollEnd);

  assert.match(pollSource, /state\.directorView === "list"\s*\n\s*\? api\.optional\(\(\) => api\.storyboardBatchOverview/);
  assert.match(pollSource, /directorPollSnapshot\(\s*nextRun,\s*nextNarration,\s*nextMaterialization,\s*nextSummary,\s*nextOverview,\s*\)/);
  assert.match(pollSource, /renderDirectorListView\(\{/);
  assert.doesNotMatch(pollSource, /method:\s*["']POST/);
  assert.doesNotMatch(pollSource, /startDirectorPreplan/);
  assert.equal((pollSource.match(/renderProjectWorkspace\(\)/g) || []).length, 1);

  const snapshotSource = productionFunction("directorPollSnapshot");
  assert.match(snapshotSource, /current_generation_id/);
  assert.match(snapshotSource, /storyboard_status/);
  assert.match(snapshotSource, /can_generate/);

  const scheduleSource = productionFunction("scheduleDirectorPolling");
  assert.match(scheduleSource, /overview/);
});

test("group switching renders an explicit error card instead of a stale view on failure", () => {
  const switchSource = productionFunction("switchDirectorGroup");
  assert.match(switchSource, /renderUnavailable\("页面加载失败", messageOf\(error\)\)/);
  const listSource = productionFunction("showDirectorListView");
  assert.match(listSource, /await ensureDirectorDraftSaved\(\)/);
  assert.match(listSource, /renderUnavailable\("页面加载失败", messageOf\(error\)\)/);
});

test("unknown preplan uses its explicit recovery action while other unknown work stays locked", () => {
  const allows = (value, action) => value?.allowed_actions?.includes(action) || false;
  const allowsAny = (values, actions) => values.some(value => actions.some(action => allows(value, action)));
  const canRegenerate = new Function("publicStatus", "allowsAny", `${productionFunction("canRegenerateDirectorPreplan")}; return canRegenerateDirectorPreplan;`)(value => value?.public_status, allowsAny);
  assert.equal(canRegenerate(null, { public_status: "RECOVERY_REQUIRED", allowed_actions: ["REGENERATE_DIRECTOR_PREPLAN", "REFRESH"] }, null), true);
  assert.equal(canRegenerate(null, { public_status: "RECOVERY_REQUIRED", allowed_actions: ["RECONCILE", "REFRESH"] }, null), false);
  assert.equal(canRegenerate(null, { public_status: "RUNNING", allowed_actions: ["REGENERATE_DIRECTOR_PREPLAN"] }, null), false);
});

test("input-change confirmation retries with explicit consent and the same intent key", async () => {
  const seen = [];
  const api = new ApiClient(async (_url, init) => {
    seen.push({ key: init.headers["Idempotency-Key"], body: JSON.parse(init.body) });
    return seen.length === 1 ? jsonResponse({ detail: { code: "director_input_changed", message: "Input changed" } }, 409) : jsonResponse({ run_id: "new-run" }, 202);
  });
  await assert.rejects(api.startDirectorPreplan("changed-project"), error => error.code === "director_input_changed");
  await api.startDirectorPreplan("changed-project", true);
  assert.deepEqual(seen.map(item => item.body), [{}, { confirm_new_input: true }]);
  assert.equal(seen[0].key, seen[1].key);
});

test("preplan input-change cancellation sends no second request or false success", async () => {
  let action;
  let calls = 0;
  const bind = new Function("$", "runManualAction", "state", "api", "window", "queryAcceptedAttempt", "openDirectorManualResolution",
    `${productionFunction("bindDirectorRunActionControls")}; return bindDirectorRunActionControls;`)(
    selector => selector === "#regenerateDirectorPreplan" ? { addEventListener: (_event, fn) => { action = fn; } } : null,
    operation => operation(), { project: { id: "project" } },
    { startDirectorPreplan: async () => { calls++; throw Object.assign(new Error("changed"), { code: "director_input_changed" }); } },
    { confirm: () => false }, () => { throw Error("must not query nonexistent run"); }, () => {},
  );
  bind(null);
  assert.equal(await action(), false);
  assert.equal(calls, 1);
  assert.match(productionFunction("runManualAction"), /if \(await operation\(\) !== false\) toast\(successMessage\)/);
});