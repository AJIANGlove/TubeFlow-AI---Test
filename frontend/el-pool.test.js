import test from "node:test";
import assert from "node:assert/strict";
import {
  statusBadge,
  quotaPercent,
  formatCharacters,
  formatVoiceSlots,
  formatTime,
  eventLabel,
  describePoolError,
  createPoolPage,
} from "./el-pool.js";

test("status badge maps every pool state with a tone", () => {
  assert.deepEqual(statusBadge("active"), { text: "可用", tone: "green" });
  assert.deepEqual(statusBadge("exhausted"), { text: "额度耗尽", tone: "amber" });
  assert.deepEqual(statusBadge("invalid"), { text: "已失效", tone: "error" });
  assert.deepEqual(statusBadge("disabled"), { text: "已停用", tone: "muted" });
  assert.deepEqual(statusBadge("something-new"), { text: "something-new", tone: "muted" });
  assert.deepEqual(statusBadge(""), { text: "未知", tone: "muted" });
});

test("quota percent is null-safe and capped at 100", () => {
  assert.equal(quotaPercent(null), null);
  assert.equal(quotaPercent({}), null);
  assert.equal(quotaPercent({ character_count: 10, character_limit: 0 }), null);
  assert.equal(quotaPercent({ character_count: 250, character_limit: 1000 }), 25);
  assert.equal(quotaPercent({ character_count: 1500, character_limit: 1000 }), 100);
});

test("character and voice slot formatting handles missing quota", () => {
  assert.equal(formatCharacters(null), "未查询");
  assert.equal(formatCharacters({ character_count: null, character_limit: 1000 }), "未查询");
  assert.equal(formatCharacters({ character_count: 12345, character_limit: 500000 }), "12,345 / 500,000");
  assert.equal(formatVoiceSlots(null), "未查询");
  assert.equal(formatVoiceSlots({ voice_slots_used: 3, voice_limit: 30 }), "3 / 30");
});

test("event labels translate known events and pass through unknown ones", () => {
  assert.equal(eventLabel("marked_exhausted"), "标记额度耗尽");
  assert.equal(eventLabel("transient_failure"), "瞬态失败");
  assert.equal(eventLabel("future_event"), "future_event");
});

test("time formatting keeps unparsable input verbatim", () => {
  assert.equal(formatTime("not-a-date"), "not-a-date");
  assert.ok(formatTime("2026-10-13T08:00:00+00:00").length > 0);
});

test("pool errors map stable codes to actionable messages", () => {
  assert.equal(describePoolError("elevenlabs_key_invalid", "x"), "ElevenLabs 拒绝了该 API key，请核对后重试。");
  assert.equal(describePoolError("elevenlabs_key_duplicate", "x"), "该 API key 已在号池中。");
  assert.equal(describePoolError("elevenlabs_unreachable", "x"), "无法连接 ElevenLabs，请检查网络或代理后重试。");
  assert.equal(describePoolError("provider_not_reached", "x"), "无法连接 ElevenLabs（mihomo 代理链路异常），请检查代理容器后重试。");
  assert.equal(describePoolError("provider_auth_failed", "x"), "ElevenLabs 校验未通过该 API key，请核对后重试。");
  assert.equal(describePoolError("other_code", "服务器原文"), "服务器原文");
  assert.equal(describePoolError(undefined, undefined), "操作失败。");
});

// —— 页面级用例：stub doc/api（createPoolPage 按 (doc, api) 注入设计）——

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function fakeElement(extra = {}) {
  return { textContent: "", hidden: false, innerHTML: "", disabled: false, ...extra };
}

function createDoc() {
  const errorBox = fakeElement();
  const keysBody = fakeElement({ addEventListener() {} });
  const eventsBody = fakeElement();
  const submitButton = fakeElement({ textContent: "添加" });
  const form = {
    elements: { label: { value: "备注A" }, api_key: { value: "sk-test" } },
    resetCalled: false,
    submitHandler: null,
    reset() {
      this.resetCalled = true;
    },
    addEventListener(type, handler) {
      if (type === "submit") this.submitHandler = handler;
    },
    querySelector() {
      return submitButton;
    },
  };
  const elements = { "error-box": errorBox, "keys-body": keysBody, "events-body": eventsBody, "add-form": form };
  return {
    doc: { getElementById: (id) => elements[id] ?? null },
    errorBox,
    keysBody,
    eventsBody,
    form,
    submitButton,
  };
}

function createApi({ addFails = false } = {}) {
  const calls = [];
  return {
    calls,
    async request(path, options = {}) {
      calls.push({ path, method: options.method || "GET" });
      if (path === "/elevenlabs-pool/keys" && options.method === "POST") {
        if (addFails) {
          throw Object.assign(new Error("boom"), { code: "provider_not_reached" });
        }
        return { key: { id: "k1" } };
      }
      if (path === "/elevenlabs-pool/keys") return { keys: [] };
      if (path.startsWith("/elevenlabs-pool/events")) return { events: [] };
      throw new Error(`unexpected request: ${path}`);
    },
  };
}

test("failed add keeps the error visible after the list refresh", async () => {
  const { doc, errorBox, form } = createDoc();
  const page = createPoolPage(doc, createApi({ addFails: true }));
  await page.refresh();
  form.submitHandler({ preventDefault() {} });
  await flush();
  assert.equal(errorBox.hidden, false);
  assert.equal(errorBox.textContent, "无法连接 ElevenLabs（mihomo 代理链路异常），请检查代理容器后重试。");
  assert.equal(form.resetCalled, false);
});

test("successful add clears the error box and resets the form", async () => {
  const { doc, errorBox, form } = createDoc();
  const page = createPoolPage(doc, createApi());
  await page.refresh();
  errorBox.hidden = false;
  errorBox.textContent = "旧错误";
  form.submitHandler({ preventDefault() {} });
  await flush();
  assert.equal(form.resetCalled, true);
  assert.equal(errorBox.hidden, true);
  assert.equal(errorBox.textContent, "");
});

test("submit button shows pending state in flight and ignores repeat submits", async () => {
  const { doc, form, submitButton } = createDoc();
  const api = createApi();
  createPoolPage(doc, api);
  form.submitHandler({ preventDefault() {} });
  form.submitHandler({ preventDefault() {} });
  assert.equal(submitButton.disabled, true);
  assert.equal(submitButton.textContent, "添加中…");
  assert.equal(api.calls.filter((call) => call.method === "POST").length, 1);
  await flush();
  assert.equal(submitButton.disabled, false);
  assert.equal(submitButton.textContent, "添加");
});
