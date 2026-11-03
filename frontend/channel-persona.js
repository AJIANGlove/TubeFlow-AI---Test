// 频道默认角色 IP 的展示层视图模型。
// 只放不依赖 DOM 的纯函数：上限常量、星标切换、可用性归一化与有序解析，
// 便于 node --test 覆盖；app.js 负责渲染与事件绑定。
// default_character_image_ids 有序、最多 3 个、可为空数组；顺序即「默认①/②/③」角标序号。

export const MAX_DEFAULT_CHARACTER_IMAGES = 3;

export function toggleDefaultCharacterId(ids, id) {
  const current = Array.isArray(ids) ? ids.filter((value) => typeof value === "string" && value) : [];
  const target = typeof id === "string" ? id : "";
  if (!target) return { ok: false, reason: "invalid" };
  if (current.includes(target)) {
    return { ok: true, ids: current.filter((value) => value !== target) };
  }
  if (current.length >= MAX_DEFAULT_CHARACTER_IMAGES) return { ok: false, reason: "limit" };
  return { ok: true, ids: [...current, target] };
}

export function normalizeDefaultCharacterIds(ids, availableIds) {
  const available = Array.isArray(availableIds) ? new Set(availableIds) : null;
  const result = [];
  for (const value of Array.isArray(ids) ? ids : []) {
    if (typeof value !== "string" || !value) continue;
    if (result.includes(value)) continue;
    if (available && !available.has(value)) continue;
    result.push(value);
    if (result.length >= MAX_DEFAULT_CHARACTER_IMAGES) break;
  }
  return result;
}

export function resolveDefaultCharacterImages(channel, images) {
  const byId = new Map(
    (Array.isArray(images) ? images : [])
      .filter((image) => typeof image?.id === "string" && image.id)
      .map((image) => [image.id, image]),
  );
  return normalizeDefaultCharacterIds(channel?.default_character_image_ids, null)
    .map((id) => byId.get(id))
    .filter(Boolean);
}
