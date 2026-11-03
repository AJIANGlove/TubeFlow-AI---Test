import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  compositionDefectNotice,
  defectWindows,
  deliveredWithDefect,
  groupVideoDefectNotice,
} from "./video-defect.js";

const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8").replaceAll("\r\n", "\n");

function productionFunction(name) {
  const start = appSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing production function ${name}`);
  const end = appSource.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated production function ${name}`);
  return appSource.slice(start, end + 2);
}

const defectVideo = {
  delivered_with_defect: true,
  content_defect: {
    code: "video_content_black_region",
    findings: [
      { start_second: 0, end_second: 4, bbox_columns: [0, 1279], bbox_rows: [0, 719], area_fraction: 0.97 },
    ],
  },
};

test("defect notice only renders for backend-marked defect delivery", () => {
  assert.equal(deliveredWithDefect(defectVideo), true);
  assert.equal(deliveredWithDefect({ delivered_with_defect: false }), false);
  assert.equal(deliveredWithDefect({}), false);
  assert.equal(deliveredWithDefect(null), false);
  assert.equal(groupVideoDefectNotice({}), "");
  assert.equal(groupVideoDefectNotice({ content_defect: defectVideo.content_defect }), "");
});

test("defect notice states time windows from content_defect findings verbatim", () => {
  assert.equal(
    groupVideoDefectNotice(defectVideo),
    "该组视频存在画面缺陷：0.0s–4.0s 大面积黑屏（已自动修复 6 次未果，按你的决策标注交付）；可在历史版本重生成或切换。",
  );
  // 多个时段按序拼接；无 findings 时不拼时段。
  const multi = {
    delivered_with_defect: true,
    content_defect: { code: "video_content_black_region", findings: [
      { start_second: 0, end_second: 4 },
      { start_second: 8.25, end_second: 9.5 },
    ] },
  };
  assert.match(groupVideoDefectNotice(multi), /0\.0s–4\.0s、8\.3s–9\.5s 大面积黑屏/);
  assert.equal(
    groupVideoDefectNotice({ delivered_with_defect: true, content_defect: null }),
    "该组视频存在画面缺陷（已自动修复 6 次未果，按你的决策标注交付）；可在历史版本重生成或切换。",
  );
});

test("defect windows skip non-numeric findings instead of fabricating times", () => {
  assert.deepEqual(defectWindows(defectVideo), ["0.0s–4.0s"]);
  assert.deepEqual(
    defectWindows({ content_defect: { findings: [{ start_second: "x" }, { end_second: 2 }, null] } }),
    [],
  );
  assert.deepEqual(defectWindows(null), []);
});

test("composition defect notice summarizes defect groups with sequences", () => {
  assert.equal(compositionDefectNotice(null), "");
  assert.equal(compositionDefectNotice({ auto_pipeline: { defect_groups: [] } }), "");
  assert.equal(compositionDefectNotice({ auto_pipeline: null }), "");
  assert.equal(
    compositionDefectNotice({ auto_pipeline: { defect_groups: [
      { group_sequence: 2, error_code: "video_content_black_region", findings: [{ start_second: 0, end_second: 4 }] },
    ] } }),
    "本片包含 1 个标注缺陷分组（第 2 组）。",
  );
  assert.equal(
    compositionDefectNotice({ auto_pipeline: { defect_groups: [
      { group_sequence: 1, error_code: "video_content_black_region", findings: [] },
      { group_sequence: 5, error_code: "video_content_black_region", findings: [] },
    ] } }),
    "本片包含 2 个标注缺陷分组（第 1、5 组）。",
  );
  // 非法组号被过滤；全部非法时只报数量，不拼组号。
  assert.equal(
    compositionDefectNotice({ auto_pipeline: { defect_groups: [{ group_sequence: "x" }] } }),
    "本片包含 1 个标注缺陷分组。",
  );
});

test("step 08 shows the defect warning above the current group video player", () => {
  const stage = productionFunction("renderGroupVideoStage");
  assert.match(stage, /const defectNotice = groupVideoDefectNotice\(video\);/);
  assert.match(stage, /画面缺陷已标注交付/);
  assert.match(stage, /escapeHtml\(defectNotice\)/);
  // 警示条位于当前组视频播放器之前。
  assert.ok(stage.indexOf("画面缺陷已标注交付") < stage.indexOf("id=\"currentGroupVideo\""));
});

test("composition surfaces show the defect group notice from workflow summary", () => {
  const auto = productionFunction("renderAutoProgressStage");
  assert.match(auto, /const defectGroupsNotice = compositionDefectNotice\(summary\);/);
  assert.match(auto, /缺陷分组标注/);
  assert.ok(auto.indexOf("缺陷分组标注") < auto.indexOf("id=\"autoProgressVideo\""));

  const composition = productionFunction("renderCompositionStage");
  assert.match(composition, /compositionDefectNotice\(state\.summary\)/);
  assert.match(composition, /缺陷分组标注/);
  assert.ok(composition.indexOf("缺陷分组标注") < composition.indexOf("id=\"currentCompositionVideo\""));

  assert.match(appSource, /from "\.\/frontend\/video-defect\.js"/);
});
