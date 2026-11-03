# 封面图生成 v5 优化实施计划（待审批）

日期：2026-09-21。状态：待用户审批（审批前不动代码）。
基线：`COVER_GENERATION_PLAN_20260920.md` v4（已实施并部署）。本文件只覆盖 v5 增量：角色绑定、参考图预览、提示词版本切换、默认勾选与维度标注、脏状态与低分引导、风格版本备注名与按版本生成。垫图（方案 D）为单独报批的实测项，不在本轮代码范围。

依据：2026-09-20/21 用户多轮确认（优化清单三档的第一档+第二档全部、决策点按推荐值）。

## 1. 角色绑定（方案 C v2，本档核心）

### 1.1 数据模型（迁移 `0032_cover_character_cards`）

新表 **`cover_character_cards`**（频道资产，可变卡；生成血缘按代记录文本，故不需不可变版本链）：
`id`、`owner_id`、`channel_id`、`character_image_id`（引用既有 `character_images.id`，频道内唯一 uq(channel_id, character_image_id)）、`identity_text`（≤100 码点）、`appearance_text`（≤500 码点）、`source`（`manual`/`analysis`）、`created_at`、`updated_at`。

`cover_style_profiles` 追加 `label`（≤60 码点，默认空串，见 §5）。
`cover_prompt_generations` 追加 `character_cards_used`（JSONB：生成时绑定的卡快照 [{card_id, character_image_id, identity_text, appearance_text}]，血缘）。

项目删除：`cover_character_cards` 为频道资产，不随项目删除（`channel_id` RESTRICT，`owner_id` 普通 FK，照 cover_ref_images 频道行模式）；`cover_prompt_generations.character_cards_used` 随项目 CASCADE。

### 1.2 角色卡解析任务单（复用视觉解析通道，每角色图显式 1 次）

system：

```text
你是 IP 角色的核心特征提取员。只提取该角色跨场景不变的识别特征（身份、头身比例、面部特征、标志性元素），不描述服装、姿势、道具、场景等情境属性，不提取画风/渲染风格（渲染风格由频道风格档案统一决定），不评价、不虚构。输出严格 JSON，无解释、无 Markdown 围栏。
```

user（附图，detail=high）：

```text
提取这张 IP 角色图片的核心识别特征，输出 JSON：
{ "identity": "<一句话：角色是谁/是什么，≤50 字>", "appearance": "<一段：头身比例、面部特征、标志性元素等跨场景不变的外形特征，不写画风/渲染风格，不写服装/姿势/道具/场景，≤300 字>" }
```

校验：`validate_character_card(raw)`——identity/appearance 均 str→trim，identity ≤100、appearance ≤500 码点截断记警告，identity 空→失败码 `cover_character_card_empty`（回退手写）。解析写入/覆盖卡（source=analysis），用户可再编辑（source 改 manual）。

### 1.3 画面生成接入（拼接优先级与屏蔽规则）

**优先级（角色绑定时）**：角色卡 > AI 撰写（受角色卡约束） > 参考图（仅限非人物子项）。
- `subject::人物身份` ← 首角色卡 `identity_text` 逐字（系统提供，LLM 留空）；`subject::外貌` ← 首角色卡 `appearance_text` 逐字。
- **屏蔽规则**：绑定生效时，参考图 provided 中 `subject::` 全部 5 子项一律不进入确定性拼接（仅不进拼接；参考文本仍注入 user 消息作上下文）；非人物子项 provided 行为不变。不绑定时行为与 v4 完全一致。
- `subject::服装/动作/表情` 由 LLM 按【角色绑定】指令结合参考构图与文案情境撰写。
- 第 2..N 个角色卡：进【出场角色】块作配角描述（LLM 整合），不进确定性子项。

**system 追加段（绑定生效时注入）**：

```text
【角色绑定】画面中的人物角色一律为本频道 IP 角色，其人物身份与外貌子项由系统逐字提供（你留空 ""）；参考图既定描述中出现的任何人物外形特征一律忽略，仅可参考其动作、表情、站位与构图；角色的服装、姿势与道具由你按标题/文案/封面文字的情境重新设计，与画面协调；角色的头身比例、面部特征与标志性元素必须保持不变；角色的渲染风格服从风格维度的既定描述，角色卡外貌文本中的画风表述不单独决定画面美术风格。（2026-10-15 修订：渲染风格裁决）
```

**user 追加块（绑定生效时注入，置于【封面文字】之后）**：

```text
【出场角色（身份与外貌由系统逐字采用；服装/姿势/动作/表情由你按情境设计）】
主角：<identity_text>；<appearance_text>
（有第 2..N 个时各一行）配角：<identity_text>；<appearance_text>
```

**评委**：user 消息在绑定时附同样【出场角色】块（`style_consistency`/`text_visual_match` 自然覆盖角色一致性），rubric 不变。

### 1.4 API 与页面

- `POST /identity/channels/{cid}/cover-character-cards`（建/改卡：character_image_id + 手填文本）、`GET .../cover-character-cards`（卡列表+各卡角色图）、`POST .../cover-character-cards/{card_id}/analyze`（202，显式解析）、`DELETE` 不做（资产无 TTL，卡可编辑清空）。
- `POST /projects/{pid}/cover/prompts` body 追加 `character_card_ids`（有序 UUID 数组，可空；**默认 = 项目快照角色图 ∩ 已有卡，按快照顺序**；显式传 `[]` = 不绑定）。
- 封面页新增 ⑥「角色绑定」区（置于 ③ 生成输入之后）：频道角色图清单 + 各卡状态（有卡/无卡）+ 解析/编辑入口 + 生成出场选择与顺序（默认按上条）；提示词版本详情展示 `character_cards_used`。
- 角色图预览：复用 §2 的参考图媒体端点（character_images 已有自己的受保护媒体链，直接复用，不新建）。

## 2. 参考图预览端点

- `GET /identity/channels/{cid}/cover-ref-images/{image_id}/media`、`GET /projects/{pid}/cover/ref-images/{image_id}/media`：受保护媒体（媒体 Cookie/Bearer + 属主/作用域/版本绑定，照 `api/cover.py` 封面图 media 模式；小图亦支持 Range）。
- 前端：勾选编辑器中的参考图列表项渲染 `<img>`（频道页与封面页共用组件处接入）。

## 3. 提示词版本切换

- `POST /projects/{pid}/cover/prompts/current` body `{version_id}`（候选须属该项目且 SUCCEEDED；仓储 `set_current_prompt_generation` 已存在，仅补端点与应用方法）。
- 前端：提示词版本历史行加「切换」按钮（与封面图版本同模式）。

## 4. 前端默认与标注

- 默认勾选集合改为 `["style", "composition"]`（原全选 8 维为误导性默认，E2E 实测全选=全量复刻）。
- 纯风格维度解析抽象化（2026-10-15）：勾选 style/composition 时，解析 user 追加【纯风格维度抽象化】段——只写跨场景可迁移的抽象短句/关键词，禁止复述具体场景元素（物体、地点、道具、人物动作与情节）；站位/视觉中心只写抽象方位与构图规律，不写具体角色与物体。
- 勾选树维度类型标注（常量随 `COVER_REF_DIMENSIONS` 维护）：
  - 纯风格：`style`、`composition`——标签"跨场景迁移优先"
  - 半场景：`subject`、`material`——标签"携带部分场景信息"
  - 场景绑定：`spatial`、`lighting`、`color`、`camera`——标签"与参考场景强绑定，跨场景慎用"
  - 标注文案（随 v5 实施）："勾选决定参考图哪些描述逐字进入成品。跨场景使用优先勾『纯风格』；『场景绑定』维度会把参考图的光色与场景一起带来。"
- 脏状态：当前勾选 ≠ 草稿 `analysis_dims` 时显示"勾选已变化，需重新解析"；点击「解析参考图」且现有 `edited_text` 非空时，先确认"重新解析将覆盖当前文本"再执行。
- 低分引导：`cover_judge_low_score` 警告旁附动作提示："画面与标题不匹配时：①只勾纯风格维度重新生成；②风格与场景冲突时：编辑风格文本为中性描述，或换用其他风格版本。"

## 5. 风格版本备注名与按版本生成

- `cover_style_profiles.label`（§1.1 迁移）：`POST .../cover-style/profiles` 接受可选 `label`；GET 风格接口返回各版本 label；频道页/封面页版本列表展示。
- `POST /projects/{pid}/cover/prompts` body 追加 `style_profile_id`（可空；仅 `style_source=channel` 时有效，须属本频道；提供时使用该版本文本而非频道当前指针文本，血缘照记）。

## 6. 测试计划（全部假 Provider）

- 领域：`validate_character_card`（正常/空/超长/非 str）、角色绑定块注入有无、屏蔽规则（绑定时参考 subject 不拼接、非人物子项照常）、拼接优先级（卡 > AI > 参考）、多角色块、评委块注入、不绑定零回归（v4 行为逐字一致）。
- 仓储合同（SQL/内存）：卡 CRUD、uq(channel_id, character_image_id)、label 往返、`character_cards_used` 血缘。
- API/服务：卡解析 202 与失败码、prompts 默认绑定（快照∩卡、顺序）、显式 `[]` 不绑定、`style_profile_id` 归属校验与使用、prompts 版本切换（非法候选拒绝）、ref 图 media 两作用域与跨账号。
- 前端：默认勾选、维度标注渲染、脏状态与覆盖确认、低分提示、角色区状态机、版本切换按钮。
- 回归：后端 `tests/unit` 全量 + ruff + mypy；前端 `npm run check`。

## 7. 实施顺序

1. 迁移 0032 + 领域增量（角色卡校验/绑定块/屏蔽与优先级）+ 仓储；
2. 应用服务（卡 CRUD/解析编排、prompts 绑定与按版本生成、版本切换）+ API + 媒体端点；
3. 前端五处（⑥角色区、预览、切换、默认与标注、脏状态与低分引导、label 录入与展示）；
4. 全量检查 + 差异审查；
5. 部署：构建新镜像 → 钉定 web/worker → `docker compose build migrate` → `up -d`（复用上次教训：migrate 必须显式重建）→ 真实 PG 应用 0032 → 健康/OpenAPI 验证；
6. v5 联调（见 §8）。

## 8. v5 联调计划（在已批额度内，逐次记录）

| 动作 | 预计消耗 | 验证点 |
| --- | --- | --- |
| 角色卡解析 ×1–2（频道现有角色图） | 解析 ≤2（余 3/5） | 核心特征提取质量、服装/姿势排除 |
| 绑定生成提示词 ×1 轮 | 文本 ≤4（余 5/15） | subject 两子项逐字=卡内容、参考 subject 零残留、服装按文案情境、评委分 |
| 出图 ×1 | 出图 ≤1（余 6/8） | IP 一致性 + 情境服装 + 绘字 |
| 提示词版本切换/预览端点/label | 0 | 纯 API |
| 垫图实测（方案 D，**执行前单独向你报批**） | 出图 1–2 | tt-image-2 `images` 参数支持与否 |

## 9. 微决策记录（已按推荐值定，可否决）

1. 角色卡为**可变**频道资产（不建版本链）：生成血缘已按代记录卡文本快照，可追溯；
2. 卡管理入口只放封面页 ⑥（频道页不重复建设，只展示风格区现有内容）；
3. 卡字段上限 identity ≤100 / appearance ≤500 码点（截断记警告）；
4. `character_card_ids` 默认值 = 项目快照角色图∩有卡（按快照顺序）；快照角色无卡时不绑定（AI 自由撰写 subject，行为同 v4）；
5. 维度类型三档标签归并：`camera` 归入"场景绑定"（机位/视角构图与参考场景同构）；
6. 角色卡不做 DELETE（资产无 TTL，可编辑清空文本）；
7. 垫图实测不在本轮代码范围；实测成功且你确认采用后，再立 v6 增量（新出图 kind 分支 + 失败路径测试）。

## 10. 无阻塞疑问

第一/二档的决策点已全部按推荐值确认；§9 为可自行否决的微决策。批准后：先把本文件决策同步进 `USER_DECISIONS.md` §18 与验收 §20，再按 §7 实施。
