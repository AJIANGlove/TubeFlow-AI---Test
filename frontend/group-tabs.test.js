import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";


test("group tab strip re-binds every tab and keeps the active group visible after re-render", () => {
  const source = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  const start = source.indexOf("function bindGroupTabs(");
  const end = source.indexOf("function narrationAudioFacts", start);
  const bindSource = source.slice(start, end);

  // 点击分组标签后整个工作区树重建，.group-tabs 的 scrollLeft 归零；
  // 渲染后必须把激活标签滚回可视区，否则点过分组 20 后滑块回到起点，
  // 用户要预览分组 21 时只能重新滚动。
  assert.match(bindSource, /\$\$\("\[data-select-group\]"\)\.forEach/);
  assert.match(bindSource, /\$\("\.group-tabs \.group-tab\.is-active"\)\?\.scrollIntoView\(\{ inline: "center", block: "nearest" \}\)/);
});
