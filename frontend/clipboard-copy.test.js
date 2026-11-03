import test from "node:test";
import assert from "node:assert/strict";

import { hasCopyableText, scriptCopyText } from "./clipboard-copy.js";

test("frozen project copies the immutable snapshot instead of the draft or editor", () => {
  const project = { script: "draft-script", snapshot: { script: "frozen-script" } };
  assert.equal(scriptCopyText(project, "unsaved-editor-value"), "frozen-script");
});

test("frozen project with an empty snapshot stays empty and never falls back", () => {
  const project = { script: "stale-draft", snapshot: { script: "" } };
  assert.equal(scriptCopyText(project, "stale-draft"), "");
});

test("draft project copies the live editor value, including empty and unsaved values", () => {
  assert.equal(scriptCopyText({ script: "saved" }, "unsaved"), "unsaved");
  assert.equal(scriptCopyText({ script: "saved" }, ""), "");
  assert.equal(scriptCopyText({ script: "saved" }, "第一段\n\n---\n\n第二段"), "第一段\n\n---\n\n第二段");
});

test("draft project falls back to the saved script when the editor is absent", () => {
  assert.equal(scriptCopyText({ script: "saved" }, undefined), "saved");
  assert.equal(scriptCopyText({ script: "saved" }, null), "saved");
  assert.equal(scriptCopyText({}, undefined), "");
  assert.equal(scriptCopyText(null, undefined), "");
});

test("script text is copied verbatim, keeping blank lines and separator lines", () => {
  const text = "第一段\n\n---\n\n第二段\n";
  assert.equal(scriptCopyText({ script: text }, text), text);
});

test("hasCopyableText treats blank strings as empty but keeps separators and zero", () => {
  assert.equal(hasCopyableText(""), false);
  assert.equal(hasCopyableText(" \n\t "), false);
  assert.equal(hasCopyableText(null), false);
  assert.equal(hasCopyableText(undefined), false);
  assert.equal(hasCopyableText(0), false);
  assert.equal(hasCopyableText("---"), true);
  assert.equal(hasCopyableText("\n\n---\n\n"), true);
  assert.equal(hasCopyableText("0"), true);
  assert.equal(hasCopyableText("第一段"), true);
});
