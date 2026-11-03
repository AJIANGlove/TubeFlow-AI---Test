// ElevenLabs 号池管理页（局域网公开页，无登录）。
// 文件前半是不依赖 DOM 的纯视图模型，由 node --test 覆盖；
// 后半 createPoolPage 负责渲染与事件绑定，仅在浏览器中启动。

import { ApiClient } from "./api-client.js";

export const KEY_STATUS_META = {
  active: { text: "可用", tone: "green" },
  exhausted: { text: "额度耗尽", tone: "amber" },
  invalid: { text: "已失效", tone: "error" },
  disabled: { text: "已停用", tone: "muted" },
};

export const EVENT_LABELS = {
  added: "添加",
  enabled: "启用",
  disabled: "停用",
  marked_exhausted: "标记额度耗尽",
  marked_invalid: "标记失效",
  transient_failure: "瞬态失败",
  quota_refreshed: "配额已刷新",
  quota_refresh_failed: "配额刷新失败",
};

const ERROR_MESSAGES = {
  elevenlabs_key_invalid: "ElevenLabs 拒绝了该 API key，请核对后重试。",
  elevenlabs_key_duplicate: "该 API key 已在号池中。",
  elevenlabs_unreachable: "无法连接 ElevenLabs，请检查网络或代理后重试。",
  provider_not_reached: "无法连接 ElevenLabs（mihomo 代理链路异常），请检查代理容器后重试。",
  provider_auth_failed: "ElevenLabs 校验未通过该 API key，请核对后重试。",
  not_found: "该 key 已不存在，请刷新列表。",
  backend_unavailable: "无法连接后端服务。",
};

export function statusBadge(status) {
  return KEY_STATUS_META[status] || { text: status || "未知", tone: "muted" };
}

export function quotaPercent(quota) {
  if (!quota) return null;
  const count = Number(quota.character_count);
  const limit = Number(quota.character_limit);
  if (!Number.isFinite(count) || !Number.isFinite(limit) || limit <= 0) return null;
  return Math.min(100, Math.round((count / limit) * 100));
}

export function formatCharacters(quota) {
  if (!quota || quota.character_count == null || quota.character_limit == null) return "未查询";
  return `${Number(quota.character_count).toLocaleString()} / ${Number(quota.character_limit).toLocaleString()}`;
}

export function formatVoiceSlots(quota) {
  if (!quota || quota.voice_slots_used == null || quota.voice_limit == null) return "未查询";
  return `${quota.voice_slots_used} / ${quota.voice_limit}`;
}

export function formatTime(iso) {
  const value = new Date(iso);
  return Number.isNaN(value.getTime()) ? String(iso ?? "") : value.toLocaleString("zh-CN", { hour12: false });
}

export function eventLabel(event) {
  return EVENT_LABELS[event] || event;
}

export function describePoolError(code, message) {
  return ERROR_MESSAGES[code] || message || "操作失败。";
}

export function createPoolPage(doc, api) {
  const errorBox = doc.getElementById("error-box");
  const keysBody = doc.getElementById("keys-body");
  const eventsBody = doc.getElementById("events-body");
  const form = doc.getElementById("add-form");
  const submitButton = form.querySelector('button[type="submit"]');

  function showError(error) {
    errorBox.textContent = error ? describePoolError(error.code, error.message) : "";
    errorBox.hidden = !error;
  }

  function badge(status) {
    const meta = statusBadge(status);
    return `<span class="status-pill ${meta.tone}">${meta.text}</span>`;
  }

  function quotaCell(key) {
    const percent = quotaPercent(key.quota);
    const bar = percent == null
      ? ""
      : `<span class="quota-bar"><span class="quota-fill" style="width:${percent}%"></span></span>`;
    const refreshed = key.quota?.refreshed_at ? formatTime(key.quota.refreshed_at) : "—";
    return `${bar}<span class="quota-text">${formatCharacters(key.quota)}</span><br><small>刷新于 ${refreshed}</small>`;
  }

  function renderKeys(keys) {
    if (!keys.length) {
      keysBody.innerHTML = '<tr><td colspan="7" class="empty-cell">号池为空，请先添加 key。</td></tr>';
      return;
    }
    keysBody.innerHTML = keys.map((key) => {
      const enabled = key.status !== "disabled";
      return `<tr data-key-id="${key.id}">
        <td>${escapeHtml(key.label || "—")}</td>
        <td class="mono">${escapeHtml(key.api_key)}</td>
        <td>${badge(key.status)}${key.last_error ? `<br><small class="last-error">${escapeHtml(key.last_error)}</small>` : ""}</td>
        <td>${quotaCell(key)}</td>
        <td>${formatVoiceSlots(key.quota)}</td>
        <td>${formatTime(key.added_at)}</td>
        <td class="actions">
          <button type="button" class="ghost-button" data-action="${enabled ? "disable" : "enable"}">${enabled ? "停用" : "启用"}</button>
          <button type="button" class="ghost-button" data-action="refresh">刷新配额</button>
          <button type="button" class="danger-button" data-action="delete">删除</button>
        </td>
      </tr>`;
    }).join("");
  }

  function renderEvents(events) {
    if (!events.length) {
      eventsBody.innerHTML = '<li class="empty-cell">暂无事件。</li>';
      return;
    }
    eventsBody.innerHTML = events.map((item) => (
      `<li><b>${eventLabel(item.event)}</b> <code>${escapeHtml(shortKeyId(item.key_id))}</code> ` +
      `<span>${escapeHtml(item.detail || "")}</span> <small>${formatTime(item.created_at)}</small></li>`
    )).join("");
  }

  function shortKeyId(keyId) {
    return typeof keyId === "string" && keyId.length > 8 ? keyId.slice(0, 8) : keyId;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  async function refresh() {
    try {
      const [keys, events] = await Promise.all([
        api.request("/elevenlabs-pool/keys"),
        api.request("/elevenlabs-pool/events?limit=50"),
      ]);
      renderKeys(keys.keys || []);
      renderEvents(events.events || []);
    } catch (error) {
      showError(error);
    }
  }

  // 先落定本次操作的结果错误，再刷新列表；refresh 不再清错误，
  // 避免"点击后错误一闪而过、看起来毫无反应"（2026-10-13 修复）。
  async function run(action) {
    let failure = null;
    try {
      await action();
    } catch (error) {
      failure = error;
    }
    showError(failure);
    await refresh();
  }

  function beginSubmit() {
    if (!submitButton) return () => {};
    const originalText = submitButton.textContent;
    submitButton.disabled = true;
    submitButton.textContent = "添加中…";
    return () => {
      submitButton.disabled = false;
      submitButton.textContent = originalText;
    };
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const label = form.elements.label.value.trim();
    const apiKey = form.elements.api_key.value.trim();
    if (!apiKey) return;
    if (submitButton && submitButton.disabled) return;
    const restoreSubmit = beginSubmit();
    void run(async () => {
      try {
        await api.request("/elevenlabs-pool/keys", { method: "POST", body: { api_key: apiKey, label } });
        form.reset();
      } finally {
        restoreSubmit();
      }
    });
  });

  keysBody.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    const row = event.target.closest("tr[data-key-id]");
    if (!button || !row) return;
    const keyId = row.dataset.keyId;
    const action = button.dataset.action;
    if (action === "delete" && !window.confirm("确定删除该 key？其事件记录将一并删除。")) return;
    const paths = { enable: "enable", disable: "disable", refresh: "refresh-quota" };
    void run(async () => {
      if (action === "delete") {
        await api.request(`/elevenlabs-pool/keys/${keyId}`, { method: "DELETE" });
      } else {
        await api.request(`/elevenlabs-pool/keys/${keyId}/${paths[action]}`, { method: "POST" });
      }
    });
  });

  void refresh();
  return { refresh };
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  createPoolPage(document, new ApiClient(window.fetch.bind(window)));
}
