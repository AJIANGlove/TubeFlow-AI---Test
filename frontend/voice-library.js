export const VOICE_PAGE_SIZE = 24;

const text = (value) => String(value ?? "").trim();
const normalized = (value) => text(value).toLocaleLowerCase("zh-CN");

export function voiceSupportsLocale(voice, locale) {
  return Array.isArray(voice?.supported_locales) && voice.supported_locales.includes(locale);
}

export function compatibleLocaleVoicePatch(voices, locale, voiceVersionId) {
  const voice = (Array.isArray(voices) ? voices : []).find((candidate) => candidate.id === voiceVersionId);
  if (!voice || !voiceSupportsLocale(voice, locale)) return null;
  return { locale, voice_version_id: voice.id };
}

export function shortStableId(value, length = 8) {
  const source = text(value);
  return source.length > length ? source.slice(0, length) : source;
}

export function voiceIdentity(voice) {
  return `v${text(voice?.version) || "?"} · voice ${shortStableId(voice?.voice_id, 12) || "?"} · UUID ${shortStableId(voice?.id) || "?"}`;
}

export function filterVoices(voices, filters = {}) {
  const query = normalized(filters.query);
  const locale = text(filters.locale);
  const gender = text(filters.gender);
  const ageBand = text(filters.ageBand);
  const accent = text(filters.accent);
  const tag = text(filters.tag);

  return (Array.isArray(voices) ? voices : []).filter((voice) => {
    if (locale && !voiceSupportsLocale(voice, locale)) return false;
    if (gender && text(voice?.gender) !== gender) return false;
    if (ageBand && text(voice?.age_band) !== ageBand) return false;
    if (accent && text(voice?.accent) !== accent) return false;
    if (tag && !(Array.isArray(voice?.tags) && voice.tags.includes(tag))) return false;
    if (!query) return true;
    const searchable = [
      voice?.display_name,
      voice?.voice_id,
      voice?.id,
      voice?.description,
      voice?.accent,
      voice?.accent_zh,
      voice?.author,
      ...(Array.isArray(voice?.tags) ? voice.tags : []),
      ...(Array.isArray(voice?.supported_locales) ? voice.supported_locales : []),
    ].map(normalized).join(" ");
    return searchable.includes(query);
  });
}

export function paginateVoices(voices, requestedPage = 1, pageSize = VOICE_PAGE_SIZE) {
  const size = Math.max(1, Math.floor(Number(pageSize) || VOICE_PAGE_SIZE));
  const total = Array.isArray(voices) ? voices.length : 0;
  const pages = Math.max(1, Math.ceil(total / size));
  const page = Math.min(pages, Math.max(1, Math.floor(Number(requestedPage) || 1)));
  const start = (page - 1) * size;
  return { items: voices.slice(start, start + size), page, pages, total };
}

export function voiceFacetValues(voices, key) {
  const values = new Set();
  for (const voice of Array.isArray(voices) ? voices : []) {
    const candidate = voice?.[key];
    if (Array.isArray(candidate)) {
      for (const value of candidate) if (text(value)) values.add(text(value));
    } else if (text(candidate)) {
      values.add(text(candidate));
    }
  }
  return [...values].sort((left, right) => left.localeCompare(right, "zh-CN"));
}
