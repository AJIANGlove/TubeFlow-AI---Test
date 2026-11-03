import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { srtDownloadFilename } from "./api-client.js";

const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");

test("SRT download filename sanitizes the project title with a stable fallback", () => {
  assert.equal(srtDownloadFilename("我的项目"), "我的项目-字幕.srt");
  assert.equal(srtDownloadFilename("a/b:c"), "a_b_c-字幕.srt");
  assert.equal(srtDownloadFilename("  项目...  "), "项目-字幕.srt");
  assert.equal(srtDownloadFilename("   "), "composition.srt");
  assert.equal(srtDownloadFilename(""), "composition.srt");
  assert.equal(srtDownloadFilename(null), "composition.srt");
  assert.equal(srtDownloadFilename(undefined), "composition.srt");
  assert.equal(Array.from(srtDownloadFilename("x".repeat(200))).length, 87);
  assert.ok(srtDownloadFilename("x".repeat(200)).endsWith("-字幕.srt"));
});

test("SRT card exposes a disabled download button beside the title", () => {
  const start = source.indexOf('composition?.srt_asset_id && allows(composition, "VIEW_MEDIA") ? `<section');
  const end = source.indexOf('<section class="panel"><div class="panel-head"><div><h2>正式导出', start);
  assert.ok(start > -1 && end > start, "composition SRT card template must exist");
  const card = source.slice(start, end);
  assert.match(card, /<button id="downloadSrtPreview" class="ghost-button compact" type="button" disabled/);
  assert.match(card, />下载 SRT<\/button>/);
});

test("SRT card reuses the publish control so STEP 09 can mark publication without scrolling up", () => {
  const start = source.indexOf('composition?.srt_asset_id && allows(composition, "VIEW_MEDIA") ? `<section');
  const end = source.indexOf('<section class="panel"><div class="panel-head"><div><h2>正式导出', start);
  assert.ok(start > -1 && end > start, "composition SRT card template must exist");
  const card = source.slice(start, end);
  // 与头部同源的发布控件，但 id 必须区分开：同页重复 id 会让后出现的入口绑定不到点击。
  assert.match(card, /class="panel-head-actions"/);
  assert.match(card, /publicationHeaderMarkup\(state\.project, canPublish, "publishProjectFromSrt"\)/);
  assert.match(card, /<button id="downloadSrtPreview"/);
  assert.doesNotMatch(card, /publishProjectFromStage/);
  assert.match(source, /\$\("#publishProjectFromSrt"\)\?\.addEventListener\("click", \(\) => void publishCurrentProject\(\)\)/);
});

test("SRT download reuses the preview blob without a third protected fetch", () => {
  assert.equal((source.match(/api\.blob\(/g) || []).length, 2);
  assert.match(source, /srtPreviewBlob = blob;/);
  const start = source.indexOf("function downloadCurrentSrt(");
  assert.ok(start > -1, "downloadCurrentSrt must exist");
  const end = source.indexOf("function historyItems(", start);
  assert.ok(end > start);
  const fn = source.slice(start, end);
  assert.match(fn, /keepObjectUrl\(blob\)/);
  assert.match(fn, /anchor\.download = srtDownloadFilename\(title\)/);
  assert.doesNotMatch(fn, /api\.blob\(/);
  assert.match(source, /downloadCurrentSrt\(srtPreviewBlob, projectName\)/);
  assert.match(source, /if \(!srtPreviewBlob\)/);
});

test("SRT button is enabled only by the render that loaded the subtitle", () => {
  assert.match(source, /if \(epoch !== state\.renderEpoch \|\| !srtDownloadButton\?\.isConnected\) return;/);
  assert.match(source, /const srtDownloadButton = \$\("#downloadSrtPreview"\);/);
  assert.doesNotMatch(source, /\$\("#downloadSrtPreview"\)\.disabled = false/);
});
