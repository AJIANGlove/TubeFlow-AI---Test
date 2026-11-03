"""H3 视频提示词编译器的版本化系统模板与用户载荷（版本化绑定层）。

模板以 KineStack `minimax-h3-structured-v6` 为底本移植，并按 TubeFlow 边界适配：
删除台词/旁白/<d> 规则体系（成品配音后期叠加，视频强制禁言），保留六段结构，
新增故事板=分镜计划的反网格约束。增强版再追加镜头↔时间轴硬锚定、负规则清单
与格式锚定示例。
"""
# ruff: noqa: E501  模板与示例属于提示词数据，长行不按代码行宽约束

from __future__ import annotations

from tubeflow_test.domain.storyboards import (
    grid_layout_for,
    panel_row_column,
    shot_cut_point_text,
    shot_time_text,
)
from tubeflow_test.video_prompt.domain import (
    SinglePanelPromptCompilerInput,
    VideoPromptCompilerInput,
    VideoPromptStrategy,
    VideoPromptViolation,
)

# 成功案例的格式锚定示例（已按增强版规则 12 改造为 SHOT 节拍；内容元素禁止复用）。
FEW_SHOT_FORMAT_EXAMPLE = """subject_definitions:
(S1) is the large muscular prehistoric man from Picture 1, with thick messy dark hair, full dark beard, strong eyebrows, tan skin, wearing a one-shoulder spotted animal-skin garment tied with a rope belt, barefoot.
summary: A 15-second cinematic prehistoric survival sequence in which a large muscular prehistoric man runs through a violent thunderstorm, seeks shelter in a cave, and then senses a mysterious presence in the darkness.
retention_analysis: The sequence maintains high retention through continuous motion, dramatic weather, sudden environmental shifts, and a suspenseful ending with mysterious eyes in darkness.
detailed_description: [0.0s–15.0s] SHOT 1 · 0.0s–3.0s: A violent thunderstorm over a wild prehistoric landscape. (S1) runs barefoot through deep mud, rain hitting his body, mud splashing, his clothes and hair soaked. Low-angle tracking shot follows him. SHOT 2 · 3.0s–6.0s: He sees a large dark cave entrance in a rocky cliff ahead. Lightning flashes behind him as he runs toward it, the camera rapidly following. SHOT 3 · 6.0s–9.0s: He rushes inside and stops, completely soaked and breathing heavily. Water drips from his hair, beard, and clothing. The storm is visible outside. SHOT 4 · 9.0s–12.0s: The environment becomes quiet; he hears a faint movement deep inside. His expression changes from relief to alertness; he slowly turns toward the darkness. SHOT 5 · 12.0s–15.0s: The camera moves over his shoulder into the deep darkness. For a brief moment, two subtle animal-like eyes appear far inside, only a vague silhouette visible. The creature does not attack, simply watches. End with the mysterious eyes in darkness.
overall_soundscape: H3 native sounds include heavy rain, thunder rumbles, splashing mud, heavy breathing, water dripping, and a faint rustling movement inside the cave. No extra voices or dialogue.
non_diegetic_music: N/A"""

_BASE_RULES = """\
你是 TubeFlow 的 MiniMax H3 结构化视频提示词编译器。必须只输出 JSON 对象，不要输出 Markdown。
目标：将输入的分镜方案编译为符合 MiniMax H3 Base Prompt 规范的结构化英文提示词，驱动参考图生成连续视频。

必须遵守：
1. 视觉、镜头、动作与音景描述全部使用清晰英文。
2. 输出 JSON 只允许一个键 "prompt"，值为完整提示词纯文本。
3. prompt 由以下六个分段组成，段名独占一行、全小写、英文冒号，严格按此顺序各出现一次：subject_definitions、summary、retention_analysis、detailed_description、overall_soundscape、non_diegetic_music。
4. 不要分析图片内容；以稳定名称 Picture 1、Picture 2……指代输入 reference_roles 中的同序参考图，并在 subject_definitions 中为每个出场主体写一句定义、说明其与对应图片的关系（如 (S1) is the ... from Picture 1）；每张参考图（含故事板）都至少在正文中被提及一次。
5. detailed_description 冒号后必须立即写 [0.0s–{duration}.0s]，并覆盖完整时长；动作提前结束则只延续自然画面与环境声。
6. 主体使用稳定编号 (S1)(S2)……；禁止人物对白、台词与口型说话，禁止任何人声，不得使用 <d> 标签。
7. overall_soundscape 只写与场景一致的 H3 原生环境声与动作音效，不得含人声。
8. non_diegetic_music 必须写 N/A。
9. summary 用一句话概括全片；retention_analysis 用一句话说明留存设计。
10. 不得新增、删除、合并或重命名分段；不得输出 visual_asset_bindings 行（由系统确定性拼装）。"""

_ENHANCED_RULES = """\
11. 最后一张参考图（role=STORYBOARD）是多格故事板：其格子按 SHOT 1..N 编号并印有秒数区间，与 shot_plan 的镜头一一对应。它仅作分镜计划参考；必须生成连续的全帧电影画面，绝对禁止分格画面、漫画格子边框与分屏构图，禁止把故事板上的任何文字渲染进视频。
12. detailed_description 按 shot_plan 逐镜叙述：每镜以 "SHOT {order} · {time_range}" 开头（time_range 与输入逐字一致），镜头顺序与 shot_plan 一致；严格使用输入给定的每镜区间，不得自行重排、合并或改写秒数。
13. 负规则：no grid layout, no comic panels, no split screen, no panel borders, no on-screen captions or text carried over from the storyboard.
14. format_example 仅作分段结构与详略示范，禁止复用其中的角色、场景、情节等内容元素。"""


# 真实成功案例（docs/提示词参考.txt）改造：音乐段按项目边界替换为 N/A，
# 引用写法归一化为本项目输出约定（Picture N / (SN)）；仅作格式锚定示例。
FEW_SHOT_STORYBOARD_V2 = """subject_definitions:
- (S1): The same photorealistic adult East Asian woman shown throughout Picture 1, preserving her facial identity, slim proportions, fair natural skin, long silver hair with vivid blue sections, blue-framed glasses, blue necklace, and confident performance expression.
- (S2): Her complete blue rooftop outfit: fitted blue crop top, oversized knitted blue cardigan worn loosely off the shoulders, pleated light-blue mini skirt with ribbon details, and white platform high-heeled sandals.
- (S3): The same open urban rooftop at night, with dark concrete flooring, low perimeter walls, evenly spaced warm safety lights, a dense illuminated modern skyline, deep navy sky, and one bright full moon.
- Picture 2: The complete six-panel photorealistic storyboard, used as the shot-order, composition, dance-action, character, costume, rooftop, lighting, and cinematic-style reference. Treat each panel as a separate chronological shot beat, not as one composite image.
summary:
Reference generation + keyframe completion. Create a 12-second photorealistic cinematic rooftop dance video following the six panels of Picture 2 from left to right and top to bottom. The sequence progresses from a distant rooftop introduction to a confident walk, expressive low-angle gesture, overhead dance step, dynamic spin, and final moonlit pose.
retention_analysis:
- (S1) — fully_preserved: retain the same face, silver-and-blue hair pattern, glasses, body proportions, and confident personality in every shot.
- (S2) — fully_preserved: retain the exact blue garments, cardigan knit texture, skirt construction, necklace, and white platform heels.
- (S3) — fully_preserved: retain the rooftop geometry, wet-looking concrete reflections, skyline arrangement, night lighting, and single moon.
- Picture 2 — fully_preserved: retain its six-stage shot order, changes in camera height, dance progression, realistic photographic style, and cool blue nighttime palette.
detailed_description:
Photorealistic live-action nighttime cinematography with crisp natural skin texture, realistic silver-and-blue hair strands, detailed knitted fabric, controlled highlights, shallow depth of field, cool city illumination, and warm rooftop edge lights. Keep grounded human movement and physically believable balance in platform heels.
[Shot 1] Picture 2 establishes (S1) wearing (S2) at the center of (S3). In a very wide symmetrical rooftop shot, the camera performs a smooth, moderately fast forward dolly of approximately four meters. She begins in the distance beneath the moon, shifts her weight, and takes two poised steps toward the camera while the skyline remains stable.
[Shot 2] At 00:02.000, cut to a full-body medium-wide shot. The camera tracks backward at walking speed as she crosses one leg elegantly in front of the other, rolls one shoulder, and lets the cardigan slide naturally along her upper arms. Her heels contact the concrete correctly, while her pleated skirt and long hair respond to the rooftop breeze.
[Shot 3] At 00:03.800, cut to a dramatic low-angle medium close-up with a slight upward tilt. She plants her feet apart, leans toward the lens, and extends her right hand forward in one fluid expressive gesture. Her fingers open naturally near the camera without distortion while her other hand lowers beside her skirt. Her gaze remains locked on the lens.
[Shot 4] At 00:05.600, cut to a high overhead view. The camera performs a slow clockwise orbit of approximately twenty degrees. She steps diagonally with her right foot, pivots on the ball of her left foot, and opens both arms for balance. Her shadow, hair, cardigan sleeves, and skirt rotate coherently with her body.
[Shot 5] At 00:07.500, cut to a dynamic three-quarter full-body shot with a restrained ten-degree Dutch angle. She completes one controlled clockwise spin. Her silver-and-blue hair sweeps outward in one continuous arc, the cardigan and pleated skirt flare with realistic inertia, and both heels remain stable when she lands.
[Shot 6] At 00:09.700, cut to a moonlit medium portrait. She settles into the final pose with one hand behind her head and the other resting lightly near her hip. The camera slowly pushes in by 6%. Her hair and cardigan gradually settle, she raises her chin, gives one natural blink, and finishes with a confident, relaxed look toward the camera. The full moon remains visible beside her head.
Maintain one facial identity, exact hair-color placement, glasses shape, necklace, garment design, correct fingers, stable legs, and consistent heel construction. No storyboard grid, panel borders, numbers, split screen, duplicated moon, additional people, changing skyline, warped hands, broken heels, foot sliding, excessive spinning, sudden costume changes, subtitles, text, logos, or watermarks.
overall_soundscape:
Steady rooftop wind, distant city traffic, faint urban hum, precise platform-heel footsteps on concrete, and natural cardigan, skirt, and hair movement. No dialogue.
non_diegetic_music:
N/A"""

# v3 示例：由 v2 真实成功案例改造——主体定义去除全部 Picture 来源指代（只写外观），
# 故事板留存条目改为 shot_plan_reference_only，[Shot n] 块带逐字格位（6 镜 16:9 为 2×3：
# 镜头 1..6 → (1,1),(1,2),(1,3),(2,1),(2,2),(2,3)）。仅作格式锚定示例。
FEW_SHOT_STORYBOARD_V3 = """subject_definitions:
- (S1): The same photorealistic adult East Asian woman, preserving her facial identity, slim proportions, fair natural skin, long silver hair with vivid blue sections, blue-framed glasses, blue necklace, and confident performance expression.
- (S2): Her complete blue rooftop outfit: fitted blue crop top, oversized knitted blue cardigan worn loosely off the shoulders, pleated light-blue mini skirt with ribbon details, and white platform high-heeled sandals.
- (S3): The same open urban rooftop at night, with dark concrete flooring, low perimeter walls, evenly spaced warm safety lights, a dense illuminated modern skyline, deep navy sky, and one bright full moon.
- Picture 2: The complete six-panel photorealistic storyboard, used as the shot-order, composition, dance-action, character, costume, rooftop, lighting, and cinematic-style reference. Treat each panel as a separate chronological shot beat, not as one composite image.
summary:
Reference generation + keyframe completion. Create a 12-second photorealistic cinematic rooftop dance video following the six panels of Picture 2 from left to right and top to bottom. The sequence progresses from a distant rooftop introduction to a confident walk, expressive low-angle gesture, overhead dance step, dynamic spin, and final moonlit pose.
retention_analysis:
- (S1) — fully_preserved: retain the same face, silver-and-blue hair pattern, glasses, body proportions, and confident personality in every shot.
- (S2) — fully_preserved: retain the exact blue garments, cardigan knit texture, skirt construction, necklace, and white platform heels.
- (S3) — fully_preserved: retain the rooftop geometry, wet-looking concrete reflections, skyline arrangement, night lighting, and single moon.
- Picture 2 — shot_plan_reference_only: retain the six-stage shot order, per-panel composition and photographic style as the shot plan; never reproduce the grid layout, gutters, captions, or empty panels as image content.
detailed_description:
Photorealistic live-action nighttime cinematography with crisp natural skin texture, realistic silver-and-blue hair strands, detailed knitted fabric, controlled highlights, shallow depth of field, cool city illumination, and warm rooftop edge lights. Keep grounded human movement and physically believable balance in platform heels.
[Shot 1] Panel 1 (row 1, column 1) establishes (S1) wearing (S2) at the center of (S3). In a very wide symmetrical rooftop shot, the camera performs a smooth, moderately fast forward dolly of approximately four meters. She begins in the distance beneath the moon, shifts her weight, and takes two poised steps toward the camera while the skyline remains stable.
[Shot 2] At 00:02.000, cut to Panel 2 (row 1, column 2), a full-body medium-wide shot. The camera tracks backward at walking speed as she crosses one leg elegantly in front of the other, rolls one shoulder, and lets the cardigan slide naturally along her upper arms. Her heels contact the concrete correctly, while her pleated skirt and long hair respond to the rooftop breeze.
[Shot 3] At 00:03.800, cut to Panel 3 (row 1, column 3), a dramatic low-angle medium close-up with a slight upward tilt. She plants her feet apart, leans toward the lens, and extends her right hand forward in one fluid expressive gesture. Her fingers open naturally near the camera without distortion while her other hand lowers beside her skirt. Her gaze remains locked on the lens.
[Shot 4] At 00:05.600, cut to Panel 4 (row 2, column 1), a high overhead view. The camera performs a slow clockwise orbit of approximately twenty degrees. She steps diagonally with her right foot, pivots on the ball of her left foot, and opens both arms for balance. Her shadow, hair, cardigan sleeves, and skirt rotate coherently with her body.
[Shot 5] At 00:07.500, cut to Panel 5 (row 2, column 2), a dynamic three-quarter full-body shot with a restrained ten-degree Dutch angle. She completes one controlled clockwise spin. Her silver-and-blue hair sweeps outward in one continuous arc, the cardigan and pleated skirt flare with realistic inertia, and both heels remain stable when she lands.
[Shot 6] At 00:09.700, cut to Panel 6 (row 2, column 3), a moonlit medium portrait. She settles into the final pose with one hand behind her head and the other resting lightly near her hip. The camera slowly pushes in by 6%. Her hair and cardigan gradually settle, she raises her chin, gives one natural blink, and finishes with a confident, relaxed look toward the camera. The full moon remains visible beside her head.
Maintain one facial identity, exact hair-color placement, glasses shape, necklace, garment design, correct fingers, stable legs, and consistent heel construction. No storyboard grid, panel borders, numbers, split screen, duplicated moon, additional people, changing skyline, warped hands, broken heels, foot sliding, excessive spinning, sudden costume changes, subtitles, text, logos, or watermarks.
overall_soundscape:
Steady rooftop wind, distant city traffic, faint urban hum, precise platform-heel footsteps on concrete, and natural cardigan, skirt, and hair movement. No dialogue.
non_diegetic_music:
N/A"""

_V2_RULES = """\
你是 TubeFlow 的 MiniMax H3 结构化视频提示词编译器。必须只输出 JSON 对象，不要输出 Markdown。
目标：将输入的分镜方案编译为符合 MiniMax H3 Base Prompt 规范的结构化英文提示词，驱动参考图生成连续全帧视频。

必须遵守：
1. 视觉、镜头、动作与音景描述全部使用清晰英文。
2. 输出 JSON 只允许一个键 "prompt"，值为完整提示词纯文本。
3. prompt 由以下六个分段组成，段名独占一行、全小写、英文冒号，严格按此顺序各出现一次：subject_definitions、summary、retention_analysis、detailed_description、overall_soundscape、non_diegetic_music。
4. 不要分析图片内容；以稳定名称 Picture 1、Picture 2……指代输入 reference_roles 中的同序参考图；出场主体使用稳定编号 (S1)(S2)……。
5. subject_definitions 中：为每个出场角色主体写一句定义并绑定对应 Picture N（如 (S1) is the ... from Picture 1）；还必须把最后一张参考图（role=STORYBOARD）定义为一个主体，写明它是完整的多格故事板、是 shot-order、composition、action、style 参考，并逐字包含句子 "Treat each panel as a separate chronological shot beat, not as one composite image."。
6. summary 用一到两句概括全片，并声明按故事板格子 from left to right and top to bottom 的顺序推进。
7. retention_analysis 用逐条清单，每个角色主体、场景主体与故事板主体各一条，格式 "- (S1) — fully_preserved: …"。
8. detailed_description 先写一段全局摄影风格段（基于 visual_style 展开画面质感、光线与运动物理）；随后按 shot_plan 逐镜成块，每块以 "[Shot n]" 开头；Shot 1 直接开场，Shot 2 起每块以 "At {cut_at}, cut to" 接续（cut_at 与输入逐字一致，mm:ss.mmm 格式）；每镜写清场景、主体动作与具体量化运镜（距离/角度/幅度数值）；镜头顺序与 shot_plan 一致，不得自行重排或改写切镜时刻。镜头块数量必须与 shot_plan 条目数完全一致：shot_plan 有几条就只输出 [Shot 1] 至对应编号的几块，无论视频时长多长都不得拆分、新增或合并镜头块；shot_plan 只有 1 条时只输出一个 [Shot 1] 块，用单镜动作填满全片。
9. detailed_description 末尾必须依次输出两段收尾：一段以 "Maintain " 开头的一致性清单（面部身份、服装、场景几何、风格等逐条罗列）；紧接一段以 "No storyboard grid" 开头的负规则清单，至少包含 panel borders, split screen, text, watermarks。
10. 禁止人物对白、台词与口型说话，禁止任何人声，不得使用 <d> 标签。
11. overall_soundscape 只写与场景一致的 H3 原生环境声与动作音效，不得含人声。
12. non_diegetic_music 必须写 N/A。
13. 不得新增、删除、合并或重命名分段；不得输出 visual_asset_bindings 行（由系统确定性拼装）。
14. format_example 仅作分段结构、镜头块格式与详略示范，禁止复用其中的角色、场景、情节、音乐等内容元素。
15. 若输入含 repair_reason，必须优先修正其中指出的 expected/actual 差异，输出前逐项复核所有硬性格式约束，不得原样重发。
16. format_example 的镜头数量仅作示例；实际镜头块数量、编号与顺序只由当前 shot_plan 决定，严禁照抄示例的镜头数量或编号。"""

# v3 规则：基于 v2 改三处——①主体定义禁止任何图片来源指代（图片用途由系统绑定行
# 确定性表达）；②故事板留存条目改为 shot_plan_reference_only（消除与负规则的冲突）；
# ③每镜块带逐字格位 Panel n (row r, column c)（镜头↔故事板格子显式映射）。其余要件同 v2。
_V3_RULES = """\
你是 TubeFlow 的 MiniMax H3 结构化视频提示词编译器。必须只输出 JSON 对象，不要输出 Markdown。
目标：将输入的分镜方案编译为符合 MiniMax H3 Base Prompt 规范的结构化英文提示词，驱动参考图生成连续全帧视频。

必须遵守：
1. 视觉、镜头、动作与音景描述全部使用清晰英文。
2. 输出 JSON 只允许一个键 "prompt"，值为完整提示词纯文本。
3. prompt 由以下六个分段组成，段名独占一行、全小写、英文冒号，严格按此顺序各出现一次：subject_definitions、summary、retention_analysis、detailed_description、overall_soundscape、non_diegetic_music。
4. 不要分析图片内容；reference_roles 中的 Picture 1、Picture 2……只是输入参考图的稳定标签；出场主体必须使用稳定编号 (S1)(S2)……，不得把角色参考图当作主体输出。
5. subject_definitions 中只允许两类条目：(Sx) 主体定义和唯一的故事板 Picture 条目。为每个出场角色主体写一句纯外观与身份定义（五官身份、发型、体型、服装等）；严禁为角色图创建 "- Picture 1:" 等独立条目，严禁在 (Sx) 条目任何位置写 Picture N（包括 "from Picture N"、"shown in Picture N"、"as Picture N"、"same … as Picture N" 等，图片用途由系统绑定行统一表达）；仍必须在 subject_definitions 中写出最后一张参考图（role=STORYBOARD）的唯一条目，但该条目必须以 "- Picture {N}:" 开头（N 为故事板在 reference_roles 中的序号），写明它是完整的多格故事板、是 shot-order、composition、action、style 参考，并逐字包含句子 "Treat each panel as a separate chronological shot beat, not as one composite image."；故事板不是出场主体，严禁为它分配 (Sx) 编号（不得写 "- (S2): … storyboard …"）。若 repair_reason 为 h3_prompt_image_provenance_forbidden，必须删除全部角色图 Picture 条目并清除 (Sx) 中所有 Picture 指代，只保留纯外观主体定义与故事板 Picture 条目，不得换种措辞重复违规。
6. summary 用一到两句概括全片，并声明按故事板格子 from left to right and top to bottom 的顺序推进。
7. retention_analysis 用逐条清单：每个角色主体与场景主体各一条，格式 "- (S1) — fully_preserved: …"；故事板一条，格式必须逐字为 "- Picture {N} — shot_plan_reference_only: …"（N 为故事板在 reference_roles 中的序号，且必须与 subject_definitions 中故事板条目的 Picture 序号一致），写明 retain its shot order, per-panel composition and style; never reproduce the grid layout, gutters, captions, or empty panels as image content；故事板条目不得使用 fully_preserved，也不得把主语写成 (Sx)（"-(S2) — shot_plan_reference_only:" 属非法格式）。
8. detailed_description 先写一段全局摄影风格段（基于 visual_style 展开画面质感、光线与运动物理）；随后按 shot_plan 逐镜成块：Shot 1 块以 "[Shot 1] Panel 1 (row 1, column 1)" 起笔，Shot 2 起每块以 "[Shot n] At {cut_at}, cut to Panel n (row r, column c)" 起笔（cut_at 与输入逐字一致，mm:ss.mmm 格式；Panel 编号与 row、column 逐字取自输入 shot_plan 条目的 panel 字段，不得自行换算）；每镜写清场景、主体动作与具体量化运镜（距离/角度/幅度数值）；镜头顺序与 shot_plan 一致，不得自行重排或改写切镜时刻与格位。镜头块数量必须与 shot_plan 条目数完全一致：shot_plan 有几条就只输出 [Shot 1] 至对应编号的几块，无论视频时长多长都不得拆分、新增或合并镜头块；shot_plan 只有 1 条时只输出一个 [Shot 1] 块，用单镜动作填满全片。
9. detailed_description 末尾必须依次输出两段收尾：一段以 "Maintain " 开头的一致性清单（面部身份、服装、场景几何、风格等逐条罗列）；紧接一段以 "No storyboard grid" 开头的负规则清单，至少包含 panel borders, split screen, text, watermarks。
10. 禁止人物对白、台词与口型说话，禁止任何人声，不得使用 <d> 标签。
11. overall_soundscape 只写与场景一致的 H3 原生环境声与动作音效，不得含人声。
12. non_diegetic_music 必须写 N/A。
13. 不得新增、删除、合并或重命名分段；不得输出 visual_asset_bindings 行（由系统确定性拼装）。
14. format_example 仅作分段结构、镜头块格式与详略示范，禁止复用其中的角色、场景、情节、音乐等内容元素。
15. 若输入含 repair_reason，必须优先修正其中指出的 expected/actual 差异，输出前逐项复核所有硬性格式约束，不得原样重发。
16. format_example 的镜头数量仅作示例；实际镜头块数量、编号与顺序只由当前 shot_plan 决定，严禁照抄示例的镜头数量或编号。"""


FEW_SHOT_SINGLE_PANEL_OPT_IN_V1 = """subject_definitions:
- The lantern-headed investigator Lumen, with a travel-worn dark cloak and one warm amber lantern glow.
- Picture 2: The single-panel storyboard reference for Shot 1, used as the composition, action, and style reference for Shot 1 only.
summary:
A quiet candlelit investigation advances through the single-panel references in shot order.
retention_analysis:
- Lumen — fully_preserved: retain the cloak, silhouette and single amber glow.
- Picture 2 — shot_plan_reference_only: retain Shot 1 composition and style; never reproduce reference borders, captions, or unrelated panel content as image content.
detailed_description:
Dark oil-painted illustration with thick texture, restrained amber light and a deep navy palette.
[Shot 1] <Picture 2>: a close view of one candle on an old map, the flame moving gently while the camera holds a slow, measured push-in.
Maintain the investigator identity, the single candle, the old map, and the navy-and-amber style.
No storyboard grid, panel borders, split screen, captions, text, logos, or watermarks.
overall_soundscape:
A soft candle hiss and quiet room tone, with no voices.
non_diegetic_music:
N/A"""


def _single_panel_system_prompt() -> str:
    replacements = {
        "4.": "4. 不要分析图片内容；以 reference_roles 中的稳定名称 Picture 1、Picture 2……指代输入参考图。Picture 1 仅作角色身份参考；每个 role=SHOT_PANEL 的 Picture 只作同号镜头的构图、动作和风格参考，禁止跨镜头复用或把单格参考重新拼成网格。",
        "5.": "5. subject_definitions 中：每个出场角色主体写纯外观与身份定义，禁止 from/shown in Picture N 等角色图片来源指代；每个 SHOT_PANEL 参考各写一条 '- Picture N: The single-panel storyboard reference for Shot K, used as the composition, action, and style reference for Shot K only.'；不得生成完整多格故事板条目，不得给参考图分配 (Sx) 编号。",
        "6.": "6. summary 用一到两句概括全片，并明确按全部 single-panel references 的 shot order 推进，不得写成复制整张网格或 from left to right and top to bottom 的网格阅读指令。",
        "7.": "7. retention_analysis 用逐条清单：角色与场景主体保留 fully_preserved 条目；每个 SHOT_PANEL 各一条 '- Picture N — shot_plan_reference_only: retain Shot K composition and style; never reproduce reference borders, captions, or unrelated panel content as image content.'；禁止把任何单格参考写成 fully_preserved。",
        "8.": "8. detailed_description 先写全局摄影风格段，再严格按 shot_plan 逐镜成块：Shot 1 以 '[Shot 1] <Picture 2>:' 起笔；Shot 2 起以 '[Shot n] At {cut_at}, cut to <Picture n+1>:' 起笔。Picture 编号必须使用该镜 shot_plan.reference，cut_at 逐字使用该镜输入；每镜写清场景、主体动作和具体量化运镜，不得改时序、换参考、增加、拆分或合并镜头。末尾依次输出 Maintain 一致性清单和 No storyboard grid 负规则。",
    }
    lines = []
    for line in _V3_RULES.splitlines():
        prefix = next((key for key in replacements if line.startswith(key)), None)
        lines.append(replacements[prefix] if prefix else line)
    return "\n".join(lines) + "\n"


def build_system_prompt(strategy: VideoPromptStrategy, *, duration_seconds: int) -> str:
    if strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1:
        return _single_panel_system_prompt()
    if strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2:
        return _V2_RULES
    if strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3:
        return _V3_RULES
    base = _BASE_RULES.replace("{duration}", str(duration_seconds))
    if strategy is VideoPromptStrategy.KINESTACK_V6_PORT:
        return base
    if strategy is VideoPromptStrategy.TUBEFLOW_ENHANCED_V1:
        return base + "\n" + _ENHANCED_RULES
    raise ValueError(f"unknown video prompt strategy: {strategy}")


def build_user_payload(
    value: VideoPromptCompilerInput | SinglePanelPromptCompilerInput,
    *,
    repair_reason: str | None = None,
) -> dict[str, object]:
    if isinstance(value, SinglePanelPromptCompilerInput):
        return build_single_panel_user_payload(value, repair_reason=repair_reason)
    if value.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3_SINGLE_PANEL_OPT_IN_V1:
        raise VideoPromptViolation("single_panel_reference_required")
    roles: list[dict[str, object]] = [
        {"label": label, "picture": f"Picture {index}", "role": "CHARACTER"}
        for index, label in enumerate(value.character_labels, 1)
    ]
    roles.append(
        {
            "label": value.storyboard_label,
            "picture": f"Picture {len(roles) + 1}",
            "role": "STORYBOARD",
        }
    )
    shot_plan: list[dict[str, object]] = []
    layout = (
        grid_layout_for(len(value.shots), value.aspect_ratio)
        if value.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3
        else None
    )
    for shot, shot_range in zip(value.shots, value.shot_ranges, strict=True):
        entry: dict[str, object] = {
            "order": shot.order,
            "time_range": shot_time_text(shot_range),
            "scene": shot.scene,
            "shot_size": shot.shot_size,
            "camera_viewpoint": shot.camera_viewpoint,
            "action_outcome": shot.subject_action_outcome,
            "narrative_function": shot.narrative_function,
            "description": shot.description,
        }
        if (
            value.strategy
            in (
                VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2,
                VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3,
            )
            and shot.order > 1
        ):
            entry["cut_at"] = shot_cut_point_text(shot_range.start_ms)
        if layout is not None:
            row, column = panel_row_column(shot.order, layout.cols)
            entry["panel"] = {"index": shot.order, "row": row, "column": column}
        shot_plan.append(entry)
    payload: dict[str, object] = {
        "json_task": "compile_minimax_h3_video_prompt",
        "generation_context": {
            "duration_seconds": value.duration_seconds,
            "aspect_ratio": value.aspect_ratio,
        },
        "reference_roles": roles,
        "visual_style": value.visual_style,
        "shot_plan": shot_plan,
        "fixed_constraints": [
            "no_dialogue",
            "no_lip_sync_speech",
            "no_music",
            "ambient_sound_only",
        ],
    }
    if value.strategy is VideoPromptStrategy.TUBEFLOW_ENHANCED_V1:
        payload["format_example"] = FEW_SHOT_FORMAT_EXAMPLE
    if value.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V2:
        payload["format_example"] = FEW_SHOT_STORYBOARD_V2
    if value.strategy is VideoPromptStrategy.TUBEFLOW_STORYBOARD_V3:
        payload["format_example"] = FEW_SHOT_STORYBOARD_V3
    if repair_reason:
        payload["repair_reason"] = repair_reason
    return payload


def build_single_panel_user_payload(
    value: SinglePanelPromptCompilerInput,
    *,
    repair_reason: str | None = None,
) -> dict[str, object]:
    roles: list[dict[str, object]] = [
        {
            "label": label,
            "picture": f"Picture {index}",
            "role": "CHARACTER",
            "identity_reference_only": True,
        }
        for index, label in enumerate(value.character_labels, 1)
    ]
    for panel in value.panels:
        roles.append(
            {
                "label": panel.label,
                "picture": f"Picture {len(roles) + 1}",
                "role": "SHOT_PANEL",
                "shot": panel.shot_order,
                "source_storyboard_sha256": panel.source_storyboard_sha256,
                "derivative_sha256": panel.derivative_sha256,
                "derivation": panel.derivation,
            }
        )
    shot_plan: list[dict[str, object]] = []
    for shot, shot_range, panel in zip(
        value.shots, value.shot_ranges, value.panels, strict=True
    ):
        entry: dict[str, object] = {
            "order": shot.order,
            "time_range": shot_time_text(shot_range),
            "scene": shot.scene,
            "shot_size": shot.shot_size,
            "camera_viewpoint": shot.camera_viewpoint,
            "action_outcome": shot.subject_action_outcome,
            "narrative_function": shot.narrative_function,
            "description": shot.description,
            "reference": panel.label,
        }
        if shot.order > 1:
            entry["cut_at"] = shot_cut_point_text(shot_range.start_ms)
        shot_plan.append(entry)
    payload: dict[str, object] = {
        "json_task": "compile_minimax_h3_video_prompt_single_panel_opt_in",
        "strategy": value.strategy.value,
        "generation_context": {
            "duration_seconds": value.duration_seconds,
            "aspect_ratio": value.aspect_ratio,
        },
        "reference_roles": roles,
        "visual_style": value.visual_style,
        "shot_plan": shot_plan,
        "fixed_constraints": [
            "no_dialogue",
            "no_lip_sync_speech",
            "no_music",
            "ambient_sound_only",
            "one_shot_panel_reference_per_shot",
            "no_grid_or_panel_leakage",
        ],
        "format_example": FEW_SHOT_SINGLE_PANEL_OPT_IN_V1,
    }
    if repair_reason:
        payload["repair_reason"] = repair_reason
    return payload
