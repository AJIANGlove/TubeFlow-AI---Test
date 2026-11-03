// 项目列表与频道命名的展示层视图模型。
// 只放不依赖 DOM 的纯函数：频道显示名回退、项目流程状态徽章、封面媒体路径、
// 状态筛选归并、创建时间排序、列表空态分型与创建时间格式化，
// 便于 node --test 覆盖；app.js 负责渲染与事件绑定。
// 状态一律来自后端 flow 事实；flow 缺失时明确显示“状态不可用”，不伪造进度。

export function channelDisplayName(channel, index) {
  const name = String(channel?.name ?? "").trim();
  if (name) return name;
  return `频道 ${String(index + 1).padStart(2, "0")}`;
}

// 删除频道的确认值（后端同口径）：有名称用完整名称，未命名用完整 URL。
// 不使用 channelDisplayName 的「频道 NN」——它由列表序号派生，删除后会漂移且后端不可知。
export function channelDeletionConfirmation(channel) {
  const name = String(channel?.name ?? "").trim();
  if (name) return name;
  return String(channel?.url ?? "");
}

export function projectFlowBadge(project) {
  const flow = project?.flow;
  if (!flow) return { text: "状态不可用", tone: "muted" };
  if (flow.attention) return { text: "需要恢复", tone: "error" };
  switch (flow.stage) {
    case "DRAFT":
      return { text: "草稿可编辑", tone: "muted" };
    case "NARRATION": {
      const sentences = Number(flow.sentences) || 0;
      const ready = Number(flow.ready_sentences) || 0;
      return {
        text: sentences > 0 ? `配音进行中 ${ready}/${sentences}` : "配音进行中",
        tone: "purple",
      };
    }
    case "DIRECTING":
      return { text: "导演方案进行中", tone: "purple" };
    case "STORYBOARD":
      return { text: "故事板生成中", tone: "purple" };
    case "VIDEO":
      return {
        text: flow.blocking_reason === "videos_incomplete" ? "视频待生成" : "视频生成中",
        tone: "purple",
      };
    case "VIDEO_READY":
      return Number(flow.compositions) > 0
        ? { text: "视频生产完成", tone: "green" }
        : { text: "待合成成片", tone: "amber" };
    case "DELETING":
      return { text: "删除中", tone: "muted" };
    default:
      // 罕见不一致态：展示 stage 原文兜底，不猜测语义。
      return { text: typeof flow.stage === "string" && flow.stage ? flow.stage : "未知状态", tone: "muted" };
  }
}

export const ACTIVE_CHANNEL_STORAGE_KEY = "tf-active-channel";
export const ALL_CHANNELS_STORAGE_VALUE = "all";

// 人工发布标记：published_at 是唯一事实源；门禁只看当前是否仍有合成成片。
export function projectIsPublished(project) {
  return Boolean(project?.published_at);
}

export function projectPublicationState(project) {
  if (projectIsPublished(project)) return "published";
  return Number(project?.flow?.compositions) > 0 ? "publishable" : "unpublished";
}

// 频道偏好：存频道 id 或 "all"；失效/缺失回落到第一个频道，无频道时为 null。
export function resolveActiveChannelId(channels, storedValue) {
  const ids = (Array.isArray(channels) ? channels : [])
    .map((channel) => channel?.id)
    .filter(Boolean);
  if (storedValue === ALL_CHANNELS_STORAGE_VALUE) return null;
  if (storedValue && ids.includes(storedValue)) return storedValue;
  return ids[0] || null;
}

export function activeChannelStorageValue(channelId) {
  return channelId || ALL_CHANNELS_STORAGE_VALUE;
}

export function formatDateTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// 项目列表封面：只有后端同时给出当前封面标记与生成 id 时才提供受保护媒体路径，否则交给渐变占位。
export function projectCoverMediaPath(project) {
  if (!project?.has_current_cover) return null;
  const generationId = project?.current_cover_image_generation_id;
  if (!project?.id || typeof generationId !== "string" || !generationId) return null;
  return `/projects/${project.id}/cover/images/${generationId}/media`;
}

// 状态筛选归并与 projectFlowBadge 文案一一对应：
// 「需要恢复」→ attention；「视频生产完成」→ done；「草稿可编辑」→ draft；其余进行类→ in_progress。
export function projectFilterCategory(project) {
  const badge = projectFlowBadge(project);
  if (badge.text === "需要恢复") return "attention";
  if (badge.text === "视频生产完成") return "done";
  if (badge.text === "草稿可编辑") return "draft";
  return "in_progress";
}

// 筛选 chips：前四个是流程维度（草稿并入「进行中」），未发布/已发布是发布维度。
export function projectMatchesStatusFilter(project, filter) {
  if (!filter || filter === "all") return true;
  if (filter === "unpublished") return !projectIsPublished(project);
  if (filter === "published") return projectIsPublished(project);
  const category = projectFilterCategory(project);
  if (filter === "in_progress") return category === "in_progress" || category === "draft";
  return category === filter;
}

export function projectStatusCounts(projects) {
  const counts = { all: 0, in_progress: 0, done: 0, attention: 0 };
  for (const project of Array.isArray(projects) ? projects : []) {
    counts.all += 1;
    const category = projectFilterCategory(project);
    if (category === "done") counts.done += 1;
    else if (category === "attention") counts.attention += 1;
    else counts.in_progress += 1;
  }
  return counts;
}

export function sortProjectsByCreated(projects, order = "newest") {
  const direction = order === "oldest" ? 1 : -1;
  const timeOf = (project) => {
    const time = new Date(project?.created_at || "").getTime();
    return Number.isNaN(time) ? 0 : time;
  };
  return (Array.isArray(projects) ? [...projects] : [])
    .sort((a, b) => (timeOf(a) - timeOf(b)) * direction);
}

export function visibleProjects(projects, options = {}, titleOf = (project) => project?.title || "") {
  const query = String(options.query || "").trim().toLowerCase();
  const filtered = (Array.isArray(projects) ? projects : []).filter((project) => {
    if (query && !String(titleOf(project)).toLowerCase().includes(query)) return false;
    return projectMatchesStatusFilter(project, options.filter);
  });
  return sortProjectsByCreated(filtered, options.sort);
}

// 列表空态分型：搜索无匹配、筛选无匹配与真正的无项目必须区分，文案与出口各不相同。
export function projectListEmptyKind(totalCount, visibleCount, query, filter) {
  if (visibleCount > 0) return null;
  if (!totalCount) return "empty";
  if (String(query || "").trim()) return "search";
  if (filter && filter !== "all") return "filter";
  return "empty";
}
