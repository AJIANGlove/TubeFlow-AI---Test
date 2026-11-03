// §23 前端纯函数：配音源选择、克隆音色视图模型与删除保护判定。
// 只做数据换算与展示决策，不触碰 DOM、网络或全局状态，便于 node:test 覆盖。

export const COSYVOICE = "cosyvoice";
export const ELEVENLABS = "elevenlabs";

export const TTS_PROVIDER_LABELS = {
  [COSYVOICE]: "CosyVoice（自有配音机）",
  [ELEVENLABS]: "ElevenLabs（付费 TTS）",
};

const text = (value) => String(value ?? "").trim();

export function ttsProviderLabel(value) {
  return TTS_PROVIDER_LABELS[text(value)] || TTS_PROVIDER_LABELS[COSYVOICE];
}

export function isElevenLabs(value) {
  return text(value) === ELEVENLABS;
}

/** 频道/项目定位配音源：缺省一律回落到 CosyVoice（历史数据不回填，§23.1）。 */
export function providerOf(entity) {
  return isElevenLabs(entity?.tts_provider ?? entity?.default_tts_provider)
    ? ELEVENLABS
    : COSYVOICE;
}

/** 冻结快照的配音源（已冻结项目以快照为准）。 */
export function frozenProvider(project) {
  return providerOf(project?.snapshot ?? project);
}

/**
 * 音色分配：返回创建/保存项目时要提交的配音源与音色字段。
 * EL 源必须给出本频道克隆音色；CosyVoice 源必须给出公共音色版本。
 */
export function voiceAssignment(provider, { voiceVersionId = null, clonedVoiceId = null } = {}) {
  if (isElevenLabs(provider)) {
    return {
      ok: Boolean(text(clonedVoiceId)),
      reason: text(clonedVoiceId) ? "" : "请选择本频道的克隆音色。",
      patch: { tts_provider: ELEVENLABS, cloned_voice_id: text(clonedVoiceId) || null },
    };
  }
  return {
    ok: Boolean(text(voiceVersionId)),
    reason: text(voiceVersionId) ? "" : "请选择公共音色。",
    patch: { tts_provider: COSYVOICE, voice_version_id: text(voiceVersionId) || null },
  };
}

/**
 * 频道默认音色更新（UX 方案A，2026-10-14 确认）：把某个音色设为频道默认的同时启用其来源，
 * 返回 PATCH /identity/channels/{id} 的字段组合；跨源时一并切换 default_tts_provider。
 */
export function channelDefaultPatch(provider, { clonedVoiceId = null, voiceVersionId = null } = {}) {
  if (isElevenLabs(provider)) {
    if (!text(clonedVoiceId)) {
      return { ok: false, reason: "请选择本频道的克隆音色。", patch: null };
    }
    return {
      ok: true,
      reason: "",
      patch: { default_tts_provider: ELEVENLABS, default_cloned_voice_id: text(clonedVoiceId) },
    };
  }
  if (!text(voiceVersionId)) {
    return { ok: false, reason: "请选择公共音色。", patch: null };
  }
  return {
    ok: true,
    reason: "",
    patch: { default_tts_provider: COSYVOICE, default_voice_version_id: text(voiceVersionId) },
  };
}

/** 频道默认音色是否已就绪（§3.13/§23.2：未设置默认音色不能创建项目）。 */
export function channelDefaultVoiceReady(channel) {
  if (!channel) return false;
  return providerOf(channel) === ELEVENLABS
    ? Boolean(text(channel.default_cloned_voice_id))
    : Boolean(text(channel.default_voice_version_id));
}

/** 频道默认音色分配（用于创建向导预选）。 */
export function channelDefaultAssignment(channel) {
  return providerOf(channel) === ELEVENLABS
    ? { provider: ELEVENLABS, clonedVoiceId: channel?.default_cloned_voice_id || null }
    : { provider: COSYVOICE, clonedVoiceId: null };
}

/** 克隆音色视图模型：列表项 + 受保护判定 + 保护原因文案（§23.5）。 */
export function clonedVoiceViews(voices, { defaultClonedVoiceId = null, protectedIds = [] } = {}) {
  const protectedSet = new Set((Array.isArray(protectedIds) ? protectedIds : []).map(text));
  const defaultId = text(defaultClonedVoiceId);
  return (Array.isArray(voices) ? voices : []).map((voice) => {
    const id = text(voice?.id);
    const isDefault = Boolean(defaultId) && id === defaultId;
    const isProtected = isDefault || protectedSet.has(id);
    return {
      id,
      name: text(voice?.name) || "未命名音色",
      remark: text(voice?.remark),
      createdAt: text(voice?.created_at),
      sourceUrl: text(voice?.source_url),
      sourceSha256: text(voice?.source_sha256),
      sizeBytes: Number(voice?.size_bytes ?? voice?.source_size_bytes ?? 0) || 0,
      isDefault,
      protected: isProtected,
      protectionReason: isDefault
        ? "频道默认音色，删除前请先改选默认音色。"
        : protectedSet.has(id)
          ? "被冻结项目或草稿引用，暂不可删除。"
          : "",
      canDelete: Boolean(id) && !isProtected,
    };
  });
}

/** 上传表单校验（与后端同口径：名称必填、源音频必选）。 */
export function validateClonedVoiceUpload({ name, file, fileName = "" } = {}) {
  if (!text(name)) return { ok: false, reason: "请填写音色名称。" };
  const resolved = fileName || file?.name;
  if (!resolved) return { ok: false, reason: "请选择用于克隆的源音频文件。" };
  if (!Number(file?.size ?? 1)) return { ok: false, reason: "源音频文件为空。" };
  return { ok: true, reason: "" };
}

/** 逐账号克隆结果汇总（如实展示失败账号，不掩盖）。 */
export function cloneOutcomeSummary(outcomes) {
  const items = Array.isArray(outcomes) ? outcomes : [];
  const ok = items.filter((item) => item?.ok);
  const failed = items.filter((item) => item && !item.ok);
  return {
    total: items.length,
    okCount: ok.length,
    failedCount: failed.length,
    failedLabels: failed.map((item) => text(item?.label) || "未命名账号"),
    detail: items.length === 0
      ? "号池中没有可用的 active key，音色尚未克隆到任何账号。"
      : failed.length === 0
        ? `已克隆到 ${ok.length} 个账号。`
        : `已克隆到 ${ok.length} 个账号，${failed.length} 个账号失败：${failed
            .map((item) => `${text(item?.label) || "未命名账号"}（${text(item?.error_code) || "未知错误"}）`)
            .join("、")}`,
  };
}

/** 配音设置页展示即将冻结的配音源/音色/模型（§23.1）。 */
export function frozenNarrationSummary(project) {
  const provider = frozenProvider(project);
  const snapshot = project?.snapshot || {};
  return {
    provider,
    providerLabel: ttsProviderLabel(provider),
    voiceName: isElevenLabs(provider)
      ? text(snapshot.cloned_voice_name) || text(project?.cloned_voice_name) || "克隆音色"
      : text(project?.voice_display_name) || "公共音色",
    frozen: Boolean(project?.snapshot),
  };
}
