import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  compatibleLocaleVoicePatch,
  filterVoices,
  paginateVoices,
  shortStableId,
  voiceFacetValues,
  voiceIdentity,
  voiceSupportsLocale,
} from "./voice-library.js";

const voices = [
  {
    id: "11111111-2222-3333-4444-555555555555",
    voice_id: "speaker_alpha_0001",
    version: 1,
    display_name: "暖阳",
    supported_locales: ["en-US", "ja-JP"],
    gender: "female",
    age_band: "young",
    accent: "standard",
    accent_zh: "标准",
    tags: ["温暖", "叙事"],
    description: "适合纪录片旁白",
  },
  {
    id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    voice_id: "speaker_beta_0002",
    version: 2,
    display_name: "远山",
    supported_locales: ["fr-FR"],
    gender: "male",
    age_band: "middle",
    accent: "regional",
    accent_zh: "地域",
    tags: ["沉稳"],
    description: "新闻播报",
  },
];

test("voice filtering combines search and fixed metadata facets", () => {
  assert.deepEqual(filterVoices(voices, { locale: "ja-JP" }).map((voice) => voice.id), [voices[0].id]);
  assert.deepEqual(filterVoices(voices, { query: "speaker_beta", gender: "male" }).map((voice) => voice.id), [voices[1].id]);
  assert.deepEqual(filterVoices(voices, { query: "纪录片", tag: "叙事", accent: "standard" }).map((voice) => voice.id), [voices[0].id]);
  assert.equal(voiceSupportsLocale(voices[1], "en-US"), false);
});

test("402 voices are rendered in bounded 24-card pages", () => {
  const catalog = Array.from({ length: 402 }, (_, index) => ({ id: `voice-${index}` }));
  const first = paginateVoices(catalog, 1);
  const last = paginateVoices(catalog, 17);
  assert.equal(first.items.length, 24);
  assert.equal(first.pages, 17);
  assert.equal(last.items.length, 18);
  assert.equal(paginateVoices(catalog, 99).page, 17);
});

test("voice cards expose stable version and short identifiers", () => {
  assert.equal(shortStableId(voices[0].id), "11111111");
  assert.match(voiceIdentity(voices[0]), /v1 · voice speaker_alph · UUID 11111111/);
  assert.deepEqual(new Set(voiceFacetValues(voices, "tags")), new Set(["叙事", "沉稳", "温暖"]));
});

test("locale and replacement voice are produced as one compatible project patch", () => {
  assert.deepEqual(compatibleLocaleVoicePatch(voices, "fr-FR", voices[1].id), {
    locale: "fr-FR",
    voice_version_id: voices[1].id,
  });
  assert.equal(compatibleLocaleVoicePatch(voices, "fr-FR", voices[0].id), null);
});

test("project creation submits the controller selection instead of a paginated DOM radio", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  // §23.1：创建向导新增配音源参数（第三个入参返回当前源），音色仍由控制器选择决定。
  assert.match(source, /handleCreateProject\(event, voiceController, \(\) => createProvider\)/);
  assert.match(source, /const voiceVersionId = voiceController\?\.getSelectedId\(\) \|\| null/);
  assert.doesNotMatch(source, /const voiceVersionId = data\.get\("voice_version_id"\)/);
  // 提交前按配音源做二选一校验，EL 源用克隆音色、CosyVoice 源用公共音色。
  assert.match(source, /voiceAssignment\(provider, \{ voiceVersionId, clonedVoiceId \}\)/);
  assert.match(source, /tts_provider: assignment\.patch\.tts_provider/);
  assert.match(source, /cloned_voice_id: assignment\.patch\.cloned_voice_id \|\| null/);
});

test("all four selection surfaces reuse the bounded lazy media component", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.equal((source.match(/mountVoiceLibrary\(/g) || []).length, 5);
  assert.match(source, /loading="lazy" decoding="async" data-protected-image/);
  assert.match(source, /channel_default_voice_version_id/);
  assert.doesNotMatch(source, /管理员导入公共音色/);
});
