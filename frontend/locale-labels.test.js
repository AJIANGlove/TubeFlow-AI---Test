import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LOCALES, localeLabel, localeShortLabel, localeShortListText } from "./locale-labels.js";

const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8").replaceAll("\r\n", "\n");

// 首期六种语言的唯一口径：长名带地区括注，短名不带地区与代码。
const EXPECTED = [
  ["en-US", "英语（美国）", "英语"],
  ["ja-JP", "日语（日本）", "日语"],
  ["ko-KR", "韩语（韩国）", "韩语"],
  ["es-ES", "西班牙语（西班牙）", "西班牙语"],
  ["fr-FR", "法语（法国）", "法语"],
  ["pt-BR", "葡萄牙语（巴西）", "葡萄牙语"],
];

test("six launch locales keep the stable dropdown order", () => {
  assert.deepEqual(LOCALES, EXPECTED.map(([code]) => code));
  assert.equal(LOCALES[0], "en-US");
  assert.equal(Object.isFrozen(LOCALES), true);
});

test("long labels carry the region while short labels stay compact", () => {
  for (const [code, label, short] of EXPECTED) {
    assert.equal(localeLabel(code), label);
    assert.equal(localeShortLabel(code), short);
    assert.notEqual(label, short);
    assert.doesNotMatch(short, /[（）()\-A-Za-z]/);
  }
  // 长短名不混用：任何短名都不等于任一长名。
  const longLabels = new Set(EXPECTED.map(([, label]) => label));
  for (const [, , short] of EXPECTED) assert.equal(longLabels.has(short), false);
});

test("unknown or empty codes fall back to the original value", () => {
  assert.equal(localeLabel("de-DE"), "de-DE");
  assert.equal(localeLabel("es-419"), "es-419");
  assert.equal(localeLabel(" en-US "), "英语（美国）");
  assert.equal(localeShortLabel("de-DE"), "de-DE");
  assert.equal(localeLabel(""), "");
  assert.equal(localeLabel(null), "");
  assert.equal(localeLabel(undefined), "");
  assert.equal(localeShortLabel(undefined), "");
});

test("voice card language list is short, ordered and deduplicated", () => {
  assert.equal(localeShortListText(["en-US", "ja-JP"]), "英语 · 日语");
  assert.equal(localeShortListText(["ja-JP", "en-US"]), "日语 · 英语");
  assert.equal(localeShortListText(["en-US", "en-US"]), "英语");
  assert.equal(localeShortListText(["en-US", "de-DE"]), "英语 · de-DE");
  assert.equal(localeShortListText(["en-US", null, ""]), "英语");
  assert.equal(localeShortListText([]), "");
  assert.equal(localeShortListText(null), "");
  assert.equal(localeShortListText(["en-US"], " / "), "英语");
});

test("app.js renders every locale surface through the label module", () => {
  assert.match(appSource, /import \{ LOCALES, localeLabel, localeShortListText \} from "\.\/frontend\/locale-labels\.js";/);
  assert.doesNotMatch(appSource, /const LOCALES = \[/);
  // 音色库语言筛选：四个挂载面共用同一处渲染。
  assert.match(appSource, /voiceSelectOptions\(LOCALES, requiredLocale, localeLabel\)/);
  // 音色卡「支持语言」短名。
  assert.match(appSource, /const localeText = localeShortListText\(voice\.supported_locales\);/);
  assert.match(appSource, /<span class="voice-locales" title="\$\{escapeHtml\(localeText\)\}">\$\{escapeHtml\(localeText\)\}<\/span>/);
  // 项目卡片 meta。
  assert.match(appSource, /<div class="project-meta"><span>\$\{escapeHtml\(localeLabel\(project\.locale\)\)\}<\/span>/);
  // 新建项目与 STEP 01 的 option 文本。
  assert.match(appSource, /<option value="\$\{locale\}">\$\{escapeHtml\(localeLabel\(locale\)\)\}<\/option>/);
  assert.match(appSource, /<option value="\$\{locale\}" \$\{project\.locale === locale \? "selected" : ""\}>\$\{escapeHtml\(localeLabel\(locale\)\)\}<\/option>/);
  // 两处 textContent 提示（不转义）。
  assert.match(appSource, /`频道默认音色不支持 \$\{localeLabel\(locale\)\}，请从下方 \$\{compatibleCount\} 个兼容音色中手动选择。`/);
  assert.match(appSource, /`系统固定音色库没有支持 \$\{localeLabel\(locale\)\} 的音色，请更换语言。`/);
  assert.match(appSource, /`当前音色不支持 \$\{localeLabel\(requestedLocale\)\}，请从下方 \$\{compatibleCount\} 个兼容音色中手动选择后再保存语言。`/);
  assert.match(appSource, /`系统固定音色库没有支持 \$\{localeLabel\(requestedLocale\)\} 的音色，请恢复原语言。`/);
  // STEP 05 说明。
  assert.match(appSource, /仅显示支持 \$\{escapeHtml\(localeLabel\(state\.project\.locale\)\)\} 的系统固定不可变版本/);
});

test("locale option values stay raw codes and are never the Chinese label", () => {
  assert.equal((appSource.match(/<option value="\$\{locale\}"/g) || []).length, 2);
  assert.doesNotMatch(appSource, /<option value="\$\{locale\}"[^>]*>\$\{locale\}</);
  assert.doesNotMatch(appSource, /<option value="\$\{escapeHtml\(localeLabel/);
  // 提交值仍是代码：创建项目、项目补丁与音色兼容判定都不经过展示层。
  assert.match(appSource, /locale: data\.get\("locale"\)/);
  assert.match(appSource, /requiredLocale: project\.locale/);
});

test("build and static checks register the new module", () => {
  const buildSource = readFileSync(new URL("../scripts/build.mjs", import.meta.url), "utf8");
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8").replace(/^\uFEFF/, ""));
  assert.match(buildSource, /"frontend\/locale-labels\.js"/);
  assert.match(pkg.scripts.lint, /node --check frontend\/locale-labels\.js/);
  assert.match(pkg.scripts.typecheck, /node --check frontend\/locale-labels\.js/);
});
