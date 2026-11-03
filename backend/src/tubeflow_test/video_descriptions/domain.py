# ruff: noqa: E501  默认提示词与本地化文案属于模板数据，长行不按代码行宽约束
"""视频简介的纯领域规则：模板占位符、最终 SRT 解析、模型输出校验与确定性拼装。

本模块不依赖 FastAPI、SQLAlchemy、对象存储或具体 Provider SDK。模型只返回章节
标题与起始 SRT 序号；真实时间与频道名永远由本模块注入最终文本。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from tubeflow_test.domain.errors import DomainError

DESCRIPTION_PROMPT_LIMIT = 20_000
DESCRIPTION_CONTRACT_VERSION = "video-description-v1"
MIN_SEGMENTS = 5
MAX_SEGMENTS = 9
MIN_LEARN_POINTS = 5
MAX_LEARN_POINTS = 7
DISCLAIMER_KEYS = frozenset({"general", "finance", "sensitive"})
REQUIRED_PLACEHOLDERS = frozenset({"topic", "language", "script", "channel_name", "timeline"})
_ALLOWED_PLACEHOLDERS = REQUIRED_PLACEHOLDERS | frozenset({"channel_url"})
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
_SRT_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)
_TIMECODE_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
DIVIDER = "─" * 41

DEFAULT_DESCRIPTION_PROMPT = """你现在是:
YouTube 视频简介撰写专家(Description Writer)。

任务:
根据给定视频脚本文案、频道信息和最终 SRT 时间线,生成 YouTube 视频简介所需的内容素材。简介的固定骨架(小节标题、真实时间、分隔线、订阅引导、免责声明)由程序拼接,你只负责生成下列内容素材,不输出完整简介。

输入:

【视频主题(即视频标题)】
{{topic}}

【频道名称(订阅引导由程序使用,不要编造其他频道名或链接)】
{{channel_name}}

【目标语言(所有产出文本必须使用该语言)】
{{language}}

【脚本文案全文】
{{script}}

【最终 SRT 时间线(格式: 序号 | 开始 | 结束 | 字幕文本)】
{{timeline}}

生成规则:
1. hook:开头钩子段,2-4 句话。制造认知缺口或反差,概括"观众以为 X、实际是 Y"的核心张力,不剧透具体结论,不复述标题。
2. intro_title:引子小节的标题(2-6 个词),概括脚本开头引子的功能。Intro 的实际时间由程序固定取 SRT 第 1 条起点。
3. segments:5-9 个正文章节对象,按脚本顺序概括全片结构。每个对象必须包含:
   - entry:该章节起始位置对应的最终 SRT 序号;只能填写输入时间线中存在的整数序号,必须大于 1 且严格递增;
   - title:该章节标题,不超过 12 个词,只概括、不照抄原文句子;
   若脚本是"等级/段位/金额区间"递进结构,标题可保留等级和范围语义,但不要自行编写任何时间文本。
4. learn_points:5-7 条观众收获要点,每条一句话(不超过 20 个词),以具体认知收益为落点,禁止空泛口号。
5. closing:收尾段,1-2 句话,呼应主题并点明"无论观众处于哪个阶段,这条视频都适用"。
6. disclaimer_key:从以下三个中选一个最贴合脚本题材的:
   - "general":通用娱乐/教育内容;
   - "finance":涉及投资、理财、赚钱方法;
   - "sensitive":涉及犯罪、黑帮、暴力、监狱等争议题材。
7. 所有产出文本必须使用【目标语言】指定的语言(即使脚本中夹杂其他语言,也必须统一为目标语言)。
8. 禁止出现 # 符号;禁止出现具体时间码或时间段文本;禁止编造脚本中不存在的精确统计数据、频道链接、社交账号或来源。

输出契约(严格遵守):
- 只输出一个 JSON 对象,禁止代码围栏,禁止任何解释、注释或元评论;
- 字段齐全,结构如下:

{
  "hook": "...",
  "intro_title": "...",
  "segments": [{"entry": 2, "title": "..."}],
  "learn_points": ["...", "..."],
  "closing": "...",
  "disclaimer_key": "general"
}
"""

_DESCRIPTION_SYSTEM_PROMPT = """你是视频简介生成器。用户消息中的模板是本次生成的唯一风格与结构指令；最终 SRT、频道和文案是权威输入。你必须只输出一个 JSON 对象，并把章节位置表示为真实存在的 SRT 整数序号。你不得编写时间码、频道链接、社交账号、参考资料或脚本外事实；实际时间与频道名由系统拼装。"""


@dataclass(frozen=True, slots=True)
class SrtEntry:
    sequence: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class DescriptionTemplate:
    id: UUID
    channel_id: UUID
    version: int
    prompt: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DescriptionProjectSource:
    project_id: UUID
    owner_id: UUID
    channel_id: UUID
    title: str
    script: str
    locale: str
    channel_name: str
    channel_url: str
    composition_id: UUID
    srt_asset_id: UUID
    srt_sha256: str
    srt_entries: tuple[SrtEntry, ...]

    @property
    def script_sha256(self) -> str:
        return hashlib.sha256(self.script.encode("utf-8")).hexdigest()

    @property
    def duration_ms(self) -> int:
        return self.srt_entries[-1].end_ms if self.srt_entries else 0


@dataclass(frozen=True, slots=True)
class DescriptionSegment:
    entry: int
    title: str


@dataclass(frozen=True, slots=True)
class DescriptionPayload:
    hook: str
    intro_title: str
    segments: tuple[DescriptionSegment, ...]
    learn_points: tuple[str, ...]
    closing: str
    disclaimer_key: str

    def as_dict(self) -> dict[str, object]:
        return {
            "hook": self.hook,
            "intro_title": self.intro_title,
            "segments": [
                {"entry": item.entry, "title": item.title} for item in self.segments
            ],
            "learn_points": list(self.learn_points),
            "closing": self.closing,
            "disclaimer_key": self.disclaimer_key,
        }


@dataclass(frozen=True, slots=True)
class DescriptionAttempt:
    id: UUID
    project_id: UUID
    owner_id: UUID
    idempotency_key: str
    template_id: UUID
    template_version: int
    input_sha256: str
    status: str
    error_code: str | None
    description_id: UUID | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProjectDescription:
    id: UUID
    project_id: UUID
    owner_id: UUID
    version_number: int
    source: str
    base_version_id: UUID | None
    prompt_template_version: int | None
    prompt_template_sha256: str
    prompt_template_snapshot: str
    title_used: str
    channel_name_used: str
    locale: str
    script_sha256: str
    srt_asset_id: UUID | None
    srt_sha256: str
    payload: dict[str, Any]
    content: str
    disclaimer_key: str
    model: str | None
    provider_receipt: str
    raw_response_sha256: str
    provider_calls: int
    elapsed_ms: int
    current: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DescriptionPageState:
    project_id: UUID
    channel_id: UUID
    channel_name: str
    available: bool
    blocking_reason: str | None
    timeline_entries: int
    duration_ms: int
    current: ProjectDescription | None
    versions: tuple[ProjectDescription, ...]
    active_attempt: DescriptionAttempt | None


@dataclass(frozen=True, slots=True)
class _LocalePack:
    name: str
    timestamps: str
    intro: str
    learn: str
    subscribe: str
    disclaimer_label: str
    disclaimers: dict[str, str]


_LOCALES: dict[str, _LocalePack] = {
    "en-US": _LocalePack(
        "English",
        "TIMESTAMPS",
        "Intro",
        "What You'll Learn:",
        "Subscribe to {channel_name} for weekly videos.",
        "DISCLAIMER",
        {
            "general": "This video is for educational and entertainment purposes only. The content is based on publicly available information and narrative interpretation. Always do your own research.",
            "finance": "This video is for educational and entertainment purposes only. Nothing in this video constitutes financial, investment, or legal advice. Always do your own research and consult a licensed financial professional before making investment decisions.",
            "sensitive": "This video is a work of educational and entertainment content. It does not endorse, glorify, or encourage any illegal, violent, or harmful activities. Viewer discretion is advised.",
        },
    ),
    "ko-KR": _LocalePack(
        "Korean",
        "타임스탬프",
        "인트로",
        "이 영상에서 배우게 될 것:",
        "{channel_name} 채널을 구독하고 매주 새로운 영상을 받아 보세요.",
        "면책 조항",
        {
            "general": "이 영상은 교육 및 오락 목적으로만 제작되었습니다. 공개된 자료와 서사적 해석을 바탕으로 하며, 관련 정보는 직접 확인하시기 바랍니다.",
            "finance": "이 영상은 교육 및 오락 목적으로만 제작되었습니다. 이 영상의 어떤 내용도 금융, 투자 또는 법률 조언에 해당하지 않습니다. 투자 결정을 내리기 전에 반드시 스스로 조사하고 공인 금융 전문가와 상담하시기 바랍니다.",
            "sensitive": "이 영상은 교육 및 오락 목적의 콘텐츠입니다. 어떠한 불법적, 폭력적 또는 유해한 행위도 옹호하거나 미화하거나 조장하지 않습니다. 시청에 주의하시기 바랍니다.",
        },
    ),
    "ja-JP": _LocalePack(
        "Japanese",
        "タイムスタンプ",
        "イントロ",
        "この動画で学べること:",
        "{channel_name}をチャンネル登録して、毎週の新着動画をお見逃しなく。",
        "免責事項",
        {
            "general": "この動画は教育および娯楽目的でのみ作成されています。公開情報と物語的解釈に基づく内容です。関連情報はご自身でご確認ください。",
            "finance": "この動画は教育および娯楽目的でのみ作成されています。この動画のいかなる内容も、金融・投資・法律に関する助言を構成するものではありません。投資判断を行う前に、必ずご自身で調査し、資格を持つ金融専門家にご相談ください。",
            "sensitive": "この動画は教育および娯楽目的のコンテンツです。違法、暴力的、有害な行為を支持、美化、奨励するものではありません。視聴の際はご注意ください。",
        },
    ),
    "es-ES": _LocalePack(
        "Spanish",
        "MARCAS DE TIEMPO",
        "Intro",
        "Lo que aprenderás:",
        "Suscríbete a {channel_name} para ver videos nuevos cada semana.",
        "AVISO LEGAL",
        {
            "general": "Este video tiene fines exclusivamente educativos y de entretenimiento. El contenido se basa en información pública e interpretación narrativa. Investiga siempre por tu cuenta.",
            "finance": "Este video tiene fines exclusivamente educativos y de entretenimiento. Nada de lo contenido en este video constituye asesoramiento financiero, de inversión o legal. Investiga siempre por tu cuenta y consulta a un profesional financiero autorizado antes de tomar decisiones de inversión.",
            "sensitive": "Este video es un contenido educativo y de entretenimiento. No respalda, glorifica ni fomenta ninguna actividad ilegal, violenta o dañina. Se recomienda discreción al espectador.",
        },
    ),
    "fr-FR": _LocalePack(
        "French",
        "HORODATAGES",
        "Intro",
        "Ce que vous allez apprendre :",
        "Abonnez-vous à {channel_name} pour de nouvelles vidéos chaque semaine.",
        "AVERTISSEMENT",
        {
            "general": "Cette vidéo est destinée uniquement à des fins éducatives et de divertissement. Le contenu repose sur des informations publiques et une interprétation narrative. Faites toujours vos propres recherches.",
            "finance": "Cette vidéo est destinée uniquement à des fins éducatives et de divertissement. Rien dans cette vidéo ne constitue un conseil financier, d'investissement ou juridique. Faites toujours vos propres recherches et consultez un professionnel financier agréé avant de prendre des décisions d'investissement.",
            "sensitive": "Cette vidéo est un contenu éducatif et de divertissement. Elle n'approuve, ne glorifie ni n'encourage aucune activité illégale, violente ou nuisible. La discrétion du spectateur est conseillée.",
        },
    ),
    "pt-BR": _LocalePack(
        "Portuguese",
        "MARCAS DE TEMPO",
        "Introdução",
        "O que você vai aprender:",
        "Inscreva-se no {channel_name} para receber vídeos novos toda semana.",
        "AVISO LEGAL",
        {
            "general": "Este vídeo tem fins exclusivamente educacionais e de entretenimento. O conteúdo é baseado em informações públicas e interpretação narrativa. Faça sempre sua própria pesquisa.",
            "finance": "Este vídeo tem fins exclusivamente educacionais e de entretenimento. Nada neste vídeo constitui aconselhamento financeiro, de investimento ou jurídico. Faça sempre sua própria pesquisa e consulte um profissional financeiro licenciado antes de tomar decisões de investimento.",
            "sensitive": "Este vídeo é um conteúdo educacional e de entretenimento. Não endossa, glorifica nem incentiva nenhuma atividade ilegal, violenta ou prejudicial. Recomenda-se discrição do espectador.",
        },
    ),
}


def validate_template_prompt(value: str) -> str:
    prompt = value.strip()
    if not prompt:
        raise DomainError("description_template_empty", "简介提示词模板不能为空。")
    if "\x00" in prompt:
        raise DomainError("invalid_parameter", "简介提示词模板不能包含空字符。")
    if len(prompt) > DESCRIPTION_PROMPT_LIMIT:
        raise DomainError(
            "description_template_prompt_too_long",
            f"简介提示词模板最多 {DESCRIPTION_PROMPT_LIMIT} 个字符，当前 {len(prompt)} 个。",
        )
    placeholders = set(_PLACEHOLDER_RE.findall(prompt))
    unknown = sorted(placeholders - _ALLOWED_PLACEHOLDERS)
    if unknown:
        raise DomainError(
            "description_template_placeholder_unknown",
            f"简介提示词模板包含未知占位符：{', '.join(unknown)}。",
        )
    missing = sorted(REQUIRED_PLACEHOLDERS - placeholders)
    if missing:
        raise DomainError(
            "description_template_placeholder_required",
            f"简介提示词模板缺少必需占位符：{', '.join(missing)}。",
        )
    return prompt


def parse_srt(content: bytes) -> tuple[SrtEntry, ...]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError("description_srt_invalid", "最终 SRT 不是有效 UTF-8 文本。") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise DomainError("description_srt_invalid", "最终 SRT 为空。")
    entries: list[SrtEntry] = []
    previous_end = 0
    for block in re.split(r"\n[ \t]*\n+", normalized):
        lines = [line.rstrip("\n") for line in block.split("\n")]
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise DomainError("description_srt_invalid", "最终 SRT 块结构不完整。")
        sequence = int(lines[0].strip())
        match = _SRT_TIME_RE.fullmatch(lines[1].strip())
        if match is None:
            raise DomainError("description_srt_invalid", "最终 SRT 时间行格式无效。")
        numbers = [int(value) for value in match.groups()]
        start_ms = ((numbers[0] * 60 + numbers[1]) * 60 + numbers[2]) * 1000 + numbers[3]
        end_ms = ((numbers[4] * 60 + numbers[5]) * 60 + numbers[6]) * 1000 + numbers[7]
        caption = "\n".join(lines[2:]).strip()
        if (
            sequence != len(entries) + 1
            or start_ms != previous_end
            or end_ms <= start_ms
            or not caption
        ):
            raise DomainError(
                "description_srt_invalid", "最终 SRT 序号、时间轴或字幕内容不完整。"
            )
        entries.append(SrtEntry(sequence, start_ms, end_ms, caption))
        previous_end = end_ms
    return tuple(entries)


def render_template_prompt(template: str, source: DescriptionProjectSource) -> str:
    locale = _locale(source.locale)
    rendered = template
    replacements = {
        "topic": source.title,
        "language": locale.name,
        "script": source.script,
        "channel_name": source.channel_name,
        "channel_url": source.channel_url,
        "timeline": timeline_prompt(source.srt_entries),
    }
    for key, value in replacements.items():
        rendered = rendered.replace("{{" + key + "}}", value)
        rendered = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", value, rendered)
    return rendered


def timeline_prompt(entries: tuple[SrtEntry, ...]) -> str:
    return "\n".join(
        f"{entry.sequence} | {_srt_time(entry.start_ms)} --> {_srt_time(entry.end_ms)} | "
        f"{entry.text.replace(chr(10), ' / ')}"
        for entry in entries
    )


def build_description_messages(
    template: str, source: DescriptionProjectSource, *, repair_reason: str = ""
) -> list[dict[str, str]]:
    prompt = render_template_prompt(template, source)
    if repair_reason:
        prompt += (
            "\n\n上一次输出未通过校验："
            + repair_reason
            + "。请严格按输出契约重新输出完整 JSON 对象，不要输出任何其他内容。"
        )
    return [
        {"role": "system", "content": _DESCRIPTION_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def parse_description_payload(
    raw: object, entries: tuple[SrtEntry, ...]
) -> DescriptionPayload:
    if not isinstance(raw, dict):
        raise _invalid_output("输出不是 JSON 对象")
    allowed = {"hook", "intro_title", "segments", "learn_points", "closing", "disclaimer_key"}
    if set(raw) != allowed:
        raise _invalid_output("输出字段集合不符合合同")
    hook = _clean_text(raw.get("hook"), "hook")
    intro_title = _clean_text(raw.get("intro_title"), "intro_title")
    closing = _clean_text(raw.get("closing"), "closing")
    raw_segments = raw.get("segments")
    if not isinstance(raw_segments, list) or not MIN_SEGMENTS <= len(raw_segments) <= MAX_SEGMENTS:
        raise _invalid_output(f"正文章节数量必须为 {MIN_SEGMENTS}-{MAX_SEGMENTS} 个")
    segments: list[DescriptionSegment] = []
    previous_entry = 1
    for item in raw_segments:
        if not isinstance(item, dict) or set(item) != {"entry", "title"}:
            raise _invalid_output("正文章节必须只包含 entry 与 title")
        entry = item.get("entry")
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise _invalid_output("正文章节 entry 必须是整数 SRT 序号")
        if entry <= previous_entry or entry > len(entries):
            raise _invalid_output("正文章节 entry 必须大于 1、严格递增且不超出 SRT 范围")
        title = _clean_text(item.get("title"), "segments[].title")
        segments.append(DescriptionSegment(entry, title))
        previous_entry = entry
    raw_learn = raw.get("learn_points")
    if not isinstance(raw_learn, list) or not MIN_LEARN_POINTS <= len(raw_learn) <= MAX_LEARN_POINTS:
        raise _invalid_output(
            f"观众收获数量必须为 {MIN_LEARN_POINTS}-{MAX_LEARN_POINTS} 条"
        )
    learn_points = tuple(_clean_text(item, "learn_points[]") for item in raw_learn)
    disclaimer_key = raw.get("disclaimer_key")
    if disclaimer_key not in DISCLAIMER_KEYS:
        raise _invalid_output("disclaimer_key 必须是 general/finance/sensitive 之一")
    return DescriptionPayload(
        hook,
        intro_title,
        tuple(segments),
        learn_points,
        closing,
        str(disclaimer_key),
    )


def assemble_description(
    payload: DescriptionPayload,
    source: DescriptionProjectSource,
) -> str:
    locale = _locale(source.locale)
    lines = [
        payload.hook,
        "",
        "⏱️ " + locale.timestamps,
        f"{format_description_time(0)} {locale.intro}: {payload.intro_title}",
    ]
    lines.extend(
        f"{format_description_time(source.srt_entries[item.entry - 1].start_ms)} {item.title}"
        for item in payload.segments
    )
    lines.extend(
        [
            "",
            "💡 " + locale.learn,
            *("→ " + point for point in payload.learn_points),
            "",
            "📌 " + payload.closing,
            "",
            DIVIDER,
            "🔔 " + locale.subscribe.format(channel_name=source.channel_name),
            DIVIDER,
            "",
            "⚠️ "
            + locale.disclaimer_label
            + ": "
            + locale.disclaimers[payload.disclaimer_key],
        ]
    )
    return "\n".join(lines)


def format_description_time(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, second = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02}:{second:02}"
    return f"{minutes}:{second:02}"


def description_input_sha256(template: DescriptionTemplate, source: DescriptionProjectSource) -> str:
    canonical = {
        "contract": DESCRIPTION_CONTRACT_VERSION,
        "title": source.title,
        "locale": source.locale,
        "channel_name": source.channel_name,
        "script_sha256": source.script_sha256,
        "srt_sha256": source.srt_sha256,
        "template_version": template.version,
        "template_sha256": prompt_sha256(template.prompt),
    }
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def validate_description_content(value: str) -> str:
    content = value.strip()
    if not content:
        raise DomainError("description_content_empty", "简介内容不能为空。")
    if "\x00" in content:
        raise DomainError("invalid_parameter", "简介内容不能包含空字符。")
    return content


def _clean_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _invalid_output(f"{field} 必须是文本")
    if "#" in value:
        raise _invalid_output(f"{field} 不能包含 #")
    text = value.strip()
    if not text or "\x00" in text:
        raise _invalid_output(f"{field} 不能为空或包含空字符")
    if _TIMECODE_RE.search(text):
        raise _invalid_output(f"{field} 不能包含时间码")
    return text


def _invalid_output(reason: str) -> DomainError:
    return DomainError("description_output_invalid", f"简介模型输出未通过校验：{reason}")


def _locale(locale: str) -> _LocalePack:
    return _LOCALES.get(locale, _LOCALES["en-US"])


def _srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"
