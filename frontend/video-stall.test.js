import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ApiClient } from "./api-client.js";
import { actionLabel } from "./step07-view.js";

test("video cancel uses the explicit attempt cancel route", async () => {
  let seen;
  const api = new ApiClient(async (url, init) => {
    seen = { url, init };
    return new Response(JSON.stringify({ public_status: "RUNNING" }), {
      status: 202,
      headers: { "content-type": "application/json" },
    });
  });
  await api.cancelVideo("a1");
  assert.equal(seen.url, "/group-videos/attempts/a1/cancel");
  assert.equal(seen.init.method, "POST");
});

test("CANCEL action has a stable Chinese label", () => {
  assert.equal(actionLabel("CANCEL"), "取消任务");
});

test("group video surface shows authoritative remote wait and stalled notice", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.match(source, /remote_wait_status === "remote_stalled"/);
  assert.match(source, /疑似卡死/);
  assert.match(source, /远端处理中 · 已等待/);
  assert.match(source, /remote_wait_seconds/);
});

test("cancel button requires CANCEL action and defaults to stalled attempts only", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.match(
    source,
    /allows\(videoAttempt, "CANCEL"\) && videoAttempt\?\.remote_wait_status === "remote_stalled"/,
  );
  assert.match(source, /api\.cancelVideo\(videoAttempt\.attempt_id\)/);
  assert.match(source, /window\.confirm\("确认取消该远端生成任务/);
});

test("batch overview surfaces stalled groups in the strip and the modal", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.match(source, /latest_attempt_stalled/);
  assert.match(source, /远端疑似卡死/);
});
