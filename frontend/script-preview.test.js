import test from "node:test";
import assert from "node:assert/strict";
import {
  confirmWarningText,
  previewRows,
  scriptDraftCounts,
  summarizePreflight,
} from "./script-preview.js";

const SAMPLE = {
  language: "en-US",
  rule_version: "punctuation-v3",
  source_sha256: "abc",
  estimated_risk_threshold_ms: 13000,
  sentences: [
    { sequence: 1, text: "Short one.", text_sha256: "a", is_separator: false, estimated_duration_ms: 1200, estimated_over_limit: false },
    { sequence: 2, text: "---", text_sha256: "b", is_separator: true, estimated_duration_ms: null, estimated_over_limit: false },
    { sequence: 3, text: "A very long sentence.", text_sha256: "c", is_separator: false, estimated_duration_ms: 17000, estimated_over_limit: true },
  ],
};

test("summary counts narratable, separators and over-limit sentences", () => {
  const summary = summarizePreflight(SAMPLE);
  assert.deepEqual(summary, { total: 3, narratable: 2, separators: 1, overLimit: 1 });
  assert.deepEqual(summarizePreflight(null), { total: 0, narratable: 0, separators: 0, overLimit: 0 });
});

test("rows map separator and risky sentence display models", () => {
  const rows = previewRows(SAMPLE);
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[1], { kind: "separator", sequence: 2, text: "---" });
  assert.equal(rows[2].kind, "sentence");
  assert.equal(rows[2].overLimit, true);
  assert.equal(rows[2].estimatedMs, 17000);
  assert.equal(rows[0].overLimit, false);
});

test("confirm warning appears only with over-limit sentences", () => {
  assert.match(confirmWarningText(summarizePreflight(SAMPLE)), /1 句估算配音可能超过 15 秒/);
  assert.equal(confirmWarningText({ total: 2, narratable: 2, separators: 0, overLimit: 0 }), "");
  assert.equal(confirmWarningText(null), "");
});

test("script draft counts characters and non-empty lines", () => {
  assert.deepEqual(scriptDraftCounts(""), { chars: 0, lines: 0 });
  assert.deepEqual(scriptDraftCounts(null), { chars: 0, lines: 0 });
  assert.deepEqual(scriptDraftCounts("第一句。\n\n第二句。\n   \n第三句"), { chars: 18, lines: 3 });
});
