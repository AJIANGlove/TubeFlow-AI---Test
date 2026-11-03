import assert from "node:assert/strict";
import test from "node:test";

import {
  DESCRIPTION_PROMPT_LIMIT,
  countDescriptionPrompt,
  descriptionDownloadFilename,
  descriptionGateText,
  descriptionGenerateMarkup,
  descriptionHistoryMarkup,
  descriptionStateModel,
  descriptionTemplatesMarkup,
  descriptionTemplatesModel,
  formatDescriptionDuration,
} from "./description-view.js";

const templatePayload = {
  items: [
    { id: "t-2", version: 2, prompt: "第二版 <模板>", updated_at: "2026-10-26T09:00:00Z" },
    { id: "t-1", version: 1, prompt: "第一版", updated_at: "2026-10-26T08:00:00Z" },
  ],
};

const versionPayload = {
  id: "d-1",
  version_number: 1,
  source: "generated",
  prompt_template_version: 1,
  content: "0:00 Intro\n2:03 成本",
  model: "fake",
  provider_calls: 1,
  elapsed_ms: 100,
  current: true,
  created_at: "2026-10-26T09:10:00Z",
};

const statePayload = {
  project_id: "p-1",
  channel_id: "c-1",
  channel_name: "韩国小生意经济财经",
  available: true,
  blocking_reason: null,
  timeline_entries: 7,
  duration_ms: 12_000,
  current: versionPayload,
  versions: [versionPayload],
  generation: null,
};

test("description template model sorts versions and markup escapes prompt text", () => {
  const templates = descriptionTemplatesModel(templatePayload);
  assert.deepEqual(templates.map((item) => item.version), [1, 2]);
  const html = descriptionTemplatesMarkup(templates, "t-2", "自动保存");
  assert.match(html, /版本 1/);
  assert.match(html, /第二版 &lt;模板&gt;/);
  assert.match(html, /新增模板/);
  assert.match(html, /{{timeline}}/);
  assert.equal(countDescriptionPrompt("🎬简介"), 3);
  assert.equal(DESCRIPTION_PROMPT_LIMIT, 20000);
});

test("description state model keeps gate, SRT count and generation fact", () => {
  const state = descriptionStateModel({
    ...statePayload,
    generation: { id: "a", status: "RUNNING", template_version: 2 },
  });
  assert.equal(state.available, true);
  assert.equal(state.timelineEntries, 7);
  assert.equal(state.durationMs, 12000);
  assert.equal(state.generation.status, "RUNNING");
  assert.equal(state.current.content, "0:00 Intro\n2:03 成本");
});

test("generate markup disables generation without final SRT and explains why", () => {
  const state = descriptionStateModel({
    ...statePayload,
    available: false,
    blocking_reason: "description_composition_required",
    current: null,
    versions: [],
  });
  const html = descriptionGenerateMarkup(state, [], null, null, "");
  assert.match(html, /暂不能生成/);
  assert.match(html, /先完成第 09 步合成/);
  assert.match(html, /id="generateDescription" disabled/);
  assert.equal(descriptionGateText("description_composition_required"), "当前项目还没有最终 SRT。请先完成第 09 步合成，再生成简介。");
});

test("generate markup shows running state, current content and escaped error", () => {
  const templates = descriptionTemplatesModel(templatePayload);
  const state = descriptionStateModel({
    ...statePayload,
    generation: { id: "a", status: "RUNNING", template_version: 1 },
  });
  const html = descriptionGenerateMarkup(
    state,
    templates,
    "t-1",
    null,
    "",
    "模型 <b>失败</b>",
  );
  assert.match(html, /正在生成简介/);
  assert.match(html, /0:00 Intro/);
  assert.match(html, /模型 &lt;b&gt;失败&lt;\/b&gt;/);
  assert.match(html, /下载 TXT/);
});

test("history markup exposes current, immutable actions and filename is safe", () => {
  const state = descriptionStateModel(statePayload);
  const html = descriptionHistoryMarkup(state.versions);
  assert.match(html, /简介 v1/);
  assert.match(html, /当前/);
  assert.match(html, /data-description-download="d-1"/);
  assert.equal(
    descriptionDownloadFilename("A/B: 测试.", { versionNumber: 3 }),
    "A_B_ 测试-简介-v3.txt",
  );
  assert.equal(formatDescriptionDuration(61_000), "1:01");
});
