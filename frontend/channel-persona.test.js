import test from "node:test";
import assert from "node:assert/strict";
import {
  MAX_DEFAULT_CHARACTER_IMAGES,
  normalizeDefaultCharacterIds,
  resolveDefaultCharacterImages,
  toggleDefaultCharacterId,
} from "./channel-persona.js";

test("toggle appends a new id to the end", () => {
  assert.deepEqual(toggleDefaultCharacterId([], "a"), { ok: true, ids: ["a"] });
  assert.deepEqual(toggleDefaultCharacterId(["a"], "b"), { ok: true, ids: ["a", "b"] });
  assert.deepEqual(toggleDefaultCharacterId(["a", "b"], "c"), { ok: true, ids: ["a", "b", "c"] });
});

test("toggle removes an already selected id", () => {
  assert.deepEqual(toggleDefaultCharacterId(["a", "b", "c"], "b"), { ok: true, ids: ["a", "c"] });
  assert.deepEqual(toggleDefaultCharacterId(["a"], "a"), { ok: true, ids: [] });
});

test("toggle rejects beyond the limit but still allows removal", () => {
  const full = ["a", "b", "c"];
  assert.equal(full.length, MAX_DEFAULT_CHARACTER_IMAGES);
  assert.deepEqual(toggleDefaultCharacterId(full, "d"), { ok: false, reason: "limit" });
  assert.deepEqual(toggleDefaultCharacterId(full, "a"), { ok: true, ids: ["b", "c"] });
});

test("toggle tolerates malformed input without throwing", () => {
  assert.deepEqual(toggleDefaultCharacterId(null, "a"), { ok: true, ids: ["a"] });
  assert.deepEqual(toggleDefaultCharacterId(undefined, "a"), { ok: true, ids: ["a"] });
  assert.deepEqual(toggleDefaultCharacterId(["", null, "a"], "b"), { ok: true, ids: ["a", "b"] });
  assert.deepEqual(toggleDefaultCharacterId(["a"], ""), { ok: false, reason: "invalid" });
  assert.deepEqual(toggleDefaultCharacterId(["a"], null), { ok: false, reason: "invalid" });
});

test("normalize filters unavailable ids, dedupes in order and caps at the limit", () => {
  assert.deepEqual(
    normalizeDefaultCharacterIds(["b", "a", "b", "x", "c", "d"], ["a", "b", "c", "d"]),
    ["b", "a", "c"],
  );
  assert.deepEqual(normalizeDefaultCharacterIds(["x", "y"], ["a"]), []);
});

test("normalize handles empty or malformed input", () => {
  assert.deepEqual(normalizeDefaultCharacterIds(null, ["a"]), []);
  assert.deepEqual(normalizeDefaultCharacterIds("a", ["a"]), []);
  assert.deepEqual(normalizeDefaultCharacterIds(["a", "a", "a"], ["a"]), ["a"]);
  assert.deepEqual(normalizeDefaultCharacterIds(["a", "", null, "b"], null), ["a", "b"]);
});

test("resolve maps ids to image objects in order and skips missing ones", () => {
  const images = [{ id: "a", width: 1 }, { id: "b", width: 2 }, { id: "c", width: 3 }];
  assert.deepEqual(
    resolveDefaultCharacterImages({ default_character_image_ids: ["c", "missing", "a"] }, images),
    [{ id: "c", width: 3 }, { id: "a", width: 1 }],
  );
  assert.deepEqual(resolveDefaultCharacterImages({}, images), []);
  assert.deepEqual(resolveDefaultCharacterImages(null, images), []);
  assert.deepEqual(resolveDefaultCharacterImages({ default_character_image_ids: ["a"] }, null), []);
});
