// 聚合进度视图（§15 一键自动生产，仅 AUTO 项目）的展示层视图模型。
// 只放不依赖 DOM 的纯函数：auto_pipeline 状态/阶段文案、暂停原因映射与全链路阶段行模型，
// 便于 node --test 直接覆盖；app.js 负责渲染、轮询与事件绑定。
// 全部数据来自 workflow-summary 权威响应；未知状态码/阶段码/原因码一律回退为后端原文，不猜测语义。

const AUTO_PIPELINE_STATE_LABELS = Object.freeze({
  RUNNING: "自动生产进行中",
  PAUSED: "自动生产已暂停",
  COMPLETED: "视频生产完成",
});

// 返回规范化状态（RUNNING/PAUSED/COMPLETED）；auto_pipeline 缺失或未知值一律为 null。
export function autoPipelineState(summary) {
  const state = summary?.auto_pipeline?.state;
  return Object.hasOwn(AUTO_PIPELINE_STATE_LABELS, state) ? state : null;
}

export function autoPipelineStateLabel(state) {
  return AUTO_PIPELINE_STATE_LABELS[state] || "等待提交";
}

// 全链路阶段码 → 人话，取值与后端 auto_pipeline.phase 八值一一对应；未知码回退后端原文。
const AUTO_PHASE_LABELS = Object.freeze({
  narration: "真实配音",
  director_preplan: "前置导演",
  director_materialization: "确定性重映射",
  storyboard_drafts: "导演草稿播种",
  storyboard: "故事板生成",
  video: "组级视频生成",
  composition: "合成",
  completed: "完成",
});

export function autoPhaseLabel(phase) {
  if (!phase) return "—";
  return AUTO_PHASE_LABELS[phase] || String(phase);
}

// 阶段顺序（与后端 AUTO_PIPELINE_PHASES 一致；仅用于用时覆盖范围判断）。
const AUTO_PHASE_ORDER = Object.keys(AUTO_PHASE_LABELS);

// 5 行各自对应的后端阶段；「导演规划」为三段相加（用户 2026-10-17 定稿）。
const AUTO_ROW_PHASES = Object.freeze({
  narration: ["narration"],
  director: ["director_preplan", "director_materialization", "storyboard_drafts"],
  storyboard: ["storyboard"],
  video: ["video"],
  composition: ["composition"],
});

// 用时展示：秒级；<1 分钟显示秒，<1 小时显示分秒，≥1 小时显示时+两位分。
// 未知/缺数据返回「—」，绝不伪造 0。
export function formatElapsedMs(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  const totalSeconds = Math.floor(value / 1000);
  if (totalSeconds < 60) return `${totalSeconds}秒`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return seconds ? `${minutes}分${seconds}秒` : `${minutes}分`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes
    ? `${hours}小时${String(restMinutes).padStart(2, "0")}分`
    : `${hours}小时`;
}

// 5 行用时模型：只读后端 timing；返回 {rowKey: {elapsedMs, running} | null}。
// 覆盖规则：行的首个阶段早于 timing.from_phase 时整行「—」，避免历史数据少算。
export function autoStageTiming(summary) {
  const result = {
    narration: null,
    director: null,
    storyboard: null,
    video: null,
    composition: null,
  };
  const timing = summary?.auto_pipeline?.timing;
  if (!timing || typeof timing !== "object") return result;
  const fromIndex = AUTO_PHASE_ORDER.indexOf(timing.from_phase);
  if (fromIndex < 0) return result;
  const phases = timing.phases && typeof timing.phases === "object" ? timing.phases : {};
  for (const [rowKey, phaseKeys] of Object.entries(AUTO_ROW_PHASES)) {
    if (AUTO_PHASE_ORDER.indexOf(phaseKeys[0]) < fromIndex) continue;
    let elapsed = 0;
    let entered = false;
    let running = false;
    for (const phase of phaseKeys) {
      const entry = phases[phase];
      if (!entry || typeof entry !== "object") continue;
      const value = entry.elapsed_ms;
      if (typeof value !== "number" || !Number.isFinite(value) || value < 0) continue;
      elapsed += value;
      entered = true;
      if (entry.running === true) running = true;
    }
    if (entered) result[rowKey] = { elapsedMs: elapsed, running };
  }
  return result;
}

// 暂停稳定原因码 → 人话（与后端 auto_pipeline.reason_code 集合一一对应；
// storyboard_failed:/video_failed: 为带子码的前缀码）。
// 未知码返回 null，由调用方展示后端原文码与补充说明。
const AUTO_PAUSE_REASON_LABELS = Object.freeze({
  narration_submission_budget_exhausted: "配音自动重试预算已用尽（单句 3 次）",
  narration_provider_rejected: "配音被服务商既定拒绝",
  narration_archive_failed: "配音音频归档失败",
  narration_submission_incomplete: "配音提交中断，待恢复",
  director_preplan_failed: "前置导演自动重试预算已用尽",
  director_validation_failed: "导演方案校验未通过，需人工修正",
  oversized_sentence: "存在实测超过 15 秒的配音句",
  composition_failed: "合成失败（既定失败，未自动重试）",
  // 2026-10-31：H3 容量不足改为排队等待，只有等待超过 6 小时才以该原因暂停。
  video_capacity_timeout: "等待 H3 视频算力超时（排队超过 6 小时）",
});

const AUTO_PAUSE_SUBCODE_LABELS = Object.freeze({
  submission_budget_exhausted: "自动重试/重提预算已用尽（每组 6 次）",
  failover_budget_exhausted: "备用渠道与模型已按序尝试，仍未成功（每组 14 次、每模型最多 2 次）",
  remote_state_unknown_exhausted: "远端状态未知且重提预算已用尽",
  intent_rejected: "生成意图被既定拒绝",
});

export function autoPauseReasonLabel(code) {
  if (AUTO_PAUSE_REASON_LABELS[code]) return AUTO_PAUSE_REASON_LABELS[code];
  for (const [prefix, stageLabel] of [["storyboard_failed:", "故事板"], ["video_failed:", "组级视频"]]) {
    if (typeof code === "string" && code.startsWith(prefix)) {
      const sub = code.slice(prefix.length);
      return `${stageLabel}生成失败：${AUTO_PAUSE_SUBCODE_LABELS[sub] || sub || "未知原因"}`;
    }
  }
  return null;
}

// 暂停原因展示模型：code 取 auto_pipeline.reason_code，回退顶层 blocking_reason；
// label 优先人话映射，message 为后端补充说明或由 details 白名单组成；
// groupSequences 为导演校验暂停涉及的组号（无则空数组）。
export function autoPauseReason(summary) {
  const pipeline = summary?.auto_pipeline || null;
  const code = pipeline?.reason_code || summary?.blocking_reason || "";
  const groupSequences = pauseGroupSequences(pipeline?.details);
  return {
    code,
    label: autoPauseReasonLabel(code) || code || "未知原因",
    message: pipeline?.message || autoPauseDetailMessage(code, groupSequences, pipeline?.details),
    groupSequences,
  };
}

// 后端 details 白名单 → 补充说明；失败原因最多列前 2 条避免刷屏；无可用细节时为空串。
function autoPauseDetailMessage(code, groupSequences, details) {
  if (code === "composition_failed") {
    // 合成失败必须回显后端稳定码，否则用户只能看到「既定失败」而无法定位。
    const failure = typeof details?.error_code === "string" ? details.error_code.trim() : "";
    return failure ? `失败码：${failure}。` : "";
  }
  if (code !== "director_validation_failed") return "";
  const parts = [];
  if (groupSequences.length) {
    const list = groupSequences.map((n) => String(n).padStart(2, "0")).join("、");
    parts.push(`导演方案校验未通过的分组：${list}（共 ${groupSequences.length} 组）。`);
  }
  const failures = Object.entries(details?.failure_messages || {});
  if (failures.length) {
    const shown = failures.slice(0, 2).map(([seq, message]) => `第 ${seq} 组：${message}`);
    parts.push(`失败原因：${shown.join("；")}${failures.length > 2 ? " 等" : ""}`);
  }
  return parts.join("");
}

function pauseGroupSequences(details) {
  const list = Array.isArray(details?.group_sequences) ? details.group_sequences : [];
  return list.map(Number).filter((n) => Number.isInteger(n) && n > 0);
}

// RUNNING/PAUSED 期间阶段级手动生成入口在本地锁定（后端同时从 allowed_actions 收口）。
export function autoPipelineBusy(summary) {
  return ["RUNNING", "PAUSED"].includes(autoPipelineState(summary));
}

// 恢复按钮门禁：仅 PAUSED 且后端 allowed_actions 明确开放 RESUME_AUTO_PIPELINE。
export function autoResumeAllowed(summary) {
  return autoPipelineState(summary) === "PAUSED"
    && Array.isArray(summary?.allowed_actions)
    && summary.allowed_actions.includes("RESUME_AUTO_PIPELINE");
}

function numberOrZero(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

// 全链路阶段行模型：配音 x/y 句、导演前置单元、故事板 x/y 组、视频 x/y 组、合成。
// 每行携带对应 stages[] 事实（status/blocking_reason），由 app.js 翻译为状态文案。
export function autoStageRows(summary) {
  const stages = Array.isArray(summary?.stages) ? summary.stages : [];
  const byKey = new Map(stages.map((stage) => [stage?.key, stage]));
  const fact = (key) => byKey.get(key) || null;
  const countsOf = (key) => fact(key)?.counts || {};
  const narration = countsOf("narration_timeline");
  const director = countsOf("director_storyboard");
  const video = countsOf("group_video_preview");
  const composition = countsOf("composition_export");
  const groups = numberOrZero(video.groups ?? director.groups ?? narration.groups);
  const rows = [
    {
      key: "narration",
      label: "真实配音",
      fact: fact("narration_timeline"),
      detail: `${numberOrZero(narration.ready_sentences)} / ${numberOrZero(narration.sentences)} 句`,
    },
    {
      key: "director",
      label: "导演规划",
      fact: fact("director_storyboard"),
      detail: numberOrZero(director.preplan_total_units)
        ? `前置 ${numberOrZero(director.preplan_completed_units)} / ${numberOrZero(director.preplan_total_units)} 单元`
        : "—",
    },
    {
      key: "storyboard",
      label: "故事板",
      fact: fact("director_storyboard"),
      detail: `${numberOrZero(director.storyboard_groups)} / ${groups} 组`,
    },
    {
      key: "video",
      label: "组级视频",
      fact: fact("group_video_preview"),
      detail: `${numberOrZero(video.video_groups)} / ${numberOrZero(video.groups ?? groups)} 组`,
    },
    {
      key: "composition",
      label: "合成成片",
      fact: fact("composition_export"),
      detail: numberOrZero(composition.current_compositions) > 0 ? "已产出当前成片" : "未合成",
    },
  ];
  const timings = autoStageTiming(summary);
  return rows.map((row) => {
    const timing = timings[row.key];
    return {
      ...row,
      elapsedMs: timing ? timing.elapsedMs : null,
      elapsedRunning: timing ? timing.running : false,
    };
  });
}
