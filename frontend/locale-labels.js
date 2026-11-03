// 项目语言代码 → 中文展示名：只做展示换算，不触碰 DOM、网络或全局状态，便于 node:test 覆盖。
// 边界：API 请求/响应、数据库、项目快照与 AI 提示词一律继续使用原始 BCP-47 代码，
// 本模块只供语言下拉选项、项目卡片、提示文案与音色卡「支持语言」使用。
const DEFINITIONS = Object.freeze([
  Object.freeze({ code: "en-US", label: "英语（美国）", shortLabel: "英语" }),
  Object.freeze({ code: "ja-JP", label: "日语（日本）", shortLabel: "日语" }),
  Object.freeze({ code: "ko-KR", label: "韩语（韩国）", shortLabel: "韩语" }),
  Object.freeze({ code: "es-ES", label: "西班牙语（西班牙）", shortLabel: "西班牙语" }),
  Object.freeze({ code: "fr-FR", label: "法语（法国）", shortLabel: "法语" }),
  Object.freeze({ code: "pt-BR", label: "葡萄牙语（巴西）", shortLabel: "葡萄牙语" }),
]);

const BY_CODE = new Map(DEFINITIONS.map((entry) => [entry.code, entry]));
const text = (value) => String(value ?? "").trim();

// 首期六种语言的固定顺序；en-US 保持第一项，等价于新建项目的默认选项。
export const LOCALES = Object.freeze(DEFINITIONS.map((entry) => entry.code));

// 长名：带地区括注，用于下拉框、项目卡片与提示文案；未知代码原样回退，空值回退空串。
export function localeLabel(code) {
  const key = text(code);
  return BY_CODE.get(key)?.label || key;
}

// 短名：不带地区与代码，用于音色卡等紧凑位置。
export function localeShortLabel(code) {
  const key = text(code);
  return BY_CODE.get(key)?.shortLabel || key;
}

// 音色卡「支持语言」：短名、保序、去重，未知代码保留原码。
export function localeShortListText(codes, separator = " · ") {
  const labels = (Array.isArray(codes) ? codes : []).map(localeShortLabel).filter(Boolean);
  return [...new Set(labels)].join(separator);
}
