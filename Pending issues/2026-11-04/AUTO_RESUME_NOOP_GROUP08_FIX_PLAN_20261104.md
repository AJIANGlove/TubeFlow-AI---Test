# AUTO「恢复」空转（第 8 组构图重复）— 根因与修复方案（交接实施）

| 项 | 内容 |
| --- | --- |
| 日期 | 2026-11-04 初稿；2026-11-05 按 `main@58c537d` 复核更新（变化见第 11 节） |
| 状态 | **方案已由用户确认**（2026-11-04 用户回复「按推荐进行」），**代码尚未实施**。第 8 组已于 2026-11-04 由其他会话按用户指令以数据修补方式解卡（第 5.0 节）。本文档只读分析产出，未改任何代码、数据、容器或配置，未触发任何外部 AI 调用。 |
| 交接对象 | 负责实施的后续会话 |
| 上游取证 | `Pending issues/2026-11-04/AUTO_RESUME_NOOP_GROUP08_20261104.md` 及同目录 `evidence/autoresume_group08/` |
| 项目 | `2c291aba-085d-499d-bd32-b3a4d75f252f`（ko-KR / 16:9，107 组 / 496 句，AUTO） |
| 代码基线 | 仓库 `main@58c537d`。本文行号均指该提交。与初稿基线 `5ac9374` 相比，本文引用的后端与前端文件中只有 `application/storyboard_prompt.py`（新增 §46.11 v2 格内规则，L158；`build_grid_sheet_prompt` 仍在 L73）与 `video_generation/panel_images.py`（crop-grid-v2 修订，`check_storyboard_panel_layout` 现在 L667）有变化，其余逐字未变；迁移头仍为 `0046`。 |
| 部署镜像 | 取证时为 `sha256:8278db3e…`（构建自 `0e832f6`）。当前为 `sha256:30ffc3c3…`（`v2-panel-rule-20261105`，构建自 `c7baf5b`），且 `TUBEFLOW_STORYBOARD_SINGLE_PANEL_NEW_PROJECTS=true`（2026-11-04T13:41:55Z 起），见 `docs/DEPLOYMENT.md` §36–§39。导演草稿校验、AUTO 播种/恢复相关代码在 `0e832f6`、`c7baf5b`、`58c537d` 之间内容未变。 |

文中标注约定：**【事实】** 已用代码或证据文件核实；**【推断】** 由事实推出但未直接取证；**【建议】** 实施细节建议，非用户决策，实施会话可在不改变已确认行为的前提下调整。

---

## 0. 实施会话必读与约束

1. 按 `AGENTS.md`：先完整阅读 `docs/planning/USER_DECISIONS.md`、`docs/planning/FINAL_ACCEPTANCE_CRITERIA.md`、`docs/planning/AGENT_AUTHORIZATION.md`。
2. 禁止编造：未运行、被跳过或失败的检查不得写成通过；ruff/mypy 在 main 上已有基线错误（`docs/DEPLOYMENT.md` §39 记为 15 / 12 条），新代码不得新增。
3. **不得改动**：前置导演提示词与单元请求哈希、项目级物化代码与 `algorithm_version`、既有故事板提示词对**不含回切的组**的输出。前两者会触发 §15.13 整轮重跑或重新物化，后者会改变既有 canonical 哈希。
4. 自动化测试一律用假 Provider；任何真实 Provider 调用（含为校准生成样例）须按 §15.11 逐次报批。
5. 部署按 USER_DECISIONS §21 与 `docs/DEPLOYMENT.md` 执行，测试门禁通过为前提，部署前确认无在途任务。
6. 本文第 8 节列出的事项**未获决定，不得实施**。

---

## 1. 问题摘要

- 现象：项目暂停在「导演方案校验未通过」（`director_validation_failed`，分组 08）。点「恢复自动执行」提示成功，但几毫秒后回到同一暂停；已连续点击多次，授权轮次到 7。
- 一句话根因：第 8 组导演方案的第 1 镜与第 6 镜「场景＋景别＋机位」完全相同，违反组内非相邻构图不重复的领域不变量，AUTO 播种必然失败并暂停。这次暂停本身符合当前规则（§20.3 / §24.4：非相邻重复不自动放行）。真正的缺陷是：「恢复」在问题未修复时确定性空转，却向用户返回成功。
- 复发：2026-11-05 项目 `23c59183` 第 36 组出现同一问题（同样是第 1、6 镜构图相同），同样以数据修补方式临时解卡（F10）。代码不修，同类数据还会继续出现。

---

## 2. 已核实的事实

| # | 事实 | 依据 |
| --- | --- | --- |
| F1 | 取证时运行行：`PAUSED / director_validation_failed / authorization_round=7`，payload `group_sequences=[8]`（解卡前已到 round 8，见 F9） | `evidence/.../db_export.txt` |
| F2 | 107 个候选：66 NOT_REQUIRED、41 REQUIRED。41 个 REQUIRED 全部带 `source_preplan_quality_conflict` 且 `final_revision_id` 为空；其中 40 组各有 1 份草稿，**只有第 8 组 0 份草稿** | `candidates_summary.csv`（本次复算） |
| F3 | 第 8 组候选 `e634a051-7e83-44b8-85ac-838b990ef52b`，分组 `eca42932-c8aa-58fe-9330-3a32db38c554`，物化 `a78f3daf-8964-43ce-8293-35d512591334`（状态 REVIEW_REQUIRED），前置导演修订 `5b7808d6-e9bb-45e0-9863-fbd700b6f271` | `group08_candidate.json`、`db_export.txt` |
| F4 | 第 1 镜与第 6 镜构图同为 `빙수점 - 계산대 장부 / CLOSE_UP / OVERHEAD`，组内位置差 5；受控语义差 3 维（事件、主体焦点、时间关系不同），事件判定键不同 | 附录 A；附录 B 回放输出 |
| F5 | 用仓库产品代码回放：原样播种抛 `director plan repeats an event or composition across the group`，与线上一致；只把第 6 镜机位改为 `EYE_LEVEL` 后走第 07 步保存路径，草稿合法、`NOT_REQUIRED`、`may_apply=True` | 附录 B |
| F6 | 取证时本项目尚无故事板（0/107），网格版式未钉，当时部署镜像不含 v2。之后 v2 已部署，单格开关自 2026-11-04T13:41:55Z 起为 `true`；按 §46.2 钉版规则，开关开启后才生成首张故事板的项目钉 `grid-v2`（本项目与 `23c59183` 的实际钉版未直接核实） | `docs/DEPLOYMENT.md` §36–§39 |
| F7 | crop-grid-v2 经 PR #5（USER_DECISIONS §47）修订后 Gate 1 重跑通过 98.3577%（539/548），Gate 2 通过；第 3–4 关通过；v2 测试项目 `484746c4` 13 组全部 grid-v2，AUTO COMPLETED。已知未结问题：`247462a4` 暗色故事板被 crop-grid-v2 误拒（提交 `c7861c6`、`9781354`） | `singlepanel_test/gate1_rerun/REPORT.md`、`docs/DEPLOYMENT.md` §36、§39 |
| F8 | 仓库内目前没有任何图像相似度检查；后端依赖已有 Pillow（`pyproject.toml`），`video_generation/panel_images.py` 已导入 `Image, ImageChops`（L12） | 代码检索 |
| F9 | 第 8 组已于 2026-11-04 14:14–14:16Z 解卡：按用户指令「你直接帮我改，然后恢复」，把候选行 `shots_json[5].camera_viewpoint` 由 `OVERHEAD` 改为 `EYE_LEVEL`（UPDATE 1 行）并调用 `resume_paused`（round 8→9）；随后第 8 组草稿生成并应用，107 组故事板生成启动。遗留：候选行与物化快照不再严格对应 | 提交 `df1c5c7`；`evidence/autoresume_group08/fix_applied/README.md` |
| F10 | 同型第二例：项目 `23c59183` 第 36 组第 1、6 镜构图同为 `popina - shared table / MEDIUM / EYE_LEVEL`，位置差 5；2026-11-05 05:17–05:19Z 以同样方式解卡（第 6 镜机位改 `HIGH_ANGLE`）。用产品领域代码复算：两镜受控语义差 2 维、事件键不同，满足 R1–R3 | 提交 `6bca6c9`；`Pending issues/2026-11-05/AUTO_RESUME_NOOP_GROUP36_20261105.md` |

---

## 3. 根因链（全部【事实】，除注明外）

1. **前置导演已记录问题，但只记录不拦截。**
   - `application/project_director.py:1692-1731` `_finalize()` 把全部单元镜头**按全项目顺序拼接**后调用 `_quality_conflicts()`（L1715），记录 `adjacent_meaningful_change_insufficient`、`unusually_many_shots`、`repeated_non_adjacent_event`、`repeated_non_adjacent_composition`（L2202-2265）。
   - 解析期只拒收相邻多样性不足（§20.2）；非相邻重复只写入冲突列表，运行照常成功。
   - 【推断】第 8 组冲突 #1 绑定候选第 1、6 镜，原始码应为 `repeated_non_adjacent_composition`：这两镜各对应一个源镜头，事件键不同，非相邻，排除其余三种码后只剩这一种。核实 SQL 见第 9 节。

2. **物化阶段把信号藏掉。**
   - `_candidate()`（L1826-1842）把源冲突统一改名为 `source_preplan_quality_conflict`，原始码丢失。
   - L1848-1856：存在阻断型冲突（`blocking`）时跳过 `DirectorPlanRevision` 构造，因此 L1875 的 `group_diversity_invalid` 不会产生。

3. **AUTO 播种在领域构造器处被拦，随后暂停（这一步符合规则）。**
   - `application/storyboard_batch.py:154-205` `seed_drafts()` 不看候选的 `review_state` 与冲突列表，直接走 `edit_draft()` → `prepare_draft()` → `DirectorPlanRevision(...)`。所以 40 个 REQUIRED 候选能正常播种（F2），候选冲突并不是 AUTO 的闸口。
   - 唯一闸口是 `domain/storyboards.py:350-370`：非相邻（位置差 >1）的事件键或构图键相同即抛 `ValueError`。
   - `auto_pipeline/service.py:285-327`：播种失败的组没有草稿 → 列入 invalid → `_pause(director_validation_failed)`。

4. **「恢复」确定性空转却返回成功（缺陷 1）。**
   - `auto_pipeline/service.py:152-156` `resume()`：`resume_paused()` 置 RUNNING、轮次 +1 → 同步 `kick()` → `advance()` 在同一请求内重新暂停 → 但返回的是 **kick 之前**的 `run`（RUNNING）。
   - `identity_project/router.py:673-691` 原样返回 `state=RUNNING`。
   - `app.js:5660-5663` 对任何 2xx 弹「已恢复自动执行；相关对象预算已按规则重置。」（`runManualAction`，`app.js:5313-5322`）。

5. **暂停详情跨次残留（缺陷 2）。**
   - `auto_pipeline/domain.py:140-164` `paused()` 以 `{**self.payload, **details}` 合并，`resumed()` 也不清理暂停详情键。所以 06:38 前置导演暂停留下的 `failure_code=director_output_adjacent_diversity_insufficient` 仍在 payload（F1 所在 `db_export.txt` 第 27 行）。
   - `identity_project/service.py:736-750` 白名单会把它暴露给 `workflow_summary`。本原因码的页面文案只读 `group_sequences` 与 `failure_messages`（`frontend/auto-progress.js:157-172`），所以这次界面没显示错，但其他原因码可能显示陈旧失败码。

---

## 4. 用户已确认的决策（2026-11-04「按推荐进行」）

### 4.1 「恢复」如实反馈（缺陷修复，不改产品语义）

- **A1** `resume()` 返回推进之后的真实运行状态。
- **A2** 前端恢复按钮按返回的 `state` 判断：仍为 `PAUSED` 时提示失败，并说明问题未修复，不弹成功。
- **A3** 每次进入暂停前，先清除上一次暂停留下的详情键，暂停详情不跨次残留。

### 4.2 组内非相邻构图「回切」有条件放行

用户原话：「我允许这种剪辑手法，但是前提是能够保证最终生成的视频画面能够吸引人观看」，随后确认按推荐值执行。

定义：同一组内，两个**不相邻**镜头的构图键（归一化后的场景、景别、机位）完全相同，称为一对「回切」。

| 编号 | 条件（全部满足才放行，否则按现规则拦截） | 推荐理由（摘要） |
| --- | --- | --- |
| R1 内容差异 | 两镜受控语义至少差 2 维（`changes_from ≥ 2`），且事件判定键不同（`event_repeat_key` 不等） | 沿用 §5.12 已确认的「至少改变 2 维」，不新造阈值 |
| R2 频率 | 每组最多 1 对回切；同一构图在组内最多出现 2 次 | 一组最多 6 镜，1 对已占 1/3 画面；本项目 107 组只有第 8 组出现组内非相邻构图重复（其余 106 组草稿都过了同一校验）；`23c59183` 79 组也只有第 36 组 |
| R3 间隔 | 两镜之间至少隔 2 个镜头（组内位置差 ≥ 3） | 一组最长 15 s；只隔 1 镜就回切，看起来像来回跳（剪辑经验判断，无实测） |
| R4 版式 | 只在 `grid-v2` 放开；`grid-v1` 不放开；提交故事板前再校验一次 | 运行时只对 v2 故事板做逐格裁切（`_check_panel_layout` 对 v1 直接返回），v1 无从验证两格画得像不像，按「无法检查就不放行」。注：推荐时引用的「v1 裁切通过率 78.8%」已被 Gate 1 重跑（98.36%）削弱，见第 11 节；开关开启后新项目均钉 v2，R4 实际只影响存量 v1 项目 |
| R5 开关 | 整个回切功能由开关控制，**默认关闭** | 相似度阈值要用真实 v2 故事板校准并经用户看图确认后才开启 |
| R6 画面质检 | 回切两格在故事板阶段做相似度检查，过于相似按可重试输出波动走现有故障转移链（每组至多 14 次，§25/§27/§46.5）；生图提示词给回切格加一句「同一地点，但必须表现本格的新内容」 | 规则只管方案，画面由生图模型产出，必须在产物上验证 |
| R7 视频层 | 本次不加回切专用视频检查 | 等首批真实数据出来再定 |

条件的解释（本文档对已确认规则的保守解读，不扩展范围）：

- R1 要求两镜都有完整受控语义。语义缺失（`UNSPECIFIED`）时无法判断「差 2 维」，视为不满足。【建议】实现上不得走 `changes_from` 的文本回退分支：回切对三项构图字段必然相同，回退分支最多只能数到 2，会把「动作和功能文本不同」误当成满足条件。
- 有完整受控语义时，「差 ≥2 维」已蕴含「事件判定键不同」；R1 后半句保留为显式断言。
- 事件重复规则（§22.6，事件判定键相同即拦截）**不变**，回切不豁免事件重复。
- 适用范围：只用于 AUTO 项目，与 §20.3「系统审批」同属 AUTO 机制；MANUAL 项目规则不变。如需扩展到 MANUAL，须由用户另行决定。

### 4.3 本项目第 8 组的处置

确认时的推荐：不等 v2，由用户在第 07 步把第 6 镜机位从 `OVERHEAD` 改为 `EYE_LEVEL` 并保存。理由：当时部署只有 v1，回切规则上线后也不会放行第 8 组；且开关在阈值校准前保持关闭。

实际结果：已由其他会话按用户指令以数据修补方式完成（F9），取值与推荐一致。见第 5.0 节。

---

## 5. 实施计划

阶段 0 已完成；阶段 1、2 互不依赖；阶段 3 原先依赖的 v2 部署已满足（F7），可以开始；阶段 4 依赖阶段 3 与用户看图确认。

### 5.0 阶段 0：第 8 组解卡（已完成）

- 2026-11-04 已由其他会话按用户指令完成（F9）：改候选行第 6 镜机位，再调用 `resume_paused`。第 36 组（`23c59183`）也用同样方式处理（F10）。
- 实施会话**不需要**再处理这两组，也不要回滚它们的候选行。

代码修复上线前如再出现同型暂停，产品内的正规路径是第 07 步人工改稿，不需要改数据库。该路径不改候选行，候选与物化快照保持一致。【事实】路径可行性：

- 物化状态非 SUCCEEDED 时，公开状态为 `RECOVERY_REQUIRED` 并带 `EDIT_FINAL_DIRECTOR`（`api/project_director.py:175-186`）。
- 第 07 步对「无草稿、无已应用方案」的组允许用候选镜头预填并保存（`app.js:5007-5017` 的 `canSeedCandidateDraft`；保存按钮 L5030）。
- 保存走 `PUT` 草稿接口（`api/storyboards.py:196-245`）。草稿 `may_apply` 时，调用 `resume_if_paused_for(director_validation_failed)`（L231-239），按原因码守卫自动续跑（§20.4）。
- 第 8 组在该路径上的回放结果见 F5 与附录 B。

步骤：第 07 步 → 违规分组 → 编辑器（已用候选预填）→ 只改后一镜的机位、景别或场景中的一项，使其与前一镜不同 → 点「保存最终导演」。

注意：

- 保存成功后，**同一请求内**就会续跑：`advance` 本跳无需再播种，直接对所有尚无成功故事板、可生成的分组逐组应用草稿并创建故事板生成意图（`auto_pipeline/service.py:328-357`、`storyboard_batch.py:278-316`），随后进入真实生图调用（原 §15 授权范围内）。所以这一步由用户本人执行。实施会话如被要求代为操作，须先按 `AGENT_AUTHORIZATION.md` 取得明确授权。
- 修好之前不要再点「恢复」：数据不会坏，但每点一次消耗一轮授权。

### 5.1 阶段 1：A1–A3（可单独实施与部署）

**A1 后端**：`auto_pipeline/service.py:152-156`

```python
def resume(self, owner_id: UUID, project_id: UUID) -> AutoPipelineRun:
    run = self._runs.resume_paused(owner_id, project_id, now=self._clock())
    try:
        after = self.advance(owner_id, project_id)
    except Exception:
        logger.exception("auto pipeline kick failed for project %s", project_id)
        return run
    return after or run
```

- 异常处理与原 `kick()`（L190-195）一致：只记日志，不破坏主流程。
- 接口返回结构不变（`state` / `reason_code` / `authorization_round`），只是值如实。

**A2 前端**：`app.js:5660-5663`

- 恢复处理函数读取响应：`state === "PAUSED"` 时先刷新项目事实，再抛错。`runManualAction` 会用红色 toast 显示错误（L5321-5322）。
- 【建议】文案：「恢复后立即再次暂停（原因码 {reason_code}）：问题尚未修复，本次恢复已消耗 1 轮授权。请按暂停说明先修正（导演方案问题在第 07 步修正对应分组）后再恢复。」
- 其他状态保持原成功提示。
- `frontend/auto-progress.test.js:439` 断言源码包含 `api.autoPipelineResume(state.project.id)`，改动时保留该调用文本或同步修订测试。

**A3 运行域**：`auto_pipeline/domain.py:140-164` `paused()`

- 合并新 details 前，先从 payload 移除以下 7 个暂停详情键：`group_sequences`、`failure_code`、`error_code`、`sequences`、`failure_messages`、`groups`、`waited_seconds`。
- 【事实】这 7 个键是 `auto_pipeline/service.py` 各暂停点写入的全部详情键（L178-180、240、255、318-321、339-340、365、378-381、420-424、449-450、470、721、731）。后端除 `identity_project/service.py:736-750` 白名单外，没有代码从 payload 读取它们。
- 其余键一律保留，包括阶段书签与计时、视频 job/容量书签、`director_failed_run_id`（`_DIRECTOR_HANDLED_KEY`，L62，`_handle_director_failure` 依赖它）。
- 只影响此后发生的暂停；当前已暂停项目的 payload 不改写。

### 5.2 阶段 2：回切规则（开关默认关闭；关闭时行为与现状逐字一致）

**领域**（`domain/storyboards.py`）

- 新值对象【建议命名】`CompositionRecutApproval(previous_shot_id, next_shot_id, reason)`，与 `ContinuousObservationApproval`（L204-213）同构。
- `DirectorPlanRevision`（L310-394）新增字段 `composition_recut_approvals: tuple[...] = ()`，放在末尾并带默认值，以减少调用点改动。
- 构造校验改为：
  - 事件键非相邻重复：维持原样，直接拒绝（错误信息不变）。
  - 构图键非相邻重复：每一对都必须被一条 approval 精确绑定；否则抛与现在相同的 `ValueError`。
  - 每条 approval 必须绑定一对真实存在的非相邻构图重复，且满足 R1–R3：两镜受控语义完整、`changes_from ≥ 2`、事件键不同、位置差 ≥ 3、approval 总数 ≤ 1、该构图组内出现次数 ≤ 2。否则抛 `ValueError`。
  - 把资格判断做成**一个**领域函数，供应用层推导与构造校验共用，避免两处口径不一致（§22.6 先例）。
- 领域层不读开关和版式。是否允许由应用层决定，并以 approval 的形式随修订持久化。这样修订从数据库读回时，校验结果与开关当前值无关。

**应用层**（`application/storyboards.py` `DirectorPlanService.prepare_draft`，L482-524）

- 播种（`seed_drafts`）、第 07 步保存（PUT → `edit_draft`）、AUTO 修复（`repair_ambiguous_drafts`，`storyboard_batch.py:206-252`，内部调用 `edit_draft`）都经过 `prepare_draft`，在这里统一推导 approval。
- 推导前提：项目为 AUTO、回切开关开启、项目有效网格为 `grid-v2`。有效网格判定与 `StoryboardGenerationService._grid_layout_version`（L1173-1178）一致：已钉版用钉版值；未钉版时由 `storyboard_single_panel_new_projects` 决定。前提不满足时不推导，行为与现状一致。
- 理由文本如实写明是系统策略，例如：「AUTO 全链路系统策略：组内非相邻构图回切按 §48 条件放行，非人工批准。」
- PUT 合同不新增字段，由服务端推导（与 §20.1 语义继承同一思路）。
- `apply_draft` → 仓储 `apply_revision` 必须把 approval 一并复制到已应用修订。

**持久化**

- 【建议】迁移 `0047`：`storyboard_director_revisions` 新增 `composition_recut_approvals_json`（JSON，非空，默认 `[]`）。当前迁移头为 `0046`；该表现有 `continuous_observation_approvals_json` 在 `adapters/persistence/storyboards/models.py:51`。
- 旧行读出为空元组，校验结果与现状相同；无回填。
- 同步更新：仓储行↔领域映射、`apply_revision` 复制、内存适配器（`adapters/in_memory_storyboards.py`）。
- `DirectorPlanRevision(` 在非测试代码中有 4 处构造：`application/project_director.py`（物化，**不改其逻辑**，使用默认空值）、`application/storyboards.py`、`adapters/persistence/storyboards/repository.py`、`adapters/persistence/storyboards/project_director_repository.py`。

**提交故事板前再校验（R4 第二道闸）**

- 在 `StoryboardGenerationService.create`（L575 起）与批量 `_generate_one`（`storyboard_batch.py:278-316`）中：已应用或待应用修订带回切 approval，而当前有效网格不是 v2 或开关已关闭时，fail-closed，不提交。
- AUTO 批量路径映射为 `needs_review`（参照 L302）。`auto_pipeline/service.py:358-366` 会以 `director_validation_failed` 暂停并列出分组；此时开关或版式已不允许，用户在第 07 步保存时不会再推导 approval，必须改构图。

**生图提示词**（`application/storyboard_prompt.py:73` `build_grid_sheet_prompt`）

- 仅当组内有回切 approval 时，给回切格（后一镜）追加一句英文说明，含义为：本格回到第 m 格的同一地点与机位，地点保持可辨认，但必须表现本格的新动作与新物件，两格明显不同。
- 无回切的组提示词逐字不变，既有 canonical 哈希不变（需黄金值测试）。
- v2 提示词已有 §46.11 的「每格为单一连续镜头」规则（`storyboard_prompt.py:158` 起）。回切说明只描述本格内容，不得与该规则冲突（不得要求格内对比或分屏）。

**页面**（【建议】）

- 第 07 步对含系统回切放行的组做标注，与连续观察系统审批的展示方式一致。
- 新失败码补中文文案。

**开关**（【建议】命名）

- 环境变量 `TUBEFLOW_DIRECTOR_COMPOSITION_RECUT` → `config.py` 字段 `director_composition_recut: bool = False`，参照 L91 `storyboard_single_panel_new_projects` 的写法。

### 5.3 阶段 3：回切格相似度质检（R6）

v2 已部署、单格开关已开启，已有全部为 grid-v2 且 AUTO COMPLETED 的测试项目（如 `484746c4`，F7），阈值校准所需的真实 v2 故事板已经存在。代码可先用合成图片做单元测试。

相似度检查建立在逐格裁切之上：`247462a4` 暗色故事板被 crop-grid-v2 误拒的问题（F7）尚未结案，修复该问题不属于本方案，但其结论会影响回切检查能否正常执行。

- 位置：`StoryboardGenerationService` 现有 v2 裁切演练 `_check_panel_layout`（L1180-1197，调用点 L933、L983）之后。仅当生成 `grid_layout_version == grid-v2` 且该组有回切 approval 时执行，其余情况不执行。
- 回切对快照：【建议】在 `create` 时把回切对的镜头序号写入生成记录的输入快照（不可变）。只有存在回切时才写，不影响其他组的 canonical 哈希。
- 计算：对两格裁切图做相似度。端口与适配器分离：应用层定义端口，参照 `StoryboardPanelLayoutCheck`（L348-353）；实现放在 `video_generation/panel_images.py`，只用已有的 Pillow，不新增依赖。【建议】算法带版本号（如 `recut-similarity-v1`）并写入失败证据。
- 判定：两格过于相似时，【建议】以新稳定码 `storyboard_recut_panels_too_similar` 走 `_auto_resubmit`，参照 `_retry_panel_layout_failure`（L1199-1215）：同模型 2 次、逐路由、至多 14 次；耗尽后按原语义终态，AUTO 照常暂停。
- **阈值（已确认的原则）**：
  1. 前提：v2 已部署并有真实 v2 故事板（已满足，F7）。
  2. 逐格裁切后，计算同组**不同镜头**格子对的距离分布作为基线。
  3. 阈值取**明显低于基线**的距离值：正常的不同镜头永远不触发，只拦「近乎相同」。
  4. 把候选阈值与对应样例图交用户看图确认，确认后把算法版本与数值写入 USER_DECISIONS，再进入阶段 4。
  - 【建议】同组相邻、构图相同的格子对（连续观察）最接近「同机位不同内容」的回切形态，可纳入样本供用户判断阈值宽严。
  - 校准为离线计算，不产生 Provider 调用；如需为校准额外生成样例，须按 §15.11 报批。
- 在阈值确认之前，开关保持关闭；代码中不得写入未经确认的阈值作为生效默认值。

### 5.4 阶段 4：开启开关

- 用户看图确认阈值后，在部署机 `.env` 开启开关，并记录到 USER_DECISIONS。
- 首批回切成片交用户观看。如果用户认为不够吸引人：关闭开关即恢复现规则，已生成内容不受影响；或收紧 R1–R3（需用户决定）。
- 按验收 §37 第 583 行，把组级非相邻构图重复暂停的发生率与回切放行次数纳入统计。

---

## 6. 测试清单（全部用假 Provider）

**阶段 1**

- A1：违规组 → `resume()` 返回 `PAUSED` / `director_validation_failed`，授权轮次 +1；正常情况返回推进后的状态；`advance` 抛异常时返回恢复后的运行并记日志。
- 既有 `backend/tests/unit/test_auto_pipeline.py:1086-1105` `test_resume_requires_paused_and_bumps_round` 断言的是返回值。若因语义变化失败，把「轮次 +1、原因清空」改为对仓储行断言，不得删除用例。
- A2：前端测试：恢复响应为 `PAUSED` 时显示错误、不显示成功；其他状态显示原成功文案。
- A3：前置导演失败暂停（含 `failure_code`）→ 恢复 → 播种失败暂停（`group_sequences`、`failure_messages`）→ payload 不再含旧 `failure_code`；`director_failed_run_id` 与视频书签保留。

**第 8 组回归**（用 `evidence/autoresume_group08/group08_candidate.json` 作夹具）

- 原样播种失败并暂停。
- 第 6 镜改 `EYE_LEVEL` 后经 PUT 保存：`NOT_REQUIRED`、`may_apply=True`、`resume_if_paused_for` 生效。
- 开关开启 + v2 时：原样播种通过并带 1 条回切 approval。
- 开关开启 + v1 时：仍失败。
- 第 36 组（`Pending issues/2026-11-05/evidence/autoresume_group36/group36_candidate.before.json`）作第二个夹具：开关开启 + v2 时放行（语义差 2 维，恰在 R1 边界），开关关闭时失败。

**阶段 2 领域矩阵**

- 合格回切带 approval → 通过。
- 合格回切不带 approval → 原 `ValueError`。
- 以下各项均拒绝：语义只差 1 维；语义缺失；位置差 2（只隔 1 镜）；两对回切；同构图出现 3 次；approval 绑定相邻对或不存在的重复对。
- 事件键非相邻重复：有无 approval 都拒绝。

**阶段 2 应用与持久化**

- 推导条件：开关关闭 → 不推导、行为与现状逐字一致；开关开启 + 钉 v1 → 不推导；开关开启 + 未钉版 + 单格开关开启 → 推导；MANUAL 项目 → 不推导。
- 推导路径：播种、PUT、AUTO 修复三条路径都推导。
- 持久化：`apply_draft` 复制 approval；持久化往返一致；旧行读出为空。
- 提交前再校验：v1 或开关关闭时 fail-closed；AUTO 下表现为 `needs_review` 暂停并列出分组。

**阶段 2 提示词**

- 无回切组的故事板提示词与 canonical 哈希等于改动前黄金值。
- 有回切组只追加约定说明句。

**阶段 3**

- 合成「近乎相同」两格 → 新失败码 → 进入故障转移重提。
- 明显不同两格 → 通过。
- v1 或无回切 → 不执行。
- 抽样或裁切异常时 fail-closed，不放行。

**静态与回归**

- ruff、mypy 不新增错误；前端 node 测试；后端单元测试全量。
- 重跑附录 B 回放脚本。

---

## 7. 规划文档同步（实施完成时一并提交）

编号说明：初稿写的是 §47 / §43，这两个编号已被 crop-grid-v2 修订占用（USER_DECISIONS §47、验收 §43），所以顺延为 §48 / §44。实施时如又有新节，按当时最后一节顺延。

### 7.1 `USER_DECISIONS.md` 新增 §48（草案，内容仅限已确认事项）

> ## 48. 2026-11-04 组内非相邻构图回切（有条件放行）与「恢复」如实反馈
>
> 状态：已确认（2026-11-04，用户指令「按推荐进行」；依据 `Pending issues/2026-11-04/AUTO_RESUME_NOOP_GROUP08_FIX_PLAN_20261104.md`）。本节修订 §5.12、§20.3、§24.4 对组内非相邻**构图**重复的适用；事件重复规则（§22.6）不变。
>
> 1. 「恢复自动执行」返回推进后的真实状态；恢复后立即再次暂停时，页面如实提示失败与原因码，不显示成功。
> 2. 每次进入暂停前清除上一次暂停的详情键，暂停详情不跨次残留。
> 3. 回切：同组两个不相邻镜头的场景、景别、机位归一化后相同。AUTO 项目在同时满足以下条件时由系统记录放行（随不可变修订留痕，理由如实写为系统策略）：两镜受控语义完整且至少差 2 维、事件判定键不同；两镜之间至少隔 2 个镜头；每组至多 1 对、同一构图组内至多出现 2 次；项目网格为 `grid-v2`；回切开关开启。任一条件不满足仍按原规则拦截并暂停。提交故事板前再次校验版式与开关，不满足即 fail-closed。MANUAL 项目不变。
> 4. 回切开关默认关闭。以真实 v2 故事板同组不同镜头的格子距离为基线，把阈值定在明显低于基线处，经用户看图确认后写入本节再开启。
> 5. 含回切的组：故事板提示词为回切格追加「同一地点，但必须表现本格的新内容」说明；回切两格在故事板阶段做相似度检查，过于相似按可重试输出波动进入既有故障转移链（至多 14 次）。不含回切的组提示词与哈希逐字不变。视频层本次不加回切检查。
> 6. 不在本节范围：组级定向 AI 修复、前置导演单元内非相邻重复拒收、物化阶段无条件结构校验、恢复按钮门禁、无草稿组从候选继承语义（见交接文档第 8 节）。

第 8 组与第 36 组的数据修补是临时解卡，按其执行记录不构成产品决策，不写入 §48。

### 7.2 `FINAL_ACCEPTANCE_CRITERIA.md` 新增 §44（草案）

- [ ] 恢复后在同一请求内再次暂停时，接口返回 `PAUSED` 与真实原因码，页面不显示成功提示。
- [ ] 暂停详情不跨次残留：新暂停的 `workflow_summary` 只含本次暂停写入的详情键。
- [ ] 开关关闭时，导演草稿校验、播种、提示词与 canonical 哈希与改动前逐字一致。
- [ ] 开关开启且项目为 v2 时，满足 §48.3 全部条件的回切对自动放行并留痕；不满足任一条件的仍拦截并暂停。
- [ ] v1 项目、MANUAL 项目不放行回切；提交故事板前版式或开关不再允许时 fail-closed。
- [ ] 回切两格过于相似时进入故障转移链重提，耗尽后按原语义终态；非回切组不执行该检查。
- [ ] 阈值经用户看图确认后才开启开关（未验证）。

---

## 8. 未获决定、本次不得实施的事项

| 事项 | 状态 |
| --- | --- |
| 组级定向 AI 修复（只改回切后一镜的取景字段，每组 3 次） | 需要新的 AI 调用授权，用户未决定 |
| B1 前置导演单元内非相邻构图/事件重复拒收 | 未决定 |
| B2 物化阶段无条件结构校验并保留原始冲突码 | 未决定；涉及 `algorithm_version` 与重新物化风险 |
| B3 `director_validation_failed` 且仍有违规组时禁用恢复或返回 409 不消耗轮次 | 未决定；会改动 §20.4「按钮语义保留」 |
| B4 无草稿组 PUT 时从来源候选继承受控语义 | 未决定 |
| 回切规则扩展到 MANUAL 项目 | 未决定 |
| 视频层回切画面检查 | 已确认本次不做 |

---

## 9. 复核时对先前分析的更正（【事实】）

1. **前置导演冲突记录不随回切规则同步修改。**
   - 先前推荐中有一条「前置导演冲突记录按同一判定」。复核发现前提不成立：`_quality_conflicts()` 在 `_finalize()` 中对**全项目**镜头序列计算（L1700-1715），这时还没有分组、版式和开关信息，无法套用组级回切条件。
   - 而且 AUTO 播种不以候选冲突为闸口（第 3 节第 3 步）。
   - 因此本方案只在组级草稿域判定回切资格，前置导演的冲突记录保持原样（它记录的仍是事实）。如需改动，属于第 8 节 B 类事项。
2. **「另外 40 个 REQUIRED 组的冲突是相邻类」这一说法无法核实。**
   - 物化把原始冲突码统一改名为 `source_preplan_quality_conflict`，证据中看不到原码。
   - 能确定的只有：这 40 组都成功播种，说明它们组内不存在非相邻事件或构图重复。

---

## 10. 待部署机核实（只读）

```sql
-- 第 8 组冲突 #1 的原始冲突码（预期 repeated_non_adjacent_composition）
select c->>'code', c->'shot_ids'
from project_director_revisions r, json_array_elements(r.quality_conflicts_json::json) c
where r.id = '5b7808d6-e9bb-45e0-9863-fbd700b6f271'
  and (c->'shot_ids')::text like '%bdeabbd3-1a01-4248-bc64-8253931aeb9b%'
  and (c->'shot_ids')::text like '%9316a613-d6ca-4940-b9fd-f701a7f7c1a0%';

-- 本项目前置导演单元提示词版本（按 §45.2 预期为 v10，未核实）
select prompt_ref, count(*) from project_director_units
where project_id = '2c291aba-085d-499d-bd32-b3a4d75f252f' group by prompt_ref;
```

`bdeabbd3…` 与 `9316a613…` 分别是候选第 1、6 镜的来源镜头 ID（取自 `group08_candidate.json` 的 `source_shot_links_json`）。

---

## 11. 2026-11-05 复核更新记录

初稿（基于 `5ac9374`）推送前，main 已前进到 `58c537d`。逐项复核后的变化：

1. 第 8 组已解卡（F9），第 5.0 节改为完成记录；同型第二例 `23c59183` 第 36 组（F10），补为第二个测试夹具。
2. v2 已部署并开启单格开关（F6、F7），阶段 3 的前置条件已满足。
3. 决策编号顺延：USER_DECISIONS §47→§48，验收 §43→§44（第 7 节）。
4. 行号：只有 `storyboard_prompt.py`（§46.11 规则在 L158）与 `panel_images.py`（`check_storyboard_panel_layout` 在 L667）变化；其余引用逐行复核未变。
5. R4 的推荐理由需要更正：推荐时引用 Gate 1 首轮「v1 故事板裁切通过率 78.8%」作为「v1 无法验证」的依据；Gate 1 重跑（crop-grid-v2 修订后，`layout_version="grid-v1"`）通过率为 98.36%。R4 本身（只在 v2 放开）仍是用户已确认的决策，且运行时只对 v2 做裁切演练，所以本方案不变。如需对 v1 也放开回切，须用户另行决定，并补 v1 的运行时裁切。
6. 附录 B 回放在 `58c537d` 上重跑，结果不变。

---

## 附录 A：第 8 组候选镜头（修补前，`group08_candidate.json`）

| 序 | shot_id | 场景 | 景别 | 机位 | 事件 | 主体焦点 | 时间 | 地点 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `137dcc45-6aab-45eb-b2e1-9b1b8c52539d` | 빙수점 - 계산대 장부 | CLOSE_UP | OVERHEAD | PERFORM_ACTION | PROCESS | LATER | SAME_LOCATION |
| 2 | `787311eb-affd-4aa4-a1db-a35831da9687` | 빙수점 - 카드 단말기 옆 | EXTREME_CLOSE_UP | OBJECTIVE | REVEAL_EVIDENCE | OBJECT_OR_EVIDENCE | SAME_MOMENT | SAME_LOCATION |
| 3 | `59c0d209-b98f-44ec-8c03-3b5bec6920b3` | 빙수점 - 계산대 앞 | MEDIUM | EYE_LEVEL | EXPRESS_UNCERTAINTY | ENVIRONMENT | SAME_MOMENT | SAME_LOCATION |
| 4 | `952d73a0-1086-4880-a1c2-4aa40b4087a6` | 빙수점 - 카드 결제대 | CLOSE_UP | OVER_SHOULDER | PERFORM_ACTION | PRIMARY_SUBJECT | LATER | SAME_LOCATION |
| 5 | `e264bcb8-4a1d-4f97-bc15-a87fb8535515` | 빙수점 - 계산대 장부 위 | CLOSE_UP | OVERHEAD | EXPRESS_UNCERTAINTY | ABSTRACT_CONCEPT | SAME_MOMENT | SAME_LOCATION |
| 6 | `f4ef2ca4-26b5-44aa-bbd7-bf034fa832c0` | 빙수점 - 계산대 장부 | CLOSE_UP | OVERHEAD | ESTABLISH_CONTEXT | OBJECT_OR_EVIDENCE | SAME_MOMENT | SAME_LOCATION |

- 第 5 镜场景多一个「위」，构图键与第 1、6 镜不同。
- 第 6 镜改为 `EYE_LEVEL` 后，与组内其他镜头均不重复：第 3 镜虽也是 `EYE_LEVEL`，但场景与景别不同。

## 附录 B：回放脚本与输出

只读，导入仓库产品代码，不写任何数据。在仓库 `backend/` 目录运行：

`PYTHONPATH=src python3 replay_group08.py "../Pending issues/2026-11-04/evidence/autoresume_group08/group08_candidate.json"`

```python
"""第 8 组回放：用仓库内产品领域代码（不另写规则）复算播种与第 07 步保存两条路径。只读，不写任何数据。"""
import json
import sys
from datetime import UTC, datetime
from uuid import UUID, uuid4

from tubeflow_test.application.storyboard_batch import _system_approvals_for
from tubeflow_test.domain.storyboards import (
    DirectorPlanRevision,
    DirectorRevisionKind,
    DirectorShot,
    ReviewState,
    ShotSentenceLink,
)

row = json.load(open(sys.argv[1]))
field = lambda k: json.loads(row[k]) if isinstance(row[k], str) else row[k]  # noqa: E731
raw, raw_links = field("shots_json"), field("links_json")
SEM = ("semantic_event", "subject_focus", "time_relation", "location_relation")


def shot(s, semantics=True):
    extra = {k: s[k] for k in SEM} if semantics else {}
    return DirectorShot(
        UUID(s["shot_id"]), s["order"], s["scene"], s["shot_size"], s["camera_viewpoint"],
        s["subject_action_outcome"], s["narrative_function"], s["description"], **extra,
    )


links = tuple(ShotSentenceLink(UUID(x["shot_id"]), UUID(x["sentence_id"])) for x in raw_links)
sentences = tuple(dict.fromkeys(UUID(x["sentence_id"]) for x in raw_links))


def review(shots, approvals):  # 与 DirectorPlanService.prepare_draft 的 review_state 判定逐行一致
    ok = {(a.previous_shot_id, a.next_shot_id) for a in approvals}
    amb = any(b.changes_from(a) < 2 and (a.shot_id, b.shot_id) not in ok for a, b in zip(shots, shots[1:]))
    return ReviewState.REQUIRED if amb else ReviewState.APPROVED if approvals else ReviewState.NOT_REQUIRED


def build(label, shots, approvals=()):
    r = review(shots, approvals)
    try:
        DirectorPlanRevision(uuid4(), uuid4(), uuid4(), uuid4(), 1, DirectorRevisionKind.DRAFT,
                             shots, links, sentences, r, tuple(approvals), datetime.now(UTC))
        print(f"{label}: OK review={r.value} may_apply={r is not ReviewState.REQUIRED}")
    except ValueError as e:
        print(f"{label}: ValueError({e}) review={r.value}")


A = tuple(shot(s) for s in raw)
print("shot6 vs shot1 受控语义差异维数:", A[5].changes_from(A[0]),
      "| 事件键相同:", A[5].event_repeat_key() == A[0].event_repeat_key())
build("1 AUTO 播种原样（带语义 + 系统审批）", A, _system_approvals_for(A))
fixed = list(raw)
fixed[5] = {**raw[5], "camera_viewpoint": "EYE_LEVEL"}  # 只改第 6 镜机位
build("2 第07步保存（无草稿 → 语义为 UNSPECIFIED，无审批）", tuple(shot(s, semantics=False) for s in fixed))
```

夹具是修补前的候选行（`group08_candidate.json`，不是 `fix_applied/group08_candidate.after.json`）。2026-11-04 在 `main@5ac9374` 与 2026-11-05 在 `main@58c537d` 上各运行一次，输出逐字相同：

```text
shot6 vs shot1 受控语义差异维数: 3 | 事件键相同: False
1 AUTO 播种原样（带语义 + 系统审批）: ValueError(director plan repeats an event or composition across the group) review=NOT_REQUIRED
2 第07步保存（无草稿 → 语义为 UNSPECIFIED，无审批）: OK review=NOT_REQUIRED may_apply=True
```

路径 2 模拟的是：第 8 组没有草稿，`_inherit_controlled_semantics`（`application/storyboards.py:450-468`）无从继承，PUT 合同又不携带语义字段（`api/storyboards.py:196-207`），所以镜头语义为 `UNSPECIFIED`；相邻差异按文本字段比较，结果相邻对均差 ≥4 项。
