# 三个 AUTO 项目导演阶段停滞与有限恢复记录（2026-11-05）

> 本文档由 `runtime/operations/director-stall-monitor-20261105/monitor.py` 自动生成。
> `<!-- BEGIN/END AUTO-GENERATED -->` 之间为受管区，脚本会重写；之外为人工批注，脚本不改动。

- 最近一次结构性更新：2026-11-05T11:53:37.946Z / 2026-11-05 19:53:37 CST（仅当事件/恢复/阶段变化时才重写本区）
- 数据库时钟（采集时）：2026-11-05T11:53:59.744Z
- 监控脚本：`runtime/operations/director-stall-monitor-20261105/monitor.py`
- 采样：常驻进程 5 分钟一轮；会话结束后由每小时兜底任务接力（精度降为 60 分钟）

<!-- BEGIN AUTO-GENERATED -->

## 0. 一句话结论

截至 2026-11-05T11:53:37.946Z / 2026-11-05 19:53:37 CST：3 个监控对象中，2 个已跳出导演阶段，1 个停在导演阶段且已暂停，0 个需要人工关注；累计执行恢复 3 次，累计记录中断事件 3 个。

## 1. 监控对象与授权

### 1.1 项目清单

| 项目 UUID | 标题 | 基线 round | 基线 phase | 当前 state | 当前 phase | 已用恢复 | 分类 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ff67c5e9-1db8-49c6-8f49-c96d9d0e140e` | Every Level of a Medieval Merchant | 4 | director_preplan | RUNNING | video | 0/3 | EXITED |
| `7cf2e79d-4d9d-48fa-8639-dde4100b6469` | Why Everyone Keeps Stealing Ukraine's Soil | 1 | storyboard | PAUSED | video | 0/3 | EXITED |
| `f881122d-1846-4d18-8f8b-4b89b0cbc0db` | You're a Crew Member on the Mary Celeste | 4 | director_preplan | PAUSED | director_preplan | 3/3 | PAUSED_DIRECTOR |

### 1.2 授权范围

- 每个项目自首次基线起最多 **3 次**恢复，判据为 `projects.auto_budget_round - 基线 round < 3`。
- 恢复通道：部署机 worker 容器内调用 `SqlAutoPipelineRunRepository.resume_paused`，与 HTTP API 同语义（round+1、清 reason、置 RUNNING）。
- 恢复后由 worker `sweep_once`（20 秒周期）创建新一轮导演 run，幂等键 `auto-pipeline-director:<project>:r<round>`。
- 不做：改表、改代码、改 .env、重启容器、重建镜像、force push、操作其他项目。

### 1.3 监控机制与精度

- 主监控：常驻进程每 300 秒一轮，单飞锁防并发。
- 兜底：每小时定时任务，共用同一把锁与状态文件。
- 精度声明：5 分钟轮询无法捕获生命周期短于 5 分钟的暂停；会话结束后精度降为 60 分钟。
  `authorization_round` 的跳变可用于证明「存在未观测到的恢复」，但无法还原其时刻。

## 2. 判定口径

### 2.1 阶段边界

- 导演阶段：`director_preplan` / `director_materialization` / `storyboard_drafts`（与前端口径一致）。
- 跳出导演：`storyboard` / `video` / `composition` / `completed`。

### 2.2 事件类型

- `PAUSED_DIRECTOR`：auto 已 PAUSED 且 phase 属于导演阶段 → 可恢复。
- `PAUSED_OUT_OF_SCOPE`：PAUSED 但 phase 不是导演阶段 → 不恢复、不耗额度。
- `SILENT_STALL_UNKNOWN`：auto 仍 RUNNING、最新导演 run 为 `REMOTE_STATE_UNKNOWN` 且静默 ≥30 分钟 → 只报警，恢复接口对其无效。
- `SWEEP_NOT_PAUSING`：auto 仍 RUNNING 但导演 run 已 FAILED，说明 worker 巡检未把状态打成 PAUSED。
- `EXITED`：已跳出导演阶段，停止监控。

### 2.3 恢复额度

`已用 = projects.auto_budget_round - 基线 round`；`剩余 = 3 - 已用`。
该列只在 `resume_paused` 中 +1，永不重置，因此可以跨进程、跨会话作为权威计数。

### 2.4 幂等与去重

事件指纹 = sha256(`project | 类型 | auto_run.id | authorization_round | reason_code | phase | director_failed_run_id | 最新 run.id | 最新 run.status | 最新 run.failure_code | 单元状态分布`) 前 16 位。
同一指纹只更新 `last_seen` 与观测次数，不重复建事件；`authorization_round` 变化即视为新一轮尝试。

### 2.5 时间口径

数据库原始值一律 UTC（ISO-8601，`Z` 结尾），渲染时同时给出 CST（UTC+8）。
唯一能给出精确暂停时刻的字段是 `auto_pipeline_runs.payload->>'paused_at_ms'`。

## 3. 中断与失败记录

### 3.1 监控启动前的失败（来自 `project_director_runs` 回溯）

> 这些 run 失败于监控启动之前，库里只保留 run 行与其 `created_at`。
> **无法**给出失败发生的精确时刻，只能给出「该 run 创建于 …」。

**Every Level of a Medieval Merchant**（`ff67c5e9-1db8-49c6-8f49-c96d9d0e140e`）

| run id | trigger | status | failure_code | total_units | 单元状态 | run 创建时间 |
| --- | --- | --- | --- | --- | --- | --- |
| `29e49f83` | NARRATION_CONFIRMATION | FAILED | director_output_sentence_coverage_invalid | 56 | {"FAILED": 1, "INTENT_PERSISTED": 32, "SUCCEEDED": 23} | 2026-11-05T09:03:58.873Z |
| `964a36b4` | AUTO_RESUME | FAILED | director_output_adjacent_diversity_insufficient | 56 | {"FAILED": 1, "INTENT_PERSISTED": 29, "SUCCEEDED": 26} | 2026-11-05T09:43:00.889Z |
| `c80ea8bb` | AUTO_RESUME | FAILED | director_output_adjacent_diversity_insufficient | 56 | {"FAILED": 1, "INTENT_PERSISTED": 20, "SUCCEEDED": 35} | 2026-11-05T10:07:12.275Z |

**You're a Crew Member on the Mary Celeste**（`f881122d-1846-4d18-8f8b-4b89b0cbc0db`）

| run id | trigger | status | failure_code | total_units | 单元状态 | run 创建时间 |
| --- | --- | --- | --- | --- | --- | --- |
| `6fae6a67` | NARRATION_CONFIRMATION | FAILED | director_output_adjacent_diversity_insufficient | 59 | {"FAILED": 1, "INTENT_PERSISTED": 49, "SUCCEEDED": 9} | 2026-11-05T09:02:44.402Z |
| `8697ee51` | AUTO_RESUME | FAILED | remote_state_unknown_exhausted | 59 | {"FAILED": 2, "INTENT_PERSISTED": 19, "SUCCEEDED": 38} | 2026-11-05T09:43:08.629Z |
| `9c2a58c3` | AUTO_RESUME | FAILED | director_output_adjacent_diversity_insufficient | 59 | {"FAILED": 2, "INTENT_PERSISTED": 19, "SUCCEEDED": 38} | 2026-11-05T10:33:20.030Z |
| `baf41aa2` | AUTO_RESUME | FAILED | director_output_adjacent_diversity_insufficient | 59 | {"FAILED": 1, "INTENT_PERSISTED": 18, "SUCCEEDED": 40} | 2026-11-05T10:47:53.601Z |

### 3.2 监控期间观测到的中断事件

#### E01 · You're a Crew Member on the Mary Celeste · 首次观测 2026-11-05T11:10:33.428Z · `PAUSED_DIRECTOR`

**可证事实（采集快照）**

| 字段 | 值 | 来源 |
| --- | --- | --- |
| auto_pipeline_runs.id | a3be1745-399c-4857-b6b8-ee4aeccd111a | auto_pipeline_runs |
| state / reason_code | PAUSED / director_preplan_failed | auto_pipeline_runs |
| authorization_round | 4 | auto_pipeline_runs |
| payload.phase | director_preplan | auto_pipeline_runs |
| payload.paused_at_ms | 1793875876654 | auto_pipeline_runs |
| payload.paused_at_ms（换算） | 2026-11-05T10:51:16Z | 同上 |
| payload.failure_code | director_output_adjacent_diversity_insufficient | auto_pipeline_runs |
| payload.director_failed_run_id | baf41aa2-a83f-4528-a4a4-c640d95bc35b | auto_pipeline_runs |
| auto_pipeline_runs.updated_at | 2026-11-05T10:51:16.654Z | auto_pipeline_runs |
| 最新导演 run.id | baf41aa2-a83f-4528-a4a4-c640d95bc35b | project_director_runs |
| 最新导演 run.status | FAILED | project_director_runs |
| 最新导演 run.failure_code | director_output_adjacent_diversity_insufficient | project_director_runs |
| total_units / auto_retries_used | 59 / 5 | project_director_runs |
| run 创建时间 | 2026-11-05T10:47:53.601Z | project_director_runs |
| 单元状态分布 | {"FAILED": 1, "INTENT_PERSISTED": 18, "SUCCEEDED": 40} | project_director_units |
| 单元失败码分布 | {"director_output_adjacent_diversity_insufficient": 2} | project_director_units |
| 项目 auto_budget_round | 4 | projects |
| 观测次数 | 3 | 本监控 |
| 首次/最近观测 | 2026-11-05T11:10:33.428Z / 2026-11-05T11:11:05.542Z | 本监控 |

**失败单元明细（最多 2 条，来自最新 run）**

| ordinal | kind | status | attempt | last_failure_code | diagnostics 摘要 |
| --- | --- | --- | --- | --- | --- |
| 40 | SHOT_PLAN | FAILED | 3 | director_output_adjacent_diversity_insufficient | {"adjacent_pair_after_order": 1, "changed_axes": 1, "previous_axes": "REVEAL_EVIDENCE\|OBJECT_OR_EVIDENCE\|SAME_MOMENT\|… |
| 42 | SHOT_PLAN | INTENT_PERSISTED | 1 | director_output_adjacent_diversity_insufficient | {"adjacent_pair_after_order": 2, "changed_axes": 1, "previous_axes": "PERFORM_ACTION\|PRIMARY_SUBJECT\|LATER\|SAME_LOCAT… |

**不可证 / 只能近似**

- 失败发生的精确时刻：`project_director_runs` / `project_director_units` 均无 `updated_at`，只能由暂停时刻反推「不晚于该时刻」。
- 每次重试的先后与耗时：单元无逐次尝试历史，`attempt_count` 只有累计值。
- 更早一次暂停的详情：`payload` 的暂停详情键会在下一次暂停时被清除。
- 远端服务的真实响应：`response_diagnostics_json` 仅保留最近一次。

**动作与结果**

| 项 | 值 |
| --- | --- |
| 动作 | resume |
| 时刻 | 2026-11-05T11:11:07.460Z |
| round 变化 | 4 → 5 |
| 调用结果 | accepted |
| 立即复核 | {"state": "RUNNING", "round": 5, "phase": "director_preplan", "paused_at_ms_present": false} |
| 新 run 验证 | {"state": "verified", "run_id": "924ee9a8-9e30-46a5-a6b8-5cd832007521", "verified_at_utc": "2026-11-05T11:11:49.123Z"} |

证据：`Pending issues/2026-11-05/evidence/auto_director_stall_monitor/incidents/E01_f881122d_2026-11-05T111033.428Z.json`

#### E02 · You're a Crew Member on the Mary Celeste · 首次观测 2026-11-05T11:28:11.203Z · `PAUSED_DIRECTOR`

**可证事实（采集快照）**

| 字段 | 值 | 来源 |
| --- | --- | --- |
| auto_pipeline_runs.id | a3be1745-399c-4857-b6b8-ee4aeccd111a | auto_pipeline_runs |
| state / reason_code | PAUSED / director_preplan_failed | auto_pipeline_runs |
| authorization_round | 5 | auto_pipeline_runs |
| payload.phase | director_preplan | auto_pipeline_runs |
| payload.paused_at_ms | 1793877381427 | auto_pipeline_runs |
| payload.paused_at_ms（换算） | 2026-11-05T11:16:21Z | 同上 |
| payload.failure_code | director_output_adjacent_diversity_insufficient | auto_pipeline_runs |
| payload.director_failed_run_id | 924ee9a8-9e30-46a5-a6b8-5cd832007521 | auto_pipeline_runs |
| auto_pipeline_runs.updated_at | 2026-11-05T11:16:21.427Z | auto_pipeline_runs |
| 最新导演 run.id | 924ee9a8-9e30-46a5-a6b8-5cd832007521 | project_director_runs |
| 最新导演 run.status | FAILED | project_director_runs |
| 最新导演 run.failure_code | director_output_adjacent_diversity_insufficient | project_director_runs |
| total_units / auto_retries_used | 59 / 4 | project_director_runs |
| run 创建时间 | 2026-11-05T11:11:50.056Z | project_director_runs |
| 单元状态分布 | {"FAILED": 1, "INTENT_PERSISTED": 14, "SUCCEEDED": 44} | project_director_units |
| 单元失败码分布 | {"director_output_adjacent_diversity_insufficient": 1} | project_director_units |
| 项目 auto_budget_round | 5 | projects |
| 观测次数 | 1 | 本监控 |
| 首次/最近观测 | 2026-11-05T11:28:11.203Z / 2026-11-05T11:28:11.203Z | 本监控 |

**失败单元明细（最多 1 条，来自最新 run）**

| ordinal | kind | status | attempt | last_failure_code | diagnostics 摘要 |
| --- | --- | --- | --- | --- | --- |
| 40 | SHOT_PLAN | FAILED | 3 | director_output_adjacent_diversity_insufficient | {"adjacent_pair_after_order": 1, "changed_axes": 1, "previous_axes": "REVEAL_EVIDENCE\|OBJECT_OR_EVIDENCE\|SAME_MOMENT\|… |

**不可证 / 只能近似**

- 失败发生的精确时刻：`project_director_runs` / `project_director_units` 均无 `updated_at`，只能由暂停时刻反推「不晚于该时刻」。
- 每次重试的先后与耗时：单元无逐次尝试历史，`attempt_count` 只有累计值。
- 更早一次暂停的详情：`payload` 的暂停详情键会在下一次暂停时被清除。
- 远端服务的真实响应：`response_diagnostics_json` 仅保留最近一次。

**动作与结果**

| 项 | 值 |
| --- | --- |
| 动作 | resume |
| 时刻 | 2026-11-05T11:28:13.394Z |
| round 变化 | 5 → 6 |
| 调用结果 | accepted |
| 立即复核 | {"state": "RUNNING", "round": 6, "phase": "director_preplan", "paused_at_ms_present": false} |
| 新 run 验证 | {"state": "verified", "run_id": "be6678d5-ae14-410c-a2ed-bccf121a9dfd", "verified_at_utc": "2026-11-05T11:33:19.549Z"} |

证据：`Pending issues/2026-11-05/evidence/auto_director_stall_monitor/incidents/E02_f881122d_2026-11-05T112811.203Z.json`

#### E03 · You're a Crew Member on the Mary Celeste · 首次观测 2026-11-05T11:43:25.180Z · `PAUSED_DIRECTOR`

**可证事实（采集快照）**

| 字段 | 值 | 来源 |
| --- | --- | --- |
| auto_pipeline_runs.id | a3be1745-399c-4857-b6b8-ee4aeccd111a | auto_pipeline_runs |
| state / reason_code | PAUSED / director_preplan_failed | auto_pipeline_runs |
| authorization_round | 6 | auto_pipeline_runs |
| payload.phase | director_preplan | auto_pipeline_runs |
| payload.paused_at_ms | 1793878281730 | auto_pipeline_runs |
| payload.paused_at_ms（换算） | 2026-11-05T11:31:21Z | 同上 |
| payload.failure_code | director_output_sentence_coverage_invalid | auto_pipeline_runs |
| payload.director_failed_run_id | be6678d5-ae14-410c-a2ed-bccf121a9dfd | auto_pipeline_runs |
| auto_pipeline_runs.updated_at | 2026-11-05T11:31:21.730Z | auto_pipeline_runs |
| 最新导演 run.id | be6678d5-ae14-410c-a2ed-bccf121a9dfd | project_director_runs |
| 最新导演 run.status | FAILED | project_director_runs |
| 最新导演 run.failure_code | director_output_sentence_coverage_invalid | project_director_runs |
| total_units / auto_retries_used | 59 / 3 | project_director_runs |
| run 创建时间 | 2026-11-05T11:28:47.368Z | project_director_runs |
| 单元状态分布 | {"FAILED": 1, "INTENT_PERSISTED": 12, "SUCCEEDED": 46} | project_director_units |
| 单元失败码分布 | {"director_output_sentence_coverage_invalid": 1} | project_director_units |
| 项目 auto_budget_round | 6 | projects |
| 观测次数 | 1 | 本监控 |
| 首次/最近观测 | 2026-11-05T11:43:25.180Z / 2026-11-05T11:43:25.180Z | 本监控 |

**失败单元明细（最多 1 条，来自最新 run）**

| ordinal | kind | status | attempt | last_failure_code | diagnostics 摘要 |
| --- | --- | --- | --- | --- | --- |
| 40 | SHOT_PLAN | FAILED | 3 | director_output_sentence_coverage_invalid | {} |

**不可证 / 只能近似**

- 失败发生的精确时刻：`project_director_runs` / `project_director_units` 均无 `updated_at`，只能由暂停时刻反推「不晚于该时刻」。
- 每次重试的先后与耗时：单元无逐次尝试历史，`attempt_count` 只有累计值。
- 更早一次暂停的详情：`payload` 的暂停详情键会在下一次暂停时被清除。
- 远端服务的真实响应：`response_diagnostics_json` 仅保留最近一次。

**动作与结果**

| 项 | 值 |
| --- | --- |
| 动作 | resume |
| 时刻 | 2026-11-05T11:43:27.154Z |
| round 变化 | 6 → 7 |
| 调用结果 | accepted |
| 立即复核 | {"state": "RUNNING", "round": 7, "phase": "director_preplan", "paused_at_ms_present": false} |
| 新 run 验证 | {"state": "verified", "run_id": "d449aa5f-0f7b-4508-bce7-374efcf5f9e7", "verified_at_utc": "2026-11-05T11:48:33.216Z"} |

证据：`Pending issues/2026-11-05/evidence/auto_director_stall_monitor/incidents/E03_f881122d_2026-11-05T114325.180Z.json`

## 4. 取证边界（可证 vs 不可证）

### 4.1 可证

- 每个导演 run 的存在、状态、失败码、创建时间、单元状态分布与失败码分布。
- 单元的累计尝试次数、最近一次失败码与安全诊断（无响应正文）。
- auto 运行的当前 state / reason_code / phase / authorization_round / payload 书签。
- 被监控观测到的暂停时刻（`payload.paused_at_ms`）。

### 4.2 不可证或只能近似

- `project_director_runs` 无 `updated_at` ⇒ 失败时刻不可知。
- `project_director_units` 无 `updated_at`、无逐次历史 ⇒ 无法重建重试序列。
- `auto_pipeline_runs` 只有一行活动记录，暂停详情会被下一次暂停清除 ⇒ 更早的暂停原因已丢失。
- 轮询间隔内的变化只能给区间；短于采样间隔的暂停可能完全漏采。
- 恢复后的 `payload.failure_code` 可能是上一次暂停的残留，不能当作当前事实。

## 5. 汇总

### 5.1 恢复次数

| 项目 | 基线 round | 当前 round | 已用 | 剩余 | 状态 | 最近一次恢复 |
| --- | --- | --- | --- | --- | --- | --- |
| Every Level of a Medieval Merchant | 4 | 4 | 0 | 3 | EXITED | — |
| Why Everyone Keeps Stealing Ukraine's Soil | 1 | 1 | 0 | 3 | EXITED | — |
| You're a Crew Member on the Mary Celeste | 4 | 7 | 3 | 0 | PAUSED_DIRECTOR | 2026-11-05T11:43:27.154Z |

### 5.2 失败码频次（最新 run 的单元级）

| failure_code | 次数 |
| --- | --- |
| director_output_adjacent_diversity_insufficient | 1 |

### 5.3 需要人工介入

- **You're a Crew Member on the Mary Celeste**：`PAUSED_DIRECTOR`，phase=`director_preplan`，剩余额度 0/3。

<!-- END AUTO-GENERATED -->

<!-- HUMAN-NOTES -->

（人工批注区，脚本不覆盖。）
