import { ApiClient, ApiError, createIdempotencyKey, duration, mmss, srtDownloadFilename, unavailable } from "./frontend/api-client.js";
import {
  actionListText,
  algorithmVersionLabel,
  blockingReasonLabel,
  conflictShotId,
  conflictSummary,
  conflictText,
  DIRECTOR_GROUP_FILTERS,
  directorViewFromHash,
  groupCandidateAnnotation,
  groupCardMatchesFilter,
  groupCardModel,
  hashForDirectorView,
  materializationStatusLabel,
  narrationDurationIsSuspicious,
  parseGroupSequenceInput,
  preplanRunCalmSummary,
  preplanRunIsCalm,
  reviewStateLabel,
  VIDEO_ATTEMPT_IN_FLIGHT,
  VIDEO_GROUP_TAB_TONE_LABELS,
  videoGroupTabTone,
} from "./frontend/step07-view.js";
import {
  VOICE_PAGE_SIZE,
  compatibleLocaleVoicePatch,
  filterVoices,
  paginateVoices,
  shortStableId,
  voiceFacetValues,
  voiceIdentity,
  voiceSupportsLocale,
} from "./frontend/voice-library.js";
import {
  confirmWarningText,
  previewRows,
  scriptDraftCounts,
  summarizePreflight,
} from "./frontend/script-preview.js";
import { hasCopyableText, scriptCopyText } from "./frontend/clipboard-copy.js";
import {
  ACTIVE_CHANNEL_STORAGE_KEY,
  activeChannelStorageValue,
  channelDeletionConfirmation,
  channelDisplayName,
  formatDateTime,
  projectCoverMediaPath,
  projectFlowBadge,
  projectIsPublished,
  projectListEmptyKind,
  projectPublicationState,
  projectStatusCounts,
  resolveActiveChannelId,
  visibleProjects,
} from "./frontend/project-list-display.js";
import {
  MAX_DEFAULT_CHARACTER_IMAGES,
  normalizeDefaultCharacterIds,
  resolveDefaultCharacterImages,
  toggleDefaultCharacterId,
} from "./frontend/channel-persona.js";
import { passwordPairIssue } from "./frontend/dialog-rules.js";
import {
  COSYVOICE,
  ELEVENLABS,
  channelDefaultPatch,
  channelDefaultVoiceReady,
  cloneOutcomeSummary,
  clonedVoiceViews,
  frozenNarrationSummary,
  providerOf,
  ttsProviderLabel,
  validateClonedVoiceUpload,
  voiceAssignment,
} from "./frontend/el-voices.js";
import { LOCALES, localeLabel, localeShortListText } from "./frontend/locale-labels.js";

import { narrationRows, retryableNarrationRows, narrationIsRunning, NARRATION_RETRY_EVIDENCE } from "./frontend/narration-view.js";
import {
  compositionDefectNotice,
  groupVideoDefectNotice,
} from "./frontend/video-defect.js";
import {
  autoPauseReason,
  autoPhaseLabel,
  autoPipelineBusy,
  autoPipelineState,
  autoPipelineStateLabel,
  autoResumeAllowed,
  autoResumeFailureMessage,
  autoStageRows,
  formatElapsedMs,
} from "./frontend/auto-progress.js";
import {
  COVER_BRIEF_MAX,
  COVER_CHARACTER_APPEARANCE_MAX,
  COVER_CHARACTER_IDENTITY_MAX,
  COVER_DIMS_GUIDE,
  COVER_EDITED_TEXT_MAX,
  COVER_PROMPT_MAX,
  COVER_STYLE_LABEL_MAX,
  coverAnalysisDirty,
  coverCharacterBindingModel,
  coverDimsCounts,
  coverDimsTree,
  coverDimsWithDefault,
  coverFailureText,
  coverLockModel,
  coverMoreSettingsKey,
  coverMoreSettingsOpen,
  coverPageModel,
  coverStyleSourceModel,
  coverTaskRunning,
  coverUploadErrorMessage,
  coverVersionList,
  coverWarningLabel,
  dimsFromPayload,
  toggleCoverChild,
  toggleCoverDim,
} from "./frontend/cover-view.js";
import {
  COVER_TEMPLATE_PROMPT_LIMIT,
  clipTemplateName,
  clipTemplatePrompt,
  countTemplatePrompt,
  coverMoreSettingsMarkup,
  coverTemplateCollectionModel,
  coverTemplateItemModel,
  coverTemplatesMarkup,
  formatSavedClock,
  moveTemplateItem,
  templateDraftChanged,
  templateDraftFromItem,
  templateSaveStatus,
} from "./frontend/cover-templates.js";
import {
  DESCRIPTION_PROMPT_LIMIT,
  countDescriptionPrompt,
  descriptionDownloadFilename,
  descriptionGateText,
  descriptionGenerateMarkup,
  descriptionHistoryMarkup,
  descriptionStateModel,
  descriptionTemplatesMarkup,
  descriptionTemplatesModel,
  formatDescriptionDuration,
} from "./frontend/description-view.js";

const api = new ApiClient();
const app = document.querySelector("#app");
const toastRoot = document.querySelector("#toast");

const STAGES = [
  { key: "project_configuration", label: "项目配置", note: "标题与基础参数" },
  { key: "script", label: "文案", note: "仅支持粘贴" },
  { key: "character_ip", label: "角色 IP", note: "选择 1–8 张" },
  { key: "visual_style", label: "画面风格", note: "项目风格快照" },
  { key: "narration_settings", label: "配音设置", note: "音色与确认" },
  { key: "narration_timeline", label: "配音与时间线", note: "真实时长" },
  { key: "director_storyboard", label: "导演与故事板", note: "前置规划与组级多格图" },
  { key: "group_video_preview", label: "分组视频与预览", note: "每组一个视频" },
  { key: "composition_export", label: "合成与导出", note: "成片与 ZIP" },
  { key: "cover", label: "封面", note: "参考图与频道风格" },
  { key: "description", label: "简介", note: "模板与最终 SRT" },
];

// §15 一键自动生产：AUTO 项目在九阶段前多一个聚合进度页签；MANUAL 项目工作台完全不变。
const AUTO_PROGRESS_STAGE = { key: "auto_progress", label: "自动生产", note: "全链路自动进度" };

const isAutoProduction = (project = state.project) => project?.production_mode === "AUTO";

function stageDefinitions(project = state.project) {
  return isAutoProduction(project) ? [AUTO_PROGRESS_STAGE, ...STAGES] : STAGES;
}

function defaultStageKey(project = state.project) {
  return isAutoProduction(project) ? AUTO_PROGRESS_STAGE.key : "project_configuration";
}

// 语言清单与中文名统一由 ./frontend/locale-labels.js 提供，避免两份清单漂移。
// 频道序号徽章四色循环：侧栏与 hero 共用，同频道同色。
const CHANNEL_AVATAR_COLORS = ["#6b5cf6", "#55a6d8", "#44ad86", "#d78a54"];
const channelAvatarColor = (index) => CHANNEL_AVATAR_COLORS[index % CHANNEL_AVATAR_COLORS.length];
const DIRECTOR_POLL_DELAYS_MS = Object.freeze([2500, 5000, 10000]);
const PROJECTS_POLL_INTERVAL_MS = 5000;
// §24（2026-10-14）：组级最终分镜上限，与后端 DIRECTOR_GROUP_SHOT_LIMIT 及生图网格声明同步。
const MAX_DIRECTOR_SHOTS = 6;

const state = {
  token: sessionStorage.getItem("tf-token") || localStorage.getItem("tf-token") || "",
  me: null,
  channels: [],
  projects: [],
  voices: [],
  clonedVoices: [],
  clonedVoiceChannelId: null,
  voicePanelViewProvider: null,
  voicePanelViewChannelId: null,
  voicePanelSubmitting: false,
  voicePanelOutcome: null,
  voicePanelNewVoice: null,
  images: [],
  activeChannelId: null,
  assetChannelId: null,
  project: null,
  narration: null,
  groups: [],
  summary: null,
  deletion: null,
  activeStage: "project_configuration",
  activeGroupId: null,
  nav: "projects",
  saveTimer: null,
  pendingSave: null,
  savePromise: null,
  objectUrls: new Set(),
  audio: null,
  renderEpoch: 0,
  narrationFailuresOnly: false,
  narrationRetryResults: [],
  narrationRetryProjectId: null,
  narrationRetryBusy: false,
  narrationRefreshPendingProjectId: null,
  narrationPollTimer: null,
  narrationPollGeneration: 0,
  narrationPollInFlight: false,
  autoPollTimer: null,
  autoPollGeneration: 0,
  autoPollInFlight: false,
  autoTickTimer: null,
  autoTickAnchor: 0,
  directorPollTimer: null,
  directorPollDelayMs: DIRECTOR_POLL_DELAYS_MS[0],
  directorPollStableCount: 0,
  directorPollInFlight: false,
  directorPollGeneration: 0,
  directorPollContext: null,
  directorPollImmediatePending: false,
  directorPollLastUpdatedAt: null,
  directorPollErrorCount: 0,
  projectsPollTimer: null,
  projectsPollInFlight: false,
  projectsPollGeneration: 0,
  projectListQuery: "",
  projectListFilter: "all",
  projectListSort: "newest",
  projectListView: "grid",
  projectPublicationPending: new Set(),
  preplanOffset: 0,
  preplanLimit: 20,
  directorView: "list",
  directorGroupFilter: "all",
  directorDraftDirty: false,
  directorDraftContext: null,
  directorStageCache: null,
  coverStyle: null,
  coverChannelStyle: null,
  coverPrompts: null,
  coverImages: null,
  coverImageUploading: false,
  coverStyleSource: null,
  coverTextChoice: null,
  coverBrief: "",
  coverStyleOverride: "",
  coverPromptDraft: null,
  coverMirrorScopeKey: null,
  coverDimsDraft: null,
  coverRefImageDraft: null,
  coverEditedTextDraft: null,
  coverAnalyzingScope: null,
  coverAnalysisDeadline: 0,
  coverPollTimer: null,
  coverPollGeneration: 0,
  coverPollInFlight: false,
  coverDraftSaveTimer: null,
  coverDraftSaveInFlight: false,
  coverPromptSaveTimer: null,
  coverWatchTimer: null,
  coverWatchGeneration: 0,
  coverCharacterCards: null,
  coverCharacterImages: null,
  coverCardSelection: null,
  coverCardDrafts: {},
  coverCardAnalyzing: null,
  coverCardAnalyzingUpdatedAt: null,
  coverAnalyzedDims: null,
  coverStyleLabelDraft: "",
  coverStyleProfileChoice: null,
  // 临时功能：频道级多模板（2026-10-27）。删除功能时连同下方绑定一起移除。
  coverTemplates: null,
  coverTemplateDrafts: {},
  coverTemplateDirty: {},
  coverTemplateSaving: {},
  coverTemplateErrors: {},
  coverTemplateSavedClock: {},
  coverTemplateDragging: null,
  description: null,
  descriptionTemplates: [],
  descriptionSelectedTemplateId: null,
  descriptionTemplateDrafts: {},
  descriptionTemplateDirty: {},
  descriptionTemplateSaveTimers: {},
  descriptionTemplateSaveInFlight: {},
  descriptionTemplateSavePromises: {},
  descriptionTemplateSaveError: "",
  descriptionTemplateSavedClock: "",
  descriptionEditingId: null,
  descriptionEditDraft: "",
  descriptionGenerating: false,
  descriptionError: "",
};

api.setToken(state.token);
api.setUnauthorizedHandler(() => {
  stopNarrationPolling();
  if (!state.token) return;
  stopDirectorPolling();
  stopAutoPipelinePolling();
  stopProjectsPolling();
  stopCoverPolling();
  stopCoverWatch();
  cancelPendingProjectPatch();
  clearObjectUrls();
  clearToken();
  state.me = null;
  state.project = null;
  state.narration = null;
  state.groups = [];
  state.summary = null;
  state.deletion = null;
  renderAuth("login", "登录已失效、账号已停用或密码已重置，请重新登录。");
});

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function messageOf(error) {
  return error instanceof ApiError ? error.message : error?.message || "请求失败，请检查后端。";
}

function projectTitle(project) {
  return project?.title || `项目 ${String(project?.id || "").slice(0, 8)}`;
}

function channelIndex(channelId) {
  return state.channels.findIndex((channel) => channel.id === channelId);
}

// §23.1/§23.3 + UX 方案A（2026-10-14 确认）：两级信息架构「① 选择配音源 → ② 源内工作区」。
// 源卡切换为纯视图操作；「设为频道默认」即生效并跨源自动切换配音源；克隆结果卡常驻、失败可重试。
function clonedVoicePanelMarkup(channel, { newVoiceId = null } = {}) {
  const provider = providerOf(channel);
  const ready = channelDefaultVoiceReady(channel);
  const viewProvider = voicePanelViewProvider(channel);
  const viewActive = viewProvider === provider;
  const isElView = viewProvider === ELEVENLABS;
  const views = clonedVoiceViews(channelClonedVoices(channel), {
    defaultClonedVoiceId: channel.default_cloned_voice_id,
  });
  const ordered = views
    .slice()
    .sort((left, right) => String(right.createdAt || "").localeCompare(String(left.createdAt || "")));
  const defaultClone = channelDefaultClonedVoice(channel);
  const defaultPublic = channelDefaultVoice(channel);

  const sourceCards = [COSYVOICE, ELEVENLABS].map((key) => {
    const isView = key === viewProvider;
    const isActive = key === provider;
    const meta = key === ELEVENLABS ? "支持音频克隆 · 频道共享" : "固定公共音色库 · 无需克隆";
    return `<button type="button" class="voice-source-card${isView ? " is-viewing" : ""}" data-voice-source="${key}" aria-pressed="${isView}">
      <span class="voice-source-copy"><strong>${escapeHtml(ttsProviderLabel(key))}</strong><small>${escapeHtml(meta)}</small></span>
      ${isActive ? '<span class="status-pill purple">当前使用</span>' : ""}
    </button>`;
  }).join("");

  const activeName = provider === ELEVENLABS ? (defaultClone?.name || "") : (defaultPublic?.display_name || "");
  let statusBar;
  if (viewActive) {
    const voiceName = isElView ? (defaultClone?.name || "") : (defaultPublic?.display_name || "");
    if (voiceName) {
      statusBar = `<div class="voice-status-bar is-active">
        <span class="voice-status-label">当前使用</span>
        <strong>${escapeHtml(voiceName)}</strong>
        <span class="voice-badge">${isElView ? "克隆音色" : "公共音色"}</span>
        <span class="voice-badge">${escapeHtml(ttsProviderLabel(provider))}</span>
        <small>频道默认音色：用于新项目，已有项目不受影响。</small>
      </div>`;
    } else {
      statusBar = `<div class="voice-status-bar is-guidance">
        <strong>尚未设置频道默认音色</strong>
        <p>请从下方选择一个已有音色${isElView ? "，或上传音频克隆一个新音色" : ""}；设置后即成为频道当前使用音色。未设置前无法创建新项目。</p>
      </div>`;
    }
  } else {
    statusBar = `<div class="voice-status-bar">
      <strong>此配音源尚未启用</strong>
      <p>频道当前使用 ${escapeHtml(ttsProviderLabel(provider))}${activeName ? ` 的「${escapeHtml(activeName)}」` : ""}；将本源的音色设为频道默认后会自动切换配音源。</p>
    </div>`;
  }

  const rows = ordered.map((voice) => {
    const inUse = voice.isDefault && viewActive;
    const isNew = voice.id === newVoiceId;
    return `<div class="asset-card${isNew ? " is-new" : ""}" data-cloned-voice="${voice.id}">
      <div class="asset-card-body">
        <strong>${escapeHtml(voice.name)}${inUse ? ' <span class="status-pill purple">使用中</span>' : ""}${isNew ? ' <span class="voice-new-badge">新</span>' : ""}</strong>
        <small>${voice.remark ? escapeHtml(voice.remark) : "无备注"} · ${voice.sizeBytes} 字节</small>
        <audio controls preload="none" src="${escapeHtml(api.clonedVoiceSourceUrl(channel.id, voice.id))}"></audio>
        <div class="asset-card-actions">
          <button type="button" class="ghost-button compact" data-cloned-default="${voice.id}" ${inUse ? "disabled" : ""}>${inUse ? "使用中" : "设为频道默认"}</button>
          <button type="button" class="ghost-button compact" data-cloned-delete="${voice.id}" ${voice.canDelete ? "" : `disabled title="${escapeHtml(voice.protectionReason)}"`}>删除</button>
        </div>
        ${voice.protectionReason ? `<small class="is-warning">${escapeHtml(voice.protectionReason)}</small>` : ""}
      </div>
    </div>`;
  }).join("");

  const outcome = state.voicePanelOutcome && state.voicePanelOutcome.channelId === channel.id
    ? state.voicePanelOutcome
    : null;
  const outcomeClass = outcome ? (outcome.kind === "fail" ? " error" : outcome.kind === "partial" ? " amber" : "") : "";

  const workspace = isElView
    ? `${statusBar}
      <div class="voice-workspace-section">
        <div class="voice-section-head"><h3>使用已有音色</h3><span class="voice-section-meta">频道共享 · ${ordered.length} 个克隆音色 · 试听为源音频</span></div>
        <div class="asset-grid">${rows || `<div class="notice-card"><strong>该频道还没有克隆音色</strong><p>上传一段音频即可克隆新音色；克隆完成后可设为频道默认。</p></div>`}</div>
      </div>
      <details class="voice-clone-section" ${(!ordered.length || outcome) ? "open" : ""}>
        <summary>上传音频克隆</summary>
        <form id="clonedVoiceForm" class="form-grid two-cols">
          <label class="field"><span>音色名称 <em>*</em></span><input name="name" maxlength="100" required /></label>
          <label class="field"><span>备注</span><input name="remark" maxlength="2000" /></label>
          <label class="field"><span>源音频 <em>*</em></span><input name="file" type="file" accept="audio/*,video/mp4,video/webm" required /></label>
          <div class="field voice-form-actions"><span>&nbsp;</span><button class="primary-button" type="submit">上传并克隆</button><small class="voice-form-note">提交后逐账号完成克隆，请保持页面打开。</small></div>
        </form>
      </details>
      <div id="clonedVoiceOutcome" class="notice-card voice-outcome${outcomeClass}" ${outcome ? "" : "hidden"}>
        <strong>${escapeHtml(outcome?.title || "")}</strong>
        <p>${escapeHtml(outcome?.detail || "")}</p>
        <div class="voice-outcome-actions">
          <button class="ghost-button compact" type="button" data-voice-outcome-retry ${outcome?.retryable ? "" : "hidden"}>重试</button>
          <button class="ghost-button compact" type="button" data-voice-outcome-dismiss>关闭</button>
        </div>
      </div>`
    : `${statusBar}
      <div class="voice-workspace-section">
        <div class="voice-section-head"><h3>使用已有音色</h3><span class="voice-section-meta">固定公共音色库 · ${state.voices.length} 个版本</span></div>
        <div id="voiceWorkspaceLibrary"></div>
      </div>`;

  return `<div class="panel">
    <div class="panel-head"><div><h2>配音源与克隆音色</h2><p>先选择配音源，再在源内选择已有音色或克隆新音色；ElevenLabs 音色由上传音频克隆得到，频道内共享。</p></div><span class="status-pill ${ready ? "purple" : "amber"}">${escapeHtml(ttsProviderLabel(provider))}</span></div>
    <div class="panel-body">
      <div class="voice-workspace">
        <div class="voice-workspace-section">
          <div class="voice-section-head"><h3>① 选择配音源</h3><span class="voice-section-meta">切换查看不影响当前配置</span></div>
          <div class="voice-source-grid">${sourceCards}</div>
        </div>
        <div class="voice-workspace-section">
          <div class="voice-section-head"><h3>② ${escapeHtml(ttsProviderLabel(viewProvider))} 音色</h3>${viewActive ? '<span class="voice-section-meta">正在使用</span>' : '<span class="voice-section-meta">未启用</span>'}</div>
          ${workspace}
        </div>
      </div>
    </div>
  </div>`;
}

// UX 方案A：音色面板局部渲染（切换查看源时使用，不触发整页刷新）。
function renderVoiceSourcePanel(root, channel) {
  const mount = $("#voiceSourcePanelMount", root);
  if (!mount || !channel) return;
  mount.innerHTML = clonedVoicePanelMarkup(channel, { newVoiceId: takeVoicePanelNewVoice(channel) });
  bindVoiceSourcePanel(mount, channel, root);
}

function bindVoiceSourcePanel(mount, channel, root) {
  $$("[data-voice-source]", mount).forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.voiceSource;
      if (target === voicePanelViewProvider(channel)) return;
      state.voicePanelViewChannelId = channel.id;
      state.voicePanelViewProvider = target;
      renderVoiceSourcePanel(root, channel);
    });
  });

  if (voicePanelViewProvider(channel) === COSYVOICE) {
    mountVoiceLibrary($("#voiceWorkspaceLibrary", mount), {
      voices: state.voices,
      selectionName: "channel_default_voice_version_id",
      selectedId: channel.default_voice_version_id,
      defaultVoiceId: channel.default_voice_version_id,
      selectable: true,
      onSelectionChange: async (voice) => {
        const assignment = channelDefaultPatch(COSYVOICE, { voiceVersionId: voice.id });
        if (!assignment.ok) throw new Error(assignment.reason);
        const updated = await api.patchChannel(channel.id, assignment.patch);
        state.channels = state.channels.map((item) => (item.id === updated.id ? updated : item));
        state.voicePanelOutcome = null;
        renderChannelRail();
        toast(`已将频道配音源切换为 ${ttsProviderLabel(COSYVOICE)}，当前使用音色：${voice.display_name}。已有项目不受影响。`);
        // 刷新面板（源卡「当前使用」、状态条、② 标题）；音色库会随重挂载自动定位到所选音色。
        renderVoiceSourcePanel(root, updated);
      },
    });
  }

  $$("[data-cloned-default]", mount).forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const voiceId = button.dataset.clonedDefault;
      const voice = channelClonedVoices(channel).find((item) => item.id === voiceId) || null;
      const assignment = channelDefaultPatch(ELEVENLABS, { clonedVoiceId: voiceId });
      if (!assignment.ok) {
        toast(assignment.reason, true);
        return;
      }
      const switching = providerOf(channel) !== ELEVENLABS;
      try {
        setBusy(true);
        const updated = await api.patchChannel(channel.id, assignment.patch);
        state.channels = state.channels.map((item) => (item.id === updated.id ? updated : item));
        state.voicePanelOutcome = null;
        renderChannelRail();
        toast(switching
          ? `已将频道配音源切换为 ${ttsProviderLabel(ELEVENLABS)}，当前使用音色：${voice?.name || "该克隆音色"}。已有项目不受影响。`
          : `已设为频道默认音色：${voice?.name || "该克隆音色"}。已有项目不受影响。`);
      } catch (error) {
        toast(messageOf(error), true);
      } finally {
        setBusy(false);
        await renderAssets();
      }
    });
  });

  $$("[data-cloned-delete]", mount).forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      const voiceId = button.dataset.clonedDelete;
      const voice = channelClonedVoices(channel).find((item) => item.id === voiceId) || null;
      const { confirmed } = await actionModal({
        title: "删除克隆音色？",
        eyebrow: "DELETE CLONED VOICE",
        bodyHtml: `<p>将删除音色「${escapeHtml(voice?.name || "未命名音色")}」：远端音色与源音频将一并清理，且不可撤销。</p>`,
        confirmText: "删除音色",
        danger: true,
      });
      if (!confirmed) return;
      try {
        setBusy(true);
        await api.deleteClonedVoice(channel.id, voiceId);
        state.voicePanelOutcome = null;
        toast("克隆音色已删除（远端音色与源音频一并清理）。");
      } catch (error) {
        toast(messageOf(error), true);
      } finally {
        setBusy(false);
        await renderAssets();
      }
    });
  });

  const form = $("#clonedVoiceForm", mount);
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.voicePanelSubmitting) return;
    const data = new FormData(form);
    const file = form.elements.file?.files?.[0] || null;
    const check = validateClonedVoiceUpload({ name: data.get("name"), file });
    if (!check.ok) {
      toast(check.reason, true);
      return;
    }
    const name = String(data.get("name") || "").trim();
    const remark = String(data.get("remark") || "").trim();
    const submitButton = $('button[type="submit"]', form);
    state.voicePanelSubmitting = true;
    form.classList.add("is-submitting");
    $$("input, button", form).forEach((control) => { control.disabled = true; });
    if (submitButton) submitButton.textContent = "处理中…";
    try {
      const result = await api.uploadClonedVoice(channel.id, file, name, remark);
      const summary = cloneOutcomeSummary(result?.accounts);
      const kind = summary.failedCount === 0 ? "ok" : summary.okCount ? "partial" : "fail";
      state.voicePanelOutcome = {
        channelId: channel.id,
        kind,
        title: kind === "ok" ? "克隆完成" : kind === "partial" ? "克隆部分失败" : "克隆失败",
        detail: kind === "ok" ? `${summary.detail} 新音色已加入列表；当前使用音色未变。` : summary.detail,
        retryable: summary.failedCount > 0,
      };
      if (result?.voice?.id) {
        state.voicePanelNewVoice = { channelId: channel.id, id: String(result.voice.id) };
      }
      toast(summary.detail, summary.failedCount > 0);
      state.voicePanelSubmitting = false;
      await renderAssets();
    } catch (error) {
      state.voicePanelSubmitting = false;
      form.classList.remove("is-submitting");
      $$("input, button", form).forEach((control) => { control.disabled = false; });
      if (submitButton) submitButton.textContent = "上传并克隆";
      state.voicePanelOutcome = {
        channelId: channel.id,
        kind: "fail",
        title: "克隆失败",
        detail: `${messageOf(error)} 表单内容已保留，可直接重试。`,
        retryable: true,
      };
      showVoiceOutcome(mount);
      toast(messageOf(error), true);
    }
  });

  $("[data-voice-outcome-retry]", mount)?.addEventListener("click", () => {
    $("#clonedVoiceForm", mount)?.requestSubmit();
  });
  $("[data-voice-outcome-dismiss]", mount)?.addEventListener("click", () => {
    state.voicePanelOutcome = null;
    const card = $("#clonedVoiceOutcome", mount);
    if (card) card.hidden = true;
  });
}

function showVoiceOutcome(mount) {
  const outcome = state.voicePanelOutcome;
  const card = $("#clonedVoiceOutcome", mount);
  if (!outcome || !card) return;
  card.hidden = false;
  card.classList.toggle("error", outcome.kind === "fail");
  card.classList.toggle("amber", outcome.kind === "partial");
  const title = $("strong", card);
  const detail = $("p", card);
  if (title) title.textContent = outcome.title;
  if (detail) detail.textContent = outcome.detail;
  const retry = $("[data-voice-outcome-retry]", card);
  if (retry) retry.hidden = !outcome.retryable;
}

function channelLabel(channelOrId) {
  const id = typeof channelOrId === "string" ? channelOrId : channelOrId?.id;
  const index = channelIndex(id);
  return channelDisplayName(index < 0 ? null : state.channels[index], index);
}

function channelById(id) {
  return state.channels.find((channel) => channel.id === id) || null;
}

function channelDefaultVoice(channel) {
  return state.voices.find((voice) => voice.id === channel?.default_voice_version_id) || null;
}

function channelClonedVoices(channel) {
  if (!channel || state.clonedVoiceChannelId !== channel.id) return [];
  return state.clonedVoices;
}

function channelDefaultClonedVoice(channel) {
  return (
    channelClonedVoices(channel).find((voice) => voice.id === channel?.default_cloned_voice_id) ||
    null
  );
}

// UX 方案A（2026-10-14 确认）：音色工作区当前查看的配音源；切换查看为纯视图操作、不写库。
function voicePanelViewProvider(channel) {
  if (!channel) return COSYVOICE;
  if (state.voicePanelViewChannelId !== channel.id) return providerOf(channel);
  return state.voicePanelViewProvider === ELEVENLABS ? ELEVENLABS : COSYVOICE;
}

// 克隆成功后的「新音色」高亮：随下一次面板渲染消费一次。
function takeVoicePanelNewVoice(channel) {
  const mark = state.voicePanelNewVoice;
  state.voicePanelNewVoice = null;
  return mark && channel && mark.channelId === channel.id ? mark.id : null;
}

function canCreateProjectForChannel(channel) {
  if (!channel || !state.images.length) return false;
  if (!channelDefaultVoiceReady(channel)) return false;
  return providerOf(channel) === ELEVENLABS ? true : state.voices.length > 0;
}

const VOICE_GENDER_LABELS = { female: "女声", male: "男声" };
const VOICE_AGE_LABELS = { child: "儿童", young: "青年", middle: "中年", old: "老年" };

function voiceSelectOptions(values, selectedValue, labelFor = (value) => value) {
  return values.map((value) => `<option value="${escapeHtml(value)}" ${value === selectedValue ? "selected" : ""}>${escapeHtml(labelFor(value))}</option>`).join("");
}

function mountVoiceLibrary(root, options = {}) {
  if (!root) return null;
  const voices = Array.isArray(options.voices) ? options.voices : [];
  const selectionName = options.selectionName || "voice_version_id";
  const selectable = options.selectable !== false;
  let requiredLocale = options.requiredLocale || "";
  let selectedId = options.selectedId || null;
  let defaultVoiceId = options.defaultVoiceId || null;
  let page = 1;
  let selectionPending = false;

  if (requiredLocale) {
    const selected = voices.find((voice) => voice.id === selectedId);
    if (selected && !voiceSupportsLocale(selected, requiredLocale)) selectedId = null;
  }

  const genders = voiceFacetValues(voices, "gender");
  const ageBands = voiceFacetValues(voices, "age_band");
  const accents = voiceFacetValues(voices, "accent");
  const tags = voiceFacetValues(voices, "tags");
  const accentLabels = new Map(voices.map((voice) => [voice.accent, voice.accent_zh || voice.accent]));

  root.classList.add("voice-library");
  root.innerHTML = `<div class="voice-library-toolbar">
    <label class="field voice-search"><span>搜索</span><input data-voice-filter="query" placeholder="名称、voice_id、UUID、描述或标签" /></label>
    <label class="field"><span>语言</span><select data-voice-filter="locale" ${requiredLocale ? "disabled" : ""}><option value="">全部语言</option>${voiceSelectOptions(LOCALES, requiredLocale, localeLabel)}</select></label>
    <label class="field"><span>性别</span><select data-voice-filter="gender"><option value="">全部</option>${voiceSelectOptions(genders, "", (value) => VOICE_GENDER_LABELS[value] || value)}</select></label>
    <label class="field"><span>年龄</span><select data-voice-filter="ageBand"><option value="">全部</option>${voiceSelectOptions(ageBands, "", (value) => VOICE_AGE_LABELS[value] || value)}</select></label>
    <label class="field"><span>口音</span><select data-voice-filter="accent"><option value="">全部</option>${voiceSelectOptions(accents, "", (value) => accentLabels.get(value) || value)}</select></label>
    <label class="field"><span>标签</span><select data-voice-filter="tag"><option value="">全部</option>${voiceSelectOptions(tags, "")}</select></label>
  </div>
  <div class="voice-library-status"><span data-voice-result-count></span><strong data-voice-selection></strong></div>
  <div class="voice-library-grid" data-voice-list></div>
  <div class="voice-library-pagination"><button class="ghost-button compact" data-voice-page="previous" type="button">上一页</button><span data-voice-page-status></span><button class="ghost-button compact" data-voice-page="next" type="button">下一页</button></div>`;

  const localeControl = $('[data-voice-filter="locale"]', root);
  const resultCount = $("[data-voice-result-count]", root);
  const selection = $("[data-voice-selection]", root);
  const list = $("[data-voice-list]", root);
  const pageStatus = $("[data-voice-page-status]", root);
  const previous = $('[data-voice-page="previous"]', root);
  const next = $('[data-voice-page="next"]', root);

  const filters = () => ({
    query: $('[data-voice-filter="query"]', root).value,
    locale: requiredLocale || localeControl.value,
    gender: $('[data-voice-filter="gender"]', root).value,
    ageBand: $('[data-voice-filter="ageBand"]', root).value,
    accent: $('[data-voice-filter="accent"]', root).value,
    tag: $('[data-voice-filter="tag"]', root).value,
  });

  const voiceCard = (voice) => {
    const isSelected = voice.id === selectedId;
    const metadata = [
      VOICE_GENDER_LABELS[voice.gender] || voice.gender,
      VOICE_AGE_LABELS[voice.age_band] || voice.age_band,
      voice.accent_zh || voice.accent,
    ].filter(Boolean);
    const localeText = localeShortListText(voice.supported_locales);
    const tagMarkup = (voice.tags || []).slice(0, 4).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
    return `<article class="voice-option-card ${isSelected ? "is-selected" : ""}">
      <label class="voice-option-main">
        <input type="radio" name="${escapeHtml(selectionName)}" value="${escapeHtml(voice.id)}" ${isSelected ? "checked" : ""} ${!selectable || selectionPending ? "disabled" : ""} />
        <span class="voice-avatar"><img loading="lazy" decoding="async" data-protected-image="${escapeHtml(voice.avatar_url)}" alt="${escapeHtml(voice.display_name)} 头像" /></span>
        <span class="voice-option-copy">
          <strong>${escapeHtml(voice.display_name)}</strong>
          <small>${escapeHtml(voiceIdentity(voice))}</small>
          <span class="voice-meta">${escapeHtml(metadata.join(" · ") || "元数据未标注")}</span>
          <span class="voice-locales" title="${escapeHtml(localeText)}">${escapeHtml(localeText)}</span>
          ${voice.description ? `<p>${escapeHtml(voice.description)}</p>` : ""}
          ${tagMarkup ? `<span class="voice-tags">${tagMarkup}</span>` : ""}
        </span>
      </label>
      <div class="voice-option-actions">
        ${voice.id === defaultVoiceId ? '<span class="status-pill purple">频道默认</span>' : ""}
        <button class="ghost-button compact" data-voice-preview="${escapeHtml(voice.preview_url)}" type="button">▶ 试听</button>
      </div>
    </article>`;
  };

  const renderPage = ({ locateSelection = false } = {}) => {
    const matching = filterVoices(voices, filters());
    if (locateSelection && selectedId) {
      const selectedIndex = matching.findIndex((voice) => voice.id === selectedId);
      if (selectedIndex >= 0) page = Math.floor(selectedIndex / VOICE_PAGE_SIZE) + 1;
    }
    const result = paginateVoices(matching, page);
    page = result.page;
    resultCount.textContent = `筛选结果 ${result.total} / ${voices.length} 个固定音色`;
    const selectedVoice = voices.find((voice) => voice.id === selectedId);
    selection.textContent = selectedVoice ? `已选：${selectedVoice.display_name} · ${voiceIdentity(selectedVoice)}` : "尚未选择音色";
    list.innerHTML = result.items.map(voiceCard).join("") || `<div class="voice-library-empty"><strong>没有符合条件的音色</strong><p>${voices.length ? "请调整搜索或筛选条件。" : "系统固定公共音色尚未就绪，请检查系统初始化状态。"}</p></div>`;
    pageStatus.textContent = `第 ${result.page} / ${result.pages} 页`;
    previous.disabled = result.page <= 1;
    next.disabled = result.page >= result.pages;
    hydrateProtectedImages(list);
    $$('[data-voice-preview]', list).forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        void playProtectedAudioPath(button.dataset.voicePreview);
      });
    });
    $$(`input[name="${selectionName}"]`, list).forEach((radio) => {
      radio.addEventListener("change", async () => {
        if (!radio.checked || selectionPending) return;
        const previousId = selectedId;
        const voice = voices.find((candidate) => candidate.id === radio.value);
        if (!voice) return;
        selectedId = voice.id;
        selectionPending = true;
        root.classList.add("is-saving");
        renderPage();
        try {
          await options.onSelectionChange?.(voice);
        } catch (error) {
          selectedId = previousId;
          toast(messageOf(error), true);
        } finally {
          selectionPending = false;
          root.classList.remove("is-saving");
          renderPage({ locateSelection: true });
        }
      });
    });
  };

  $$('[data-voice-filter]', root).forEach((control) => {
    control.addEventListener(control.tagName === "INPUT" ? "input" : "change", () => {
      page = 1;
      renderPage();
    });
  });
  previous.addEventListener("click", () => {
    page -= 1;
    renderPage();
  });
  next.addEventListener("click", () => {
    page += 1;
    renderPage();
  });

  const controller = {
    getSelectedId: () => selectedId,
    setRequiredLocale(locale, { fallbackToDefault = true } = {}) {
      requiredLocale = locale || "";
      localeControl.value = requiredLocale;
      localeControl.disabled = Boolean(requiredLocale);
      const selectedVoice = voices.find((voice) => voice.id === selectedId);
      if (requiredLocale && (!selectedVoice || !voiceSupportsLocale(selectedVoice, requiredLocale))) {
        const defaultVoice = voices.find((voice) => voice.id === defaultVoiceId);
        selectedId = fallbackToDefault && defaultVoice && voiceSupportsLocale(defaultVoice, requiredLocale) ? defaultVoice.id : null;
        options.onSelectionChange?.(selectedId ? defaultVoice : null);
      }
      page = 1;
      renderPage({ locateSelection: true });
    },
    setDefaultVoiceId(voiceId) {
      defaultVoiceId = voiceId || null;
    },
  };
  renderPage({ locateSelection: true });
  return controller;
}

function actionsOf(value) {
  return Array.isArray(value?.allowed_actions) ? value.allowed_actions : [];
}

function allows(value, action) {
  return actionsOf(value).includes(action);
}

function stageFact(key) {
  return state.summary?.stages?.find((stage) => stage.key === key) || null;
}

// 聚合进度页签在 stages[] 里没有对应事实；用 auto_pipeline.state 合成展示状态。
function autoProgressStageFact() {
  const statusByState = { RUNNING: "RUNNING", PAUSED: "RECOVERY_REQUIRED", COMPLETED: "SUCCEEDED" };
  return { status: statusByState[autoPipelineState(state.summary)] || "NOT_STARTED" };
}

function activeStageStatusFact() {
  return state.activeStage === AUTO_PROGRESS_STAGE.key ? autoProgressStageFact() : stageFact(state.activeStage);
}

// AUTO 项目 RUNNING/PAUSED 期间，本地硬编码的生成类入口一律锁定并给出原因（§15.1 不再需要独立手动动作）。
function autoBusy() {
  return isAutoProduction() && autoPipelineBusy(state.summary);
}

function autoBusyReason() {
  return autoPipelineState(state.summary) === "PAUSED"
    ? "自动生产已暂停：请先在「自动生产」页查看原因并恢复"
    : "自动生产进行中：阶段级手动生成入口已关闭";
}

function stageAllows(key, action) {
  return allows(stageFact(key), action) || allows(state.summary, action);
}

function publicStatus(value) {
  return value?.public_status || value?.status || value?.state || "NOT_STARTED";
}

function statusText(value) {
  const status = publicStatus(value);
  return {
    NOT_STARTED: "未开始",
    QUEUED: "已排队",
    RUNNING: "处理中",
    RECOVERY_REQUIRED: "需要恢复",
    SUCCEEDED: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
    DELETING: "正在删除",
    DELETE_FAILED: "删除失败",
    DELETED: "删除完成",
    complete: "已完成",
    current: "当前阶段",
    blocked: "未开放",
  }[status] || String(status);
}

function statusClass(value) {
  const status = publicStatus(value);
  if (["SUCCEEDED", "complete"].includes(status)) return "is-complete";
  if (["QUEUED", "RUNNING", "current"].includes(status)) return "is-pending";
  if (["FAILED"].includes(status)) return "is-error";
  return "is-blocked";
}

function latestUnfinishedOrFailedAttempt(data) {
  const items = Array.isArray(data) ? data : Array.isArray(data?.items) ? data.items : [];
  const latest = items[0] || null;
  return latest && ["QUEUED", "RUNNING", "RECOVERY_REQUIRED", "FAILED", "CANCELLED"].includes(publicStatus(latest))
    ? latest
    : null;
}

function permitsNewAttempt(latestAttempt) {
  if (!latestAttempt) return true;
  const status = publicStatus(latestAttempt);
  if (["QUEUED", "RUNNING", "RECOVERY_REQUIRED"].includes(status)) return false;
  if (["FAILED", "CANCELLED"].includes(status)) return allows(latestAttempt, "RETRY");
  return true;
}

function allowsAny(values, actions) {
  return values.some((value) => actions.some((action) => allows(value, action)));
}

function canRegenerateDirectorPreplan(fact, run, current) {
  if (["QUEUED", "RUNNING"].includes(publicStatus(run))) return false;
  if (run) {
    return allowsAny([run], ["REGENERATE_DIRECTOR_PREPLAN", "RETRY"]);
  }
  return allowsAny([fact, current], ["REGENERATE_DIRECTOR_PREPLAN"]);
}

function clearDirectorPollTimer() {
  if (state.directorPollTimer != null) {
    window.clearTimeout(state.directorPollTimer);
    state.directorPollTimer = null;
  }
}

function stopDirectorPolling() {
  clearDirectorPollTimer();
  state.directorPollGeneration += 1;
  state.directorPollContext = null;
  state.directorPollImmediatePending = false;
  state.directorPollStableCount = 0;
  state.directorPollDelayMs = DIRECTOR_POLL_DELAYS_MS[0];
  state.directorPollErrorCount = 0;
}

function stopProjectsPolling() {
  if (state.projectsPollTimer != null) {
    window.clearInterval(state.projectsPollTimer);
    state.projectsPollTimer = null;
  }
  state.projectsPollGeneration += 1;
}

function startProjectsPolling() {
  stopProjectsPolling();
  state.projectsPollTimer = window.setInterval(() => {
    void pollProjectsOnce();
  }, PROJECTS_POLL_INTERVAL_MS);
}

// 项目列表轮询：隐藏页签跳过、单飞；数据无变化不重渲；搜索框聚焦时
// 只更新数据避免打断输入；失败静默保留旧数据，下个周期再试。
async function pollProjectsOnce() {
  if (document.hidden || state.projectsPollInFlight || state.projectsPollTimer == null) return;
  if (state.projectPublicationPending?.size) return;
  const generation = state.projectsPollGeneration;
  state.projectsPollInFlight = true;
  try {
    const next = await api.projects();
    if (generation !== state.projectsPollGeneration) return;
    if (JSON.stringify(next) === JSON.stringify(state.projects)) return;
    state.projects = next;
    if (document.activeElement === $("#projectSearch")) return;
    renderProjects();
  } catch {
    // 轮询失败不提示，保留当前列表。
  } finally {
    state.projectsPollInFlight = false;
  }
}

function directorValueIsActive(value) {
  return ["QUEUED", "RUNNING"].includes(publicStatus(value));
}

function directorPollingIsActive(run, narration = state.narration, materialization = null) {
  return [run, narration, materialization].some(directorValueIsActive);
}

function directorFactFromSummary(summary) {
  return summary?.stages?.find((stage) => stage.key === "director_storyboard") || null;
}

function directorPollSnapshot(run, narration, materialization, summary, overview = null) {
  const fact = directorFactFromSummary(summary);
  return {
    run: run ? {
      run_id: run.run_id || null,
      public_status: publicStatus(run),
      message: run.message || "",
      allowed_actions: [...actionsOf(run)],
      completed_units: Number(run.completed_units || 0),
      total_units: Number(run.total_units || 0),
      succeeded_units: Number(run.succeeded_units || 0),
      inflight_units: Number(run.inflight_units || 0),
      retry_pending_units: Number(run.retry_pending_units || 0),
      auto_retries_used: Number(run.auto_retries_used || 0),
      failed_unit_ordinal: run.failed_unit_ordinal ?? null,
      failed_unit_kind: run.failed_unit_kind || null,
      unsent_units: Number(run.unsent_units || 0),
      failure_code: run.failure_code || null,
    } : null,
    narration: narration ? {
      public_status: publicStatus(narration),
      message: narration.message || "",
      groups: (Array.isArray(narration.groups) ? narration.groups : []).map((group) => ({
        group_id: group.group_id || null,
        start_ms: group.start_ms ?? null,
        end_ms: group.end_ms ?? null,
        target_duration_ms: group.target_duration_ms ?? null,
      })),
    } : null,
    materialization: materialization ? {
      materialization_id: materialization.materialization_id || materialization.run_id || null,
      preplan_revision_id: materialization.preplan_revision_id || null,
      public_status: publicStatus(materialization),
      message: materialization.message || "",
      allowed_actions: [...actionsOf(materialization)],
      candidates: (Array.isArray(materialization.candidates) ? materialization.candidates : []).map((candidate) => ({
        group_id: candidate.group_id || null,
        candidate_id: candidate.candidate_id || null,
        final_revision_id: candidate.final_revision_id || null,
        review_state: candidate.review_state || null,
        conflict_count: Array.isArray(candidate.conflicts) ? candidate.conflicts.length : 0,
      })),
    } : null,
    fact: fact ? {
      status: fact.status || null,
      blocking_reason: fact.blocking_reason || "",
      allowed_actions: [...actionsOf(fact)],
      counts: fact.counts || null,
    } : null,
    // 列表视图把批量 overview 的每组故事板状态/计数纳入签名；详情视图恒为 null，轮询行为不变。
    overview: overview ? {
      items: (Array.isArray(overview.items) ? overview.items : []).map((item) => ({
        group_id: item.group_id || null,
        storyboard_status: item.storyboard_status || null,
        storyboard_version: item.storyboard_version ?? null,
        current_generation_id: item.current_generation_id || null,
        draft_review_state: item.draft_review_state || null,
        can_generate: Boolean(item.can_generate),
      })),
    } : null,
  };
}

function directorPollSnapshotSignature(snapshot) {
  return JSON.stringify(snapshot);
}

function directorPollStructureSignature(snapshot) {
  return JSON.stringify({
    run_id: snapshot.run?.run_id || null,
    group_ids: (snapshot.narration?.groups || []).map((group) => group.group_id),
    materialization_id: snapshot.materialization?.materialization_id || null,
    preplan_revision_id: snapshot.materialization?.preplan_revision_id || null,
  });
}

function directorPollReachedTerminal(previous, next) {
  return [
    [previous.run, next.run],
    [previous.narration, next.narration],
    [previous.materialization, next.materialization],
  ].some(([before, after]) => directorValueIsActive(before) && !directorValueIsActive(after));
}

function directorPollTime(timestamp) {
  if (!timestamp) return "";
  return new Date(timestamp).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function directorAutoRefreshContent(run, materialization = null, override = null) {
  if (override) return override;
  const suffix = state.directorPollLastUpdatedAt
    ? ` · 最近更新 ${directorPollTime(state.directorPollLastUpdatedAt)}`
    : "";
  return directorPollingIsActive(run, state.narration, materialization)
    ? `自动更新中${suffix}`
    : `自动更新已停止${suffix}`;
}

function directorAutoRefreshMarkup(run, materialization = null) {
  const active = directorPollingIsActive(run, state.narration, materialization);
  return `<span id="directorAutoRefreshStatus" class="director-auto-refresh" data-state="${active ? "active" : "idle"}" role="status" aria-live="polite">${escapeHtml(directorAutoRefreshContent(run, materialization))}</span>`;
}

function updateDirectorAutoRefreshStatus(run, materialization = null, override = null, tone = null) {
  const target = $("#directorAutoRefreshStatus");
  if (!target) return;
  const nextText = directorAutoRefreshContent(run, materialization, override);
  const nextState = tone || (directorPollingIsActive(run, state.narration, materialization) ? "active" : "idle");
  if (target.textContent !== nextText) target.textContent = nextText;
  if (target.dataset.state !== nextState) target.dataset.state = nextState;
}

const VIDEO_STAGE_TEXT = {
  CREATED: "已创建，等待暂存",
  STAGING: "正在向节点暂存参考图",
  NOT_SUBMITTED: "暂存未完成，等待恢复",
  SUBMITTING: "正在提交节点",
  REMOTE_STATE_UNKNOWN: "远端状态确认中（对账恢复）",
  SUBMITTED: "节点已受理，排队执行",
  RUNNING: "节点生成中",
  SUCCEEDED: "节点已完成，下载归档中",
  ARCHIVED: "已归档完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

function attemptStatusNotice(label, attempt) {
  if (!attempt) return "";
  const status = publicStatus(attempt);
  const stalled = attempt.remote_wait_status === "remote_stalled";
  const tone = status === "FAILED" ? " error" : status === "RECOVERY_REQUIRED" || stalled ? " amber" : "";
  const actions = actionsOf(attempt);
  const stageLine = attempt.stage && VIDEO_STAGE_TEXT[attempt.stage]
    ? `<p>当前阶段：${escapeHtml(VIDEO_STAGE_TEXT[attempt.stage])}<span id="videoAttemptElapsed"></span></p>`
    : "";
  const waitLine = stalled
    ? `<p>远端长时间无状态变化，疑似卡死${attempt.remote_wait_seconds != null ? `（已等待 ${formatMilliseconds(attempt.remote_wait_seconds * 1000)}）` : ""}。可先对账；确认卡死后可取消该远端任务，再手动重新生成。</p>`
    : attempt.remote_wait_status === "waiting_remote" && attempt.remote_wait_seconds != null
      ? `<p>远端处理中 · 已等待 ${formatMilliseconds(attempt.remote_wait_seconds * 1000)}</p>`
      : "";
  return `<div class="notice-card${tone}"><strong>${escapeHtml(label)} · ${escapeHtml(statusText(attempt))}${stalled ? "（疑似卡死）" : ""}</strong><p>${escapeHtml(attempt.message || "后端已返回任务公开状态。")}</p>${stageLine}${waitLine}<small>可执行操作：${escapeHtml(actionListText(actions))}</small></div>`;
}

function updateVideoAttemptElapsed(attempt) {
  const target = $("#videoAttemptElapsed");
  if (!target || !attempt) return;
  const now = Date.now();
  const base = typeof attempt.remote_wait_seconds === "number" ? attempt.remote_wait_seconds : null;
  if (state.videoAttemptSeenId !== attempt.attempt_id || state.videoAttemptSeenBase !== base) {
    state.videoAttemptSeenId = attempt.attempt_id;
    state.videoAttemptSeenAt = now;
    state.videoAttemptSeenBase = base;
  }
  const elapsed = Math.max(0, Math.floor((now - (state.videoAttemptSeenAt || now)) / 1000));
  const seconds = (state.videoAttemptSeenBase ?? 0) + elapsed;
  target.textContent = ` · 已等待 ${formatMilliseconds(seconds * 1000)}`;
}

function stopGroupVideoPolling() {
  if (state.groupVideoPollTimer) {
    clearTimeout(state.groupVideoPollTimer);
    state.groupVideoPollTimer = null;
  }
}

function scheduleGroupVideoPolling(attempt) {
  stopGroupVideoPolling();
  if (!attempt || !["QUEUED", "RUNNING", "RECOVERY_REQUIRED"].includes(publicStatus(attempt))) return;
  if (!allows(attempt, "REFRESH")) return;
  const attemptId = attempt.attempt_id;
  const delays = [2500, 5000, 10000];
  let step = 0;
  const tick = async () => {
    if (state.activeStage !== "group_video_preview") {
      stopGroupVideoPolling();
      return;
    }
    try {
      const latest = await api.videoAttempt(attemptId);
      if (state.activeStage !== "group_video_preview") {
        stopGroupVideoPolling();
        return;
      }
      if (publicStatus(latest) !== publicStatus(attempt) || latest?.stage !== attempt.stage) {
        stopGroupVideoPolling();
        await refreshProjectFacts();
        renderProjectWorkspace();
        return;
      }
      updateVideoAttemptElapsed(attempt);
      step = Math.min(step + 1, delays.length - 1);
      state.groupVideoPollTimer = setTimeout(tick, delays[step]);
    } catch {
      stopGroupVideoPolling();
    }
  };
  state.groupVideoPollTimer = setTimeout(tick, delays[step]);
}

function formatMilliseconds(milliseconds) {
  return duration(Math.max(0, Number(milliseconds || 0)) / 1000);
}

function compactCount(value) {
  return Number.isFinite(Number(value)) ? String(Number(value)) : "—";
}

function setBusy(active) {
  document.body.classList.toggle("is-busy", active);
  app.toggleAttribute("inert", active);
  if (active) app.setAttribute("aria-busy", "true");
  else app.removeAttribute("aria-busy");
}

function toast(text, isError = false) {
  if (!toastRoot) return;
  const item = document.createElement("div");
  item.className = `toast${isError ? " error" : ""}`;
  item.innerHTML = `<b aria-hidden="true">${isError ? "!" : "✓"}</b><div><strong>${isError ? "操作失败" : "操作完成"}</strong><p>${escapeHtml(text)}</p></div><button class="toast-close" type="button" aria-label="关闭提示">×</button>`;
  toastRoot.append(item);
  let timer = window.setTimeout(() => item.remove(), 4200);
  item.addEventListener("mouseenter", () => {
    if (timer) window.clearTimeout(timer);
    timer = null;
  });
  item.addEventListener("mouseleave", () => {
    if (!timer) timer = window.setTimeout(() => item.remove(), 1800);
  });
  $(".toast-close", item).addEventListener("click", () => {
    if (timer) window.clearTimeout(timer);
    item.remove();
  });
}

function legacyCopyText(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.readOnly = true;
  area.setAttribute("aria-hidden", "true");
  area.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0;";
  document.body.appendChild(area);
  const active = document.activeElement;
  let copied = false;
  try {
    area.focus({ preventScroll: true });
    area.select();
    area.setSelectionRange(0, area.value.length);
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  } finally {
    area.remove();
    if (active instanceof HTMLElement && active.isConnected) active.focus({ preventScroll: true });
  }
  return copied;
}

// 局域网 HTTP 不是安全上下文，navigator.clipboard 不可用；此时在点击的同步栈内
// 用已废弃但仍可用的 execCommand 兜底，失败只提示，绝不伪造成功。
function copyTextToClipboard(text, successMessage, emptyMessage = "没有可复制的内容") {
  if (!hasCopyableText(text)) {
    toast(emptyMessage, true);
    return;
  }
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      navigator.clipboard.writeText(text).then(
        () => toast(successMessage),
        () => {
          if (legacyCopyText(text)) toast(successMessage);
          else toast("复制失败，请手动选择文本复制", true);
        },
      );
      return;
    } catch {
      // 同步异常时继续走 execCommand 兜底。
    }
  }
  if (legacyCopyText(text)) toast(successMessage);
  else toast("复制失败，请手动选择文本复制", true);
}

function clearToken() {
  localStorage.removeItem("tf-token");
  sessionStorage.removeItem("tf-token");
  state.token = "";
  api.setToken("");
}

function clearObjectUrls() {
  for (const url of state.objectUrls) URL.revokeObjectURL(url);
  state.objectUrls.clear();
  if (state.audio) {
    state.audio.pause();
    state.audio = null;
  }
}

function keepObjectUrl(blob) {
  const url = URL.createObjectURL(blob);
  state.objectUrls.add(url);
  return url;
}

function hydrateProtectedImages(root = document) {
  const epoch = state.renderEpoch;
  $$("[data-protected-image]", root).forEach((image) => {
    if (epoch !== state.renderEpoch || !image.isConnected) return;
    setNativeMediaSource(image, api.mediaUrl(image.dataset.protectedImage), "图片");
  });
}

function setNativeMediaSource(element, url, label) {
  if (!element || !url) return;
  element.addEventListener("error", () => {
    if (element instanceof HTMLImageElement) {
      element.alt = `${label}不可用，请刷新页面或重新登录`;
      element.closest(".asset-card, .voice-option-card, .cover-ref-item, .cover-char-row, .hero-persona-image, .project-cover")?.classList.add("is-unavailable");
    } else {
      toast(`${label}不可用，请刷新页面或重新登录。`, true);
    }
  }, { once: true });
  element.src = url;
}

async function playProtectedAudio(button) {
  const path = button.dataset.audioPath;
  if (!path) return;
  try {
    setBusy(true);
    if (state.audio) state.audio.pause();
    const audio = new Audio(api.mediaUrl(path));
    audio.preload = "metadata";
    audio.addEventListener("error", () => {
      toast("配音不可用，请刷新页面或重新登录。", true);
    }, { once: true });
    state.audio = audio;
    const card = button.closest(".audio-demo");
    const progress = $(".audio-progress i", card);
    audio.addEventListener("timeupdate", () => {
      if (progress && audio.duration) progress.style.width = `${(audio.currentTime / audio.duration) * 100}%`;
    });
    audio.addEventListener("ended", () => {
      button.textContent = "▶";
      if (progress) progress.style.width = "0";
    });
    button.textContent = "Ⅱ";
    await audio.play();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

function renderBrand() {
  return `<div class="brand">
    <div class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></div>
    <div><strong>TubeFlow</strong><small>AI VIDEO STUDIO</small></div>
  </div>`;
}

async function boot() {
  try {
    const setup = await api.setup();
    if (!setup.initialized) {
      renderAuth("setup");
      return;
    }
    if (!state.token) {
      renderAuth("login");
      return;
    }
    state.me = await api.me();
    await loadWorkspace();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      clearToken();
      renderAuth("login", "登录已失效，请重新登录。");
      return;
    }
    renderConnectionError(error);
  }
}

function renderConnectionError(error) {
  clearObjectUrls();
  app.className = "auth-page";
  app.innerHTML = `<section class="auth-card">
    ${renderBrand()}
    <span class="eyebrow">BACKEND UNAVAILABLE</span>
    <h1>无法连接真实后端</h1>
    <p>${escapeHtml(messageOf(error))}</p>
    <button id="retryConnection" class="primary" type="button">重新连接</button>
  </section>`;
  $("#retryConnection").addEventListener("click", boot);
}

function renderAuth(kind, note = "") {
  clearObjectUrls();
  const setup = kind === "setup";
  app.className = "auth-shell";
  app.innerHTML = `<section class="auth-brand-panel">
    ${renderBrand()}
    <div>
      <span class="eyebrow">TUBEFLOW AI</span>
      <h1>从真实配音时间线到组级视频成片</h1>
      <p>所有项目、任务、版本和媒体状态均来自后端权威数据。</p>
    </div>
    <small>LOCAL AI VIDEO PRODUCTION</small>
  </section>
  <section class="auth-form-panel">
    <div>
      <span class="eyebrow">${setup ? "FIRST RUN" : "SECURE ACCESS"}</span>
      <h1>${setup ? "初始化管理员" : "登录工作台"}</h1>
      <p>${setup ? "一次性初始化，完成后该入口不可再次使用。" : "使用管理员创建的账号登录；不提供自助注册或找回密码。"}</p>
      ${note ? `<div class="notice-card error"><strong>提示</strong><p>${escapeHtml(note)}</p></div>` : ""}
      <form id="authForm">
        <label class="field"><span>用户名</span><input name="username" autocomplete="username" required /></label>
        <label class="field"><span>密码</span><input name="password" type="password" minlength="8" autocomplete="${setup ? "new-password" : "current-password"}" required /></label>
        ${setup ? "" : `<label class="check"><input name="remember" type="checkbox" /> 记住登录（默认不勾选）</label>`}
        <div class="auth-actions"><button class="primary-button" type="submit">${setup ? "完成初始化" : "登录"}</button></div>
      </form>
    </div>
  </section>`;

  $("#authForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    try {
      setBusy(true);
      if (setup) {
        await api.initialize({ username: values.username, password: values.password });
        renderAuth("login", "初始化完成，请登录。");
        return;
      }
      const result = await api.login({ username: values.username, password: values.password });
      clearToken();
      const storage = values.remember ? localStorage : sessionStorage;
      storage.setItem("tf-token", result.token);
      state.token = result.token;
      api.setToken(result.token);
      state.me = await api.me();
      await loadWorkspace();
    } catch (error) {
      renderAuth(kind, messageOf(error));
    } finally {
      setBusy(false);
    }
  });
}

async function loadWorkspace() {
  [state.channels, state.projects, state.voices] = await Promise.all([
    api.channels(),
    api.projects(),
    api.voices(),
  ]);
  state.activeChannelId = resolveActiveChannelId(state.channels, readActiveChannelPreference());
  state.assetChannelId = state.activeChannelId || state.channels[0]?.id || null;
  state.images = state.assetChannelId ? await api.images(state.assetChannelId) : [];
  renderShell();
  const route = directorViewFromHash(globalThis.location?.hash || "");
  const routeProject = route ? state.projects.find((project) => project.id === route.projectId) : null;
  if (routeProject
    && stageDefinitions(routeProject).some((definition) => definition.key === route.stage)) {
    await openProject(route.projectId, route);
    return;
  }
  renderProjects();
}

function renderShell() {
  clearObjectUrls();
  app.className = "app-shell";
  app.innerHTML = `<aside class="sidebar" aria-label="主导航">
    ${renderBrand()}
    <nav class="main-nav" aria-label="工作区导航">
      <button class="nav-item" data-nav="channels" type="button"><span class="nav-icon">◫</span><span>频道管理</span></button>
      <button class="nav-item is-active" data-nav="projects" type="button"><span class="nav-icon">▶</span><span>视频项目</span></button>
      ${state.me?.role === "admin" ? `<button class="nav-item" data-nav="admin" type="button"><span class="nav-icon">⚙</span><span>账号管理</span></button>` : ""}
    </nav>
    <div class="sidebar-section">
      <div class="sidebar-label-row"><span>我的频道</span><button id="quickAddChannel" class="icon-button" type="button" title="添加频道">＋</button></div>
      <div id="channelRail" class="channel-rail"></div>
    </div>
    <div class="sidebar-footer">
      <div class="storage-row"><span>后端项目</span><strong id="projectCount">0 个项目</strong></div>
      <p>数据来自 PostgreSQL 权威状态</p>
      <div class="user-summary"><strong>${escapeHtml(state.me?.username || "")}</strong><small>${escapeHtml(state.me?.role || "")}</small></div>
      <button id="logoutButton" class="ghost-button compact" type="button">退出登录</button>
    </div>
  </aside>
  <div class="app-content">
    <header class="topbar">
      <div id="breadcrumb" class="breadcrumb"></div>
      <div class="topbar-actions">
        <span id="saveStatus" class="save-status"><i></i><span>已连接后端</span></span>
        <span id="videoHealth" class="workflow-badge is-pending">视频服务检测中</span>
        <button id="flowHelp" class="ghost-button compact" type="button">流程说明</button>
      </div>
    </header>
    <main id="viewRoot" class="view-root" tabindex="-1"></main>
  </div>`;

  renderChannelRail();
  bindShellEvents();
  api.videoService()
    .then((result) => {
      const badge = $("#videoHealth");
      if (!badge) return;
      if (result?.available === true) {
        badge.className = "workflow-badge is-ready";
        badge.textContent = "视频服务总体可用";
        return;
      }
      badge.className = "workflow-badge is-error";
      badge.textContent = `视频服务不可用${result?.message ? `：${result.message}` : ""}`;
    })
    .catch((error) => {
      const badge = $("#videoHealth");
      if (!badge) return;
      badge.className = "workflow-badge is-error";
      badge.textContent = `视频服务不可用：${messageOf(error)}`;
    });
}

function bindShellEvents() {
  $$("[data-nav]").forEach((button) => {
    button.addEventListener("click", async () => {
      await navigate(button.dataset.nav);
    });
  });
  $("#quickAddChannel").addEventListener("click", async () => {
    await navigate("channels", { focusCreate: true });
  });
  $("#logoutButton").addEventListener("click", async () => {
    if (state.project && !(await flushPendingSave())) {
      toast("草稿保存失败，已取消注销；请先处理保存问题。", true);
      return;
    }
    let logoutError = null;
    try {
      await api.logout();
    } catch (error) {
      logoutError = error;
    }
    stopProjectsPolling();
    cancelPendingProjectPatch();
    clearToken();
    state.me = null;
    state.project = null;
    state.deletion = null;
    renderAuth(
      "login",
      logoutError ? `服务端注销失败：${messageOf(logoutError)} 本机登录凭证已清除。` : "",
    );
  });
  $("#flowHelp").addEventListener("click", () => {
    openFlowHelpModal();
  });
}

function readActiveChannelPreference() {
  try {
    return localStorage.getItem(ACTIVE_CHANNEL_STORAGE_KEY);
  } catch {
    return null;
  }
}

function rememberActiveChannel(channelId) {
  try {
    localStorage.setItem(ACTIVE_CHANNEL_STORAGE_KEY, activeChannelStorageValue(channelId));
  } catch {
    // 隐私模式等无法写入时只失去记忆能力，不影响当前页面状态。
  }
}

async function selectChannel(channelId) {
  if (state.project && !(await flushPendingSave())) return;
  if (!(await ensureCoverEditsSaved())) return;
  state.project = null;
  state.activeChannelId = channelId;
  rememberActiveChannel(channelId);
  // 「全部频道」只影响项目列表筛选，素材页保留最后一次选中的具体频道。
  state.assetChannelId = channelId || state.assetChannelId || state.channels[0]?.id || null;
  if (channelId) state.images = await api.images(state.assetChannelId);
  else if (!state.assetChannelId) state.images = [];
  renderChannelRail();
  if (state.nav === "channels") {
    if (channelId) renderAssets();
    else await navigate("projects");
  } else if (state.nav === "projects") {
    renderProjects();
  } else {
    await navigate("projects");
  }
}

function renderChannelRail() {
  const rail = $("#channelRail");
  if (!rail) return;
  const railActiveId = state.project
    ? state.project.channel_id
    : (state.nav === "channels" ? state.assetChannelId : state.activeChannelId);
  const allChannels = `<div class="channel-rail-item channel-rail-all ${railActiveId ? "" : "is-active"}">
      <button class="channel-rail-switch" data-channel-all="1" type="button">
        <span class="mini-avatar" aria-hidden="true">全</span>
        <span class="channel-rail-text"><strong>全部频道</strong><small>${state.channels.length} 个频道</small></span>
        <span class="rail-count">${state.projects.length}</span>
      </button>
    </div>`;
  rail.innerHTML = allChannels + (state.channels.map((channel, index) => {
    const count = state.projects.filter((project) => project.channel_id === channel.id).length;
    return `<div class="channel-rail-item ${channel.id === railActiveId ? "is-active" : ""}">
      <button class="channel-rail-switch" data-channel-id="${channel.id}" type="button">
        <span class="mini-avatar" style="--avatar:${channelAvatarColor(index)}">${String(index + 1).padStart(2, "0")}</span>
        <span class="channel-rail-text"><strong>${escapeHtml(channelDisplayName(channel, index))}</strong><small title="${escapeHtml(channel.url)}">${escapeHtml(channel.url)}</small></span>
        <span class="rail-count">${count}</span>
      </button>
      <button class="icon-button" data-rename-channel="${channel.id}" type="button" title="重命名频道">✎</button>
    </div>`;
  }).join("") || `<p class="sidebar-empty">尚无频道</p>`);
  $("#projectCount").textContent = `${state.projects.length} 个项目`;
  $("[data-channel-all]", rail)?.addEventListener("click", () => {
    void selectChannel(null);
  });
  $$("[data-channel-id]", rail).forEach((button) => {
    button.addEventListener("click", () => {
      if (button.querySelector(".channel-rename-input")) return;
      void selectChannel(button.dataset.channelId);
    });
  });
  $$("[data-rename-channel]", rail).forEach((button) => {
    button.addEventListener("click", () => beginChannelRename(button.dataset.renameChannel, button));
  });
}

// 频道内联改名：侧栏与频道管理页共用。Enter/失焦保存、Esc 取消；
// 成功后刷新 rail，若当前在频道管理页则整页重渲，保证两处名称一致。
function beginChannelRename(channelId, editButton) {
  const channel = channelById(channelId);
  const row = editButton?.closest(".channel-rail-item, .channel-row");
  const viewButton = row?.querySelector(".channel-rail-switch, .channel-row-select");
  if (!channel || !row || !viewButton || row.querySelector(".channel-rename-form")) return;
  const form = document.createElement("form");
  form.className = "channel-rename-form";
  const input = document.createElement("input");
  input.className = `channel-rename-input${viewButton.classList.contains("channel-row-select") ? " is-light" : ""}`;
  input.maxLength = 30;
  input.value = channel.name || "";
  input.placeholder = channelDisplayName(null, channelIndex(channelId));
  form.append(input);
  viewButton.replaceWith(form);
  input.focus();
  input.select();
  let settled = false;
  const restore = () => {
    renderChannelRail();
    if (state.nav === "channels") void renderAssets();
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    input.blur();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      settled = true;
      restore();
    }
  });
  input.addEventListener("blur", () => {
    if (settled) return;
    settled = true;
    void saveChannelRename(channelId, input.value, restore);
  });
}

async function saveChannelRename(channelId, rawName, restore) {
  const name = rawName.trim();
  const channel = channelById(channelId);
  if (name === String(channel?.name || "").trim()) {
    restore();
    return;
  }
  try {
    const updated = await api.patchChannel(channelId, { name });
    state.channels = state.channels.map((item) => (item.id === updated.id ? updated : item));
    restore();
    toast(name ? `频道已重命名为「${name}」。` : "已清除频道名，恢复自动编号。");
  } catch (error) {
    toast(messageOf(error), true);
    restore();
  }
}

// 删除频道（2026-10-20 用户确认）：仅空频道可删；确认值＝完整频道名称（未命名时完整 URL）。
// 项目数量在前端先做提示，后端 409 仍是权威判定；成功后收敛当前选择并整页重渲。
async function beginChannelDeletion(channelId) {
  const channel = channelById(channelId);
  if (!channel) return;
  if (!(await ensureCoverEditsSaved())) {
    toast("存在未保存的封面修改，删除操作已取消。", true);
    return;
  }
  const expected = channelDeletionConfirmation(channel);
  const projectCount = state.projects.filter((item) => item.channel_id === channel.id).length;
  if (projectCount > 0) {
    await actionModal({
      title: "无法删除频道",
      eyebrow: "DELETE CHANNEL",
      danger: true,
      confirmText: "知道了",
      bodyHtml: `<div class="notice-card error"><strong>该频道下还有 ${projectCount} 个项目</strong><p>请先在项目列表删除这些项目，再删除频道「${escapeHtml(channelDisplayName(channel, channelIndex(channel.id)))}」。</p></div>`,
    });
    return;
  }
  const result = await actionModal({
    title: "删除频道",
    eyebrow: "DELETE CHANNEL",
    danger: true,
    confirmText: "确认删除频道",
    bodyHtml: `<div class="notice-card error"><strong>不可恢复删除</strong><p>将删除频道「${escapeHtml(channelDisplayName(channel, channelIndex(channel.id)))}」及其频道级资产：角色图片、克隆音色（含 ElevenLabs 远端副本）、封面参考图、封面风格与角色卡。</p></div>
      <label class="field mt-12"><span>请输入完整的${channel.name?.trim() ? "频道名称" : "频道 URL"} <em>*</em></span><input id="deleteChannelConfirmInput" autocomplete="off" /></label>
      <p class="modal-hint">需与下方内容完全一致：<code>${escapeHtml(expected)}</code></p>
      <p id="deleteChannelHint" class="modal-hint" hidden>输入内容不完整或不匹配。</p>`,
    setup: ({ submit }) => {
      const input = $("#deleteChannelConfirmInput");
      const hint = $("#deleteChannelHint");
      submit.disabled = true;
      input.addEventListener("input", () => {
        const matched = input.value === expected;
        submit.disabled = !matched;
        hint.hidden = matched || !input.value;
      });
      input.focus();
    },
  });
  if (!result.confirmed) return;
  try {
    setBusy(true);
    const deletion = await api.deleteChannel(channel.id, expected);
    const index = state.channels.findIndex((item) => item.id === channel.id);
    state.channels = state.channels.filter((item) => item.id !== channel.id);
    const next = state.channels[index] || state.channels[index - 1] || null;
    const wasAssetChannel = state.assetChannelId === channel.id;
    if (wasAssetChannel) {
      state.assetChannelId = next?.id || null;
      state.images = [];
      state.clonedVoices = [];
      state.clonedVoiceChannelId = null;
      state.coverChannelStyle = null;
    }
    // 项目列表筛选（activeChannelId）只在它恰好指向被删频道时才回落；
    // 「全部频道」是 null，不能因为删素材频道被改成下一个频道。
    if (state.activeChannelId === channel.id) {
      state.activeChannelId = next?.id || null;
      rememberActiveChannel(state.activeChannelId);
    }
    if (wasAssetChannel) await renderAssets();
    else renderChannelRail();
    if (Number(deletion?.remote_voice_cleanup_failures) > 0) {
      toast(`频道已删除，但有 ${deletion.remote_voice_cleanup_failures} 个克隆音色远端副本清理失败，已记录日志。`, true);
    } else if (Number(deletion?.object_cleanup_failures) > 0) {
      toast(`频道已删除，但有 ${deletion.object_cleanup_failures} 个资产文件清理失败，已记录日志。`, true);
    } else {
      toast("频道已删除。");
    }
  } catch (error) {
    if (error.code === "channel_has_projects") {
      toast("请先删除该频道下的项目。", true);
      return;
    }
    if (error.status === 404) {
      state.channels = state.channels.filter((item) => item.id !== channel.id);
      if (state.assetChannelId === channel.id) state.assetChannelId = null;
      await renderAssets();
      toast("该频道已被删除。");
      return;
    }
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

function setBreadcrumb(parts) {
  const root = $("#breadcrumb");
  if (!root) return;
  root.innerHTML = parts.map((part, index) => {
    const last = index === parts.length - 1;
    return `${index ? "<b>/</b>" : ""}<${last ? "strong" : "span"}>${escapeHtml(part)}</${last ? "strong" : "span"}>`;
  }).join("");
}

function markNavigation(name) {
  state.nav = name;
  $$("[data-nav]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.nav === name);
  });
}

async function navigate(name, options = {}) {
  stopNarrationPolling();
  if (state.project && !(await flushPendingSave())) return false;
  if (state.project && !(await ensureDirectorDraftSaved())) return false;
  if (!(await ensureCoverEditsSaved())) return false;
  stopDirectorPolling();
  stopAutoPipelinePolling();
  stopProjectsPolling();
  clearObjectUrls();
  state.project = null;
  state.directorStageCache = null;
  state.directorDraftDirty = false;
  state.directorDraftContext = null;
  clearLocationHash();
  markNavigation(name);
  if (name === "channels") await renderAssets(options);
  else if (name === "admin" && state.me?.role === "admin") await renderAdmin();
  else {
    renderChannelRail();
    renderProjects();
  }
  return true;
}

function renderProjects() {
  stopNarrationPolling();
  stopDirectorPolling();
  stopAutoPipelinePolling();
  startProjectsPolling();
  state.renderEpoch += 1;
  clearObjectUrls();
  markNavigation("projects");
  setBreadcrumb(["TubeFlow AI", "视频项目"]);
  const root = $("#viewRoot");
  const channel = channelById(state.activeChannelId);
  const channelProjects = state.projects.filter((project) => (channel ? project.channel_id === channel.id : true));
  const counts = projectStatusCounts(channelProjects);
  const filterChip = (key, label, showCount = true) => `<button type="button" data-project-filter="${key}" class="${state.projectListFilter === key ? "is-active" : ""}">${label}${showCount ? ` <b>${counts[key]}</b>` : ""}</button>`;
  const personaImages = channel && state.assetChannelId === channel.id
    ? resolveDefaultCharacterImages(channel, state.images)
    : [];
  const frozen = channelProjects.filter((project) => project.snapshot).length;
  const defaultVoice = channelDefaultVoice(channel);
  const canCreate = canCreateProjectForChannel(channel);
  const allChannels = !channel && state.channels.length > 0;
  const createAttrs = canCreate
    ? ""
    : (allChannels ? `aria-disabled="true" title="请先在左侧选择频道"` : "disabled");
  const heroChannelName = channel
    ? channelLabel(channel)
    : (state.channels.length ? "全部频道" : "尚无频道");
  const heroChannelSubtitle = channel?.url
    || (state.channels.length ? `${state.channels.length} 个频道 · 共 ${state.projects.length} 个项目` : "请先添加频道 URL");

  root.innerHTML = `<div class="page">
    <div class="page-heading">
      <div><span class="eyebrow">VIDEO PROJECTS</span><h1>视频项目</h1><p>项目状态只来自后端；页面不会生成虚假完成百分比。</p></div>
      <div class="heading-actions"><button id="newProjectButton" class="primary-button" type="button" ${createAttrs}>＋ 新建视频项目</button></div>
    </div>
    <section class="overview-hero${personaImages.length ? " has-persona" : ""}">
      <div class="hero-content">
        <div class="hero-channel-row"><span class="mini-avatar" ${channel ? `style="--avatar:${channelAvatarColor(channelIndex(channel.id))}"` : ""}>${channel ? String(channelIndex(channel.id) + 1).padStart(2, "0") : "—"}</span><div><strong>${escapeHtml(heroChannelName)}</strong><small title="${escapeHtml(heroChannelSubtitle)}">${escapeHtml(heroChannelSubtitle)}</small></div></div>
        <h2>真实后端视频生产工作台</h2>
        <p>文案、角色、风格、音色、语言和画幅将在确认配音后形成不可变快照。</p>
      </div>
      ${personaImages.length ? `<div class="hero-persona">
        <div class="hero-persona-stack">${personaImages.map((image, index) => `<img class="hero-persona-image hero-persona-image-${index + 1}" data-protected-image="/identity/channels/${channel.id}/character-images/${image.id}/content" alt="频道 IP 形象 ${index + 1}" />`).join("")}</div>
        <small>频道 IP 形象</small>
      </div>` : ""}
      <div class="hero-stats">
        <div class="hero-stat"><strong>${channelProjects.length}</strong><span>${channel ? "频道项目数" : "全部项目数"}</span></div>
        <div class="hero-stat"><strong>${frozen}</strong><span>已冻结项目</span></div>
        <div class="hero-stat"><strong>${channelProjects.length - frozen}</strong><span>草稿项目</span></div>
        <div class="hero-stat"><strong class="is-voice-name" title="${escapeHtml(channel && defaultVoice ? defaultVoice.display_name : "")}">${channel ? (defaultVoice ? escapeHtml(defaultVoice.display_name) : "未设置") : "—"}</strong><span>频道默认音色</span></div>
      </div>
    </section>
    ${channel && !defaultVoice ? `<div class="notice-card amber"><strong>请先设置频道默认音色</strong><p>新项目必须从频道默认音色开始；请到频道管理页试听并选择一个固定公共音色。</p></div>` : ""}
    <div class="section-head">
      <div><h2>项目列表</h2><p>${channel ? escapeHtml(channelLabel(channel)) : "全部频道"}</p></div>
      <div class="section-tools">
        <label class="search-box"><span>⌕</span><input id="projectSearch" value="${escapeHtml(state.projectListQuery)}" placeholder="搜索项目标题" /></label>
        <div class="filter-chips" role="group" aria-label="按状态筛选">${filterChip("all", "全部")}${filterChip("in_progress", "进行中")}${filterChip("done", "已完成")}${filterChip("attention", "需恢复")}${filterChip("unpublished", "未发布", false)}${filterChip("published", "已发布", false)}</div>
        <button id="projectSortToggle" class="ghost-button compact" type="button" title="切换创建时间排序">${state.projectListSort === "newest" ? "最新优先" : "最旧优先"}</button>
        <div class="segment-filter" role="group" aria-label="切换项目视图"><button type="button" data-project-view="grid" class="${state.projectListView === "grid" ? "is-active" : ""}" title="网格视图">▦</button><button type="button" data-project-view="list" class="${state.projectListView === "list" ? "is-active" : ""}" title="列表视图">☰</button></div>
      </div>
    </div>
    <div id="projectGridRegion" tabindex="-1">${projectGridMarkup(channelProjects, canCreate, allChannels)}</div>
  </div>`;

  hydrateProtectedImages(root);
  const openCreate = () => renderCreateProject();
  $("#newProjectButton")?.addEventListener("click", openCreate);
  bindProjectGridEvents(openCreate);
  // 搜索输入只重渲网格容器，避免整页重渲丢失焦点；与轮询的焦点保护行为对齐。
  $("#projectSearch")?.addEventListener("input", (event) => {
    state.projectListQuery = event.target.value;
    renderProjectGrid();
  });
  $$("[data-project-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.projectListFilter = button.dataset.projectFilter;
      renderProjects();
    });
  });
  $("#projectSortToggle")?.addEventListener("click", () => {
    state.projectListSort = state.projectListSort === "newest" ? "oldest" : "newest";
    renderProjects();
  });
  $$("[data-project-view]").forEach((button) => {
    button.addEventListener("click", () => {
      if (state.projectListView === button.dataset.projectView) return;
      state.projectListView = button.dataset.projectView;
      renderProjects();
    });
  });
}

// 未发布徽章在两种未发布分支共用（有当前成片可标记 / 无当前成片不可标记），
// 与已发布徽章同为「圆点 + 文案」结构，避免同一行出现两种状态外观。
function projectPublicationMarkup(project) {
  const publication = projectPublicationState(project);
  const deleting = project.flow?.stage === "DELETING";
  const unpublished = `<span class="publication-state is-unpublished"><i aria-hidden="true"></i>未发布</span>`;
  if (publication === "published") {
    return `<span class="publication-state is-published"><i aria-hidden="true"></i>已发布</span>${deleting ? "" : `<button class="publication-action" type="button" data-unpublish-project="${project.id}" aria-label="取消已发布：${escapeHtml(projectTitle(project))}">取消已发布</button>`}`;
  }
  // 有当前成片时状态与操作并存：左「未发布」右「标记已发布」，与已发布行同构。
  if (publication === "publishable" && !deleting) {
    return `${unpublished}<button class="publication-mark" type="button" data-publish-project="${project.id}" aria-label="标记已发布：${escapeHtml(projectTitle(project))}">标记已发布</button>`;
  }
  return unpublished;
}

// 详情页发布控制（AUTO PRODUCTION / STEP 09 头部 / STEP 09 的 SRT 预览共用）：published_at 是唯一事实源；
// 已发布只留绿色徽章（取消入口仍在项目列表），无当前成片时按钮保留但禁用。
// buttonId 让同一页面的多个发布入口使用不同 id，避免重复 id 导致后出现的入口点不动。
function publicationHeaderMarkup(project, canPublish, buttonId = "publishProjectFromStage") {
  if (projectIsPublished(project)) {
    return `<span class="publication-state is-published" title="发布于 ${escapeHtml(formatDateTime(project.published_at))}"><i aria-hidden="true"></i>已发布</span>`;
  }
  const hint = canPublish ? "标记为已发布；取消入口在项目列表" : "需要当前成片后才能标记发布";
  return `<button id="${buttonId}" class="ghost-button compact" type="button" title="${hint}" ${canPublish ? "" : "disabled"}>标记已发布</button>`;
}

function projectCard(project) {
  const channel = channelById(project.channel_id);
  const badge = projectFlowBadge(project);
  const coverPath = projectCoverMediaPath(project);
  const title = projectTitle(project);
  const letter = [...title.trim()][0] || "项";
  return `<article class="project-card">
    <button class="project-card-hit" type="button" data-project-open="${project.id}" aria-label="打开项目：${escapeHtml(title)}"></button>
    <div class="project-cover cover-gradient-storm">
      ${coverPath ? `<img data-protected-image="${escapeHtml(coverPath)}" alt="项目封面" />` : `<span class="cover-letter" aria-hidden="true">${escapeHtml(letter)}</span>`}
      <span class="cover-aspect">${escapeHtml(project.aspect_ratio)}</span>
    </div>
    <div class="project-card-body">
      <div class="project-card-main">
        <h3>${escapeHtml(title)}</h3>
        <div class="project-meta"><span>${escapeHtml(localeLabel(project.locale))}</span><span>${escapeHtml(channel ? channelLabel(channel) : "频道不可用")}</span></div>
      </div>
      <div class="project-card-foot">
        <p class="project-created"><span class="project-created-icon" aria-hidden="true">📅</span>${escapeHtml(formatDateTime(project.created_at))}</p>
        <div class="project-publication">${projectPublicationMarkup(project)}</div>
      </div>
    </div>
    <span class="card-status tone-${badge.tone}"><i aria-hidden="true"></i>${escapeHtml(badge.text)}</span>
  </article>`;
}

// 项目列表网格区：筛选/排序/搜索只重渲该容器，空态区分搜索无匹配、筛选无匹配与真正的无项目。
function projectGridMarkup(channelProjects, canCreate, allChannels = false) {
  const projects = visibleProjects(channelProjects, {
    query: state.projectListQuery,
    filter: state.projectListFilter,
    sort: state.projectListSort,
  }, projectTitle);
  const emptyKind = projectListEmptyKind(channelProjects.length, projects.length, state.projectListQuery, state.projectListFilter);
  const createAttrs = canCreate ? "" : (allChannels ? `aria-disabled="true" title="请先在左侧选择频道"` : "disabled");
  if (!emptyKind) {
    return `<div class="project-grid${state.projectListView === "list" ? " is-list" : ""}">${projects.map(projectCard).join("")}<button id="addProjectCard" class="add-card" type="button" ${createAttrs}><span class="add-card-inner"><b>＋</b><strong>创建新项目</strong><small>继承频道默认音色</small></span></button></div>`;
  }
  if (emptyKind === "search") {
    return `<div class="empty-state"><div><b>搜</b><h3>没有匹配「${escapeHtml(state.projectListQuery.trim())}」的项目</h3><p>请检查关键词，或清除搜索查看全部项目。</p><button id="clearProjectSearch" class="secondary-button compact" type="button">清除搜索</button></div></div>`;
  }
  if (emptyKind === "filter") {
    return `<div class="empty-state"><div><b>筛</b><h3>当前筛选状态下暂无项目</h3><p>切换到「全部」查看频道内所有项目。</p><button id="showAllProjects" class="secondary-button compact" type="button">查看全部项目</button></div></div>`;
  }
  return `<div class="empty-state"><div><b>影</b><h3>还没有项目</h3><p>请先准备频道 URL、角色图片和频道默认音色。</p><button id="emptyNewProject" class="primary-button" type="button" ${createAttrs}>创建项目</button></div></div>`;
}

function renderProjectGrid() {
  const region = $("#projectGridRegion");
  if (!region) return;
  const channel = channelById(state.activeChannelId);
  const channelProjects = state.projects.filter((project) => (channel ? project.channel_id === channel.id : true));
  const allChannels = !channel && state.channels.length > 0;
  region.innerHTML = projectGridMarkup(channelProjects, canCreateProjectForChannel(channel), allChannels);
  hydrateProtectedImages(region);
  bindProjectGridEvents(() => renderCreateProject());
}

function bindProjectGridEvents(openCreate) {
  $("#addProjectCard")?.addEventListener("click", openCreate);
  $("#emptyNewProject")?.addEventListener("click", openCreate);
  $("#clearProjectSearch")?.addEventListener("click", () => {
    state.projectListQuery = "";
    const input = $("#projectSearch");
    if (input) input.value = "";
    renderProjectGrid();
  });
  $("#showAllProjects")?.addEventListener("click", () => {
    state.projectListFilter = "all";
    renderProjects();
  });
  $$("[data-project-open]").forEach((card) => card.addEventListener("click", () => openProject(card.dataset.projectOpen)));
  $$("[data-publish-project]").forEach((button) => button.addEventListener("click", () => void changeProjectPublication(button.dataset.publishProject, true)));
  $$("[data-unpublish-project]").forEach((button) => button.addEventListener("click", () => void changeProjectPublication(button.dataset.unpublishProject, false)));
}

async function changeProjectPublication(projectId, publish) {
  if (state.projectPublicationPending.has(projectId)) return;
  const project = state.projects.find((item) => item.id === projectId);
  if (!project) return;
  if (!publish) {
    const decision = await actionModal({
      title: "取消已发布",
      eyebrow: "PUBLICATION",
      bodyHtml: `<p>确认将「${escapeHtml(projectTitle(project))}」改回「未发布」？此操作只清除发布标记，不会删除项目或成片。</p>`,
      confirmText: "取消已发布",
      danger: true,
    });
    if (!decision.confirmed) return;
  }
  state.projectPublicationPending.add(projectId);
  state.projectsPollGeneration += 1;
  try {
    const updated = publish
      ? await api.publishProject(projectId)
      : await api.unpublishProject(projectId);
    if (updated && Object.prototype.hasOwnProperty.call(updated, "published_at")) {
      state.projects = state.projects.map((item) => (
        item.id === projectId ? { ...item, published_at: updated.published_at ?? null } : item
      ));
    } else {
      state.projects = await api.projects();
    }
    renderProjectGrid();
    toast(publish ? "已标记为已发布。" : "已取消发布标记。");
    const next = $(publish
      ? `[data-unpublish-project="${projectId}"]`
      : `[data-publish-project="${projectId}"]`);
    (next || $(`[data-project-open="${projectId}"]`) || $("#projectGridRegion"))?.focus();
  } catch (error) {
    if (error?.code === "project_composition_required") {
      toast("该项目还没有当前成片，暂时不能标记发布。", true);
    } else {
      toast(messageOf(error), true);
    }
    renderProjectGrid();
  } finally {
    state.projectPublicationPending.delete(projectId);
  }
}

// 详情页发布：刷新链路不重取 project，成功后必须本地 patch 详情与列表缓存，再整页重渲。
async function publishCurrentProject() {
  const projectId = state.project?.id;
  if (!projectId || projectIsPublished(state.project) || state.projectPublicationPending.has(projectId)) return;
  state.projectPublicationPending.add(projectId);
  try {
    setBusy(true);
    const updated = await api.publishProject(projectId);
    // 契约返回完整项目；仅在缺字段时回退详情 GET，避免把缺失当成「已发布」。
    const fresh = updated && Object.prototype.hasOwnProperty.call(updated, "published_at")
      ? updated
      : await api.project(projectId);
    const publishedAt = fresh?.published_at ?? null;
    if (state.project?.id === projectId) state.project = { ...state.project, published_at: publishedAt };
    // 返回列表时 renderProjects() 先读缓存，不同步会短暂显示「未发布」。
    if (Array.isArray(state.projects)) {
      state.projects = state.projects.map((item) => (
        item.id === projectId ? { ...item, published_at: publishedAt } : item
      ));
    }
    toast("已标记为已发布。");
    if (state.project?.id === projectId) renderProjectWorkspace();
  } catch (error) {
    toast(error?.code === "project_composition_required"
      ? "该项目还没有当前成片，暂时不能标记发布。"
      : error?.code === "project_purging"
        ? "项目正在删除中，不能标记发布。"
        : messageOf(error), true);
    // 409 说明本页 composition 已过期，重渲后按钮自动回到 disabled。
    if (state.project?.id === projectId) renderProjectWorkspace();
  } finally {
    state.projectPublicationPending.delete(projectId);
    setBusy(false);
  }
}

async function renderCreateProject() {
  stopProjectsPolling();
  state.renderEpoch += 1;
  clearObjectUrls();
  const channel = channelById(state.activeChannelId);
  if (!channel) {
    if ((state.channels?.length || 0) > 0) {
      toast("请先在左侧选择频道。", true);
      renderProjects();
      return;
    }
    toast("请先添加频道 URL。", true);
    renderAssets({ focusCreate: true });
    return;
  }
  if (!channelDefaultVoiceReady(channel)) {
    toast(
      providerOf(channel) === ELEVENLABS
        ? "请先为频道设置默认克隆音色。"
        : "请先为频道设置默认音色。",
      true,
    );
    await renderAssets();
    return;
  }
  state.assetChannelId = channel.id;
  $("#viewRoot").innerHTML = `<div class="page"><div class="empty-state is-compact"><div><span class="loading-spinner" aria-hidden="true"></span><h3>正在准备新建项目</h3><p>从后端读取频道角色图片。</p></div></div></div>`;
  state.images = await api.images(channel.id);
  // §23.1：项目默认继承频道配音源与该源下的默认音色（EL 需要本频道克隆音色清单）。
  const cloned = providerOf(channel) === ELEVENLABS
    ? await api.optional(() => api.clonedVoices(channel.id))
    : { data: [] };
  const channelCloned = Array.isArray(cloned.data) ? cloned.data : [];
  let createProvider = providerOf(channel);
  let createClonedVoiceId = channel.default_cloned_voice_id || null;
  // 角色 IP 预选：频道默认角色 IP ∩ 当前图库（按默认顺序）；频道无可用默认时维持第一张勾选的现行为。
  const availableImageIds = state.images.map((image) => image.id);
  const defaultImageIds = normalizeDefaultCharacterIds(channel.default_character_image_ids, availableImageIds);
  const precheckedImageIds = new Set(defaultImageIds.length ? defaultImageIds : availableImageIds.slice(0, 1));
  setBreadcrumb(["TubeFlow AI", "视频项目", "新建项目"]);
  const root = $("#viewRoot");
  root.innerHTML = `<div class="page">
    <div class="page-heading"><div><span class="eyebrow">NEW VIDEO PROJECT</span><h1>创建视频项目草稿</h1><p>后端合同要求创建时收齐生产配置；创建后在九阶段工作区继续调整。</p></div><button id="backToProjects" class="ghost-button" type="button">返回项目列表</button></div>
    <form id="createProjectForm" class="panel">
      <div class="panel-head"><div><h2>项目基础信息</h2><p>文案仅支持粘贴，角色图片选择 1–8 张。</p></div></div>
      <div class="panel-body">
        <div class="form-grid two-cols">
          <label class="field"><span>项目标题 <em>*</em></span><input name="title" maxlength="120" required /></label>
          <label class="field"><span>所属频道</span><input value="${escapeHtml(channelLabel(channel))} · ${escapeHtml(channel.url)}" disabled /></label>
          <label class="field"><span>语言 <em>*</em></span><select name="locale">${LOCALES.map((locale) => `<option value="${locale}">${escapeHtml(localeLabel(locale))}</option>`).join("")}</select></label>
          <label class="field"><span>画幅 <em>*</em></span><select name="aspect_ratio"><option value="16:9">16:9</option></select></label>
          <label class="field"><span>期望时长 <small>可选，mm:ss，仅展示参考</small></span><input name="expected_duration" placeholder="01:30" /></label>
          <label class="field"><span>画面风格 <em>*</em><small>默认取频道设置，可修改</small></span><input name="visual_style" required maxlength="500" value="${escapeHtml(channel.default_visual_style || "")}" placeholder="例如：写实电影感、暖色调、浅景深、粗颗粒胶片" /></label>
        </div>
        <label class="field"><span>文案内容 <em>*</em></span><textarea name="script" rows="10" placeholder="在此粘贴文案" required></textarea></label>
        <div class="panel mt-14"><div class="panel-head"><div><h2>角色 IP</h2><p>选择当前频道共享资产的不可变版本。</p></div><span class="status-pill purple">${state.images.length} 张可选</span></div><div class="panel-body"><div class="asset-grid">${state.images.map((image, index) => `<label class="asset-card"><img data-protected-image="/identity/channels/${channel.id}/character-images/${image.id}/content" alt="角色图 ${index + 1}" /><div class="asset-card-body"><label class="check"><input type="checkbox" name="character_image_ids" value="${image.id}" ${precheckedImageIds.has(image.id) ? "checked" : ""} />角色图 ${String(index + 1).padStart(2, "0")}</label><small>${image.width}×${image.height} · ${escapeHtml(image.media_type)}</small></div></label>`).join("") || `<div class="notice-card amber"><strong>缺少角色图片</strong><p>请先到频道管理页上传。</p></div>`}</div></div></div>
        <div class="panel voice-library-panel mt-14"><div class="panel-head"><div><h2>配音源与音色</h2><p>El 源的项目必须选择本频道克隆音色；固定公共音色仅适用于 CosyVoice。</p></div><span class="status-pill purple">${escapeHtml(ttsProviderLabel(createProvider))}</span></div><div class="panel-body">
          <div class="form-grid two-cols">
            <label class="field"><span>配音源 <em>*</em></span><select id="createTtsProvider"><option value="${COSYVOICE}" ${createProvider === COSYVOICE ? "selected" : ""}>${ttsProviderLabel(COSYVOICE)}</option><option value="${ELEVENLABS}" ${createProvider === ELEVENLABS ? "selected" : ""}>${ttsProviderLabel(ELEVENLABS)}</option></select></label>
            <label class="field" id="createClonedVoiceField"><span>克隆音色 <em>*</em></span><select id="createClonedVoice">${channelCloned.map((voice) => `<option value="${voice.id}" ${voice.id === createClonedVoiceId ? "selected" : ""}>${escapeHtml(voice.name)}</option>`).join("")}</select></label>
          </div>
          <div id="createVoicePicker"></div>
          <div id="createVoiceLocaleNote" class="notice-card amber" hidden><strong>需要选择兼容音色</strong><p></p></div>
          <div id="createClonedVoiceNote" class="notice-card amber" hidden><strong>该频道还没有克隆音色</strong><p>请先到频道管理页上传音频克隆音色，或切换到 CosyVoice 配音源。</p></div>
        </div></div>
        <div class="modal-actions"><button id="createProjectSubmit" class="primary-button" type="submit" disabled>创建并进入九阶段工作区</button></div>
      </div>
    </form>
  </div>`;
  hydrateProtectedImages(root);
  $("#backToProjects").addEventListener("click", async () => {
    await navigate("projects");
  });
  const createForm = $("#createProjectForm");
  const initialLocale = createForm.elements.locale.value;
  let voiceController = null;
  const syncVoiceRequirement = () => {
    const locale = createForm.elements.locale.value;
    const selectedVoice = state.voices.find((voice) => voice.id === voiceController?.getSelectedId());
    const compatibleCount = filterVoices(state.voices, { locale }).length;
    const valid = Boolean(selectedVoice && voiceSupportsLocale(selectedVoice, locale));
    const unavailableNote = $("#createVoiceLocaleNote");
    unavailableNote.hidden = valid;
    if (!valid) {
      $("p", unavailableNote).textContent = compatibleCount
        ? `频道默认音色不支持 ${localeLabel(locale)}，请从下方 ${compatibleCount} 个兼容音色中手动选择。`
        : `系统固定音色库没有支持 ${localeLabel(locale)} 的音色，请更换语言。`;
    }
    // §23.1/§23.2：EL 源用频道克隆音色（多语言模型全覆盖六种首期语言），与公共音色互斥。
    const useElevenLabs = createProvider === ELEVENLABS;
    const clonedSelect = $("#createClonedVoice");
    const clonedReady = Boolean(clonedSelect?.value);
    $("#createClonedVoiceField").hidden = !useElevenLabs;
    clonedSelect.hidden = !useElevenLabs;
    const clonedNote = $("#createClonedVoiceNote");
    clonedNote.hidden = !useElevenLabs || clonedReady;
    $("#createVoicePicker").hidden = useElevenLabs;
    unavailableNote.hidden = useElevenLabs || valid;
    const assignment = useElevenLabs
      ? voiceAssignment(ELEVENLABS, { clonedVoiceId: clonedSelect?.value })
      : voiceAssignment(COSYVOICE, { voiceVersionId: voiceController?.getSelectedId() });
    $("#createProjectSubmit").disabled =
      !state.images.length || !assignment.ok;
  };
  voiceController = mountVoiceLibrary($("#createVoicePicker"), {
    voices: state.voices,
    selectionName: "voice_version_id",
    selectedId: voiceSupportsLocale(channelDefaultVoice(channel), initialLocale)
      ? channelDefaultVoice(channel)?.id
      : null,
    defaultVoiceId: channelDefaultVoice(channel)?.id || null,
    requiredLocale: initialLocale,
    onSelectionChange: syncVoiceRequirement,
  });
  syncVoiceRequirement();
  $("#createTtsProvider")?.addEventListener("change", (event) => {
    createProvider = event.currentTarget.value === ELEVENLABS ? ELEVENLABS : COSYVOICE;
    if (createProvider === ELEVENLABS && !createClonedVoiceId) {
      createClonedVoiceId = channel.default_cloned_voice_id || channelCloned[0]?.id || null;
      const select = $("#createClonedVoice");
      if (select && createClonedVoiceId) select.value = createClonedVoiceId;
    }
    syncVoiceRequirement();
  });
  $("#createClonedVoice")?.addEventListener("change", (event) => {
    createClonedVoiceId = event.currentTarget.value || null;
    syncVoiceRequirement();
  });
  createForm.elements.locale.addEventListener("change", () => {
    voiceController.setRequiredLocale(createForm.elements.locale.value);
    syncVoiceRequirement();
  });
  createForm.addEventListener("submit", (event) =>
    handleCreateProject(event, voiceController, () => createProvider));
}

async function handleCreateProject(event, voiceController, providerOfForm) {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  const imageIds = data.getAll("character_image_ids");
  if (imageIds.length < 1 || imageIds.length > 8) {
    toast("请选择 1–8 张角色图片。", true);
    return;
  }
  const provider = typeof providerOfForm === "function" ? providerOfForm() : COSYVOICE;
  const voiceVersionId = voiceController?.getSelectedId() || null;
  const clonedVoiceId = $("#createClonedVoice")?.value || null;
  const assignment = voiceAssignment(provider, { voiceVersionId, clonedVoiceId });
  if (!assignment.ok) {
    toast(assignment.reason, true);
    return;
  }
  if (provider === COSYVOICE) {
    const selectedVoice = state.voices.find((voice) => voice.id === voiceVersionId);
    if (!selectedVoice || !voiceSupportsLocale(selectedVoice, data.get("locale"))) {
      toast("请选择支持当前语言的公共音色。", true);
      return;
    }
  }
  try {
    setBusy(true);
    const project = await api.createProject({
      title: data.get("title"),
      channel_id: state.activeChannelId,
      script: data.get("script"),
      character_image_ids: imageIds,
      visual_style: data.get("visual_style"),
      voice_version_id: assignment.patch.voice_version_id || null,
      tts_provider: assignment.patch.tts_provider,
      cloned_voice_id: assignment.patch.cloned_voice_id || null,
      locale: data.get("locale"),
      aspect_ratio: data.get("aspect_ratio"),
      expected_duration_seconds: mmss(data.get("expected_duration")),
    });
    state.projects.unshift(project);
    renderChannelRail();
    await openProject(project.id);
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}


async function openProject(projectId, route = null) {
  stopProjectsPolling();
  await flushDescriptionTemplateSaves();
  try {
    setBusy(true);
    state.project = await api.project(projectId);
    state.assetChannelId = state.project.channel_id;
    state.images = await api.images(state.project.channel_id);
    state.activeStage = route && stageDefinitions(state.project).some((definition) => definition.key === route.stage)
      ? route.stage
      : defaultStageKey(state.project);
    state.activeGroupId = null;
    state.directorView = "list";
    state.directorGroupFilter = "all";
    state.directorDraftDirty = false;
    state.directorDraftContext = null;
    state.directorStageCache = null;
    state.coverStyle = null;
    state.coverChannelStyle = null;
    state.coverPrompts = null;
    state.coverImages = null;
    state.coverImageUploading = false;
    state.coverStyleSource = null;
    state.coverTextChoice = null;
    state.coverPromptDraft = null;
    state.coverBrief = "";
    state.coverStyleOverride = "";
    state.coverMirrorScopeKey = null;
    state.coverDimsDraft = null;
    state.coverRefImageDraft = null;
    state.coverEditedTextDraft = null;
    state.coverAnalyzingScope = null;
    state.coverCharacterCards = null;
    state.coverCharacterImages = null;
    state.coverCardSelection = null;
    state.coverCardDrafts = {};
    state.coverCardAnalyzing = null;
    state.coverCardAnalyzingUpdatedAt = null;
    state.coverAnalyzedDims = null;
    state.coverStyleLabelDraft = "";
    state.coverStyleProfileChoice = null;
    state.coverTemplates = null;
    state.coverTemplateDrafts = {};
    state.coverTemplateDirty = {};
    state.coverTemplateSaving = {};
    state.coverTemplateErrors = {};
    state.coverTemplateSavedClock = {};
    state.coverTemplateDragging = null;
    state.description = null;
    state.descriptionTemplates = [];
    state.descriptionSelectedTemplateId = null;
    state.descriptionTemplateDrafts = {};
    state.descriptionTemplateDirty = {};
    Object.values(state.descriptionTemplateSaveTimers).forEach((timer) => clearTimeout(timer));
    state.descriptionTemplateSaveTimers = {};
    state.descriptionTemplateSaveInFlight = {};
    state.descriptionTemplateSavePromises = {};
    state.descriptionTemplateSaveError = "";
    state.descriptionTemplateSavedClock = "";
    state.descriptionEditingId = null;
    state.descriptionEditDraft = "";
    state.descriptionGenerating = false;
    state.descriptionError = "";
    state.narrationFailuresOnly = false;
    state.preplanOffset = 0;
    state.directorPollLastUpdatedAt = null;
    await refreshProjectFacts();
    // 会话恢复的 hash 指向详情视图时落到对应分组；组号失效则回落列表视图。
    if (route?.stage === "director_storyboard" && route.groupSequence != null) {
      const routed = state.groups.find((group) => Number(group.sequence) === route.groupSequence)
        || state.groups[route.groupSequence - 1];
      if (routed) {
        state.activeGroupId = groupId(routed);
        state.directorView = "detail";
      }
    }
    renderChannelRail();
    renderProjectWorkspace();
  } catch (error) {
    toast(messageOf(error), true);
    if ($("#projectGridRegion")) startProjectsPolling();
  } finally {
    setBusy(false);
  }
}

async function refreshProjectFacts() {
  if (!state.project) return;
  const [narrationResult, summaryResult] = await Promise.all([
    api.optional(() => api.narration(state.project.id)),
    api.optional(() => api.summary(state.project.id)),
  ]);
  applyProjectFacts(narrationResult.data, summaryResult.data);
}

function applyProjectFacts(narration, summary) {
  state.narration = narration;
  state.groups = Array.isArray(state.narration?.groups) ? state.narration.groups : [];
  state.summary = summary;
  if (!state.activeGroupId || !state.groups.some((group) => group.group_id === state.activeGroupId)) {
    state.activeGroupId = state.groups[0]?.group_id || null;
  }
}

function renderProjectWorkspace() {
  stopNarrationPolling();
  stopDirectorPolling();
  stopAutoPipelinePolling();
  stopProjectsPolling();
  stopCoverPolling();
  stopCoverWatch();
  $("#storyboardLightbox")?.remove();
  state.renderEpoch += 1;
  clearObjectUrls();
  if (groupCardThumbObserver) {
    groupCardThumbObserver.disconnect();
    groupCardThumbObserver = null;
  }
  state.directorDraftDirty = false;
  state.directorDraftContext = null;
  markNavigation("projects");
  setBreadcrumb(["TubeFlow AI", channelLabel(state.project.channel_id), projectTitle(state.project)]);
  const root = $("#viewRoot");
  const summaryAvailable = Boolean(state.summary);
  root.innerHTML = `<div class="studio">
    <aside class="studio-steps">
      <button id="backToProjectList" class="back-link" type="button">← 返回项目列表</button>
      <div class="step-list">${stageDefinitions().map((definition) => {
        const autoTab = definition.key === AUTO_PROGRESS_STAGE.key;
        const fact = autoTab ? null : stageFact(definition.key);
        const autoState = autoTab ? autoPipelineState(state.summary) : null;
        const status = autoTab
          ? (autoState === "COMPLETED" ? "complete" : "current")
          : fact?.status || "blocked";
        const stepNumber = autoTab ? "⚡" : String(STAGES.indexOf(definition) + 1).padStart(2, "0");
        const note = autoTab
          ? (autoState === "PAUSED"
            ? `已暂停 · ${autoPauseReason(state.summary).label}`
            : autoState ? autoPipelineStateLabel(autoState) : definition.note)
          : blockingReasonLabel(fact?.blocking_reason) || fact?.blocking_reason || definition.note;
        const classes = [
          "step-button",
          definition.key === state.activeStage ? "is-active" : "",
          status === "complete" ? "is-complete" : "",
          status === "blocked" ? "is-blocked" : "",
        ].filter(Boolean).join(" ");
        return `<button class="${classes}" data-stage-key="${definition.key}" type="button">
          <span class="step-number">${status === "complete" ? "✓" : stepNumber}</span>
          <span><strong>${definition.label}</strong><small ${fact?.blocking_reason ? `title="${escapeHtml(fact.blocking_reason)}"` : ""}>${escapeHtml(note)}</small></span>
        </button>`;
      }).join("")}</div>
      <div class="project-step-footer">
        <span class="workflow-badge ${summaryAvailable ? statusClass(activeStageStatusFact()) : "is-error"}">${summaryAvailable ? statusText(activeStageStatusFact()) : "流程摘要不可用"}</span>
        <button id="deleteProjectButton" class="danger-button compact" type="button">删除项目</button>
      </div>
    </aside>
    <section class="studio-main">
      <div id="studioMainInner" class="studio-main-inner"></div>
    </section>
  </div>`;

  $("#backToProjectList").addEventListener("click", async () => {
    await flushDescriptionTemplateSaves();
    await navigate("projects");
  });
  $("#deleteProjectButton").addEventListener("click", deleteCurrentProject);
  $$("[data-stage-key]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!(await flushPendingSave())) return;
      if (!(await ensureDirectorDraftSaved())) return;
      if (!(await ensureCoverEditsSaved())) return;
      if (!(await flushDescriptionTemplateSaves())) return;
      state.activeStage = button.dataset.stageKey;
      renderProjectWorkspace();
    });
  });
  renderActiveStage().catch((error) => {
    const target = $("#studioMainInner");
    if (target) target.innerHTML = renderUnavailable("页面加载失败", messageOf(error));
  }).finally(() => syncLocationHash());
}

async function renderActiveStage() {
  const renderer = {
    auto_progress: renderAutoProgressStage,
    project_configuration: renderConfigurationStage,
    script: renderScriptStage,
    character_ip: renderCharacterStage,
    visual_style: renderVisualStyleStage,
    narration_settings: renderNarrationSettingsStage,
    narration_timeline: renderNarrationTimelineStage,
    director_storyboard: renderDirectorStoryboardStage,
    group_video_preview: renderGroupVideoStage,
    composition_export: renderCompositionStage,
    cover: renderCoverStage,
    description: renderDescriptionStage,
  }[state.activeStage];
  if (!renderer) {
    $("#studioMainInner").innerHTML = renderUnavailable("阶段不可用", "当前阶段没有前端渲染器。");
    return;
  }
  await renderer();
}

function studioHeading(eyebrow, description, actions = "") {
  const title = projectTitle(state.project);
  return `<div class="studio-heading">
    <div><span class="eyebrow">${escapeHtml(eyebrow)}</span><div class="copy-title" title="${escapeHtml(title)}（点击复制）"><h1 id="projectWorkspaceTitle">${escapeHtml(title)}</h1></div><p>${escapeHtml(description)}</p></div>
    <div class="studio-heading-actions">${actions}</div>
  </div>`;
}

function renderUnavailable(title, detail) {
  return `<div class="notice-card amber"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`;
}

function snapshotNote() {
  return state.project.snapshot
    ? `<div class="notice-card"><strong>生产快照已冻结</strong><p>文案、角色 IP、画面风格、音色、语言和画幅不可再修改；项目标题仍可改名。</p></div>`
    : "";
}

function setSaveStatus(text, stateName = "") {
  const root = $("#saveStatus");
  if (!root) return;
  root.classList.toggle("is-saving", stateName === "saving");
  const label = $("span", root);
  if (label) label.textContent = text;
}

function cancelPendingProjectPatch(message = "") {
  window.clearTimeout(state.saveTimer);
  state.saveTimer = null;
  state.pendingSave = null;
  if (message) setSaveStatus(message);
}

function scheduleProjectPatch(changes) {
  if (!changes || typeof changes !== "object" || !Object.keys(changes).length) return;
  window.clearTimeout(state.saveTimer);
  state.pendingSave = { ...(state.pendingSave || {}), ...changes };
  setSaveStatus("等待保存", "saving");
  state.saveTimer = window.setTimeout(() => {
    void flushPendingSave();
  }, 700);
}

async function flushPendingSave() {
  window.clearTimeout(state.saveTimer);
  state.saveTimer = null;
  if (state.savePromise) {
    const inFlight = state.savePromise;
    const saved = await inFlight;
    if (state.savePromise === inFlight) state.savePromise = null;
    if (!saved) return false;
    if (state.savePromise) return flushPendingSave();
    return state.pendingSave ? flushPendingSave() : true;
  }
  const changes = state.pendingSave;
  state.pendingSave = null;
  if (!changes || !Object.keys(changes).length) return true;
  const project = state.project;
  if (!project) return false;

  const savePromise = (async () => {
    try {
      setSaveStatus("保存中", "saving");
      const body = { expected_revision: project.draft_revision, ...changes };
      const updated = await api.patchProject(project.id, body);
      if (state.project?.id === project.id) state.project = updated;
      state.projects = state.projects.map((item) => (item.id === updated.id ? updated : item));
      setSaveStatus("已保存");
      if (state.project?.id === updated.id) {
        setBreadcrumb(["TubeFlow AI", channelLabel(updated.channel_id), projectTitle(updated)]);
        const titleNode = $("#projectWorkspaceTitle");
        if (titleNode) {
          const title = projectTitle(updated);
          titleNode.textContent = title;
          const copyTitle = titleNode.closest(".copy-title");
          if (copyTitle) copyTitle.title = `${title}（点击复制）`;
        }
      }
      return true;
    } catch (error) {
      state.pendingSave = { ...changes, ...(state.pendingSave || {}) };
      setSaveStatus("保存失败");
      toast(`保存失败：${messageOf(error)}`, true);
      return false;
    }
  })();
  state.savePromise = savePromise;
  const saved = await savePromise;
  if (state.savePromise === savePromise) state.savePromise = null;
  if (saved && state.pendingSave) return flushPendingSave();
  return saved;
}

function renderConfigurationStage() {
  const project = state.project;
  const frozen = Boolean(project.snapshot);
  $("#studioMainInner").innerHTML = `${studioHeading("STEP 01 · PROJECT CONFIGURATION", "维护项目管理元数据、语言、画幅与可选期望时长。")}
    ${snapshotNote()}
    <section class="panel">
      <div class="panel-head"><div><h2>项目配置</h2><p>冻结前防抖自动保存；期望时长仅用于展示参考。</p></div><span class="status-pill ${frozen ? "" : "purple"}">${frozen ? "生产字段已锁定" : "草稿可编辑"}</span></div>
      <div class="panel-body">
        <form id="configurationForm" class="form-grid">
          <div class="form-grid two-cols">
            <label class="field"><span>项目标题 <em>*</em></span><input name="title" maxlength="120" value="${escapeHtml(project.title || "")}" required /></label>
            <label class="field"><span>所属频道</span><input value="${escapeHtml(channelLabel(project.channel_id))} · ${escapeHtml(channelById(project.channel_id)?.url || "")}" disabled /></label>
            <label class="field"><span>语言 <em>*</em></span><select name="locale" ${frozen ? "disabled" : ""}>${LOCALES.map((locale) => `<option value="${locale}" ${project.locale === locale ? "selected" : ""}>${escapeHtml(localeLabel(locale))}</option>`).join("")}</select></label>
            <label class="field"><span>画幅 <em>*</em></span><select name="aspect_ratio" ${frozen ? "disabled" : ""}><option value="16:9" ${project.aspect_ratio === "16:9" ? "selected" : ""}>16:9</option>${project.aspect_ratio === "9:16" ? '<option value="9:16" selected>9:16（历史项目）</option>' : ""}</select></label>
            <label class="field"><span>期望时长 <small>可选 mm:ss，不参与生产计算</small></span><input name="expected_duration" value="${project.expected_duration_seconds == null ? "" : duration(project.expected_duration_seconds)}" placeholder="01:30" ${frozen ? "disabled" : ""} /></label>
          </div>
        </form>
        ${frozen ? "" : `<div class="panel voice-library-panel mt-14"><div class="panel-head"><div><h2>语言对应音色</h2><p>更换语言时，当前音色不兼容则必须手动选择；语言和新音色会在同一次保存中提交。</p></div><span class="status-pill purple">固定只读库</span></div><div class="panel-body"><div id="configurationVoiceLocaleNote" class="notice-card amber" hidden><strong>尚未保存语言变更</strong><p></p></div><div id="configurationVoicePicker"></div></div></div>`}
      </div>
    </section>`;

  const form = $("#configurationForm");
  let configurationVoiceController = null;
  const syncConfigurationVoiceRequirement = () => {
    if (frozen || !configurationVoiceController) return false;
    const requestedLocale = form.elements.locale.value;
    const patch = compatibleLocaleVoicePatch(
      state.voices,
      requestedLocale,
      configurationVoiceController.getSelectedId(),
    );
    const note = $("#configurationVoiceLocaleNote");
    note.hidden = Boolean(patch);
    if (!patch) {
      const compatibleCount = filterVoices(state.voices, { locale: requestedLocale }).length;
      $("p", note).textContent = compatibleCount
        ? `当前音色不支持 ${localeLabel(requestedLocale)}，请从下方 ${compatibleCount} 个兼容音色中手动选择后再保存语言。`
        : `系统固定音色库没有支持 ${localeLabel(requestedLocale)} 的音色，请恢复原语言。`;
    }
    return Boolean(patch);
  };
  if (!frozen) {
    configurationVoiceController = mountVoiceLibrary($("#configurationVoicePicker"), {
      voices: state.voices,
      selectionName: "configuration_voice_version_id",
      selectedId: project.voice_version_id,
      defaultVoiceId: channelById(project.channel_id)?.default_voice_version_id,
      requiredLocale: project.locale,
      onSelectionChange: (voice) => {
        const requestedLocale = form.elements.locale.value;
        const patch = compatibleLocaleVoicePatch(state.voices, requestedLocale, voice?.id);
        syncConfigurationVoiceRequirement();
        if (patch) scheduleProjectPatch(patch);
      },
    });
    syncConfigurationVoiceRequirement();
  }
  form.addEventListener("input", (event) => {
    const data = new FormData(form);
    const changes = { title: data.get("title") };
    if (!frozen) {
      try {
        Object.assign(changes, {
          aspect_ratio: data.get("aspect_ratio"),
          expected_duration_seconds: mmss(data.get("expected_duration")),
        });
      } catch (error) {
        cancelPendingProjectPatch(messageOf(error));
        return;
      }
      if (event.target.name === "locale") {
        configurationVoiceController.setRequiredLocale(data.get("locale"), { fallbackToDefault: false });
        const patch = compatibleLocaleVoicePatch(
          state.voices,
          data.get("locale"),
          configurationVoiceController.getSelectedId(),
        );
        syncConfigurationVoiceRequirement();
        if (patch) Object.assign(changes, patch);
      }
    }
    scheduleProjectPatch(changes);
  });
}

let scriptPreflightTimer = null;
let scriptPreflightToken = 0;

function syncScriptCounts(script) {
  const counts = scriptDraftCounts(script);
  const charCount = $("#scriptCharCount");
  const lineCount = $("#scriptLineCount");
  if (charCount) charCount.textContent = `${counts.chars} 字`;
  if (lineCount) lineCount.textContent = `${counts.lines} 句`;
}

function syncScriptCopyButton() {
  const button = $("#copyScript");
  if (!button) return;
  const copyable = hasCopyableText(scriptCopyText(state.project, $("#scriptEditor")?.value));
  button.setAttribute("aria-disabled", copyable ? "false" : "true");
  button.title = copyable ? "复制完整文案（保留空行与分隔行）" : "文案为空，暂无可复制内容";
}

function renderScriptStage() {
  const frozen = Boolean(state.project.snapshot);
  // AUTO 项目分句与超长处理内部化，不展示分句预检（§15.10）；MANUAL 项目保持预检不变。
  const auto = isAutoProduction();
  const canCopyScript = hasCopyableText(scriptCopyText(state.project, state.project.script || ""));
  $("#studioMainInner").innerHTML = `${studioHeading("STEP 02 · SCRIPT", "文案来源仅支持用户粘贴；页面不会导入文件或自动抓取链接。")}
    ${snapshotNote()}
    <section class="panel">
      <div class="panel-head"><div><h2>文案内容</h2><p>${frozen ? "生产快照中的文案只读。" : "更改后自动保存到真实后端草稿。"}</p></div><div class="panel-head-actions"><span class="status-pill ${frozen ? "" : "purple"}">${frozen ? "已冻结" : "自动保存"}</span><button id="copyScript" class="ghost-button compact copy-script-button" type="button" aria-disabled="${canCopyScript ? "false" : "true"}" title="${canCopyScript ? "复制完整文案（保留空行与分隔行）" : "文案为空，暂无可复制内容"}">复制文案</button></div></div>
      <div class="panel-body"><label class="field"><span>粘贴文案</span><textarea id="scriptEditor" class="copy-editor" ${frozen ? "readonly" : ""}>${escapeHtml(state.project.script || "")}</textarea></label><div class="copy-toolbar"><div class="copy-counts"><span id="scriptCharCount">${scriptDraftCounts(state.project.script).chars} 字</span><span id="scriptLineCount">${scriptDraftCounts(state.project.script).lines} 句</span></div>${auto ? `<span class="copy-auto-note">AUTO 项目：分句与超长预检已内化到自动提交流程，本页无需预检。</span>` : ""}</div></div>
    </section>
    ${frozen || auto ? "" : `<section class="panel"><div class="panel-head"><div><h2>分句预检</h2><p>后端按当前分句规则实时预检：分隔行不参与配音；超长句按从句边界再切分；估算时长仅供冻结前参考，生产只读实测时长。</p></div></div><div class="panel-body"><div id="scriptPreview"><div class="empty-state is-compact"><div><b>句</b><h3>输入文案后自动预检</h3><p>仍可能超过 15 秒的句子会在此高亮，冻结前可继续修改。</p></div></div></div></div></section>`}`;
  syncScriptCopyButton();
  $("#copyScript")?.addEventListener("click", () => {
    const editor = $("#scriptEditor");
    copyTextToClipboard(
      scriptCopyText(state.project, editor?.value),
      "已复制文案",
      "文案为空，暂无可复制内容",
    );
  });
  if (!frozen) {
    $("#scriptEditor").addEventListener("input", (event) => {
      const script = event.target.value;
      syncScriptCounts(script);
      syncScriptCopyButton();
      scheduleProjectPatch({ script });
      if (!auto) scheduleScriptPreflight(script);
    });
    if (!auto && (state.project.script || "").trim()) {
      scheduleScriptPreflight(state.project.script);
    }
  }
}

function scheduleScriptPreflight(script) {
  if (scriptPreflightTimer) clearTimeout(scriptPreflightTimer);
  scriptPreflightTimer = setTimeout(() => {
    scriptPreflightTimer = null;
    void runScriptPreflight(script);
  }, 600);
}

async function runScriptPreflight(script) {
  const host = $("#scriptPreview");
  if (!host || !state.project) return;
  if (!script.trim()) {
    state.scriptPreview = null;
    host.innerHTML = `<div class="empty-state is-compact"><div><b>句</b><h3>输入文案后自动预检</h3><p>仍可能超过 15 秒的句子会在此高亮，冻结前可继续修改。</p></div></div>`;
    return;
  }
  const token = ++scriptPreflightToken;
  try {
    const result = await api.preflight(state.project.id, script);
    if (token !== scriptPreflightToken) return;
    const current = $("#scriptPreview");
    if (!current) return;
    state.scriptPreview = result;
    current.innerHTML = renderScriptPreview(result);
  } catch (error) {
    if (token !== scriptPreflightToken) return;
    const current = $("#scriptPreview");
    if (!current) return;
    state.scriptPreview = null;
    current.innerHTML = `<div class="notice-card amber"><strong>预检未通过</strong><p>${escapeHtml(messageOf(error))}</p></div>`;
  }
}

function renderScriptPreview(result) {
  const summary = summarizePreflight(result);
  const rows = previewRows(result).map((row) => {
    if (row.kind === "separator") {
      return `<div class="sentence-row is-separator sentence-row-preview"><span class="sentence-index">${String(row.sequence).padStart(2, "0")}</span><span class="sentence-text" title="${escapeHtml(row.text)}"><em>分隔行 · 不参与配音与时间轴</em></span><span class="sentence-time">—</span></div>`;
    }
    return `<div class="sentence-row sentence-row-preview${row.overLimit ? " is-anomaly" : ""}"><span class="sentence-index">${String(row.sequence).padStart(2, "0")}</span><span class="sentence-text" title="${escapeHtml(row.text)}">${escapeHtml(row.text)}</span><span class="sentence-time${row.overLimit ? " is-anomaly" : ""}">约 ${formatMilliseconds(row.estimatedMs ?? 0)}${row.overLimit ? " ⚠" : ""}</span></div>`;
  }).join("");
  const warning = confirmWarningText(summary);
  return `<div class="real-status-grid"><article><span>配音句</span><strong>${summary.narratable}</strong><small>${summary.separators} 条分隔行不参与</small></article><article><span>超长风险</span><strong>${summary.overLimit}</strong><small>估算 ≥ 13 秒</small></article></div>${warning ? `<div class="notice-card amber mt-12"><strong>冻结前提醒</strong><p>${escapeHtml(warning)}</p></div>` : ""}<div class="sentence-list mt-12">${rows}</div>`;
}

function renderCharacterStage() {
  const frozen = Boolean(state.project.snapshot);
  const selected = new Set(state.project.character_image_ids || []);
  $("#studioMainInner").innerHTML = `${studioHeading("STEP 03 · CHARACTER IP", "从所属频道共享角色资产库选择 1–8 张不可变图片版本。")}
    ${snapshotNote()}
    <section class="panel">
      <div class="panel-head"><div><h2>角色 IP</h2><p>当前频道共有 ${state.images.length} 张可选图片。</p></div><span id="characterCount" class="status-pill purple">${selected.size} / 8</span></div>
      <div class="panel-body">${state.images.length ? `<div id="characterPicker" class="asset-grid">${state.images.map((image, index) => `<label class="asset-card ${selected.has(image.id) ? "is-selected" : ""}"><img data-protected-image="/identity/channels/${state.project.channel_id}/character-images/${image.id}/content" alt="角色图 ${index + 1}" /><div class="asset-card-body"><label class="check"><input type="checkbox" value="${image.id}" ${selected.has(image.id) ? "checked" : ""} ${frozen ? "disabled" : ""} />角色图 ${String(index + 1).padStart(2, "0")}</label><small>${image.width}×${image.height} · ${escapeHtml(image.media_type)}</small></div></label>`).join("")}</div>` : `<div class="empty-state is-compact"><div><b>图</b><h3>频道尚无角色图片</h3><p>请先到频道管理页上传角色图片；同频道项目共享不可变版本。</p></div></div>`}</div>
    </section>`;
  hydrateProtectedImages($("#studioMainInner"));
  if (!frozen) {
    $("#characterPicker")?.addEventListener("change", () => {
      const ids = $$("#characterPicker input:checked").map((input) => input.value);
      $("#characterCount").textContent = `${ids.length} / 8`;
      $$(".asset-card", $("#characterPicker")).forEach((card) => {
        card.classList.toggle("is-selected", Boolean($("input", card)?.checked));
      });
      if (ids.length < 1 || ids.length > 8) {
        cancelPendingProjectPatch("请选择 1–8 张");
        return;
      }
      scheduleProjectPatch({ character_image_ids: ids });
    });
  }
}

function renderVisualStyleStage() {
  const frozen = Boolean(state.project.snapshot);
  $("#studioMainInner").innerHTML = `${studioHeading("STEP 04 · VISUAL STYLE", "填写项目画面风格；首期不进行图片、视频或链接解析。")}
    ${snapshotNote()}
    <section class="panel">
      <div class="panel-head"><div><h2>画面风格</h2><p>该内容会进入故事板和视频生成输入快照。</p></div><span class="status-pill ${frozen ? "" : "purple"}">${frozen ? "已冻结" : "自动保存"}</span></div>
      <div class="panel-body"><label class="field"><span>风格描述${frozen ? " 🔒" : ""}</span><textarea id="visualStyleEditor" rows="9" placeholder="例如：写实电影感、暖色调、浅景深、粗颗粒胶片" ${frozen ? "readonly" : ""}>${escapeHtml(state.project.visual_style || "")}</textarea></label></div>
    </section>`;
  if (!frozen) {
    $("#visualStyleEditor").addEventListener("input", (event) => {
      const visualStyle = event.target.value;
      // 空风格会在下游导演/故事板/H3 门禁处失败，这里直接拦住自动保存。
      if (!visualStyle.trim()) {
        toast("画面风格不能为空。", true);
        return;
      }
      scheduleProjectPatch({ visual_style: visualStyle });
    });
  }
}

function renderNarrationSettingsStage() {
  const frozen = Boolean(state.project.snapshot);
  const fact = stageFact("narration_settings");
  const canConfirm = !frozen && allows(fact, "confirm_narration");
  const auto = isAutoProduction();
  const provider = providerOf(state.project);
  const useElevenLabs = provider === ELEVENLABS;
  const channel = channelById(state.project.channel_id);
  const frozenFacts = frozenNarrationSummary({
    ...state.project,
    voice_display_name: channelDefaultVoice(channel)?.display_name,
  });
  $("#studioMainInner").innerHTML = `${studioHeading("STEP 05 · NARRATION SETTINGS", auto ? "确认配音源与音色；提交后冻结快照并授权全链路自动生产。" : "确认配音源与音色；确认后同一动作会启动真实配音和无时长前置导演处理。")}
    ${snapshotNote()}
    <section class="panel">
      <div class="panel-head"><div><h2>配音源与音色</h2><p>配音源与音色随确认配音一并冻结；冻结前可改选，冻结后只能新建项目。</p></div><span class="status-pill purple">${escapeHtml(frozenFacts.providerLabel)}</span></div>
      <div class="panel-body">
        <div class="form-grid two-cols">
          <label class="field"><span>配音源${frozen ? " 🔒" : ""}</span><select id="narrationProvider" ${frozen ? "disabled" : ""}><option value="${COSYVOICE}" ${useElevenLabs ? "" : "selected"}>${ttsProviderLabel(COSYVOICE)}</option><option value="${ELEVENLABS}" ${useElevenLabs ? "selected" : ""}>${ttsProviderLabel(ELEVENLABS)}</option></select></label>
          <label class="field"><span>将冻结的音色</span><input disabled value="${escapeHtml(frozenFacts.voiceName)}" /></label>
        </div>
        <div id="narrationVoiceNote" class="notice-card amber" hidden><strong>需要先设置音色</strong><p></p></div>
        <div class="panel mt-14"><div class="panel-head"><div><h2>${useElevenLabs ? "频道克隆音色" : "公共音色"}</h2><p>${useElevenLabs ? "EL 配音只使用本频道上传音频克隆的音色。" : `仅显示支持 ${escapeHtml(localeLabel(state.project.locale))} 的系统固定不可变版本；冻结前可改选。`}</p></div><span class="status-pill purple">${useElevenLabs ? `${channelClonedVoices(channel).length} 个克隆音色` : `${filterVoices(state.voices, { locale: state.project.locale }).length} 个兼容音色`}</span></div>
        <div class="panel-body"><div id="voicePicker"></div></div></div>
      </div>
    </section>
    ${frozen ? "" : `<section class="panel"><div class="panel-head"><div><h2>${auto ? "提交并开始全链路自动生产" : "确认并开始真实配音与前置导演"}</h2><p>${auto ? "该动作会冻结生产字段；提交同步完成分句与超长句处理后，自动串联配音、导演、故事板、视频与合成。" : "该动作会冻结生产字段；后端在同一确认流程中自动提交真实配音和 Director Provider 任务。"}</p></div></div><div class="panel-body"><div class="notice-card amber"><strong>不可逆生产快照与真实 Provider 调用</strong><p>${auto ? "文案、角色、风格、配音源、音色、语言和画幅提交后不可编辑；一次提交即授权全链路自动执行与有界自动重试，无法自动恢复时流程暂停并展示真实原因。" : "文案、角色、风格、配音源、音色、语言和画幅确认后不可编辑；前置导演会按冻结文案分批处理，但不会估算配音时长或生成故事板。"}</p></div>${canConfirm ? "" : renderUnavailable("当前不可确认", fact?.blocking_reason || unavailable("workflow_summary"))}<label class="check mt-14"><input id="confirmNarrationAck" type="checkbox" /> ${auto ? "我已确认全部生产配置并同意授权全链路自动生产" : "我已确认全部生产配置并同意同时启动前置导演"}</label><div class="modal-actions"><button id="confirmNarrationButton" class="primary-button" type="button" disabled>${auto ? "提交并开始生产" : "确认并开始配音与前置导演"}</button></div></div></section>`}`;

  const syncNarrationVoice = () => {
    const selectedProvider = providerOf({ tts_provider: $("#narrationProvider")?.value });
    const elevenlabs = selectedProvider === ELEVENLABS;
    const clonedVoices = channelClonedVoices(channel);
    const selectedCloned = state.project.cloned_voice_id || channel?.default_cloned_voice_id || null;
    const assignment = elevenlabs
      ? voiceAssignment(ELEVENLABS, { clonedVoiceId: selectedCloned })
      : voiceAssignment(COSYVOICE, { voiceVersionId: state.project.voice_version_id });
    const note = $("#narrationVoiceNote");
    note.hidden = assignment.ok;
    if (!assignment.ok) $("p", note).textContent = assignment.reason;
    $("#voicePicker").hidden = elevenlabs;
    const confirmButton = $("#confirmNarrationButton");
    if (confirmButton) confirmButton.disabled = true;
    return elevenlabs ? clonedVoices.length > 0 : true;
  };

  if (useElevenLabs) {
    const clonedVoices = channelClonedVoices(channel);
    $("#voicePicker").innerHTML = clonedVoices.length
      ? clonedVoices
          .map(
            (voice) => `<div class="asset-card ${state.project.cloned_voice_id === voice.id ? "is-selected" : ""}" data-narration-cloned="${voice.id}"><div class="asset-card-body"><strong>${escapeHtml(voice.name)}</strong>${voice.id === channel?.default_cloned_voice_id ? '<span class="status-pill purple">频道默认</span>' : ""}<small>${voice.remark ? escapeHtml(voice.remark) : "无备注"}</small><audio controls preload="none" src="${escapeHtml(api.clonedVoiceSourceUrl(channel.id, voice.id))}"></audio><div class="asset-card-actions"><button type="button" class="ghost-button compact" data-narration-cloned-select="${voice.id}" ${frozen ? "disabled" : ""}>${state.project.cloned_voice_id === voice.id ? "已选择" : "选择"}</button></div></div></div>`,
          )
          .join("")
      : '<div class="notice-card amber"><strong>该频道还没有克隆音色</strong><p>请先到频道管理页上传音频克隆音色，或把配音源切换为 CosyVoice。</p></div>';
    $$("[data-narration-cloned-select]").forEach((button) => {
      button.addEventListener("click", () => {
        if (frozen) return;
        scheduleProjectPatch({ cloned_voice_id: button.dataset.narrationClonedSelect });
        state.project = { ...state.project, cloned_voice_id: button.dataset.narrationClonedSelect };
        renderNarrationSettingsStage();
      });
    });
    // 直接进入配音设置（未经过频道管理页）时清单尚未加载：按频道拉取后重渲本阶段。
    if (channel && state.clonedVoiceChannelId !== channel.id) {
      const epoch = state.renderEpoch;
      const projectId = state.project.id;
      api
        .optional(() => api.clonedVoices(channel.id))
        .then((loaded) => {
          state.clonedVoices = Array.isArray(loaded.data) ? loaded.data : [];
          state.clonedVoiceChannelId = channel.id;
          if (state.renderEpoch === epoch && state.project?.id === projectId) {
            renderNarrationSettingsStage();
          }
        })
        .catch(() => {});
    }
  } else {
    mountVoiceLibrary($("#voicePicker"), {
      voices: state.voices,
      selectionName: "voice_version_id",
      selectedId: state.project.voice_version_id,
      defaultVoiceId: channelById(state.project.channel_id)?.default_voice_version_id,
      requiredLocale: state.project.locale,
      selectable: !frozen,
      onSelectionChange: (voice) => {
        if (voice && !frozen) scheduleProjectPatch({ voice_version_id: voice.id });
      },
    });
  }
  $("#narrationProvider")?.addEventListener("change", async (event) => {
    const next = providerOf({ tts_provider: event.currentTarget.value });
    const patch =
      next === ELEVENLABS
        ? { tts_provider: next, cloned_voice_id: state.project.cloned_voice_id || channel?.default_cloned_voice_id || null }
        : { tts_provider: next, voice_version_id: state.project.voice_version_id || channel?.default_voice_version_id || null };
    if (!patch.cloned_voice_id && next === ELEVENLABS) {
      toast("该频道还没有克隆音色，请先到频道管理页上传。", true);
      event.currentTarget.value = providerOf(state.project);
      return;
    }
    scheduleProjectPatch(patch);
    state.project = { ...state.project, ...patch };
    renderNarrationSettingsStage();
  });
  if (!frozen) {
    syncNarrationVoice();
    $("#confirmNarrationAck")?.addEventListener("change", (event) => {
      $("#confirmNarrationButton").disabled = !event.target.checked || !canConfirm;
    });
    $("#confirmNarrationButton")?.addEventListener("click", confirmNarration);
  }
}

async function playProtectedAudioPath(path) {
  if (!path) return;
  try {
    setBusy(true);
    if (state.audio) state.audio.pause();
    const audio = new Audio(api.mediaUrl(path));
    audio.preload = "metadata";
    audio.addEventListener("error", () => {
      toast("音色试听不可用，请刷新页面或重新登录。", true);
    }, { once: true });
    state.audio = audio;
    await audio.play();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function confirmNarration() {
  if (!stageAllows("narration_settings", "confirm_narration")) {
    toast("后端当前未允许确认配音。", true);
    return;
  }
  const auto = isAutoProduction();
  const submitButton = auto ? $("#confirmNarrationButton") : null;
  try {
    setBusy(true);
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "提交中…正在分句与超长句处理";
    }
    if (!(await flushPendingSave())) {
      if (auto) renderProjectWorkspace();
      return;
    }
    if (state.pendingSave || state.savePromise) {
      throw new Error("仍有项目修改尚未确认保存，不能冻结生产快照。");
    }
    const projectId = state.project.id;
    state.project = await api.project(projectId);
    state.projects = state.projects.map((item) => (
      item.id === state.project.id ? state.project : item
    ));
    // AUTO 项目分句与超长处理已在确认链路内部化，不再调用预检端点（§15.10）。
    const preflight = auto ? null : await api.preflight(projectId, state.project.script);
    await api.confirmNarration(projectId);
    state.project = await api.project(projectId);
    await refreshProjectFacts();
    state.activeStage = auto ? AUTO_PROGRESS_STAGE.key : "narration_timeline";
    renderProjectWorkspace();
    if (auto) {
      toast("已提交，全链路自动生产已开始；进度见「自动生产」页。");
    } else {
      const overLimit = summarizePreflight(preflight).overLimit;
      toast(overLimit > 0
        ? `真实配音与前置导演任务已提交；注意：${overLimit} 句估算配音可能超过 15 秒，对应分组将无法生成视频。`
        : "真实配音与前置导演任务已由同一确认流程提交。");
    }
  } catch (error) {
    toast(messageOf(error), true);
    // AUTO 提交是同步长请求；响应丢失等失败先回读权威状态再决定落点，避免误导重试。
    if (auto && state.project) {
      try {
        state.project = await api.project(state.project.id);
        await refreshProjectFacts();
        if (state.project?.snapshot) state.activeStage = AUTO_PROGRESS_STAGE.key;
        renderProjectWorkspace();
      } catch { /* 回读失败时保留原错误提示；用户可刷新页面核对真实状态。 */ }
    }
  } finally {
    setBusy(false);
  }
}

async function deleteCurrentProject() {
  if (!(await flushPendingSave())) {
    toast("存在未保存的项目修改，删除操作已取消。", true);
    return;
  }
  const project = state.project;
  if (!project) return;
  const expected = projectTitle(project);
  const result = await actionModal({
    title: "删除项目",
    eyebrow: "DELETE PROJECT",
    danger: true,
    confirmText: "确认删除",
    bodyHtml: `<div class="notice-card error"><strong>不可恢复删除</strong><p>删除将物理清理项目专属数据。请输入完整项目标题「${escapeHtml(expected)}」确认。</p></div>
      <label class="field mt-12"><span>项目标题 <em>*</em></span><input id="deleteProjectTitleInput" autocomplete="off" /></label>
      <p id="deleteProjectHint" class="modal-hint" hidden>输入的标题不完整或不匹配。</p>`,
    setup: ({ submit }) => {
      const input = $("#deleteProjectTitleInput");
      const hint = $("#deleteProjectHint");
      submit.disabled = true;
      input.addEventListener("input", () => {
        const matched = input.value === expected;
        submit.disabled = !matched;
        hint.hidden = matched || !input.value;
      });
      input.focus();
    },
  });
  if (!result.confirmed) return;
  const confirmation = expected;
  try {
    setBusy(true);
    const deletion = await api.deleteProject(project.id, confirmation);
    cancelPendingProjectPatch();
    state.deletion = {
      project_id: project.id,
      project_title: expected,
      ...deletion,
    };
    state.project = null;
    state.narration = null;
    state.groups = [];
    state.summary = null;
    clearLocationHash();
    renderDeletionStatus();
    toast("项目删除请求已提交，正在等待后端权威状态。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

function deletionLifecycleStatus(deletion) {
  if (["DELETING", "DELETE_FAILED", "DELETED"].includes(deletion?.status)) {
    return deletion.status;
  }
  return {
    QUEUED: "DELETING",
    RUNNING: "DELETING",
    FAILED: "DELETE_FAILED",
    SUCCEEDED: "DELETED",
  }[deletion?.public_status] || "UNKNOWN";
}

function renderDeletionStatus() {
  state.renderEpoch += 1;
  clearObjectUrls();
  markNavigation("projects");
  const deletion = state.deletion;
  if (!deletion) {
    renderProjects();
    return;
  }
  const lifecycle = deletionLifecycleStatus(deletion);
  const canRefresh = allows(deletion, "REFRESH");
  const canRetry = allows(deletion, "RETRY_DELETE");
  const isFailed = lifecycle === "DELETE_FAILED";
  const isDeleted = lifecycle === "DELETED";
  const projectName = deletion.project_title || "已提交删除的项目";
  setBreadcrumb(["TubeFlow AI", "视频项目", "删除状态"]);
  const root = $("#viewRoot");
  root.innerHTML = `<div class="page">
    <div class="page-heading">
      <div><span class="eyebrow">PROJECT DELETION</span><h1>项目删除状态</h1><p>删除为不可恢复的异步物理清理，页面只展示后端权威结果。</p></div>
      <div class="heading-actions">${canRefresh ? `<button id="refreshDeletionButton" class="ghost-button" type="button">刷新状态</button>` : ""}${canRetry ? `<button id="retryDeletionButton" class="danger-button" type="button">重试删除</button>` : ""}<button id="leaveDeletionButton" class="secondary-button" type="button">返回项目列表</button></div>
    </div>
    <div class="real-status-grid">
      <article><span>项目</span><strong>${escapeHtml(projectName)}</strong><small>${escapeHtml(deletion.project_id || "")}</small></article>
      <article><span>清理状态</span><strong>${escapeHtml(statusText({ status: lifecycle }))}</strong><small>${escapeHtml(lifecycle)}</small></article>
      <article><span>公开状态</span><strong>${escapeHtml(statusText(deletion))}</strong><small>${escapeHtml(deletion.public_status || "后端未返回")}</small></article>
      <article><span>失败对象</span><strong>${escapeHtml(compactCount(deletion.failed_count))}</strong><small>以后端精确清理记录为准</small></article>
    </div>
    <div class="notice-card ${isFailed ? "error" : isDeleted ? "" : "amber"}">
      <strong>${escapeHtml(statusText({ status: lifecycle }))}</strong>
      <p>${escapeHtml(deletion.message || "后端未返回可公开的状态说明。")}</p>
    </div>
    ${!canRefresh && !canRetry && !isDeleted ? renderUnavailable("暂无可执行恢复动作", "后端 allowed_actions 当前未开放刷新或重试；页面不会自行重复提交删除请求。") : ""}
  </div>`;
  $("#refreshDeletionButton")?.addEventListener("click", refreshDeletionStatus);
  $("#retryDeletionButton")?.addEventListener("click", retryCurrentDeletion);
  $("#leaveDeletionButton")?.addEventListener("click", leaveDeletionStatus);
}

async function refreshDeletionStatus() {
  const context = state.deletion;
  if (!context?.project_id || !allows(context, "REFRESH")) return;
  try {
    setBusy(true);
    const deletion = await api.deletion(context.project_id, context.deletion_receipt);
    state.deletion = { ...context, ...deletion };
    if (deletionLifecycleStatus(state.deletion) === "DELETED") {
      state.projects = state.projects.filter((project) => project.id !== context.project_id);
      renderChannelRail();
    }
    renderDeletionStatus();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function retryCurrentDeletion() {
  const context = state.deletion;
  if (!context?.project_id || !allows(context, "RETRY_DELETE")) return;
  try {
    setBusy(true);
    const deletion = await api.retryDeletion(context.project_id, context.deletion_receipt);
    state.deletion = { ...context, ...deletion };
    renderDeletionStatus();
    toast("删除恢复请求已提交，后续结果仍以后端状态为准。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function leaveDeletionStatus() {
  try {
    setBusy(true);
    state.projects = await api.projects();
    state.deletion = null;
    renderChannelRail();
    renderProjects();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}


function narrationSentences() {
  return Array.isArray(state.narration?.sentences) ? state.narration.sentences : [];
}

function narrationTimeline() {
  return Array.isArray(state.narration?.timeline) ? state.narration.timeline : [];
}

function sentenceSequence(item) {
  return Number(item?.sentence?.sequence ?? item?.sequence ?? item?.sentence_sequence ?? 0);
}

function sentenceId(item) {
  return String(item?.sentence_id || "");
}

function sentenceText(item) {
  return item?.sentence?.text || item?.text || "";
}

function timelineForSequence(sequence) {
  return narrationTimeline().find((entry) => Number(entry.sentence_sequence) === Number(sequence)) || null;
}

function narratedSentenceById(id) {
  return narrationSentences().find((item) => sentenceId(item) === String(id)) || null;
}

function groupId(group) {
  return group?.group_id || group?.id || "";
}

function groupDurationMs(group) {
  return Number(group?.target_duration_ms ?? (Number(group?.end_ms || 0) - Number(group?.start_ms || 0)) ?? 0);
}

function activeGroup() {
  return state.groups.find((group) => groupId(group) === state.activeGroupId) || state.groups[0] || null;
}

function sentencesForGroup(group) {
  const sequences = new Set((group?.sentence_sequences || []).map(Number));
  return narrationSentences().filter((sentence) => sequences.has(sentenceSequence(sentence)));
}

function renderGroupTabs(annotations = null) {
  if (!state.groups.length) return "";
  const tabs = state.groups.map((group, index) => {
    const active = groupId(group) === state.activeGroupId;
    const note = annotations?.get(groupId(group));
    return `<button class="group-tab ${active ? "is-active" : ""}" data-select-group="${groupId(group)}" type="button" ${note ? `title="${escapeHtml(note.label)}"` : ""}>${note ? `<i class="tab-dot is-${note.tone}" aria-hidden="true"></i>` : ""}分组 ${String(index + 1).padStart(2, "0")} · ${formatMilliseconds(groupDurationMs(group))}</button>`;
  }).join("");
  return `<div class="group-nav"><button id="groupNavPrev" class="group-nav-arrow" type="button" aria-label="上一分组" title="上一分组">◀</button><div class="group-tabs">${tabs}</div><button id="groupNavNext" class="group-nav-arrow" type="button" aria-label="下一分组" title="下一分组">▶</button></div>`;
}

function bindGroupTabs(onSelectGroup = null) {
  const selectGroup = (nextGroupId) => {
    if (onSelectGroup) {
      void onSelectGroup(nextGroupId);
      return;
    }
    state.activeGroupId = nextGroupId;
    renderProjectWorkspace();
  };
  $$("[data-select-group]").forEach((button) => {
    button.addEventListener("click", () => selectGroup(button.dataset.selectGroup));
  });
  const stepGroup = (delta) => {
    const index = state.groups.findIndex((group) => groupId(group) === state.activeGroupId);
    const next = Math.min(state.groups.length - 1, Math.max(0, index + delta));
    if (next < 0 || next === index || !state.groups[next]) return;
    selectGroup(groupId(state.groups[next]));
  };
  $("#groupNavPrev")?.addEventListener("click", () => stepGroup(-1));
  $("#groupNavNext")?.addEventListener("click", () => stepGroup(1));
  $(".group-tabs .group-tab.is-active")?.scrollIntoView({ inline: "center", block: "nearest" });
}

function narrationAudioFacts(item) {
  if (!item) return { ready: false, assetId: "", measuredMs: 0 };
  const sequence = sentenceSequence(item);
  const timeline = timelineForSequence(sequence);
  const assetId = timeline?.audio_asset_id || item?.normalized_asset?.asset_id || "";
  const measuredMs = timeline
    ? Number(timeline.end_ms) - Number(timeline.start_ms)
    : Number(item?.normalized_qc?.measured_duration_ms || 0);
  return {
    ready: item?.normalized_qc?.passed === true && Boolean(assetId) && measuredMs > 0,
    assetId,
    measuredMs,
  };
}

function narrationAudioCard(item) {
  if (!item) {
    return `<div class="audio-demo is-pending"><span class="audio-pending-icon">♪</span><div class="audio-demo-content"><strong>待配音</strong><small>后端尚未返回该句真实音频</small></div></div>`;
  }
  const { ready, assetId, measuredMs } = narrationAudioFacts(item);
  if (!ready) {
    return `<div class="audio-demo is-pending"><span class="audio-pending-icon">♪</span><div class="audio-demo-content"><strong>待配音</strong><small>${escapeHtml(item?.normalized_qc?.reason || "真实音频或实测时长尚未就绪")}</small></div></div>`;
  }
  const path = `/projects/${state.project.id}/media/${assetId}/content`;
  return `<div class="audio-demo is-ready">
    <button class="audio-play-button" data-audio-path="${path}" type="button" title="播放真实配音">▶</button>
    <div class="audio-demo-content">
      <div class="audio-file-row"><strong title="${escapeHtml(assetId)}">配音资产 ${escapeHtml(String(assetId).slice(0, 12))}</strong><span>${formatMilliseconds(measuredMs)}</span></div>
      <div class="audio-progress"><i></i></div>
      <small>真实配音 · ${escapeHtml(item?.normalized_asset?.media_type || "受保护媒体")}</small>
    </div>
  </div>`;
}

function bindAudioButtons(root = document) {
  $$("[data-audio-path]", root).forEach((button) => {
    button.addEventListener("click", () => playProtectedAudio(button));
  });
}

function stopNarrationPolling() {
  if (state.narrationPollTimer != null) window.clearTimeout(state.narrationPollTimer);
  state.narrationPollTimer = null;
  state.narrationPollGeneration += 1;
}

function narrationPollIsCurrent(context) {
  return context.generation === state.narrationPollGeneration
    && context.epoch === state.renderEpoch
    && context.projectId === state.project?.id
    && state.activeStage === "narration_timeline" && Boolean(state.token);
}

function scheduleNarrationPolling() {
  stopNarrationPolling();
  if ((!narrationIsRunning(state.narration) && state.narrationRefreshPendingProjectId !== state.project?.id)
    || (state.narrationRetryBusy && state.narrationRetryProjectId === state.project?.id)) return;
  const context = { generation: state.narrationPollGeneration, epoch: state.renderEpoch, projectId: state.project.id };
  let step = 0;
  const schedule = () => {
    if (narrationPollIsCurrent(context)) state.narrationPollTimer = window.setTimeout(tick, DIRECTOR_POLL_DELAYS_MS[step]);
  };
  const tick = async () => {
    state.narrationPollTimer = null;
    if (!narrationPollIsCurrent(context)) return;
    if (document.visibilityState === "hidden" || state.narrationPollInFlight) { schedule(); return; }
    state.narrationPollInFlight = true;
    try {
      const [narration, summary] = await Promise.all([api.narration(context.projectId), api.summary(context.projectId)]);
      if (!narrationPollIsCurrent(context)) return;
      const changed = JSON.stringify([narration, summary]) !== JSON.stringify([state.narration, state.summary]);
      applyProjectFacts(narration, summary);
      if (state.narrationRefreshPendingProjectId === context.projectId) state.narrationRefreshPendingProjectId = null;
      if (changed || !narrationIsRunning(narration)) {
        renderNarrationTimelineStage();
        return;
      }
      if ($("#narrationAutoRefresh")) $("#narrationAutoRefresh").textContent = "自动更新中";
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } catch (error) {
      if (narrationPollIsCurrent(context) && $("#narrationAutoRefresh")) {
        $("#narrationAutoRefresh").textContent = `状态更新失败：${messageOf(error)}；稍后继续查询。`;
      }
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } finally {
      state.narrationPollInFlight = false;
      schedule();
    }
  };
  schedule();
}

window.addEventListener("pagehide", stopNarrationPolling);
window.addEventListener("pagehide", stopAutoPipelinePolling);
// 简介模板保持切页自动落库；封面模板改为手动保存，由 beforeunload 原生提示。
window.addEventListener("pagehide", () => {
  if (Object.keys(state.descriptionTemplateDirty).length) {
    void flushDescriptionTemplateSaves();
  }
});

function narrationRetryResultsMarkup() {
  if (state.narrationRetryProjectId !== state.project?.id) return "";
  return state.narrationRetryResults.map((result) => {
    const row = narrationRows(state.narration).find((item) => item.sequence === result.sequence);
    const message = result.accepted && row?.public_status === "SUCCEEDED"
      ? "配音已完成"
      : result.accepted && ["FAILED", "RECOVERY_REQUIRED"].includes(row?.public_status)
        ? `配音仍需处理：${row.safe_error_code || row.message}`
        : result.message;
    return `<p>句 ${String(result.sequence).padStart(2, "0")}：${escapeHtml(message)}</p>`;
  }).join("");
}

async function retryNarrationRows(rows) {
  if (state.narrationRetryBusy || !rows.length) return;
  const projectId = state.project.id;
  const epoch = state.renderEpoch;
  const confirmResult = await actionModal({
    title: "重试失败配音句",
    eyebrow: "RETRY NARRATION",
    confirmText: "确认重试",
    bodyHtml: `<p>确认重试第 ${rows.map((row) => row.sequence).join("、")} 句？每句将分别提交并报告受理结果。</p><div class="notice-card mt-10"><strong>重试证据</strong><p>${escapeHtml(NARRATION_RETRY_EVIDENCE)}</p></div>`,
  });
  if (!confirmResult.confirmed) return;
  stopNarrationPolling();
  state.narrationRetryBusy = true;
  state.narrationRetryProjectId = projectId;
  state.narrationRefreshPendingProjectId = projectId;
  state.narrationRetryResults = [];
  renderNarrationTimelineStage();
  try {
    for (const row of rows) {
      let result;
      try {
        const accepted = await api.recoverNarration(projectId, row.idempotency_key, NARRATION_RETRY_EVIDENCE);
        if (accepted && state.project?.id === projectId && state.renderEpoch === epoch) applyProjectFacts(accepted, state.summary);
        result = { sequence: row.sequence, accepted: true, message: "重试已受理，等待配音完成。" };
      } catch (error) {
        result = { sequence: row.sequence, accepted: false, message: `未确认受理：${messageOf(error)} 请刷新核对。` };
      }
      state.narrationRetryResults.push(result);
      if (state.project?.id === projectId && $("#narrationRetryResults")) {
        $("#narrationRetryResults").innerHTML = state.narrationRetryResults.map((item) => `<p>句 ${item.sequence}：${escapeHtml(item.message)}</p>`).join("");
      }
    }
    const [narration, summary] = await Promise.all([api.narration(projectId), api.summary(projectId)]);
    if (state.project?.id === projectId && state.renderEpoch === epoch) {
      applyProjectFacts(narration, summary);
      state.narrationRefreshPendingProjectId = null;
    }
  } catch (error) {
    toast(`恢复请求结果已记录，但刷新失败：${messageOf(error)}`, true);
  } finally {
    state.narrationRetryBusy = false;
    if (state.project && state.activeStage === "narration_timeline") renderNarrationTimelineStage();
  }
}

function renderNarrationTimelineStage() {
  const narration = state.narration;
  const fact = stageFact("narration_timeline");
  const ready = narration?.public_status === "SUCCEEDED";
  const audioBySequence = new Map(narrationSentences().map((item) => [sentenceSequence(item), item]));
  const allRows = narrationRows(narration);
  const rows = narrationRows(narration, state.narrationFailuresOnly);
  const failures = narrationRows(narration, true);
  const retryable = retryableNarrationRows(narration);
  const total = Number(narration?.total_duration_ms || 0);
  const readyCount = allRows.filter((row) => row.public_status === "SUCCEEDED").length;
  // 轮询整渲保持滚动位置，避免长列表跳回顶部。
  const scroller = $(".studio-main");
  const previousScrollTop = scroller ? scroller.scrollTop : 0;
  $("#studioMainInner").innerHTML = `${studioHeading("STEP 06 · NARRATION & TIMELINE", "逐句查看配音、失败原因和实测时间线。", '<button id="refreshNarration" class="ghost-button compact" type="button">刷新状态</button>')}
    ${narration ? `<div class="notice-card ${failures.length ? "error" : ""}"><strong>${failures.length ? "需人工处理" : escapeHtml(statusText(narration))}</strong>
      ${failures.length ? `<p>失败句：${failures.map((row) => String(row.sequence).padStart(2, "0")).join("、")}</p>${failures.map((row) => `<p>句 ${row.sequence} · ${escapeHtml(row.safe_error_code || "请查看恢复建议")}：${escapeHtml(row.message)}</p>`).join("")}` : `<p>${escapeHtml(narration.message || fact?.blocking_reason || "后端已返回配音状态。")}</p>`}
      <small id="narrationAutoRefresh">${state.narrationRefreshPendingProjectId === state.project?.id ? "正在核对恢复结果" : narrationIsRunning(narration) ? "自动更新中" : "当前任务已停止自动更新，可手动刷新。"}</small>
    </div>
    <div class="real-status-grid mt-14">
      <article><span>配音状态</span><strong>${escapeHtml(statusText(narration))}</strong><small>后端权威状态</small></article>
      <article><span>句子完成</span><strong>${readyCount} / ${allRows.length}</strong><small>真实音频与 QC</small></article>
      <article><span>实测总时长</span><strong>${formatMilliseconds(total)}</strong><small>来自真实配音</small></article>
      <article><span>配音分组</span><strong>${state.groups.length}</strong><small>完整句顺序装组</small></article>
    </div>
    <section class="panel">
      <div class="panel-head"><div><h2>逐句配音</h2><p>失败句显示错误码和恢复建议；完成句可试听。</p></div>
        <label><input id="narrationFailuresOnly" type="checkbox" ${state.narrationFailuresOnly ? "checked" : ""}> 只看失败（${failures.length}）</label>
        ${retryable.length ? `<button id="retryAllNarration" class="secondary-button compact" type="button" ${state.narrationRetryBusy ? "disabled" : ""}>重试全部失败句（${retryable.length}）</button>` : ""}
      </div>
      <div class="panel-body"><div id="narrationRetryResults" role="status" aria-live="polite">${narrationRetryResultsMarkup()}</div><div class="sentence-list">${rows.map((row) => {
        const audio = audioBySequence.get(row.sequence);
        const statusLine = `${statusText(row)}${row.safe_error_code ? ` · ${row.safe_error_code}` : ""} · ${row.message}`;
        return `<div class="sentence-row sentence-row-narration"><span class="sentence-index">${String(row.sequence).padStart(2, "0")}</span><span class="sentence-text">${escapeHtml(row.text)}<small class="sentence-status" title="${escapeHtml(statusLine)}">${escapeHtml(statusLine)}</small></span><span class="sentence-side">${audio ? narrationAudioCard(audio) : ""}${allows(row, "RETRY_INVALID_OUTPUT") ? `<button class="secondary-button compact" data-retry-narration="${row.sequence}" type="button" ${state.narrationRetryBusy ? "disabled" : ""}>重试</button>` : ""}</span></div>`;
      }).join("") || `<div class="empty-state is-compact"><p>${state.narrationFailuresOnly ? "当前没有失败句。" : "尚无逐句配音状态。"}</p></div>`}</div></div>
    </section>
    ${ready ? `<section class="panel"><div class="panel-head"><div><h2>权威分组时间线</h2><p>分组完全来自逐句实测时间，页面不重新计算。</p></div></div><div class="panel-body"><div class="sentence-list">${state.groups.map((group, index) => `<div class="sentence-row"><span class="sentence-index">${String(index + 1).padStart(2, "0")}</span><span class="sentence-text">句子序号：${escapeHtml((group.sentence_sequences || []).join("、"))}</span><span class="sentence-time">${formatMilliseconds(groupDurationMs(group))}${group.video_blocked ? " · 已阻止视频" : ""}</span></div>`).join("")}</div></div></section>` : ""}
    ` : renderUnavailable("配音状态端点不可用", unavailable("narration_status"))}`;
  if (scroller) scroller.scrollTop = previousScrollTop;
  $("#refreshNarration").addEventListener("click", async () => {
    if (state.narrationPollInFlight || state.narrationRetryBusy) return;
    state.narrationPollInFlight = true;
    stopNarrationPolling();
    const projectId = state.project.id;
    const epoch = state.renderEpoch;
    try {
      const [latest, summary] = await Promise.all([api.narration(projectId), api.summary(projectId)]);
      if (state.project?.id !== projectId || state.renderEpoch !== epoch) return;
      applyProjectFacts(latest, summary);
      renderNarrationTimelineStage();
    } catch (error) {
      toast(messageOf(error), true);
      if (state.project?.id === projectId && state.renderEpoch === epoch) scheduleNarrationPolling();
    } finally {
      state.narrationPollInFlight = false;
    }
  });
  $("#narrationFailuresOnly")?.addEventListener("change", (event) => {
    state.narrationFailuresOnly = event.target.checked;
    renderNarrationTimelineStage();
  });
  $("#retryAllNarration")?.addEventListener("click", () => retryNarrationRows(retryable));
  $$("[data-retry-narration]").forEach((button) => button.addEventListener("click", () => {
    const row = retryable.find((item) => item.sequence === Number(button.dataset.retryNarration));
    if (row) void retryNarrationRows([row]);
  }));
  bindAudioButtons($("#studioMainInner"));
  scheduleNarrationPolling();
}

function compactNarrationAudioCard(sentence, fallbackId = "") {
  if (!sentence) {
    return `<div class="audio-demo is-pending"><span class="audio-pending-icon">♪</span><div class="audio-demo-content"><strong>待配音</strong><small>句子 ${escapeHtml(fallbackId)} 尚未就绪</small></div></div>`;
  }
  const { ready, assetId, measuredMs } = narrationAudioFacts(sentence);
  if (!ready) {
    return `<div class="audio-demo is-pending"><span class="audio-pending-icon">♪</span><div class="audio-demo-content"><strong>待配音</strong><small>${escapeHtml(sentence?.normalized_qc?.reason || "真实音频或实测时长尚未就绪")}</small></div></div>`;
  }
  const suspicious = narrationDurationIsSuspicious(measuredMs);
  const path = `/projects/${state.project.id}/media/${assetId}/content`;
  const text = sentenceText(sentence);
  return `<div class="audio-demo is-ready audio-compact${suspicious ? " is-anomaly" : ""}" title="配音资产 ${escapeHtml(assetId)}">
    <button class="audio-play-button" data-audio-path="${path}" type="button" title="播放真实配音">▶</button>
    <span class="audio-compact-text" title="${escapeHtml(text)}">${escapeHtml(text)}</span>
    <span class="audio-compact-duration" ${suspicious ? 'title="实测时长不足 1 秒，请检查配音"' : ""}>${formatMilliseconds(measuredMs)}${suspicious ? " ⚠" : ""}</span>
    <div class="audio-progress"><i></i></div>
  </div>`;
}

function linkedNarrationForShot(shot) {
  const ids = Array.isArray(shot?.sentence_ids) ? shot.sentence_ids : [];
  if (!ids.length) {
    return `<div class="audio-demo is-pending"><span class="audio-pending-icon">♪</span><div class="audio-demo-content"><strong>待配音</strong><small>当前镜头没有后端句子关联</small></div></div>`;
  }
  return `<div class="linked-narration-stack"><label class="linked-narration-label">关联旁白 · ${ids.length} 条</label>${ids.map((id) => compactNarrationAudioCard(narratedSentenceById(id), id)).join("")}</div>`;
}

function directorSentencePicker(shot, editable, sentences = []) {
  const selected = new Set((shot?.sentence_ids || []).map(String));
  if (!editable) {
    return `<div class="director-sentence-picker is-readonly"><strong>句子关联</strong><p>${selected.size ? `${selected.size} 条真实旁白` : "无句子关联"}</p></div>`;
  }
  if (!sentences.length) {
    return `<div class="director-sentence-picker is-readonly"><strong>句子关联</strong><p>尚无权威配音句子，不能保存导演草稿。</p></div>`;
  }
  return `<fieldset class="director-sentence-picker"><legend>句子关联（至少选择 1 条）</legend>
    <div>${sentences.map((sentence) => {
      const id = sentenceId(sentence);
      return `<label><input data-shot-sentence type="checkbox" value="${escapeHtml(id)}" ${selected.has(id) ? "checked" : ""}><span><b>句 ${String(sentenceSequence(sentence)).padStart(2, "0")}</b>${escapeHtml(sentenceText(sentence))}</span></label>`;
    }).join("")}</div>
  </fieldset>`;
}

function storyboardShotRow(shot, index, editable = false, sentences = []) {
  return `<article class="storyboard-shot-row director-shot-row">
    <div class="shot-row-main" data-director-shot data-shot-id="${escapeHtml(shot.shot_id || "")}">
      <div class="shot-row-title"><span class="shot-kicker">分镜 <b data-shot-number>${String(index + 1).padStart(2, "0")}</b></span>${editable ? `<label class="shot-scene-edit"><span>场景</span><input data-shot-field="scene" required value="${escapeHtml(shot.scene || "")}"></label>` : `<strong class="shot-scene">${escapeHtml(shot.scene || `分镜描述 ${index + 1}`)}</strong>`}${editable ? `<div class="director-shot-actions"><button class="ghost-button compact" data-shot-move="up" type="button" aria-label="上移分镜">↑</button><button class="ghost-button compact" data-shot-move="down" type="button" aria-label="下移分镜">↓</button><button class="danger-button compact" data-shot-delete type="button">删除</button></div>` : ""}</div>
      <div class="shot-row-fields">
        <div class="shot-field"><label>画面描述</label><textarea data-shot-field="description" data-autosize ${editable ? "required" : "readonly"}>${escapeHtml(shot.description || "")}</textarea><small>${escapeHtml([shot.shot_size, shot.camera_viewpoint, shot.narrative_function].filter(Boolean).join(" · "))}</small></div>
        <div class="shot-narration-stack">${linkedNarrationForShot(shot)}</div>
      </div>
      <details class="director-advanced">
        <summary>高级结构字段与句子关联</summary>
        <div class="director-advanced-grid">
          <label><span>景别</span><input data-shot-field="shot_size" ${editable ? "required" : "readonly"} value="${escapeHtml(shot.shot_size || "")}"></label>
          <label><span>机位 / 视角</span><input data-shot-field="camera_viewpoint" ${editable ? "required" : "readonly"} value="${escapeHtml(shot.camera_viewpoint || "")}"></label>
          <label><span>叙事功能</span><input data-shot-field="narrative_function" ${editable ? "required" : "readonly"} value="${escapeHtml(shot.narrative_function || "")}"></label>
          <label class="is-wide"><span>主体动作与结果</span><textarea data-shot-field="subject_action_outcome" data-autosize ${editable ? "required" : "readonly"}>${escapeHtml(shot.subject_action_outcome || "")}</textarea></label>
        </div>
        ${directorSentencePicker(shot, editable, sentences)}
      </details>
    </div>
  </article>`;
}

function directorApprovalEditor(shots, draft, editable) {
  if (shots.length < 2) return "";
  const approvals = Array.isArray(draft?.continuous_observation_approvals)
    ? draft.continuous_observation_approvals
    : [];
  return `<details class="director-approval-panel">
    <summary>连续观察例外审批</summary>
    <p>仅当相邻镜头确实需要保持同一构图连续观察时启用，并填写明确理由。</p>
    <div>${shots.slice(1).map((shot, index) => {
      const previous = shots[index];
      const approval = approvals.find((item) => String(item.previous_shot_id) === String(previous.shot_id) && String(item.next_shot_id) === String(shot.shot_id));
      if (!editable) {
        return `<div class="director-approval-readonly"><strong>${String(index + 1).padStart(2, "0")} → ${String(index + 2).padStart(2, "0")}</strong><span>${escapeHtml(approval?.reason || "未批准连续观察")}</span></div>`;
      }
      return `<label class="director-approval-row" data-approval-pair data-previous-shot-id="${escapeHtml(previous.shot_id || "")}" data-next-shot-id="${escapeHtml(shot.shot_id || "")}">
        <input data-approval-toggle type="checkbox" ${approval ? "checked" : ""}>
        <span><b>${String(index + 1).padStart(2, "0")} → ${String(index + 2).padStart(2, "0")}</b>批准连续观察</span>
        <input data-approval-reason type="text" placeholder="填写连续观察理由" value="${escapeHtml(approval?.reason || "")}" ${approval ? "required" : "disabled"}>
      </label>`;
    }).join("")}</div>
    ${directorRecutNotice(shots, draft)}
  </details>`;
}

// §48.3：系统放行的组内非相邻构图回切，只读展示（与连续观察审批同处）。
function directorRecutNotice(shots, draft) {
  const recuts = Array.isArray(draft?.composition_recut_approvals)
    ? draft.composition_recut_approvals
    : [];
  if (!recuts.length) return "";
  const position = (shotId) => {
    const index = shots.findIndex((shot) => String(shot.shot_id) === String(shotId));
    return index < 0 ? "??" : String(index + 1).padStart(2, "0");
  };
  return `<div class="director-recut-readonly">${recuts.map((item) =>
    `<div class="director-approval-readonly"><strong>${position(item.previous_shot_id)} ↩ ${position(item.next_shot_id)}</strong><span>系统回切放行：${escapeHtml(item.reason || "")}</span></div>`
  ).join("")}</div>`;
}

function readDirectorDraftForm(draft, sourceCandidateId = null) {
  const form = $("#directorDraftForm");
  if (!form || !form.reportValidity()) {
    throw new Error("请完整填写导演草稿必填字段。");
  }
  const shots = $$('[data-director-shot]', form).map((row, index) => {
    const field = (name) => $(`[data-shot-field="${name}"]`, row)?.value.trim() || "";
    const sentenceIds = $$('[data-shot-sentence]:checked', row).map((input) => input.value);
    if (!sentenceIds.length) {
      throw new Error(`分镜 ${String(index + 1).padStart(2, "0")} 至少关联一条真实旁白。`);
    }
    return {
      shot_id: row.dataset.shotId,
      order: index + 1,
      scene: field("scene"),
      shot_size: field("shot_size"),
      camera_viewpoint: field("camera_viewpoint"),
      subject_action_outcome: field("subject_action_outcome"),
      narrative_function: field("narrative_function"),
      description: field("description"),
      sentence_ids: sentenceIds,
    };
  });
  if (!shots.length || shots.length > MAX_DIRECTOR_SHOTS) {
    throw new Error(`导演草稿必须包含 1 至 ${MAX_DIRECTOR_SHOTS} 个分镜。`);
  }
  const continuousObservationApprovals = $$('[data-approval-pair]', form)
    .filter((row) => $("[data-approval-toggle]", row)?.checked)
    .map((row) => ({
      previous_shot_id: row.dataset.previousShotId,
      next_shot_id: row.dataset.nextShotId,
      reason: $("[data-approval-reason]", row)?.value.trim() || "",
    }));
  if (continuousObservationApprovals.some((item) => !item.reason)) {
    throw new Error("启用连续观察例外时必须填写审批理由。");
  }
  return {
    shots,
    expected_draft_revision_id: draft.revision_id,
    source_candidate_id: sourceCandidateId,
    continuous_observation_approvals: continuousObservationApprovals,
  };
}

function bindDirectorDraftEditor(root = document) {
  $$('[data-approval-toggle]', root).forEach((toggle) => {
    toggle.addEventListener("change", () => {
      const row = toggle.closest("[data-approval-pair]");
      const reason = $("[data-approval-reason]", row);
      reason.disabled = !toggle.checked;
      reason.required = toggle.checked;
      if (!toggle.checked) reason.value = "";
    });
  });
}

function newDirectorShotId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (character) => {
    const value = Math.floor(Math.random() * 16);
    return (character === "x" ? value : (value & 3) | 8).toString(16);
  });
}

function directorApprovalState(form) {
  return $$('[data-approval-pair]', form)
    .filter((row) => $("[data-approval-toggle]", row)?.checked)
    .map((row) => ({
      previous_shot_id: row.dataset.previousShotId,
      next_shot_id: row.dataset.nextShotId,
      reason: $("[data-approval-reason]", row)?.value.trim() || "",
    }));
}

function refreshDirectorShotControls(form, approvals = directorApprovalState(form)) {
  const rows = $$('[data-director-shot]', form);
  rows.forEach((row, index) => {
    const article = row.closest(".storyboard-shot-row");
    const number = $("[data-shot-number]", article);
    if (number) number.textContent = String(index + 1).padStart(2, "0");
    const up = $('[data-shot-move="up"]', row);
    const down = $('[data-shot-move="down"]', row);
    const remove = $("[data-shot-delete]", row);
    if (up) up.disabled = index === 0;
    if (down) down.disabled = index === rows.length - 1;
    if (remove) remove.disabled = rows.length <= 1;
  });
  const add = $("#addDirectorShot");
  if (add) add.disabled = rows.length >= MAX_DIRECTOR_SHOTS;
  $(".director-approval-panel", form)?.remove();
  const shotIds = rows.map((row) => ({ shot_id: row.dataset.shotId }));
  form.insertAdjacentHTML("beforeend", directorApprovalEditor(
    shotIds,
    { continuous_observation_approvals: approvals },
    true,
  ));
  bindDirectorDraftEditor(form);
  bindAutosizeTextareas(form);
}

function bindDirectorShotControls(groupSentences) {
  const form = $("#directorDraftForm");
  const list = $(".group-shot-list", form);
  if (!form || !list) return;
  refreshDirectorShotControls(form);
  form.addEventListener("click", (event) => {
    const add = event.target.closest("#addDirectorShot");
    const move = event.target.closest("[data-shot-move]");
    const remove = event.target.closest("[data-shot-delete]");
    if (!add && !move && !remove) return;
    event.preventDefault();
    const approvals = directorApprovalState(form);
    if (add) {
      const count = $$('[data-director-shot]', form).length;
      if (count >= MAX_DIRECTOR_SHOTS) return;
      $(".empty-state", list)?.remove();
      list.insertAdjacentHTML("beforeend", storyboardShotRow({ shot_id: newDirectorShotId() }, count, true, groupSentences));
    } else {
      const article = event.target.closest(".storyboard-shot-row");
      if (!article) return;
      if (remove) {
        if ($$('[data-director-shot]', form).length <= 1) return;
        article.remove();
      } else if (move.dataset.shotMove === "up" && article.previousElementSibling) {
        article.previousElementSibling.before(article);
      } else if (move.dataset.shotMove === "down" && article.nextElementSibling) {
        article.nextElementSibling.after(article);
      }
    }
    refreshDirectorShotControls(form, approvals);
  });
}

// 弹窗焦点管理：打开时焦点移入弹窗内首个可聚焦元素，返回还原函数（关闭后焦点回到触发元素）。
function manageDialogFocus(overlay) {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  overlay.querySelector("input, textarea, select, button")?.focus();
  return () => {
    if (previous?.isConnected) previous.focus();
  };
}

// 站内动作弹窗（替代 window.prompt/confirm）：resolve { confirmed, value }；
// ESC/遮罩/× 统一走批次 A 的 closeOverlay 注册；setup 负责输入绑定与确认按钮门控。
function actionModal({ title, eyebrow = "CONFIRM ACTION", bodyHtml = "", confirmText = "确认", danger = false, setup = null }) {
  return new Promise((resolve) => {
    $("#actionModalOverlay")?.remove();
    app.insertAdjacentHTML("beforeend", `<div id="actionModalOverlay" class="modal-backdrop">
      <section class="modal" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
        <div class="modal-head"><div><span class="eyebrow">${escapeHtml(eyebrow)}</span><h2>${escapeHtml(title)}</h2></div><button id="closeActionModal" class="modal-close" type="button">×</button></div>
        ${bodyHtml}
        <div class="modal-actions"><button id="cancelActionModal" class="ghost-button" type="button">取消</button><button id="submitActionModal" class="${danger ? "danger-button" : "primary-button"}" type="button">${escapeHtml(confirmText)}</button></div>
      </section>
    </div>`);
    const overlay = $("#actionModalOverlay");
    const restoreFocus = manageDialogFocus(overlay);
    let value = null;
    let settled = false;
    const close = () => {
      overlay.remove();
      restoreFocus();
    };
    const finish = (confirmed) => {
      if (settled) return;
      settled = true;
      close();
      resolve({ confirmed, value });
    };
    overlay.closeOverlay = () => finish(false);
    $("#closeActionModal").addEventListener("click", () => finish(false));
    $("#cancelActionModal").addEventListener("click", () => finish(false));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) finish(false);
    });
    $("#submitActionModal").addEventListener("click", () => finish(true));
    if (setup) setup({ submit: $("#submitActionModal"), setValue: (next) => { value = next; } });
  });
}

// 顶栏「流程说明」：阶段清单直接映射 STAGES 常量，不另抄字符串；附 AUTO 项目差异说明。
function openFlowHelpModal() {
  $("#flowHelpOverlay")?.remove();
  const rows = STAGES.map((stage, index) => `<div class="sentence-row sentence-row-flow"><span class="sentence-index">${String(index + 1).padStart(2, "0")}</span><span class="sentence-text"><strong>${escapeHtml(stage.label)}</strong>　${escapeHtml(stage.note)}</span></div>`).join("");
  app.insertAdjacentHTML("beforeend", `<div id="flowHelpOverlay" class="modal-backdrop">
    <section class="modal" role="dialog" aria-modal="true" aria-label="流程说明">
      <div class="modal-head"><div><span class="eyebrow">WORKFLOW</span><h2>流程说明</h2></div><button id="closeFlowHelp" class="modal-close" type="button">×</button></div>
      <p>生产严格按以下阶段顺序推进，所有外部 AI 调用均需明确动作触发；页面只展示后端权威状态，不伪造进度。</p>
      <div class="sentence-list mt-10">${rows}</div>
      <p class="mt-10"><small>2026-09-15 起新建的 AUTO 项目在九阶段前另有「自动生产」页签：一次提交授权全链路自动执行，未知态与停滞按规则自动对账重提；既有手动项目流程不变。</small></p>
    </section>
  </div>`);
  const restoreFocus = manageDialogFocus($("#flowHelpOverlay"));
  const close = () => {
    $("#flowHelpOverlay")?.remove();
    restoreFocus();
  };
  $("#flowHelpOverlay").closeOverlay = close;
  $("#closeFlowHelp").addEventListener("click", close);
  $("#flowHelpOverlay").addEventListener("click", (event) => {
    if (event.target.id === "flowHelpOverlay") close();
  });
}

function openDirectorManualResolution(run) {
  $("#directorResolutionOverlay")?.remove();
  app.insertAdjacentHTML("beforeend", `<div id="directorResolutionOverlay" class="modal-backdrop">
    <section class="modal" role="dialog" aria-modal="true" aria-label="导演任务人工证据处置">
      <div class="modal-head"><div><span class="eyebrow">MANUAL EVIDENCE</span><h2>处置未知导演任务</h2></div><button id="closeDirectorResolution" class="modal-close" type="button">×</button></div>
      <div class="notice-card amber"><strong>仅依据外部证据处置</strong><p>确认请求未被 Provider 接受后才可能开放手动重提；确认已接受但失败会终态化且不开放重提。</p></div>
      <form id="directorResolutionForm">
        <label class="field"><span>处置结论</span><select name="resolution" required><option value="CONFIRMED_NOT_ACCEPTED">确认请求未被 Provider 接受</option><option value="CONFIRMED_FAILED">确认 Provider 已接受但任务失败</option></select></label>
        <label class="field"><span>外部证据</span><textarea name="external_evidence" rows="5" maxlength="2000" placeholder="填写 Provider 控制台状态、查询时间和可复核说明" required></textarea></label>
        <div class="modal-actions"><button id="cancelDirectorResolution" class="ghost-button" type="button">取消</button><button class="primary-button" type="submit">确认处置</button></div>
      </form>
    </section>
  </div>`);
  const restoreFocus = manageDialogFocus($("#directorResolutionOverlay"));
  const close = () => {
    $("#directorResolutionOverlay")?.remove();
    restoreFocus();
  };
  $("#directorResolutionOverlay").closeOverlay = close;
  $("#closeDirectorResolution").addEventListener("click", close);
  $("#cancelDirectorResolution").addEventListener("click", close);
  $("#directorResolutionOverlay").addEventListener("click", (event) => {
    if (event.target.id === "directorResolutionOverlay") close();
  });
  $("#directorResolutionForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    try {
      setBusy(true);
      const resolution = values.get("resolution");
      await api.resolveDirectorPreplan(
        state.project.id,
        run.run_id,
        resolution,
        values.get("external_evidence"),
      );
      close();
      toast(resolution === "CONFIRMED_NOT_ACCEPTED"
        ? "已确认 Provider 未受理；后端允许时可手动重新处理。"
        : "已确认 Provider 接受后失败；该任务已终态化且不允许重提。");
      await refreshProjectFacts();
      renderProjectWorkspace();
    } catch (error) {
      toast(messageOf(error), true);
    } finally {
      setBusy(false);
    }
  });
}

const STORYBOARD_BATCH_OUTCOME_TEXT = {
  queued: "已入队",
  skipped_succeeded: "跳过：已有成功故事板",
  already_running: "跳过：已有在途任务",
  needs_review: "未提交：最终导演需人工修正",
  no_director: "未提交：无可用导演方案",
  unresolved: "未提交：存在未决远端任务，需先对账",
  error: "失败",
};

async function runBatchDirectorSave() {
  if (autoBusy()) {
    toast(autoBusyReason(), true);
    return;
  }
  const button = $("#batchSaveDirectors");
  if (button) button.disabled = true;
  try {
    setBusy(true);
    const result = await api.storyboardBatchSaveDirectors(state.project.id);
    const items = Array.isArray(result?.results) ? result.results : [];
    const count = (fn) => items.filter(fn).length;
    const seeded = count((i) => i.outcome === "seeded" && i.review_state !== "REQUIRED");
    const review = count((i) => i.outcome === "seeded" && i.review_state === "REQUIRED");
    const existing = count((i) => i.outcome === "already_draft" || i.outcome === "already_applied");
    const failed = count((i) => i.outcome === "error" || i.outcome === "no_candidate");
    toast(
      `批量保存完成：新播种 ${seeded} 组，已存在 ${existing} 组，需人工修正 ${review} 组${failed ? `，失败 ${failed} 组` : ""}。`,
      review + failed > 0,
    );
    await refreshProjectFacts();
    renderProjectWorkspace();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
    if (button) button.disabled = false;
  }
}

async function openStoryboardBatchModal() {
  if (autoBusy()) {
    toast(autoBusyReason(), true);
    return;
  }
  $("#storyboardBatchOverlay")?.remove();
  let items = [];
  try {
    const overview = await api.storyboardBatchOverview(state.project.id);
    items = Array.isArray(overview?.items) ? overview.items : [];
  } catch (error) {
    toast(messageOf(error), true);
    return;
  }
  const row = (item) => {
    const seq = String(item.group_sequence).padStart(2, "0");
    const labels = [];
    if (item.storyboard_status === "SUCCEEDED") labels.push(`已生成 v${item.storyboard_version}`);
    else if (item.storyboard_status) labels.push(`状态 ${escapeHtml(item.storyboard_status)}`);
    else labels.push("未生成");
    if (item.draft_review_state === "REQUIRED") labels.push("导演需人工修正");
    const inFlight = item.storyboard_status
      && !["SUCCEEDED", "FAILED"].includes(item.storyboard_status);
    const disabled = !item.can_generate || inFlight;
    return `<label class="check batch-check">
      <input type="checkbox" data-batch-group value="${escapeHtml(item.group_id)}" ${disabled ? "disabled" : "checked"} />
      <span>分组 ${seq}</span><small>${labels.join(" · ")}</small>
    </label>`;
  };
  app.insertAdjacentHTML("beforeend", `<div id="storyboardBatchOverlay" class="modal-backdrop">
    <section class="modal" role="dialog" aria-modal="true" aria-label="批量生成故事板">
      <div class="modal-head"><div><span class="eyebrow">BATCH STORYBOARD</span><h2>批量生成故事板</h2></div><button id="closeStoryboardBatch" class="modal-close" type="button">×</button></div>
      <p>每个勾选分组都会真实调用一次图片 Provider；已有在途任务或导演未通过校验的分组不可勾选。</p>
      <div id="batchGroupList" class="batch-list">${items.map(row).join("") || "<p>暂无可生成的分组。</p>"}</div>
      <label class="check"><input id="batchForceRegenerate" type="checkbox" /> 强制重新生图：勾选后已成功的分组也会生成新版本（旧版本保留）</label>
      <div id="batchResultList"></div>
      <div class="modal-actions"><button id="cancelStoryboardBatch" class="ghost-button" type="button">取消</button><button id="submitStoryboardBatch" class="primary-button" type="button">确认批量生成（<span id="batchSelectedCount">0</span> 组）</button></div>
    </section>
  </div>`);
  const restoreFocus = manageDialogFocus($("#storyboardBatchOverlay"));
  const close = () => {
    $("#storyboardBatchOverlay")?.remove();
    restoreFocus();
  };
  $("#storyboardBatchOverlay").closeOverlay = close;
  $("#closeStoryboardBatch").addEventListener("click", close);
  $("#cancelStoryboardBatch").addEventListener("click", close);
  $("#storyboardBatchOverlay").addEventListener("click", (event) => {
    if (event.target.id === "storyboardBatchOverlay") close();
  });
  const updateCount = () => {
    $("#batchSelectedCount").textContent = String($$("[data-batch-group]:checked").length);
  };
  $$("[data-batch-group]").forEach((input) => input.addEventListener("change", updateCount));
  updateCount();
  $("#submitStoryboardBatch").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const groupIds = $$("[data-batch-group]:checked").map((input) => input.value);
    if (!groupIds.length) {
      toast("请至少勾选一个分组。", true);
      return;
    }
    button.disabled = true;
    try {
      setBusy(true);
      const result = await api.storyboardBatchGenerate(
        state.project.id,
        groupIds,
        $("#batchForceRegenerate").checked,
      );
      const results = Array.isArray(result?.results) ? result.results : [];
      const queued = results.filter((i) => i.outcome === "queued").length;
      $("#batchResultList").innerHTML = `<div class="notice-card mt-10"><strong>已提交 ${queued} 组</strong></div>`
        + results.map((item) => `<div class="batch-result-row"><span>分组 ${String(item.group_sequence).padStart(2, "0")}</span> <small>${escapeHtml(STORYBOARD_BATCH_OUTCOME_TEXT[item.outcome] || item.outcome)}${item.message ? `：${escapeHtml(item.message)}` : ""}</small></div>`).join("");
      $("#batchGroupList").style.display = "none";
      await refreshProjectFacts();
      renderProjectWorkspace();
    } catch (error) {
      toast(messageOf(error), true);
      button.disabled = false;
    } finally {
      setBusy(false);
    }
  });
}

const VIDEO_BATCH_OUTCOME_TEXT = {
  queued: "已提交排队",
  video_blocked: "未提交：该组已阻止视频生成",
  single_sentence_exceeds_15000ms: "未提交：单句配音超过 15 秒已阻止",
  has_current_video: "跳过：已有当前视频",
  no_current_storyboard: "未提交：无当前故事板",
  already_running: "跳过：已有在途任务",
  remote_acceptance_unknown: "未提交：远端状态未知，需先对账",
  error: "失败",
};

// 在途组视频任务阶段事实由 step07-view.js 的 VIDEO_ATTEMPT_IN_FLIGHT 承载（供批量进度与 tab 注解共用）。

function videoBatchReasonText(item) {
  if (item.video_blocked) return VIDEO_BATCH_OUTCOME_TEXT.single_sentence_exceeds_15000ms;
  return VIDEO_BATCH_OUTCOME_TEXT[item.reason] || item.reason || "";
}

async function openVideoBatchModal() {
  if (autoBusy()) {
    toast(autoBusyReason(), true);
    return;
  }
  $("#videoBatchOverlay")?.remove();
  let items = [];
  try {
    const overview = await api.videoBatchOverview(state.project.id);
    items = Array.isArray(overview?.items) ? overview.items : [];
  } catch (error) {
    toast(messageOf(error), true);
    return;
  }
  const row = (item) => {
    const seq = String(item.group_sequence).padStart(2, "0");
    const labels = [];
    if (item.has_current_video) labels.push("已有当前视频");
    if (item.latest_attempt_stage) labels.push(`最近任务 ${escapeHtml(item.latest_attempt_stage)}`);
    if (item.latest_attempt_stalled) labels.push("远端疑似卡死");
    const reason = videoBatchReasonText(item);
    if (reason) labels.push(escapeHtml(reason));
    return `<label class="check batch-check">
      <input type="checkbox" data-video-batch-group value="${escapeHtml(item.group_id)}" ${item.eligible ? "checked" : "disabled"} />
      <span>分组 ${seq}</span><small>${labels.join(" · ")}</small>
    </label>`;
  };
  app.insertAdjacentHTML("beforeend", `<div id="videoBatchOverlay" class="modal-backdrop">
    <section class="modal" role="dialog" aria-modal="true" aria-label="批量生成组级视频">
      <div class="modal-head"><div><span class="eyebrow">BATCH H3 VIDEO</span><h2>批量生成组级视频</h2></div><button id="closeVideoBatch" class="modal-close" type="button">×</button></div>
      <p>每个勾选分组都会真实创建一个 H3 任务；执行并发由节点槽位调度决定（当前 3 台节点）。已阻止、在途或远端未知态的分组不可勾选。</p>
      <div id="videoBatchGroupList" class="batch-list">${items.map(row).join("") || "<p>暂无可生成的分组。</p>"}</div>
      <label class="check"><input id="videoBatchForce" type="checkbox" /> 强制重新生成：勾选后已有当前视频的分组也会生成新版本（旧版本保留）</label>
      <div id="videoBatchProgress"></div>
      <div id="videoBatchResultList"></div>
      <div class="modal-actions"><button id="cancelVideoBatch" class="ghost-button" type="button">取消</button><button id="submitVideoBatch" class="primary-button" type="button">确认批量生成（<span id="videoBatchSelectedCount">0</span> 组）</button></div>
    </section>
  </div>`);
  let progressTimer = null;
  const restoreFocus = manageDialogFocus($("#videoBatchOverlay"));
  const close = () => {
    if (progressTimer) clearInterval(progressTimer);
    $("#videoBatchOverlay")?.remove();
    restoreFocus();
  };
  $("#videoBatchOverlay").closeOverlay = close;
  $("#closeVideoBatch").addEventListener("click", close);
  $("#cancelVideoBatch").addEventListener("click", close);
  $("#videoBatchOverlay").addEventListener("click", (event) => {
    if (event.target.id === "videoBatchOverlay") close();
  });
  const updateCount = () => {
    $("#videoBatchSelectedCount").textContent = String($$("[data-video-batch-group]:checked").length);
  };
  $$("[data-video-batch-group]").forEach((input) => input.addEventListener("change", updateCount));
  updateCount();
  $("#submitVideoBatch").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const groupIds = $$("[data-video-batch-group]:checked").map((input) => input.value);
    if (!groupIds.length) {
      toast("请至少勾选一个分组。", true);
      return;
    }
    button.disabled = true;
    try {
      setBusy(true);
      const job = await api.videoBatchGenerate(
        state.project.id,
        groupIds,
        $("#videoBatchForce").checked,
      );
      if (!job?.job_id) throw new Error("后端未返回批量任务 job_id。");
      $("#videoBatchGroupList").style.display = "none";
      const renderJob = (view) => {
        const items = Array.isArray(view?.items) ? view.items : [];
        const queued = items.filter((i) => i.status === "queued").length;
        const statusText = (item) => item.status === "queued"
          ? VIDEO_BATCH_OUTCOME_TEXT.queued
          : item.status === "PENDING"
            ? "等待创建意图"
            : (VIDEO_BATCH_OUTCOME_TEXT[item.status] || item.status);
        $("#videoBatchProgress").innerHTML = `<div class="notice-card mt-10"><strong>已提交意图 ${queued} · 共 ${view.total} 组${view.completed ? " · 批次创建完成" : " · 创建中…"}</strong></div>`;
        $("#videoBatchResultList").innerHTML = items.map((item) => `<div class="batch-result-row"><span>分组 ${String(item.group_sequence).padStart(2, "0")}</span> <small>${escapeHtml(statusText(item))}${item.message ? `：${escapeHtml(item.message)}` : ""}</small></div>`).join("");
      };
      renderJob(job);
      progressTimer = setInterval(async () => {
        try {
          const latest = await api.videoBatchJob(state.project.id, job.job_id);
          renderJob(latest);
          if (latest?.completed && progressTimer) {
            clearInterval(progressTimer);
            progressTimer = null;
            await refreshProjectFacts();
            renderProjectWorkspace();
          }
        } catch { /* 进度刷新失败不阻断；页面刷新仍有权威状态 */ }
      }, 3000);
      await refreshProjectFacts();
      renderProjectWorkspace();
    } catch (error) {
      toast(messageOf(error), true);
      button.disabled = false;
    } finally {
      setBusy(false);
    }
  });
}

function directorStatusStripMarkup(run, materialization, extraChips = "") {
  return `<div class="studio-status-strip">${directorAutoRefreshMarkup(run, materialization)}${extraChips}</div>`;
}

function autosizeTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight + 2}px`;
}

function bindAutosizeTextareas(root = document) {
  $$("textarea[data-autosize]", root).forEach(autosizeTextarea);
  $$("details", root).forEach((panel) => {
    if (panel.dataset.autosizeBound) return;
    panel.dataset.autosizeBound = "1";
    panel.addEventListener("toggle", () => {
      $$("textarea[data-autosize]", panel).forEach(autosizeTextarea);
    });
  });
}

function bindActionMenu(menu) {
  if (!menu) return;
  menu.addEventListener("click", (event) => {
    if (event.target.closest(".action-menu-list button")) menu.open = false;
  });
}

// ——— 故事板大图查看器（Lightbox）———
// 只读浏览：可查看当前组与历史版本图片；当前版本切换仍在“故事板历史”抽屉完成。
const storyboardLightboxState = {
  groupIndex: 0,
  versions: [],
  scale: 1,
  translateX: 0,
  translateY: 0,
  fit: true,
};

function storyboardLightboxMarkup() {
  return `<div id="storyboardLightbox" class="lightbox-backdrop">
    <section class="lightbox" role="dialog" aria-modal="true" aria-label="故事板大图查看器">
      <div class="lightbox-head">
        <div class="lightbox-title"><strong id="lightboxTitle">故事板</strong><small id="lightboxSubtitle"></small></div>
        <div class="lightbox-tools">
          <select id="lightboxVersion" class="lightbox-version" aria-label="选择要查看的故事板版本"></select>
          <button id="lightboxZoomFit" class="ghost-button compact" type="button">适应屏幕</button>
          <button id="lightboxZoomFull" class="ghost-button compact" type="button">100%</button>
          <button id="lightboxPrevGroup" class="ghost-button compact" type="button" title="上一组（←）">◀ 上一组</button>
          <button id="lightboxNextGroup" class="ghost-button compact" type="button" title="下一组（→）">下一组 ▶</button>
          <button id="closeLightbox" class="modal-close" type="button" aria-label="关闭">×</button>
        </div>
      </div>
      <div id="lightboxStage" class="lightbox-stage"><img id="lightboxImage" alt="故事板大图" /></div>
      <div class="lightbox-foot"><small>滚轮缩放 · 拖动平移 · ← / → 切换分组 · Esc 关闭；版本下拉仅用于查看，切换当前版本请走「故事板历史」</small></div>
    </section>
  </div>`;
}

function closeStoryboardLightbox() {
  $("#storyboardLightbox")?.remove();
}

function lightboxApplyTransform() {
  const img = $("#lightboxImage");
  if (!img) return;
  if (storyboardLightboxState.fit) {
    img.classList.add("is-fit");
    img.style.transform = "";
    return;
  }
  img.classList.remove("is-fit");
  img.style.transform = `translate(${storyboardLightboxState.translateX}px, ${storyboardLightboxState.translateY}px) scale(${storyboardLightboxState.scale})`;
}

function lightboxSetZoom(fit, scale = 1) {
  storyboardLightboxState.fit = fit;
  storyboardLightboxState.scale = scale;
  storyboardLightboxState.translateX = 0;
  storyboardLightboxState.translateY = 0;
  lightboxApplyTransform();
}

function lightboxShowGeneration(generationId, caption) {
  const img = $("#lightboxImage");
  if (!img) return;
  const subtitle = $("#lightboxSubtitle");
  if (subtitle) subtitle.textContent = caption;
  const select = $("#lightboxVersion");
  if (select && String(select.value) !== String(generationId)) select.value = String(generationId);
  setNativeMediaSource(img, api.storyImageUrl(generationId), "故事板图片");
  lightboxSetZoom(true);
}

async function lightboxLoadGroup(index) {
  const total = state.groups.length;
  if (!total) return;
  const nextIndex = Math.min(total - 1, Math.max(0, index));
  storyboardLightboxState.groupIndex = nextIndex;
  const group = state.groups[nextIndex];
  const title = $("#lightboxTitle");
  if (title) title.textContent = `分组 ${String(nextIndex + 1).padStart(2, "0")} / 共 ${total} 组`;
  const previous = $("#lightboxPrevGroup");
  const next = $("#lightboxNextGroup");
  if (previous) previous.disabled = nextIndex <= 0;
  if (next) next.disabled = nextIndex >= total - 1;
  const subtitle = $("#lightboxSubtitle");
  if (subtitle) subtitle.textContent = "加载中…";
  const [currentResult, historyResult] = await Promise.all([
    api.optional(() => api.storyboard(groupId(group))),
    api.optional(() => api.storyboards(groupId(group))),
  ]);
  if (!$("#storyboardLightbox") || storyboardLightboxState.groupIndex !== nextIndex) return;
  const current = currentResult.data;
  const versions = historyItems(historyResult.data);
  storyboardLightboxState.versions = versions;
  const select = $("#lightboxVersion");
  if (select) {
    select.innerHTML = versions.map((item, itemIndex) => `<option value="${escapeHtml(item.generation_id)}">版本 ${escapeHtml(item.version ?? itemIndex + 1)}${historyCurrent(item) ? " · 当前" : ""}</option>`).join("");
    select.disabled = !versions.length;
  }
  const fallback = versions.find((item) => item.asset && item.generation_id);
  const chosen = current?.asset && current.public_status === "SUCCEEDED" && current.generation_id
    ? { generationId: current.generation_id, caption: `版本 ${current.version} · 当前版本` }
    : fallback
      ? { generationId: fallback.generation_id, caption: `版本 ${fallback.version ?? ""} · ${statusText(fallback)}` }
      : null;
  if (!chosen) {
    if (subtitle) subtitle.textContent = "该组尚无可用故事板图片";
    $("#lightboxImage")?.removeAttribute("src");
    return;
  }
  lightboxShowGeneration(chosen.generationId, chosen.caption);
}

function lightboxStepGroup(delta) {
  lightboxLoadGroup(storyboardLightboxState.groupIndex + delta)
    .catch((error) => toast(messageOf(error), true));
}

async function openStoryboardLightbox(targetGroupId = null) {
  if (!state.groups.length) return;
  $("#storyboardLightbox")?.remove();
  app.insertAdjacentHTML("beforeend", storyboardLightboxMarkup());
  $("#closeLightbox").addEventListener("click", closeStoryboardLightbox);
  $("#storyboardLightbox").addEventListener("click", (event) => {
    if (event.target.id === "storyboardLightbox") closeStoryboardLightbox();
  });
  $("#lightboxPrevGroup").addEventListener("click", () => lightboxStepGroup(-1));
  $("#lightboxNextGroup").addEventListener("click", () => lightboxStepGroup(1));
  $("#lightboxVersion").addEventListener("change", (event) => {
    const item = storyboardLightboxState.versions
      .find((entry) => String(entry.generation_id) === String(event.target.value));
    if (item) {
      lightboxShowGeneration(
        item.generation_id,
        `版本 ${item.version ?? ""} · ${statusText(item)}${historyCurrent(item) ? " · 当前版本" : ""}`,
      );
    }
  });
  $("#lightboxZoomFit").addEventListener("click", () => lightboxSetZoom(true));
  $("#lightboxZoomFull").addEventListener("click", () => lightboxSetZoom(false, 1));
  const stage = $("#lightboxStage");
  stage.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
    storyboardLightboxState.fit = false;
    storyboardLightboxState.scale = Math.min(6, Math.max(0.2, storyboardLightboxState.scale * factor));
    lightboxApplyTransform();
  }, { passive: false });
  let dragOrigin = null;
  stage.addEventListener("pointerdown", (event) => {
    if (storyboardLightboxState.fit) return;
    dragOrigin = {
      x: event.clientX - storyboardLightboxState.translateX,
      y: event.clientY - storyboardLightboxState.translateY,
    };
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener("pointermove", (event) => {
    if (!dragOrigin) return;
    storyboardLightboxState.translateX = event.clientX - dragOrigin.x;
    storyboardLightboxState.translateY = event.clientY - dragOrigin.y;
    lightboxApplyTransform();
  });
  const endDrag = () => { dragOrigin = null; };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);
  const startIndex = Math.max(0, state.groups
    .findIndex((group) => groupId(group) === (targetGroupId || state.activeGroupId)));
  await lightboxLoadGroup(startIndex);
}

function renderDirectorConflicts(title, conflicts) {
  const items = Array.isArray(conflicts) ? conflicts : [];
  if (!items.length) return "";
  return `<div class="notice-card amber conflict-card"><strong>${escapeHtml(title)} · 共 ${items.length} 项，需人工处理</strong><div class="director-conflict-list">${items.map((conflict) => {
    const shotId = conflictShotId(conflict);
    return `<p><button class="conflict-link" type="button" data-conflict-shot="${escapeHtml(shotId)}" ${shotId ? "" : "disabled"}>${escapeHtml(conflictSummary(conflict))}</button></p>`;
  }).join("")}</div><details class="conflict-tech"><summary>技术详情（原始冲突码与 ID）</summary>${items.map((conflict) => `<p>${escapeHtml(conflictText(conflict))}</p>`).join("")}</details></div>`;
}

function bindDirectorConflictNavigation(root = document) {
  $$("[data-conflict-shot]", root).forEach((button) => {
    button.addEventListener("click", () => {
      const shotId = button.dataset.conflictShot;
      if (!shotId) return;
      const row = $(`[data-shot-id="${CSS.escape(shotId)}"]`);
      if (!row) {
        toast("该冲突对应的分镜不在当前分组页面。", true);
        return;
      }
      const article = row.closest(".storyboard-shot-row") || row;
      article.scrollIntoView({ behavior: "smooth", block: "center" });
      article.classList.add("conflict-flash");
      window.setTimeout(() => article.classList.remove("conflict-flash"), 1600);
    });
  });
}

function preplanRunContent(run) {
  if (!run) {
    return renderUnavailable("前置导演任务尚未建档", "确认配音后，后端会在同一确认流程中自动创建前置导演任务；页面不会补发自动请求。");
  }
  const total = Math.max(0, Number(run.total_units || 0));
  const completed = Math.max(0, Number(run.completed_units || 0));
  const percentage = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  const failedOrdinal = run.failed_unit_ordinal == null ? "—" : String(Number(run.failed_unit_ordinal));
  const failedKindLabels = {
    GLOBAL_PLAN: "全局规划",
    SHOT_PLAN: "镜头单元",
    NONVISUAL_SEPARATOR: "分隔句",
  };
  const failedKind = failedKindLabels[run.failed_unit_kind] || null;
  const retryPending = Math.max(0, Number(run.retry_pending_units || 0));
  const retryBudget = `${Number(run.auto_retries_used || 0)}/${Number(run.auto_retry_ceiling || 0)}`;
  const failureLine = run.status === "FAILED" && run.failure_code
    ? `<p class="preplan-failure-line">失败码：${escapeHtml(String(run.failure_code))} · 批次 ${escapeHtml(failedOrdinal)}（${escapeHtml(failedKind || "未知类型")}）</p>`
    : "";
  const retryLine = run.status === "RUNNING" && retryPending > 0
    ? `<p class="preplan-retry-line">自动重试中 ${retryPending} 个批次 · 已用重试 ${escapeHtml(retryBudget)}</p>`
    : "";
  const statusGrid = `<div class="real-status-grid">
      <article><span>处理进度</span><strong>${completed} / ${total || "—"}</strong><small>镜头单元批次</small></article>
      <article><span>在途并行</span><strong>${compactCount(run.inflight_units)}</strong><small>仅后端工作流提交</small></article>
      <article><span>自动重试中</span><strong>${compactCount(retryPending)}</strong><small>已用 ${escapeHtml(retryBudget)}</small></article>
      <article><span>失败批次</span><strong>${escapeHtml(failedOrdinal)}</strong><small>${escapeHtml(failedKind || "未出现失败批次")}</small></article>
      <article><span>尚未提交</span><strong>${compactCount(run.unsent_units)}</strong><small>不会自动重提未知任务</small></article>
    </div>`;
  if (preplanRunIsCalm(run)) {
    const calmMessage = run.message ? `<p class="preplan-calm-message">${escapeHtml(run.message)}</p>` : "";
    return `<div class="preplan-calm-line"><strong>✓ ${escapeHtml(preplanRunCalmSummary(run))}</strong><details class="preplan-calm-detail"><summary>运行详情</summary>${calmMessage}${statusGrid}</details></div>`;
  }
  return `${attemptStatusNotice("项目级前置导演", run)}${failureLine}${retryLine}
    ${statusGrid}
    <div class="preplan-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percentage}"><i style="width:${percentage}%"></i></div>`;
}

function preplanRunMarkup(run) {
  return `<div id="directorPreplanRunRegion">${preplanRunContent(run)}</div>`;
}

function preplanShotRow(shot, index, current) {
  const sentenceMap = new Map((current?.sentences || []).map((sentence) => [String(sentence.sentence_id), sentence]));
  const linked = (shot?.sentence_ids || []).map((id) => sentenceMap.get(String(id)) || { sentence_id: id });
  return `<article class="storyboard-shot-row preplan-shot-row">
    <div class="shot-row-index"><b>${String(Number(current?.offset || 0) + index + 1).padStart(2, "0")}</b><span>预案</span></div>
    <div class="shot-row-main">
      <div class="shot-row-title"><strong>${escapeHtml(shot.scene || `前置分镜 ${Number(current?.offset || 0) + index + 1}`)}</strong><span>无时长 · 冻结文案关联</span></div>
      <div class="shot-row-fields">
        <div class="shot-field"><label>画面描述</label><textarea data-autosize readonly>${escapeHtml(shot.description || "")}</textarea><small>${escapeHtml([shot.shot_size, shot.camera_viewpoint, shot.narrative_function].filter(Boolean).join(" · "))}</small></div>
        <div class="preplan-sentence-links">${linked.map((sentence) => `<p><b>句 ${String(sentence.sequence || "—").padStart(2, "0")}</b>${escapeHtml(sentence.text || `句子 ${sentence.sentence_id}`)}</p>`).join("") || "<p>当前镜头没有冻结句子关联</p>"}</div>
      </div>
      ${shot.subject_action_outcome ? `<div class="shot-field preplan-outcome"><label>主体动作与结果</label><p>${escapeHtml(shot.subject_action_outcome)}</p></div>` : ""}
    </div>
  </article>`;
}

function preplanCurrentMarkup(current) {
  if (!current) {
    return renderUnavailable("最新前置导演方案尚未形成", "任务处理中可刷新或等待页面自动查询；只有完整成功的不可变修订会成为当前最新版。");
  }
  const offset = Math.max(0, Number(current.offset || 0));
  const limit = Math.max(1, Number(current.limit || state.preplanLimit));
  const total = Math.max(0, Number(current.total_shots || 0));
  const shots = Array.isArray(current.shots) ? current.shots : [];
  const start = total ? offset + 1 : 0;
  const end = Math.min(total, offset + shots.length);
  return `<section class="panel preplan-current-panel">
    <div class="panel-head"><div><h2>当前最新前置导演方案</h2><p>项目级无时长预案；数据库保留不可变修订，页面只读取当前指针。</p></div><span class="status-pill purple">${start}–${end} / ${total}</span></div>
    <div class="panel-body">
      ${renderDirectorConflicts("前置质量冲突", current.quality_conflicts)}
      <div class="group-shot-list">${shots.map((shot, index) => preplanShotRow(shot, index, current)).join("") || `<div class="empty-state"><div><b>镜</b><h3>当前页没有分镜</h3><p>请返回上一页或刷新当前最新版。</p></div></div>`}</div>
      <div class="preplan-pagination"><button id="previousPreplanPage" class="ghost-button compact" type="button" ${offset <= 0 ? "disabled" : ""}>上一页</button><span>修订 ${escapeHtml(current.revision_number ?? current.revision_id)} · ${start}–${end} / ${total}</span><button id="nextPreplanPage" class="ghost-button compact" type="button" ${offset + limit >= total ? "disabled" : ""}>下一页</button></div>
    </div>
  </section>`;
}

function materializationCandidate(materialization, group) {
  return (materialization?.candidates || []).find((candidate) => String(candidate.group_id) === String(groupId(group))) || null;
}

function directorPollContextIsCurrent(context) {
  return Boolean(context)
    && context === state.directorPollContext
    && context.generation === state.directorPollGeneration
    && context.renderEpoch === state.renderEpoch
    && state.activeStage === "director_storyboard"
    && state.project?.id === context.projectId;
}

function patchDirectorStageFact() {
  const fact = stageFact("director_storyboard");
  const button = $('[data-stage-key="director_storyboard"]');
  if (button) {
    const status = fact?.status || "blocked";
    button.classList.toggle("is-complete", status === "complete");
    button.classList.toggle("is-blocked", status === "blocked");
    const note = $("small", button);
    const rawReason = fact?.blocking_reason || "";
    const nextNote = blockingReasonLabel(rawReason) || rawReason || STAGES.find((stage) => stage.key === "director_storyboard")?.note || "";
    if (note && note.textContent !== nextNote) note.textContent = nextNote;
    if (note) note.title = rawReason;
  }
  const badge = $(".project-step-footer .workflow-badge");
  if (badge && state.activeStage === "director_storyboard") {
    const nextClass = `workflow-badge ${state.summary ? statusClass(fact) : "is-error"}`;
    const nextText = state.summary ? statusText(fact) : "流程摘要不可用";
    if (badge.className !== nextClass) badge.className = nextClass;
    if (badge.textContent !== nextText) badge.textContent = nextText;
  }
}

function patchDirectorRunControls(fact, run, current) {
  const target = $("#directorRunControls");
  if (!target) return;
  const nextMarkup = directorRunControlButtonsMarkup(fact, run, current);
  if (target.innerHTML === nextMarkup) return;
  target.innerHTML = nextMarkup;
  bindDirectorRunActionControls(run);
}

function patchDirectorMaterializationStatus(materialization) {
  const badge = $(".materialization-panel .workflow-badge");
  if (!badge || !materialization) return;
  const nextClass = `workflow-badge ${statusClass(materialization)}`;
  const nextText = materializationStatusLabel(materialization);
  if (badge.className !== nextClass) badge.className = nextClass;
  if (badge.textContent !== nextText) badge.textContent = nextText;
}

function patchDirectorPollingUi(run, materialization, current) {
  const runRegion = $("#directorPreplanRunRegion");
  const nextRunMarkup = preplanRunContent(run);
  if (runRegion && runRegion.innerHTML !== nextRunMarkup) runRegion.innerHTML = nextRunMarkup;
  patchDirectorRunControls(stageFact("director_storyboard"), run, current);
  patchDirectorMaterializationStatus(materialization);
  patchDirectorStageFact();
  updateDirectorAutoRefreshStatus(run, materialization);
}

function advanceDirectorPollDelay(changed) {
  if (changed) {
    state.directorPollStableCount = 0;
    state.directorPollDelayMs = DIRECTOR_POLL_DELAYS_MS[0];
    return;
  }
  state.directorPollStableCount += 1;
  state.directorPollDelayMs = state.directorPollStableCount >= 3
    ? DIRECTOR_POLL_DELAYS_MS[2]
    : DIRECTOR_POLL_DELAYS_MS[1];
}

async function pollDirectorStatus(context) {
  if (!directorPollContextIsCurrent(context)) return;
  if (state.directorPollInFlight) {
    state.directorPollImmediatePending = true;
    return;
  }
  state.directorPollInFlight = true;
  let nextSchedule = null;
  try {
    const materializationRequest = (context.materialization || state.groups.length)
      ? api.optional(() => api.directorMaterialization(context.projectId))
      : Promise.resolve({ data: null, reason: "narration_not_ready" });
    // 列表视图在轮询批次中追加聚合 overview（仍是 GET-only、单请求在途）；详情视图不追加。
    const overviewRequest = state.directorView === "list"
      ? api.optional(() => api.storyboardBatchOverview(context.projectId))
      : Promise.resolve({ data: null });
    const [runResult, narrationResult, summaryResult, materializationResult, overviewResult] = await Promise.all([
      api.optional(() => api.latestDirectorPreplanRun(context.projectId)),
      api.optional(() => api.narration(context.projectId)),
      api.optional(() => api.summary(context.projectId)),
      materializationRequest,
      overviewRequest,
    ]);
    if (!directorPollContextIsCurrent(context)) return;

    const nextRun = runResult.data;
    const nextNarration = narrationResult.data;
    const nextSummary = summaryResult.data;
    const nextMaterialization = materializationResult.data;
    const nextOverview = state.directorView === "list" ? overviewResult.data : null;
    const previousSnapshot = directorPollSnapshot(
      context.run,
      context.narration,
      context.materialization,
      context.summary,
      context.overview,
    );
    const nextSnapshot = directorPollSnapshot(
      nextRun,
      nextNarration,
      nextMaterialization,
      nextSummary,
      nextOverview,
    );
    const changed = directorPollSnapshotSignature(previousSnapshot) !== directorPollSnapshotSignature(nextSnapshot);
    const structureChanged = directorPollStructureSignature(previousSnapshot) !== directorPollStructureSignature(nextSnapshot);
    const terminalChanged = directorPollReachedTerminal(previousSnapshot, nextSnapshot);

    applyProjectFacts(nextNarration, nextSummary);
    const recoveredFromPollError = state.directorPollErrorCount > 0;
    state.directorPollErrorCount = 0;
    if (structureChanged || terminalChanged) {
      state.directorPollLastUpdatedAt = Date.now();
      // 整渲前先过脏草稿守卫：自动保存失败则不整渲，避免轮询覆盖正在编辑的内容；
      // 按原上下文继续轮询，下个周期重试守卫与整渲。
      if (!(await ensureDirectorDraftSaved())) {
        scheduleDirectorPolling(context.run, context.materialization, context.current, { overview: context.overview || null });
        return;
      }
      renderProjectWorkspace();
      return;
    }
    advanceDirectorPollDelay(changed || recoveredFromPollError);
    if (changed) {
      state.directorPollLastUpdatedAt = Date.now();
      if (state.directorView === "list") {
        // 列表视图不做局部补丁：签名变化（含每组故事板状态/计数）整渲列表一次。
        state.directorStageCache = {
          run: nextRun,
          current: context.current,
          materialization: nextMaterialization,
          materializationReason: materializationResult.reason || "",
        };
        state.renderEpoch += 1;
        await renderDirectorListView({
          run: nextRun,
          current: context.current,
          materialization: nextMaterialization,
          fact: stageFact("director_storyboard"),
          epoch: state.renderEpoch,
          overviewResult,
          materializationReason: materializationResult.reason || "",
        });
        return;
      }
      patchDirectorPollingUi(nextRun, nextMaterialization, context.current);
    } else {
      updateDirectorAutoRefreshStatus(nextRun, nextMaterialization);
    }
    nextSchedule = { run: nextRun, materialization: nextMaterialization, current: context.current, overview: nextOverview };
  } catch (error) {
    if (!directorPollContextIsCurrent(context)) return;
    state.directorPollErrorCount += 1;
    state.directorPollDelayMs = state.directorPollErrorCount > 1
      ? DIRECTOR_POLL_DELAYS_MS[2]
      : DIRECTOR_POLL_DELAYS_MS[1];
    updateDirectorAutoRefreshStatus(
      context.run,
      context.materialization,
      `自动更新暂时失败 · ${Math.round(state.directorPollDelayMs / 1000)} 秒后重试`,
      "error",
    );
    nextSchedule = { run: context.run, materialization: context.materialization, current: context.current, overview: context.overview || null };
  } finally {
    state.directorPollInFlight = false;
    const pendingContext = state.directorPollContext;
    if (!directorPollContextIsCurrent(context)
      && state.directorPollImmediatePending
      && directorPollContextIsCurrent(pendingContext)) {
      state.directorPollImmediatePending = false;
      scheduleDirectorPolling(
        pendingContext.run,
        pendingContext.materialization,
        pendingContext.current,
        { immediate: true, overview: pendingContext.overview || null },
      );
    }
  }

  if (!directorPollContextIsCurrent(context)) return;
  if (!nextSchedule) return;
  const immediate = state.directorPollImmediatePending;
  state.directorPollImmediatePending = false;
  scheduleDirectorPolling(nextSchedule.run, nextSchedule.materialization, nextSchedule.current, { immediate, overview: nextSchedule.overview || null });
}

function scheduleDirectorPolling(run, materialization = null, current = null, { immediate = false, overview = null } = {}) {
  clearDirectorPollTimer();
  if (!directorPollingIsActive(run, state.narration, materialization)
    || state.activeStage !== "director_storyboard"
    || !state.project) {
    state.directorPollContext = null;
    updateDirectorAutoRefreshStatus(run, materialization);
    return;
  }
  const context = {
    generation: state.directorPollGeneration,
    renderEpoch: state.renderEpoch,
    projectId: state.project.id,
    run,
    narration: state.narration,
    materialization,
    summary: state.summary,
    current,
    overview,
  };
  state.directorPollContext = context;
  if (document.visibilityState === "hidden") return;
  state.directorPollTimer = window.setTimeout(() => {
    state.directorPollTimer = null;
    void pollDirectorStatus(context);
  }, immediate ? 0 : state.directorPollDelayMs);
}

function requestDirectorPollNow() {
  const context = state.directorPollContext;
  if (!directorPollContextIsCurrent(context)) return;
  clearDirectorPollTimer();
  if (state.directorPollInFlight) {
    state.directorPollImmediatePending = true;
    return;
  }
  void pollDirectorStatus(context);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") requestDirectorPollNow();
});
window.addEventListener("pagehide", stopDirectorPolling);
window.addEventListener("pagehide", stopProjectsPolling);

// 点击「⋯ 更多」菜单外部时收起（菜单 DOM 会随阶段重渲替换，这里用全局委托）。
document.addEventListener("click", (event) => {
  $$("details.action-menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
});

// 阶段页标题点击复制：阶段切换与 AUTO 轮询都会重建标题 DOM，所以用全局委托。
document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : event.target?.parentElement;
  const copyTitle = target?.closest?.(".copy-title");
  if (!copyTitle || !state.project) return;
  const selection = window.getSelection();
  if (selection && selection.rangeCount && !selection.isCollapsed) return;
  copyTextToClipboard(projectTitle(state.project), "已复制标题");
});

// 自适应高度输入框：内容变化时按 scrollHeight 撑高，避免卡片内嵌套滚动条。
document.addEventListener("input", (event) => {
  if (event.target instanceof HTMLTextAreaElement && event.target.hasAttribute("data-autosize")) {
    autosizeTextarea(event.target);
  }
});

// 键盘最小集：Esc 关闭最上层浮层；Lightbox 打开时 ← / → 切换分组；第 07 步详情视图
// 且无浮层打开时 ← / → 切换分组（会先自动保存未保存的导演草稿）。输入控件聚焦时不劫持。
document.addEventListener("keydown", (event) => {
  const editableTarget = event.target instanceof HTMLElement
    && event.target.closest("input, textarea, select, [contenteditable='true']");
  if (event.key === "Escape") {
    if (editableTarget) return;
    if ($("#storyboardLightbox")) {
      closeStoryboardLightbox();
      return;
    }
    const openMenu = $("details.action-menu[open]");
    if (openMenu) {
      openMenu.open = false;
      return;
    }
    const overlay = $$(".modal-backdrop, .drawer-backdrop").pop();
    // 优先走弹窗注册的关闭逻辑（如批量视频弹窗需清理轮询定时器），未注册才直接移除。
    if (overlay) {
      if (typeof overlay.closeOverlay === "function") overlay.closeOverlay();
      else overlay.remove();
    }
    return;
  }
  if (editableTarget) return;
  if ($("#storyboardLightbox")) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      lightboxStepGroup(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      lightboxStepGroup(1);
    }
    return;
  }
  // 有弹窗/抽屉/菜单打开时不切组；仅详情视图响应 ← / →。
  if (state.activeStage !== "director_storyboard"
    || state.directorView !== "detail"
    || $$(".modal-backdrop, .drawer-backdrop, details.action-menu[open]").length) return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    stepDirectorGroupByDelta(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    stepDirectorGroupByDelta(1);
  }
});

function directorRunControlButtonsMarkup(fact, run, current) {
  const manualReview = allows(run, "MANUAL_REVIEW")
    ? `<button id="manualResolveDirector" class="secondary-button compact" type="button">人工证据处置</button>`
    : "";
  const regenerate = canRegenerateDirectorPreplan(fact, run, current)
    ? `<button id="regenerateDirectorPreplan" class="secondary-button compact" type="button">重新处理前置导演</button>`
    : "";
  return `${manualReview}${regenerate}`;
}

function directorRunControlsMarkup(fact, run, current) {
  const recoveryNote = run?.public_status === "RECOVERY_REQUIRED" && allows(run, "REGENERATE_DIRECTOR_PREPLAN")
    ? "输入一致时保留已成功单元，仅重新请求未完成单元。" : "";
  return `<span id="directorRunControls" class="director-run-controls">${directorRunControlButtonsMarkup(fact, run, current)}<small>${recoveryNote}</small></span>`;
}

function bindDirectorRunActionControls(run) {
  $("#regenerateDirectorPreplan")?.addEventListener("click", () => runManualAction(
    async () => {
      const projectId = state.project.id;
      let created;
      try {
        created = await api.startDirectorPreplan(projectId);
      } catch (error) {
        if (error.code !== "director_input_changed") throw error;
        if (!window.confirm("导演输入已经变化，无法复用上一轮成功单元。确认按新输入重跑全部前置导演？项目冻结的文案等配置保持不变。")) return false;
        created = await api.startDirectorPreplan(projectId, true);
      }
      if (!created?.run_id) throw new Error("后端未返回前置导演 run_id。");
      return queryAcceptedAttempt(
        () => api.directorPreplanRun(projectId, created.run_id),
        "前置导演任务",
      );
    },
    "前置导演重新处理任务已提交",
  ));
  $("#manualResolveDirector")?.addEventListener("click", () => openDirectorManualResolution(run));
}

function bindPreplanControls(run, current) {
  $("#refreshDirectorPreplan")?.addEventListener("click", refreshAndRenderCurrentStage);
  bindDirectorRunActionControls(run);
  $("#previousPreplanPage")?.addEventListener("click", () => {
    state.preplanOffset = Math.max(0, state.preplanOffset - Number(current?.limit || state.preplanLimit));
    renderProjectWorkspace();
  });
  $("#nextPreplanPage")?.addEventListener("click", () => {
    state.preplanOffset += Number(current?.limit || state.preplanLimit);
    renderProjectWorkspace();
  });
}

async function renderDirectorStoryboardStage() {
  const epoch = state.renderEpoch;
  const fact = stageFact("director_storyboard");
  const group = activeGroup();
  const projectId = state.project.id;
  const [runResult, currentResult, materializationResult] = await Promise.all([
    api.optional(() => api.latestDirectorPreplanRun(projectId)),
    api.optional(() => api.directorPreplanCurrent(projectId, state.preplanOffset, state.preplanLimit)),
    group
      ? api.optional(() => api.directorMaterialization(projectId))
      : Promise.resolve({ data: null, reason: "narration_not_ready" }),
  ]);
  if (epoch !== state.renderEpoch) return;

  const run = runResult.data;
  const current = currentResult.data;
  const materialization = materializationResult.data;
  const preplanActionMarkup = `<button id="refreshDirectorPreplan" class="ghost-button compact" type="button">刷新</button>${directorRunControlsMarkup(fact, run, current)}`;

  if (!group) {
    $("#studioMainInner").innerHTML = `${studioHeading("STEP 07 · DIRECTOR PREPLAN", "配音处理中可并行查看无时长前置导演；最终分组、故事板和视频仍由真实配音时间线门禁。", preplanActionMarkup)}
      ${directorStatusStripMarkup(run, materialization)}
      <div class="notice-card amber"><strong>当前处于前置导演阶段</strong><p>这里只处理冻结文案的叙事事件与分镜描述，不估算 start_ms/end_ms，不创建配音分组，也不开放故事板。</p></div>
      ${preplanRunMarkup(run)}
      ${preplanCurrentMarkup(current)}`;
    bindPreplanControls(run, current);
    bindAutosizeTextareas($("#studioMainInner"));
    scheduleDirectorPolling(run, materialization, current);
    return;
  }

  // 项目级事实缓存：列表/详情互跳与详情内切组时复用，不重复拉取项目级端点。
  state.directorStageCache = {
    run,
    current,
    materialization,
    materializationReason: materializationResult.reason || "",
  };
  if (state.directorView === "detail") {
    await renderDirectorDetailView({
      group,
      run,
      current,
      materialization,
      fact,
      epoch,
      materializationReason: materializationResult.reason || "",
    });
    return;
  }
  await renderDirectorListView({
    run,
    current,
    materialization,
    fact,
    epoch,
    materializationReason: materializationResult.reason || "",
  });
}

function directorGroupAnnotations(materialization) {
  return new Map(state.groups.map((item) => [
    groupId(item),
    groupCandidateAnnotation(item, materializationCandidate(materialization, item)),
  ]));
}

function directorGroupToneCounts(annotations) {
  const counts = { ready: 0, review: 0, blocked: 0, missing: 0 };
  for (const note of annotations.values()) counts[note.tone] += 1;
  return counts;
}

function directorGroupSummaryChips(counts) {
  const groupsTotalMs = state.groups.reduce((sum, item) => sum + groupDurationMs(item), 0);
  return `<span class="strip-chip">共 ${state.groups.length} 组 · 总时长 ${formatMilliseconds(groupsTotalMs)}</span><span class="strip-chip is-ok">候选就绪 ${counts.ready}</span>${counts.review ? `<span class="strip-chip is-warn">待人工修正 ${counts.review}</span>` : ""}${counts.blocked ? `<span class="strip-chip is-bad">已阻止 ${counts.blocked}</span>` : ""}${counts.missing ? `<span class="strip-chip">尚无候选 ${counts.missing}</span>` : ""}`;
}

// 确定性重映射面板：scope "group" 供详情视图（含当前组候选与冲突），"project" 供列表视图（候选评审汇总）。
function materializationPanelMarkup(materialization, {
  candidate = null,
  candidateConflicts = [],
  scope = "group",
  unavailableReason = "",
} = {}) {
  if (!materialization) {
    return renderUnavailable("确定性重映射尚未形成", unavailableReason || unavailable("director_materialization_current"));
  }
  const candidates = Array.isArray(materialization.candidates) ? materialization.candidates : [];
  const reviewCount = candidates.filter((item) => ["REQUIRED", "REVIEW_REQUIRED"].includes(item?.review_state)
    || (Array.isArray(item?.conflicts) && item.conflicts.length > 0)).length;
  const candidateCell = scope === "project"
    ? `<article><span>候选评审</span><strong>${reviewCount ? `待修正 ${reviewCount}` : "全部就绪"}</strong><small>${candidates.length} 个权威分组候选</small></article>`
    : `<article><span>当前组候选状态</span><strong>${escapeHtml(reviewStateLabel(candidate?.review_state) || "缺失")}</strong><small${candidate?.final_revision_id ? ` title="${escapeHtml(candidate.final_revision_id)}"` : ""}>${candidate?.final_revision_id ? `最终修订 ${escapeHtml(String(candidate.final_revision_id).slice(0, 8))}` : "尚无最终修订"}</small></article>`;
  return `<section class="panel materialization-panel"><div class="panel-head"><div><h2>确定性重映射</h2><p>不调用外部 AI；按稳定句子身份与权威分组生成最终候选。</p></div><span class="workflow-badge ${statusClass(materialization)}">${escapeHtml(materializationStatusLabel(materialization))}</span></div><div class="panel-body"><div class="real-status-grid"><article><span>前置方案</span><strong>当前修订</strong><small title="${escapeHtml(materialization.preplan_revision_id || "")}">修订 ${escapeHtml(String(materialization.preplan_revision_id || "—").slice(0, 8))}</small></article><article><span>算法版本</span><strong>${escapeHtml(algorithmVersionLabel(materialization.algorithm_version))}</strong><small title="${escapeHtml(materialization.algorithm_version || "")}">确定性重映射</small></article><article><span>分组候选</span><strong>${compactCount(materialization.candidates?.length)}</strong><small>权威配音分组</small></article>${candidateCell}</div>${scope === "group" ? renderDirectorConflicts("当前组原始重映射冲突", candidateConflicts) : ""}</div></section>`;
}

function directorGroupFilterBarMarkup(models) {
  const countFor = (key) => (key === "all"
    ? models.length
    : models.filter((model) => groupCardMatchesFilter(model, key)).length);
  return `<div class="group-filter-bar" role="toolbar" aria-label="按状态筛选分组">${DIRECTOR_GROUP_FILTERS.map((filter) => `<button class="group-filter${state.directorGroupFilter === filter.key ? " is-active" : ""}" data-group-filter="${filter.key}" type="button">${filter.label} <b>${countFor(filter.key)}</b></button>`).join("")}</div>`;
}

function directorGroupCardMarkup(model) {
  const sequenceLabel = String(model.sequence).padStart(2, "0");
  const thumb = model.generationId
    ? `<div class="group-card-thumb"><img data-card-thumb="${escapeHtml(api.storyImageUrl(model.generationId))}" alt="分组 ${sequenceLabel} 当前故事板" /></div>`
    : `<div class="group-card-thumb group-card-thumb-placeholder"><div class="group-image-placeholder"><b>＋</b><strong>尚无当前故事板</strong><span>${escapeHtml(model.storyboardStatus ? `最近状态 ${model.storyboardStatus}` : "未生成")}</span></div></div>`;
  const sentenceRows = model.sentences.map((sentence) => `<li><span class="sentence-index">${String(sentence.sequence).padStart(2, "0")}</span><span class="sentence-text" title="${escapeHtml(sentence.text)}">${escapeHtml(sentence.text)}</span><span class="sentence-time${sentence.suspicious ? " is-anomaly" : ""}" ${sentence.suspicious ? 'title="实测时长不足 1 秒，请检查配音"' : ""}>${formatMilliseconds(sentence.durationMs)}${sentence.suspicious ? " ⚠" : ""}</span></li>`).join("");
  return `<article class="group-card is-${model.tone}" data-open-group="${escapeHtml(model.groupId)}" tabindex="0" role="button" aria-label="打开分组 ${sequenceLabel} 详情">
    <div class="group-card-head"><strong>分组 ${sequenceLabel}</strong><i class="tab-dot is-${model.tone}" aria-hidden="true"></i><span class="group-card-tone">${escapeHtml(model.toneLabel)}</span><span class="group-card-duration">${formatMilliseconds(model.durationMs)}</span></div>
    ${thumb}
    <div class="group-card-meta"><span>分镜 ${model.shotCount == null ? "—" : `${model.shotCount} 条`}</span><span>旁白 ${model.sentences.length} 句</span></div>
    <ul class="group-card-sentences">${sentenceRows || `<li><span class="sentence-text">本组没有旁白句子</span></li>`}</ul>
  </article>`;
}

function directorGroupCardGridMarkup(models) {
  const visible = models.filter((model) => groupCardMatchesFilter(model, state.directorGroupFilter));
  return visible.map(directorGroupCardMarkup).join("")
    || `<div class="empty-state"><div><b>筛</b><h3>没有匹配的分组</h3><p>当前筛选条件下没有分组；请切换其他筛选。</p></div></div>`;
}

let groupCardThumbObserver = null;

// 卡片缩略图懒加载：进入视口才设置真实 src（api.storyImageUrl 的当前版本地址），加载完成淡入。
function bindGroupCardThumbs(root) {
  if (groupCardThumbObserver) {
    groupCardThumbObserver.disconnect();
    groupCardThumbObserver = null;
  }
  const images = $$("img[data-card-thumb]", root);
  if (!images.length) return;
  const load = (img) => setNativeMediaSource(img, img.dataset.cardThumb, "故事板缩略图");
  images.forEach((img) => {
    img.addEventListener("load", () => img.classList.add("is-loaded"), { once: true });
    // 加载失败同样淡入，让 setNativeMediaSource 设置的错误替代文本可见。
    img.addEventListener("error", () => img.classList.add("is-loaded"), { once: true });
  });
  if (typeof IntersectionObserver !== "function") {
    images.forEach(load);
    return;
  }
  groupCardThumbObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      groupCardThumbObserver?.unobserve(entry.target);
      load(entry.target);
    });
  }, { rootMargin: "160px" });
  images.forEach((img) => groupCardThumbObserver.observe(img));
}

function bindDirectorGroupCards(root) {
  $$("[data-open-group]", root).forEach((card) => {
    card.addEventListener("click", () => { void switchDirectorGroup(card.dataset.openGroup); });
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        void switchDirectorGroup(card.dataset.openGroup);
      }
    });
  });
  bindGroupCardThumbs(root);
}

async function renderDirectorListView({
  run,
  current,
  materialization,
  fact,
  epoch,
  overviewResult = null,
  materializationReason = "",
}) {
  // 列表只消费聚合 overview（一次 GET），不逐组请求故事板/导演端点。
  const overview = overviewResult || await api.optional(() => api.storyboardBatchOverview(state.project.id));
  if (epoch !== state.renderEpoch) return;
  const overviewItems = Array.isArray(overview.data?.items) ? overview.data.items : [];
  const overviewByGroup = new Map(overviewItems.map((item) => [String(item.group_id), item]));
  const timeline = narrationTimeline();
  const groupAnnotations = directorGroupAnnotations(materialization);
  const models = state.groups.map((group, index) => groupCardModel({
    group,
    index,
    candidate: materializationCandidate(materialization, group),
    overviewItem: overviewByGroup.get(String(groupId(group))) || null,
    sentences: sentencesForGroup(group),
    timeline,
  }));
  const batchLockAttr = autoBusy() ? ` aria-disabled="true" title="${autoBusyReason()}"` : "";
  const listActions = `<button id="refreshDirectorPreplan" class="ghost-button compact" type="button">刷新</button><details class="action-menu" id="step07MoreMenu"><summary class="ghost-button compact">⋯ 更多</summary><div class="action-menu-list"><button id="batchSaveDirectors" class="ghost-button compact" type="button"${batchLockAttr}>批量保存最终导演</button><button id="batchGenerateStoryboards" class="ghost-button compact" type="button"${batchLockAttr}>批量生成故事板</button>${directorRunControlsMarkup(fact, run, current)}</div></details>`;

  $("#studioMainInner").innerHTML = `${studioHeading("STEP 07 · DIRECTOR & STORYBOARD", "前置方案已按真实配音分组确定性重映射；点击分组卡片进入单组导演与故事板编辑。", listActions)}
    ${directorStatusStripMarkup(run, materialization, directorGroupSummaryChips(directorGroupToneCounts(groupAnnotations)))}
    ${preplanRunMarkup(run)}
    ${materializationPanelMarkup(materialization, { scope: "project", unavailableReason: materializationReason === "not_found" ? "真实配音已形成分组，但重映射结果尚未就绪。" : "" })}
    ${overview.data ? "" : renderUnavailable("故事板批量总览不可用", unavailable("storyboard_batch_overview"))}
    ${directorGroupFilterBarMarkup(models)}
    <div id="directorGroupGrid" class="group-card-grid">${directorGroupCardGridMarkup(models)}</div>`;

  bindPreplanControls(run, current);
  bindActionMenu($("#step07MoreMenu"));
  $("#batchSaveDirectors")?.addEventListener("click", runBatchDirectorSave);
  $("#batchGenerateStoryboards")?.addEventListener("click", openStoryboardBatchModal);
  $$("[data-group-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      // 纯客户端过滤：只重渲卡片网格，不重新请求。
      state.directorGroupFilter = button.dataset.groupFilter;
      $$("[data-group-filter]").forEach((item) => item.classList.toggle("is-active", item === button));
      const grid = $("#directorGroupGrid");
      if (!grid) return;
      grid.innerHTML = directorGroupCardGridMarkup(models);
      bindDirectorGroupCards(grid);
    });
  });
  bindDirectorGroupCards($("#directorGroupGrid"));
  scheduleDirectorPolling(run, materialization, current, { overview: overview.data || null });
}

async function renderDirectorDetailView({
  group,
  run,
  current,
  materialization,
  fact,
  epoch,
  materializationReason = "",
}) {
  const [draftResult, appliedResult, storyboardResult, storyboardHistoryResult] = await Promise.all([
    api.optional(() => api.draft(groupId(group))),
    api.optional(() => api.appliedDirector(groupId(group))),
    api.optional(() => api.storyboard(groupId(group))),
    api.optional(() => api.storyboards(groupId(group))),
  ]);
  if (epoch !== state.renderEpoch) return;

  const draft = draftResult.data;
  const applied = appliedResult.data;
  const director = draft || applied;
  const storyboard = storyboardResult.data;
  const storyboardAttempt = latestUnfinishedOrFailedAttempt(storyboardHistoryResult.data);
  const candidate = materializationCandidate(materialization, group);
  const candidateConflicts = Array.isArray(candidate?.conflicts) ? candidate.conflicts : [];
  const candidateReviewRequired = ["REQUIRED", "REVIEW_REQUIRED"].includes(candidate?.review_state) || candidateConflicts.length > 0;
  const materializationAvailable = ["SUCCEEDED", "RECOVERY_REQUIRED"].includes(publicStatus(materialization)) && Boolean(candidate);
  const shots = director?.shots || candidate?.shots || storyboard?.shots || [];
  const groupSentences = sentencesForGroup(group);
  const canSeedCandidateDraft = !director
    && materializationAvailable
    && allows(materialization, "EDIT_FINAL_DIRECTOR");
  const canEditDirector = (Boolean(draft?.revision_id) && allows(draft, "EDIT_DIRECTOR_DRAFT"))
    || canSeedCandidateDraft;
  const finalDirectorNeedsReview = Boolean(director)
    ? !allows(director, "GENERATE_STORYBOARD")
    : candidateReviewRequired;
  const canGenerateStoryboard = materializationAvailable
    && Boolean(director?.revision_id)
    && allows(director, "GENERATE_STORYBOARD")
    && permitsNewAttempt(storyboardAttempt)
    && !["QUEUED", "RUNNING", "RECOVERY_REQUIRED"].includes(publicStatus(run))
    && !autoBusy();
  const batchLockAttr = autoBusy() ? ` aria-disabled="true" title="${autoBusyReason()}"` : "";
  const groupIndex = state.groups.findIndex((item) => groupId(item) === groupId(group));
  const groupAnnotations = directorGroupAnnotations(materialization);
  const finalActions = `<button id="backToGroupList" class="ghost-button compact group-breadcrumb" type="button">‹ 分组列表</button><button id="refreshStoryboard" class="ghost-button compact" type="button">刷新</button><details class="action-menu" id="step07MoreMenu"><summary class="ghost-button compact">⋯ 更多</summary><div class="action-menu-list"><button id="storyboardHistory" class="ghost-button compact" type="button">故事板历史</button><button id="batchSaveDirectors" class="ghost-button compact" type="button"${batchLockAttr}>批量保存最终导演</button><button id="batchGenerateStoryboards" class="ghost-button compact" type="button"${batchLockAttr}>批量生成故事板</button>${directorRunControlsMarkup(fact, run, current)}</div></details><button id="saveDirector" class="secondary-button compact" type="button" ${canEditDirector ? "" : "disabled"}>保存最终导演</button><button id="generateStoryboard" class="primary-button compact" type="button" ${canGenerateStoryboard ? "" : autoBusy() ? `aria-disabled="true" title="${autoBusyReason()}"` : "disabled"}>生成故事板</button>`;

  $("#studioMainInner").innerHTML = `${studioHeading("STEP 07 · DIRECTOR & STORYBOARD", "前置方案已按真实配音分组确定性重映射；只有当前组最终导演通过校验后才能生成故事板。", finalActions)}
    ${directorStatusStripMarkup(run, materialization, directorGroupSummaryChips(directorGroupToneCounts(groupAnnotations)))}
    ${preplanRunMarkup(run)}
    ${materializationPanelMarkup(materialization, { candidate, candidateConflicts, unavailableReason: materializationReason === "not_found" ? "真实配音已形成分组，但重映射结果尚未就绪。" : "" })}
    ${renderGroupTabs(groupAnnotations)}
    <div class="group-switcher">
      <button id="groupStepPrev" class="ghost-button compact" type="button" ${groupIndex <= 0 ? "disabled" : ""}>‹ 上一组</button>
      <label class="group-sequence-field">第 <input id="groupSequenceInput" type="text" inputmode="numeric" autocomplete="off" value="${groupIndex + 1}" aria-label="输入组号，回车跳转" /> / ${state.groups.length} 组</label>
      <button id="groupStepNext" class="ghost-button compact" type="button" ${groupIndex >= state.groups.length - 1 ? "disabled" : ""}>下一组 ›</button>
    </div>
    <div class="group-summary">
      <div><strong>分组 ${String(groupIndex + 1).padStart(2, "0")}</strong><p>本组包含 ${shots.length} 个最终分镜；句子选择严格限制在本组 ${groupSentences.length} 条权威旁白内。</p></div>
      <div class="group-total"><b>${formatMilliseconds(groupDurationMs(group))}</b><span>${finalDirectorNeedsReview ? "需要人工修正最终导演" : group.video_blocked ? escapeHtml(group.block_reason || "已阻止视频") : "来自真实配音实测时长"}</span></div>
    </div>
    ${attemptStatusNotice("故事板生成任务", storyboardAttempt)}
    <section class="storyboard-group-shell">
      <div class="group-image-column">
        <div class="group-image-preview ${state.project?.aspect_ratio === "9:16" ? "is-portrait" : ""} ${storyboard?.asset && storyboard.public_status === "SUCCEEDED" ? "is-ready" : "is-pending"}" ${storyboard?.asset && storyboard.public_status === "SUCCEEDED" ? 'id="storyboardPreviewTrigger" role="button" tabindex="0" title="点击放大查看故事板"' : ""}>
          ${storyboard?.asset && storyboard.public_status === "SUCCEEDED" ? `<img id="currentStoryboardImage" class="group-image-fit" alt="分组 ${groupIndex + 1} 当前多格故事板" /><span class="group-image-zoom-hint" aria-hidden="true">⤢ 点击放大</span>` : `<div class="group-image-placeholder"><b>＋</b><strong>待生成分组故事板</strong><span>最终导演校验通过后才开放</span></div>`}
          <span class="group-image-badge">分组图 ${String(groupIndex + 1).padStart(2, "0")}</span>
        </div>
        <p class="group-image-hint" title="每组由图片模型一次生成整张多格故事板，承载右侧全部最终分镜">ⓘ 一个分组一张多格图</p>
        ${storyboard ? `<div class="group-image-meta"><strong>版本 ${escapeHtml(storyboard.version)}</strong><span title="${escapeHtml(storyboard.message || statusText(storyboard))}">${escapeHtml(storyboard.message || statusText(storyboard))}</span></div>` : renderUnavailable("当前无故事板", storyboardResult.reason === "not_found" ? "尚未生成当前版本。" : unavailable("storyboard_current"))}
      </div>
      <form id="directorDraftForm" class="group-shot-column">
        <div class="group-shot-head"><div><strong>本组最终分镜</strong><span>${shots.length} 条</span><span class="shot-title-note">全部包含在本组唯一多格故事板中</span></div><div>${canEditDirector ? `<button id="addDirectorShot" class="ghost-button compact" type="button" ${shots.length >= MAX_DIRECTOR_SHOTS ? "disabled" : ""}>新增分镜</button>` : ""}<small>仅可关联本组旁白句子</small></div></div>
        <div class="group-shot-list">${shots.map((shot, index) => storyboardShotRow(shot, index, canEditDirector, groupSentences)).join("") || `<div class="empty-state is-compact"><div><b>镜</b><h3>尚无最终导演分镜</h3><p>等待确定性重映射形成当前组候选。</p></div></div>`}</div>
        ${directorApprovalEditor(shots, director, canEditDirector)}
      </form>
    </section>
    ${director ? `<div class="storyboard-footer"><p>当前导演方案：${escapeHtml(reviewStateLabel(director.review_state) || statusText(director))} · ${escapeHtml(director.message || statusText(director))}</p><details class="footer-tech"><summary>技术详情</summary><p>revision_id：${escapeHtml(director.revision_id)} · review_state：${escapeHtml(director.review_state || "—")}</p></details></div>` : renderUnavailable("最终导演方案不可用", draftResult.reason === "not_found" && appliedResult.reason === "not_found" ? "当前分组尚无确定性重映射草稿。" : unavailable("director_revision"))}`;

  bindPreplanControls(run, current);
  bindGroupTabs((nextGroupId) => switchDirectorGroup(nextGroupId));
  bindAudioButtons($("#studioMainInner"));
  bindDirectorConflictNavigation($("#studioMainInner"));
  bindAutosizeTextareas($("#studioMainInner"));
  bindActionMenu($("#step07MoreMenu"));
  $("#storyboardPreviewTrigger")?.addEventListener("click", () => openStoryboardLightbox(groupId(group)));
  $("#storyboardPreviewTrigger")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openStoryboardLightbox(groupId(group));
    }
  });
  $("#backToGroupList").addEventListener("click", () => { void showDirectorListView(); });
  $("#groupStepPrev")?.addEventListener("click", () => stepDirectorGroupByDelta(-1));
  $("#groupStepNext")?.addEventListener("click", () => stepDirectorGroupByDelta(1));
  const sequenceInput = $("#groupSequenceInput");
  sequenceInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sequenceInput.blur();
    }
  });
  sequenceInput?.addEventListener("change", () => {
    const sequence = parseGroupSequenceInput(sequenceInput.value, state.groups.length);
    if (sequence == null || sequence === groupIndex + 1) {
      if (sequence == null) toast(`请输入 1 至 ${state.groups.length} 之间的有效组号。`, true);
      sequenceInput.value = String(groupIndex + 1);
      return;
    }
    void switchDirectorGroup(groupId(state.groups[sequence - 1])).then((switched) => {
      if (!switched && sequenceInput.isConnected) sequenceInput.value = String(groupIndex + 1);
    });
  });
  if (canEditDirector) bindDirectorShotControls(groupSentences);
  if (storyboard?.asset && storyboard.public_status === "SUCCEEDED" && epoch === state.renderEpoch && $("#currentStoryboardImage")) {
    setNativeMediaSource(
      $("#currentStoryboardImage"),
      api.storyImageUrl(storyboard.generation_id),
      "故事板图片",
    );
  }
  $("#refreshStoryboard").addEventListener("click", refreshAndRenderCurrentStage);
  $("#storyboardHistory").addEventListener("click", () => openHistory("storyboard"));
  $("#batchSaveDirectors")?.addEventListener("click", runBatchDirectorSave);
  $("#batchGenerateStoryboards")?.addEventListener("click", openStoryboardBatchModal);
  if (allows(storyboardAttempt, "RECONCILE")) {
    $("#storyboardHistory").insertAdjacentHTML("afterend", `<button id="reconcileStoryboard" class="secondary-button compact" type="button">对账故事板任务</button>`);
    $("#reconcileStoryboard").addEventListener("click", () => runManualAction(
      async () => {
        await api.reconcileStoryboard(storyboardAttempt.generation_id);
        return api.storyboardGeneration(storyboardAttempt.generation_id);
      },
      "故事板任务对账已提交",
    ));
  }
  $("#saveDirector").addEventListener("click", async () => {
    if (!canEditDirector) {
      toast("当前最终导演修订不可编辑。", true);
      return;
    }
    await runManualAction(
      () => api.editDirectorDraft(
        groupId(group),
        readDirectorDraftForm(
          draft || { revision_id: null },
          draft ? null : candidate?.candidate_id || null,
        ),
      ),
      "最终导演已保存为当前最新版",
    );
  });
  $("#generateStoryboard").addEventListener("click", async () => {
    if (!canGenerateStoryboard) {
      toast(autoBusy() ? autoBusyReason() : "当前最终导演仍有冲突，后端未允许生成故事板。", true);
      return;
    }
    await runManualAction(
      async () => {
        let revisionId = director.revision_id;
        if (canEditDirector) {
          const saved = await api.editDirectorDraft(groupId(group), readDirectorDraftForm(draft));
          if (!allows(saved, "GENERATE_STORYBOARD")) {
            throw new Error(saved.message || "最终导演未通过生成校验。");
          }
          revisionId = saved.revision_id;
        }
        const created = await api.generateStoryboard(groupId(group), revisionId);
        if (!created?.generation_id) throw new Error("后端未返回故事板 generation_id。");
        return queryAcceptedAttempt(
          () => api.storyboardGeneration(created.generation_id),
          "故事板任务",
        );
      },
      "组级多格故事板任务已提交",
    );
  });
  // 未保存编辑跟踪：输入或结构调整都标脏；切换分组/视图/阶段时先自动保存。
  const draftForm = $("#directorDraftForm");
  if (draftForm && canEditDirector) {
    state.directorDraftContext = {
      groupId: groupId(group),
      revisionId: draft?.revision_id ?? null,
      sourceCandidateId: draft ? null : candidate?.candidate_id || null,
    };
    state.directorDraftDirty = false;
    const markDirty = () => { state.directorDraftDirty = true; };
    draftForm.addEventListener("input", markDirty);
    draftForm.addEventListener("change", markDirty);
    draftForm.addEventListener("click", (event) => {
      if (event.target.closest("#addDirectorShot, [data-shot-move], [data-shot-delete]")) markDirty();
    });
  } else {
    state.directorDraftContext = null;
    state.directorDraftDirty = false;
  }
  scheduleDirectorPolling(run, materialization, current);
}

// 与 #saveDirector 完全相同的表单校验 + API 路径；错误抛出给调用方如实 toast。
async function saveDirectorDraftFromForm(context) {
  const body = readDirectorDraftForm(
    { revision_id: context.revisionId },
    context.sourceCandidateId,
  );
  await api.editDirectorDraft(context.groupId, body);
}

// 未保存编辑守卫：切组/返回列表/切阶段/离开项目前，脏草稿先自动保存；失败则不切换。
async function ensureDirectorDraftSaved() {
  if (state.activeStage !== "director_storyboard"
    || state.directorView !== "detail"
    || !state.directorDraftDirty
    || !state.directorDraftContext) return true;
  try {
    await saveDirectorDraftFromForm(state.directorDraftContext);
    state.directorDraftDirty = false;
    toast("已自动保存最终导演修改。");
    return true;
  } catch (error) {
    toast(`最终导演自动保存失败，未切换：${messageOf(error)}`, true);
    return false;
  }
}

function activeGroupSequence() {
  const group = activeGroup();
  if (!group) return null;
  const sequence = Number(group.sequence);
  if (Number.isInteger(sequence) && sequence > 0) return sequence;
  const index = state.groups.findIndex((item) => groupId(item) === groupId(group));
  return index >= 0 ? index + 1 : null;
}

// URL hash 记忆：仅 replaceState 写入，不做 popstate 历史导航。
function syncLocationHash() {
  if (!state.project) return;
  const hash = state.activeStage === "director_storyboard"
    ? hashForDirectorView(state.project.id, state.directorView === "detail" ? activeGroupSequence() : null)
    : `#/project/${state.project.id}/${state.activeStage}`;
  try {
    globalThis.history?.replaceState(null, "", hash);
  } catch { /* 本地 file:// 等环境不允许写 hash 时忽略，不影响功能 */ }
}

function clearLocationHash() {
  try {
    const location = globalThis.location;
    if (!location) return;
    globalThis.history?.replaceState(null, "", `${location.pathname}${location.search}`);
  } catch { /* 同上 */ }
}

// 第 07 步内部互跳：只重渲 #studioMainInner，左侧九阶段导航不动；切换前先过未保存守卫。
async function switchDirectorGroup(nextGroupId) {
  if (!nextGroupId || (nextGroupId === state.activeGroupId && state.directorView === "detail")) return true;
  if (!(await ensureDirectorDraftSaved())) return false;
  const targetGroup = state.groups.find((group) => groupId(group) === nextGroupId);
  if (!targetGroup) {
    // 分组事实已过期（例如轮询刚更新了分组），重拉权威数据而不是沿用旧指针。
    await renderDirectorStoryboardStage();
    return false;
  }
  stopDirectorPolling();
  stopProjectsPolling();
  state.renderEpoch += 1;
  state.activeGroupId = groupId(targetGroup);
  state.directorView = "detail";
  syncLocationHash();
  const cache = state.directorStageCache;
  if (!cache) {
    await renderDirectorStoryboardStage();
    return true;
  }
  try {
    await renderDirectorDetailView({
      group: activeGroup(),
      run: cache.run,
      current: cache.current,
      materialization: cache.materialization,
      fact: stageFact("director_storyboard"),
      epoch: state.renderEpoch,
      materializationReason: cache.materializationReason || "",
    });
  } catch (error) {
    const target = $("#studioMainInner");
    if (target) target.innerHTML = renderUnavailable("页面加载失败", messageOf(error));
    return false;
  }
  return true;
}

async function showDirectorListView() {
  if (state.directorView === "list") return true;
  if (!(await ensureDirectorDraftSaved())) return false;
  stopDirectorPolling();
  stopProjectsPolling();
  state.renderEpoch += 1;
  state.directorView = "list";
  syncLocationHash();
  const cache = state.directorStageCache;
  if (!cache) {
    await renderDirectorStoryboardStage();
    return true;
  }
  try {
    await renderDirectorListView({
      run: cache.run,
      current: cache.current,
      materialization: cache.materialization,
      fact: stageFact("director_storyboard"),
      epoch: state.renderEpoch,
      materializationReason: cache.materializationReason || "",
    });
  } catch (error) {
    const target = $("#studioMainInner");
    if (target) target.innerHTML = renderUnavailable("页面加载失败", messageOf(error));
    return false;
  }
  return true;
}

function stepDirectorGroupByDelta(delta) {
  const index = state.groups.findIndex((group) => groupId(group) === state.activeGroupId);
  const next = Math.min(state.groups.length - 1, Math.max(0, index + delta));
  if (next < 0 || next === index || !state.groups[next]) return;
  void switchDirectorGroup(groupId(state.groups[next]));
}

async function runManualAction(operation, successMessage) {
  const directorPollResume = state.activeStage === "director_storyboard" ? state.directorPollContext : null;
  if (directorPollResume) { stopDirectorPolling(); stopProjectsPolling(); }
  try {
    setBusy(true);
    if (await operation() !== false) toast(successMessage);
    await refreshProjectFacts();
    renderProjectWorkspace();
  } catch (error) {
    toast(messageOf(error), true);
    if (directorPollResume && state.activeStage === "director_storyboard") {
      scheduleDirectorPolling(
        directorPollResume.run,
        directorPollResume.materialization,
        directorPollResume.current,
        { overview: directorPollResume.overview || null },
      );
    }
  } finally {
    setBusy(false);
  }
}

async function queryAcceptedAttempt(query, label) {
  try {
    return await query();
  } catch (error) {
    toast(`${label}已提交，但立即查询状态失败：${messageOf(error)} 请使用刷新状态继续查询。`, true);
    return null;
  }
}

async function refreshAndRenderCurrentStage() {
  const directorPollResume = state.activeStage === "director_storyboard" ? state.directorPollContext : null;
  if (directorPollResume) { stopDirectorPolling(); stopProjectsPolling(); }
  try {
    setBusy(true);
    await refreshProjectFacts();
    renderProjectWorkspace();
  } catch (error) {
    toast(messageOf(error), true);
    if (directorPollResume && state.activeStage === "director_storyboard") {
      scheduleDirectorPolling(
        directorPollResume.run,
        directorPollResume.materialization,
        directorPollResume.current,
        { overview: directorPollResume.overview || null },
      );
    }
  } finally {
    setBusy(false);
  }
}


function videoAssetId(video) {
  return video?.archived_asset?.asset_id || video?.archived_asset?.id || video?.media_asset_id || "";
}

// 第 08 步分组 tabs：用批量 overview 的组视频进度补 tone 圆点，与第 07 步色板对齐。
function videoGroupTabAnnotations(progressItems) {
  const byGroup = new Map((Array.isArray(progressItems) ? progressItems : [])
    .map((item) => [String(item?.group_id || ""), item]));
  return new Map(state.groups.map((group) => {
    const tone = videoGroupTabTone(byGroup.get(groupId(group)));
    return [groupId(group), { tone, label: VIDEO_GROUP_TAB_TONE_LABELS[tone] }];
  }));
}

async function renderGroupVideoStage() {
  const epoch = state.renderEpoch;
  const group = activeGroup();
  const fact = stageFact("group_video_preview");
  if (!group) {
    $("#studioMainInner").innerHTML = `${studioHeading("STEP 08 · GROUP VIDEO & PREVIEW", "每个权威配音分组对应一个组级视频。")}${renderUnavailable("没有配音分组", fact?.blocking_reason || "请先完成配音与时间线。")}`;
    return;
  }
  const [storyboardResult, videoResult, videoHistoryResult, videoServiceResult, batchOverviewResult] = await Promise.all([
    api.optional(() => api.storyboard(groupId(group))),
    api.optional(() => api.video(groupId(group))),
    api.optional(() => api.videos(groupId(group))),
    api.optional(() => api.videoService()),
    api.optional(() => api.videoBatchOverview(state.project.id)),
  ]);
  if (epoch !== state.renderEpoch) return;
  const storyboard = storyboardResult.data;
  const video = videoResult.data;
  const videoService = videoServiceResult.data;
  const videoAttempt = latestUnfinishedOrFailedAttempt(videoHistoryResult.data);
  const assetId = videoAssetId(video);
  const generateDisabledReason = videoService?.available !== true
    ? "H3 视频服务当前不可用"
    : !allows(fact, "generate_group_video")
      ? `流程未就绪：${fact?.blocking_reason || "前序阶段未完成"}`
      : !(storyboard?.public_status === "SUCCEEDED" && storyboard?.generation_id)
        ? "该组尚无成功的当前故事板"
        : group.video_blocked
          ? `该组已被阻止：${group.block_reason || "单句配音超过 15 秒"}`
          : !permitsNewAttempt(videoAttempt)
            ? "该组已有在途任务，请先刷新或对账"
            : null;
  const autoLockReason = autoBusy() ? autoBusyReason() : null;
  const generateBlockedReason = autoLockReason || generateDisabledReason;
  const canGenerate = generateBlockedReason === null;
  const batchDisabledReason = allows(fact, "generate_group_videos_batch")
    ? null
    : `流程未就绪：${fact?.blocking_reason || "前序阶段未完成"}`;
  const batchBlockedReason = autoLockReason || batchDisabledReason;
  const batchOverview = batchOverviewResult.data;
  const progressItems = Array.isArray(batchOverview?.items) ? batchOverview.items : [];
  const progressStrip = progressItems.length
    ? `<div class="studio-status-strip"><span class="chip">组视频进度：已完成 ${progressItems.filter((i) => i.has_current_video).length} · 在途 ${progressItems.filter((i) => VIDEO_ATTEMPT_IN_FLIGHT.includes(i.latest_attempt_stage)).length} · 待生成 ${progressItems.filter((i) => i.eligible && !i.has_current_video).length} · 已阻止 ${progressItems.filter((i) => i.video_blocked).length}${progressItems.some((i) => i.latest_attempt_stalled) ? ` · <strong>远端疑似卡死 ${progressItems.filter((i) => i.latest_attempt_stalled).length}</strong>` : ""}</span></div>`
    : "";
  const canRefreshAttempt = !videoAttempt || allows(videoAttempt, "REFRESH");
  const groupIndex = state.groups.findIndex((item) => groupId(item) === groupId(group));
  // 带病交付标注：仅后端 delivered_with_defect 为真时展示，时段取自 content_defect.findings。
  const defectNotice = groupVideoDefectNotice(video);

  $("#studioMainInner").innerHTML = `${studioHeading("STEP 08 · GROUP VIDEO & PREVIEW", "每个配音分组生成一个 H3 视频；刷新只查询现有任务。", `<button id="refreshGroupVideo" class="ghost-button compact" type="button">刷新</button><button id="videoHistory" class="ghost-button compact" type="button">历史版本</button><button id="batchGenerateVideos" class="ghost-button compact" type="button" ${batchBlockedReason ? `aria-disabled="true" title="${escapeHtml(batchBlockedReason)}"` : ""}>批量生成组级视频</button><button id="generateGroupVideo" class="primary-button compact" type="button" ${generateBlockedReason ? `aria-disabled="true" title="${escapeHtml(generateBlockedReason)}"` : ""}>生成组级视频</button>`)}
    ${progressStrip}
    ${renderGroupTabs(videoGroupTabAnnotations(progressItems))}
    ${videoService?.available === true ? "" : renderUnavailable("H3 视频服务不可用", videoService?.message || videoServiceResult.reason || unavailable("group_video_contract"))}
    <div class="group-summary"><div><strong>分组 ${String(groupIndex + 1).padStart(2, "0")}</strong><p>故事板整图与角色参考图共同作为 H3 REF2VA 输入。</p></div><div class="group-total"><b>${formatMilliseconds(groupDurationMs(group))}</b><span>${group.video_blocked ? escapeHtml(group.block_reason || "视频生成已阻止") : "目标时长来自真实配音"}</span></div></div>
    <div class="real-status-grid mt-14">
      <article><span>故事板</span><strong>${storyboard ? escapeHtml(statusText(storyboard)) : "无当前版本"}</strong><small>${storyboard?.version ? `版本 ${escapeHtml(storyboard.version)}` : escapeHtml(storyboardResult.reason || "")}</small></article>
      <article><span>组视频</span><strong>${video ? escapeHtml(statusText(video)) : "未生成"}</strong><small>${escapeHtml(video?.message || videoResult.reason || "")}</small></article>
      <article><span>目标时长</span><strong>${formatMilliseconds(groupDurationMs(group))}</strong><small>精确配音时长</small></article>
      <article><span>实际视频时长</span><strong>${video?.actual_duration_ms ? formatMilliseconds(video.actual_duration_ms) : "—"}</strong><small>后端 QC 结果</small></article>
    </div>
    ${attemptStatusNotice("H3 生成任务", videoAttempt)}
    ${video ? `<div class="notice-card ${video.public_status === "FAILED" ? "error" : video.public_status === "RECOVERY_REQUIRED" ? "amber" : ""}"><strong>${escapeHtml(statusText(video))}</strong><p>${escapeHtml(video.message || "后端已返回当前视频状态。")}</p></div>` : renderUnavailable("当前无组视频", videoResult.reason === "not_found" ? "尚未生成当前版本。" : unavailable("group_video_current"))}
    ${defectNotice ? `<div class="notice-card amber"><strong>画面缺陷已标注交付</strong><p>${escapeHtml(defectNotice)}</p></div>` : ""}
    ${assetId && allows(video, "VIEW_MEDIA") ? `<figure class="media-preview mt-14"><video id="currentGroupVideo" controls preload="metadata"></video><figcaption>受保护的当前组视频 · 不公开对象存储地址</figcaption></figure>` : `<div class="notice-card mt-14"><strong>媒体尚不可预览</strong><p>仅当后端返回当前归档资产并允许 VIEW_MEDIA 时开放播放。</p></div>`}
    ${video?.prompt ? `<section class="panel"><div class="panel-head"><div><h2>组级视频提示词</h2><p>后端输入快照，只读。</p></div></div><div class="panel-body"><p>${escapeHtml(video.prompt)}</p></div></section>` : ""}`;

  bindGroupTabs();
  $("#refreshGroupVideo").disabled = !canRefreshAttempt;
  if (allows(videoAttempt, "RECONCILE")) {
    $("#videoHistory").insertAdjacentHTML("afterend", `<button id="reconcileGroupVideo" class="secondary-button compact" type="button">对账 H3 任务</button>`);
  }
  if (allows(videoAttempt, "CANCEL") && videoAttempt?.remote_wait_status === "remote_stalled") {
    $("#videoHistory").insertAdjacentHTML("afterend", `<button id="cancelGroupVideo" class="ghost-button compact" type="button">取消远端任务</button>`);
  }
  $("#refreshGroupVideo").addEventListener("click", refreshAndRenderCurrentStage);
  $("#videoHistory").addEventListener("click", () => openHistory("video"));
  $("#reconcileGroupVideo")?.addEventListener("click", () => runManualAction(
    async () => {
      await api.reconcileVideo(videoAttempt.attempt_id);
      return api.videoAttempt(videoAttempt.attempt_id);
    },
    "H3 任务对账已提交",
  ));
  $("#cancelGroupVideo")?.addEventListener("click", () => {
    if (!window.confirm("确认取消该远端生成任务？取消不会删除既有版本，需手动重新生成。")) return;
    runManualAction(
      async () => {
        await api.cancelVideo(videoAttempt.attempt_id);
        return api.videoAttempt(videoAttempt.attempt_id);
      },
      "远端任务取消请求已提交",
    );
  });
  $("#generateGroupVideo").addEventListener("click", () => {
    if (!canGenerate) {
      toast(generateBlockedReason || "后端当前未允许生成组级视频。", true);
      return;
    }
    runManualAction(
      async () => {
        const created = await api.generateVideo(groupId(group), storyboard.generation_id);
        if (!created?.attempt_id) throw new Error("后端未返回 H3 attempt_id。");
        return queryAcceptedAttempt(
          () => api.videoAttempt(created.attempt_id),
          "H3 任务",
        );
      },
      "组级视频任务已提交",
    );
  });
  $("#batchGenerateVideos").addEventListener("click", () => {
    if (batchBlockedReason) {
      toast(batchBlockedReason, true);
      return;
    }
    openVideoBatchModal();
  });
  updateVideoAttemptElapsed(videoAttempt);
  scheduleGroupVideoPolling(videoAttempt);
  if (assetId && allows(video, "VIEW_MEDIA")) {
    if (epoch === state.renderEpoch && $("#currentGroupVideo")) {
      setNativeMediaSource(
        $("#currentGroupVideo"),
        api.mediaUrl(`/projects/${state.project.id}/media/${assetId}/content`),
        "组级视频",
      );
    }
  }
}

// ——— §15 聚合进度视图（仅 AUTO 项目）———
// 数据只来自 workflow-summary 权威响应；轮询复用既有 2.5/5/10s 退避范式：
// 单在途、过期响应保护、签名比对无变化不重绘、非 RUNNING 即停。

function stopAutoElapsedTicker() {
  if (state.autoTickTimer != null) window.clearInterval(state.autoTickTimer);
  state.autoTickTimer = null;
}

// 只更新时间文本：基准来自后端用时，锚点为本次渲染时刻；不推算任何状态或计数。
function startAutoElapsedTicker() {
  stopAutoElapsedTicker();
  const cells = $$("[data-elapsed-ms][data-running='1']");
  const total = $("#autoTotalElapsed[data-total-running='1']");
  if (!cells.length && !total) return;
  state.autoTickAnchor = Date.now();
  state.autoTickTimer = window.setInterval(() => {
    if (document.visibilityState === "hidden") return;
    const delta = Math.max(0, Date.now() - state.autoTickAnchor);
    for (const cell of cells) {
      const base = Number(cell.dataset.elapsedMs);
      if (Number.isFinite(base)) cell.textContent = formatElapsedMs(base + delta);
    }
    if (total) {
      const base = Number(total.dataset.totalMs);
      if (Number.isFinite(base)) total.textContent = formatElapsedMs(base + delta);
    }
  }, 1000);
}

function stopAutoPipelinePolling() {
  if (state.autoPollTimer != null) window.clearTimeout(state.autoPollTimer);
  state.autoPollTimer = null;
  state.autoPollGeneration += 1;
  stopAutoElapsedTicker();
}

function autoPipelinePollIsCurrent(context) {
  return context.generation === state.autoPollGeneration
    && context.epoch === state.renderEpoch
    && context.projectId === state.project?.id
    && state.activeStage === AUTO_PROGRESS_STAGE.key && Boolean(state.token);
}

function scheduleAutoProgressPolling() {
  stopAutoPipelinePolling();
  if (!isAutoProduction() || autoPipelineState(state.summary) !== "RUNNING") return;
  const context = { generation: state.autoPollGeneration, epoch: state.renderEpoch, projectId: state.project.id };
  let step = 0;
  const schedule = () => {
    if (autoPipelinePollIsCurrent(context)) state.autoPollTimer = window.setTimeout(tick, DIRECTOR_POLL_DELAYS_MS[step]);
  };
  const tick = async () => {
    state.autoPollTimer = null;
    if (!autoPipelinePollIsCurrent(context)) return;
    if (document.visibilityState === "hidden" || state.autoPollInFlight) { schedule(); return; }
    state.autoPollInFlight = true;
    try {
      const summary = await api.summary(context.projectId);
      if (!autoPipelinePollIsCurrent(context)) return;
      const changed = JSON.stringify(summary) !== JSON.stringify(state.summary);
      applyProjectFacts(state.narration, summary);
      if (changed || autoPipelineState(summary) !== "RUNNING") {
        renderProjectWorkspace();
        return;
      }
      if ($("#autoProgressAutoRefresh")) $("#autoProgressAutoRefresh").textContent = "自动更新中";
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } catch (error) {
      if (autoPipelinePollIsCurrent(context) && $("#autoProgressAutoRefresh")) {
        $("#autoProgressAutoRefresh").textContent = `状态更新失败：${messageOf(error)}；稍后继续查询。`;
      }
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } finally {
      state.autoPollInFlight = false;
      schedule();
    }
  };
  schedule();
}

async function renderAutoProgressStage() {
  const epoch = state.renderEpoch;
  const summary = state.summary;
  const pipelineState = autoPipelineState(summary);
  const phase = summary?.auto_pipeline?.phase;
  const productionComplete = pipelineState === "COMPLETED" || summary?.stage === "VIDEO_READY";
  let composition = null;
  if (productionComplete) {
    const compositionResult = await api.optional(() => api.composition(state.project.id));
    if (epoch !== state.renderEpoch) return;
    composition = compositionResult.data;
  }

  const reason = autoPauseReason(summary);
  const statusNotice = !summary
    ? renderUnavailable("流程摘要不可用", unavailable("workflow_summary"))
    : pipelineState === "PAUSED"
      ? `<div class="notice-card error"><strong>自动生产已暂停：${escapeHtml(reason.label)}</strong><p>${escapeHtml(reason.message || "后端未返回补充说明。")}</p><small>原因码 ${escapeHtml(reason.code || "未知")} · 当前阶段 ${escapeHtml(autoPhaseLabel(phase))}</small>${reason.groupSequences.length ? `<div class="modal-actions mv-12"><button id="gotoDirectorFix" class="secondary-button compact" type="button">前往第 07 步修正（${reason.groupSequences.length} 组）</button></div>` : ""}</div>`
      : productionComplete
        ? `<div class="notice-card"><strong>视频生产完成</strong><p>全链路自动执行已完成，新成片已自动切换为当前版本。</p></div>`
        : pipelineState === "RUNNING"
          ? `<div class="notice-card"><strong>自动生产进行中</strong><p>当前阶段：${escapeHtml(autoPhaseLabel(phase))}；各阶段由后端自动串联执行。</p></div>`
          : `<div class="notice-card amber"><strong>尚未提交</strong><p>完成第 01–05 步配置后，在第 05 步点击「提交并开始生产」，一次提交即授权全链路自动执行。</p></div>`;

  const rowsMarkup = autoStageRows(summary).map((row) => {
    const reasonText = row.fact?.blocking_reason
      ? ` · ${blockingReasonLabel(row.fact.blocking_reason) || row.fact.blocking_reason}`
      : "";
    const elapsedClass = row.elapsedMs == null ? " is-empty" : row.elapsedRunning ? " is-running" : "";
    const elapsedAttrs = row.elapsedMs == null
      ? ""
      : ` data-elapsed-ms="${row.elapsedMs}"${row.elapsedRunning ? ' data-running="1"' : ""}`;
    return `<div class="sentence-row sentence-row-auto"><span class="sentence-text"><strong>${escapeHtml(row.label)}</strong></span><span class="sentence-elapsed${elapsedClass}"${elapsedAttrs} title="阶段用时（不含暂停/人工等待）">${escapeHtml(formatElapsedMs(row.elapsedMs))}</span><span class="sentence-text"><small>${escapeHtml(statusText(row.fact))}${escapeHtml(reasonText)}</small></span><span class="sentence-time">${escapeHtml(row.detail)}</span></div>`;
  }).join("");

  const canResume = autoResumeAllowed(summary);
  // 成片缺陷分组标注：来自 workflow-summary 的 auto_pipeline.defect_groups（无缺陷为空）。
  const defectGroupsNotice = compositionDefectNotice(summary);
  const canExport = productionComplete
    && allows(stageFact("composition_export"), "export")
    && composition?.public_status === "SUCCEEDED"
    && allows(composition, "VIEW_MEDIA");
  const canPublish = productionComplete && Boolean(composition);
  const headerActions = `${publicationHeaderMarkup(state.project, canPublish)}<button id="refreshAutoProgress" class="ghost-button compact" type="button">刷新</button>${productionComplete ? `<button id="autoExportButton" class="primary-button compact" type="button" ${canExport ? "" : "disabled"}>导出 ZIP</button>` : ""}`;

  const timing = summary?.auto_pipeline?.timing || null;
  const totalElapsedMs = Number.isFinite(timing?.total_elapsed_ms) ? timing.total_elapsed_ms : null;
  const pausedMs = Number.isFinite(timing?.paused_ms) ? timing.paused_ms : 0;
  const totalRunning = totalElapsedMs != null && pipelineState === "RUNNING";
  const totalAttrs = totalElapsedMs == null
    ? ""
    : ` data-total-ms="${totalElapsedMs}"${totalRunning ? ' data-total-running="1"' : ""}`;
  const pausedNote = pausedMs >= 1000
    ? `<span>另等待 ${escapeHtml(formatElapsedMs(pausedMs))}</span>`
    : "";
  const totalMarkup = `<div class="panel-metric"><small>总用时</small><strong id="autoTotalElapsed"${totalAttrs}>${escapeHtml(formatElapsedMs(totalElapsedMs))}</strong>${pausedNote}</div>`;

  $("#studioMainInner").innerHTML = `${studioHeading("AUTO PRODUCTION", "一次提交授权全链路自动执行：配音 → 拆句 → 导演 → 故事板 → 视频 → 合成。", headerActions)}
    <div class="studio-status-strip"><span class="workflow-badge ${statusClass(autoProgressStageFact())}">${escapeHtml(autoPipelineStateLabel(pipelineState))}</span><span class="strip-chip">当前阶段 ${escapeHtml(autoPhaseLabel(phase))}</span><small id="autoProgressAutoRefresh">${pipelineState === "RUNNING" ? "自动更新中" : "自动更新已停止"}</small></div>
    ${statusNotice}
    ${pipelineState === "PAUSED" ? `<div class="modal-actions mv-12"><button id="resumeAutoPipeline" class="primary-button" type="button" ${canResume ? "" : "disabled"}>恢复自动执行</button>${canResume ? "" : `<small>后端当前未开放恢复动作（allowed_actions 不含 RESUME_AUTO_PIPELINE）。</small>`}</div>` : ""}
    ${summary && !pipelineState ? `<div class="modal-actions mv-12"><button id="autoGoSubmit" class="secondary-button" type="button">前往第 05 步提交</button></div>` : ""}
    <section class="panel"><div class="panel-head"><div><h2>全链路进度</h2><p>计数与状态全部来自后端权威 workflow-summary；用时按后端阶段时间戳展示，秒级刷新。</p></div>${totalMarkup}</div><div class="panel-body"><div class="sentence-list">${rowsMarkup}</div></div></section>
    ${productionComplete ? `${defectGroupsNotice ? `<div class="notice-card amber"><strong>缺陷分组标注</strong><p>${escapeHtml(defectGroupsNotice)}</p></div>` : ""}${composition?.video_asset_id && allows(composition, "VIEW_MEDIA") ? `<figure class="media-preview mt-14"><video id="autoProgressVideo" controls preload="metadata"></video><figcaption>当前正式成片 · 受保护媒体</figcaption></figure>` : `<div class="notice-card mt-14"><strong>成片暂不可播放</strong><p>后端返回当前成片并允许 VIEW_MEDIA 时自动开放播放。</p></div>`}` : ""}`;

  $("#refreshAutoProgress")?.addEventListener("click", refreshAndRenderCurrentStage);
  $("#publishProjectFromStage")?.addEventListener("click", () => void publishCurrentProject());
  $("#resumeAutoPipeline")?.addEventListener("click", () => runManualAction(
    async () => {
      const result = await api.autoPipelineResume(state.project.id);
      const failure = autoResumeFailureMessage(result);
      if (failure) {
        // 恢复已消耗 1 轮授权但又暂停：先刷新事实（轮次、暂停详情），再按失败提示。
        await refreshProjectFacts();
        renderProjectWorkspace();
        throw new Error(failure);
      }
    },
    "已恢复自动执行；相关对象预算已按规则重置。",
  ));
  $("#autoExportButton")?.addEventListener("click", downloadFormalExport);
  $("#autoGoSubmit")?.addEventListener("click", () => {
    state.activeStage = "narration_settings";
    renderProjectWorkspace();
  });
  // 暂停详情直达：跳到第 07 步并预选首个阻塞组（复用 openProject 的组号路由语义）。
  $("#gotoDirectorFix")?.addEventListener("click", () => {
    const sequence = reason.groupSequences[0];
    state.activeStage = "director_storyboard";
    const routed = state.groups.find((group) => Number(group.sequence) === sequence)
      || state.groups[sequence - 1];
    if (routed) {
      state.activeGroupId = groupId(routed);
      state.directorView = "detail";
    }
    renderProjectWorkspace();
  });
  if (productionComplete && composition?.video_asset_id && allows(composition, "VIEW_MEDIA") && epoch === state.renderEpoch && $("#autoProgressVideo")) {
    setNativeMediaSource(
      $("#autoProgressVideo"),
      api.mediaUrl(`/projects/${state.project.id}/media/${composition.video_asset_id}/content`),
      "成片",
    );
  }
  scheduleAutoProgressPolling();
  // 必须在 scheduleAutoProgressPolling 之后启动：它会先 stopAutoPipelinePolling，
  // 而 stop 会清掉 ticker。
  startAutoElapsedTicker();
}

async function renderCompositionStage() {
  const epoch = state.renderEpoch;
  const projectName = projectTitle(state.project);
  // 复用本次渲染已取得的受保护字幕 Blob：下载按钮不再发起第二次媒体请求。
  let srtPreviewBlob = null;
  const fact = stageFact("composition_export");
  const [compositionResult, attemptsResult] = await Promise.all([
    api.optional(() => api.composition(state.project.id)),
    api.optional(() => api.compositionAttempts(state.project.id)),
  ]);
  if (epoch !== state.renderEpoch) return;
  const composition = compositionResult.data;
  const attempts = Array.isArray(attemptsResult.data?.items)
    ? attemptsResult.data.items
    : Array.isArray(attemptsResult.data)
      ? attemptsResult.data
      : [];
  const latestAttempt = attempts[0] || null;
  const canCompose = allows(fact, "compose");
  const skippedGroups = Number(fact?.counts?.skipped_groups || 0);
  const skippedMs = skippedGroups
    ? state.groups.filter((item) => item.video_blocked).reduce((sum, item) => sum + groupDurationMs(item), 0)
    : 0;
  const skippedNotice = skippedGroups
    ? `<div class="notice-card amber"><strong>已跳过 ${skippedGroups} 个被阻止分组</strong><p>其配音与画面（合计约 ${formatMilliseconds(skippedMs)}）已从成片与字幕中整体排除，剩余内容按连续时间轴合成。</p></div>`
    : "";
  const canRetryComposition = allows(fact, "RETRY_COMPOSITION")
    && allows(latestAttempt, "RETRY_COMPOSITION");
  const canExport = allows(fact, "export")
    && composition?.public_status === "SUCCEEDED"
    && allows(composition, "VIEW_MEDIA");
  const canPublish = Boolean(composition);
  // 带病交付分组标注（AUTO 项目由 workflow-summary 透出；MANUAL 无该键，恒为空）。
  const defectGroupsNotice = compositionDefectNotice(state.summary);

  $("#studioMainInner").innerHTML = `${studioHeading("STEP 09 · COMPOSITION & EXPORT", "按组顺序合成当前视频、真实 TTS、环境声和逐句 SRT。", `${publicationHeaderMarkup(state.project, canPublish)}<button id="refreshComposition" class="ghost-button compact" type="button">刷新</button><button id="compositionHistory" class="ghost-button compact" type="button">历史版本</button><button id="startComposition" class="secondary-button compact" type="button" ${canCompose ? "" : "disabled"}>开始合成</button>`)}
    ${skippedNotice}
    <div class="real-status-grid">
      <article><span>流程阶段</span><strong>${escapeHtml(fact?.status || "不可用")}</strong><small>${escapeHtml(fact?.blocking_reason || "")}</small></article>
      <article><span>当前成片</span><strong>${composition ? escapeHtml(statusText(composition)) : "无当前版本"}</strong><small>${composition?.version_id ? `版本 ${escapeHtml(composition.version_id)}` : ""}</small></article>
      <article><span>最近合成任务</span><strong>${latestAttempt ? escapeHtml(statusText(latestAttempt)) : "未提交"}</strong><small>${escapeHtml(latestAttempt?.message || "")}</small></article>
      <article><span>正式下载</span><strong>${canExport ? "可用" : "未开放"}</strong><small>ZIP：最终 MP4 + SRT</small></article>
    </div>
    ${latestAttempt ? `<div class="notice-card ${latestAttempt.public_status === "FAILED" ? "error" : latestAttempt.public_status === "RECOVERY_REQUIRED" ? "amber" : ""}"><strong>${escapeHtml(statusText(latestAttempt))}</strong><p>${escapeHtml(latestAttempt.message || "后端已返回合成任务状态。")}</p></div>` : renderUnavailable("尚无合成任务", attemptsResult.reason ? unavailable("composition_attempts") : "仅在后端 allowed_actions 允许时手动开始。")}
    ${defectGroupsNotice ? `<div class="notice-card amber"><strong>缺陷分组标注</strong><p>${escapeHtml(defectGroupsNotice)}</p></div>` : ""}
    ${composition?.video_asset_id && allows(composition, "VIEW_MEDIA") ? `<figure class="media-preview mt-14"><video id="currentCompositionVideo" controls preload="metadata"></video><figcaption>当前正式成片 · 受保护媒体</figcaption></figure>` : `<div class="notice-card mt-14"><strong>当前没有可播放成片</strong><p>新合成失败不会覆盖旧的当前版本。</p></div>`}
    ${composition?.srt_asset_id && allows(composition, "VIEW_MEDIA") ? `<section class="panel"><div class="panel-head"><div><h2>SRT 预览</h2><p>逐句时间来自真实配音累计时间线。</p></div><div class="panel-head-actions">${publicationHeaderMarkup(state.project, canPublish, "publishProjectFromSrt")}<button id="downloadSrtPreview" class="ghost-button compact" type="button" disabled title="字幕加载完成后可下载">下载 SRT</button></div></div><div class="panel-body"><pre id="currentSrtPreview">正在读取受保护字幕…</pre></div></section>` : ""}
    <section class="panel"><div class="panel-head"><div><h2>正式导出</h2><p>唯一正式下载入口是临时或流式 ZIP。</p></div><span class="status-pill ${canExport ? "" : "muted"}">${canExport ? "最终视频 + SRT" : "等待当前成片"}</span></div><div class="panel-body"><div class="export-card ${canExport ? "" : "is-disabled"}"><div class="export-icon">ZIP</div><h3>成片与字幕</h3><p>响应完成或中断后由后端清理临时 ZIP。</p><button id="downloadExport" class="primary-button" type="button" ${canExport ? "" : "disabled"}>下载正式 ZIP</button></div></div></section>`;

  // 捕获本次渲染的按钮节点：过期渲染只能操作已脱离文档的旧节点，不能启用新一代渲染的按钮。
  const srtDownloadButton = $("#downloadSrtPreview");

  if (canRetryComposition) {
    $("#startComposition").insertAdjacentHTML(
      "afterend",
      `<button id="retryComposition" class="secondary-button compact" type="button">重试失败任务</button>`,
    );
  }
  $("#refreshComposition").addEventListener("click", refreshAndRenderCurrentStage);
  $("#publishProjectFromStage")?.addEventListener("click", () => void publishCurrentProject());
  // SRT 预览面板的发布入口：长页面底部不必滚回顶部才能标记发布。
  $("#publishProjectFromSrt")?.addEventListener("click", () => void publishCurrentProject());
  $("#compositionHistory").addEventListener("click", () => openHistory("composition"));
  $("#startComposition").addEventListener("click", () => {
    if (!canCompose) {
      toast("后端当前未允许开始合成。", true);
      return;
    }
    runManualAction(() => api.compose(state.project.id), "合成任务已提交");
  });
  $("#retryComposition")?.addEventListener("click", () => {
    if (!canRetryComposition) {
      toast("后端当前未允许重试合成任务。", true);
      return;
    }
    runManualAction(() => api.retryComposition(state.project.id), "合成重试任务已提交");
  });
  $("#downloadExport").addEventListener("click", downloadFormalExport);
  srtDownloadButton?.addEventListener("click", () => {
    if (!srtPreviewBlob) {
      toast("字幕尚未加载完成，请稍后重试。", true);
      return;
    }
    downloadCurrentSrt(srtPreviewBlob, projectName);
  });

  if (composition?.video_asset_id && allows(composition, "VIEW_MEDIA")) {
    if (epoch === state.renderEpoch && $("#currentCompositionVideo")) {
      setNativeMediaSource(
        $("#currentCompositionVideo"),
        api.mediaUrl(`/projects/${state.project.id}/media/${composition.video_asset_id}/content`),
        "成片",
      );
    }
  }
  if (composition?.srt_asset_id && allows(composition, "VIEW_MEDIA")) {
    const target = $("#currentSrtPreview");
    try {
      const blob = await api.blob(`/projects/${state.project.id}/media/${composition.srt_asset_id}/content`);
      const text = await blob.text();
      if (epoch !== state.renderEpoch || !srtDownloadButton?.isConnected) return;
      srtPreviewBlob = blob;
      if (target) target.textContent = text;
      srtDownloadButton.disabled = false;
      srtDownloadButton.title = "下载当前预览的 SRT 字幕";
    } catch (error) {
      if (epoch !== state.renderEpoch) return;
      if (target) target.textContent = `字幕读取失败：${messageOf(error)}`;
      if (srtDownloadButton?.isConnected) srtDownloadButton.title = "字幕读取失败，请刷新后重试";
    }
  }
}

// ---------------- STEP 11：简介（频道级模板 + 最终 SRT 章节时间） ----------------
function descriptionTemplatesWithDrafts() {
  return state.descriptionTemplates.map((template) => ({
    ...template,
    prompt: state.descriptionTemplateDrafts[template.id] ?? template.prompt,
  }));
}

function descriptionSaveText() {
  if (state.descriptionTemplateSaveError) return "保存失败，点击重试";
  if (Object.keys(state.descriptionTemplateSaveInFlight).some((id) => state.descriptionTemplateSaveInFlight[id])) return "保存中…";
  if (state.descriptionTemplateSavedClock) return `已保存 ${state.descriptionTemplateSavedClock}`;
  return "自动保存";
}

async function renderDescriptionStage() {
  const epoch = state.renderEpoch;
  const projectId = state.project?.id;
  if (!projectId) return;
  try {
    const [descriptionPayload, templatesPayload] = await Promise.all([
      api.descriptionState(projectId),
      api.descriptionTemplates(state.project.channel_id),
    ]);
    if (epoch !== state.renderEpoch || state.project?.id !== projectId || state.activeStage !== "description") return;
    state.description = descriptionStateModel(descriptionPayload);
    state.descriptionTemplates = descriptionTemplatesModel(templatesPayload);
    if (!state.descriptionTemplates.some((item) => item.id === state.descriptionSelectedTemplateId)) {
      const currentTemplate = state.descriptionTemplates.find(
        (item) => item.version === state.description?.current?.templateVersion,
      );
      state.descriptionSelectedTemplateId = currentTemplate?.id || state.descriptionTemplates[0]?.id || null;
    }
    renderDescriptionView();
  } catch (error) {
    if (epoch !== state.renderEpoch) return;
    $("#studioMainInner").innerHTML = renderUnavailable(
      "简介不可用",
      messageOf(error),
    );
  }
}

function renderDescriptionView() {
  if (!state.description) return;
  const templates = descriptionTemplatesWithDrafts();
  $("#studioMainInner").innerHTML = `${studioHeading(
    "STEP 11 · DESCRIPTION",
    "频道级模板、真实频道信息与当前最终 SRT 共同生成简介；该步骤不影响视频生产完成状态。",
    `<button id="refreshDescription" class="ghost-button compact" type="button">刷新</button>`,
  )}
    ${descriptionTemplatesMarkup(templates, state.descriptionSelectedTemplateId, descriptionSaveText())}
    ${descriptionGenerateMarkup(
      state.description,
      templates,
      state.descriptionSelectedTemplateId,
      state.descriptionEditingId,
      state.descriptionEditDraft,
      state.descriptionError,
    )}
    ${descriptionHistoryMarkup(state.description.versions)}`;
  bindDescriptionEvents();
}

function bindDescriptionEvents() {
  $("#refreshDescription")?.addEventListener("click", () => void renderDescriptionStage());
  $("#addDescriptionTemplate")?.addEventListener("click", () => void addDescriptionTemplate());
  $("#descriptionTemplateSelect")?.addEventListener("change", (event) => {
    state.descriptionSelectedTemplateId = event.target.value || null;
  });
  $$("[data-description-template-input]").forEach((area) => {
    const templateId = area.dataset.descriptionTemplateInput;
    area.addEventListener("input", () => {
      state.descriptionTemplateDrafts[templateId] = area.value;
      state.descriptionTemplateDirty[templateId] = true;
      state.descriptionTemplateSaveError = "";
      const counter = $(`#descriptionTemplateCount-${CSS.escape(templateId)}`);
      if (counter) counter.textContent = `${countDescriptionPrompt(area.value)} / ${DESCRIPTION_PROMPT_LIMIT}`;
      paintDescriptionTemplateStatus();
      scheduleDescriptionTemplateSave(templateId);
    });
    area.addEventListener("blur", () => void flushDescriptionTemplateSave(templateId));
  });
  $$("[data-description-template-copy]").forEach((button) => {
    button.addEventListener("click", () => {
      const templateId = button.dataset.descriptionTemplateCopy;
      const template = descriptionTemplatesWithDrafts().find((item) => item.id === templateId);
      copyTextToClipboard(template?.prompt || "", "已复制模板", "模板为空，暂无可复制内容");
    });
  });
  $$("[data-description-template-delete]").forEach((button) => {
    button.addEventListener("click", () => void deleteDescriptionTemplate(button.dataset.descriptionTemplateDelete));
  });
  $("#generateDescription")?.addEventListener("click", () => void generateDescriptionFromPage());
  $("#saveDescriptionEdit")?.addEventListener("click", () => void saveDescriptionEdit());
  $("#cancelDescriptionEdit")?.addEventListener("click", () => {
    state.descriptionEditingId = null;
    state.descriptionEditDraft = "";
    renderDescriptionView();
  });
  $$("[data-description-copy]").forEach((button) => {
    button.addEventListener("click", () => {
      const version = descriptionVersionById(button.dataset.descriptionCopy);
      copyTextToClipboard(version?.content || "", "已复制简介", "简介为空，暂无可复制内容");
    });
  });
  $$("[data-description-download]").forEach((button) => {
    button.addEventListener("click", () => downloadDescriptionVersion(button.dataset.descriptionDownload));
  });
  $$("[data-description-select]").forEach((button) => {
    button.addEventListener("click", () => void selectDescriptionVersion(button.dataset.descriptionSelect));
  });
  $$("[data-description-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      const version = descriptionVersionById(button.dataset.descriptionEdit);
      if (!version) return;
      state.descriptionEditingId = version.id;
      state.descriptionEditDraft = version.content;
      renderDescriptionView();
      $("#descriptionEditText")?.focus();
    });
  });
  $("#descriptionTemplateSaveState")?.addEventListener("click", () => {
    if (!state.descriptionTemplateSaveError) return;
    state.descriptionTemplateSaveError = "";
    void flushDescriptionTemplateSaves();
  });
}

function paintDescriptionTemplateStatus() {
  const node = $(".description-save-state");
  if (!node) return;
  node.textContent = descriptionSaveText();
  node.classList.toggle("is-error", Boolean(state.descriptionTemplateSaveError));
}

const DESCRIPTION_TEMPLATE_SAVE_DELAY = 600;

function scheduleDescriptionTemplateSave(templateId) {
  clearTimeout(state.descriptionTemplateSaveTimers[templateId]);
  state.descriptionTemplateSaveTimers[templateId] = setTimeout(() => {
    void flushDescriptionTemplateSave(templateId);
  }, DESCRIPTION_TEMPLATE_SAVE_DELAY);
}

async function flushDescriptionTemplateSave(templateId) {
  clearTimeout(state.descriptionTemplateSaveTimers[templateId]);
  delete state.descriptionTemplateSaveTimers[templateId];
  const previous = state.descriptionTemplateSavePromises[templateId] || Promise.resolve(true);
  const next = previous.then(() => saveDescriptionTemplateNow(templateId));
  state.descriptionTemplateSavePromises[templateId] = next;
  const saved = await next;
  if (state.descriptionTemplateSavePromises[templateId] === next) {
    delete state.descriptionTemplateSavePromises[templateId];
  }
  return saved;
}

async function saveDescriptionTemplateNow(templateId) {
  const channelId = state.project?.channel_id;
  const template = state.descriptionTemplates.find((item) => item.id === templateId);
  const prompt = state.descriptionTemplateDrafts[templateId];
  if (!channelId || !template || prompt === undefined || prompt === template.prompt) {
    delete state.descriptionTemplateDirty[templateId];
    return true;
  }
  state.descriptionTemplateSaveInFlight[templateId] = true;
  paintDescriptionTemplateStatus();
  try {
    const response = await api.updateDescriptionTemplate(channelId, templateId, prompt);
    state.descriptionTemplates = descriptionTemplatesModel(response);
    state.descriptionTemplateSavedClock = formatSavedClock(new Date());
    state.descriptionTemplateSaveError = "";
    if (state.descriptionTemplateDrafts[templateId] === prompt) {
      delete state.descriptionTemplateDrafts[templateId];
      delete state.descriptionTemplateDirty[templateId];
    }
    return true;
  } catch (error) {
    state.descriptionTemplateSaveError = messageOf(error);
    return false;
  } finally {
    delete state.descriptionTemplateSaveInFlight[templateId];
    paintDescriptionTemplateStatus();
  }
}

async function flushDescriptionTemplateSaves() {
  const ids = Object.keys({
    ...state.descriptionTemplateDirty,
    ...state.descriptionTemplateSavePromises,
  });
  if (!ids.length) return true;
  const results = [];
  for (const id of ids) results.push(await flushDescriptionTemplateSave(id));
  return results.every(Boolean);
}

async function addDescriptionTemplate() {
  const channelId = state.project?.channel_id;
  if (!channelId || state.descriptionGenerating) return;
  if (!(await flushDescriptionTemplateSaves())) {
    toast("模板尚未保存完成，请处理保存错误后再新增。", true);
    return;
  }
  const selected = descriptionTemplatesWithDrafts().find(
    (item) => item.id === state.descriptionSelectedTemplateId,
  ) || state.descriptionTemplates[0];
  if (!selected) return;
  try {
    setBusy(true);
    const response = await api.createDescriptionTemplate(channelId, selected.prompt);
    state.descriptionTemplates = descriptionTemplatesModel(response);
    state.descriptionSelectedTemplateId = state.descriptionTemplates.at(-1)?.id || null;
    state.descriptionTemplateSavedClock = formatSavedClock(new Date());
    renderDescriptionView();
    toast("已新增简介模板。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function deleteDescriptionTemplate(templateId) {
  const template = descriptionTemplatesWithDrafts().find((item) => item.id === templateId);
  if (!template) return;
  if (!(await flushDescriptionTemplateSaves())) {
    toast("模板尚未保存完成，请处理保存错误后再删除。", true);
    return;
  }
  const result = await actionModal({
    title: `删除${template.label}？`,
    eyebrow: "DELETE DESCRIPTION TEMPLATE",
    bodyHtml: `<p>将删除该频道下的「${escapeHtml(template.label)}」。已经生成的简介版本会保留当时提示词快照，不会被删除。</p>`,
    confirmText: "删除模板",
    danger: true,
  });
  if (!result.confirmed) return;
  try {
    setBusy(true);
    const response = await api.deleteDescriptionTemplate(state.project.channel_id, templateId);
    state.descriptionTemplates = descriptionTemplatesModel(response);
    delete state.descriptionTemplateDrafts[templateId];
    delete state.descriptionTemplateDirty[templateId];
    if (state.descriptionSelectedTemplateId === templateId) {
      state.descriptionSelectedTemplateId = state.descriptionTemplates[0]?.id || null;
    }
    renderDescriptionView();
    toast("简介模板已删除。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function generateDescriptionFromPage() {
  const projectId = state.project?.id;
  const templateId = state.descriptionSelectedTemplateId;
  if (!projectId || !templateId || state.descriptionGenerating) return;
  if (!(await flushDescriptionTemplateSaves())) {
    state.descriptionError = "模板尚未保存完成，请先处理保存错误。";
    renderDescriptionView();
    return;
  }
  const template = descriptionTemplatesWithDrafts().find((item) => item.id === templateId);
  state.descriptionGenerating = true;
  state.descriptionError = "";
  if (state.description) {
    state.description = {
      ...state.description,
      generation: {
        id: "local-pending",
        status: "RUNNING",
        templateVersion: template?.version || 0,
        errorCode: "",
      },
    };
  }
  renderDescriptionView();
  try {
    const response = await api.idempotent(
      `description:${projectId}:${templateId}`,
      (key) => api.generateDescription(projectId, templateId, key),
    );
    state.description = descriptionStateModel(response);
    state.descriptionEditingId = null;
    state.descriptionEditDraft = "";
    await refreshProjectFacts();
    if (state.project?.id === projectId && state.activeStage === "description") {
      renderDescriptionView();
    }
    toast("简介已生成。");
  } catch (error) {
    state.descriptionError = messageOf(error);
    if (state.project?.id === projectId && state.activeStage === "description") {
      try {
        state.description = descriptionStateModel(await api.descriptionState(projectId));
      } catch {
        // 保留已渲染的权威状态；错误仍以本次 POST 为准展示。
      }
      renderDescriptionView();
    }
  } finally {
    state.descriptionGenerating = false;
  }
}

function descriptionVersionById(versionId) {
  return state.description?.versions.find((item) => item.id === versionId) || null;
}

function downloadDescriptionVersion(versionId) {
  const version = descriptionVersionById(versionId);
  if (!version) return;
  const anchor = document.createElement("a");
  anchor.href = URL.createObjectURL(new Blob([version.content], { type: "text/plain; charset=utf-8" }));
  anchor.download = descriptionDownloadFilename(projectTitle(state.project), version);
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(anchor.href);
  toast("简介 TXT 下载已开始。");
}

async function selectDescriptionVersion(versionId) {
  if (!state.project || state.descriptionGenerating) return;
  try {
    setBusy(true);
    state.description = descriptionStateModel(await api.selectDescription(state.project.id, versionId));
    await refreshProjectFacts();
    renderDescriptionView();
    toast("当前简介已切换。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function saveDescriptionEdit() {
  if (!state.project || !state.descriptionEditingId) return;
  const content = $("#descriptionEditText")?.value || "";
  if (!content.trim()) {
    toast("简介内容不能为空。", true);
    return;
  }
  try {
    setBusy(true);
    const response = await api.editDescription(
      state.project.id,
      state.descriptionEditingId,
      content,
    );
    state.description = descriptionStateModel(response);
    state.descriptionEditingId = null;
    state.descriptionEditDraft = "";
    await refreshProjectFacts();
    renderDescriptionView();
    toast("已保存为新的简介版本。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function downloadFormalExport() {
  const fact = stageFact("composition_export");
  if (!allows(fact, "export")) {
    toast("后端当前未允许正式导出。", true);
    return;
  }
  try {
    setBusy(true);
    const blob = await api.blob(`/projects/${state.project.id}/export`);
    const url = keepObjectUrl(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "composition.zip";
    anchor.click();
    toast("正式 ZIP 下载已开始。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

// 复用预览阶段已取得的字幕 Blob：点击下载不产生新的媒体请求。
function downloadCurrentSrt(blob, title) {
  const anchor = document.createElement("a");
  anchor.href = keepObjectUrl(blob);
  anchor.download = srtDownloadFilename(title);
  anchor.click();
  toast("SRT 字幕下载已开始。");
}

function historyItems(data) {
  if (Array.isArray(data)) return data;
  return Array.isArray(data?.items) ? data.items : [];
}

function historyId(kind, item) {
  if (kind === "storyboard") return item.generation_id;
  if (kind === "video") return item.attempt_id;
  return item.version_id;
}

function historyCurrent(item) {
  return Boolean(item.is_current || item.current);
}

function historyAssetId(kind, item) {
  if (kind === "video") return videoAssetId(item);
  if (kind === "composition") return item.video_asset_id || "";
  return item.asset?.media_asset_id || "";
}

async function openHistory(kind) {
  const group = activeGroup();
  const load = {
    storyboard: () => api.storyboards(groupId(group)),
    video: () => api.videos(groupId(group)),
    composition: () => api.compositions(state.project.id),
  }[kind];
  if (!load || (kind !== "composition" && !group)) {
    toast("当前没有可查询的历史范围。", true);
    return;
  }
  try {
    setBusy(true);
    const result = await api.optional(load);
    const items = historyItems(result.data);
    $("#historyOverlay")?.remove();
    app.insertAdjacentHTML("beforeend", `<div id="historyOverlay" class="drawer-backdrop">
      <aside class="history-drawer" role="dialog" aria-modal="true" aria-label="历史版本">
        <div class="drawer-head"><div><span class="eyebrow">IMMUTABLE HISTORY</span><h2>历史版本</h2></div><button id="closeHistory" class="modal-close" type="button">×</button></div>
        <div id="historyPreview"></div>
        ${result.data ? items.map((item, index) => {
          const id = historyId(kind, item);
          const current = historyCurrent(item);
          const selectable = allows(item, "SELECT_CURRENT") && !current;
          const previewable = allows(item, "VIEW_MEDIA") && (kind === "storyboard" ? Boolean(item.asset) : Boolean(historyAssetId(kind, item)));
          return `<article class="version-item ${current ? "is-current" : ""}">
            <div><strong>版本 ${escapeHtml(item.version ?? index + 1)} · ${escapeHtml(statusText(item))}</strong><small>${escapeHtml(item.message || (current ? "当前选用" : "历史不可变版本"))}</small></div>
            <div class="version-actions">${previewable ? `<button class="ghost-button compact" data-history-preview="${id}" data-history-kind="${kind}" data-history-asset="${historyAssetId(kind, item)}" type="button">预览</button>` : ""}<button class="secondary-button compact" data-history-select="${id}" data-history-kind="${kind}" type="button" ${selectable ? "" : "disabled"}>${current ? "当前版本" : "切换"}</button></div>
          </article>`;
        }).join("") || `<div class="empty-state"><div><b>史</b><h3>没有历史版本</h3><p>后端返回了空历史集合。</p></div></div>` : renderUnavailable("历史端点不可用", unavailable(`${kind}_history`))}
      </aside>
    </div>`);
    const restoreFocus = manageDialogFocus($("#historyOverlay"));
    const closeHistoryDrawer = () => {
      $("#historyOverlay")?.remove();
      restoreFocus();
    };
    $("#historyOverlay").closeOverlay = closeHistoryDrawer;
    $("#closeHistory").addEventListener("click", closeHistoryDrawer);
    $("#historyOverlay").addEventListener("click", (event) => {
      if (event.target.id === "historyOverlay") closeHistoryDrawer();
    });
    $$("[data-history-preview]").forEach((button) => {
      button.addEventListener("click", () => previewHistoryVersion(button));
    });
    $$("[data-history-select]").forEach((button) => {
      button.addEventListener("click", () => selectHistoryVersion(button));
    });
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function previewHistoryVersion(button) {
  const kind = button.dataset.historyKind;
  const id = button.dataset.historyPreview;
  const assetId = button.dataset.historyAsset;
  const target = $("#historyPreview");
  if (!target) return;
  try {
    setBusy(true);
    target.innerHTML = kind === "storyboard"
      ? `<figure class="media-preview"><img alt="历史故事板" /></figure>`
      : `<figure class="media-preview"><video controls preload="metadata"></video></figure>`;
    setNativeMediaSource(
      kind === "storyboard" ? $("img", target) : $("video", target),
      kind === "storyboard"
        ? api.storyImageUrl(id)
        : api.mediaUrl(`/projects/${state.project.id}/media/${assetId}/content`),
      kind === "storyboard" ? "历史故事板" : "历史视频",
    );
  } catch (error) {
    target.innerHTML = renderUnavailable("历史媒体读取失败", messageOf(error));
  } finally {
    setBusy(false);
  }
}

async function selectHistoryVersion(button) {
  const kind = button.dataset.historyKind;
  const id = button.dataset.historySelect;
  const group = activeGroup();
  const operation = {
    storyboard: () => api.selectStoryboard(groupId(group), id),
    video: () => api.selectVideo(groupId(group), id),
    composition: () => api.selectComposition(state.project.id, id),
  }[kind];
  if (!operation || button.disabled) return;
  try {
    setBusy(true);
    await operation();
    $("#historyOverlay")?.remove();
    toast("当前版本已切换。");
    await refreshProjectFacts();
    renderProjectWorkspace();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}


// ——— 第十阶段「封面」与频道封面风格区 ———

const COVER_SAVE_DEBOUNCE_MS = 800;
const COVER_ANALYSIS_WATCH_MS = 150000;

function coverAnalyzingKey(scope, id) {
  return `${scope}:${id}`;
}

function coverAnalyzing(scope, id) {
  return Boolean(id) && state.coverAnalyzingScope === coverAnalyzingKey(scope, id);
}

function coverStyleSlot(scope) {
  return scope === "channel" ? "coverChannelStyle" : "coverStyle";
}

function coverDraftApi(scope, id) {
  return scope === "channel"
    ? {
      upload: (file) => api.uploadChannelCoverRefImage(id, file),
      save: (body) => api.saveChannelCoverDraft(id, body),
      analyze: (force) => api.analyzeChannelCoverStyle(id, force),
      promote: (body) => api.promoteChannelCoverStyle(id, body),
    }
    : {
      upload: (file) => api.uploadProjectCoverRefImage(id, file),
      save: (body) => api.saveProjectCoverDraft(id, body),
      analyze: (force) => api.analyzeProjectCoverStyle(id, force),
      promote: () => api.promoteProjectCoverStyle(id),
    };
}

// 封面编辑器本地镜像按 scope:id 挂载：键变化即清空，未保存编辑不会串到别的项目/频道。
function coverEditorMount(scope, id) {
  const key = coverAnalyzingKey(scope, id);
  if (state.coverMirrorScopeKey === key) return;
  state.coverMirrorScopeKey = key;
  state.coverDimsDraft = null;
  state.coverRefImageDraft = null;
  state.coverEditedTextDraft = null;
  state.coverAnalyzedDims = null;
  state.coverStyleLabelDraft = "";
  state.coverStyleProfileChoice = null;
  state.coverCardSelection = null;
  state.coverCardDrafts = {};
  state.coverCardAnalyzing = null;
  state.coverCardAnalyzingUpdatedAt = null;
}

function coverDraftBodyFor(scope) {
  const draft = state[coverStyleSlot(scope)]?.draft || null;
  return {
    ref_image_id: state.coverRefImageDraft ?? draft?.ref_image_id ?? null,
    dims: state.coverDimsDraft ?? coverDimsWithDefault(draft?.dims),
    edited_text: state.coverEditedTextDraft ?? String(draft?.edited_text || ""),
  };
}

// 最近一次解析所用勾选（脏状态推导基准）：优先取后端草稿视图可能提供的 analysis_dims；
// 后端草稿视图当前不外泄该字段，回落为会话内提交解析成功时记录的勾选（页面重载后无法
// 确知 → 不标脏，见 coverAnalysisDirty）。
function coverAnalyzedDimsFor(scope) {
  const draft = state[coverStyleSlot(scope)]?.draft;
  if (draft?.analysis_dims != null) return dimsFromPayload(draft.analysis_dims);
  return state.coverAnalyzedDims;
}

function coverDraftDirty() {
  return state.coverDimsDraft != null
    || state.coverRefImageDraft != null
    || state.coverEditedTextDraft != null;
}

function setCoverDraftStatus(text, saving = false) {
  const node = $("#coverDraftSaveStatus");
  if (!node) return;
  node.classList.toggle("is-saving", saving);
  node.textContent = text;
}

function scheduleCoverDraftSave(scope, id) {
  window.clearTimeout(state.coverDraftSaveTimer);
  setCoverDraftStatus("等待保存", true);
  state.coverDraftSaveTimer = window.setTimeout(() => {
    void flushCoverDraftSave(scope, id);
  }, COVER_SAVE_DEBOUNCE_MS);
}

// 风格草稿防抖保存（800ms、单在途）：解析/固定/生成与切页面前必须先落盘。
// 不传参数时冲刷当前挂载的编辑器（页面守卫用）；false = 保存失败，调用方中止后续动作。
async function flushCoverDraftSave(scope = null, id = null) {
  window.clearTimeout(state.coverDraftSaveTimer);
  state.coverDraftSaveTimer = null;
  const mountedKey = state.coverMirrorScopeKey || "";
  const sep = mountedKey.indexOf(":");
  const resolvedScope = scope || (sep > 0 ? mountedKey.slice(0, sep) : "project");
  const resolvedId = id || (sep > 0 ? mountedKey.slice(sep + 1) : "");
  if (!resolvedId) return true;
  if (!coverDraftDirty()) { setCoverDraftStatus("已保存"); return true; }
  const body = coverDraftBodyFor(resolvedScope);
  const serverDraft = state[coverStyleSlot(resolvedScope)]?.draft;
  const unchanged = Boolean(serverDraft)
    && String(serverDraft.ref_image_id || "") === String(body.ref_image_id || "")
    && JSON.stringify(dimsFromPayload(serverDraft.dims)) === JSON.stringify(body.dims)
    && String(serverDraft.edited_text || "") === body.edited_text;
  if (unchanged) {
    state.coverDimsDraft = null;
    state.coverRefImageDraft = null;
    state.coverEditedTextDraft = null;
    setCoverDraftStatus("已保存");
    return true;
  }
  if (state.coverDraftSaveInFlight) {
    scheduleCoverDraftSave(resolvedScope, resolvedId);
    return true;
  }
  state.coverDraftSaveInFlight = true;
  try {
    setCoverDraftStatus("保存中", true);
    const result = await coverDraftApi(resolvedScope, resolvedId).save(body);
    const slot = coverStyleSlot(resolvedScope);
    if (result?.draft) state[slot] = { ...(state[slot] || {}), draft: result.draft };
    if (JSON.stringify(state.coverDimsDraft) === JSON.stringify(body.dims)) state.coverDimsDraft = null;
    if (state.coverRefImageDraft === body.ref_image_id) state.coverRefImageDraft = null;
    if (state.coverEditedTextDraft === body.edited_text) state.coverEditedTextDraft = null;
    setCoverDraftStatus("已保存");
    if (coverDraftDirty()) scheduleCoverDraftSave(resolvedScope, resolvedId);
    return true;
  } catch (error) {
    setCoverDraftStatus("保存失败");
    toast(`封面风格草稿保存失败：${messageOf(error)}`, true);
    return false;
  } finally {
    state.coverDraftSaveInFlight = false;
  }
}

function setCoverPromptStatus(text, saving = false) {
  const node = $("#coverPromptSaveStatus");
  if (!node) return;
  node.classList.toggle("is-saving", saving);
  node.textContent = text;
}

function scheduleCoverPromptSave() {
  window.clearTimeout(state.coverPromptSaveTimer);
  setCoverPromptStatus("等待保存", true);
  state.coverPromptSaveTimer = window.setTimeout(() => {
    void flushCoverPromptSave();
  }, COVER_SAVE_DEBOUNCE_MS);
}

// 面板整段编辑防抖保存：只更新当前版本的 prompt_current，不产生新版本。
async function flushCoverPromptSave() {
  window.clearTimeout(state.coverPromptSaveTimer);
  state.coverPromptSaveTimer = null;
  if (state.coverPromptDraft == null) return true;
  const current = state.coverPrompts?.current;
  const text = state.coverPromptDraft;
  if (!current || state.activeStage !== "cover" || !state.project?.snapshot) {
    state.coverPromptDraft = null;
    return true;
  }
  if (String(current.prompt_current || "") === text) {
    state.coverPromptDraft = null;
    setCoverPromptStatus("已保存");
    return true;
  }
  try {
    setCoverPromptStatus("保存中", true);
    const updated = await api.patchCurrentCoverPrompt(state.project.id, { prompt_current: text });
    state.coverPromptDraft = null;
    applyCoverPromptUpdate(updated);
    setCoverPromptStatus("已保存");
    syncCoverPromptEditedBadge(updated);
    return true;
  } catch (error) {
    setCoverPromptStatus("保存失败");
    toast(`封面提示词保存失败：${messageOf(error)}`, true);
    return false;
  }
}

// 切阶段/离开页面前的封面编辑守卫：先风格草稿、后面板文本。
async function ensureCoverEditsSaved() {
  if (!(await flushCoverDraftSave())) return false;
  if (!(await ensureCoverTemplateEditsSaved())) return false;
  return flushCoverPromptSave();
}

function applyCoverPromptUpdate(updated) {
  if (!updated || !state.coverPrompts) return;
  const matches = (item) => String(item?.generation_id || "") === String(updated.generation_id || "");
  const current = state.coverPrompts.current;
  const items = Array.isArray(state.coverPrompts.items) ? state.coverPrompts.items : [];
  state.coverPrompts = {
    ...state.coverPrompts,
    current: current && matches(current) ? updated : current,
    items: items.map((item) => (matches(item) ? updated : item)),
  };
}

function syncCoverPromptEditedBadge(updated) {
  const badge = $("#coverPromptEditedBadge");
  if (!badge) return;
  const edited = String(updated?.prompt_current || "") !== String(updated?.prompt_system || "");
  badge.textContent = edited ? "编辑版" : "系统版";
  badge.classList.toggle("purple", edited);
  badge.classList.toggle("muted", !edited);
}

function stopCoverPolling() {
  if (state.coverPollTimer != null) window.clearTimeout(state.coverPollTimer);
  state.coverPollTimer = null;
  state.coverPollGeneration += 1;
}

function stopCoverWatch() {
  if (state.coverWatchTimer != null) window.clearTimeout(state.coverWatchTimer);
  state.coverWatchTimer = null;
  state.coverWatchGeneration += 1;
}

async function fetchCoverFacts(projectId, channelId) {
  const [style, channelStyle, prompts, images, summary, cards, characterImages, templates] = await Promise.all([
    api.optional(() => api.projectCoverStyle(projectId)),
    api.optional(() => api.channelCoverStyle(channelId)),
    api.optional(() => api.coverPrompts(projectId)),
    api.optional(() => api.coverImages(projectId)),
    api.optional(() => api.summary(projectId)),
    api.optional(() => api.coverCharacterCards(channelId)),
    api.optional(() => api.images(channelId)),
    // 临时功能：模板端点缺失或迁移未执行时不影响封面页其余部分。
    api.optional(() => api.channelCoverTemplates(channelId)).catch(() => ({ data: null })),
  ]);
  return {
    style: style.data,
    channelStyle: channelStyle.data,
    prompts: prompts.data,
    images: images.data,
    summary: summary.data,
    cards: cards.data,
    characterImages: characterImages.data,
    templates: templates.data,
  };
}

function applyCoverFacts(facts) {
  state.coverStyle = facts.style;
  state.coverChannelStyle = facts.channelStyle;
  state.coverPrompts = facts.prompts;
  state.coverImages = facts.images;
  state.coverCharacterCards = facts.cards;
  state.coverCharacterImages = facts.characterImages;
  applyCoverTemplateFacts(facts.templates);
  if (facts.summary) state.summary = facts.summary;
}

function applyCoverTemplateFacts(payload) {
  const model = payload ? coverTemplateCollectionModel(payload) : null;
  state.coverTemplates = model;
  if (!model) return;
  const ids = new Set(model.items.map((item) => item.id));
  // 有未保存草稿的卡片不被服务端快照覆盖；已删除的卡片同步清掉草稿。
  for (const item of model.items) {
    if (!state.coverTemplateDirty[item.id] && !state.coverTemplateSaving[item.id]) {
      state.coverTemplateDrafts[item.id] = templateDraftFromItem(item);
    }
  }
  for (const id of Object.keys(state.coverTemplateDrafts)) {
    if (!ids.has(id)) {
      delete state.coverTemplateDrafts[id];
      delete state.coverTemplateDirty[id];
      delete state.coverTemplateSaving[id];
      delete state.coverTemplateErrors[id];
      delete state.coverTemplateSavedClock[id];
    }
  }
}

function coverPollIsCurrent(context) {
  return context.generation === state.coverPollGeneration
    && context.epoch === state.renderEpoch
    && context.projectId === state.project?.id
    && state.activeStage === "cover"
    && Boolean(state.token);
}

function coverRunningRows() {
  return [
    state.coverPrompts?.current,
    ...(Array.isArray(state.coverPrompts?.items) ? state.coverPrompts.items : []),
    state.coverImages?.current,
    ...(Array.isArray(state.coverImages?.items) ? state.coverImages.items : []),
  ].filter(Boolean);
}

function coverNeedsPolling() {
  return coverRunningRows().some(coverTaskRunning)
    || coverAnalyzing("project", state.project?.id)
    || Boolean(state.coverCardAnalyzing);
}

// 封面轮询照配音模式：2.5s/5s/10s 退避、单在途、终态停止；解析在途另设
// deadline，超时停止等待并恢复按钮（解析失败不写入草稿，页面提示后可重试）。
function scheduleCoverPolling() {
  stopCoverPolling();
  if (!state.project || !coverNeedsPolling()) return;
  const context = { generation: state.coverPollGeneration, epoch: state.renderEpoch, projectId: state.project.id };
  let step = 0;
  const schedule = () => {
    if (coverPollIsCurrent(context)) state.coverPollTimer = window.setTimeout(tick, DIRECTOR_POLL_DELAYS_MS[step]);
  };
  const tick = async () => {
    state.coverPollTimer = null;
    if (!coverPollIsCurrent(context)) return;
    if (document.visibilityState === "hidden" || state.coverPollInFlight) { schedule(); return; }
    state.coverPollInFlight = true;
    try {
      const facts = await fetchCoverFacts(context.projectId, state.project.channel_id);
      if (!coverPollIsCurrent(context)) return;
      const changed = JSON.stringify([facts.style, facts.channelStyle, facts.prompts, facts.images, facts.summary, facts.cards, facts.characterImages])
        !== JSON.stringify([state.coverStyle, state.coverChannelStyle, state.coverPrompts, state.coverImages, state.summary, state.coverCharacterCards, state.coverCharacterImages]);
      applyCoverFacts(facts);
      if (state.coverCardAnalyzing) {
        const card = (Array.isArray(state.coverCharacterCards?.items) ? state.coverCharacterCards.items : [])
          .find((item) => String(item?.card_id || "") === state.coverCardAnalyzing);
        const landed = Boolean(card) && String(card?.updated_at || "") !== String(state.coverCardAnalyzingUpdatedAt || "");
        if (!card || landed || Date.now() > state.coverAnalysisDeadline) {
          state.coverCardAnalyzing = null;
          state.coverCardAnalyzingUpdatedAt = null;
          renderCoverStageView();
          return;
        }
      }
      if (coverAnalyzing("project", context.projectId)
        && (state.coverStyle?.draft?.has_analysis || Date.now() > state.coverAnalysisDeadline)) {
        state.coverAnalyzingScope = null;
        renderCoverStageView();
        return;
      }
      if (changed || !coverNeedsPolling()) {
        renderCoverStageView();
        return;
      }
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } catch (error) {
      if (coverPollIsCurrent(context) && $("#coverAutoRefresh")) {
        $("#coverAutoRefresh").textContent = `状态更新失败：${messageOf(error)}；稍后继续查询。`;
      }
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } finally {
      state.coverPollInFlight = false;
      schedule();
    }
  };
  schedule();
}

// 频道管理页的解析守候：独立于封面页轮询，页签/频道切换即失效。
function scheduleChannelCoverWatch(channelId) {
  stopCoverWatch();
  const context = { generation: state.coverWatchGeneration, epoch: state.renderEpoch, channelId };
  let step = 0;
  const current = () => context.generation === state.coverWatchGeneration
    && context.epoch === state.renderEpoch
    && state.nav === "channels"
    && state.assetChannelId === context.channelId
    && coverAnalyzing("channel", context.channelId)
    && Boolean(state.token);
  const schedule = () => {
    if (current()) state.coverWatchTimer = window.setTimeout(tick, DIRECTOR_POLL_DELAYS_MS[step]);
  };
  const tick = async () => {
    state.coverWatchTimer = null;
    if (!current()) return;
    try {
      const style = await api.channelCoverStyle(context.channelId);
      if (!current()) return;
      state.coverChannelStyle = style;
      if (style?.draft?.has_analysis || Date.now() > state.coverAnalysisDeadline) {
        state.coverAnalyzingScope = null;
        stopCoverWatch();
        await renderAssets();
        return;
      }
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } catch {
      // 守候失败保留旧数据，下个周期再试。
      step = Math.min(step + 1, DIRECTOR_POLL_DELAYS_MS.length - 1);
    } finally {
      schedule();
    }
  };
  schedule();
}

async function reloadCoverFactsAndRender() {
  const projectId = state.project?.id;
  const epoch = state.renderEpoch;
  if (!projectId) return;
  try {
    const facts = await fetchCoverFacts(projectId, state.project.channel_id);
    if (state.project?.id !== projectId || state.renderEpoch !== epoch) return;
    applyCoverFacts(facts);
    renderCoverStageView();
  } catch (error) {
    toast(`封面状态刷新失败：${messageOf(error)}`, true);
  }
}

async function refreshCoverScope(scope) {
  if (scope === "channel") {
    await renderAssets();
    return;
  }
  await reloadCoverFactsAndRender();
}

// 项目快照角色图 id（按快照 ordinal 顺序；缺快照时回落项目当前选择，封面页冻结后必有快照）。
function coverSnapshotImageIds(project) {
  const characters = Array.isArray(project?.snapshot?.characters) ? project.snapshot.characters : [];
  if (characters.length) {
    return [...characters]
      .sort((a, b) => (Number(a?.ordinal) || 0) - (Number(b?.ordinal) || 0))
      .map((item) => String(item?.character_image_id || ""))
      .filter(Boolean);
  }
  return (Array.isArray(project?.character_image_ids) ? project.character_image_ids : []).map(String);
}

// 角色卡本地回写：保存/解析提交后用响应卡替换或追加列表项，避免等待整轮刷新。
function upsertLocalCoverCard(card) {
  if (!card || !state.coverCharacterCards) return;
  const items = Array.isArray(state.coverCharacterCards.items) ? [...state.coverCharacterCards.items] : [];
  const index = items.findIndex((item) => String(item?.card_id || "") === String(card.card_id || ""));
  if (index >= 0) items[index] = card;
  else items.push(card);
  state.coverCharacterCards = { ...state.coverCharacterCards, items };
}

// 勾选编辑器参考图缩略图：经 v5 受保护媒体端点同源加载（频道/项目两作用域）。
function hydrateCoverRefImages(root, scope, id) {
  $$("[data-cover-ref-image]", root).forEach((image) => {
    setNativeMediaSource(image, api.coverRefImageMediaUrl(scope, id, image.dataset.coverRefImage), "参考图");
  });
}

function coverDimsTreeMarkup(tree) {
  return tree.map((dim) => `<div class="cover-dim" data-cover-dim-row="${escapeHtml(dim.key)}">
    <label class="cover-dim-head"><input type="checkbox" data-cover-dim value="${escapeHtml(dim.key)}" ${dim.state === "all" ? "checked" : ""} /><strong>${escapeHtml(dim.label)}</strong><small class="cover-dim-kind">${escapeHtml(dim.kindLabel)}</small><small>${dim.checkedCount}/${dim.total}</small></label>
    <div class="cover-dim-children">${dim.children.map((child) => `<label class="cover-dim-child"><input type="checkbox" data-cover-child data-cover-child-dim="${escapeHtml(dim.key)}" data-cover-child-label="${escapeHtml(child.label)}" value="${escapeHtml(child.id)}" ${child.checked ? "checked" : ""} /><span>${escapeHtml(child.label)}</span></label>`).join("")}</div>
    <div class="cover-dim-hint">${escapeHtml(dim.kindHint)}</div>
  </div>`).join("");
}

function syncCoverDimIndeterminate(tree) {
  tree.forEach((dim) => {
    const input = $(`[data-cover-dim-row="${dim.key}"] [data-cover-dim]`);
    if (input) input.indeterminate = dim.state === "some";
  });
}

function refreshCoverDimsTreeUI(scope, id) {
  const container = $("#coverDimsTree");
  if (!container) return;
  const tree = coverDimsTree(coverDraftBodyFor(scope).dims);
  container.innerHTML = coverDimsTreeMarkup(tree);
  syncCoverDimIndeterminate(tree);
  const counts = coverDimsCounts(tree);
  const countNode = $("#coverDimsCount");
  if (countNode) countNode.textContent = `已选 ${counts.checked} / ${counts.total} 子项`;
  const analyze = $("#coverAnalyze");
  if (analyze) {
    analyze.disabled = !(coverDraftBodyFor(scope).ref_image_id && counts.checked > 0) || coverAnalyzing(scope, id);
  }
  const dirtyNode = $("#coverDimsDirty");
  if (dirtyNode) {
    const draft = state[coverStyleSlot(scope)]?.draft;
    dirtyNode.hidden = !coverAnalysisDirty({
      hasAnalysis: Boolean(draft?.has_analysis),
      analyzedDims: coverAnalyzedDimsFor(scope),
      selected: coverDraftBodyFor(scope).dims,
    }).dirty;
  }
}

// 参考图与勾选编辑器（封面页第二区与频道页封面风格区共用）。
function coverRefEditorMarkup({ scope, style, analyzing }) {
  const body = coverDraftBodyFor(scope);
  const tree = coverDimsTree(body.dims);
  const counts = coverDimsCounts(tree);
  const refImages = Array.isArray(style?.ref_images) ? style.ref_images : [];
  const canAnalyze = Boolean(body.ref_image_id) && counts.checked > 0 && !analyzing;
  const canPromote = Boolean(body.edited_text.trim()) && !analyzing;
  const selectedRef = String(body.ref_image_id || "");
  const dimsDirty = coverAnalysisDirty({
    hasAnalysis: Boolean(style?.draft?.has_analysis),
    analyzedDims: coverAnalyzedDimsFor(scope),
    selected: body.dims,
  });
  return `<div id="coverRefEditor" data-cover-scope="${escapeHtml(scope)}">
    <form id="coverRefUploadForm" class="upload-zone">
      <label class="field"><span>上传参考图 <small>jpg / jpeg / png / webp，≤10MB</small></span><input name="file" type="file" accept="image/jpeg,image/png,image/webp" required /></label>
      <button class="secondary-button compact" type="submit">上传参考图</button>
    </form>
    <div class="cover-ref-list mt-12">${refImages.map((image, index) => {
      const refId = String(image.ref_image_id || "");
      const meta = [
        image.width && image.height ? `${image.width}×${image.height}` : "",
        image.image_format || "",
        image.byte_size ? `${(Number(image.byte_size) / 1024 / 1024).toFixed(2)} MB` : "",
      ].filter(Boolean).join(" · ");
      return `<label class="cover-ref-item ${refId && refId === selectedRef ? "is-selected" : ""}">
        <input type="radio" name="cover_ref_image" value="${escapeHtml(refId)}" ${refId && refId === selectedRef ? "checked" : ""} />
        <img class="cover-ref-thumb" data-cover-ref-image="${escapeHtml(refId)}" alt="参考图 ${index + 1}" />
        <span><strong>参考图 ${index + 1}</strong><small>${escapeHtml(meta)}</small></span>
      </label>`;
    }).join("") || `<div class="empty-state is-compact"><div><b>图</b><h3>尚未上传参考图</h3><p>上传后勾选下方维度并点击「解析参考图」。</p></div></div>`}</div>
    <div class="cover-dims-head"><strong>勾选解析维度</strong><small id="coverDimsCount">已选 ${counts.checked} / ${counts.total} 子项</small></div>
    <p class="cover-hint cover-dims-guide">${escapeHtml(COVER_DIMS_GUIDE)}</p>
    <div id="coverDimsTree" class="cover-dims-tree">${coverDimsTreeMarkup(tree)}</div>
    <div class="cover-editor-actions">
      <button id="coverAnalyze" class="secondary-button compact" type="button" ${canAnalyze ? "" : "disabled"}>${analyzing ? "解析中…" : "解析参考图"}</button>
      <button id="coverReanalyze" class="secondary-button compact" type="button" ${canAnalyze && body.edited_text.trim() ? "" : "disabled"} title="忽略缓存强制重新解析，将覆盖当前文本">重新解析</button>
      <span id="coverDimsDirty" class="status-pill amber" ${dimsDirty.dirty ? "" : "hidden"}>${escapeHtml(dimsDirty.notice)}</span>
      <small id="coverDraftSaveStatus" class="cover-save-status"></small>
    </div>
    <label class="field"><span>解析文本 <small>≤${COVER_EDITED_TEXT_MAX} 码点，可编辑，防抖自动保存；生成时作为参考约束</small><small id="coverEditedTextCount" class="cover-count">${body.edited_text.length} / ${COVER_EDITED_TEXT_MAX}</small></span>
      <textarea id="coverEditedText" rows="8" maxlength="${COVER_EDITED_TEXT_MAX}" placeholder="解析后自动填充；格式为「维度：子项：描述；…」逐行">${escapeHtml(body.edited_text)}</textarea></label>
    <div class="cover-editor-actions">
      ${scope === "channel" ? `<label class="field cover-label-field"><span>版本备注名 <small>可选，≤${COVER_STYLE_LABEL_MAX} 码点</small><small id="coverStyleLabelCount" class="cover-count">${state.coverStyleLabelDraft.length} / ${COVER_STYLE_LABEL_MAX}</small></span><input id="coverStyleLabel" maxlength="${COVER_STYLE_LABEL_MAX}" value="${escapeHtml(state.coverStyleLabelDraft)}" placeholder="例如：蓝调科技风" /></label>` : ""}
      <button id="coverPromote" class="primary-button compact" type="button" ${canPromote ? "" : "disabled"}>固定为频道风格</button>
      <small class="cover-hint">固化为频道不可变风格版本并设为当前；新项目生成封面时默认使用。</small>
    </div>
  </div>`;
}

function bindCoverRefEditor({ scope, id }) {
  const root = $("#coverRefEditor");
  if (!root) return;
  $$('input[name="cover_ref_image"]', root).forEach((radio) => {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      state.coverRefImageDraft = radio.value;
      $$(".cover-ref-item", root).forEach((item) => {
        item.classList.toggle("is-selected", $("input", item)?.value === radio.value);
      });
      scheduleCoverDraftSave(scope, id);
    });
  });
  $("#coverDimsTree", root)?.addEventListener("change", (event) => {
    const input = event.target;
    const current = coverDraftBodyFor(scope).dims;
    if (input.matches("[data-cover-dim]")) {
      state.coverDimsDraft = toggleCoverDim(current, input.value, input.checked);
    } else if (input.matches("[data-cover-child]")) {
      state.coverDimsDraft = toggleCoverChild(current, input.dataset.coverChildDim, input.dataset.coverChildLabel, input.checked);
    } else {
      return;
    }
    refreshCoverDimsTreeUI(scope, id);
    scheduleCoverDraftSave(scope, id);
  });
  const editedText = $("#coverEditedText", root);
  editedText?.addEventListener("input", () => {
    state.coverEditedTextDraft = editedText.value;
    const counter = $("#coverEditedTextCount", root);
    if (counter) counter.textContent = `${editedText.value.length} / ${COVER_EDITED_TEXT_MAX}`;
    scheduleCoverDraftSave(scope, id);
  });
  const styleLabel = $("#coverStyleLabel", root);
  styleLabel?.addEventListener("input", () => {
    state.coverStyleLabelDraft = styleLabel.value;
    const counter = $("#coverStyleLabelCount", root);
    if (counter) counter.textContent = `${styleLabel.value.length} / ${COVER_STYLE_LABEL_MAX}`;
  });
  $("#coverRefUploadForm", root)?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = new FormData(event.currentTarget).get("file");
    if (!(file instanceof File) || !file.size) {
      toast("请选择有效图片文件。", true);
      return;
    }
    try {
      setBusy(true);
      const uploaded = await coverDraftApi(scope, id).upload(file);
      if (!coverDraftBodyFor(scope).ref_image_id && uploaded?.ref_image_id) {
        state.coverRefImageDraft = uploaded.ref_image_id;
        await flushCoverDraftSave(scope, id);
      }
      toast("参考图已上传。");
    } catch (error) {
      toast(messageOf(error), true);
      return;
    } finally {
      setBusy(false);
    }
    await refreshCoverScope(scope);
  });
  $("#coverAnalyze", root)?.addEventListener("click", () => {
    void submitCoverAnalysis(scope, id);
  });
  $("#coverReanalyze", root)?.addEventListener("click", () => {
    void submitCoverAnalysis(scope, id, true);
  });
  $("#coverPromote", root)?.addEventListener("click", () => {
    void submitCoverPromote(scope, id);
  });
  syncCoverDimIndeterminate(coverDimsTree(coverDraftBodyFor(scope).dims));
}

async function submitCoverAnalysis(scope, id, force = false) {
  // v5 §4：重新解析会覆盖当前文本，edited_text 非空时先显式确认。
  if (coverDraftBodyFor(scope).edited_text.trim()
    && !window.confirm("重新解析将覆盖当前文本，确认继续？")) return;
  if (!(await flushCoverDraftSave(scope, id))) return;
  const submittedDims = coverDraftBodyFor(scope).dims;
  try {
    setBusy(true);
    const result = await coverDraftApi(scope, id).analyze(force);
    const slot = coverStyleSlot(scope);
    if (result?.draft) state[slot] = { ...(state[slot] || {}), draft: result.draft };
    state.coverAnalyzedDims = submittedDims;
    if (result?.reused) {
      toast("勾选与上次解析一致，已复用既有解析文本。");
    } else {
      state.coverAnalyzingScope = coverAnalyzingKey(scope, id);
      state.coverAnalysisDeadline = Date.now() + COVER_ANALYSIS_WATCH_MS;
      toast("参考图解析任务已提交。");
    }
  } catch (error) {
    toast(messageOf(error), true);
    return;
  } finally {
    setBusy(false);
  }
  await refreshCoverScope(scope);
}

async function submitCoverPromote(scope, id) {
  if (!(await flushCoverDraftSave(scope, id))) return;
  const label = scope === "channel" ? state.coverStyleLabelDraft.trim() : "";
  try {
    setBusy(true);
    await coverDraftApi(scope, id).promote(label ? { label } : undefined);
    state.coverStyleLabelDraft = "";
    toast("已固定为频道风格新版本。");
  } catch (error) {
    toast(messageOf(error), true);
    return;
  } finally {
    setBusy(false);
  }
  await refreshCoverScope(scope);
}

function coverStyleSourceMarkup(model) {
  const source = model.styleSource;
  const option = (value, title, detail, available) => `<label class="cover-source-option ${available ? "" : "is-disabled"}">
    <input type="radio" name="cover_style_source" value="${value}" ${source.mode === value ? "checked" : ""} ${available ? "" : "disabled"} />
    <span><strong>${title}</strong><small title="${escapeHtml(detail)}">${escapeHtml(detail)}</small></span>
  </label>`;
  return `<section class="panel" id="coverSourcePanel">
    <div class="panel-head"><div><h2>风格来源</h2><p>生成画面时作为参考约束的文本来源；选择只作用于下一轮生成。</p></div><span class="status-pill ${source.mode === "none" ? "muted" : "purple"}">${source.mode === "channel" ? "频道风格" : source.mode === "project" ? "项目试验" : "无参考"}</span></div>
    <div class="panel-body">
      <div class="cover-source-options">
        ${option("channel", "频道风格", source.channel.available ? `版本 ${source.channel.version}${source.channel.label ? ` · ${escapeHtml(source.channel.label)}` : ""} · ${source.channel.textPreview || ""}` : "尚未在频道管理页固定风格", source.channel.available)}
        ${option("project", "项目试验风格", source.project.available ? source.project.textPreview || "已解析" : "下方参考图解析后可用", source.project.available)}
        ${option("none", "无参考", "不使用风格约束直接生成", true)}
      </div>
      ${source.hint ? `<div class="notice-card amber mt-12"><strong>尚无可用风格</strong><p>${escapeHtml(source.hint)}</p></div>` : ""}
    </div>
  </section>`;
}

function coverGenerateInputMarkup(model) {
  const input = model.generateInput;
  const profiles = input.channelStyleProfiles;
  const currentVersion = profiles.find((profile) => profile.isCurrent)?.version ?? model.styleSource.channel.version;
  const profileSelect = model.styleSource.mode === "channel" && profiles.length
    ? `<label class="field"><span>风格版本 <small>默认使用频道当前版本；可指定本频道任一已固定版本（仅本轮生成生效）</small></span>
      <select id="coverStyleProfileChoice">
        <option value="" ${state.coverStyleProfileChoice ? "" : "selected"}>频道当前版本${currentVersion ? `（版本 ${escapeHtml(currentVersion)}）` : ""}</option>
        ${profiles.map((profile) => `<option value="${escapeHtml(profile.profileId)}" ${state.coverStyleProfileChoice === profile.profileId ? "selected" : ""}>版本 ${escapeHtml(profile.version)}${profile.label ? ` · ${escapeHtml(profile.label)}` : ""}${profile.isCurrent ? "（当前）" : ""}</option>`).join("")}
      </select></label>`
    : "";
  return `<section class="panel" id="coverInputPanel">
    <div class="panel-head"><div><h2>生成输入</h2><p>标题来自冻结项目；补充需求与项目微调仅作用于下一轮生成。</p></div></div>
    <div class="panel-body"><div class="form-grid">
      <label class="field"><span>当前标题</span><input value="${escapeHtml(input.title)}" disabled /></label>
      <label class="field"><span>补充需求 <small>可选，≤${COVER_BRIEF_MAX} 码点，首要画面约束</small><small id="coverBriefCount" class="cover-count">${state.coverBrief.length} / ${COVER_BRIEF_MAX}</small></span><textarea id="coverBrief" rows="3" maxlength="${COVER_BRIEF_MAX}" placeholder="例如：突出前后对比，画面左侧留大字区域">${escapeHtml(state.coverBrief)}</textarea></label>
      <label class="field"><span>项目微调文本 <small>可选，≤${COVER_EDITED_TEXT_MAX} 码点，追加到风格参考约束，不改写风格草稿</small><small id="coverOverrideCount" class="cover-count">${state.coverStyleOverride.length} / ${COVER_EDITED_TEXT_MAX}</small></span><textarea id="coverStyleOverride" rows="3" maxlength="${COVER_EDITED_TEXT_MAX}" placeholder="例如：整体改为夜间办公室灯光">${escapeHtml(state.coverStyleOverride)}</textarea></label>
      ${profileSelect}
    </div></div>
  </section>`;
}

// ⑥「角色绑定」区（v5 §1.4，仅封面页）：频道角色图清单 × 卡状态 × 出场选择与顺序。
function coverCharacterBindingMarkup() {
  if (!state.coverCharacterCards) {
    return `<section class="panel"><div class="panel-head"><div><h2>角色绑定</h2><p>频道角色卡：锁定人物身份与核心外貌。</p></div></div><div class="panel-body">${renderUnavailable("角色卡端点不可用", unavailable("cover_character_cards"))}</div></section>`;
  }
  const binding = coverCharacterBindingModel({
    snapshotImageIds: coverSnapshotImageIds(state.project),
    channelImages: Array.isArray(state.coverCharacterImages) ? state.coverCharacterImages : [],
    cards: state.coverCharacterCards?.items,
    selection: state.coverCardSelection,
    analyzingCardId: state.coverCardAnalyzing,
  });
  const channelId = state.project.channel_id;
  const rows = binding.rows.map((row) => {
    const draft = state.coverCardDrafts[row.imageId] || null;
    const identity = draft?.identity_text ?? row.identityText;
    const appearance = draft?.appearance_text ?? row.appearanceText;
    const statusPill = row.analyzing
      ? `<span class="status-pill purple">解析中…</span>`
      : row.hasCard
        ? `<span class="status-pill">${row.source === "analysis" ? "已解析" : "有卡"}</span>`
        : `<span class="status-pill muted">无卡</span>`;
    return `<article class="cover-char-row" data-cover-char-row="${escapeHtml(row.imageId)}">
      <div class="cover-char-head">
        <img class="cover-char-thumb" data-protected-image="/identity/channels/${escapeHtml(channelId)}/character-images/${escapeHtml(row.imageId)}/content" alt="${escapeHtml(row.label)}" />
        <div class="cover-char-title"><strong>${escapeHtml(row.label)}</strong><small>${escapeHtml(row.meta)}</small>
          <div class="cover-char-pills">${row.inSnapshot ? `<span class="status-pill green">项目快照</span>` : ""}${statusPill}</div>
        </div>
        <label class="check cover-char-pick"><input type="checkbox" data-cover-char-pick="${escapeHtml(row.cardId || "")}" ${row.selected ? "checked" : ""} ${row.bindable && !row.analyzing ? "" : "disabled"} />出场${row.order ? ` · 第 ${row.order} 位` : ""}</label>
      </div>
      <div class="cover-char-fields">
        <label class="field"><span>身份 <small>≤${COVER_CHARACTER_IDENTITY_MAX} 码点，一句话说明角色是谁/是什么</small><small class="cover-count" data-cover-char-identity-count>${identity.length} / ${COVER_CHARACTER_IDENTITY_MAX}</small></span><input data-cover-char-identity maxlength="${COVER_CHARACTER_IDENTITY_MAX}" value="${escapeHtml(identity)}" placeholder="例如：频道 IP 小蓝，一个 Q 版机器人" ${row.analyzing ? "disabled" : ""} /></label>
        <label class="field"><span>外貌 <small>≤${COVER_CHARACTER_APPEARANCE_MAX} 码点，只写跨场景不变特征（不含服装/姿势/场景）</small><small class="cover-count" data-cover-char-appearance-count>${appearance.length} / ${COVER_CHARACTER_APPEARANCE_MAX}</small></span><textarea data-cover-char-appearance rows="2" maxlength="${COVER_CHARACTER_APPEARANCE_MAX}" placeholder="画风、头身比例、面部特征、标志性元素" ${row.analyzing ? "disabled" : ""}>${escapeHtml(appearance)}</textarea></label>
      </div>
      <div class="cover-editor-actions">
        <button class="secondary-button compact" type="button" data-cover-char-save="${escapeHtml(row.imageId)}" ${row.analyzing ? "disabled" : ""}>保存卡</button>
        ${row.hasCard ? `<button class="ghost-button compact" type="button" data-cover-char-analyze="${escapeHtml(row.cardId)}" ${row.analyzing ? "disabled" : ""}>${row.analyzing ? "解析中…" : "解析特征"}</button>` : `<small class="cover-hint">保存卡后可「解析特征」自动提取。</small>`}
        ${row.selected && binding.selectedIds.length > 1 ? `<button class="icon-button" type="button" data-cover-char-move="${escapeHtml(row.cardId)}" data-dir="-1" title="上移">↑</button><button class="icon-button" type="button" data-cover-char-move="${escapeHtml(row.cardId)}" data-dir="1" title="下移">↓</button>` : ""}
      </div>
    </article>`;
  }).join("");
  return `<section class="panel" id="coverCharacterBinding">
    <div class="panel-head"><div><h2>角色绑定</h2><p>角色卡只锁身份与核心外貌（服装/姿势由 AI 按情境设计）；首张卡逐字进入「人物身份/外貌」，第 2 张起作配角。默认绑定 = 项目快照角色图 ∩ 已有卡（按快照顺序）。</p></div><span class="status-pill ${binding.boundCount ? "purple" : "muted"}">${binding.boundCount ? `绑定 ${binding.boundCount} 张` : "未绑定"}</span></div>
    <div class="panel-body">
      ${binding.rows.length ? `<div class="cover-char-list">${rows}</div>` : `<div class="notice-card"><strong>没有可绑定的角色</strong><p>${escapeHtml(binding.emptyText)}</p></div>`}
      <div class="cover-editor-actions">
        <small class="cover-hint">${escapeHtml(binding.summaryText)}</small>
        ${binding.isDefault
          ? (binding.selectedIds.length ? `<button id="coverBindNone" class="ghost-button compact" type="button">本轮不绑定角色</button>` : "")
          : `<button id="coverBindDefault" class="ghost-button compact" type="button">恢复默认绑定</button>`}
      </div>
    </div>
  </section>`;
}

function coverPromptPanelMarkup(model) {
  const panel = model.promptPanel;
  if (!state.coverPrompts) {
    return `<section class="panel"><div class="panel-head"><div><h2>提示词面板</h2><p>封面文字候选与整段提示词。</p></div></div><div class="panel-body">${renderUnavailable("封面提示词端点不可用", unavailable("cover_prompts"))}</div></section>`;
  }
  const runningRow = [state.coverPrompts?.current, ...(Array.isArray(state.coverPrompts?.items) ? state.coverPrompts.items : [])].find(coverTaskRunning);
  const canGenerate = allows(state.coverPrompts, "GENERATE_COVER_PROMPT") && !panel.running;
  const current = panel.current;
  let currentMarkup = `<div class="notice-card"><strong>尚无提示词版本</strong><p>${escapeHtml(panel.emptyText)}</p></div>`;
  if (current) {
    const selector = current.selector;
    const promptText = state.coverPromptDraft ?? current.promptCurrent;
    const disabled = !current.succeeded || panel.running;
    const candidates = selector.candidates.length
      ? `<div class="cover-block"><strong class="cover-block-title">封面文字候选 <small>换候选立即重排提示词文字，不产生新版本</small></strong>
        <div class="cover-candidates">${selector.candidates.map((item) => `<label class="cover-candidate ${item.isSelected ? "is-selected" : ""}">
          <input type="radio" name="cover_text_choice" value="${escapeHtml(item.text)}" ${item.isSelected ? "checked" : ""} ${disabled ? "disabled" : ""} />
          <span class="cover-candidate-text">${escapeHtml(item.text)}</span>
          ${item.isRecommended ? `<span class="status-pill purple">推荐</span>` : ""}
          ${item.isUsed ? `<span class="status-pill green">画面文字</span>` : ""}
        </label>`).join("")}</div>
        ${selector.mismatch ? `<div class="notice-card amber"><strong>画面是按原文字设计的</strong><p>${escapeHtml(selector.notice)}</p></div>` : ""}
        <div class="cover-editor-actions">
          <button id="coverRegenerateWithChoice" class="secondary-button compact" type="button" ${!disabled && selector.selectedText ? "" : "disabled"}>以所选文字重新生成画面</button>
          <small class="cover-hint">以所选文字作为画面生成输入，产生新的提示词版本。</small>
        </div>
      </div>`
      : "";
    const warnings = current.warnings.length
      ? `<div class="cover-warnings">${current.warnings.map((code) => `<span class="status-pill amber">${escapeHtml(coverWarningLabel(code))}</span>`).join("")}</div>
        ${current.lowScoreGuide ? `<div class="notice-card amber mt-8"><strong>低分整改建议</strong><p>${escapeHtml(current.lowScoreGuide)}</p></div>` : ""}`
      : "";
    const cardsUsed = current.characterCardsUsed
      ? `<div class="cover-block"><strong class="cover-block-title">出场角色卡 <small>本轮生成时绑定的角色卡快照；首张逐字进入人物身份与外貌</small></strong>
        ${current.characterCardsUsed.length
          ? `<div class="sentence-list">${current.characterCardsUsed.map((card, index) => `<div class="sentence-row sentence-row-single"><span class="sentence-text"><strong>${index === 0 ? "主角" : `配角 ${index}`}</strong>：${escapeHtml(card.identityText)}${card.appearanceText ? `；${escapeHtml(card.appearanceText)}` : ""}</span></div>`).join("")}</div>`
          : `<p class="cover-hint">本轮未绑定角色卡：人物由 AI 按标题与文案自由撰写。</p>`}
      </div>`
      : "";
    const judge = current.judge
      ? `<details class="cover-judge"><summary>评委结果 · 最优为版本 ${current.judge.bestVersion}（总分 140）</summary>
        <div class="sentence-list mt-8">${current.judge.rows.map((row) => `<div class="sentence-row sentence-row-judge"><span class="sentence-text">版本 ${row.version}${row.isBest ? " · 最优" : ""}${row.comment ? `：${escapeHtml(row.comment)}` : ""}</span><span class="sentence-time">${row.total} / 140</span></div>`).join("")}</div>
        ${current.judge.advice ? `<p class="cover-hint mt-8">整改意见：${escapeHtml(current.judge.advice)}</p>` : ""}
      </details>`
      : "";
    currentMarkup = `${current.failure ? `<div class="notice-card error"><strong>提示词生成失败</strong><p>${escapeHtml(coverFailureText(current.failure) || "后端已返回失败状态。")}</p></div>` : ""}
      ${candidates}
      ${cardsUsed}
      <label class="field"><span>整段提示词 <small>编辑只影响当前版本；出图以此为准</small><small id="coverPromptCount" class="cover-count">${promptText.length} / ${COVER_PROMPT_MAX}</small></span>
        <textarea id="coverPromptText" rows="10" maxlength="${COVER_PROMPT_MAX}" ${disabled ? "disabled" : ""}>${escapeHtml(promptText)}</textarea></label>
      <div class="cover-editor-actions">
        <span id="coverPromptEditedBadge" class="status-pill ${current.edited ? "purple" : "muted"}">${current.edited ? "编辑版" : "系统版"}</span>
        <button id="coverPromptReset" class="ghost-button compact" type="button" ${!disabled && current.edited ? "" : "disabled"}>重置为系统版</button>
        <small id="coverPromptSaveStatus" class="cover-save-status"></small>
      </div>
      ${warnings}
      ${judge}`;
  }
  return `<section class="panel" id="coverPromptPanel">
    <div class="panel-head"><div><h2>提示词面板</h2><p>串行生成封面文字与画面提示词；两版并行、评委择优。</p></div>
      <button id="coverGeneratePrompt" class="primary-button compact" type="button" ${canGenerate ? "" : "disabled"}>${runningRow ? `${escapeHtml(statusText(runningRow))}…` : "生成封面提示词"}</button></div>
    <div class="panel-body">
      ${currentMarkup}
      <strong class="cover-block-title mt-14">版本历史</strong>
      ${coverVersionListMarkup(panel.versions, "prompt")}
    </div>
  </section>`;
}

function coverImageReviewMarkup(review) {
  if (!review) return "";
  if (review.status === "failed") {
    return `<div class="notice-card"><strong>视觉复审未完成</strong><p>复审调用失败（${escapeHtml(review.code)}）；出图与归档不受影响，本版本未获得内容复审结论。</p></div>`;
  }
  const rows = review.issues.map((issue) => `<li>[${issue.severity === "blocker" ? "需修复" : "建议"}] ${escapeHtml(issue.description)}${issue.suggestedEdit ? `<br><small>修复建议：${escapeHtml(issue.suggestedEdit)}</small>` : ""}</li>`).join("");
  const title = review.ok
    ? "视觉复审通过：未发现事实/交付错误"
    : `视觉复审：${review.blockerCount} 个需修复问题（不影响出图与当前版本，供你决定是否重新生成）`;
  return `<div class="notice-card ${review.ok ? "" : "error"}"><strong>${escapeHtml(title)}</strong>${rows ? `<ul>${rows}</ul>` : ""}</div>`;
}

function coverImagePanelMarkup(model) {
  const panel = model.imagePanel;
  if (!state.coverImages) {
    return `<section class="panel"><div class="panel-head"><div><h2>封面图</h2><p>按当前面板提示词出图。</p></div></div><div class="panel-body">${renderUnavailable("封面图端点不可用", unavailable("cover_images"))}</div></section>`;
  }
  const runningRow = [state.coverImages?.current, ...(Array.isArray(state.coverImages?.items) ? state.coverImages.items : [])].find(coverTaskRunning);
  const promptCurrent = state.coverPrompts?.current || null;
  const canGenerate = allows(state.coverImages, "GENERATE_COVER_IMAGE")
    && allows(promptCurrent, "GENERATE_COVER_IMAGE")
    && panel.canGenerate;
  const canUpload = allows(state.coverImages, "UPLOAD_COVER_IMAGE")
    && !state.coverImageUploading;
  const current = panel.current;
  const preview = current?.mediaUrl
    ? `<figure class="media-preview"><img id="coverCurrentImage" alt="当前封面图" /><figcaption id="coverPreviewCaption">当前封面图 · 版本 ${escapeHtml(current.version)} · ${escapeHtml(current.meta)}</figcaption></figure>`
    : `<div class="notice-card"><strong>当前没有封面图</strong><p>${escapeHtml(panel.emptyText)}</p></div>`;
  return `<section class="panel" id="coverImagePanel">
    <div class="panel-head"><div><h2>封面图</h2><p>生成 2560×1440 封面，或上传 png/jpeg/webp 图片（≤25MB，≤4000 万像素，原样归档，不限尺寸与宽高比）；不可变版本归档，上传成功即设为当前，生成首次成功自动设为当前。</p></div>
      <div class="panel-head-actions">
        <button id="coverUploadImage" class="secondary-button compact" type="button" ${canUpload ? "" : "disabled"}>${state.coverImageUploading ? "上传中…" : "上传封面图"}</button>
        <button id="coverGenerateImage" class="primary-button compact" type="button" ${canGenerate ? "" : "disabled"}>${runningRow ? `${escapeHtml(statusText(runningRow))}…` : "生成封面图"}</button>
        <input id="coverUploadImageFile" type="file" accept="image/png,image/jpeg,image/webp" hidden />
      </div></div>
    <div class="panel-body">
      ${current?.failure ? `<div class="notice-card error"><strong>封面图生成失败</strong><p>${escapeHtml(coverFailureText(current.failure) || "后端已返回失败状态。")}</p></div>` : ""}
      ${preview}
      ${coverImageReviewMarkup(current?.review)}
      <strong class="cover-block-title mt-14">版本历史</strong>
      ${coverVersionListMarkup(panel.versions, "image")}
    </div>
  </section>`;
}

function coverVersionListMarkup(versions, kind) {
  if (!versions.length) {
    return `<div class="empty-state is-compact"><div><b>版</b><h3>没有历史版本</h3><p>生成或上传成功后这里会列出不可变版本。</p></div></div>`;
  }
  return `<div class="cover-version-list">${versions.map((item) => {
    const origin = item.source === "upload" ? "上传 · " : "";
    const detail = `${origin}${statusText({ public_status: item.status })}${item.failure ? ` · ${coverFailureText(item.failure)}` : ""}${item.createdAt ? ` · ${formatDateTime(item.createdAt)}` : ""}`;
    const actions = kind === "image"
      ? `${item.mediaUrl ? `<button class="ghost-button compact" data-cover-image-preview="${escapeHtml(item.id)}" type="button">预览</button>` : ""}<button class="secondary-button compact" data-cover-image-select="${escapeHtml(item.id)}" type="button" ${item.selectable ? "" : "disabled"}>${item.isCurrent ? "当前版本" : "切换"}</button>`
      : kind === "prompt" && item.switchable
        ? `<button class="secondary-button compact" data-cover-prompt-select="${escapeHtml(item.id)}" type="button">切换</button>`
        : "";
    return `<article class="version-item ${item.isCurrent ? "is-current" : ""}">
      <div><strong>版本 ${escapeHtml(item.version)}${item.label ? ` · ${escapeHtml(item.label)}` : ""}${item.isCurrent ? " · 当前" : ""}</strong><small>${escapeHtml(detail)}</small></div>
      ${actions ? `<div class="version-actions">${actions}</div>` : ""}
    </article>`;
  }).join("")}</div>`;
}

async function renderCoverStage() {
  const epoch = state.renderEpoch;
  const project = state.project;
  const fact = stageFact("cover");
  if (coverLockModel(project, fact).locked) {
    state.coverStyle = null;
    state.coverChannelStyle = null;
    state.coverPrompts = null;
    state.coverImages = null;
    state.coverCharacterCards = null;
    state.coverCharacterImages = null;
    renderCoverStageView();
    return;
  }
  try {
    const facts = await fetchCoverFacts(project.id, project.channel_id);
    if (epoch !== state.renderEpoch) return;
    applyCoverFacts(facts);
    renderCoverStageView();
  } catch (error) {
    if (epoch === state.renderEpoch) {
      $("#studioMainInner").innerHTML = renderUnavailable("封面数据读取失败", messageOf(error));
    }
  }
}

function renderCoverStageView() {
  const project = state.project;
  const fact = stageFact("cover");
  const model = coverPageModel({
    fact,
    project,
    channelStyle: state.coverChannelStyle,
    projectStyle: state.coverStyle,
    prompts: state.coverPrompts,
    images: state.coverImages,
    analyzing: coverAnalyzing("project", project?.id),
    styleSource: state.coverStyleSource,
    textChoice: state.coverTextChoice,
    analyzedDims: coverAnalyzedDimsFor("project"),
  });
  if (model.locked) {
    $("#studioMainInner").innerHTML = `${studioHeading("STEP 10 · COVER", "参考图解析 → 封面文字 → 画面提示词 → 出图。")}
      <div class="notice-card amber"><strong>${escapeHtml(model.lockTitle)}</strong><p>${escapeHtml(model.lockDetail)}</p></div>`;
    return;
  }
  coverEditorMount("project", project.id);
  const sourceLabel = model.styleSource.mode === "channel" ? "频道风格" : model.styleSource.mode === "project" ? "项目试验" : "无参考";
  // 轮询会整块重绘：保留正在输入的控件与光标位置（模板文本、补充需求等）。
  const active = document.activeElement;
  const restoreId = active instanceof HTMLElement && active.id && $("#studioMainInner")?.contains(active)
    ? active.id
    : "";
  const restoreSelection = active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement
    ? active.selectionStart
    : null;
  $("#studioMainInner").innerHTML = `${studioHeading("STEP 10 · COVER", "参考图解析 → 封面文字 → 画面提示词 → 出图。", `<button id="refreshCover" class="ghost-button compact" type="button">刷新</button>`)}
    <div class="real-status-grid">
      <article><span>风格来源</span><strong>${sourceLabel}</strong><small>${model.styleSource.channel.available ? `频道版本 ${model.styleSource.channel.version}` : "未固定频道风格"}</small></article>
      <article><span>提示词</span><strong>${model.promptPanel.current ? `版本 ${escapeHtml(model.promptPanel.current.version)}` : "未生成"}</strong><small>${model.promptPanel.current ? escapeHtml(statusText({ public_status: model.promptPanel.current.status })) : "—"}</small></article>
      <article><span>当前封面</span><strong>${model.imagePanel.current ? `版本 ${escapeHtml(model.imagePanel.current.version)}` : "无当前封面"}</strong><small>${model.imagePanel.current ? escapeHtml(statusText({ public_status: model.imagePanel.current.status })) : "—"}</small></article>
      <article><span>阶段状态</span><strong>${escapeHtml(statusText(fact))}</strong><small id="coverAutoRefresh">${coverNeedsPolling() ? "自动更新中" : "当前任务已停止自动更新，可手动刷新。"}</small></article>
    </div>
    ${coverTemplateBlockMarkup()}
    ${coverMoreSettingsMarkup({
      open: coverMoreSettingsOpen(readCoverMoreSetting(project.id)),
      body: `<div class="anchor-chips">${[["coverSourcePanel", "风格来源"], ["coverRefPanel", "参考图与勾选"], ["coverInputPanel", "生成输入"], ["coverCharacterBinding", "出场角色卡"], ["coverPromptPanel", "提示词"], ["coverImagePanel", "封面图"]].map(([id, label]) => `<button class="anchor-chip" type="button" data-cover-anchor="${id}">${label}</button>`).join("")}</div>
        ${coverStyleSourceMarkup(model)}
        <section class="panel" id="coverRefPanel">
          <div class="panel-head"><div><h2>参考图与勾选</h2><p>上传参考图、勾选解析维度并解析；解析文本可编辑，防抖自动保存。</p></div><span class="status-pill ${model.refEditor.hasAnalysis ? "" : "muted"}">${model.refEditor.analyzing ? "解析中" : model.refEditor.hasAnalysis ? "已解析" : "未解析"}</span></div>
          <div class="panel-body">${coverRefEditorMarkup({ scope: "project", style: state.coverStyle, analyzing: model.refEditor.analyzing })}</div>
        </section>
        ${coverGenerateInputMarkup(model)}
        ${coverCharacterBindingMarkup()}
        ${coverPromptPanelMarkup(model)}`,
    })}
    ${coverImagePanelMarkup(model)}`;
  if (restoreId) {
    const restored = document.getElementById(restoreId);
    if (restored instanceof HTMLElement) {
      restored.focus({ preventScroll: true });
      if (restoreSelection != null && "setSelectionRange" in restored) {
        try {
          restored.setSelectionRange(restoreSelection, restoreSelection);
        } catch {
          // 非文本控件不支持选区，忽略。
        }
      }
    }
  }
  bindCoverStageEvents(model);
  if (model.imagePanel.current?.mediaUrl && $("#coverCurrentImage")) {
    setNativeMediaSource($("#coverCurrentImage"), api.coverImageUrl(project.id, model.imagePanel.current.generationId), "封面图");
  }
  hydrateProtectedImages($("#studioMainInner"));
  hydrateCoverRefImages($("#studioMainInner"), "project", project.id);
  warmCoverTemplateOriginal();
  scheduleCoverPolling();
}

// ---------------- 临时功能：频道级多模板（2026-10-27） ----------------
// 删除本功能时：整块移除；删 state 里的 7 个 coverTemplate* 字段与重置行；
// 删 coverTemplateBlockMarkup 调用、ensureCoverEditsSaved 里的守卫、beforeunload 监听；
// 删 frontend/cover-templates.js、api-client 的模板方法、styles.css 的 cover-template 段。
let coverTemplateOriginalCache = {};
let coverTemplateOriginalLoading = {};
let coverTemplateEscBound = false;
let coverTemplateReordering = false;

function coverTemplateItemById(id) {
  return (state.coverTemplates?.items || []).find((item) => item.id === id) || null;
}

function coverTemplateDraft(item) {
  return state.coverTemplateDrafts[item.id] || templateDraftFromItem(item);
}

function coverTemplateStatuses() {
  const map = {};
  for (const item of state.coverTemplates?.items || []) {
    map[item.id] = templateSaveStatus({
      dirty: Boolean(state.coverTemplateDirty[item.id]),
      saving: Boolean(state.coverTemplateSaving[item.id]),
      error: state.coverTemplateErrors[item.id] || "",
      savedAt: state.coverTemplateSavedClock[item.id] || formatSavedClock(item.updatedAt),
    });
  }
  return map;
}

function readCoverMoreSetting(projectId) {
  try {
    return localStorage.getItem(coverMoreSettingsKey(projectId)) || "";
  } catch {
    return "";
  }
}

function writeCoverMoreSetting(projectId, open) {
  try {
    localStorage.setItem(coverMoreSettingsKey(projectId), open ? "open" : "closed");
  } catch {
    // 隐私模式下 localStorage 不可写：仅本次会话生效，不影响功能。
  }
}

function setCoverMoreOpen(open) {
  const body = $("#coverMoreBody");
  const toggle = $("#coverMoreToggle");
  if (!body || !toggle) return;
  body.hidden = !open;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  const chevron = toggle.querySelector(".cover-more-chevron");
  if (chevron) chevron.textContent = open ? "▾" : "▸";
  writeCoverMoreSetting(state.project?.id, open);
}

function openCoverMoreSettings() {
  const body = $("#coverMoreBody");
  if (body?.hidden) setCoverMoreOpen(true);
}

function bindCoverMoreSettings() {
  const toggle = $("#coverMoreToggle");
  const body = $("#coverMoreBody");
  if (!toggle || !body) return;
  toggle.addEventListener("click", () => setCoverMoreOpen(body.hidden));
}

function coverTemplateBlockMarkup() {
  if (!state.coverTemplates) return "";
  return coverTemplatesMarkup(
    state.coverTemplates,
    state.coverTemplateDrafts,
    state.coverTemplateDirty,
    coverTemplateStatuses(),
  );
}

function paintCoverTemplateStatus(id) {
  const card = document.querySelector(`[data-template-id="${id}"]`);
  const node = card?.querySelector("[data-template-status]");
  if (!node) return;
  const status = coverTemplateStatuses()[id] || templateSaveStatus({});
  node.textContent = status.text;
  node.className = `cover-template-status is-${status.tone}`;
}

function paintAllCoverTemplateStatuses() {
  for (const item of state.coverTemplates?.items || []) paintCoverTemplateStatus(item.id);
}

function markCoverTemplateDirty(id) {
  const item = coverTemplateItemById(id);
  if (!item) return;
  const draft = coverTemplateDraft(item);
  if (templateDraftChanged(item, draft)) state.coverTemplateDirty[id] = true;
  else delete state.coverTemplateDirty[id];
  paintCoverTemplateStatus(id);
}

function replaceCoverTemplateItem(next) {
  if (!state.coverTemplates) return;
  state.coverTemplates.items = state.coverTemplates.items.map((item) => (
    item.id === next.id ? next : item
  ));
}

function coverTemplateDirtyCount() {
  return Object.keys(state.coverTemplateDirty).length;
}

// 三个选项：保存并继续 / 放弃修改 / 取消。
function coverTemplateUnsavedModal(count) {
  return new Promise((resolve) => {
    $("#coverTemplateUnsavedOverlay")?.remove();
    app.insertAdjacentHTML("beforeend", `<div id="coverTemplateUnsavedOverlay" class="modal-backdrop">
      <section class="modal" role="dialog" aria-modal="true" aria-label="模板有未保存修改">
        <div class="modal-head"><div><span class="eyebrow">UNSAVED TEMPLATES</span><h2>模板有未保存修改</h2></div><button id="coverTemplateUnsavedClose" class="modal-close" type="button">×</button></div>
        <p>有 ${count} 个模板的名称或提示词尚未保存。保存后继续，或放弃这些修改。</p>
        <div class="modal-actions">
          <button id="coverTemplateUnsavedCancel" class="ghost-button" type="button">取消</button>
          <button id="coverTemplateUnsavedDiscard" class="ghost-button" type="button">放弃修改</button>
          <button id="coverTemplateUnsavedSave" class="primary-button" type="button">保存并继续</button>
        </div>
      </section>
    </div>`);
    const overlay = $("#coverTemplateUnsavedOverlay");
    const restoreFocus = manageDialogFocus(overlay);
    let settled = false;
    const finish = (choice) => {
      if (settled) return;
      settled = true;
      overlay.remove();
      restoreFocus();
      resolve(choice);
    };
    overlay.closeOverlay = () => finish("cancel");
    $("#coverTemplateUnsavedClose").addEventListener("click", () => finish("cancel"));
    $("#coverTemplateUnsavedCancel").addEventListener("click", () => finish("cancel"));
    $("#coverTemplateUnsavedDiscard").addEventListener("click", () => finish("discard"));
    $("#coverTemplateUnsavedSave").addEventListener("click", () => finish("save"));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) finish("cancel");
    });
  });
}

async function ensureCoverTemplateEditsSaved() {
  if (!coverTemplateDirtyCount()) return true;
  const choice = await coverTemplateUnsavedModal(coverTemplateDirtyCount());
  if (choice === "cancel") return false;
  if (choice === "discard") {
    state.coverTemplateDirty = {};
    state.coverTemplateErrors = {};
    for (const id of Object.keys(state.coverTemplateDrafts)) {
      const item = coverTemplateItemById(id);
      if (item) state.coverTemplateDrafts[id] = templateDraftFromItem(item);
    }
    paintAllCoverTemplateStatuses();
    return true;
  }
  return saveAllCoverTemplates();
}

async function saveAllCoverTemplates() {
  for (const id of Object.keys(state.coverTemplateDirty)) {
    if (!(await saveCoverTemplateItem(id))) return false;
  }
  return true;
}

async function saveCoverTemplateItem(id) {
  const item = coverTemplateItemById(id);
  const channelId = state.project?.channel_id;
  if (!item || !channelId) return false;
  if (state.coverTemplateSaving[id]) return false;
  const draft = coverTemplateDraft(item);
  if (!templateDraftChanged(item, draft)) {
    delete state.coverTemplateDirty[id];
    paintCoverTemplateStatus(id);
    return true;
  }
  state.coverTemplateSaving[id] = true;
  delete state.coverTemplateErrors[id];
  paintCoverTemplateStatus(id);
  const payload = {
    name: clipTemplateName(draft.name),
    prompt: clipTemplatePrompt(draft.prompt),
  };
  const key = createIdempotencyKey();
  let saved = null;
  for (let attempt = 0; attempt <= 2; attempt += 1) {
    try {
      const response = await api.saveChannelCoverTemplateItem(channelId, id, payload, key);
      saved = coverTemplateItemModel(response);
      break;
    } catch (error) {
      const retryable = !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
      if (attempt === 2 || !retryable) {
        state.coverTemplateErrors[id] = messageOf(error);
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000 * (attempt + 1)));
    }
  }
  delete state.coverTemplateSaving[id];
  if (!saved) {
    paintCoverTemplateStatus(id);
    return false;
  }
  replaceCoverTemplateItem(saved);
  state.coverTemplateDrafts[id] = templateDraftFromItem(saved);
  delete state.coverTemplateDirty[id];
  delete state.coverTemplateErrors[id];
  state.coverTemplateSavedClock[id] = formatSavedClock(new Date());
  const nameInput = document.querySelector(`[data-template-name="${id}"]`);
  if (nameInput instanceof HTMLInputElement && nameInput.value !== saved.name) {
    nameInput.value = saved.name;
  }
  const promptArea = document.querySelector(`[data-template-prompt="${id}"]`);
  if (promptArea instanceof HTMLTextAreaElement && promptArea.value !== saved.prompt) {
    promptArea.value = saved.prompt;
  }
  paintCoverTemplateStatus(id);
  return true;
}

async function createCoverTemplate() {
  const channelId = state.project?.channel_id;
  if (!channelId) return;
  try {
    setBusy(true);
    const response = await api.createChannelCoverTemplateItem(channelId);
    const item = coverTemplateItemModel(response);
    if (state.coverTemplates) state.coverTemplates.items.push(item);
    state.coverTemplateDrafts[item.id] = templateDraftFromItem(item);
    renderCoverStageView();
    requestAnimationFrame(() => {
      const input = document.querySelector(`[data-template-name="${item.id}"]`);
      input?.scrollIntoView({ behavior: "smooth", block: "center" });
      input?.focus({ preventScroll: true });
      input?.select();
    });
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function deleteCoverTemplate(id) {
  const items = state.coverTemplates?.items || [];
  const item = coverTemplateItemById(id);
  const channelId = state.project?.channel_id;
  if (!item || !channelId) return;
  if (items.length <= 1) {
    toast("至少保留一个模板。", true);
    return;
  }
  const dirty = Boolean(state.coverTemplateDirty[id]);
  const body = dirty
    ? `<div class="notice-card amber"><strong>该模板有未保存的修改</strong><p>删除后这些修改与封面图都会消失，不可恢复。</p></div>`
    : `<div class="notice-card error"><strong>删除「${escapeHtml(item.name)}」</strong><p>封面图会一并删除，不可恢复。</p></div>`;
  const result = await actionModal({
    title: `删除「${item.name}」`,
    danger: true,
    confirmText: dirty ? "放弃修改并删除" : "删除模板",
    bodyHtml: body,
  });
  if (!result.confirmed) return;
  try {
    setBusy(true);
    await api.deleteChannelCoverTemplateItem(channelId, id);
    state.coverTemplates.items = items.filter((row) => row.id !== id);
    delete state.coverTemplateDrafts[id];
    delete state.coverTemplateDirty[id];
    delete state.coverTemplateSaving[id];
    delete state.coverTemplateErrors[id];
    delete state.coverTemplateSavedClock[id];
    renderCoverStageView();
    toast("模板已删除。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function uploadCoverTemplateFile(id, file) {
  const channelId = state.project?.channel_id;
  if (!file || !channelId) return;
  setBusy(true);
  try {
    const response = await api.uploadChannelCoverTemplateItem(channelId, id, file);
    replaceCoverTemplateItem(coverTemplateItemModel(response));
    renderCoverStageView();
    toast("封面已更新。");
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
    const input = document.querySelector(`[data-template-file="${id}"]`);
    if (input) input.value = "";
  }
}

async function ensureCoverTemplateOriginal(id) {
  const item = coverTemplateItemById(id);
  const channelId = state.project?.channel_id;
  if (!item?.cover || !channelId) return "";
  const cached = coverTemplateOriginalCache[id];
  if (cached?.sha === item.cover.sha256 && cached.url) return cached.url;
  if (coverTemplateOriginalLoading[id]) return "";
  coverTemplateOriginalLoading[id] = true;
  let objectUrl = "";
  try {
    const blob = await api.channelCoverTemplateOriginalBlob(channelId, id);
    objectUrl = URL.createObjectURL(blob);
    await new Promise((resolve, reject) => {
      const preload = new Image();
      preload.onload = () => resolve(null);
      preload.onerror = () => reject(new Error("cover template original failed to decode"));
      preload.src = objectUrl;
    });
    if (cached?.url) URL.revokeObjectURL(cached.url);
    coverTemplateOriginalCache[id] = { sha: item.cover.sha256, url: objectUrl };
    return objectUrl;
  } catch (error) {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    throw error;
  } finally {
    coverTemplateOriginalLoading[id] = false;
  }
}

function warmCoverTemplateOriginal() {
  for (const item of state.coverTemplates?.items || []) {
    if (!item.cover) continue;
    if (coverTemplateOriginalCache[item.id]?.sha === item.cover.sha256) continue;
    setTimeout(() => {
      void ensureCoverTemplateOriginal(item.id).catch(() => {});
    }, 300);
  }
}

// 局域网 HTTP 下写不了剪贴板位图；用「选中图片 + execCommand('copy')」复制，
// 不支持时打开原图并提示右键复制，绝不伪造成功。
function copyImageUrlToClipboard(url) {
  if (!url) return false;
  const holder = document.createElement("div");
  holder.contentEditable = "true";
  holder.setAttribute("aria-hidden", "true");
  holder.style.cssText = "position:fixed;left:-10000px;top:0;width:1px;height:1px;overflow:hidden;opacity:0;";
  const image = document.createElement("img");
  image.src = url;
  holder.appendChild(image);
  document.body.appendChild(holder);
  let copied = false;
  try {
    if (image.complete && image.naturalWidth > 0) {
      const range = document.createRange();
      range.selectNode(image);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      copied = document.execCommand("copy");
      selection.removeAllRanges();
    }
  } catch {
    copied = false;
  } finally {
    holder.remove();
  }
  return copied;
}

function flashCoverTemplateCopied(button) {
  if (!(button instanceof HTMLButtonElement)) return;
  const original = button.textContent;
  button.textContent = "已复制 ✓";
  button.disabled = true;
  setTimeout(() => {
    if (!button.isConnected) return;
    button.textContent = original;
    button.disabled = false;
  }, 1500);
}

function copyCoverTemplateTextValue(text, button) {
  if (!String(text || "").trim()) {
    toast("没有可复制的内容", true);
    return;
  }
  const done = () => flashCoverTemplateCopied(button);
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done, () => {
      if (legacyCopyText(text)) done();
      else toast("复制失败，请手动选择文本复制", true);
    });
    return;
  }
  if (legacyCopyText(text)) done();
  else toast("复制失败，请手动选择文本复制", true);
}

async function copyCoverTemplateText(id, button) {
  if (state.coverTemplateDirty[id] && !(await saveCoverTemplateItem(id))) {
    toast("保存失败，未复制。", true);
    return;
  }
  const item = coverTemplateItemById(id);
  if (!item) return;
  copyCoverTemplateTextValue(item.prompt, button);
}

async function copyCoverTemplateImage(id, button) {
  const item = coverTemplateItemById(id);
  if (!item?.cover) return;
  const cached = coverTemplateOriginalCache[id];
  if (cached?.sha === item.cover.sha256 && copyImageUrlToClipboard(cached.url)) {
    flashCoverTemplateCopied(button);
    return;
  }
  try {
    const url = await ensureCoverTemplateOriginal(id);
    if (url && copyImageUrlToClipboard(url)) {
      flashCoverTemplateCopied(button);
      return;
    }
  } catch {
    // 落到打开原图的手动复制路径。
  }
  window.open(item.cover.originalUrl, "_blank", "noopener");
  toast("已打开原图，请在图片上右键选择「复制图片」。");
}

async function openCoverTemplateLightbox(id) {
  const item = coverTemplateItemById(id);
  const box = $("#coverTemplateLightbox");
  if (!item?.cover || !box) return;
  box.hidden = false;
  const name = $("#coverTemplateLightboxName");
  if (name) name.textContent = item.name || "模板封面";
  const meta = $("#coverTemplateLightboxMeta");
  if (meta) meta.textContent = item.cover.metaText;
  const image = $("#coverTemplateLightboxImage");
  if (!image) return;
  image.src = item.cover.previewUrl;
  try {
    const original = await ensureCoverTemplateOriginal(id);
    if (original) image.src = original;
  } catch {
    // 原图读取失败时保留压缩图预览。
  }
}

function closeCoverTemplateLightbox() {
  const box = $("#coverTemplateLightbox");
  if (!box) return;
  box.hidden = true;
  const image = $("#coverTemplateLightboxImage");
  if (image) image.removeAttribute("src");
}

async function reorderCoverTemplates(fromId, toId, placeAfter) {
  if (!state.coverTemplates || coverTemplateReordering || fromId === toId) return;
  const previous = [...state.coverTemplates.items];
  const next = moveTemplateItem(previous, fromId, toId, placeAfter);
  if (next.every((item, index) => item.id === previous[index]?.id)) return;
  state.coverTemplates.items = next;
  state.coverTemplateDragging = null;
  coverTemplateReordering = true;
  renderCoverStageView();
  try {
    const response = await api.reorderChannelCoverTemplateItems(
      state.project.channel_id,
      next.map((item) => item.id),
    );
    state.coverTemplates = coverTemplateCollectionModel(response);
  } catch (error) {
    state.coverTemplates.items = previous;
    toast(messageOf(error), true);
  } finally {
    coverTemplateReordering = false;
    renderCoverStageView();
  }
}

function bindCoverTemplateDrag() {
  const list = $("#coverTemplatesPanel .cover-template-list");
  for (const card of $$("[data-template-id]")) {
    const handle = card.querySelector("[data-template-drag]");
    handle?.addEventListener("mousedown", () => {
      card.draggable = true;
    });
    card.addEventListener("mouseup", () => {
      card.draggable = false;
    });
    card.addEventListener("dragstart", (event) => {
      state.coverTemplateDragging = card.dataset.templateId;
      card.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.templateId || "");
    });
    card.addEventListener("dragend", () => {
      card.draggable = false;
      card.classList.remove("is-dragging");
      state.coverTemplateDragging = null;
      for (const row of $$("[data-template-id]")) {
        row.classList.remove("is-drop-target", "is-drop-after");
      }
    });
    card.addEventListener("dragover", (event) => {
      if (!state.coverTemplateDragging) return;
      event.preventDefault();
      const rect = card.getBoundingClientRect();
      card.classList.toggle("is-drop-after", event.clientY > rect.top + rect.height / 2);
      card.classList.add("is-drop-target");
    });
    card.addEventListener("dragleave", () => {
      card.classList.remove("is-drop-target", "is-drop-after");
    });
    card.addEventListener("drop", (event) => {
      const fromId = state.coverTemplateDragging;
      if (!fromId) return;
      event.preventDefault();
      const rect = card.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      card.classList.remove("is-drop-target", "is-drop-after");
      void reorderCoverTemplates(fromId, card.dataset.templateId, after);
    });
  }
  list?.addEventListener("dragover", (event) => {
    if (state.coverTemplateDragging) event.preventDefault();
  });
  list?.addEventListener("drop", (event) => {
    const fromId = state.coverTemplateDragging;
    if (!fromId) return;
    if (event.target instanceof Element && event.target.closest("[data-template-id]")) return;
    event.preventDefault();
    const items = state.coverTemplates?.items || [];
    const last = items[items.length - 1];
    if (last) void reorderCoverTemplates(fromId, last.id, true);
  });
}

function bindCoverTemplateEvents() {
  bindCoverTemplateDrag();
  $("#coverTemplateAdd")?.addEventListener("click", () => {
    void createCoverTemplate();
  });
  if (!state.coverTemplates) return;
  $("#coverTemplateLightboxClose")?.addEventListener("click", closeCoverTemplateLightbox);
  $("#coverTemplateLightbox")?.addEventListener("click", (event) => {
    if (event.target.id === "coverTemplateLightbox") closeCoverTemplateLightbox();
  });
  if (!coverTemplateEscBound) {
    coverTemplateEscBound = true;
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const box = $("#coverTemplateLightbox");
      if (box && !box.hidden) closeCoverTemplateLightbox();
    });
  }
  for (const item of state.coverTemplates.items) {
    const id = item.id;
    $(`[data-template-delete="${id}"]`)?.addEventListener("click", () => {
      void deleteCoverTemplate(id);
    });
    $(`[data-template-save="${id}"]`)?.addEventListener("click", () => {
      void saveCoverTemplateItem(id);
    });
    $(`[data-template-copy="${id}"]`)?.addEventListener("click", (event) => {
      void copyCoverTemplateText(id, event.currentTarget);
    });
    $(`[data-template-copy-image="${id}"]`)?.addEventListener("click", (event) => {
      void copyCoverTemplateImage(id, event.currentTarget);
    });
    $(`[data-template-open="${id}"]`)?.addEventListener("click", () => {
      void openCoverTemplateLightbox(id);
    });
    $(`[data-template-upload="${id}"]`)?.addEventListener("click", () => {
      document.querySelector(`[data-template-file="${id}"]`)?.click();
    });
    $(`[data-template-file="${id}"]`)?.addEventListener("change", (event) => {
      const file = event.target.files?.[0];
      void uploadCoverTemplateFile(id, file);
    });
    $(`[data-template-name="${id}"]`)?.addEventListener("input", (event) => {
      const current = coverTemplateItemById(id);
      if (!current) return;
      state.coverTemplateDrafts[id] = { ...coverTemplateDraft(current), name: event.target.value };
      markCoverTemplateDirty(id);
    });
    $(`[data-template-prompt="${id}"]`)?.addEventListener("input", (event) => {
      const current = coverTemplateItemById(id);
      if (!current) return;
      state.coverTemplateDrafts[id] = { ...coverTemplateDraft(current), prompt: event.target.value };
      const counter = event.target.closest(".cover-template-text")?.querySelector("[data-template-count]");
      if (counter) {
        counter.textContent = `${countTemplatePrompt(event.target.value)} / ${COVER_TEMPLATE_PROMPT_LIMIT}`;
      }
      markCoverTemplateDirty(id);
    });
    const statusNode = document.querySelector(`[data-template-id="${id}"] [data-template-status]`);
    statusNode?.addEventListener("click", () => {
      if (state.coverTemplateErrors[id]) void saveCoverTemplateItem(id);
    });
  }
}

// 关闭/刷新只能弹浏览器原生提示（浏览器不允许自定义按钮）。
window.addEventListener("beforeunload", (event) => {
  if (!coverTemplateDirtyCount()) return;
  event.preventDefault();
  event.returnValue = "";
});

function bindCoverStageEvents(model) {
  const projectId = state.project.id;
  $$("[data-cover-anchor]").forEach((button) => {
    button.addEventListener("click", () => {
      openCoverMoreSettings();
      requestAnimationFrame(() => {
        document.getElementById(button.dataset.coverAnchor)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  });
  bindCoverMoreSettings();
  bindCoverTemplateEvents();
  $("#refreshCover")?.addEventListener("click", async () => {
    await ensureCoverEditsSaved();
    await reloadCoverFactsAndRender();
  });
  bindCoverRefEditor({ scope: "project", id: projectId });
  $$('input[name="cover_style_source"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) state.coverStyleSource = radio.value;
    });
  });
  const brief = $("#coverBrief");
  brief?.addEventListener("input", () => {
    state.coverBrief = brief.value;
    const counter = $("#coverBriefCount");
    if (counter) counter.textContent = `${brief.value.length} / ${COVER_BRIEF_MAX}`;
  });
  const overrideInput = $("#coverStyleOverride");
  overrideInput?.addEventListener("input", () => {
    state.coverStyleOverride = overrideInput.value;
    const counter = $("#coverOverrideCount");
    if (counter) counter.textContent = `${overrideInput.value.length} / ${COVER_EDITED_TEXT_MAX}`;
  });
  $("#coverGeneratePrompt")?.addEventListener("click", () => {
    void submitCoverPromptGeneration(null);
  });
  $$('input[name="cover_text_choice"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) void chooseCoverText(radio.value);
    });
  });
  $("#coverRegenerateWithChoice")?.addEventListener("click", () => {
    const selected = $('input[name="cover_text_choice"]:checked')?.value
      || model.promptPanel.current?.selector.selectedText
      || "";
    if (!selected) {
      toast("请先选择封面文字候选。", true);
      return;
    }
    void submitCoverPromptGeneration(selected);
  });
  const promptText = $("#coverPromptText");
  promptText?.addEventListener("input", () => {
    state.coverPromptDraft = promptText.value;
    const counter = $("#coverPromptCount");
    if (counter) counter.textContent = `${promptText.value.length} / ${COVER_PROMPT_MAX}`;
    scheduleCoverPromptSave();
  });
  $("#coverPromptReset")?.addEventListener("click", () => {
    void resetCoverPrompt();
  });
  $("#coverGenerateImage")?.addEventListener("click", () => {
    void submitCoverImageGeneration();
  });
  $("#coverUploadImage")?.addEventListener("click", () => {
    $("#coverUploadImageFile")?.click();
  });
  $("#coverUploadImageFile")?.addEventListener("change", (event) => {
    void uploadCoverImageFile(event.target.files?.[0]);
  });
  const styleProfileChoice = $("#coverStyleProfileChoice");
  styleProfileChoice?.addEventListener("change", () => {
    state.coverStyleProfileChoice = styleProfileChoice.value || null;
  });
  $$("[data-cover-prompt-select]").forEach((button) => {
    button.addEventListener("click", () => {
      void selectCoverPromptVersion(button.dataset.coverPromptSelect);
    });
  });
  bindCoverCharacterEvents();
  $$("[data-cover-image-select]").forEach((button) => {
    button.addEventListener("click", () => {
      void selectCoverImageVersion(button.dataset.coverImageSelect);
    });
  });
  $$("[data-cover-image-preview]").forEach((button) => {
    button.addEventListener("click", () => {
      previewCoverImageVersion(button.dataset.coverImagePreview, model);
    });
  });
}

async function submitCoverPromptGeneration(coverText = null) {
  if (!allows(state.coverPrompts, "GENERATE_COVER_PROMPT")) {
    toast("后端当前未允许生成封面提示词。", true);
    return;
  }
  if (!(await flushCoverDraftSave("project", state.project.id))) return;
  if (!(await flushCoverPromptSave())) return;
  const styleSource = state.coverStyleSource || coverStyleSourceModel({ channelStyle: state.coverChannelStyle, projectStyle: state.coverStyle }).mode;
  const body = {
    brief: state.coverBrief.trim(),
    style_override_text: state.coverStyleOverride.trim() || null,
    style_source: styleSource,
  };
  // v5 §1.4：未显式选择（null）则不传，后端按「快照∩有卡按快照顺序」默认绑定；显式 [] = 不绑定。
  if (state.coverCardSelection != null) body.character_card_ids = [...state.coverCardSelection];
  // v5 §5：仅频道风格来源时可指定风格版本；缺省用频道当前指针版本。
  if (styleSource === "channel" && state.coverStyleProfileChoice) body.style_profile_id = state.coverStyleProfileChoice;
  if (coverText != null) body.cover_text = coverText;
  try {
    setBusy(true);
    await api.generateCoverPrompts(state.project.id, body);
    state.coverTextChoice = null;
    toast(coverText != null ? "已提交「以所选文字重新生成画面」。" : "封面提示词生成任务已提交。");
    await reloadCoverFactsAndRender();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function chooseCoverText(text) {
  const previous = state.coverTextChoice;
  state.coverTextChoice = text;
  try {
    setBusy(true);
    const updated = await api.patchCurrentCoverPrompt(state.project.id, { cover_text_choice: text });
    applyCoverPromptUpdate(updated);
    renderCoverStageView();
  } catch (error) {
    state.coverTextChoice = previous;
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function resetCoverPrompt() {
  try {
    setBusy(true);
    const updated = await api.patchCurrentCoverPrompt(state.project.id, { reset: true });
    state.coverPromptDraft = null;
    applyCoverPromptUpdate(updated);
    toast("已重置为系统版提示词。");
    renderCoverStageView();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

async function submitCoverImageGeneration() {
  if (!allows(state.coverImages, "GENERATE_COVER_IMAGE")) {
    toast("后端当前未允许生成封面图。", true);
    return;
  }
  if (!(await flushCoverPromptSave())) return;
  try {
    setBusy(true);
    await api.generateCoverImage(state.project.id);
    toast("封面图生成任务已提交。");
    await reloadCoverFactsAndRender();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

const COVER_IMAGE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024;

// 用户直接上传封面图（2026-10-22）：同步落为不可变版本，成功后立即成为当前。
async function uploadCoverImageFile(file) {
  if (!(file instanceof File) || !file.size) {
    toast("请选择有效图片文件。", true);
    return;
  }
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    toast("仅支持 jpg / jpeg / png / webp 图片。", true);
    return;
  }
  if (file.size > COVER_IMAGE_UPLOAD_MAX_BYTES) {
    toast("图片不能超过 25MB。", true);
    return;
  }
  if (!allows(state.coverImages, "UPLOAD_COVER_IMAGE")) {
    toast("后端当前未允许上传封面图。", true);
    return;
  }
  state.coverImageUploading = true;
  const button = $("#coverUploadImage");
  if (button) {
    button.disabled = true;
    button.textContent = "上传中…";
  }
  try {
    setBusy(true);
    const response = await api.uploadProjectCoverImage(state.project.id, file);
    toast(response?.reused ? "相同图片已存在，已设为当前封面图。" : "封面图已上传并设为当前。");
  } catch (error) {
    toast(coverUploadErrorMessage(error) || messageOf(error), true);
  } finally {
    state.coverImageUploading = false;
    setBusy(false);
    const input = $("#coverUploadImageFile");
    if (input) input.value = "";
  }
  await reloadCoverFactsAndRender();
}

async function selectCoverImageVersion(generationId) {
  if (!generationId) return;
  try {
    setBusy(true);
    await api.selectCoverImage(state.project.id, generationId);
    toast("当前封面图已切换。");
    await reloadCoverFactsAndRender();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

function previewCoverImageVersion(generationId, model) {
  const image = $("#coverCurrentImage");
  if (!image) return;
  const version = model.imagePanel.versions.find((item) => item.id === generationId);
  setNativeMediaSource(image, api.coverImageUrl(state.project.id, generationId), "封面图");
  $$("[data-cover-image-preview]").forEach((button) => {
    button.closest(".version-item")?.classList.toggle("is-previewing", button.dataset.coverImagePreview === generationId);
  });
  const caption = $("#coverPreviewCaption");
  if (caption) caption.textContent = `预览历史版本 ${version?.version ?? ""}（不切换当前版本）`;
}

// 提示词版本切换（v5 §3）：候选须 SUCCEEDED（视图层 switchable 已过滤），切换后整轮刷新。
async function selectCoverPromptVersion(generationId) {
  if (!generationId) return;
  try {
    setBusy(true);
    await api.setCurrentCoverPrompt(state.project.id, generationId);
    state.coverPromptDraft = null;
    state.coverTextChoice = null;
    toast("当前提示词版本已切换。");
    await reloadCoverFactsAndRender();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

// ——— ⑥ 角色绑定区事件与动作（v5 §1.4）———

function coverBindingSelectedIds() {
  return coverCharacterBindingModel({
    snapshotImageIds: coverSnapshotImageIds(state.project),
    channelImages: Array.isArray(state.coverCharacterImages) ? state.coverCharacterImages : [],
    cards: state.coverCharacterCards?.items,
    selection: state.coverCardSelection,
    analyzingCardId: state.coverCardAnalyzing,
  }).selectedIds;
}

function bindCoverCharacterEvents() {
  const panel = $("#coverCharacterBinding");
  if (!panel) return;
  panel.addEventListener("input", (event) => {
    const field = event.target;
    const row = field.closest("[data-cover-char-row]");
    if (!row) return;
    const imageId = row.dataset.coverCharRow;
    const draft = state.coverCardDrafts[imageId] || (state.coverCardDrafts[imageId] = {});
    if (field.matches("[data-cover-char-identity]")) {
      draft.identity_text = field.value;
      const counter = $("[data-cover-char-identity-count]", row);
      if (counter) counter.textContent = `${field.value.length} / ${COVER_CHARACTER_IDENTITY_MAX}`;
    } else if (field.matches("[data-cover-char-appearance]")) {
      draft.appearance_text = field.value;
      const counter = $("[data-cover-char-appearance-count]", row);
      if (counter) counter.textContent = `${field.value.length} / ${COVER_CHARACTER_APPEARANCE_MAX}`;
    }
  });
  $$("[data-cover-char-pick]", panel).forEach((input) => {
    input.addEventListener("change", () => {
      const cardId = input.dataset.coverCharPick;
      if (!cardId) return;
      const ids = coverBindingSelectedIds();
      state.coverCardSelection = input.checked ? [...ids, cardId] : ids.filter((id) => id !== cardId);
      renderCoverStageView();
    });
  });
  $$("[data-cover-char-move]", panel).forEach((button) => {
    button.addEventListener("click", () => {
      const cardId = button.dataset.coverCharMove;
      const ids = [...coverBindingSelectedIds()];
      const from = ids.indexOf(cardId);
      const to = from + (button.dataset.dir === "-1" ? -1 : 1);
      if (from < 0 || to < 0 || to >= ids.length) return;
      [ids[from], ids[to]] = [ids[to], ids[from]];
      state.coverCardSelection = ids;
      renderCoverStageView();
    });
  });
  $$("[data-cover-char-save]", panel).forEach((button) => {
    button.addEventListener("click", () => {
      void saveCoverCharacterCard(button.dataset.coverCharSave);
    });
  });
  $$("[data-cover-char-analyze]", panel).forEach((button) => {
    button.addEventListener("click", () => {
      void analyzeCoverCharacterCard(button.dataset.coverCharAnalyze);
    });
  });
  $("#coverBindDefault", panel)?.addEventListener("click", () => {
    state.coverCardSelection = null;
    renderCoverStageView();
  });
  $("#coverBindNone", panel)?.addEventListener("click", () => {
    state.coverCardSelection = [];
    renderCoverStageView();
  });
}

async function saveCoverCharacterCard(imageId) {
  const row = $(`[data-cover-char-row="${imageId}"]`);
  if (!row) return;
  const identity = $("[data-cover-char-identity]", row)?.value ?? "";
  const appearance = $("[data-cover-char-appearance]", row)?.value ?? "";
  try {
    setBusy(true);
    const card = await api.upsertCoverCharacterCard(state.project.channel_id, {
      character_image_id: imageId,
      identity_text: identity.trim(),
      appearance_text: appearance.trim(),
    });
    upsertLocalCoverCard(card);
    delete state.coverCardDrafts[imageId];
    toast("角色卡已保存。");
    renderCoverStageView();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

// 「解析特征」：202 后按会话标记在途卡，封面轮询发现卡 updated_at 变化（解析落卡）
// 或超时（COVER_ANALYSIS_WATCH_MS）后解除在途；失败不覆盖卡，超时解除后可重试。
async function analyzeCoverCharacterCard(cardId) {
  if (!cardId || state.coverCardAnalyzing) return;
  try {
    setBusy(true);
    const result = await api.analyzeCoverCharacterCard(state.project.channel_id, cardId);
    const card = result?.card || null;
    if (card) {
      upsertLocalCoverCard(card);
      delete state.coverCardDrafts[String(card.character_image_id || "")];
    }
    state.coverCardAnalyzing = String(cardId);
    state.coverCardAnalyzingUpdatedAt = String(card?.updated_at || "");
    state.coverAnalysisDeadline = Date.now() + COVER_ANALYSIS_WATCH_MS;
    toast("角色卡解析任务已提交。");
    renderCoverStageView();
  } catch (error) {
    toast(messageOf(error), true);
  } finally {
    setBusy(false);
  }
}

// 频道管理页「封面风格」区：当前风格文本预览、版本列表与切换、独立上传/解析入口。
function channelCoverStylePanelMarkup(channel) {
  if (!channel) return "";
  const style = state.coverChannelStyle;
  if (!style) {
    return `<div class="panel cover-style-panel"><div class="panel-head"><div><h2>封面风格</h2><p>频道级封面参考与固定风格。</p></div></div><div class="panel-body">${renderUnavailable("封面风格端点不可用", unavailable("channel_cover_style"))}</div></div>`;
  }
  const profile = style.current_profile || null;
  const versions = coverVersionList(style.profiles);
  return `<div class="panel cover-style-panel">
    <div class="panel-head"><div><h2>封面风格</h2><p>频道级封面参考与固定风格；生成封面时默认使用当前频道风格。</p></div><span class="status-pill ${profile ? "" : "muted"}">${profile ? `当前版本 ${escapeHtml(profile.version)}` : "未固定"}</span></div>
    <div class="panel-body">
      <strong class="cover-block-title">当前风格文本</strong>
      ${profile ? `<pre class="cover-style-preview">${escapeHtml(profile.edited_text || "（空）")}</pre>` : `<div class="notice-card"><strong>尚未固定频道风格</strong><p>在下方上传参考图并解析后，点击「固定为频道风格」形成首个版本。</p></div>`}
      ${versions.length ? `<strong class="cover-block-title mt-12">风格版本</strong><div class="cover-version-list">${versions.map((item) => `<article class="version-item ${item.isCurrent ? "is-current" : ""}">
        <div><strong>版本 ${escapeHtml(item.version)}${item.label ? ` · ${escapeHtml(item.label)}` : ""}${item.isCurrent ? " · 当前" : ""}</strong><small>${item.createdAt ? escapeHtml(formatDateTime(item.createdAt)) : ""}</small></div>
        <div class="version-actions"><button class="secondary-button compact" data-cover-profile-select="${escapeHtml(item.id)}" type="button" ${item.selectable ? "" : "disabled"}>${item.isCurrent ? "当前版本" : "切换"}</button></div>
      </article>`).join("")}</div>` : ""}
      <strong class="cover-block-title mt-12">参考图与勾选</strong>
      ${coverRefEditorMarkup({ scope: "channel", style, analyzing: coverAnalyzing("channel", channel.id) })}
    </div>
  </div>`;
}

// 频道管理页「默认画面风格」区：仅作为该频道新建项目的默认值，只影响未来项目。
function channelDefaultVisualStyleMarkup(channel) {
  if (!channel) return "";
  const value = channel.default_visual_style || "";
  return `<div class="panel">
    <div class="panel-head"><div><h2>默认画面风格</h2><p>该频道新建项目时默认带出此值，可随时修改；已有项目不受影响。</p></div><span class="status-pill ${value ? "purple" : "muted"}">${value ? "已设置" : "未设置"}</span></div>
    <div class="panel-body">
      <form id="channelVisualStyleForm">
        <label class="field"><span>画面风格描述 <small id="channelVisualStyleCount">${value.length}/500</small></span><textarea name="default_visual_style" rows="3" maxlength="500" placeholder="例如：写实电影感、暖色调、浅景深、粗颗粒胶片">${escapeHtml(value)}</textarea></label>
        <div class="modal-actions"><button class="secondary-button compact" type="submit">保存默认画面风格</button></div>
      </form>
    </div>
  </div>`;
}


async function renderAssets(options = {}) {
  state.renderEpoch += 1;
  clearObjectUrls();
  markNavigation("channels");
  setBreadcrumb(["TubeFlow AI", "频道管理"]);
  const root = $("#viewRoot");
  const channel = channelById(state.assetChannelId) || state.channels[0] || null;
  if (channel) {
    state.assetChannelId = channel.id;
    root.innerHTML = `<div class="page"><div class="empty-state is-compact"><div><span class="loading-spinner" aria-hidden="true"></span><h3>正在载入频道资产</h3><p>从后端读取频道与角色图片。</p></div></div></div>`;
    try {
      state.images = await api.images(channel.id);
      state.coverChannelStyle = (await api.optional(() => api.channelCoverStyle(channel.id))).data;
      // §23.3：克隆音色按频道读取；失败不阻塞资产页（面板如实显示为空清单）。
      const cloned = await api.optional(() => api.clonedVoices(channel.id));
      state.clonedVoices = Array.isArray(cloned.data) ? cloned.data : [];
      state.clonedVoiceChannelId = channel.id;
    } catch (error) {
      root.innerHTML = `<div class="page">${renderUnavailable("角色资产读取失败", messageOf(error))}<div class="modal-actions actions-center"><button id="retryLoadAssets" class="secondary-button" type="button">重试</button></div></div>`;
      $("#retryLoadAssets")?.addEventListener("click", () => renderAssets(options));
      return;
    }
  } else {
    state.images = [];
    state.coverChannelStyle = null;
    state.clonedVoices = [];
    state.clonedVoiceChannelId = null;
  }

  // 默认角色 IP：归一化到当前图库（过滤已删除图片、去重保序、上限 3 个），数组序号即角标顺序。
  const defaultPersonaIds = normalizeDefaultCharacterIds(channel?.default_character_image_ids, state.images.map((image) => image.id));
  const personaOrder = new Map(defaultPersonaIds.map((id, index) => [id, index]));
  root.innerHTML = `<div class="page">
    <div class="page-heading">
      <div><span class="eyebrow">CHANNEL ASSETS</span><h1>频道、默认音色与共享角色资产库</h1><p>频道仅保存 URL；公共音色固定只读，角色图片按频道共享并形成不可变版本。</p></div>
    </div>
    <div class="asset-library">
      <section class="panel">
        <div class="panel-head"><div><h2>频道 URL</h2><p>不解析频道名称、简介或外部内容。</p></div><span class="status-pill purple">${state.channels.length} 个频道</span></div>
        <div class="panel-body">
          <form id="channelCreateForm" class="form-grid">
            <label class="field"><span>频道完整 URL</span><input id="newChannelUrl" name="url" type="url" placeholder="https://www.youtube.com/@channel" required /></label>
            <button class="primary-button" type="submit">添加频道</button>
          </form>
          <div class="sentence-list mt-16">${state.channels.map((item, index) => `<div class="channel-row ${item.id === state.assetChannelId ? "is-active" : ""}">
            <button class="channel-row-select" data-asset-channel="${item.id}" type="button">
              <span class="sentence-index">${String(index + 1).padStart(2, "0")}</span>
              <span class="channel-row-text" title="${escapeHtml(item.url)}"><strong>${escapeHtml(channelDisplayName(item, index))}</strong><small>${escapeHtml(item.url)}</small></span>
            </button>
            <div class="channel-row-actions">
              <button class="icon-button" data-rename-channel="${item.id}" type="button" title="重命名频道" aria-label="重命名频道">✎</button>
              <button class="icon-button is-danger" data-delete-channel="${item.id}" type="button" title="删除频道" aria-label="删除频道">✕</button>
            </div>
          </div>`).join("") || `<div class="empty-state is-compact"><div><b>频</b><h3>尚无频道</h3><p>添加完整频道 URL 后开始上传角色资产。</p></div></div>`}</div>
        </div>
      </section>
      <section>
        <div class="panel">
          <div class="panel-head"><div><h2>共享角色资产</h2><p>${channel ? `${escapeHtml(channelLabel(channel))} · ${escapeHtml(channel.url)}` : "请先添加频道 URL"}</p>${channel ? `<p>默认角色 IP（${defaultPersonaIds.length}/${MAX_DEFAULT_CHARACTER_IMAGES}）将展示在视频项目主页，并自动预选到新建项目。</p>` : ""}</div><span class="status-pill purple">${state.images.length} 个版本</span></div>
          <div class="panel-body">
            <form id="imageUploadForm" class="upload-zone">
              <label class="field"><span>上传角色图片</span><input name="file" type="file" accept="image/*" required ${channel ? "" : "disabled"} /></label>
              <button class="secondary-button compact" type="submit" ${channel ? "" : "disabled"}>上传为不可变版本</button>
            </form>
            <div class="asset-grid mt-14">${state.images.map((image, index) => {
              const personaIndex = personaOrder.get(image.id);
              const isDefaultPersona = personaIndex !== undefined;
              return `<article class="asset-card">
              ${channel ? `<button class="persona-star${isDefaultPersona ? " is-active" : ""}" type="button" data-persona-toggle="${image.id}" aria-pressed="${isDefaultPersona}" title="${isDefaultPersona ? "取消默认" : "设为默认角色 IP"}">${isDefaultPersona ? "★" : "☆"}</button>` : ""}
              <img data-protected-image="/identity/channels/${channel.id}/character-images/${image.id}/content" alt="角色图 ${index + 1}" />
              <div class="asset-card-body"><strong>角色图 ${String(index + 1).padStart(2, "0")}${isDefaultPersona ? ` <span class="persona-badge">默认${["①", "②", "③"][personaIndex]}</span>` : ""}</strong><small>${image.width}×${image.height} · ${escapeHtml(image.media_type)}</small></div>
            </article>`;
            }).join("") || `<div class="empty-state"><div><b>图</b><h3>尚无角色图片</h3><p>上传后，同频道多个项目可引用该不可变版本。</p></div></div>`}</div>
          </div>
        </div>
        ${channelDefaultVisualStyleMarkup(channel)}
        ${channel ? '<div id="voiceSourcePanelMount"></div>' : ""}
        ${channelCoverStylePanelMarkup(channel)}
      </section>
    </div>
  </div>`;

  renderChannelRail();
  hydrateProtectedImages(root);
  // UX 方案A（2026-10-14 确认）：两级音色面板（① 选择配音源 → ② 源内工作区）挂载与事件绑定；
  // 源卡切换为纯视图操作，「设为频道默认」即生效并跨源自动切换配音源。
  renderVoiceSourcePanel(root, channel);
  // 默认画面风格：显式保存（多行文本不做逐字 PATCH）；留空保存即清空回到「未设置」。
  const visualStyleForm = $("#channelVisualStyleForm");
  if (visualStyleForm && channel) {
    const visualStyleInput = visualStyleForm.elements.default_visual_style;
    const visualStyleCount = $("#channelVisualStyleCount");
    visualStyleInput.addEventListener("input", () => {
      visualStyleCount.textContent = `${visualStyleInput.value.length}/500`;
    });
    visualStyleForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = visualStyleInput.value.trim();
      if (value === (channel.default_visual_style || "")) {
        toast("画面风格未修改。");
        return;
      }
      try {
        setBusy(true);
        const updated = await api.patchChannel(channel.id, { default_visual_style: value });
        state.channels = state.channels.map((item) => (item.id === updated.id ? updated : item));
        toast(value ? "已保存频道默认画面风格，将用于该频道新建项目。" : "已清空频道默认画面风格。");
        await renderAssets();
      } catch (error) {
        toast(messageOf(error), true);
      } finally {
        setBusy(false);
      }
    });
  }
  if (options.focusCreate) $("#newChannelUrl")?.focus();
  $("#channelCreateForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = new FormData(event.currentTarget).get("url");
    try {
      setBusy(true);
      const created = await api.createChannel(url);
      state.channels.push(created);
      state.activeChannelId = created.id;
      state.assetChannelId = created.id;
      rememberActiveChannel(created.id);
      state.images = [];
      renderChannelRail();
      await renderAssets();
      toast("频道 URL 已保存。");
    } catch (error) {
      toast(messageOf(error), true);
    } finally {
      setBusy(false);
    }
  });
  $$("[data-asset-channel]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.querySelector(".channel-rename-input")) return;
      if (!(await ensureCoverEditsSaved())) return;
      state.assetChannelId = button.dataset.assetChannel;
      state.activeChannelId = state.assetChannelId;
      rememberActiveChannel(state.activeChannelId);
      await renderAssets();
    });
  });
  $$("[data-rename-channel]", root).forEach((button) => {
    button.addEventListener("click", () => beginChannelRename(button.dataset.renameChannel, button));
  });
  // 删除频道（2026-10-20）：仅空频道可删；确认值＝完整名称（未命名时完整 URL）。
  $$("[data-delete-channel]", root).forEach((button) => {
    button.addEventListener("click", () => {
      void beginChannelDeletion(button.dataset.deleteChannel);
    });
  });
  // 默认角色 IP 星标：纯函数计算 → PATCH 频道；保存中禁用全部星标防重入，失败回滚显示。
  let personaSaving = false;
  $$("[data-persona-toggle]", root).forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!channel || personaSaving || button.disabled) return;
      const imageId = button.dataset.personaToggle;
      const result = toggleDefaultCharacterId(defaultPersonaIds, imageId);
      if (!result.ok) {
        toast(`默认角色 IP 最多 ${MAX_DEFAULT_CHARACTER_IMAGES} 个，请先取消一个。`, true);
        return;
      }
      personaSaving = true;
      $$("[data-persona-toggle]", root).forEach((star) => { star.disabled = true; });
      try {
        const updated = await api.patchChannel(channel.id, { default_character_image_ids: result.ids });
        state.channels = state.channels.map((item) => (item.id === updated.id ? updated : item));
        toast(result.ids.includes(imageId) ? "已设为默认角色 IP，将展示在视频项目主页。" : "已取消默认角色 IP。");
        await renderAssets();
      } catch (error) {
        toast(messageOf(error), true);
        $$("[data-persona-toggle]", root).forEach((star) => { star.disabled = false; });
      } finally {
        personaSaving = false;
      }
    });
  });
  // 文件选择与拖拽共用同一上传路径（同一校验与提交流程）。
  const uploadImageFile = async (file) => {
    if (!(file instanceof File) || !file.size || !state.assetChannelId) {
      toast("请选择有效图片文件。", true);
      return;
    }
    try {
      setBusy(true);
      await api.uploadImage(state.assetChannelId, file);
      state.images = await api.images(state.assetChannelId);
      await renderAssets();
      toast("角色图片不可变版本已上传。");
    } catch (error) {
      toast(messageOf(error), true);
    } finally {
      setBusy(false);
    }
  };
  $("#imageUploadForm").addEventListener("submit", (event) => {
    event.preventDefault();
    void uploadImageFile(new FormData(event.currentTarget).get("file"));
  });
  // 上传区视觉是 dropzone，补上真实拖拽：dragover 高亮、drop 取 dataTransfer.files。
  const uploadZone = $("#imageUploadForm");
  if (channel) {
    uploadZone.addEventListener("dragover", (event) => {
      event.preventDefault();
      uploadZone.classList.add("is-dragover");
    });
    uploadZone.addEventListener("dragleave", (event) => {
      if (!uploadZone.contains(event.relatedTarget)) uploadZone.classList.remove("is-dragover");
    });
    uploadZone.addEventListener("drop", (event) => {
      event.preventDefault();
      uploadZone.classList.remove("is-dragover");
      const file = event.dataTransfer?.files?.[0];
      if (!file || !String(file.type || "").startsWith("image/")) {
        toast("请拖入图片文件。", true);
        return;
      }
      void uploadImageFile(file);
    });
  }
  if (channel) {
    coverEditorMount("channel", channel.id);
    hydrateCoverRefImages(root, "channel", channel.id);
    bindCoverRefEditor({ scope: "channel", id: channel.id });
    $$("[data-cover-profile-select]", root).forEach((button) => {
      button.addEventListener("click", async () => {
        if (button.disabled) return;
        try {
          setBusy(true);
          await api.selectChannelCoverStyle(channel.id, button.dataset.coverProfileSelect);
          toast("频道封面风格已切换。");
          await renderAssets();
        } catch (error) {
          toast(messageOf(error), true);
        } finally {
          setBusy(false);
        }
      });
    });
    if (coverAnalyzing("channel", channel.id)) scheduleChannelCoverWatch(channel.id);
  }
}

async function renderAdmin() {
  if (state.me?.role !== "admin") {
    renderProjects();
    return;
  }
  state.renderEpoch += 1;
  clearObjectUrls();
  markNavigation("admin");
  setBreadcrumb(["TubeFlow AI", "账号管理"]);
  const root = $("#viewRoot");
  root.innerHTML = `<div class="page"><div class="empty-state is-compact"><div><span class="loading-spinner" aria-hidden="true"></span><h3>正在载入账号列表</h3><p>从后端读取账号。</p></div></div></div>`;
  try {
    const users = await api.users();
    root.innerHTML = `<div class="page">
      <div class="page-heading"><div><span class="eyebrow">ACCOUNT ADMINISTRATION</span><h1>账号管理</h1><p>管理员可创建、停用账号和重置密码；不暴露基础设施或 Provider 密钥。</p></div></div>
      <section class="panel">
        <div class="panel-head"><div><h2>创建账号</h2><p>系统不提供自助注册。</p></div></div>
        <div class="panel-body"><form id="createUserForm" class="form-grid two-cols"><label class="field"><span>用户名</span><input name="username" required /></label><label class="field"><span>初始密码</span><input name="password" type="password" minlength="8" required /></label><div><button class="primary-button" type="submit">创建账号</button></div></form></div>
      </section>
      <section class="panel">
        <div class="panel-head"><div><h2>账号列表</h2><p>停用或重置密码会使该账号已有会话失效。</p></div><span class="status-pill purple">${users.length} 个账号</span></div>
        <div class="account-list">${users.map((user) => `<article class="account-row">
          <div><strong>${escapeHtml(user.username)}</strong><small>${escapeHtml(user.role)} · ${user.active ? "已启用" : "已停用"}</small></div>
          <span class="status-pill ${user.active ? "" : "muted"}">${user.active ? "启用" : "停用"}</span>
          <div><button class="ghost-button compact" data-reset-user="${user.id}" type="button">重置密码</button><button class="danger-button compact" data-disable-user="${user.id}" type="button" ${!user.active || user.id === state.me.id ? "disabled" : ""}>停用</button></div>
        </article>`).join("")}</div>
      </section>
    </div>`;

    $("#createUserForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        setBusy(true);
        await api.createUser(Object.fromEntries(new FormData(event.currentTarget)));
        toast("账号已创建。");
        await renderAdmin();
      } catch (error) {
        toast(messageOf(error), true);
      } finally {
        setBusy(false);
      }
    });
    $$("[data-reset-user]").forEach((button) => {
      button.addEventListener("click", async () => {
        const target = users.find((user) => user.id === button.dataset.resetUser);
        const result = await actionModal({
          title: "重置账号密码",
          eyebrow: "RESET PASSWORD",
          confirmText: "确认重置",
          bodyHtml: `<p>为账号 <strong>${escapeHtml(target?.username || "")}</strong> 设置新密码；重置后该账号已有会话立即失效。</p>
            <label class="field mt-12"><span>新密码 <em>*</em></span><input id="resetPasswordInput" type="password" minlength="8" autocomplete="new-password" /></label>
            <label class="field"><span>再次输入新密码 <em>*</em></span><input id="resetPasswordConfirm" type="password" minlength="8" autocomplete="new-password" /></label>
            <p id="resetPasswordHint" class="modal-hint" hidden></p>`,
          setup: ({ submit, setValue }) => {
            const first = $("#resetPasswordInput");
            const second = $("#resetPasswordConfirm");
            const hint = $("#resetPasswordHint");
            submit.disabled = true;
            const sync = () => {
              const issue = passwordPairIssue(first.value, second.value);
              submit.disabled = Boolean(issue);
              if (issue === "mismatch" && second.value) {
                hint.textContent = "两次输入不一致。";
                hint.hidden = false;
              } else if (issue === "too_short" && first.value) {
                hint.textContent = "新密码至少需要 8 位。";
                hint.hidden = false;
              } else {
                hint.hidden = true;
              }
              setValue(first.value);
            };
            first.addEventListener("input", sync);
            second.addEventListener("input", sync);
            first.focus();
          },
        });
        if (!result.confirmed) return;
        try {
          setBusy(true);
          await api.resetPassword(button.dataset.resetUser, result.value);
          toast("密码已重置，旧会话已失效。");
        } catch (error) {
          toast(messageOf(error), true);
        } finally {
          setBusy(false);
        }
      });
    });
    $$("[data-disable-user]").forEach((button) => {
      button.addEventListener("click", async () => {
        const target = users.find((user) => user.id === button.dataset.disableUser);
        const result = await actionModal({
          title: "停用账号",
          eyebrow: "DISABLE ACCOUNT",
          danger: true,
          confirmText: "确认停用",
          bodyHtml: `<div class="notice-card error"><strong>停用立即生效且会话失效</strong><p>确认停用账号「${escapeHtml(target?.username || "")}」？其已有会话将立即失效，重新启用后才能登录。</p></div>`,
        });
        if (!result.confirmed) return;
        try {
          setBusy(true);
          await api.disableUser(button.dataset.disableUser);
          toast("账号已停用。");
          await renderAdmin();
        } catch (error) {
          toast(messageOf(error), true);
        } finally {
          setBusy(false);
        }
      });
    });
  } catch (error) {
    root.innerHTML = `<div class="page">${renderUnavailable("账号管理不可用", messageOf(error))}</div>`;
  }
}

boot();
