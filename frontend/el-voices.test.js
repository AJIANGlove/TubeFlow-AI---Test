import test from "node:test";
import assert from "node:assert/strict";

import {
  COSYVOICE,
  ELEVENLABS,
  channelDefaultAssignment,
  channelDefaultPatch,
  channelDefaultVoiceReady,
  cloneOutcomeSummary,
  clonedVoiceViews,
  frozenNarrationSummary,
  frozenProvider,
  providerOf,
  ttsProviderLabel,
  validateClonedVoiceUpload,
  voiceAssignment,
} from "./el-voices.js";

test("providerOf defaults to CosyVoice for legacy rows", () => {
  assert.equal(providerOf({}), COSYVOICE);
  assert.equal(providerOf({ tts_provider: "elevenlabs" }), ELEVENLABS);
  assert.equal(providerOf({ default_tts_provider: "elevenlabs" }), ELEVENLABS);
  assert.equal(providerOf({ default_tts_provider: "unknown" }), COSYVOICE);
  assert.match(ttsProviderLabel("elevenlabs"), /ElevenLabs/);
});

test("frozenProvider prefers the immutable snapshot", () => {
  const project = {
    tts_provider: ELEVENLABS,
    snapshot: { tts_provider: COSYVOICE },
    cloned_voice_id: "c1",
  };
  assert.equal(frozenProvider(project), COSYVOICE);
  assert.equal(frozenProvider({ tts_provider: ELEVENLABS }), ELEVENLABS);
});

test("voiceAssignment enforces exactly one voice kind per provider", () => {
  const el = voiceAssignment(ELEVENLABS, { clonedVoiceId: "clone-1" });
  assert.equal(el.ok, true);
  assert.deepEqual(el.patch, { tts_provider: ELEVENLABS, cloned_voice_id: "clone-1" });
  assert.equal(voiceAssignment(ELEVENLABS, {}).ok, false);
  assert.match(voiceAssignment(ELEVENLABS, {}).reason, /克隆音色/);

  const cosy = voiceAssignment(COSYVOICE, { voiceVersionId: "voice-1" });
  assert.equal(cosy.ok, true);
  assert.deepEqual(cosy.patch, { tts_provider: COSYVOICE, voice_version_id: "voice-1" });
  assert.equal(voiceAssignment(COSYVOICE, { clonedVoiceId: "clone-1" }).ok, false);
});

test("channelDefaultPatch binds the channel provider to the chosen voice (design A)", () => {
  const el = channelDefaultPatch(ELEVENLABS, { clonedVoiceId: "c1" });
  assert.equal(el.ok, true);
  assert.deepEqual(el.patch, { default_tts_provider: ELEVENLABS, default_cloned_voice_id: "c1" });
  assert.equal(channelDefaultPatch(ELEVENLABS, {}).ok, false);
  assert.match(channelDefaultPatch(ELEVENLABS, {}).reason, /克隆音色/);

  const cosy = channelDefaultPatch(COSYVOICE, { voiceVersionId: "v1" });
  assert.deepEqual(cosy.patch, { default_tts_provider: COSYVOICE, default_voice_version_id: "v1" });
  assert.equal(channelDefaultPatch(COSYVOICE, {}).ok, false);
  assert.equal(channelDefaultPatch("legacy", { voiceVersionId: "v1" }).patch.default_tts_provider, COSYVOICE);
});

test("channelDefaultVoiceReady tracks the default voice of the selected provider", () => {
  assert.equal(channelDefaultVoiceReady(null), false);
  assert.equal(channelDefaultVoiceReady({ default_voice_version_id: "v1" }), true);
  assert.equal(channelDefaultVoiceReady({}), false);
  assert.equal(
    channelDefaultVoiceReady({ default_tts_provider: ELEVENLABS, default_cloned_voice_id: "c1" }),
    true,
  );
  assert.equal(
    channelDefaultVoiceReady({ default_tts_provider: ELEVENLABS, default_voice_version_id: "v1" }),
    false,
  );
  assert.deepEqual(
    channelDefaultAssignment({ default_tts_provider: ELEVENLABS, default_cloned_voice_id: "c1" }),
    { provider: ELEVENLABS, clonedVoiceId: "c1" },
  );
  assert.deepEqual(channelDefaultAssignment({ default_voice_version_id: "v1" }), {
    provider: COSYVOICE,
    clonedVoiceId: null,
  });
});

test("clonedVoiceViews marks the channel default as protected", () => {
  const views = clonedVoiceViews(
    [
      { id: "c1", name: "旁白", remark: "备注", source_url: "/x", source_size_bytes: 10 },
      { id: "c2", name: "配角", source_url: "/y" },
      { id: "c3", name: "旧音色" },
    ],
    { defaultClonedVoiceId: "c1", protectedIds: ["c2"] },
  );
  assert.equal(views[0].isDefault, true);
  assert.equal(views[0].canDelete, false);
  assert.match(views[0].protectionReason, /频道默认音色/);
  assert.equal(views[1].protected, true);
  assert.equal(views[1].canDelete, false);
  assert.match(views[1].protectionReason, /冻结/);
  assert.equal(views[2].canDelete, true);
  assert.equal(views[2].protectionReason, "");
  assert.equal(views[2].sizeBytes, 0);
  assert.deepEqual(clonedVoiceViews(null), []);
});

test("validateClonedVoiceUpload mirrors backend requirements", () => {
  assert.equal(validateClonedVoiceUpload({ name: "旁白", file: { name: "a.wav", size: 12 } }).ok, true);
  assert.match(validateClonedVoiceUpload({ name: " ", file: { name: "a.wav", size: 1 } }).reason, /名称/);
  assert.match(validateClonedVoiceUpload({ name: "旁白", file: null }).reason, /源音频/);
  assert.match(
    validateClonedVoiceUpload({ name: "旁白", file: { name: "a.wav", size: 0 } }).reason,
    /为空/,
  );
});

test("cloneOutcomeSummary reports partial failures honestly", () => {
  assert.match(cloneOutcomeSummary([]).detail, /没有可用的 active key/);
  const partial = cloneOutcomeSummary([
    { key_id: "k1", label: "主号", ok: true, el_voice_id: "el-1" },
    { key_id: "k2", label: "备号", ok: false, error_code: "provider_rejected" },
  ]);
  assert.equal(partial.okCount, 1);
  assert.equal(partial.failedCount, 1);
  assert.deepEqual(partial.failedLabels, ["备号"]);
  assert.match(partial.detail, /备号（provider_rejected）/);
  assert.match(cloneOutcomeSummary([{ label: "主号", ok: true }]).detail, /1 个账号/);
});

test("frozenNarrationSummary shows the frozen provider and voice", () => {
  assert.deepEqual(frozenNarrationSummary({}), {
    provider: COSYVOICE,
    providerLabel: ttsProviderLabel(COSYVOICE),
    voiceName: "公共音色",
    frozen: false,
  });
  const summary = frozenNarrationSummary({
    snapshot: { tts_provider: ELEVENLABS, cloned_voice_name: "旁白克隆" },
  });
  assert.equal(summary.provider, ELEVENLABS);
  assert.equal(summary.voiceName, "旁白克隆");
  assert.equal(summary.frozen, true);
});
