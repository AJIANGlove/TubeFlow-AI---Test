import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  COVER_TEMPLATE_NAME_LIMIT,
  COVER_TEMPLATE_PROMPT_LIMIT,
  clipTemplateName,
  clipTemplatePrompt,
  countTemplatePrompt,
  coverMoreSettingsMarkup,
  coverTemplateCollectionModel,
  coverTemplateItemModel,
  coverTemplateMetaText,
  coverTemplatesMarkup,
  formatFileSize,
  formatSavedClock,
  moveTemplateItem,
  templateDraftChanged,
  templateDraftFromItem,
  templateSaveStatus,
} from "./cover-templates.js";
import { coverMoreSettingsKey, coverMoreSettingsOpen } from "./cover-view.js";

const item = (overrides = {}) => ({
  item_id: "id-1",
  name: "模板 1",
  position: 0,
  prompt: "提示词",
  cover: null,
  created_at: "2026-10-27T02:00:00Z",
  updated_at: "2026-10-27T02:30:00Z",
  ...overrides,
});

test("file size keeps the agreed compact format", () => {
  assert.equal(formatFileSize(0), "0B");
  assert.equal(formatFileSize(700), "700B");
  assert.equal(formatFileSize(86 * 1024), "86KB");
  assert.equal(formatFileSize(1.4 * 1024 * 1024), "1.4MB");
  assert.equal(formatFileSize(12 * 1024 * 1024), "12MB");
});

test("meta text shows original dimensions and original file size only", () => {
  assert.equal(
    coverTemplateMetaText({ width: 1920, height: 1080, byte_size: 1468006 }),
    "1920×1080 · 1.4MB",
  );
  assert.equal(coverTemplateMetaText({}), "");
  assert.equal(coverTemplateMetaText(null), "");
});

test("item model tolerates empty cover and missing fields", () => {
  const model = coverTemplateItemModel(item());
  assert.equal(model.id, "id-1");
  assert.equal(model.name, "模板 1");
  assert.equal(model.cover, null);
  assert.equal(model.prompt, "提示词");
  assert.equal(coverTemplateItemModel({}).id, "");
});

test("collection model sorts by position and tolerates empty payload", () => {
  const model = coverTemplateCollectionModel({
    items: [item({ item_id: "b", position: 1 }), item({ item_id: "a", position: 0 })],
  });
  assert.deepEqual(model.items.map((row) => row.id), ["a", "b"]);
  assert.deepEqual(coverTemplateCollectionModel(null).items, []);
});

test("prompt and name limits count code points", () => {
  assert.equal(countTemplatePrompt("🎬"), 1);
  assert.equal(clipTemplatePrompt("🎬🎬🎬", 2), "🎬🎬");
  assert.equal(clipTemplatePrompt("x".repeat(COVER_TEMPLATE_PROMPT_LIMIT + 2)).length, COVER_TEMPLATE_PROMPT_LIMIT);
  assert.equal(clipTemplateName("🎬".repeat(COVER_TEMPLATE_NAME_LIMIT + 1)).length, COVER_TEMPLATE_NAME_LIMIT * 2);
  assert.equal(Array.from(clipTemplateName("🎬".repeat(COVER_TEMPLATE_NAME_LIMIT + 1))).length, COVER_TEMPLATE_NAME_LIMIT);
});

test("draft comparison detects name and prompt changes", () => {
  const model = coverTemplateItemModel(item());
  assert.equal(templateDraftChanged(model, templateDraftFromItem(model)), false);
  assert.equal(templateDraftChanged(model, { name: "新名字", prompt: "提示词" }), true);
  assert.equal(templateDraftChanged(model, { name: "模板 1", prompt: "新提示词" }), true);
});

test("moveTemplateItem reorders before and after the target", () => {
  const items = [{ id: "a", position: 0 }, { id: "b", position: 1 }, { id: "c", position: 2 }];
  assert.deepEqual(moveTemplateItem(items, "c", "a", false).map((row) => row.id), ["c", "a", "b"]);
  assert.deepEqual(moveTemplateItem(items, "a", "c", true).map((row) => row.id), ["b", "c", "a"]);
  assert.deepEqual(moveTemplateItem(items, "a", "a", false).map((row) => row.id), ["a", "b", "c"]);
  assert.deepEqual(moveTemplateItem(items, "x", "a", false).map((row) => row.id), ["a", "b", "c"]);
});

test("save status prioritises error over saving over dirty over saved", () => {
  assert.deepEqual(templateSaveStatus({ error: "boom", saving: true, dirty: true, savedAt: "10:00" }), {
    tone: "error",
    text: "保存失败，点击重试",
  });
  assert.deepEqual(templateSaveStatus({ saving: true, dirty: true, savedAt: "10:00" }), {
    tone: "saving",
    text: "保存中…",
  });
  assert.deepEqual(templateSaveStatus({ dirty: true, savedAt: "10:00" }), {
    tone: "dirty",
    text: "未保存",
  });
  assert.deepEqual(templateSaveStatus({ savedAt: "10:00" }), {
    tone: "saved",
    text: "已保存 10:00",
  });
  assert.deepEqual(templateSaveStatus({}), { tone: "idle", text: "未保存" });
});

test("saved clock is zero padded local time", () => {
  assert.equal(formatSavedClock(new Date(2026, 9, 27, 9, 5, 0)), "09:05");
  assert.equal(formatSavedClock("not a date"), "");
});

test("cards expose name, save, copy, upload, delete and drag affordances", () => {
  const model = coverTemplateCollectionModel({
    items: [
      item({
        item_id: "a",
        name: "电商 <大促>",
        prompt: "<script>alert(1)</script>",
        cover: {
          width: 1920,
          height: 1080,
          byte_size: 1024,
          media_url_preview: "/preview?a=1&b=2",
          media_url_original: "/original",
          sha256: "sha",
        },
      }),
    ],
  });
  const html = coverTemplatesMarkup(model, {}, {}, { a: templateSaveStatus({ dirty: true }) });
  assert.match(html, /data-template-name="a"/);
  assert.match(html, /电商 &lt;大促&gt;/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>alert/);
  assert.match(html, /data-template-save="a"/);
  assert.match(html, /data-template-copy="a"/);
  assert.match(html, /data-template-upload="a"/);
  assert.match(html, /data-template-copy-image="a"/);
  assert.match(html, /data-template-delete="a"/);
  assert.match(html, /data-template-drag="a"/);
  assert.match(html, /未保存/);
  assert.match(html, /复制图片/);
  assert.match(html, /1920×1080 · 1KB/);
  assert.match(html, /data-protected-image="\/preview\?a=1&amp;b=2"/);
  assert.doesNotMatch(html, /自动保存/);
});

test("empty list shows onboarding and the add card", () => {
  const html = coverTemplatesMarkup(coverTemplateCollectionModel(null));
  assert.match(html, /暂无模板/);
  assert.match(html, /新增模板/);
  assert.match(html, /id="coverTemplateAdd"/);
});

test("upload is disabled when a template has no cover", () => {
  const html = coverTemplatesMarkup(coverTemplateCollectionModel({ items: [item()] }));
  assert.match(html, /data-template-copy-image="id-1" disabled/);
  assert.match(html, /上传封面/);
});

test("more settings is collapsed by default and expanded only when asked", () => {
  const closed = coverMoreSettingsMarkup({ open: false, body: "<p>body</p>" });
  assert.match(closed, /aria-expanded="false"/);
  assert.match(closed, /id="coverMoreBody" hidden/);
  const open = coverMoreSettingsMarkup({ open: true, body: "<p>body</p>" });
  assert.match(open, /aria-expanded="true"/);
  assert.doesNotMatch(open, /id="coverMoreBody" hidden/);
});

test("collapse state storage key and parser are project scoped", () => {
  assert.equal(coverMoreSettingsKey("p-1"), "tubeflow:cover-more-settings:p-1");
  assert.equal(coverMoreSettingsOpen("open"), true);
  assert.equal(coverMoreSettingsOpen("closed"), false);
  assert.equal(coverMoreSettingsOpen(""), false);
  assert.equal(coverMoreSettingsOpen(null), false);
});

test("cover page helpers used by render and bind are all defined in app.js", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  for (const name of [
    "readCoverMoreSetting",
    "writeCoverMoreSetting",
    "setCoverMoreOpen",
    "openCoverMoreSettings",
    "bindCoverMoreSettings",
    "coverTemplateBlockMarkup",
    "bindCoverTemplateEvents",
    "ensureCoverTemplateEditsSaved",
    "warmCoverTemplateOriginal",
  ]) {
    assert.match(source, new RegExp(`function ${name}\\(`), `${name} must be defined`);
  }
});
