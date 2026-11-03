# AUTO 全链路自动化「卡点清单」审查报告

- 日期：2026-10-14
- 范围：`D:\AAA-project\TubeFlow AI - Test` 当前工作区代码（`backend/src/tubeflow_test`）+ 部署机 `192.168.110.200` 生产库只读实测
- 口径：一切结论以代码/配置/生产数据为准；无法确认处标注「待确认」或「未找到」。
- 方法：核心链路人肉逐行读（含 7 个关键模块全读）+ 两个独立子代理交叉核对（故事板链路、视频链路）+ 部署机 PostgreSQL 只读 SQL 实证（未做任何写操作）。

---

## 0. 摘要（先说结论）

1. **链路本身的设计已经相当完整**：每阶段都有稳定暂停码、AUTO 有界重试（5s/15s/30s ×3）、每对象幂等键、进程内踢点 + 20s 编排巡检 + 各 outbox relay + 恢复工作流的复核网。
2. **当前最大的结构性风险不在"失败"而在"静默停滞"**：存在一批路径，编排器既不暂停也不推进（`_touch` 空转），页面也没有可用动作——用户看不到"需要恢复"的徽章（`auto_pipeline/service.py:285-331`、`ports.py:55-91`）。
3. **已实锤一个生产级永久死锁**（非理论）：韩国项目 `a5367080` 存在一个"孤儿 SUBMITTING 导演单元"，永久阻塞导演恢复 → 点「恢复自动执行」必然再次以 `director_run_unresolved` 暂停（代码路径 + 生产数据双重证实）。
4. **发现一个未捕获异常缺陷**：`KeyRotationNeeded`（`application/elevenlabs_narration.py:47,233`）全仓库无任何捕获点 → EL 补克隆遇配额/鉴权失败时，既不换 key 也不标记坏 key，以 20 分钟租约空转 + 吃掉句子重试预算的方式"烧预算"。
5. **最高频的人工介入来源是故事板内容审核拒绝**：生产库实测 115 条 FAILED 故事板，审核拒绝占大头（44 次/35 组/4 项目）；每次恢复都需人工点「恢复自动执行」。
6. **基础设施层有一个"全局停摆"点**：TubeFlow compose 无 `restart` 策略（`docker-compose.yml` 全文无 `restart:`；`docs/DEPLOYMENT.md` §9 已记录为待办），而 worker 是 fail-fast 监督（任一组件退出 → 整进程退出）→ 容器停住后全链路停摆，必须手工 `docker compose up -d`。

**当前部署机实测快照**（2026-10-14 10:16，只读）：

| 维度 | 实测 |
|---|---|
| AUTO 运行 | 6 COMPLETED / 1 PAUSED（`a5367080`，`director_preplan_failed`，`failure_code=director_run_unresolved`）/ 0 RUNNING |
| 配音 attempt（全史） | COMPLETED 3115 / PROVIDER_REJECTED 1558 / SAFE_TO_RETRY 69 / OUTPUT_INVALID 40 |
| 故事板 | SUCCEEDED 796 / FAILED 115 / REMOTE_STATE_UNKNOWN 4（§13 历史保留） |
| 导演 run | SUCCEEDED 11 / **FAILED `remote_state_unknown_exhausted` 11** / 其它失败 16 |
| 视频 attempt | ARCHIVED 799 / QC 失败 14 / CANCELLED 10 / FAILED 3 / `video_input_asset_integrity_failed` 1 |
| 合成 job | SUCCEEDED 10 / FAILED `media_command_cancelled` 5 |
| outbox | 故事板 984 全部 ack；合成 11 SUPERSEDED + 3 DISPATCHED（陈留） |

---

## 1. 链路总览（任务创建 → 终态）

**起点**：用户在页面点击「提交并开始生产」= `POST /narration/confirm`（冻结不可变快照 + 创建 `NarrationJob` + 创建 `AutoPipelineRun(RUNNING, round=1)` + 创建导演运行，同一事务，`api/narration_runtime.py:145-335`）。
**终点**：`AutoPipelineRun.COMPLETED`（仅在当前成片存在时到达，`auto_pipeline/domain.py:116-124`）；失败路径落 `PAUSED` + 稳定原因码（`auto_pipeline/domain.py:29-44`）。
**范围外**：封面（独立手动功能，§18.2）、导出 ZIP（手动端点）、项目删除（异步清理链，另属 purge）。
**模式**：当前所有新建项目固定 `production_mode="AUTO"`（`identity_project/service.py:628`），MANUAL 代码保留但不再产生新项目。

### 2.1 阶段表

| # | 阶段 | 触发（谁、什么事件） | 执行主体 | 依赖外部资源 | 权威状态流转 | 回调/推进 |
|---|---|---|---|---|---|---|
| S0 | 提交与冻结 | 用户点「提交」→ `POST /narration/confirm` | web（`ProjectNarrationService.confirm_and_start`）+ 拆句 agent（DeepSeek） | DeepSeek、PostgreSQL | 快照冻结 → NarrationJob(PENDING) → AutoPipelineRun(RUNNING) → 导演运行入库 | narrator relay 2s 轮询搬 job |
| S1 | 配音（真实 TTS） | `NarrationJob` PENDING → relay → `NarrationWorkflow` | Temporal worker（20 句/批） | **CosyVoice 或 EL（经 mihomo 代理）**、MinIO、FFmpeg | attempt 9 态（`domain/narration.py:80-89`）；job `PENDING→DISPATCHED→COMPLETED/PARTIAL_FAILED/FAILED` | `finish_job`→`auto_kick`；全成功→`finalize_timeline`（时序+≤15s 分组） |
| S2 | 前置导演（与配音并行） | 冻结事务内 `prepare_run` → outbox `PROJECT_DIRECTOR_RUN_REQUESTED` | `ProjectDirectorRunWorkflow`（并发=parallelism 个 `advance_project_director`） | 导演 LLM（grsai `gpt-5.6-sol`，超时 900s） | run：`INTENT_PERSISTED→RUNNING→(REMOTE_STATE_UNKNOWN)→SUCCEEDED/FAILED`；unit：`…→LEASED→SUBMITTING→…` | run 终态/物化→`auto_kick` |
| S3 | 确定性重映射 + 草稿播种 | 编排器 `storyboard_drafts` 阶段（无外部 AI） | `AutoPipelineService.advance` + `StoryboardBatchService` | 无 | 每组一份可应用草稿；AUTO 对歧义相邻对加系统审批 | `_touch` 进下一跳 |
| S4 | 故事板批量生成 | 编排器 `storyboard` 阶段；或手动批量端点 | `GridSheetGenerationWorkflow` → activity `advance_storyboard` | **grsai gpt-image-2**、MinIO | `INTENT_PERSISTED→SUBMITTING→SUBMITTED→RUNNING→DOWNLOADED→QC_PASSED→OBJECT_STORED→SUCCEEDED`（+FAILED/UNKNOWN） | 终态→`auto_kick` |
| S5 | 视频提示词编译 + H3 组级视频 | 编排器 `video` 阶段 → 批量 job → 每组意图（编译同步执行） | `GroupVideoRecoveryWorkflow` → activity `recover_group_video_attempt` | **DeepSeek（编译）**、**KineStack H3 引擎 ×N 节点** | attempt：`CREATED→STAGING→…→SUBMITTED→RUNNING→SUCCEEDED→ARCHIVED`（+FAILED/CANCELLED/QC_FAILED/UNKNOWN） | 终态→`auto_kick`；节点心跳 10s；槽位租约 2min |
| S6 | 合成 | 编排器 `composition` 阶段 → composition outbox → relay（1s + 60s 失联对账） | `ManualCompositionWorkflow` → activity `compose-project-media-v1` | FFmpeg/ffprobe、MinIO | 候选版本；AUTO 首版自动 `select_current` | finally→`auto_kick` |
| S7 | 完成判定 | 编排器 `advance`：`composition_current_exists` | web/worker | — | `RUNNING→COMPLETED`；同事务 `close_superseded` | 页面 `workflow-summary` 轮询（2.5/5/10s） |

### 2.2 推进/保活机制（卡点分析的坐标系）

| 机制 | 位置 | 行为 |
|---|---|---|
| 进程内踢点 `auto_kick` | 各 activity 终态处（配音 `workflows/narration.py:104-105` 等） | 触发 `advance()`；异常仅记日志（`auto_pipeline/service.py:123-128`） |
| 编排巡检 `sweep_once` | `workflows/worker.py:569-574`，默认 20s（`config.py:130`） | 只扫 **RUNNING** 运行；PAUSED 不扫（`adapters/persistence/auto_pipeline.py:158-165`） |
| 各 outbox relay | narrator 2s / storyboard 0.5s / composition 1s / video 0.5s | 只搬 PENDING 行；派发失败按指数退避重排 |
| 恢复工作流 | `GridSheetRecoveryWorkflow`（5s 循环）、`GroupVideoRecoveryWorkflow`（2-30s，STALLED 后 300s） | 未知态/在途状态的持续推进责任方 |
| 失联对账 | 合成 60s（`workflows/worker.py:288-301`）；H3 outbox redrive 5min（`video_generation/worker.py:197-242`） | 容器崩溃/心跳超时后的重排 |
| worker 监督 | `SupervisedWorkerGroup` fail-fast（`workflows/supervision.py:42-111`） | **任一组件退出 → 整进程退出** |
| 容器重启 | `docker-compose.yml` | **无 `restart:` 策略**（DEPLOYMENT.md §9 待办） |

---

## 3. 卡点清单（按阶段）

> 说明：
> - **类型** =「有码暂停」（有稳定暂停码+页面恢复入口）｜「静默停滞」（既不暂停也不推进，页面无可用动作）｜「入口阻断」（提交前被拒）。
> - 严重度/频率为基于代码控制流 + 生产数据的推断；频率标注依据在「实证」列。

### A. 提交与冻结阶段

| # | 卡点 | 触发条件 | 根因 | 代码位置 | 重试/超时 | 日志/异常特征 | 影响 | 类型 | 严重度/频率 |
|---|---|---|---|---|---|---|---|---|---|
| A1 | 拆句 agent 失败导致提交整体失败 | 风险句上游 LLM（DeepSeek）超时/5xx/返回不合合同 | 设计即"任一环节失败 → 整个提交失败，不落快照/job" | `sentence_split/service.py:62-152`（3 次尝试、5/15/30s、429 尊重 Retry-After）；`application/narration.py:804-814` | 每句 3 次 | `sentence_split_unavailable` / `sentence_split_response_invalid` / `sentence_split_provider_rejected` | 用户无法提交，需再次点击提交（无自动恢复） | 入口阻断 | 中/低-中（上游可用性） |
| A2 | 重复提交/快照竞态 | 提交事务行锁 + 既有 job 防重入 | 防重入设计（非缺陷） | `api/narration_runtime.py:152-186,295-296` | — | 409 `project_snapshot_locked`/`submission_in_progress` | 明确报错，不卡 | 入口阻断 | 低/低 |
| A3 | 频道默认音色/克隆音色缺失 | 创建项目门禁 | 设计门禁 | `identity_project/service.py:591-608` | — | `channel_default_voice_required` | 明确拒绝，不卡 | 入口阻断 | 低/低 |

### B. 配音阶段（narration）

| # | 卡点 | 触发条件 | 根因 | 代码位置 | 重试/超时 | 日志/异常特征 | 影响 | 类型 | 严重度/频率 |
|---|---|---|---|---|---|---|---|---|---|
| B1 | EL 号池全不可用 / key 失效 | 所有 key 被标 `exhausted`/`invalid`，或复核 401 | §23.6 明确：账号级阻断不耗预算、不自动重投 | 抛出：`elevenlabs_narration.py:185-197`；代码映射：`domain/narration.py:394-402`；阻断落库：`application/narration.py:1222-1234` | **无**（只能充值/换 key + 手动恢复） | `narration_provider_quota_exhausted` / `narration_provider_auth_failed`（阻断码）；上游 `provider_quota_exhausted`/`provider_auth_failed` | 项目 PAUSED；号池补 key 后点「恢复」可续；恢复前反复暂停 | 有码暂停 | 高/中（付费额度现实） |
| **B2** | **`KeyRotationNeeded` 未捕获缺陷** | EL 项目 + 当前账号缺该克隆音色映射 + 补克隆遇 402/401 | `_clone_into_account` 抛 `KeyRotationNeeded`，但 `_synthesize_with_rotation` 只 catch `DomainError`，**全仓无捕获点** → 既不 rotate 也不标 key | 定义/抛出：`application/elevenlabs_narration.py:47,231-233`；未被捕获点：`elevenlabs_narration.py:157-189`；后果状态机：`adapters/persistence/narration.py:874-890`（租约到期→`REMOTE_STATE_UNKNOWN`） | Temporal activity 重试；20 分钟提交租约（`application/narration.py:65,350-354`） | 无专属日志；现象=attempt 停在 `SUBMISSION_STARTED` → 20 分钟后 `submission_lease_expired` → 走预算重试 | 坏 key 永远不换、不标记；每轮以 20 分钟空转 + 消耗句子 3 次预算，最终 `narration_submission_budget_exhausted` 暂停 | 静默→有码暂停（缺陷） | 高/低-中（条件触发） |
| B3 | 代理/网络故障消耗对象预算 | mihomo 容器挂/节点失联/网络抖动 | `provider_not_reached` 属临时故障 → 每次尝试计预算；约 50s 内可耗尽 3 次 | 分类：`adapters/elevenlabs.py:97-104`；计预算重试：`application/narration.py:167-183,404-427`；耗尽：`domain/narration.py:377-380` | 3 次 5/15/30s | `provider_not_reached` → `narration_submission_budget_exhausted` | 短时网络故障即可把在途句打成暂停（需人工恢复；恢复=新一轮预算） | 有码暂停 | 中-高/中（2026-10-13 mihomo 连续两次故障史） |
| B4 | EL 内容审核 403（`content_against_policy`） | 文案/音色触审核 | 确定性失败不自动重试；**AUTO 文案已冻结不可改** | 分类：`adapters/elevenlabs.py:257-265`；不重试：`domain/narration.py:382-390`；冻结不可变：§3.8 | **无重试** | `provider_content_rejected` → `narration_provider_rejected` | 项目对同文案必然再次失败，恢复按钮无意义；**唯一出路=新建项目改文案/音色** | 有码暂停（实质死局） | 高/低（截至 2026-10-13 EL 0 次、CosyVoice 0 次） |
| B5 | 预算耗尽（OUTPUT_INVALID/未知态自动重试耗尽） | 3 次尝试（含自动重提）全部失败 | 设计预算 | `application/narration.py:1181-1273` | 3 次 5/15/30s；恢复重置 | `narration_submission_budget_exhausted` | 暂停→恢复可再试 3 次 | 有码暂停 | 中/中 |
| **B6** | **ARCHIVE_FAILED 死胡同** | 已拿到 Provider 产物，但 checkpoint 落库/归档失败（MinIO 等） | `ARCHIVE_FAILED` 不在自动可重试集合；人工对账 resoluton 无一可用（`RETRY_MISSING` 要求 `SAFE_TO_RETRY`、`RETRY_INVALID_OUTPUT` 要求 `OUTPUT_INVALID`、`CONFIRMED_FAILED`→`PROVIDER_REJECTED` 仍阻断） | 状态：`application/narration.py:236-252,464-473`；不可重试判定：`domain/narration.py:382-390`；reconcile 校验：`application/narration.py:1439-1485` | **无可用出口**（代码中未找到恢复路径） | `narration_archive_failed` | 项目永久暂停；机制上本可用 checkpoint 免 Provider 重归档（`execute_batch` 的 checkpoint 分支），但调度层从不放行 | 有码暂停（实质死局） | 中-高/低 |
| B7 | 提交租约 20 分钟（worker 被杀） | 提交开始后进程死亡 | 租约到期自动转未知态再走自动重试 | `adapters/persistence/narration.py:874-890` | 20 分钟自动恢复 | `submission_lease_expired` | 仅延迟 | 自动恢复 | 低/低 |
| B8 | `submission_lease_active` 热重试 | 同句另一 worker 持租约 | activity 层重试等租约到期 | `application/narration.py:355-372` | 最多 20 分钟 | `submission_lease_active` | 仅延迟 | 自动恢复 | 低/低 |
| B9 | 单句时长护栏 | EL 输出 >60s | 输出不确定性 → OUTPUT_INVALID → 自动重试 | `elevenlabs_narration.py:276-293` | 3 次预算 | `provider_output_invalid` | 自动 | 自动恢复 | 低/低 |

### C. 前置导演阶段

| # | 卡点 | 触发条件 | 根因 | 代码位置 | 重试/超时 | 日志/异常特征 | 影响 | 类型 | 严重度/频率 |
|---|---|---|---|---|---|---|---|---|---|
| **C1** | **孤儿 SUBMITTING 单元永久阻塞恢复（已实锤）** | 并行单元调用中另一个单元先到终态 → run 被判 FAILED；在途单元被工作流关闭丢弃，永远停在 `SUBMITTING` | ① 恢复创建要求"无任何 LEASED/SUBMITTING 单元"；② stale 回收只在 `advance()` 里执行，而 `advance()` 对 FAILED run 提前返回 → 无人回收 | 恢复阻塞：`adapters/persistence/storyboards/project_director_repository.py:313-328`、`application/project_director.py:259-263`；错误码：`project_director_repository.py:373-376`；stale 回收唯一入口：`application/project_director.py:692-704`（前置提前返回 `:685-689`） | **无**（无 director 专用清扫组件，worker 监督组件清单 `workflows/worker.py:82-99` 无相关项） | `director_run_unresolved`（暂停 details.failure_code；编排器 `auto_pipeline/service.py:395-405`） | **项目永久死锁**：点恢复 → `create_recovery_run` 被拒 → 再暂停，循环往复；UI 无出路 | 静默→有码（但恢复入口无效） | **极高/中**（需"并行+终态化"竞态；生产已现 1 例） |
| C2 | 导演单次调用超时截断（历史已修） | 单次 LLM 调用耗时 > 超时（实测 471.8s vs 原 300s） | HTTP 超时 < 租约 < 活动超时 的三处联动被截断 | 现不变式：`config.py:68-71`（900s）、`application/project_director.py:72`（20min 租约）、`workflows/storyboards.py:30`（20min 活动） | 单元 3 次 5/15/30s；整轮 `2×批次数` | `remote_state_unknown_exhausted`（生产 11 次 run FAILED 全史） | 已修复（900s）；修复前韩国项目 100% 复现 | 有码暂停（已治） | 中/（历史高） |
| C3 | 快照/输入身份变化 fail-closed | 部署新代码/新 Prompt（canonical 变）或快照被改 | §15.13：canonical 变自动整轮重跑（留审计）；快照身份变 = 异常信号 fail-closed | `application/project_director.py:562-592`、`:571-575` | 无（等人工） | `director_snapshot_integrity_violation` / `director_input_changed` | 前者需人工判定；后者=正常部署路径（自动放行） | 有码暂停 | 中/低 |
| C4 | 导演结构冲突/草稿校验失败 | 覆盖缺失、镜头超限、真重复事件等 | 结构类冲突不自动放行（设计） | `auto_pipeline/service.py:195-257`；保存即续跑：`auto_pipeline/service.py:110-121` | 无（人工修正） | `director_validation_failed`（details: group_sequences + failure_messages） | 用户在第 07 步修正草稿保存 → 自动续跑 | 有码暂停 | 中/低（§20 治理后） |
| C5 | provider 偶发虚假 4xx | grsai 偶发 400（原样重放即 200） | §15.14：LLM 类 4xx（除 401/403）按临时故障重试 | `adapters/director_model.py`（4xx 分类）+ `project_director.py:748-761` | 3 次预算 | `provider_http_400`（生产 5 次） | 自动恢复 | 自动恢复 | 低/中 |

### D. 故事板阶段

| # | 卡点 | 触发条件 | 根因 | 代码位置 | 重试/超时 | 日志/异常特征 | 影响 | 类型 | 严重度/频率 |
|---|---|---|---|---|---|---|---|---|---|
| **D1** | 内容审核拒绝（远程 FAILED） | grsai 判定 `output_moderation` 等 | 远端确认的失败不自动重试（§15.3）；提示词不变则可能复现 | poll FAILED→截图：`adapters/grsai_grid_sheet.py:120-123`；落 FAILED：`application/storyboards.py:649-660`；暂停：`auto_pipeline/service.py:247-257` | 无自动（人工 resume → 新一轮预算重提） | `storyboard_failed:output_moderation`（示例） | 每组需人工点恢复；恢复后自动重提（新预算）；**可能反复触发**（Norse Men 组 44 连续 2 次未过，仍 FAILED） | 有码暂停 | 中/**高**（实锤 44 次/35 组/4 项目） |
| D2 | 重提预算/对账预算耗尽 | 429/connect 重提 3 次失败，或未知态对账连续无果 3 次 | 预算设计 | `application/storyboards.py:856-883,602-612`；常量 `domain/storyboards.py:26` | 3 次 5/15/30s | `storyboard_failed:submission_budget_exhausted` / `remote_state_unknown_exhausted` / 中间码 `reconcile_no_remote_task` | 暂停→resume | 有码暂停 | 中/低-中 |
| **D3** | 处理阶段永久异常 → 无限重试（静默） | Provider 返回"成功"但下载/QC/对象写入持续失败（哈希不符、尺寸非法、MinIO 故障） | ① 该分支只写 `failure_stage/failure_code`，不改状态；② 状态机 `_may_transition` **不允许 `DOWNLOADED→FAILED`**；③ 工作流无显式 RetryPolicy（Temporal 默认无限重试） | 处理分支：`application/storyboards.py:681-696,712-732,748-796`；状态机：`adapters/persistence/storyboards/repository.py:2178-2221`；工作流：`workflows/storyboards.py:109-121`（无 retry_policy） | **无限**（无 MAX、无停滞判定；对照：H3 有 StallPolicy，故事板未找到同类） | `storyboard_processing_failed`（DomainError 文案 "Storyboard download or QC failed"/"object storage failed"） | 该组永远非终态 → 编排器不暂停不推进；页面仅 `REFRESH`；**无任何可点击动作** | 静默停滞 | 高/低 |
| D4 | 未知态（带 task_id）对账持续无果 | 引擎持续不可达/持续 RUNNING | 恢复工作流终止集合仅 `{SUCCEEDED, FAILED}`，无墙钟上限 | `workflows/storyboards.py:161-173` | 每 5s 无限循环 | 页面 `RECOVERY_REQUIRED`（`api/public_status.py:40-45`） | 静默空转；Provider 恢复即自愈 | 静默停滞 | 中/低 |
| D5 | 30 分钟处理租约 | worker 在 claim_processing 后被杀 | 租约未释放；到期后可重认领 | `application/storyboards.py:562-573`；`repository.py:1399-1428` | 到期自动重认领（延迟≤30min） | `lease_until` 残留 | 仅延迟 | 自动恢复 | 低/低 |
| D6 | 明确 4xx 拒绝 | grsai 拒绝提交/上传 | 既定失败直落 FAILED | `application/storyboards.py:520-529`；`grsai_grid_sheet.py:144-155` | 无 | `ProviderSubmitRejectedError` | 暂停→人工 | 有码暂停 | 中/低 |

### E. 视频生成（H3）阶段

| # | 卡点 | 触发条件 | 根因 | 代码位置 | 重试/超时 | 日志/异常特征 | 影响 | 类型 | 严重度/频率 |
|---|---|---|---|---|---|---|---|---|---|
| **E1** | 无可用节点/容量耗尽 → 静默等待 | 全部节点 disabled/心跳过期/不兼容/容量 0 | claim 抛 `VideoRuleViolation` → recover 返回 `WAITING` → attempt 停 `CREATED`（属 in-flight）→ 编排器 `_touch`；无节点阶段无任何终态/暂停语义 | 抛出：`sqlalchemy_repository.py:776-802,750-751`；WAITING：`video_generation/temporal.py:68-74`；in-flight 判定：`auto_pipeline/ports.py:55-91`；编排器：`auto_pipeline/service.py:285-331` | 恢复工作流 2→30s 无限重试（节点恢复即自愈） | `no compatible H3 node has capacity`（含容量快照日志，`sqlalchemy_repository.py:1108-1112`，限流 60s） | 整项目静默停滞；AUTO 页面无动作（手动动作对 RUNNING 运行被剥离，`identity_project/service.py:49-72,698-708`） | 静默停滞 | 高/中-高 |
| **E2** | durable 工作流未知态永不 STALLED | submit 超时/连接失败、inspect 网络错误、404 | ① `_stall_observation` 仅对 SUBMITTED/RUNNING 标停滞，UNKNOWN 强制 `stalled_at=None`；② durable（候选 `h3.tubeflow.storyboard-to-video`）不适用 AUTO 未知态自动重提 | 停滞：`video_generation/orchestration.py:790-829`（测试 `test_video_stall.py:126-137`）；重提守卫：`orchestration.py:764-777` + `_durable_candidate:2510-2514` | 恢复工作流 2→30s 无限轮询；**无上限、不重提** | `retry_evidence`：`inspect:<Exc>`/`reconcile:not_visible`；页面 `RECOVERY_REQUIRED`（AUTO 项被剥离） | 依赖引擎自愈；若引擎长期不恢复 → 静默停滞 | 静默停滞 | 高/中 |
| **E3** | GPU 探测未配置/失败 → 停滞自愈失效 | `h3_gpu_agent_token` 为空或探测端点未配 | 未配置回退 `UnavailableGpuStatusProbe` 永远 `PROBE_FAILED`；探测失败"绝不当作空闲" → 永久降频等待（5 分钟） | 接线：`video_generation/runtime.py:103-116`；回退：`gpu_probe.py:121-125`；判定：`orchestration.py:944-990` | 无（停滞轮询 300s 持续） | `h3 gpu status probe failed for node`；`GPU_PROBE_INCONCLUSIVE` | 配合"僵尸任务"（DEPLOYMENT §10）→ 永不自愈，需人工处理远端 | 静默停滞 | 高/中（未配探测时必现） |
| E4 | `LOCAL_INTEGRITY_BLOCKED`（非终态阶段） | 已推进阶段上哈希/权威快照校验失败（素材被删改、快照漂移） | 只改 `error_code` 不改 stage；redrive 每 5 分钟重启再撞同一校验 | `orchestration.py:519-562`（blocked 分支）；redrive：`video_generation/worker.py:197-242` | 无限 5 分钟循环 | `local_integrity_blocked:<code>` / `video_input_asset_integrity_failed`（生产 1 次 FAILED） | 静默；无 pause 码 | 静默停滞 | 高/低（需数据破坏） |
| E5 | 非终态槽位泄漏 → 转化为 E1 | recovery workflow 持久失败（5 次）且 attempt 非终态 | `sweep_expired_slots` 只回收 `FAILED/CANCELLED/ARCHIVED` 且 `remote_acceptance!=UNKNOWN`；非终态槽位永不回收 | `sqlalchemy_repository.py:675-714` | 10s 清扫但条件受限；workflow 靠 redrive 5 分钟重启 | E1 的容量告警 | 容量永久虚占 → 新提交落 E1 | 静默停滞 | 高/低-中 |
| E6 | QC_FAILED（分辨率/时长/音轨/帧率不达标） | `_verify_media` 不过 | AUTO 自动重生成（预算 3）；用尽后暂停 | 归档校验：`orchestration.py:2409-2440`；自动重生成：`:1450-1460`；预算：`:884-904` | 3 次预算 | `video_failed:<qc码>`（暂停码） | 有码暂停→resume | 有码暂停 | 中/中（生产 14 次） |
| E7 | 取消不确定循环 | 用户取消时引擎不可达 | `cancel_inspect_count>=3` 重发；重发失败即归零重来 | `orchestration.py:1245-1323` | 无总上限 | 审计事件 `CANCEL_INSPECT_ERROR/NOT_FOUND/REDRIVE_UNKNOWN` | 取消动作永不确认 | 静默停滞 | 中/低 |
| E8 | 提示词编译失败 | LLM 输出不合合同/端点不可用 | 编译在意图创建时同步执行；失败 → item error | `video_prompt/compiler.py:88-167`；AUTO `max_repairs=2`：`orchestration.py:2138-2151`；暂停：`auto_pipeline/service.py:305-325` | 2 次修复 + 3 次调用预算 | `video_prompt_compilation_failed` / `unavailable` → `video_failed:intent_rejected` | 暂停→resume | 有码暂停 | 中/低 |
| E9 | 提交预算耗尽 | 提交/重提 3 次失败 | 预算设计 | `orchestration.py:836-841,884-922` | 3 次 5/15/30s | `video_failed:submission_budget_exhausted` | 暂停→resume | 有码暂停 | 中/低 |
| E10 | recovery workflow 5×失败 | activity 连续 5 次失败 | 靠 outbox redrive（5 分钟）+ Temporal 默认 AllowDuplicate 重启；持久确定性错误则循环 | `temporal.py:127-139`；redrive：`video_generation/worker.py:197-242` | 5 次/轮，5 分钟重启 | Temporal 侧 workflow FAILED | 瞬时故障自愈（≤5min）；持久错误长期循环 | 静默停滞 | 中/低 |

### F. 合成与完成阶段

| # | 卡点 | 触发条件 | 根因 | 代码位置 | 重试/超时 | 日志/异常特征 | 影响 | 类型 | 严重度/频率 |
|---|---|---|---|---|---|---|---|---|---|
| F1 | FFmpeg 失败 | 临时（被杀/超时/命令失败）或既定失败 | 临时码自动重试 3 次；耗尽/既定 → 暂停 | `application/composition.py:23-34,195-209`；活动 `workflows/composition.py:38-44`（30min、心跳 30s、max 3） | 3 次 5/15/30s | `composition_failed`（暂停码）；生产 5 次 `media_command_cancelled`（疑似容器崩溃杀 ffmpeg） | 有码暂停→resume（`RETRY_COMPOSITION`） | 有码暂停 | 中/中 |
| F2 | 工作流失联（容器崩溃） | 心跳超时死亡 | 60s 对账把 RUNNING job 标 `composition_workflow_lost`，AUTO 按代际重排（≤3 次） | `media_repository.py:1982-2104`；relay：`workflows/worker.py:288-301` | 3 次 5/15/30s | `composition_workflow_lost` | 自愈或暂停 | 自动/有码 | 中/低-中 |
| F3 | 完成态判定依赖"当前成片" | 合成候选未自动 select_current | AUTO 首版自动切换（设计） | `application/composition.py:188-194`；`auto_pipeline/service.py:333-349` | — | — | 正常 | — | — |

### G. 平台级/横切

| # | 卡点 | 触发条件 | 根因 | 代码位置 | 重试/超时 | 日志/异常特征 | 影响 | 类型 | 严重度/频率 |
|---|---|---|---|---|---|---|---|---|---|
| **G1** | **Worker 容器停摆后无自动恢复（全局）** | 任一被监督组件退出（如 Temporal 瞬断使 `dispatch_pending_jobs` 抛错 `application/narration.py:837-842`，narration relay 无 try 直接抛出 `workflows/worker.py:324-328`）→ SupervisedWorkerGroup fail-fast → 进程退出；compose 无 restart → 容器停住 | 设计 fail-fast + compose 未配重启（DEPLOYMENT.md §9 已记录待办） | 监督：`workflows/supervision.py:42-111`；relay 无捕获：`workflows/worker.py:322-328`；compose 无 `restart:`：`docker-compose.yml` 全文 | **无**（重启后 Temporal/DB 状态幂等，可安全重启） | 容器 Exited；日志尾部为组件异常 | **全链路停摆**（配音/导演/故事板/视频/合成/巡检全停），需 SSH 手工 `docker compose up -d`；断电重启同理 | 静默停滞（全局） | **极高/中** |
| G2 | mihomo 容器不可用 | 容器挂/订阅失效 | web/worker `depends_on: service_healthy` → 起不来；运行中挂 → EL 线路故障（见 B3） | `docker-compose.yml`（depends_on/healthcheck）；入口脚本 `docker/mihomo/entrypoint.sh` | mihomo 无重启策略（同一 G1） | `[mihomo-init]` 日志 | EL 线路全断；其它线路不受影响 | 静默停滞（EL 专项） | 高/中（已两次故障修复） |
| G3 | Temporal 不可用 | 服务宕/网络 | 派发失败→relay 退避；预检拒绝启动 | `workflows/worker.py:343-346`；`application/composition_outbox.py:71-81` | 退避重试 | `Temporal ... dispatch failed` | 任务积压不丢（outbox 留 PENDING） | 自动恢复 | 中/低 |
| G4 | PostgreSQL/MinIO 故障 | 服务宕 | 各 activity 异常重试；组件可能 fail-fast（G1） | 全局 | 视组件 | 各类连接异常 | 取决于 G1 是否触发 | 视情况 | 高/低 |
| G5 | `h3_configured=False`（配置缺失） | 部署 `.env` 未配 H3 节点 | worker 不装配视频组件；编排器 `self._videos is None` → 永 `_touch("video")` | `config.py:150-152`；`workflows/worker.py:432-557`；`auto_pipeline/service.py:301-302` | **无** | 无 | 视频阶段静默停滞 | 静默停滞 | 高/极低（配置正确性） |

---

## 4. 优先级排序（按 严重度 × 频率）

### P0（最高优先——静默停滞 / 全局停摆 / 永久死锁）

| 排序 | 编号 | 卡点 | 为什么是 P0 |
|---|---|---|---|
| 1 | **G1** | worker 容器停摆无自动恢复 | 单点即可导致**全链路停摆**；触发条件宽松（一次 Temporal 瞬断/断电即可）；无任何脚本外出路 |
| 2 | **C1** | 导演孤儿 SUBMITTING 单元死锁 | **已实锤在线**；永久性、UI 恢复按钮无效；属"恢复机制自身缺陷" |
| 3 | **E1** | 无可用节点静默等待 | 一旦节点全不可用（历史发生过 3 台 ComfyUI 503、僵尸任务占容量）整项目静默停滞，无 pause、无动作 |
| 4 | **E2** | durable 未知态永不 STALLED | 网络抖动即触发；无上限轮询、无自动重提、无暂停 |
| 5 | **E3** | GPU 探测未配/失败 | 使"停滞自愈"整体失效；与 DEPLOYMENT §10 僵尸任务叠加即永卡 |

### P1（高优先——有缺陷或高频率的人工介入）

| 排序 | 编号 | 卡点 | 依据 |
|---|---|---|---|
| 6 | **B2** | `KeyRotationNeeded` 未捕获（缺陷） | 明确代码缺陷；后果=坏 key 不换 + 20 分钟空转 + 预算误耗尽 |
| 7 | **D1** | 故事板审核拒绝 | **实测最高频**人工介入来源（44 次/35 组/4 项目）；可能反复触发 |
| 8 | **B1** | EL 号池耗尽/失效 | 付费额度现实下必然发生；有恢复入口但需人工充值/换 key |
| 9 | **B3** | 代理/网络故障消耗预算 | 短时网络抖动即可把在途句打成暂停；mihomo 有两次故障史 |
| 10 | **B4/B6** | 内容审核死局 / ARCHIVE_FAILED 死胡同 | 概率低但**无有效恢复路径**（B4 需新项目；B6 未找到出口） |

### P2（中优先——有码暂停，恢复机制正常）

| 编号 | 卡点 | 说明 |
|---|---|---|
| E6/F1/F2/E8/E9/D2/D6/C3/C4/C5/B5/B7/B8/A1 | …… | 均有稳定码 + 有效人工入口（「恢复自动执行」或阶段级动作），或自动恢复 |

### P3（待观察/条件触发）

| 编号 | 卡点 | 说明 |
|---|---|---|
| D3/D4/E4/E5/E7/E10/G5 | 处理阶段无限重试、未知态空转、完整性阻断、槽位泄漏、取消循环、recovery 循环、H3 未配置 | 概率低，但一旦命中无出口；建议纳入巡检指标而非立即改造 |

---

## 5. 高优先级卡点的候选解决方案与取舍（供讨论，不实施）

### P0-1（G1）worker 容器停摆无自动恢复
- **A. 给 web/worker 加 `restart: unless-stopped`**（compose 一行级改动）。取舍：直接消除"停摆等人"，但可能掩盖 crash-loop 根因——建议同时加"连续重启次数"告警（或保留 healthcheck + 日志监控）。风险：重启瞬间 Temporal 重放/幂等已由设计兜底（有 PENDING outbox 与 Temporal 持久化），不产生重复外部提交（幂等键已存在）。
- **B. 改为组件级自动重启（进程内退避重拉）**：把 fail-fast 改为"单组件失败→退避重启该组件"，仅当连续失败才整体退出。取舍：改动监督语义、影响面大；但更精细。
- **C. 外部看护（systemd timer / 定时巡检脚本）**：不改应用。取舍：引入非仓库内运维依赖，需与"两机不并行"约定协调。
- 备注：无论哪种，**断电/重启后仍需手工起栈**的问题一并解决（A/C）。

### P0-2（C1）导演孤儿 SUBMITTING 单元死锁
- **A. 恢复创建前做"存量单元回收"**：在 `create_recovery_run`（或 repo `create_run_in_session` 的 AUTO_RESUME 分支）对 **lease 已过期的** LEASED/SUBMITTING 单元先按既有 `worker_lost_after_call_started` 语义置 FAILED（不伪造"远端未受理"，仅记录本地回收事实），再执行 in-flight 校验。取舍：语义上与 `advance()` 的 stale 回收一致，风险低；需补测试守卫（防止回收活跃中的单元）。
- **B. 让 stale 回收覆盖终态 run**：在 `advance()` 提前返回之前先执行 stale 回收（或新增一个"单元回收"巡检）。取舍：更彻底；但 advance 对终态 run 的"只读"假设被打破，需要审计。
- **C. 一次数据修复**：把 `a5367080` 的孤儿单元置 FAILED。取舍：治当前一例，不治根因；建议与 A/B 一起做。
- 长期建议：给 director 链路加"运行终结时的收尾钩子"——run 转 FAILED/SUCCEEDED 时统一终结其非终态单元。

### P0-3（E1）无可用节点静默等待
- **A. 新增"等待容量"有界窗口**：无节点超过 N 分钟（如 30-60min）→ `_pause(video_waiting_capacity)`，页面显示原因 + 提供「恢复」。取舍：新增稳定码（须同步文档/测试）；短时打满很常见，阈值需防误报。
- **B. 仅页面化不改状态**：把"等待 GPU 容量"作为可执行的公共状态展示（§7.14 已有"远端等待"概念），并显示排队组数。取舍：零语义变更，但自动链路仍停着；适合先做。
- **C. 编排器把"节点全不可用"当作显式前置门禁**（与 H3 fail-closed 判定对齐）：总体不可用时暂停并展示。取舍：与 §7.15 判定天然契合；实现需读 availability 端口。
- 建议组合：B 立即做（透出），A/C 二选一（有界暂停）。

### P0-4（E2）durable 未知态永不 STALLED
- **A. 未知态纳入停滞观测**：UNKNOWN 超 N 分钟 → 标记 `remote_stalled`（仅状态事实，不重提）。取舍：与现测试 `test_remote_state_unknown_never_marks_stall` 的既定语义冲突（该测试断言未知态不标停滞——需先确认该设计意图，属产品边界变更）。
- **B. 未知态超时 → 暂停等人**（不回退自动重提，符合 §16.1 候选约束）。取舍：保住"未知不得自动重提"，但把"无限等待"变成"有界等待+人工决策"；需新增稳定码。
- **C. 维持现状 + 监控**：把"未知态持续时长"做巡检指标/页面展示。取舍：零风险，但 UX 仍差。

### P0-5（E3）GPU 探测未配置/失败
- **A. 配置层**：确保三台节点配好只读 GPU 采集端点 + `h3_gpu_agent_token`（部署动作）。取舍：最直接；需用户提供端点。
- **B. 代码层**：`PROBE_FAILED` 连续 N 次（如 30 分钟）→ 落暂停/告警，替代无限降频。取舍：防"探测本身挂了导致不自愈"；阈值需调。
- 建议：A 为准，B 为兜底。

### P1-6（B2）KeyRotationNeeded 缺陷
- **A. 最小修复**：`_synthesize_with_rotation` 捕获 `(DomainError, KeyRotationNeeded)`，与既有旋转路径合并（`rotate_on_failure` + `active_keys()` 检查），语义即代码注释所述"换新 key 原地续跑、不消耗预算"。取舍：几乎无副作用；补一条单测（克隆遇到 402 → 换 key 成功）。
- **B. 更广的防御**：对 provider 层所有非 DomainError 异常做收敛包装，避免同类"逃逸异常"再次出现。取舍：改动面大，建议仅在 provider 边界加兜底（记录 + 包装为稳定码）。

### P1-7（D1）故事板审核拒绝
- **A. 现状 + 优化文案**：恢复按钮旁展示上次拒绝原因与建议（当前已有 `failure_code`）。取舍：零风险。
- **B. 自动重试前做提示词微调**：越权（自动改创意），不建议。
- **C. N 次拒绝后升级提示**："该组连续 N 次被审核拒绝，请人工修改该组镜头描述后再重试"。取舍：需新增计数展示；符合"不自动改创意"边界。

### P1-8（B1）EL 号池
- **A. 现状**（手动充值/换 key + 恢复）符合 §23.6 已确认决策，建议补：号池页置顶提示"有项目因号池耗尽暂停：XXX"。取舍：产品文案层，低成本。
- **B. 自动恢复探测**（充值后自动解除暂停）：与 §23.6"由用户手动恢复"冲突，不建议改。

### P1-9（B3）网络故障消耗预算
- **A. 分类细化**：把"代理/连接不可达"归为"线路级临时故障"（不消耗对象预算，达到阈值后暂停为线路故障），或仍计预算但在页面上明确"网络故障为非对象错误"。取舍：触及 §15 预算分类，需文档同步 + 测试改造。
- **B. 保留现状 + 代理侧加固**：确保 mihomo 常驻（G1 重启策略）+ 节点健康检查失败告警。取舍：不改语义，靠运维兜底。
- 建议：先 B 后议 A。

### P1-10（B6）ARCHIVE_FAILED 死胡同
- **A. checkpoint 免 Provider 重归档**：允许"checkpoint 存在"的 ARCHIVE_FAILED 句子进入自动重投（调度层放行即可——`execute_batch` 的 checkpoint 分支天然不会再次调用 Provider）。取舍：需精细区分有/无 checkpoint；语义安全（无重复计费），是**性价比最高的一项**。
- **B. 新增专用 resolution**：允许对 ARCHIVE_FAILED 执行"重归档"人工动作。取舍：更显式，但要动 API 合同。

### P1-11（D3）故事板处理阶段无限重试
- **A. 补显式 RetryPolicy + 最大次数**，超限落 FAILED（需同时放开状态机 `DOWNLOADED/QC_PASSED/OBJECT_STORED → FAILED` 的出口）。取舍：状态机变更需谨慎（不得伪造成功）；新增迁移无关，纯代码。
- **B. 编排器兜底**：故事板组非终态超过 N 分钟 → 暂停（不依赖状态机改造）。取舍：改动集中在编排器，较独立；需与"慢 Provider"区分。

---

## 6. 人工介入入口总表（现状）

| 场景 | 入口 | 位置 |
|---|---|---|
| AUTO 任意暂停恢复（预算轮+1） | `POST /projects/{id}/auto-pipeline/resume` | `identity_project/router.py:600-619` |
| `director_validation_failed` 修正即续跑 | 第 07 步保存草稿（守卫：原因码匹配） | `auto_pipeline/service.py:110-121` |
| 配音句级恢复（OUTPUT_INVALID 等） | `POST /narration/recover`（resolution + evidence） | `api/narration_router.py:142-165` |
| 故事板重试/对账 | `RETRY`（批量重新生成）/ `RECONCILE`（仅 REMOTE_STATE_UNKNOWN） | `api/storyboards.py`；`repository.py:1081-1113` |
| 视频对账/取消/切换当前版本 | `/group-videos/attempts/{id}/reconcile|cancel|archive`、`/groups/{id}/video/current` | `video_generation/api.py:76-105` |
| 合成重试 | `RETRY_COMPOSITION` | `identity_project/service.py:44`；`media_router.py:195+` |
| 号池管理 | 号池管理页（增删/启停/刷新配额） | `api/elevenlabs_pool.py` |
| **无入口的卡点** | C1（需数据级/代码级）、D3、E1–E5（AUTO 运行期手动动作被剥离）、G1（SSH 手工） | 见 §3 各行 |

> 注意：AUTO 运行 `RUNNING` 期间，编排器会剥离阶段级手动动作（`identity_project/service.py:47-72`），暂停时恢复按钮出现——这是"静默停滞"为什么危险的关键设计交互。

---

## 7. 附录

### 7.1 关键常量速查

| 常量 | 值 | 位置 |
|---|---|---|
| 配音对象预算 / 退避 | 3 次 / 5,15,30s | `domain/narration.py:20-23` |
| 配音提交租约 | 1200s（20min） | `application/narration.py:65,350-354` |
| 导演单元租约 / 活动超时 | 20min / 20min（HTTP 900s） | `project_director.py:72`、`workflows/storyboards.py:30`、`config.py:71` |
| 故事板预算 / 处理租约 | 3 次 / 30min | `domain/storyboards.py:26`、`application/storyboards.py:562-570` |
| H3 停滞阈值 / 降频 | SUBMITTED 1800s / RUNNING 3600s / 300s | `config.py:126-128` |
| H3 提交预算 / 对账无果上限 | 3 / 3 | `orchestration.py:101-110` |
| 槽位租约 / 心跳新鲜度 | 2min / 30s | `sqlalchemy_repository.py:809`、`availability.py:17,81` |
| 合成自动重试 | 3 次 5/15/30s | `application/composition.py:23-34` |
| 编排巡检间隔 | 20s | `config.py:130` |
| 合成失联对账 | 60s | `workflows/worker.py:288-301` |

### 7.2 生产实测数据（2026-10-14 10:16 只读查询）

见 §0 摘要表；关键佐证：
- `auto_pipeline_runs`：`a5367080` PAUSED、`reason_code=director_preplan_failed`、`payload.failure_code=director_run_unresolved`、`director_failed_run_id=8902bd93-…`。
- `project_director_units`：全库唯一一条 `SUBMITTING` 单元 = 项目 `a5367080` / run `8902bd93` / 序号 120 / `project-director-unit-v5` / 创建于 10-13 12:20 —— 与 C1 的根因模型完全吻合。
- `storyboard_generations` FAILED 最近 10 条中 `output_moderation` 占 6 条（同一组 `b0b15b67` 连续 5 次）。
- `project_director_runs`：`remote_state_unknown_exhausted` 11 次（对照 director_timeout 事故记录 100% 复现）。

### 7.3 「待确认」清单

1. **H3 工作流档案**：新项目实际走 legacy 还是 candidate（durable）工作流取决于部署机 `.env` 的 allowlist（`h3_default_workflow_profile` 默认 `legacy`，`config.py:124`）；E2 的"不自动重提"仅对 durable 候选工作流成立（本次未读取部署机 `.env`，不确认当前档案）。
2. **Temporal SDK 默认 RetryPolicy**：故事板工作流"无限重试"结论基于 Temporal Python SDK 默认 `maximum_attempts=0`（无限）；未在离线环境复核 SDK 源码。
3. **`_storyboard_groups` 最新行选取**在 `created_at` 完全相同、靠 `id` 排序时的确定性（`adapters/persistence/auto_pipeline.py:394-401`）——理论边界，出现概率极低。
4. **ARCHIVE_FAILED 是否真的无恢复入口**：已通读 `application/narration.py` 全部 reconcile/重试路径与 `narration_router` 路由，**未找到**；但不排除我用例覆盖不全。
5. **E3 当前实际触发概率**：取决于部署机 `.env` 是否配置 GPU 探测端点（涉密未读取）。
6. **故事板 D3 的实际发生频率**：当前 4 条 `REMOTE_STATE_UNKNOWN` 均为 §13 历史保留，未观察到 D3 命中样本；属"低概率高风险"。
7. EL `provider_request_id` 列为空（回执在 attempt payload 的 `remote_id`）——UAT 已记录的实现偏差，非卡点，但影响对账 SQL 写法。

---

*报告生成方式：核心模块逐行阅读（auto_pipeline / narration / storyboards / video_generation / composition / worker / identity_project）+ 两个独立子代理交叉核对 + 部署机生产库只读 SQL 实证。所有代码行号以当前工作区为准。*

---

## 8. 修复状态追加（2026-10-14 11:35 CST）

- **C1（导演孤儿 SUBMITTING 单元死锁）已修复并上线**（镜像 `stuckpoints-fix-20261014` v2，`sha256:34d08d48…`；v1 `8ced423d…` 已由 v2 替换）：恢复/重跑创建前回收"终态运行 + 租约过期"的孤儿单元（SUBMITTING→FAILED/`worker_lost_after_call_started`；LEASED→INTENT_PERSISTED）。上线前只读预演：全库恰有 1 个单元（项目 `a5367080` / run `8902bd93` / 序号 120）命中回收谓词，活跃运行零影响。韩国项目待用户点击「恢复自动执行」完成真实路径验证（详见 `runtime/operations/stuckpoints_fix_20261014.md`）。
- 同批上线：故事板提示词安全替换（对应本文 D1/问题1）、短音频归一化增益窗口修复（配音 `processing_failed` 族/问题3）。
- 本文其余卡点状态不变（B2 `KeyRotationNeeded`、B6 `ARCHIVE_FAILED`、E1–E3/E5、G1 等仍为待决策项）。
