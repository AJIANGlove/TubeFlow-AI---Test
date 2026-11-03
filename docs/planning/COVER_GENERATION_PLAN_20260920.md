# 封面图生成功能实施计划（已批准基线 v4）

日期：2026-09-20。状态：决策全部确认，作为实施基线；实施中的真实付费调用仍按授权文件逐次报批。
依据：`docs/planning/USER_DECISIONS.md` §18、`docs/planning/FINAL_ACCEPTANCE_CRITERIA.md` §20、`runtime/operations/cover_feature_authorization_20260920.md`、参考交接文档 `D:\AAA-project\Intelligent editing software\docs\参考图勾选生成_实现交接文档.md`。

v4 变更（2026-09-20 用户确认）：封面文字与画面改为**串行链路**——先独立文字调用依标题+文案全文生成 5 条候选（模型推荐条），再以选中文字作为画面生成输入，使标题、封面文字、画面三者互相强化形成统一钩子；评委 rubric 增加第 7 维 `text_visual_match`；面板支持换候选重装配与「以所选文字重新生成画面」。

本计划只覆盖首期独立手动功能；并入 AUTO 链路、YouTube 合规压缩副本属于未来另行决策项，不在本计划内。

## 1. 功能一句话

为每个已冻结项目生成 YouTube 封面图：参考图（或已固定的频道风格文本）经视觉解析/勾选得到参考约束 → 文本模型先依标题与文案全文生成封面文字（5 候选取推荐）→ 再以该文字 + 标题 + 文案全文 + 用户补充需求按 8 维度 36 子项契约生成匹配画面描述（两版并行 + 评委择优 + 审计整改）→ 装配层确定性拼成最终提示词（维度文本 + 绘字段 + 锚点）→ gpt-image-2 出 1 张图（含绘字）→ 不可变版本归档并随导出 ZIP 交付。

## 2. 模块划分（对齐现有分层，高内聚低耦合）

设计约束：封面功能是**纯新增**，不修改配音/导演/故事板/视频/合成任一既有模块的行为；既有代码的触点仅 4 处且均为追加式（`STAGES`、`_workflow_stages`、导出 ZIP 工厂、项目删除清理枚举、频道/项目序列化布尔字段）。规则全部集中在领域纯函数，Provider 细节全部在适配器，编排全部在应用服务——任何一层可单独替换与测试。

| 层 | 新文件 | 职责（内聚点） |
| --- | --- | --- |
| 领域 | `backend/src/tubeflow_test/domain/cover.py` | 8 维 36 子项常量、勾选编码、解析校验/渲染/回读、provided/splice/让步、封面文字任务单与候选校验、画面任务单、装配（维度文本 + 绘字段 + 锚点）、审计激活矩阵、评委规整、生成契约校验；纯函数，零框架/零 I/O 依赖 |
| 应用 | `application/cover_style.py`、`application/cover_prompts.py`、`application/cover_images.py` | 风格草稿/版本、提示词版本、封面图版本的用例编排；意图 + 幂等键 + outbox（照 `application/storyboards.py:373-440` 模式） |
| 适配器 | `adapters/cover_ref_analysis.py`、`adapters/cover_prompt.py`、`adapters/cover_image.py` | 三个 Provider 通道（见 §5），互不知道彼此存在；另配内存 fake 实现供测试 |
| 持久化 | `adapters/persistence/cover/` | SQL 与内存两套仓储，合同测试对齐 |
| 工作流 | `workflows/cover.py` | 三个 Temporal 工作流（解析/提示词/出图），活动内执行 Provider 调用 |
| API | `api/cover.py` | 频道与项目两组路由（见 §7） |
| 前端 | `app.js`（STAGES/renderCoverStage）+ `frontend/cover-view.js` + `frontend/cover-view.test.js` | 第十阶段页面与频道页封面风格区 |
| 配置 | `config.py` 三个配置组 + `.env.example` | 见 §4 |

依赖方向：domain ← application ← api/workflows；adapters 实现 domain 的端口协议并被 application 注入；无任何反向依赖。

## 3. 数据模型（迁移 `0030_cover_generation.py`）

照角色 IP 图与故事板生成的资产规则：不可变版本 + 当前指针 + 精确对象键清理。

**`cover_ref_images`（参考图资产，频道或项目作用域）**
`id`、`owner_id`、`scope`（`channel`/`project`）、`channel_id`（可空）、`project_id`（可空）、`sha256`、`object_key`、`byte_size`、`width`、`height`、`image_format`、`created_at`；去重约束 `uq(channel_id, sha256)` 与 `uq(project_id, sha256)`（照 `CharacterImageRow` 模式）。

**`cover_style_drafts`（风格工作草稿，每作用域一份，可变）**
`id`、`owner_id`、`scope`、`channel_id`、`project_id`、`ref_image_id`（可空）、`dims`（JSONB 勾选编码）、`analysis`（JSONB 规整解析结果）、`analysis_dims`（JSONB）、`ref_image_sha256`（解析时图像哈希，供缓存一致性判断）、`edited_text`（≤2000 码点）、`updated_at`。上传/勾选/编辑只写草稿，不调模型；「解析参考图」用显式动作刷新 `analysis/analysis_dims/edited_text/ref_image_sha256`。

**`cover_style_profiles`（频道风格不可变版本 + 当前指针）**
`id`、`owner_id`、`channel_id`、`version`（频道内递增）、`ref_image_id`、`dims`、`analysis_dims`、`edited_text`、`created_at`、`supersedes_id`；当前指针为 `channels.current_cover_style_profile_id`（可空，新列）。「固定为频道风格」把草稿固化为新版本并切换指针；旧版本保留可查。项目作用域不建 profiles——项目试验直接用草稿，生成时以草稿文本为参考约束。

**`cover_prompt_generations`（封面提示词版本）**
`id`、`owner_id`、`project_id`、`version`（项目内递增）、`style_source`（`channel_profile`/`project_draft`/`none`）、`channel_style_profile_id`（可空）、`style_text_used`、`style_dims_used`（JSONB）、`style_override_text`（项目微调，可空）、`brief`（≤500 码点）、`cover_text_candidates`（JSONB，文字调用产出的候选列表）、`cover_text_default`（模型推荐条/回退标题）、`cover_text_used`（画面生成与绘字段实际采用的文字，≤30 码点）、`prompt_body`（维度文本 + 锚点装配结果，不可变；供换候选时确定性重装配）、`prompt_system`（= prompt_body + 绘字段(cover_text_default)，不可变）、`prompt_current`（面板当前版，可编辑/可重置为 system）、`title_used`、`script_sha256`（冻结文案哈希）、`warnings`（JSONB）、`judge`（JSONB 可空）、`model`、`parameters`（JSONB）、`idempotency_key`、`status`、`failure_stage`、`failure_code`、`created_at`。当前指针 `projects.current_cover_prompt_generation_id`（新列）。编辑面板文本只更新当前版本的 `prompt_current`，不产生新版本（与参考实现 promptStudio 语义一致）。

**`cover_image_generations`（封面图不可变版本）**
字段照 `StoryboardGeneration` 精简：`id`、`owner_id`、`project_id`、`prompt_generation_id`、`version`、`prompt`（出图时快照）、`model`、`size`、`quality`、`parameters`、`idempotency_key`、`provider_idempotency_key`、`canonical_request_sha256`、`status`、`provider_receipt`、`expected_sha256`、`downloaded_sha256`、`downloaded_size`、`qc_width`、`qc_height`、`qc_mime`、`object_asset`（MediaAssetRef）、`failure_stage`、`failure_code`、`created_at`。当前指针 `projects.current_cover_image_generation_id`（新列）。首次成功归档自动设为当前；其后由用户明确切换。

项目删除：上述 `project` 作用域行与对象键全部进入项目专属清理清单（按 §9 精确键删除）；频道级 `cover_ref_images`/`cover_style_profiles` 与 `channels.current_cover_style_profile_id` 不受项目删除影响（照角色资产 §9.6）。

## 4. 配置组（`config.py` 追加，env 前缀 `TUBEFLOW_`）

```python
cover_analysis_provider_url: str = "https://grsai.dakka.com.cn/v1"
cover_analysis_provider_token: SecretStr = SecretStr("")
cover_analysis_provider_model: str = "gpt-5.5"
cover_analysis_provider_fallback_model: str = "gemini-3.1-pro"
cover_analysis_provider_timeout_seconds: float = Field(default=120.0, gt=0)
cover_prompt_provider_url: str = "https://api.deepseek.com/v1"
cover_prompt_provider_token: SecretStr = SecretStr("")
cover_prompt_provider_model: str = "deepseek-v4-flash"
cover_prompt_provider_fallback_model: str = "deepseek-v4-pro"
cover_prompt_provider_timeout_seconds: float = Field(default=300.0, gt=0)
cover_image_provider_url: str = "https://api.lingkeai.ai/v1"
cover_image_provider_token: SecretStr = SecretStr("")
cover_image_provider_model: str = "gpt-image-2"
cover_image_provider_size: str = "2560x1440"
cover_image_provider_quality: str = "high"
cover_image_provider_timeout_seconds: float = Field(default=180.0, gt=0)
```

配套：三个 `*_provider_configured` 属性与 URL/model 校验（照 :159-196 既有模式）；`.env.example` 追加对应条目（不含密钥值）。密钥由用户提供后写入 `.env`，不进仓库。

## 5. Provider 适配器（请求形态按交接文档 §1.6 展开）

- **`cover_ref_analysis.py`**：OpenAI 兼容 chat/completions。POST `{baseURL}/chat/completions`，body `{model, temperature:0.1, messages:[system, user(content=[text, image_url(data URL, detail=high)])], response_format:{type:"json_object"}}`。裸 base64 的 MIME 按头字节推断（FF D8 FF→jpeg、RIFF→webp、默认 png）。渠道故障可换 `fallback_model` 一次（同 baseURL/凭据，仅换 model）。
- **`cover_prompt.py`**：同传输形态，DeepSeek 端点，供文字生成、画面生成两版、整改、评委四处复用；温度分别 0.9、0.8/1.0、0.8、0.2；fallback `deepseek-v4-pro`。底层传输复用/对齐既有 LLM 重试语义（408/425/429/5xx 最多两次、退避 1s/3s + 抖动、Retry-After ≤60s）。
- **`cover_image.py`**：POST `{baseURL}/images/generations`，body `{model, prompt, size:"2560x1440", quality:"high", n:1}`；解析 `data[0].b64_json` 或 `data[0].url`（url 时下载 bytes 并复核哈希）。适配器不自动换模型、不自动重试；同步请求-响应，无远端任务对账概念。

三个适配器均注入 `call`/HTTP 客户端依赖，fake 实现用于全部自动化测试（零真实调用）。

## 6. 执行模式（手动触发三动作）

三个动作各自独立：`解析参考图`（频道或项目草稿）、`生成封面提示词`、`生成封面图`。统一照故事板模式：API 同步落意图（幂等键 = owner 域 sha256 provider 键，`Idempotency-Key` 头或动作内确定性推导）→ 同事务写 outbox → Temporal 工作流执行活动 → 终态落库 → 页面 GET 轮询（2.5s/5s/10s 退避、单在途、过期响应保护）。要点：

- 上传参考图、保存勾选/文本、编辑面板、换候选重装配：纯同步/本地确定性操作，零 Provider 调用。
- 解析活动：读草稿（图 sha256 + dims），`(sha256, dims)` 与草稿中 `ref_image_sha256/analysis_dims` 一致且 `edited_text` 非空 → 直接复用（仍算一次显式点击但不产生 Provider 调用）；否则调视觉模型 → `validateRefAnalysis` → `renderRefAnalysisText` → 覆盖草稿 `analysis/analysis_dims/edited_text`（沿用参考实现"重解析覆盖编辑文本"语义）。解析结果为空 → 明确失败码。
- 提示词活动（**串行两步 + 并行两版**）：
  1. **封面文字**：body 提供 `cover_text` 时跳过本步直接采用（「以所选文字重新生成画面」路径，候选列表记为该单条）；否则文字调用：当前标题 + 冻结文案全文（不截断）+ brief → 5 候选 + 推荐条（校验：逐条 trim、≤30 码点截断记警告、空条丢弃、去重、recommended 钳位；调用失败或全部为空 → 回退项目标题截断至 30 码点并记警告，**不阻断**）。
  2. **匹配画面**：以选中文字（推荐条或 body 指定）+ 标题 + 文案全文 + brief + 参考约束（频道当前风格版本文本 或 项目草稿文本；空则无参考）→ 服务端解析 `byChild` → `provided` → 两版并行 dims 生成 → 校验 → 装配（维度文本 + 绘字段 + 锚点）→ 评委 → 整改 → 清理兜底 → 落 `cover_prompt_generations` 新版本（含 `prompt_body`/`cover_text_candidates`/`cover_text_default`/`cover_text_used`）并切指针。
- 换候选重装配：`prompt_current = prompt_body + 绘字段(所选候选)`，确定性、零 Provider 调用；页面提示"画面是按原文字设计的"，换候选后可另行点击「以所选文字重新生成画面」（即带 `cover_text` 的新一轮提示词生成，产生新版本）。
- 出图活动：取当前提示词版本 `prompt_current`（trim、非空、≤4000 码点）→ 出图 → QC（可解码、尺寸 2560x1440、格式、哈希）→ MinIO 归档 → 不可变版本 + 切指针（首成功自动当前）。
- 远端失败：LLM/出图 HTTP 层明确失败（429/5xx/超时）如实落 FAILED + 稳定失败码；手动功能不自动重提（传输层内重试与一次 fallback 除外），用户手动再次点击即新授权。LLM 类 401/403 既定不重试；400/404/409/410/422 首期手动模式一律如实报错（不类推 §15.14 放宽），待稳定后随 AUTO 并入再议。

## 7. API 合同（`api/cover.py`，均校验账号所有权）

频道侧（挂在 `/identity`）：
- `POST /identity/channels/{channel_id}/cover-ref-images`（multipart 上传，≤10MB，jpg/jpeg/png/webp，真实媒体校验）
- `GET /identity/channels/{channel_id}/cover-style`（草稿 + 当前风格版本 + 版本列表）
- `PUT /identity/channels/{channel_id}/cover-style/draft`（保存 dims/editedText/ref_image_id）
- `POST /identity/channels/{channel_id}/cover-style/analyze`（显式解析，202）
- `POST /identity/channels/{channel_id}/cover-style/profiles`（草稿固化为新版本并切当前）
- `POST /identity/channels/{channel_id}/cover-style/current`（切历史版本为当前）

项目侧（`/projects/{project_id}/cover`，冻结后开放，未冻结 404/409 稳定失败）：
- `POST .../ref-images`、`GET .../style`、`PUT .../style/draft`、`POST .../style/analyze`（项目试验工作区，同上语义）
- `POST .../style/promote`（把项目草稿固化为**频道**风格新版本；D13 的"保存为频道风格"主入口）
- `POST .../prompts`（生成提示词，202，body：`brief`、`style_override_text`（可空）、`style_source`（`channel`/`project`/`none`，默认有频道风格用频道、否则项目草稿）、`cover_text`（可空；提供时跳过文字调用直接采用，供「以所选文字重新生成画面」使用））
- `GET .../prompts`（版本列表 + 当前版本面板，含 `cover_text_candidates`/`cover_text_default`/`cover_text_used`）
- `PATCH .../prompts/current`（编辑 `prompt_current`、`reset:true` 恢复系统版、或 `cover_text_choice` 换候选重装配）
- `POST .../images`（出图，202，按当前面板文本）
- `GET .../images`（版本列表 + 当前）
- `POST .../images/current`（切换当前封面图）
- `GET .../images/{generation_id}/media`（受保护媒体，媒体 Cookie + Range，照 `api/media_router.py` 模式）

错误模型沿用 `public_status`/`message`/`allowed_actions` 与稳定业务错误码；封面动作不进入既有阶段门禁，`workflow_summary` 仅追加 `cover` 阶段事实（见 §9）。

## 8. 前端（`app.js` + `frontend/cover-view.js`）

- `STAGES` 追加 `{ key: "cover", label: "封面", note: "参考图与频道风格" }`（位于 `composition_export` 之后）；`renderActiveStage` 注册 `renderCoverStage`；纯视图逻辑入 `frontend/cover-view.js` 并配 `node:test` 测试。
- 封面页五区：① 风格来源（频道当前风格摘要/项目试验切换/无风格提示）；② 参考图与勾选（上传/替换/预览、8 类 36 子项勾选树（父级全选/半选）、「解析参考图」、解析文本编辑框（2000 码点、防抖保存）、「固定为频道风格」）；③ 生成输入（当前标题只读展示、补充需求 ≤500 码点、项目微调文本）；④ 提示词面板（「生成封面提示词」、封面文字候选选择器（5 条 + 推荐标记 + 换候选提示"画面是按原文字设计的"）、「以所选文字重新生成画面」按钮、系统版/编辑版切换、整段编辑与重置、警告与评委信息、版本历史）；⑤ 封面图（「生成封面图」、当前封面预览、版本历史与切换）。任务在途禁用对应按钮并轮询。
- 频道管理页（`renderAssets`）加「封面风格」区：当前风格文本预览、版本列表与切换、独立上传/解析入口（D13 的兼备入口）。
- 未冻结项目进入封面页显示锁定说明，不开放任何动作。

## 9. workflow_summary、导出 ZIP 与其他改动

- `identity_project/service.py` `_workflow_stages` 追加 `cover`（key 与前端一致）：`completed` = 存在当前封面图版本；永不构成 `blocking_reason`，不影响"视频生产完成"判定、列表徽章与 AUTO 运行。
- 导出（`api/media_router.py:219` 的 `export_factory` 链路）：ZIP 在 `composition.mp4` + `composition.srt` 之外，项目存在当前封面图时追加 `cover.<ext>`（原始字节，不转码不压缩）；无当前封面时产物与原合同逐字节一致。
- 频道序列化（`_channel`）追加 `has_cover_style` 布尔；项目序列化追加 `has_current_cover` 布尔（供列表/页签展示，不新增徽章）。
- 项目删除清理枚举追加封面相关精确对象键（项目作用域）。

## 10. 规则引擎移植清单

**照搬（机制代码，Python 重写）**：`REF_DIMENSIONS`（8 维 36 子项，key/中文/顺序）、勾选编码 `normalizeRefDims`/`normalizeRefSelection`（含"不自动折叠大类"语义）、`validateRefAnalysis`（空/"不明显"丢弃、字段限长 200）、`renderRefAnalysisText`、`parseRefTextByChild`（含 `dim_line_invalid` 拒绝与备注行语义）、`providedChildIds`/`spliceRefChildren`/`refRuleSuppressions`、装配函数骨架、两版编排 + 评委 + 定向整改 + 本地清理兜底、面板 system/editable 双文本语义。

**照搬（数据常量）**：`REF_CHILD_RULE_MAP`（36 项）、5 张违禁词表（风格/时代/特写/夜暗/下跪）、`POLICY_BLOCK` 平台政策段全文。

**调整（通用化重写，全文见 §11）**：解析/文字/画面/评委四套任务单；锚点集 9→2（主体完整/通用质量）+ 装配层确定性绘字段（文字来自独立文字调用）；评委 rubric 7 维通用版；激活矩阵——通用 base 下内容类审计（风格/时代/夜暗/特写/下跪）**默认不激活**（风格完全由参考图/频道风格决定，全局风格假设会自伤），锚点补入仅 2 个激活项；让步映射数据保留，仅对激活项生效（`composition::人物占比/视觉中心`、`camera::焦距/视角/景深` provided → 跳过主体完整锚点补入，沿用映射中的 `framing_anchor` 键；`composition::留白` 映射的 `no_text` 在通用版**无激活行为**——画面本就需要绘字）。

**移除**：scene 枚举与 scene/kneel/scene_dup 审计；景别体系（SHOTS/SHOT_DEFS/enforceShots/shot_diversity/relaxShots）；`CATEGORY_VISUALS` 题材词库与 category 参数；style modern/ancient 分支；trailerBeats；synopsis 截断注入（被文案全文取代）；contractVersion 三分支（只实现带参考拼接的八维度分支）；真人/日光/中国面孔/三层空间/高饱和/手部/无文字 7 个锚点。

**已知缺陷修复对照**（交接文档 §15）：

| 参考实现缺陷 | 本项目处理 |
| --- | --- |
| 装配 2000 UTF-16 截断丢已选字段 | 不截断（修复） |
| synopsis 截断 2000 注入 | 文案全文注入（修复） |
| 重试句硬编码"4 条" | 动态按 1 条生成（修复） |
| 任务单场景段落自相矛盾 | 通用版无该段（消除） |
| 缓存不比对图像 hash | 草稿记录 `ref_image_sha256`，比对 (sha256, dims)（修复） |
| "不明显"/整行缺失拒绝/备注行语义/评委不重评整改版/单版跳过 | 保持（有文档依据的行为） |

## 11. 通用版提示词全文（已确认基线）

### 11.1 视觉解析 system

```text
你是视频封面参考图解析员。只客观描述图片的画面形态（构图、站位、光影、色彩、材质、风格等），不评价好坏、不虚构内容、不猜测人物真实身份与关系、不得输出内容解读。输出严格 JSON，无解释、无 Markdown 围栏。
```

### 11.2 视觉解析 user（全选示例；部分勾选时只列选中维度/子项）

```text
解析这张视频封面参考图，仅按下列勾选维度输出中文描述（每个子项一句话，具体、可直接用于文生图提示词；图中无法确认的方面写"不明显"）：
- "subject"（主体）：人物身份、外貌、动作、表情、服装
- "spatial"（空间）：前景、中景、背景、环境、道具
- "composition"（构图）：人物占比、站位、画面比例、留白、视觉中心
- "camera"（镜头）：焦距、机位、视角、景深、对焦
- "lighting"（光影）：光源、方向、强弱、阴影
- "color"（色彩）：主色、辅色、饱和度、色彩情绪
- "material"（材质）：衣料、皮肤、金属、环境纹理
- "style"（风格）：艺术流派、时代感、设计语言、后期效果
【纯风格维度抽象化】（仅勾选 style/composition 时注入，2026-10-15 起）style、composition 两个纯风格维度只写跨场景可迁移的抽象短句或关键词（如「粗黑描边」「平涂色块」「简化圆形角色」「主体占比大」「顶部留白放标题」），禁止复述图中的具体场景元素（具体物体、地点、道具、人物动作与情节）；站位/视觉中心只写抽象方位与构图规律（如「主体居左」「标题居中顶部」），不写具体角色与物体。
若画面含有显著标题文字，请在构图/风格的相关子项中描述其样式、颜色、描边与位置（描述样式即可，不逐字抄录文字内容）。
输出 JSON：{ "<维度key>": { "<子项>": "<描述>" } }，只包含上述维度与子项。
```

### 11.3 封面文字生成任务单（串行第一步）

system：

```text
你是 YouTube 爆款封面文案专家，专为视频封面（缩略图）撰写大号标题文字。你的文字要在 1 秒内抓住眼球、制造点击冲动。

【铁律】
- 极短：优先 1–3 个词，且不超过 30 个字符（以项目语言为准）；小尺寸缩略图上一眼读完。
- 好奇缺口：放大标题的悬念或冲突，但不重复标题已用的词，也不剧透标题所问的答案；封面文字 + 标题合起来让观众非点不可。
- 补位：封面文字、画面、标题各管一件事、不重复。不写画面本身能表达的信息（画面画了原始人，就不写「原始人」），只写画面表达不了的话题或悬念。
- 用词与句尾：优先具体名词、数字，少用形容词；疑问式结尾用「？」或「？！」。
- 情绪优先：震惊、反差、紧迫、好奇，拒绝平淡陈述。
- 每条从下列句式模式中选不同的一种，避免雷同：否定式（NO JOBS / 别再…）、对比式（$0 → $10K / 上班 vs 躺平）、数字式（3 个错误 / 7 天）、提问式（就这？/ 为什么没人说）、宣告式（I QUIT / 到此为止）、身份/结局反转式。
- 不用 emoji、不堆砌标点、不说教、不出现品牌名或真人姓名。
- 内容必须与文案一致：可以强化情绪与悬念，不得虚构文案中没有的事实（数字、结果、身份），不得误导。
- 画面与文字均不得描绘或暗示以下违禁类别内容。
{POLICY_BLOCK}
输出严格 JSON，无解释、无 Markdown 围栏。
```

user：

```text
【标题】
<当前项目标题>

【用户补充需求】（仅 brief 非空注入，≤500 码点）
<brief>

【文案全文（事实来源）】
<冻结文案原文全文，不截断>

【项目语言】
<如 en-US>

【推导步骤】（2026-10-15 起）先完成以下推导再输出候选（推导过程不输出）：
1. 用一句话写出这个视频回答的核心问题。
2. 列出 3 组「观众默认认知 × 视频情境」反差对（格式：【常识A】×【情境B】，例：来月经要用卫生巾 × 原始人什么都没有）。
3. 对每组写出观众看到后脑子里冒出的那句疑问（通常是「等等，那……怎么办/真的吗？」），再把这句疑问压缩成封面文字。

请写 5 条候选封面文字，输出 JSON：
{
  "candidates": [
    { "text": "<封面文字>", "pattern": "<句式模式>" }
  ],
  "recommended": <int，最推荐的一条序号，从 1 开始>
}
务必正好 5 条，句式模式尽量互不相同。
```

校验：candidates 逐条 `text` trim、≤30 码点截断记警告、空条丢弃、逐字去重；`recommended` 钳位到有效范围，非法取 1；全部为空或调用失败 → 回退 `[项目标题截断至 30 码点]` 并记警告（不阻断画面生成）。拉丁文字候选另加两条确定性 lint（2026-10-15 起，只记警告不阻断）：>3 词记 `cover_text_over_word_budget`（含 CJK 候选跳过，长度仍走 30 码点规则）；与标题重复实义词 ≥2 个记 `cover_text_title_overlap`（去功能词后比较，标题为 CJK 时跳过；回退标题候选不参与重叠判定）。

### 11.4 画面提示词生成 system（串行第二步，两版并行）

```text
你是视频封面（YouTube 缩略图）的视觉提示词专家，为文生图模型撰写高点击率封面提示词。你要为给定的标题、封面文字与文案写 1 条封面提示词。

【输出结构·八维度】封面按下列八个维度撰写，每个子项一句具体、可执行、还原到物的中文描述（只描述画面本身，不写分析、不评价）：
1) subject 主体（人物身份/外貌/动作/表情/服装）：画面的核心主体与状态；画面无人物时填写主体对象，确实不适用的子项写"无"。
2) spatial 空间（前景/中景/背景/环境/道具）：交代地点、层次与氛围。
3) composition 构图（人物占比/站位/画面比例/留白/视觉中心）。
4) camera 镜头（焦距/机位/视角/景深/对焦）。
5) lighting 光影（光源/方向/强弱/阴影）。
6) color 色彩（主色/辅色/饱和度/色彩情绪）。
7) material 材质（衣料/皮肤/金属/环境纹理）。
8) style 风格（艺术流派/时代感/设计语言/后期效果）。

【封面文字呼应】任务单附有本张封面的封面文字（由系统逐字拼入成品，你不得撰写或改动）。文字下的什么钩子，画面就画兑现这个钩子的一幕：文字是对比，画面就画对比双方；文字是否定/宣告，画面就画对应的状态或瞬间；文字是提问/悬念，画面就画悬念揭晓前一刻。文字与画面必须互相强化、形成统一钩子。
【事实铁律】画面的题材、时代、场景、主体与美术风格只能依据【标题】【封面文字】【文案】【参考图既定描述】与【用户补充需求】确定；不得套用任何现成题材模板；文案未提供的视觉细节可做中性补全，但不得与文案矛盾；八个维度之间必须自洽（同一时代、同一场景逻辑、同一美术方向）。
【标题关联】封面必须兑现标题的钩子并与标题信息互补：标题传达什么主题、冲突或悬念，封面就画对应的一幕；标题已明说的信息不必重复图解，封面优先补足标题没说的画面信息。
【画面要求】主体突出、视觉焦点明确，缩略图小尺寸下仍能一眼辨认主体；主角有明确表情，且至少一个旁观者给出夸张反应（替观众先惊讶）；可放 1–2 个暗示答案的小道具，但不解释、不画结果；表情与情绪写可辨认的面部/肢体表现（如「瞪大双眼」「眉毛上扬」「双手抱头」），不写「惊讶」「震撼」等抽象情绪词；色彩子项写具体颜色与色值（如「亮黄 #FFD700」），避免「氛围感」「高级感」等空泛形容词；构图需为封面文字预留合适的显著区域（如顶部或主体旁留空），且右下角（约宽 20%、高 20% 的区域）为平台时长徽章留空，不放主体、封面文字与关键信息；但画面文字本身由系统统一追加，你无需在 dims 中撰写；除指定封面文字外，画面中不得出现其他文字、字母、数字、招牌或水印。
{refExemptDirective}
画面与 prompt 均不得描绘或暗示以下违禁类别内容。
{POLICY_BLOCK}

【格式】
- 描述用中文撰写；只写 dims 结构化字段，不要输出整段成文提示词。
- 封面文字绘字段、主体完整声明与质量锚点均由系统统一追加，你无需撰写。
- 描述要与标题和文案的情绪、题材一致。

输出严格 JSON，无解释、无 Markdown 围栏。
```

`{refExemptDirective}`（仅 provided 非空注入，机制与参考实现一致）：

```text
【参考图豁免】下列子项的画面描述由系统从用户参考图逐字采用，你在输出 JSON 中将这些字段一律填空字符串 ""，不撰写、不发挥、不改写：
- <维度标签·子项>（<dim::子项>）
这些子项不受本任务单任何铁律约束；你撰写的其余子项仍须遵守全部铁律，且画面应与这些既定描述协调一致。
```

### 11.5 画面提示词生成 user

```text
【参数】
- 封面比例：16:9
- 目标文生图模型：gpt-image-2

【标题】
<当前项目标题>

【封面文字（系统将要求图像模型逐字绘制，你不得撰写或改动；画面必须与其呼应）】
<cover_text>

【参考图既定描述（系统逐字采用）】（仅参考文本非空注入）
<refText>
（以上子项描述将由系统逐字拼入成品提示词，你不得撰写或改动；你撰写的其余内容应与其画面协调，画什么仍由标题与文案决定。）

【用户补充需求】（仅 brief 非空注入，≤500 码点）
<brief>
（以上是用户指定的封面画面约束，为首要画面约束；与文案冲突处以文案事实为准。内容政策不因该约束豁免。）

【文案全文（画面事实来源）】
<冻结文案原文全文，不截断>

请为这个标题写 1 条封面描述，输出 JSON：
{
  "ratio": "16:9",
  "covers": [
    {
      "rank": 1,
      "forTitle": "<回填标题，便于核对>",
      "dims": {
      "subject": { "人物身份": "<一句话，具体可执行>", "外貌": "...", "动作": "...", "表情": "...", "服装": "..." },
      "spatial": { "前景": "...", "中景": "...", "背景": "...", "环境": "...", "道具": "..." },
      "composition": { "人物占比": "...", "站位": "...", "画面比例": "...", "留白": "...", "视觉中心": "..." },
      "camera": { "焦距": "...", "机位": "...", "视角": "...", "景深": "...", "对焦": "..." },
      "lighting": { "光源": "...", "方向": "...", "强弱": "...", "阴影": "..." },
      "color": { "主色": "...", "辅色": "...", "饱和度": "...", "色彩情绪": "..." },
      "material": { "衣料": "...", "皮肤": "...", "金属": "...", "环境纹理": "..." },
      "style": { "艺术流派": "...", "时代感": "...", "设计语言": "...", "后期效果": "..." }
      }
    }
  ]
}

务必正好输出 1 条封面，含完整 dims 八个维度。标注为 "" 的子项由系统提供，原样留空即可。
```

（provided 子项在模板中占位 `""`，与豁免条款呼应。）

### 11.6 装配：绘字段与锚点

装配顺序：维度文本（有值才输出、全角冒号/分号、固定顺序）→ 锚点句（全角逗号相连）→ 以上为 `prompt_body`；最终提示词 = `prompt_body` + 绘字段；**不做总长截断**。

**绘字段（非锚点，恒定追加；`{cover_text}` 为文字调用推荐条或用户指定值，经校验：trim、≤30 码点截断记警告、空/缺失回退项目标题截断至 30 码点记警告）**

```text
画面中加入标题文字「{cover_text}」：大号醒目字体，粗体描边或与背景高对比，拼写正确、清晰可读，位于画面显著位置且不遮挡主体；除该文字外，画面中不得出现其他任何文字、字母、数字、招牌或水印
```

**锚点（通用版 2 个，按序检测补入）**

| 规则键 | 锚点原文 | 检测正则（已含则跳过） |
| --- | --- | --- |
| `framing_anchor` | `画面主体完整入画，关键元素不被画面任何边缘裁切` | `/完整入画|不被.*裁切/` |
| （恒定质量，不让步） | `主体突出，轮廓与边缘清晰，缩略图小尺寸下主体仍可一眼辨认` | `/一眼辨认/` |

`no_text` 规则键在通用版无激活行为（画面本就需要绘字；映射数据保留）。

### 11.7 评委 system 与 rubric（7 维，总分 140）

```text
你是视频封面（YouTube 缩略图）的严苛评审（评委），只负责打分与点评，不重写内容。按固定 rubric 逐版打分，分数要拉开差距、直言缺陷。
打分前先模拟一个正在刷手机的普通观众只看 0.5 秒：读到了什么字？脑子里冒出什么问题？想不想点进去？以这三问作为各维打分的锚点。

【rubric（每维 0-20 分）】
- visual_impact（0-20 分）：构图冲击：主体突出、视觉焦点明确、有吸引力
- thumbnail_clarity（0-20 分）：缩略图可读性：小尺寸下主体与场景一眼可辨，不糊不乱
- title_match（0-20 分）：标题关联：画面兑现标题的钩子并与其信息互补
- text_visual_match（0-20 分）：文字画面呼应：封面文字与画面内容互相强化、形成统一钩子
- script_fidelity（0-20 分）：文案贴合：画面元素与文案一致，无与文案矛盾的虚构
- style_consistency（0-20 分）：风格一致：与参考图既定描述/用户补充需求协调统一
- technical_quality（0-20 分）：技术质量：细节丰富、无畸形糊块、封面文字拼写正确且清晰可读

输出严格 JSON，无解释文字、无 Markdown 围栏。
```

评委 user：

```text
【任务】评审 2 版视频封面文生图提示词（每版 1 条，须兑现标题「<标题>」的钩子且与其信息互补）。逐版打分并选出最优版。

【各版内容】
[{"version":1,"covers":[{"rank":1,"forTitle":"<标题>","prompt":"<第A版完整最终提示词>"}]},{"version":2,"covers":[{"rank":1,"forTitle":"<标题>","prompt":"<第B版完整最终提示词>"}]}]

输出 JSON：
{
  "verdicts": [
    { "version": 1, "scores": { "visual_impact": <int 0-20>, "thumbnail_clarity": <int 0-20>, "title_match": <int 0-20>, "text_visual_match": <int 0-20>, "script_fidelity": <int 0-20>, "style_consistency": <int 0-20>, "technical_quality": <int 0-20> }, "comment": "<一句话点评，中文>" }
  ],
  "best": <int，最优版 version 号>,
  "advice": "<对最优版仍存在的问题给出整改意见，中文；无问题则空串>"
}
```

评委规整沿用参考实现：分数钳 0-20、本地求和（总分 140）、best 非法取最高总分、<80%（112 分）低分警告不阻断、评委失败本地择优（条数→审计问题数）。

## 12. 测试策略（全部假 Provider，零真实调用）

- 领域单测（边界：正常/最小/最大/非法）：勾选规范化、解析校验/渲染/回读（含整行缺失拒绝、备注行、重复子项覆盖）、provided/splice/让步并集、装配（无截断 + 绘字段逐字嵌入 + 2 锚点检测补入 + 换候选重装配恒等）、文字候选校验（正常/空回退标题/超长截断/去重/recommended 钳位/警告记录）、`validateCovers`（cap=1、provided 忽略模型值、维度缺失判 needsRetry）、评委规整（7 维）、配置校验。
- 编排测试（fake 适配器）：串行链路顺序（文字→画面）、body 指定 cover_text 跳过文字调用、文字失败回退不阻断、两版并行单版失败跳过、评委失败本地择优、整改只换更优版。
- 仓储合同测试：SQL 与内存行为一致（风格草稿/版本/指针、提示词版本/面板编辑/换候选、图片版本/当前切换、项目删除清理枚举）。
- API/工作流测试：所有权隔离、幂等键冲突、重复点击零重复提交、冻结前 404/409、面板编辑/重置/换候选、ZIP 含/不含封面两分支、删除后频道风格存活。
- 前端 `node --test`：勾选树（全选/半选/编码）、候选选择器状态、面板状态、版本列表渲染。
- 回归保障：既有后端测试套件与前端 `npm run check` 全绿方可合并；导出 ZIP 在无封面项目上与原合同逐字节一致有专门测试。
- 验证命令：后端 `python -m pytest`、`python -m ruff check src tests`、`python -m mypy src`；根目录 `npm run check`。

## 13. 实施顺序

1. 迁移 0030 + 领域 `domain/cover.py` + 仓储（SQL/内存）+ 配置组；
2. 三个适配器（含 fake）+ 应用服务 + outbox/dispatcher + Temporal 工作流 + worker 装配；
3. `api/cover.py` + workflow_summary + 频道序列化；
4. 前端第十阶段 + 频道页封面风格区 + api-client；
5. 导出 ZIP + 项目删除清理；
6. 全量测试/静态检查/构建 + 差异审查（确认既有触点均为追加式、无行为改变）；
7. 联调（§14 额度内）+ 修复；
8. 确认无活动任务后重建 Docker `web`/`worker`（不清共享数据），健康检查与 OpenAPI 合同检查。

## 14. 联调与真实验收（已批额度）

| 通道 | 额度 | 用途 |
| --- | --- | --- |
| 视觉解析 | ≤5 次 | 解析 1–2 次、勾选变化重解析 1 次、缓存命中验证（零调用）、fallback（可选） |
| 文本生成+评委 | ≤15 次 | 2–3 轮提示词生成（每轮 ≤5 次：1 文字 + 2 画面 + 1 评委 + ≤1 整改） |
| 出图 | ≤8 张 | 2–3 次出图 + QC/归档/当前切换/ZIP 内容/绘字效果验证 |

逐次记录于授权文件；费用未知记未知。正式真实路径验收（含 LAN 端到端）按 §15.11 逐次报批。联调前由用户提供三个通道密钥（写入 `.env`）。

## 15. 决策确认记录（2026-09-20，全部闭环）

1. 封面文字：独立文字调用依标题 + 文案全文 + 爆款规则生成 5 条项目语言候选（模型推荐条，≤30 码点校验、空/缺失回退项目标题截断、超长截断，均记警告；调用失败回退标题不阻断）；
2. 串行链路：选中文字作为画面生成输入，画面任务单含文字呼应铁律，标题、文字、画面形成统一钩子；
3. 评委 rubric 含第 7 维 `text_visual_match`（总分 140、80% 低分线 112）；
4. 面板：候选选择器（换候选确定性重装配并提示"画面是按原文字设计的"）+「以所选文字重新生成画面」（带 `cover_text` 的新一轮生成）+ 整段自由编辑与重置；
5. 文字语言跟随项目语言；绘字段模板按 §11.6；参考图中的文字样式可解析进频道风格、内容不逐字采用；
6. 风格完全由参考图/频道风格决定；内容类审计（含风格违禁词表）默认不激活，常量保留；
7. 绘字拼写不做 OCR 自动校验（评委软约束 + 人工重生成兜底）；
8. 失败策略：首期手动模式 LLM 4xx 一律如实报错（不类推 §15.14 放宽）；
9. 数据模型（§3）与 API 合同（§7，含项目草稿固化为频道风格的 promote 入口）按本文件执行。
