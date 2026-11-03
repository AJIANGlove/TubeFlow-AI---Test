// STEP 11 简介页展示层：只含纯函数与字符串模板；DOM、网络与异步状态由 app.js 负责。

export const DESCRIPTION_PROMPT_LIMIT = 20000;

const escape = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

export function countDescriptionPrompt(value) {
  return Array.from(String(value ?? "")).length;
}

export function descriptionTemplatesModel(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  return items
    .map((item) => ({
      id: String(item?.id || ""),
      version: Number(item?.version) || 0,
      label: String(item?.label || `版本 ${Number(item?.version) || "?"}`),
      prompt: String(item?.prompt || ""),
      updatedAt: String(item?.updated_at || ""),
    }))
    .filter((item) => item.id && item.version > 0)
    .sort((left, right) => left.version - right.version);
}

export function descriptionStateModel(payload) {
  const versions = Array.isArray(payload?.versions) ? payload.versions : [];
  return {
    projectId: String(payload?.project_id || ""),
    channelId: String(payload?.channel_id || ""),
    channelName: String(payload?.channel_name || ""),
    available: Boolean(payload?.available),
    blockingReason: String(payload?.blocking_reason || ""),
    timelineEntries: Number(payload?.timeline_entries) || 0,
    durationMs: Number(payload?.duration_ms) || 0,
    current: descriptionVersionModel(payload?.current),
    versions: versions.map(descriptionVersionModel).filter(Boolean),
    generation: payload?.generation && typeof payload.generation === "object"
      ? {
        id: String(payload.generation.id || ""),
        status: String(payload.generation.status || ""),
        templateVersion: Number(payload.generation.template_version) || 0,
        errorCode: String(payload.generation.error_code || ""),
      }
      : null,
  };
}

export function descriptionVersionModel(value) {
  if (!value || typeof value !== "object") return null;
  return {
    id: String(value.id || ""),
    versionNumber: Number(value.version_number) || 0,
    source: String(value.source || "generated"),
    baseVersionId: String(value.base_version_id || ""),
    templateVersion: Number(value.prompt_template_version) || 0,
    content: String(value.content || ""),
    model: String(value.model || ""),
    providerCalls: Number(value.provider_calls) || 0,
    elapsedMs: Number(value.elapsed_ms) || 0,
    current: Boolean(value.current),
    createdAt: String(value.created_at || ""),
  };
}

export function descriptionGateText(reason) {
  const messages = {
    description_composition_required: "当前项目还没有最终 SRT。请先完成第 09 步合成，再生成简介。",
    description_generation_in_progress: "已有简介正在生成，请等待当前请求完成。",
    project_purging: "项目正在删除，简介功能不可用。",
  };
  return messages[reason] || "最终 SRT 可用后才能生成简介。";
}

export function descriptionSourceLabel(source) {
  return source === "manual" ? "人工编辑" : "AI 生成";
}

export function descriptionVersionLabel(version) {
  return `简介 v${version.versionNumber || "?"}`;
}

export function descriptionDownloadFilename(title, version) {
  const cleaned = String(title ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .replace(/[\\/:*?"<>|]/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[. ]+$/g, "");
  const clipped = Array.from(cleaned).slice(0, 80).join("").replace(/[. ]+$/g, "");
  const suffix = `简介-v${version?.versionNumber || "latest"}.txt`;
  return clipped ? `${clipped}-${suffix}` : suffix;
}

export function descriptionTemplatesMarkup(templates, selectedId, saveText) {
  const rows = templates.map((template) => {
    const count = countDescriptionPrompt(template.prompt);
    return `<article class="description-template-row" data-description-template-row="${escape(template.id)}">
      <div class="description-template-head">
        <label for="descriptionTemplate-${escape(template.id)}">${escape(template.label)}</label>
        <span class="description-template-tools">
          <small id="descriptionTemplateCount-${escape(template.id)}">${count} / ${DESCRIPTION_PROMPT_LIMIT}</small>
          <button type="button" class="ghost-button compact" data-description-template-copy="${escape(template.id)}">复制</button>
          <button type="button" class="danger-button compact" data-description-template-delete="${escape(template.id)}">删除</button>
        </span>
      </div>
      <textarea id="descriptionTemplate-${escape(template.id)}" data-description-template-input="${escape(template.id)}" rows="8" spellcheck="false" maxlength="${DESCRIPTION_PROMPT_LIMIT}">${escape(template.prompt)}</textarea>
    </article>`;
  }).join("");
  return `<section class="panel description-templates-panel">
    <div class="panel-head">
      <div><h2>简介提示词模板</h2><p>频道级独立模板；版本号只增不复用。支持占位符：{{topic}}、{{language}}、{{script}}、{{channel_name}}、{{timeline}}。</p></div>
      <div class="panel-actions"><span id="descriptionTemplateSaveState" class="description-save-state">${escape(saveText || "自动保存")}</span><button type="button" class="secondary-button compact" id="addDescriptionTemplate">新增模板</button></div>
    </div>
    <div class="panel-body description-template-list">${rows || `<div class="empty-state is-compact"><div><b>简</b><h3>暂无模板</h3><p>新增模板后才能生成简介。</p></div></div>`}</div>
  </section>`;
}

export function descriptionGenerateMarkup(state, templates, selectedTemplateId, editingId, editDraft, errorText = "") {
  const current = state.current;
  const editing = editingId
    ? state.versions.find((item) => item.id === editingId) || current
    : null;
  const options = templates.map((template) => `<option value="${escape(template.id)}" ${template.id === selectedTemplateId ? "selected" : ""}>${escape(template.label)}</option>`).join("");
  const canGenerate = state.available && templates.length > 0 && state.generation?.status !== "RUNNING";
  const gate = state.available
    ? `<div class="notice-card success"><strong>最终 SRT 已就绪</strong><p>${state.timelineEntries} 条字幕 · 实际时长 ${escape(formatDescriptionDuration(state.durationMs))}。</p></div>`
    : `<div class="notice-card amber"><strong>暂不能生成</strong><p>${escape(descriptionGateText(state.blockingReason))}</p></div>`;
  const running = state.generation?.status === "RUNNING"
    ? `<div class="notice-card"><strong>正在生成简介</strong><p>使用模板 V${state.generation.templateVersion}，请等待本次请求完成。</p></div>`
    : "";
  const content = editing
    ? `<textarea id="descriptionEditText" class="description-editor" rows="16" spellcheck="false">${escape(editDraft ?? editing.content)}</textarea>
      <div class="description-actions"><button type="button" class="primary-button" id="saveDescriptionEdit">保存为新版本</button><button type="button" class="ghost-button" id="cancelDescriptionEdit">取消</button></div>`
    : current
      ? `<pre id="descriptionCurrentText" class="description-content">${escape(current.content)}</pre>
      <div class="description-actions"><button type="button" class="ghost-button" data-description-copy="${escape(current.id)}">复制</button><button type="button" class="ghost-button" data-description-download="${escape(current.id)}">下载 TXT</button><button type="button" class="secondary-button" data-description-edit="${escape(current.id)}">编辑当前</button></div>`
      : `<div class="empty-state is-compact"><div><b>介</b><h3>尚无简介</h3><p>选择模板版本后点击生成；结果不会覆盖历史版本。</p></div></div>`;
  return `<section class="panel description-generate-panel">
    <div class="panel-head"><div><h2>生成简介</h2><p>版本选择只作用于本次生成；实际时间来自当前最终 SRT。</p></div><span class="status-pill ${state.available ? "green" : "muted"}">${state.available ? "SRT 就绪" : "等待合成"}</span></div>
    <div class="panel-body">
      ${gate}${running}
      ${errorText ? `<div class="notice-card error"><strong>简介操作失败</strong><p>${escape(errorText)}</p></div>` : ""}
      <div class="description-generate-controls">
        <label class="field"><span>使用模板版本</span><select id="descriptionTemplateSelect" ${templates.length ? "" : "disabled"}>${options}</select></label>
        <button type="button" class="primary-button" id="generateDescription" ${canGenerate ? "" : "disabled"}>${state.generation?.status === "RUNNING" ? "正在生成…" : "生成简介"}</button>
      </div>
      ${content}
    </div>
  </section>`;
}

export function descriptionHistoryMarkup(versions) {
  const rows = versions.map((version) => `<article class="description-history-row ${version.current ? "is-current" : ""}">
    <div class="description-history-main">
      <strong>${escape(descriptionVersionLabel(version))}</strong>
      <small>${escape(descriptionSourceLabel(version.source))} · 模板 V${version.templateVersion || "?"} · ${escape(formatDateTimeShort(version.createdAt))}</small>
      ${version.current ? `<span class="status-pill green">当前</span>` : ""}
    </div>
    <div class="description-history-actions">
      <button type="button" class="ghost-button compact" data-description-copy="${escape(version.id)}">复制</button>
      <button type="button" class="ghost-button compact" data-description-download="${escape(version.id)}">下载</button>
      ${version.current ? "" : `<button type="button" class="secondary-button compact" data-description-select="${escape(version.id)}">设为当前</button>`}
      <button type="button" class="ghost-button compact" data-description-edit="${escape(version.id)}">编辑</button>
    </div>
  </article>`).join("");
  return `<section class="panel description-history-panel"><div class="panel-head"><div><h2>简介版本历史</h2><p>历史版本不可变；切换或人工编辑不会覆盖旧版本。</p></div></div><div class="panel-body description-history-list">${rows || `<div class="empty-state is-compact"><div><b>史</b><h3>暂无历史</h3><p>生成成功后版本会保留在这里。</p></div></div>`}</div></section>`;
}

export function formatDescriptionDuration(ms) {
  const seconds = Math.max(0, Math.floor(Number(ms) / 1000 || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const second = seconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(second).padStart(2, "0")}`
    : `${minutes}:${String(second).padStart(2, "0")}`;
}

function formatDateTimeShort(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
