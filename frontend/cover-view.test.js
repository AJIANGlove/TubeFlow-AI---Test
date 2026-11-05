import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  COVER_BRIEF_MAX,
  COVER_DEFAULT_DIMS,
  COVER_DIMS_GUIDE,
  COVER_EDITED_TEXT_MAX,
  COVER_PROMPT_MAX,
  COVER_REF_DIMENSIONS,
  coverAnalysisDirty,
  coverCandidateSelector,
  coverCharacterBindingModel,
  coverDimsCounts,
  coverDimsTree,
  coverDimsWithDefault,
  coverFailureText,
  coverJudgeSummary,
  coverLockModel,
  coverImageReviewView,
  coverPageModel,
  coverStyleSourceModel,
  coverTaskRunning,
  coverUploadErrorMessage,
  coverVersionList,
  coverWarningLabel,
  dimsFromPayload,
  normalizeCoverDims,
  toggleCoverChild,
  toggleCoverDim,
} from "./cover-view.js";

test("dimension table mirrors the backend 8 dims and 36 children verbatim", () => {
  assert.deepEqual(
    COVER_REF_DIMENSIONS.map((dim) => dim.key),
    ["subject", "spatial", "composition", "camera", "lighting", "color", "material", "style"],
  );
  assert.equal(COVER_REF_DIMENSIONS.reduce((sum, dim) => sum + dim.children.length, 0), 36);
  assert.deepEqual(COVER_REF_DIMENSIONS[0].children, ["人物身份", "外貌", "动作", "表情", "服装"]);
  assert.deepEqual(COVER_REF_DIMENSIONS[7].children, ["艺术流派", "时代感", "设计语言", "后期效果"]);
  assert.equal(COVER_EDITED_TEXT_MAX, 2000);
  assert.equal(COVER_BRIEF_MAX, 500);
  assert.equal(COVER_PROMPT_MAX, 4000);
});

test("normalizeCoverDims keeps catalog order, drops unknown ids and lets whole key win", () => {
  assert.deepEqual(normalizeCoverDims(null), []);
  assert.deepEqual(normalizeCoverDims("composition"), []);
  assert.deepEqual(
    normalizeCoverDims(["style::时代感", "unknown", "subject::外貌", "composition::站位", " ", "style::时代感"]),
    ["subject::外貌", "composition::站位", "style::时代感"],
  );
  // 裸 key 命中时忽略该类重复子项；不做"全子项折叠为大类"。
  assert.deepEqual(
    normalizeCoverDims(["camera::焦距", "camera", "camera::对焦", "subject::动作"]),
    ["subject::动作", "camera"],
  );
  assert.deepEqual(
    normalizeCoverDims(["material::衣料", "material::皮肤", "material::金属", "material::环境纹理"]),
    ["material::衣料", "material::皮肤", "material::金属", "material::环境纹理"],
  );
});

test("dimsFromPayload reads the storage shape and tolerates garbage", () => {
  assert.deepEqual(dimsFromPayload({ selected: ["spatial", "color::主色"] }), ["spatial", "color::主色"]);
  assert.deepEqual(dimsFromPayload(["lighting::光源"]), ["lighting::光源"]);
  assert.deepEqual(dimsFromPayload({}), []);
  assert.deepEqual(dimsFromPayload(undefined), []);
});

test("dims tree reports all/some/none per dimension", () => {
  const tree = coverDimsTree(["subject", "camera::焦距", "camera::景深"]);
  assert.equal(tree.length, 8);
  assert.equal(tree[0].state, "all");
  assert.equal(tree[0].checkedCount, 5);
  assert.ok(tree[0].children.every((child) => child.checked));
  const camera = tree.find((dim) => dim.key === "camera");
  assert.equal(camera.state, "some");
  assert.equal(camera.checkedCount, 2);
  assert.deepEqual(camera.children.filter((child) => child.checked).map((child) => child.label), ["焦距", "景深"]);
  assert.equal(tree.find((dim) => dim.key === "style").state, "none");
  assert.deepEqual(coverDimsCounts(tree), { checked: 7, total: 36 });
});

test("dim toggle selects the whole dimension as the bare key and clears it", () => {
  assert.deepEqual(toggleCoverDim(["subject::外貌"], "subject", true), ["subject"]);
  assert.deepEqual(toggleCoverDim(["subject", "color::主色"], "subject", false), ["color::主色"]);
  assert.deepEqual(toggleCoverDim([], "nope", true), []);
});

test("child toggle keeps half selection as child ids", () => {
  assert.deepEqual(
    toggleCoverChild([], "composition", "站位", true),
    ["composition::站位"],
  );
  assert.deepEqual(
    toggleCoverChild(["composition::站位"], "composition", "留白", true),
    ["composition::站位", "composition::留白"],
  );
  assert.deepEqual(
    toggleCoverChild(["composition::站位", "composition::留白"], "composition", "站位", false),
    ["composition::留白"],
  );
});

test("child toggle collapses a fully checked dimension into the bare key", () => {
  const partial = ["camera::焦距", "camera::机位", "camera::视角", "camera::景深"];
  assert.deepEqual(toggleCoverChild(partial, "camera", "对焦", true), ["camera"]);
  // 整类全选中取消一个子项 → 展开为其余子项逐项编码。
  assert.deepEqual(
    toggleCoverChild(["camera"], "camera", "景深", false),
    ["camera::焦距", "camera::机位", "camera::视角", "camera::对焦"],
  );
  // 已整类全选时再勾子项保持大类编码不变。
  assert.deepEqual(toggleCoverChild(["camera"], "camera", "对焦", true), ["camera"]);
  assert.deepEqual(toggleCoverChild([], "camera", "不存在", true), []);
});

test("task running follows public_status", () => {
  assert.equal(coverTaskRunning({ public_status: "QUEUED" }), true);
  assert.equal(coverTaskRunning({ public_status: "RUNNING" }), true);
  assert.equal(coverTaskRunning({ public_status: "SUCCEEDED" }), false);
  assert.equal(coverTaskRunning(null), false);
});

const promptGeneration = {
  generation_id: "g1",
  version: 2,
  public_status: "SUCCEEDED",
  cover_text_candidates: [{ text: "别再被骗了" }, { text: "3 个错误" }, { text: "就这？" }],
  cover_text_default: "3 个错误",
  cover_text_used: "3 个错误",
  prompt_body: "主体：……",
  prompt_system: "主体：……\n画面中加入标题文字「3 个错误」",
  prompt_current: "主体：……\n画面中加入标题文字「3 个错误」",
  allowed_actions: ["VIEW", "EDIT_PROMPT", "GENERATE_COVER_IMAGE"],
};

test("candidate selector marks recommended, used and selected", () => {
  const selector = coverCandidateSelector(promptGeneration);
  assert.equal(selector.candidates.length, 3);
  assert.equal(selector.selectedText, "3 个错误");
  assert.deepEqual(selector.candidates.map((item) => item.isRecommended), [false, true, false]);
  assert.deepEqual(selector.candidates.map((item) => item.isUsed), [false, true, false]);
  assert.deepEqual(selector.candidates.map((item) => item.isSelected), [false, true, false]);
  assert.equal(selector.mismatch, false);
  assert.equal(selector.notice, "");
});

test("candidate selector flags a switched choice with the designed-for-original notice", () => {
  const selector = coverCandidateSelector(promptGeneration, "就这？");
  assert.equal(selector.selectedText, "就这？");
  assert.equal(selector.mismatch, true);
  assert.match(selector.notice, /画面是按原文字设计的/);
  // 非法 choice 回退到画面实际使用的文字。
  const fallback = coverCandidateSelector(promptGeneration, "不在候选里");
  assert.equal(fallback.selectedText, "3 个错误");
  assert.equal(fallback.mismatch, false);
  // 空生成不产生候选。
  const empty = coverCandidateSelector(null);
  assert.deepEqual(empty.candidates, []);
  assert.equal(empty.selectedText, "");
});

test("version list maps generations and profiles with current and selectable flags", () => {
  const versions = coverVersionList([
    { generation_id: "g2", version: 2, is_current: true, public_status: "SUCCEEDED", source: "upload", allowed_actions: ["SELECT_CURRENT"] },
    { generation_id: "g1", version: 1, is_current: false, public_status: "FAILED", failure: { stage: "generate", code: "cover_prompt_empty" }, allowed_actions: ["SELECT_CURRENT"] },
    { profile_id: "p1", is_current: false, public_status: "SUCCEEDED", allowed_actions: [] },
  ]);
  assert.deepEqual(versions.map((item) => item.id), ["g2", "g1", "p1"]);
  assert.deepEqual(versions.map((item) => item.version), [2, 1, 3]);
  assert.deepEqual(versions.map((item) => item.isCurrent), [true, false, false]);
  assert.deepEqual(versions.map((item) => item.selectable), [false, true, false]);
  assert.deepEqual(versions.map((item) => item.source), ["upload", "", ""]);
  assert.deepEqual(versions[1].failure, { stage: "generate", code: "cover_prompt_empty" });
  assert.deepEqual(coverVersionList(null), []);
});

test("warning, failure and judge display models", () => {
  assert.equal(coverWarningLabel("cover_text_fallback_title"), "封面文字不可用，已回退为项目标题");
  assert.equal(coverWarningLabel("cover_judge_low_score"), "最优版评委总分低于 112 / 140，建议重试生成");
  assert.equal(coverWarningLabel("brand_new_warning"), "brand_new_warning");
  assert.equal(coverFailureText({ stage: "image", code: "provider_rejected" }), "image · provider_rejected");
  assert.equal(coverFailureText(null), "");
  const judge = coverJudgeSummary({
    best: 1,
    verdicts: [
      { version: 1, total: 101, comment: "一般" },
      { version: 2, total: 118, comment: "更聚焦" },
    ],
    advice: "加强对比",
  });
  assert.equal(judge.bestVersion, 2);
  assert.deepEqual(judge.rows.map((row) => row.isBest), [false, true]);
  assert.equal(judge.advice, "加强对比");
  assert.equal(coverJudgeSummary(null), null);
  assert.equal(coverJudgeSummary({}), null);
});

test("style source prefers channel profile, then project draft, then none", () => {
  const channelStyle = { current_profile: { version: 3, edited_text: "主体：站立男子", created_at: "2026-09-20" } };
  const projectStyle = { draft: { has_analysis: true, edited_text: "色彩：高饱和", updated_at: "2026-09-21" } };
  const both = coverStyleSourceModel({ channelStyle, projectStyle });
  assert.equal(both.mode, "channel");
  assert.equal(both.channel.available, true);
  assert.equal(both.channel.version, 3);
  assert.equal(both.project.available, true);
  assert.equal(both.hint, "");
  const projectOnly = coverStyleSourceModel({ channelStyle: null, projectStyle });
  assert.equal(projectOnly.mode, "project");
  const none = coverStyleSourceModel({ channelStyle: null, projectStyle: null });
  assert.equal(none.mode, "none");
  assert.match(none.hint, /尚无频道风格/);
  const preferred = coverStyleSourceModel({ channelStyle, projectStyle, preferred: "none" });
  assert.equal(preferred.mode, "none");
});

test("lock model covers purging, not-frozen and frozen projects", () => {
  assert.equal(coverLockModel({ production_state: "PURGING", snapshot: {} }, null).locked, true);
  const notFrozen = coverLockModel({ snapshot: null }, { blocking_reason: "narration_not_confirmed" });
  assert.equal(notFrozen.locked, true);
  assert.match(notFrozen.detail, /尚未冻结/);
  assert.equal(coverLockModel({ snapshot: { script_sha256: "x" } }, { blocking_reason: null }).locked, false);
});

const frozenProject = { id: "p1", title: "封面标题", snapshot: { script_sha256: "x" } };

const projectStylePayload = {
  draft: {
    ref_image_id: "r1",
    dims: { selected: ["subject", "color::主色"] },
    edited_text: "主体：站立男子",
    has_analysis: true,
    updated_at: "2026-09-21",
  },
  ref_images: [
    { ref_image_id: "r1", width: 1920, height: 1080, image_format: "PNG", byte_size: 2 * 1024 * 1024, sha256: "abcdef0123456789" },
    { ref_image_id: "r2", width: 1280, height: 720, image_format: "JPEG", byte_size: 512 * 1024, sha256: "9876543210abcdef" },
  ],
  current_profile: null,
  profiles: [],
};

test("page model builds ref editor state with counts, selection and analyze gate", () => {
  const model = coverPageModel({ fact: { blocking_reason: null }, project: frozenProject, projectStyle: projectStylePayload });
  assert.equal(model.locked, false);
  assert.equal(model.refEditor.checked, 6);
  assert.equal(model.refEditor.total, 36);
  assert.equal(model.refEditor.selectedRefImageId, "r1");
  assert.deepEqual(model.refEditor.refImages.map((image) => image.isSelected), [true, false]);
  assert.match(model.refEditor.refImages[0].meta, /1920×1080/);
  assert.match(model.refEditor.refImages[0].meta, /PNG · 2\.00 MB/);
  assert.doesNotMatch(model.refEditor.refImages[0].meta, /SHA256/);
  assert.equal(model.refEditor.hasAnalysis, true);
  assert.equal(model.refEditor.canAnalyze, true);
  assert.equal(model.refEditor.canPromote, true);
  // 解析在途禁用解析与固定。
  const busy = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, analyzing: true });
  assert.equal(busy.refEditor.canAnalyze, false);
  assert.equal(busy.refEditor.canPromote, false);
  // 未选参考图或未勾选维度不可解析。
  const noImage = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: { ...projectStylePayload, draft: { ...projectStylePayload.draft, ref_image_id: null } },
  });
  assert.equal(noImage.refEditor.canAnalyze, false);
  const noDims = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: { ...projectStylePayload, draft: { ...projectStylePayload.draft, dims: { selected: [] } } },
  });
  assert.equal(noDims.refEditor.canAnalyze, false);
  const empty = coverPageModel({ fact: null, project: frozenProject, projectStyle: { draft: null, ref_images: [] } });
  assert.equal(empty.refEditor.refImages.length, 0);
  assert.match(empty.refEditor.emptyImagesText, /尚未上传参考图/);
});

test("page model builds prompt panel with running, edited and selector state", () => {
  const prompts = {
    current: promptGeneration,
    items: [
      promptGeneration,
      { ...promptGeneration, generation_id: "g0", version: 1, is_current: false, public_status: "FAILED", failure: { stage: "generate", code: "cover_prompt_generation_failed" } },
    ],
  };
  const model = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts });
  assert.equal(model.locked, false);
  assert.equal(model.promptPanel.running, false);
  assert.equal(model.promptPanel.current.edited, false);
  assert.equal(model.promptPanel.current.selector.candidates.length, 3);
  assert.equal(model.promptPanel.versions.length, 2);
  assert.equal(model.imagePanel.canGenerate, true);
  const edited = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: projectStylePayload,
    prompts: { ...prompts, current: { ...promptGeneration, prompt_current: "手动改过的提示词" } },
  });
  assert.equal(edited.promptPanel.current.edited, true);
  const running = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: projectStylePayload,
    prompts: { current: { ...promptGeneration, public_status: "QUEUED" }, items: [] },
  });
  assert.equal(running.promptPanel.running, true);
  assert.equal(running.imagePanel.canGenerate, false);
  const empty = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts: null });
  assert.equal(empty.promptPanel.current, null);
  assert.match(empty.promptPanel.emptyText, /尚未生成封面提示词/);
});

test("page model builds image panel with current media and generate gate", () => {
  const images = {
    current: {
      generation_id: "i1",
      version: 1,
      public_status: "SUCCEEDED",
      media_url: "/projects/p1/cover/images/i1/media",
      width: 2560,
      height: 1440,
      model: "gpt-image-2",
      is_current: true,
    },
    items: [
      { generation_id: "i1", version: 1, public_status: "SUCCEEDED", is_current: true, media_url: "/projects/p1/cover/images/i1/media", allowed_actions: ["VIEW_MEDIA", "SELECT_CURRENT"] },
    ],
  };
  const prompts = { current: promptGeneration, items: [promptGeneration] };
  const model = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts, images });
  assert.equal(model.imagePanel.current.mediaUrl, "/projects/p1/cover/images/i1/media");
  assert.match(model.imagePanel.current.meta, /2560×1440/);
  assert.equal(model.imagePanel.versions[0].isCurrent, true);
  assert.match(model.imagePanel.emptyText, /上传/);
  assert.match(model.imagePanel.emptyText, /png/);
  // 2026-11-05：上传不再要求 16:9 或 ≥1280×720。
  assert.doesNotMatch(model.imagePanel.emptyText, /16:9/);
  // 提示词未就绪不可出图。
  const noPrompt = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts: { current: null, items: [] }, images });
  assert.equal(noPrompt.imagePanel.canGenerate, false);
  // 出图在途禁用再次出图。
  const imageRunning = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: projectStylePayload,
    prompts,
    images: { current: null, items: [{ generation_id: "i2", public_status: "RUNNING", is_current: false }] },
  });
  assert.equal(imageRunning.imagePanel.running, true);
  assert.equal(imageRunning.imagePanel.canGenerate, false);
});

test("page model locks the whole page for a not-frozen project", () => {
  const model = coverPageModel({ fact: { blocking_reason: "narration_not_confirmed" }, project: { id: "p2", snapshot: null } });
  assert.equal(model.locked, true);
  assert.match(model.lockTitle, /冻结/);
});

test("cover stage is registered as the tenth stage with renderer, polling and channel section", () => {
  const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  assert.match(appSource, /\{ key: "cover", label: "封面", note: "参考图与频道风格" \}/);
  assert.match(appSource, /cover: renderCoverStage/);
  assert.match(appSource, /async function renderCoverStage\(\)/);
  assert.match(appSource, /function scheduleCoverPolling\(\)/);
  assert.match(appSource, /解析参考图/);
  assert.match(appSource, /生成封面提示词/);
  assert.match(appSource, /生成封面图/);
  assert.match(appSource, /上传封面图/);
  assert.match(appSource, /id="coverUploadImage"/);
  assert.match(appSource, /id="coverUploadImageFile"/);
  assert.match(appSource, /api\.uploadProjectCoverImage/);
  assert.match(appSource, /async function uploadCoverImageFile/);
  // 2026-11-05：取消最小尺寸与 16:9 限制，失败提示按 code 映射中文。
  assert.doesNotMatch(appSource, /≥1280×720/);
  assert.match(appSource, /coverUploadErrorMessage/);
  assert.match(appSource, /固定为频道风格/);
  assert.match(appSource, /以所选文字重新生成画面/);
  assert.match(appSource, /function channelCoverStylePanelMarkup\(channel\)/);
  assert.match(appSource, /api\.selectChannelCoverStyle/);
  // 封面草稿与面板文本走 800ms 防抖保存，且不写 localStorage；
  // 唯一例外是「更多设置」折叠偏好（2026-10-21 永久改造），按项目记忆、不承载任何草稿内容。
  assert.match(appSource, /const COVER_SAVE_DEBOUNCE_MS = 800/);
  const coverBlock = appSource.slice(appSource.indexOf("const COVER_SAVE_DEBOUNCE_MS"), appSource.indexOf("async function renderAssets"));
  assert.doesNotMatch(coverBlock, /localStorage\.(?:getItem|setItem)\((?!coverMoreSettingsKey)/);
});

test("v5 default selection is style+composition and only applies to a never-saved draft", () => {
  assert.deepEqual([...COVER_DEFAULT_DIMS], ["style", "composition"]);
  assert.deepEqual(coverDimsWithDefault(null), ["style", "composition"]);
  assert.deepEqual(coverDimsWithDefault(undefined), ["style", "composition"]);
  // 已保存过的勾选（含显式清空的空集合）以存储值为准，不回填默认。
  assert.deepEqual(coverDimsWithDefault({ selected: ["subject"] }), ["subject"]);
  assert.deepEqual(coverDimsWithDefault({ selected: [] }), []);
  // 从未保存的草稿（dims 载荷缺失）展示默认勾选：style 4 子项 + composition 5 子项。
  const fresh = coverPageModel({ fact: null, project: frozenProject, projectStyle: { draft: null, ref_images: [] } });
  assert.equal(fresh.refEditor.checked, 9);
  assert.deepEqual(
    fresh.refEditor.tree.filter((dim) => dim.state === "all").map((dim) => dim.key),
    ["composition", "style"],
  );
  const cleared = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: { draft: { ref_image_id: "r1", dims: { selected: [] }, edited_text: "", has_analysis: false }, ref_images: [] },
  });
  assert.equal(cleared.refEditor.checked, 0);
});

test("dimension tree carries the v5 three-tier migration annotations and guide copy", () => {
  const byKey = Object.fromEntries(coverDimsTree([]).map((dim) => [dim.key, dim]));
  for (const key of ["style", "composition"]) {
    assert.equal(byKey[key].kind, "pure_style");
    assert.equal(byKey[key].kindLabel, "纯风格");
    assert.equal(byKey[key].kindHint, "跨场景迁移优先");
  }
  for (const key of ["subject", "material"]) {
    assert.equal(byKey[key].kind, "semi_scene");
    assert.equal(byKey[key].kindLabel, "半场景");
    assert.equal(byKey[key].kindHint, "携带部分场景信息");
  }
  for (const key of ["spatial", "lighting", "color", "camera"]) {
    assert.equal(byKey[key].kind, "scene_bound");
    assert.equal(byKey[key].kindLabel, "场景绑定");
    assert.equal(byKey[key].kindHint, "与参考场景强绑定，跨场景慎用");
  }
  assert.match(COVER_DIMS_GUIDE, /勾选决定参考图哪些描述逐字进入成品/);
  assert.match(COVER_DIMS_GUIDE, /『场景绑定』维度会把参考图的光色与场景一起带来/);
});

test("analysis dirty flag needs a known analyzed selection that differs from the current one", () => {
  assert.equal(coverAnalysisDirty({ hasAnalysis: false, analyzedDims: ["style"], selected: ["subject"] }).dirty, false);
  // 无法确知最近解析勾选（后端草稿视图不外泄 analysis_dims）时不标脏。
  assert.equal(coverAnalysisDirty({ hasAnalysis: true, analyzedDims: null, selected: ["subject"] }).dirty, false);
  const clean = coverAnalysisDirty({ hasAnalysis: true, analyzedDims: ["composition::站位", "style"], selected: ["style", "composition::站位"] });
  assert.equal(clean.dirty, false);
  assert.equal(clean.notice, "");
  const dirty = coverAnalysisDirty({ hasAnalysis: true, analyzedDims: ["style", "composition"], selected: ["style", "color::主色"] });
  assert.equal(dirty.dirty, true);
  assert.equal(dirty.notice, "勾选已变化，需重新解析");
  // 页面模型透传脏状态徽标：草稿勾选 subject+color::主色 ≠ 解析时勾选。
  const model = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, analyzedDims: ["style"] });
  assert.equal(model.refEditor.dimsDirty, true);
  assert.match(model.refEditor.dimsDirtyNotice, /需重新解析/);
  const same = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, analyzedDims: ["subject", "color::主色"] });
  assert.equal(same.refEditor.dimsDirty, false);
  assert.equal(same.refEditor.dimsDirtyNotice, "");
});

test("low score warning attaches the v5 action guide", () => {
  const prompts = { current: { ...promptGeneration, warnings: ["cover_judge_low_score"] }, items: [] };
  const model = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts });
  assert.match(model.promptPanel.current.lowScoreGuide, /①只勾纯风格维度重新生成/);
  assert.match(model.promptPanel.current.lowScoreGuide, /换用其他风格版本/);
  const quiet = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts: { current: promptGeneration, items: [] } });
  assert.equal(quiet.promptPanel.current.lowScoreGuide, "");
});

test("character binding model resolves default, explicit and explicit-empty selections", () => {
  const channelImages = [
    { id: "img-a", width: 512, height: 512, media_type: "image/png" },
    { id: "img-b", width: 1024, height: 1024, media_type: "image/jpeg" },
    { id: "img-c", width: 256, height: 256, media_type: "image/webp" },
  ];
  const cards = [
    { card_id: "card-a", character_image_id: "img-a", identity_text: "小蓝", appearance_text: "Q版三头身机器人", source: "analysis" },
    { card_id: "card-b", character_image_id: "img-b", identity_text: "", appearance_text: "", source: "manual" },
  ];
  // 默认 = 项目快照 ∩ 可绑定卡（identity 非空），按快照顺序：img-b 的卡 identity 为空视为无卡。
  const binding = coverCharacterBindingModel({ snapshotImageIds: ["img-b", "img-a"], channelImages, cards });
  assert.equal(binding.available, true);
  assert.deepEqual(binding.defaultIds, ["card-a"]);
  assert.deepEqual(binding.selectedIds, ["card-a"]);
  assert.equal(binding.isDefault, true);
  assert.equal(binding.boundCount, 1);
  assert.deepEqual(binding.rows.map((row) => row.inSnapshot), [true, true, false]);
  assert.equal(binding.rows[0].selected, true);
  assert.equal(binding.rows[0].order, 1);
  assert.equal(binding.rows[1].hasCard, true);
  assert.equal(binding.rows[1].bindable, false);
  assert.equal(binding.rows[1].selected, false);
  assert.equal(binding.rows[2].hasCard, false);
  assert.match(binding.summaryText, /默认绑定 1 张角色卡/);
  // 显式选择保留用户顺序并过滤已不存在的卡。
  const explicit = coverCharacterBindingModel({ snapshotImageIds: ["img-b", "img-a"], channelImages, cards, selection: ["card-gone", "card-b", "card-a"] });
  assert.deepEqual(explicit.selectedIds, ["card-b", "card-a"]);
  assert.equal(explicit.isDefault, false);
  assert.equal(explicit.rows[1].order, 1);
  assert.equal(explicit.rows[0].order, 2);
  assert.match(explicit.summaryText, /已手动选择 2 张角色卡/);
  // 显式空 = 本轮不绑定角色。
  const none = coverCharacterBindingModel({ snapshotImageIds: ["img-b", "img-a"], channelImages, cards, selection: [] });
  assert.deepEqual(none.selectedIds, []);
  assert.equal(none.isDefault, false);
  assert.equal(none.boundCount, 0);
  assert.match(none.summaryText, /不绑定角色/);
  // 在途解析标记与端点不可用/空频道。
  const busy = coverCharacterBindingModel({ snapshotImageIds: [], channelImages, cards, analyzingCardId: "card-a" });
  assert.equal(busy.rows[0].analyzing, true);
  assert.equal(busy.rows[1].analyzing, false);
  assert.equal(coverCharacterBindingModel({ cards: null }).available, false);
  const empty = coverCharacterBindingModel({ snapshotImageIds: [], channelImages: [], cards: [] });
  assert.equal(empty.rows.length, 0);
  assert.match(empty.emptyText, /频道尚无角色图片/);
});

test("version list exposes label and prompt switch flag; prompt panel surfaces bound cards", () => {
  const versions = coverVersionList([
    { generation_id: "g2", version: 2, is_current: true, public_status: "SUCCEEDED" },
    { generation_id: "g1", version: 1, is_current: false, public_status: "SUCCEEDED" },
    { generation_id: "g0", version: 1, is_current: false, public_status: "FAILED" },
    { profile_id: "p1", version: 3, is_current: false, public_status: "SUCCEEDED", label: "蓝调科技" },
  ]);
  assert.deepEqual(versions.map((item) => item.switchable), [false, true, false, true]);
  assert.equal(versions[3].label, "蓝调科技");
  assert.equal(versions[0].label, "");
  // 提示词版本详情带出 character_cards_used 快照；生成输入带出频道风格版本列表（含 label）。
  const prompts = {
    current: {
      ...promptGeneration,
      character_cards_used: [{ card_id: "card-a", character_image_id: "img-a", identity_text: "小蓝", appearance_text: "Q版" }],
    },
    items: [],
  };
  const channelStyle = {
    current_profile: { version: 2, edited_text: "色彩：高饱和", label: "蓝调", created_at: "2026-09-21" },
    profiles: [
      { profile_id: "p1", version: 2, label: "蓝调", is_current: true },
      { profile_id: "p0", version: 1, label: "", is_current: false },
    ],
  };
  const model = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts, channelStyle });
  assert.deepEqual(model.promptPanel.current.characterCardsUsed.map((card) => card.identityText), ["小蓝"]);
  assert.deepEqual(model.promptPanel.current.characterCardsUsed.map((card) => card.cardId), ["card-a"]);
  assert.deepEqual(model.generateInput.channelStyleProfiles.map((profile) => profile.label), ["蓝调", ""]);
  assert.deepEqual(model.generateInput.channelStyleProfiles.map((profile) => profile.isCurrent), [true, false]);
  assert.equal(model.styleSource.channel.label, "蓝调");
  // v4 旧版本没有 character_cards_used 字段 → null（前端不渲染该区块）。
  const legacy = coverPageModel({ fact: null, project: frozenProject, projectStyle: projectStylePayload, prompts: { current: promptGeneration, items: [] } });
  assert.equal(legacy.promptPanel.current.characterCardsUsed, null);
});

test("cover v5 wiring is present in app.js and the shared editor", () => {
  const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8");
  // 提示词版本切换（POST prompts/current）。
  assert.match(appSource, /data-cover-prompt-select/);
  assert.match(appSource, /api\.setCurrentCoverPrompt/);
  // ⑥ 角色绑定区与角色卡动作。
  assert.match(appSource, /coverCharacterBindingMarkup/);
  assert.match(appSource, /api\.upsertCoverCharacterCard/);
  assert.match(appSource, /api\.analyzeCoverCharacterCard/);
  assert.match(appSource, /api\.coverCharacterCards/);
  assert.match(appSource, /character_card_ids/);
  // 按风格版本生成与备注名。
  assert.match(appSource, /style_profile_id/);
  assert.match(appSource, /coverStyleLabel/);
  // 参考图受保护媒体预览（两作用域共用组件接入）。
  assert.match(appSource, /api\.coverRefImageMediaUrl/);
  assert.match(appSource, /data-cover-ref-image/);
  // 脏状态徽标与覆盖确认。
  assert.match(appSource, /coverDimsDirty/);
  assert.match(appSource, /重新解析将覆盖当前文本/);
  // 默认绑定不传 character_card_ids（后端按快照∩有卡解析）。
  assert.match(appSource, /if \(state\.coverCardSelection != null\) body\.character_card_ids/);
});


test("image review view model normalizes payloads and passes through page model", () => {
  assert.equal(coverImageReviewView(null), null);
  assert.equal(coverImageReviewView({}), null);
  assert.equal(coverImageReviewView({ status: "unknown" }), null);
  const failed = coverImageReviewView({ status: "failed" });
  assert.equal(failed.code, "cover_image_review_failed");
  assert.equal(failed.issues.length, 0);
  const raw = {
    status: "succeeded",
    ok: false,
    issues: [
      { kind: "text_spelling", severity: "blocker", description: "拼写错误", suggested_edit: "改为 X" },
      { kind: "other", severity: "fatal", description: "非 blocker 归为 advice" },
      { kind: "other", severity: "advice", description: "  " },
    ],
  };
  const review = coverImageReviewView(raw);
  assert.equal(review.ok, false);
  assert.equal(review.blockerCount, 1);
  assert.equal(review.issues.length, 2);
  assert.equal(review.issues[0].suggestedEdit, "改为 X");
  assert.equal(review.issues[1].severity, "advice");
  const images = {
    current: {
      generation_id: "i1",
      version: 1,
      public_status: "SUCCEEDED",
      media_url: "/projects/p1/cover/images/i1/media",
      is_current: true,
      visual_review: raw,
    },
    items: [],
  };
  const model = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: projectStylePayload,
    prompts: { current: promptGeneration, items: [promptGeneration] },
    images,
  });
  assert.equal(model.imagePanel.current.review.blockerCount, 1);
  const noReview = coverPageModel({
    fact: null,
    project: frozenProject,
    projectStyle: projectStylePayload,
    prompts: { current: promptGeneration, items: [promptGeneration] },
    images: { ...images, current: { ...images.current, visual_review: null } },
  });
  assert.equal(noReview.imagePanel.current.review, null);
});

test("upload error codes map to Chinese hints and unknown codes fall back", () => {
  assert.match(coverUploadErrorMessage({ code: "invalid_image_type" }), /png/);
  assert.match(coverUploadErrorMessage({ code: "invalid_image_size" }), /25MB/);
  assert.match(coverUploadErrorMessage({ code: "invalid_image_dimensions" }), /4000 万像素/);
  assert.match(coverUploadErrorMessage({ code: "invalid_image" }), /无法解码/);
  assert.equal(coverUploadErrorMessage({ code: "something_else" }), "");
  assert.equal(coverUploadErrorMessage(null), "");
  assert.equal(coverUploadErrorMessage({}), "");
});
