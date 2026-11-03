"""封面图生成领域规则：纯函数规则引擎，零框架/零 I/O 依赖（仅标准库）。

语义移植自《参考图勾选生成_实现交接文档》的 JS 实现，并按
`docs/planning/COVER_GENERATION_PLAN_20260920.md`（v4 基线）做通用化改造：

- 移除 scene 枚举、景别体系、题材词库与 7 个题材专用锚点；锚点收敛为
  主体完整（framing_anchor）与通用质量（恒定追加、不让步）2 个；
- 内容类审计（风格/时代/夜暗/特写/下跪）默认不激活，仅 `active_rules` 显式启用；
- 装配层不做总长截断（修复参考实现 2000 码元截断丢字段缺陷）；
- 新增封面文字串行链路（独立文字调用 + 绘字段确定性装配）；
- 评委 rubric 为 7 维通用版（每维 0-20，总分 140）。

长度口径：JS 的 `String.length`/`slice` 按 UTF-16 码元计数，Python `str` 按
Unicode 码点计数。本模块所有限长（FIELD_MAX/REF_CONSTRAINT_MAX/COVER_TEXT_MAX
等）均以码点为准，对基本多文种平面以外的字符与 JS 原版存在理论差异。
"""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from tubeflow_test.domain.errors import DomainError

FIELD_MAX = 200
REF_CONSTRAINT_MAX = 2000
COVER_TEXT_MAX = 30
BRIEF_MAX = 500
PROMPT_MAX = 4000
COVER_VARIANT_TEMPS = (0.8, 1.0)
COVER_TEXT_TEMPERATURE = 0.9
JUDGE_TEMPERATURE = 0.2
RATIOS = ("16:9", "9:16", "1:1")


@dataclass(frozen=True, slots=True)
class RefDimension:
    """八维度分类表条目：维度 key、中文标签与有序子项（附录 A.1 逐字移植）。"""

    key: str
    label: str
    children: tuple[str, ...]


REF_DIMENSIONS: tuple[RefDimension, ...] = (
    RefDimension("subject", "主体", ("人物身份", "外貌", "动作", "表情", "服装")),
    RefDimension("spatial", "空间", ("前景", "中景", "背景", "环境", "道具")),
    RefDimension("composition", "构图", ("人物占比", "站位", "画面比例", "留白", "视觉中心")),
    RefDimension("camera", "镜头", ("焦距", "机位", "视角", "景深", "对焦")),
    RefDimension("lighting", "光影", ("光源", "方向", "强弱", "阴影")),
    RefDimension("color", "色彩", ("主色", "辅色", "饱和度", "色彩情绪")),
    RefDimension("material", "材质", ("衣料", "皮肤", "金属", "环境纹理")),
    RefDimension("style", "风格", ("艺术流派", "时代感", "设计语言", "后期效果")),
)

# 子项规则让步映射（附录 A.2 全 36 项逐字移植）。通用版仅 `framing_anchor`
# 在装配层有激活行为（provided → 跳过主体完整锚点补入）；`no_text` 无激活行为
# （画面本就需要绘字）；其余键为休眠数据，供审计激活矩阵与后续启用使用。
REF_CHILD_RULE_MAP: dict[str, tuple[str, ...]] = {
    "subject::人物身份": ("face_anchor",),
    "subject::外貌": ("face_anchor",),
    "subject::动作": ("kneel_limit", "hand_rule"),
    "subject::表情": (),
    "subject::服装": ("era_words",),
    "spatial::前景": ("richness_anchor",),
    "spatial::中景": ("richness_anchor",),
    "spatial::背景": ("richness_anchor", "scene_dup", "era_words"),
    "spatial::环境": ("richness_anchor", "scene_dup", "era_words"),
    "spatial::道具": ("richness_anchor",),
    "composition::人物占比": ("shot_diversity", "scene_dup", "framing_anchor", "framing_words"),
    "composition::站位": ("shot_diversity", "scene_dup"),
    "composition::画面比例": (),
    "composition::留白": ("no_text",),
    "composition::视觉中心": ("shot_diversity", "scene_dup", "framing_anchor", "framing_words"),
    "camera::焦距": ("shot_diversity", "scene_dup", "framing_anchor", "framing_words"),
    "camera::机位": ("shot_diversity", "scene_dup"),
    "camera::视角": ("shot_diversity", "scene_dup", "framing_anchor", "framing_words"),
    "camera::景深": ("shot_diversity", "scene_dup", "framing_anchor", "framing_words"),
    "camera::对焦": (),
    "lighting::光源": ("daylight_anchor", "dark_words", "saturation_word"),
    "lighting::方向": ("daylight_anchor", "dark_words", "saturation_word"),
    "lighting::强弱": ("daylight_anchor", "dark_words", "saturation_word"),
    "lighting::阴影": ("daylight_anchor", "dark_words", "saturation_word"),
    "color::主色": ("daylight_anchor", "dark_words", "saturation_word"),
    "color::辅色": ("daylight_anchor", "dark_words", "saturation_word"),
    "color::饱和度": ("saturation_word",),
    "color::色彩情绪": ("daylight_anchor", "dark_words", "saturation_word"),
    "material::衣料": ("era_words",),
    "material::皮肤": (),
    "material::金属": (),
    "material::环境纹理": ("era_words",),
    "style::艺术流派": ("era_words", "photoreal_anchor"),
    "style::时代感": ("era_words",),
    "style::设计语言": (),
    "style::后期效果": ("photoreal_anchor",),
}

# 内容类审计词表（附录 A.4 逐字移植）。通用版默认不激活，作休眠常量保留。
BANNED_STYLE_WORDS: tuple[str, ...] = (
    "anime",
    "cartoon",
    "illustration",
    "manga",
    "comic",
    "cgi",
    "3d render",
    "digital painting",
    "watercolor",
    "sketch",
    "drawing",
    "动漫",
    "卡通",
    "插画",
    "漫画",
    "二次元",
    "数字绘画",
    "水彩",
    "素描",
    "手绘",
    "3d渲染",
)

BANNED_ERA_WORDS: tuple[str, ...] = (
    "palace",
    "emperor",
    "empress",
    "imperial",
    "dynasty",
    "hanfu",
    "ancient robe",
    "throne",
    "concubine",
    "eunuch",
    "forbidden city",
    "dragon robe",
    "phoenix crown",
    "imperial palace",
    "royal court",
    "皇宫",
    "皇帝",
    "皇后",
    "宫廷",
    "朝代",
    "汉服",
    "龙袍",
    "凤冠",
    "紫禁城",
    "太监",
    "贵妃",
    "妃嫔",
    "圣旨",
    "后宫",
    "登基",
)

BANNED_FRAMING_WORDS: tuple[str, ...] = (
    "closeup",
    "close-up",
    "extreme close up",
    "tight crop",
    "cropped face",
    "cropped head",
    "特写",
    "裁脸",
    "裁头",
    "裁掉头顶",
    "裁切头部",
)

BANNED_DARK_WORDS: tuple[str, ...] = (
    "night",
    "nighttime",
    "midnight",
    "moonlight",
    "moonlit",
    "rain",
    "rainy",
    "raining",
    "rainstorm",
    "downpour",
    "drizzle",
    "storm",
    "thunderstorm",
    "dusk",
    "twilight",
    "sunset",
    "dim",
    "dimly",
    "gloomy",
    "darkness",
    "dark night",
    "dark alley",
    "deep shadows",
    "heavy shadows",
    "low-key lighting",
    "candlelight",
    "candlelit",
    "夜晚",
    "夜景",
    "午夜",
    "深夜",
    "黑夜",
    "月光",
    "雨夜",
    "雨天",
    "暴雨",
    "大雨",
    "雷雨",
    "黄昏",
    "暮色",
    "日落",
    "昏暗",
    "阴暗",
    "烛光",
    "暗巷",
    "夜幕降临",
)

KNEEL_WORDS: tuple[str, ...] = (
    "kneel",
    "on his knees",
    "on her knees",
    "on their knees",
    "on knees",
    "kowtow",
    "下跪",
    "跪地",
    "跪求",
    "跪下",
    "跪拜",
    "双膝跪",
)

# 平台内容政策注入段（附录 A.6 全文逐字移植）。
POLICY_BLOCK = (
    "【平台内容政策】按以下完整分类编排，各分类及其词条均为违禁方向：\n"
    "- 垃圾与欺骗：诈骗、钓鱼、误导标题、刷量、虚假互动、冒充、外链诱导\n"
    "- 敏感内容：裸露色情、性暗示、性服务、挑逗性姿势、脱衣动作、暗示性着装描写、"
    "未成年人安全、未成年人处于危险或受害情境、利用儿童形象引流、自杀自残、饮食失调、粗俗用语\n"
    "- 暴力与危险：死亡血腥、血液、伤口特写、尸体、伤害酷刑、酷刑工具、危险挑战、犯罪组织、"
    "恐怖主义、仇恨歧视、骚扰威胁、人肉隐私、动物虐待\n"
    "- 管制商品：毒品、烟草、酒精、枪械、爆炸物、赌博、伪造品、非法服务、人口走私\n"
    "- 虚假信息：有害虚假信息、医疗虚假信息、选举干扰、技术操纵或虚构冒充\n"
    "- 广告友好度扩展：惊悚内容、争议性问题、敏感事件、战争灾难、促成不诚实行为、"
    "不适合儿童和家庭、煽动与贬低\n"
    "【边界限定】\n"
    "1. 禁止的是针对现实受保护特征（种族、宗教、性别、地域、残障等）的仇恨、贬低与煽动；"
    "豪门打压、职场欺压、家庭矛盾等戏剧化个人恩怨属正当戏剧冲突，不在此列。\n"
    "2. 冲突画面只画进行中，无接触、无伤痕、无后果；标题不得含第二人称攻击与现实群体标签。\n"
    "3. 儿童仅可作为亲子温情场景的非焦点元素出现，绝不处于冲突中心、危险或受害情境，"
    "画面主角永远为成年人；校园人物一律按青年/大学生（young adults, college）表述。\n"
    "4. 烟酒类道具一律以普通饮品替代；文生图提示词不得出现上述任何类别对应的中英文词汇，"
    "包括以 no/none/without 等否定形式出现。"
)

# 通用版锚点（计划 §11.6）：按序检测补入；quality 恒定追加、不让步。
FRAMING_ANCHOR = "画面主体完整入画，关键元素不被画面任何边缘裁切"
QUALITY_ANCHOR = "主体突出，轮廓与边缘清晰，缩略图小尺寸下主体仍可一眼辨认"

# 绘字段模板（计划 §11.6，非锚点，恒定追加；{cover_text} 经 normalize_cover_text 处理）。
COVER_TEXT_FIELD_TEMPLATE = (
    "画面中加入标题文字「{cover_text}」：大号醒目字体，粗体描边或与背景高对比，"
    "拼写正确、清晰可读，位于画面显著位置且不遮挡主体；"
    "除该文字外，画面中不得出现其他任何文字、字母、数字、招牌或水印"
)

# 评委 rubric（计划 §11.7，7 维通用版，每维 0-20 分，总分 140）。
COVER_RUBRIC: tuple[tuple[str, str], ...] = (
    ("visual_impact", "构图冲击：主体突出、视觉焦点明确、有吸引力"),
    ("thumbnail_clarity", "缩略图可读性：小尺寸下主体与场景一眼可辨，不糊不乱"),
    ("title_match", "标题关联：画面兑现标题的钩子并与其信息互补"),
    ("text_visual_match", "文字画面呼应：封面文字与画面内容互相强化、形成统一钩子"),
    ("script_fidelity", "文案贴合：画面元素与文案一致，无与文案矛盾的虚构"),
    ("style_consistency", "风格一致：与参考图既定描述/用户补充需求协调统一"),
    ("technical_quality", "技术质量：细节丰富、无畸形糊块、封面文字拼写正确且清晰可读"),
)

# 审计激活矩阵可用的规则键；默认全不激活（空集 → 零问题）。
AUDIT_RULE_KEYS = ("style_words", "era_words", "dark_words", "framing_words", "kneel_limit")

# 视觉解析 system（计划 §11.1 全文）。
_REF_ANALYSIS_SYSTEM = (
    "你是视频封面参考图解析员。只客观描述图片的画面形态（构图、站位、光影、色彩、材质、风格等），"
    "不评价好坏、不虚构内容、不猜测人物真实身份与关系、不得输出内容解读。"
    "输出严格 JSON，无解释、无 Markdown 围栏。"
)

# 封面文字生成 system（计划 §11.3 全文，{POLICY_BLOCK} 已注入）。
_COVER_TEXT_SYSTEM = (
    "你是 YouTube 爆款封面文案专家，专为视频封面（缩略图）撰写大号标题文字。"
    "你的文字要在 1 秒内抓住眼球、制造点击冲动。\n"
    "\n"
    "【铁律】\n"
    "- 极短：优先 1–3 个词，且不超过 30 个字符（以项目语言为准）；小尺寸缩略图上一眼读完。\n"
    "- 好奇缺口：放大标题的悬念或冲突，但不重复标题已用的词，也不剧透标题所问的答案；"
    "封面文字 + 标题合起来让观众非点不可。\n"
    "- 补位：封面文字、画面、标题各管一件事、不重复。不写画面本身能表达的信息"
    "（画面画了原始人，就不写「原始人」），只写画面表达不了的话题或悬念。\n"
    "- 用词与句尾：优先具体名词、数字，少用形容词；疑问式结尾用「？」或「？！」。\n"
    "- 情绪优先：震惊、反差、紧迫、好奇，拒绝平淡陈述。\n"
    "- 每条从下列句式模式中选不同的一种，避免雷同：否定式（NO JOBS / 别再…）、"
    "对比式（$0 → $10K / 上班 vs 躺平）、数字式（3 个错误 / 7 天）、"
    "提问式（就这？/ 为什么没人说）、宣告式（I QUIT / 到此为止）、身份/结局反转式。\n"
    "- 不用 emoji、不堆砌标点、不说教、不出现品牌名或真人姓名。\n"
    "- 内容必须与文案一致：可以强化情绪与悬念，不得虚构文案中没有的事实（数字、结果、身份），"
    "不得误导。\n"
    "- 画面与文字均不得描绘或暗示以下违禁类别内容。\n"
    + POLICY_BLOCK
    + "\n"
    "输出严格 JSON，无解释、无 Markdown 围栏。"
)

# 画面提示词生成 system（计划 §11.4 全文；参考图豁免段按 provided 动态注入）。
_COVER_PROMPT_SYSTEM_HEAD = (
    "你是视频封面（YouTube 缩略图）的视觉提示词专家，为文生图模型撰写高点击率封面提示词。"
    "你要为给定的标题、封面文字与文案写 1 条封面提示词。\n"
    "\n"
    "【输出结构·八维度】封面按下列八个维度撰写，每个子项一句具体、可执行、还原到物的中文描述"
    "（只描述画面本身，不写分析、不评价）：\n"
    "1) subject 主体（人物身份/外貌/动作/表情/服装）：画面的核心主体与状态；"
    "画面无人物时填写主体对象，确实不适用的子项写\"无\"。\n"
    "2) spatial 空间（前景/中景/背景/环境/道具）：交代地点、层次与氛围。\n"
    "3) composition 构图（人物占比/站位/画面比例/留白/视觉中心）。\n"
    "4) camera 镜头（焦距/机位/视角/景深/对焦）。\n"
    "5) lighting 光影（光源/方向/强弱/阴影）。\n"
    "6) color 色彩（主色/辅色/饱和度/色彩情绪）。\n"
    "7) material 材质（衣料/皮肤/金属/环境纹理）。\n"
    "8) style 风格（艺术流派/时代感/设计语言/后期效果）。\n"
    "\n"
    "【封面文字呼应】任务单附有本张封面的封面文字（由系统逐字拼入成品，你不得撰写或改动）。"
    "文字下的什么钩子，画面就画兑现这个钩子的一幕：文字是对比，画面就画对比双方；"
    "文字是否定/宣告，画面就画对应的状态或瞬间；文字是提问/悬念，画面就画悬念揭晓前一刻。"
    "文字与画面必须互相强化、形成统一钩子。\n"
    "【事实铁律】画面的题材、时代、场景、主体与美术风格只能依据"
    "【标题】【封面文字】【文案】【参考图既定描述】与【用户补充需求】确定；"
    "不得套用任何现成题材模板；文案未提供的视觉细节可做中性补全，但不得与文案矛盾；"
    "八个维度之间必须自洽（同一时代、同一场景逻辑、同一美术方向）。\n"
    "【标题关联】封面必须兑现标题的钩子并与标题信息互补：标题传达什么主题、冲突或悬念，"
    "封面就画对应的一幕；标题已明说的信息不必重复图解，封面优先补足标题没说的画面信息。\n"
    "【画面要求】主体突出、视觉焦点明确，缩略图小尺寸下仍能一眼辨认主体；"
    "主角有明确表情，且至少一个旁观者给出夸张反应（替观众先惊讶）；"
    "可放 1–2 个暗示答案的小道具，但不解释、不画结果；"
    "表情与情绪写可辨认的面部/肢体表现（如「瞪大双眼」「眉毛上扬」「双手抱头」），"
    "不写「惊讶」「震撼」等抽象情绪词；"
    "色彩子项写具体颜色与色值（如「亮黄 #FFD700」），避免「氛围感」「高级感」等空泛形容词；"
    "构图需为封面文字预留合适的显著区域（如顶部或主体旁留空），"
    "且右下角（约宽 20%、高 20% 的区域）为平台时长徽章留空，"
    "不放主体、封面文字与关键信息；"
    "但画面文字本身由系统统一追加，你无需在 dims 中撰写；"
    "除指定封面文字外，画面中不得出现其他文字、字母、数字、招牌或水印。\n"
)

_COVER_PROMPT_SYSTEM_TAIL = (
    "画面与 prompt 均不得描绘或暗示以下违禁类别内容。\n"
    + POLICY_BLOCK
    + "\n"
    "\n"
    "【格式】\n"
    "- 描述用中文撰写；只写 dims 结构化字段，不要输出整段成文提示词。\n"
    "- 封面文字绘字段、主体完整声明与质量锚点均由系统统一追加，你无需撰写。\n"
    "- 描述要与标题和文案的情绪、题材一致。\n"
    "\n"
    "输出严格 JSON，无解释、无 Markdown 围栏。"
)

# 评委 system（计划 §11.7 全文，rubric 行由 COVER_RUBRIC 派生）。
_JUDGE_SYSTEM = (
    "你是视频封面（YouTube 缩略图）的严苛评审（评委），只负责打分与点评，不重写内容。"
    "按固定 rubric 逐版打分，分数要拉开差距、直言缺陷。\n"
    "打分前先模拟一个正在刷手机的普通观众只看 0.5 秒：读到了什么字？"
    "脑子里冒出什么问题？想不想点进去？以这三问作为各维打分的锚点。\n"
    "\n"
    "【rubric（每维 0-20 分）】\n"
    + "\n".join(f"- {key}（0-20 分）：{desc}" for key, desc in COVER_RUBRIC)
    + "\n"
    "\n"
    "输出严格 JSON，无解释文字、无 Markdown 围栏。"
)

_LINE_RE = re.compile(r"^\s*([^：\n]{1,6})：([\s\S]*)$")
_PART_RE = re.compile(r"^\s*([^：；]+)：([\s\S]*)$")
_FRAMING_ANCHOR_RE = re.compile(r"完整入画|不被.*裁切")
_QUALITY_ANCHOR_RE = re.compile(r"一眼辨认")
_CJK_RE = re.compile(r"[一-鿿]")


def _dim_by_key(key: str) -> RefDimension | None:
    for dim in REF_DIMENSIONS:
        if dim.key == key:
            return dim
    return None


def _trunc_int(value: object) -> int | None:
    """JS `Math.trunc(Number(x))` 移植：无法转为有限数值时返回 None。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return math.trunc(number)


def normalize_ref_dims(dims: object) -> list[str]:
    """勾选编码规整（附录 B normalizeRefDims 移植）。

    非列表/元组 → []；元素转 str 去空白去重；只接受大类 key 或 `key::已知中文子项`；
    按分类表顺序输出；整类命中（裸 key）忽略该类重复子项；不做"全子项折叠为大类"。
    """
    raw: set[str] = set()
    if isinstance(dims, (list, tuple)):
        raw = {("" if d is None else str(d)).strip() for d in dims}
        raw.discard("")
    out: list[str] = []
    for dim in REF_DIMENSIONS:
        if dim.key in raw:
            out.append(dim.key)
            continue
        for child in dim.children:
            child_id = f"{dim.key}::{child}"
            if child_id in raw:
                out.append(child_id)
    return out


def normalize_ref_selection(dims: object) -> list[dict[str, Any]]:
    """归并勾选为 [{key, label, children}]（children 空列表 = 整类全选）。"""
    flat = normalize_ref_dims(dims)
    out: list[dict[str, Any]] = []
    for dim in REF_DIMENSIONS:
        if dim.key in flat:
            out.append({"key": dim.key, "label": dim.label, "children": []})
            continue
        prefix = f"{dim.key}::"
        picked = [d[len(prefix) :] for d in flat if d.startswith(prefix)]
        if picked:
            out.append({"key": dim.key, "label": dim.label, "children": picked})
    return out


def image_data_url(image_base64: str = "", image_url: str = "") -> str:
    """参考图载荷 → data URL（附录 B imageDataUrl 移植）。

    image_url 非空直接使用；裸 base64 按魔数判 MIME
    （FF D8 FF → jpeg、RIFF → webp、默认 png）；data: 前缀直用；
    缺图片数据抛 DomainError（上游会对非法图片给出明确错误）。
    """
    if image_url:
        return str(image_url)
    raw = (image_base64 or "").strip()
    if not raw:
        raise DomainError("cover_ref_image_missing", "参考图解析缺少图片数据。")
    if raw.startswith("data:"):
        return raw
    mime = "image/png"
    with suppress(ValueError):
        head = base64.b64decode(raw)
        if head[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif head[:4] == b"RIFF":
            mime = "image/webp"
    return f"data:{mime};base64,{raw}"


def build_ref_analysis_messages(
    image: Mapping[str, Any], dims: object
) -> list[dict[str, Any]]:
    """构造参考图解析消息（计划 §11.1/§11.2 全文）。

    image 为 {"image_base64": str, "image_url": str} 映射；部分勾选时只列选中
    维度/子项；user.content = [text, image_url(detail=high)]。勾选为空时 dimText
    为空行，是否允许调用由应用层决定（与参考实现 build 层不做校验一致）。
    """
    selection = normalize_ref_selection(dims)
    dim_lines: list[str] = []
    for sel in selection:
        dim = _dim_by_key(str(sel["key"]))
        children = sel["children"] or (list(dim.children) if dim else [])
        dim_lines.append(f'- "{sel["key"]}"（{sel["label"]}）：{"、".join(children)}')
    instructions = [
        "解析这张视频封面参考图，仅按下列勾选维度输出中文描述"
        "（每个子项一句话，具体、可直接用于文生图提示词；图中无法确认的方面写\"不明显\"）：",
        "\n".join(dim_lines),
    ]
    if any(str(sel["key"]) in ("style", "composition") for sel in selection):
        # 纯风格维度抽象化（2026-10-15 用户确认）：防止解析复述具体场景。
        instructions.append(
            "【纯风格维度抽象化】style、composition 两个纯风格维度只写"
            "跨场景可迁移的抽象短句或关键词"
            "（如「粗黑描边」「平涂色块」「简化圆形角色」「主体占比大」「顶部留白放标题」），"
            "禁止复述图中的具体场景元素（具体物体、地点、道具、人物动作与情节）；"
            "站位/视觉中心只写抽象方位与构图规律（如「主体居左」「标题居中顶部」），不写具体角色与物体。"
        )
    instructions.extend(
        [
            "若画面含有显著标题文字，请在构图/风格的相关子项中描述其样式、颜色、描边与位置"
            "（描述样式即可，不逐字抄录文字内容）。",
            '输出 JSON：{ "<维度key>": { "<子项>": "<描述>" } }，只包含上述维度与子项。',
        ]
    )
    text = "\n".join(instructions)
    url = image_data_url(
        image_base64=str(image.get("image_base64") or ""),
        image_url=str(image.get("image_url") or ""),
    )
    return [
        {"role": "system", "content": _REF_ANALYSIS_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
            ],
        },
    ]


def validate_ref_analysis(raw: object, dims: object) -> dict[str, dict[str, str]]:
    """校验并规整解析结果（附录 B validateRefAnalysis 移植）。

    只留勾选维度内已知子项；值转 str → trim → 截 200 码点；空值与严格等于
    "不明显"丢弃；无字段维度丢弃。返回 {} 表示模型没认出可用内容。
    """
    selection = normalize_ref_selection(dims)
    source: Mapping[Any, Any] = raw if isinstance(raw, Mapping) else {}
    out: dict[str, dict[str, str]] = {}
    for sel in selection:
        dim = _dim_by_key(str(sel["key"]))
        children = sel["children"] or (list(dim.children) if dim else [])
        node = source.get(sel["key"])
        node = node if isinstance(node, Mapping) else {}
        fields: dict[str, str] = {}
        for child in children:
            value = node.get(child)
            text = ("" if value is None else str(value)).strip()[:FIELD_MAX]
            if text and text != "不明显":
                fields[child] = text
        if fields:
            out[str(sel["key"])] = fields
    return out


def render_ref_analysis_text(analysis: object) -> str:
    """解析 JSON → 中文约束文本（附录 B renderRefAnalysisText 移植）。

    分类表顺序、每大类一行、全角冒号/分号；空分析 → 空串；整体 ≤2000 码点。
    注意 Python len/slice 按码点计，JS 原版按 UTF-16 码元计。
    """
    source: Mapping[Any, Any] = analysis if isinstance(analysis, Mapping) else {}
    lines: list[str] = []
    for dim in REF_DIMENSIONS:
        fields = source.get(dim.key)
        if not isinstance(fields, Mapping):
            continue
        parts = [f"{child}：{fields[child]}" for child in dim.children if fields.get(child)]
        if parts:
            lines.append(f"{dim.label}：{'；'.join(parts)}")
    return "\n".join(lines)[:REF_CONSTRAINT_MAX]


def parse_ref_text_by_child(text: object, dims: object = None) -> dict[str, Any]:
    """约束文本拆回子项级文本（附录 B parseRefTextByChild 移植）。

    行协议：全角冒号、标签 ≤6 字符、全角分号切段、已知子项、空值跳过、
    重复子项后写覆盖；非格式行/未知维度行视为用户备注忽略（文本仍可作模型上下文）。
    dims 提供时勾选维度整行缺失或零有效子项 →
    {"ok": False, "reason": "dim_line_invalid", "dim": key}；行内个别勾选子项
    缺失不算损坏（该子项由调用方回退 LLM 撰写）。
    """
    src = ("" if text is None else str(text)).strip()
    if not src:
        return {"ok": False, "reason": "empty"}
    by_child: dict[str, str] = {}
    dim_ok: dict[str, bool] = {}
    for line in re.split(r"\r?\n", src):
        match = _LINE_RE.match(line)
        if not match:
            continue
        label = match.group(1).strip()
        dim = next((d for d in REF_DIMENSIONS if d.label == label), None)
        if dim is None:
            continue
        found = 0
        for part in match.group(2).split("；"):
            part_match = _PART_RE.match(part)
            if not part_match:
                continue
            child = part_match.group(1).strip()
            value = part_match.group(2).strip()
            if child not in dim.children or not value:
                continue
            by_child[f"{dim.key}::{child}"] = value[:FIELD_MAX]
            found += 1
        if found > 0:
            dim_ok[dim.key] = True
        elif dim.key not in dim_ok:
            dim_ok[dim.key] = False
    for sel in normalize_ref_selection(dims):
        if dim_ok.get(str(sel["key"])) is not True:
            return {"ok": False, "reason": "dim_line_invalid", "dim": sel["key"]}
    return {"ok": True, "by_child": by_child}


def provided_child_ids(ref_dims: object, by_child: Mapping[str, Any] | None) -> list[str]:
    """勾选维度 ∩ 有逐字文本的子项（附录 D providedChildIds 移植，整类展开）。

    让步规则与「系统提供」口径都以这个交集为准：勾选但无文本的子项回退 LLM
    撰写、不让步。
    """

    def has(child_id: str) -> bool:
        value = (by_child or {}).get(child_id)
        return bool(("" if value is None else str(value)).strip())

    out: list[str] = []
    for child_id in normalize_ref_dims(ref_dims):
        if "::" in child_id:
            if has(child_id):
                out.append(child_id)
            continue
        dim = _dim_by_key(child_id)
        for child in dim.children if dim else ():
            full_id = f"{child_id}::{child}"
            if has(full_id):
                out.append(full_id)
    return out


def ref_rule_suppressions(ref_dims: object) -> set[str]:
    """勾选子项经 REF_CHILD_RULE_MAP 求规则并集（附录 D refRuleSuppressions 移植）。"""
    out: set[str] = set()
    entries = ref_dims if isinstance(ref_dims, (list, tuple)) else []
    for entry in entries:
        child_id = ("" if entry is None else str(entry)).strip()
        if not child_id:
            continue
        if "::" in child_id:
            out.update(REF_CHILD_RULE_MAP.get(child_id, ()))
            continue
        dim = _dim_by_key(child_id)
        for child in dim.children if dim else ():
            out.update(REF_CHILD_RULE_MAP.get(f"{child_id}::{child}", ()))
    return out


def splice_ref_children(
    dims: object, by_child: Mapping[str, Any] | None, ref_dims: object
) -> dict[str, dict[str, Any]]:
    """参考子项逐字覆盖/补入 AI dims（附录 D spliceRefChildren 移植）。

    只处理勾选范围内的子项；逐字采用，不做任何清洗/审词；输入不被修改。
    """
    out: dict[str, dict[str, Any]] = {}
    source: Mapping[Any, Any] = dims if isinstance(dims, Mapping) else {}
    for key, node in source.items():
        out[str(key)] = dict(node) if isinstance(node, Mapping) else {}
    checked = set(normalize_ref_dims(ref_dims))
    for dim in REF_DIMENSIONS:
        whole = dim.key in checked
        for child in dim.children:
            child_id = f"{dim.key}::{child}"
            if not whole and child_id not in checked:
                continue
            value = (by_child or {}).get(child_id)
            text = ("" if value is None else str(value)).strip()
            if text:
                out.setdefault(dim.key, {})[child] = text
    return out


def _strip_cover_text_chars(text: object) -> str:
    """trim 并去除换行与「」（封面文字单行绘字约束）。"""
    cleaned = ("" if text is None else str(text)).strip()
    return cleaned.replace("\r", "").replace("\n", "").replace("「", "").replace("」", "")


def build_cover_text_messages(
    title: str, script: str, brief: str = "", language: str = ""
) -> list[dict[str, Any]]:
    """封面文字生成任务单（计划 §11.3 全文，串行第一步）。

    brief 去空白截 500 码点后非空才注入其块；POLICY_BLOCK 已注入 system；
    文案全文不截断。
    """
    brief_text = (brief or "").strip()[:BRIEF_MAX]
    blocks = [f"【标题】\n{title}"]
    if brief_text:
        blocks.append(f"【用户补充需求】\n{brief_text}")
    blocks.append(f"【文案全文（事实来源）】\n{script}")
    blocks.append(f"【项目语言】\n{language}")
    blocks.append(
        "【推导步骤】先完成以下推导再输出候选（推导过程不输出）：\n"
        "1. 用一句话写出这个视频回答的核心问题。\n"
        "2. 列出 3 组「观众默认认知 × 视频情境」反差对（格式：【常识A】×【情境B】，"
        "例：来月经要用卫生巾 × 原始人什么都没有）。\n"
        "3. 对每组写出观众看到后脑子里冒出的那句疑问（通常是「等等，那……怎么办/真的吗？」），"
        "再把这句疑问压缩成封面文字。"
    )
    blocks.append(
        "请写 5 条候选封面文字，输出 JSON：\n"
        "{\n"
        '  "candidates": [\n'
        '    { "text": "<封面文字>", "pattern": "<句式模式>" }\n'
        "  ],\n"
        '  "recommended": <int，最推荐的一条序号，从 1 开始>\n'
        "}\n"
        "务必正好 5 条，句式模式尽量互不相同。"
    )
    return [
        {"role": "system", "content": _COVER_TEXT_SYSTEM},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_COVER_TEXT_WORD_BUDGET = 3
_COVER_TEXT_TITLE_OVERLAP_MIN = 2
# 标题重叠 lint 的英文功能词（仅用于重叠计算，数字与实义词保留）。
_COVER_TEXT_STOPWORDS = frozenset(
    (
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "by", "for",
        "with", "without", "from", "as", "is", "are", "was", "were", "be", "been", "being",
        "am", "do", "does", "did", "have", "has", "had", "how", "what", "when", "where",
        "which", "who", "whom", "why", "this", "that", "these", "those", "it", "its",
        "i", "you", "he", "she", "they", "we", "me", "him", "her", "them", "us", "my",
        "your", "his", "their", "our", "not", "no", "yes", "if", "then", "than", "so",
        "such", "can", "could", "will", "would", "should", "may", "might", "must",
    )
)


def _latin_content_words(text: str) -> set[str]:
    return {
        word
        for word in _LATIN_WORD_RE.findall(text.lower())
        if word not in _COVER_TEXT_STOPWORDS
    }


def _cover_text_rule_warnings(text: str, title: str) -> list[str]:
    """拉丁文字候选的确定性 lint（只记警告不阻断）。

    含 CJK 的候选跳过（分词不可靠，长度仍走既有 30 码点规则）；
    标题重叠仅在标题同为拉丁文字时判定，重复实义词 ≥2 个记警告。
    """
    if _has_cjk(text):
        return []
    warnings: list[str] = []
    if len(_LATIN_WORD_RE.findall(text)) > _COVER_TEXT_WORD_BUDGET:
        warnings.append("cover_text_over_word_budget")
    if not _has_cjk(title):
        shared = _latin_content_words(text) & _latin_content_words(title)
        if len(shared) >= _COVER_TEXT_TITLE_OVERLAP_MIN:
            warnings.append("cover_text_title_overlap")
    return warnings


def validate_cover_text_candidates(raw: object, title: str) -> dict[str, Any]:
    """封面文字候选校验（计划 §11.3 校验规则）。

    逐条 text trim、去换行与「」、>30 码点截断（warning cover_text_truncated）、
    空条丢弃、逐字去重；recommended 钳位 1..n，非法取 1；全部为空 →
    回退 [标题截断 30]（warning cover_text_fallback_title，不阻断）。
    返回 {"candidates": [...], "recommended": int, "warnings": [...]}。
    """
    source: Mapping[Any, Any] = raw if isinstance(raw, Mapping) else {}
    items = source.get("candidates")
    warnings: list[str] = []
    candidates: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        text = _strip_cover_text_chars(item.get("text"))
        if not text:
            continue
        if len(text) > COVER_TEXT_MAX:
            text = text[:COVER_TEXT_MAX]
            warnings.append("cover_text_truncated")
        if text in candidates:
            continue
        candidates.append(text)
        warnings.extend(_cover_text_rule_warnings(text, title))
    if not candidates:
        candidates = [_strip_cover_text_chars(title)[:COVER_TEXT_MAX]]
        warnings.append("cover_text_fallback_title")
    recommended = _trunc_int(source.get("recommended"))
    if recommended is None or recommended < 1 or recommended > len(candidates):
        recommended = 1
    return {"candidates": candidates, "recommended": recommended, "warnings": warnings}


def normalize_cover_text(text: object, title: str) -> tuple[str, list[str]]:
    """封面文字规整：trim、去换行与「」；空 → 标题截断 30（warning
    cover_text_fallback_title）；>30 码点 → 截断（warning cover_text_truncated）。
    返回 (text, warnings)。
    """
    cleaned = _strip_cover_text_chars(text)
    if not cleaned:
        return _strip_cover_text_chars(title)[:COVER_TEXT_MAX], ["cover_text_fallback_title"]
    warnings: list[str] = []
    if len(cleaned) > COVER_TEXT_MAX:
        cleaned = cleaned[:COVER_TEXT_MAX]
        warnings.append("cover_text_truncated")
    return cleaned, warnings


def _ordered_provided_ids(provided: Iterable[str], ref_dims: object) -> list[str]:
    """provided 子项确定性排序：先按勾选编码顺序（整类展开），再按分类表顺序
    兜底，未知子项以字典序收尾（参考实现为 Set 插入序，此处固定为确定性顺序）。
    """
    wanted = {("" if p is None else str(p)).strip() for p in provided}
    wanted.discard("")
    ordered: list[str] = []
    for child_id in normalize_ref_dims(ref_dims):
        if "::" in child_id:
            candidates = [child_id]
        else:
            dim = _dim_by_key(child_id)
            candidates = [f"{child_id}::{c}" for c in (dim.children if dim else ())]
        for full_id in candidates:
            if full_id in wanted and full_id not in ordered:
                ordered.append(full_id)
    for dim in REF_DIMENSIONS:
        for child in dim.children:
            full_id = f"{dim.key}::{child}"
            if full_id in wanted and full_id not in ordered:
                ordered.append(full_id)
    ordered.extend(sorted(wanted - set(ordered)))
    return ordered


def _label_of(child_id: str) -> str:
    key, sep, child = child_id.partition("::")
    dim = _dim_by_key(key) if sep else None
    return f"{dim.label}·{child}" if dim else child_id


def build_cover_prompt_messages(
    title: str,
    cover_text: str,
    script: str,
    brief: str = "",
    ref_text: str = "",
    ref_dims: object = (),
    provided: Iterable[str] = (),
    extra_directive: str = "",
    *,
    character_cards: Iterable[CharacterCardBinding] = (),
) -> list[dict[str, Any]]:
    """画面提示词生成任务单（计划 §11.4/§11.5 全文，串行第二步，两版并行复用）。

    ref_text 为空 → 无【参考图豁免】段、无参考块、dims 模板无 "" 占位、无
    "标注为"提示句（provided 仅在 ref_text 非空时生效）；brief 空 → 无该块；
    extra_directive 非空追加【定向整改要求】；输出契约正好 1 条。
    ref_dims 仅用于 provided 子项的确定性排序（勾选编码顺序）。
    character_cards 非空时角色绑定生效（v5 §1.3）：参考 provided 的 subject::
    全部子项不进入拼接，首卡身份/外貌作为系统提供字段进入 subject::人物身份/
    subject::外貌（无参考文本时同样生效），system 注入【角色绑定】段、user 在
    【封面文字】之后注入【出场角色】块；为空时与 v4 逐字一致。
    """
    cards = tuple(character_cards or ())
    ref = (ref_text or "").strip()
    provided_ids = _ordered_provided_ids(provided, ref_dims) if ref else []
    if cards:
        provided_ids = _ordered_provided_ids(
            [
                *(pid for pid in provided_ids if not pid.startswith("subject::")),
                *_CHARACTER_CARD_PROVIDED_IDS,
            ],
            [*normalize_ref_dims(ref_dims), *_CHARACTER_CARD_PROVIDED_IDS],
        )
    exempt = ""
    if provided_ids:
        provided_lines = "\n".join(f"- {_label_of(pid)}（{pid}）" for pid in provided_ids)
        exempt = (
            "【参考图豁免】下列子项的画面描述由系统从用户参考图逐字采用，"
            '你在输出 JSON 中将这些字段一律填空字符串 ""，不撰写、不发挥、不改写：\n'
            f"{provided_lines}\n"
            "这些子项不受本任务单任何铁律约束；你撰写的其余子项仍须遵守全部铁律，"
            "且画面应与这些既定描述协调一致。"
        )
    system = _COVER_PROMPT_SYSTEM_HEAD + (exempt + "\n" if exempt else "")
    binding_system = build_character_binding_system_block(cards)
    if binding_system:
        system += binding_system + "\n"
    system += _COVER_PROMPT_SYSTEM_TAIL

    provided_set = set(provided_ids)
    dims_lines: list[str] = []
    for dim in REF_DIMENSIONS:
        parts = []
        for child in dim.children:
            placeholder = "" if f"{dim.key}::{child}" in provided_set else "<一句话，具体可执行>"
            parts.append(f'"{child}": "{placeholder}"')
        dims_lines.append(f'      "{dim.key}": {{ {", ".join(parts)} }}')
    dims_template = ",\n".join(dims_lines)

    user_blocks = [
        "【参数】\n- 封面比例：16:9\n- 目标文生图模型：gpt-image-2",
        f"【标题】\n{title}",
        "【封面文字（系统将要求图像模型逐字绘制，你不得撰写或改动；画面必须与其呼应）】\n"
        f"{cover_text}",
    ]
    binding_user = build_character_binding_user_block(cards)
    if binding_user:
        user_blocks.append(binding_user)
    if ref:
        user_blocks.append(
            f"【参考图既定描述（系统逐字采用）】\n{ref}\n"
            "（以上子项描述将由系统逐字拼入成品提示词，你不得撰写或改动；"
            "你撰写的其余内容应与其画面协调，画什么仍由标题与文案决定。）"
        )
    brief_text = (brief or "").strip()[:BRIEF_MAX]
    if brief_text:
        user_blocks.append(
            f"【用户补充需求】\n{brief_text}\n"
            "（以上是用户指定的封面画面约束，为首要画面约束；与文案冲突处以文案事实为准。"
            "内容政策不因该约束豁免。）"
        )
    user_blocks.append(f"【文案全文（画面事实来源）】\n{script}")
    request = (
        "请为这个标题写 1 条封面描述，输出 JSON：\n"
        "{\n"
        '  "ratio": "16:9",\n'
        '  "covers": [\n'
        "    {\n"
        '      "rank": 1,\n'
        '      "forTitle": "<回填标题，便于核对>",\n'
        '      "dims": {\n'
        f"{dims_template}\n"
        "      }\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "务必正好输出 1 条封面，含完整 dims 八个维度。"
    )
    if provided_ids:
        request += '标注为 "" 的子项由系统提供，原样留空即可。'
    user_blocks.append(request)
    user = "\n\n".join(user_blocks)
    directive = (extra_directive or "").strip()
    if directive:
        user += f"\n\n【定向整改要求】{directive}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def validate_covers(
    parsed: object, ratio: str = "16:9", provided_children: Iterable[str] = frozenset()
) -> dict[str, Any]:
    """画面生成契约校验（附录 D validateCovers 八维度分支移植，移除 scene/shot，cap=1）。

    ratio 取模型自报合法值，否则请求值，否则 16:9；rank 按保留条重排；
    forTitle 仅 str 才 trim；只留已知维度/子项，provided 子项跳过模型值，
    值截 200 码点；维度有效 = 至少一个 AI 子项或至少一个 provided 子项；
    整条无 AI 且 provided 空 → 丢弃；维度缺失记入 dimIssues → needsRetry。
    返回 {"ratio", "covers", "needsRetry", "dimIssues"}。
    """
    source: Mapping[Any, Any] = parsed if isinstance(parsed, Mapping) else {}
    model_ratio = source.get("ratio")
    if isinstance(model_ratio, str) and model_ratio in RATIOS:
        out_ratio = model_ratio
    elif ratio in RATIOS:
        out_ratio = ratio
    else:
        out_ratio = "16:9"
    raw_covers = source.get("covers")
    provided = {str(p) for p in provided_children}
    cap = 1
    covers: list[dict[str, Any]] = []
    dim_issues: list[dict[str, Any]] = []
    for entry in raw_covers if isinstance(raw_covers, list) else []:
        if not isinstance(entry, Mapping):
            continue
        for_title = entry.get("forTitle")
        cover: dict[str, Any] = {
            "rank": len(covers) + 1,
            "forTitle": for_title.strip() if isinstance(for_title, str) else "",
        }
        raw_dims = entry.get("dims")
        raw_dims = raw_dims if isinstance(raw_dims, Mapping) else {}
        dims: dict[str, dict[str, str]] = {}
        missing: list[str] = []
        for dim in REF_DIMENSIONS:
            node = raw_dims.get(dim.key)
            node = node if isinstance(node, Mapping) else {}
            fields: dict[str, str] = {}
            for child in dim.children:
                if f"{dim.key}::{child}" in provided:
                    continue
                value = node.get(child)
                text = ("" if value is None else str(value)).strip()[:FIELD_MAX]
                if text:
                    fields[child] = text
            if fields:
                dims[dim.key] = fields
            elif not any(f"{dim.key}::{child}" in provided for child in dim.children):
                missing.append(dim.key)
        if not dims and not provided:
            continue
        cover["dims"] = dims
        if missing:
            dim_issues.append({"rank": cover["rank"], "kind": "dims_missing", "dims": missing})
        covers.append(cover)
        if len(covers) >= cap:
            break
    needs_retry = len(covers) < cap
    if dim_issues:
        needs_retry = True
    return {
        "ratio": out_ratio,
        "covers": covers,
        "needsRetry": needs_retry,
        "dimIssues": dim_issues,
    }


def assemble_prompt_body(dims: object, skip: Iterable[str] = frozenset()) -> str:
    """装配 prompt_body：维度文本 + 锚点（计划 §11.6，不截断）。

    render_ref_analysis_text(dims) 后按序检测补入 2 个激活锚点：framing_anchor
    （检测 /完整入画|不被.*裁切/，skip 集含 "framing_anchor" 时跳过）与通用质量
    锚点（恒定追加，已含 /高清晰度/ 则跳过）；检测扫全文含已追加 tail；
    锚点句全角逗号相连另起一行。
    """
    text = render_ref_analysis_text(dims)
    skip_set = set(skip)
    tail: list[str] = []
    if (
        "framing_anchor" not in skip_set
        and not _FRAMING_ANCHOR_RE.search(text)
        and not any(_FRAMING_ANCHOR_RE.search(t) for t in tail)
    ):
        tail.append(FRAMING_ANCHOR)
    if not _QUALITY_ANCHOR_RE.search(text) and not any(_QUALITY_ANCHOR_RE.search(t) for t in tail):
        tail.append(QUALITY_ANCHOR)
    if tail:
        text = f"{text}\n{'，'.join(tail)}" if text else "，".join(tail)
    return text


def assemble_final_prompt(prompt_body: str, cover_text: str) -> str:
    """最终提示词 = prompt_body + 换行 + 绘字段（cover_text 逐字嵌入「」）。

    调用方保证 cover_text 已经 normalize_cover_text 处理；本函数不再清洗。
    """
    field = COVER_TEXT_FIELD_TEMPLATE.replace("{cover_text}", cover_text)
    return f"{prompt_body}\n{field}" if prompt_body else field


def _has_cjk(text: str) -> bool:
    """是否含 CJK 字符：中文词没有词边界，\\b 整词匹配会失灵，需改走子串匹配。"""
    return _CJK_RE.search(text) is not None


def find_dark_words(text: object) -> list[str]:
    """夜暗词匹配（附录 F findDarkWords 移植）：英文 \\b 整词匹配、中文子串匹配。"""
    source = "" if text is None else str(text)
    hits: list[str] = []
    for word in BANNED_DARK_WORDS:
        if _has_cjk(word):
            if word in source:
                hits.append(word)
        elif re.search(r"\b" + re.escape(word) + r"\b", source, re.IGNORECASE):
            hits.append(word)
    return hits


def audit_cover_prompts(
    covers: object, active_rules: Iterable[str] = frozenset()
) -> dict[str, Any]:
    """内容类审计（附录 F auditCoverPrompts 通用化移植）：激活矩阵默认全不激活。

    仅检查 active_rules 显式启用的规则键（AUDIT_RULE_KEYS）：
    style_words → banned_word、era_words → era_word、framing_words → framing_word
    （均为小写子串匹配）；dark_words → dark_word（英文 \\b 整词、中文子串）；
    kneel_limit → 下跪画面计数 ≥2 记 kneel_excess。默认空集 → 零问题。
    返回 {"issues": [...], "ok": bool}。
    """
    active = set(active_rules)
    issues: list[dict[str, Any]] = []
    prompts: list[tuple[int, str]] = []
    for cover in covers if isinstance(covers, list) else []:
        if not isinstance(cover, Mapping):
            continue
        prompt = cover.get("prompt")
        if not isinstance(prompt, str):
            continue
        prompts.append((_trunc_int(cover.get("rank")) or 0, prompt))
    for rank, prompt in prompts:
        lower = prompt.lower()
        if "style_words" in active:
            for word in BANNED_STYLE_WORDS:
                if word in lower:
                    issues.append({"rank": rank, "kind": "banned_word", "word": word})
        if "framing_words" in active:
            for word in BANNED_FRAMING_WORDS:
                if word in lower:
                    issues.append({"rank": rank, "kind": "framing_word", "word": word})
        if "era_words" in active:
            for word in BANNED_ERA_WORDS:
                if word in lower:
                    issues.append({"rank": rank, "kind": "era_word", "word": word})
        if "dark_words" in active:
            for word in find_dark_words(prompt):
                issues.append({"rank": rank, "kind": "dark_word", "word": word})
    if "kneel_limit" in active:
        kneel_count = sum(1 for _, p in prompts if any(w in p.lower() for w in KNEEL_WORDS))
        if kneel_count >= 2:
            issues.append({"rank": 0, "kind": "kneel_excess", "count": kneel_count})
    return {"issues": issues, "ok": not issues}


def clean_banned_words(
    text: object,
    *,
    era_words: Iterable[str] = (),
    dark_words: Iterable[str] = (),
    framing_words: Iterable[str] = (),
) -> str:
    """违禁词清理（附录 F cleanBannedWords 移植，休眠工具，供整改兜底启用）。

    恒删风格违禁词；era/framing/dark 词表由调用方传入才删。风格/时代/特写
    子串删（忽略大小写）；夜暗英文 \\b 整词删、中文子串删；清理残留标点。
    """
    out = "" if text is None else str(text)
    for word in BANNED_STYLE_WORDS:
        out = re.sub(re.escape(word), "", out, flags=re.IGNORECASE)
    for word in era_words:
        out = re.sub(re.escape(word), "", out, flags=re.IGNORECASE)
    for word in framing_words:
        out = re.sub(re.escape(word), "", out, flags=re.IGNORECASE)
    for word in dark_words:
        if _has_cjk(word):
            out = out.replace(word, "")
        else:
            out = re.sub(r"\b" + re.escape(word) + r"\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s*,\s*,+", ",", out)
    out = re.sub(r"，\s*，+", "，", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"^\s*[，,]\s*|\s*[，,]\s*$", "", out)
    return out.strip()


def build_judge_messages(
    versions: Iterable[Mapping[str, Any]],
    title: str,
    *,
    character_cards: Iterable[CharacterCardBinding] = (),
) -> list[dict[str, Any]]:
    """评委任务单（计划 §11.7 全文）。

    versions 为 [{version, covers: [{rank, forTitle, prompt}]}]；载荷按
    JSON.stringify 等价形式（紧凑分隔符、不转义非 ASCII）注入。
    character_cards 非空时 user 附同样【出场角色】块（v5 §1.3，rubric 不变）。
    """
    cards = tuple(character_cards or ())
    payload: list[dict[str, Any]] = []
    for index, version in enumerate(versions):
        raw_covers = version.get("covers")
        covers: list[dict[str, Any]] = []
        for cover_index, cover in enumerate(raw_covers if isinstance(raw_covers, list) else []):
            node: Mapping[Any, Any] = cover if isinstance(cover, Mapping) else {}
            covers.append(
                {
                    "rank": _trunc_int(node.get("rank")) or cover_index + 1,
                    "forTitle": str(node.get("forTitle") or ""),
                    "prompt": str(node.get("prompt") or ""),
                }
            )
        version_no = _trunc_int(version.get("version"))
        payload.append(
            {
                "version": version_no if version_no and version_no > 0 else index + 1,
                "covers": covers,
            }
        )
    per_version = len(payload[0]["covers"]) if payload else 1
    scores_template = ", ".join(f'"{key}": <int 0-20>' for key, _desc in COVER_RUBRIC)
    blocks = [
        f"【任务】评审 {len(payload)} 版视频封面文生图提示词（每版 {per_version} 条，"
        f"须兑现标题「{title}」的钩子且与其信息互补）。逐版打分并选出最优版。",
        "【各版内容】\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ]
    binding_user = build_character_binding_user_block(cards)
    if binding_user:
        blocks.append(binding_user)
    blocks.append(
        "输出 JSON：\n"
        "{\n"
        '  "verdicts": [\n'
        '    { "version": 1, "scores": { '
        + scores_template
        + ' }, "comment": "<一句话点评，中文>" }\n'
        "  ],\n"
        '  "best": <int，最优版 version 号>,\n'
        '  "advice": "<对最优版仍存在的问题给出整改意见，中文；无问题则空串>"\n'
        "}"
    )
    return [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]


def validate_verdict(raw: object, n_versions: int) -> dict[str, Any]:
    """评委结果规整（附录 E validateVerdict 移植，COVER_RUBRIC 7 维）。

    version 合法（1..n_versions）且去重（重复取首次）；分数逐维取整钳 0-20，
    本地求和总分；best 非法取最高总分版（并列取先），无有效 verdict 取 1；
    advice 非字符串取空串。返回 {"best": 0 基序号, "verdicts": [...], "advice": str}。
    """
    source: Mapping[Any, Any] = raw if isinstance(raw, Mapping) else {}
    raw_verdicts = source.get("verdicts")
    verdicts: list[dict[str, Any]] = []
    for item in raw_verdicts if isinstance(raw_verdicts, list) else []:
        if not isinstance(item, Mapping):
            continue
        version = _trunc_int(item.get("version"))
        if version is None or version < 1 or version > n_versions:
            continue
        if any(v["version"] == version for v in verdicts):
            continue
        raw_scores = item.get("scores")
        raw_scores = raw_scores if isinstance(raw_scores, Mapping) else {}
        scores: dict[str, int] = {}
        total = 0
        for key, _desc in COVER_RUBRIC:
            score = _trunc_int(raw_scores.get(key))
            score = 0 if score is None else min(20, max(0, score))
            scores[key] = score
            total += score
        comment = item.get("comment")
        verdicts.append(
            {
                "version": version,
                "scores": scores,
                "total": total,
                "comment": comment if isinstance(comment, str) else "",
            }
        )
    best = _trunc_int(source.get("best"))
    if best is None or best < 1 or best > n_versions:
        best = max(verdicts, key=lambda v: v["total"])["version"] if verdicts else 1
    advice = source.get("advice")
    return {
        "best": best - 1,
        "verdicts": verdicts,
        "advice": advice if isinstance(advice, str) else "",
    }


# ---------------------------------------------------------------------------
# v5 角色绑定（COVER_GENERATION_PLAN_V5_20260921 §1）
# ---------------------------------------------------------------------------

CHARACTER_CARD_IDENTITY_MAX = 100
CHARACTER_CARD_APPEARANCE_MAX = 500

# 解析结果 identity 为空（缺失/非 str/trim 后空）的稳定失败码（回退手写）。
COVER_CHARACTER_CARD_EMPTY = "cover_character_card_empty"

# 绑定生效时由系统逐字提供的两个子项（首角色卡身份/外貌）。
_CHARACTER_CARD_PROVIDED_IDS = ("subject::人物身份", "subject::外貌")

# 角色卡解析 system（计划 v5 §1.2 全文）。
_CHARACTER_CARD_ANALYSIS_SYSTEM = (
    "你是 IP 角色的核心特征提取员。只提取该角色跨场景不变的识别特征"
    "（身份、头身比例、面部特征、标志性元素），不描述服装、姿势、道具、场景等情境属性，"
    "不提取画风/渲染风格（渲染风格由频道风格档案统一决定），"
    "不评价、不虚构。输出严格 JSON，无解释、无 Markdown 围栏。"
)

# 角色卡解析 user 文本（计划 v5 §1.2，2026-10-17 升 a 去画风；附图，detail=high）。
_CHARACTER_CARD_ANALYSIS_USER = (
    "提取这张 IP 角色图片的核心识别特征，输出 JSON：\n"
    '{ "identity": "<一句话：角色是谁/是什么，≤50 字>", '
    '"appearance": "<一段：头身比例、面部特征、标志性元素等跨场景不变的外形特征，'
    '不写画风/渲染风格，不写服装/姿势/道具/场景，≤300 字>" }'
)

# system 追加段（计划 v5 §1.3 全文，绑定生效时注入）。
_CHARACTER_BINDING_SYSTEM_BLOCK = (
    "【角色绑定】画面中的人物角色一律为本频道 IP 角色，其人物身份与外貌子项由系统逐字提供"
    '（你留空 ""）；参考图既定描述中出现的任何人物外形特征一律忽略，'
    "仅可参考其动作、表情、站位与构图；角色的服装、姿势与道具由你按标题/文案/封面文字的情境"
    "重新设计，与画面协调；角色的头身比例、面部特征与标志性元素必须保持不变；"
    "角色的渲染风格服从风格维度的既定描述，角色卡外貌文本中的画风表述不单独决定画面美术风格。"
)

# user 追加块首行（计划 v5 §1.3 全文）。
_CHARACTER_BINDING_USER_HEAD = (
    "【出场角色（身份与外貌由系统逐字采用；服装/姿势/动作/表情由你按情境设计）】"
)


@dataclass(frozen=True, slots=True)
class CharacterCardBinding:
    """角色绑定卡（拼接用）：首卡进确定性子项，第 2..N 个进【出场角色】配角行。"""

    identity_text: str
    appearance_text: str


def build_character_card_messages(image: Mapping[str, Any]) -> list[dict[str, Any]]:
    """角色卡解析消息（计划 v5 §1.2 全文，复用视觉解析通道）。

    image 为 {"image_base64": str, "image_url": str} 映射；user.content =
    [text, image_url(detail=high)]，与参考图解析消息同构。
    """
    url = image_data_url(
        image_base64=str(image.get("image_base64") or ""),
        image_url=str(image.get("image_url") or ""),
    )
    return [
        {"role": "system", "content": _CHARACTER_CARD_ANALYSIS_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _CHARACTER_CARD_ANALYSIS_USER},
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
            ],
        },
    ]


def validate_character_card(raw: object) -> dict[str, Any]:
    """角色卡解析结果校验（计划 v5 §1.2）。

    identity/appearance 均 str→trim；identity ≤100、appearance ≤500 码点，
    超长截断记警告；identity 空（缺失/非 str/trim 后空）→ DomainError
    cover_character_card_empty（回退手写）；appearance 缺失/非 str 按空串。
    返回 {"identity": str, "appearance": str, "warnings": [...]}。
    """
    source: Mapping[Any, Any] = raw if isinstance(raw, Mapping) else {}
    raw_identity = source.get("identity")
    identity = raw_identity.strip() if isinstance(raw_identity, str) else ""
    if not identity:
        raise DomainError(
            COVER_CHARACTER_CARD_EMPTY, "角色卡解析结果缺少人物身份，请改为手动填写。"
        )
    raw_appearance = source.get("appearance")
    appearance = raw_appearance.strip() if isinstance(raw_appearance, str) else ""
    warnings: list[str] = []
    if len(identity) > CHARACTER_CARD_IDENTITY_MAX:
        identity = identity[:CHARACTER_CARD_IDENTITY_MAX]
        warnings.append("cover_character_card_identity_truncated")
    if len(appearance) > CHARACTER_CARD_APPEARANCE_MAX:
        appearance = appearance[:CHARACTER_CARD_APPEARANCE_MAX]
        warnings.append("cover_character_card_appearance_truncated")
    return {"identity": identity, "appearance": appearance, "warnings": warnings}


def build_character_binding_system_block(cards: Iterable[CharacterCardBinding]) -> str:
    """绑定生效时的 system 追加段（计划 v5 §1.3 全文）；无绑定卡 → 空串（不注入）。"""
    return _CHARACTER_BINDING_SYSTEM_BLOCK if tuple(cards or ()) else ""


def build_character_binding_user_block(cards: Iterable[CharacterCardBinding]) -> str:
    """绑定生效时的【出场角色】块（计划 v5 §1.3 全文，含第 2..N 个配角行）。

    首卡为「主角」行，其余各一行「配角」；无绑定卡 → 空串（不注入）。
    """
    bound = tuple(cards or ())
    if not bound:
        return ""
    lines = [_CHARACTER_BINDING_USER_HEAD]
    lines.append(f"主角：{bound[0].identity_text}；{bound[0].appearance_text}")
    for card in bound[1:]:
        lines.append(f"配角：{card.identity_text}；{card.appearance_text}")
    return "\n".join(lines)


def bind_character_cards(
    ref_dims: object,
    by_child: Mapping[str, Any] | None,
    cards: Iterable[CharacterCardBinding],
) -> tuple[list[str], dict[str, Any]]:
    """角色绑定生效时的拼接输入（计划 v5 §1.3 屏蔽规则与优先级）。

    参考 provided 的 subject:: 全部 5 子项不进入确定性拼接（仅不进拼接；参考
    文本仍注入 user 消息作上下文），非人物子项不变；首角色卡 identity/appearance
    作为系统提供字段进入 subject::人物身份/subject::外貌。cards 为空时原样
    返回（与 v4 完全一致）。返回 (有效勾选编码, 有效子项文本)。
    """
    bound = tuple(cards or ())
    if not bound:
        return normalize_ref_dims(ref_dims), dict(by_child or {})
    masked_dims = [
        child_id
        for child_id in normalize_ref_dims(ref_dims)
        if child_id != "subject" and not child_id.startswith("subject::")
    ]
    masked_dims.extend(_CHARACTER_CARD_PROVIDED_IDS)
    masked_by_child = {
        str(key): value
        for key, value in (by_child or {}).items()
        if not str(key).startswith("subject::")
    }
    first = bound[0]
    masked_by_child["subject::人物身份"] = first.identity_text
    masked_by_child["subject::外貌"] = first.appearance_text
    return masked_dims, masked_by_child


# ---------------------------------------------------------------------------
# 出图后视觉复审（2026-10-15 用户确认）：消息构造与结果校验，纯函数零 I/O。
# 复审只呈现不阻断、不自动重提；失败由应用层就地落 failed，不走本模块异常。
# ---------------------------------------------------------------------------

COVER_IMAGE_REVIEW_THUMBNAIL_WIDTH = 168
COVER_IMAGE_REVIEW_THUMBNAIL_HEIGHT = 94

_COVER_IMAGE_REVIEW_SYSTEM = (
    "你是视频封面（YouTube 缩略图）的视觉复审员，评审已经生成的封面图本身。"
    "只客观报告画面中可见的问题，不重写提示词、不推测画面外信息、不做审美拔高。\n"
    "【问题分级】blocker 仅用于事实/交付错误：封面文字拼写错误/缺漏字/与给定文字不一致、"
    "封面文字不可读、出现给定封面文字以外的其他文字/字母/数字/招牌/水印、"
    "画面与风格既定描述明显矛盾、168×94 小图下主体或封面文字无法辨认、"
    "主体/封面文字/关键信息落入右下角平台时长徽章区域"
    "（约画面宽 20%、高 20% 的右下角矩形）。"
    "配色、构图、美观等偏好与「还可以更好看」一类意见一律为 advice，不得升为 blocker。\n"
    "输出严格 JSON，无解释、无 Markdown 围栏。"
)


def build_cover_image_review_messages(
    *,
    title: str,
    cover_text: str,
    style_text: str,
    image_base64: str,
    thumbnail_base64: str,
) -> list[dict[str, Any]]:
    """出图后视觉复审消息：user.content = [text, 原图, 168×94 缩略图]。

    style_text 为空时无风格块、复审跳过风格项；图片载荷复用 image_data_url。
    """
    blocks: list[str] = []
    if (title or "").strip():
        blocks.append(f"【标题】\n{title.strip()}")
    if (cover_text or "").strip():
        blocks.append(f"【封面文字（画面中应逐字只有这段文字）】\n{cover_text.strip()}")
    if (style_text or "").strip():
        blocks.append(f"【风格既定描述】\n{style_text.strip()}")
    blocks.append(
        "附图第一张为封面原图，第二张为其 168×94 缩略图（供小尺寸可读性判定）。"
        "请按检查单复审：\n"
        "1) 封面文字是否与给定文字逐字一致、拼写正确、清晰可读，"
        "画面是否还存在其他文字或水印；\n"
        "2) 画面与风格既定描述是否协调（无风格描述时跳过本项）；\n"
        "3) 168×94 小图下主体与封面文字是否仍可辨认；\n"
        "4) 主体、封面文字或关键信息是否落入右下角平台时长徽章区域；\n"
        "5) 其他事实/交付错误。\n"
        '输出 JSON：{"issues":[{"kind":"text_spelling|text_unreadable|extra_text|'
        'style_mismatch|small_size_unreadable|duration_badge_overlap|other",'
        '"severity":"blocker|advice","description":"<中文，客观描述可见问题>",'
        '"suggested_edit":"<中文，定向修复建议；无则空串>"}]}。\n'
        "无问题时 issues 输出空数组；每条问题必须给出 severity，审美建议一律 advice。"
    )
    return [
        {"role": "system", "content": _COVER_IMAGE_REVIEW_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "\n\n".join(blocks)},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_data_url(image_base64=image_base64),
                        "detail": "high",
                    },
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_data_url(image_base64=thumbnail_base64),
                        "detail": "high",
                    },
                },
            ],
        },
    ]


def validate_cover_image_review(raw: object) -> dict[str, Any]:
    """复审结果规整：issues 逐条 trim、空描述丢弃、severity 仅 blocker/advice
    （其余一律 advice）、description/suggested_edit 截 500 码点、kind 截 64 码点、
    最多保留 20 条；ok = 无 blocker（本地判定，不采用模型自报）。
    返回 {"issues": [...], "ok": bool}。
    """
    source: Mapping[Any, Any] = raw if isinstance(raw, Mapping) else {}
    items = source.get("issues")
    issues: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        description = str(item.get("description") or "").strip()[:500]
        if not description:
            continue
        kind = str(item.get("kind") or "").strip()[:64] or "general"
        severity = (
            "blocker" if str(item.get("severity") or "").strip().lower() == "blocker" else "advice"
        )
        issues.append(
            {
                "kind": kind,
                "severity": severity,
                "description": description,
                "suggested_edit": str(item.get("suggested_edit") or "").strip()[:500],
            }
        )
        if len(issues) >= 20:
            break
    return {"issues": issues, "ok": not any(i["severity"] == "blocker" for i in issues)}
