import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ApiClient } from "./api-client.js";


test("video service availability uses the aggregate backend contract", async () => {
  let seen;
  const api = new ApiClient(async (url, init = {}) => {
    seen = { url, init };
    return new Response(JSON.stringify({
      available: false,
      status: "UNAVAILABLE",
      message: "No fresh compatible node heartbeat.",
    }), { status: 200, headers: { "content-type": "application/json" } });
  });

  const contract = await api.videoService();

  assert.equal(seen.url, "/group-videos/contract");
  assert.equal(seen.init.method, "GET");
  assert.equal(contract.available, false);
});


test("step 08 requires a currently available H3 service before enabling generation", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const start = source.indexOf("async function renderGroupVideoStage()");
  const end = source.indexOf("function historyTitle", start);
  const stageSource = source.slice(start, end);

  assert.match(stageSource, /api\.optional\(\(\) => api\.videoService\(\)\)/);
  assert.match(stageSource, /generateDisabledReason = videoService\?\.available !== true/);
  assert.match(stageSource, /H3 视频服务不可用/);
  assert.match(stageSource, /aria-disabled/);
  assert.match(stageSource, /await api\.generateVideo/);
});
