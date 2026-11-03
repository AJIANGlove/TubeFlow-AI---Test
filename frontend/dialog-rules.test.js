import test from "node:test";
import assert from "node:assert/strict";
import { passwordPairIssue } from "./dialog-rules.js";

test("password pair requires at least 8 characters", () => {
  assert.equal(passwordPairIssue("", ""), "too_short");
  assert.equal(passwordPairIssue("1234567", "1234567"), "too_short");
  assert.equal(passwordPairIssue(null, null), "too_short");
});

test("password pair reports mismatch only after length passes", () => {
  assert.equal(passwordPairIssue("12345678", "12345679"), "mismatch");
  assert.equal(passwordPairIssue("12345678", ""), "mismatch");
  assert.equal(passwordPairIssue("1234567", "完全不同"), "too_short");
});

test("password pair accepts equal long-enough input", () => {
  assert.equal(passwordPairIssue("12345678", "12345678"), "");
  assert.equal(passwordPairIssue("含中文密码九个字符啊", "含中文密码九个字符啊"), "");
});
