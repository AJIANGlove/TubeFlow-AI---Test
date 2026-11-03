# 状态、存储与异常处理

> 本文展开 [流程说明](./PROJECT_VIDEO_E2E_FLOW_20261014.md) 中的状态和异常。除“未找到”与运行值边界外，均为【已确认】的静态代码事实；未将规划目标或测试替身行为混入生产主链。

## 1. AUTO 顶层状态机与阶段书签

定义：`backend/src/tubeflow_test/auto_pipeline/domain.py:17-57,84-166`。

```text
RUNNING ──预算耗尽/既定失败/超长句/结构冲突──> PAUSED
RUNNING ──前置门禁满足且存在 current 成片────> COMPLETED
PAUSED ──用户恢复，授权轮次+1───────────────> RUNNING
COMPLETED ──无后续迁移
```

- `payload["phase"]` 是展示/书签，可取 `narration / director_preplan / director_materialization / storyboard_drafts / storyboard / video / composition / completed`。
- `AutoPipelineService.advance()` 每次从数据库重读权威 facts，不能只凭 phase 判断完成。
- AUTO 没有独立 `FAILED` 运行状态；自动链无法继续时主要收口为 `PAUSED`。
- sweeper 只列出 `RUNNING`：`auto_pipeline/service.py:130-136` 与 `adapters/persistence/auto_pipeline.py` 的运行查询；PAUSED 需要恢复动作。

## 2. 各阶段状态机

### 2.1 配音

**attempt 状态**（`domain/narration.py:80-90`）：

```text
INTENT_RECORDED
SUBMISSION_STARTED
REMOTE_STATE_UNKNOWN
SAFE_TO_RETRY
PROVIDER_REJECTED
OUTPUT_INVALID
ARCHIVE_FAILED
CANCELLED
COMPLETED
```

**job 状态**：`PENDING / DISPATCHED / COMPLETED / PARTIAL_FAILED / FAILED`，持久化切换见 `adapters/persistence/narration.py:1099-1175`。

- `DISPATCHED` 只表示 Temporal 已受理，不表示配音成功。
- 全部句子成功后才生成权威时间轴与分组；部分失败不能被当成完成。
- AUTO 可自动重投的集合：`SAFE_TO_RETRY / REMOTE_STATE_UNKNOWN / OUTPUT_INVALID`。
- `PROVIDER_REJECTED`、`ARCHIVE_FAILED` 不进入普通 AUTO 重投集合，转为项目阻断原因。

### 2.2 前置导演

**run**：`INTENT_PERSISTED / RUNNING / REMOTE_STATE_UNKNOWN / SUCCEEDED / FAILED`。

**unit**：`INTENT_PERSISTED / LEASED / SUBMITTING / REMOTE_STATE_UNKNOWN / SUCCEEDED / FAILED`。

定义：`domain/project_director.py:18-44`。

- unit 认领与租约用于避免并发重复执行；`SUBMITTING` 表示调用已开始，不等价于远端已受理。
- 全部单元成功后才 `_finalize()` 形成当前前置修订。
- run `FAILED` 由 AUTO 映射为 `director_preplan_failed`；恢复会创建新 run，并按代码规则复用可复用结果或整轮重跑。

### 2.3 故事板

主状态定义：`domain/storyboards.py:67-77`；精确允许迁移表：`adapters/persistence/storyboards/repository.py:2179-2222`。

```text
INTENT_PERSISTED → SUBMITTING → SUBMITTED/RUNNING
  → DOWNLOADED → QC_PASSED → OBJECT_STORED → SUCCEEDED

SUBMITTING/SUBMITTED/RUNNING → REMOTE_STATE_UNKNOWN
允许的失败点 → FAILED
```

- 成功与失败都是终态；`REMOTE_STATE_UNKNOWN` 由 recovery workflow 继续对账。
- `complete_and_switch_current()` 只有在成功版本号高于当前指针时自动切换，并受活跃 H3 attempt 保护：`adapters/persistence/storyboards/repository.py:1445-1559`。
- 人工切换允许选择同组其他合法成功版本，旧资产不删除：`adapters/persistence/storyboards/repository.py:1561-1625`。

### 2.4 H3 组视频

主要 stage/archive 状态和允许迁移：`video_generation/orchestration.py:60-83,315-382`。

```text
CREATED → STAGING → SUBMITTING → SUBMITTED/RUNNING → SUCCEEDED → ARCHIVED
STAGING → NOT_SUBMITTED → STAGING
SUBMITTING → REMOTE_STATE_UNKNOWN
SUBMITTED → REMOTE_STATE_UNKNOWN
RUNNING → REMOTE_STATE_UNKNOWN（仅 durable candidate 补充允许）
各允许失败点 → FAILED / CANCELLED

SUCCEEDED 后还可能有 QC_FAILED；归档另有 STORED/COMPLETE 等 archive_stage。
```

- 首个 `ARCHIVED + COMPLETE` 成功版本只在组内没有 current 时自动设为 current：`video_generation/orchestration.py:1581-1585`。
- 原始 MP4 在 QC 前已写入不可变归档；QC_FAILED 不等于删除原始产物。
- 公开 API 状态会映射为 `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/RECOVERY_REQUIRED`，映射见 `video_generation/api.py:321-383`。

### 2.5 合成

**outbox**：`PENDING / DISPATCHED / SUPERSEDED`，另有 lease、workflow_generation、attempt、next_attempt_at 等字段。

**job**：`RUNNING / SUCCEEDED / FAILED`。

**版本**：`composition_versions.current=false/true`；每项目至多一条 current。

代码：`adapters/persistence/media_repository.py:599-794,813-993,995-1172,1977-2213`。

- `DISPATCHED` 不是合成完成；成功事实在 job/version，不在单独的 outbox `SUCCEEDED` 状态。
- 新候选激活时首先 `current=false`；AUTO 只在成功后仍无 current 时自动选择：`application/composition.py:188-194`。
- AUTO 完成时把项目非终态合成 outbox 收口为 `SUPERSEDED`，但不取消已经派发的 Temporal workflow：`adapters/persistence/media_repository.py:2106-2133`、`application/composition_outbox.py:50-57`。

## 3. 失败分类与重试：必须区分不同层次

### 3.1 总表

| 环节 | AUTO 当前代码的自动处理 | 预算/退避 | 不自动重试或特别出口 |
| --- | --- | --- | --- |
| 拆句 | 临时错误、指定响应错误、无损拆分进入修复重试 | 每风险句最多3次；5/15/30s；429 可取 Retry-After | 最终失败在冻结前终止提交 |
| 配音 workflow/activity | 每批 pending 循环、activity RetryPolicy | 每批最多3轮；activity 最多100次；与 Provider 句级预算是不同层 | `NarrationIntentConflictError` 不做 activity 重试 |
| 配音 Provider 对象 | SAFE/UNKNOWN/OUTPUT_INVALID 派生新重试 job | 每句每预算轮最多3次；5/15/30s | PROVIDER_REJECTED/ARCHIVE_FAILED 不走普通自动重投；EL 全池不可用按独立阻断码 |
| 导演单元 | 临时错误、结构化输出错误及 AUTO 未知分支 | 每单元最多3次；5/15/30s | AUTO run FAILED 后暂停；MANUAL 另有整轮 2×unit 限制 |
| 视频 Prompt 编译 | 输出合同失败进入 repair；临时错误按内部 attempts 退避 | AUTO `max_repairs=2`，MANUAL=1；临时退避 5/15s | 不回退旧故事板 Prompt；不能把 attempts 简单宣称为整个编译严格3次 HTTP 总上限 |
| 故事板 | connect 未送达/429 可自动重提；未知态可对账后按 AUTO 分支重提 | 每组链最多3次提交；5/15/30s | 明确 Provider FAILED 不自动重生；下载/QC/对象写入异常未找到自动创建新版本路径 |
| H3 对象提交/重提 | 明确拒绝、未知/对账、QC_FAILED、STALLED 各有 AUTO 分支 | 通常每对象/预算轮3次，5/15/30s；停滞降频默认300s | candidate/durable 对未知、404、取消采用更严格证据规则；H3 重提复用已编译 Prompt |
| H3 recovery activity | Temporal 对 activity 异常按独立策略重试 | 最多5次；2秒起步指数退避、最大30秒；10min start-to-close、3min heartbeat | `VideoRuleViolation` 不可重试；与对象提交预算是不同层（`video_generation/temporal.py:127-138`） |
| 合成 activity | Temporal 对非 DomainError 的活动异常重试 | 最多3次；30min start-to-close；30s heartbeat | DomainError 被标记 non_retryable |
| 合成应用层 | 临时媒体命令错误及非 DomainError 可 AUTO 重排 | job.attempt<3；5/15/30s；workflow_generation+1 | 既定 DomainError 或预算耗尽后 AUTO PAUSED `composition_failed` |
| outbox 派发 | 临时/不确定派发错误按各 outbox 规则重新排程 | 多为指数退避；具体上限因 outbox 而异 | H3 确定性派发错误落 `DEAD_LETTER`，不按 UNKNOWN 指数退避重试；不等于 Provider 重试 |

### 3.2 拆句与导演 LLM 的 HTTP 分类

当前适配器对 401/403 按永久拒绝；400/404/409/410/422 进入可重试分支；429/5xx 可重试，429 支持 `Retry-After`。位置：

- 拆句：`adapters/deepseek_sentence_split.py:71-90`。
- 导演：`adapters/director_model.py:61-98` 及应用层 `_retry_or_terminal()`。
- 视频 Prompt：`adapters/deepseek_video_prompt.py:64-78`、`video_prompt/compiler.py:114-167`。

这些分类描述本侧代码，不断言 Provider 对相同输入最终会成功。

### 3.3 故事板异常不是一类错误

- **明确提交拒绝**：落 FAILED，不因 AUTO 直接换版本重试：`application/storyboards.py:527-536`。
- **connect 未送达/429**：AUTO 在预算内创建后继 generation：`:838-890`。
- **发送后读超时/5xx等未知**：进入 REMOTE_STATE_UNKNOWN 与 recovery：`:551-560,581-637,829-836`。
- **Provider 明确失败**：保存 FAILED，不自动创建新版本：`:656-667`。
- **下载/QC/对象存储异常**：记录 failure_stage/failure_code 并抛 `storyboard_processing_failed`；当前主链未找到自动创建新版本的路径：`:687-703,719-776`。

### 3.4 H3 停滞与候选工作流边界

- 默认停滞阈值：accepted 1800s、running 3600s，停滞后轮询默认 300s：`config.py:126-128`。
- AUTO 停滞时根据 GPU 探测决定继续等待或取消重提：`video_generation/orchestration.py:963-1035`；探测失败不能被当成“GPU 空闲”。
- 候选 workflow 的身份是 `h3.tubeflow.storyboard-to-video`；未知、查询失败或404 不能自动当作未受理证据。
- QC_FAILED 可自动生成替代 attempt，旧 QC 证据保留：`video_generation/orchestration.py:1469-1479`。
- 上述均为代码分支；实际项目使用 legacy 还是 candidate 由持久化路由与部署配置决定，【运行值未确认】。

### 3.5 合成失联对账

- relay 每秒尝试派发，每 60 秒执行一次失联对账：`workflows/worker.py:280-302`。
- 默认 DISPATCHED 失联阈值 2100 秒：`config.py:98-100`。
- 对账依据 PostgreSQL 的 outbox/job 时间戳；RUNNING job 可标为 `FAILED/composition_workflow_lost`，AUTO 在预算内安排新代际：`adapters/persistence/media_repository.py:1982-2104`。
- 该路径未调用 Temporal 查询远端详情，不能把它描述为外部 callback。

## 4. AUTO 暂停原因与恢复入口

### 4.1 稳定原因

| 来源 | 暂停原因示例 | 代码 |
| --- | --- | --- |
| 配音 | 沿用稳定 blocking reason，如 `narration_submission_budget_exhausted`、号池/认证阻断码 | `auto_pipeline/service.py:145-161`、`domain/narration.py:367-402` |
| 实测超长 | `oversized_sentence` | `auto_pipeline/service.py:163-181` |
| 导演 run 失败 | `director_preplan_failed` | `auto_pipeline/service.py:183-186,384-415` |
| 草稿/候选校验 | `director_validation_failed` | `auto_pipeline/service.py:195-245` |
| 故事板终态失败 | `storyboard_failed:<failure_code>` | `auto_pipeline/service.py:247-257`、`auto_pipeline/domain.py:35-38` |
| 视频终态/意图失败 | `video_failed:<error_code>`、`video_failed:intent_rejected` | `auto_pipeline/service.py:285-325` |
| 合成失败 | `composition_failed` | `auto_pipeline/service.py:338-344` |

### 4.2 恢复动作

- 通用恢复：`POST /identity/projects/{project_id}/auto-pipeline/resume`，`identity_project/router.py:600-619`。
- 只允许 PAUSED 恢复；数据库事务把项目预算轮与 run 授权轮递增，清原因并改回 RUNNING：`adapters/persistence/auto_pipeline.py:183-216`。
- 随后立即 `kick()`：`auto_pipeline/service.py:104-108`。
- 导演草稿修复：保存接口只在暂停原因为 `director_validation_failed` 时调用原因码守卫恢复：`api/storyboards.py:225-234`、`auto_pipeline/service.py:110-121`。
- 合成人工重试：`POST /projects/{project_id}/composition/retry`，`api/media_router.py:207-217`；仓储规则在 `adapters/persistence/media_repository.py:694-794`。

## 5. 结果存储与版本边界

| 产物 | PostgreSQL 中的权威事实 | 媒体字节/临时文件 |
| --- | --- | --- |
| 项目/快照 | Project、snapshot 字段、AUTO run | 无 |
| 拆句/导演 | 拆分血缘、run/unit、回执、不可变修订、canonical 哈希、物化候选与来源血缘 | 无外部媒体 |
| 配音 | narration attempt/job、权威句子、QC、实测帧数/时长、组与成员 | raw 与 normalized WAV 入 MinIO |
| 故事板 | generation、prompt/镜头/角色/参数快照、版本与 current 指针 | 整图入 MinIO；下载临时文件清理 |
| H3 | attempt、request/input snapshot、节点/槽位、QC/回执、归档血缘、current 指针 | 原始 MP4 入 MinIO；staged 引用按流程清理 |
| 合成 | input revision、outbox、job、manifest、media_assets、composition_versions/current | MP4+SRT 入 MinIO；本地 candidate 最终删除 |
| 导出 | 无持久 export job/outbox | 本地临时 ZIP，响应结束或中断清理；未写入 MinIO |

对象存储写入与 SHA 元数据：`adapters/object_storage.py:63-84`。PostgreSQL 不保存 MP4/SRT 本体，MinIO 也不承担 current 状态机。

## 6. 状态更新与通知

### 6.1 内部推进

- `auto_kick` 是 Worker 内函数调用，不是 HTTP：`workflows/worker.py:224-230`。
- `AutoPipelineService.kick()` 调 `advance()`，异常仅记日志：`auto_pipeline/service.py:123-128`。
- 默认20秒 `sweep_once()` 是 RUNNING 运行的兜底；它不是 PAUSED 自动恢复器。

### 6.2 页面状态

- `workflow-summary` 在每次 GET 时从权威表投影：`identity_project/service.py:645-727`。
- AUTO RUNNING 时会从公开动作中剥离多数阶段级手动动作；PAUSED 追加 `RESUME_AUTO_PIPELINE`；COMPLETED 仅对完成前残留的**合成失败提示**有专门展示收口（`identity_project/service.py:709-726`）。
- 前端使用 HTTP 轮询，不是推送：AUTO 聚合页 `app.js:5050-5098`。

### 6.3 【未找到相关实现】

在当前仓库生产代码与前端中未找到：

1. 对外业务 HTTP webhook 或第三方完成通知 endpoint；
2. SSE/`EventSource`、WebSocket 推送；
3. SMTP/邮件、Slack/钉钉/企业微信/飞书等通知；
4. AUTO 完成后自动导出 ZIP；
5. AUTO 自动生成封面；
6. 独立的 AUTO Temporal workflow 或 auto-pipeline outbox；
7. 从主题/brief 自动生成完整项目脚本的主链；
8. 当前生产/API/AUTO 主链调用旧 `DirectorGenerationService.create_intent()` 的入口（旧 outbox 排水设施仍保留）。

## 7. 事实与推断边界

- 本文的状态枚举、迁移、重试常量、API、存储位置都来自当前代码，属于【已确认】。
- “某分支被触发后必然耗时多久、Provider 是否最终恢复、部署当前选哪个 profile”均未做运行时验证，不作推断。
- 对“未找到自动恢复/通知/导出”的表述仅限当前仓库检索结果；没有用设想中的替代流程补齐。
