// 封面页「封面模板」的展示层（临时功能，2026-10-27 多模板版）。
// 一个模板 = 一张封面 + 一段提示词；可增删、拖动排序，文本必须点【保存】才落库。
// 只放不依赖 DOM 的纯函数与字符串模板：app.js 负责取数、保存、上传与复制。
// 删除本功能时：删本文件、app.js 的挂载块、api-client 的对应方法、
// styles.css 的 cover-template 段、后端 cover_templates 包与 0044 迁移。

export const COVER_TEMPLATE_PROMPT_LIMIT = 20000;
export const COVER_TEMPLATE_NAME_LIMIT = 30;

const escape = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

// 「1920×1080 · 1.4MB」：只显示原图尺寸与原图大小。
export function formatFileSize(bytes) {
  const size = Math.max(0, Math.round(Number(bytes) || 0));
  if (size < 1024) return `${size}B`;
  const kb = size / 1024;
  if (kb < 1024) return `${Math.round(kb)}KB`;
  const mb = kb / 1024;
  return `${mb >= 10 ? Math.round(mb) : mb.toFixed(1).replace(/\.0$/, "")}MB`;
}

export function coverTemplateMetaText(cover) {
  const width = Number(cover?.width) || 0;
  const height = Number(cover?.height) || 0;
  if (!width || !height) return "";
  return `${width}×${height} · ${formatFileSize(cover?.byte_size)}`;
}

export function coverTemplateItemModel(raw) {
  const cover = raw && typeof raw === "object" ? raw.cover : null;
  return {
    id: String(raw?.item_id || ""),
    name: String(raw?.name || ""),
    position: Number(raw?.position) || 0,
    prompt: String(raw?.prompt || ""),
    cover: cover
      ? {
        width: Number(cover.width) || 0,
        height: Number(cover.height) || 0,
        byteSize: Number(cover.byte_size) || 0,
        imageFormat: String(cover.image_format || ""),
        sha256: String(cover.sha256 || ""),
        metaText: coverTemplateMetaText(cover),
        previewUrl: String(cover.media_url_preview || ""),
        originalUrl: String(cover.media_url_original || ""),
      }
      : null,
    createdAt: String(raw?.created_at || ""),
    updatedAt: String(raw?.updated_at || ""),
  };
}

export function coverTemplateCollectionModel(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  return {
    items: items
      .map(coverTemplateItemModel)
      .sort((left, right) => left.position - right.position),
    updatedAt: String(payload?.updated_at || ""),
  };
}

// 后端按 Python 码点计长；JS 的 length 是 UTF-16 单元，emoji 会算 2，必须按码点裁剪。
export function countTemplatePrompt(value) {
  return Array.from(String(value ?? "")).length;
}

export function clipTemplatePrompt(value, limit = COVER_TEMPLATE_PROMPT_LIMIT) {
  const points = Array.from(String(value ?? ""));
  return points.length <= limit ? points.join("") : points.slice(0, limit).join("");
}

export function clipTemplateName(value, limit = COVER_TEMPLATE_NAME_LIMIT) {
  return Array.from(String(value ?? "")).slice(0, limit).join("");
}

export function templateDraftFromItem(item) {
  return { name: item.name, prompt: item.prompt };
}

export function templateDraftChanged(item, draft) {
  const next = draft || templateDraftFromItem(item);
  return String(next.name ?? "") !== item.name || String(next.prompt ?? "") !== item.prompt;
}

export function moveTemplateItem(items, fromId, toId, placeAfter = false) {
  const list = [...items];
  const fromIndex = list.findIndex((item) => item.id === fromId);
  const toIndex = list.findIndex((item) => item.id === toId);
  if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return list;
  const [moved] = list.splice(fromIndex, 1);
  const targetIndex = list.findIndex((item) => item.id === toId);
  list.splice(placeAfter ? targetIndex + 1 : targetIndex, 0, moved);
  return list.map((item, index) => ({ ...item, position: index }));
}

// 状态文案：错误优先，其次保存中，其次有改动未保存，最后显示已保存时间。
export function templateSaveStatus({ dirty = false, saving = false, error = "", savedAt = "" } = {}) {
  if (error) return { tone: "error", text: "保存失败，点击重试" };
  if (saving) return { tone: "saving", text: "保存中…" };
  if (dirty) return { tone: "dirty", text: "未保存" };
  if (savedAt) return { tone: "saved", text: `已保存 ${savedAt}` };
  return { tone: "idle", text: "未保存" };
}

export function formatSavedClock(date) {
  const value = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(value.getTime())) return "";
  return `${String(value.getHours()).padStart(2, "0")}:${String(value.getMinutes()).padStart(2, "0")}`;
}

function statusMarkup(status) {
  const tone = ["error", "saving", "dirty", "saved"].includes(status?.tone)
    ? status.tone
    : "idle";
  return `<span class="cover-template-status is-${tone}" data-template-status>${escape(status?.text || "未保存")}</span>`;
}

function mediaMarkup(item, id) {
  const cover = item.cover;
  const thumb = cover
    ? `<button type="button" class="cover-template-thumb" data-template-open="${escape(id)}" aria-label="放大查看封面">
        <img data-protected-image="${escape(cover.previewUrl)}" alt="封面模板" />
      </button>`
    : `<div class="cover-template-thumb is-empty"><span>暂无封面</span></div>`;
  return `<div class="cover-template-media">
    ${thumb}
    <small class="cover-template-meta">${cover ? escape(cover.metaText) : "可以先保存文本，之后再上传封面"}</small>
    <div class="cover-template-media-actions">
      <button type="button" class="secondary-button compact" data-template-upload="${escape(id)}">${cover ? "重新上传" : "上传封面"}</button>
      <button type="button" class="ghost-button compact" data-template-copy-image="${escape(id)}" ${cover ? "" : "disabled"}>复制图片</button>
      <input type="file" data-template-file="${escape(id)}" accept=".jpg,.jpeg,.png,.webp" hidden />
    </div>
  </div>`;
}

function cardMarkup(item, draft, dirty, state) {
  const promptCount = countTemplatePrompt(draft.prompt);
  return `<article class="cover-template-card${dirty ? " is-dirty" : ""}" data-template-id="${escape(item.id)}" draggable="false">
    <div class="cover-template-card-head">
      <span class="cover-template-drag" data-template-drag="${escape(item.id)}" title="按住拖动排序" aria-hidden="true">⋮⋮</span>
      <input class="cover-template-name" data-template-name="${escape(item.id)}" value="${escape(draft.name)}" maxlength="${COVER_TEMPLATE_NAME_LIMIT}" placeholder="模板名称" aria-label="模板名称" />
      <button type="button" class="danger-button compact" data-template-delete="${escape(item.id)}">删除</button>
    </div>
    <div class="cover-template-card-body">
      ${mediaMarkup(item, item.id)}
      <div class="cover-template-text">
        <textarea data-template-prompt="${escape(item.id)}" rows="6" spellcheck="false" maxlength="${COVER_TEMPLATE_PROMPT_LIMIT}" placeholder="提示词模板文本；修改后点【保存】才会生效">${escape(draft.prompt)}</textarea>
        <div class="cover-template-card-foot">
          <small class="cover-template-count" data-template-count>${promptCount} / ${COVER_TEMPLATE_PROMPT_LIMIT}</small>
          ${statusMarkup(state)}
          <span class="cover-template-foot-actions">
            <button type="button" class="ghost-button compact" data-template-copy="${escape(item.id)}">复制</button>
            <button type="button" class="primary-button compact" data-template-save="${escape(item.id)}">保存</button>
          </span>
        </div>
      </div>
    </div>
  </article>`;
}

export function coverTemplatesMarkup(model, drafts = {}, dirty = {}, states = {}) {
  const items = Array.isArray(model?.items) ? model.items : [];
  const cards = items.map((item) => {
    const draft = drafts[item.id] || templateDraftFromItem(item);
    return cardMarkup(item, draft, Boolean(dirty[item.id]), states[item.id] || {});
  }).join("");
  const empty = items.length
    ? ""
    : `<div class="empty-state is-compact"><div><b>模</b><h3>暂无模板</h3><p>新增一个模板，先写提示词或先上传封面都可以。</p></div></div>`;
  return `<section class="panel cover-templates" id="coverTemplatesPanel">
    <div class="panel-head"><div><h2>模板</h2><p>频道级，所有项目共用；一个模板 = 一张封面 + 一段提示词，文本点【保存】才生效。</p></div></div>
    <div class="panel-body cover-template-list">
      ${empty}
      ${cards}
      <button type="button" class="cover-template-add" id="coverTemplateAdd">
        <strong>+ 新增模板</strong><small>创建空模板后，可先写文本，再上传封面</small>
      </button>
    </div>
    ${coverTemplateLightboxMarkup()}
  </section>`;
}

export function coverTemplateLightboxMarkup() {
  return `<div id="coverTemplateLightbox" class="lightbox-backdrop cover-template-lightbox" hidden>
    <section class="lightbox" role="dialog" aria-modal="true" aria-label="模板封面原图">
      <div class="lightbox-head">
        <div class="lightbox-title"><strong id="coverTemplateLightboxName">模板封面</strong><small id="coverTemplateLightboxMeta"></small></div>
        <div class="lightbox-tools"><button id="coverTemplateLightboxClose" class="modal-close" type="button" aria-label="关闭">×</button></div>
      </div>
      <div class="lightbox-stage"><img id="coverTemplateLightboxImage" alt="模板封面原图" /></div>
    </section>
  </div>`;
}

// 折叠容器（永久改造，不属于临时功能本体）：只有标题行，默认收起。
export function coverMoreSettingsMarkup({ open = false, body = "" } = {}) {
  return `<section class="panel cover-more-settings" id="coverMoreSettings">
    <button type="button" class="cover-more-toggle" id="coverMoreToggle" aria-expanded="${open ? "true" : "false"}" aria-controls="coverMoreBody">
      <span>更多设置</span><span class="cover-more-chevron" aria-hidden="true">${open ? "▾" : "▸"}</span>
    </button>
    <div class="cover-more-body" id="coverMoreBody" ${open ? "" : "hidden"}>${body}</div>
  </section>`;
}
