// 封面页（第十阶段）与频道封面风格区的展示层视图模型。
// 只放不依赖 DOM 的纯函数：8 维 36 子项勾选树与后端勾选编码互转、五区展示态、
// 候选选择器、版本列表与未冻结锁定；app.js 负责渲染、轮询与事件绑定。
// 维度常量与 backend/src/tubeflow_test/domain/cover.py 的 REF_DIMENSIONS 逐字对齐；
// 勾选编码语义与 normalize_ref_dims 对齐：大类 key 或 `key::中文子项`，
// 整类全选编码为大类 key（不折叠为子项枚举），裸 key 命中时忽略该类重复子项。

export const COVER_REF_DIMENSIONS = Object.freeze([
  Object.freeze({ key: "subject", label: "主体", children: Object.freeze(["人物身份", "外貌", "动作", "表情", "服装"]) }),
  Object.freeze({ key: "spatial", label: "空间", children: Object.freeze(["前景", "中景", "背景", "环境", "道具"]) }),
  Object.freeze({ key: "composition", label: "构图", children: Object.freeze(["人物占比", "站位", "画面比例", "留白", "视觉中心"]) }),
  Object.freeze({ key: "camera", label: "镜头", children: Object.freeze(["焦距", "机位", "视角", "景深", "对焦"]) }),
  Object.freeze({ key: "lighting", label: "光影", children: Object.freeze(["光源", "方向", "强弱", "阴影"]) }),
  Object.freeze({ key: "color", label: "色彩", children: Object.freeze(["主色", "辅色", "饱和度", "色彩情绪"]) }),
  Object.freeze({ key: "material", label: "材质", children: Object.freeze(["衣料", "皮肤", "金属", "环境纹理"]) }),
  Object.freeze({ key: "style", label: "风格", children: Object.freeze(["艺术流派", "时代感", "设计语言", "后期效果"]) }),
]);

// 与后端 cover.py 限长一致：解析文本 ≤2000 码点、补充需求 ≤500 码点、面板提示词 ≤4000 码点。
export const COVER_EDITED_TEXT_MAX = 2000;
export const COVER_BRIEF_MAX = 500;
export const COVER_PROMPT_MAX = 4000;
// 与后端 v5 限长一致：角色卡身份 ≤100、外貌 ≤500 码点；风格版本备注名 ≤60 码点。
export const COVER_CHARACTER_IDENTITY_MAX = 100;
export const COVER_CHARACTER_APPEARANCE_MAX = 500;
export const COVER_STYLE_LABEL_MAX = 60;

// v5 §4：默认勾选集合（原全选 8 维为误导性默认，E2E 实测全选=全量复刻）。
export const COVER_DEFAULT_DIMS = Object.freeze(["style", "composition"]);

// v5 §4：勾选树维度类型三档标注（常量随 COVER_REF_DIMENSIONS 维护）。
export const COVER_DIM_KINDS = Object.freeze({
  pure_style: Object.freeze({ label: "纯风格", hint: "跨场景迁移优先" }),
  semi_scene: Object.freeze({ label: "半场景", hint: "携带部分场景信息" }),
  scene_bound: Object.freeze({ label: "场景绑定", hint: "与参考场景强绑定，跨场景慎用" }),
});

const COVER_DIM_KIND_BY_KEY = Object.freeze({
  subject: "semi_scene",
  spatial: "scene_bound",
  composition: "pure_style",
  camera: "scene_bound",
  lighting: "scene_bound",
  color: "scene_bound",
  material: "semi_scene",
  style: "pure_style",
});

// v5 §4：勾选树标注引导文案（原文）。
export const COVER_DIMS_GUIDE = "勾选决定参考图哪些描述逐字进入成品。跨场景使用优先勾『纯风格』；『场景绑定』维度会把参考图的光色与场景一起带来。";

// v5 §4：评委低分警告（cover_judge_low_score）旁附的动作提示（原文）。
export const COVER_LOW_SCORE_GUIDE = "画面与标题不匹配时：①只勾纯风格维度重新生成；②风格与场景冲突时：编辑风格文本为中性描述，或换用其他风格版本。";

function coverDim(key) {
  return COVER_REF_DIMENSIONS.find((dim) => dim.key === key) || null;
}

// 勾选编码规整（对齐后端 normalize_ref_dims）：非数组输入 → []；元素转字符串去空白去重；
// 只接受大类 key 或 `key::已知中文子项`；按分类表顺序输出；整类命中（裸 key）忽略该类重复子项；
// 不做"全子项折叠为大类"。
export function normalizeCoverDims(input) {
  const raw = new Set(
    (Array.isArray(input) ? input : [])
      .map((item) => String(item ?? "").trim())
      .filter(Boolean),
  );
  const out = [];
  for (const dim of COVER_REF_DIMENSIONS) {
    if (raw.has(dim.key)) {
      out.push(dim.key);
      continue;
    }
    for (const child of dim.children) {
      const childId = `${dim.key}::${child}`;
      if (raw.has(childId)) out.push(childId);
    }
  }
  return out;
}

// API 草稿/版本的 dims 载荷读取：仓储形态 {"selected": [...]}；也容忍直接给扁平数组。
export function dimsFromPayload(dims) {
  if (Array.isArray(dims)) return normalizeCoverDims(dims);
  return normalizeCoverDims(dims && typeof dims === "object" ? dims.selected : []);
}

// 展示层默认勾选（v5 §4）：草稿从未保存勾选（dims 载荷缺失）时回填 COVER_DEFAULT_DIMS；
// 已保存过（含显式清空的空集合）一律以存储值为准，不回填默认。
export function coverDimsWithDefault(dims) {
  if (dims == null) return [...COVER_DEFAULT_DIMS];
  return dimsFromPayload(dims);
}

// 脏状态推导（v5 §4）：当前勾选 ≠ 最近一次解析所用勾选（草稿 analysis_dims 语义）时，
// 提示"勾选已变化，需重新解析"。analyzedDims 为 null 表示无法确知（后端草稿视图不外泄
// analysis_dims，由 app.js 在提交解析成功时按会话记录），此时不标脏。
export function coverAnalysisDirty({ hasAnalysis = false, analyzedDims = null, selected = [] } = {}) {
  if (!hasAnalysis || analyzedDims == null) return { dirty: false, notice: "" };
  const dirty = JSON.stringify(normalizeCoverDims(analyzedDims)) !== JSON.stringify(normalizeCoverDims(selected));
  return { dirty, notice: dirty ? "勾选已变化，需重新解析" : "" };
}

// 勾选树展示模型：每维度 {key,label,children:[{id,label,checked}],checkedCount,total,state}；
// state 为 all（全选）/ some（半选）/ none（全清），整类编码视为全部子项选中。
// v5 §4：每维度追加 kind/kindLabel/kindHint 三档迁移标注（纯风格/半场景/场景绑定）。
export function coverDimsTree(selected) {
  const flat = new Set(normalizeCoverDims(selected));
  return COVER_REF_DIMENSIONS.map((dim) => {
    const whole = flat.has(dim.key);
    const children = dim.children.map((child) => ({
      id: `${dim.key}::${child}`,
      label: child,
      checked: whole || flat.has(`${dim.key}::${child}`),
    }));
    const checkedCount = children.filter((child) => child.checked).length;
    const kind = COVER_DIM_KIND_BY_KEY[dim.key] || "semi_scene";
    const kindMeta = COVER_DIM_KINDS[kind];
    return {
      key: dim.key,
      label: dim.label,
      children,
      checkedCount,
      total: children.length,
      state: checkedCount === 0 ? "none" : checkedCount === children.length ? "all" : "some",
      kind,
      kindLabel: kindMeta.label,
      kindHint: kindMeta.hint,
    };
  });
}

// 勾选计数摘要：基于树状态统计（整类编码按全部子项计入）。
export function coverDimsCounts(tree) {
  const rows = Array.isArray(tree) ? tree : coverDimsTree(tree);
  return rows.reduce(
    (acc, dim) => ({ checked: acc.checked + dim.checkedCount, total: acc.total + dim.total }),
    { checked: 0, total: 0 },
  );
}

// 维度级切换：勾选 → 整类全选（编码收敛为大类 key）；取消 → 清除该类全部编码。
export function toggleCoverDim(selected, key, checked) {
  const dim = coverDim(key);
  if (!dim) return normalizeCoverDims(selected);
  const flat = new Set(normalizeCoverDims(selected));
  for (const child of dim.children) flat.delete(`${key}::${child}`);
  if (checked) flat.add(key);
  else flat.delete(key);
  return normalizeCoverDims([...flat]);
}

// 子项级切换：整类全选中取消一个子项 → 展开为其余子项逐项编码；
// 勾满该类全部子项 → 折叠为大类 key（与后端"整类全选用大类 key"一致）。
export function toggleCoverChild(selected, key, child, checked) {
  const dim = coverDim(key);
  if (!dim || !dim.children.includes(child)) return normalizeCoverDims(selected);
  const flat = new Set(normalizeCoverDims(selected));
  const childId = `${key}::${child}`;
  if (checked) {
    if (!flat.has(key)) {
      flat.add(childId);
      if (dim.children.every((item) => flat.has(`${key}::${item}`))) {
        for (const item of dim.children) flat.delete(`${key}::${item}`);
        flat.add(key);
      }
    }
  } else if (flat.has(key)) {
    flat.delete(key);
    for (const item of dim.children) {
      if (item !== child) flat.add(`${key}::${item}`);
    }
  } else {
    flat.delete(childId);
  }
  return normalizeCoverDims([...flat]);
}

// 任务在途判定（public_status 口径，与 app.js publicStatus 的 QUEUED/RUNNING 一致）。
export function coverTaskRunning(value) {
  return ["QUEUED", "RUNNING"].includes(value?.public_status || value?.status);
}

// 封面文字候选选择器：5 候选 + 推荐标记（cover_text_default）+ 画面实际使用（cover_text_used）。
// choice 为面板当前所选（缺省回退 used → default → 首个候选）；所选与画面设计文字不一致时
// mismatch 为 true，提示"画面是按原文字设计的"。
export function coverCandidateSelector(generation, choice = null) {
  const texts = (Array.isArray(generation?.cover_text_candidates) ? generation.cover_text_candidates : [])
    .map((item) => String(item?.text ?? "").trim())
    .filter(Boolean);
  const recommendedText = String(generation?.cover_text_default ?? "").trim();
  const usedText = String(generation?.cover_text_used ?? "").trim();
  const requested = String(choice ?? "").trim();
  const selectedText = (requested && texts.includes(requested) ? requested : "")
    || usedText
    || recommendedText
    || texts[0]
    || "";
  const candidates = texts.map((text, index) => ({
    text,
    index,
    isRecommended: Boolean(recommendedText) && text === recommendedText,
    isUsed: Boolean(usedText) && text === usedText,
    isSelected: Boolean(selectedText) && text === selectedText,
  }));
  const mismatch = Boolean(selectedText && usedText && selectedText !== usedText);
  return {
    candidates,
    selectedText,
    recommendedText,
    usedText,
    mismatch,
    notice: mismatch ? "画面是按原文字设计的：换候选只重排提示词中的文字，可再点「以所选文字重新生成画面」。" : "",
  };
}

// 版本列表展示模型（提示词版本 / 封面图版本 / 频道风格版本共用）：
// 当前标记、失败信息、可切换标记（后端 allowed_actions 含 SELECT_CURRENT 且非当前）。
// v5：label 为风格版本备注名（无则空串）；switchable 为「非当前且 SUCCEEDED」，
// 供提示词版本「切换」按钮使用（提示词视图不带 SELECT_CURRENT，v5 §3 以状态为准）。
export function coverVersionList(items) {
  return (Array.isArray(items) ? items : []).map((item, index) => ({
    id: String(item?.generation_id || item?.profile_id || ""),
    version: Number.isInteger(Number(item?.version)) ? Number(item.version) : index + 1,
    isCurrent: Boolean(item?.is_current),
    status: item?.public_status || item?.status || "",
    failure: item?.failure || null,
    mediaUrl: item?.media_url || null,
    label: String(item?.label || ""),
    source: String(item?.source || ""),
    createdAt: item?.created_at || "",
    selectable: !item?.is_current
      && Array.isArray(item?.allowed_actions)
      && item.allowed_actions.includes("SELECT_CURRENT"),
    switchable: !item?.is_current && String(item?.public_status || item?.status || "") === "SUCCEEDED",
  }));
}

// 提示词警告码 → 人话（取值与 application/cover_prompts.py 的 warnings 一致；未知码回退原文）。
const COVER_WARNING_LABELS = Object.freeze({
  cover_text_truncated: "封面文字超过 30 码点，已截断",
  cover_text_fallback_title: "封面文字不可用，已回退为项目标题",
  cover_variant_failed: "一个并行版本生成失败，已按单版继续",
  cover_judge_fallback_local: "评委调用失败，已按本地规则择优",
  cover_judge_low_score: "最优版评委总分低于 112 / 140，建议重试生成",
  cover_text_over_word_budget: "封面文字超过 3 个词，小尺寸下可能读不完",
  cover_text_title_overlap: "封面文字与标题实义词重复，建议与标题互补而非重复",
});

export function coverWarningLabel(code) {
  return COVER_WARNING_LABELS[code] || String(code || "");
}

// 出图后视觉复审视图模型：null = 未复审（旧版本/开关关闭）；
// {status:"succeeded", ok, issues, blockerCount} 或 {status:"failed", code}。
export function coverImageReviewView(raw) {
  if (!raw || typeof raw !== "object") return null;
  const status = String(raw.status || "");
  if (status === "failed") {
    return { status, code: String(raw.code || "cover_image_review_failed"), ok: false, issues: [], blockerCount: 0 };
  }
  if (status !== "succeeded") return null;
  const issues = (Array.isArray(raw.issues) ? raw.issues : []).map((issue) => ({
    kind: String(issue?.kind || "other"),
    severity: String(issue?.severity) === "blocker" ? "blocker" : "advice",
    description: String(issue?.description || "").trim(),
    suggestedEdit: String(issue?.suggested_edit || "").trim(),
  })).filter((issue) => issue.description);
  return {
    status,
    ok: raw.ok !== false,
    issues,
    blockerCount: issues.filter((issue) => issue.severity === "blocker").length,
    code: "",
  };
}

// 失败信息展示：{stage, code} → "阶段 · 错误码"；无失败返回空串。
export function coverFailureText(failure) {
  if (!failure || typeof failure !== "object") return "";
  const parts = [failure.stage, failure.code].map((part) => String(part || "").trim()).filter(Boolean);
  return parts.join(" · ");
}

// 评委结果展示模型：best 为 0 基序号（后端 validate_verdict），展示时换算为 1 基版本号。
export function coverJudgeSummary(judge) {
  if (!judge || typeof judge !== "object") return null;
  const bestIndex = Number.isInteger(Number(judge.best)) ? Number(judge.best) : 0;
  const rows = (Array.isArray(judge.verdicts) ? judge.verdicts : []).map((verdict) => ({
    version: Number(verdict?.version) || 0,
    total: Number(verdict?.total) || 0,
    comment: String(verdict?.comment || ""),
    isBest: Number(verdict?.version) === bestIndex + 1,
  }));
  if (!rows.length && !judge.advice) return null;
  return { rows, bestVersion: bestIndex + 1, advice: String(judge.advice || "") };
}

// 风格来源区（第一区）：频道当前风格版本 / 项目试验草稿 / 无参考 三态；
// preferred 为用户显式选择，缺省与后端默认一致（有频道风格用频道、否则项目草稿、否则无参考）。
export function coverStyleSourceModel({ channelStyle = null, projectStyle = null, preferred = null } = {}) {
  const profile = channelStyle?.current_profile || null;
  const channelText = String(profile?.edited_text || "").trim();
  const draftText = String(projectStyle?.draft?.edited_text || "").trim();
  const projectAvailable = Boolean(projectStyle?.draft?.has_analysis && draftText);
  const channelAvailable = Boolean(channelText);
  const mode = ["channel", "project", "none"].includes(preferred)
    ? preferred
    : channelAvailable
      ? "channel"
      : projectAvailable
        ? "project"
        : "none";
  return {
    mode,
    channel: {
      available: channelAvailable,
      version: profile?.version ?? null,
      label: String(profile?.label || ""),
      textPreview: channelText.slice(0, 200),
      updatedAt: profile?.created_at || "",
    },
    project: {
      available: projectAvailable,
      textPreview: draftText.slice(0, 200),
      updatedAt: projectStyle?.draft?.updated_at || "",
    },
    hint: channelAvailable || projectAvailable
      ? ""
      : "尚无频道风格与项目解析文本：可直接生成（无参考），或先在下方上传参考图并解析。",
  };
}

// 「更多设置」折叠容器（2026-10-21 用户确认的永久改造）：
// 锚点条 + 风格来源 / 参考图与勾选 / 生成输入 / 出场角色卡 / 提示词 默认收起；
// 封面图保持在原位置不参与折叠。折叠状态按项目记忆在 localStorage。
export function coverMoreSettingsKey(projectId) {
  return `tubeflow:cover-more-settings:${String(projectId || "")}`;
}

export function coverMoreSettingsOpen(stored) {
  return String(stored || "") === "open";
}

// 未冻结锁定：封面动作仅在项目冻结（生产快照）后开放；删除中项目同样锁定。
export function coverLockModel(project, fact) {  if (project?.production_state === "PURGING" || fact?.blocking_reason === "project_purging") {
    return { locked: true, title: "项目正在删除", detail: "项目删除期间封面功能不可用，删除完成后该项目数据将被物理清理。" };
  }
  if (!project?.snapshot || fact?.blocking_reason === "narration_not_confirmed") {
    return {
      locked: true,
      title: "封面功能在配音确认冻结后开放",
      detail: "项目尚未冻结：请先完成「配音设置」中的确认冻结，再回到本页生成封面。冻结前不开放参考图上传、解析与生成动作。",
    };
  }
  return { locked: false, title: "", detail: "" };
}

// 封面页五区视图模型：① 风格来源 ② 参考图与勾选 ③ 生成输入 ④ 提示词面板 ⑤ 封面图。
// 全部从 API 载荷推导展示态（在途禁用、未冻结锁定、空态文案），不触碰 DOM 与网络。
export function coverPageModel({
  fact = null,
  project = null,
  channelStyle = null,
  projectStyle = null,
  prompts = null,
  images = null,
  analyzing = false,
  styleSource = null,
  textChoice = null,
  analyzedDims = null,
} = {}) {
  const lock = coverLockModel(project, fact);
  const draft = projectStyle?.draft || null;
  const selectedDims = coverDimsWithDefault(draft?.dims);
  const tree = coverDimsTree(selectedDims);
  const counts = coverDimsCounts(tree);
  const dimsDirty = coverAnalysisDirty({
    hasAnalysis: Boolean(draft?.has_analysis),
    analyzedDims,
    selected: selectedDims,
  });
  const refImages = (Array.isArray(projectStyle?.ref_images) ? projectStyle.ref_images : []).map((image, index) => ({
    refImageId: String(image?.ref_image_id || ""),
    label: `参考图 ${index + 1}`,
    meta: [
      image?.width && image?.height ? `${image.width}×${image.height}` : "",
      image?.image_format || "",
      image?.byte_size ? `${(Number(image.byte_size) / 1024 / 1024).toFixed(2)} MB` : "",
    ].filter(Boolean).join(" · "),
    isSelected: Boolean(draft?.ref_image_id) && String(image?.ref_image_id || "") === String(draft.ref_image_id),
  }));
  const promptItems = Array.isArray(prompts?.items) ? prompts.items : [];
  const imageItems = Array.isArray(images?.items) ? images.items : [];
  const promptCurrent = prompts?.current || null;
  const imageCurrent = images?.current || null;
  const promptRunning = [promptCurrent, ...promptItems].some(coverTaskRunning);
  const imageRunning = [imageCurrent, ...imageItems].some(coverTaskRunning);
  const promptSucceeded = promptCurrent?.public_status === "SUCCEEDED";
  const imageSucceeded = imageCurrent?.public_status === "SUCCEEDED";
  const editedText = String(draft?.edited_text || "");
  const promptWarnings = Array.isArray(promptCurrent?.warnings) ? promptCurrent.warnings : [];
  return {
    locked: lock.locked,
    lockTitle: lock.title,
    lockDetail: lock.detail,
    styleSource: coverStyleSourceModel({ channelStyle, projectStyle, preferred: styleSource }),
    refEditor: {
      refImages,
      selectedRefImageId: draft?.ref_image_id ? String(draft.ref_image_id) : null,
      tree,
      checked: counts.checked,
      total: counts.total,
      editedText,
      editedTextMax: COVER_EDITED_TEXT_MAX,
      hasAnalysis: Boolean(draft?.has_analysis),
      analyzing,
      dimsDirty: dimsDirty.dirty,
      dimsDirtyNotice: dimsDirty.notice,
      canAnalyze: Boolean(draft?.ref_image_id) && counts.checked > 0 && !analyzing,
      canPromote: Boolean(editedText.trim()) && !analyzing,
      emptyImagesText: "尚未上传参考图：上传 jpg / png / webp（≤10MB）后勾选维度并解析。",
    },
    generateInput: {
      title: String(project?.title || ""),
      briefMax: COVER_BRIEF_MAX,
      overrideMax: COVER_EDITED_TEXT_MAX,
      promptMax: COVER_PROMPT_MAX,
      channelStyleProfiles: (Array.isArray(channelStyle?.profiles) ? channelStyle.profiles : []).map((profile, index) => ({
        profileId: String(profile?.profile_id || ""),
        version: Number.isInteger(Number(profile?.version)) ? Number(profile.version) : index + 1,
        label: String(profile?.label || ""),
        isCurrent: Boolean(profile?.is_current),
      })),
    },
    promptPanel: {
      running: promptRunning,
      emptyText: "尚未生成封面提示词：确认风格来源与补充需求后点击「生成封面提示词」。",
      current: promptCurrent ? {
        generationId: String(promptCurrent.generation_id || ""),
        version: promptCurrent.version,
        status: promptCurrent.public_status || "",
        succeeded: promptSucceeded,
        promptCurrent: String(promptCurrent.prompt_current || ""),
        promptSystem: String(promptCurrent.prompt_system || ""),
        edited: String(promptCurrent.prompt_current || "") !== String(promptCurrent.prompt_system || ""),
        brief: String(promptCurrent.brief || ""),
        styleOverrideText: String(promptCurrent.style_override_text || ""),
        styleSource: String(promptCurrent.style_source || ""),
        warnings: promptWarnings,
        lowScoreGuide: promptWarnings.includes("cover_judge_low_score") ? COVER_LOW_SCORE_GUIDE : "",
        characterCardsUsed: Array.isArray(promptCurrent.character_cards_used)
          ? promptCurrent.character_cards_used.map((card) => ({
            cardId: String(card?.card_id || ""),
            characterImageId: String(card?.character_image_id || ""),
            identityText: String(card?.identity_text || ""),
            appearanceText: String(card?.appearance_text || ""),
          }))
          : null,
        judge: coverJudgeSummary(promptCurrent.judge),
        failure: promptCurrent.failure || null,
        selector: coverCandidateSelector(promptCurrent, textChoice),
      } : null,
      versions: coverVersionList(promptItems),
    },
    imagePanel: {
      running: imageRunning,
      emptyText: "尚无封面图版本：可直接上传一张 16:9 图片，或在提示词就绪后生成封面图。",
      canGenerate: promptSucceeded && !imageRunning,
      current: imageCurrent ? {
        generationId: String(imageCurrent.generation_id || ""),
        version: imageCurrent.version,
        status: imageCurrent.public_status || "",
        succeeded: imageSucceeded,
        mediaUrl: imageCurrent.media_url || null,
        meta: [
          imageCurrent?.width && imageCurrent?.height ? `${imageCurrent.width}×${imageCurrent.height}` : "",
          imageCurrent?.model || "",
        ].filter(Boolean).join(" · "),
        failure: imageCurrent.failure || null,
        review: coverImageReviewView(imageCurrent.visual_review),
      } : null,
      versions: coverVersionList(imageItems),
    },
  };
}

// ⑥「角色绑定」区视图模型（v5 §1.4）：频道角色图清单 × 卡状态（有卡/无卡/在途解析）×
// 生成出场选择与顺序。默认选择 = 项目快照角色图 ∩ 已有卡，按快照顺序（identity 为空的卡
// 照后端语义视为无卡，不参与绑定）；selection 为 null 走默认，显式 [] 表示本轮不绑定角色，
// 显式列表按用户顺序出场（仅保留仍存在的卡）。
export function coverCharacterBindingModel({
  snapshotImageIds = [],
  channelImages = [],
  cards = null,
  selection = null,
  analyzingCardId = null,
} = {}) {
  const cardByImage = new Map();
  (Array.isArray(cards) ? cards : []).forEach((card) => {
    const imageId = String(card?.character_image_id || "");
    if (imageId) cardByImage.set(imageId, card);
  });
  const snapshotOrder = new Map(
    (Array.isArray(snapshotImageIds) ? snapshotImageIds : []).map((id, index) => [String(id), index]),
  );
  const knownCardIds = new Set();
  const rows = (Array.isArray(channelImages) ? channelImages : []).map((image, index) => {
    const imageId = String(image?.id || "");
    const card = cardByImage.get(imageId) || null;
    const cardId = String(card?.card_id || "");
    const identityText = String(card?.identity_text || "");
    if (cardId) knownCardIds.add(cardId);
    return {
      imageId,
      label: `角色图 ${index + 1}`,
      meta: [
        image?.width && image?.height ? `${image.width}×${image.height}` : "",
        image?.media_type || "",
      ].filter(Boolean).join(" · "),
      inSnapshot: snapshotOrder.has(imageId),
      snapshotIndex: snapshotOrder.has(imageId) ? snapshotOrder.get(imageId) : null,
      cardId: cardId || null,
      hasCard: Boolean(cardId),
      bindable: Boolean(cardId) && Boolean(identityText.trim()),
      identityText,
      appearanceText: String(card?.appearance_text || ""),
      source: String(card?.source || ""),
      analyzing: Boolean(cardId) && cardId === String(analyzingCardId || ""),
      selected: false,
      order: null,
    };
  });
  const defaultIds = rows
    .filter((row) => row.inSnapshot && row.bindable)
    .sort((a, b) => a.snapshotIndex - b.snapshotIndex)
    .map((row) => row.cardId);
  const explicit = Array.isArray(selection)
    ? selection.map((id) => String(id)).filter((id) => knownCardIds.has(id))
    : null;
  const selectedIds = explicit ?? defaultIds;
  rows.forEach((row) => {
    const position = row.cardId ? selectedIds.indexOf(row.cardId) : -1;
    row.selected = position >= 0;
    row.order = position >= 0 ? position + 1 : null;
  });
  const isDefault = explicit == null;
  return {
    available: cards != null,
    rows,
    defaultIds,
    selectedIds,
    isDefault,
    boundCount: selectedIds.length,
    snapshotCount: snapshotOrder.size,
    summaryText: isDefault
      ? selectedIds.length
        ? `默认绑定 ${selectedIds.length} 张角色卡（项目快照 ∩ 已有卡，按快照顺序）。`
        : "默认无可绑定角色卡：项目快照角色尚无已填身份的卡，本轮人物由 AI 自由撰写。"
      : selectedIds.length
        ? `已手动选择 ${selectedIds.length} 张角色卡（按列表顺序出场）。`
        : "已选择本轮不绑定角色：人物由 AI 按标题与文案自由撰写。",
    emptyText: "频道尚无角色图片：请先到频道管理页上传，再回这里维护角色卡。",
  };
}
