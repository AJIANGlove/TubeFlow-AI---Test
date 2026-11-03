import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiClient } from "./api-client.js";
import { narrationRows, retryableNarrationRows, narrationIsRunning, NARRATION_RETRY_EVIDENCE } from "./narration-view.js";

const source = readFileSync(new URL("../app.js", import.meta.url), "utf8").replaceAll("\r\n", "\n");
const narration = { public_status: "FAILED", sentence_statuses: [
  { sequence: 1, public_status: "SUCCEEDED", allowed_actions: ["VIEW_AUDIO"] },
  { sequence: 2, public_status: "RECOVERY_REQUIRED", safe_error_code: "processing_failed", allowed_actions: ["RETRY_INVALID_OUTPUT", "REFRESH"] },
  { sequence: 3, public_status: "RECOVERY_REQUIRED", allowed_actions: ["RECONCILE", "REFRESH"] },
  { sequence: 4, public_status: "FAILED", allowed_actions: ["MANUAL_REVIEW"] },
] };

test("failure filter includes all public failures but retries only authorized invalid output", () => {
  assert.deepEqual(narrationRows(narration, true).map((row) => row.sequence), [2, 3, 4]);
  assert.deepEqual(retryableNarrationRows(narration).map((row) => row.sequence), [2]);
  assert.equal(narrationRows(narration).length, 4);
  assert.equal(narrationIsRunning(narration), false);
  assert.equal(narrationIsRunning({ ...narration, sentence_statuses: [{ public_status: "QUEUED" }] }), true);
  assert.deepEqual(narrationRows(null, true), []);
});

test("narration recovery sends the original failed attempt key and visible evidence", async () => {
  const seen = [];
  const api = new ApiClient(async (url, init) => {
    seen.push({ url, body: JSON.parse(init.body), method: init.method });
    return new Response(JSON.stringify({ public_status: "QUEUED" }), { status: 202 });
  });
  await api.recoverNarration("project-1", "original-attempt-key", NARRATION_RETRY_EVIDENCE);
  assert.deepEqual(seen, [{ url: "/narration/recover", method: "POST", body: {
    project_id: "project-1", idempotency_key: "original-attempt-key",
    resolution: "RETRY_INVALID_OUTPUT", external_evidence: NARRATION_RETRY_EVIDENCE,
  } }]);
});

test("step 06 renders failure details, filters and allowed retry actions", () => {
  const stage = source.slice(source.indexOf("function renderNarrationTimelineStage()"), source.indexOf("function compactNarrationAudioCard("));
  assert.match(stage, /需人工处理/);
  assert.match(stage, /safe_error_code/);
  assert.match(stage, /只看失败/);
  assert.match(stage, /allows\(row, "RETRY_INVALID_OUTPUT"\)/);
  assert.match(stage, /重试全部失败句/);
  assert.doesNotMatch(stage, /row\.state|OUTPUT_INVALID/);
  assert.match(stage, /scheduleNarrationPolling\(/);
});

test("manual retries confirm once, report each result and never mark 202 as completion", () => {
  const start = source.indexOf("async function retryNarrationRows(");
  assert.ok(start >= 0);
  const action = source.slice(start, source.indexOf("function renderNarrationTimelineStage()", start));
  assert.match(action, /await actionModal\(/);
  assert.match(action, /NARRATION_RETRY_EVIDENCE/);
  assert.match(action, /if \(!confirmResult\.confirmed\) return/);
  assert.match(action, /for \(const row of rows\)/);
  assert.match(action, /await api\.recoverNarration\(projectId, row\.idempotency_key/);
  assert.match(action, /已受理/);
  assert.match(action, /catch \(error\)/);
});

function productionFunction(name, asyncFunction = false) {
  const start = source.indexOf(`${asyncFunction ? "async " : ""}function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  const end = source.indexOf("\n}\n", start);
  return source.slice(start, end + 2);
}

test("bulk recovery confirms once and preserves mixed per-sentence outcomes", async () => {
  const state = { project: { id: "project-1" }, activeStage: "narration_timeline", narrationRetryBusy: false };
  const calls = [];
  let confirms = 0;
  const api = {
    recoverNarration: async (id, key) => { calls.push([id, key]); if (key === "failed") throw new Error("offline"); },
    narration: async () => ({ public_status: "QUEUED" }), summary: async () => ({}),
  };
  const action = new Function("state", "actionModal", "api", "NARRATION_RETRY_EVIDENCE", "stopNarrationPolling", "renderNarrationTimelineStage", "$", "escapeHtml", "messageOf", "applyProjectFacts", "toast",
    `${productionFunction("retryNarrationRows", true)}; return retryNarrationRows;`)(
    state, async () => { confirms += 1; return { confirmed: true }; }, api, NARRATION_RETRY_EVIDENCE,
    () => {}, () => {}, () => null, String, (error) => error.message, () => {}, () => {},
  );
  await action([{ sequence: 2, idempotency_key: "failed" }, { sequence: 3, idempotency_key: "accepted" }]);
  assert.equal(confirms, 1);
  assert.deepEqual(calls, [["project-1", "failed"], ["project-1", "accepted"]]);
  assert.deepEqual(state.narrationRetryResults.map((row) => row.accepted), [false, true]);
  assert.match(state.narrationRetryResults[0].message, /offline/);
  assert.match(state.narrationRetryResults[1].message, /已受理.*等待/);
  assert.equal(state.narrationRetryBusy, false);
});

test("cancelled confirmation submits no recovery", async () => {
  const state = { project: { id: "project-1" } };
  const action = new Function("state", "actionModal", "escapeHtml", "NARRATION_RETRY_EVIDENCE",
    `${productionFunction("retryNarrationRows", true)}; return retryNarrationRows;`)(
    state, async () => ({ confirmed: false }), String, NARRATION_RETRY_EVIDENCE,
  );
  await action([{ sequence: 2 }]);
  assert.equal(state.narrationRetryBusy, undefined);
});

test("polling is read-only, serial and ignores responses after leaving the project", async () => {
  const timers = new Map();
  let timerId = 0;
  let resolveNarration;
  let calls = 0;
  let applies = 0;
  const state = { project: { id: "p1" }, token: "test", renderEpoch: 1, activeStage: "narration_timeline", narrationPollGeneration: 0, narrationPollInFlight: false, narration: { public_status: "RUNNING" } };
  const api = { narration: () => { calls += 1; return new Promise((resolve) => { resolveNarration = resolve; }); }, summary: async () => ({}) };
  const window = { setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => timers.delete(id) };
  const create = new Function("state", "window", "document", "api", "narrationIsRunning", "DIRECTOR_POLL_DELAYS_MS", "applyProjectFacts", "renderNarrationTimelineStage", "$", "messageOf",
    `${productionFunction("stopNarrationPolling")}\n${productionFunction("narrationPollIsCurrent")}\n${productionFunction("scheduleNarrationPolling")}\nreturn {scheduleNarrationPolling, stopNarrationPolling};`);
  const polling = create(state, window, { visibilityState: "visible" }, api, narrationIsRunning, [2500, 5000, 10000], () => { applies += 1; }, () => {}, () => null, String);
  polling.scheduleNarrationPolling();
  const tick = timers.get(timerId); timers.delete(timerId);
  const pending = tick();
  await tick();
  assert.equal(calls, 1);
  state.project = { id: "p2" };
  polling.stopNarrationPolling();
  resolveNarration({ public_status: "SUCCEEDED" });
  await pending;
  assert.equal(applies, 0);
  assert.equal(state.narrationPollInFlight, false);
  assert.equal(timers.size, 0);
  assert.doesNotMatch(productionFunction("scheduleNarrationPolling"), /recoverNarration|confirmNarration|method:.*POST/);
});

function recoveryPollingHarness(api) {
  const state = {
    project: { id: "project-A" }, activeStage: "narration_timeline",
    token: "test", renderEpoch: 1, narrationPollGeneration: 0,
    narrationPollInFlight: false, narrationRetryBusy: false,
    narrationRefreshPendingProjectId: null,
    narration: { public_status: "FAILED", sentence_statuses: [
      { sequence: 2, public_status: "RECOVERY_REQUIRED", allowed_actions: ["RETRY_INVALID_OUTPUT"] },
    ] },
    summary: {},
  };
  const timers = new Map();
  const applied = [];
  let timerId = 0;
  let engine;
  const window = {
    setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  };
  const render = () => engine.scheduleNarrationPolling();
  engine = new Function(
    "state", "window", "document", "api", "narrationIsRunning", "DIRECTOR_POLL_DELAYS_MS",
    "applyProjectFacts", "renderNarrationTimelineStage", "$", "escapeHtml", "messageOf",
    "toast", "NARRATION_RETRY_EVIDENCE", "actionModal",
    [
      productionFunction("stopNarrationPolling"),
      productionFunction("narrationPollIsCurrent"),
      productionFunction("scheduleNarrationPolling"),
      productionFunction("retryNarrationRows", true),
      "return { retryNarrationRows, scheduleNarrationPolling };",
    ].join("\n"),
  )(
    state, window, { visibilityState: "visible" }, api, narrationIsRunning, [2500, 5000, 10000],
    (latest, summary) => {
      applied.push(latest);
      state.narration = latest;
      state.summary = summary;
    },
    render, () => null, String, (error) => error.message, () => {}, NARRATION_RETRY_EVIDENCE,
    async () => ({ confirmed: true }),
  );
  return {
    state, timers, applied, engine, render,
    tick: async () => {
      assert.equal(timers.size, 1, "one read-only poll must be scheduled");
      const [id, callback] = timers.entries().next().value;
      timers.delete(id);
      await callback();
    },
  };
}

test("accepted or uncertain recovery survives failed refresh using GET only until terminal", async () => {
  for (const postAccepted of [true, false]) {
    const calls = [];
    const queued = { public_status: "QUEUED", sentence_statuses: [
      { sequence: 2, public_status: "QUEUED", allowed_actions: ["REFRESH"] },
    ] };
    const completed = { public_status: "SUCCEEDED", sentence_statuses: [
      { sequence: 2, public_status: "SUCCEEDED", allowed_actions: ["VIEW_AUDIO"] },
    ] };
    let reads = 0;
    const harness = recoveryPollingHarness({
      recoverNarration: async (projectId, key) => {
        calls.push(["POST", projectId, key]);
        if (!postAccepted) throw new Error("POST response lost");
        return queued;
      },
      narration: async (projectId) => {
        calls.push(["GET", projectId]);
        reads += 1;
        if (reads <= 2) throw new Error("GET temporarily unavailable");
        return completed;
      },
      summary: async () => ({}),
    });
    const original = harness.state.narration;
    await harness.engine.retryNarrationRows([{ sequence: 2, idempotency_key: "original-key" }]);
    assert.equal(harness.state.narration, postAccepted ? queued : original);
    assert.equal(harness.state.narrationRetryResults[0].accepted, postAccepted);
    assert.equal(harness.state.narrationRefreshPendingProjectId, "project-A");
    assert.equal(harness.timers.size, 1);
    await harness.tick();
    assert.equal(harness.timers.size, 1, "failed GET must remain recoverable");
    await harness.tick();
    assert.equal(harness.state.narration, completed);
    assert.equal(harness.state.narrationRefreshPendingProjectId, null);
    assert.equal(harness.state.narrationPollInFlight, false);
    assert.equal(harness.timers.size, 0, "terminal state must stop polling");
    assert.deepEqual(calls, [
      ["POST", "project-A", "original-key"],
      ["GET", "project-A"], ["GET", "project-A"], ["GET", "project-A"],
    ]);
  }
});

test("another project keeps polling during a batch and stale project or epoch responses are ignored", async () => {
  for (const targetProjectId of ["project-B", "project-A"]) {
    let releasePost;
    const calls = [];
    const staleAccepted = { public_status: "QUEUED", marker: "old POST" };
    const staleRefreshed = { public_status: "QUEUED", marker: "old refresh" };
    const current = { public_status: "RUNNING", marker: "current page" };
    const completed = { public_status: "SUCCEEDED", marker: "current terminal" };
    let batchRefreshPending = true;
    let currentPollResult = current;
    const harness = recoveryPollingHarness({
      recoverNarration: (projectId) => {
        calls.push(["POST", projectId]);
        return new Promise((resolve) => { releasePost = resolve; });
      },
      narration: async (projectId) => {
        calls.push(["GET", projectId]);
        if (projectId === "project-A" && batchRefreshPending) {
          batchRefreshPending = false;
          return staleRefreshed;
        }
        return currentPollResult;
      },
      summary: async () => ({}),
    });
    const batch = harness.engine.retryNarrationRows([{ sequence: 2, idempotency_key: "old-key" }]);
    // 确认弹窗是异步的：先让出事件循环，等确认通过后 POST 发出（releasePost 就位）。
    await new Promise((resolve) => setImmediate(resolve));
    harness.state.project = { id: targetProjectId };
    harness.state.renderEpoch += 1;
    harness.state.narration = current;
    harness.render();
    if (targetProjectId === "project-B") {
      await harness.tick();
      assert.equal(harness.state.narrationRetryBusy, true);
      assert.ok(calls.some(([method, id]) => method === "GET" && id === "project-B"));
    }
    releasePost(staleAccepted);
    await batch;
    assert.equal(harness.state.narration, current, "old batch must not overwrite the current view");
    assert.equal(harness.state.narrationRetryBusy, false);
    assert.ok(!harness.applied.includes(staleAccepted));
    assert.ok(!harness.applied.includes(staleRefreshed));
    assert.equal(harness.timers.size, 1, "current page must keep polling after the old batch");
    currentPollResult = completed;
    await harness.tick();
    assert.equal(harness.state.narration, completed);
    assert.equal(harness.timers.size, 0);
    assert.deepEqual(calls.filter(([method]) => method === "POST"), [["POST", "project-A"]]);
  }
});
