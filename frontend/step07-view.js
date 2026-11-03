// Step 07（导演与故事板）页面的展示层视图模型。
// 这里只放不依赖 DOM 的纯函数：状态推导、文案映射与显示规则，
// 便于用 node --test 直接覆盖；app.js 负责渲染与事件绑定。

const DIRECTOR_ACTION_LABELS = Object.freeze({
  VIEW_CURRENT: "查看当前",
  VIEW_MEDIA: "预览媒体",
  REGENERATE_DIRECTOR_PREPLAN: "重新处理前置导演",
  MANUAL_REVIEW: "人工证据处置",
  EDIT_FINAL_DIRECTOR: "编辑最终导演",
  EDIT_DIRECTOR_DRAFT: "编辑导演草稿",
  GENERATE_STORYBOARD: "生成故事板",
  SELECT_CURRENT: "切换为当前版本",
  RECONCILE: "对账任务",
  CANCEL: "取消任务",
  RETRY: "重试",
});

export function actionLabel(action) {
  return DIRECTOR_ACTION_LABELS[action] || String(action || "");
}

export function actionListText(actions) {
  const items = Array.isArray(actions) ? actions : [];
  return items.length ? items.map(actionLabel).join(" / ") : "无";
}

// 取值与 backend/src/tubeflow_test/identity_project/service.py 的 workflow_summary 门禁逐一对应。
const BLOCKING_REASON_LABELS = Object.freeze({
  previous_stage_incomplete: "前置阶段未完成",
  project_purging: "项目正在删除",
  narration_not_confirmed: "配音未确认",
  narration_not_ready: "配音尚未完成",
  narration_not_ready_for_final_mapping: "配音未就绪，暂不能重映射",
  director_preplan_not_started: "前置导演未开始",
  director_preplan_queued: "前置导演已排队",
  director_preplan_running: "前置导演处理中",
  director_preplan_failed: "前置导演失败",
  director_preplan_remote_state_unknown: "前置导演远端状态未知",
  director_preplan_authority_inconsistent: "前置导演数据不一致",
  director_materialization_pending: "确定性重映射处理中",
  final_director_review_required: "待确认最终导演",
  director_authority_inconsistent: "导演数据不一致",
  storyboard_authority_inconsistent: "故事板数据不一致",
  video_authority_inconsistent: "视频数据不一致",
  composition_has_blocked_groups: "存在被阻止的分组",
  storyboard_attempt_running: "故事板任务进行中",
  storyboard_attempt_failed: "故事板任务失败",
  storyboard_attempt_cancelled: "故事板任务已取消",
  storyboard_attempt_remote_state_unknown: "故事板任务远端状态未知",
  storyboard_attempt_status_unknown: "故事板任务状态未知",
  video_attempt_running: "视频任务进行中",
  video_attempt_failed: "视频任务失败",
  video_attempt_cancelled: "视频任务已取消",
  video_attempt_remote_state_unknown: "视频任务远端状态未知",
  video_attempt_status_unknown: "视频任务状态未知",
  composition_attempt_running: "合成任务进行中",
  composition_attempt_failed: "合成任务失败",
  composition_attempt_cancelled: "合成任务已取消",
  composition_attempt_remote_state_unknown: "合成任务远端状态未知",
  composition_attempt_status_unknown: "合成任务状态未知",
});

// 返回 null 表示没有已知人话映射，调用方回退为后端原文。
export function blockingReasonLabel(reason) {
  if (!reason) return null;
  return BLOCKING_REASON_LABELS[reason] || null;
}

const REVIEW_STATE_LABELS = Object.freeze({
  REQUIRED: "待人工修正",
  REVIEW_REQUIRED: "待人工修正",
  NOT_REQUIRED: "无需变更",
});

export function reviewStateLabel(value) {
  if (value == null || value === "") return "";
  return REVIEW_STATE_LABELS[value] || String(value);
}

// 材料化（确定性重映射）面板的徽标文案：优先把评审态翻译成人话。
export function materializationStatusLabel(materialization) {
  const status = materialization?.status || materialization?.public_status || "";
  return REVIEW_STATE_LABELS[status] || status || "未知状态";
}

const ALGORITHM_VERSION_PATTERN = /^(?:controlled-semantic-)?lineage-remap-(.+)$/;

export function algorithmVersionLabel(version) {
  if (!version) return "—";
  const match = ALGORITHM_VERSION_PATTERN.exec(String(version));
  return match ? `确定性重映射 ${match[1]}` : String(version);
}

export function nonNegativeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

// “全部完成且无异常”的前置导演运行：终态成功、无在途、无重试、无未提交、无失败批次。
export function preplanRunIsCalm(run) {
  if (!run) return false;
  const total = nonNegativeNumber(run.total_units);
  const completed = nonNegativeNumber(run.completed_units);
  return run.status === "SUCCEEDED"
    && total > 0
    && completed >= total
    && nonNegativeNumber(run.inflight_units) === 0
    && nonNegativeNumber(run.retry_pending_units) === 0
    && nonNegativeNumber(run.unsent_units) === 0
    && !run.failure_code
    && run.failed_unit_ordinal == null;
}

export function preplanRunCalmSummary(run) {
  const total = nonNegativeNumber(run?.total_units);
  const completed = nonNegativeNumber(run?.completed_units);
  return `前置导演 ${completed}/${total} 已完成 · 无在途、无失败批次`;
}

// 分组导航圆点状态：只使用页面已加载的权威数据推导，不新增请求。
//   blocked —— 已被标记阻止视频生成；
//   review  —— 导演候选需人工修正或存在冲突；
//   ready   —— 导演候选就绪（无需变更）；
//   missing —— 尚无该组导演候选。
export function groupCandidateTone(group, candidate) {
  if (group?.video_blocked) return "blocked";
  if (!candidate) return "missing";
  const conflicts = Array.isArray(candidate.conflicts) ? candidate.conflicts : [];
  if (["REQUIRED", "REVIEW_REQUIRED"].includes(candidate.review_state) || conflicts.length > 0) {
    return "review";
  }
  if (candidate.review_state === "NOT_REQUIRED") return "ready";
  return "missing";
}

export const GROUP_TONE_LABELS = Object.freeze({
  blocked: "已阻止视频生成",
  review: "导演候选需人工修正",
  ready: "导演候选就绪",
  missing: "尚无导演候选",
});

export function groupCandidateAnnotation(group, candidate) {
  const tone = groupCandidateTone(group, candidate);
  return { tone, label: GROUP_TONE_LABELS[tone] };
}

// 组视频生成任务在途阶段（后端 attempt 公开阶段合同的前端镜像；与 app.js 批量进度条共用同一份事实）。
export const VIDEO_ATTEMPT_IN_FLIGHT = Object.freeze(["CREATED", "STAGING", "NOT_SUBMITTED", "SUBMITTING", "SUBMITTED", "RUNNING", "SUCCEEDED"]);

// 第 08 步分组 tabs 圆点注解：与第 07 步共用 ready/review/blocked/missing 色板，语义换成组视频进度。
export const VIDEO_GROUP_TAB_TONE_LABELS = Object.freeze({
  blocked: "已阻止视频生成",
  review: "视频任务在途",
  ready: "已有当前视频",
  missing: "尚未生成视频",
});

export function videoGroupTabTone(item) {
  if (item?.video_blocked) return "blocked";
  if (item?.has_current_video) return "ready";
  if (VIDEO_ATTEMPT_IN_FLIGHT.includes(item?.latest_attempt_stage)) return "review";
  return "missing";
}

// 已就绪音频的实测时长低于 1 秒时视为异常（页面上 00:00 这类显示）。
export function narrationDurationIsSuspicious(measuredMs) {
  const value = Number(measuredMs);
  return Number.isFinite(value) && value > 0 && value < 1000;
}

// 冲突条目的人话摘要 + 技术详情。技术详情保留后端原始 code / id，不吞掉排障信息。
export function conflictText(conflict) {
  if (typeof conflict === "string") return conflict;
  if (!conflict || typeof conflict !== "object") return "后端返回未分类质量冲突";
  const sentenceIds = Array.isArray(conflict.sentence_ids) && conflict.sentence_ids.length
    ? `句子 ${conflict.sentence_ids.join("、")}`
    : null;
  const shotIds = Array.isArray(conflict.shot_ids) && conflict.shot_ids.length
    ? `分镜 ${conflict.shot_ids.join("、")}`
    : null;
  return [conflict.code, conflict.message, conflict.shot_id, conflict.sentence_id, sentenceIds, shotIds]
    .filter(Boolean)
    .map(String)
    .join(" · ") || "后端返回未分类质量冲突";
}

// 取值与 backend/src/tubeflow_test/application/project_director.py 中 ProjectDirectorConflict 的 code 对应；
// 该合同没有 message 字段，人话摘要必须从这里映射。
const DIRECTOR_CONFLICT_LABELS = Object.freeze({
  source_preplan_quality_conflict: "继承自前置方案的质量冲突",
  adjacent_meaningful_change_insufficient: "相邻分镜的有效叙事或构图变化不足",
  unusually_many_shots: "同一句话关联了过多分镜",
  repeated_non_adjacent_event: "非相邻分镜重复同一叙事事件",
  repeated_non_adjacent_composition: "非相邻分镜重复同一构图",
  sentence_coverage_missing: "存在未被任何分镜覆盖的句子",
  group_shot_limit_exceeded: "分镜数量超过每组上限",
  group_has_no_shots: "本组没有任何分镜",
  group_diversity_invalid: "分镜叙事多样性未通过校验",
});

export function conflictSummary(conflict) {
  if (typeof conflict === "string") return conflict;
  if (!conflict || typeof conflict !== "object") return "后端返回未分类质量冲突";
  if (conflict.code && DIRECTOR_CONFLICT_LABELS[conflict.code]) {
    return DIRECTOR_CONFLICT_LABELS[conflict.code];
  }
  return String(conflict.message || conflict.code || "后端返回未分类质量冲突");
}

export function conflictShotId(conflict) {
  if (!conflict || typeof conflict !== "object") return "";
  if (conflict.shot_id) return String(conflict.shot_id);
  const ids = Array.isArray(conflict.shot_ids) ? conflict.shot_ids : [];
  return ids.length ? String(ids[0]) : "";
}

// ——— 列表视图（分组卡片网格）与详情视图的展示模型 ———

// 列表视图筛选栏选项；no-storyboard 只看 overview 的 current_generation_id 是否缺失。
export const DIRECTOR_GROUP_FILTERS = Object.freeze([
  { key: "all", label: "全部" },
  { key: "ready", label: "候选就绪" },
  { key: "review", label: "待人工修正" },
  { key: "blocked", label: "已阻止" },
  { key: "missing", label: "尚无候选" },
  { key: "no-storyboard", label: "无故事板" },
]);

// 分组卡片展示模型：只推导展示事实，不触碰 DOM 与网络。
//   group        —— 权威配音分组（group_id / sequence / 起止与目标时长 / video_blocked）；
//   candidate    —— 确定性重映射候选（可为 null；有 shots 时给出分镜条数）；
//   overviewItem —— storyboard-batch/overview 对应条目（可为 null；给出当前故事板版本）；
//   sentences    —— 本组旁白句子（ narration status 的 sentences 成员）；
//   timeline     —— narration status 的实测时间线，优先于 normalized_qc 的实测时长。
export function groupCardModel({ group, index = 0, candidate = null, overviewItem = null, sentences = [], timeline = [] } = {}) {
  const annotation = groupCandidateAnnotation(group, candidate);
  const durationMs = Number(group?.target_duration_ms ?? (Number(group?.end_ms || 0) - Number(group?.start_ms || 0)) ?? 0);
  const timelineBySequence = new Map(
    (Array.isArray(timeline) ? timeline : []).map((entry) => [Number(entry?.sentence_sequence), entry]),
  );
  const sentenceCards = (Array.isArray(sentences) ? sentences : []).map((item) => {
    const sequence = Number(item?.sentence?.sequence ?? item?.sequence ?? item?.sentence_sequence ?? 0);
    const entry = timelineBySequence.get(sequence) || null;
    const measuredMs = entry
      ? Number(entry.end_ms) - Number(entry.start_ms)
      : Number(item?.normalized_qc?.measured_duration_ms || 0);
    return {
      sequence,
      text: item?.sentence?.text || item?.text || "",
      durationMs: measuredMs,
      suspicious: narrationDurationIsSuspicious(measuredMs),
    };
  });
  const sequence = Number(group?.sequence ?? index + 1);
  return {
    groupId: group?.group_id || group?.id || "",
    sequence: Number.isInteger(sequence) && sequence > 0 ? sequence : index + 1,
    tone: annotation.tone,
    toneLabel: annotation.label,
    durationMs: Number.isFinite(durationMs) && durationMs > 0 ? durationMs : 0,
    shotCount: Array.isArray(candidate?.shots) ? candidate.shots.length : null,
    storyboardStatus: overviewItem?.storyboard_status || null,
    generationId: overviewItem?.current_generation_id || null,
    sentences: sentenceCards,
  };
}

// 纯客户端筛选：all 与未知筛选键都视为全部；no-storyboard 匹配无当前故事板版本的组。
export function groupCardMatchesFilter(model, filter) {
  if (!model) return false;
  if (filter === "no-storyboard") return !model.generationId;
  if (["ready", "review", "blocked", "missing"].includes(filter)) return model.tone === filter;
  return true;
}

// 「第 [n]/N 组」输入解析：只接受 1..groupCount 的整数；非法输入返回 null（调用方 toast 且不切换）。
export function parseGroupSequenceInput(value, groupCount) {
  const count = Math.max(0, Math.floor(Number(groupCount) || 0));
  if (!count) return null;
  const text = String(value ?? "").trim();
  if (!/^\d+$/.test(text)) return null;
  const sequence = Number(text);
  return sequence >= 1 && sequence <= count ? sequence : null;
}

// URL hash 记忆：#/project/{projectId}/{stageKey} 与详情专用的 …/director_storyboard/group/{组号}。
// 返回 { projectId, stage, groupSequence|null }；任何畸形输入都返回 null，由调用方忽略。
export function directorViewFromHash(hash) {
  const match = /^#\/project\/([^/]+)\/([a-z0-9_]+)(?:\/group\/(\d+))?\/?$/.exec(String(hash || "").trim());
  if (!match) return null;
  const [, projectId, stage, groupRaw] = match;
  if (groupRaw == null) return { projectId, stage, groupSequence: null };
  if (stage !== "director_storyboard") return null;
  const groupSequence = Number(groupRaw);
  if (!Number.isInteger(groupSequence) || groupSequence < 1) return null;
  return { projectId, stage, groupSequence };
}

// 组号无效或缺失时回落到列表视图 hash，绝不伪造组号。
export function hashForDirectorView(projectId, groupSequence = null) {
  const base = `#/project/${projectId}/director_storyboard`;
  const sequence = Number(groupSequence);
  return Number.isInteger(sequence) && sequence > 0 ? `${base}/group/${sequence}` : base;
}
