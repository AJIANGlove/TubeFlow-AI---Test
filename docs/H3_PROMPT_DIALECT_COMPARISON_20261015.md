# H3 视频提示词方言对照分析（2026-10-15）

> 性质：纯分析文档，不构成已确认决策，不改动任何代码与合同。
> 触发：用户要求将 zenstory-ai/drama-skills 的 MiniMax H3 提示词规范与本项目现行
> `tubeflow_storyboard_v2` 模板逐条对照，列出差异与可借鉴点。

## 0. 对照三方与证据来源

| 方 | 材料 | 说明 |
|---|---|---|
| 本项目现行实现 | `backend/src/tubeflow_test/video_prompt/templates.py`（`_V2_RULES`、`FEW_SHOT_STORYBOARD_V2`）；`backend/src/tubeflow_test/video_prompt/domain.py`（`validate_compiled_prompt`） | 默认策略 `tubeflow_storyboard_v2`，编译器为 DeepSeek `deepseek-v4-flash` |
| MiniMax 官方 | `MiniMax-AI/MiniMax-H3` 仓库 `skills/h3-prompt-writing/references/base-en.txt` 与 `ref-en.txt`（2026-10-15 抓取，存 `tmp/ref/`）；官方 API 文档页 `platform.minimax.io/docs/api-reference/video-generation-v2-create`（同日抓取） | 方言事实的最高权威 |
| 社区产线经验 | `zenstory-ai/drama-skills`（MIT，2026-10-14 更新），`skills/short-drama-video-prompts/references/minimax-h3.md`、`motion-recipe.md`、`generability.md` 等（克隆于 `tmp/ref/drama-skills/`） | 标注了 `structural_invariant` / `craft_default` / `taste_option` 三档规则强度，多数附小样本实测 |

## 1. 已对齐项（无需改动）

经逐条核对，以下方面本项目与官方指南一致或更严：

1. 六段结构、段名、顺序、全小写英文段名：与官方 ref-en.txt §1 完全一致。
2. `non_diegetic_music: N/A` 显式写空：与官方示例一致；drama-skills 有实测支撑（省略该段时小样本中出现模型自行补乐）。本项目由校验器强制（`h3_prompt_music_forbidden`）。
3. 切镜时间戳格式 `[Shot N] At MM:SS.mmm, ...`、首镜不带时间戳：与官方 §5.1 一致。
4. 参考图 ≤9 张、首/尾帧与参考模式互斥、本项目只用 REF2VA：与官方 API 输入合同一致。
5. 禁字幕/禁故事板文字入镜、负规则清单：方向一致，且本项目校验器强制（`h3_prompt_negative_rules_missing`）。
6. 编译输出机器校验（六段齐全、镜头块数与 shot_plan 严格相等、切点逐字命中、`Maintain `/`No storyboard grid` 收尾、输入指纹 SHA-256 二次核对）：比 drama-skills 的提示词侧检查更严格，是本项目的优势项。
7. 切点精度兜底：官方与 drama-skills 均承认切点"接近要求但有偏差，要求精确时应检查实际切点并在剪辑中调整"；本项目合成时按实测配音时长裁剪，已覆盖。

## 2. 方言级差异（逐条）

### D1. 素材/主体标签写法：裸 `Picture N` + `(S1)` vs 官方 `<Picture N>` + `<Subject N>`

- 本项目：`Picture 1`、`(S1)`（`templates.py` 规则 4/5 与示例）。
- 官方：`<Picture N>` 指具体帧/分镜锚点，`<Subject N>` 指可复用可见内容；标签一旦指派，六段内保持同一写法（ref-en.txt §2）。drama-skills 同样使用尖括号，且强调编号必须按素材提交顺序，"两套编号不一致时模型会收到互相矛盾的素材说明，而接口不会报错"。
- 评估：语义映射能对上（我们的 `(S1)`≈`<Subject N>`，`Picture N`≈`<Picture N>`），但记号非官方写法。补充证据（2026-10-15 核实）：KineStack 引擎自己的引用翻译函数 `translate_references` 把 `@图一` 等中文引用翻成 **`<Picture 1>` 尖括号形式**（`kinestack-engine/app/h3_workflows.py:222-227`）——即本部署自身的惯例也是尖括号。无法仅凭文本断定对生成质量的影响方向，但三个独立来源（官方指南、drama-skills、KineStack 引擎）一致使用尖括号。
- 级别：**需 A/B 实验**。改动面：模板规则、示例、校验器（`h3_prompt_reference_missing` 的正则口径）。

### D2. 故事板的角色定义：官方要求"逐镜映射声明"

- 本项目：v2 规则 5 把故事板定义为一个主体，含固定句 "Treat each panel as a separate chronological shot beat..."；格子映射靠故事板上印刷的秒数区间与格子编号。
- 官方 §2.2：图像作为 storyboard/shot-planning 参考时，应"state which shots it maps to and what planning information it provides"，例：`<Picture 3> is a storyboard reference for [Shot 1] and [Shot 2], defining their viewpoint, subject placement, and shot order.`
- 可借鉴：在故事板定义句中显式列出"格子 i ↔ [Shot i] ↔ 时间区间"的映射声明（信息在编译输入里全部现成，可确定性生成或要求编译器输出）。
- 级别：**可直接借鉴**，改动限于模板文本与校验器新增断言，不改数据与接口。

### D3. retention 分级词汇与镜次标注

- 本项目：`retention_analysis` 只用 `fully_preserved`，格式 `- (S1) — fully_preserved: …`，无镜次标注。
- 官方 §4：视觉素材四级 `fully_preserved / partially_preserved / attribute_transfer / weak_reference`；条目格式含镜次，如 `<Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - ...`。
- 可借鉴：(a) 故事板条目的保留强度宜降为 `partially_preserved`（格子的构图/姿态只需部分保留，逐格强 `fully_preserved` 可能过约束，诱发格子复刻）；(b) 条目附 `(appears in [Shot n])` 镜次。
- 级别：**需 A/B 实验**（保留强度变化影响画面自由度，方向需实测）。

### D4. summary 任务类型前缀

- 本项目：示例为 "Reference generation + keyframe completion."，规则未强制方括号前缀。
- 官方 §3：summary 以方括号任务类型开头，如 `[reference generation]`，多类型用 ` + ` 连接。
- 级别：**可直接借鉴**（纯格式，校验器可加一条前缀断言）。

### D5. detailed_description 篇幅带

- 官方 §5.2：生成任务 `detailed_description` 通常 350–500 英文词；单镜不因此缩短，信息按镜头负载分配。
- 本项目：只有 20000 字符硬上限，无篇幅带。
- 可借鉴：在模板中给出词数带（如 300–500 词），并允许编译校验对过短输出告警；是否硬校验需实验。
- 级别：**可直接借鉴为软指导**，硬校验需 A/B。

### D6. 多镜容器的"对齐句"与逐切关键帧实测

- drama-skills 实测（9 秒三切容器）：每切挂各自关键帧 + 正文首行对齐句（`<Picture 1> aligns with the 0.00-second mark as the opening frame of [Shot 1]; ...`），切点落在 2.88–3.04 / 6.00–6.04 秒；只挂首切关键帧时切点漂到 2.92–3.88 / 5.88–6.21 秒，并出现一次多余幻切。
- 本项目设计与之同源：多格故事板的格子即"逐切关键帧合一"，且格子印刷秒数区间——drama-skills 的实测从侧面支持该设计。
- 可借鉴：正文首行（`subject_definitions` 之前）加一句对齐句，把"格子 i"逐字钉到秒数。官方 base-en.txt 中 I2VA/FL2VA 模式本就有首行对齐指令的惯例（§2.1），说明模型对该句式有训练接触。
- 级别：**需 A/B 实验**（v2 已实测稳定，引入新句式需对照验证不劣化）。

### D7. 无对白镜的嘴部状态

- drama-skills `craft_default`：无对白不等于全程闭嘴，"人物嘴部状态按实际表演写"；强制闭嘴会导致表情僵硬。
- 本项目：规则 10 禁止对白与口型说话（用户确认边界：配音后期叠加）。该边界不变，但可在措辞上区分"禁止发声说话"与"允许自然的非言语口部动作（喘息、微笑、咀嚼）"，避免模型把人物统一处理成抿嘴僵脸。
- 级别：**可直接借鉴措辞**，不触碰"禁人声"边界。

### D8. 时长合同：官方整数 4–15 秒 vs 本项目 3–15 秒（2026-10-15 已核实：不冲突）

- 官方 API 文档（2026-10-15 抓取）：`duration` 必填、整数，MiniMax-H3 云端 API 为 **4–15** 秒，H3-Max 为 5–15 秒。drama-skills 同样标注为 `structural_invariant`。
- 本项目：`domain.py` 接受 3–15 秒（`max(3, ceil(实测毫秒/1000))`）；USER_DECISIONS §7.9 确认"Provider 请求时长最低为 3 秒"。
- **核实结论**：本项目不经官方云端 API，而是经 KineStack 三节点（仙宫云社区 ComfyUI H3 部署，模型版本 `2026-08-13-community-duration-v1`）提交。该部署的引擎层明确接受 3–15 秒（`kinestack-engine/app/h3_workflows.py` `MIN_DURATION_SECONDS = 3`，`community_store.py` 校验信息"视频时长必须是 3 至 15 秒之间的整数"），秒数按 24fps 换算后取整到 17n+5 帧的时间网格。已有真实边界验收记录（KS-0004 工作项，2026-08-13/14）：3 秒文生 Attempt `dc141568-6c92-49be-b1ff-9d1a40e7d8ab` 实际产出 73 帧 = 3.042 秒；10 秒 → 243 帧 = 10.125 秒；15 秒单参考图 → 362 帧 = 15.084 秒，三项均 24fps 且字节数与 SHA-256 校验通过。KineStack 生产页面的时长控件同样是最小 3、最大 15。
- 因此：官方云端文档的 4 秒下限**不适用于**本部署；§7.9 的 3 秒下限在 KineStack 路径上是被真实验收过的行为，无需调整。唯一需知晓的副作用：3 秒请求实际产出 3.042 秒（网格取整），由合成阶段裁剪到精确组时长覆盖，属既有设计。
- 附带发现：官方云端 API 的 ComfyUI 伙伴节点（云 API 节点，duration min=4/max=15）与本地社区部署是两个不同接入形态；本项目只使用后者。

### D9. 时效性提醒：drama-skills 对 H3-Max 的描述已过时

- drama-skills 称 H3-Max"仅支持文生和首/尾帧，不支持 full-reference"；当前官方 API 页显示 H3-Max 已支持 reference-to-video（参考图/视频/音频）。
- 结论：方言文档会老化，官方 API 文档为准。本项目不直连官方 API、不用 H3-Max，本条仅为信息来源可信度的标定。

### D10. 连续段续接（reference_video + 实际尾帧）

- 官方与 drama-skills 支持以 `reference_video`（上一段实际视频）+ 尾帧参考图做跨段连续。
- 本项目各配音分组独立生成，无跨组续接。
- 级别：**不适用本期**；若未来用户希望改善组间画面连续性，此为官方支持的路线，需另行产品决策。

## 3. 可借鉴的工艺规则（与模型方言无关，面向减少崩镜）

来自 drama-skills `generability.md`（动作可生成性），可进入编译器系统提示词或分组导演提示词：

1. 判据一句话：动作写成"日常影像中高频、整体、单一部位可完成"的动作；自检法是"念给没读过剧本的人，能否马上比划出来"。
2. 四类高风险写法（建议编译器主动改写或告警）：
   - 精确拦截（两主体同时精确作用于同一物件）；
   - 不可见内部状态（"神色一凛"类推断词，应翻成可见表情/肢体）；
   - 否定式动作（"没有接话"，应改写为正在发生的替代动作）；
   - 三步以上双手精细编排链（物理上每步不足一秒）。
3. 改写顺序：换承担者 → 拆到相邻镜 → 换成停留 → 交给声音 → 标注高风险并给降级实现。

与本项目现状的关系：v2 规则 8 已要求量化运镜与具体动作，但对"动作可生成性"无显式约束；上述清单可作为编译器规则增量。级别：**可直接借鉴为提示词规则**（纯文本增量），是否加机器校验另行决定。

## 4. 不宜引入项（边界核对）

1. 对白体系（`<d>`、说话人绑定 `(Sx)`、参考音频 `fully_copy` 等）：与 USER_DECISIONS §7.17"删除台词/旁白规则体系、强制禁止人物对白"直接冲突，不引入。
2. drama-skills 的 REF/PLAN 文档流与 agent skill 形态：面向 Claude Code/Codex 交互环境，不适合直接装入本项目后端；只借鉴知识与规则，不引入其代码与文件约定。
3. OpenMontage（上一轮检索）：AGPL-3.0 许可证有传染性，且其定位是 agent 编排系统，与本项目架构不同；仅可参考其"渲染后 ffprobe 校验、抽帧查黑帧、响度分析"的质检思路，不引入代码。
4. L2VA（仅尾帧）模式：本项目起点权威在故事板与导演方案，与 drama-skills 弃用 L2VA 的理由一致，维持不用。

## 5. 建议后续动作（均需用户决定，本分析不改动任何实现）

| 优先级 | 动作 | 类型 |
|---|---|---|
| ~~1~~ | ~~实机核实 D8~~ 已完成（2026-10-15）：KineStack 社区部署接受 3–15 秒并有 3/10/15 秒真实验收记录，与官方云端 4–15 文档不冲突，§7.9 无需调整，见 D8 | 已定论 |
| 2 | 若做下一轮提示词 A/B：候选变量为 D1（尖括号标签）、D3（retention 分级+镜次）、D6（对齐句），单变量对照，沿用 §7.17 的实机 A/B 规程 | 需用户批准真实调用额度 |
| 3 | D2/D4/D5/D7 与 §3 工艺规则：纯提示词文本增量，可与 A/B 合并为一版候选模板 | 需用户批准后改模板与测试 |
| 4 | 画面级"多格同框"质检兜底（USER_DECISIONS §7.20 已注明另行立项）：可评估 Netflix VMAF / DOVER 作为客观画质基线 | 另行立项时评估 |

## 附：关键原文出处

- 官方 ref-en.txt §2.2 故事板映射、§3 任务类型前缀、§4 保留分级、§5.2 篇幅带（`tmp/ref/h3-ref-en.txt`，源：github.com/MiniMax-AI/MiniMax-H3）。
- 官方 API duration/resolution/输入互斥（platform.minimax.io/docs/api-reference/video-generation-v2-create，2026-10-15 抓取）。
- drama-skills minimax-h3.md 多镜容器实测、无对白镜、时长 invariant；generability.md VID-21 与四类高风险写法（`tmp/ref/drama-skills/`，MIT）。
- 本项目模板与校验：`backend/src/tubeflow_test/video_prompt/templates.py`、`backend/src/tubeflow_test/video_prompt/domain.py`。
