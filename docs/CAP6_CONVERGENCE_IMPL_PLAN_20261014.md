# 实施计划：组级镜头上限 6 ＋ 确定性自动收敛（cap6-converge）

> 编制日期：2026-10-14。状态：**待批准（未动任何代码）**。
> 全部结论基于当天逐行代码核查与部署库只读数据；行号为核查时实测。
> 目标：① 组级镜头上限 5→6；② 复映射阶段"确定性自动收敛"（>6 自动相邻合并至 ≤6）。
> 约束：不引入 bug；与既有决策/验收文档同步；可回滚（见 §5 窗口约束）。

## 0. 事实基线（决定方案形态的关键核查结论）

- **F1 两类播种路径**：①主路径——物化持久化时对 `final_revision != None` 的候选**直接落草稿＋指针＋血缘**（`adapters/persistence/storyboards/project_director_repository.py:1213-1286`，含 revision_number 计算、`DirectorPointerRow` 指向、`DirectorRevisionLineageRow`）；②回退路径——管线 `seed_drafts` 对无 final_revision 的候选经 `edit_draft` 播种（`application/storyboard_batch.py:154-204`）。
- **F2 本库实证（韩项目）**：133 候选＝103 主路径 + 29 回退 + 1 失败（组 85）；草稿 132 条、全 `revision_number=1`、10 APPROVED。→ **收敛放在候选构建阶段即可让全部收敛结果走主路径（全自动）**。
- **F3 上限 5 的分布点（9 处，逐行核实）**：
  1. `domain/storyboards.py:277`（DirectorPlanRevision 1–5）
  2. `domain/storyboards.py:484`（StoryboardGeneration.shots_snapshot 1–5）
  3. `application/project_director.py:1555`（切片 >5 → `group_shot_limit_exceeded`）
  4. `application/project_director.py:1583`（final_revision 条件 `<=5`）
  5. `application/storyboard_prompt.py:25-26`（网格表无 `6` 键，`grid_layout_for(6)` 直接 ValueError）
  6. `api/storyboards.py:32`（`ShotInput.order le=5`）
  7. `api/storyboards.py:49`（`DraftInput.shots max_length=5`）
  8. `app.js:3159 / 3222 / 3248 / 4590`（4 处前端：表单消息、动态禁用、点击守卫、初始禁用）
  9. `application/director_generation.py:443`（休眠链路，库内 0 行、无生产调用方——见 D6）
- **F4 无需数据库迁移**：候选/修订以 JSON 存镜头与血缘（`_shot_json` 已含语义字段，`project_director_repository.py:1526-1536`）；多父血缘仅是**域校验**放宽（`domain/project_director.py:352-358` 目前要求"每候选恰一来源"）。
- **F5 网格几何零变化**：5 格=2×3＋第 6 格留黑（`storyboard_prompt.py:25-26,85-89`）；6 格=同画布全用（`empty_panels` 由容量自动计算）。
- **F6 回滚窗口**：域对象在**读取即校验**——任何 6 镜草稿落库后，旧代码读取将 fail-closed。→ 见 §5。

## 1. 执行前需你决策的事项（D1–D9，集中列出；★=推荐）

| # | 事项 | 选项与影响 |
|---|---|---|
| D1 | **决策文本修订批准** | A★ 按拟稿修订 4 处：§5.11（1 至 5→1 至 6）、§6.11（补"6 格时 2×3 全用、不设留黑"）、§5.7（"超过 5 个镜头"→"超过组级上限 6"；"不得自动截断、静默合并"→"**允许带审计的确定性自动合并**；仍禁止自动截断与无审计处理"）、§20.3（结构冲突拆为"可确定收敛子类=自动 / 其余=人工"）。B 只批部分——若 §5.7/§20.3 不修订，收敛功能不合法、仅能做 cap=6。 |
| D2 | **合并对选择规则** | A★ 逐次合并"（subject_action_outcome+description）合计字符最短"的相邻对，平局取 order 较大者（代价最小、确定性强）；B 固定从尾部合并（实现更简、可能并入信息量大的镜头）。规则固化进 `PLAN_CONVERGENCE_VERSION`。 |
| D3 | **合并字段配方** | scene/shot_size/camera_viewpoint/narrative_function/四元语义取**前镜**；action/description=前镜文本＋分隔符＋后镜文本。分隔符：A★ 单空格（语言中立）；B 中文分号"；"；C 自定义。 |
| D4 | **多父血缘** | A★ 域校验放宽为"每候选≥1来源、来源对唯一"（合并镜头保留双亲证据；无迁移）；B 维持 1:1 仅记前镜（需另加审计字段，不推荐）。 |
| D5 | **算法版本** | A★ `controlled-semantic-lineage-remap-v3`→`v4`（行为已变，必须升版保审计真实）；旧项目保留 v3 不强制重算（韩项目候选维持 v3，仅页面候选 tone 残留，属 cosmetic）。B 不升版（审计失真，不推荐）。 |
| D6 | **休眠链路** `director_generation.py:443` | A★ 不改，加注释标注休眠遗留（库内 0 行、无调用方）；B 同步改 6（一致性好但触达无使用代码）。 |
| D7 | **6 格生图真实验证** | A★ 部署前 1 次真实 grsai 最小验证（6 格提示词效果）——按 §15.11 需你逐次授权真实调用与费用；B 不单测、待首个 6 镜组自然进入生成时验证（零额外费用、风险后移）。 |
| D8 | **是否并入"候选播种路径语义回退"修复**（评审 P0-2） | A★ 不并入（独立小项，本次范围保持最小）；B 并入（增加服务端语义回填工作量）。 |
| D9 | **收敛范围** | A★ 仅"超过组上限"；B 同时纳入 0 镜/覆盖缺失（当前 0 例、规则未定义，扩大决策面）。 |

## 2. 分步实施计划

### S1 决策与验收文档修订
- **目标**：决策生效、可追溯；验收与设计文档同步。
- **改动范围**：`docs/planning/USER_DECISIONS.md`（新增 §24，按 D1 拟稿）；`docs/planning/FINAL_ACCEPTANCE_CRITERIA.md:96`（1 至 5→1 至 6）；`docs/G85_PREVENTION_DESIGN_20261014.md`（§0bis/§9 补记本次方向确认）。
- **关键思路**：§24 明确：上限 6；网格 6 格全用；§5.7/§20.3 修订措辞；收敛规则受 `PLAN_CONVERGENCE_VERSION` 约束；审计=血缘+版本+时间。
- **依赖**：D1 批准。
- **验证**：人工复核 diff。

### S2 域层上限与单一常量
- **目标**：域允许 1–6；上限单一定义源。
- **改动范围**：`backend/src/tubeflow_test/domain/storyboards.py`：新增 `DIRECTOR_GROUP_SHOT_LIMIT = 6`；L277、L484 改用常量；消息文本 "1 to 5"→"1 to 6"。
- **关键思路**：常量放域层（两处域对象所在地）；`project_director`/`api` 由此 import，杜绝散落数字。
- **依赖**：S1（决策后落地）。
- **验证**：两处域对象边界单测（6 通过 / 7 拒 / 0 拒）。

### S3 收敛模块（新增，纯函数）
- **目标**：独立可测的确定性合并器。
- **改动范围**：新增 `backend/src/tubeflow_test/application/plan_convergence.py`；`domain/project_director.py:352-358` 按 D4-A 放宽。
- **关键思路**：
  - `PLAN_CONVERGENCE_VERSION = "adjacent-merge-v1"`；
  - `converge_group_shots(shots, links, source_shot_links, *, limit) -> (shots, links, source_shot_links)`：while 数量>limit，按 D2 规则选相邻对合并；
  - 合并语义：保留**前镜候选 ID**（后镜 ID 退场，其全部来源重定向到保留 ID——多父血缘由此自然形成）；字段按 D3；order 重排 1..n 连续；links 去重（(shot,sentence) 对唯一）保持稳定序；
  - 输入 ≤limit 时恒等返回（避免无谓重写）。
- **依赖**：D2/D3/D4；S2 常量。
- **验证**：纯函数单测——7→6、8→6、9→6、恒等、确定性（同输入同输出）、链接并集、renumber、多父、平局规则。

### S4 复映射接入＋版本升 v4
- **目标**：`_candidate` 在 final_revision 判定前收敛；全部收敛结果走主路径（F1/F2）。
- **改动范围**：`application/project_director.py`：
  - L59 版本常量 → `controlled-semantic-lineage-remap-v4`；
  - L1539-1547 之后插入收敛调用（`if len(new_shots) > LIMIT: …`）；
  - L1555 `> 5`→`> LIMIT`（**防御分支保留**：收敛后理论不可达，防配置错误）；
  - L1583 `<= 5`→`<= LIMIT`；
  - L1571-1574 质量冲突映射对合并后重复候选 ID 去重（`dict.fromkeys`）；
  - 注释更新（为何在此处、版本语义）。
- **关键思路**：收敛只改变"候选是否合法"；`missing/ambiguous/conflicts` 均在收敛后基于合并内容重算，天然继承 §20 的审批语义。
- **依赖**：S3。
- **验证**：内存仓储级用例（见 S8）。

### S5 生图网格声明（**必须与 S2 同版发布**）
- **目标**：6 格可用、无留黑指令。
- **改动范围**：`application/storyboard_prompt.py:25-26` 新增 `6: (2,3)`（16:9）与 `6: (3,2)`（9:16）。
- **关键思路**：容量=格数 → `empty_panels=()` 自动成立；引号留黑指令（L114-116）天然只在有空格时生成。
- **依赖**：无单独依赖，但**同版约束**：若只上 S2 不上 S5，6 镜组在生成时 `grid_layout_for(6)` 抛 ValueError。
- **验证**：`test_storyboard_prompt.py` 更新（见 S8）。

### S6 API 模型（与 S2 同版）
- **目标**：接口边界允许 6 镜。
- **改动范围**：`api/storyboards.py:32`（`order le=5`→`le=6`/常量）；`:49`（`max_length`）；import 常量。
- **关键思路**：`ShotInput.order` 是隐蔽点（漏改则 6 镜 PUT 422）。
- **依赖**：S2；同版。
- **验证**：API 单测（6 镜 PUT 通过 / 7 镜 422）。

### S7 前端（与 S2 同版）
- **目标**：页面可编辑/保存 6 镜。
- **改动范围**：`app.js` 四处：L3159（消息"1 至 6 个分镜"）、L3222（`>= 5`→`>= 6`）、L3248（点击守卫）、L4590（初始禁用）；引入 `const MAX_DIRECTOR_SHOTS = 6`。
- **依赖**：S6；同版。
- **验证**：`node --test frontend/*.test.js` 全量 + `scripts/build.mjs`；人工核对禁用边界。

### S8 测试与门禁（"不引入 bug"的执行保障）
- **更新已知用例**：
  - `backend/tests/unit/test_project_director.py:2001-2026`：由"6 镜必 REVIEW"改为"**7 镜→收敛为 6**、无冲突、final_revision 非空、多父血缘断言"；新增"6 镜直通（无冲突）"用例。
  - `backend/tests/unit/test_storyboard_prompt.py`：L56/L70 增 6 例；L83 rejection 用例 `(6,"16:9")`→`(7,"16:9")`；新增 6 格提示词文案断言（含 Panel 6、无留黑句）。
  - 核对 v3 硬编码的语义（仅作 fixture 值则可保留）：`test_project_director.py:2090/2470/2506`、`tests/integration/test_project_director_current_materialization.py:263`、`test_storyboard_director_lineage_edit.py`。
- **新增用例**：S2/S3 边界；收敛端到端（内存仓储 7 镜）；多父血缘持久化往返（SQL 仓储）。
- **全量回归**：backend 全量（基线 **1711 passed / 5 skipped**）；frontend（基线 **182 passed**）＋构建；改动文件 ruff/mypy 无新增问题。
- **依赖**：S2–S7 完成。

### S9 部署与在线验证（按 `docs/DEPLOYMENT.md` §5 四步）
- **目标**：上线 `cap6-converge` 并完成在线验证。
- **步骤**：构建镜像（tag `cap6-converge-20261014`）→ save/gzip/scp/load（两机 md5 一致）→ compose 三处改钉（两机一致）→ 前置检查（无 RUNNING 管道、备份 compose）→ `up -d` → 容器 healthy＋`/health/live` 200。
- **在线验证**：
  1. 韩项目由用户点「恢复自动执行」→ 断言组 85 **6 镜草稿落库**、管道推进（该组候选为 v3 存量，conflict 标记残留属已知 cosmetic）；
  2. 6 镜组进入生图时核对提示词含 Panel 6 且无留黑句（若 D7-A 已前置验证可跳过）；
  3. SQL 巡检：候选 `source_shot_links_json` 多父条目计数=收敛触发次数（当前存量应为 0；新项目按需核对）。
- **依赖**：S8 全绿；（真实生成调用按 §15.11 逐次报批）。
- **回滚**：重钉旧镜像（见 S10 窗口约束）。

### S10 回滚窗口与不可逆约束（部署前必须知晓）
- 无数据库迁移，回滚=重钉旧镜像；**但**：域在读取时校验（F6）——自部署起、**一旦首个 6 镜草稿落库（韩项目恢复即触发），旧代码读取 6 镜修订将 fail-closed**，届时回滚不可行（只能前向修复）。
- **处置**：部署与韩项目恢复之间保留人工确认点；如需保留回滚余地，可在恢复前评估（恢复动作由你控制）。

### S11 本次范围外（另行立项，避免范围蔓延）
模型/提示词变更门禁、近上限监控产品化、候选播种语义回退（D8-B）、0 镜/覆盖类自动收敛（D9-B）、分组隔离放行。

## 3. 依赖关系摘要

D1–D9 → S1 → S2 →（S3 → S4）；**S2、S5、S6、S7 必须同版**；S8 ←（S2–S7）→ S9 → S10 生效。

## 4. 风险与注意事项（基于实码，逐条给置信度）

1. **同版发布约束**：S2/S5/S6/S7 缺一则 6 镜在"保存（422）/生成（ValueError）"环节失败。（高）
2. `ShotInput.order le=5`（api L32）为最隐蔽遗漏点。（高）
3. v4 版本字符串不破坏前端标签正则（`step07-view.js:87`）与 `algorithmVersionLabel`。（已核实，高）
4. `group_shot_limit_exceeded` 收敛后成防御分支——保留代码并保纯函数测试兜底。（高）
5. **回滚窗口**（S10）与韩项目 cosmetic 残留需在部署前确认。（高）
6. 测试影响面：已知更新点已列（S8）；残余由全量回归兜底。（中高）
7. 6 格生图实际效果：未经真实调用验证，由 D7 控制。（待验证）

## 5. 交付物清单（批准后执行）

- 代码：`domain/storyboards.py`、`domain/project_director.py`、新增 `application/plan_convergence.py`、`application/project_director.py`、`application/storyboard_prompt.py`、`api/storyboards.py`、`app.js`。
- 文档：USER_DECISIONS §24、FINAL_ACCEPTANCE_CRITERIA、G85_PREVENTION_DESIGN 补注、部署记录（DEPLOYMENT.md §17）。
- 测试：上述新增/更新用例 + 全量回归报告。
